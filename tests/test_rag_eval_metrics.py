from types import SimpleNamespace

from app.rag_eval.runner import _distinct_documents, aggregate_metrics


def test_legacy_harness_keeps_distinct_top_ten():
    rows = [SimpleNamespace(canonical_key=f"doc-{index}", source_key="") for index in range(10)]
    assert len(_distinct_documents(rows, 10)) == 10
    assert len(_distinct_documents(rows, 5)) == 5


def test_fixed_golden_grader_is_not_a_formal_metric():
    metrics = aggregate_metrics([])
    assert metrics["gradeMacroF1"]["status"] == "notApplicable"
    assert metrics["facetPlanAccuracy"]["status"] == "notApplicable"
    assert "retrievedFactCoverageAt5" in metrics
    assert "retrievedFactCoverageAt10" in metrics
    assert "usableEvidenceFactCoverage" in metrics
