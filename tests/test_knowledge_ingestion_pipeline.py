import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import Base
from app.models.entities import KnowledgeArtifact, KnowledgeChunk, KnowledgeDocument, KnowledgeElement
from app.services.knowledge import chunk_text
from app.services.knowledge_ingestion.artifact_store import LocalArtifactStore
from app.services.knowledge_ingestion.pipeline import KnowledgeIngestionPipeline


class ArtifactStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = LocalArtifactStore(self.root, max_bytes=1024)

    def tearDown(self):
        self.temp.cleanup()

    def test_storage_key_is_content_addressed_and_filename_never_controls_path(self):
        first = self.store.put("制度.txt", b"same", "text/plain")
        second = self.store.put("另一个名字.txt", b"same", "text/plain")
        self.assertEqual(first.storage_key, second.storage_key)
        self.assertEqual(first.path, second.path)
        self.assertTrue(first.path.is_relative_to(self.root.resolve()))
        self.assertEqual(first.path.read_bytes(), b"same")

    def test_path_traversal_absolute_path_and_oversized_file_are_rejected(self):
        for filename in ("../secret.txt", "..\\secret.txt", "C:\\secret.txt", "/secret.txt"):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                self.store.put(filename, b"safe", "text/plain")
        with self.assertRaises(ValueError):
            self.store.put("large.txt", b"x" * 1025, "text/plain")


class LegacyPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.db.execute(text("PRAGMA foreign_keys=ON"))
        self.settings = Settings(
            _env_file=None,
            database_url="sqlite+pysqlite:///:memory:",
            knowledge_vector_enabled=False,
            knowledge_chunk_size=12,
            knowledge_chunk_overlap=3,
            knowledge_artifact_dir=self.temp.name,
            knowledge_upload_max_bytes=4096,
        )
        self.pipeline = KnowledgeIngestionPipeline(self.db, self.settings)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    def test_legacy_text_pipeline_is_idempotent_and_preserves_exact_chunks(self):
        content = "第一段正文。\n第二段正文。第三段正文。"
        expected = chunk_text(content, 12, 3)
        first = self.pipeline.ingest_legacy_text(source="upload.txt", content=content)
        second = self.pipeline.ingest_legacy_text(source="upload.txt", content=content)

        document = self.db.query(KnowledgeDocument).filter_by(source_key="upload.txt").one()
        chunks = self.db.query(KnowledgeChunk).filter_by(document_id=document.id).order_by(KnowledgeChunk.source_index).all()
        self.assertEqual([item.content for item in chunks], expected)
        self.assertEqual(first.chunk_count, second.chunk_count)
        self.assertEqual(document.ingestion_status, "READY")
        self.assertEqual(document.parser_profile, "legacy")
        self.assertEqual(document.chunking_profile, "legacy_char_v1")
        self.assertEqual(self.db.query(KnowledgeArtifact).filter_by(document_id=document.id).count(), 1)

    def test_same_filename_with_different_content_keeps_both_artifact_hashes(self):
        self.pipeline.ingest_legacy_text(source="same.txt", content="版本一正文")
        self.pipeline.ingest_legacy_text(source="same.txt", content="版本二正文")
        document = self.db.query(KnowledgeDocument).filter_by(source_key="same.txt").one()
        artifacts = self.db.query(KnowledgeArtifact).filter_by(document_id=document.id).all()
        self.assertEqual(len({item.sha256 for item in artifacts}), 2)

    def test_failed_reprocessing_rolls_back_and_keeps_active_chunks(self):
        document = KnowledgeDocument(
            source_key="active.txt",
            canonical_key="active.txt",
            managed_by="UPLOAD",
            title="线上文档",
            source_type="UPLOAD",
            domain="MENTAL_HEALTH",
            tags_json="[]",
            site="ALL",
            status="ACTIVE",
            verified_at=datetime.now(UTC).replace(tzinfo=None),
            version="1",
            content_hash="old",
            ingestion_status="READY",
            parser_profile="legacy",
            chunking_profile="legacy_char_v1",
        )
        self.db.add(document)
        self.db.flush()
        self.db.add(KnowledgeChunk(document_id=document.id, source="active.txt", source_index=0, content="旧线上正文"))
        self.db.commit()

        with patch.object(self.pipeline, "_replace_chunks", side_effect=RuntimeError("forced")):
            with self.assertRaises(RuntimeError):
                self.pipeline.ingest_legacy_text(source="active.txt", content="新但失败的正文")

        self.db.expire_all()
        preserved = self.db.query(KnowledgeDocument).filter_by(source_key="active.txt").one()
        self.assertEqual(preserved.status, "ACTIVE")
        self.assertEqual([item.content for item in preserved.chunks], ["旧线上正文"])

    def test_rechunking_existing_parent_children_deletes_children_before_parents(self):
        document_id = self.pipeline.ingest_legacy_text(
            source="parent-child.txt",
            content="第一段内容。" * 20 + "第二段内容。" * 20,
        )
        document = self.db.query(KnowledgeDocument).filter_by(source_key="parent-child.txt").one()

        first = self.pipeline.rechunk_document(document.id)
        second = self.pipeline.rechunk_document(document.id)

        self.assertGreater(first.chunk_count, 0)
        self.assertGreater(second.chunk_count, 0)
        parents = {
            item.id
            for item in self.db.query(KnowledgeChunk).filter_by(
                document_id=document.id, chunk_kind="TEXT_PARENT"
            ).all()
        }
        children = self.db.query(KnowledgeChunk).filter_by(
            document_id=document.id, chunk_kind="TEXT_CHILD"
        ).all()
        self.assertTrue(parents)
        self.assertTrue(children)
        self.assertTrue(all(item.parent_chunk_id in parents for item in children))

    def test_changed_legacy_content_discards_stale_elements_before_rechunking(self):
        self.pipeline.ingest_legacy_text(source="updated.txt", content="旧内容。" * 20)
        document = self.db.query(KnowledgeDocument).filter_by(source_key="updated.txt").one()
        self.pipeline.rechunk_document(document.id)
        self.assertGreater(
            self.db.query(KnowledgeElement).filter_by(document_id=document.id).count(),
            0,
        )

        self.pipeline.ingest_legacy_text(
            source="updated.txt",
            content="电话67883110，地点行政楼。",
        )
        self.assertEqual(
            self.db.query(KnowledgeElement).filter_by(document_id=document.id).count(),
            0,
        )

        self.pipeline.rechunk_document(document.id)
        contents = "\n".join(
            item.content
            for item in self.db.query(KnowledgeChunk)
            .filter_by(document_id=document.id, chunk_kind="TEXT_CHILD")
            .all()
        )
        self.assertIn("67883110", contents)
        self.assertNotIn("旧内容", contents)


if __name__ == "__main__":
    unittest.main()
