
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from typing import Protocol

from app.core.config import Settings
from sqlalchemy.orm import Session

from app.models.entities import ChatMessage, ConversationSummary
from app.schemas.dtos import AiMessage


logger = logging.getLogger(__name__)


class SummarySettings(Protocol):
    context_recent_message_limit: int


class RedisShortTermMemoryStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = self._connect()

    def load_recent(self, session_public_id: str, *, user_id: int | None = None) -> list[AiMessage]:
        return [
            AiMessage(role=item.role, content=item.content)
            for item in self.load_recent_records(session_public_id, user_id=user_id)
        ]

    def load_recent_records(
        self, session_public_id: str, *, user_id: int | None = None
    ) -> list["MemoryMessage"]:
        records, _ = self.load_recent_records_with_status(session_public_id, user_id=user_id)
        return records

    def load_recent_records_with_status(
        self, session_public_id: str, *, user_id: int | None = None
    ) -> tuple[list["MemoryMessage"], str]:
        if self.client is None:
            return [], "unavailable"
        try:
            records = self._read(session_public_id, self.settings.redis_memory_max_messages, user_id=user_id)
            if records:
                return records, "hit"
            exists = getattr(self.client, "exists", None)
            if callable(exists) and exists(self._key(session_public_id, user_id)):
                return [], "empty"
            return [], "miss"
        except Exception as exc:
            logger.warning("Redis memory read unavailable: %s", exc)
            return [], "error"

    def messages_from_rows(self, rows: list[ChatMessage]) -> list[AiMessage]:
        return [self._message_from_row(row) for row in rows]

    def append(
        self,
        session_public_id: str,
        role: str,
        content: str,
        message_id: int | None = None,
        *,
        user_id: int | None = None,
    ) -> None:
        if self.client is None:
            return
        key = self._key(session_public_id, user_id)
        payload = self._serialize(role, content, message_id)
        try:
            self.client.rpush(key, payload)
            self.client.ltrim(key, -self.settings.redis_memory_max_messages, -1)
            self.client.expire(key, self.settings.redis_memory_ttl_seconds)
        except Exception as exc:
            logger.warning("Redis memory append unavailable: %s", exc)

    def replace(
        self, session_public_id: str, messages: list[AiMessage], *, user_id: int | None = None
    ) -> None:
        if self.client is None:
            return
        key = self._key(session_public_id, user_id)
        pipe = self.client.pipeline()
        pipe.delete(key)
        if messages:
            pipe.rpush(key, *[self._serialize(message.role, message.content) for message in messages])
            pipe.ltrim(key, -self.settings.redis_memory_max_messages, -1)
            pipe.expire(key, self.settings.redis_memory_ttl_seconds)
        try:
            pipe.execute()
        except Exception as exc:
            logger.warning("Redis memory replace unavailable: %s", exc)

    def merge_records(
        self,
        session_public_id: str,
        records: list["MemoryMessage"],
        *,
        user_id: int | None = None,
        max_attempts: int = 3,
    ) -> bool:
        """Atomically merge a SQL snapshot without overwriting a concurrent cache tail."""
        if self.client is None or not records:
            return False
        key = self._key(session_public_id, user_id)
        watch_error = getattr(import_module("redis"), "WatchError")
        for _ in range(max(1, max_attempts)):
            pipe = self.client.pipeline()
            try:
                pipe.watch(key)
                existing = self._decode_items(pipe.lrange(key, 0, -1))
                merged = {
                    int(item.id): item
                    for item in [*records, *existing]
                    if item.id is not None and item.id > 0
                }
                ordered = [merged[item_id] for item_id in sorted(merged)]
                ordered = ordered[-int(self.settings.redis_memory_max_messages):]
                pipe.multi()
                pipe.delete(key)
                if ordered:
                    pipe.rpush(
                        key,
                        *[self._serialize(item.role, item.content, item.id) for item in ordered],
                    )
                    pipe.expire(key, self.settings.redis_memory_ttl_seconds)
                pipe.execute()
                return True
            except watch_error:
                continue
            except Exception as exc:
                logger.warning("Redis memory merge unavailable: %s", exc)
                return False
            finally:
                try:
                    pipe.reset()
                except Exception:
                    pass
        return False

    def delete(self, session_public_id: str, *, user_id: int | None = None) -> None:
        if self.client is None:
            return
        try:
            self.client.delete(self._key(session_public_id, user_id))
        except Exception as exc:
            logger.warning("Redis memory delete unavailable: %s", exc)

    def _read(
        self, session_public_id: str, limit: int, *, user_id: int | None = None
    ) -> list["MemoryMessage"]:
        raw_items = self.client.lrange(self._key(session_public_id, user_id), -limit, -1)
        return self._decode_items(raw_items)

    @staticmethod
    def _decode_items(raw_items) -> list["MemoryMessage"]:
        messages = []
        for raw in raw_items:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            role = str(data.get("role", "")).lower()
            content = str(data.get("content", ""))
            if role and content:
                raw_id = data.get("messageId")
                messages.append(
                    MemoryMessage(
                        id=int(raw_id) if isinstance(raw_id, int) or str(raw_id).isdigit() else None,
                        role=role,
                        content=content,
                    )
                )
        return messages

    def _connect(self):
        if not bool(getattr(self.settings, "redis_memory_enabled", True)):
            return None
        try:
            redis_module = import_module("redis")
        except ModuleNotFoundError as exc:
            raise RuntimeError("请先安装 requirements.txt 中的 redis 依赖") from exc
        client = redis_module.Redis.from_url(
            self.settings.redis_url,
            decode_responses=True,
            socket_timeout=self.settings.redis_socket_timeout_seconds,
            socket_connect_timeout=self.settings.redis_socket_timeout_seconds,
        )
        try:
            client.ping()
        except Exception as exc:
            logger.warning("Redis memory disabled: %s", exc)
            return None
        return client

    def _message_from_row(self, row: ChatMessage) -> AiMessage:
        return AiMessage(role=row.role.lower(), content=row.content)

    def _serialize(self, role: str, content: str, message_id: int | None = None) -> str:
        return json.dumps(
            {
                "role": role.lower(),
                "content": content,
                "createdAt": datetime.now(UTC).isoformat(),
                "messageId": message_id,
            },
            ensure_ascii=False,
        )

    def _key(self, session_public_id: str, user_id: int | None = None) -> str:
        if user_id is None:
            return f"mindbridge:short-term-memory:{session_public_id}"
        return f"campuscare:memory:v3:recent:{int(user_id)}:{session_public_id}"


@dataclass(frozen=True)
class MemoryMessage:
    id: int | None
    role: str
    content: str


EMPTY_STRUCTURED_SUMMARY = {
    "schema_version": 2,
    "current_goal": None,
    "confirmed_facts": [],
    "constraints_and_preferences": [],
    "open_questions": [],
    "corrections": [],
    "previous_support": [],
    "active_topics": [],
}


class ConversationSummaryRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, session_id: int) -> ConversationSummary | None:
        return self.db.query(ConversationSummary).filter(ConversationSummary.session_id == session_id).first()

    def create(self, session_id: int) -> ConversationSummary:
        row = ConversationSummary(
            session_id=session_id,
            version=0,
            covered_until_message_id=0,
            summary_json=json.dumps(EMPTY_STRUCTURED_SUMMARY, ensure_ascii=False),
        )
        self.db.add(row)
        self.db.flush()
        return row

    def compare_and_swap(
        self,
        row_id: int,
        expected_version: int,
        covered_until_message_id: int,
        summary: dict,
        degraded: bool,
        last_error: str,
    ) -> bool:
        updated = (
            self.db.query(ConversationSummary)
            .filter(ConversationSummary.id == row_id, ConversationSummary.version == expected_version)
            .update(
                {
                    ConversationSummary.version: expected_version + 1,
                    ConversationSummary.covered_until_message_id: covered_until_message_id,
                    ConversationSummary.summary_json: json.dumps(summary, ensure_ascii=False),
                    ConversationSummary.degraded: degraded,
                    ConversationSummary.last_error: last_error[:1000],
                    ConversationSummary.updated_at: datetime.now(UTC).replace(tzinfo=None),
                },
                synchronize_session=False,
            )
        )
        return updated == 1


class ConversationSummaryService:
    def __init__(self, db: Session, settings: SummarySettings | None = None, summarizer=None):
        self.db = db
        self.repository = ConversationSummaryRepository(db)
        self.settings = settings
        self.summarizer = summarizer

    def update(self, session_id: int, max_attempts: int = 2) -> ConversationSummary | None:
        for _ in range(max_attempts):
            row = self.repository.get(session_id) or self.repository.create(session_id)
            messages = (
                self.db.query(ChatMessage)
                .filter(ChatMessage.session_id == session_id, ChatMessage.id > row.covered_until_message_id)
                .order_by(ChatMessage.id.asc())
                .all()
            )
            recent_limit = max(2, int(getattr(self.settings, "context_recent_message_limit", 8)))
            compressible = messages[:-recent_limit]
            if not compressible:
                self.db.rollback()
                return row
            prior = _safe_summary_json(row.summary_json)
            degraded = False
            error = ""
            try:
                summary = (
                    self.summarizer(prior, compressible)
                    if self.summarizer
                    else update_summary_v2(prior, compressible)
                )
                summary = _validate_structured_summary(summary)
            except Exception as exc:
                summary = update_summary_v2(prior, compressible)
                degraded = True
                error = f"{type(exc).__name__}: {exc}"
            if self.repository.compare_and_swap(
                row.id, row.version, compressible[-1].id, summary, degraded, error
            ):
                self.db.commit()
                return self.repository.get(session_id)
            self.db.rollback()
        logger.warning("Conversation summary CAS conflict for session_id=%s", session_id)
        return None


def normalize_summary_v2(raw: str | dict) -> dict:
    parse_failed = False
    if isinstance(raw, str):
        try:
            value = json.loads(raw or "{}")
        except (TypeError, json.JSONDecodeError):
            value = {}
            parse_failed = True
    else:
        value = raw
    if not isinstance(value, dict):
        value = {}
        parse_failed = True

    normalized = {
        "schema_version": 2,
        "current_goal": _normalize_summary_item(value.get("current_goal"), "text", 160),
        "confirmed_facts": _normalize_summary_items(
            value.get("confirmed_facts"), "value", 8, 120, key_prefix="legacy_fact"
        ),
        "constraints_and_preferences": _normalize_summary_items(
            value.get("constraints_and_preferences", value.get("user_preferences")),
            "value",
            6,
            120,
            key_prefix="legacy_preference",
        ),
        "open_questions": _normalize_summary_items(
            value.get("open_questions"), "text", 4, 120
        ),
        "corrections": _normalize_corrections(value.get("corrections")),
        "previous_support": _normalize_summary_items(
            value.get("previous_support", value.get("decisions")), "text", 4, 100
        ),
        "active_topics": _bounded_unique(
            value.get("active_topics", []) if isinstance(value.get("active_topics", []), list) else [],
            6,
        ),
    }
    if parse_failed:
        return dict(EMPTY_STRUCTURED_SUMMARY)
    return normalized


def _safe_summary_json(raw: str) -> dict:
    return normalize_summary_v2(raw)


def _validate_structured_summary(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("summary must be an object")
    return normalize_summary_v2(value)


def update_summary_v2(prior: dict, messages: list[ChatMessage]) -> dict:
    summary = normalize_summary_v2(prior)
    facts = list(summary["confirmed_facts"])
    preferences = list(summary["constraints_and_preferences"])
    questions = list(summary["open_questions"])
    corrections = list(summary["corrections"])
    support = list(summary["previous_support"])
    topics = list(summary["active_topics"])

    for message in messages:
        content = message.content
        if message.role.upper() == "ASSISTANT":
            if content:
                support.append(
                    {
                        "text": _clip(content, 100),
                        "source_message_id": message.id,
                    }
                )
            continue
        if message.role.upper() != "USER" or not content:
            continue

        topics.extend(_topic_labels(content))
        goal_match = re.search(
            r"(?:我的目标是|目标是|我想要|我准备|计划)(?P<goal>[^。！？\n]{2,160})",
            content,
        )
        if goal_match:
            summary["current_goal"] = {
                "text": _clip(goal_match.group("goal"), 160),
                "source_message_id": message.id,
            }

        correction = re.search(
            r"不是\s*(?P<old>[^，,。；;]+)[，,。；;]?\s*(?:而是|是|改成)\s*(?P<new>[^，,。；;]+)",
            content,
        )
        if correction:
            old = correction.group("old").strip()
            new = correction.group("new").strip()
            key = _summary_key(content)
            facts = [item for item in facts if item.get("key") != key and old not in str(item.get("value", ""))]
            preferences = [
                item
                for item in preferences
                if item.get("key") != key and old not in str(item.get("value", ""))
            ]
            target = preferences if _is_preference_or_constraint(content) else facts
            target.append({"key": key, "value": _clip(new, 120), "source_message_id": message.id})
            corrections.append(
                {
                    "key": key,
                    "old_value": _clip(old, 60),
                    "new_value": _clip(new, 60),
                    "source_message_id": message.id,
                }
            )
            continue

        fact_match = re.search(
            r"我的(?P<key>课程|专业|学院|校区|考试日期|截止时间)是(?P<value>[^，,。；;]+)",
            content,
        )
        if fact_match:
            key = _summary_key(fact_match.group("key"))
            facts = [item for item in facts if item.get("key") != key]
            facts.append(
                {
                    "key": key,
                    "value": _clip(fact_match.group("value"), 120),
                    "source_message_id": message.id,
                }
            )

        if _is_preference_or_constraint(content):
            key = _summary_key(content)
            preferences = [item for item in preferences if item.get("key") != key]
            preferences.append(
                {"key": key, "value": _clip(content, 120), "source_message_id": message.id}
            )
        if any(term in content for term in ("还没确定", "待确认", "还需确认")):
            questions.append({"text": _clip(content, 120), "source_message_id": message.id})

    summary["schema_version"] = 2
    summary["confirmed_facts"] = facts[-8:]
    summary["constraints_and_preferences"] = preferences[-6:]
    summary["open_questions"] = questions[-4:]
    summary["corrections"] = corrections[-4:]
    summary["previous_support"] = support[-4:]
    summary["active_topics"] = _bounded_unique(topics, 6)
    return summary


def _bounded_unique(values: list, limit: int) -> list:
    result: list = []
    for value in values:
        normalized = " ".join(str(value).split())
        if normalized and normalized not in result:
            result.append(normalized)
    return result[-limit:]


def _normalize_summary_item(value, text_field: str, max_chars: int) -> dict | None:
    if isinstance(value, dict):
        text = _clip(str(value.get(text_field, "")), max_chars)
        if not text:
            return None
        return {
            text_field: text,
            "source_message_id": _optional_int(value.get("source_message_id")),
        }
    text = _clip(str(value or ""), max_chars)
    return {"text": text, "source_message_id": None} if text else None


def _normalize_summary_items(
    values,
    text_field: str,
    limit: int,
    max_chars: int,
    key_prefix: str | None = None,
) -> list[dict]:
    result = []
    for index, value in enumerate(values if isinstance(values, list) else []):
        if isinstance(value, dict):
            text = _clip(str(value.get(text_field, "")), max_chars)
            key = str(value.get("key", "")).strip()
            source_id = _optional_int(value.get("source_message_id"))
        else:
            text = _clip(str(value), max_chars)
            key = ""
            source_id = None
        if not text:
            continue
        item = {text_field: text, "source_message_id": source_id}
        if key_prefix is not None:
            item["key"] = key or f"{key_prefix}_{index + 1}"
        result.append(item)
    return result[-limit:]


def _normalize_corrections(values) -> list[dict]:
    result = []
    for index, value in enumerate(values if isinstance(values, list) else []):
        if isinstance(value, dict):
            old_value = _clip(str(value.get("old_value", "")), 60)
            new_value = _clip(str(value.get("new_value", "")), 60)
            key = str(value.get("key", "")).strip() or f"legacy_correction_{index + 1}"
            source_id = _optional_int(value.get("source_message_id"))
        else:
            parts = str(value).split("->", 1)
            old_value = _clip(parts[0], 60)
            new_value = _clip(parts[1] if len(parts) > 1 else "", 60)
            key = f"legacy_correction_{index + 1}"
            source_id = None
        if old_value or new_value:
            result.append(
                {
                    "key": key,
                    "old_value": old_value,
                    "new_value": new_value,
                    "source_message_id": source_id,
                }
            )
    return result[-4:]


def _optional_int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _is_preference_or_constraint(text: str) -> bool:
    return any(
        term in text
        for term in ("喜欢", "偏好", "习惯", "可用时间", "有空", "每周", "截止", "只能", "必须")
    )


def _summary_key(text: str) -> str:
    mapping = (
        (("课程", "科目"), "course"),
        (("考试日期", "考试时间", "截止"), "deadline"),
        (("可用时间", "有空", "晚上", "早上", "下午"), "available_time"),
        (("清单", "图表", "视频", "文字", "方式"), "learning_format"),
        (("专业",), "major"),
        (("学院",), "college"),
        (("校区",), "campus"),
    )
    for terms, key in mapping:
        if any(term in text for term in terms):
            return key
    compact = re.sub(r"\s+", "", text)
    return f"topic_{hashlib.sha256(compact.encode('utf-8')).hexdigest()[:12]}"


def _topic_labels(text: str) -> list[str]:
    labels = []
    if any(word in text for word in ["挂科", "补考", "重修", "绩点", "保研", "论文", "学业"]):
        labels.append("ACADEMIC")
    if any(word in text for word in ["宿舍", "调宿", "处分", "申诉", "助学金", "奖学金", "校园"]):
        labels.append("CAMPUS_SERVICE")
    if any(word in text for word in ["焦虑", "失眠", "抑郁", "心理", "压力"]):
        labels.append("MENTAL_HEALTH")
    return labels


def _clip(text: str, limit: int) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)] + "..."
