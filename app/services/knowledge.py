from __future__ import annotations

import json
import hashlib
import logging
import re
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Hashable

from pypdf import PdfReader
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, joinedload

from app.core.config import Settings
from app.models.entities import KnowledgeChunk, KnowledgeDocument, KnowledgeIndexRegistry
from app.services.embedding import create_embedding_backend
from app.services.knowledge_query import get_knowledge_taxonomy
from app.services.knowledge_scoring import (
    EvidenceProvenance,
    KnowledgeTokenizer,
    RetrievalScore,
    bm25f_scores,
    reciprocal_rank_fusion,
)
from app.services.vector_store import (
    FALLBACK_RETRIEVAL_LABEL,
    PRIMARY_RETRIEVAL_LABEL,
    ChromaKnowledgeStore,
    VectorQueryFilter,
)
from app.services.retrieval_capture import record_retrieval_candidates


logger = logging.getLogger(__name__)


@dataclass
class StructuredSearchRequest:
    query: str
    domains: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    site: str = "ALL"
    allowed_source_types: tuple[str, ...] = ()
    as_of: datetime | None = None
    top_k: int = 4
    document_limit: int = 4
    include_neighbors: bool = True


@dataclass
class KnowledgeSearchResult:
    chunk_id: int | None
    source: str
    content: str
    score: float
    source_key: str = ""
    document_id: int | None = None
    title: str = ""
    source_url: str | None = None
    domain: str = ""
    section_title: str | None = None
    page_number: int | None = None
    version: str = ""
    verified_at: datetime | None = None
    expires_at: datetime | None = None
    source_type: str = ""
    site: str = "ALL"
    status: str = "ACTIVE"
    tags: tuple[str, ...] = ()
    canonical_key: str = ""
    retrieval_score: RetrievalScore | None = None
    provenance: EvidenceProvenance | None = None
    retrieval_method: str = "hybrid"
    parent_chunk_id: int | None = None


SearchResult = KnowledgeSearchResult


@dataclass(frozen=True)
class CandidateSearchResult:
    bm25_ranking: tuple[KnowledgeSearchResult, ...]
    vector_ranking: tuple[KnowledgeSearchResult, ...]
    candidates: tuple[KnowledgeSearchResult, ...]
    diagnostics: dict


class KnowledgeService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.embedding_backend = create_embedding_backend(settings)
        self.vector_store = None
        self.vector_error = ""
        self.active_collection: str | None = None
        self.active_signature: str | None = None
        self._logged_vector_errors: set[str] = set()
        self.last_search_diagnostics: dict = {"retrievalMode": "bm25-only", "vectorDegraded": False}
        if settings.knowledge_vector_enabled:
            registry = self._index_registry()
            if registry is None or not registry.active_collection:
                self.vector_error = "knowledge_index_registry 中没有 ACTIVE collection"
            else:
                self.active_collection = registry.active_collection
                self.active_signature = registry.active_signature
                try:
                    self.vector_store = ChromaKnowledgeStore(settings, registry.active_collection, create=False)
                except Exception as exc:
                    self.vector_error = f"{type(exc).__name__}: {exc}"

    def count(self) -> int:
        return self.db.query(KnowledgeChunk).count()

    def ensure_source(self, source: str, content: str, domain: str = "MENTAL_HEALTH") -> int:
        chunks = chunk_text(content, self.settings.knowledge_chunk_size, self.settings.knowledge_chunk_overlap)
        document = self.db.query(KnowledgeDocument).filter(KnowledgeDocument.source_key == source).one_or_none()
        existing = [] if document is None else [
            chunk.content for chunk in self.db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.document_id == document.id).order_by(KnowledgeChunk.source_index.asc()).all()
        ]
        if existing == chunks:
            changed = False
            for field, value in (
                ("canonical_key", document.canonical_key or source),
                ("managed_by", document.managed_by or "LEGACY"),
                ("title", source.removesuffix(".md")),
                ("source_type", "INTERNAL_GUIDANCE"),
                ("domain", domain),
                ("status", "ACTIVE"),
            ):
                if getattr(document, field) != value:
                    setattr(document, field, value)
                    changed = True
            if document.verified_at is None:
                document.verified_at = datetime.now(UTC).replace(tzinfo=None)
                changed = True
            if changed:
                self.db.add(document)
                self.db.commit()
            return len(existing)
        return self._ingest_document(
            source=source,
            content=content,
            title=source.removesuffix(".md"),
            source_type="INTERNAL_GUIDANCE",
            domain=domain,
            status="ACTIVE",
            verified_at=datetime.now(UTC).replace(tzinfo=None),
        )

    def status(self) -> dict:
        registry = self._index_registry()
        vector_chunks = None
        vector_error = self.vector_error
        vector_available = bool(self.vector_store and self.vector_store.can_query)
        if vector_available:
            try:
                vector_chunks = self.vector_store.count()
            except Exception as exc:
                vector_error = f"{type(exc).__name__}: {exc}"
                vector_available = False
        if vector_available and not self.embedding_backend.available():
            vector_available = False
            vector_error = f"{self.embedding_backend.name} embedding backend 不可用"
        if vector_available:
            retrieval_mode = "hybrid"
        elif self.settings.knowledge_vector_enabled:
            retrieval_mode = "degraded-bm25-only"
        else:
            retrieval_mode = "bm25-only"
        return {
            "retrievalOrder": [
                PRIMARY_RETRIEVAL_LABEL,
                f"{FALLBACK_RETRIEVAL_LABEL} when embedding/Chroma/ACTIVE index is unavailable",
            ],
            "primaryRetrieval": PRIMARY_RETRIEVAL_LABEL,
            "fallbackRetrieval": FALLBACK_RETRIEVAL_LABEL,
            "retrievalMode": retrieval_mode,
            "databaseChunks": self.count(),
            "vectorEnabled": self.settings.knowledge_vector_enabled,
            "vectorAvailable": vector_available,
            "vectorRequired": self.settings.knowledge_vector_required,
            "embeddingProvider": self.embedding_backend.name,
            "embeddingModel": self.embedding_backend.model,
            "vectorChunks": vector_chunks,
            "chromaPersistDir": self.settings.chroma_persist_dir,
            "activeCollection": registry.active_collection if registry else None,
            "readyCollection": registry.ready_collection if registry else None,
            "previousCollection": registry.previous_collection if registry else None,
            "indexState": registry.state if registry else "EMPTY",
            "activeSignature": registry.active_signature if registry else None,
            "readySignature": registry.ready_signature if registry else None,
            "chromaSnapshotDir": self.settings.chroma_snapshot_dir,
            "candidateK": self.settings.knowledge_candidate_k,
            "vectorError": vector_error,
        }

    def ingest(self, source: str, content: str) -> int:
        from app.services.knowledge_ingestion.pipeline import KnowledgeIngestionPipeline

        return KnowledgeIngestionPipeline(self.db, self.settings).ingest_legacy_text(
            source=source,
            content=content,
        ).chunk_count

    def _ingest_document(
        self,
        source: str,
        content: str,
        title: str,
        source_type: str,
        domain: str,
        status: str,
        verified_at: datetime | None,
    ) -> int:
        from app.services.knowledge_ingestion.pipeline import KnowledgeIngestionPipeline

        return KnowledgeIngestionPipeline(self.db, self.settings).ingest_legacy_text(
            source=source,
            content=content,
            title=title,
            source_type=source_type,
            domain=domain,
            status=status,
            verified_at=verified_at,
            managed_by="UPLOAD" if source_type == "UPLOAD" else "LEGACY",
        ).chunk_count

    def ingest_file(self, filename: str, data: bytes) -> int:
        from app.services.knowledge_ingestion.pipeline import KnowledgeIngestionPipeline

        return KnowledgeIngestionPipeline(self.db, self.settings).ingest_legacy_file(
            source=filename,
            data=data,
        ).chunk_count

    def retrieve(self, query: str, top_k: int | None = None) -> list[SearchResult]:
        return self.search(
            StructuredSearchRequest(
                query=query,
                top_k=top_k or self.settings.knowledge_top_k,
                document_limit=top_k or self.settings.knowledge_top_k,
            )
        )

    def search(self, request: StructuredSearchRequest) -> list[KnowledgeSearchResult]:
        started = time.perf_counter()
        top_k = max(1, request.top_k)
        candidate_k = self._candidate_k(top_k)
        filter_started = time.perf_counter()
        eligible_chunks = self._filtered_chunks(request)
        filter_ms = _elapsed_stage_ms(filter_started)
        eligible_by_id = {int(chunk.id): chunk for chunk in eligible_chunks if chunk.id is not None}
        vector_started = time.perf_counter()
        vector_results = (
            self._retrieve_vector(request, candidate_k, eligible_by_id)
            if self.settings.knowledge_vector_enabled
            else []
        )
        vector_ms = _elapsed_stage_ms(vector_started)
        bm25_started = time.perf_counter()
        bm25_results = self._retrieve_bm25(request.query, candidate_k, eligible_chunks)
        bm25_ms = _elapsed_stage_ms(bm25_started)
        rank_started = time.perf_counter()
        ranked = self._rank_with_rrf(vector_results, bm25_results, candidate_k)
        candidate_diagnostics = [_candidate_diagnostic(item) for item in ranked]
        if ranked:
            ranked = self._select_diverse(
                ranked,
                top_k,
                request.document_limit,
                request.query,
                eligible_by_id,
            )
            results = self._expand_seeds(ranked, top_k) if request.include_neighbors else ranked[:top_k]
        else:
            results = []
        rank_ms = _elapsed_stage_ms(rank_started)
        vector_degraded = bool(self.settings.knowledge_vector_enabled and self.vector_error)
        if vector_degraded:
            retrieval_mode = "degraded-bm25-only"
        elif self.settings.knowledge_vector_enabled:
            retrieval_mode = "hybrid"
        else:
            retrieval_mode = "bm25-only"
        self.last_search_diagnostics = {
            "schemaVersion": 1,
            "retrievalMode": retrieval_mode,
            "taxonomyVersion": get_knowledge_taxonomy().version,
            "tokenizerVersion": KnowledgeTokenizer.version,
            "indexVersion": self.active_signature,
            "activeCollection": self.active_collection,
            "vectorBackend": {
                "enabled": bool(self.settings.knowledge_vector_enabled),
                "provider": self.embedding_backend.name,
                "model": self.embedding_backend.model,
                "degraded": vector_degraded,
                "degradationReason": self.vector_error or None,
            },
            "vectorDegraded": vector_degraded,
            "vectorError": self.vector_error,
            "bm25CandidateCount": len(bm25_results),
            "vectorCandidateCount": len(vector_results),
            "eligibleChunkCount": len(eligible_chunks),
            "candidateCount": len(candidate_diagnostics),
            "candidates": candidate_diagnostics,
            "finalCandidates": [
                {
                    "rank": rank,
                    "chunkId": item.chunk_id,
                    "canonicalKey": item.canonical_key,
                    "sourceKey": item.source_key,
                }
                for rank, item in enumerate(results, start=1)
            ],
            "stageDurationMs": {
                "filter": filter_ms,
                "vector": vector_ms,
                "bm25": bm25_ms,
                "fusionRerankExpand": rank_ms,
                "total": _elapsed_stage_ms(started),
            },
        }
        record_retrieval_candidates(results)
        return results

    def search_candidates(
        self,
        *,
        query: str,
        per_list_k: int = 40,
        site: str | None = None,
        allowed_source_types: tuple[str, ...] = (),
        as_of: datetime | None = None,
    ) -> CandidateSearchResult:
        request = StructuredSearchRequest(
            query=query,
            domains=(),
            site=site or "ALL",
            allowed_source_types=allowed_source_types,
            as_of=as_of,
            top_k=max(1, per_list_k),
            include_neighbors=False,
        )
        started = time.perf_counter()
        eligible_chunks = self._filtered_chunks(request)
        eligible_by_id = {int(chunk.id): chunk for chunk in eligible_chunks if chunk.id is not None}
        bm25_error = ""
        vector_error = ""
        try:
            bm25 = self._retrieve_bm25(query, max(1, per_list_k), eligible_chunks)
        except Exception as exc:
            bm25 = []
            bm25_error = f"{type(exc).__name__}: {exc}"
        try:
            vector = self._retrieve_vector(request, max(1, per_list_k), eligible_by_id)
            vector_error = self.vector_error
        except Exception as exc:
            vector = []
            vector_error = f"{type(exc).__name__}: {exc}"
        by_key: dict[Hashable, KnowledgeSearchResult] = {}
        for item in (*bm25, *vector):
            by_key.setdefault(result_key(item), item)
        diagnostics = {
            "schemaVersion": 1,
            "retrievalMode": "hybrid" if not bm25_error and not vector_error else "degraded",
            "eligibleChunkCount": len(eligible_chunks),
            "bm25CandidateCount": len(bm25),
            "vectorCandidateCount": len(vector),
            "bm25Degraded": bool(bm25_error),
            "vectorDegraded": bool(vector_error),
            "bm25Error": bm25_error,
            "vectorError": vector_error,
            "indexVersion": self.active_signature,
            "activeCollection": self.active_collection,
            "stageDurationMs": {"total": _elapsed_stage_ms(started)},
        }
        candidates = list(by_key.values())
        # Capture the exact objects returned to the caller. Evaluation must observe
        # production candidates, rather than a reconstructed or Golden-derived copy.
        record_retrieval_candidates(candidates)
        self.last_search_diagnostics = diagnostics
        return CandidateSearchResult(tuple(bm25), tuple(vector), tuple(candidates), diagnostics)

    def _filtered_chunks(self, request: StructuredSearchRequest) -> list[KnowledgeChunk]:
        as_of = request.as_of or datetime.now(UTC).replace(tzinfo=None)
        query = self.db.query(KnowledgeChunk).options(joinedload(KnowledgeChunk.document)).outerjoin(KnowledgeDocument)
        query = query.filter(
            KnowledgeChunk.document_id.is_not(None),
            KnowledgeDocument.status == "ACTIVE",
            KnowledgeDocument.verified_at.is_not(None),
            or_(KnowledgeDocument.expires_at.is_(None), KnowledgeDocument.expires_at > as_of),
        )
        query = query.filter(
            or_(KnowledgeChunk.chunk_kind.is_(None), KnowledgeChunk.chunk_kind != "TEXT_PARENT")
        )
        if request.domains and "MIXED" not in request.domains:
            query = query.filter(KnowledgeDocument.domain.in_(request.domains))
        if request.allowed_source_types:
            query = query.filter(KnowledgeDocument.source_type.in_(request.allowed_source_types))
        if request.site and request.site != "ALL":
            query = query.filter(KnowledgeDocument.site.in_(("ALL", request.site)))
        return query.all()

    def _retrieve_bm25(self, query: str, top_k: int, chunks: list[KnowledgeChunk] | None = None) -> list[SearchResult]:
        chunks = chunks if chunks is not None else self.db.query(KnowledgeChunk).all()
        scores = bm25f_scores(query, chunks, KnowledgeTokenizer())
        ranked = [
            self._result_from_chunk(chunk, scores.get(chunk.id, 0.0), "bm25")
            for chunk in chunks
            if chunk.id is not None and scores.get(chunk.id, 0.0) > 0
        ]
        ranked.sort(key=lambda item: item.score, reverse=True)
        return ranked[:top_k]

    def _rank_with_rrf(
        self,
        vector_results: list[SearchResult],
        bm25_results: list[SearchResult],
        candidate_k: int,
    ) -> list[SearchResult]:
        by_key = {result_key(item): item for item in [*bm25_results, *vector_results]}
        if not by_key:
            return []
        bm25_keys = [result_key(item) for item in bm25_results]
        vector_keys = [result_key(item) for item in vector_results]
        rrf = reciprocal_rank_fusion(
            (
                (bm25_keys, 1.0),
                (vector_keys, 1.0 if vector_keys else 0.0),
            )
        )
        bm25_ranks = {key: rank for rank, key in enumerate(bm25_keys, start=1)}
        vector_ranks = {key: rank for rank, key in enumerate(vector_keys, start=1)}
        reranked = []
        for key, item in by_key.items():
            rrf_score = rrf.get(key, 0.0)
            components = RetrievalScore(
                bm25_rank=bm25_ranks.get(key),
                vector_rank=vector_ranks.get(key),
                reciprocal_rank_score=rrf_score,
                rerank_relevance=rrf_score,
            )
            reranked.append(
                replace(
                    item,
                    score=rrf_score,
                    retrieval_score=components,
                    retrieval_method="rrf_hybrid" if vector_keys else "bm25f",
                )
            )
        reranked.sort(key=lambda item: (-item.score, item.chunk_id or 0))
        return reranked[:candidate_k]

    def _candidate_k(self, top_k: int) -> int:
        return max(top_k, 48, self.settings.knowledge_candidate_k)

    def _retrieve_vector(
        self,
        request: StructuredSearchRequest,
        top_k: int,
        eligible_by_id: dict[int, KnowledgeChunk],
    ) -> list[SearchResult]:
        if self.vector_store is None:
            if not self.vector_error:
                self.vector_error = "RuntimeError: ACTIVE 向量索引不可用"
            if self.settings.knowledge_vector_required:
                raise RuntimeError(self.vector_error)
            self._log_vector_degradation("retrieve", self.vector_error)
            return []
        try:
            self._validate_active_index()
            query_embedding = self.embedding_backend.embed_query(request.query)
            document_ids = tuple(
                sorted({int(chunk.document_id) for chunk in eligible_by_id.values() if chunk.document_id is not None})
            )
            sites = () if not request.site or request.site == "ALL" else ("ALL", request.site)
            filters = VectorQueryFilter(
                domains=request.domains,
                sites=sites,
                statuses=("ACTIVE",),
                source_types=request.allowed_source_types,
                document_ids=document_ids,
            )
            hits = self.vector_store.query(
                query_embedding,
                max(top_k, self.settings.knowledge_vector_candidate_k),
                filters,
            )
        except Exception as exc:
            self._handle_vector_error("retrieve", exc)
            return []
        results = []
        for hit in hits:
            chunk = eligible_by_id.get(int(hit.chunk_id)) if hit.chunk_id is not None else None
            if chunk is None:
                continue
            results.append(self._result_from_chunk(chunk, hit.score, "vector"))
        return results[:top_k]

    def _result_from_chunk(self, chunk: KnowledgeChunk, score: float, method: str) -> KnowledgeSearchResult:
        document = chunk.document
        return KnowledgeSearchResult(
            chunk_id=chunk.id,
            parent_chunk_id=chunk.parent_chunk_id,
            source=chunk.source,
            content=chunk.content,
            score=score,
            source_key=document.source_key if document else chunk.source,
            document_id=document.id if document else None,
            title=document.title if document else chunk.source,
            source_url=document.source_url if document else None,
            domain=document.domain if document else "",
            section_title=chunk.section_title,
            page_number=chunk.page_number,
            version=document.version if document else "legacy",
            verified_at=document.verified_at if document else None,
            expires_at=document.expires_at if document else None,
            source_type=document.source_type if document else "",
            site=document.site if document else "ALL",
            status=document.status if document else "ACTIVE",
            tags=tuple(_json_list(document.tags_json)) if document else (),
            canonical_key=(document.canonical_key or document.source_key) if document else chunk.source,
            provenance=EvidenceProvenance(
                seed_chunk_id=chunk.id,
                child_chunk_ids=(chunk.id,) if chunk.id is not None else (),
                page_numbers=(chunk.page_number,) if chunk.page_number is not None else (),
                content_hashes=(chunk.content_hash,) if chunk.content_hash else (),
            ),
            retrieval_method=method,
        )

    def _select_diverse(
        self,
        ranked: list[SearchResult],
        top_k: int,
        document_limit: int,
        query: str,
        eligible_by_id: dict[int, KnowledgeChunk] | None = None,
    ) -> list[SearchResult]:
        accepted_documents: set[Hashable] = set()
        per_document: dict[Hashable, int] = {}
        chunk_ids = [item.chunk_id for item in ranked if item.chunk_id is not None]
        if eligible_by_id is None:
            eligible_by_id = {
                int(item.id): item
                for item in self.db.query(KnowledgeChunk).filter(KnowledgeChunk.id.in_(chunk_ids)).all()
                if item.id is not None
            }
        chunk_rows = [eligible_by_id[item] for item in chunk_ids if item in eligible_by_id]
        parent_by_chunk = {
            int(item.id): item.parent_chunk_id
            for item in chunk_rows
        }
        table_context_by_chunk = {
            int(item.id): _first_json_id(item.element_ids_json)
            for item in chunk_rows
            if item.chunk_kind in {"TABLE_SUMMARY", "TABLE_ROW"}
        }
        parent_ids = {value for value in parent_by_chunk.values() if value is not None}
        related_rows = (
            self.db.query(KnowledgeChunk)
            .filter(
                or_(
                    KnowledgeChunk.id.in_(parent_ids),
                    KnowledgeChunk.parent_chunk_id.in_(parent_ids),
                )
            )
            .order_by(KnowledgeChunk.source_index.asc())
            .all()
            if parent_ids
            else []
        )
        parent_rows = [item for item in related_rows if item.id in parent_ids]
        children_by_parent: dict[int, list[KnowledgeChunk]] = {}
        for child in related_rows:
            if child.parent_chunk_id in parent_ids:
                children_by_parent.setdefault(int(child.parent_chunk_id), []).append(child)
        parent_content = {int(item.id): item.content for item in parent_rows}
        self._parent_expansion_cache = {
            "chunks": {int(item.id): item for item in chunk_rows},
            "parents": {int(item.id): item for item in parent_rows},
            "children": children_by_parent,
        }
        selected_parent_contexts = _select_parent_contexts(
            ranked,
            parent_by_chunk,
            parent_content,
            query,
            max(1, self.settings.knowledge_max_evidence_per_document),
        )
        accepted_contexts: set[tuple[Hashable, int]] = set()
        table_context_counts: dict[tuple[Hashable, int], int] = {}
        per_document_limit = max(1, self.settings.knowledge_max_evidence_per_document)
        results: list[SearchResult] = []
        for item in ranked:
            key: Hashable = item.document_id if item.document_id is not None else item.source
            parent_id = parent_by_chunk.get(int(item.chunk_id)) if item.chunk_id is not None else None
            table_element_id = table_context_by_chunk.get(int(item.chunk_id)) if item.chunk_id is not None else None
            if table_element_id is not None:
                table_key = (key, table_element_id)
                if table_context_counts.get(table_key, 0) >= 2:
                    continue
            if parent_id is not None:
                context_key = (key, int(parent_id))
                if context_key not in selected_parent_contexts:
                    continue
                if context_key in accepted_contexts:
                    continue
            if per_document.get(key, 0) >= per_document_limit:
                continue
            if document_limit > 0 and key not in accepted_documents and len(accepted_documents) >= document_limit:
                continue
            accepted_documents.add(key)
            if parent_id is not None:
                accepted_contexts.add((key, int(parent_id)))
            per_document[key] = per_document.get(key, 0) + 1
            if table_element_id is not None:
                table_context_counts[(key, table_element_id)] = table_context_counts.get((key, table_element_id), 0) + 1
            results.append(item)
            if len(results) >= top_k:
                break
        return results

    def _handle_vector_error(self, action: str, exc: Exception) -> None:
        self.vector_error = f"{type(exc).__name__}: {exc}"
        if self.settings.knowledge_vector_required:
            raise exc
        self._log_vector_degradation(action, self.vector_error)

    def _log_vector_degradation(self, action: str, reason: str) -> None:
        key = f"{action}:{reason}"
        if key in self._logged_vector_errors:
            return
        self._logged_vector_errors.add(key)
        logger.warning(
            "%s %s failed; falling back to %s: %s",
            PRIMARY_RETRIEVAL_LABEL,
            action,
            FALLBACK_RETRIEVAL_LABEL,
            reason,
        )

    def _index_registry(self) -> KnowledgeIndexRegistry | None:
        try:
            return (
                self.db.query(KnowledgeIndexRegistry)
                .filter(KnowledgeIndexRegistry.logical_name == self.settings.knowledge_vector_collection_base)
                .one_or_none()
            )
        except Exception as exc:
            self.vector_error = f"knowledge_index_registry 不可用: {type(exc).__name__}: {exc}"
            return None

    def _validate_active_index(self) -> None:
        registry = self._index_registry()
        if registry is None or not registry.active_collection or not registry.active_signature:
            raise RuntimeError("knowledge_index_registry 中没有 ACTIVE collection/signature")
        if self.vector_store.collection_name != registry.active_collection:
            raise RuntimeError("请求加载的 Chroma collection 与 registry ACTIVE 指针不一致")
        if self.vector_store.metadata.get("index_signature") != registry.active_signature:
            raise RuntimeError("Chroma collection signature 与 registry ACTIVE signature 不一致")
        try:
            metadata = json.loads(registry.metadata_json or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("knowledge_index_registry metadata_json 非法") from exc
        collection_metadata = (metadata.get("collections") or {}).get(registry.active_collection) or {}
        if collection_metadata.get("embedding_provider") != self.embedding_backend.name:
            raise RuntimeError("ACTIVE 索引 embedding provider 与当前配置不一致")
        if collection_metadata.get("embedding_model") != self.embedding_backend.model:
            raise RuntimeError("ACTIVE 索引 embedding model 与当前配置不一致")
        current_digest = self.embedding_backend.model_digest()
        if collection_metadata.get("embedding_model_digest") != current_digest:
            raise RuntimeError("ACTIVE 索引 embedding model digest 与当前后端不一致")

    def _expand_seeds(self, ranked: list[SearchResult], top_k: int) -> list[SearchResult]:
        cache = getattr(self, "_parent_expansion_cache", {})
        chunks = cache.get("chunks", {})
        parents = cache.get("parents", {})
        children_by_parent = cache.get("children", {})
        results: list[SearchResult] = []
        seen: set[tuple[str, str]] = set()
        for seed in ranked:
            chunk = chunks.get(int(seed.chunk_id)) if seed.chunk_id is not None else None
            if chunk is not None and chunk.parent_chunk_id is not None and int(chunk.parent_chunk_id) in parents:
                expanded = self._expanded_parent_result(
                    seed,
                    chunk,
                    parents[int(chunk.parent_chunk_id)],
                    children_by_parent.get(int(chunk.parent_chunk_id), []),
                )
            else:
                expanded = self._expand_seed(seed)
            normalized_hash = hashlib.sha256(compact_text(expanded.content).encode("utf-8")).hexdigest()
            identity = (expanded.canonical_key or expanded.source_key, normalized_hash)
            if identity in seen:
                continue
            seen.add(identity)
            results.append(expanded)
            if len(results) >= top_k:
                break
        return results

    @staticmethod
    def _expanded_parent_result(
        result: SearchResult,
        chunk: KnowledgeChunk,
        parent: KnowledgeChunk,
        children: list[KnowledgeChunk],
    ) -> SearchResult:
        return replace(
            result,
            content=parent.content,
            provenance=EvidenceProvenance(
                seed_chunk_id=chunk.id,
                child_chunk_ids=tuple(item.id for item in children if item.id is not None),
                page_numbers=tuple(dict.fromkeys(item.page_number for item in children if item.page_number is not None)),
                content_hashes=tuple(item.content_hash for item in children if item.content_hash),
            ),
        )

    def expand_context(self, result: SearchResult) -> SearchResult:
        """Resolve the retrieved child's parent_chunk_id from MySQL after reranking."""
        if result.chunk_id is None:
            return result
        child = self.db.get(KnowledgeChunk, result.chunk_id)
        if child is None or child.parent_chunk_id is None:
            return result
        parent = self.db.get(KnowledgeChunk, child.parent_chunk_id)
        return replace(result, content=parent.content) if parent is not None else result

    def _expand_seed(self, result: SearchResult) -> SearchResult:
        if result.chunk_id is None:
            return result
        chunk = self.db.get(KnowledgeChunk, result.chunk_id)
        if chunk is None:
            return result
        if chunk.parent_chunk_id is not None:
            parent = self.db.get(KnowledgeChunk, chunk.parent_chunk_id)
            if parent is not None:
                children = (
                    self.db.query(KnowledgeChunk)
                    .filter(KnowledgeChunk.parent_chunk_id == parent.id)
                    .order_by(KnowledgeChunk.source_index.asc())
                    .all()
                )
                return self._expanded_parent_result(result, chunk, parent, children)
        neighbors = (
            self.db.query(KnowledgeChunk)
            .filter(
                KnowledgeChunk.document_id == chunk.document_id
                if chunk.document_id is not None
                else KnowledgeChunk.source == chunk.source
            )
            .filter(KnowledgeChunk.source_index >= max(0, chunk.source_index - 1))
            .filter(KnowledgeChunk.source_index <= chunk.source_index + 1)
            .order_by(KnowledgeChunk.source_index.asc())
            .all()
        )
        neighbors = [
            item
            for item in neighbors
            if item.id == chunk.id
            or (
                bool(chunk.section_title)
                and item.section_title == chunk.section_title
            )
            or (
                chunk.page_number is not None
                and item.page_number is not None
                and abs(item.page_number - chunk.page_number) <= 1
            )
        ]
        content = ""
        for item in neighbors:
            content = _merge_passage(content, item.content)
        child_ids = tuple(item.id for item in neighbors if item.id is not None)
        pages = tuple(dict.fromkeys(item.page_number for item in neighbors if item.page_number is not None))
        hashes = tuple(item.content_hash for item in neighbors if item.content_hash)
        return replace(
            result,
            content=content or result.content,
            provenance=EvidenceProvenance(
                seed_chunk_id=chunk.id,
                child_chunk_ids=child_ids,
                page_numbers=pages,
                content_hashes=hashes,
            ),
        )


def _candidate_diagnostic(item: KnowledgeSearchResult) -> dict:
    score = item.retrieval_score
    provenance = item.provenance
    return {
        "chunkId": item.chunk_id,
        "documentId": item.document_id,
        "canonicalKey": item.canonical_key,
        "sourceKey": item.source_key,
        "sectionTitleHash": hashlib.sha256((item.section_title or "").encode("utf-8")).hexdigest()
        if item.section_title
        else None,
        "bm25Rank": score.bm25_rank if score else None,
        "vectorRank": score.vector_rank if score else None,
        "rrf": score.reciprocal_rank_score if score else 0.0,
        "rerank": score.rerank_relevance if score else item.score,
        "entityMatch": score.entity_match if score else 0.0,
        "facetMatch": score.facet_match if score else 0.0,
        "scopeMatch": score.scope_match if score else 0.0,
        "seedChunkId": provenance.seed_chunk_id if provenance else item.chunk_id,
    }


def _select_parent_contexts(
    ranked: list[SearchResult],
    parent_by_chunk: dict[int, int | None],
    parent_content: dict[int, str],
    query: str,
    limit: int,
) -> set[tuple[Hashable, int]]:
    grouped: dict[Hashable, list[tuple[int, int]]] = {}
    for rank, item in enumerate(ranked):
        if item.chunk_id is None:
            continue
        parent_id = parent_by_chunk.get(int(item.chunk_id))
        if parent_id is None:
            continue
        document_key: Hashable = item.document_id if item.document_id is not None else item.source
        values = grouped.setdefault(document_key, [])
        if all(existing_parent != int(parent_id) for _rank, existing_parent in values):
            values.append((rank, int(parent_id)))
    tokenizer = KnowledgeTokenizer()
    query_tokens = set(tokenizer.tokenize(query))
    selected: set[tuple[Hashable, int]] = set()
    for document_key, candidates in grouped.items():
        remaining = list(candidates)
        covered: set[str] = set()
        while remaining and len([key for key in selected if key[0] == document_key]) < limit:
            if not covered:
                chosen = remaining[0]
            else:
                chosen = max(
                    remaining,
                    key=lambda value: (
                        len((set(tokenizer.tokenize(parent_content.get(value[1], ""))) & query_tokens) - covered),
                        -value[0],
                    ),
                )
            remaining.remove(chosen)
            selected.add((document_key, chosen[1]))
            covered.update(set(tokenizer.tokenize(parent_content.get(chosen[1], ""))) & query_tokens)
    return selected


def _first_json_id(raw: str) -> int | None:
    try:
        values = json.loads(raw or "[]")
        return int(values[0]) if isinstance(values, list) and values else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _elapsed_stage_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def chunk_text(content: str, size: int, overlap: int) -> list[str]:
    """Legacy character chunking retained only for compatibility replay and rollback."""
    text = re.sub(r"\s+", " ", content or "").strip()
    if not text:
        return []
    chunks = []
    start = 0
    step = max(1, size - overlap)
    while start < len(text):
        chunks.append(text[start:start + size])
        start += step
    return chunks


def _merge_passage(existing: str, incoming: str) -> str:
    incoming = incoming.strip()
    if not existing:
        return incoming
    if not incoming or incoming in existing:
        return existing
    maximum = min(200, len(existing), len(incoming))
    for length in range(maximum, 7, -1):
        if existing[-length:] == incoming[:length]:
            return existing + incoming[length:]
    sentences = [item.strip() for item in re.split(r"(?<=[。！？；])", incoming) if item.strip()]
    unique = [sentence for sentence in sentences if sentence not in existing]
    return f"{existing}\n\n{''.join(unique) if unique else incoming}".strip()


def compact_text(text: str) -> str:
    return re.sub(r"\s+", "", text.lower())


def result_key(result: SearchResult) -> Hashable:
    return result.chunk_id if result.chunk_id is not None else (result.source, result.content)


def _json_list(raw: str) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def extract_pdf(data: bytes) -> str:
    from io import BytesIO

    reader = PdfReader(BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)
