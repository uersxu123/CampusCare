from __future__ import annotations

import asyncio
import inspect
import math
from collections import defaultdict
from typing import Awaitable, Callable

from app.evaluation.contracts import EndToEndCase, EvaluationRuntimeOutcome
from app.evaluation.ragas_eval.applicability import metric_applicability
from app.evaluation.ragas_eval.dataset_adapter import to_ragas_sample
from app.evaluation.ragas_eval.factories import create_factories
from app.evaluation.ragas_eval.metrics import create_metrics


class RagasEvaluationError(RuntimeError):
    pass


async def evaluate_ragas_cases(
    cases: list[EndToEndCase],
    outcomes: list[EvaluationRuntimeOutcome],
    settings,
    *,
    timeout_seconds: float | None = None,
) -> dict:
    by_id = {item.case_id: item for item in outcomes}
    factories = create_factories(settings)
    metrics = create_metrics(factories)
    rows = []
    errors = []
    values: dict[str, list[float]] = defaultdict(list)
    for case in cases:
        outcome = by_id.get(case.id)
        if outcome is None:
            errors.append({"caseId": case.id, "errorCode": "RUNTIME_OUTCOME_MISSING"})
            continue
        try:
            sample = to_ragas_sample(case, outcome)
        except ValueError as exc:
            errors.append({"caseId": case.id, "errorCode": "INVALID_RAGAS_INPUT", "message": str(exc)})
            continue
        applicability = metric_applicability(
            case.expected_action,
            has_reference_context_ids=bool(case.reference_context_ids),
            declared=case.metric_applicability,
        )
        case_metrics = dict(applicability)
        for name, metric in metrics.items():
            if case_metrics[name]["status"] == "notApplicable":
                continue
            try:
                score = await asyncio.wait_for(
                    _score_metric(metric, sample),
                    timeout=timeout_seconds or settings.judge_timeout_seconds,
                )
                numeric = float(score)
                if not math.isfinite(numeric):
                    raise RagasEvaluationError("指标返回 NaN 或无穷值")
                case_metrics[name] = {"status": "success", "value": numeric}
                values[name].append(numeric)
            except Exception as exc:
                case_metrics[name] = {"status": "error", "errorCode": type(exc).__name__}
                errors.append({"caseId": case.id, "metric": name, "errorCode": type(exc).__name__})
        for name, status in case_metrics.items():
            if status["status"] == "pending":
                status.update({"status": "error", "errorCode": "APPLICABLE_METRIC_MISSING"})
                errors.append({"caseId": case.id, "metric": name, "errorCode": "APPLICABLE_METRIC_MISSING"})
        rows.append({"caseId": case.id, "metrics": case_metrics})
    means = {name: sum(items) / len(items) for name, items in values.items() if items}
    critical_ids = {case.id for case in cases if case.critical}
    critical_faithfulness = [
        row["metrics"]["faithfulness"]["value"]
        for row in rows
        if row["caseId"] in critical_ids
        and row["metrics"].get("faithfulness", {}).get("status") == "success"
    ]
    gates = {
        "faithfulnessMean": means.get("faithfulness", float("nan")) >= 0.90,
        "faithfulnessCriticalMinimum": not critical_ids or (
            bool(critical_faithfulness) and min(critical_faithfulness) >= 0.85
        ),
        "answerRelevancyMean": means.get("answerRelevancy", float("nan")) >= 0.80,
        "contextPrecisionMean": means.get("contextPrecision", float("nan")) >= 0.75,
        "contextRecallMean": means.get("contextRecall", float("nan")) >= 0.85,
        "metricErrorRate": not errors,
        "nanRate": not any(
            status.get("status") == "success" and not math.isfinite(float(status["value"]))
            for row in rows for status in row["metrics"].values()
        ),
    }
    return {
        "ragasVersion": factories.version,
        "metrics": means,
        "denominators": {name: len(items) for name, items in values.items()},
        "metricErrors": errors,
        "gates": gates,
        "passed": all(gates.values()),
        "results": rows,
    }


async def _score_metric(metric: object, sample: dict) -> float:
    if getattr(metric, "_mindbridge_legacy_single_turn", False):
        from ragas.dataset_schema import SingleTurnSample

        value = metric.single_turn_ascore(SingleTurnSample(**sample))
        if inspect.isawaitable(value):
            value = await value
        return float(value)
    method = getattr(metric, "ascore", None) or getattr(metric, "score", None)
    if method is None:
        raise RagasEvaluationError("RAGAS metric 不提供可调用评分接口")
    parameters = inspect.signature(method).parameters
    kwargs = {name: value for name, value in sample.items() if name in parameters}
    value = method(**kwargs)
    if inspect.isawaitable(value):
        value = await value
    if hasattr(value, "value"):
        value = value.value
    if isinstance(value, dict):
        value = next(iter(value.values()))
    return float(value)
