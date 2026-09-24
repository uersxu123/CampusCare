from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agents.autonomous import (
    CampusAffairsAgent, SpecialistAgent, ResponseAgent, _answer_contract_error,
    _validated_model_evidence_notes, _tools_for_work_item, _dedupe_evidence_items,
)
from app.agents.events import AgentTask, AgentArtifact, CollaborationBlackboard
from app.agents.routing import _build_v5_normal_plan
from app.services.routing_v5 import PlanningResultV6
from app.services.agent_loop import AgentLoop
from app.services.tool_models import AiToolDefinition, AiToolCall, ToolResult
from app.services.execution_control import ExecutionBudget, bind_execution_budget, remaining_timeout, ExecutionDeadlineExceeded
from app.schemas.dtos import AiMessage
from app.evaluation.runtime.action_resolution import resolve_evaluation_action
from app.agents.event_driven_runtime import _business_outcome
from app.services.trace import _safe


RAG = AiToolDefinition("chat_readonly__rag_search", "检索", {"type": "object"})
READ = AiToolDefinition("context_readonly__read_tool_evidence", "回读", {"type": "object"})
WEATHER = AiToolDefinition("chat_readonly__get_current_weather", "天气", {"type": "object"})


def completion(*calls, content="已完成"):
    return SimpleNamespace(content=content, tool_calls=calls, verified_complete=True)


@pytest.mark.parametrize("note", [{"evidenceId": "e", "note": "说明"}, {"evidenceId": "e", "quote": ""},
                                 {"evidenceId": "e", "quote": "  "}, {"quote": "原文"}])
def test_missing_or_empty_quote_is_rejected_by_production_schema(note):
    with pytest.raises(ValidationError):
        SpecialistAgent.AnswerContract.model_validate({"answerBrief": "回答", "answerStatus": "FULL", "evidenceNotes": [note]})


def test_duplicate_valid_quotes_are_not_invalid_evidence():
    contract = SpecialistAgent.AnswerContract.model_validate({
        "answerBrief": "需要登记", "answerStatus": "FULL",
        "evidenceNotes": [{"evidenceId": "e", "quote": "需要登记"}] * 2,
    })
    notes = _validated_model_evidence_notes(contract, [{"evidenceId": "e", "content": "需要登记。"}])
    assert len(notes) == 1
    assert _answer_contract_error(contract, True, notes) is None


@pytest.mark.parametrize("query", ["同一问题", "  同一问题？  "])
def test_duplicate_rag_finalizes_with_existing_evidence(query):
    calls = []
    rounds = []

    class Client:
        def complete_with_tools(self, messages, tools, **kwargs):
            rounds.append(tools)
            index = len(rounds)
            if index <= 2:
                return completion(AiToolCall(str(index), RAG.name, {"query": "同一问题" if index == 1 else query}), content="")
            assert tools == []
            assert "需要登记" in json.dumps([m.content for m in messages], ensure_ascii=False)
            return completion(content="已知需要登记，办理时长未确认。")

    class Executor:
        def execute(self, **kwargs):
            calls.append(kwargs)
            return ToolResult(True, "OK", data={"items": [{"evidenceId": "e", "content": "需要登记。"}]}, dispatched=True)

    result = AgentLoop(client=Client(), executor=Executor(), max_model_rounds=4).run(agent_name="test", messages=[], tools=[RAG])
    assert len(calls) == 1
    assert result.stop_reason == "COMPLETED"
    assert result.model_rounds == 3
    assert result.budget_stop_reason == "DUPLICATE_TOOL_CALL"
    assert result.content.endswith("未确认。")
    assert result.call_details[-1]["dispatched"] is False


def test_four_round_sequence_preserves_both_read_fragments():
    dispatched = []

    class Client:
        def complete_with_tools(self, messages, tools, *, model_round, **kwargs):
            if model_round == 4:
                assert not tools
                return completion(content="已读取两段")
            name = RAG.name if model_round == 1 else READ.name
            return completion(AiToolCall(str(model_round), name, {
                "execution_id": "durable", "evidence_ids": [f"e{model_round}"], "focus": "核对",
            }), content="")

    class Executor:
        def execute(self, **kwargs):
            dispatched.append(kwargs["tool_name"])
            return ToolResult(True, "OK", data={"excerpts": [{"evidenceId": "e", "text": str(len(dispatched))}]},
                              execution_id="durable", persisted=True, dispatched=True)

    result = AgentLoop(client=Client(), executor=Executor(), max_model_rounds=4).run(agent_name="test", messages=[], tools=[RAG, READ])
    assert dispatched == [RAG.name, READ.name, READ.name]
    assert result.model_rounds == 4 and result.stop_reason == "COMPLETED"
    assert len(_dedupe_evidence_items(list(result.visible_tool_evidence))) == 3


@pytest.mark.parametrize("text", ["翻译‘国家助学金申请条件’", "用 Java 写空字符串判断方法", "把‘明天天气如何’译成英文"])
def test_pure_generation_does_not_expose_weather(text):
    assert _tools_for_work_item([WEATHER], {"taskText": text}, []) == []


def test_weather_and_dependency_reads_remain_available():
    assert _tools_for_work_item([WEATHER], {"taskText": "北京今天下雨吗"}, []) == [WEATHER]
    dep = [{"evidenceItems": [{"executionId": "x", "persisted": True}]}]
    assert _tools_for_work_item([RAG, READ], {"taskText": "按已有材料制定计划"}, dep, planning_only=True) == [READ]


def test_remaining_deadline_is_passed_to_model_and_tool():
    observed = []
    class Client:
        def complete_with_tools(self, messages, tools, *, model_round, **kwargs):
            observed.append(remaining_timeout())
            return completion(AiToolCall("1", RAG.name, {}), content="") if model_round == 1 else completion()
    class Executor:
        def execute(self, **kwargs):
            observed.append(kwargs["remaining_seconds"])
            return ToolResult(True, "OK", dispatched=True)
    with bind_execution_budget(ExecutionBudget.start(10)):
        result = AgentLoop(client=Client(), executor=Executor(), final_answer_reserve_seconds=2).run(agent_name="test", messages=[], tools=[RAG])
    assert result.stop_reason == "COMPLETED"
    assert all(0 < value <= 10 for value in observed)
    assert observed[1] <= 8
    with bind_execution_budget(ExecutionBudget.start(0)):
        with pytest.raises(ExecutionDeadlineExceeded):
            remaining_timeout()


def test_invalid_claim_is_quarantined_before_response(monkeypatch):
    planning = PlanningResultV6.model_validate({"schemaVersion": 6, "workItems": [{
        "intent": "CAMPUS", "objective": "确认申请日期", "taskText": "确认申请截止日期",
        "sourceRefs": ["current:0"], "contextRefs": [], "dependsOn": [],
    }]})
    plan = _build_v5_normal_plan("确认申请截止日期", {}, planning)
    board = CollaborationBlackboard(turn_id="synthetic", user_input="确认申请截止日期")
    route = AgentArtifact(id="route", owner="test", kind="route_plan", payload=plan.as_payload(), confidence=1.0)
    board = board.add_artifact(route)
    work = plan.as_payload()["workItems"][0]
    agent = CampusAffairsAgent(SimpleNamespace())
    def run(*args):
        agent._last_answer_contract = SpecialistAgent.AnswerContract.model_validate({
            "answerBrief": "申请在8月9日截止", "answerStatus": "FULL",
            "evidenceNotes": [{"evidenceId": "e", "quote": "申请在8月9日截止"}],
        })
        return "COMPLETED", "RETRIEVAL_COMPLETED", "申请在8月9日截止", [{"evidenceId": "e", "content": "资金在8月9日发放。"}], ["e"], [], {"errorCodes": []}
    monkeypatch.setattr(agent, "_run_loop", run)
    result = agent.act(AgentTask(id="task", title="合成", metadata={
        "routePlanArtifactId": "route", "planId": plan.plan_id, "workItemId": work["workItemId"], "workItem": work,
    }), board)
    safe = next(a.payload for a in result.artifacts if a.kind == "specialist_result")
    raw = next(a.payload for a in result.artifacts if a.kind == "specialist_diagnostic")
    assert "申请在8月9日截止" not in safe["answerBrief"]
    assert safe["answerStatus"] == "NONE" and safe["missingInfo"] and safe["answerConstraints"]
    assert safe["evidenceItems"] and raw["rawAnswer"] == "申请在8月9日截止"


@pytest.mark.parametrize("answer,expected", [("FULL", "ANSWER"), ("PARTIAL", "PARTIAL_ANSWER"), ("NONE", "ABSTAIN"), ("NOT_REQUIRED", "ANSWER")])
def test_v3_action_uses_answer_status(answer, expected):
    harness = SimpleNamespace(specialist_results=[{"schemaVersion": 3, "status": "COMPLETED", "reasonCode": "RETRIEVAL_COMPLETED", "answerStatus": answer}])
    assert resolve_evaluation_action({}, harness).action == expected


def test_business_partial_and_engineering_failure_are_separate():
    result = {"schemaVersion": 3, "workItemId": "w", "status": "PARTIAL", "answerStatus": "PARTIAL",
              "reasonCode": "EVIDENCE_QUOTE_INVALID", "toolSummary": {"errorCodes": ["EVIDENCE_QUOTE_INVALID"]}}
    status, errors = _business_outcome([result], has_terminal_response=True, final_states=[{"workItemId": "w", "answerStatus": "FULL", "updatedByResponse": True}])
    assert status == "PARTIAL" and "EVIDENCE_QUOTE_INVALID" in errors


def test_response_two_reads_and_finalize_stay_inside_three_calls():
    rounds = []
    class Client:
        def complete_with_tools(self, messages, tools, *, model_round, **kwargs):
            rounds.append("tools")
            return completion(AiToolCall(str(model_round), READ.name, {
                "execution_id": "durable", "evidence_ids": [str(model_round)], "focus": "核对",
            }), content="")
        def complete_structured(self, messages, response_model, **kwargs):
            rounds.append("structured")
            return SimpleNamespace(value=response_model.model_validate({"answerText": "已核对两段原文。", "workItemUpdates": []}))
    class Executor:
        def execute(self, **kwargs):
            return ToolResult(True, "OK", data={"excerpts": [{"evidenceId": "e", "text": kwargs["arguments"]["evidence_ids"][0]}]}, dispatched=True)
    services = SimpleNamespace(settings=SimpleNamespace(), model_registry=SimpleNamespace(client_for=lambda _: Client()), tool_runtime=SimpleNamespace(executor=Executor()))
    from app.core.enums import RiskLevel
    answer, diagnostic, evidence, _ = ResponseAgent(services)._generate_candidate(
        [], RiskLevel.LOW, [READ], [{"workItemId": "w", "readableExecutionIds": ["durable"]}])
    assert rounds == ["tools", "tools", "structured"]
    assert answer == "已核对两段原文。" and len(evidence) == 2
    assert diagnostic["modelRoundCount"] == 3


def test_safety_revision_reuses_read_quota_and_visible_excerpts():
    from app.core.enums import RiskLevel
    from app.services.model_completion import ModelCompletionMetadata, ModelFinishReason, ModelCompletion
    seen = []
    class Client:
        def complete(self, messages, **kwargs):
            seen.extend(messages)
            return ModelCompletion(content="依据已读取片段修订。", metadata=ModelCompletionMetadata(
                provider="fake", model="fake", finish_reason=ModelFinishReason.STOP,
                semantic_finish_seen=True, transport_terminal_seen=True, terminal_signal="done",
                provider_finish_reason="stop", configured_output_limit=100,
            ))
        def complete_with_tools(self, *args, **kwargs):
            raise AssertionError("读取配额已耗尽，不能重启工具循环")
    services = SimpleNamespace(settings=SimpleNamespace(), model_registry=SimpleNamespace(client_for=lambda _: Client()),
                               tool_runtime=SimpleNamespace(executor=object()),
                               response_read_calls=[{"dispatched": True, "toolName": READ.name}] * 2,
                               response_read_evidence=[{"evidenceId": "e", "content": "已经核验的片段"}])
    _, diagnostic, evidence, _ = ResponseAgent(services)._generate_candidate([], RiskLevel.LOW, [READ])
    assert "已经核验的片段" in seen[-1].content
    assert diagnostic["responseToolSummary"]["dispatchCount"] == 2
    assert evidence == services.response_read_evidence
