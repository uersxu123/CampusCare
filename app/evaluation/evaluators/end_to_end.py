from __future__ import annotations


FAILURE_ORDER = (
    "ROUTING_ERROR",
    "KNOWLEDGE_DECISION_ERROR",
    "RETRIEVAL_MISS",
    "EVIDENCE_GRADE_ERROR",
    "CONTEXT_TRANSFER_ERROR",
    "GENERATION_GROUNDING_ERROR",
    "ANSWER_QUALITY_ERROR",
    "SAFETY_ERROR",
    "JUDGE_ERROR",
    "INFRA_ERROR",
)


def attribute_failure(
    *,
    route_correct: bool | None,
    knowledge_decision_correct: bool | None,
    candidate_hit: bool | None,
    usable_hit: bool | None,
    context_transferred: bool | None,
    faithfulness: float | None,
    business_passed: bool | None,
    safety_passed: bool | None,
    judge_error: bool = False,
    infra_error: bool = False,
) -> dict:
    reasons = []
    if route_correct is False:
        reasons.append("ROUTING_ERROR")
    if knowledge_decision_correct is False:
        reasons.append("KNOWLEDGE_DECISION_ERROR")
    if candidate_hit is False:
        reasons.append("RETRIEVAL_MISS")
    if candidate_hit is True and usable_hit is False:
        reasons.append("EVIDENCE_GRADE_ERROR")
    if usable_hit is True and context_transferred is False:
        reasons.append("CONTEXT_TRANSFER_ERROR")
    if faithfulness is not None and faithfulness < 0.9:
        reasons.append("GENERATION_GROUNDING_ERROR")
    if business_passed is False:
        reasons.append("ANSWER_QUALITY_ERROR")
    if safety_passed is False:
        reasons.append("SAFETY_ERROR")
    if judge_error:
        reasons.append("JUDGE_ERROR")
    if infra_error:
        reasons.append("INFRA_ERROR")
    ordered = [name for name in FAILURE_ORDER if name in reasons]
    return {
        "primary": ordered[0] if ordered else None,
        "secondary": ordered[1:],
    }
