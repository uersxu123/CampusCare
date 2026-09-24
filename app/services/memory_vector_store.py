from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.models.entities import ConversationEpisode, UserMemory
from app.services.embedding import EmbeddingBackend, create_memory_embedding_backend


class MemoryVectorStoreUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class MemoryVectorHit:
    document_id: str
    content: str
    metadata: dict[str, Any]
    score: float


def build_memory_where(*clauses: dict[str, Any]) -> dict[str, Any]:
    values = [clause for clause in clauses if clause]
    if not values:
        return {}
    return values[0] if len(values) == 1 else {"$and": values}


class MemoryVectorStore:
    """Dedicated Chroma collections with caller-owned, explicit embeddings."""

    def __init__(
        self,
        settings: Settings,
        *,
        backend: EmbeddingBackend | None = None,
        client=None,
    ):
        self.settings = settings
        self.backend = backend or create_memory_embedding_backend(settings)
        self.embedding_version = _safe_version(settings.memory_embedding_version)
        self._client = client
        self._episode_collection = None
        self._profile_collection = None

    @property
    def episode_collection_name(self) -> str:
        return f"campuscare_episodic_v1_{self.embedding_version}"[:63]

    @property
    def profile_collection_name(self) -> str:
        return f"campuscare_profile_v1_{self.embedding_version}"[:63]

    def available(self) -> bool:
        if self.settings.memory_chroma_mode == "disabled":
            return False
        try:
            return bool(self.backend.available())
        except Exception:
            return False

    def upsert_episode(self, episode: ConversationEpisode) -> None:
        if episode.status != "ACTIVE":
            raise ValueError("只能索引 ACTIVE 情景记忆")
        vector = self.backend.embed_documents([episode.summary_text])[0]
        self._episodes().upsert(
            ids=[self.episode_document_id(episode.public_id)],
            documents=[episode.summary_text],
            embeddings=[vector],
            metadatas=[{
                "user_id": int(episode.user_id),
                "session_id": int(episode.session_id),
                "source_start": int(episode.source_start_message_id),
                "source_end": int(episode.source_end_message_id),
                "status": episode.status,
                "version": int(episode.version),
                "content_hash": episode.content_hash,
                "embedding_version": self.embedding_version,
                "is_tail": bool(episode.is_tail),
            }],
        )

    def upsert_profile(self, memory: UserMemory) -> None:
        if memory.status != "ACTIVE":
            raise ValueError("只能索引 ACTIVE 用户画像")
        vector = self.backend.embed_documents([memory.content])[0]
        self._profiles().upsert(
            ids=[self.profile_document_id(memory.user_id, memory.public_id)],
            documents=[memory.content],
            embeddings=[vector],
            metadatas=[{
                "user_id": int(memory.user_id),
                "memory_public_id": memory.public_id,
                "memory_key": memory.memory_key or "",
                "origin": memory.origin,
                "status": memory.status,
                "version": int(memory.version),
                "memory_epoch": int(memory.memory_epoch),
                "sensitivity": memory.sensitivity,
                "visibility_scope": memory.visibility_scope,
                "embedding_version": self.embedding_version,
            }],
        )

    def query_episodes(
        self,
        *,
        user_id: int,
        query_text: str,
        top_k: int,
        exclude_session_id: int | None = None,
    ) -> list[MemoryVectorHit]:
        if top_k <= 0 or self._episodes().count() <= 0:
            return []
        where = build_memory_where(
            {"user_id": int(user_id)},
            {"status": "ACTIVE"},
            {"embedding_version": self.embedding_version},
            {"session_id": {"$ne": int(exclude_session_id)}} if exclude_session_id is not None else {},
        )
        return self._query(self._episodes(), query_text, top_k, where)

    def query_profiles(self, *, user_id: int, query_text: str, top_k: int) -> list[MemoryVectorHit]:
        if top_k <= 0 or self._profiles().count() <= 0:
            return []
        where = build_memory_where(
            {"user_id": int(user_id)},
            {"status": "ACTIVE"},
            {"embedding_version": self.embedding_version},
        )
        return self._query(self._profiles(), query_text, top_k, where)

    def delete_episode(self, public_id: str) -> None:
        self._episodes().delete(ids=[self.episode_document_id(public_id)])

    def delete_profile(self, user_id: int, public_id: str) -> None:
        self._profiles().delete(ids=[self.profile_document_id(user_id, public_id)])

    def close(self) -> None:
        """Release local Chroma file handles when the owning worker stops."""
        client = self._client
        self._episode_collection = None
        self._profile_collection = None
        self._client = None
        system = getattr(client, "_system", None)
        stop = getattr(system, "stop", None)
        if callable(stop):
            stop()

    def _query(self, collection, query_text: str, top_k: int, where: dict) -> list[MemoryVectorHit]:
        query_vector = self.backend.embed_query(query_text)
        count = int(collection.count())
        if count <= 0:
            return []
        result = collection.query(
            query_embeddings=[query_vector],
            n_results=min(max(1, int(top_k)), count),
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        rows = []
        for index, document_id in enumerate(ids):
            distance = float(distances[index]) if index < len(distances) else 1.0
            rows.append(MemoryVectorHit(
                document_id=str(document_id),
                content=str(documents[index] or "") if index < len(documents) else "",
                metadata=dict(metadatas[index] or {}) if index < len(metadatas) else {},
                score=1.0 / (1.0 + max(0.0, distance)),
            ))
        return rows

    def _episodes(self):
        if self._episode_collection is None:
            self._episode_collection = self._collection(self.episode_collection_name, "episodic")
        return self._episode_collection

    def _profiles(self):
        if self._profile_collection is None:
            self._profile_collection = self._collection(self.profile_collection_name, "profile")
        return self._profile_collection

    def _collection(self, name: str, memory_kind: str):
        client = self._client or self._create_client()
        self._client = client
        try:
            collection = client.get_or_create_collection(
                name=name,
                embedding_function=None,
                metadata={
                    "hnsw:space": "cosine",
                    "memory_kind": memory_kind,
                    "embedding_version": self.embedding_version,
                    "embedding_provider": str(self.backend.name),
                    "embedding_model": str(self.backend.model),
                },
            )
        except Exception as exc:
            raise MemoryVectorStoreUnavailable(f"无法打开记忆 Chroma collection {name}: {exc}") from exc
        metadata = dict(getattr(collection, "metadata", None) or {})
        if metadata.get("embedding_version") not in {None, self.embedding_version}:
            raise MemoryVectorStoreUnavailable(f"collection {name} 的 embedding 版本不匹配")
        return collection

    def _create_client(self):
        try:
            import chromadb
        except ImportError as exc:
            raise MemoryVectorStoreUnavailable("缺少 chromadb 依赖") from exc
        try:
            if self.settings.memory_chroma_mode == "http":
                return chromadb.HttpClient(
                    host=self.settings.memory_chroma_host,
                    port=int(self.settings.memory_chroma_port),
                )
            if self.settings.memory_chroma_mode == "persistent":
                path = Path(self.settings.memory_chroma_path)
                if not path.is_absolute():
                    path = self.settings.project_root / path
                path.mkdir(parents=True, exist_ok=True)
                return chromadb.PersistentClient(path=str(path))
        except Exception as exc:
            raise MemoryVectorStoreUnavailable(f"记忆 Chroma 连接失败: {exc}") from exc
        raise MemoryVectorStoreUnavailable("记忆 Chroma 已禁用")

    @staticmethod
    def episode_document_id(public_id: str) -> str:
        return f"episode:{public_id}"

    @staticmethod
    def profile_document_id(user_id: int, public_id: str) -> str:
        return f"profile:{int(user_id)}:{public_id}"


def _safe_version(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "v1")).strip("-_")
    return (normalized or "v1")[:32]
