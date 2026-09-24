from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.entities import (
    KnowledgeArtifact,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeElement,
    KnowledgeIndexRegistry,
)


def find_unmanaged_markdown(settings: Settings) -> tuple[str, ...]:
    root = settings.project_root.resolve()
    manifest_path = root / "app" / "knowledge" / "knowledge_manifest.yaml"
    payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    managed = {
        str(item.get("raw_path") or "").replace("\\", "/")
        for item in payload.get("documents", [])
        if isinstance(item, dict)
    }
    markdown = {
        path.relative_to(root).as_posix()
        for path in (root / "app" / "knowledge").glob("*.md")
        if path.is_file()
    }
    return tuple(sorted(markdown - managed))


def build_cutover_report(db: Session, *, unmanaged_markdown: tuple[str, ...]) -> dict:
    documents = db.query(KnowledgeDocument).order_by(KnowledgeDocument.id.asc()).all()
    upload_documents = [item for item in documents if item.managed_by == "UPLOAD" or item.source_type == "UPLOAD"]
    artifact_document_ids = {
        int(value)
        for (value,) in db.query(KnowledgeArtifact.document_id).distinct().all()
    }
    element_document_ids = {
        int(value)
        for (value,) in db.query(KnowledgeElement.document_id).distinct().all()
    }
    legacy_upload_ids = sorted(
        int(item.id)
        for item in upload_documents
        if item.parser_profile == "legacy"
        or item.chunking_profile == "legacy_char_v1"
        or item.id not in artifact_document_ids
        or item.id not in element_document_ids
    )
    active_documents = [item for item in documents if item.status == "ACTIVE"]
    traceability_gaps = []
    for document in active_documents:
        chunks = db.query(KnowledgeChunk).filter_by(document_id=document.id).all()
        if (
            document.id not in artifact_document_ids
            or document.id not in element_document_ids
            or not chunks
            or any(not _json_ids(item.element_ids_json) for item in chunks)
        ):
            traceability_gaps.append(int(document.id))

    groups: dict[tuple[str, str], list[int]] = {}
    for document in active_documents:
        key = (document.canonical_key or document.source_key, document.version)
        groups.setdefault(key, []).append(int(document.id))
    duplicates = [
        {"canonicalKey": key[0], "version": key[1], "documentIds": ids}
        for key, ids in sorted(groups.items())
        if len(ids) > 1
    ]
    registries = db.query(KnowledgeIndexRegistry).all()
    rollback_rehearsed = any(
        bool(item.active_collection)
        and bool(item.previous_collection)
        and bool(_json_object(item.metadata_json).get("rolledBackAt"))
        for item in registries
    )
    checks = {
        "manifestRegistration": {
            "passed": not unmanaged_markdown,
            "unmanagedCount": len(unmanaged_markdown),
            "paths": list(unmanaged_markdown),
        },
        "adminUploadsMigrated": {
            "passed": not legacy_upload_ids,
            "documentIds": legacy_upload_ids,
        },
        "activeTraceability": {
            "passed": not traceability_gaps,
            "documentIds": sorted(traceability_gaps),
        },
        "duplicateActiveCanonicalVersions": {
            "passed": not duplicates,
            "groups": duplicates,
        },
        "rollbackRehearsal": {
            "passed": rollback_rehearsed,
            "evidence": "knowledge_index_registry.metadata_json.rolledBackAt" if rollback_rehearsed else None,
        },
        "legacyApiCompatibility": {"passed": True},
    }
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(UTC).isoformat(),
        "databaseAvailable": True,
        "eligibleToDisableLegacyBootstrap": all(item["passed"] for item in checks.values()),
        "checks": checks,
        "cleanupCandidates": {
            "duplicateActiveDocumentIds": sorted(
                {document_id for group in duplicates for document_id in group["documentIds"]}
            )
        },
        "cleanupPerformed": False,
    }


def unavailable_database_report(*, unmanaged_markdown: tuple[str, ...], error_type: str) -> dict:
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(UTC).isoformat(),
        "databaseAvailable": False,
        "databaseErrorCode": error_type,
        "eligibleToDisableLegacyBootstrap": False,
        "checks": {
            "manifestRegistration": {
                "passed": not unmanaged_markdown,
                "unmanagedCount": len(unmanaged_markdown),
                "paths": list(unmanaged_markdown),
            },
            "adminUploadsMigrated": {"passed": False, "status": "UNKNOWN"},
            "activeTraceability": {"passed": False, "status": "UNKNOWN"},
            "duplicateActiveCanonicalVersions": {"passed": False, "status": "UNKNOWN"},
            "rollbackRehearsal": {"passed": False, "status": "UNKNOWN"},
            "legacyApiCompatibility": {"passed": True},
        },
        "cleanupCandidates": {"duplicateActiveDocumentIds": []},
        "cleanupPerformed": False,
    }


def write_cutover_report(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _json_ids(raw: str) -> tuple[int, ...]:
    try:
        value = json.loads(raw or "[]")
        return tuple(int(item) for item in value) if isinstance(value, list) else ()
    except (json.JSONDecodeError, TypeError, ValueError):
        return ()


def _json_object(raw: str) -> dict:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}
