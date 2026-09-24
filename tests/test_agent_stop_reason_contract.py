from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.agents.autonomous as autonomous
from app.agents.autonomous import AcademicPlanningAgent
from app.core.enums import IntentType
from app.schemas.dtos import AiMessage
from app.services.agent_loop import AgentLoop
from app.services.model_completion import ModelCompletionMetadata, ModelFinishReason
from app.services.tool_executor import ToolExecutor
from app.services.tool_models import AgentLoopResult, AiToolCall, AiToolCompletion, AiToolDefinition, ToolResult
from app.services.tool_registry import ToolRegistry


def _metadata() -> ModelCompletionMetadata:
    return ModelCompletionMetadata(
        provider="fake",
        model="fake",
        finish_reason=ModelFinishReason.TOOL_CALL,
        semantic_finish_seen=True,
        transport_terminal_seen=True,
        terminal_signal="test",
        provider_finish_reason="tool_calls",
        configured_output_limit=100,
    )


class _RepairClient:
    def complete_with_tools(self, _messages, _tools, *, model_round, purpose):
        assert purpose == f"AcademicPlanningAgent.agent_loop.round{model_round}"
        arguments = [
            {"query": "休学流程", "facets": {}},
            {"query": "休学流程", "facets": [""]},
            {"query": "休学流程", "facets": ["申请流程"], "top_k": 5},
        ][model_round - 1]
        return AiToolCompletion(
            "",
            (AiToolCall(f"call-{model_round}", "chat_readonly__rag_search", arguments),),
            _metadata(),
        )


class _Runtime:
    def __init__(self):
        self.calls = 0

    def call_tool_sync(self, *_args, **_kwargs):
        self.calls += 1
        return {
            "isError": False,
            "structuredContent": {
                "status": "OK",
                "items": [{"evidenceId": "ev-3", "content": "休学申请证据"}],
            },
        }


def _agent_for_result(monkeypatch, loop_result: AgentLoopResult):
    class _Loop:
        def __init__(self, **_kwargs):
            pass

        def run(self, **_kwargs):
            return loop_result

    monkeypatch.setattr(autonomous, "AgentLoop", _Loop)
    services = SimpleNamespace(
        tool_runtime=SimpleNamespace(
            registry=SimpleNamespace(definitions_for_agent=lambda _name: [AiToolDefinition(
                "chat_readonly__rag_search",
                "本地知识检索",
                {"type": "object", "properties": {}},
            )]),
            executor=object(),
        ),
        skill_manager=None,
        context_packet=SimpleNamespace(safety_context=None, for_specialist=lambda *_args: {}),
        settings=SimpleNamespace(
            agent_loop_max_model_rounds=3,
            agent_loop_max_tool_calls=4,
            agent_loop_max_result_chars=12000,
            agent_loop_deadline_seconds=100,
            context_input_max_tokens=12000,
            context_model_safety_margin_tokens=1024,
            agent_model_specialist_max_tokens=1024,
            ollama_num_ctx=16384,
        ),
        model_registry=SimpleNamespace(client_for=lambda _name: object()),
    )
    return AcademicPlanningAgent(services)


def test_last_round_cannot_dispatch_even_after_argument_repairs(monkeypatch) -> None:
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
    runtime = _Runtime()
    executor = ToolExecutor(registry=registry, runtime=runtime)
    tool = registry.definitions_for_agent("AcademicPlanningAgent")[0]
    loop_result = AgentLoop(client=_RepairClient(), executor=executor, max_model_rounds=3).run(
        agent_name="AcademicPlanningAgent",
        messages=[AiMessage(role="user", content="休学申请怎么办理？")],
        tools=[tool],
    )
    agent = _agent_for_result(monkeypatch, loop_result)

    actual = agent._run_loop({"objective": "说明休学流程", "intent": IntentType.ACADEMIC.value}, [])

    assert runtime.calls == 0
    assert loop_result.stop_reason == "FINALIZE_TOOL_CALL_REJECTED"
    assert actual[:2] == ("FAILED", "FINALIZE_TOOL_CALL_REJECTED")
    assert actual[3] == []
    assert actual[4] == []
    assert len(loop_result.call_details) == 3
    assert actual[6]["dispatchCount"] == 0
    assert actual[6]["agentStopReason"] == "FINALIZE_TOOL_CALL_REJECTED"


def test_rerank_degradation_is_partial_and_preserves_candidate_evidence(monkeypatch):
    call = AiToolCall("quality", "chat_readonly__rag_search", {"query": "合成问题"})
    evidence = [{"evidenceId": "ev-1", "content": "仍可审查的候选资料"}]
    tool = ToolResult(True, "OK", data={"status": "OK", "items": evidence,
                                       "diagnostics": {"rerankDegraded": True,
                                                       "rerankErrorCode": "RERANK_INVALID_RANKED_IDS"}},
                      degraded=True, dispatched=True)
    agent = _agent_for_result(monkeypatch, AgentLoopResult("候选结果", ((call, tool),), 2, "COMPLETED"))
    actual = agent._run_loop({"objective": "合成问题", "intent": IntentType.ACADEMIC.value}, [])
    assert actual[:2] == ("PARTIAL", "RETRIEVAL_DEGRADED")
    assert actual[3] == evidence
    assert actual[6]["retrievalDiagnostics"]["rerankDegraded"] is True


@pytest.mark.parametrize(
    "stop_reason",
    ["DEADLINE_EXCEEDED", "INPUT_BUDGET_EXCEEDED", "TOOL_RESULT_BUDGET_EXCEEDED"],
)
def test_rag_success_never_overwrites_agent_failure(monkeypatch, stop_reason) -> None:
    call = AiToolCall("call-1", "chat_readonly__rag_search", {"query": "休学流程"})
    loop_result = AgentLoopResult(
        "",
        ((call, ToolResult(True, "OK", data={
            "status": "SUFFICIENT",
            "items": [{"evidenceId": "ev-1", "content": "有效证据"}],
        }, dispatched=True)),),
        2,
        stop_reason,
    )
    agent = _agent_for_result(monkeypatch, loop_result)

    actual = agent._run_loop({"objective": "说明休学流程", "intent": IntentType.ACADEMIC.value}, [])

    assert actual[0] == "PARTIAL"
    assert actual[1] == stop_reason
    assert actual[3][0]["evidenceId"] == "ev-1"
