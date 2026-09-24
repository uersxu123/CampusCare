import pytest

from app.evaluation.contracts import EndToEndCase, EvaluationRuntimeOutcome
from app.evaluation.ragas_eval.dataset_adapter import to_ragas_sample


def test_ragas_input_merges_golden_only_after_runtime():
    case = EndToEndCase.model_validate({
        "id": "ragas-input", "turns": ["问题"], "expected_action": "ANSWER",
        "reference": "人工答案", "reference_contexts": ["人工上下文"],
        "reference_context_ids": ["knowledge:1"],
    })
    outcome = EvaluationRuntimeOutcome(
        case_id=case.id, turn_index=0, response="真实回答", route={}, risk_level="LOW",
        action="ANSWER", knowledge_requested=True, knowledge_used=True,
        retrieved_context_ids=["knowledge:2"], retrieved_contexts=["真实检索上下文"],
        usable_context_ids=["knowledge:2"], usable_contexts=["真实检索上下文"],
        trace_id="1", turn_metrics={},
    )
    sample = to_ragas_sample(case, outcome)
    assert sample["response"] == "真实回答"
    assert sample["retrieved_contexts"] == ["真实检索上下文"]
    assert sample["reference"] == "人工答案"


def test_claimed_knowledge_without_captured_context_fails_closed():
    case = EndToEndCase.model_validate({
        "id": "ragas-empty", "turns": ["问题"], "expected_action": "ANSWER", "reference": "答案",
    })
    outcome = EvaluationRuntimeOutcome(
        case_id=case.id, turn_index=0, response="回答", route={}, risk_level="LOW",
        action="ANSWER", knowledge_requested=True, knowledge_used=True,
        retrieved_context_ids=[], retrieved_contexts=[], usable_context_ids=[], usable_contexts=[],
        trace_id="1", turn_metrics={},
    )
    with pytest.raises(ValueError):
        to_ragas_sample(case, outcome)
