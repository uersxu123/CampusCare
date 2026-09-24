import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import Base
from app.models.entities import KnowledgeChunk, KnowledgeDocument, KnowledgeIndexRegistry
from app.services.embedding import OllamaEmbeddingBackend
from app.services.knowledge_index import KnowledgeIndexManager
from app.services.vector_store import (
    VectorQueryFilter,
    VectorSearchHit,
    VectorStoreUnavailable,
    build_chroma_where,
    searchable_text_for_chunk,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload




class FakeHttpClient:
    def __init__(self, *, get_payload=None, post_payload=None, get_error=None):
        self.get_payload = get_payload
        self.post_payload = post_payload
        self.get_error = get_error
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if self.get_error is not None:
            raise self.get_error
        return FakeResponse(self.get_payload or {})

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return FakeResponse(self.post_payload or {})


class FakeEmbeddingBackend:
    name = "fake"
    model = "fake-model"
    digest_resolved = True

    def __init__(self, digest="digest-v1", fail=False):
        self.digest = digest
        self.fail = fail

    def available(self):
        return True

    def model_digest(self):
        return self.digest

    def embed_documents(self, texts):
        if self.fail:
            raise RuntimeError("injected embedding failure")
        return [[float(len(text)), 1.0] for text in texts]

    def embed_query(self, text):
        return self.embed_documents([text])[0]


class FakeStore:
    collections = {}

    def __init__(self, settings, collection_name, *, create=False, collection_metadata=None):
        self.collection_name = collection_name
        if create:
            if collection_name in self.collections:
                raise VectorStoreUnavailable("collection exists")
            self.collections[collection_name] = {
                "metadata": dict(collection_metadata or {}),
                "rows": {},
            }
        if collection_name not in self.collections:
            raise VectorStoreUnavailable("collection missing")
        self.data = self.collections[collection_name]
        self.can_query = True

    @property
    def metadata(self):
        return self.data["metadata"]

    def upsert_chunks(self, chunks, embeddings, searchable_texts=None):
        for chunk, embedding, text in zip(chunks, embeddings, searchable_texts or []):
            self.data["rows"][chunk.id] = (chunk, embedding, text)
        return len(chunks)

    def query(self, embedding, top_k, filters):
        hits = []
        for chunk, _vector, text in self.data["rows"].values():
            document = chunk.document
            if filters.statuses and document.status not in filters.statuses:
                continue
            hits.append(
                VectorSearchHit(
                    chunk.id,
                    chunk.source,
                    chunk.source_index,
                    text,
                    0.9,
                    chunk.document_id,
                )
            )
        return hits[:top_k]

    def count(self):
        return len(self.data["rows"])

    def delete_collection(self):
        self.collections.pop(self.collection_name, None)


class EmbeddingBackendTests(unittest.TestCase):
    def test_ollama_uses_embed_endpoint_and_records_real_digest(self):
        settings = Settings(
            _env_file=None,
            knowledge_embedding_provider="ollama",
            knowledge_embedding_model="bge-m3:latest",
            knowledge_embedding_base_url="http://ollama.test",
        )
        client = FakeHttpClient(
            get_payload={"models": [{"name": "bge-m3:latest", "digest": "sha256:real"}]},
            post_payload={"embeddings": [[0.1, 0.2], [0.3, 0.4]]},
        )
        backend = OllamaEmbeddingBackend(settings, client=client)
        vectors = backend.embed_documents(["甲", "乙"])
        self.assertEqual(vectors, [[0.1, 0.2], [0.3, 0.4]])
        self.assertEqual(backend.model_digest(), "sha256:real")
        self.assertEqual(client.post_calls[0][0], "http://ollama.test/api/embed")
        self.assertEqual(client.post_calls[0][1]["json"]["model"], "bge-m3:latest")
        self.assertEqual(client.get_calls[0][0], "http://ollama.test/api/tags")

    def test_unresolved_ollama_digest_is_explicit_and_stable(self):
        settings = Settings(_env_file=None, knowledge_embedding_base_url="http://ollama.test")
        error = httpx.ConnectError("offline", request=httpx.Request("GET", "http://ollama.test/api/tags"))
        client = FakeHttpClient(get_error=error)
        backend = OllamaEmbeddingBackend(settings, client=client)
        digest = backend.model_digest()
        self.assertTrue(digest.startswith("unresolved:"))
        self.assertEqual(backend.model_digest(), digest)
        self.assertFalse(backend.digest_resolved)


class VectorMetadataTests(unittest.TestCase):
    def test_chroma_filter_is_built_before_query(self):
        where = build_chroma_where(
            VectorQueryFilter(
                domains=("ACADEMIC",),
                sites=("ALL", "NANWANGSHAN"),
                statuses=("ACTIVE",),
                source_types=("OFFICIAL_POLICY",),
                document_ids=(1, 2),
            )
        )
        self.assertEqual(where["$and"][0], {"domain": "ACADEMIC"})
        self.assertIn({"site": {"$in": ["ALL", "NANWANGSHAN"]}}, where["$and"])
        self.assertIn({"document_id": {"$in": [1, 2]}}, where["$and"])

    def test_searchable_text_contains_all_fields(self):
        document = KnowledgeDocument(
            source_key="guide",
            title="住宿服务指南",
            source_type="OFFICIAL_SERVICE_GUIDE",
            domain="CAMPUS_SERVICE",
            tags_json='["调宿"]',
            site="ALL",
            status="ACTIVE",
            version="1",
            content_hash="doc",
        )
        chunk = KnowledgeChunk(source="guide", source_index=0, section_title="调宿流程", content="填写申请表")
        chunk.document = document
        text = searchable_text_for_chunk(chunk)
        self.assertIn("文档标题: 住宿服务指南", text)
        self.assertIn("章节标题: 调宿流程", text)
        self.assertIn("标签: 调宿", text)
        self.assertIn("正文: 填写申请表", text)


class KnowledgeIndexManagerTests(unittest.TestCase):
    def setUp(self):
        FakeStore.collections = {}
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        now = datetime.now(UTC).replace(tzinfo=None)
        document = KnowledgeDocument(
            source_key="housing",
            canonical_key="housing",
            managed_by="MANIFEST",
            title="住宿服务指南",
            source_type="OFFICIAL_SERVICE_GUIDE",
            domain="CAMPUS_SERVICE",
            tags_json='["调宿"]',
            site="ALL",
            status="ACTIVE",
            verified_at=now,
            version="1",
            content_hash="document-hash",
        )
        self.db.add(document)
        self.db.flush()
        self.db.add(
            KnowledgeChunk(
                document_id=document.id,
                source="housing",
                source_index=0,
                section_title="调宿办理流程",
                content="填写宿舍调整申请表。",
                content_hash="chunk-hash",
            )
        )
        self.db.commit()
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            _env_file=None,
            knowledge_vector_enabled=True,
            knowledge_vector_collection_base="test_v3",
            chroma_persist_dir=str(Path(self.temp.name) / "chroma"),
        )

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    def manager(self, backend):
        return KnowledgeIndexManager(self.db, self.settings, backend=backend, store_factory=FakeStore)

    def test_build_stops_at_ready_until_shadow_then_activate_and_rollback(self):
        first = self.manager(FakeEmbeddingBackend("digest-v1"))
        ready = first.build()
        registry = self.db.query(KnowledgeIndexRegistry).one()
        self.assertEqual(ready.state, "READY")
        self.assertIsNone(registry.active_collection)
        self.assertEqual(registry.ready_collection, ready.collection)
        with self.assertRaisesRegex(RuntimeError, "shadow"):
            first.activate()
        first.shadow(["调宿申请"])
        first.activate()
        self.db.refresh(registry)
        self.assertEqual(registry.active_collection, ready.collection)

        second = self.manager(FakeEmbeddingBackend("digest-v2"))
        next_ready = second.build()
        self.assertNotEqual(next_ready.signature, ready.signature)
        second.shadow(["调宿申请"])
        second.activate()
        self.db.refresh(registry)
        self.assertEqual(registry.previous_collection, ready.collection)
        second.rollback()
        self.db.refresh(registry)
        self.assertEqual(registry.active_collection, ready.collection)

    def test_failed_build_preserves_active_pointer(self):
        manager = self.manager(FakeEmbeddingBackend())
        ready = manager.build()
        manager.shadow(["调宿申请"])
        manager.activate()
        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.manager(FakeEmbeddingBackend("digest-v2", fail=True)).build()
        registry = self.db.query(KnowledgeIndexRegistry).one()
        self.assertEqual(registry.state, "FAILED")
        self.assertEqual(registry.active_collection, ready.collection)
        self.assertIn("injected embedding failure", json.loads(registry.metadata_json)["lastError"])


if __name__ == "__main__":
    unittest.main()
