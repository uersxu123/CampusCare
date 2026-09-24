"""按需工具原文存储与受控读取。

The default store is process-local for development/tests.  A SQLAlchemy
session can be supplied to persist records; persistence is explicit so small
results remain memory-only.
"""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
import base64
import logging
from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any


_scope: ContextVar[dict[str, str]] = ContextVar("tool_result_scope", default={})
logger = logging.getLogger(__name__)


@contextmanager
def bind_tool_result_scope(*, user_id: str, session_id: str, turn_id: str = ""):
    token = _scope.set({
        "user_id": str(user_id),
        "session_id": str(session_id),
        "turn_id": str(turn_id),
    })
    try:
        yield
    finally:
        _scope.reset(token)


@dataclass(frozen=True)
class ToolExecutionRecord:
    execution_id: str
    user_id: str = ""
    session_id: str = ""
    turn_id: str = ""
    work_item_id: str = ""
    agent_run_id: str = ""
    agent_name: str = ""
    tool_call_id: str = ""
    attempt: int = 1
    tool_name: str = ""
    tool_version: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    wire_result: Any = None
    normalized_result: Any = None
    raw_hash: str = ""
    payload_bytes: int = 0
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    valid_until: datetime | None = None
    corpus_signature: str = ""
    index_signature: str = ""
    transport_status: str = "OK"
    business_status: str = "OK"
    quality_status: str = ""
    error_code: str = ""
    cached_from_execution_id: str = ""
    persist_reason: str = ""


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class ToolResultStore:
    def __init__(self, session_factory=None, *, namespace: str = "default", ttl_seconds: int = 86400,
                 redis_client=None, read_max_tokens: int = 1536):
        self.session_factory = session_factory
        self.namespace = namespace
        self.ttl_seconds = max(1, int(ttl_seconds))
        self._records: dict[str, ToolExecutionRecord] = {}
        self._lock = threading.RLock()
        self.redis = redis_client
        self.read_max_tokens = max(512, int(read_max_tokens))

    def capture(self, payload: Any, **metadata: Any) -> tuple[str, Any]:
        execution_id = str(metadata.pop("execution_id", "exec_" + uuid.uuid4().hex))
        return execution_id, payload

    def persist(self, execution_id: str, payload: Any, *, user_id: str = "", session_id: str = "", persist_reason: str = "", **metadata: Any) -> ToolExecutionRecord:
        scope = _scope.get()
        user_id = str(user_id or scope.get("user_id") or "")
        session_id = str(session_id or scope.get("session_id") or "")
        if not metadata.get("turn_id") and scope.get("turn_id"):
            metadata["turn_id"] = scope["turn_id"]
        raw_hash = canonical_hash(payload)
        with self._lock:
            existing = self._records.get(execution_id)
            if existing is not None:
                if existing.user_id != user_id or existing.session_id != session_id:
                    raise ValueError("execution_id 不属于当前会话")
                if existing.raw_hash != raw_hash:
                    raise ValueError("execution_id 已绑定不同原文")
                return existing
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
            record = ToolExecutionRecord(
                execution_id=execution_id, user_id=str(user_id), session_id=str(session_id),
                wire_result=deepcopy(payload), normalized_result=deepcopy(payload), raw_hash=raw_hash,
                payload_bytes=len(encoded), persist_reason=persist_reason,
                valid_until=datetime.now(UTC) + timedelta(seconds=self.ttl_seconds), **metadata,
            )
            if self.session_factory is not None:
                self._persist_database(record)
            self._records[execution_id] = record
            self._cache_record(record)
            return record

    def _cache_record(self, record):
        if self.redis is None:
            return
        try:
            ttl = min(3600, max(1, int((record.valid_until - datetime.now(UTC)).total_seconds()))) if record.valid_until else 3600
            self.redis.setex(f"{self.namespace}:tool-result:{record.execution_id}", ttl,
                            json.dumps(asdict(record), ensure_ascii=False, default=lambda x: x.isoformat()))
        except Exception:
            logger.warning("工具原文 Redis 缓存写入失败，将从数据库回读")

    def _read_cache(self, execution_id):
        if self.redis is None:
            return None

        try:
            raw = self.redis.get(f"{self.namespace}:tool-result:{execution_id}")
            if not raw:
                return None
            value = json.loads(raw)
            if value.get("execution_id") != execution_id:
                return None
            for key in ("observed_at", "valid_until"):
                if value.get(key):
                    value[key] = datetime.fromisoformat(value[key])
            record = ToolExecutionRecord(**value)
            return record if canonical_hash(record.normalized_result) == record.raw_hash else None
        except Exception:
            logger.warning("工具原文 Redis 缓存读取失败，将从数据库回读")
            return None

    def save_compaction_view(self, payload, goal):
        if self.session_factory is None:
            return
        from app.models.entities import ToolResultViewEntity
        from app.services.context_builder import estimate_tokens
        rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self.session_factory() as db:
            db.add(ToolResultViewEntity(view_id="view_" + uuid.uuid4().hex,
                execution_id=payload["executionId"], raw_hash=payload["rawHash"], goal_hash=canonical_hash(goal),
                audience="runtime", mode="LLM_SUMMARY", summary_prompt_version="context_summary_v1",
                view_json=rendered, quality_status="QUOTE_VALIDATED", token_estimate=estimate_tokens(rendered)))
            db.commit()

    def save_context_manifest(self, event):
        if self.session_factory is None:
            return
        from app.models.entities import ModelContextManifestEntity
        with self.session_factory() as db:
            db.add(ModelContextManifestEntity(request_id="ctx_" + uuid.uuid4().hex,
                turn_id=_scope.get().get("turn_id", ""), manifest_json=json.dumps(event, ensure_ascii=False)))
            db.commit()
    def _persist_database(self, record: ToolExecutionRecord) -> None:
        from app.models.entities import ToolExecutionRecordEntity
        db = self.session_factory()
        try:
            existing = db.query(ToolExecutionRecordEntity).filter_by(execution_id=record.execution_id).one_or_none()
            if existing is not None:
                if (existing.raw_hash != record.raw_hash or existing.user_id != record.user_id
                        or existing.session_id != record.session_id):
                    raise ValueError("execution_id 已绑定不同数据库原文")
                return
            serialized = json.dumps(record.normalized_result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
            db.add(ToolExecutionRecordEntity(
                execution_id=record.execution_id, user_id=record.user_id, session_id=record.session_id,
                turn_id=record.turn_id, work_item_id=record.work_item_id, agent_run_id=record.agent_run_id,
                agent_name=record.agent_name, tool_call_id=record.tool_call_id, attempt=record.attempt,
                tool_name=record.tool_name, tool_version=record.tool_version,
                arguments_json=json.dumps(record.arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                wire_result_json=serialized, normalized_result_json=serialized, raw_hash=record.raw_hash,
                payload_bytes=record.payload_bytes, observed_at=record.observed_at.replace(tzinfo=None),
                valid_until=record.valid_until.replace(tzinfo=None) if record.valid_until else None,
                corpus_signature=record.corpus_signature, index_signature=record.index_signature,
                transport_status=record.transport_status, business_status=record.business_status,
                quality_status=record.quality_status, error_code=record.error_code,
                cached_from_execution_id=record.cached_from_execution_id, persist_reason=record.persist_reason,
            ))
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def read(self, execution_id: str, *, user_id: str = "", session_id: str = "", evidence_ids: list[str] | None = None,
             cursor: str | None = None) -> dict[str, Any]:
        scope = _scope.get()
        user_id = str(user_id or scope.get("user_id") or "")
        session_id = str(session_id or scope.get("session_id") or "")
        with self._lock:
            record = self._records.get(execution_id)
        if record is None:
            record = self._read_cache(execution_id)
        if record is None and self.session_factory is not None:
            record = self._read_database(execution_id)
            if record is not None:
                self._cache_record(record)
        if (
            record is None
            or record.user_id != user_id
            or record.session_id != session_id
        ):
            return {"status": "NOT_FOUND_OR_NOT_AUTHORIZED", "executionId": execution_id, "excerpts": []}
        if record.valid_until and record.valid_until < datetime.now(UTC):
            return {"status": "SOURCE_EXPIRED", "executionId": execution_id, "excerpts": []}
        else:
            freshness = "CURRENT"
        data = record.normalized_result
        if canonical_hash(data) != record.raw_hash:
            return {"status": "HASH_MISMATCH", "executionId": execution_id, "excerpts": []}
        excerpts = _extract_excerpts(data, evidence_ids or [])
        found = {item["evidenceId"] for item in excerpts}
        if not excerpts or (evidence_ids and not set(evidence_ids).issubset(found)):
            return {"status": "EVIDENCE_NOT_FOUND", "executionId": execution_id, "excerpts": []}
        response = {"status": "OK", "executionId": execution_id, "rawHash": record.raw_hash,
                    "qualityStatus": record.quality_status or "VERIFIED", "freshness": freshness,
                    "excerpts": [], "truncated": False, "nextCursor": None}
        return self._page(response, excerpts, evidence_ids or [], cursor)

    def _page(self, response, excerpts, evidence_ids, cursor):
        from app.services.context_builder import estimate_tokens
        binding = canonical_hash([response["executionId"], response["rawHash"], sorted(set(evidence_ids))])
        index, offset = 0, 0
        if cursor:
            try:
                value = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
                index, offset = value["index"], value["offset"]
                if value["binding"] != binding or type(index) is not int or type(offset) is not int:
                    raise ValueError()
                if not 0 <= index < len(excerpts) or not 0 <= offset < len(excerpts[index]["text"]):
                    raise ValueError()
            except Exception:
                return {"status": "INVALID_CURSOR", "executionId": response["executionId"], "excerpts": []}
        def finish_cursor(i, o):
            response["truncated"] = i < len(excerpts)
            response["nextCursor"] = (base64.urlsafe_b64encode(json.dumps(
                {"binding": binding, "index": i, "offset": o}).encode()).decode() if i < len(excerpts) else None)
        while index < len(excerpts):
            original = excerpts[index]
            text = original["text"]
            low, high = 0, len(text) - offset
            # 留出 ToolResult 包装、工具名称和消息信封的预算。
            while low < high:
                length = (low + high + 1) // 2
                item = {**original, "text": text[offset:offset + length], "offset": offset}
                candidate = {**response, "excerpts": [*response["excerpts"], item]}
                candidate["nextCursor"] = "x" * 220
                if estimate_tokens(json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))) <= self.read_max_tokens - 192:
                    low = length
                else:
                    high = length - 1
            if not low and text:
                break
            response["excerpts"].append({**original, "text": text[offset:offset + low], "offset": offset})
            offset += low
            if offset < len(text):
                break
            index, offset = index + 1, 0
        if not response["excerpts"]:
            return {"status": "READ_BUDGET_TOO_SMALL", "executionId": response["executionId"], "excerpts": []}
        finish_cursor(index, offset)
        return response

    def _read_database(self, execution_id: str) -> ToolExecutionRecord | None:
        from app.models.entities import ToolExecutionRecordEntity
        db = self.session_factory()
        try:
            row = db.query(ToolExecutionRecordEntity).filter_by(execution_id=execution_id).one_or_none()
            if row is None:
                return None
            return ToolExecutionRecord(
                execution_id=row.execution_id, user_id=row.user_id, session_id=row.session_id,
                turn_id=row.turn_id, work_item_id=row.work_item_id, agent_run_id=row.agent_run_id,
                agent_name=row.agent_name, tool_call_id=row.tool_call_id, attempt=row.attempt,
                tool_name=row.tool_name, tool_version=row.tool_version,
                arguments=json.loads(row.arguments_json), wire_result=json.loads(row.wire_result_json),
                normalized_result=json.loads(row.normalized_result_json), raw_hash=row.raw_hash,
                payload_bytes=row.payload_bytes, observed_at=row.observed_at.replace(tzinfo=UTC),
                valid_until=row.valid_until.replace(tzinfo=UTC) if row.valid_until else None,
                corpus_signature=row.corpus_signature, index_signature=row.index_signature,
                transport_status=row.transport_status, business_status=row.business_status,
                quality_status=row.quality_status, error_code=row.error_code,
                cached_from_execution_id=row.cached_from_execution_id, persist_reason=row.persist_reason,
            )
        finally:
            db.close()

    def __len__(self) -> int:
        return len(self._records)


def _extract_excerpts(payload: Any, evidence_ids: list[str]) -> list[dict[str, Any]]:
    rows = payload.get("items", []) if isinstance(payload, dict) else []
    result = []
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            continue
        evidence_id = str(row.get("evidenceId") or row.get("id") or f"block_{index}")
        if evidence_ids and evidence_id not in evidence_ids:
            continue
        for field in ("content", "text", "snippet", "parentContent"):
            text = row.get(field)
            if text:
                result.append({"evidenceId": evidence_id, "blockId": f"block_{index}_{field}",
                    "fieldPath": f"items[{index}].{field}", "text": str(text),
                    "scope": {"site": row.get("site", "UNKNOWN")}})
    if not rows and not evidence_ids:
        result.append({"evidenceId": "result", "blockId": "result", "fieldPath": "$",
                       "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":")), "scope": {}})
    return result
