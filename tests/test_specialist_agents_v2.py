from dataclasses import replace
from types import SimpleNamespace

import pytest

import app.agents.autonomous as autonomous
from app.agents.autonomous import AcademicPlanningAgent
from app.agents.events import AgentArtifact, AgentTask, CollaborationBlackboard
from app.agents.result import SpecialistResultV2
from app.agents.routing import RoutePlan, WorkItem
from app.schemas.dtos import AiMessage
from app.services.tool_models import AgentLoopResult, AiToolCall, AiToolDefinition, ToolResult


class _Registry:
    def definitions_for_agent(self, _agent_name):
        return [AiToolDefinition(
            "chat_readonly__rag_search",
            "本地知识检索",
            {"type": "object", "properties": {"query": {"type": "string"}}},
        )]


class _Context:
    safety_context = None

    def for_specialist(self, work_item, dependency_results):
        return {"workItem": work_item, "dependencyResults": dependency_results}


def _agent(loop_result):
    runtime = SimpleNamespace(registry=_Registry(), executor=object())
    services = SimpleNamespace(
        tool_runtime=runtime,
        skill_manager=None,
        context_packet=_Context(),
        settings=SimpleNamespace(
            agent_loop_max_model_rounds=3,
            agent_loop_max_tool_calls=4,
            agent_loop_max_result_chars=12000,
            agent_loop_deadline_seconds=100,
        ),
        model_registry=SimpleNamespace(client_for=lambda _name: object()),
    )
    agent = AcademicPlanningAgent(services)
    return agent, loop_result


def _install_loop(monkeypatch, loop_result):
    class _Loop:
        def __init__(self, **_kwargs):
            pass

        def run(self, **_kwargs):
            return loop_result

    monkeypatch.setattr(autonomous, "AgentLoop", _Loop)


def _with_route(task: AgentTask):
    work = dict(task.metadata["workItem"])
    item = WorkItem(
        work_item_id=str(task.metadata["workItemId"]),
        intent=autonomous.IntentType.ACADEMIC,
        objective=str(work.get("objective") or "测试目标"),
        source_text=str(work.get("sourceText") or work.get("objective") or "测试输入"),
        known_arguments={str(key): str(value) for key, value in (work.get("knownArguments") or {}).items()},
        confidence=1.0,
    )
    plan = RoutePlan(
        plan_id=str(task.metadata["planId"]),
        primary_intent=autonomous.IntentType.ACADEMIC,
        intents=(autonomous.IntentType.ACADEMIC,),
        work_items=(item,),
        synthesis_order=(item.work_item_id,),
        confidence=1.0,
        reason_codes=(),
    )
    artifact = AgentArtifact(
        id="route-artifact",
        owner="UnderstandingAgent",
        kind="route_plan",
        payload=plan.as_payload(),
    )
    task = replace(task, metadata={**task.metadata, "routePlanArtifactId": artifact.id})
    board = CollaborationBlackboard(turn_id="turn-1").add_artifact(artifact)
    return task, board


@pytest.mark.parametrize(
    ("rag_status", "expected"),
    [
        ("SUFFICIENT", ("COMPLETED", "EVIDENCE_COMPLETE")),
        ("PARTIAL", ("PARTIAL", "EVIDENCE_PARTIAL")),
        ("INSUFFICIENT", ("PARTIAL", "EVIDENCE_INSUFFICIENT")),
        ("CONFLICT", ("PARTIAL", "EVIDENCE_CONFLICT")),
        ("DEGRADED", ("PARTIAL", "GRADE_UNAVAILABLE")),
    ],
)
def test_rag_business_status_mapping_is_exhaustive(monkeypatch, rag_status, expected):
    call = AiToolCall("call-1", "chat_readonly__rag_search", {"query": "奖学金条件"})
    result = ToolResult(True, "OK", data={
        "status": rag_status,
        "items": [{"evidenceId": "ev-1"}],
    })
    loop_result = AgentLoopResult("结论", ((call, result),), 2, "COMPLETED")
    agent, _ = _agent(loop_result)
    _install_loop(monkeypatch, loop_result)

    actual = agent._run_loop({"objective": "规划申请", "intent": "ACADEMIC"}, [])

    assert actual[:2] == expected
    assert actual[4] == ([] if rag_status == "DEGRADED" else ["ev-1"])


@pytest.mark.parametrize(
    "code",
    ["RAG_UNAVAILABLE", "TIMEOUT", "MCP_UNAVAILABLE", "MCP_PROTOCOL_ERROR", "CIRCUIT_OPEN"],
)
def test_rag_infrastructure_failure_never_becomes_clarification(monkeypatch, code):
    call = AiToolCall("call-1", "chat_readonly__rag_search", {"query": "奖学金条件"})
    loop_result = AgentLoopResult("", ((call, ToolResult(False, code, error="不可用")),), 1, "COMPLETED")
    agent, _ = _agent(loop_result)
    _install_loop(monkeypatch, loop_result)

    actual = agent._run_loop({"objective": "规划申请", "intent": "ACADEMIC"}, [])

    assert actual[:2] == ("FAILED", "TOOL_UNAVAILABLE")


@pytest.mark.parametrize(
    ("rag_status", "expected_evidence"),
    [("SUFFICIENT", True), ("DEGRADED", False)],
)
def test_model_error_publishes_partial_result_with_rag_evidence(monkeypatch, rag_status, expected_evidence):
    call = AiToolCall("call-1", "chat_readonly__rag_search", {"query": "奖学金条件"})
    result = ToolResult(True, "OK", data={
        "status": rag_status,
        "items": [{"evidenceId": "ev-1", "content": "真实片段"}],
    })
    loop_result = AgentLoopResult("", ((call, result),), 2, "MODEL_ERROR")
    agent, _ = _agent(loop_result)
    _install_loop(monkeypatch, loop_result)

    actual = agent._run_loop({"objective": "规划申请", "intent": "ACADEMIC"}, [])

    assert actual[:2] == ("PARTIAL", "MODEL_ERROR")
    assert actual[3] == ([{"evidenceId": "ev-1", "content": "真实片段"}] if expected_evidence else [])
    assert actual[4] == (["ev-1"] if expected_evidence else [])


def test_model_incomplete_cannot_be_marked_completed(monkeypatch):
    call = AiToolCall("call-1", "chat_readonly__rag_search", {"query": "奖学金条件"})
    result = ToolResult(True, "OK", data={
        "status": "SUFFICIENT",
        "items": [{"evidenceId": "ev-1"}],
    })
    loop_result = AgentLoopResult("", ((call, result),), 2, "MODEL_INCOMPLETE")
    agent, _ = _agent(loop_result)
    _install_loop(monkeypatch, loop_result)

    actual = agent._run_loop({"objective": "规划申请", "intent": "ACADEMIC"}, [])

    assert actual[:2] == ("PARTIAL", "MODEL_INCOMPLETE")
    assert actual[0] != "COMPLETED"


def test_model_error_still_publishes_specialist_result(monkeypatch):
    call = AiToolCall("call-1", "chat_readonly__rag_search", {"query": "奖学金条件"})
    loop_result = AgentLoopResult(
        "",
        ((call, ToolResult(True, "OK", data={"status": "SUFFICIENT", "items": [{"evidenceId": "ev-1"}]})),),
        2,
        "MODEL_ERROR",
    )
    agent, _ = _agent(loop_result)
    _install_loop(monkeypatch, loop_result)
    task = AgentTask(
        id="task-specialist",
        title="规划申请",
        description="规划申请",
        created_by="CoordinatorAgent",
        metadata={
            "kind": "specialist",
            "planId": "plan-1",
            "workItemId": "wi-1",
            "intent": "ACADEMIC",
            "workItem": {
                "workItemId": "wi-1",
                "intent": "ACADEMIC",
                "objective": "规划申请",
                "knownArguments": {},
                "missingArguments": [],
                "dependsOn": [],
            },
        },
    )

    task, board = _with_route(task)
    payload = agent.act(task, board).artifacts[0].payload

    assert payload["status"] == "PARTIAL"
    assert payload["reasonCode"] == "MODEL_ERROR"
    assert payload["evidenceItems"] == [{"evidenceId": "ev-1"}]
    # 仅检索到 ID、模型没有有效引文时，不发布为已验证引用。
    assert payload["citationRefs"] == []
    assert payload["answerStatus"] == "NONE"
    assert payload["status"] != "COMPLETED"


def test_execution_time_missing_argument_closes_with_result_without_question():
    agent = AcademicPlanningAgent(SimpleNamespace())
    task = AgentTask(
        id="task-1",
        title="规划",
        description="规划申请",
        created_by="CoordinatorAgent",
        metadata={
            "kind": "specialist",
            "planId": "plan-1",
            "workItemId": "wi-1",
            "intent": "ACADEMIC",
            "workItem": {
                "workItemId": "wi-1",
                "intent": "ACADEMIC",
                "objective": "规划申请",
                "knownArguments": {},
                "missingArguments": [{"field": "deadline"}],
                "dependsOn": [],
            },
        },
    )

    task, board = _with_route(task)
    result = agent.act(task, board)

    assert len(result.artifacts) == 1
    payload = result.artifacts[0].payload
    assert payload["status"] == "FAILED"
    assert payload["reasonCode"] == "USER_INPUT_MISSING"
    assert payload["schemaVersion"] == 3
    assert "taskKind" not in payload
    assert result.events == ()
    assert result.messages == ()


def test_study_plan_does_not_call_rag(monkeypatch):
    loop_result = AgentLoopResult("按用户截止时间制定计划", (), 1, "COMPLETED")
    agent, _ = _agent(loop_result)
    captured = {}

    class _Loop:
        def __init__(self, **_kwargs):
            pass

        def run(self, **kwargs):
            captured["tools"] = kwargs["tools"]
            captured["messages"] = kwargs["messages"]
            return loop_result

    monkeypatch.setattr(autonomous, "AgentLoop", _Loop)
    actual = agent._run_loop(
        {
            "objective": "制定数学考试计划",
            "sourceText": "帮我制定数学考试学习计划",
            "intent": "ACADEMIC",
            "knownArguments": {"course": "数学", "deadline": "8月8日 17:00"},
        },
        [],
    )

    assert captured["tools"] == []
    assert "不得列出今天已经过去的学习时段" in captured["messages"][0].content
    assert actual[:2] == ("COMPLETED", "NO_TOOL_REQUIRED")


def test_study_plan_result_keeps_user_constraints_without_hidden_subtype(monkeypatch):
    loop_result = AgentLoopResult("按用户截止时间制定计划", (), 1, "COMPLETED")
    agent, _ = _agent(loop_result)
    _install_loop(monkeypatch, loop_result)

    task = AgentTask(
            id="task-study",
            title="规划数学",
            description="规划数学",
            created_by="CoordinatorAgent",
            metadata={
                "kind": "specialist",
                "planId": "plan-1",
                "workItemId": "wi-1",
                "workItem": {
                    "workItemId": "wi-1",
                    "intent": "ACADEMIC",
                    "objective": "制定数学考试计划",
                    "sourceText": "帮我制定数学考试学习计划",
                    "knownArguments": {"course": "数学", "deadline": "8月8日 17:00"},
                    "missingArguments": [],
                    "dependsOn": [],
                },
            },
        )
    task, board = _with_route(task)
    result = agent.act(task, board)

    payload = result.artifacts[0].payload
    assert payload["schemaVersion"] == 3
    assert "taskKind" not in payload
    assert payload["knownArguments"] == {"course": "数学", "deadline": "8月8日 17:00"}


def test_specialist_result_v2_rejects_v1_and_old_task_field():
    payload = {
        "schemaVersion": 2,
        "planId": "plan-1",
        "workItemId": "wi-1",
        "intent": "ACADEMIC",
        "agentName": "AcademicPlanningAgent",
        "status": "COMPLETED",
        "objective": "制定计划",
        "knownArguments": {},
        "answerBrief": "已完成",
        "keyPoints": [],
        "evidenceItems": [],
        "citationRefs": [],
        "answerConstraints": [],
        "assumptions": [],
        "reasonCode": "NO_TOOL_REQUIRED",
        "selectedSkillIds": [],
        "toolSummary": {},
        "dependencyResultIds": [],
        "contextManifest": {},
        "confidence": 0.8,
    }
    assert SpecialistResultV2.from_payload(payload).schemaVersion == 2
    with pytest.raises(ValueError):
        SpecialistResultV2.from_payload({**payload, "schemaVersion": 1})
    with pytest.raises(ValueError):
        SpecialistResultV2.from_payload({**payload, "schemaVersion": "2"})
    with pytest.raises(ValueError):
        SpecialistResultV2.from_payload({**payload, "taskKind": "STUDY_PLAN"})

@pytest.mark.parametrize(
    ("rag_status", "items", "expected"),
    [
        ("OK", [{"evidenceId": "ev-1"}], ("COMPLETED", "RETRIEVAL_COMPLETED", ["ev-1"])),
        ("EMPTY", [], ("PARTIAL", "RETRIEVAL_EMPTY", [])),
    ],
)
def test_current_v7_rag_status_contract_remains_unchanged(monkeypatch, rag_status, items, expected):
    call = AiToolCall("call-current", "chat_readonly__rag_search", {"query": "奖学金条件"})
    result = ToolResult(True, "OK", data={"status": rag_status, "items": items})
    loop_result = AgentLoopResult("结论", ((call, result),), 2, "COMPLETED")
    agent, _ = _agent(loop_result)
    _install_loop(monkeypatch, loop_result)

    actual = agent._run_loop({"objective": "规划申请", "intent": "ACADEMIC"}, [])

    assert actual[:2] == expected[:2]
    assert actual[4] == expected[2]


def test_specialist_uses_structured_work_item_and_selected_ids_match_prompt(monkeypatch):
    loop_result = AgentLoopResult("已完成", (), 1, "COMPLETED")
    agent, _ = _agent(loop_result)
    captured = {}

    class Match:
        skill = SimpleNamespace(name="chosen_skill")
        prompt_context = "应用 skill: chosen_skill\n完整流程"
        def as_dict(self):
            return {
                "name": "chosen_skill", "prompt_context": self.prompt_context,
                "selection_mode": "scenario", "rule_score": 0.9,
            }

    class Manager:
        def select(self, request):
            captured["request"] = request
            return SimpleNamespace(
                matches=(Match(),),
                diagnostics={
                    "selectorVersion": "skill-cascade-v2", "workItemId": request.work_item_id,
                    "eligibleSkillIds": ["chosen_skill"], "candidates": [], "groups": [],
                    "qualifiedSkillIds": ["chosen_skill"], "injectedSkillIds": ["chosen_skill"],
                    "budgetRejectedSkillIds": [],
                },
            )

    class Loop:
        def __init__(self, **_kwargs):
            pass
        def run(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return loop_result

    agent.services.skill_manager = Manager()
    monkeypatch.setattr(autonomous, "AgentLoop", Loop)
    actual = agent._run_loop({
        "workItemId": "wi-1", "intent": "ACADEMIC", "taskText": "当前任务文本",
        "objective": "目标摘要", "knownArguments": {"deadline": "明天"},
        "evidenceFacets": ["不得参与匹配"],
    }, [])

    assert captured["request"].task_text == "当前任务文本"
    assert captured["request"].objective == "目标摘要"
    assert captured["request"].known_arguments == {"deadline": "明天"}
    assert "不得参与匹配" not in captured["request"].task_text
    assert "应用 skill: chosen_skill\n完整流程" in captured["messages"][0].content
    assert actual[5] == ["chosen_skill"]


def test_no_selected_skill_does_not_fail_specialist(monkeypatch):
    loop_result = AgentLoopResult("仍可正常处理", (), 1, "COMPLETED")
    agent, _ = _agent(loop_result)

    class Manager:
        def select(self, request):
            return SimpleNamespace(matches=(), diagnostics={
                "selectorVersion": "skill-cascade-v2", "workItemId": request.work_item_id,
                "eligibleSkillIds": [], "candidates": [], "groups": [], "qualifiedSkillIds": [],
                "injectedSkillIds": [], "budgetRejectedSkillIds": [],
            })

    agent.services.skill_manager = Manager()
    _install_loop(monkeypatch, loop_result)
    actual = agent._run_loop({"workItemId": "wi-1", "objective": "普通任务", "taskText": "普通任务"}, [])
    assert actual[0] == "COMPLETED"
    assert actual[5] == []


def test_selected_skill_ids_follow_actual_context_budget_injection(monkeypatch):
    from app.services.context_builder import ContextManifest, PromptBuildResult

    loop_result = AgentLoopResult("已完成", (), 1, "COMPLETED")
    agent, _ = _agent(loop_result)

    class Match:
        def __init__(self, name):
            self.skill = SimpleNamespace(name=name)
            self.prompt_context = f"应用 skill: {name}\n完整流程"
        def as_dict(self):
            return {"name": self.skill.name, "prompt_context": self.prompt_context,
                    "selection_mode": "scenario", "rule_score": 0.9}

    class Manager:
        def select(self, request):
            return SimpleNamespace(matches=(Match("kept"), Match("removed")), diagnostics={
                "selectorVersion": "skill-cascade-v2", "workItemId": request.work_item_id,
                "eligibleSkillIds": ["kept", "removed"], "candidates": [], "groups": [],
                "qualifiedSkillIds": ["kept", "removed"], "injectedSkillIds": ["kept", "removed"],
                "budgetRejectedSkillIds": [],
            })

    class Builder:
        def build_specialist_prompt(self, **_kwargs):
            return PromptBuildResult(
                messages=(AiMessage(role="system", content="应用 skill: kept\n完整流程"),),
                manifest=ContextManifest(skill_ids=("kept",)), injected_skill_ids=("kept",),
            )

    agent.services.skill_manager = Manager()
    agent.services.context_builder = Builder()
    _install_loop(monkeypatch, loop_result)
    actual = agent._run_loop({"workItemId": "wi-1", "objective": "任务", "taskText": "任务"}, [])
    assert actual[5] == ["kept"]
    assert agent._last_skill_selection["injectedSkillIds"] == ["kept"]
    assert agent._last_skill_selection["budgetRejectedSkillIds"] == ["removed"]
