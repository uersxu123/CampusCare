from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from sqlalchemy import and_, or_, text
from sqlalchemy.orm import Session, joinedload

from app.core.config import Settings
from app.models.entities import KnowledgeChunk, KnowledgeDocument, KnowledgeIndexRegistry
from app.services.embedding import EmbeddingBackend, create_embedding_backend, embedding_identity
from app.services.knowledge_query import get_knowledge_taxonomy
from app.services.vector_store import (
    SEARCHABLE_TEXT_SCHEMA_VERSION,
    ChromaKnowledgeStore,
    VectorQueryFilter,
    VectorStoreUnavailable,
    searchable_text_for_chunk,
)


INDEX_STATE_EMPTY = "EMPTY"
INDEX_STATE_BUILDING = "BUILDING"
INDEX_STATE_READY = "READY"
INDEX_STATE_ACTIVE = "ACTIVE"
INDEX_STATE_FAILED = "FAILED"


@dataclass(frozen=True)
class IndexBuildResult:
    collection: str
    signature: str
    chunk_count: int
    embedding_dimension: int
    state: str


StoreFactory = Callable[..., ChromaKnowledgeStore]


class KnowledgeIndexManager:
    def __init__(
        self,
        db: Session,
        settings: Settings,
        *,
        backend: EmbeddingBackend | None = None,
        store_factory: StoreFactory = ChromaKnowledgeStore,
    ):
        self.db = db
        self.settings = settings
        self.backend = backend or create_embedding_backend(settings)
        self.store_factory = store_factory
        self.logical_name = settings.knowledge_vector_collection_base
        if not self.logical_name or len(self.logical_name) > 64:
            raise ValueError("KNOWLEDGE_VECTOR_COLLECTION_BASE 必须为 1 到 64 个字符")

    def build(self, *, batch_size: int = 32) -> IndexBuildResult:
        if not self.settings.knowledge_vector_enabled:
            raise RuntimeError("KNOWLEDGE_VECTOR_ENABLED=false，不能构建向量索引")
        if not self.backend.available():
            raise RuntimeError(f"{self.backend.name} embedding backend 不可用")
        lock_acquired = self._acquire_build_lock()
        if not lock_acquired:
            raise RuntimeError("另一个实例正在构建知识向量索引")
        registry = self._registry(create=True, lock=True)
        registry.state = INDEX_STATE_BUILDING
        registry.metadata_json = _merge_metadata(
            registry.metadata_json,
            {"buildStartedAt": _utcnow().isoformat(), "lastError": ""},
        )
        self.db.add(registry)
        self.db.commit()
        store: ChromaKnowledgeStore | None = None
        try:
            chunks = self._eligible_chunks()
            if not chunks:
                raise RuntimeError("没有可建立索引的 ACTIVE 且已验证知识片段")
            identity = embedding_identity(self.backend)
            corpus_hash = compute_corpus_hash(chunks)
            texts = [searchable_text_for_chunk(chunk) for chunk in chunks]
            first_end = min(len(chunks), max(1, batch_size))
            first_embeddings = self.backend.embed_documents(texts[:first_end])
            dimension = len(first_embeddings[0]) if first_embeddings else 0
            if dimension <= 0:
                raise RuntimeError("embedding backend 返回了空维度")
            signature_metadata = {
                "corpus_hash": corpus_hash,
                "taxonomy_version": get_knowledge_taxonomy().version,
                "embedding_provider": identity.provider,
                "embedding_model": identity.model,
                "embedding_model_digest": identity.digest,
                "embedding_digest_resolved": identity.digest_resolved,
                "embedding_dimension": dimension,
                "searchable_text_schema_version": SEARCHABLE_TEXT_SCHEMA_VERSION,
                "chunk_count": len(chunks),
                "parser_versions": sorted({chunk.document.parser_version for chunk in chunks if chunk.document}),
                "chunking_profiles": sorted({chunk.chunking_profile for chunk in chunks}),
            }
            signature = compute_index_signature(signature_metadata)
            collection_name = f"{self.settings.knowledge_vector_collection_base}__{signature[:12]}"
            current = self._registry(create=True)
            if current.active_collection == collection_name and current.active_signature == signature:
                verification = self.verify("active")
                return IndexBuildResult(collection_name, signature, verification["chunkCount"], dimension, INDEX_STATE_ACTIVE)
            store = self._fresh_collection(collection_name, signature_metadata, current.active_collection)
            store.upsert_chunks(chunks[:first_end], first_embeddings, texts[:first_end])
            for start in range(first_end, len(chunks), max(1, batch_size)):
                end = min(len(chunks), start + max(1, batch_size))
                embeddings = self.backend.embed_documents(texts[start:end])
                if any(len(vector) != dimension for vector in embeddings):
                    raise RuntimeError("embedding backend 在构建期间返回了不同维度")
                store.upsert_chunks(chunks[start:end], embeddings, texts[start:end])
            if store.count() != len(chunks):
                raise RuntimeError(f"Chroma count 校验失败: {store.count()} != {len(chunks)}")

            metadata = _metadata(registry.metadata_json)
            collections = dict(metadata.get("collections") or {})
            collections[collection_name] = {
                **signature_metadata,
                "index_signature": signature,
                "created_at": _utcnow().isoformat(),
                "shadow_verified": False,
            }
            registry = self._registry(create=True, lock=True)
            registry.ready_collection = collection_name
            registry.ready_signature = signature
            registry.state = INDEX_STATE_READY
            registry.metadata_json = _merge_metadata(
                registry.metadata_json,
                {
                    "collections": collections,
                    "buildCompletedAt": _utcnow().isoformat(),
                    "lastError": "",
                },
            )
            self.db.add(registry)
            self.db.commit()
            return IndexBuildResult(collection_name, signature, len(chunks), dimension, INDEX_STATE_READY)
        except Exception as exc:
            self.db.rollback()
            registry = self._registry(create=True, lock=True)
            registry.state = INDEX_STATE_FAILED
            registry.metadata_json = _merge_metadata(
                registry.metadata_json,
                {
                    "buildFailedAt": _utcnow().isoformat(),
                    "lastError": f"{type(exc).__name__}: {str(exc)[:500]}",
                },
            )
            self.db.add(registry)
            self.db.commit()
            raise
        finally:
            self._release_build_lock()

    def verify(self, target: str = "ready") -> dict:
        registry = self._registry(create=False)
        if registry is None:
            raise RuntimeError("knowledge_index_registry 尚未初始化")
        collection, signature = self._target(registry, target)
        metadata = _collection_metadata(registry.metadata_json, collection)
        if not collection or not signature or not metadata:
            raise RuntimeError(f"registry 中没有可验证的 {target.upper()} collection")
        if metadata.get("index_signature") != signature:
            raise RuntimeError("registry signature 与 collection 元数据不一致")
        store = self.store_factory(self.settings, collection, create=False)
        if store.metadata.get("index_signature") != signature:
            raise RuntimeError("Chroma collection signature 与 MySQL registry 不一致")
        expected_count = int(metadata.get("chunk_count") or 0)
        actual_count = store.count()
        if actual_count != expected_count:
            raise RuntimeError(f"Chroma count 校验失败: {actual_count} != {expected_count}")
        current_hash = compute_corpus_hash(self._eligible_chunks())
        if current_hash != metadata.get("corpus_hash"):
            raise RuntimeError("当前 MySQL 语料 hash 与索引 corpus_hash 不一致")
        return {
            "target": target,
            "collection": collection,
            "signature": signature,
            "chunkCount": actual_count,
            "embeddingDimension": int(metadata.get("embedding_dimension") or 0),
            "corpusHash": current_hash,
            "verified": True,
        }

    def shadow(self, queries: list[str] | None = None, *, compare_active: bool = True) -> dict:
        registry = self._registry(create=False)
        if registry is None or not registry.ready_collection:
            raise RuntimeError("没有 READY collection 可执行 shadow")
        self.verify("ready")
        queries = queries or ["调宿申请流程", "处分申诉材料", "休学申请怎么办", "心理咨询预约"]
        ready_store = self.store_factory(self.settings, registry.ready_collection, create=False)
        active_store = (
            self.store_factory(self.settings, registry.active_collection, create=False)
            if compare_active and registry.active_collection
            else None
        )
        comparisons = []
        for query in queries:
            embedding = self.backend.embed_query(query)
            filters = VectorQueryFilter(statuses=("ACTIVE",))
            ready_ids = [hit.chunk_id for hit in ready_store.query(embedding, 8, filters)]
            if not ready_ids:
                raise RuntimeError("新索引验证查询未返回任何片段")
            active_ids = [hit.chunk_id for hit in active_store.query(embedding, 8, filters)] if active_store else []
            comparisons.append(
                {
                    "queryHash": hashlib.sha256(query.encode("utf-8")).hexdigest()[:16],
                    "readyChunkIds": ready_ids,
                    "activeChunkIds": active_ids,
                    "overlap": len(set(ready_ids).intersection(active_ids)),
                }
            )
        registry = self._registry(create=False, lock=True)
        metadata = _metadata(registry.metadata_json)
        collections = dict(metadata.get("collections") or {})
        ready_metadata = dict(collections.get(registry.ready_collection) or {})
        ready_metadata["shadow_verified"] = True
        ready_metadata["shadow_verified_at"] = _utcnow().isoformat()
        ready_metadata["shadow_query_count"] = len(comparisons)
        collections[registry.ready_collection] = ready_metadata
        registry.metadata_json = _merge_metadata(registry.metadata_json, {"collections": collections})
        self.db.add(registry)
        self.db.commit()
        return {
            "readyCollection": registry.ready_collection,
            "comparedActive": compare_active,
            "comparisons": comparisons,
            "passed": True,
        }

    def activate(self) -> dict:
        registry = self._registry(create=False, lock=True)
        if registry is None or not registry.ready_collection or not registry.ready_signature:
            raise RuntimeError("没有 READY collection 可激活")
        ready_metadata = _collection_metadata(registry.metadata_json, registry.ready_collection)
        if not ready_metadata.get("shadow_verified"):
            raise RuntimeError("READY collection 尚未通过 shadow，禁止激活")
        self.verify("ready")
        registry = self._registry(create=False, lock=True)
        registry.previous_collection = registry.active_collection
        registry.active_collection = registry.ready_collection
        registry.active_signature = registry.ready_signature
        registry.ready_collection = None
        registry.ready_signature = None
        registry.state = INDEX_STATE_ACTIVE
        registry.metadata_json = _merge_metadata(
            registry.metadata_json,
            {"activatedAt": _utcnow().isoformat()},
        )
        self.db.add(registry)
        self.db.commit()
        return self.status()

    def rollback(self) -> dict:
        registry = self._registry(create=False, lock=True)
        if registry is None or not registry.previous_collection:
            raise RuntimeError("没有 previous collection 可回滚")
        previous_metadata = _collection_metadata(registry.metadata_json, registry.previous_collection)
        previous_signature = str(previous_metadata.get("index_signature") or "")
        if not previous_signature:
            raise RuntimeError("previous collection 缺少索引签名")
        current = registry.active_collection
        registry.active_collection = registry.previous_collection
        registry.active_signature = previous_signature
        registry.previous_collection = current
        registry.state = INDEX_STATE_ACTIVE
        registry.metadata_json = _merge_metadata(
            registry.metadata_json,
            {"rolledBackAt": _utcnow().isoformat()},
        )
        self.db.add(registry)
        self.db.commit()
        return self.status()

    def status(self) -> dict:
        registry = self._registry(create=False)
        if registry is None:
            return {
                "logicalName": self.logical_name,
                "state": INDEX_STATE_EMPTY,
                "activeCollection": None,
                "readyCollection": None,
                "previousCollection": None,
            }
        metadata = _metadata(registry.metadata_json)
        return {
            "logicalName": registry.logical_name,
            "state": registry.state,
            "activeCollection": registry.active_collection,
            "previousCollection": registry.previous_collection,
            "readyCollection": registry.ready_collection,
            "activeSignature": registry.active_signature,
            "readySignature": registry.ready_signature,
            "metadata": metadata,
            "updatedAt": registry.updated_at.isoformat() if registry.updated_at else None,
        }

    def _fresh_collection(self, name: str, metadata: dict, active_collection: str | None) -> ChromaKnowledgeStore:
        collection_metadata = {
            "index_signature": compute_index_signature(metadata),
            "corpus_hash": str(metadata["corpus_hash"]),
            "embedding_provider": str(metadata["embedding_provider"]),
            "embedding_model": str(metadata["embedding_model"]),
            "embedding_model_digest": str(metadata["embedding_model_digest"]),
            "embedding_dimension": int(metadata["embedding_dimension"]),
            "searchable_text_schema_version": str(metadata["searchable_text_schema_version"]),
        }
        try:
            return self.store_factory(
                self.settings,
                name,
                create=True,
                collection_metadata=collection_metadata,
            )
        except VectorStoreUnavailable:
            if name == active_collection:
                raise RuntimeError("禁止在 ACTIVE collection 上原地重建")
            stale = self.store_factory(self.settings, name, create=False)
            stale.delete_collection()
            return self.store_factory(
                self.settings,
                name,
                create=True,
                collection_metadata=collection_metadata,
            )

    def _eligible_chunks(self) -> list[KnowledgeChunk]:
        now = _utcnow()
        return (
            self.db.query(KnowledgeChunk)
            .options(joinedload(KnowledgeChunk.document))
            .join(KnowledgeDocument)
            .filter(
                and_(
                    KnowledgeDocument.status == "ACTIVE",
                    KnowledgeDocument.verified_at.is_not(None),
                    or_(KnowledgeDocument.expires_at.is_(None), KnowledgeDocument.expires_at > now),
                )
            )
            .filter(or_(KnowledgeChunk.chunk_kind.is_(None), KnowledgeChunk.chunk_kind != "TEXT_PARENT"))
            .order_by(KnowledgeDocument.canonical_key.asc(), KnowledgeChunk.source_index.asc(), KnowledgeChunk.id.asc())
            .all()
        )

    def _registry(self, *, create: bool, lock: bool = False) -> KnowledgeIndexRegistry | None:
        query = self.db.query(KnowledgeIndexRegistry).filter(KnowledgeIndexRegistry.logical_name == self.logical_name)
        if lock:
            query = query.with_for_update()
        row = query.one_or_none()
        if row is None and create:
            row = KnowledgeIndexRegistry(logical_name=self.logical_name, state=INDEX_STATE_EMPTY, metadata_json="{}")
            self.db.add(row)
            self.db.flush()
        return row

    @staticmethod
    def _target(registry: KnowledgeIndexRegistry, target: str) -> tuple[str | None, str | None]:
        if target == "ready":
            return registry.ready_collection, registry.ready_signature
        if target == "active":
            return registry.active_collection, registry.active_signature
        raise ValueError("target 必须是 ready 或 active")

    def _acquire_build_lock(self) -> bool:
        if self.db.bind is None or self.db.bind.dialect.name != "mysql":
            return True
        value = self.db.execute(
            text("SELECT GET_LOCK(:name, 0)"),
            {"name": f"mindbridge:index:{self.logical_name}"},
        ).scalar()
        return int(value or 0) == 1

    def _release_build_lock(self) -> None:
        if self.db.bind is None or self.db.bind.dialect.name != "mysql":
            return
        try:
            self.db.execute(
                text("SELECT RELEASE_LOCK(:name)"),
                {"name": f"mindbridge:index:{self.logical_name}"},
            )
        except Exception:
            self.db.rollback()


def compute_corpus_hash(chunks: list[KnowledgeChunk]) -> str:
    rows = []
    for chunk in chunks:
        document = chunk.document
        rows.append(
            {
                "chunk_id": chunk.id,
                "source_index": chunk.source_index,
                "content_hash": chunk.content_hash,
                "section_title": chunk.section_title,
                "page_number": chunk.page_number,
                "canonical_key": (document.canonical_key or document.source_key) if document else chunk.source,
                "document_hash": document.content_hash if document else "",
                "domain": document.domain if document else "",
                "site": document.site if document else "ALL",
                "status": document.status if document else "ACTIVE",
                "source_type": document.source_type if document else "LEGACY",
                "parser_version": document.parser_version if document else "legacy",
                "parser_profile": document.parser_profile if document else "legacy",
                "chunk_kind": chunk.chunk_kind,
                "chunking_profile": chunk.chunking_profile,
                "token_count": chunk.token_count,
                "heading_path_json": chunk.heading_path_json,
                "element_ids_json": chunk.element_ids_json,
            }
        )
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_index_signature(metadata: dict) -> str:
    payload = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _metadata(raw: str) -> dict:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _merge_metadata(raw: str, changes: dict) -> str:
    value = _metadata(raw)
    value.update(changes)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _collection_metadata(raw: str, collection: str | None) -> dict:
    if not collection:
        return {}
    return dict((_metadata(raw).get("collections") or {}).get(collection) or {})


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
