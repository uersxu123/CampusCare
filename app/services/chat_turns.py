from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.entities import ChatMessage, ChatSession, ChatTurn, UserAccount
from app.services.conversations import ArchivedConversationError, ConversationNotFoundError
from app.services.trace import AgentTraceService


ACTIVE_TURN_STATUSES = {"RECEIVED", "GENERATING"}
TERMINAL_TURN_STATUSES = {"COMPLETED", "INTERRUPTED", "FAILED"}


class ChatTurnNotFoundError(LookupError):
    pass


class ChatTurnService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings

    def get_owned(self, user_id: int, request_id: str) -> ChatTurn:
        row = (
            self.db.query(ChatTurn)
            .filter(ChatTurn.user_id == user_id, ChatTurn.request_id == request_id)
            .first()
        )
        if row is None:
            raise ChatTurnNotFoundError("生成任务不存在")
        return row

    def create_or_get(
        self,
        user: UserAccount,
        request_id: str,
        session_public_id: str | None,
        text: str,
    ) -> tuple[ChatTurn, bool]:
        existing = (
            self.db.query(ChatTurn)
            .filter(ChatTurn.user_id == user.id, ChatTurn.request_id == request_id)
            .first()
        )
        if existing:
            if existing.status == "RECEIVED" and existing.user_message_id is None:
                self.ensure_user_message(existing, text)
            return existing, False
        session = self._resolve_session(user, session_public_id, text)
        row = ChatTurn(
            public_id=uuid.uuid4().hex,
            request_id=request_id,
            user_id=user.id,
            session_id=session.id,
            status="RECEIVED",
            partial_content="",
            error="",
        )
        self.db.add(row)
        try:
            self.db.flush()
            message = ChatMessage(
                user_id=user.id,
                session_id=session.id,
                role="USER",
                content=text,
            )
            self.db.add(message)
            self.db.flush()
            row.user_message_id = message.id
            session.touch()
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            return self.get_owned(user.id, request_id), False
        self.db.refresh(row)
        return row, True

    def ensure_user_message(self, turn: ChatTurn, text: str) -> ChatMessage:
        if turn.user_message_id is not None:
            return self.get_user_message(turn)
        if turn.status != "RECEIVED":
            raise ValueError("生成任务缺少已持久化的用户消息")
        message = ChatMessage(
            user_id=turn.user_id,
            session_id=turn.session_id,
            role="USER",
            content=text,
        )
        self.db.add(message)
        self.db.flush()
        turn.user_message_id = message.id
        session = self.db.get(ChatSession, turn.session_id)
        if session is not None:
            session.touch()
        self.db.commit()
        self.db.refresh(message)
        return message

    def get_user_message(self, turn: ChatTurn) -> ChatMessage:
        if turn.user_message_id is None:
            raise ValueError("生成任务缺少已持久化的用户消息")
        message = self.db.get(ChatMessage, turn.user_message_id)
        if (
            message is None
            or message.user_id != turn.user_id
            or message.session_id != turn.session_id
            or message.role.upper() != "USER"
        ):
            raise ValueError("生成任务关联的用户消息无效")
        return message

    def mark_stale_interrupted(self) -> int:
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            seconds=max(1, int(self.settings.chat_turn_stale_seconds))
        )
        rows = (
            self.db.query(ChatTurn)
            .filter(ChatTurn.status.in_(ACTIVE_TURN_STATUSES), ChatTurn.updated_at < cutoff)
            .all()
        )
        finalized_at = datetime.now(UTC).replace(tzinfo=None)
        for row in rows:
            metadata = {
                "schemaVersion": 2,
                "source": "APPLICATION",
                "provider": "application",
                "model": "application",
                "finishReason": "CANCELLED",
                "providerFinishReason": "",
                "semanticFinishSeen": False,
                "transportTerminalSeen": False,
                "outputChars": len(row.partial_content or ""),
                "continuationCount": 0,
                "completionVerified": False,
                "attempts": [],
                "recovery": "STALE_TURN",
                "turnMetrics": {
                    "schemaVersion": 1,
                    "status": "INCOMPLETE",
                    "incompleteReason": "PROCESS_RESTART",
                },
            }
            row.status = "INTERRUPTED"
            row.finish_reason = "CANCELLED"
            row.completion_verified = False
            row.generation_metadata_json = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
            row.error = "GENERATION_CANCELLED"
            row.completed_at = finalized_at
            row.updated_at = finalized_at
            if row.trace_id is not None:
                AgentTraceService(self.db, self.settings).finalize_generation_trace(
                    row.trace_id, metadata, commit=False
                )
        count = len(rows)
        self.db.commit()
        return count

    def _resolve_session(self, user: UserAccount, public_id: str | None, text: str) -> ChatSession:
        if public_id:
            session = (
                self.db.query(ChatSession)
                .filter(ChatSession.public_id == public_id, ChatSession.user_id == user.id)
                .first()
            )
            if session is None:
                raise ConversationNotFoundError("会话不存在")
            if session.archived:
                raise ArchivedConversationError("该会话已归档，不能继续发送消息。")
            return session
        session = ChatSession(public_id=uuid.uuid4().hex, user_id=user.id, title=text[:36])
        self.db.add(session)
        self.db.flush()
        return session
