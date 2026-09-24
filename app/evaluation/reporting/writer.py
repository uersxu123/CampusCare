from __future__ import annotations

import json
import hashlib
import os
import uuid
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class EvaluationReportWriter:
    def __init__(self, output_dir: Path, run_id: str):
        self.run_dir = output_dir / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self._case_hashes: dict[tuple[str, int, str, str], str] = {}

    def write_json(self, name: str, payload: Any) -> Path:
        safe = validate_report_privacy(payload)
        path = self.run_dir / name
        self._write_atomic(path, json.dumps(safe, ensure_ascii=False, indent=2) + "\n")
        return path

    def write_cases(self, rows: list[Any]) -> Path:
        path = self.run_dir / "cases.jsonl"
        safe_rows = [validate_report_privacy(item) for item in rows]
        self._write_atomic(
            path,
            "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in safe_rows),
        )
        return path

    def append_case(self, row: Any, *, fsync: bool = True) -> bool:
        safe = validate_report_privacy(row)
        record = {
            "schemaVersion": 4,
            "recordType": "turn_outcome",
            "runId": self.run_id,
            "terminalStatus": "FAILED" if safe.get("error_code") else "COMPLETED",
            "recordedAt": datetime.now(UTC).isoformat(),
            **safe,
        }
        identity = (
            str(record.get("case_id") or record.get("caseId") or ""),
            int(record.get("turn_index") if record.get("turn_index") is not None else record.get("turnIndex") or 0),
            str(record.get("attemptId") or "1"),
            str(record.get("generationId") or "1"),
        )
        serialized = json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        digest = _outcome_digest(record)
        previous = self._case_hashes.get(identity)
        if previous == digest:
            return False
        if previous is not None:
            raise ValueError(f"outcome terminal conflict: {identity}")
        self._append_line(self.run_dir / "cases.jsonl", serialized, fsync=fsync)
        self._case_hashes[identity] = digest
        return True

    def append_journal(self, event: dict[str, Any], *, fsync: bool = False) -> Path:
        allowed = {
            "caseId", "turnIndex", "attemptId", "generationId", "phase", "status",
            "elapsedMs", "remainingMs", "errorCode", "coverage", "resourceCleanup",
            "providerCancellation", "started", "finished", "aborted", "notRun",
        }
        record = {
            "schemaVersion": 1,
            "recordType": "lifecycle_event",
            "runId": self.run_id,
            "recordedAt": datetime.now(UTC).isoformat(),
            **{key: _plain(value) for key, value in event.items() if key in allowed},
        }
        serialized = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        path = self.run_dir / "lifecycle.jsonl"
        self._append_line(path, serialized, fsync=fsync)
        return path

    def recover_cases(self) -> dict[str, Any]:
        path = self.run_dir / "cases.jsonl"
        if not path.exists():
            return {"records": [], "corruptTail": None, "validBytes": 0}
        content = path.read_bytes()
        records = []
        offset = 0
        corrupt_offset = None
        for raw_line in content.splitlines(keepends=True):
            if not raw_line.endswith((b"\n", b"\r\n")):
                corrupt_offset = offset
                break
            try:
                item = json.loads(raw_line.decode("utf-8"))
                if not isinstance(item, dict):
                    raise ValueError("JSONL record must be an object")
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                corrupt_offset = offset
                break
            records.append(item)
            offset += len(raw_line)
        sidecar = None
        if corrupt_offset is not None:
            tail = content[corrupt_offset:]
            sidecar = self.run_dir / f"cases.corrupt-tail.{uuid.uuid4().hex[:8]}.bin"
            with sidecar.open("xb") as stream:
                stream.write(tail)
                stream.flush()
                os.fsync(stream.fileno())
            self._write_atomic(path, content[:corrupt_offset].decode("utf-8"))
        self._case_hashes.clear()
        for item in records:
            identity = (
                str(item.get("case_id") or item.get("caseId") or ""),
                int(item.get("turn_index") if item.get("turn_index") is not None else item.get("turnIndex") or 0),
                str(item.get("attemptId") or "1"),
                str(item.get("generationId") or "1"),
            )
            digest = _outcome_digest(item)
            previous = self._case_hashes.get(identity)
            if previous is not None and previous != digest:
                raise ValueError(f"outcome terminal conflict during recovery: {identity}")
            self._case_hashes[identity] = digest
        return {
            "records": records,
            "corruptTail": str(sidecar) if sidecar else None,
            "validBytes": corrupt_offset if corrupt_offset is not None else len(content),
        }

    @staticmethod
    def _append_line(path: Path, serialized: str, *, fsync: bool) -> None:
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(serialized + "\n")
            stream.flush()
            if fsync:
                os.fsync(stream.fileno())

    @staticmethod
    def _write_atomic(path: Path, content: str) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()


def _outcome_digest(record: dict[str, Any]) -> str:
    # 接收时间属于落盘元数据，不改变同一执行结果的身份。
    stable = {key: value for key, value in record.items() if key != "recordedAt"}
    serialized = json.dumps(stable, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def validate_report_privacy(payload: Any) -> Any:
    return _plain(payload)


def summary_document(*, run_id: str, suite: str, profile: str, passed: bool, **sections: Any) -> dict:
    return {
        "schemaVersion": 3,
        "runId": run_id,
        "createdAt": datetime.now(UTC).isoformat(),
        "suite": suite,
        "profile": profile,
        "passed": passed,
        **sections,
    }


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value
