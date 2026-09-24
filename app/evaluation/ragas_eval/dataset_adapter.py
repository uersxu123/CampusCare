from __future__ import annotations

from app.evaluation.contracts import EndToEndCase, EvaluationRuntimeOutcome


def to_ragas_sample(case: EndToEndCase, outcome: EvaluationRuntimeOutcome) -> dict:
    if outcome.case_id != case.id:
        raise ValueError("Runtime outcome 与 golden case 不匹配")
    if outcome.knowledge_used and not outcome.usable_contexts:
        raise ValueError("运行结果声称使用知识但没有捕获 usable contexts")
    return {
        "user_input": case.turns[-1],
        "response": outcome.response,
        "retrieved_contexts": list(outcome.usable_contexts),
        "reference": case.reference,
        "reference_contexts": list(case.reference_contexts),
        "retrieved_context_ids": list(outcome.usable_context_ids),
        "reference_context_ids": list(case.reference_context_ids),
    }
