from __future__ import annotations

from math import isfinite


def expected_calibration_error(correct: list[bool], confidences: list[float], bins: int = 10) -> float:
    _validate(correct, confidences, bins)
    total = len(correct)
    error = 0.0
    for index in range(bins):
        lower = index / bins
        upper = (index + 1) / bins
        members = [
            pos for pos, confidence in enumerate(confidences)
            if lower <= confidence < upper or (index == bins - 1 and confidence == 1.0)
        ]
        if members:
            accuracy = sum(correct[pos] for pos in members) / len(members)
            confidence = sum(confidences[pos] for pos in members) / len(members)
            error += len(members) / total * abs(accuracy - confidence)
    return error


def brier_score(correct: list[bool], confidences: list[float]) -> float:
    _validate(correct, confidences, 1)
    return sum((confidence - float(label)) ** 2 for label, confidence in zip(correct, confidences)) / len(correct)


def _validate(correct: list[bool], confidences: list[float], bins: int) -> None:
    if not correct or len(correct) != len(confidences):
        raise ValueError("校准指标需要长度一致的非空输入")
    if bins < 1 or any(not isfinite(value) or not 0 <= value <= 1 for value in confidences):
        raise ValueError("confidence 必须是 0 到 1 的有限数值")
