from __future__ import annotations

import json
from types import SimpleNamespace

from app.agents.routing import _build_v5_normal_plan
from app.schemas.dtos import AiMessage
from app.services.agent_loop import AgentLoop
from app.services.context_budget import BudgetConfig, estimate_messages
from app.services.model_completion import ModelCompletionMetadata, ModelFinishReason
from app.services.routing_v5 import PlanningResultV6
from app.services.tool_executor import parse_call_tool_result
from app.services.tool_models import AiToolCall, AiToolCompletion, AiToolDefinition, ToolResult
from app.services.tool_result_store import ToolResultStore, bind_tool_result_scope
from app.core.config import Settings
from app.evaluation.config import EvaluationSettings
from app.evaluation.judges.deepseek import DeepSeekJudge
from app.services.ai import StructuredCompletionError, StructuredCompletionOptions
from app.agents.autonomous import (
    _answer_contract_error,
    _merge_response_updates,
    _validated_model_evidence_notes,
    _validated_response_updates,
)


def _metadata(reason: ModelFinishReason) -> ModelCompletionMetadata:
    return ModelCompletionMetadata(
        provider="fake", model="fake", finish_reason=reason, semantic_finish_seen=True,
        transport_terminal_seen=True, terminal_signal="test", provider_finish_reason=reason.value,
        configured_output_limit=128,
    )


def test_mcp_text_json_is_parsed_before_any_context_projection():
    value = {"status": "OK", "items": [{"content": "中" * 8000}]}
    result = parse_call_tool_result({"isError": False, "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]})
    assert result.ok
    assert result.data == value


def test_mcp_multiple_text_blocks_fail_closed():
    result = parse_call_tool_result({"isError": False, "content": [{"text": "{}"}, {"text": "{}"}]})
    assert result.code == "MCP_PROTOCOL_ERROR"


def test_v6_planner_fields_survive_python_route_plan():
    planning = PlanningResultV6.model_validate({"schemaVersion": 6, "workItems": [{
        "intent": "CAMPUS", "objective": "核对奖学金条件",
        "taskText": "核对本科生国家奖学金条件，但不要推断我已经符合资格。",
        "sourceRefs": ["current:0"], "contextRefs": [], "dependsOn": [],
    }]})
    plan = _build_v5_normal_plan("请核对奖学金条件，但不要推断我符合资格。", {}, planning)
    item = plan.work_items[0]
    assert plan.schema_version == 6
    assert item.task_text.endswith("不要推断我已经符合资格。")
    assert item.source_refs == ("current:0",)


def test_complete_budget_counts_tools_and_recomputes_output_reserve():
    messages = [AiMessage(role="system", content="安全规则"), AiMessage(role="user", content="问题")]
    tool = AiToolDefinition("context_readonly__read_tool_evidence", "读取证据", {"type": "object", "properties": {"execution_id": {"type": "string"}}})
    assert estimate_messages(messages, [tool]) > estimate_messages(messages)
    smaller_output = BudgetConfig(output_max_tokens=1024)
    larger_output = BudgetConfig(output_max_tokens=8192, input_max_tokens=40000)
    assert larger_output.input_budget < smaller_output.input_budget


def test_large_result_is_persisted_before_projection_and_can_be_read():
    store = ToolResultStore()
    tool = AiToolDefinition("chat_readonly__rag_search", "检索", {"type": "object"})

    class Client:
        def complete_with_tools(self, messages, tools, *, model_round, purpose):
            if model_round == 1:
                return AiToolCompletion("", (AiToolCall("call-1", tool.name, {}),), _metadata(ModelFinishReason.TOOL_CALL))
            payload = json.loads(messages[-1].content)
            assert payload["persisted"] is True
            return AiToolCompletion("完成", (), _metadata(ModelFinishReason.STOP))

    class Executor:
        result_store = store
        index_version = "synthetic-index"
        def execute(self, **kwargs):
            return ToolResult(True, "OK", data={"items": [{"evidenceId": "ev-1", "content": "证据" * 300}]},
                              execution_id="exec-synthetic", raw_hash="sha256:test", dispatched=True)

    result = AgentLoop(client=Client(), executor=Executor(), tool_result_large_tokens=50).run(
        agent_name="AcademicPlanningAgent", messages=[AiMessage(role="user", content="问题")], tools=[tool])
    assert result.stop_reason == "COMPLETED"
    assert len(store) == 1
    reread = store.read("exec-synthetic", evidence_ids=["ev-1"])
    assert reread["status"] == "OK"
    assert reread["excerpts"][0]["text"].startswith("证据")


def test_small_result_remains_memory_only():
    store = ToolResultStore()

    class Executor:
        result_store = store
        def execute(self, **kwargs):
            return ToolResult(True, "OK", data={"status": "OK"}, execution_id="exec-small", dispatched=True)

    class Client:
        def complete_with_tools(self, messages, tools, *, model_round, purpose):
            if model_round == 1:
                return AiToolCompletion("", (AiToolCall("c", tools[0].name, {}),), _metadata(ModelFinishReason.TOOL_CALL))
            return AiToolCompletion("完成", (), _metadata(ModelFinishReason.STOP))

    tool = AiToolDefinition("synthetic", "合成", {"type": "object"})
    AgentLoop(client=Client(), executor=Executor(), tool_result_large_tokens=8000).run(
        agent_name="synthetic", messages=[], tools=[tool])
    assert len(store) == 0


def test_tool_result_scope_is_implicit_and_cross_user_read_is_denied():
    store = ToolResultStore()
    with bind_tool_result_scope(user_id="user-a", session_id="session-a", turn_id="turn-a"):
        store.persist("exec-scoped", {"items": [{"evidenceId": "ev-1", "content": "原文"}]})
        assert store.read("exec-scoped", evidence_ids=["ev-1"])["status"] == "OK"
    with bind_tool_result_scope(user_id="user-b", session_id="session-a", turn_id="turn-b"):
        assert store.read("exec-scoped", evidence_ids=["ev-1"])["status"] == "NOT_FOUND_OR_NOT_AUTHORIZED"


def test_formal_judge_structured_failure_does_not_make_second_call():
    judge = DeepSeekJudge(Settings(_env_file=None), EvaluationSettings(
        _env_file=None, judge_provider="ollama", judge_model="qwen3:8b",
        judge_max_retries=0, judge_allow_prompt_fallback=False,
    ))
    calls = {"structured": 0, "plain": 0}
    def fail(*args, **kwargs):
        calls["structured"] += 1
        raise StructuredCompletionError("STRUCTURED_OUTPUT_UNSUPPORTED", "synthetic")
    judge.client.complete_structured = fail
    judge.client.complete = lambda *args, **kwargs: calls.__setitem__("plain", calls["plain"] + 1)
    result = judge._judge_once(
        [AiMessage(role="system", content="评测"), AiMessage(role="user", content="合成")],
        StructuredCompletionOptions(temperature=0, max_tokens=100, repair_attempts=0),
    )
    assert result.error_code == "JUDGE_INVALID_STRUCTURED_OUTPUT"
    assert calls == {"structured": 1, "plain": 0}


def test_specialist_quote_must_exist_in_visible_evidence():
    contract = SimpleNamespace(
        answerStatus="FULL",
        evidenceNotes=[{"evidenceId": "ev-1", "quote": "申请截止日期为 9 月 1 日"}],
        missingInfo=[],
    )
    evidence = [{"evidenceId": "ev-1", "content": "奖学金发放日期为 9 月 1 日。"}]
    notes = _validated_model_evidence_notes(contract, evidence)
    assert notes == []
    assert _answer_contract_error(contract, True, notes) == "EVIDENCE_QUOTE_INVALID"


def test_policy_contract_rejects_not_required_and_full_with_missing_info():
    not_required = SimpleNamespace(answerStatus="NOT_REQUIRED", evidenceNotes=[], missingInfo=[])
    assert _answer_contract_error(not_required, True, []) == "ANSWER_CONTRACT_INVALID"
    contradictory = SimpleNamespace(
        answerStatus="FULL",
        evidenceNotes=[{"evidenceId": "ev-1", "quote": "需要申请表"}],
        missingInfo=["截止日期"],
    )
    notes = [{"evidenceId": "ev-1", "quote": "需要申请表"}]
    assert _answer_contract_error(contradictory, True, notes) == "ANSWER_CONTRACT_INVALID"


def test_response_can_upgrade_only_with_actual_reread_reference():
    states = [{
        "workItemId": "wi-1", "answerStatus": "PARTIAL",
        "missingInfo": ["审批去向"], "evidenceRefs": ["ev-old"],
    }]
    evidence = [{"evidenceId": "ev-new", "content": "提交学生工作处审批。"}]
    updates = _validated_response_updates([{
        "workItemId": "wi-1", "answerStatus": "FULL", "missingInfo": [],
        "evidenceRefs": ["ev-old", "ev-new"],
    }], states, evidence)
    merged = _merge_response_updates(states, updates)
    assert merged[0]["answerStatus"] == "FULL"
    assert merged[0]["updatedByResponse"] is True

    with __import__("pytest").raises(ValueError):
        _validated_response_updates([{
            "workItemId": "wi-1", "answerStatus": "FULL", "missingInfo": [],
            "evidenceRefs": ["ev-old"],
        }], states, evidence)
