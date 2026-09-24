from __future__ import annotations

import hashlib
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

import httpx

from app.services.embedding import EmbeddingBackend, EmbeddingUnavailable


SEMANTIC_TEMPLATE_VERSION = "skill-semantic-v1"


@dataclass(frozen=True)
class SemanticCandidate:
    skill_id: str
    document: str


@dataclass(frozen=True)
class SemanticDecision:
    selected_id: str | None
    scores: dict[str, float]
    reason: str
    margin: float | None
    embedding_called: bool
    embedding_call_count: int
    cache_hits: int
    elapsed_ms: int


class SkillSemanticService:
    """Embed only a rule-qualified competing group.

    Candidate vectors are cached without user content. Query vectors live only in
    one selection call and may be reused across groups through ``query_cache``.
    """

    _cache: OrderedDict[tuple[str, ...], tuple[float, ...]] = OrderedDict()
    _cache_lock = threading.RLock()
    _fill_lock = threading.Lock()

    def __init__(
        self,
        backend: EmbeddingBackend,
        *,
        provider: str,
        base_url: str,
        model: str,
        embedding_version: str,
        min_similarity: float,
        min_margin: float,
        budget_ms: int,
        cache_max_items: int = 256,
        clock: Callable[[], float] = time.perf_counter,
    ):
        self.backend = backend
        self.provider = provider.strip().lower()
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.embedding_version = embedding_version
        self.min_similarity = min_similarity
        self.min_margin = min_margin
        self.budget_ms = budget_ms
        self.cache_max_items = cache_max_items
        self.clock = clock

    def disambiguate(
        self,
        query: str,
        candidates: list[SemanticCandidate],
        *,
        query_cache: dict[str, tuple[float, ...]] | None = None,
        started_at: float | None = None,
    ) -> SemanticDecision:
        started = self.clock() if started_at is None else started_at
        calls = 0
        hits = 0

        def finish(reason: str, *, selected_id: str | None = None, scores=None, margin=None):
            return SemanticDecision(
                selected_id=selected_id,
                scores=scores or {},
                reason=reason,
                margin=margin,
                embedding_called=calls > 0,
                embedding_call_count=calls,
                cache_hits=hits,
                elapsed_ms=max(0, int((self.clock() - started) * 1000)),
            )

        if self.budget_ms <= 0 or self._expired(started):
            return finish("SEMANTIC_TIMEOUT")
        if len(candidates) < 2 or not query.strip():
            return finish("SEMANTIC_INVALID_INPUT")
        try:
            if getattr(self.backend, "name", "") == "disabled":
                return finish("SEMANTIC_UNAVAILABLE")
            vectors: dict[str, tuple[float, ...]] = {}
            misses: list[SemanticCandidate] = []
            for candidate in candidates:
                key = self._cache_key(candidate.document)
                cached = self._cache_get(key)
                if cached is None:
                    misses.append(candidate)
                else:
                    hits += 1
                    vectors[candidate.skill_id] = cached
            if misses:
                with self._fill_lock:
                    still_missing = []
                    for candidate in misses:
                        cached = self._cache_get(self._cache_key(candidate.document))
                        if cached is None:
                            still_missing.append(candidate)
                        else:
                            hits += 1
                            vectors[candidate.skill_id] = cached
                    if still_missing:
                        if self._expired(started):
                            return finish("SEMANTIC_TIMEOUT")
                        encoded = self.backend.embed_documents([item.document for item in still_missing])
                        calls += 1
                        if self._expired(started):
                            return finish("SEMANTIC_TIMEOUT")
                        if len(encoded) != len(still_missing):
                            return finish("SEMANTIC_VECTOR_COUNT_MISMATCH")
                        validated = [_validated_vector(item) for item in encoded]
                        if any(item is None for item in validated):
                            return finish("SEMANTIC_INVALID_VECTOR")
                        for candidate, vector in zip(still_missing, validated, strict=True):
                            assert vector is not None
                            vectors[candidate.skill_id] = vector
                            self._cache_put(self._cache_key(candidate.document), vector)
            cache = query_cache if query_cache is not None else {}
            query_key = hashlib.sha256(query.encode("utf-8")).hexdigest()
            query_vector = cache.get(query_key)
            if query_vector is None:
                if self._expired(started):
                    return finish("SEMANTIC_TIMEOUT")
                query_vector = _validated_vector(self.backend.embed_query(query))
                calls += 1
                if self._expired(started):
                    return finish("SEMANTIC_TIMEOUT")
                if query_vector is None:
                    return finish("SEMANTIC_INVALID_VECTOR")
                cache[query_key] = query_vector
            if len(vectors) != len(candidates):
                return finish("SEMANTIC_VECTOR_COUNT_MISMATCH")
            dimensions = {len(query_vector), *(len(item) for item in vectors.values())}
            if len(dimensions) != 1:
                return finish("SEMANTIC_DIMENSION_MISMATCH")
            scores = {item.skill_id: _cosine(query_vector, vectors[item.skill_id]) for item in candidates}
            if any(not math.isfinite(value) for value in scores.values()):
                return finish("SEMANTIC_INVALID_VECTOR")
            ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
            margin = ranked[0][1] - ranked[1][1]
            if ranked[0][1] < self.min_similarity:
                return finish("SEMANTIC_LOW_SCORE", scores=scores, margin=margin)
            if margin < self.min_margin:
                return finish("SEMANTIC_AMBIGUOUS", scores=scores, margin=margin)
            return finish("SEMANTIC_SELECTED", selected_id=ranked[0][0], scores=scores, margin=margin)
        except TimeoutError:
            return finish("SEMANTIC_TIMEOUT")
        except (EmbeddingUnavailable, httpx.TimeoutException):
            return finish("SEMANTIC_UNAVAILABLE")
        except (httpx.HTTPError, ValueError, TypeError, ArithmeticError):
            return finish("SEMANTIC_UNAVAILABLE")
        except Exception:
            # Backends are pluggable. A provider-specific failure must reject
            # the whole group without exposing request content or exception text.
            return finish("SEMANTIC_UNAVAILABLE")

    def _expired(self, started: float) -> bool:
        return (self.clock() - started) * 1000 >= self.budget_ms

    def _cache_key(self, document: str) -> tuple[str, ...]:
        return (
            self.provider,
            self.base_url,
            self.model,
            self.embedding_version,
            SEMANTIC_TEMPLATE_VERSION,
            hashlib.sha256(document.encode("utf-8")).hexdigest(),
        )

    @classmethod
    def clear_cache(cls) -> None:
        with cls._cache_lock:
            cls._cache.clear()

    @classmethod
    def _cache_get(cls, key: tuple[str, ...]) -> tuple[float, ...] | None:
        with cls._cache_lock:
            value = cls._cache.get(key)
            if value is not None:
                cls._cache.move_to_end(key)
            return value

    def _cache_put(self, key: tuple[str, ...], value: tuple[float, ...]) -> None:
        with self._cache_lock:
            self._cache[key] = value
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_max_items:
                self._cache.popitem(last=False)


def build_semantic_document(description: str, examples: tuple[str, ...]) -> str:
    rows = [f"适用能力：{description.strip()}"]
    rows.extend(f"代表任务：{item.strip()}" for item in examples if item.strip())
    return "\n".join(rows)


def _validated_vector(value) -> tuple[float, ...] | None:
    if not isinstance(value, (list, tuple)) or not value:
        return None
    try:
        vector = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if any(not math.isfinite(item) for item in vector):
        return None
    if math.sqrt(sum(item * item for item in vector)) == 0.0:
        return None
    return vector


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    denominator = math.sqrt(sum(item * item for item in left)) * math.sqrt(sum(item * item for item in right))
    if denominator == 0.0:
        return math.nan
    return sum(a * b for a, b in zip(left, right, strict=True)) / denominator
