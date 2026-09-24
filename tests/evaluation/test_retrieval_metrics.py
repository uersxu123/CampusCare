from types import SimpleNamespace

import pytest

from app.evaluation.metrics.retrieval import distinct_document_keys, ndcg_at_k, recall_at_k, reciprocal_rank


def test_top_five_and_top_ten_are_not_truncated_during_document_deduplication():
    candidates = [SimpleNamespace(canonical_key=f"doc-{index}", source_key="") for index in range(10)]
    keys = distinct_document_keys(candidates, 10)
    assert recall_at_k(keys, {"doc-8"}, 5) == 0
    assert recall_at_k(keys, {"doc-8"}, 10) == 1


def test_duplicate_chunks_do_not_duplicate_document_relevance():
    candidates = [
        SimpleNamespace(canonical_key="a", source_key=""),
        SimpleNamespace(canonical_key="a", source_key=""),
        SimpleNamespace(canonical_key="b", source_key=""),
    ]
    assert distinct_document_keys(candidates, 10) == ["a", "b"]
    assert reciprocal_rank(["x", "b"], {"b"}) == 0.5
    assert ndcg_at_k([], {"b"}, 5) == 0
