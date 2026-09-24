from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from importlib import import_module

from app.core.config import Settings


logger = logging.getLogger(__name__)


class ChatTurnSnapshotStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = self._connect()

    def read(self, request_id: str, user_id: int) -> dict | None:
        if self.client is None:
            return None
        try:
            raw = self.client.get(self._key(request_id))
            value = json.loads(raw) if raw else None
            return value if isinstance(value, dict) and int(value.get("userId", -1)) == user_id else None
        except Exception as exc:
            logger.warning("Redis chat snapshot read unavailable request_id=%s: %s", request_id, exc)
            return None

    def write(self, request_id: str, payload: dict) -> None:
        if self.client is None:
            return
        try:
            self.client.setex(
                self._key(request_id),
                max(60, int(self.settings.chat_turn_snapshot_ttl_seconds)),
                json.dumps(payload, ensure_ascii=False, default=str),
            )
        except Exception as exc:
            logger.warning("Redis chat snapshot write unavailable request_id=%s: %s", request_id, exc)

    def payload(
        self,
        user_id: int,
        session_id: str,
        turn_id: str,
        status: str,
        content: str,
        *,
        finish_reason: str | None = None,
        completion_verified: bool = False,
        partial: bool = False,
        retryable: bool = False,
        continuation_count: int = 0,
        output_tokens: int | None = None,
        error: str = "",
    ) -> dict:
        return {
            "userId": user_id,
            "sessionId": session_id,
            "turnId": turn_id,
            "status": status,
            "content": content,
            "finishReason": finish_reason,
            "completionVerified": completion_verified,
            "partial": partial,
            "retryable": retryable,
            "continuationCount": continuation_count,
            "outputTokens": output_tokens,
            "error": error,
            "updatedAt": datetime.now(UTC).isoformat(),
        }

    def _connect(self):
        if not bool(getattr(self.settings, "chat_turn_snapshot_enabled", True)):
            return None
        try:
            redis_module = import_module("redis")
            client = redis_module.Redis.from_url(
                self.settings.redis_url,
                decode_responses=True,
                socket_timeout=self.settings.redis_socket_timeout_seconds,
                socket_connect_timeout=self.settings.redis_socket_timeout_seconds,
            )
            client.ping()
            return client
        except Exception as exc:
            logger.warning("Redis chat snapshots disabled: %s", exc)
            return None

    @staticmethod
    def _key(request_id: str) -> str:
        return f"mindbridge:chat-turn:{request_id}"
