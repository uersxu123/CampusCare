import pytest

from app.core.enums import IntentType
from app.services.intent_fusion import IntentCandidate, UnderstandingDecision
from app.services.route_planning import (
    SemanticDependencyGraphInvalid,
    SegmentAnchorFailed,
    SegmentCoverageFailed,
    SegmentOverlapOrDuplicate,
    build_and_validate_rule_dependency_graphs,
    build_rule_route_draft,
    reconcile_segments,
    rule_segments,
    stable_topological_order,
    validate_and_build_accepted_dependency_graphs,
)


def rule(text):
    if "复习" in text or "计划" in text: return IntentCandidate(IntentType.ACADEMIC, .92, "RULE")
    if "焦虑" in text: return IntentCandidate(IntentType.MENTAL, .94, "RULE")
    if "奖学金" in text or "调宿" in text or "补考" in text: return IntentCandidate(IntentType.CAMPUS, .94, "RULE")
    return None


def decision(texts, hints=(), relation="NEW_TOPIC"):
    return UnderstandingDecision.model_validate({
        "schemaVersion": 3, "routeStatus": "ROUTE", "contextRelation": relation,
        "segments": [{"sourceText": text, "intent": (rule(text).intent if rule(text) else IntentType.CHAT).value, "confidence": .9, "reasonCodes": []} for text in texts],
        "dependencyHints": list(hints),
    })


def test_rule_draft_detects_five_targets_but_does_not_route_itself():
    text = "查补考政策，同时核对奖学金资格，同时制定英语复习计划，同时总结执行清单，同时解释申请流程"
    draft = build_rule_route_draft(text, rule, limit=4)
    assert len(draft.action_spans) == 5
    assert draft.limit_exceeded is True


def test_reconcile_requires_exact_unique_complete_nonoverlap():
    text = "我最近很焦虑，也想制定期末复习计划"
    draft = build_rule_route_draft(text, rule, limit=4)
    result = reconcile_segments(text, draft, decision(["我最近很焦虑", "也想制定期末复习计划"]), limit=4)
    assert [item.source_text for item in result] == ["我最近很焦虑", "也想制定期末复习计划"]
    with pytest.raises(SegmentAnchorFailed): reconcile_segments(text, draft, decision(["改写目标"]), limit=4)
    with pytest.raises(SegmentOverlapOrDuplicate): reconcile_segments("查奖学金截止", build_rule_route_draft("查奖学金截止", rule, limit=4), decision(["查奖学金", "奖学金截止"]), limit=4)
    with pytest.raises(SegmentCoverageFailed): reconcile_segments(text, draft, decision(["我最近很焦虑"]), limit=4)


def test_hard_data_and_order_only_use_separate_graphs():
    text = "先查调宿流程，再给我复习建议"
    draft = build_rule_route_draft(text, rule, limit=4)
    segments = reconcile_segments(text, draft, decision(["查调宿流程", "给我复习建议"]), limit=4)
    graphs = build_and_validate_rule_dependency_graphs(segments, draft)
    assert graphs.execution.edges == ()
    assert len(graphs.presentation.edges) == 1


def test_low_confidence_hard_hint_is_abstained():
    text = "查奖学金政策，制定申请计划"
    draft = build_rule_route_draft(text, rule, limit=4)
    model = decision(["查奖学金政策", "制定申请计划"], [{"sourceIndex":0,"targetIndex":1,"relation":"HARD_DATA","confidence":.8,"reasonCode":"TARGET_USES_SOURCE_RESULT"}])
    segments = reconcile_segments(text, draft, model, limit=4)
    graphs, rejected = validate_and_build_accepted_dependency_graphs(segments, draft, model)
    assert rejected == 1
    assert graphs.execution.edges == ()


def test_llm_cycle_rejects_whole_dependency_plan():
    text = "查政策，查资格，制定计划"
    draft = build_rule_route_draft(text, rule, limit=4)
    hints = [
        {"sourceIndex":0,"targetIndex":1,"relation":"HARD_DATA","confidence":.9,"reasonCode":"TARGET_USES_SOURCE_FACT"},
        {"sourceIndex":1,"targetIndex":2,"relation":"HARD_DATA","confidence":.9,"reasonCode":"TARGET_USES_SOURCE_RESULT"},
        {"sourceIndex":2,"targetIndex":0,"relation":"HARD_DATA","confidence":.9,"reasonCode":"TARGET_USES_SOURCE_RESULT"},
    ]
    model = decision(["查政策", "查资格", "制定计划"], hints)
    segments = reconcile_segments(text, draft, model, limit=4)
    with pytest.raises(SemanticDependencyGraphInvalid): validate_and_build_accepted_dependency_graphs(segments, draft, model)


def test_topological_order_is_stable():
    text = "先查调宿流程，再给我复习建议"
    draft = build_rule_route_draft(text, rule, limit=4)
    segments = rule_segments(draft)
    graphs = build_and_validate_rule_dependency_graphs(segments, draft)
    assert [item.source_text for item in stable_topological_order(segments, graphs.presentation)] == ["查调宿流程", "给我复习建议"]
