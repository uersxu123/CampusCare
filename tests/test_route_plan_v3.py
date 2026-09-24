"""当前 Planner 合同；V3 历史读取兼容另见 test_routing_v5。"""
import copy

import pytest

from app.agents.routing import RoutePlan, classify_route
from app.core.config import Settings
from app.core.enums import IntentType
from app.services.routing_v5 import PlanningResultV6
from app.services.understanding import UnderstandingInvocationResult


def invoke(intent="CHAT", text="你好"):
    return UnderstandingInvocationResult(PlanningResultV6.model_validate({
        "schemaVersion": 6, "workItems": [{
            "intent": intent, "objective": "处理当前目标", "taskText": text,
            "sourceRefs": ["current:0"], "contextRefs": [], "dependsOn": [],
        }],
    }), 1, 3)


def plan(text, intent):
    return classify_route(text, semantic_classifier=lambda *_: invoke(intent, text),
                          settings=Settings(_env_file=None, route_fast_enabled=False)).route_plan


@pytest.mark.parametrize(("text", "intent"), [
    ("你好", "CHAT"), ("下月三门考试怎么复习", "ACADEMIC"),
    ("补考需要交什么材料", "ACADEMIC"), ("申请调宿需要什么材料", "CAMPUS"),
    ("最近焦虑得睡不着", "MENTAL"),
])
def test_current_planner_preserves_domain_and_task(text, intent):
    result = plan(text, intent)
    assert result.primary_intent == IntentType(intent)
    assert result.schema_version == 6
    assert result.work_items[0].task_text == text
    assert result.work_items[0].source_refs == ("current:0",)
    assert "taskKind" not in str(result.as_payload())


def test_planner_does_not_own_risk_classification():
    # 当前 Planner 仅四个主领域，风险由独立 Safety 负责。
    with pytest.raises(ValueError):
        invoke("RISK", "我现在想自杀")
    result = plan("我现在想自杀", "MENTAL")
    assert result.intents == (IntentType.MENTAL,)


def test_study_plan_arguments_are_boolean_policy_not_route_subtype():
    item = plan("帮我制定学习计划", "ACADEMIC").work_items[0]
    assert [arg.name for arg in item.missing_arguments] == ["course", "deadline"]
    assert plan("给我一些学习建议", "ACADEMIC").work_items[0].missing_arguments == ()


def test_route_plan_strictly_rejects_v2_task_kind_unknowns_and_cycles():
    payload = plan("你好", "CHAT").as_payload()
    assert RoutePlan.from_payload(payload).as_payload() == payload
    for change in ("version", "taskKind", "unknown", "cycle"):
        bad = copy.deepcopy(payload)
        if change == "version":
            bad["schemaVersion"] = 2
        elif change == "taskKind":
            bad["workItems"][0]["taskKind"] = "GENERAL_CHAT"
        elif change == "unknown":
            bad["legacyRoute"] = "x"
        else:
            bad["workItems"][0]["dependsOn"] = [bad["workItems"][0]["workItemId"]]
        with pytest.raises(ValueError):
            RoutePlan.from_payload(bad)


def test_ids_are_stable_for_identical_input():
    first = plan("补考材料怎么交", "ACADEMIC")
    second = plan("补考材料怎么交", "ACADEMIC")
    assert first.plan_id == second.plan_id
    assert first.work_items[0].work_item_id == second.work_items[0].work_item_id


def test_internal_input_limit_is_not_truncated():
    with pytest.raises(ValueError):
        plan("中" * 1001, "CHAT")


def test_one_work_item_preserves_compound_task_without_facets():
    text = "我拿了国家奖学金，因病休学后还能拿国家助学金吗，复学后怎么办"
    result = plan(text, "CAMPUS")
    assert len(result.work_items) == 1
    item = RoutePlan.from_payload(result.as_payload()).work_items[0]
    assert item.task_text == text
    assert item.evidence_facets == ()
    assert item.source_refs == ("current:0",)
