from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.agents.autonomous import (
    ResponseAgent, SpecialistAgent, _answer_contract_error, _validated_model_evidence_notes,
    _validated_response_updates,
)
from app.core.enums import RiskLevel
from app.evaluation.runner import _failed_case_ids
from app.services.agent_loop import AgentLoop, _fingerprint
from app.services.chat_tool_runtime import _CompositeRuntime
from app.services.evidence_contract import (
    capture_evidence_diagnostics, contact_scope_violations, locate_quote, record_evidence_diagnostic,
)
from app.services.tool_executor import parse_call_tool_result
from app.services.tool_models import AiToolCall, AiToolDefinition, ToolResult
from app.services.tool_result_store import ToolResultStore, bind_tool_result_scope


RAG = AiToolDefinition("chat_readonly__rag_search", "检索", {"type": "object"})
READ = AiToolDefinition("context_readonly__read_tool_evidence", "读取", {"type": "object"})
EVIDENCE = [{"evidenceId": "synthetic", "site": "ALL", "content":
             "咨询服务\n部门：服务中心\n咨询与服务：北湖校区服务中心\n电话：76543210"}]


def completion(*calls, content="完成", complete=True):
    return SimpleNamespace(content=content, tool_calls=calls, verified_complete=complete)


def test_layout_normalization_preserves_exact_source_quote():
    original = "请经所在学\n院审核后提交。"
    contract = SpecialistAgent.AnswerContract(answerBrief="经学院审核", answerStatus="FULL",
        evidenceNotes=[{"evidenceId": "e", "quote": "所在学院审核"}])
    notes = _validated_model_evidence_notes(contract, [{"evidenceId": "e", "content": original}])
    assert notes == [{"evidenceId": "e", "quote": "所在学\n院审核"}]
    assert notes[0]["quote"] in original
    assert _answer_contract_error(contract, True, notes) is None


@pytest.mark.parametrize("quote,original", [
    ("金额1200", "金额12 00"), ("可以申请", "不可以申请的情形"),
    ("截止日期为八月", "发放日期为八月"), ("甲乙", "甲，乙"),
])
def test_quote_normalization_never_changes_facts(quote, original):
    if quote == "可以申请":
        # Substring provenance is not semantic entailment; keep this limitation explicit.
        assert locate_quote(quote, original) == quote
    else:
        assert locate_quote(quote, original) is None


def test_wrong_evidence_id_cannot_borrow_matching_text():
    contract = SimpleNamespace(evidenceNotes=[{"evidenceId": "wrong", "quote": "原文"}])
    assert _validated_model_evidence_notes(contract, [{"evidenceId": "right", "content": "原文"}]) == []


@pytest.mark.parametrize("answer", ["学校电话为76543210。", "所有校区电话：76543210。",
                                    "北湖校区的情况如下。学校电话为76543210。"])
def test_contact_cannot_lose_scope_even_if_metadata_says_all(answer):
    assert contact_scope_violations(answer, EVIDENCE) == ["76543210"]


def test_scoped_contact_answer_and_non_phone_values_are_not_rejected():
    assert not contact_scope_violations("北湖校区服务中心电话为76543210，其他校区尚未确认。", EVIDENCE)
    assert not contact_scope_violations("申请编号为76543210。", [{"content": "北湖校区申请编号76543210"}])


def test_two_campuses_do_not_authorize_swapped_numbers():
    evidence = [*EVIDENCE, {"content": "南港校区服务中心\n电话：87654321"}]
    assert contact_scope_violations("南港校区电话76543210。北湖校区电话87654321。", evidence) == ["76543210", "87654321"]
    assert not contact_scope_violations("北湖校区电话76543210。南港校区电话87654321。", evidence)


def test_failure_composition_cannot_call_model_or_invent_failed_branch():
    services = SimpleNamespace(model_registry=SimpleNamespace(client_for=lambda _: pytest.fail("不能重答失败分支")))
    states = [
        {"workItemId": "w1", "status": "FAILED", "answerBrief": "不可发布的半成品", "answerStatus": None},
        {"workItemId": "w2", "status": "COMPLETED", "answerBrief": "已确认需要申请表。", "answerStatus": "FULL"},
    ]
    answer, diagnostics, _, updates = ResponseAgent(services)._generate_candidate([], RiskLevel.LOW, work_item_states=states)
    assert "暂时未能完成" in answer and "申请表" in answer and "半成品" not in answer
    assert diagnostics["attempts"] == 0 and diagnostics["failedWorkItemIds"] == ["w1"]
    assert updates == [] and states[0]["status"] == "FAILED"


def test_response_read_cannot_clear_execution_failure():
    with pytest.raises(ValueError, match="执行失败"):
        _validated_response_updates([{"workItemId": "w", "answerStatus": "FULL", "evidenceRefs": ["e"]}],
            [{"workItemId": "w", "status": "FAILED", "answerStatus": None}], [{"evidenceId": "e"}])


def test_small_result_never_publishes_read_tool_or_writes_store():
    store = ToolResultStore()
    class Client:
        def complete_with_tools(self, messages, tools, *, model_round, **kwargs):
            assert READ not in tools
            return completion(AiToolCall("r", RAG.name, {}), content="") if model_round == 1 else completion()
    class Executor:
        result_store = store
        def execute(self, **kwargs):
            return ToolResult(True, "OK", data={"items": [{"evidenceId": "e", "content": "短原文"}]},
                              execution_id="memory-only", dispatched=True)
    result = AgentLoop(client=Client(), executor=Executor()).run(agent_name="test", messages=[], tools=[RAG, READ])
    assert result.stop_reason == "COMPLETED" and len(store) == 0


def test_fabricated_read_reference_is_not_dispatched():
    class Client:
        def complete_with_tools(self, messages, tools, *, model_round, **kwargs):
            return completion(AiToolCall("r", READ.name, {"execution_id": "unknown"}), content="") if model_round == 1 else completion()
    class Executor:
        def execute(self, **kwargs):
            pytest.fail("不得读取未授权引用")
    result = AgentLoop(client=Client(), executor=Executor()).run(agent_name="test", messages=[], tools=[READ])
    assert result.call_details[0]["dispatched"] is False
    assert result.budget_stop_reason == "NOT_FOUND_OR_NOT_AUTHORIZED"


def test_read_fingerprint_ignores_focus_and_order_but_preserves_source():
    first = AiToolCall("1", READ.name, {"execution_id": "e", "evidence_ids": ["b", "a"], "focus": "日期"})
    second = AiToolCall("2", READ.name, {"execution_id": "e", "evidence_ids": ["a", "b"], "focus": "换个说法"})
    assert _fingerprint(first) == _fingerprint(second)
    assert _fingerprint(first) != _fingerprint(replace(second, arguments={**second.arguments, "execution_id": "other"}))


def test_empty_model_result_ends_once_without_hidden_retry():
    calls = []
    class Client:
        def complete_with_tools(self, *args, **kwargs):
            calls.append(kwargs)
            return completion(content="", complete=False)
    result = AgentLoop(client=Client(), executor=object()).run(agent_name="test", messages=[], tools=[RAG])
    assert result.stop_reason == "MODEL_INCOMPLETE" and len(calls) == 1


@pytest.mark.parametrize("mode,expected", [("missing", "NOT_FOUND_OR_NOT_AUTHORIZED"),
    ("wrong-evidence", "EVIDENCE_NOT_FOUND"), ("expired", "SOURCE_EXPIRED"), ("tampered", "HASH_MISMATCH")])
def test_read_business_errors_are_failed_tool_calls(mode, expected):
    store = ToolResultStore()
    record = store.persist("known", {"items": [{"evidenceId": "e", "content": "原文"}]})
    if mode == "expired":
        store._records["known"] = replace(record, valid_until=datetime.now(UTC) - timedelta(seconds=1))
    if mode == "tampered":
        record.normalized_result["items"][0]["content"] = "篡改"
    raw = _CompositeRuntime(None, store).call_tool_sync("context_readonly", "read_tool_evidence", {
        "execution_id": "missing" if mode == "missing" else "known",
        "evidence_ids": ["wrong" if mode == "wrong-evidence" else "e"],
    }, 2)
    result = parse_call_tool_result(raw)
    assert not result.ok and result.code == expected


def test_owned_record_cannot_be_read_without_caller_identity():
    store = ToolResultStore()
    with bind_tool_result_scope(user_id="u1", session_id="s1"):
        store.persist("e", {"items": [{"evidenceId": "a", "content": "授权原文"}]})
        assert store.read("e")["status"] == "OK"
    assert store.read("e")["status"] == "NOT_FOUND_OR_NOT_AUTHORIZED"
    with bind_tool_result_scope(user_id="u2", session_id="s1"):
        assert store.read("e")["status"] == "NOT_FOUND_OR_NOT_AUTHORIZED"


def test_failed_cases_include_quality_and_judge_failures_without_duplicates():
    reports = {"e2e": {"results": [{"caseId": "quality", "passed": False},
        {"caseId": "judge", "error_code": "INVALID"}, {"caseId": "ok", "passed": True}]},
        "other": {"results": [{"caseId": "quality", "passed": False}]}}
    assert _failed_case_ids(reports) == ["quality", "judge"]


def test_diagnostics_capture_is_opt_in_and_keeps_immutable_snapshot():
    record_evidence_diagnostic({"rawAnswer": "生产默认不捕获"})
    value = {"rawAnswer": "原始失败", "visibleEvidence": EVIDENCE}
    with capture_evidence_diagnostics() as records:
        record_evidence_diagnostic(value)
        value["rawAnswer"] = "覆盖"
    assert records[0]["rawAnswer"] == "原始失败"
    with capture_evidence_diagnostics() as fresh:
        assert fresh == []


def test_rag_agent_schema_does_not_allow_top_k_or_facets():
    from app.chat_tools.server import mcp
    schema = mcp._tool_manager._tools["rag_search"].parameters
    assert set(schema["properties"]) == {"query", "site"}
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("specialist_scoped", [False, True])
def test_production_agents_quarantine_scope_loss_and_export_rejected_contract(specialist_scoped):
    from app.core.config import Settings
    from scripts.probe_context_workitems_followup import run_case
    from app.services.model_completion import ModelCompletion, ModelCompletionMetadata, ModelFinishReason
    import json

    answer = "北湖校区服务中心电话为76543210。" if specialist_scoped else "全校电话为76543210。"
    contract = {"answerBrief": answer, "answerStatus": "FULL",
                "evidenceNotes": [{"evidenceId": "synthetic-ev", "quote": "电话：76543210"}], "missingInfo": []}
    class Client:
        def complete_structured(self, messages, *, response_model, **kwargs):
            return SimpleNamespace(value=response_model.model_validate({
                "answerText": "学校电话为76543210。", "workItemUpdates": [],
            }))
        def complete_with_tools(self, messages, tools, *, model_round, **kwargs):
            if model_round == 1:
                return completion(AiToolCall("r", RAG.name, {"query": "服务电话"}), content="")
            return completion(content=json.dumps(contract, ensure_ascii=False))
        def complete(self, messages, **kwargs):
            return ModelCompletion(content="学校电话为76543210。", metadata=ModelCompletionMetadata(
                provider="fake", model="fake", finish_reason=ModelFinishReason.STOP,
                semantic_finish_seen=True, transport_terminal_seen=True, terminal_signal="done",
                provider_finish_reason="stop", configured_output_limit=1536))
    fixture = {"id": "scope-loss", "intent": "CAMPUS", "question": "服务中心的电话是什么？",
               "evidence": EVIDENCE[0]["content"]}
    with capture_evidence_diagnostics() as diagnostics:
        result = run_case(fixture, Settings(_env_file=None), SimpleNamespace(client_for=lambda _: Client()))
    assert "76543210" not in result["response"]
    assert result["diagnostics"]["finishReason"] == "EVIDENCE_SCOPE_INVALID"
    assert diagnostics[-1]["rawAnswer"] == "学校电话为76543210。"
    if not specialist_scoped:
        assert result["specialist"]["reasonCode"] == "EVIDENCE_SCOPE_INVALID"
        assert result["specialist"]["answerStatus"] != "FULL"
        assert diagnostics[0]["rawContract"] == contract
        assert diagnostics[0]["visibleEvidence"]


def test_persisted_result_enables_read_only_in_following_model_round():
    seen = []
    store = ToolResultStore()
    class Client:
        def complete_with_tools(self, messages, tools, *, model_round, **kwargs):
            seen.append(READ in tools)
            if model_round == 1:
                return completion(AiToolCall("r", RAG.name, {}), content="")
            if model_round == 2:
                return completion(AiToolCall("read", READ.name, {
                    "execution_id": "durable", "evidence_ids": ["e"], "focus": "条件",
                }), content="")
            return completion()
    class Executor:
        result_store = store
        def execute(self, **kwargs):
            if kwargs["tool_name"] == READ.name:
                return ToolResult(True, "OK", data=store.read("durable", evidence_ids=["e"]), dispatched=True)
            return ToolResult(True, "OK", data={"items": [{"evidenceId": "e", "content": "较长原文" * 60}]},
                              execution_id="durable", dispatched=True)
    result = AgentLoop(client=Client(), executor=Executor(), tool_result_large_tokens=20, max_model_rounds=4).run(
        agent_name="test", messages=[], tools=[RAG, READ])
    assert seen[:2] == [False, True]
    assert result.stop_reason == "COMPLETED" and len(store) == 1
    assert len(result.tool_results) == 2


def test_read_store_exception_is_not_misclassified_as_success():
    store = SimpleNamespace(read=lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("database unavailable")))
    raw = _CompositeRuntime(None, store).call_tool_sync("context_readonly", "read_tool_evidence", {}, 1)
    result = parse_call_tool_result(raw)
    assert not result.ok and result.code == "STORE_UNAVAILABLE"
