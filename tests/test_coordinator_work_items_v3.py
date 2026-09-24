from types import SimpleNamespace

import pytest

from app.agents.autonomous import AcademicPlanningAgent, CampusAffairsAgent
from app.agents.coordinator import EventDrivenCoordinator
from app.agents.events import AgentArtifact, CollaborationBlackboard, TaskStatus
from app.agents.registry import AgentRegistry
from app.agents.routing import RoutePlan, WorkItem
from app.core.enums import IntentType
from app.services.clarification_models import MissingArgument


def _coordinator(agents=()) -> EventDrivenCoordinator:
    settings = SimpleNamespace(
        agent_max_rounds=12,
        agent_max_rounds_hard_limit=16,
        agent_max_claims_per_round=4,
        agent_max_claims_per_agent=3,
        agent_final_acceptance_min_confidence=0.6,
    )
    coordinator_agent = SimpleNamespace(name="CoordinatorAgent")
    return EventDrivenCoordinator(AgentRegistry(list(agents)), coordinator_agent, settings)


def _seeded_board(text: str, *, intents=(IntentType.CAMPUS,), dependent=False, clarify=False) -> CollaborationBlackboard:
    # 本文件验证调度与黑板，用显式合法计划隔离实际 Router 的分类行为。
    items = tuple(WorkItem(
        work_item_id=f"w{index + 1}", intent=intent,
        objective=f"合成工作项 {index + 1}", source_text=text,
        depends_on=("w1",) if dependent and index else (),
        missing_arguments=(MissingArgument("studentType", "POLICY_SCOPE_REQUIRED", ("本科生", "研究生")),) if clarify else (),
    ) for index, intent in enumerate(intents))
    plan = RoutePlan(
        plan_id="synthetic-plan", primary_intent=intents[0],
        intents=tuple(dict.fromkeys(intents)), work_items=items,
        synthesis_order=tuple(item.work_item_id for item in items),
        confidence=1.0, reason_codes=(), schema_version=5,
    ).as_payload()
    return (
        CollaborationBlackboard(turn_id="turn-1", user_input=text, model_input=text)
        .add_artifact(AgentArtifact(
            id="route-plan",
            owner="UnderstandingAgent",
            kind="route_plan",
            payload=plan,
        ))
        .add_artifact(AgentArtifact(
            id="risk",
            owner="SafetyAgent",
            kind="risk",
            payload={"risk": "LOW"},
        ))
    )


def _specialist_payload(plan, item, *, status="COMPLETED", reason="NO_TOOL_REQUIRED", dependency_ids=()):
    return {
        "schemaVersion": 2,
        "planId": plan.plan_id,
        "workItemId": item.work_item_id,
        "intent": item.intent.value,
        "agentName": "TestSpecialistAgent",
        "status": status,
        "objective": item.objective,
        "knownArguments": item.known_arguments,
        "answerBrief": "测试结果",
        "keyPoints": [],
        "evidenceItems": [],
        "citationRefs": [],
        "answerConstraints": [],
        "assumptions": [],
        "reasonCode": reason,
        "selectedSkillIds": [],
        "toolSummary": {},
        "dependencyResultIds": list(dependency_ids),
        "contextManifest": {},
        "confidence": 0.8 if status == "COMPLETED" else 0.0,
    }


def test_blackboard_selects_each_work_item_without_global_latest_loss() -> None:
    board = _seeded_board("查补考政策，同时制定英语复习计划", intents=(IntentType.ACADEMIC, IntentType.ACADEMIC))
    plan = RoutePlan.from_payload(board.latest_artifact("route_plan").payload)
    for index, item in enumerate(plan.work_items, 1):
        board = board.add_artifact(AgentArtifact(
            id=f"result-{index}",
            owner="TestSpecialistAgent",
            kind="specialist_result",
            payload=_specialist_payload(plan, item),
            metadata={"planId": plan.plan_id, "workItemId": item.work_item_id},
        ))

    assert board.latest_artifact_for_work_item("specialist_result", plan.work_items[0].work_item_id).id == "result-1"
    assert board.latest_artifact_for_work_item("specialist_result", plan.work_items[1].work_item_id).id == "result-2"


def test_each_work_item_rejects_a_second_terminal_result() -> None:
    board = _seeded_board("补考政策怎么查")
    plan = RoutePlan.from_payload(board.latest_artifact("route_plan").payload)
    item = plan.work_items[0]
    artifact = AgentArtifact(
        id="result-1",
        owner="CampusAffairsAgent",
        kind="specialist_result",
        payload=_specialist_payload(plan, item),
        metadata={"planId": plan.plan_id, "workItemId": item.work_item_id},
    )
    board = board.add_artifact(artifact)

    with pytest.raises(ValueError):
        board.add_artifact(AgentArtifact(**{**artifact.__dict__, "id": "result-2"}))


def test_only_coordinator_can_publish_clarification_request() -> None:
    with pytest.raises(ValueError):
        CollaborationBlackboard(turn_id="turn-1").add_artifact(AgentArtifact(
            id="clarification",
            owner="UnderstandingAgent",
            kind="clarification_request",
            payload={},
        ))


def test_clarification_pauses_plan_before_any_specialist_task() -> None:
    board = _coordinator()._derive_missing_work(_seeded_board("国家奖学金怎么申请？", clarify=True))

    assert board.latest_artifact("clarification_request") is not None
    assert not any(task.metadata.get("kind") == "specialist" for task in board.tasks.values())
    assert not any(task.metadata.get("kind") == "response" for task in board.tasks.values())


def test_dependency_gate_and_fan_in_wait_for_all_results() -> None:
    campus = CampusAffairsAgent(SimpleNamespace())
    academic = AcademicPlanningAgent(SimpleNamespace())
    coordinator = _coordinator((campus, academic))
    board = coordinator._derive_missing_work(_seeded_board("先查奖学金截止时间，再根据日期给我考研建议", intents=(IntentType.CAMPUS, IntentType.ACADEMIC), dependent=True))
    tasks = [task for task in board.tasks.values() if task.metadata.get("kind") == "specialist"]
    first = next(task for task in tasks if not task.depends_on)
    second = next(task for task in tasks if task.depends_on)

    candidates = coordinator._claim_candidates(board, {})
    assert [task.id for task, _ in candidates] == [first.id]
    first_agent = candidates[0][1].agent
    claimed = first.claim(first_agent.profile.name)
    board = board.update_task(claimed).apply_turn_result(claimed, first_agent.profile.name, first_agent.act(claimed, board))
    board = coordinator._derive_missing_work(board)

    assert board.tasks[first.id].status == TaskStatus.CLOSED
    assert not any(task.metadata.get("kind") == "response" for task in board.tasks.values())
    candidates = coordinator._claim_candidates(board, {})
    assert [task.id for task, _ in candidates] == [second.id]


def test_failed_upstream_closes_downstream_with_upstream_failed_result() -> None:
    campus = CampusAffairsAgent(SimpleNamespace())
    academic = AcademicPlanningAgent(SimpleNamespace())
    coordinator = _coordinator((campus, academic))
    board = coordinator._derive_missing_work(_seeded_board("先查奖学金截止时间，再根据日期给我考研建议", intents=(IntentType.CAMPUS, IntentType.ACADEMIC), dependent=True))
    tasks = [task for task in board.tasks.values() if task.metadata.get("kind") == "specialist"]
    first = next(task for task in tasks if not task.depends_on)
    second = next(task for task in tasks if task.depends_on)
    failed = AgentArtifact(
        id="failed-upstream",
        owner="CampusAffairsAgent",
        kind="specialist_result",
        payload=_specialist_payload(
            RoutePlan.from_payload(board.latest_artifact("route_plan").payload),
            next(item for item in RoutePlan.from_payload(board.latest_artifact("route_plan").payload).work_items if item.work_item_id == first.metadata["workItemId"]),
            status="FAILED",
            reason="TOOL_UNAVAILABLE",
        ),
        metadata={"planId": first.metadata["planId"], "workItemId": first.metadata["workItemId"]},
    )
    board = board.add_artifact(failed).update_task(first.close())
    result = academic.act(second, board)
    board = board.apply_turn_result(second, academic.name, result)

    downstream = board.latest_artifact_for_work_item("specialist_result", second.metadata["workItemId"])
    assert downstream.payload["status"] == "FAILED"
    assert downstream.payload["reasonCode"] == "UPSTREAM_FAILED"
    assert board.tasks[second.id].status == TaskStatus.CLOSED


def test_four_campus_work_items_all_close_with_one_agent():
    campus = CampusAffairsAgent(SimpleNamespace())
    coordinator = _coordinator((campus,))
    coordinator.max_claims_per_agent = 4
    text = "查补考政策，核对奖学金资格，确认重修流程，查询校园预约规定"
    board = coordinator._derive_missing_work(_seeded_board(text, intents=(IntentType.CAMPUS,) * 4))
    claim_counts = {}
    for _ in range(4):
        candidates = coordinator._claim_candidates(board, claim_counts)
        assert len(candidates) == 1
        task, candidate = candidates[0]
        claimed = task.claim(candidate.agent.profile.name)
        board = board.update_task(claimed).apply_turn_result(
            claimed,
            candidate.agent.profile.name,
            candidate.agent.act(claimed, board),
        )
        claim_counts[candidate.agent.profile.name] = claim_counts.get(candidate.agent.profile.name, 0) + 1
        board = coordinator._derive_missing_work(board)
    plan_id = board.latest_artifact("route_plan").payload["planId"]
    assert len(board.completed_work_item_ids(plan_id)) == 4
