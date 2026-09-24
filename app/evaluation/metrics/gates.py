from __future__ import annotations

import math


def evaluate_thresholds(metrics: dict, thresholds: dict[str, tuple[str, float]], *, fail_on_nan: bool = True) -> dict:
    gates = {}
    for name, (operator, threshold) in thresholds.items():
        value = metrics.get(name)
        if not isinstance(value, (int, float)) or (fail_on_nan and not math.isfinite(float(value))):
            gates[name] = {"passed": False, "value": value, "operator": operator, "threshold": threshold}
            continue
        passed = value >= threshold if operator == ">=" else value <= threshold if operator == "<=" else value == threshold
        gates[name] = {"passed": passed, "value": value, "operator": operator, "threshold": threshold}
    return gates
