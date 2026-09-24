"""Import the delivered corpus, merging short children within structural boundaries."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from app.models.entities import KnowledgeChunk, KnowledgeDocument, KnowledgeElement


PROFILE = "handbook_prechunked_v2"
SOURCE_PREFIX = "handbook-v2:"


def merge_children(parents: list[dict], children: list[dict], target: int = 240, maximum: int = 400):
    """保留原文与父块；长度采用原 token 计数加分隔符预算。"""
    if not 0 < target <= maximum:
        raise ValueError("合并预算无效")
    merged, pending = [], []
    aliases = {}
    allowed = {"ARTICLE", "PARAGRAPH", "LIST_ITEM", "LIST"}

    def flush():
        if not pending:
            return
        row = dict(pending[0])
        if len(pending) > 1:
            row["text"] = "\n\n".join(item["text"] for item in pending)
            row["content_sha256"] = hashlib.sha256(row["text"].encode("utf-8")).hexdigest()
            row["token_count"] = sum(item["token_count"] for item in pending) + 2 * (len(pending) - 1)
            row["merged_chunk_ids"] = [item["chunk_id"] for item in pending]
            row["merge_reason"] = "adjacent_short_children_240_400"
            row["token_count_method"] = "original_counts_plus_separator_budget"
            for key in (
                "source_fragments", "source_element_ids", "source_chunk_ids", "source_parent_ids",
                "pdf_pages", "printed_pages", "article_labels", "reference_ids", "context_refs",
            ):
                values = []
                for item in pending:
                    for value in item.get(key, []):
                        if value not in values:
                            values.append(value)
                row[key] = values
        merged.append(row)
        for item in pending:
            aliases[item["chunk_id"]] = row["chunk_id"]
        pending.clear()

    for row in children:
        if row.get("kind") not in allowed:
            flush()
            pending.append(row)
            flush()
            continue
        if pending:
            previous = pending[-1]
            boundary = any(
                previous.get(key) != row.get(key)
                for key in ("document_id", "parent_id", "heading_path")
            )
            budget = sum(item["token_count"] for item in pending) + 2 * (len(pending) - 1)
            if boundary or budget >= target or budget + 2 + row["token_count"] > maximum:
                flush()
        pending.append(row)
    flush()
    updated_parents = [
        dict(row, child_ids=list(dict.fromkeys(aliases[key] for key in row["child_ids"])))
        for row in parents
    ]
    return updated_parents, merged


def load_corpus(root: Path) -> tuple[list[dict], list[dict]]:
    parents, children = (
        [json.loads(line) for line in (root / name).read_text(encoding="utf-8").splitlines() if line.strip()]
        for name in ("parents_v2.jsonl", "children_v2.jsonl")
    )
    parent_map = {row["parent_id"]: row for row in parents}
    child_map = {row["chunk_id"]: row for row in children}
    if not parents or not children or len(parent_map) != len(parents) or len(child_map) != len(children):
        raise ValueError("父子分块为空或 ID 重复")
    for row in parents + children:
        if not row["text"].strip() or hashlib.sha256(row["text"].encode("utf-8")).hexdigest() != row["content_sha256"]:
            raise ValueError("分块正文为空或哈希校验失败")
    for row in children:
        if row["parent_id"] is None:
            continue
        parent = parent_map.get(row["parent_id"])
        if parent is None or parent["document_id"] != row["document_id"] or row["chunk_id"] not in parent["child_ids"]:
            raise ValueError("子块父级关联无效")
    for parent in parents:
        if len(set(parent["child_ids"])) != len(parent["child_ids"]):
            raise ValueError("父块包含重复子块")
        for child_id in parent["child_ids"]:
            if child_id not in child_map or child_map[child_id]["parent_id"] != parent["parent_id"]:
                raise ValueError("父块子级关联无效")
    return parents, children


class HandbookImporter:
    def __init__(self, db, settings, root: Path | None = None):
        self.db = db
        self.root = root or settings.project_root / "app/knowledge/handbook_v2"

    def import_all(self) -> dict:
        parents, children = merge_children(*load_corpus(self.root))
        groups = defaultdict(list)
        for row in parents + children:
            groups[row["document_id"]].append(row)
        keys = {SOURCE_PREFIX + key for key in groups}
        changed = 0
        try:
            for document_id, rows in groups.items():
                source = SOURCE_PREFIX + document_id
                digest = hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
                document = self.db.query(KnowledgeDocument).filter_by(source_key=source).one_or_none()
                if document is not None and document.content_hash == digest and len(document.chunks) == len(rows):
                    document.status = "ACTIVE"
                    continue
                if document is None:
                    document = KnowledgeDocument(source_key=source)
                    self.db.add(document)
                else:
                    chunks = self.db.query(KnowledgeChunk).filter_by(document_id=document.id)
                    chunks.filter(KnowledgeChunk.parent_chunk_id.is_not(None)).delete(synchronize_session=False)
                    chunks.delete(synchronize_session=False)
                    self.db.query(KnowledgeElement).filter_by(document_id=document.id).delete(synchronize_session=False)
                document.canonical_key = source
                document.managed_by = PROFILE
                title_row = next((row for row in rows if "chunk_id" in row), rows[0])
                document.title = title_row["text"].splitlines()[0][:256]
                document.source_type = "STUDENT_HANDBOOK"
                document.domain = "CAMPUS_SERVICE"
                document.site = "ALL"
                document.status = "ACTIVE"
                document.version = "2025-v2"
                document.verified_at = datetime.now(UTC).replace(tzinfo=None)
                document.content_hash = digest
                document.parser_profile = PROFILE
                document.parser_version = "delivery-v2"
                document.chunking_profile = PROFILE
                document.ingestion_status = "READY"
                self.db.flush()
                parent_ids = {}
                for index, row in enumerate(rows):
                    is_parent = "chunk_id" not in row
                    element = KnowledgeElement(
                        document_id=document.id, element_index=index,
                        element_type="TEXT", content=row["text"], content_hash=row["content_sha256"],
                        metadata_json=json.dumps({key: value for key, value in row.items() if key != "text"}, ensure_ascii=False),
                        parser_name=PROFILE, parser_version="v2",
                    )
                    self.db.add(element)
                    self.db.flush()
                    chunk = KnowledgeChunk(
                        document_id=document.id, source=source, source_index=index,
                        content=row["text"], content_hash=row["content_sha256"],
                        chunk_kind="TEXT_PARENT" if is_parent else "TEXT_CHILD",
                        parent_chunk_id=None if is_parent else parent_ids.get(row["parent_id"]),
                        section_title=" / ".join(row.get("heading_path") or [])[:256] or None,
                        heading_path_json=json.dumps(row.get("heading_path", []), ensure_ascii=False),
                        page_number=next(iter(row.get("pdf_pages", [])), None),
                        token_count=row.get("token_count"), chunking_profile=PROFILE,
                        element_ids_json=json.dumps([element.id]),
                    )
                    self.db.add(chunk)
                    self.db.flush()
                    if is_parent:
                        parent_ids[row["parent_id"]] = chunk.id
                changed += 1
            # Archive old documents instead of deleting their rollback history.
            archived = self.db.query(KnowledgeDocument).filter(
                KnowledgeDocument.source_key.notin_(keys), KnowledgeDocument.status != "ARCHIVED"
            ).update({KnowledgeDocument.status: "ARCHIVED"}, synchronize_session=False)
            # Unmanaged orphan chunks cannot be archived; exclude them from retrieval.
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return {"documents": len(groups), "parents": len(parents), "children": len(children),
                "changedDocuments": changed, "archivedDocuments": archived}
