from __future__ import annotations

from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.mysql import LONGTEXT

from app.core.database import Base


def now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class UserAccount(Base):
    __tablename__ = "user_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(128))
    roles_csv: Mapped[str] = mapped_column(String(256), default="ROLE_USER")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    sessions: Mapped[list["ChatSession"]] = relationship(back_populates="user")

    @property
    def roles(self) -> list[str]:
        return [role for role in self.roles_csv.split(",") if role]

    @roles.setter
    def roles(self, value: list[str] | set[str]) -> None:
        self.roles_csv = ",".join(sorted(value))


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (Index("ix_chat_sessions_user_archive_updated", "user_id", "archived_at", "updated_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(160))
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)

    user: Mapped[UserAccount] = relationship(back_populates="sessions")
    messages: Mapped[list["ChatMessage"]] = relationship(back_populates="session", cascade="all, delete-orphan")

    def touch(self) -> None:
        self.updated_at = now()

    @property
    def archived(self) -> bool:
        return self.archived_at is not None


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"))
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"))
    role: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    session: Mapped[ChatSession] = relationship(back_populates="messages")


class ConversationSummary(Base):
    __tablename__ = "conversation_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"), unique=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    covered_until_message_id: Mapped[int] = mapped_column(Integer, default=0)
    summary_json: Mapped[str] = mapped_column(Text, default="{}")
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class PendingClarification(Base):
    __tablename__ = "pending_clarifications"
    __table_args__ = (
        Index("ix_pending_clarifications_session_status", "session_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="WAITING_USER", index=True)
    intent: Mapped[str] = mapped_column(String(16), index=True)
    original_message: Mapped[str] = mapped_column(Text)
    known_arguments_json: Mapped[str] = mapped_column(Text, default="{}")
    missing_arguments_json: Mapped[str] = mapped_column(Text, default="[]")
    resume_context_json: Mapped[str] = mapped_column(Text, default="{}")
    approved_question: Mapped[str] = mapped_column(Text)
    round_count: Mapped[int] = mapped_column(Integer, default=1)
    max_rounds: Mapped[int] = mapped_column(Integer, default=3)
    no_progress_count: Mapped[int] = mapped_column(Integer, default=0)
    finish_reason: Mapped[str] = mapped_column(String(64), default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class ChatTurn(Base):
    __tablename__ = "chat_turns"
    __table_args__ = (
        UniqueConstraint("user_id", "request_id", name="uq_chat_turns_user_request"),
        Index("ix_chat_turns_session_status", "session_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    request_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    user_message_id: Mapped[Optional[int]] = mapped_column(ForeignKey("chat_messages.id"), nullable=True, index=True)
    assistant_message_id: Mapped[Optional[int]] = mapped_column(ForeignKey("chat_messages.id"), nullable=True, index=True)
    trace_id: Mapped[Optional[int]] = mapped_column(ForeignKey("agent_run_traces.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="RECEIVED", index=True)
    partial_content: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    finish_reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    completion_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    generation_metadata_json: Mapped[str] = mapped_column(
        Text,
        default='{"schemaVersion":1,"completionVerified":false}',
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class UserMemory(Base):
    __tablename__ = "user_memories"
    __table_args__ = (
        Index(
            "ix_user_memories_user_status_category_key",
            "user_id",
            "status",
            "category",
            "memory_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    memory_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    source_message_id: Mapped[Optional[int]] = mapped_column(ForeignKey("chat_messages.id"), nullable=True, index=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    origin: Mapped[str] = mapped_column(String(32), default="EXPLICIT", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_from_turn_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("chat_turns.id"), nullable=True, index=True
    )
    evidence_message_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    sensitivity: Mapped[str] = mapped_column(String(32), default="NORMAL", index=True)
    visibility_scope: Mapped[str] = mapped_column(String(32), default="ALL_AGENTS", index=True)
    index_status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    memory_epoch: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", index=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class ConversationEpisode(Base):
    __tablename__ = "conversation_episodes"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "source_start_message_id",
            "source_end_message_id",
            "schema_version",
            name="uq_conversation_episode_source_range",
        ),
        Index("ix_conversation_episodes_user_status_created", "user_id", "status", "created_at"),
        Index("ix_conversation_episodes_session_range", "session_id", "source_start_message_id", "source_end_message_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    source_start_message_id: Mapped[int] = mapped_column(ForeignKey("chat_messages.id"), index=True)
    source_end_message_id: Mapped[int] = mapped_column(ForeignKey("chat_messages.id"), index=True)
    summary_text: Mapped[str] = mapped_column(Text)
    facts_json: Mapped[str] = mapped_column(Text, default="{}")
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", index=True)
    index_status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    embedding_version: Mapped[str] = mapped_column(String(160), default="unindexed")
    is_tail: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)


class UserProfileState(Base):
    __tablename__ = "user_profile_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"), unique=True, index=True)
    profile_version: Mapped[int] = mapped_column(Integer, default=0)
    extracted_until_message_id: Mapped[int] = mapped_column(Integer, default=0)
    memory_epoch: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class MemoryJob(Base):
    __tablename__ = "memory_jobs"
    __table_args__ = (
        Index("ix_memory_jobs_ready", "status", "run_after", "lease_expires_at"),
        Index("ix_memory_jobs_scope", "user_id", "session_id", "kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    dedupe_key: Mapped[str] = mapped_column(String(191), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    session_id: Mapped[Optional[int]] = mapped_column(ForeignKey("chat_sessions.id"), nullable=True, index=True)
    turn_id: Mapped[Optional[int]] = mapped_column(ForeignKey("chat_turns.id"), nullable=True, index=True)
    target_message_id: Mapped[Optional[int]] = mapped_column(ForeignKey("chat_messages.id"), nullable=True, index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    run_after: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    expected_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    memory_epoch: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    canonical_key: Mapped[Optional[str]] = mapped_column(String(160), nullable=True, index=True)
    managed_by: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(256))
    source_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    source_type: Mapped[str] = mapped_column(String(64), index=True)
    domain: Mapped[str] = mapped_column(String(64), index=True)
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    site: Mapped[str] = mapped_column(String(64), default="ALL", index=True)
    status: Mapped[str] = mapped_column(String(32), default="DRAFT", index=True)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    version: Mapped[str] = mapped_column(String(64), default="1")
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    ingestion_status: Mapped[str] = mapped_column(String(32), default="READY", index=True)
    parser_profile: Mapped[str] = mapped_column(String(64), default="legacy")
    chunking_profile: Mapped[str] = mapped_column(String(64), default="legacy_char_v1")
    parser_version: Mapped[str] = mapped_column(String(64), default="legacy")
    last_ingestion_error: Mapped[str] = mapped_column(Text, default="")
    ingestion_warnings_json: Mapped[str] = mapped_column(Text, default="[]")
    ingestion_quality_json: Mapped[str] = mapped_column(Text, default="{}")
    active_revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    chunks: Mapped[list["KnowledgeChunk"]] = relationship(back_populates="document", cascade="all, delete-orphan")
    artifacts: Mapped[list["KnowledgeArtifact"]] = relationship(back_populates="document", cascade="all, delete-orphan")
    elements: Mapped[list["KnowledgeElement"]] = relationship(back_populates="document", cascade="all, delete-orphan")
    tables: Mapped[list["KnowledgeTable"]] = relationship(back_populates="document", cascade="all, delete-orphan")
    ingestion_jobs: Mapped[list["KnowledgeIngestionJob"]] = relationship(back_populates="document")


class KnowledgeArtifact(Base):
    __tablename__ = "knowledge_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("knowledge_documents.id"), index=True)
    storage_key: Mapped[str] = mapped_column(String(512), index=True)
    original_filename: Mapped[str] = mapped_column(String(256))
    mime_type: Mapped[str] = mapped_column(String(128))
    byte_size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    document: Mapped[KnowledgeDocument] = relationship(back_populates="artifacts")
    elements: Mapped[list["KnowledgeElement"]] = relationship(back_populates="artifact")


class KnowledgeElement(Base):
    __tablename__ = "knowledge_elements"
    __table_args__ = (UniqueConstraint("document_id", "element_index", name="uq_knowledge_element_document_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("knowledge_documents.id"), index=True)
    artifact_id: Mapped[Optional[int]] = mapped_column(ForeignKey("knowledge_artifacts.id"), nullable=True, index=True)
    element_index: Mapped[int] = mapped_column(Integer)
    element_type: Mapped[str] = mapped_column(String(32), index=True)
    page_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    bbox_json: Mapped[str] = mapped_column(Text, default="null")
    heading_path_json: Mapped[str] = mapped_column(Text, default="[]")
    parent_element_id: Mapped[Optional[int]] = mapped_column(ForeignKey("knowledge_elements.id"), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    parser_name: Mapped[str] = mapped_column(String(64))
    parser_version: Mapped[str] = mapped_column(String(64))
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    document: Mapped[KnowledgeDocument] = relationship(back_populates="elements")
    artifact: Mapped[Optional[KnowledgeArtifact]] = relationship(back_populates="elements")
    tables: Mapped[list["KnowledgeTable"]] = relationship(back_populates="element")


class KnowledgeTable(Base):
    __tablename__ = "knowledge_tables"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("knowledge_documents.id"), index=True)
    element_id: Mapped[int] = mapped_column(ForeignKey("knowledge_elements.id"), unique=True, index=True)
    page_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    caption: Mapped[str] = mapped_column(String(512), default="")
    headers_json: Mapped[str] = mapped_column(Text, default="[]")
    rows_json: Mapped[str] = mapped_column(Text, default="[]")
    table_html: Mapped[str] = mapped_column(Text, default="")
    schema_json: Mapped[str] = mapped_column(Text, default="{}")
    content_hash: Mapped[str] = mapped_column(String(64), index=True)

    document: Mapped[KnowledgeDocument] = relationship(back_populates="tables")
    element: Mapped[KnowledgeElement] = relationship(back_populates="tables")


class KnowledgeIngestionJob(Base):
    __tablename__ = "knowledge_ingestion_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[Optional[int]] = mapped_column(ForeignKey("knowledge_documents.id"), nullable=True, index=True)
    job_type: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    request_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    error_code: Mapped[str] = mapped_column(String(64), default="")
    error_message: Mapped[str] = mapped_column(String(256), default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    document: Mapped[Optional[KnowledgeDocument]] = relationship(back_populates="ingestion_jobs")


class KnowledgeIndexRegistry(Base):
    __tablename__ = "knowledge_index_registry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    logical_name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    active_collection: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    previous_collection: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    ready_collection: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    active_signature: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    ready_signature: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="EMPTY")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (UniqueConstraint("document_id", "source_index", name="uq_knowledge_chunk_document_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[Optional[int]] = mapped_column(ForeignKey("knowledge_documents.id"), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(256), index=True)
    source_index: Mapped[int] = mapped_column(Integer)
    section_title: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    page_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    chunk_kind: Mapped[str] = mapped_column(String(32), default="TEXT_CHILD", index=True)
    parent_chunk_id: Mapped[Optional[int]] = mapped_column(ForeignKey("knowledge_chunks.id"), nullable=True, index=True)
    heading_path_json: Mapped[str] = mapped_column(Text, default="[]")
    element_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    token_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    chunking_profile: Mapped[str] = mapped_column(String(64), default="legacy_char_v1")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    document: Mapped[Optional[KnowledgeDocument]] = relationship(back_populates="chunks")


class PsychologicalReport(Base):
    __tablename__ = "psychological_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"))
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"))
    content: Mapped[str] = mapped_column(Text)
    intent: Mapped[str] = mapped_column(String(32))
    emotion: Mapped[str] = mapped_column(String(32))
    emotion_score: Mapped[float] = mapped_column(Float)
    risk_level: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float] = mapped_column(Float)
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class RiskCase(Base):
    __tablename__ = "risk_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    risk_level: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    owner: Mapped[str] = mapped_column(String(128), default="unassigned")
    summary: Mapped[str] = mapped_column(Text)
    handoff_summary: Mapped[str] = mapped_column(Text, default="")
    acknowledged_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class CaseNote(Base):
    __tablename__ = "case_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(Integer, index=True)
    actor: Mapped[str] = mapped_column(String(128))
    note: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AlertRecord(Base):
    __tablename__ = "alert_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(Integer, index=True)
    channel: Mapped[str] = mapped_column(String(64))
    recipient: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ExcelRecord(Base):
    __tablename__ = "excel_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(Integer, index=True)
    file_path: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ToolJob(Base):
    __tablename__ = "tool_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    depends_on_job_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    run_after: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class DeadLetterRecord(Base):
    __tablename__ = "dead_letter_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    report_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AgentRunTrace(Base):
    __tablename__ = "agent_run_traces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    report_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    intent: Mapped[str] = mapped_column(String(32), index=True)
    risk_level: Mapped[str] = mapped_column(String(32), default="LOW", index=True)
    original_input: Mapped[str] = mapped_column(Text)
    sanitized_input: Mapped[str] = mapped_column(Text)
    memory_brief: Mapped[str] = mapped_column(Text, default="")
    agent_steps_json: Mapped[str] = mapped_column(Text, default="[]")
    evidence_items_json: Mapped[str] = mapped_column(Text, default="[]")
    response_messages_json: Mapped[str] = mapped_column(Text, default="[]")
    assessment_json: Mapped[str] = mapped_column(Text, default="{}")
    context_manifest_json: Mapped[str] = mapped_column(Text, default="{}")
    generation_json: Mapped[str] = mapped_column(Text, default="{}")
    tool_diagnostics_json: Mapped[str] = mapped_column(Text, default="{}")
    finalized_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ToolAuditRecord(Base):
    __tablename__ = "tool_audit_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    report_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    tool_name: Mapped[str] = mapped_column(String(64), index=True)
    policy: Mapped[str] = mapped_column(String(128), default="")
    allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ToolExecutionRecordEntity(Base):
    __tablename__ = "tool_execution_records"
    __table_args__ = (
        Index("ix_tool_execution_scope", "user_id", "session_id", "execution_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    execution_id: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    user_id: Mapped[str] = mapped_column(String(96), default="", index=True)
    session_id: Mapped[str] = mapped_column(String(96), default="", index=True)
    turn_id: Mapped[str] = mapped_column(String(96), default="")
    work_item_id: Mapped[str] = mapped_column(String(96), default="")
    agent_run_id: Mapped[str] = mapped_column(String(96), default="")
    agent_name: Mapped[str] = mapped_column(String(96), default="")
    tool_call_id: Mapped[str] = mapped_column(String(128), default="")
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    tool_name: Mapped[str] = mapped_column(String(160), default="")
    tool_version: Mapped[str] = mapped_column(String(64), default="")
    arguments_json: Mapped[str] = mapped_column(Text, default="{}")
    wire_result_json: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), "mysql"))
    normalized_result_json: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), "mysql"))
    raw_hash: Mapped[str] = mapped_column(String(80), index=True)
    payload_bytes: Mapped[int] = mapped_column(Integer, default=0)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    valid_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    corpus_signature: Mapped[str] = mapped_column(String(128), default="")
    index_signature: Mapped[str] = mapped_column(String(128), default="")
    transport_status: Mapped[str] = mapped_column(String(32), default="OK")
    business_status: Mapped[str] = mapped_column(String(32), default="OK")
    quality_status: Mapped[str] = mapped_column(String(32), default="")
    error_code: Mapped[str] = mapped_column(String(64), default="")
    cached_from_execution_id: Mapped[str] = mapped_column(String(96), default="")
    persist_reason: Mapped[str] = mapped_column(String(64), default="")


class ToolResultViewEntity(Base):
    __tablename__ = "tool_result_views"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    view_id: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    execution_id: Mapped[str] = mapped_column(String(96), index=True)
    raw_hash: Mapped[str] = mapped_column(String(80), index=True)
    goal_hash: Mapped[str] = mapped_column(String(80), default="")
    audience: Mapped[str] = mapped_column(String(64), default="")
    schema_version: Mapped[str] = mapped_column(String(32), default="v1")
    summary_prompt_version: Mapped[str] = mapped_column(String(64), default="")
    mode: Mapped[str] = mapped_column(String(32))
    view_json: Mapped[str] = mapped_column(Text)
    quality_status: Mapped[str] = mapped_column(String(32), default="")
    token_estimate: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ModelContextManifestEntity(Base):
    __tablename__ = "model_context_manifests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    turn_id: Mapped[str] = mapped_column(String(96), default="", index=True)
    work_item_id: Mapped[str] = mapped_column(String(96), default="")
    agent_run_id: Mapped[str] = mapped_column(String(96), default="")
    model_round: Mapped[int] = mapped_column(Integer, default=0)
    manifest_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
