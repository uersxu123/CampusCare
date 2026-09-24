from app.evaluation.evaluators.end_to_end import attribute_failure


def test_cross_layer_attribution_preserves_primary_and_secondary_order():
    result = attribute_failure(
        route_correct=False,
        knowledge_decision_correct=False,
        candidate_hit=False,
        usable_hit=False,
        context_transferred=False,
        faithfulness=0.5,
        business_passed=False,
        safety_passed=False,
        judge_error=True,
        infra_error=True,
    )
    assert result["primary"] == "ROUTING_ERROR"
    assert result["secondary"] == [
        "KNOWLEDGE_DECISION_ERROR",
        "RETRIEVAL_MISS",
        "GENERATION_GROUNDING_ERROR",
        "ANSWER_QUALITY_ERROR",
        "SAFETY_ERROR",
        "JUDGE_ERROR",
        "INFRA_ERROR",
    ]


def test_candidate_without_usable_evidence_is_grader_error():
    result = attribute_failure(
        route_correct=True,
        knowledge_decision_correct=True,
        candidate_hit=True,
        usable_hit=False,
        context_transferred=False,
        faithfulness=None,
        business_passed=None,
        safety_passed=None,
    )
    assert result == {"primary": "EVIDENCE_GRADE_ERROR", "secondary": []}
