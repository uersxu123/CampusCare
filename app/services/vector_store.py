from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.core.config import Settings
from app.models.entities import KnowledgeChunk


PRIMARY_RETRIEVAL_LABEL = "versioned Chroma vector + BM25F + RRF"
FALLBACK_RETRIEVAL_LABEL = "BM25F + evidence coverage grader"
SEARCHABLE_TEXT_SCHEMA_VERSION = "v1:title-section-tags-content"


class VectorStoreUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class VectorSearchHit:
    chunk_id: int | None
    source: str
    source_index: int
    content: str
    score: float
    document_id: int | None = None


@dataclass(frozen=True)
class VectorQueryFilter:
    domains: tuple[str, ...] = ()
    sites: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ("ACTIVE",)
    source_types: tuple[str, ...] = ()
    document_ids: tuple[int, ...] = ()


class ChromaKnowledgeStore:
    """A single physical Chroma collection. Embedding is owned by embedding.py."""

    def __init__(
        self,
        settings: Settings,
        collection_name: str,
        *,
        create: bool = False,
        collection_metadata: dict | None = None,
    ):
        self.settings = settings
        self.collection_name = collection_name
        self.error = ""
        self.can_query = False
        if not settings.knowledge_vector_enabled:
            self.error = "Chroma 向量库未启用"
            return
        if not collection_name:
            self.error = "knowledge_index_registry 中没有 ACTIVE collection"
            return
        try:
            import chromadb
        except ImportError as exc:
            raise VectorStoreUnavailable("缺少 chromadb 依赖，无法访问版本化向量索引") from exc

        self.persist_dir = self._resolve_path(settings.chroma_persist_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=str(self.persist_dir))
        try:
            if create:
                metadata = {"hnsw:space": "cosine", **(collection_metadata or {})}
                self.collection = self.client.create_collection(
                    name=collection_name,
                    embedding_function=None,
                    metadata=metadata,
                )
            else:
                self.collection = self.client.get_collection(name=collection_name, embedding_function=None)
        except Exception as exc:
            raise VectorStoreUnavailable(f"无法打开 Chroma collection {collection_name}: {exc}") from exc
        self.can_query = True

    @property
    def can_embed(self) -> bool:
        """Compatibility alias. This store never performs embedding itself."""
        return self.can_query

    @property
    def metadata(self) -> dict:
        return dict(getattr(self.collection, "metadata", None) or {}) if self.can_query else {}

    def upsert_chunks(
        self,
        chunks: list[KnowledgeChunk],
        embeddings: list[list[float]],
        searchable_texts: list[str] | None = None,
    ) -> int:
        rows = [chunk for chunk in chunks if chunk.id is not None and chunk.content.strip()]
        if len(rows) != len(embeddings):
            raise ValueError("向量数量与知识片段数量不一致")
        if not rows:
            return 0
        documents = searchable_texts or [searchable_text_for_chunk(chunk) for chunk in rows]
        if len(documents) != len(rows):
            raise ValueError("索引文本数量与知识片段数量不一致")
        self.collection.upsert(
            ids=[self._id(int(chunk.id)) for chunk in rows],
            documents=documents,
            metadatas=[self._metadata(chunk) for chunk in rows],
            embeddings=embeddings,
        )
        return len(rows)

    def query(
        self,
        query_embedding: list[float],
        top_k: int,
        filters: VectorQueryFilter | None = None,
    ) -> list[VectorSearchHit]:
        count = self.count()
        if count <= 0:
            return []
        where = build_chroma_where(filters or VectorQueryFilter())
        result = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=min(max(1, top_k), count),
            where=where or None,
            include=["documents", "metadatas", "distances"],
        )
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        hits = []
        for index, document in enumerate(documents):
            metadata = metadatas[index] if index < len(metadatas) else {}
            distance = float(distances[index]) if index < len(distances) else 1.0
            hits.append(
                VectorSearchHit(
                    chunk_id=int(metadata["db_id"]) if metadata.get("db_id") is not None else None,
                    source=str(metadata.get("source", "")),
                    source_index=int(metadata.get("source_index", 0)),
                    content=document or "",
                    score=1.0 / (1.0 + max(0.0, distance)),
                    document_id=(
                        int(metadata["document_id"])
                        if metadata.get("document_id") is not None
                        else None
                    ),
                )
            )
        return hits

    def count(self) -> int:
        return int(self.collection.count()) if self.can_query else 0

    def ids(self) -> set[str]:
        return set(self.collection.get(include=[]).get("ids") or []) if self.can_query else set()

    def snapshot(self) -> str | None:
        if not self.can_query or not self.persist_dir.exists():
            return None
        snapshot_root = self._resolve_path(self.settings.chroma_snapshot_dir)
        snapshot_root.mkdir(parents=True, exist_ok=True)
        destination = snapshot_root / datetime.utcnow().strftime("%Y%m%d-%H%M%S-%f")
        shutil.copytree(self.persist_dir, destination)
        self._prune_snapshots(snapshot_root)
        return str(destination)

    def delete_collection(self) -> None:
        if self.can_query:
            self.client.delete_collection(self.collection_name)
            self.can_query = False

    def _resolve_path(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.settings.project_root / path

    def _prune_snapshots(self, snapshot_root: Path) -> None:
        keep = max(1, self.settings.chroma_snapshot_keep)
        snapshots = sorted([path for path in snapshot_root.iterdir() if path.is_dir()], reverse=True)
        for stale in snapshots[keep:]:
            shutil.rmtree(stale, ignore_errors=True)

    @staticmethod
    def _id(chunk_id: int) -> str:
        return f"knowledge-chunk-{chunk_id}"

    @staticmethod
    def _metadata(chunk: KnowledgeChunk) -> dict:
        document = chunk.document
        if document is None:
            return {
                "db_id": int(chunk.id),
                "source": chunk.source,
                "source_index": int(chunk.source_index),
                "status": "ACTIVE",
                "site": "ALL",
                "source_type": "LEGACY",
                "domain": "",
                "content_hash": chunk.content_hash or "",
            }
        return {
            "db_id": int(chunk.id),
            "source": chunk.source,
            "source_index": int(chunk.source_index),
            "document_id": int(document.id),
            "source_key": document.source_key,
            "canonical_key": document.canonical_key or document.source_key,
            "domain": document.domain,
            "site": document.site,
            "status": document.status,
            "source_type": document.source_type,
            "section_title": chunk.section_title or "",
            "page_number": int(chunk.page_number or 0),
            "content_hash": chunk.content_hash or "",
        }


def searchable_text_for_chunk(chunk: KnowledgeChunk) -> str:
    document = chunk.document
    title = document.title if document else chunk.source
    tags = ""
    if document is not None:
        try:
            parsed = json.loads(document.tags_json or "[]")
            tags = " ".join(str(item) for item in parsed) if isinstance(parsed, list) else ""
        except json.JSONDecodeError:
            tags = ""
    return "\n".join(
        (
            f"文档标题: {title}",
            f"章节标题: {chunk.section_title or ''}",
            f"标题路径: {chunk.heading_path_json or '[]'}",
            f"片段类型: {chunk.chunk_kind or 'TEXT_CHILD'}",
            f"标签: {tags}",
            f"正文: {chunk.content}",
        )
    )


def build_chroma_where(filters: VectorQueryFilter) -> dict:
    clauses: list[dict] = []
    for field, values in (
        ("domain", filters.domains),
        ("site", filters.sites),
        ("status", filters.statuses),
        ("source_type", filters.source_types),
        ("document_id", filters.document_ids),
    ):
        unique = list(dict.fromkeys(values))
        if not unique:
            continue
        clauses.append({field: unique[0] if len(unique) == 1 else {"$in": unique}})
    if not clauses:
        return {}
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


ChromaKnowledgeVectorStore = ChromaKnowledgeStore
