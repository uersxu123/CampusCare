from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import SessionLocal
from app.models.entities import (
    KnowledgeArtifact,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeElement,
    KnowledgeIndexRegistry,
    KnowledgeIngestionJob,
    KnowledgeTable,
)
from app.services.knowledge_ingestion.artifact_store import LocalArtifactStore
from app.services.knowledge_ingestion.models import ParserProfile
from app.services.knowledge_ingestion.parser_registry import ParserRegistry
from app.services.knowledge_ingestion.pipeline import KnowledgeIngestionPipeline


logger = logging.getLogger(__name__)


class KnowledgeAdminService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        root = Path(settings.knowledge_artifact_dir)
        if not root.is_absolute():
            root = settings.project_root / root
        self.artifact_store = LocalArtifactStore(root, max_bytes=settings.knowledge_upload_max_bytes)

    def enqueue_document(
        self,
        *,
        filename: str,
        data: bytes,
        mime_type: str,
        title: str,
        canonical_key: str,
        domain: str,
        tags: tuple[str, ...],
        site: str,
        version: str,
        parser_profile: str,
        chunking_profile: str,
        verified_at: datetime | None,
        expires_at: datetime | None,
    ) -> KnowledgeIngestionJob:
        ParserRegistry.default().resolve(mime_type, filename)
        canonical = canonical_key.strip() or Path(filename).stem
        if not canonical or len(canonical) > 160:
            raise ValueError("canonical_key_invalid")
        if chunking_profile not in {"structure_token_v2", "legacy_char_v1"}:
            raise ValueError("unsupported_chunking_profile")
        artifact = self.artifact_store.put(filename, data, mime_type)
        source_key = _source_key(canonical, version)
        document = self.db.query(KnowledgeDocument).filter_by(source_key=source_key).one_or_none()
        if document is None:
            document = KnowledgeDocument(
                source_key=source_key,
                canonical_key=canonical,
                managed_by="UPLOAD",
                title=title.strip() or filename,
                source_type="UPLOAD",
                domain=domain,
                tags_json=json.dumps(tags, ensure_ascii=False),
                site=site,
                status="DRAFT",
                verified_at=verified_at,
                expires_at=expires_at,
                version=version,
                content_hash=artifact.sha256,
                ingestion_status="PENDING",
                parser_profile=parser_profile,
                chunking_profile=chunking_profile,
                parser_version="pending",
                active_revision=1,
            )
            self.db.add(document)
            self.db.flush()
        elif document.status != "ACTIVE":
            document.ingestion_status = "PENDING"
            document.last_ingestion_error = ""
        request = {
            "artifact": {
                "storageKey": artifact.storage_key,
                "filename": filename,
                "mimeType": mime_type,
                "sha256": artifact.sha256,
            },
            "document": {
                "sourceKey": source_key,
                "title": title.strip() or filename,
                "canonicalKey": canonical,
                "domain": domain,
                "tags": list(tags),
                "site": site,
                "version": version,
                "parserProfile": parser_profile,
                "chunkingProfile": chunking_profile,
                "verifiedAt": _iso(verified_at),
                "expiresAt": _iso(expires_at),
            },
        }
        job = self._new_job(document.id, "UPLOAD", request)
        self.db.commit()
        return job

    def enqueue_reprocess(
        self,
        document_id: int,
        *,
        parser_profile: str,
        chunking_profile: str,
    ) -> KnowledgeIngestionJob:
        document = self._document(document_id)
        artifact = (
            self.db.query(KnowledgeArtifact)
            .filter_by(document_id=document.id)
            .order_by(KnowledgeArtifact.created_at.desc(), KnowledgeArtifact.id.desc())
            .first()
        )
        if artifact is None:
            raise ValueError("knowledge_artifact_not_found")
        if chunking_profile not in {"structure_token_v2", "legacy_char_v1"}:
            raise ValueError("unsupported_chunking_profile")
        request = {
            "artifact": {
                "storageKey": artifact.storage_key,
                "filename": artifact.original_filename,
                "mimeType": artifact.mime_type,
                "sha256": artifact.sha256,
            },
            "document": {
                "sourceKey": document.source_key,
                "title": document.title,
                "canonicalKey": document.canonical_key or document.source_key,
                "domain": document.domain,
                "tags": _json_list(document.tags_json),
                "site": document.site,
                "version": document.version,
                "parserProfile": parser_profile,
                "chunkingProfile": chunking_profile,
                "verifiedAt": _iso(document.verified_at),
                "expiresAt": _iso(document.expires_at),
            },
        }
        job = self._new_job(document.id, "REPROCESS", request)
        self.db.commit()
        return job

    def run_job(self, job_id: int) -> KnowledgeIngestionJob:
        job = (
            self.db.query(KnowledgeIngestionJob)
            .filter(KnowledgeIngestionJob.id == job_id)
            .with_for_update()
            .one_or_none()
        )
        if job is None:
            raise ValueError("knowledge_ingestion_job_not_found")
        if job.status != "PENDING":
            return job
        if job.attempts >= job.max_attempts:
            raise ValueError("knowledge_ingestion_job_attempts_exhausted")
        job.status = "RUNNING"
        job.attempts += 1
        job.started_at = _now()
        job.updated_at = _now()
        self.db.commit()
        try:
            request = json.loads(job.request_json)
            artifact = request["artifact"]
            metadata = request["document"]
            data = self.artifact_store.read(str(artifact["storageKey"]))
            if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
                raise ValueError("artifact_hash_mismatch")
            document = self._document(job.document_id)
            result = KnowledgeIngestionPipeline(self.db, self.settings).ingest_document(
                filename=str(artifact["filename"]),
                data=data,
                mime_type=str(artifact["mimeType"]),
                title=str(metadata["title"]),
                source_key=str(metadata["sourceKey"]),
                canonical_key=str(metadata["canonicalKey"]),
                domain=str(metadata["domain"]),
                tags=tuple(str(item) for item in metadata.get("tags", [])),
                site=str(metadata["site"]),
                version=str(metadata["version"]),
                parser_profile=ParserProfile(name=str(metadata["parserProfile"])),
                chunking_profile=str(metadata["chunkingProfile"]),
                status=document.status,
                verified_at=_datetime(metadata.get("verifiedAt")),
                expires_at=_datetime(metadata.get("expiresAt")),
            )
            job = self.db.get(KnowledgeIngestionJob, job_id)
            job.document_id = result.document_id
            job.status = "SUCCEEDED"
            job.result_json = json.dumps(
                {"documentId": result.document_id, "chunks": result.chunk_count, "warnings": list(result.warnings)},
                ensure_ascii=False,
            )
            job.error_code = ""
            job.error_message = ""
            job.finished_at = _now()
            job.updated_at = _now()
            self.db.commit()
            return job
        except Exception as exc:
            self.db.rollback()
            job = self.db.get(KnowledgeIngestionJob, job_id)
            job.status = "FAILED"
            job.error_code, job.error_message = _safe_failure(exc)
            job.finished_at = _now()
            job.updated_at = _now()
            self.db.commit()
            logger.warning("knowledge ingestion job failed: job_id=%s error_code=%s", job.id, job.error_code)
            return job

    def retry_job(self, job_id: int) -> KnowledgeIngestionJob:
        job = self.db.get(KnowledgeIngestionJob, job_id)
        if job is None:
            raise ValueError("knowledge_ingestion_job_not_found")
        if job.status != "FAILED" or job.attempts >= job.max_attempts:
            raise ValueError("knowledge_ingestion_job_not_retryable")
        job.status = "PENDING"
        job.error_code = ""
        job.error_message = ""
        job.run_after = _now()
        job.finished_at = None
        job.updated_at = _now()
        self.db.commit()
        return job

    def get_job(self, job_id: int) -> dict:
        job = self.db.get(KnowledgeIngestionJob, job_id)
        if job is None:
            raise ValueError("knowledge_ingestion_job_not_found")
        return _job_payload(job)

    def list_documents(self) -> list[dict]:
        rows = self.db.query(KnowledgeDocument).order_by(KnowledgeDocument.updated_at.desc(), KnowledgeDocument.id.desc()).all()
        return [self._document_payload(item) for item in rows]

    def get_document(self, document_id: int) -> dict:
        return self._document_payload(self._document(document_id), include_error=True)

    def preview_document(self, document_id: int) -> dict:
        document = self._document(document_id)
        elements = (
            self.db.query(KnowledgeElement)
            .filter_by(document_id=document.id)
            .order_by(KnowledgeElement.element_index.asc())
            .all()
        )
        chunks = (
            self.db.query(KnowledgeChunk)
            .filter_by(document_id=document.id)
            .order_by(KnowledgeChunk.source_index.asc())
            .all()
        )
        lengths = sorted(len(item.content) for item in chunks)
        page_count = len({item.page_number for item in elements if item.page_number is not None})
        table_count = self.db.query(KnowledgeTable).filter_by(document_id=document.id).count()
        return {
            "document": self._document_payload(document, include_error=True),
            "parser": {"profile": document.parser_profile, "version": document.parser_version},
            "chunkingProfile": document.chunking_profile,
            "quality": _json_dict(document.ingestion_quality_json),
            "warnings": _json_list(document.ingestion_warnings_json),
            "counts": {"elements": len(elements), "pages": page_count, "tables": table_count, "chunks": len(chunks)},
            "chunkLengths": _length_distribution(lengths),
            "elements": [
                {
                    "id": item.id,
                    "index": item.element_index,
                    "type": item.element_type,
                    "pageNumber": item.page_number,
                    "headingPath": _json_list(item.heading_path_json),
                    "content": item.content,
                }
                for item in elements
            ],
            "chunks": [
                {
                    "id": item.id,
                    "index": item.source_index,
                    "type": item.chunk_kind,
                    "pageNumber": item.page_number,
                    "headingPath": _json_list(item.heading_path_json),
                    "parentChunkId": item.parent_chunk_id,
                    "tokenCount": item.token_count,
                    "content": item.content,
                }
                for item in chunks
            ],
            "index": self._index_status(),
        }

    def update_metadata(
        self,
        document_id: int,
        *,
        title: str,
        domain: str,
        tags: tuple[str, ...],
        site: str,
        version: str,
        verified_at: datetime | None,
        expires_at: datetime | None,
    ) -> dict:
        document = self._document(document_id)
        document.title = title
        document.domain = domain
        document.tags_json = json.dumps(tags, ensure_ascii=False)
        document.site = site
        document.version = version
        document.verified_at = verified_at
        document.expires_at = expires_at
        self.db.commit()
        return self._document_payload(document, include_error=True)

    def publish(self, document_id: int, *, verified_at: datetime | None) -> dict:
        document = self._document(document_id)
        if document.ingestion_status != "READY":
            raise ValueError("knowledge_document_not_ready")
        effective_verified_at = verified_at or document.verified_at
        if effective_verified_at is None:
            raise ValueError("knowledge_document_verified_at_required")
        document.verified_at = effective_verified_at
        document.status = "ACTIVE"
        self.db.commit()
        return self._document_payload(document, include_error=True)

    def deactivate(self, document_id: int) -> dict:
        document = self._document(document_id)
        document.status = "INACTIVE"
        self.db.commit()
        return self._document_payload(document, include_error=True)

    def _new_job(self, document_id: int, job_type: str, request: dict) -> KnowledgeIngestionJob:
        active = (
            self.db.query(KnowledgeIngestionJob)
            .filter(
                KnowledgeIngestionJob.document_id == document_id,
                KnowledgeIngestionJob.status.in_(["PENDING", "RUNNING"]),
            )
            .first()
        )
        if active is not None:
            raise ValueError("knowledge_ingestion_job_already_active")
        job = KnowledgeIngestionJob(
            document_id=document_id,
            job_type=job_type,
            status="PENDING",
            request_json=json.dumps(request, ensure_ascii=False, sort_keys=True),
            max_attempts=self.settings.knowledge_ingestion_max_attempts,
            run_after=_now(),
        )
        self.db.add(job)
        self.db.flush()
        return job

    def _document(self, document_id: int | None) -> KnowledgeDocument:
        document = self.db.get(KnowledgeDocument, document_id) if document_id is not None else None
        if document is None:
            raise ValueError("knowledge_document_not_found")
        return document

    def _document_payload(self, document: KnowledgeDocument, *, include_error: bool = False) -> dict:
        payload = {
            "id": document.id,
            "sourceKey": document.source_key,
            "canonicalKey": document.canonical_key,
            "title": document.title,
            "domain": document.domain,
            "tags": _json_list(document.tags_json),
            "site": document.site,
            "version": document.version,
            "status": document.status,
            "ingestionStatus": document.ingestion_status,
            "parserProfile": document.parser_profile,
            "parserVersion": document.parser_version,
            "chunkingProfile": document.chunking_profile,
            "activeRevision": document.active_revision,
            "verifiedAt": _iso(document.verified_at),
            "expiresAt": _iso(document.expires_at),
            "updatedAt": _iso(document.updated_at),
            "chunkCount": self.db.query(KnowledgeChunk).filter_by(document_id=document.id).count(),
        }
        if include_error:
            payload["failure"] = (
                {"code": "ingestion_failed", "message": "处理失败，请检查文件格式或调整解析配置后重试。"}
                if document.last_ingestion_error
                else None
            )
        return payload

    def _index_status(self) -> dict:
        row = (
            self.db.query(KnowledgeIndexRegistry)
            .filter_by(logical_name=self.settings.knowledge_vector_collection_base)
            .one_or_none()
        )
        if row is None:
            return {"state": "EMPTY", "activeCollection": None, "readyCollection": None}
        return {
            "state": row.state,
            "activeCollection": row.active_collection,
            "readyCollection": row.ready_collection,
            "previousCollection": row.previous_collection,
        }


class KnowledgeIngestionWorker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.settings.knowledge_ingestion_worker_enabled or (self.thread is not None and self.thread.is_alive()):
            return
        self.stop_event.clear()
        self._recover_running_jobs()
        self.thread = threading.Thread(target=self._loop, name="mindbridge-knowledge-ingestion", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._dispatch_once()
            except Exception as exc:
                logger.warning("knowledge ingestion worker dispatch failed: error_type=%s", type(exc).__name__)
            self.stop_event.wait(self.settings.knowledge_ingestion_poll_interval_seconds)

    def _dispatch_once(self) -> None:
        db = SessionLocal()
        try:
            job_ids = [
                item.id
                for item in db.query(KnowledgeIngestionJob)
                .filter(KnowledgeIngestionJob.status == "PENDING", KnowledgeIngestionJob.run_after <= _now())
                .order_by(KnowledgeIngestionJob.created_at.asc())
                .limit(self.settings.knowledge_ingestion_batch_size)
                .all()
            ]
            service = KnowledgeAdminService(db, self.settings)
            for job_id in job_ids:
                service.run_job(job_id)
        finally:
            db.close()

    def _recover_running_jobs(self) -> None:
        db = SessionLocal()
        try:
            for job in db.query(KnowledgeIngestionJob).filter_by(status="RUNNING").all():
                job.status = "PENDING"
                job.error_code = "worker_restarted"
                job.error_message = "服务重启后任务已恢复，等待重新执行。"
                job.run_after = _now()
            db.commit()
        finally:
            db.close()


_worker: KnowledgeIngestionWorker | None = None


def get_knowledge_ingestion_worker(settings: Settings) -> KnowledgeIngestionWorker:
    global _worker
    if _worker is None:
        _worker = KnowledgeIngestionWorker(settings)
    return _worker


def _source_key(canonical_key: str, version: str) -> str:
    digest = hashlib.sha256(f"{canonical_key}\n{version}".encode("utf-8")).hexdigest()[:24]
    return f"upload:{digest}"


def _job_payload(job: KnowledgeIngestionJob) -> dict:
    return {
        "id": job.id,
        "documentId": job.document_id,
        "type": job.job_type,
        "status": job.status,
        "attempts": job.attempts,
        "maxAttempts": job.max_attempts,
        "result": _json_dict(job.result_json),
        "failure": {"code": job.error_code, "message": job.error_message} if job.error_code else None,
        "createdAt": _iso(job.created_at),
        "startedAt": _iso(job.started_at),
        "finishedAt": _iso(job.finished_at),
    }


def _length_distribution(values: list[int]) -> dict:
    if not values:
        return {"min": 0, "max": 0, "p50": 0, "p95": 0}
    return {
        "min": values[0],
        "max": values[-1],
        "p50": values[min(len(values) - 1, int((len(values) - 1) * 0.50))],
        "p95": values[min(len(values) - 1, int((len(values) - 1) * 0.95))],
    }


def _json_list(raw: str) -> list:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def _json_dict(raw: str) -> dict:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _safe_failure(exc: Exception) -> tuple[str, str]:
    value = str(exc)
    if "encrypted" in value.lower():
        return "encrypted_pdf", "PDF 已加密，请提供未加密文件后重试。"
    if value.startswith("unsupported_document_type"):
        return "unsupported_document_type", "不支持该文件类型，请上传 PDF、Markdown 或纯文本文件。"
    if value == "artifact_not_found":
        return "artifact_not_found", "上传原件不可用，请重新上传文件。"
    if value == "artifact_hash_mismatch":
        return "artifact_hash_mismatch", "上传原件校验失败，请重新上传文件。"
    return "ingestion_failed", "处理失败，请检查文件格式或调整解析配置后重试。"


def _datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
