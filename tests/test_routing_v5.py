from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents.routing import RoutePlan, classify_route
from app.core.config import Settings
from app.core.enums import IntentType, MAX_WORK_ITEMS
from app.services.route_planning import SemanticPlannerUnavailable
from app.services.routing_v5 import (
    FIXED_DEGRADED_CLARIFICATION,
    GlobalDegradedRouter,
    PlannedWorkItem,
    PlanningResultV5,
    PlannerValidationError,
    RoutingMode,
    primary_rule_scores,
    validate_planning_result,
)
from app.services.understanding import UnderstandingInvocationResult


def invocation(items: list[dict], provider_attempts: int = 1) -> UnderstandingInvocationResult:
    return UnderstandingInvocationResult(
        PlanningResultV5.model_validate({"workItems": items}),
        provider_attempts,
        5,
    )


def test_v5_normal_planner_is_minimal_and_skips_fallback():
    calls = 0

    def planner(text, context):
        nonlocal calls
        calls += 1
        assert text == "办理休学，同时问助学金是否停发"
        assert context["current_goal"] == "继续处理学生事务"
        return invocation([
            {"sourceText": "办理休学", "intent": "ACADEMIC", "dependsOn": []},
            {"sourceText": "助学金是否停发", "intent": "CAMPUS", "dependsOn": []},
        ])

    decision = classify_route(
        "办理休学，同时问助学金是否停发",
        {"current_goal": "继续处理学生事务"},
        semantic_classifier=planner,
        settings=Settings(_env_file=None, route_fast_enabled=False),
    )
    assert calls == 1
    plan = decision.route_plan
    assert plan is not None
    assert plan.schema_version == 5
    assert plan.routing_mode is RoutingMode.NORMAL
    assert plan.degraded is False
    assert [item.intent for item in plan.work_items] == [IntentType.ACADEMIC, IntentType.CAMPUS]
    assert all(item.evidence_facets == () for item in plan.work_items)
    assert plan.work_items[0].work_item_id == "W1"
    assert plan.work_items[1].work_item_id == "W2"


def test_v5_dependency_indexes_are_converted_to_ids():
    decision = classify_route(
        "先查政策，再基于政策制定方案",
        semantic_classifier=lambda *_: invocation([
            {"sourceText": "查政策", "intent": "CAMPUS", "dependsOn": []},
            {"sourceText": "基于政策制定方案", "intent": "ACADEMIC", "dependsOn": [0]},
        ]),
        settings=Settings(_env_file=None, route_fast_enabled=False),
    )
    assert decision.route_plan is not None
    assert decision.route_plan.work_items[1].depends_on == ("W1",)


def test_v5_invalid_dependency_retries_once_then_degrades(monkeypatch):
    calls = 0

    def planner(*_):
        nonlocal calls
        calls += 1
        return invocation([
            {"sourceText": "怎么办理休学", "intent": "ACADEMIC", "dependsOn": [0]},
        ])

    class NoEmbedding:
        def scores(self, text):
            return None

    # Keep this test deterministic and independent from a real embedding service.
    monkeypatch.setattr(
        "app.agents.routing.GlobalDegradedRouter",
        lambda settings: GlobalDegradedRouter(settings, embedding_router=NoEmbedding()),
    )
    decision = classify_route(
        "怎么办理休学",
        semantic_classifier=planner,
        settings=Settings(_env_file=None, route_fast_enabled=False),
    )
    assert calls == 2
    assert decision.route_plan is not None
    assert decision.route_plan.routing_mode is RoutingMode.DEGRADED_DIRECT
    assert decision.route_plan.intents == (IntentType.ACADEMIC,)


def test_v5_model_failure_zero_intent_returns_program_clarification(monkeypatch):
    class NoEmbedding:
        def scores(self, text):
            return None

    monkeypatch.setattr(
        "app.agents.routing.GlobalDegradedRouter",
        lambda settings: GlobalDegradedRouter(settings, embedding_router=NoEmbedding()),
    )

    def planner(*_):
        raise SemanticPlannerUnavailable("down")

    decision = classify_route("这个怎么弄", semantic_classifier=planner, settings=Settings(_env_file=None, route_fast_enabled=False))
    assert decision.route_plan is None
    assert decision.fixed_response == FIXED_DEGRADED_CLARIFICATION
    assert decision.diagnostics.logical_invocation_count == 2


def test_v5_too_many_work_items_is_control_not_global_degraded():
    items = [
        {"sourceText": f"目标{i}", "intent": "CHAT", "dependsOn": []}
        for i in range(MAX_WORK_ITEMS + 1)
    ]
    decision = classify_route(
        "很多目标",
        semantic_classifier=lambda *_: invocation(items),
        settings=Settings(_env_file=None, route_fast_enabled=False),
    )
    assert decision.route_plan is None
    assert decision.diagnostics.plan_source == "LLM_LIMIT"
    assert "一次最多处理" in (decision.fixed_response or "")


def test_v5_rule_boundary_examples_follow_new_taxonomy():
    assert primary_rule_scores("怎么办理休学")[IntentType.ACADEMIC] >= 0.9
    assert primary_rule_scores("休学以后国家助学金还发吗")[IntentType.CAMPUS] >= 0.9
    assert primary_rule_scores("补考流程是什么")[IntentType.ACADEMIC] >= 0.9
    assert primary_rule_scores("心理咨询怎么预约")[IntentType.MENTAL] >= 0.9
    assert primary_rule_scores("我想申请调宿换寝室")[IntentType.CAMPUS] >= 0.9


def test_rule_only_can_broadcast_multiple_primary_intents():
    class NoEmbedding:
        def scores(self, text):
            return None

    router = GlobalDegradedRouter(Settings(_env_file=None, route_fast_enabled=False), embedding_router=NoEmbedding())
    result = router.route("最近焦虑睡不着，我想休学，休学后助学金还发吗")
    assert result.mode is RoutingMode.DEGRADED_BROADCAST
    assert set(result.selected) >= {IntentType.MENTAL, IntentType.ACADEMIC, IntentType.CAMPUS}


def test_validator_rejects_self_forward_and_duplicate_dependencies():
    with pytest.raises(PlannerValidationError):
        validate_planning_result(PlanningResultV5(workItems=[PlannedWorkItem(sourceText="a", intent="CHAT", dependsOn=[0])]))
    with pytest.raises(PlannerValidationError):
        validate_planning_result(PlanningResultV5(workItems=[
            PlannedWorkItem(sourceText="a", intent="CHAT", dependsOn=[]),
            PlannedWorkItem(sourceText="b", intent="CHAT", dependsOn=[0, 0]),
        ]))


def test_route_plan_v5_round_trip_and_v3_read_compatibility():
    decision = classify_route(
        "你好",
        semantic_classifier=lambda *_: invocation([{"sourceText": "你好", "intent": "CHAT", "dependsOn": []}]),
        settings=Settings(_env_file=None, route_fast_enabled=False),
    )
    payload = decision.as_payload()
    assert RoutePlan.from_payload(payload).as_payload() == payload

    v3 = dict(payload)
    v3.pop("degraded")
    v3.pop("routingMode")
    v3.pop("degradedReason")
    v3["schemaVersion"] = 3
    assert RoutePlan.from_payload(v3).routing_mode is RoutingMode.NORMAL
