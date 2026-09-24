from __future__ import annotations

from types import SimpleNamespace

from app.agents.coordinator import EventDrivenCoordinator
from app.agents.events import AgentArtifact, CollaborationBlackboard
from app.agents.routing import RoutePlan, WorkItem
from app.core.config import Settings
from app.core.enums import IntentType
from app.services.routing_v5 import RoutingMode


class DummyRegistry:
    pass


class DummyCoordinator:
    name = "CoordinatorAgent"


def coordinator():
    return EventDrivenCoordinator(
        DummyRegistry(),
        DummyCoordinator(),
        Settings(_env_file=None),
    )


def risk(level: str) -> AgentArtifact:
    return AgentArtifact(
        id=f"risk-{level}", owner="SafetyAgent", kind="risk",
        payload={"risk": level}, confidence=1.0,
    )


def v5_plan() -> RoutePlan:
    item = WorkItem(
        work_item_id="W1", intent=IntentType.CHAT,
        objective="处理当前 CHAT 领域用户目标：你好", source_text="你好",
        evidence_facets=(), known_arguments={}, missing_arguments=(),
        depends_on=(), priority="NORMAL", confidence=1.0, reason_codes=("GENERAL_CHAT",),
    )
    return RoutePlan(
        plan_id="plan-v5", primary_intent=IntentType.CHAT, intents=(IntentType.CHAT,),
        work_items=(item,), synthesis_order=("W1",), confidence=1.0,
        reason_codes=("GENERAL_CHAT",), schema_version=5, degraded=False,
        routing_mode=RoutingMode.NORMAL, degraded_reason=None,
    )


def test_high_risk_does_not_wait_for_route_plan():
    board = CollaborationBlackboard(turn_id="t", user_input="我不想活了").add_artifact(risk("HIGH"))
    result = coordinator()._derive_missing_work(board)
    response_tasks = [t for t in result.tasks.values() if t.metadata.get("kind") == "response"]
    assert response_tasks
    assert any(event.type.value == "SAFETY_OVERRIDE" for event in result.events)


def test_route_control_waits_for_risk_then_creates_program_response():
    control = AgentArtifact(
        id="control-1", owner="UnderstandingAgent", kind="route_control",
        payload={"fixedResponse": "请再具体说明一下。"}, confidence=1.0,
    )
    board = CollaborationBlackboard(turn_id="t", user_input="这个呢").add_artifact(control).add_artifact(risk("LOW"))
    result = coordinator()._derive_missing_work(board)
    response = result.latest_artifact("response_proposal")
    assert response is not None
    assert response.payload["directResponse"] == "请再具体说明一下。"
    assert response.payload["generationDiagnostics"]["finishReason"] == "PROGRAM_TEMPLATE"


def test_specialist_task_carries_exact_route_plan_artifact_id():
    plan = v5_plan()
    route_artifact = AgentArtifact(
        id="route-exact", owner="UnderstandingAgent", kind="route_plan",
        payload=plan.as_payload(), confidence=1.0,
    )
    board = CollaborationBlackboard(turn_id="t", user_input="你好").add_artifact(route_artifact).add_artifact(risk("LOW"))
    result = coordinator()._derive_missing_work(board)
    task = next(t for t in result.tasks.values() if t.metadata.get("kind") == "specialist")
    assert task.metadata["routePlanArtifactId"] == "route-exact"
