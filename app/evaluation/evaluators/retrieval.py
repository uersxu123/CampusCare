from __future__ import annotations

from app.evaluation.metrics.retrieval import ndcg_at_k, recall_at_k, reciprocal_rank


def evaluate_retrieval_keys(actual_keys: list[str], expected_keys: list[str]) -> dict:
    return {
        "recallAt5": recall_at_k(actual_keys, expected_keys, 5),
        "recallAt10": recall_at_k(actual_keys, expected_keys, 10),
        "mrr": reciprocal_rank(actual_keys, expected_keys, 10),
        "ndcgAt5": ndcg_at_k(actual_keys, expected_keys, 5),
    }
