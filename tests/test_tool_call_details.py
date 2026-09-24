import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.entities import AgentRunTrace, ChatSession, ChatTurn, UserAccount
from app.schemas.dtos import AiMessage
from app.services.agent_loop import AgentLoop
from app.services.tool_call_details import bounded_arguments, rerank_status
from app.services.tool_models import AiToolCall, AiToolDefinition, ToolResult
from app.services.trace import AgentTraceService, _trace_tool_diagnostics


TOOL = AiToolDefinition("chat_readonly__get_current_weather", "天气", {"type": "object"})
RAG = AiToolDefinition("chat_readonly__rag_search", "知识检索", {"type": "object"})


def completion(*calls, verified=True):
    return SimpleNamespace(content="" if calls else "完成", tool_calls=calls, verified_complete=verified)


class Client:
    def __init__(self, *steps):
        self.steps = iter(steps)
        self.messages = []

    def complete_with_tools(self, messages, tools, **kwargs):
        self.messages.append(list(messages))
        step = next(self.steps)
        if isinstance(step, Exception):
            raise step
        return step


class Executor:
    def __init__(self, result):
        self.result = result
        self.count = 0

    def execute(self, **kwargs):
        self.count += 1
        return self.result


def run(client, result=None, tools=None, **kwargs):
    executor = Executor(result or ToolResult(True, "OK", data={"temperature": 20}))
    output = AgentLoop(client=client, executor=executor, **kwargs).run(agent_name="GeneralChatAgent", messages=[AiMessage(role="user", content="天气")], tools=tools or [TOOL])
    return output, executor


def call(index=1, name=TOOL.name, **arguments):
    return AiToolCall(f"call-{index}", name, arguments or {"location": "武汉"})


@pytest.mark.parametrize("result,status", [
    (ToolResult(True, "OK"), "SUCCEEDED"),
    (ToolResult(True, "OK", cached=True), "SUCCEEDED"),
    (ToolResult(False, "TIMEOUT", degraded=True, error="工具超时"), "TIMEOUT"),
    (ToolResult(False, "MCP_UNAVAILABLE", degraded=True), "FAILED"),
    (ToolResult(False, "CIRCUIT_OPEN", degraded=True), "REJECTED"),
    (ToolResult(False, "INVALID_ARGUMENT", error="secret raw parameters"), "REJECTED"),
])
def test_results_and_timing_are_recorded_without_model_payload_changes(monkeypatch, result, status):
    ticks = iter([1.0, 1.025])
    monkeypatch.setattr("app.services.tool_call_details.time.perf_counter", lambda: next(ticks))
    client = Client(completion(call()), completion())
    output, executor = run(client, result)
    detail, = output.call_details
    assert output.stop_reason == "COMPLETED"
    assert detail["status"] == status
    assert detail["durationMs"] == 25.0
    assert detail["cached"] == result.cached
    assert detail["degraded"] == result.degraded
    assert detail["argumentsSummary"] == {"location": "武汉"}
    assert detail["startedAt"].endswith("+00:00")
    assert json.loads(client.messages[1][-1].content) == result.as_payload()
    assert executor.count == len(output.tool_results) == 1
    if result.code == "INVALID_ARGUMENT":
        assert "secret" not in detail["errorMessage"]


@pytest.mark.parametrize("followup,reason", [(RuntimeError("failed"), "MODEL_ERROR"), (completion(verified=False), "MODEL_INCOMPLETE")])
def test_followup_failure_keeps_completed_call(followup, reason):
    output, _ = run(Client(completion(call()), followup))
    assert output.stop_reason == reason
    assert len(output.call_details) == 1
    assert output.call_details[0]["success"] is True


@pytest.mark.parametrize("kwargs,reason,count", [
    ({"max_tool_calls": 0}, "COMPLETED", 0),
    ({"max_result_chars": 1}, "TOOL_RESULT_BUDGET_EXCEEDED", 1),
    ({"max_model_rounds": 1}, "FINALIZE_TOOL_CALL_REJECTED", 0),
])
def test_budgets_preserve_existing_behavior_and_record_attempts(kwargs, reason, count):
    output, executor = run(Client(completion(call()), completion()), **kwargs)
    assert output.stop_reason == reason
    assert executor.count == count
    assert sum(result.ok for _, result in output.tool_results) == count
    assert len(output.call_details) == 1
    assert output.call_details[0]["status"] == ("NOT_EXECUTED" if count == 0 else "SUCCEEDED")


def test_duplicate_and_parallel_rejection_do_not_count_as_execution():
    output, executor = run(Client(completion(call()), completion(call(2)), completion()))
    assert output.stop_reason == "COMPLETED"
    assert output.budget_stop_reason == "DUPLICATE_TOOL_CALL"
    assert executor.count == 1
    assert [item["status"] for item in output.call_details] == ["SUCCEEDED", "NOT_EXECUTED"]
    output, executor = run(Client(completion(call(), call(2), call(3))))
    assert output.stop_reason == "PARALLEL_TOOL_BUDGET_EXCEEDED"
    assert executor.count == len(output.tool_results) == 0
    assert len(output.call_details) == 3


def test_denied_tool_and_two_rag_calls_are_recorded():
    output, executor = run(Client(completion(call(name="not_allowed")), completion()))
    assert executor.count == 0
    assert output.call_details[0]["status"] == "REJECTED"
    output, executor = run(Client(completion(call(name=RAG.name, query="补考")), completion(call(2, name=RAG.name, query="重修")), completion()), tools=[RAG])
    assert output.stop_reason == "COMPLETED"
    assert output.budget_stop_reason == ""
    assert executor.count == 2
    assert [item["status"] for item in output.call_details] == ["SUCCEEDED", "SUCCEEDED"]


def test_deadline_and_result_budget_keep_prior_calls(monkeypatch):
    client = Client(completion(call()), completion())
    loop = AgentLoop(client=client, executor=Executor(ToolResult(True, "OK")))
    monkeypatch.setattr(loop, "_remaining", lambda started: 0.0 if loop.executor.count else 10.0)
    output = loop.run(agent_name="GeneralChatAgent", messages=[], tools=[TOOL])
    assert output.stop_reason == "DEADLINE_EXCEEDED"
    assert len(output.call_details) == 1
    assert output.call_details[0]["status"] == "SUCCEEDED"
    output, executor = run(Client(completion(call(), call(2, location="北京"))), max_result_chars=1)
    assert executor.count == 1
    assert [item["status"] for item in output.call_details] == ["SUCCEEDED", "NOT_EXECUTED"]


@pytest.mark.parametrize("result,expected", [
    (ToolResult(True, "OK", data={"diagnostics": {"rerankDegraded": False, "rerankAssessmentCount": 2}}), "SUCCEEDED"),
    (ToolResult(True, "OK", data={"diagnostics": {"rerankDegraded": False, "rerankAssessmentCount": 0}}), "SKIPPED"),
    (ToolResult(True, "OK", data={"diagnostics": {"rerankDegraded": True}}), "FAILED"),
    (ToolResult(True, "OK", cached=True), "NOT_EXECUTED_CACHED"),
    (ToolResult(True, "OK", data={}), "UNKNOWN"),
])
def test_rerank_is_not_inferred_from_success_alone(result, expected):
    assert rerank_status(RAG.name, result) == expected
    assert rerank_status(TOOL.name, result) == "NOT_APPLICABLE"


def test_parameter_redaction_and_size_do_not_mutate_call():
    arguments = {"password": "private-value", "nested": {"api_key": "key-value"}, "query": "联系 test@example.com 13800138000 token=private-token " + "长" * 1000}
    safe = bounded_arguments(arguments)
    rendered = json.dumps(safe, ensure_ascii=False)
    assert not any(secret in rendered for secret in ("private-value", "key-value", "private-token", "test@example.com", "13800138000"))
    assert arguments["password"] == "private-value"
    assert len(json.dumps(bounded_arguments({str(i): "长" * 1000 for i in range(100)}), ensure_ascii=False)) <= 3100


def test_existing_trace_persists_details_and_request_link_after_reopen(tmp_path):
    output, _ = run(Client(completion(call()), completion()))
    diagnostics = {"workItems": [{"workItemId": "w1", "agentName": "GeneralChatAgent", "status": "COMPLETED", "reasonCode": "TOOL_COMPLETE", "toolSummary": {"usedTools": [TOOL.name], "callCount": 1, "calls": list(output.call_details)}}], "totalCallCount": 1}
    url = "sqlite+pysqlite:///" + (tmp_path / "trace.sqlite").as_posix()
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = UserAccount(username="student", display_name="学生", password_hash="unused")
        db.add(user)
        db.flush()
        session = ChatSession(public_id="s1", title="测试", user_id=user.id)
        db.add(session)
        db.flush()
        service = AgentTraceService(db, SimpleNamespace(trace_include_prompt_content=False))
        trace = service.create_minimal_trace(user=user, session=session, original_input="天气", sanitized_input="天气")
        service.finalize_generation_trace(trace.id, {}, diagnostics, commit=True)
        db.add(ChatTurn(public_id="turn-1", request_id="req-1", user_id=user.id, session_id=session.id, trace_id=trace.id))
        db.commit()
        user_id = user.id
    engine.dispose()
    engine = create_engine(url)
    with Session(engine) as db:
        turn = db.scalar(select(ChatTurn).where(ChatTurn.user_id == user_id, ChatTurn.request_id == "req-1"))
        saved = json.loads(db.get(AgentRunTrace, turn.trace_id).tool_diagnostics_json)
        assert saved["workItems"][0]["toolSummary"]["calls"] == list(output.call_details)
        assert saved["totalCallCount"] == 1
    engine.dispose()
    assert _trace_tool_diagnostics({})["workItems"] == []


def test_runtime_keeps_details_out_of_specialist_artifact_and_isolates_turns(monkeypatch):
    import app.agents.event_driven_runtime as runtime_module
    from app.agents.autonomous import GeneralChatAgent
    from app.agents.events import AgentArtifact, AgentTask
    from app.agents.routing import RoutePlan, WorkItem
    from app.core.enums import IntentType

    clients = []
    packet = SimpleNamespace(manifest=SimpleNamespace(degraded_sources=[]), safety_context=None, as_payload=lambda: {}, for_specialist=lambda work, dependencies: {"workItem": work})
    runtime = object.__new__(runtime_module.EventDrivenAgentRuntimeService)
    runtime.db = None
    runtime.settings = SimpleNamespace()
    runtime.ai = None
    runtime.knowledge = None
    runtime.context_builder = SimpleNamespace(build_base_context=lambda **kwargs: packet)
    runtime.skill_manager = None
    runtime.tool_runtime = SimpleNamespace(registry=SimpleNamespace(definitions_for_agent=lambda name: [TOOL]), executor=Executor(ToolResult(True, "OK")))

    def client_for(name):
        client = Client(completion(call()), completion())
        clients.append(client)
        return client

    runtime.model_registry = SimpleNamespace(client_for=client_for)
    work = {"workItemId": "w1", "sourceText": "武汉天气", "objective": "查询天气", "intent": "CHAT", "knownArguments": {}, "missingArguments": [], "dependsOn": []}

    class Coordinator:
        def __init__(self, registry, coordinator_agent, settings):
            self.services = coordinator_agent.services

        def run(self, board):
            plan = RoutePlan(
                plan_id="plan1",
                primary_intent=IntentType.CHAT,
                intents=(IntentType.CHAT,),
                work_items=(WorkItem("w1", IntentType.CHAT, "查询天气", "查询天气"),),
                synthesis_order=("w1",),
                confidence=1.0,
                reason_codes=(),
            )
            route = AgentArtifact("route-1", "UnderstandingAgent", "route_plan", plan.as_payload())
            board = board.add_artifact(route)
            task = AgentTask(id="task1", title="天气", metadata={"kind": "specialist", "planId": "plan1", "workItemId": "w1", "intent": "CHAT", "workItem": work, "routePlanArtifactId": route.id})
            artifact = GeneralChatAgent(self.services).act(task, board).artifacts[0]
            assert "calls" not in artifact.payload["toolSummary"]
            assert "argumentsSummary" not in json.dumps(artifact.payload)
            return artifact

    def to_result(board):
        payload = board.payload
        assert "calls" not in payload["toolSummary"]
        return SimpleNamespace(tool_diagnostics=runtime_module._tool_diagnostics([payload]))

    monkeypatch.setattr(runtime_module, "EventDrivenCoordinator", Coordinator)
    runtime._to_result = to_result
    for _ in range(2):
        result = runtime.run(
            SimpleNamespace(id=1),
            SimpleNamespace(public_id="session1"),
            "天气",
            "天气",
            SimpleNamespace(),
            packet,
        )
        summary = result.tool_diagnostics["workItems"][0]["toolSummary"]
        assert summary["callCount"] == 1
        assert len(summary["calls"]) == 1
        assert summary["calls"][0]["toolCallId"] == "call-1"
    assert all("argumentsSummary" not in json.dumps([message.content for message in messages]) for client in clients for messages in client.messages)


def test_save_run_also_preserves_details_without_opening_prompt_redaction(tmp_path):
    from app.agents.result import AgentRunResult
    from app.core.enums import IntentType, RiskLevel

    output, _ = run(Client(completion(call()), completion()))
    diagnostics = {"workItems": [{"workItemId": "w1", "agentName": "GeneralChatAgent", "toolSummary": {"calls": list(output.call_details)}}]}
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = UserAccount(username="student", display_name="学生", password_hash="unused")
        db.add(user)
        db.flush()
        session = ChatSession(public_id="s1", title="测试", user_id=user.id)
        db.add(session)
        db.flush()
        result = AgentRunResult(primary_intent=IntentType.CHAT, intents=(IntentType.CHAT,), risk_level=RiskLevel.LOW, assessment=None, response_messages=[], steps=[], memory_brief="", route_plan={}, specialist_results=[], evidence_items=[], tool_diagnostics=diagnostics)
        service = AgentTraceService(db, SimpleNamespace(trace_include_prompt_content=False))
        trace = service.save_run(user, session, "天气", "天气", "", result, None)
        assert json.loads(trace.tool_diagnostics_json)["workItems"][0]["toolSummary"]["calls"] == list(output.call_details)
        from app.services.trace import _json
        assert json.loads(_json({"arguments": {"password": "secret"}}))["arguments"] == "[REDACTED]"
    engine.dispose()
