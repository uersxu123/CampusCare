import math

from app.evaluation.metrics.gates import evaluate_thresholds


def test_nan_and_missing_metrics_fail_closed():
    gates = evaluate_thresholds(
        {"faithfulness": math.nan},
        {"faithfulness": (">=", 0.9), "contextRecall": (">=", 0.85)},
    )
    assert not gates["faithfulness"]["passed"]
    assert not gates["contextRecall"]["passed"]
