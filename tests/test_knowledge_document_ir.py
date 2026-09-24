import dataclasses
import tempfile
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import Base
from app.models.entities import KnowledgeElement
from app.services.knowledge_ingestion.models import DocumentElement, ParsedDocument
from app.services.knowledge_ingestion.pipeline import KnowledgeIngestionPipeline


class DocumentIrTests(unittest.TestCase):
    def test_ir_is_frozen_and_uses_typed_elements(self):
        element = DocumentElement(0, "PARAGRAPH", "正文", heading_path=("一级",))
        parsed = ParsedDocument("标题", (element,), "fake", "1")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            element.content = "修改"
        self.assertEqual(parsed.elements[0].heading_path, ("一级",))

    def test_pipeline_persists_plaintext_elements_and_parser_version(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = create_engine("sqlite+pysqlite:///:memory:")
            Base.metadata.create_all(engine)
            with Session(engine) as db:
                settings = Settings(
                    _env_file=None,
                    database_url="sqlite+pysqlite:///:memory:",
                    knowledge_vector_enabled=False,
                    knowledge_artifact_dir=directory,
                )
                result = KnowledgeIngestionPipeline(db, settings).ingest_document(
                    filename="说明.txt",
                    data="第一段。\n\n第二段。".encode("utf-8"),
                    mime_type="text/plain",
                    title="说明",
                )
                rows = db.query(KnowledgeElement).order_by(KnowledgeElement.element_index).all()
                self.assertEqual([item.content for item in rows], ["第一段。", "第二段。"])
                self.assertTrue(all(item.parser_name == "plaintext" for item in rows))
                self.assertEqual(result.ingestion_status, "READY")
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
