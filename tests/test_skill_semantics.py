import math

import pytest

from app.services.embedding import EmbeddingUnavailable
from app.services.skill_semantics import SemanticCandidate, SkillSemanticService


class FakeBackend:
    name = "fake"
    model = "fake-model"

    def __init__(self, documents=None, query=None, *, available=True, document_error=None, query_error=None):
        self.documents = documents if documents is not None else [[1.0, 0.0], [0.0, 1.0]]
        self.query = query if query is not None else [1.0, 0.0]
        self.is_available = available
        self.document_error = document_error
        self.query_error = query_error
        self.document_calls = 0
        self.query_calls = 0

    def available(self):
        return self.is_available

    def embed_documents(self, texts):
        self.document_calls += 1
        if self.document_error:
            raise self.document_error
        return self.documents

    def embed_query(self, _text):
        self.query_calls += 1
        if self.query_error:
            raise self.query_error
        return self.query

    def model_digest(self):
        return "fake"


def service(backend, **updates):
    options = dict(provider="fake", base_url="http://fake", model="fake-model", embedding_version="v1",
                   min_similarity=0.70, min_margin=0.05, budget_ms=3000, cache_max_items=32)
    options.update(updates)
    return SkillSemanticService(backend, **options)


def candidates(description="描述一"):
    return [SemanticCandidate("a", description), SemanticCandidate("b", "描述二")]


def setup_function():
    SkillSemanticService.clear_cache()


def test_semantic_selects_only_with_absolute_score_and_margin():
    backend = FakeBackend()
    result = service(backend).disambiguate("当前任务", candidates())
    assert result.selected_id == "a"
    assert result.reason == "SEMANTIC_SELECTED"
    assert result.scores == {"a": 1.0, "b": 0.0}
    assert backend.document_calls == 1
    assert backend.query_calls == 1


def test_low_absolute_score_rejects_group():
    backend = FakeBackend(documents=[[0.6, 0.8], [0.0, 1.0]])
    result = service(backend).disambiguate("当前任务", candidates())
    assert result.selected_id is None
    assert result.reason == "SEMANTIC_LOW_SCORE"


def test_small_margin_rejects_group():
    backend = FakeBackend(documents=[[1.0, 0.0], [0.999, 0.04]])
    result = service(backend).disambiguate("当前任务", candidates())
    assert result.selected_id is None
    assert result.reason == "SEMANTIC_AMBIGUOUS"


@pytest.mark.parametrize(
    ("backend", "reason"),
    [
        (FakeBackend(document_error=EmbeddingUnavailable("unavailable")), "SEMANTIC_UNAVAILABLE"),
        (FakeBackend(document_error=TimeoutError()), "SEMANTIC_TIMEOUT"),
        (FakeBackend(document_error=EmbeddingUnavailable("down")), "SEMANTIC_UNAVAILABLE"),
        (FakeBackend(documents=[[1.0, 0.0]]), "SEMANTIC_VECTOR_COUNT_MISMATCH"),
        (FakeBackend(documents=[[1.0, 0.0], [1.0, 0.0, 0.0]]), "SEMANTIC_DIMENSION_MISMATCH"),
        (FakeBackend(documents=[[0.0, 0.0], [0.0, 1.0]]), "SEMANTIC_INVALID_VECTOR"),
        (FakeBackend(documents=[[math.nan, 1.0], [0.0, 1.0]]), "SEMANTIC_INVALID_VECTOR"),
        (FakeBackend(query=[math.inf, 0.0]), "SEMANTIC_INVALID_VECTOR"),
    ],
)
def test_embedding_failure_rejects_whole_group(backend, reason):
    result = service(backend).disambiguate("当前任务", candidates())
    assert result.selected_id is None
    assert result.reason == reason


def test_candidate_cache_is_hot_but_query_is_not_persisted():
    first = FakeBackend()
    first_result = service(first).disambiguate("用户原文一", candidates())
    second = FakeBackend()
    second_result = service(second).disambiguate("用户原文二", candidates())
    assert first_result.cache_hits == 0
    assert second_result.cache_hits == 2
    assert second.document_calls == 0
    assert second.query_calls == 1
    assert all("用户原文" not in part for key in SkillSemanticService._cache for part in key)


@pytest.mark.parametrize(
    "changed",
    [
        {"model": "other-model"},
        {"base_url": "http://other"},
        {"embedding_version": "v2"},
    ],
)
def test_model_endpoint_and_version_changes_invalidate_cache(changed):
    service(FakeBackend()).disambiguate("任务", candidates())
    backend = FakeBackend()
    service(backend, **changed).disambiguate("任务", candidates())
    assert backend.document_calls == 1


def test_description_change_invalidates_only_changed_candidate():
    service(FakeBackend()).disambiguate("任务", candidates())
    backend = FakeBackend(documents=[[1.0, 0.0]])
    result = service(backend).disambiguate("任务", candidates("新描述"))
    assert backend.document_calls == 1
    assert result.cache_hits == 1


def test_zero_budget_times_out_without_embedding_call():
    backend = FakeBackend()
    result = service(backend, budget_ms=0).disambiguate("任务", candidates())
    assert result.reason == "SEMANTIC_TIMEOUT"
    assert backend.document_calls == backend.query_calls == 0
