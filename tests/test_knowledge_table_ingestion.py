import json
import tempfile
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import Base
from app.models.entities import KnowledgeChunk, KnowledgeTable
from app.services.knowledge import KnowledgeSearchResult, KnowledgeService
from app.services.knowledge_ingestion.pipeline import KnowledgeIngestionPipeline
from app.services.knowledge_ingestion.tables import KnowledgeTableQueryService, TableFilter, TableQuery


class KnowledgeTableIngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.settings = Settings(
            _env_file=None,
            database_url="sqlite+pysqlite:///:memory:",
            knowledge_vector_enabled=False,
            knowledge_artifact_dir=self.temp.name,
        )
        source = """# 奖学金

| 奖项 | 比例 | 金额 | 备注 |
|---|---:|---:|---|
| 一等 | 5% | 3000 元 | 本科生 |
| 二等 | 12% | 2000 元 | |
"""
        self.result = KnowledgeIngestionPipeline(self.db, self.settings).ingest_document(
            filename="奖学金.md",
            data=source.encode("utf-8"),
            mime_type="text/markdown",
            title="2025 年奖学金评定标准",
            domain="CAMPUS_SERVICE",
        )

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    def test_raw_structure_empty_cells_html_schema_and_derived_chunks_are_preserved(self):
        table = self.db.query(KnowledgeTable).one()
        self.assertEqual(json.loads(table.headers_json), ["奖项", "比例", "金额", "备注"])
        self.assertEqual(json.loads(table.rows_json)[1], ["二等", "12%", "2000 元", ""])
        self.assertIn("<table>", table.table_html)
        schema = json.loads(table.schema_json)
        self.assertEqual(schema["columns"][1]["type"], "percent")
        kinds = [item.chunk_kind for item in self.db.query(KnowledgeChunk).filter_by(document_id=self.result.document_id).all()]
        self.assertEqual(kinds.count("TABLE_SUMMARY"), 1)
        self.assertEqual(kinds.count("TABLE_ROW"), 2)
        row_text = "\n".join(item.content for item in self.db.query(KnowledgeChunk).filter_by(chunk_kind="TABLE_ROW").all())
        self.assertIn("奖项=一等", row_text)
        self.assertIn("比例=12%", row_text)

    def test_restricted_query_filters_typed_percent_and_returns_row_citations(self):
        table = self.db.query(KnowledgeTable).one()
        service = KnowledgeTableQueryService(self.db)
        result = service.query(
            TableQuery(
                document_id=self.result.document_id,
                table_id=table.id,
                filters=(TableFilter("比例", "gt", "10%"),),
                limit=10,
            )
        )
        self.assertEqual([item.values["奖项"] for item in result.rows], ["二等"])
        self.assertEqual(result.rows[0].citation["tableId"], table.id)
        self.assertEqual(result.rows[0].citation["rowIndex"], 2)

    def test_query_rejects_unknown_columns_operators_scope_and_excessive_limit(self):
        table = self.db.query(KnowledgeTable).one()
        service = KnowledgeTableQueryService(self.db)
        invalid = (
            TableQuery(self.result.document_id, table.id, (TableFilter("不存在", "eq", "x"),)),
            TableQuery(self.result.document_id, table.id, (TableFilter("比例", "sql", "x"),)),
            TableQuery(self.result.document_id + 1, table.id),
            TableQuery(self.result.document_id, table.id, limit=1001),
        )
        for query in invalid:
            with self.subTest(query=query), self.assertRaises(ValueError):
                service.query(query)

    def test_retrieval_limits_results_from_the_same_table(self):
        chunks = self.db.query(KnowledgeChunk).filter_by(document_id=self.result.document_id).all()
        table_chunks = [item for item in chunks if item.chunk_kind in {"TABLE_SUMMARY", "TABLE_ROW"}]
        ranked = [
            KnowledgeSearchResult(
                chunk_id=item.id,
                document_id=item.document_id,
                source=item.source,
                content=item.content,
                score=1.0 - index * 0.01,
            )
            for index, item in enumerate(table_chunks)
        ]
        selected = KnowledgeService(self.db, self.settings)._select_diverse(
            ranked,
            top_k=10,
            document_limit=10,
            query="奖学金比例金额",
        )
        self.assertEqual(len(selected), 2)


if __name__ == "__main__":
    unittest.main()
