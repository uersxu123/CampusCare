import json
import tempfile
import unittest
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import Base
from app.models.entities import KnowledgeChunk, KnowledgeDocument
from app.services.knowledge import KnowledgeService, StructuredSearchRequest
from app.services.knowledge_ingestion.chunker import ChunkProfile, chunk_document
from app.services.knowledge_ingestion.models import DocumentElement, ParsedDocument


class CharacterTokenCounter:
    name = "fake-char"
    version = "1"

    def count(self, text: str) -> int:
        return len("".join(text.split()))


class StructureChunkerTests(unittest.TestCase):
    def setUp(self):
        self.profile = ChunkProfile(
            name="structure_token_v2",
            child_target_tokens=12,
            child_max_tokens=18,
            child_min_tokens=4,
            parent_max_tokens=48,
            overlap_tokens=5,
        )
        self.counter = CharacterTokenCounter()

    def test_chunks_respect_top_heading_full_units_and_token_limit(self):
        parsed = ParsedDocument(
            "制度",
            (
                DocumentElement(0, "HEADING", "第一章", heading_path=("第一章",)),
                DocumentElement(1, "PARAGRAPH", "第一句完整。第二句完整。", heading_path=("第一章",)),
                DocumentElement(2, "LIST_ITEM", "1. 完整列表项目", heading_path=("第一章",)),
                DocumentElement(3, "HEADING", "第二章", heading_path=("第二章",)),
                DocumentElement(4, "PARAGRAPH", "第二章正文。", heading_path=("第二章",)),
            ),
            "fake",
            "1",
        )
        plan = chunk_document(parsed, self.profile, self.counter)
        self.assertTrue(plan.children)
        self.assertTrue(all(item.token_count <= self.profile.child_max_tokens for item in plan.children))
        self.assertTrue(all(len({path[0] for path in item.heading_paths if path}) <= 1 for item in plan.children))
        self.assertTrue(any("完整列表项目" in item.content for item in plan.children))
        self.assertFalse(any("完整列表项" in item.content and "目" not in item.content for item in plan.children))
        self.assertTrue(all(item.parent_index is not None for item in plan.children))

    def test_overlap_reuses_complete_sentence_not_arbitrary_tail(self):
        parsed = ParsedDocument(
            "说明",
            (DocumentElement(0, "PARAGRAPH", "甲乙丙丁。戊己庚辛。壬癸子丑。", heading_path=("章节",)),),
            "fake",
            "1",
        )
        plan = chunk_document(parsed, self.profile, self.counter)
        self.assertGreaterEqual(len(plan.children), 2)
        previous_sentences = {item.strip() for item in plan.children[0].content.split("。") if item.strip()}
        next_sentences = {item.strip() for item in plan.children[1].content.split("。") if item.strip()}
        self.assertTrue(previous_sentences.intersection(next_sentences))

    def test_no_space_long_text_uses_explicit_bounded_fallback(self):
        parsed = ParsedDocument(
            "长文",
            (DocumentElement(0, "PARAGRAPH", "长" * 55, heading_path=("长章节",)),),
            "fake",
            "1",
        )
        plan = chunk_document(parsed, self.profile, self.counter)
        self.assertTrue(all(item.token_count <= self.profile.child_max_tokens for item in plan.children))
        self.assertTrue(any(item.fallback for item in plan.children))


class ParentRetrievalTests(unittest.TestCase):
    def test_child_ranking_expands_and_deduplicates_parent_context(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            document = KnowledgeDocument(
                source_key="v2",
                canonical_key="v2",
                managed_by="UPLOAD",
                title="V2 文档",
                source_type="OFFICIAL_POLICY",
                domain="ACADEMIC",
                tags_json="[]",
                site="ALL",
                status="ACTIVE",
                verified_at=datetime.now(UTC).replace(tzinfo=None),
                version="1",
                content_hash="doc",
                ingestion_status="READY",
                parser_profile="plaintext",
                chunking_profile="structure_token_v2",
            )
            db.add(document)
            db.flush()
            parent = KnowledgeChunk(
                document_id=document.id,
                source="v2",
                source_index=0,
                content="父块完整上下文：申请材料和办理步骤。",
                content_hash="parent",
                chunk_kind="TEXT_PARENT",
                chunking_profile="structure_token_v2",
                element_ids_json=json.dumps([1, 2]),
            )
            db.add(parent)
            db.flush()
            db.add_all(
                [
                    KnowledgeChunk(document_id=document.id, source="v2", source_index=1, content="申请材料", content_hash="c1", chunk_kind="TEXT_CHILD", parent_chunk_id=parent.id, chunking_profile="structure_token_v2"),
                    KnowledgeChunk(document_id=document.id, source="v2", source_index=2, content="办理步骤", content_hash="c2", chunk_kind="TEXT_CHILD", parent_chunk_id=parent.id, chunking_profile="structure_token_v2"),
                ]
            )
            db.commit()
            service = KnowledgeService(
                db,
                Settings(_env_file=None, knowledge_vector_enabled=False),
            )
            results = service.search(StructuredSearchRequest(query="申请材料办理步骤", top_k=4, document_limit=4))
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].content, parent.content)
            self.assertEqual(set(results[0].provenance.child_chunk_ids), {parent.id + 1, parent.id + 2})
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
