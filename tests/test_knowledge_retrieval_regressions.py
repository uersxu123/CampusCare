import json
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import Base
from app.services.knowledge import KnowledgeService, StructuredSearchRequest
from app.services.knowledge_import import KnowledgeManifestImporter
from app.services.knowledge_scoring import KnowledgeTokenizer, reciprocal_rank_fusion


class KnowledgeScoringTests(unittest.TestCase):
    def test_tokenizer_never_creates_cross_punctuation_ngram(self):
        tokens = KnowledgeTokenizer().tokenize("调宿，医保")
        self.assertNotIn("宿医", tokens)
        self.assertIn("concept:DORM_CHANGE", tokens)
        self.assertIn("concept:MEDICAL_INSURANCE", tokens)

    def test_rrf_uses_rank_not_batch_score_normalization(self):
        scores = reciprocal_rank_fusion(((['a', 'b'], 1.0), (['b', 'c'], 1.0)), k=60)
        self.assertGreater(scores["b"], scores["a"])
        self.assertGreater(scores["b"], scores["c"])
        self.assertAlmostEqual(scores["a"], 1 / 61)


class OfficialCorpusRetrieverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(cls.engine)
        cls.db = Session(cls.engine)
        cls.settings = Settings(
            _env_file=None, database_url="sqlite+pysqlite:///:memory:",
            knowledge_vector_enabled=False,
        )
        KnowledgeManifestImporter(cls.db, cls.settings).import_all(rebuild_index=False)
        cls.service = KnowledgeService(cls.db, cls.settings)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.engine.dispose()

    def test_handbook_dorm_rules_replace_retired_service_guide(self):
        for query in ("学生宿舍住宿管理办法", "宿舍住宿规定"):
            results = self.service.search(StructuredSearchRequest(
                query=query, domains=("CAMPUS_SERVICE",), site="NANWANGSHAN",
                top_k=8, document_limit=6, include_neighbors=True,
            ))
            self.assertIn("handbook-v2:doc-12", [item.source_key for item in results[:3]], query)

    def test_status_reports_explicit_vector_degradation(self):
        settings = self.settings.model_copy(update={"knowledge_vector_enabled": True})
        service = KnowledgeService(self.db, settings)
        service.vector_store = None
        service.vector_error = "INJECTED_VECTOR_FAILURE"

        self.assertEqual(service.status()["retrievalMode"], "degraded-bm25-only")

    def test_retriever_preserves_complete_provenance(self):
        results = self.service.search(StructuredSearchRequest(
            query="调宿申请材料办理流程", domains=("CAMPUS_SERVICE",), site="NANWANGSHAN",
            top_k=8, document_limit=6, include_neighbors=True,
        ))
        self.assertTrue(results)
        for item in results:
            self.assertIsNotNone(item.chunk_id)
            self.assertIsNotNone(item.document_id)
            self.assertIsNotNone(item.verified_at)
            self.assertIsNotNone(item.provenance)
            self.assertTrue(item.provenance.child_chunk_ids)
            self.assertTrue(item.provenance.content_hashes)

    def test_quality_regression_queries_recall_expected_document(self):
        cases = [
            {"id": "discipline", "userQuestion": "学生纪律处分办法", "expectedDocumentKeys": ["handbook-v2:doc-08"]},
            {"id": "appeal", "userQuestion": "学生申诉处理办法", "expectedDocumentKeys": ["handbook-v2:doc-15"]},
            {"id": "scholarship", "userQuestion": "国家奖学金评选办法", "expectedDocumentKeys": ["handbook-v2:doc-28"]},
        ]
        for item in cases:
            with self.subTest(case_id=item["id"]):
                results = self.service.search(StructuredSearchRequest(
                    query=item["userQuestion"],
                    top_k=8,
                    document_limit=6,
                    include_neighbors=True,
                ))
                document_keys = {result.canonical_key for result in results}
                self.assertTrue(
                    document_keys.intersection(item["expectedDocumentKeys"]),
                    f"expected {item['expectedDocumentKeys']}, got {sorted(document_keys)}",
                )
                diagnostics = self.service.last_search_diagnostics
                expected = set(item["expectedDocumentKeys"])
                self.assertTrue(any(row["canonicalKey"] in expected for row in diagnostics["candidates"]))
                self.assertTrue(any(row["canonicalKey"] in expected for row in diagnostics["finalCandidates"]))
                self.assertFalse(any("content" in row for row in diagnostics["candidates"]))


if __name__ == "__main__":
    unittest.main()
