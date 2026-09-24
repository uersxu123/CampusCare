from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.entities import ChatMessage, ChatSession
from app.schemas.dtos import (
    ConversationArchiveResponse,
    ConversationListItemResponse,
    ConversationListResponse,
    ConversationMessageResponse,
    ConversationResponse,
)
from app.services.memory import RedisShortTermMemoryStore
from app.services.memory_jobs import MemoryJobService, get_memory_worker


logger = logging.getLogger(__name__)


class ConversationNotFoundError(LookupError):
    pass


class ArchivedConversationError(ValueError):
    pass


class StudentConversationService:
    def __init__(
        self,
        db: Session,
        settings: Settings,
        memory: RedisShortTermMemoryStore | None = None,
    ):
        self.db = db
        self.settings = settings
        self.memory = memory

    def list_active(self, user_id: int, limit: int = 30) -> ConversationListResponse:
        message_stats = (
            self.db.query(
                ChatMessage.session_id.label("session_id"),
                func.count(ChatMessage.id).label("message_count"),
                func.max(ChatMessage.id).label("last_message_id"),
            )
            .group_by(ChatMessage.session_id)
            .subquery()
        )
        rows = (
            self.db.query(ChatSession, message_stats.c.message_count, ChatMessage.content)
            .outerjoin(message_stats, message_stats.c.session_id == ChatSession.id)
            .outerjoin(ChatMessage, ChatMessage.id == message_stats.c.last_message_id)
            .filter(ChatSession.user_id == user_id, ChatSession.archived_at.is_(None))
            .order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
            .limit(max(1, min(limit, 100)))
            .all()
        )
        return ConversationListResponse(
            items=[
                ConversationListItemResponse(
                    sessionId=session.public_id,
                    title=(session.title or "").strip() or "新会话",
                    preview=_preview(content),
                    messageCount=int(message_count or 0),
                    createdAt=session.created_at,
                    updatedAt=session.updated_at,
                )
                for session, message_count, content in rows
            ]
        )

    def get_active(self, user_id: int, public_id: str) -> ConversationResponse:
        session = (
            self.db.query(ChatSession)
            .filter(
                ChatSession.public_id == public_id,
                ChatSession.user_id == user_id,
                ChatSession.archived_at.is_(None),
            )
            .first()
        )
        if session is None:
            raise ConversationNotFoundError("会话不存在")
        return _conversation_response(self.db, session)

    def require_available(self, user_id: int, public_id: str) -> ChatSession:
        session = (
            self.db.query(ChatSession)
            .filter(ChatSession.public_id == public_id, ChatSession.user_id == user_id)
            .first()
        )
        if session is None:
            raise ConversationNotFoundError("会话不存在")
        if session.archived:
            raise ArchivedConversationError("该会话已归档，不能继续发送消息。")
        return session

    def archive(self, user_id: int, public_id: str) -> ConversationArchiveResponse:
        session = (
            self.db.query(ChatSession)
            .filter(ChatSession.public_id == public_id, ChatSession.user_id == user_id)
            .first()
        )
        if session is None:
            raise ConversationNotFoundError("会话不存在")
        if session.archived_at is None:
            session.archived_at = datetime.now(UTC).replace(tzinfo=None)
            self.db.add(session)
            target_message_id = (
                self.db.query(ChatMessage.id)
                .filter(ChatMessage.session_id == session.id, ChatMessage.user_id == user_id)
                .order_by(ChatMessage.id.desc())
                .limit(1)
                .scalar()
            )
            if self.settings.memory_v3_enabled and target_message_id is not None:
                MemoryJobService(self.db, self.settings).enqueue_finalize_session(
                    user_id=user_id,
                    session_id=session.id,
                    target_message_id=int(target_message_id),
                )
            self.db.commit()
            self.db.refresh(session)
        memory = self.memory or RedisShortTermMemoryStore(self.settings)
        try:
            try:
                memory.delete(session.public_id, user_id=user_id)
            except TypeError:
                # One-release compatibility for injected V2 cache adapters.
                memory.delete(session.public_id)
        except Exception as exc:
            logger.warning("Conversation memory cleanup unavailable for session=%s: %s", session.public_id, exc)
        if self.settings.memory_v3_enabled and self.settings.memory_worker_enabled:
            get_memory_worker(self.settings).wake()
        return ConversationArchiveResponse(
            sessionId=session.public_id,
            archived=True,
            archivedAt=session.archived_at,
        )


def _conversation_response(db: Session, session: ChatSession) -> ConversationResponse:
    rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        .all()
    )
    return ConversationResponse(
        sessionId=session.public_id,
        title=(session.title or "").strip() or "新会话",
        archived=session.archived,
        archivedAt=session.archived_at,
        createdAt=session.created_at,
        updatedAt=session.updated_at,
        messages=[
            ConversationMessageResponse(role=row.role, content=row.content, createdAt=row.created_at)
            for row in rows
        ],
    )


def _preview(content: str | None, limit: int = 80) -> str:
    normalized = " ".join((content or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."
