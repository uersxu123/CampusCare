import unittest

from scripts.compare_knowledge_ingestion_v1_v2 import compare_reports


def _report(profile, recall, citation, latency, *, passed=True):
    return {
        "dataset": "fixed.json",
        "totalCases": 100,
        "ingestionBaseline": {"profile": profile},
        "modes": {
            "bm25": {
                "passed": passed,
                "metrics": {
                    "recallAt10": recall,
                    "citationPageAccuracy": citation,
                    "searchP95Seconds": latency,
                },
            }
        },
    }


class KnowledgeIngestionComparisonTests(unittest.TestCase):
    def test_candidate_must_not_regress_recall_or_citations(self):
        baseline = _report("legacy_char_v1", 0.96, 0.97, 0.5)
        candidate = _report("structure_token_v2", 0.96, 0.98, 0.59)

        report = compare_reports(baseline, candidate, latency_tolerance=1.2)

        self.assertTrue(report["passed"])
        self.assertTrue(all(report["gates"].values()))
        self.assertTrue(report["observations"]["searchP95WithinTolerance"])

    def test_quality_failure_blocks_activation_but_latency_is_informational(self):
        baseline = _report("legacy_char_v1", 0.96, 1.0, 0.5)
        candidate = _report("structure_token_v2", 0.95, 0.99, 0.7)

        report = compare_reports(baseline, candidate, latency_tolerance=1.2)

        self.assertFalse(report["passed"])
        self.assertFalse(report["gates"]["recallAt10"])
        self.assertFalse(report["gates"]["citationPageAccuracy"])
        self.assertNotIn("searchP95Seconds", report["gates"])
        self.assertFalse(report["observations"]["searchP95WithinTolerance"])

    def test_latency_regression_does_not_block_activation(self):
        baseline = _report("legacy_char_v1", 0.96, 0.97, 0.5)
        candidate = _report("structure_token_v2", 0.97, 0.98, 0.8)

        report = compare_reports(baseline, candidate, latency_tolerance=1.2)

        self.assertTrue(report["passed"])
        self.assertFalse(report["latencyGateEnabled"])
        self.assertFalse(report["observations"]["searchP95WithinTolerance"])


if __name__ == "__main__":
    unittest.main()
