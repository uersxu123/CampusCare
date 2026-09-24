from types import SimpleNamespace

import pytest

from app.agents.autonomous import CampusAffairsAgent, _validated_model_evidence_notes
from app.services.agent_loop import AgentLoop
from app.services.tool_models import AiToolCall, AiToolDefinition, ToolResult


RAG = AiToolDefinition("chat_readonly__rag_search", "检索", {"type": "object"})
READ = AiToolDefinition("context_readonly__read_tool_evidence", "回读", {"type": "object"})
TASK = "北湖校区学生休学、复学后如何处理助学金？"
FOLLOWUP = "北湖校区学生复学后恢复助学金的流程"


def completion(*calls, content="完成"):
    return SimpleNamespace(content=content, tool_calls=calls, verified_complete=True)


def rag_call(index, query):
    return AiToolCall(str(index), RAG.name, {"query": query})


def evidence_result(index, text, *, status="OK"):
    return ToolResult(True, "OK", data={
        "status": status, "items": [{"evidenceId": f"e{index}", "content": text}],
    }, execution_id=f"exec{index}", raw_hash=f"hash{index}", persisted=True, dispatched=True)


class Executor:
    def __init__(self, *results):
        self.results = iter(results)
        self.calls = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.results)


class Client:
    def __init__(self, *steps):
        self.steps = iter(steps)
        self.messages = []

    def complete_with_tools(self, messages, tools, **kwargs):
        self.messages.append(list(messages))
        return next(self.steps)


def test_first_query_is_trusted_and_followup_keeps_gap_query():
    client = Client(completion(rag_call(1, "错误地缩短任务")),
                    completion(rag_call(2, FOLLOWUP)), completion())
    executor = Executor(evidence_result(1, "休学期间暂停发放。"),
                        evidence_result(2, "复学后重新申请。"))
    result = AgentLoop(client=client, executor=executor, trusted_rag_query=TASK,
                       max_model_rounds=4).run(agent_name="test", messages=[], tools=[RAG])
    assert [c["arguments"]["query"] for c in executor.calls] == [TASK, FOLLOWUP]
    assert result.stop_reason == "COMPLETED" and not result.budget_stop_reason
    assert {e["evidenceId"] for e in result.visible_tool_evidence} == {"e1", "e2"}
    assert client.messages[1][-1].role == "tool"
    assert client.messages[1][-2].tool_calls[0]["arguments"]["query"] == TASK
    assert client.messages[2][-2].tool_calls[0]["arguments"]["query"] == FOLLOWUP


def test_third_rag_is_blocked_even_when_generic_tool_budget_is_higher():
    client = Client(completion(rag_call(1, TASK)), completion(rag_call(2, FOLLOWUP)),
                    completion(rag_call(3, "第三个问题")), completion())
    executor = Executor(evidence_result(1, "休学规定"), evidence_result(2, "复学规定"))
    result = AgentLoop(client=client, executor=executor, max_model_rounds=5,
                       max_tool_calls=8, max_calls_per_tool=8).run(agent_name="test", messages=[], tools=[RAG])
    assert len(executor.calls) == 2
    assert result.budget_stop_reason == "RAG_CALL_BUDGET_EXCEEDED"
    assert result.call_details[-1]["dispatched"] is False
    assert result.stop_reason == "COMPLETED"


def test_rag_cannot_be_preplanned_twice_in_one_batch():
    client = Client(completion(rag_call(1, TASK), rag_call(2, FOLLOWUP)), completion())
    executor = Executor(evidence_result(1, "首次结果"))
    result = AgentLoop(client=client, executor=executor, max_model_rounds=4).run(
        agent_name="test", messages=[], tools=[RAG])
    assert len(executor.calls) == 1
    assert result.budget_stop_reason == "RAG_REQUIRES_PREVIOUS_RESULT"


def test_followup_does_not_spend_final_answer_reserve(monkeypatch):
    client = Client(completion(rag_call(1, TASK)), completion(rag_call(2, FOLLOWUP)), completion())
    executor = Executor(evidence_result(1, "已有证据"))
    loop = AgentLoop(client=client, executor=executor, max_model_rounds=4,
                     final_answer_reserve_seconds=15)
    monkeypatch.setattr(loop, "_remaining", lambda *_: 25.0 if executor.calls else 100.0)
    result = loop.run(agent_name="test", messages=[], tools=[RAG])
    assert len(executor.calls) == 1
    assert result.budget_stop_reason == "RAG_FOLLOWUP_TIME_RESERVED"
    assert result.stop_reason == "COMPLETED"


def test_argument_rejection_does_not_consume_rag_dispatch_quota():
    client = Client(completion(rag_call(1, "初次请求")), completion(rag_call(2, "修正参数")),
                    completion(rag_call(3, FOLLOWUP)), completion())
    executor = Executor(ToolResult(False, "INVALID_ARGUMENT"),
                        evidence_result(1, "首次证据"), evidence_result(2, "补充证据"))
    result = AgentLoop(client=client, executor=executor, max_model_rounds=5,
                       trusted_rag_query=TASK).run(agent_name="test", messages=[], tools=[RAG])
    assert [c["arguments"]["query"] for c in executor.calls] == [TASK, TASK, FOLLOWUP]
    assert result.stop_reason == "COMPLETED" and not result.budget_stop_reason


@pytest.mark.parametrize("limit", [0, 1])
def test_followup_respects_total_tool_limit(limit):
    steps = [completion(rag_call(1, TASK))]
    if limit:
        steps.append(completion(rag_call(2, FOLLOWUP)))
    client = Client(*steps, completion())
    executor = Executor(evidence_result(1, "已有证据"))
    result = AgentLoop(client=client, executor=executor, max_model_rounds=4,
                       max_tool_calls=limit).run(agent_name="test", messages=[], tools=[RAG])
    assert len(executor.calls) == limit
    assert result.budget_stop_reason == "TOOL_BUDGET_EXCEEDED"
    assert result.stop_reason == "COMPLETED"


def run_specialist(results, *, read=False, followup=True):
    contract = CampusAffairsAgent.AnswerContract(
        answerBrief="休学暂停发放；复学后重新申请，其他信息尚未确认。", answerStatus="PARTIAL",
        evidenceNotes=[{"evidenceId": "e1", "quote": "休学暂停发放"},
                       {"evidenceId": "e2", "quote": "复学后重新申请"}],
        missingInfo=["其他信息尚未确认"],
    )
    steps = [completion(rag_call(1, "短查询"))]
    if followup:
        steps.append(completion(rag_call(2, FOLLOWUP)))
    if read:
        steps.append(completion(AiToolCall("read", READ.name, {
            "execution_id": "exec1", "evidence_ids": ["e1"], "focus": "补充适用条件",
        })))
    steps.append(completion(content=contract.model_dump_json()))
    client = Client(*steps)
    client.complete_structured = lambda *args, **kwargs: SimpleNamespace(value=contract)
    executor = Executor(*results)
    services = SimpleNamespace(
        tool_runtime=SimpleNamespace(registry=SimpleNamespace(definitions_for_agent=lambda _: [RAG, READ]),
                                     executor=executor),
        skill_manager=None,
        context_packet=SimpleNamespace(safety_context=None, for_specialist=lambda w, d: {"workItem": w}),
        settings=SimpleNamespace(), model_registry=SimpleNamespace(client_for=lambda _: client),
    )
    agent = CampusAffairsAgent(services)
    outcome = agent._run_loop({"workItemId": "w", "taskText": TASK, "objective": TASK}, [])
    return agent, outcome


def test_specialist_keeps_both_sources_and_validates_quotes_from_both():
    first = evidence_result(1, "休学暂停发放。")
    first.data["items"].append({"evidenceId": "e2", "content": "复学后重新申请。"})
    agent, outcome = run_specialist([first], followup=False)
    evidence, refs, summary = outcome[3], outcome[4], outcome[6]
    assert refs == ["e1", "e2"]
    assert [(e["executionId"], e["rawHash"]) for e in evidence] == [("exec1", "hash1"), ("exec1", "hash1")]
    assert summary["persistedExecutionIds"] == ["exec1"]
    assert len(_validated_model_evidence_notes(agent._last_answer_contract, agent._last_visible_evidence)) == 2


@pytest.mark.parametrize("second,expected_status,expected_reason", [
    (ToolResult(True, "OK", data={"status": "EMPTY", "items": []}, dispatched=True),
     "PARTIAL", "RAG_PARTIAL_FAILURE"),
    (ToolResult(False, "TIMEOUT", dispatched=True), "PARTIAL", "RAG_PARTIAL_FAILURE"),
    (evidence_result(2, "不可靠候选", status="DEGRADED"), "PARTIAL", "RAG_PARTIAL_FAILURE"),
])
def test_denied_followup_does_not_erase_first_evidence(second, expected_status, expected_reason):
    _, outcome = run_specialist([evidence_result(1, "休学暂停发放。"), second])
    assert outcome[:2] == (expected_status, expected_reason)
    assert [e["evidenceId"] for e in outcome[3]] == ["e1"]
    assert outcome[3][0]["executionId"] == "exec1"
    assert outcome[6]["dispatchCount"] == 1
    assert "RAG_CALL_BUDGET_EXCEEDED" in outcome[6]["errorCodes"]


def test_read_after_single_search_keeps_original_execution_reference():
    read = ToolResult(True, "OK", data={"executionId": "exec1", "rawHash": "hash1", "excerpts": [
        {"evidenceId": "e1", "text": "休学暂停发放，适用于在校学生。"},
    ]}, raw_hash="hash-of-read-response", dispatched=True)
    _, outcome = run_specialist([evidence_result(1, "休学暂停发放。"), read], read=True, followup=False)
    reread = next(e for e in outcome[3] if e.get("source") == "read_tool_evidence")
    assert reread["executionId"] == "exec1" and reread["rawHash"] == "hash1"
    assert outcome[6]["callCount"] == 2
