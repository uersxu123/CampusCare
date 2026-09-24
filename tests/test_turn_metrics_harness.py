import json
import unittest

from app.harness.runner import _metrics_case, resolve_suites


class TurnMetricsHarnessRegistrationTests(unittest.TestCase):
    def test_metrics_alias_selects_only_turn_metrics_harness(self):
        suites = resolve_suites(["metrics"])
        self.assertEqual([name for name, _fn in suites], ["Turn Metrics Harness"])

    def test_all_suites_include_turn_metrics_before_api(self):
        names = [name for name, _fn in resolve_suites(["all"])]
        self.assertIn("Turn Metrics Harness", names)
        self.assertLess(names.index("RAG Harness"), names.index("Turn Metrics Harness"))
        self.assertLess(names.index("Turn Metrics Harness"), names.index("API Harness"))

    def test_report_case_is_json_serializable_and_contains_no_calls(self):
        details = _metrics_case(
            "normal_generation",
            {
                "status": "COMPLETED",
                "finalModelTtftMs": 1,
                "serverE2eTtftMs": 2,
                "firstContentReadyMs": 3,
                "serverTurnDurationMs": 4,
                "tokenUsage": {
                    "providerCallCount": 1,
                    "accuracy": "ESTIMATED",
                    "promptTokens": 10,
                    "outputTokens": 5,
                    "totalTokens": 15,
                },
                "calls": [{"purpose": "response.generate"}],
            },
        )
        serialized = json.dumps(details)
        self.assertNotIn("calls", details)
        self.assertNotIn("response.generate", serialized)


if __name__ == "__main__":
    unittest.main()
