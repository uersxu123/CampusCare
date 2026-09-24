from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.evaluation.reporting.writer import validate_report_privacy


def baseline_key(identity: dict) -> str:
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def load_baseline(directory: Path, identity: dict) -> dict | None:
    path = directory / f"{baseline_key(identity)}.json"
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if payload.get("identity") == identity else {"status": "STALE", "path": str(path)}
    if not directory.is_dir():
        return None
    for candidate in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        previous = payload.get("identity") or {}
        if previous.get("suite") == identity.get("suite") and previous.get("profile") == identity.get("profile"):
            return {"status": "STALE", "path": str(candidate)}
    return None


def update_baseline(directory: Path, identity: dict, summary: dict) -> Path:
    if not summary.get("passed"):
        raise ValueError("未通过的评测结果不能更新 baseline")
    payload = validate_report_privacy({"schemaVersion": 1, "identity": identity, "summary": summary})
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{baseline_key(identity)}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return path
