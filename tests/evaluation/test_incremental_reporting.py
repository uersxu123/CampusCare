from __future__ import annotations

import asyncio
import json

import pytest

from app.evaluation.contracts import EndToEndCase, EvaluationRuntimeOutcome
from app.evaluation.reporting.writer import EvaluationReportWriter
from app.evaluation.runner import _execute_cases


def _case(case_id: str) -> EndToEndCase:
    return EndToEndCase.model_validate({
        "id": case_id,
        "turns": ["合成输入"],
        "expected_action": "ANSWER",
        "reference": "合成参考",
    })


def _outcome(case_id: str, turn_index: int = 0) -> EvaluationRuntimeOutcome:
    return EvaluationRuntimeOutcome(
        case_id=case_id,
        turn_index=turn_index,
        response="合成回复",
        route={"primaryIntent": "CHAT"},
        risk_level="LOW",
        action="ANSWER",
        knowledge_requested=False,
        knowledge_used=False,
        retrieved_context_ids=[],
        retrieved_contexts=[],
        usable_context_ids=[],
        usable_contexts=[],
        trace_id="trace",
        turn_metrics={},
    )


def test_completed_case_is_durable_when_next_case_crashes(tmp_path) -> None:
    class Adapter:
        async def run_case(self, case):
            if case.id == "case-b":
                raise RuntimeError("synthetic crash")
            return [_outcome(case.id)]

    writer = EvaluationReportWriter(tmp_path, "incremental")
    with pytest.raises(RuntimeError, match="synthetic crash"):
        asyncio.run(_execute_cases([_case("case-a"), _case("case-b")], Adapter(), writer=writer))

    rows = [json.loads(line) for line in (writer.run_dir / "cases.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["case_id"] == "case-a"
    assert rows[0]["terminalStatus"] == "COMPLETED"


def test_corrupt_jsonl_tail_is_preserved_exactly_and_valid_prefix_recovers(tmp_path) -> None:
    writer = EvaluationReportWriter(tmp_path, "recover")
    writer.append_case(_outcome("case-a").__dict__)
    path = writer.run_dir / "cases.jsonl"
    corrupt_tail = b'{"case_id":"case-b","response":"incomplete"'
    with path.open("ab") as stream:
        stream.write(corrupt_tail)
        stream.flush()

    recovered = writer.recover_cases()

    assert [item["case_id"] for item in recovered["records"]] == ["case-a"]
    sidecar = writer.run_dir / recovered["corruptTail"].split("\\")[-1]
    assert sidecar.read_bytes() == corrupt_tail
    assert path.read_bytes().endswith(b"\n")
    assert b"case-b" not in path.read_bytes()


def test_atomic_summary_failure_keeps_previous_complete_file(tmp_path, monkeypatch) -> None:
    writer = EvaluationReportWriter(tmp_path, "atomic")
    writer.write_json("summary.json", {"status": "old"})

    import app.evaluation.reporting.writer as module

    monkeypatch.setattr(module.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(OSError, match="replace failed"):
        writer.write_json("summary.json", {"status": "new"})

    assert json.loads((writer.run_dir / "summary.json").read_text(encoding="utf-8")) == {"status": "old"}
    assert list(writer.run_dir.glob("*.tmp")) == []


def test_multi_turn_identity_and_conflicting_terminal_are_explicit(tmp_path) -> None:
    writer = EvaluationReportWriter(tmp_path, "identity")
    assert writer.append_case(_outcome("case-a", 0).__dict__)
    assert writer.append_case(_outcome("case-a", 1).__dict__)
    conflict = _outcome("case-a", 1).__dict__
    conflict["response"] = "不同的迟到结果"
    with pytest.raises(ValueError, match="terminal conflict"):
        writer.append_case(conflict)

    assert len((writer.run_dir / "cases.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_journal_uses_whitelist_and_keeps_unknown_cost_absent(tmp_path) -> None:
    writer = EvaluationReportWriter(tmp_path, "journal")
    writer.append_journal({
        "caseId": "case-a",
        "phase": "tool",
        "status": "FAILED",
        "errorCode": "TIMEOUT",
        "coverage": "PARTIAL",
        "secret": "不得落盘",
        "prompt": "不得落盘",
    }, fsync=True)
    record = json.loads((writer.run_dir / "lifecycle.jsonl").read_text(encoding="utf-8"))
    assert record["coverage"] == "PARTIAL"
    assert "secret" not in record
    assert "prompt" not in record
    assert "cost" not in record
