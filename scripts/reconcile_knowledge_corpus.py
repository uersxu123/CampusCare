from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import get_settings
from app.core.database import session_scope
from app.models.entities import KnowledgeDocument


DEPRECATED_DOMAINS = {"CAMPUS_LIFE"}


def scan_corpus(db) -> dict:
    rows = db.query(KnowledgeDocument).order_by(KnowledgeDocument.id.asc()).all()
    active_rows = [row for row in rows if row.status == "ACTIVE"]
    canonical_groups: dict[str, list[KnowledgeDocument]] = defaultdict(list)
    hash_groups: dict[str, list[KnowledgeDocument]] = defaultdict(list)
    for row in rows:
        canonical_groups[str(row.canonical_key or row.source_key)].append(row)
        if row.content_hash:
            hash_groups[row.content_hash].append(row)
    duplicate_canonical = [
        _group_payload(key, items)
        for key, items in canonical_groups.items()
        if sum(item.status == "ACTIVE" for item in items) > 1
    ]
    duplicate_content = [
        _group_payload(digest, items)
        for digest, items in hash_groups.items()
        if len({item.source_key for item in items}) > 1
    ]
    incomplete = [
        _document_payload(row)
        for row in rows
        if row.status == "ACTIVE"
        and (row.verified_at is None or not row.site or not row.managed_by or not row.canonical_key)
    ]
    deprecated = [_document_payload(row) for row in rows if row.status == "ACTIVE" and row.domain in DEPRECATED_DOMAINS]
    legacy_keys = [
        _document_payload(row)
        for row in rows
        if ":" in row.source_key or row.source_key.endswith(".md") or row.managed_by == "LEGACY"
    ]
    active_canonical_groups: dict[str, list[KnowledgeDocument]] = defaultdict(list)
    active_hash_groups: dict[str, list[KnowledgeDocument]] = defaultdict(list)
    for row in active_rows:
        active_canonical_groups[str(row.canonical_key or row.source_key)].append(row)
        if row.content_hash:
            active_hash_groups[row.content_hash].append(row)
    return {
        "schemaVersion": 1,
        "createdAt": datetime.now(UTC).isoformat(),
        "documentCount": len(rows),
        "activeDocumentCount": len(active_rows),
        "activeDocuments": [_document_payload(row) for row in active_rows],
        "duplicateCanonicalActive": [
            _group_payload(key, items)
            for key, items in active_canonical_groups.items()
            if len(items) > 1
        ],
        "duplicateContentActive": [
            _group_payload(digest, items)
            for digest, items in active_hash_groups.items()
            if len({item.source_key for item in items}) > 1
        ],
        "duplicateCanonical": duplicate_canonical,
        "duplicateContent": duplicate_content,
        "deprecatedDomain": deprecated,
        "incompleteActive": incomplete,
        "legacyCandidates": legacy_keys,
    }


def apply_reconciliation(db, report: dict) -> list[dict]:
    changes: list[dict] = []
    for group in report.get("duplicateCanonical", []):
        ids = [int(item["id"]) for item in group.get("documents", [])]
        rows = [db.get(KnowledgeDocument, item_id) for item_id in ids]
        rows = [row for row in rows if row is not None and row.status == "ACTIVE"]
        if len(rows) < 2:
            continue
        survivor = max(rows, key=_survivor_rank)
        for row in rows:
            if row.id == survivor.id:
                continue
            changes.append(_change(row, "DUPLICATE_CANONICAL", "INACTIVE"))
            row.status = "INACTIVE"
    for item in report.get("deprecatedDomain", []):
        row = db.get(KnowledgeDocument, int(item["id"]))
        if row is None or row.status != "ACTIVE":
            continue
        changes.append(_change(row, "DEPRECATED_DOMAIN", "INACTIVE"))
        row.status = "INACTIVE"
    db.commit()
    return changes


def restore_reconciliation(db, audit: dict) -> int:
    restored = 0
    for item in audit.get("changes", []):
        row = db.get(KnowledgeDocument, int(item["documentId"]))
        if row is None:
            continue
        row.status = str(item["previousStatus"])
        restored += 1
    db.commit()
    return restored


def _survivor_rank(row: KnowledgeDocument) -> tuple:
    return (
        row.managed_by == "MANIFEST",
        row.verified_at is not None,
        row.source_type.startswith("OFFICIAL"),
        row.updated_at or row.created_at,
        row.id,
    )


def _group_payload(key: str, rows: list[KnowledgeDocument]) -> dict:
    return {"key": key, "documents": [_document_payload(row) for row in rows]}


def _document_payload(row: KnowledgeDocument) -> dict:
    return {
        "id": row.id,
        "sourceKey": row.source_key,
        "canonicalKey": row.canonical_key,
        "managedBy": row.managed_by,
        "status": row.status,
        "domain": row.domain,
        "contentHash": row.content_hash,
        "verifiedAt": row.verified_at.isoformat() if row.verified_at else None,
    }


def _change(row: KnowledgeDocument, reason: str, new_status: str) -> dict:
    return {
        "documentId": row.id,
        "sourceKey": row.source_key,
        "canonicalKey": row.canonical_key,
        "reason": reason,
        "previousStatus": row.status,
        "newStatus": new_status,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="核对受管知识语料；默认只生成 dry-run 报告。")
    parser.add_argument("--apply", action="store_true", help="软归档明确重复项和废弃领域")
    parser.add_argument("--restore", type=Path, help="按审计文件恢复状态")
    parser.add_argument("--output", type=Path, help="报告或审计输出路径")
    args = parser.parse_args()
    settings = get_settings()
    output = args.output or settings.project_root / "target" / "knowledge-reconcile-audit.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    db = session_scope()
    try:
        if args.restore:
            audit = json.loads(args.restore.read_text(encoding="utf-8"))
            payload = {"schemaVersion": 1, "restored": restore_reconciliation(db, audit)}
        else:
            report = scan_corpus(db)
            changes = apply_reconciliation(db, report) if args.apply else []
            payload = {**report, "dryRun": not args.apply, "changes": changes}
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"语料核对结果已写入：{output}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
