from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.entities import KnowledgeChunk, KnowledgeDocument, KnowledgeIndexRegistry


@dataclass(frozen=True)
class CorpusFingerprint:
    reference_source_type: str
    relational_database_type: str
    corpus_hash: str
    manifest_hash: str
    chunking_profiles: tuple[str, ...]
    active_collection: str | None
    index_signature: str | None
    document_count: int
    chunk_count: int
    generated_at: str

    def as_dict(self) -> dict:
        return asdict(self)


def fingerprint_active_corpus(db: Session, *, manifest_path: Path | None = None) -> CorpusFingerprint:
    documents = (
        db.query(KnowledgeDocument)
        .filter(KnowledgeDocument.status == "ACTIVE")
        .order_by(KnowledgeDocument.canonical_key, KnowledgeDocument.source_key)
        .all()
    )
    document_ids = [item.id for item in documents]
    document_keys = {item.id: item.canonical_key or item.source_key for item in documents}
    chunks = (
        db.query(KnowledgeChunk)
        .filter(KnowledgeChunk.document_id.in_(document_ids))
        .order_by(KnowledgeChunk.document_id, KnowledgeChunk.source_index)
        .all()
        if document_ids
        else []
    )
    registry = db.query(KnowledgeIndexRegistry).order_by(KnowledgeIndexRegistry.logical_name).first()
    payload = {
        "documents": [
            {
                "key": item.canonical_key or item.source_key,
                "sourceKey": item.source_key,
                "contentHash": item.content_hash,
                "version": item.version,
                "site": item.site,
                "verifiedAt": item.verified_at.isoformat() if item.verified_at else None,
                "expiresAt": item.expires_at.isoformat() if item.expires_at else None,
                "chunkingProfile": item.chunking_profile,
            }
            for item in documents
        ],
        "chunks": [
            {
                "documentKey": document_keys.get(item.document_id, ""),
                "sourceIndex": item.source_index,
                "contentHash": item.content_hash,
                "chunkKind": item.chunk_kind,
                "chunkingProfile": item.chunking_profile,
            }
            for item in sorted(
                chunks,
                key=lambda row: (document_keys.get(row.document_id, ""), row.source_index),
            )
        ],
    }
    dialect = db.get_bind().dialect.name
    return CorpusFingerprint(
        reference_source_type="mysql-active-corpus" if dialect == "mysql" else f"{dialect}-active-corpus",
        relational_database_type=dialect,
        corpus_hash=_hash_json(payload),
        manifest_hash=_hash_file(manifest_path),
        chunking_profiles=tuple(sorted({
            item.chunking_profile
            for item in chunks
            if item.chunking_profile
        } or {
            item.chunking_profile
            for item in documents
            if item.chunking_profile
        })),
        active_collection=registry.active_collection if registry else None,
        index_signature=registry.active_signature if registry else None,
        document_count=len(documents),
        chunk_count=len(chunks),
        generated_at=datetime.now(UTC).isoformat(),
    )


def fingerprint_from_audit(path: Path) -> CorpusFingerprint:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    source = payload.get("referenceSource") or {}
    return CorpusFingerprint(
        reference_source_type=str(source.get("type") or "harness-snapshot"),
        relational_database_type=str(source.get("relationalDatabaseType") or "sqlite"),
        corpus_hash=str(source.get("corpusHash") or payload.get("knowledgeDatabaseSha256") or ""),
        manifest_hash=str(source.get("manifestHash") or ""),
        chunking_profiles=tuple(source.get("chunkingProfiles") or ()),
        active_collection=source.get("activeCollection"),
        index_signature=source.get("indexSignature"),
        document_count=int(source.get("documentCount") or 0),
        chunk_count=int(source.get("chunkCount") or 0),
        generated_at=str(source.get("generatedAt") or payload.get("generatedAt") or ""),
    )


def compare_corpus_fingerprints(golden: CorpusFingerprint, runtime: CorpusFingerprint) -> dict:
    matching = bool(golden.corpus_hash) and golden.corpus_hash == runtime.corpus_hash
    if golden.manifest_hash and runtime.manifest_hash:
        matching = matching and golden.manifest_hash == runtime.manifest_hash
    if golden.chunking_profiles and runtime.chunking_profiles:
        matching = matching and golden.chunking_profiles == runtime.chunking_profiles
    registry_valid = runtime.relational_database_type != "mysql" or bool(
        runtime.active_collection and runtime.index_signature
    )
    matching = matching and registry_valid
    return {
        "status": "MATCH" if matching else "CORPUS_MISMATCH",
        "passed": matching,
        "runtimeRegistryValid": registry_valid,
        "goldenCorpusFingerprint": golden.as_dict(),
        "runtimeCorpusFingerprint": runtime.as_dict(),
    }


def _hash_json(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _hash_file(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()
