from types import SimpleNamespace

from app.evaluation.runtime.action_resolution import (
    context_id_from_item,
    resolve_evaluation_action,
)


def _harness(*results, clarification=None):
    return SimpleNamespace(specialist_results=list(results), clarification_request=clarification)


def _result(status, reason, evidence=None, tools=None, errors=None):
    return {
        "workItemId": "wi-1",
        "intent": "CAMPUS",
        "status": status,
        "reasonCode": reason,
        "evidenceItems": evidence or [],
        "toolSummary": {
            "usedTools": tools or [],
            "errorCodes": errors or [],
        },
    }


def test_context_identity_prefers_context_id_and_migrates_evidence_id():
    assert context_id_from_item({"evidenceId": "ev_chunk_6311", "contextId": "knowledge:6311"}) == "knowledge:6311"
    assert context_id_from_item({"evidenceId": "ev_chunk_6311"}) == "knowledge:6311"
    assert context_id_from_item({"source": "looks-like-a-chunk"}) == ""


def test_action_resolution_reaches_abstain_and_partial_aggregate():
    insufficient = _result("PARTIAL", "EVIDENCE_INSUFFICIENT", tools=["mindbridge__rag_search"])
    answered = _result("COMPLETED", "EVIDENCE_COMPLETE", [{"contextId": "knowledge:6311"}])
    assert resolve_evaluation_action({}, _harness(insufficient)).action == "ABSTAIN"
    assert resolve_evaluation_action({}, _harness(answered, insufficient)).action == "PARTIAL_ANSWER"


def test_action_resolution_does_not_default_empty_specialists_to_answer():
    resolved = resolve_evaluation_action({}, _harness())
    assert resolved.reason_codes == ("ACTION_UNRESOLVED",)
    assert resolved.action != "ANSWER"
