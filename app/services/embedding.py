from __future__ import annotations

import atexit
import hashlib
import threading
from dataclasses import dataclass
from functools import cached_property
from typing import Protocol

import httpx

from app.core.config import Settings


class EmbeddingUnavailable(RuntimeError):
    pass


_SHARED_CLIENTS: dict[tuple[str, str, float, bool], httpx.Client] = {}
_SHARED_CLIENTS_LOCK = threading.Lock()


def _shared_embedding_client(*, provider: str, base_url: str, timeout: float, trust_env: bool) -> httpx.Client:
    """Return one process-level client per embedding endpoint/timeout budget.

    Fast routing creates lightweight scorer/router objects frequently. Reusing the
    underlying HTTP client avoids rebuilding transports/SSL contexts on every
    request and keeps connections warm.
    """
    key = (provider, base_url.rstrip("/"), float(timeout), bool(trust_env))
    client = _SHARED_CLIENTS.get(key)
    if client is not None:
        return client
    with _SHARED_CLIENTS_LOCK:
        client = _SHARED_CLIENTS.get(key)
        if client is None:
            client = httpx.Client(timeout=timeout, trust_env=trust_env)
            _SHARED_CLIENTS[key] = client
        return client


def close_shared_embedding_clients() -> None:
    with _SHARED_CLIENTS_LOCK:
        clients = list(_SHARED_CLIENTS.values())
        _SHARED_CLIENTS.clear()
    for client in clients:
        try:
            client.close()
        except Exception:
            pass


atexit.register(close_shared_embedding_clients)


class EmbeddingBackend(Protocol):
    name: str
    model: str

    def available(self) -> bool: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...

    def model_digest(self) -> str: ...


@dataclass(frozen=True)
class EmbeddingIdentity:
    provider: str
    model: str
    digest: str
    digest_resolved: bool


class DisabledEmbeddingBackend:
    name = "disabled"
    model = ""

    def available(self) -> bool:
        return False

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingUnavailable("知识向量 embedding 已禁用")

    def embed_query(self, text: str) -> list[float]:
        raise EmbeddingUnavailable("知识向量 embedding 已禁用")

    def model_digest(self) -> str:
        return "disabled"


class OllamaEmbeddingBackend:
    name = "ollama"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.model = settings.knowledge_embedding_model or settings.ollama_embedding_model
        self.base_url = (settings.knowledge_embedding_base_url or settings.ollama_base_url).rstrip("/")
        self.timeout = settings.embedding_timeout_seconds
        self._client = client or _shared_embedding_client(
            provider=self.name, base_url=self.base_url, timeout=self.timeout, trust_env=False
        )

    def available(self) -> bool:
        try:
            response = self._client.get(
                f"{self.base_url}/api/tags",
                timeout=min(self.timeout, 5.0),
            )
            response.raise_for_status()
            return True
        except (httpx.HTTPError, ValueError):
            return False

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self._client.post(
            f"{self.base_url}/api/embed",
            json={"model": self.model, "input": [text if text.strip() else " " for text in texts]},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return _validate_embeddings(response.json().get("embeddings"), len(texts), "Ollama")

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    @cached_property
    def _identity(self) -> EmbeddingIdentity:
        try:
            response = self._client.get(
                f"{self.base_url}/api/tags",
                timeout=min(self.timeout, 10.0),
            )
            response.raise_for_status()
            rows = response.json().get("models") or []
            requested = _normalized_model_names(self.model)
            for row in rows:
                if requested.intersection(_normalized_model_names(str(row.get("name") or row.get("model") or ""))):
                    digest = str(row.get("digest") or "").strip()
                    if digest:
                        return EmbeddingIdentity(self.name, self.model, digest, True)
        except (httpx.HTTPError, ValueError, TypeError):
            pass
        fingerprint = hashlib.sha256(f"{self.name}:{self.model}:digest-unresolved".encode("utf-8")).hexdigest()
        return EmbeddingIdentity(self.name, self.model, f"unresolved:{fingerprint}", False)

    def model_digest(self) -> str:
        return self._identity.digest

    @property
    def digest_resolved(self) -> bool:
        return self._identity.digest_resolved


class OpenAIEmbeddingBackend:
    name = "openai"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.model = settings.knowledge_embedding_model or settings.openai_embedding_model
        self.base_url = (settings.knowledge_embedding_base_url or settings.openai_base_url).rstrip("/")
        self.api_key = settings.openai_api_key
        self.timeout = settings.embedding_timeout_seconds
        self._client = client or _shared_embedding_client(
            provider=self.name, base_url=self.base_url, timeout=self.timeout, trust_env=True
        )

    def available(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.api_key:
            raise EmbeddingUnavailable("缺少 OPENAI_API_KEY，OpenAI embedding 不可用")
        response = self._client.post(
            f"{self.base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": [text if text.strip() else " " for text in texts]},
            timeout=self.timeout,
        )
        response.raise_for_status()
        rows = sorted(response.json().get("data") or [], key=lambda item: int(item.get("index", 0)))
        return _validate_embeddings([row.get("embedding") for row in rows], len(texts), "OpenAI")

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    def model_digest(self) -> str:
        return hashlib.sha256(f"openai:{self.model}".encode("utf-8")).hexdigest()

    @property
    def digest_resolved(self) -> bool:
        return True


def create_embedding_backend(settings: Settings) -> EmbeddingBackend:
    if not settings.knowledge_vector_enabled:
        return DisabledEmbeddingBackend()
    provider = (settings.knowledge_embedding_provider or "disabled").strip().lower()
    if provider == "ollama":
        return OllamaEmbeddingBackend(settings)
    if provider == "openai":
        return OpenAIEmbeddingBackend(settings)
    if provider in {"", "disabled", "none"}:
        return DisabledEmbeddingBackend()
    raise ValueError(f"不支持的 KNOWLEDGE_EMBEDDING_PROVIDER: {provider}")


def create_memory_embedding_backend(settings: Settings) -> EmbeddingBackend:
    """Create the memory embedding backend without coupling it to knowledge flags.

    The concrete backend classes still share connection pools and validation, but
    memory has its own provider/model/version switches and can be disabled or
    migrated independently from the knowledge collection.
    """
    if not settings.memory_v3_enabled or not settings.memory_episodic_enabled:
        return DisabledEmbeddingBackend()
    provider = (settings.memory_embedding_provider or "disabled").strip().lower()
    if provider in {"", "disabled", "none"}:
        return DisabledEmbeddingBackend()
    adapted = settings.model_copy(
        update={
            "knowledge_vector_enabled": True,
            "knowledge_embedding_provider": provider,
            "knowledge_embedding_model": settings.memory_embedding_model,
            "knowledge_embedding_base_url": settings.memory_embedding_base_url,
            "embedding_timeout_seconds": min(
                float(settings.embedding_timeout_seconds),
                max(0.05, float(settings.memory_read_deadline_ms) / 1000.0),
            ),
        }
    )
    return create_embedding_backend(adapted)


def create_skill_embedding_backend(settings: Settings) -> EmbeddingBackend:
    """Create the Skill selector backend independently of knowledge vectors."""
    if not settings.skill_semantic_enabled:
        return DisabledEmbeddingBackend()
    provider = (settings.skill_embedding_provider or settings.knowledge_embedding_provider or "disabled").strip().lower()
    if provider in {"", "disabled", "none"}:
        return DisabledEmbeddingBackend()
    adapted = settings.model_copy(
        update={
            "knowledge_vector_enabled": True,
            "knowledge_embedding_provider": provider,
            "knowledge_embedding_model": settings.skill_embedding_model or settings.knowledge_embedding_model,
            "knowledge_embedding_base_url": settings.skill_embedding_base_url or settings.knowledge_embedding_base_url,
            "embedding_timeout_seconds": min(
                float(settings.embedding_timeout_seconds),
                max(0.05, float(settings.skill_semantic_budget_ms) / 1000.0),
            ),
        }
    )
    return create_embedding_backend(adapted)


def embedding_identity(backend: EmbeddingBackend) -> EmbeddingIdentity:
    digest = backend.model_digest()
    return EmbeddingIdentity(
        provider=backend.name,
        model=backend.model,
        digest=digest,
        digest_resolved=bool(getattr(backend, "digest_resolved", not digest.startswith("unresolved:"))),
    )


def _validate_embeddings(raw, expected: int, provider: str) -> list[list[float]]:
    if not isinstance(raw, list) or len(raw) != expected:
        raise EmbeddingUnavailable(f"{provider} embeddings 接口返回向量数量不匹配")
    vectors: list[list[float]] = []
    dimension = 0
    for row in raw:
        if not isinstance(row, list) or not row:
            raise EmbeddingUnavailable(f"{provider} embeddings 接口返回空向量")
        try:
            vector = [float(value) for value in row]
        except (TypeError, ValueError) as exc:
            raise EmbeddingUnavailable(f"{provider} embeddings 接口返回非法向量") from exc
        dimension = dimension or len(vector)
        if len(vector) != dimension:
            raise EmbeddingUnavailable(f"{provider} embeddings 接口返回的向量维度不一致")
        vectors.append(vector)
    return vectors


def _normalized_model_names(value: str) -> set[str]:
    normalized = value.strip().lower()
    if not normalized:
        return set()
    return {normalized, normalized.removesuffix(":latest")}
