from app.evaluation.ragas_eval.applicability import metric_applicability


def test_non_answer_metrics_are_explicitly_not_applicable():
    report = metric_applicability("CLARIFY", has_reference_context_ids=False)
    assert all(item["status"] == "notApplicable" for item in report.values())
    assert report["faithfulness"]["reason"] == "expectedAction=CLARIFY"


def test_answer_id_metrics_require_reference_ids():
    report = metric_applicability("ANSWER", has_reference_context_ids=False)
    assert report["faithfulness"]["status"] == "pending"
    assert report["idBasedContextRecall"]["status"] == "notApplicable"
