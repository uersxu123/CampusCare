import json
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.entities import (
    KnowledgeArtifact,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeElement,
    KnowledgeIndexRegistry,
)
from app.services.knowledge_ingestion.cutover import build_cutover_report
from app.services.knowledge_ingestion.cutover import find_unmanaged_markdown
from app.core.config import Settings


class KnowledgeCutoverAuditTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _migrated_upload(self, *, canonical_key="guide", version="1", status="ACTIVE", source_suffix="primary"):
        document = KnowledgeDocument(
            source_key=f"upload:{canonical_key}:{version}:{source_suffix}",
            canonical_key=canonical_key,
            managed_by="UPLOAD",
            title="指南",
            source_type="UPLOAD",
            domain="CAMPUS_SERVICE",
            tags_json="[]",
            site="ALL",
            status=status,
            version=version,
            content_hash="a" * 64,
            ingestion_status="READY",
            parser_profile="auto",
            parser_version="markdown-ast-1",
            chunking_profile="structure_token_v2",
        )
        self.db.add(document)
        self.db.flush()
        artifact = KnowledgeArtifact(
            document_id=document.id,
            storage_key=f"sha256/aa/{canonical_key}",
            original_filename="guide.md",
            mime_type="text/markdown",
            byte_size=10,
            sha256="b" * 64,
        )
        self.db.add(artifact)
        self.db.flush()
        element = KnowledgeElement(
            document_id=document.id,
            artifact_id=artifact.id,
            element_index=0,
            element_type="PARAGRAPH",
            content="可追踪正文",
            content_hash="c" * 64,
            parser_name="markdown_ast",
            parser_version="markdown-ast-1",
        )
        self.db.add(element)
        self.db.flush()
        self.db.add(
            KnowledgeChunk(
                document_id=document.id,
                source=document.source_key,
                source_index=0,
                content="可追踪正文",
                content_hash="d" * 64,
                chunk_kind="TEXT_CHILD",
                element_ids_json=json.dumps([element.id]),
                chunking_profile="structure_token_v2",
            )
        )
        self.db.flush()
        return document

    def test_cutover_is_eligible_only_when_all_gates_pass(self):
        self._migrated_upload()
        self.db.add(
            KnowledgeIndexRegistry(
                logical_name="mindbridge_knowledge_v3",
                state="ACTIVE",
                active_collection="active",
                previous_collection="previous",
                metadata_json=json.dumps({"rolledBackAt": "2026-08-03T00:00:00"}),
            )
        )
        self.db.commit()

        report = build_cutover_report(self.db, unmanaged_markdown=())
        self.assertTrue(report["eligibleToDisableLegacyBootstrap"])
        self.assertTrue(all(item["passed"] for item in report["checks"].values()))

    def test_cutover_reports_legacy_upload_duplicates_traceability_and_missing_rollback(self):
        first = self._migrated_upload(canonical_key="duplicate")
        second = self._migrated_upload(canonical_key="duplicate", source_suffix="copy")
        first.parser_profile = "legacy"
        first.chunking_profile = "legacy_char_v1"
        first.chunks[0].element_ids_json = "[]"
        self.db.commit()

        report = build_cutover_report(self.db, unmanaged_markdown=("app/knowledge/unmanaged.md",))
        self.assertFalse(report["eligibleToDisableLegacyBootstrap"])
        self.assertEqual(report["checks"]["manifestRegistration"]["unmanagedCount"], 1)
        self.assertEqual(report["checks"]["adminUploadsMigrated"]["documentIds"], [first.id])
        self.assertEqual(report["checks"]["activeTraceability"]["documentIds"], [first.id])
        self.assertEqual(len(report["checks"]["duplicateActiveCanonicalVersions"]["groups"]), 1)
        self.assertFalse(report["checks"]["rollbackRehearsal"]["passed"])

    def test_current_manifest_selects_verified_prechunked_corpus_not_legacy_markdown(self):
        import yaml
        from app.services.handbook_import import load_corpus
        settings = Settings(_env_file=None)
        root = settings.project_root
        manifest = yaml.safe_load((root / "app/knowledge/knowledge_manifest.yaml").read_text(encoding="utf-8"))
        self.assertEqual(manifest["corpus"], "handbook_prechunked_v2")
        self.assertEqual(manifest["documents"], [])
        parents, children = load_corpus(root / manifest["corpus_path"])
        self.assertTrue(parents and children)
        # 旧 Markdown 仍保留作历史文件，旧 cutover 检查不得被伪造为通过。
        unmanaged = find_unmanaged_markdown(settings)
        self.assertTrue(unmanaged)
        self.assertTrue(all((root / name).is_file() for name in unmanaged))


if __name__ == "__main__":
    unittest.main()
