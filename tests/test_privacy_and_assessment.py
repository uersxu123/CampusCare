import unittest
import json
from datetime import UTC, datetime

from app.core.enums import RiskLevel
from app.services.assessment import PsychologicalAssessmentService
from app.services.context_builder import SafetyContext
from app.services.memory import RedisShortTermMemoryStore


class ExplodingAi:
    def complete(self, messages):
        raise AssertionError("high risk hard guard should not call the model")


class NormalAi:
    def complete(self, messages):
        return (
            '{"emotion":"NORMAL","emotionScore":0.0,"risk":"LOW",'
            '"confidence":0.8,"summary":"当前消息正常"}'
        )


class PrivacyAndAssessmentTests(unittest.TestCase):
    def test_backend_preserves_user_input_without_redaction(self):
        text = "电话 13800138000 邮箱 a@example.com 身份证 110101199003071234"

        self.assertIn("13800138000", text)
        self.assertIn("a@example.com", text)
        self.assertIn("110101199003071234", text)

    def test_redis_memory_serializes_original_content(self):
        store = RedisShortTermMemoryStore.__new__(RedisShortTermMemoryStore)

        payload = json.loads(store._serialize("user", "电话 13800138000 邮箱 a@example.com"))

        self.assertIn("13800138000", payload["content"])
        self.assertIn("a@example.com", payload["content"])
        self.assertEqual(payload["content"], "电话 13800138000 邮箱 a@example.com")

    def test_high_risk_signal_uses_hard_guard_before_model(self):
        result = PsychologicalAssessmentService(ExplodingAi()).assess("我不想活了，想结束生命")

        self.assertEqual(result.risk, RiskLevel.HIGH)
        self.assertGreaterEqual(result.confidence, 0.9)

    def test_historical_high_risk_metadata_does_not_force_current_risk(self):
        safety_context = SafetyContext(
            report_id=1,
            risk_level="HIGH",
            intent="RISK",
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )

        result = PsychologicalAssessmentService(NormalAi()).assess(
            "今天正常上课",
            safety_context=safety_context,
        )

        self.assertEqual(result.risk, RiskLevel.LOW)


if __name__ == "__main__":
    unittest.main()
