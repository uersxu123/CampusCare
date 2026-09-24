from pathlib import Path
from types import SimpleNamespace
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.core.database import Base
from app.models.entities import KnowledgeChunk, KnowledgeDocument
from app.services.handbook_import import HandbookImporter, load_corpus, merge_children
from app.services.knowledge import KnowledgeService

ROOT = Path(__file__).resolve().parents[1]


def test_delivered_corpus_and_sql_parent_links():
    parents, children = merge_children(*load_corpus(ROOT / "app/knowledge/handbook_v2"))
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        old = KnowledgeDocument(source_key="old", title="旧知识", source_type="LEGACY", domain="CAMPUS_SERVICE", content_hash="old", status="ACTIVE")
        db.add(old)
        db.commit()
        importer = HandbookImporter(db, SimpleNamespace(project_root=ROOT))
        result = importer.import_all()
        assert (result["documents"], result["parents"], result["children"]) == (49, 272, len(children))
        db.refresh(old)
        assert old.status == "ARCHIVED"
        assert importer.import_all()["changedDocuments"] == 0
        assert db.query(KnowledgeChunk).count() == len(parents) + len(children)
        linked = db.query(KnowledgeChunk).filter(KnowledgeChunk.parent_chunk_id.is_not(None)).all()
        assert len(linked) == sum(row["parent_id"] is not None for row in children)
        for child in linked:
            parent = db.get(KnowledgeChunk, child.parent_chunk_id)
            assert parent.chunk_kind == "TEXT_PARENT" and parent.document_id == child.document_id
        service = object.__new__(KnowledgeService)
        service.db = db
        child = linked[0]
        hit = service._result_from_chunk(child, 1, "vector")
        assert service.expand_context(hit).content == db.get(KnowledgeChunk, child.parent_chunk_id).content


def test_merge_preserves_every_source_and_boundaries():
    parents, original = load_corpus(ROOT / "app/knowledge/handbook_v2")
    updated, merged = merge_children(parents, original)
    lookup = {row["chunk_id"]: row for row in original}
    covered = []
    for row in merged:
        ids = row.get("merged_chunk_ids", [row["chunk_id"]])
        covered.extend(ids)
        sources = [lookup[key] for key in ids]
        assert row["text"] == "\n\n".join(item["text"] for item in sources)
        assert all(
            tuple(item.get(key) for key in ("document_id", "parent_id"))
            == tuple(row.get(key) for key in ("document_id", "parent_id"))
            and item.get("heading_path") == row.get("heading_path")
            for item in sources
        )
        if len(ids) > 1:
            assert row["token_count"] <= 400
            assert all(item["kind"] not in {"QA", "TABLE_ROW"} for item in sources)
    assert covered == [row["chunk_id"] for row in original]
    assert len(merged) < len(original)
    assert [row["text"] for row in updated] == [row["text"] for row in parents]
    assert merge_children(updated, merged) == (updated, merged)
