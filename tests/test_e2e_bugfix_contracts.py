from __future__ import annotations

import json
from pathlib import Path

from app.evaluation.dataset import load_e2e_cases
from app.schemas.dtos import AiMessage
from app.services.agent_loop import AgentLoop
from app.services.model_completion import ModelCompletionMetadata, ModelFinishReason
from app.services.tool_executor import ToolExecutor
from app.services.tool_models import AiToolCall, AiToolCompletion
from app.services.tool_registry import ToolRegistry
from app.services.turn_metrics import TurnMetricsCollector
from tools.run_e2e_smoke_local import _freeze


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "app/evaluation/datasets/e2e-smoke-current-v2"


def _metadata(reason: ModelFinishReason) -> ModelCompletionMetadata:
    return ModelCompletionMetadata(
        provider="fake",
        model="fake",
        finish_reason=reason,
        semantic_finish_seen=True,
        transport_terminal_seen=True,
        terminal_signal="test",
        provider_finish_reason=reason.value.lower(),
        configured_output_limit=100,
    )


class RepairClient:
    def complete_with_tools(self, messages, tools, *, model_round, purpose):
        if model_round == 4:
            assert tools == []
            return AiToolCompletion("已基于取得的证据完成回答。", (), _metadata(ModelFinishReason.STOP))
        arguments = [
            {"query": "本科生休学申请怎么办理？", "facets": {}},
            {"query": "本科生休学申请怎么办理？", "facets": [""]},
            {"query": "本科生休学申请怎么办理？", "facets": ["申请流程"], "top_k": 5},
        ][model_round - 1]
        return AiToolCompletion(
            "",
            (AiToolCall(f"call-{model_round}", "chat_readonly__rag_search", arguments),),
            _metadata(ModelFinishReason.TOOL_CALL),
        )


class Runtime:
    def __init__(self):
        self.calls = []

    def call_tool_sync(self, server, name, arguments, timeout, **kwargs):
        self.calls.append((server, name, arguments, kwargs))
        return {"isError": False, "structuredContent": {"status": "OK", "items": [{"id": "knowledge:8801"}]}}


def test_two_validation_failures_do_not_consume_dispatch_budget():
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 2},
            "facets": {"type": ["array", "null"], "items": {"type": "string", "minLength": 1}},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 8},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    registry = ToolRegistry()
    registry.register_tools("chat_readonly", [{"name": "rag_search", "description": "RAG", "inputSchema": schema}])
    runtime = Runtime()
    executor = ToolExecutor(registry=registry, runtime=runtime)
    tool = registry.definitions_for_agent("AcademicPlanningAgent")[0]
    result = AgentLoop(client=RepairClient(), executor=executor, max_model_rounds=4).run(
        agent_name="AcademicPlanningAgent",
        messages=[AiMessage(role="user", content="本科生休学申请怎么办理？")],
        tools=[tool],
    )
    assert [item.code for _, item in result.tool_results] == ["INVALID_ARGUMENT", "INVALID_ARGUMENT", "OK"]
    assert len(runtime.calls) == 1
    assert result.tool_results[-1][1].dispatched is True
    assert result.stop_reason == "COMPLETED" and result.model_rounds == 4


def test_v2_dataset_contract_and_frozen_hashes():
    business = load_e2e_cases(DATASET / "business.jsonl")
    safety = load_e2e_cases(DATASET / "safety.jsonl")
    assert len(business) == 13
    assert len(safety) == 3
    assert all(case.expected_route.primaryIntent.value == "MENTAL" for case in safety)
    assert all(case.expected_route.riskLevel.value == "HIGH" for case in safety)
    campus_110 = next(case for case in business if case.id == "smoke-current-campus-security-site-clarify")
    assert campus_110.allowed_actions == ["CLARIFY", "ANSWER"]
    assert campus_110.tools_for_action("ANSWER").required == ["rag_search"]
    frozen = _freeze(DATASET / "business.jsonl", DATASET / "safety.jsonl", DATASET / "corpus-audit.json")
    assert frozen["businessCaseCount"] == 13
    assert frozen["safetyCaseCount"] == 3


def test_child_telemetry_merge_is_idempotent_and_marks_partial():
    collector = TurnMetricsCollector("parent")
    envelope = {
        "toolCallId": "call-1",
        "attempt": 1,
        "serverGeneration": 2,
        "tokenUsageCoverage": "PARTIAL",
        "calls": [{
            "sequence": 1,
            "purpose": "rag.rewrite",
            "stage": "rag",
            "promptTokens": 10,
            "outputTokens": 4,
            "thinkingTokens": 0,
            "promptTokenSource": "PROVIDER",
            "outputTokenSource": "PROVIDER",
            "status": "COMPLETED",
        }],
    }
    collector.merge_tool_telemetry(envelope)
    collector.merge_tool_telemetry(envelope)
    payload = collector.as_dict()
    assert len(payload["calls"]) == 1
    assert payload["tokenUsage"]["knownTokensTotal"] == 14
    assert payload["tokenUsage"]["tokenUsageCoverage"] == "PARTIAL"
