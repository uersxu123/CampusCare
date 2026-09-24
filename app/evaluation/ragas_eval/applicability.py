from __future__ import annotations


ANSWER_METRICS = (
    "faithfulness",
    "answerRelevancy",
    "contextPrecision",
    "contextRecall",
    "factualCorrectness",
    "idBasedContextPrecision",
    "idBasedContextRecall",
)


def metric_applicability(
    expected_action: str,
    *,
    has_reference_context_ids: bool,
    declared: dict[str, bool] | None = None,
) -> dict[str, dict]:
    action = expected_action.upper()
    declared = declared or {}
    result = {}
    for metric in ANSWER_METRICS:
        applicable = action in {"ANSWER", "PARTIAL_ANSWER"}
        if metric == "factualCorrectness" and action == "PARTIAL_ANSWER":
            applicable = True
        if metric.startswith("idBased") and not has_reference_context_ids:
            applicable = False
            reason = "referenceContextIds 为空"
        elif not applicable:
            reason = f"expectedAction={action}"
        elif metric in declared and not declared[metric]:
            applicable = False
            reason = "数据集声明该指标不适用"
        else:
            reason = ""
        result[metric] = {"status": "pending"} if applicable else {"status": "notApplicable", "reason": reason}
    return result
