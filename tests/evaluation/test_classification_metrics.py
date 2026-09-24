import pytest

from app.evaluation.metrics.calibration import brier_score, expected_calibration_error
from app.evaluation.metrics.classification import classification_metrics


def test_classification_reports_macro_f1_and_confusion_matrix():
    report = classification_metrics(["A", "A", "B"], ["A", "B", "B"], labels=("A", "B", "C"))
    assert report["accuracy"] == pytest.approx(2 / 3)
    assert report["perClass"]["C"]["f1"] == 0
    assert report["confusionMatrix"]["matrix"]["A"]["B"] == 1


def test_calibration_metrics_are_finite_and_exact_for_perfect_predictions():
    assert expected_calibration_error([True, False], [1.0, 0.0]) == 0
    assert brier_score([True, False], [1.0, 0.0]) == 0
