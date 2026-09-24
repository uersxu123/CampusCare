from __future__ import annotations

from collections import Counter
from typing import Iterable


def confusion_matrix(expected: Iterable[str], actual: Iterable[str], labels: Iterable[str] | None = None) -> dict:
    pairs = list(zip(expected, actual, strict=True))
    ordered = list(labels or sorted({value for pair in pairs for value in pair}))
    matrix = {label: {predicted: 0 for predicted in ordered} for label in ordered}
    for truth, prediction in pairs:
        matrix.setdefault(truth, {}).setdefault(prediction, 0)
        matrix[truth][prediction] += 1
    return {"labels": ordered, "matrix": matrix}


def classification_metrics(
    expected: Iterable[str], actual: Iterable[str], labels: Iterable[str] | None = None
) -> dict:
    truth = list(expected)
    predicted = list(actual)
    if len(truth) != len(predicted):
        raise ValueError("expected 与 actual 长度必须一致")
    ordered = list(labels or sorted(set(truth) | set(predicted)))
    per_class = {}
    f1_values = []
    for label in ordered:
        tp = sum(a == label and b == label for a, b in zip(truth, predicted))
        fp = sum(a != label and b == label for a, b in zip(truth, predicted))
        fn = sum(a == label and b != label for a, b in zip(truth, predicted))
        support = sum(a == label for a in truth)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_class[label] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": support,
        }
        f1_values.append(f1)
    return {
        "accuracy": sum(a == b for a, b in zip(truth, predicted)) / len(truth) if truth else 0.0,
        "macroF1": sum(f1_values) / len(f1_values) if f1_values else 0.0,
        "perClass": per_class,
        "confusionMatrix": confusion_matrix(truth, predicted, ordered),
        "count": len(truth),
    }
