import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.entities import KnowledgeChunk, KnowledgeDocument
from app.services.knowledge_import import (
    KnowledgeManifestImporter,
    _clean_pdf_text,
    stable_chunk_text,
)
from scripts.reconcile_knowledge_corpus import (
    apply_reconciliation,
    restore_reconciliation,
    scan_corpus,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def alembic_config(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


class ManifestLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.raw = self.root / "knowledge.md"
        self.raw.write_text("# 办理指南\n\n第一步提交申请表。\n\n第二步等待审核。", encoding="utf-8")
        self.manifest = self.root / "manifest.yaml"
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.settings = SimpleNamespace(
            project_root=self.root,
            knowledge_chunk_size=64,
            knowledge_chunk_overlap=8,
            knowledge_vector_enabled=False,
        )

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    def _write_manifest(self, *, title="办理指南", documents=True):
        digest = hashlib.sha256(self.raw.read_bytes()).hexdigest().upper()
        body = "documents: []\n"
        if documents:
            body = f"""documents:
  - source_key: managed-guide-v1
    canonical_key: managed-guide
    title: {title}
    source_url:
    source_type: OFFICIAL_SERVICE_GUIDE
    domain: CAMPUS_SERVICE
    tags: [办理]
    site: ALL
    status: ACTIVE
    verified_at: "2026-07-31T00:00:00"
    version: "1"
    raw_path: knowledge.md
    expected_sha256: {digest}
    cleaning_strategy: markdown_headings
"""
        self.manifest.write_text(f"manifest_version: 1\n{body}", encoding="utf-8")

    def test_metadata_only_update_preserves_stable_chunks(self):
        self._write_manifest()
        importer = KnowledgeManifestImporter(self.db, self.settings, self.manifest)
        first = importer.import_all(rebuild_index=False)
        row = self.db.query(KnowledgeDocument).one()
        chunk_ids = [item.id for item in self.db.query(KnowledgeChunk).order_by(KnowledgeChunk.id).all()]

        self._write_manifest(title="更新后的办理指南")
        second = importer.import_all(rebuild_index=False)
        self.db.refresh(row)

        self.assertTrue(first["documents"][0]["content_changed"])
        self.assertFalse(second["documents"][0]["content_changed"])
        self.assertTrue(second["documents"][0]["metadata_changed"])
        self.assertEqual(row.title, "更新后的办理指南")
        self.assertEqual(row.canonical_key, "managed-guide")
        self.assertEqual(row.managed_by, "MANIFEST")
        self.assertEqual(
            [item.id for item in self.db.query(KnowledgeChunk).order_by(KnowledgeChunk.id).all()],
            chunk_ids,
        )

    def test_removed_manifest_document_is_inactive_but_upload_is_untouched(self):
        self._write_manifest()
        importer = KnowledgeManifestImporter(self.db, self.settings, self.manifest)
        importer.import_all(rebuild_index=False)
        upload = KnowledgeDocument(
            source_key="upload.txt",
            canonical_key="upload.txt",
            managed_by="UPLOAD",
            title="上传草稿",
            source_type="UPLOAD",
            domain="CAMPUS_SERVICE",
            tags_json="[]",
            site="ALL",
            status="DRAFT",
            version="1",
            content_hash="upload",
        )
        self.db.add(upload)
        self.db.commit()

        self._write_manifest(documents=False)
        report = importer.import_all(rebuild_index=False)
        managed = self.db.query(KnowledgeDocument).filter_by(source_key="managed-guide-v1").one()
        self.db.refresh(upload)

        self.assertEqual(managed.status, "INACTIVE")
        self.assertEqual(upload.status, "DRAFT")
        self.assertEqual(len(report["archived"]), 1)

    def test_stable_chunking_and_pdf_noise_cleaning_are_deterministic(self):
        text = "第一段说明。\n\n第二段说明。\n\n第三段说明。"
        self.assertEqual(stable_chunk_text(text, 16, 4), stable_chunk_text(text, 16, 4))
        cleaned = _clean_pdf_text("中国地质大学学生手册\n- 12 -\n问：问：如何办理\n答：答：提交申请表\n答：答：提交申请表")
        self.assertNotIn("问：问：", cleaned)
        self.assertNotIn("答：答：", cleaned)
        self.assertEqual(cleaned.count("提交申请表"), 1)


class ReconcileCorpusTests(unittest.TestCase):
    def test_apply_and_restore_only_change_document_status(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            now = datetime.now(UTC).replace(tzinfo=None)
            rows = [
                KnowledgeDocument(
                    source_key="old-copy",
                    canonical_key="same-guide",
                    managed_by="LEGACY",
                    title="旧副本",
                    source_type="INTERNAL_GUIDANCE",
                    domain="CAMPUS_SERVICE",
                    tags_json="[]",
                    site="ALL",
                    status="ACTIVE",
                    verified_at=now,
                    version="1",
                    content_hash="same",
                ),
                KnowledgeDocument(
                    source_key="managed-copy",
                    canonical_key="same-guide",
                    managed_by="MANIFEST",
                    title="受管版本",
                    source_type="OFFICIAL_SERVICE_GUIDE",
                    domain="CAMPUS_SERVICE",
                    tags_json="[]",
                    site="ALL",
                    status="ACTIVE",
                    verified_at=now,
                    version="2",
                    content_hash="same",
                ),
            ]
            db.add_all(rows)
            db.commit()
            report = scan_corpus(db)
            changes = apply_reconciliation(db, report)
            self.assertEqual(len(changes), 1)
            self.assertEqual(rows[0].status, "INACTIVE")
            self.assertEqual(rows[1].status, "ACTIVE")
            restored = restore_reconciliation(db, {"changes": changes})
            self.assertEqual(restored, 1)
            self.assertEqual(rows[0].status, "ACTIVE")
        engine.dispose()


class KnowledgeGovernanceMigrationTests(unittest.TestCase):
    def test_0009_round_trip_marks_legacy_without_changing_content(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{Path(directory) / 'governance.sqlite'}"
            config = alembic_config(url)
            command.upgrade(config, "0008_response_completion")
            engine = create_engine(url)
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "INSERT INTO knowledge_documents "
                        "(source_key,title,source_type,domain,tags_json,site,status,version,content_hash,created_at,updated_at) "
                        "VALUES ('历史来源','历史中文标题','INTERNAL_GUIDANCE','ACADEMIC','[]','ALL','ACTIVE','1','hash',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                    )
                )
            command.upgrade(config, "0009_knowledge_governance")
            with engine.connect() as connection:
                values = connection.execute(
                    sa.text("SELECT canonical_key, managed_by, title FROM knowledge_documents")
                ).one()
            self.assertEqual(values, ("历史来源", "LEGACY", "历史中文标题"))
            self.assertIn("knowledge_index_registry", sa.inspect(engine).get_table_names())
            command.downgrade(config, "0008_response_completion")
            self.assertNotIn(
                "canonical_key",
                {item["name"] for item in sa.inspect(engine).get_columns("knowledge_documents")},
            )
            command.upgrade(config, "0009_knowledge_governance")
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
