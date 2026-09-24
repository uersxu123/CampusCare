import pytest

from app.evaluation.contracts import EndToEndCase, EvaluationRuntimeOutcome, RoutingCase


def test_golden_fields_never_enter_sut_input():
    sentinel = "唯一黄金哨兵"
    case = EndToEndCase.model_validate({
        "id": "e2e-1",
        "turns": ["用户问题"],
        "expected_action": "ANSWER",
        "reference": sentinel,
        "reference_facts": [sentinel],
        "expected_document_keys": [sentinel],
        "forbidden_claims": [sentinel],
    })
    assert sentinel not in str(case.sut_input())
    assert case.sut_input() == {"turns": ["用户问题"]}


def test_routing_contract_accepts_strict_v3_expected_fields():
    case = RoutingCase.model_validate({
        "id": "routing-1",
        "messages": [{"role": "user", "content": "你好"}],
        "expected": {
            "primaryIntent": "CHAT", "intents": ["CHAT"],
            "workItemCount": 1, "workItemIntents": ["CHAT"],
            "sourceTextFragments": [["你好"]], "hardDataEdges": [], "orderOnlyEdges": [],
            "missingArgumentNamesByWorkItem": [[]], "contextRelation": "NEW_TOPIC",
            "capacityExceeded": False,
        },
        "tags": ["single-goal"],
    })
    assert case.expected.primaryIntent.value == "CHAT"


def test_routing_contract_normalizes_tags_and_rejects_blank_message():
    payload = {
        "id": "routing-1",
        "messages": [{"role": "user", "content": "你好"}],
        "expected": {
            "primaryIntent": "CHAT", "intents": ["CHAT"],
            "workItemCount": 1, "workItemIntents": ["CHAT"],
            "sourceTextFragments": [["你好"]], "hardDataEdges": [], "orderOnlyEdges": [],
            "missingArgumentNamesByWorkItem": [[]], "contextRelation": "NEW_TOPIC",
            "capacityExceeded": False,
        },
        "tags": [" single-goal "],
    }
    assert RoutingCase.model_validate(payload).tags == ["single-goal"]
    with pytest.raises(ValueError):
        RoutingCase.model_validate({**payload, "messages": [{"role": "user", "content": "   "}]})
    with pytest.raises(ValueError):
        RoutingCase.model_validate({**payload, "tags": ["single-goal", " single-goal "]})
