from app.core.config import Settings
from app.services.ai import _shared_ollama_client, close_shared_ollama_clients


def test_default_local_ollama_endpoints_use_ipv4_loopback():
    settings = Settings(_env_file=None)
    assert settings.ollama_base_url == "http://127.0.0.1:11434"
    assert settings.knowledge_embedding_base_url == "http://127.0.0.1:11434"


def test_ollama_ai_clients_are_reused_for_same_endpoint():
    close_shared_ollama_clients()
    first = _shared_ollama_client("http://127.0.0.1:11434")
    second = _shared_ollama_client("http://127.0.0.1:11434/")
    assert first is second
    close_shared_ollama_clients()


def test_different_ollama_endpoints_do_not_share_client():
    close_shared_ollama_clients()
    ipv4 = _shared_ollama_client("http://127.0.0.1:11434")
    other = _shared_ollama_client("http://127.0.0.1:11435")
    assert ipv4 is not other
    close_shared_ollama_clients()
