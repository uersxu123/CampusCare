from types import SimpleNamespace

from app.agents.coordinator import EventDrivenCoordinator
from app.agents.events import AgentArtifact, CollaborationBlackboard
from app.agents.registry import AgentRegistry
from app.agents.routing import _build_v5_normal_plan
from app.services.routing_v5 import PlannedWorkItem, PlanningResultV5


def _coordinator() -> EventDrivenCoordinator:
    settings = SimpleNamespace(
        agent_max_rounds=1,
        agent_max_claims_per_round=1,
        agent_max_claims_per_agent=1,
        agent_final_acceptance_min_confidence=0.6,
    )
    coordinator_agent = SimpleNamespace(name="CoordinatorAgent")
    return EventDrivenCoordinator(AgentRegistry([]), coordinator_agent, settings)


def test_blackboard_keeps_multiple_artifacts_of_the_same_kind() -> None:
    route = _build_v5_normal_plan("查补考政策，同时制定英语复习计划", {}, PlanningResultV5(workItems=[
        PlannedWorkItem(sourceText="查补考政策", intent="ACADEMIC", dependsOn=[]),
        PlannedWorkItem(sourceText="制定英语复习计划", intent="ACADEMIC", dependsOn=[]),
    ]))
    board = CollaborationBlackboard(turn_id="turn-1").add_artifact(AgentArtifact(
        id="route-plan", owner="UnderstandingAgent", kind="route_plan", payload=route.as_payload()
    ))
    first_item, second_item = route.work_items
    def payload(item, agent):
        return {
            "schemaVersion": 2, "planId": route.plan_id, "workItemId": item.work_item_id,
            "intent": item.intent.value, "agentName": agent, "status": "COMPLETED",
            "objective": item.objective, "knownArguments": item.known_arguments, "answerBrief": "完成",
            "keyPoints": [], "evidenceItems": [], "citationRefs": [], "answerConstraints": [],
            "assumptions": [], "reasonCode": "NO_TOOL_REQUIRED", "selectedSkillIds": [],
            "toolSummary": {}, "dependencyResultIds": [], "contextManifest": {}, "confidence": 0.8,
        }
    first = AgentArtifact(
        id="specialist-1",
        owner="CampusAffairsAgent",
        kind="specialist_result",
        payload=payload(first_item, "CampusAffairsAgent"),
        metadata={"planId": route.plan_id, "workItemId": first_item.work_item_id},
    )
    second = AgentArtifact(
        id="specialist-2",
        owner="AcademicPlanningAgent",
        kind="specialist_result",
        payload=payload(second_item, "AcademicPlanningAgent"),
        metadata={"planId": route.plan_id, "workItemId": second_item.work_item_id},
    )

    updated = board.add_artifact(first).add_artifact(second)

    assert [item.id for item in updated.artifacts_by_kind("specialist_result")] == [
        "specialist-1",
        "specialist-2",
    ]


def test_coordinator_rejects_safety_review_for_an_older_response() -> None:
    board = CollaborationBlackboard(turn_id="turn-1")
    old_response = AgentArtifact(
        id="response-old",
        owner="ResponseAgent",
        kind="response_proposal",
        payload={"messages": []},
        confidence=0.9,
    )
    current_response = AgentArtifact(
        id="response-current",
        owner="ResponseAgent",
        kind="response_proposal",
        payload={"messages": []},
        confidence=0.9,
    )
    stale_review = AgentArtifact(
        id="review-old",
        owner="SafetyAgent",
        kind="safety_review",
        payload={"approved": True},
        metadata={"responseArtifactId": old_response.id},
    )
    board = board.add_artifact(old_response).add_artifact(stale_review).add_artifact(current_response)

    result = _coordinator()._try_accept_final(board)

    assert result.final_artifact_id == ""


def test_coordinator_accepts_only_the_reviewed_current_response() -> None:
    board = CollaborationBlackboard(turn_id="turn-1")
    response = AgentArtifact(
        id="response-current",
        owner="ResponseAgent",
        kind="response_proposal",
        payload={"messages": []},
        confidence=0.9,
    )
    review = AgentArtifact(
        id="review-current",
        owner="SafetyAgent",
        kind="safety_review",
        payload={"approved": True},
        metadata={"responseArtifactId": response.id},
    )
    board = board.add_artifact(response).add_artifact(review)

    result = _coordinator()._try_accept_final(board)

    assert result.final_artifact_id == response.id
