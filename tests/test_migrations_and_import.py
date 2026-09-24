import re
import tempfile
import unittest
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import Base
from app.models.entities import AgentRunTrace, KnowledgeChunk, KnowledgeDocument, UserMemory
from app.services.knowledge_import import KnowledgeManifestImporter


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def alembic_config(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


class MigrationTests(unittest.TestCase):
    @staticmethod
    def _insert_clarification(connection, *, public_id: str, task_kind: str, status: str = "RESOLVED"):
        connection.execute(sa.text(
            "INSERT INTO user_accounts (username, display_name, password_hash, roles_csv, created_at) "
            "VALUES (:username, '用户', 'x', 'ROLE_USER', CURRENT_TIMESTAMP)"
        ), {"username": f"user-{public_id}"})
        user_id = connection.execute(sa.text("SELECT MAX(id) FROM user_accounts")).scalar_one()
        connection.execute(sa.text(
            "INSERT INTO chat_sessions (public_id, title, user_id, archived_at, created_at, updated_at) "
            "VALUES (:session, '测试', :user_id, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ), {"session": f"session-{public_id}", "user_id": user_id})
        session_id = connection.execute(sa.text("SELECT MAX(id) FROM chat_sessions")).scalar_one()
        connection.execute(sa.text(
            "INSERT INTO pending_clarifications "
            "(public_id,user_id,session_id,status,task_kind,original_message,known_arguments_json,missing_arguments_json,"
            "approved_question,round_count,max_rounds,expires_at,version,created_at,updated_at,resume_context_json,no_progress_count,finish_reason) "
            "VALUES (:public_id,:user_id,:session_id,:status,:task_kind,'用户原文',:known,:missing,"
            "'请补充日期',1,3,'2099-01-01 00:00:00',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,:resume,0,'')"
        ), {"public_id": public_id, "user_id": user_id, "session_id": session_id, "status": status,
             "task_kind": task_kind, "known": '{"course":"数学"}', "missing": '[{"name":"deadline"}]',
             "resume": '{"schemaVersion":2}'})

    def test_five_intent_cutover_backfills_every_legacy_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = f"sqlite:///{Path(tmp) / 'route-v3-values.sqlite'}"
            config = alembic_config(url)
            command.upgrade(config, "0013_specialist_trace_contract")
            engine = create_engine(url)
            values = [
                ("study_plan", "ACADEMIC"), ("knowledge_scope", "CAMPUS"),
                ("CHAT:GENERAL_CHAT", "CHAT"), ("ACADEMIC:STUDY_PLAN", "ACADEMIC"),
                ("ACADEMIC:CAREER_DECISION", "ACADEMIC"), ("CAMPUS:INSTITUTIONAL_FACT", "CAMPUS"),
                ("MENTAL:EMOTIONAL_SUPPORT", "MENTAL"), ("RISK:HIGH_RISK_SUPPORT", "RISK"),
            ]
            with engine.begin() as connection:
                for index, (legacy, _) in enumerate(values):
                    self._insert_clarification(connection, public_id=f"row-{index}", task_kind=legacy)
            command.upgrade(config, "head")
            with engine.connect() as connection:
                actual = connection.execute(sa.text("SELECT intent FROM pending_clarifications ORDER BY id")).scalars().all()
            self.assertEqual(actual, [expected for _, expected in values])
            self.assertNotIn("task_kind", {column["name"] for column in sa.inspect(engine).get_columns("pending_clarifications")})
            engine.dispose()

    def test_five_intent_cutover_rejects_invalid_value_before_schema_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = f"sqlite:///{Path(tmp) / 'route-v3-invalid.sqlite'}"
            config = alembic_config(url)
            command.upgrade(config, "0013_specialist_trace_contract")
            engine = create_engine(url)
            with engine.begin() as connection:
                self._insert_clarification(connection, public_id="invalid", task_kind="UNKNOWN:VALUE")
            with self.assertRaises(RuntimeError):
                command.upgrade(config, "head")
            columns = {column["name"] for column in sa.inspect(engine).get_columns("pending_clarifications")}
            self.assertIn("task_kind", columns)
            self.assertNotIn("intent", columns)
            engine.dispose()

    def test_five_intent_cutover_interrupts_waiting_state_and_erases_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = f"sqlite:///{Path(tmp) / 'route-v3-waiting.sqlite'}"
            config = alembic_config(url)
            command.upgrade(config, "0013_specialist_trace_contract")
            engine = create_engine(url)
            with engine.begin() as connection:
                self._insert_clarification(connection, public_id="waiting", task_kind="ACADEMIC:STUDY_PLAN", status="WAITING_USER")
            command.upgrade(config, "head")
            with engine.connect() as connection:
                row = connection.execute(sa.text(
                    "SELECT intent,status,finish_reason,resume_context_json,known_arguments_json,missing_arguments_json,original_message,approved_question "
                    "FROM pending_clarifications"
                )).one()
            self.assertEqual(row, (
                "ACADEMIC", "INTERRUPTED", "ROUTE_SCHEMA_V3_CUTOVER",
                '{"schemaVersion":3,"cutoverReason":"ROUTE_SCHEMA_V3_CUTOVER"}', "{}", "[]", "", "",
            ))
            engine.dispose()

    def test_empty_database_upgrade_and_downgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = f"sqlite:///{Path(tmp) / 'empty.sqlite'}"
            config = alembic_config(url)
            command.upgrade(config, "head")
            engine = create_engine(url)
            names = set(sa.inspect(engine).get_table_names())
            self.assertIn("conversation_summaries", names)
            self.assertIn("knowledge_documents", names)
            chat_columns = {column["name"] for column in sa.inspect(engine).get_columns("chat_sessions")}
            self.assertIn("archived_at", chat_columns)
            clarification_columns = {
                column["name"] for column in sa.inspect(engine).get_columns("pending_clarifications")
            }
            self.assertTrue({"no_progress_count", "finish_reason"}.issubset(clarification_columns))
            command.downgrade(config, "base")
            self.assertEqual(set(sa.inspect(engine).get_table_names()), {"alembic_version"})
            engine.dispose()

    def test_context_memory_v2_upgrade_downgrade_round_trip_preserves_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = f"sqlite:///{Path(tmp) / 'context-v2.sqlite'}"
            config = alembic_config(url)
            command.upgrade(config, "0006_context_reconnect")
            engine = create_engine(url)
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "INSERT INTO user_accounts "
                        "(username, display_name, password_hash, roles_csv, created_at) "
                        "VALUES ('memory-user', 'Memory User', 'x', 'ROLE_USER', CURRENT_TIMESTAMP)"
                    )
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO chat_sessions "
                        "(public_id, title, user_id, archived_at, created_at, updated_at) "
                        "VALUES ('context-v2', 'Context V2', 1, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO user_memories "
                        "(public_id, user_id, category, content, confidence, status, created_at, updated_at) "
                        "VALUES ('legacy-memory', 1, 'EXPLICIT', '历史记忆', 1, 'ACTIVE', "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO agent_run_traces "
                        "(user_id, session_id, intent, risk_level, original_input, sanitized_input, "
                        "memory_brief, agent_steps_json, retrieved_knowledge_json, response_messages_json, "
                        "assessment_json, created_at) "
                        "VALUES (1, 1, 'CHAT', 'LOW', '输入', '输入', '', '[]', '[]', '[]', '{}', "
                        "CURRENT_TIMESTAMP)"
                    )
                )

            command.upgrade(config, "head")
            inspector = sa.inspect(engine)
            memory_columns = {column["name"] for column in inspector.get_columns("user_memories")}
            trace_columns = {column["name"] for column in inspector.get_columns("agent_run_traces")}
            self.assertTrue({"memory_key", "last_used_at"}.issubset(memory_columns))
            self.assertIn("context_manifest_json", trace_columns)
            self.assertTrue({"evidence_items_json", "tool_diagnostics_json"}.issubset(trace_columns))
            self.assertNotIn("retrieved_knowledge_json", trace_columns)
            self.assertNotIn("retrieval_diagnostics_json", trace_columns)
            with engine.connect() as connection:
                self.assertEqual(
                    connection.execute(
                        sa.text("SELECT context_manifest_json FROM agent_run_traces")
                    ).scalar_one(),
                    "{}",
                )
                self.assertEqual(
                    connection.execute(sa.text("SELECT evidence_items_json FROM agent_run_traces")).scalar_one(),
                    "[]",
                )
                self.assertEqual(
                    connection.execute(sa.text("SELECT content FROM user_memories")).scalar_one(),
                    "历史记忆",
                )

            command.downgrade(config, "0006_context_reconnect")
            self.assertNotIn(
                "memory_key",
                {column["name"] for column in sa.inspect(engine).get_columns("user_memories")},
            )
            command.upgrade(config, "head")
            with engine.connect() as connection:
                self.assertEqual(
                    connection.execute(sa.text("SELECT content FROM user_memories")).scalar_one(),
                    "历史记忆",
                )
            engine.dispose()

    def test_orm_v2_fields_allow_legacy_null_memory_key(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            user = UserMemory(
                public_id="legacy-null-key",
                user_id=1,
                category="EXPLICIT",
                memory_key=None,
                content="历史内容",
            )
            trace = AgentRunTrace(
                user_id=1,
                session_id=1,
                intent="CHAT",
                risk_level="LOW",
                original_input="输入",
                sanitized_input="输入",
            )
            db.add_all([user, trace])
            db.commit()
            self.assertIsNone(user.memory_key)
            self.assertEqual(trace.context_manifest_json, "{}")
        engine.dispose()

    def test_legacy_chunks_are_migrated_without_orphans(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = f"sqlite:///{Path(tmp) / 'legacy.sqlite'}"
            config = alembic_config(url)
            command.upgrade(config, "0001_current_schema_baseline")
            engine = create_engine(url)
            with engine.begin() as connection:
                connection.execute(sa.text("INSERT INTO knowledge_chunks (source, source_index, content, created_at) VALUES ('old.md', 0, '旧知识正文', CURRENT_TIMESTAMP)"))
            command.upgrade(config, "head")
            with engine.connect() as connection:
                chunks = connection.execute(sa.text("SELECT COUNT(*) FROM knowledge_chunks")).scalar_one()
                documents = connection.execute(sa.text("SELECT COUNT(*) FROM knowledge_documents WHERE source_key LIKE 'legacy-%'")).scalar_one()
                orphans = connection.execute(sa.text("SELECT COUNT(*) FROM knowledge_chunks WHERE document_id IS NULL")).scalar_one()
            self.assertEqual((chunks, documents, orphans), (1, 1, 0))
            engine.dispose()

    def test_existing_sessions_remain_active_after_archive_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = f"sqlite:///{Path(tmp) / 'archive.sqlite'}"
            config = alembic_config(url)
            command.upgrade(config, "0004_migrate_legacy_knowledge")
            engine = create_engine(url)
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "INSERT INTO user_accounts "
                        "(username, display_name, password_hash, roles_csv, created_at) "
                        "VALUES ('archive-user', 'Archive User', 'x', 'ROLE_USER', CURRENT_TIMESTAMP)"
                    )
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO chat_sessions "
                        "(public_id, title, user_id, created_at, updated_at) "
                        "VALUES ('before-archive', 'Existing session', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )
            command.upgrade(config, "head")
            with engine.connect() as connection:
                archived_at = connection.execute(
                    sa.text("SELECT archived_at FROM chat_sessions WHERE public_id = 'before-archive'")
                ).scalar_one()
            self.assertIsNone(archived_at)
            engine.dispose()


class ManifestImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(cls.engine)
        cls.db = Session(cls.engine, autoflush=False)
        cls.settings = Settings(
            _env_file=None,
            database_url="sqlite+pysqlite:///:memory:",
            knowledge_vector_enabled=False,
        )
        cls.importer = KnowledgeManifestImporter(cls.db, cls.settings)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.engine.dispose()

    def test_current_corpus_hashes_page_mapping_and_idempotent_import(self):
        from app.services.handbook_import import load_corpus, merge_children
        parents, children = merge_children(*load_corpus(PROJECT_ROOT / "app/knowledge/handbook_v2"))
        first = self.importer.import_all(rebuild_index=False)
        before_ids = [(row.id, row.content_hash) for row in self.db.query(KnowledgeChunk).order_by(KnowledgeChunk.id)]
        second = self.importer.import_all(rebuild_index=False)
        self.assertEqual((first["documents"], first["parents"], first["children"]), (49, 272, 855))
        self.assertEqual(second["changedDocuments"], 0)
        self.assertEqual(before_ids, [(row.id, row.content_hash) for row in self.db.query(KnowledgeChunk).order_by(KnowledgeChunk.id)])
        self.assertEqual(self.db.query(KnowledgeDocument).filter_by(status="ACTIVE").count(), 49)
        self.assertEqual(self.db.query(KnowledgeChunk).filter(KnowledgeChunk.document_id.is_(None)).count(), 0)
        records = self.db.query(KnowledgeChunk).all()
        self.assertEqual(len(records), len(parents) + len(children))
        # 相同正文可能出现在不同页，不能把 content_hash 当作唯一来源身份。
        by_source = {(row.source, row.source_index): row for row in records}
        source_indexes = {}
        for source in parents + children:
            document_id = source["document_id"]
            source_index = source_indexes.get(document_id, 0)
            source_indexes[document_id] = source_index + 1
            imported = by_source[("handbook-v2:" + document_id, source_index)]
            self.assertEqual(imported.content_hash, source["content_sha256"])
            self.assertEqual(imported.content, source["text"])
            self.assertEqual(imported.page_number, next(iter(source.get("pdf_pages", [])), None))
            if "chunk_id" in source and source["parent_id"] is not None:
                parent = self.db.get(KnowledgeChunk, imported.parent_chunk_id)
                self.assertEqual(parent.chunk_kind, "TEXT_PARENT")
                self.assertEqual(parent.document_id, imported.document_id)

    def test_handbook_contact_directory_is_structured_and_complete(self):
        self.importer.import_all(rebuild_index=False)
        chunks = (self.db.query(KnowledgeChunk).join(KnowledgeDocument)
                  .filter(KnowledgeDocument.source_key == "handbook-v2:doc-49",
                          KnowledgeChunk.chunk_kind == "TEXT_CHILD").all())
        from app.services.handbook_import import load_corpus
        _, children = load_corpus(PROJECT_ROOT / "app/knowledge/handbook_v2")
        originals = [row for row in children if row["document_id"] == "doc-49"]
        self.assertTrue(originals)
        self.assertEqual({row.content for row in chunks}, {row["text"] for row in originals})
        self.assertTrue(all(row.page_number == 254 for row in chunks))
        for service, phone, location in (
            ("南望山校区校园110", "67883110", "（原表空白）"),
            ("未来城校区校园110", "65277110", "（原表空白）"),
            ("招生与学籍管理办公室", "67883171", "新峰公寓102"),
            ("心理健康教育中心", "67883678", "心理中心210"),
            ("就业服务管理中心", "67883308", "新峰公寓108"),
            ("学生住宿服务中心", "67886371", "西区56栋后"),
            ("校园网络维护", "67885175", "东区南望厅13/14号窗口"),
            ("医保咨询", "67885891", "东区"),
        ):
            matches = [re.sub(r"\s+", "", row.content) for row in chunks
                       if phone in row.content]
            self.assertEqual(len(matches), 1)
            self.assertIn(service, matches[0])
            self.assertIn("电话：" + phone, matches[0])
            self.assertIn("地点：" + location, matches[0])

    def test_import_does_not_invent_phone_numbers(self):
        from app.services.handbook_import import load_corpus
        self.importer.import_all(rebuild_index=False)
        parents, children = load_corpus(PROJECT_ROOT / "app/knowledge/handbook_v2")
        source_numbers = set(re.findall(r"\d{8}", "\n".join(row["text"] for row in parents + children)))
        contents = "\n".join(row.content for row in self.db.query(KnowledgeChunk)
                             .join(KnowledgeDocument).filter(KnowledgeDocument.status == "ACTIVE"))
        imported_numbers = set(re.findall(r"\d{8}", contents))
        self.assertTrue(source_numbers)
        self.assertEqual(imported_numbers, source_numbers)


if __name__ == "__main__":
    unittest.main()
