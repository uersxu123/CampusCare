import unittest

from app.agents.routing import classify_route
from app.core.enums import IntentType, RiskLevel
from app.services.assessment import PsychologicalAssessmentService
from app.core.config import Settings
from app.services.routing_v5 import PlanningResultV6, RoutingScoreSnapshot, primary_rule_scores
from app.services.understanding import UnderstandingInvocationResult


class RuleOnlyScorer:
    def score(self, text):
        rules = primary_rule_scores(text)
        return RoutingScoreSnapshot({key: value or 0.0 for key, value in rules.items()}, rules, None, False)


class ExplodingAi:
    def complete(self, messages):
        raise RuntimeError("model unavailable")


class RoutingSafetyBoundaryTests(unittest.TestCase):
    def test_software_crash_does_not_hide_personal_distress(self):
        service = PsychologicalAssessmentService(ExplodingAi())
        for text in ("浏览器突然崩溃了，怎么修复？", "APP 又崩溃了"):
            with self.subTest(text=text):
                self.assertEqual(service.assess(text).risk, RiskLevel.LOW)
        self.assertEqual(service.assess("程序崩溃了，我也快崩溃了，很难过。").risk, RiskLevel.MEDIUM)
        self.assertEqual(service.assess("程序崩溃了，我想结束生命。").risk, RiskLevel.HIGH)

    def test_technical_context_stays_chat(self):
        for text in ("帮我写一个服务器压力测试脚本。", "这个接口的压测结果怎么看？", "Python 程序崩溃了怎么排查？"):
            with self.subTest(text=text):
                # 固定 Planner 合法返回，只验证计划传递；风险误报另由真实 Safety 规则验证。
                invocation = UnderstandingInvocationResult(PlanningResultV6.model_validate({
                    "schemaVersion": 6, "workItems": [{"intent": "CHAT", "objective": "处理技术问题",
                        "taskText": text, "sourceRefs": ["current:0"], "contextRefs": [], "dependsOn": []}],
                }), 1, 0)
                decision = classify_route(text, semantic_classifier=lambda *_: invocation,
                    settings=Settings(_env_file=None, route_fast_enabled=False))
                self.assertEqual(decision.route_plan.primary_intent, IntentType.CHAT)
                self.assertEqual(PsychologicalAssessmentService(ExplodingAi()).assess(text).risk, RiskLevel.LOW)

    def test_quoted_analytical_and_negated_context_is_not_risk(self):
        for text in (
            "我在论文中分析自杀预防政策。", "这部电影里的角色说他不想活了，这句怎么翻译？",
            "请概括这段关于自残干预的文献。", "我没有自杀或自残的想法，只是想了解相关政策。",
        ):
            with self.subTest(text=text):
                self.assertNotEqual(PsychologicalAssessmentService(ExplodingAi()).assess(text).risk, RiskLevel.HIGH)

    def test_current_danger_is_always_risk(self):
        for text in ("我不想活了。", "我想结束生命。", "我最近真的撑不下去了，想彻底消失。", "我已经准备好要伤害自己了。"):
            with self.subTest(text=text):
                class MustNotCallModel:
                    def complete(self, *_args, **_kwargs):
                        raise AssertionError("明确当前危险应在模型调用前被识别")
                from unittest.mock import Mock
                client = MustNotCallModel()
                client.complete = Mock(side_effect=client.complete)
                self.assertEqual(PsychologicalAssessmentService(client).assess(text).risk, RiskLevel.HIGH)
                client.complete.assert_not_called()

    def test_current_rule_taxonomy_without_embedding_or_model(self):
        cases = (
            ("最近焦虑睡不着，想聊聊。", IntentType.MENTAL),
            ("我在考研、就业和实习之间拿不定主意。", IntentType.ACADEMIC),
            ("挂科后还能补考吗？", IntentType.ACADEMIC),
            ("南望山校区怎么办理调宿？", IntentType.CAMPUS),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                decision = classify_route(text, routing_scorer=RuleOnlyScorer(),
                    settings=Settings(_env_file=None, route_fast_enabled=False))
                self.assertEqual(decision.route_plan.primary_intent, expected)

    def test_weak_rule_signal_without_planner_requests_clarification(self):
        # 该原始样本没有强规则命中。保留事实：模型不可用时只能澄清，不能宣称识别为 MENTAL。
        decision = classify_route("最近压力很大，想聊聊。", routing_scorer=RuleOnlyScorer(),
            settings=Settings(_env_file=None, route_fast_enabled=False))
        self.assertIsNone(decision.route_plan)
        self.assertTrue(decision.fixed_response)
        self.assertEqual(decision.diagnostics.fallback_reason, "MODEL_UNAVAILABLE")

    def test_model_failure_keeps_minimum_risk_boundaries(self):
        service = PsychologicalAssessmentService(ExplodingAi())
        self.assertEqual(service.assess("我想结束生命。").risk, RiskLevel.HIGH)
        self.assertNotEqual(service.assess("我最近真的撑不下去了，想彻底消失。").risk, RiskLevel.LOW)
        self.assertEqual(service.assess("我在论文中分析自杀预防政策。").risk, RiskLevel.LOW)


if __name__ == "__main__":
    unittest.main()
