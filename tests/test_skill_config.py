import math

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services.embedding import DisabledEmbeddingBackend, OllamaEmbeddingBackend, create_skill_embedding_backend


@pytest.mark.parametrize(
    "updates",
    [
        {"skill_rule_min_points": -1},
        {"skill_rule_min_points": 101},
        {"skill_rule_min_margin_points": 0},
        {"skill_semantic_min_similarity": math.nan},
        {"skill_semantic_min_similarity": 1.1},
        {"skill_semantic_min_margin": 0.0},
        {"skill_semantic_budget_ms": -1},
        {"skill_total_chars": -1},
        {"skill_max_matches": -1},
        {"skill_embedding_version": ""},
        {"skill_embedding_provider": "mystery"},
    ],
)
def test_invalid_skill_configuration_fails_at_load(updates):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **updates)


def test_skill_embedding_does_not_depend_on_knowledge_vector_switch():
    settings = Settings(
        _env_file=None,
        knowledge_vector_enabled=False,
        skill_semantic_enabled=True,
        skill_embedding_provider="ollama",
        skill_embedding_model="skill-model",
        skill_embedding_base_url="http://skill-endpoint",
    )
    backend = create_skill_embedding_backend(settings)
    assert isinstance(backend, OllamaEmbeddingBackend)
    assert backend.model == "skill-model"
    assert backend.base_url == "http://skill-endpoint"


def test_explicit_disabled_skill_provider_does_not_fallback():
    settings = Settings(_env_file=None, skill_embedding_provider="disabled")
    assert isinstance(create_skill_embedding_backend(settings), DisabledEmbeddingBackend)
