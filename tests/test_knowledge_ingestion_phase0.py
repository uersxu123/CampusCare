import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.bootstrap import import_legacy_markdown_compat
from app.core.config import Settings
from app.core.database import Base
from app.models.entities import KnowledgeChunk, KnowledgeDocument
from app.rag_eval.runner import corpus_chunk_metrics
from app.services.knowledge import KnowledgeService, chunk_text
from app.services.knowledge_import import _legacy_markdown_chunks, _markdown_chunks, _pdf_page_chunks, stable_chunk_text
from scripts.reconcile_knowledge_corpus import scan_corpus


class _Page:
    def __init__(self, text: str):
        self.text = text

    def extract_text(self) -> str:
        return self.text


class _Reader:
    pages = [
        _Page("第一页正文" * 12),
        _Page("第二页正文" * 12),
    ]

    def __init__(self, _source):
        pass


class LegacyChunkContractTests(unittest.TestCase):
    def test_chunk_text_keeps_character_window_and_arbitrary_overlap(self):
        self.assertEqual(
            chunk_text("ab  cd\nefghij", size=5, overlap=2),
            ["ab cd", "cd ef", "efghi", "hij"],
        )

    def test_stable_chunk_text_keeps_legacy_oversized_overlap_behavior(self):
        chunks = stable_chunk_text("甲乙丙丁。\n\n一二三四。\n\n五六七八。", size=8, overlap=3)
        self.assertEqual(chunks, ["甲乙丙丁。", "丙丁。 一二三四。", "三四。 五六七八。"])
        self.assertGreater(len(chunks[1]), 8)

    def test_markdown_parser_keeps_only_current_heading_and_drops_heading_text(self):
        settings = SimpleNamespace(knowledge_chunk_size=128, knowledge_chunk_overlap=16)
        chunks = _markdown_chunks("# 一级标题\n正文甲。\n## 二级标题\n正文乙。", settings)
        self.assertEqual(
            [(item.section_title, item.page_number, item.content) for item in chunks],
            [("一级标题", None, "正文甲。"), ("二级标题", None, "正文乙。")],
        )

    def test_manifest_legacy_markdown_keeps_bootstrap_chunk_contract(self):
        settings = SimpleNamespace(knowledge_chunk_size=16, knowledge_chunk_overlap=4)
        text = "# 一级标题\n正文甲。\n## 二级标题\n正文乙。"

        chunks = _legacy_markdown_chunks(text, settings)

        self.assertEqual(
            [item.content for item in chunks],
            chunk_text(text, settings.knowledge_chunk_size, settings.knowledge_chunk_overlap),
        )
        self.assertTrue(all(item.section_title is None for item in chunks))

    def test_manifest_pdf_parser_keeps_each_physical_page_number(self):
        settings = SimpleNamespace(knowledge_chunk_size=512, knowledge_chunk_overlap=64)
        with patch("app.services.knowledge_import.PdfReader", _Reader):
            chunks = _pdf_page_chunks(Path("unused.pdf"), "测试 PDF", settings)
        self.assertEqual([item.page_number for item in chunks], [1, 2])
        self.assertEqual([item.section_title for item in chunks], ["测试 PDF", "测试 PDF"])


class AdminPdfCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.settings = Settings(
            _env_file=None,
            database_url="sqlite+pysqlite:///:memory:",
            knowledge_vector_enabled=False,
            knowledge_chunk_size=512,
            knowledge_chunk_overlap=64,
        )

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_admin_pdf_upload_flattens_pages_and_loses_page_numbers(self):
        with patch("app.services.knowledge.PdfReader", _Reader):
            count = KnowledgeService(self.db, self.settings).ingest_file("制度.pdf", b"pdf")

        rows = self.db.query(KnowledgeChunk).order_by(KnowledgeChunk.source_index).all()
        self.assertEqual(count, 1)
        self.assertIn("第一页正文", rows[0].content)
        self.assertIn("第二页正文", rows[0].content)
        self.assertTrue(all(row.page_number is None for row in rows))


class CorpusAuditContractTests(unittest.TestCase):
    def test_scan_reports_active_documents_and_duplicate_keys_without_body_text(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            now = datetime.now(UTC).replace(tzinfo=None)
            db.add_all(
                [
                    KnowledgeDocument(
                        source_key=f"source-{index}",
                        canonical_key="same-key",
                        managed_by="MANIFEST",
                        title=f"文档 {index}",
                        source_type="OFFICIAL_POLICY",
                        domain="ACADEMIC",
                        tags_json="[]",
                        site="ALL",
                        status="ACTIVE",
                        verified_at=now,
                        version=str(index),
                        content_hash="same-hash",
                    )
                    for index in (1, 2)
                ]
            )
            db.commit()
            report = scan_corpus(db)
            active_count = db.query(KnowledgeDocument).filter_by(status="ACTIVE").count()

        self.assertEqual(report["activeDocumentCount"], 2)
        self.assertEqual(len(report["activeDocuments"]), 2)
        self.assertEqual(len(report["duplicateCanonicalActive"]), 1)
        self.assertEqual(len(report["duplicateContentActive"]), 1)
        document_keys = {key for item in report["activeDocuments"] for key in item}
        self.assertNotIn("content", document_keys)
        self.assertEqual(active_count, 2)
        engine.dispose()


class BootstrapCompatibilityTests(unittest.TestCase):
    def test_legacy_markdown_import_is_enabled_by_default(self):
        self.assertFalse(Settings(_env_file=None).knowledge_legacy_markdown_bootstrap_enabled)

    def test_disabled_legacy_markdown_import_skips_files_and_emits_structured_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            knowledge_dir = root / "knowledge"
            knowledge_dir.mkdir()
            (knowledge_dir / "legacy.md").write_text("旧知识正文", encoding="utf-8")
            settings = SimpleNamespace(
                project_root=root,
                knowledge_legacy_markdown_bootstrap_enabled=False,
            )
            service = SimpleNamespace(ensure_source=lambda *_args: self.fail("不应导入"))
            importer = SimpleNamespace(managed_raw_paths=lambda: set())

            with patch("app.core.bootstrap.logger.warning") as warning:
                count = import_legacy_markdown_compat(service, importer, settings, root)

        self.assertEqual(count, 0)
        warning.assert_called_once()
        self.assertEqual(warning.call_args.kwargs["extra"]["event"], "legacy_markdown_bootstrap_disabled")
        self.assertEqual(warning.call_args.kwargs["extra"]["unmanaged_count"], 1)


class EvaluationBaselineContractTests(unittest.TestCase):
    def test_chunk_metrics_record_active_count_length_distribution_and_no_body(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            document = KnowledgeDocument(
                source_key="baseline",
                canonical_key="baseline",
                managed_by="MANIFEST",
                title="基线",
                source_type="OFFICIAL_POLICY",
                domain="ACADEMIC",
                tags_json="[]",
                site="ALL",
                status="ACTIVE",
                verified_at=datetime.now(UTC).replace(tzinfo=None),
                version="1",
                content_hash="hash",
            )
            db.add(document)
            db.flush()
            db.add_all(
                [
                    KnowledgeChunk(document_id=document.id, source="baseline", source_index=0, content="甲" * 10),
                    KnowledgeChunk(document_id=document.id, source="baseline", source_index=1, content="乙" * 20),
                ]
            )
            db.commit()
            metrics = corpus_chunk_metrics(db)

        self.assertEqual(metrics["activeChunkCount"], 2)
        self.assertEqual(metrics["lengthCharacters"], {"min": 10, "p50": 10, "p95": 20, "max": 20, "mean": 15.0})
        self.assertNotIn("甲", str(metrics))
        self.assertNotIn("乙", str(metrics))
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
