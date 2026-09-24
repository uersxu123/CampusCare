from __future__ import annotations

import hashlib
import json
import mimetypes
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.entities import KnowledgeArtifact, KnowledgeChunk, KnowledgeDocument, KnowledgeElement, KnowledgeTable
from app.services.knowledge import chunk_text, extract_pdf
from app.services.knowledge_ingestion.artifact_store import LocalArtifactStore
from app.services.knowledge_ingestion.catalog import transition_ingestion
from app.services.knowledge_ingestion.chunker import ChunkPlan, ChunkProfile, UnicodeLexicalTokenCounter, chunk_document
from app.services.knowledge_ingestion.models import DocumentElement, IngestionResult, LegacyChunk, ParsedDocument, ParserProfile, StoredArtifact
from app.services.knowledge_ingestion.parser_registry import ParserRegistry
from app.services.knowledge_ingestion.quality import enforce_quality
from app.services.knowledge_ingestion.tables import normalize_table, table_row_text, table_summary_text


class KnowledgeIngestionPipeline:
    def __init__(
        self,
        db: Session,
        settings: Settings,
        artifact_store: LocalArtifactStore | None = None,
        parser_registry: ParserRegistry | None = None,
    ):
        self.db = db
        self.settings = settings
        artifact_root = Path(getattr(settings, "knowledge_artifact_dir", "data/knowledge-artifacts"))
        if not artifact_root.is_absolute():
            artifact_root = settings.project_root / artifact_root
        self.artifact_store = artifact_store or LocalArtifactStore(
            artifact_root,
            max_bytes=getattr(settings, "knowledge_upload_max_bytes", 20 * 1024 * 1024),
        )
        self.parser_registry = parser_registry or ParserRegistry.default()

    def ingest_document(
        self,
        *,
        filename: str,
        data: bytes,
        mime_type: str,
        title: str | None = None,
        source_key: str | None = None,
        canonical_key: str | None = None,
        domain: str = "MENTAL_HEALTH",
        tags: tuple[str, ...] = (),
        site: str = "ALL",
        version: str = "1",
        parser_profile: ParserProfile | None = None,
        chunking_profile: str | None = None,
        status: str = "DRAFT",
        verified_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> IngestionResult:
        profile = parser_profile or ParserProfile()
        selected_chunking_profile = chunking_profile or getattr(
            self.settings,
            "knowledge_chunking_profile",
            "structure_token_v2",
        )
        source = source_key or filename
        artifact = self.artifact_store.put(filename, data, mime_type)
        parser = self.parser_registry.resolve(mime_type, filename)
        try:
            parsed = parser.parse(artifact, profile)
            enforce_quality(parsed)
            document = self.db.query(KnowledgeDocument).filter(KnowledgeDocument.source_key == source).one_or_none()
            was_existing = document is not None
            previous_parser_profile = document.parser_profile if document is not None else None
            previous_chunking_profile = document.chunking_profile if document is not None else None
            metadata = {
                "canonical_key": canonical_key or source,
                "managed_by": "UPLOAD",
                "title": title or parsed.title or filename,
                "source_url": None,
                "source_type": "UPLOAD",
                "domain": domain,
                "tags_json": json.dumps(list(tags), ensure_ascii=False),
                "site": site,
                "status": status,
                "verified_at": verified_at,
                "expires_at": expires_at,
                "version": version,
            }
            if status == "ACTIVE" and verified_at is None:
                raise ValueError(f"ACTIVE 文档缺少 verified_at：{source}")
            element_digest = hashlib.sha256(
                "\n".join(
                    f"{item.element_type}|{item.page_number}|{'/'.join(item.heading_path)}|{item.content}"
                    for item in parsed.elements
                ).encode("utf-8")
            ).hexdigest()
            content_changed = document is None or document.content_hash != element_digest
            if document is None:
                document = KnowledgeDocument(
                    source_key=source,
                    content_hash=element_digest,
                    ingestion_status="PENDING",
                    parser_profile=profile.name,
                    chunking_profile=selected_chunking_profile,
                    parser_version=parsed.parser_version,
                    last_ingestion_error="",
                    ingestion_warnings_json=json.dumps(parsed.warnings, ensure_ascii=False),
                    ingestion_quality_json=json.dumps(parsed.quality, ensure_ascii=False, sort_keys=True),
                    active_revision=1,
                    **metadata,
                )
                self.db.add(document)
                self.db.flush()
                metadata_changed = True
            else:
                metadata_changed = any(getattr(document, key) != value for key, value in metadata.items())
                for key, value in metadata.items():
                    setattr(document, key, value)
                document.parser_profile = profile.name
                document.parser_version = parsed.parser_version
                document.chunking_profile = selected_chunking_profile
                document.last_ingestion_error = ""
                document.ingestion_warnings_json = json.dumps(parsed.warnings, ensure_ascii=False)
                document.ingestion_quality_json = json.dumps(parsed.quality, ensure_ascii=False, sort_keys=True)
            transition_ingestion(document, "PARSING")
            artifact_row = self._record_artifact(document, artifact)
            element_rows = self._persist_elements(document, artifact_row, parsed)
            transition_ingestion(document, "PARSED")
            if selected_chunking_profile == "structure_token_v2":
                plan = chunk_document(parsed, ChunkProfile.from_settings(self.settings), UnicodeLexicalTokenCounter())
                table_chunk_count = self._replace_structured_chunks(document, plan, element_rows)
                chunk_count = len(plan.children) + table_chunk_count
            elif selected_chunking_profile == "legacy_char_v1":
                legacy_text = "\n\n".join(item.content for item in parsed.elements if item.content.strip())
                legacy_chunks = [
                    LegacyChunk(content=value)
                    for value in chunk_text(
                        legacy_text,
                        self.settings.knowledge_chunk_size,
                        self.settings.knowledge_chunk_overlap,
                    )
                ]
                self._replace_chunks(document, legacy_chunks)
                chunk_count = len(legacy_chunks)
            else:
                raise ValueError(f"unsupported_chunking_profile:{selected_chunking_profile}")
            document.content_hash = element_digest
            if was_existing and (
                content_changed
                or previous_parser_profile != profile.name
                or previous_chunking_profile != selected_chunking_profile
            ):
                document.active_revision = max(1, int(document.active_revision or 1)) + 1
            transition_ingestion(document, "CHUNKED")
            transition_ingestion(document, "READY")
            self.db.flush()
            result = IngestionResult(
                source=source,
                document_id=int(document.id),
                chunk_count=chunk_count,
                status=document.status,
                ingestion_status=document.ingestion_status,
                content_changed=content_changed,
                metadata_changed=metadata_changed,
                warnings=parsed.warnings,
            )
            self.db.commit()
            return result
        except Exception as exc:
            self.db.rollback()
            self._record_failure(
                source=source,
                filename=filename,
                title=title or filename,
                artifact=artifact,
                parser_profile=profile.name,
                parser_version=getattr(parser, "version", "unknown"),
                error=exc,
                domain=domain,
                site=site,
                version=version,
            )
            raise

    def ingest_legacy_text(
        self,
        *,
        source: str,
        content: str,
        title: str | None = None,
        source_type: str = "UPLOAD",
        domain: str = "MENTAL_HEALTH",
        status: str = "DRAFT",
        verified_at: datetime | None = None,
        managed_by: str | None = None,
    ) -> IngestionResult:
        data = content.encode("utf-8")
        artifact = self.artifact_store.put(source, data, "text/plain; charset=utf-8")
        values = [LegacyChunk(content=item) for item in chunk_text(
            content,
            self.settings.knowledge_chunk_size,
            self.settings.knowledge_chunk_overlap,
        )]
        digest = hashlib.sha256("\n".join(item.content for item in values).encode("utf-8")).hexdigest()
        try:
            return self._ingest_legacy_chunks(
                source=source,
                chunks=values,
                content_hash=digest,
                artifact=artifact,
                metadata={
                    "canonical_key": source,
                    "managed_by": managed_by or ("UPLOAD" if source_type == "UPLOAD" else "LEGACY"),
                    "title": title or source,
                    "source_url": None,
                    "source_type": source_type,
                    "domain": domain,
                    "tags_json": "[]",
                    "site": "ALL",
                    "status": status,
                    "verified_at": verified_at,
                    "expires_at": None,
                    "version": "1",
                },
                commit=True,
            )
        except Exception:
            self.db.rollback()
            if artifact.created:
                artifact.path.unlink(missing_ok=True)
            raise

    def rechunk_document(self, document_id: int, profile: ChunkProfile | None = None) -> IngestionResult:
        document = self.db.get(KnowledgeDocument, document_id)
        if document is None:
            raise ValueError(f"knowledge_document_not_found:{document_id}")
        chunk_profile = profile or ChunkProfile.from_settings(self.settings)
        try:
            element_rows = {
                item.element_index: item
                for item in self.db.query(KnowledgeElement)
                .filter(KnowledgeElement.document_id == document.id)
                .order_by(KnowledgeElement.element_index.asc())
                .all()
            }
            if element_rows:
                elements = tuple(
                    DocumentElement(
                        element_index=item.element_index,
                        element_type=item.element_type,
                        content=item.content,
                        page_number=item.page_number,
                        heading_path=tuple(json.loads(item.heading_path_json or "[]")),
                        metadata=json.loads(item.metadata_json or "{}"),
                    )
                    for item in element_rows.values()
                )
            else:
                legacy_chunks = (
                    self.db.query(KnowledgeChunk)
                    .filter(KnowledgeChunk.document_id == document.id)
                    .filter(or_chunk_is_retrievable())
                    .order_by(KnowledgeChunk.source_index.asc())
                    .all()
                )
                elements = tuple(
                    DocumentElement(
                        element_index=index,
                        element_type="PARAGRAPH",
                        content=item.content,
                        page_number=item.page_number,
                        heading_path=(item.section_title,) if item.section_title else (),
                        metadata={"legacy_chunk_id": item.id},
                    )
                    for index, item in enumerate(legacy_chunks)
                )
                parsed_legacy = ParsedDocument(
                    title=document.title,
                    elements=elements,
                    parser_name="legacy-ir-adapter",
                    parser_version="1.0",
                    warnings=("legacy_chunk_ir_adapter",),
                    quality={"visible_characters": float(sum(len(item.content) for item in elements))},
                )
                artifact = document.artifacts[-1] if document.artifacts else None
                if artifact is None:
                    artifact = KnowledgeArtifact(
                        document_id=document.id,
                        storage_key=f"legacy/{document.source_key}",
                        original_filename=document.source_key,
                        mime_type="text/plain",
                        byte_size=sum(len(item.content.encode("utf-8")) for item in elements),
                        sha256=document.content_hash,
                    )
                    self.db.add(artifact)
                    self.db.flush()
                element_rows = self._persist_elements(document, artifact, parsed_legacy)
            parsed = ParsedDocument(
                title=document.title,
                elements=elements,
                parser_name=document.parser_profile,
                parser_version=document.parser_version,
                warnings=tuple(json.loads(document.ingestion_warnings_json or "[]")),
                quality=json.loads(document.ingestion_quality_json or "{}"),
            )
            transition_ingestion(document, "PARSING")
            transition_ingestion(document, "PARSED")
            plan = chunk_document(parsed, chunk_profile, UnicodeLexicalTokenCounter())
            self._replace_structured_chunks(document, plan, element_rows)
            document.chunking_profile = chunk_profile.name
            document.active_revision = max(1, int(document.active_revision or 1)) + 1
            transition_ingestion(document, "CHUNKED")
            transition_ingestion(document, "READY")
            self.db.commit()
            return IngestionResult(
                source=document.source_key,
                document_id=int(document.id),
                chunk_count=len(plan.children),
                status=document.status,
                ingestion_status=document.ingestion_status,
                content_changed=True,
                metadata_changed=False,
                warnings=parsed.warnings,
            )
        except Exception:
            self.db.rollback()
            raise

    def ingest_legacy_file(self, *, source: str, data: bytes) -> IngestionResult:
        mime_type = mimetypes.guess_type(source)[0] or "application/octet-stream"
        artifact = self.artifact_store.put(source, data, mime_type)
        text = extract_pdf(data) if source.lower().endswith(".pdf") else data.decode("utf-8", errors="ignore")
        values = [LegacyChunk(content=item) for item in chunk_text(
            text,
            self.settings.knowledge_chunk_size,
            self.settings.knowledge_chunk_overlap,
        )]
        digest = hashlib.sha256("\n".join(item.content for item in values).encode("utf-8")).hexdigest()
        try:
            return self._ingest_legacy_chunks(
                source=source,
                chunks=values,
                content_hash=digest,
                artifact=artifact,
                metadata={
                    "canonical_key": source,
                    "managed_by": "UPLOAD",
                    "title": source,
                    "source_url": None,
                    "source_type": "UPLOAD",
                    "domain": "MENTAL_HEALTH",
                    "tags_json": "[]",
                    "site": "ALL",
                    "status": "DRAFT",
                    "verified_at": None,
                    "expires_at": None,
                    "version": "1",
                },
                commit=True,
            )
        except Exception:
            self.db.rollback()
            if artifact.created:
                artifact.path.unlink(missing_ok=True)
            raise

    def ingest_manifest_legacy(
        self,
        *,
        spec: dict,
        chunks: list,
        path: Path,
        sha256: str,
    ) -> IngestionResult:
        values = [
            LegacyChunk(
                content=_normalize(item.content),
                section_title=item.section_title,
                page_number=item.page_number,
            )
            for item in chunks
        ]
        if not values:
            raise ValueError(f"知识文档未生成任何文本块：{spec['source_key']}")
        digest = hashlib.sha256(
            "\n".join(f"{item.section_title}|{item.page_number}|{item.content}" for item in values).encode("utf-8")
        ).hexdigest()
        artifact = StoredArtifact(
            storage_key=f"manifest/{path.relative_to(self.settings.project_root).as_posix()}",
            original_filename=path.name,
            mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            byte_size=path.stat().st_size,
            sha256=sha256.lower(),
            path=path,
        )
        return self._ingest_legacy_chunks(
            source=str(spec["source_key"]),
            chunks=values,
            content_hash=digest,
            artifact=artifact,
            metadata={
                "canonical_key": str(spec["canonical_key"]),
                "managed_by": "MANIFEST",
                "title": str(spec["title"]),
                "source_url": spec.get("source_url"),
                "source_type": str(spec["source_type"]),
                "domain": str(spec["domain"]),
                "tags_json": json.dumps(spec.get("tags", []), ensure_ascii=False),
                "site": str(spec.get("site", "ALL")),
                "status": str(spec["status"]).upper(),
                "verified_at": _parse_datetime(spec.get("verified_at")),
                "expires_at": _parse_datetime(spec.get("expires_at")),
                "version": str(spec["version"]),
            },
            commit=False,
        )

    def _ingest_legacy_chunks(
        self,
        *,
        source: str,
        chunks: list[LegacyChunk],
        content_hash: str,
        artifact: StoredArtifact,
        metadata: dict,
        commit: bool,
    ) -> IngestionResult:
        if metadata["status"] == "ACTIVE" and metadata["verified_at"] is None:
            raise ValueError(f"ACTIVE 文档缺少 verified_at：{source}")
        document = self.db.query(KnowledgeDocument).filter(KnowledgeDocument.source_key == source).one_or_none()
        content_changed = document is None or document.content_hash != content_hash
        if document is None:
            document = KnowledgeDocument(
                source_key=source,
                content_hash=content_hash,
                ingestion_status="PENDING",
                parser_profile="legacy",
                chunking_profile="legacy_char_v1",
                parser_version="legacy",
                last_ingestion_error="",
                active_revision=1,
                **metadata,
            )
            self.db.add(document)
            self.db.flush()
            metadata_changed = True
        else:
            metadata_changed = any(getattr(document, key) != value for key, value in metadata.items())
            for key, value in metadata.items():
                setattr(document, key, value)
            document.parser_profile = "legacy"
            document.chunking_profile = "legacy_char_v1"
            document.parser_version = "legacy"
            document.last_ingestion_error = ""
        transition_ingestion(document, "PARSING")
        self._record_artifact(document, artifact)
        transition_ingestion(document, "PARSED")
        if content_changed:
            self.db.query(KnowledgeTable).filter(
                KnowledgeTable.document_id == document.id
            ).delete(synchronize_session=False)
            self.db.query(KnowledgeElement).filter(
                KnowledgeElement.document_id == document.id
            ).delete(synchronize_session=False)
            self._replace_chunks(document, chunks)
            document.content_hash = content_hash
        transition_ingestion(document, "CHUNKED")
        transition_ingestion(document, "READY")
        self.db.flush()
        count = self.db.query(KnowledgeChunk).filter(KnowledgeChunk.document_id == document.id).count()
        result = IngestionResult(
            source=source,
            document_id=int(document.id),
            chunk_count=count,
            status=document.status,
            ingestion_status=document.ingestion_status,
            content_changed=content_changed,
            metadata_changed=metadata_changed,
        )
        if commit:
            self.db.commit()
        return result

    def _record_artifact(self, document: KnowledgeDocument, artifact: StoredArtifact) -> KnowledgeArtifact:
        row = (
            self.db.query(KnowledgeArtifact)
            .filter(
                KnowledgeArtifact.document_id == document.id,
                KnowledgeArtifact.sha256 == artifact.sha256,
                KnowledgeArtifact.original_filename == artifact.original_filename,
            )
            .one_or_none()
        )
        if row is None:
            row = KnowledgeArtifact(
                document_id=document.id,
                storage_key=artifact.storage_key,
                original_filename=artifact.original_filename,
                mime_type=artifact.mime_type,
                byte_size=artifact.byte_size,
                sha256=artifact.sha256,
            )
            self.db.add(row)
            self.db.flush()
        return row

    def _persist_elements(self, document: KnowledgeDocument, artifact: KnowledgeArtifact, parsed) -> dict[int, KnowledgeElement]:
        self.db.query(KnowledgeTable).filter(KnowledgeTable.document_id == document.id).delete()
        self.db.query(KnowledgeElement).filter(KnowledgeElement.document_id == document.id).delete()
        rows: dict[int, KnowledgeElement] = {}
        for item in parsed.elements:
            bbox = None if item.bbox is None else {
                "x0": item.bbox.x0,
                "y0": item.bbox.y0,
                "x1": item.bbox.x1,
                "y1": item.bbox.y1,
            }
            row = KnowledgeElement(
                document_id=document.id,
                artifact_id=artifact.id,
                element_index=item.element_index,
                element_type=item.element_type,
                page_number=item.page_number,
                bbox_json=json.dumps(bbox, ensure_ascii=False),
                heading_path_json=json.dumps(item.heading_path, ensure_ascii=False),
                content=item.content,
                content_hash=hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
                metadata_json=json.dumps(dict(item.metadata), ensure_ascii=False, sort_keys=True),
                parser_name=parsed.parser_name,
                parser_version=parsed.parser_version,
                confidence=_optional_float(item.metadata.get("confidence")),
            )
            self.db.add(row)
            rows[item.element_index] = row
        self.db.flush()
        for item in parsed.elements:
            if item.parent_index is not None and item.parent_index in rows:
                rows[item.element_index].parent_element_id = rows[item.parent_index].id
            if item.element_type == "TABLE":
                normalized = normalize_table(item)
                payload = json.dumps(
                    {"headers": normalized.headers, "rows": normalized.rows, "schema": normalized.schema},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                self.db.add(
                    KnowledgeTable(
                        document_id=document.id,
                        element_id=rows[item.element_index].id,
                        page_number=item.page_number,
                        caption=normalized.caption,
                        headers_json=json.dumps(normalized.headers, ensure_ascii=False),
                        rows_json=json.dumps(normalized.rows, ensure_ascii=False),
                        table_html=normalized.table_html,
                        schema_json=json.dumps(normalized.schema, ensure_ascii=False, sort_keys=True),
                        content_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                    )
                )
        self.db.flush()
        return rows

    def _replace_structured_chunks(
        self,
        document: KnowledgeDocument,
        plan: ChunkPlan,
        elements: dict[int, KnowledgeElement],
    ) -> int:
        chunk_query = self.db.query(KnowledgeChunk).filter(KnowledgeChunk.document_id == document.id)
        chunk_query.filter(KnowledgeChunk.parent_chunk_id.is_not(None)).delete(synchronize_session="fetch")
        chunk_query.delete(synchronize_session="fetch")
        parent_rows: list[KnowledgeChunk] = []
        source_index = 0
        for item in plan.parents:
            row = self._structured_chunk_row(
                document,
                item,
                elements,
                source_index=source_index,
                chunk_kind="TEXT_PARENT",
                parent_chunk_id=None,
                profile=plan.profile_name,
            )
            self.db.add(row)
            parent_rows.append(row)
            source_index += 1
        self.db.flush()
        for item in plan.children:
            parent_id = parent_rows[item.parent_index].id if item.parent_index is not None else None
            self.db.add(
                self._structured_chunk_row(
                    document,
                    item,
                    elements,
                    source_index=source_index,
                    chunk_kind="TEXT_CHILD",
                    parent_chunk_id=parent_id,
                    profile=plan.profile_name,
                )
            )
            source_index += 1
        self.db.flush()
        table_chunk_count = 0
        counter = UnicodeLexicalTokenCounter()
        tables = (
            self.db.query(KnowledgeTable)
            .filter(KnowledgeTable.document_id == document.id)
            .order_by(KnowledgeTable.id.asc())
            .all()
        )
        for table in tables:
            heading_path = json.loads(table.element.heading_path_json or "[]")
            summary = table_summary_text(table)
            self.db.add(
                KnowledgeChunk(
                    document_id=document.id,
                    source=document.source_key,
                    source_index=source_index,
                    section_title=table.caption,
                    page_number=table.page_number,
                    content=summary,
                    content_hash=hashlib.sha256(summary.encode("utf-8")).hexdigest(),
                    chunk_kind="TABLE_SUMMARY",
                    heading_path_json=json.dumps(heading_path, ensure_ascii=False),
                    element_ids_json=json.dumps([table.element_id]),
                    token_count=counter.count(summary),
                    chunking_profile=plan.profile_name,
                )
            )
            source_index += 1
            table_chunk_count += 1
            for row_index, row in enumerate(json.loads(table.rows_json)):
                content = table_row_text(table, row_index, row)
                self.db.add(
                    KnowledgeChunk(
                        document_id=document.id,
                        source=document.source_key,
                        source_index=source_index,
                        section_title=table.caption,
                        page_number=table.page_number,
                        content=content,
                        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                        chunk_kind="TABLE_ROW",
                        heading_path_json=json.dumps(heading_path, ensure_ascii=False),
                        element_ids_json=json.dumps([table.element_id]),
                        token_count=counter.count(content),
                        chunking_profile=plan.profile_name,
                    )
                )
                source_index += 1
                table_chunk_count += 1
        self.db.flush()
        return table_chunk_count

    @staticmethod
    def _structured_chunk_row(
        document: KnowledgeDocument,
        item,
        elements: dict[int, KnowledgeElement],
        *,
        source_index: int,
        chunk_kind: str,
        parent_chunk_id: int | None,
        profile: str,
    ) -> KnowledgeChunk:
        database_element_ids = [elements[value].id for value in item.element_ids if value in elements]
        heading_path = next((path for path in item.heading_paths if path), ())
        return KnowledgeChunk(
            document_id=document.id,
            source=document.source_key,
            source_index=source_index,
            section_title=heading_path[-1] if heading_path else None,
            page_number=item.page_numbers[0] if item.page_numbers else None,
            content=item.content,
            content_hash=hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
            chunk_kind=chunk_kind,
            parent_chunk_id=parent_chunk_id,
            heading_path_json=json.dumps(heading_path, ensure_ascii=False),
            element_ids_json=json.dumps(database_element_ids),
            token_count=item.token_count,
            chunking_profile=profile,
        )

    def _record_failure(
        self,
        *,
        source: str,
        filename: str,
        title: str,
        artifact: StoredArtifact,
        parser_profile: str,
        parser_version: str,
        error: Exception,
        domain: str,
        site: str,
        version: str,
    ) -> None:
        document = self.db.query(KnowledgeDocument).filter(KnowledgeDocument.source_key == source).one_or_none()
        if document is not None and document.status == "ACTIVE":
            document.last_ingestion_error = _safe_error(error)
            self.db.commit()
            return
        if document is None:
            document = KnowledgeDocument(
                source_key=source,
                canonical_key=source,
                managed_by="UPLOAD",
                title=title,
                source_type="UPLOAD",
                domain=domain,
                tags_json="[]",
                site=site,
                status="DRAFT",
                version=version,
                content_hash=artifact.sha256,
                ingestion_status="FAILED",
                parser_profile=parser_profile,
                chunking_profile="legacy_char_v1",
                parser_version=parser_version,
                last_ingestion_error=_safe_error(error),
                active_revision=1,
            )
            self.db.add(document)
            self.db.flush()
        else:
            document.ingestion_status = "FAILED"
            document.last_ingestion_error = _safe_error(error)
        self._record_artifact(document, artifact)
        self.db.commit()

    def _replace_chunks(self, document: KnowledgeDocument, chunks: list[LegacyChunk]) -> None:
        chunk_query = self.db.query(KnowledgeChunk).filter(KnowledgeChunk.document_id == document.id)
        chunk_query.filter(KnowledgeChunk.parent_chunk_id.is_not(None)).delete(synchronize_session="fetch")
        chunk_query.delete(synchronize_session="fetch")
        for index, item in enumerate(chunks):
            self.db.add(
                KnowledgeChunk(
                    document_id=document.id,
                    source=document.source_key,
                    source_index=index,
                    section_title=item.section_title,
                    page_number=item.page_number,
                    content=item.content,
                    content_hash=hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
                    chunk_kind="TEXT_CHILD",
                    heading_path_json="[]",
                    element_ids_json="[]",
                    chunking_profile="legacy_char_v1",
                )
            )
        self.db.flush()


def _normalize(text: str) -> str:
    return " ".join((text or "").split())


def _parse_datetime(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)


def _optional_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _safe_error(exc: Exception) -> str:
    value = str(exc)
    if "encrypted" in value.lower():
        code = "encrypted_pdf"
    elif value.startswith("unsupported_document_type"):
        code = "unsupported_document_type"
    elif value.startswith("unsupported_chunking_profile"):
        code = "unsupported_chunking_profile"
    else:
        code = "ingestion_failed"
    return f"{type(exc).__name__}:{code}"


def or_chunk_is_retrievable():
    from sqlalchemy import or_

    return or_(KnowledgeChunk.chunk_kind.is_(None), KnowledgeChunk.chunk_kind != "TEXT_PARENT")
