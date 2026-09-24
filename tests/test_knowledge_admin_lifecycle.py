import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import ingest_knowledge, router
from app.core.config import Settings
from app.core.database import Base, get_db
from app.core.security import hash_password
from app.models.entities import KnowledgeChunk, KnowledgeDocument, KnowledgeIngestionJob, UserAccount
from app.schemas.dtos import KnowledgeIngestRequest
from app.services.knowledge_ingestion.admin import KnowledgeAdminService


class KnowledgeAdminLifecycleTests(unittest.TestCase):
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
            knowledge_ingestion_worker_enabled=False,
        )
        self.service = KnowledgeAdminService(self.db, self.settings)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    def _enqueue_and_run(self):
        job = self.service.enqueue_document(
            filename="学生手册.md",
            data="# 第一章\n\n奖学金申请材料。".encode("utf-8"),
            mime_type="text/markdown",
            title="学生手册",
            canonical_key="student-handbook",
            domain="CAMPUS_SERVICE",
            tags=("奖学金",),
            site="ALL",
            version="2026",
            parser_profile="auto",
            chunking_profile="structure_token_v2",
            verified_at=None,
            expires_at=None,
        )
        self.assertEqual(job.status, "PENDING")
        return self.service.run_job(job.id)

    def test_database_job_drives_document_detail_and_preview(self):
        job = self._enqueue_and_run()
        self.assertEqual(job.status, "SUCCEEDED")
        self.assertIsNotNone(job.document_id)

        listed = self.service.list_documents()
        self.assertEqual(listed[0]["canonicalKey"], "student-handbook")
        detail = self.service.get_document(job.document_id)
        self.assertEqual(detail["ingestionStatus"], "READY")
        preview = self.service.preview_document(job.document_id)
        self.assertEqual(preview["parser"]["profile"], "auto")
        self.assertGreaterEqual(preview["counts"]["elements"], 1)
        self.assertGreaterEqual(preview["counts"]["chunks"], 1)
        self.assertEqual(preview["chunks"][0]["headingPath"], ["第一章"])
        self.assertIn("index", preview)

    def test_publish_deactivate_and_metadata_edit_require_ready_document(self):
        job = self._enqueue_and_run()
        verified_at = datetime.now(UTC).replace(tzinfo=None)
        updated = self.service.update_metadata(
            job.document_id,
            title="学生手册（审核版）",
            domain="CAMPUS_SERVICE",
            tags=("奖学金", "办事"),
            site="MAIN",
            version="2026.1",
            verified_at=verified_at,
            expires_at=None,
        )
        self.assertEqual(updated["title"], "学生手册（审核版）")
        published = self.service.publish(job.document_id, verified_at=verified_at)
        self.assertEqual(published["status"], "ACTIVE")
        deactivated = self.service.deactivate(job.document_id)
        self.assertEqual(deactivated["status"], "INACTIVE")

    def test_failed_active_reprocess_preserves_serving_revision_and_chunks(self):
        job = self._enqueue_and_run()
        verified_at = datetime.now(UTC).replace(tzinfo=None)
        self.service.publish(job.document_id, verified_at=verified_at)
        document = self.db.get(KnowledgeDocument, job.document_id)
        old_revision = document.active_revision
        old_chunks = [item.content for item in self.db.query(KnowledgeChunk).filter_by(document_id=document.id).all()]

        reprocess = self.service.enqueue_reprocess(
            document.id,
            parser_profile="auto",
            chunking_profile="structure_token_v2",
        )
        with patch("app.services.knowledge_ingestion.admin.KnowledgeIngestionPipeline.ingest_document", side_effect=RuntimeError("正文不应进入任务错误")):
            failed = self.service.run_job(reprocess.id)

        self.db.expire_all()
        preserved = self.db.get(KnowledgeDocument, document.id)
        self.assertEqual(failed.status, "FAILED")
        self.assertNotIn("正文不应进入任务错误", failed.error_message)
        self.assertEqual(preserved.status, "ACTIVE")
        self.assertEqual(preserved.active_revision, old_revision)
        self.assertEqual(
            [item.content for item in self.db.query(KnowledgeChunk).filter_by(document_id=document.id).all()],
            old_chunks,
        )
        retried = self.service.retry_job(failed.id)
        self.assertEqual(retried.status, "PENDING")

    def test_successful_reprocess_increments_revision(self):
        job = self._enqueue_and_run()
        before = self.db.get(KnowledgeDocument, job.document_id).active_revision
        reprocess = self.service.enqueue_reprocess(
            job.document_id,
            parser_profile="auto",
            chunking_profile="structure_token_v2",
        )
        completed = self.service.run_job(reprocess.id)
        self.db.expire_all()
        self.assertEqual(completed.status, "SUCCEEDED")
        self.assertEqual(self.db.get(KnowledgeDocument, job.document_id).active_revision, before)

        rechunk = self.service.enqueue_reprocess(
            job.document_id,
            parser_profile="auto",
            chunking_profile="legacy_char_v1",
        )
        self.assertEqual(self.service.run_job(rechunk.id).status, "SUCCEEDED")
        self.db.expire_all()
        self.assertEqual(self.db.get(KnowledgeDocument, job.document_id).active_revision, before + 1)

    def test_legacy_text_api_keeps_source_and_chunks_response(self):
        admin = UserAccount(username="admin-lifecycle", display_name="管理员", password_hash="unused")
        with patch("app.api.routes.get_settings", return_value=self.settings):
            response = ingest_knowledge(KnowledgeIngestRequest(source="legacy.txt", content="兼容正文"), admin, self.db)
        self.assertEqual(response.model_dump(), {"source": "legacy.txt", "chunks": 1})


class KnowledgeAdminApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        db = self.sessions()
        db.add(
            UserAccount(
                username="knowledge-admin",
                display_name="知识管理员",
                password_hash=hash_password("admin123"),
                roles_csv="ROLE_ADMIN,ROLE_USER",
            )
        )
        db.commit()
        db.close()
        self.settings = Settings(
            _env_file=None,
            database_url="sqlite+pysqlite:///:memory:",
            knowledge_vector_enabled=False,
            knowledge_artifact_dir=self.temp.name,
            knowledge_ingestion_worker_enabled=False,
        )

        def override_db():
            session = self.sessions()
            try:
                yield session
            finally:
                session.close()

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = override_db
        self.settings_patch = patch("app.api.routes.get_settings", return_value=self.settings)
        self.settings_patch.start()
        self.client = TestClient(app)
        self.auth = ("knowledge-admin", "admin123")

    def tearDown(self):
        self.client.close()
        self.settings_patch.stop()
        self.engine.dispose()
        self.temp.cleanup()

    def test_document_job_preview_publish_and_deactivate_apis(self):
        created = self.client.post(
            "/api/admin/knowledge/documents",
            auth=self.auth,
            files={"file": ("指南.md", "# 办事指南\n\n申请正文。".encode("utf-8"), "text/markdown")},
            data={
                "title": "办事指南",
                "canonical_key": "service-guide",
                "domain": "CAMPUS_SERVICE",
                "tags": "申请,材料",
                "site": "ALL",
                "version": "2026",
                "parser_profile": "auto",
                "chunking_profile": "structure_token_v2",
            },
        )
        self.assertEqual(created.status_code, 202)
        job_id = created.json()["id"]
        db = self.sessions()
        KnowledgeAdminService(db, self.settings).run_job(job_id)
        db.close()

        job = self.client.get(f"/api/admin/knowledge/jobs/{job_id}", auth=self.auth)
        self.assertEqual(job.json()["status"], "SUCCEEDED")
        document_id = job.json()["documentId"]
        listed = self.client.get("/api/admin/knowledge/documents", auth=self.auth)
        self.assertEqual(listed.json()["items"][0]["id"], document_id)
        preview = self.client.get(f"/api/admin/knowledge/documents/{document_id}/preview", auth=self.auth)
        self.assertEqual(preview.json()["counts"]["pages"], 0)
        self.assertGreater(preview.json()["counts"]["chunks"], 0)

        verified = datetime.now(UTC).isoformat()
        published = self.client.post(
            f"/api/admin/knowledge/documents/{document_id}/publish",
            auth=self.auth,
            json={"verifiedAt": verified},
        )
        self.assertEqual(published.json()["status"], "ACTIVE")
        deactivated = self.client.post(
            f"/api/admin/knowledge/documents/{document_id}/deactivate",
            auth=self.auth,
        )
        self.assertEqual(deactivated.json()["status"], "INACTIVE")


if __name__ == "__main__":
    unittest.main()
