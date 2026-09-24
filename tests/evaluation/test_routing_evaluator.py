import json
from pathlib import Path

from app.core.config import Settings
from app.evaluation.contracts import RoutingCase
from app.evaluation.evaluators.routing import ProductionRoutingEvaluator
from app.services.intent_fusion import IntentFusion
from app.services.routing_v5 import PlanningResultV5
from app.services.understanding import UnderstandingInvocationResult


ROOT = Path(__file__).resolve().parents[2]


class _NoEmbedding:
    name = "disabled"
    model = ""

    def available(self):
        return False

    def model_digest(self):
        return "disabled"


class _GoldenUnderstanding:
    def __init__(self, payload_by_message):
        self.payload_by_message = payload_by_message

    def classify(self, text, _context):
        return UnderstandingInvocationResult(PlanningResultV5.model_validate(self.payload_by_message[text]), 1, 1)


def _case(case_id, text, intents, *, hard=(), order=(), tags=()):
    fragments = [[segment] for segment, _ in intents]
    return RoutingCase.model_validate({
        "id": case_id,
        "messages": [{"role": "user", "content": text}],
        "expected": {
            "primaryIntent": intents[0][1], "intents": list(dict.fromkeys(intent for _, intent in intents)),
            "workItemCount": len(intents), "workItemIntents": [intent for _, intent in intents],
            "sourceTextFragments": fragments, "hardDataEdges": list(hard), "orderOnlyEdges": list(order),
            "missingArgumentNamesByWorkItem": [[] for _ in intents], "contextRelation": "NEW_TOPIC", "capacityExceeded": False,
        },
        "tags": list(tags),
    })


def _decision(intents, *, hard=(), order=()):
    hard_by_target = {target: source for source, target in hard}
    return {
        "workItems": [
            {
                "sourceText": text,
                "intent": intent,
                "dependsOn": [hard_by_target[index]] if index in hard_by_target else [],
            }
            for index, (text, intent) in enumerate(intents)
        ]
    }


def test_production_routing_report_uses_full_sut_and_hard_gates():
    hard_intents = [("查补考截止时间", "CAMPUS"), ("根据日期制定复习安排", "ACADEMIC")]
    order_intents = [("先查补考政策", "CAMPUS"), ("再聊聊考试焦虑", "MENTAL")]
    cases = [
        _case("single", "你好", [("你好", "CHAT")], tags=("single-goal",)),
        _case("hard", "查补考截止时间，根据日期制定复习安排", hard_intents, hard=((0, 1),), tags=("hard-data",)),
    ]
    payloads = {
        "你好": _decision([("你好", "CHAT")]),
        "查补考截止时间，根据日期制定复习安排": _decision(hard_intents, hard=((0, 1),)),
    }
    settings = Settings(_env_file=None, route_fast_enabled=False)
    evaluator = ProductionRoutingEvaluator(
        settings,
        _GoldenUnderstanding(payloads),
        fusion=IntentFusion(settings, _NoEmbedding()),
    )
    report = evaluator.evaluate(cases)
    assert "planSourceCounts" in report
    assert report["metrics"]["hardDataDependencyAccuracy"] == 1.0


def test_production_routing_report_survives_failed_single_goal_case():
    case = _case("single-error", "你好", [("你好", "CHAT")], tags=("single-goal",))
    settings = Settings(_env_file=None, route_fast_enabled=False)
    evaluator = ProductionRoutingEvaluator(
        settings,
        _GoldenUnderstanding({}),
        fusion=IntentFusion(settings, _NoEmbedding()),
    )

    report = evaluator.evaluate([case])

    assert report["passed"] is False
    assert report["errors"] == [{"caseHash": report["results"][0]["caseHash"], "errorCode": "KeyError"}]
    assert report["metrics"]["singleGoalNoOversplitRate"] == 0.0
    assert report["results"][0]["actual"] == {}
    json.dumps(report)
