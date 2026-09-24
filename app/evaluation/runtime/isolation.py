from __future__ import annotations

import tempfile
import uuid
from contextlib import AbstractContextManager
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.database import Base
from app.models.entities import KnowledgeChunk, KnowledgeDocument, KnowledgeIndexRegistry, UserAccount


class EvaluationIsolation(AbstractContextManager):
    def __init__(
        self,
        app_settings: Settings,
        source_db: Session | None = None,
        *,
        require_hybrid_retrieval: bool = True,
        isolation_mode: str = "isolated",
        storage_admin_url: str = "",
    ):
        self._temporary = tempfile.TemporaryDirectory(prefix="mindbridge-evaluation-")
        self.database_path = Path(self._temporary.name) / "evaluation.sqlite3"
        self._admin_engine = None
        self._database_name = ""
        self._redis = None
        use_mysql = isolation_mode == "mysql_isolated"
        if use_mysql:
            if not storage_admin_url:
                raise ValueError("mysql_isolated 需要 EVAL_STORAGE_ADMIN_URL")
            self._database_name = "mindbridge_eval_" + uuid.uuid4().hex
            admin_url = make_url(storage_admin_url).set(database="mysql")
            self._admin_engine = create_engine(admin_url, pool_pre_ping=True)
            with self._admin_engine.begin() as connection:
                connection.execute(text(f"CREATE DATABASE `{self._database_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
            database_url = make_url(storage_admin_url).set(database=self._database_name).render_as_string(
                hide_password=False
            )
            redis_url = _redis_database_url(app_settings.redis_url, 15)
        else:
            database_url = f"sqlite+pysqlite:///{self.database_path.as_posix()}"
            redis_url = app_settings.redis_url
        self.settings = app_settings.model_copy(
            update={
                "database_url": database_url,
                "tool_queue_enabled": False,
                "knowledge_vector_enabled": True,
                "knowledge_vector_required": require_hybrid_retrieval,
                "redis_url": redis_url,
                "redis_memory_enabled": use_mysql,
                "chat_turn_snapshot_enabled": use_mysql,
            }
        )
        engine_kwargs = {"pool_pre_ping": True}
        if not use_mysql:
            engine_kwargs["connect_args"] = {"check_same_thread": False}
        self.engine = create_engine(self.settings.database_url, **engine_kwargs)
        Base.metadata.create_all(self.engine)
        if use_mysql:
            import redis
            self._redis = redis.Redis.from_url(redis_url, socket_timeout=2.0)
            self._redis.flushdb()
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        self.db = self.session_factory()
        if source_db is not None:
            self._copy_active_corpus(source_db)
        self.user = UserAccount(
            username=f"eval-{uuid.uuid4().hex}",
            display_name="评测合成用户",
            password_hash="EVALUATION_ONLY",
        )
        self.user.roles = {"ROLE_USER"}
        self.db.add(self.user)
        self.db.commit()
        self.db.refresh(self.user)

    def __enter__(self) -> EvaluationIsolation:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.db.close()
        self.engine.dispose()
        if self._redis is not None:
            self._redis.flushdb()
            self._redis.close()
        if self._admin_engine is not None and self._database_name:
            with self._admin_engine.begin() as connection:
                connection.execute(text(f"DROP DATABASE `{self._database_name}`"))
            self._admin_engine.dispose()
        self._temporary.cleanup()

    def _copy_active_corpus(self, source: Session) -> None:
        documents = source.query(KnowledgeDocument).filter(KnowledgeDocument.status == "ACTIVE").all()
        document_ids = [item.id for item in documents]
        chunks = (
            source.query(KnowledgeChunk).filter(KnowledgeChunk.document_id.in_(document_ids)).all()
            if document_ids else []
        )
        registries = source.query(KnowledgeIndexRegistry).all()
        self.db.add_all([_clone(item) for item in documents])
        self.db.flush()
        self.db.add_all([_clone(item) for item in chunks])
        self.db.add_all([_clone(item) for item in registries])
        self.db.commit()


def _clone(row):
    values = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    return type(row)(**values)


def _redis_database_url(value: str, database: int) -> str:
    from urllib.parse import urlsplit, urlunsplit
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{database}", parsed.query, parsed.fragment))
