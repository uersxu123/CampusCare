"""Planner 调用计数与降级：测试替身使用当前 V6 输出。"""
import pytest

from app.agents.routing import classify_route
from app.core.config import Settings
from app.core.enums import IntentType, MAX_WORK_ITEMS
from app.services.ai import StructuredCompletionError
from app.services.route_planning import SemanticPlannerUnavailable, SemanticStructuredOutputInvalid
from app.services.routing_v5 import PlanningResultV6, RoutingScoreSnapshot, primary_rule_scores
from app.services.understanding import UnderstandingInvocationResult, UnderstandingService


class RuleOnlyScorer:
    def score(self, text):
        rules = primary_rule_scores(text)
        return RoutingScoreSnapshot({key: value or 0.0 for key, value in rules.items()}, rules, None, False)


def result(text, intent="ACADEMIC", count=1):
    return UnderstandingInvocationResult(PlanningResultV6.model_validate({
        "schemaVersion": 6,
        "workItems": [{"intent": intent, "objective": "处理当前目标", "taskText": text,
                       "sourceRefs": ["current:0"], "contextRefs": [], "dependsOn": []}
                      for _ in range(count)],
    }), 1, 2)


def route(text, classifier):
    return classify_route(text, semantic_classifier=classifier,
                          settings=Settings(_env_file=None, route_fast_enabled=False),
                          routing_scorer=RuleOnlyScorer())


def test_valid_planner_result_needs_one_logical_invocation():
    calls = []
    def classifier(value, context):
        calls.append((value, context))
        return result(value)
    decision = route("补考需要什么材料", classifier)
    assert len(calls) == 1
    assert decision.diagnostics.logical_invocation_count == 1
    assert decision.diagnostics.provider_attempt_count == 1
    assert decision.diagnostics.plan_source == "LLM_ACCEPTED"


def test_llm_route_wins_over_rule_suspected_capacity():
    text = "查补考政策，同时核对奖学金资格，同时制定英语复习计划，同时总结执行清单，同时解释申请流程"
    decision = route(text, lambda value, _: result(value))
    assert decision.route_plan.primary_intent == IntentType.ACADEMIC
    assert decision.diagnostics.plan_source == "LLM_ACCEPTED"


def test_current_schema_rejects_capacity_overflow_before_route_construction():
    with pytest.raises(ValueError, match="at most 4 items"):
        result("很多独立目标", "CHAT", MAX_WORK_ITEMS + 1)
    # V5 历史接口的容量控制返回由 test_routing_v5 单独覆盖。


def test_model_failure_uses_current_rule_pipeline_with_bounded_attempts():
    calls = []
    def classifier(*_):
        calls.append(1)
        raise SemanticStructuredOutputInvalid("bad", 1, 9)
    decision = route("补考需要什么材料", classifier)
    assert len(calls) == 2
    assert decision.route_plan.primary_intent == IntentType.ACADEMIC
    assert decision.route_plan.degraded
    assert decision.diagnostics.fallback_reason == "STRUCTURED_OUTPUT_INVALID"
    assert decision.diagnostics.provider_attempt_count == 2


def test_danger_text_does_not_bypass_current_planner_contract():
    calls = []
    def classifier(value, _):
        calls.append(value)
        return result(value, "MENTAL")
    decision = route("我现在想自杀", classifier)
    assert calls == ["我现在想自杀"]
    assert decision.route_plan.primary_intent == IntentType.MENTAL
    # 独立 Safety 的零模型高风险判定由 test_routing_safety_boundaries 验证。


@pytest.mark.parametrize(("code", "attempts"), [
    ("MODEL_UNAVAILABLE", 0), ("STRUCTURED_OUTPUT_UNSUPPORTED", 0),
    ("PROVIDER_REQUEST_FAILED", 1),
])
def test_understanding_configuration_and_request_failures_count_separately(code, attempts):
    class Client:
        def complete_structured(self, *_args, **_kwargs):
            raise StructuredCompletionError(code, "合成故障")
    with pytest.raises(SemanticPlannerUnavailable) as captured:
        UnderstandingService(Client(), Settings(_env_file=None)).classify("查询补考要求", {})
    assert captured.value.provider_attempt_count == attempts


def test_http_schema_refusal_still_counts_a_dispatched_request():
    import httpx
    request = httpx.Request("POST", "http://synthetic.invalid")
    response = httpx.Response(400, request=request)
    class Client:
        def complete_structured(self, *_args, **_kwargs):
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise StructuredCompletionError("STRUCTURED_OUTPUT_UNSUPPORTED", "HTTP refusal") from exc
    with pytest.raises(SemanticPlannerUnavailable) as captured:
        UnderstandingService(Client(), Settings(_env_file=None)).classify("查询补考要求", {})
    assert captured.value.provider_attempt_count == 1
