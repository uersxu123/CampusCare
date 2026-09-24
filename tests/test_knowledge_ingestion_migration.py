import tempfile
import unittest
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def alembic_config(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


class KnowledgeIngestionMigrationTests(unittest.TestCase):
    def test_0012_round_trip_backfills_legacy_without_rechunking_or_deleting_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{Path(directory) / 'ingestion-v2.sqlite'}"
            config = alembic_config(url)
            command.upgrade(config, "0011_clarification_retry")
            engine = create_engine(url)
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "INSERT INTO knowledge_documents "
                        "(source_key,canonical_key,managed_by,title,source_type,domain,tags_json,site,status,version,content_hash,created_at,updated_at) "
                        "VALUES ('legacy','legacy','LEGACY','旧文档','INTERNAL_GUIDANCE','ACADEMIC','[]','ALL','ACTIVE','1','doc-hash',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                    )
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO knowledge_chunks (document_id,source,source_index,content,content_hash,created_at) "
                        "VALUES (1,'legacy',0,'旧知识正文','chunk-hash',CURRENT_TIMESTAMP)"
                    )
                )

            command.upgrade(config, "0012_knowledge_ingestion_v2")
            inspector = sa.inspect(engine)
            self.assertTrue({"knowledge_artifacts", "knowledge_elements", "knowledge_tables"}.issubset(inspector.get_table_names()))
            with engine.connect() as connection:
                document = connection.execute(
                    sa.text("SELECT ingestion_status,parser_profile,chunking_profile FROM knowledge_documents WHERE id=1")
                ).one()
                chunk = connection.execute(
                    sa.text("SELECT content,chunk_kind FROM knowledge_chunks WHERE id=1")
                ).one()
            self.assertEqual(document, ("READY", "legacy", "legacy_char_v1"))
            self.assertEqual(chunk, ("旧知识正文", "TEXT_CHILD"))

            command.downgrade(config, "0011_clarification_retry")
            with engine.connect() as connection:
                self.assertEqual(connection.execute(sa.text("SELECT content FROM knowledge_chunks WHERE id=1")).scalar_one(), "旧知识正文")
                self.assertEqual(connection.execute(sa.text("SELECT title FROM knowledge_documents WHERE id=1")).scalar_one(), "旧文档")
            self.assertNotIn("knowledge_artifacts", sa.inspect(engine).get_table_names())
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
