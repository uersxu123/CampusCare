from __future__ import annotations

import math
from collections.abc import Iterable


def distinct_document_keys(candidates: Iterable, limit: int) -> list[str]:
    if limit < 0:
        raise ValueError("limit 不能为负数")
    seen: set[str] = set()
    rows: list[str] = []
    for item in candidates:
        if isinstance(item, str):
            key = item
        elif isinstance(item, dict):
            key = str(item.get("canonical_key") or item.get("source_key") or "")
        else:
            key = str(getattr(item, "canonical_key", None) or getattr(item, "source_key", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append(key)
        if len(rows) == limit:
            break
    return rows


def recall_at_k(keys: Iterable[str], expected: Iterable[str], k: int) -> float:
    relevant = set(expected)
    return len(set(list(keys)[:k]) & relevant) / len(relevant) if relevant else 1.0


def reciprocal_rank(keys: Iterable[str], expected: Iterable[str], k: int | None = None) -> float:
    relevant = set(expected)
    if not relevant:
        return 1.0
    rows = list(keys)
    for index, key in enumerate(rows[:k] if k is not None else rows, start=1):
        if key in relevant:
            return 1.0 / index
    return 0.0


def ndcg_at_k(keys: Iterable[str], expected: Iterable[str], k: int) -> float:
    relevant = set(expected)
    if not relevant:
        return 1.0
    gains = [1.0 if key in relevant else 0.0 for key in list(keys)[:k]]
    dcg = sum(gain / math.log2(index + 2.0) for index, gain in enumerate(gains))
    ideal = sum(1.0 / math.log2(index + 2.0) for index in range(min(len(relevant), k)))
    return dcg / ideal if ideal else 0.0


def fact_coverage(required: Iterable[str], texts: Iterable[str]) -> float:
    facts = [item for item in required if item]
    corpus = "\n".join(texts)
    return sum(item in corpus for item in facts) / len(facts) if facts else 1.0
