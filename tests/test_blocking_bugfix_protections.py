from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).parent / "fixtures/protected-dataset-hashes.json"
LOCAL_RUN_LOCK = ROOT / "target/evaluation/smoke-current-v2/.single-smoke-v2-started.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def test_protected_dataset_hashes_are_unchanged() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    protected = [item for item in baseline["files"] if item["path"].startswith("app/")]
    assert protected
    for item in protected:
        assert _sha256(ROOT / item["path"]) == item["sha256"]


@pytest.mark.skipif(not LOCAL_RUN_LOCK.exists(), reason="历史单次评测锁仅存在于原评测机器，不属于仓库运行依赖")
def test_historical_single_run_lock_hash_is_unchanged() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    expected = next(item for item in baseline["files"] if item["path"] == LOCAL_RUN_LOCK.relative_to(ROOT).as_posix())
    assert _sha256(LOCAL_RUN_LOCK) == expected["sha256"]


def test_changed_source_is_utf8_without_bom_and_has_no_accidental_unicode_escapes() -> None:
    changed = [
        "app/services/agent_loop.py",
        "app/services/context_builder.py",
        "app/services/execution_control.py",
        "app/services/process_supervisor.py",
        "app/services/turn_execution.py",
        "app/agents/autonomous.py",
        "app/agents/coordinator.py",
        "app/agents/event_driven_runtime.py",
        "app/agents/harness.py",
        "app/agents/result.py",
        "app/evaluation/runner.py",
        "app/evaluation/config.py",
        "app/evaluation/contracts.py",
        "app/evaluation/reporting/writer.py",
        "app/evaluation/runtime/adapter.py",
        "app/evaluation/runtime/capture.py",
        "app/evaluation/runtime/supervisor.py",
    ]
    escape = re.compile(r"\\u[0-9a-fA-F]{4}")
    for relative in changed:
        raw = (ROOT / relative).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), relative
        text = raw.decode("utf-8")
        assert escape.search(text) is None, relative
    assert "工具结果已按输入预算截断" in (ROOT / "app/services/agent_loop.py").read_text(encoding="utf-8")
