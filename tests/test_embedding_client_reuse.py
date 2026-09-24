from app.core.config import Settings
from app.services.embedding import OllamaEmbeddingBackend, close_shared_embedding_clients


def test_ollama_embedding_clients_are_reused_for_same_endpoint_and_timeout():
    close_shared_embedding_clients()
    settings = Settings(
        _env_file=None,
        knowledge_embedding_provider="ollama",
        knowledge_embedding_model="bge-m3:latest",
        knowledge_embedding_base_url="http://127.0.0.1:11434",
        embedding_timeout_seconds=3.0,
    )
    first = OllamaEmbeddingBackend(settings)
    second = OllamaEmbeddingBackend(settings)
    assert first._client is second._client
    close_shared_embedding_clients()


def test_different_timeout_budgets_do_not_share_http_client():
    close_shared_embedding_clients()
    fast = Settings(
        _env_file=None,
        knowledge_embedding_provider="ollama",
        knowledge_embedding_model="bge-m3:latest",
        knowledge_embedding_base_url="http://127.0.0.1:11434",
        embedding_timeout_seconds=3.0,
    )
    knowledge = fast.model_copy(update={"embedding_timeout_seconds": 30.0})
    fast_backend = OllamaEmbeddingBackend(fast)
    knowledge_backend = OllamaEmbeddingBackend(knowledge)
    assert fast_backend._client is not knowledge_backend._client
    close_shared_embedding_clients()
