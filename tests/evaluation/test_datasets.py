from collections import Counter
from pathlib import Path

from app.evaluation.dataset import load_e2e_cases, load_routing_cases


ROOT = Path(__file__).resolve().parents[2]


def test_routing_dataset_has_required_scale_and_distribution():
    cases = load_routing_cases(ROOT / "app/evaluation/datasets/routing-v3.jsonl")
    counts = Counter(item.expected.primaryIntent.value for item in cases)
    assert len(cases) == 411
    assert set(counts) == {"CHAT", "ACADEMIC", "CAMPUS", "MENTAL", "RISK"}
    assert all(item.expected.workItemCount <= 4 for item in cases)


def test_routing_v3_separates_hard_data_and_order_only():
    cases = load_routing_cases(ROOT / "app/evaluation/datasets/routing-v3.jsonl")
    hard = [item for item in cases if "hard-data" in item.tags]
    ordered = [item for item in cases if "order-only" in item.tags]
    assert len(hard) >= 20
    assert len(ordered) >= 15
    assert all(item.expected.hardDataEdges for item in hard)
    assert all(not item.expected.hardDataEdges and item.expected.orderOnlyEdges for item in ordered)


def test_full_ragas_dataset_contract_and_action_distribution():
    cases = load_e2e_cases(ROOT / "app/evaluation/datasets/mindbridge-e2e-ragas-v1.jsonl")
    counts = Counter(item.expected_action for item in cases)
    assert len(cases) == 181
    assert counts == {"ANSWER": 142, "PARTIAL_ANSWER": 9, "CLARIFY": 7, "ABSTAIN": 23}
