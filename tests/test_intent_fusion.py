import pytest

from app.core.config import Settings
from app.core.enums import IntentType
from app.services.intent_fusion import IntentCandidate, IntentFusion, UnderstandingDecision


class UnavailableBackend:
    name = "disabled"
    model = ""
    def available(self): return False
    def model_digest(self): return "disabled"


def fusion():
    return IntentFusion(Settings(_env_file=None), UnavailableBackend())


def candidate(intent, confidence, source):
    return IntentCandidate(intent, confidence, source, (f"{source}_SIGNAL",))


def test_three_way_agreement_uses_fixed_weights():
    result = fusion().fuse_five_intents(
        candidate(IntentType.CAMPUS, 0.9, "LLM"),
        candidate(IntentType.CAMPUS, 0.8, "EMBEDDING"),
        candidate(IntentType.CAMPUS, 0.94, "RULE"),
    )
    assert result.intent == IntentType.CAMPUS
    assert result.confidence == pytest.approx(0.884)


def test_two_way_agreement_beats_one_conflict():
    result = fusion().fuse_five_intents(
        candidate(IntentType.MENTAL, 0.9, "LLM"),
        candidate(IntentType.MENTAL, 0.8, "EMBEDDING"),
        candidate(IntentType.CAMPUS, 0.94, "RULE"),
    )
    assert result.intent == IntentType.MENTAL


def test_rule_may_abstain_and_llm_context_still_wins():
    result = fusion().fuse_five_intents(candidate(IntentType.ACADEMIC, 0.92, "LLM"), None, None)
    assert result.intent == IntentType.ACADEMIC
    assert result.confidence == 0.92


def test_all_signals_abstain_to_low_confidence_chat():
    result = fusion().fuse_five_intents(None, None, None)
    assert result == type(result)(IntentType.CHAT, 0.0, ("AMBIGUOUS_ROUTING",))


def test_low_confidence_returns_ambiguous_chat():
    result = fusion().fuse_five_intents(
        candidate(IntentType.ACADEMIC, 0.55, "LLM"),
        candidate(IntentType.CAMPUS, 0.95, "EMBEDDING"),
        None,
    )
    assert result.intent == IntentType.CHAT
    assert result.reason_codes == ("AMBIGUOUS_ROUTING",)


def test_embedding_cannot_raise_risk_without_llm_risk():
    result = fusion().fuse_five_intents(
        candidate(IntentType.CHAT, 0.9, "LLM"),
        candidate(IntentType.RISK, 0.99, "EMBEDDING"),
        None,
    )
    assert result.intent == IntentType.CHAT


def test_candidate_source_must_match_slot():
    with pytest.raises(ValueError):
        fusion().fuse_five_intents(candidate(IntentType.CHAT, 0.9, "RULE"), None, None)


def test_understanding_v3_rejects_old_fields_and_versions():
    base = {
        "schemaVersion": 3, "routeStatus": "ROUTE", "contextRelation": "NEW_TOPIC",
        "segments": [{"sourceText": "你好", "intent": "CHAT", "confidence": 0.9, "reasonCodes": []}],
        "dependencyHints": [],
    }
    UnderstandingDecision.model_validate(base)
    with pytest.raises(Exception):
        UnderstandingDecision.model_validate({**base, "schemaVersion": 2})
    with pytest.raises(Exception):
        UnderstandingDecision.model_validate({**base, "segments": [{**base["segments"][0], "taskKind": "GENERAL_CHAT"}]})


def test_capacity_and_ambiguous_schema_are_strict():
    UnderstandingDecision.model_validate({"schemaVersion": 3, "routeStatus": "TOO_MANY_WORK_ITEMS", "contextRelation": "NEW_TOPIC", "segments": [], "dependencyHints": []})
    with pytest.raises(Exception):
        UnderstandingDecision.model_validate({"schemaVersion": 3, "routeStatus": "TOO_MANY_WORK_ITEMS", "contextRelation": "AMBIGUOUS", "segments": [], "dependencyHints": []})
    with pytest.raises(Exception):
        UnderstandingDecision.model_validate({"schemaVersion": 3, "routeStatus": "ROUTE", "contextRelation": "AMBIGUOUS", "segments": [{"sourceText": "它呢", "intent": "CAMPUS", "confidence": 0.9, "reasonCodes": []}], "dependencyHints": []})
