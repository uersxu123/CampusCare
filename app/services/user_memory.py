from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.entities import ConversationEpisode, ConversationSummary, UserMemory, UserProfileState


EXPLICIT_MEMORY_TERMS = ("请记住", "记住我", "帮我记住", "记一下", "以后记得")
CORRECTION_TERMS = ("更正", "改成", "不是", "更新")
SENSITIVE_PATTERNS = (
    r"(?:密码|验证码)",
    r"1[3-9][0-9]{9}",
    r"[0-9]{15}(?:[0-9]{2}[0-9Xx])?",
    r"\b1[3-9]\d{9}\b",
    r"\b\d{15}(?:\d{2}[0-9Xx])?\b",
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
)
TEMPORARY_EMOTION_TERMS = ("今天很难过", "现在很焦虑", "暂时压力大", "刚刚崩溃")
TOKEN_STOP_TERMS = {
    "请记住",
    "记住我",
    "帮我记住",
    "记一下",
    "以后记得",
    "更正",
    "更新",
    "改成",
    "这个",
    "那个",
    "可以",
    "帮我",
    "我的",
}
MIN_RELEVANCE_SCORE = 1.2


@dataclass(frozen=True)
class RankedUserMemory:
    row: UserMemory
    score: float

    @property
    def id(self) -> int:
        return self.row.id

    @property
    def public_id(self) -> str:
        return self.row.public_id

    @property
    def category(self) -> str:
        return self.row.category

    @property
    def memory_key(self) -> str | None:
        return self.row.memory_key

    @property
    def content(self) -> str:
        return self.row.content

    @property
    def source_message_id(self) -> int | None:
        return self.row.source_message_id


class UserMemoryService:
    def __init__(self, db: Session, settings=None):
        self.db = db
        self.settings = settings or _settings_for_jobs()

    def remember_explicit(
        self,
        user_id: int,
        text: str,
        source_message_id: int | None = None,
        *,
        updated_from_turn_id: int | None = None,
        commit: bool = True,
    ) -> UserMemory | None:
        if not any(term in text for term in EXPLICIT_MEMORY_TERMS):
            return None
        content = text
        for term in EXPLICIT_MEMORY_TERMS:
            content = content.replace(term, "")
        content = content.strip("：:，,。 ")
        if not content or self._forbidden(content):
            return None
        category = self._category(content)
        memory_key = derive_memory_key(category, content)
        if source_message_id is not None:
            existing = self.db.query(UserMemory).filter(
                UserMemory.user_id == user_id,
                UserMemory.source_message_id == source_message_id,
                UserMemory.origin == "EXPLICIT",
                UserMemory.status == "ACTIVE",
            ).first()
            if existing is not None:
                return existing
        state = self._profile_state(user_id)
        superseded = []
        if any(term in text for term in CORRECTION_TERMS):
            superseded = self._supersede_related(user_id, category, memory_key)
        latest_version = (
            self.db.query(UserMemory.version)
            .filter(UserMemory.user_id == user_id, UserMemory.memory_key == memory_key)
            .order_by(UserMemory.version.desc())
            .limit(1)
            .scalar()
            or 0
        )
        row = UserMemory(
            public_id=uuid.uuid4().hex,
            user_id=user_id,
            category=category,
            memory_key=memory_key,
            content=content[:1000],
            source_message_id=source_message_id,
            confidence=1.0,
            origin="EXPLICIT",
            version=int(latest_version) + 1,
            updated_from_turn_id=updated_from_turn_id,
            evidence_message_ids_json=(f"[{int(source_message_id)}]" if source_message_id is not None else "[]"),
            sensitivity="NORMAL",
            visibility_scope="ALL_AGENTS",
            index_status="PENDING",
            memory_epoch=state.memory_epoch,
            status="ACTIVE",
        )
        self.db.add(row)
        self.db.flush()
        state.profile_version += 1
        state.updated_at = datetime.now(UTC).replace(tzinfo=None)
        self.db.add(state)
        from app.services.memory_jobs import MemoryJobService

        jobs = MemoryJobService(self.db, self.settings)
        for old in superseded:
            jobs.enqueue_memory_delete(
                user_id=user_id,
                public_id=old.public_id,
                memory_epoch=state.memory_epoch,
            )
        jobs.enqueue_profile_index(row, state=state)
        if commit:
            self.db.commit()
            self.db.refresh(row)
        return row

    def active_for_prompt(
        self,
        user_id: int,
        current_text: str,
        limit: int = 5,
        exclude_source_message_id: int | None = None,
    ) -> list[RankedUserMemory]:
        if limit <= 0:
            return []
        now = datetime.now(UTC).replace(tzinfo=None)
        query = self.db.query(UserMemory).filter(
            UserMemory.user_id == user_id,
            UserMemory.status == "ACTIVE",
            or_(UserMemory.expires_at.is_(None), UserMemory.expires_at > now),
        )
        if exclude_source_message_id is not None:
            query = query.filter(
                or_(
                    UserMemory.source_message_id.is_(None),
                    UserMemory.source_message_id != exclude_source_message_id,
                )
            )
        rows = (
            query
            .order_by(UserMemory.updated_at.desc(), UserMemory.id.desc())
            .limit(50)
            .all()
        )
        query_tokens = lexical_tokens(current_text)
        query_domain = _query_domain(current_text)
        normalized_query = _normalized_text(current_text)
        scored = [
            RankedUserMemory(
                row=row,
                score=_relevance_score(
                    row,
                    query_tokens,
                    normalized_query,
                    query_domain,
                    now,
                ),
            )
            for row in rows
        ]
        ranked = sorted(
            (item for item in scored if item.score >= MIN_RELEVANCE_SCORE),
            key=lambda item: (
                item.score,
                item.row.updated_at,
                item.row.id,
            ),
            reverse=True,
        )
        return ranked[: min(limit, 50)]

    def list_active(self, user_id: int, limit: int = 100) -> list[UserMemory]:
        return (
            self.db.query(UserMemory)
            .filter(UserMemory.user_id == user_id, UserMemory.status == "ACTIVE")
            .order_by(UserMemory.updated_at.desc(), UserMemory.id.desc())
            .limit(max(1, min(limit, 100)))
            .all()
        )

    def delete(self, user_id: int, public_id: str, *, commit: bool = True) -> bool:
        row = (
            self.db.query(UserMemory)
            .filter(
                UserMemory.user_id == user_id,
                UserMemory.public_id == public_id,
                UserMemory.status == "ACTIVE",
            )
            .first()
        )
        if row is None:
            return False
        state = self._profile_state(user_id)
        state.memory_epoch += 1
        state.profile_version += 1
        state.updated_at = datetime.now(UTC).replace(tzinfo=None)
        (
            self.db.query(UserMemory)
            .filter(UserMemory.user_id == user_id, UserMemory.status == "ACTIVE")
            .update({UserMemory.memory_epoch: state.memory_epoch}, synchronize_session="fetch")
        )
        row.status = "DELETED"
        row.index_status = "PENDING_DELETE"
        row.memory_epoch = state.memory_epoch
        row.updated_at = datetime.now(UTC).replace(tzinfo=None)
        self.db.add_all([row, state])
        from app.services.memory_jobs import MemoryJobService

        jobs = MemoryJobService(self.db, self.settings)
        jobs.enqueue_memory_delete(
            user_id=user_id,
            public_id=row.public_id,
            memory_epoch=state.memory_epoch,
        )
        if row.source_message_id is not None:
            episodes = self.db.query(ConversationEpisode).filter(
                ConversationEpisode.user_id == user_id,
                ConversationEpisode.status == "ACTIVE",
                ConversationEpisode.source_start_message_id <= row.source_message_id,
                ConversationEpisode.source_end_message_id >= row.source_message_id,
            ).all()
            for episode in episodes:
                episode.status = "DELETED"
                episode.index_status = "PENDING_DELETE"
                episode.updated_at = datetime.now(UTC).replace(tzinfo=None)
                jobs.enqueue_memory_delete(
                    user_id=user_id,
                    public_id=episode.public_id,
                    memory_epoch=state.memory_epoch,
                    document_kind="episode",
                )
                summary_row = self.db.query(ConversationSummary).filter(
                    ConversationSummary.session_id == episode.session_id
                ).first()
                if summary_row is not None:
                    summary_row.summary_json = json.dumps(
                        _remove_summary_source(summary_row.summary_json, row.source_message_id),
                        ensure_ascii=False,
                    )
                    summary_row.version += 1
                    summary_row.degraded = True
                    summary_row.last_error = "MEMORY_DELETION_REDACTION"
                    summary_row.updated_at = datetime.now(UTC).replace(tzinfo=None)
                    self.db.add(summary_row)
        for active in self.db.query(UserMemory).filter(
            UserMemory.user_id == user_id,
            UserMemory.status == "ACTIVE",
            UserMemory.id != row.id,
        ).all():
            active.index_status = "PENDING"
            jobs.enqueue_profile_index(active, state=state)
        if commit:
            self.db.commit()
        return True

    def mark_used(
        self,
        memory_ids: list[int] | tuple[int, ...],
        minimum_interval_hours: int = 24,
    ) -> None:
        ids = []
        for item in memory_ids:
            try:
                value = int(item)
            except (TypeError, ValueError):
                continue
            if value > 0:
                ids.append(value)
        ids = sorted(set(ids))
        if not ids:
            return
        now = datetime.now(UTC).replace(tzinfo=None)
        from datetime import timedelta

        cutoff = now - timedelta(hours=max(1, int(minimum_interval_hours)))
        (
            self.db.query(UserMemory)
            .filter(
                UserMemory.id.in_(ids),
                UserMemory.status == "ACTIVE",
                or_(UserMemory.last_used_at.is_(None), UserMemory.last_used_at < cutoff),
            )
            .update({UserMemory.last_used_at: now}, synchronize_session=False)
        )
        self.db.commit()

    def _supersede_related(self, user_id: int, category: str, memory_key: str) -> list[UserMemory]:
        rows = (
            self.db.query(UserMemory)
            .filter(
                UserMemory.user_id == user_id,
                UserMemory.category == category,
                UserMemory.status == "ACTIVE",
                or_(
                    UserMemory.memory_key == memory_key,
                    UserMemory.memory_key.is_(None),
                ),
            )
            .all()
        )
        superseded = []
        for row in rows:
            if row.memory_key == memory_key or (
                row.memory_key is None
                and derive_memory_key(row.category, row.content) == memory_key
            ):
                row.status = "SUPERSEDED"
                row.index_status = "PENDING_DELETE"
                row.updated_at = datetime.now(UTC).replace(tzinfo=None)
                superseded.append(row)
        return superseded

    def _profile_state(self, user_id: int) -> UserProfileState:
        query = (
            self.db.query(UserProfileState)
            .filter(UserProfileState.user_id == user_id)
            .with_for_update()
        )
        row = query.first()
        if row is None:
            row = UserProfileState(user_id=user_id, profile_version=0, extracted_until_message_id=0, memory_epoch=0)
            try:
                with self.db.begin_nested():
                    self.db.add(row)
                    self.db.flush()
            except IntegrityError:
                row = query.first()
                if row is None:
                    raise
        return row

    @staticmethod
    def _category(content: str) -> str:
        if any(term in content for term in ("校区", "专业", "学院")):
            return "PROFILE"
        if any(term in content for term in ("喜欢", "偏好", "习惯", "方式")):
            return "LEARNING_PREFERENCE"
        if any(term in content for term in ("课程", "截止", "时间", "每周")):
            return "LONG_TERM_CONSTRAINT"
        return "EXPLICIT"

    @staticmethod
    def _forbidden(content: str) -> bool:
        return any(re.search(pattern, content, re.IGNORECASE) for pattern in SENSITIVE_PATTERNS) or any(
            term in content for term in TEMPORARY_EMOTION_TERMS
        )


def derive_memory_key(category: str, content: str) -> str:
    normalized_category = (category or "EXPLICIT").upper()
    rules = {
        "PROFILE": (
            (("校区",), "campus"),
            (("专业",), "major"),
            (("学院",), "college"),
        ),
        "LEARNING_PREFERENCE": (
            (("学习时间", "晚上学习", "早上学习", "下午学习", "夜里学习"), "study_time"),
            (("清单", "图表", "视频", "文字", "学习方式", "呈现方式"), "learning_format"),
            (("图书馆", "宿舍学习", "安静", "学习环境"), "study_environment"),
        ),
        "LONG_TERM_CONSTRAINT": (
            (("课程", "科目"), "course"),
            (("截止", "考试日期", "考试时间"), "deadline"),
            (("每周", "周一", "周二", "周三", "周四", "周五", "周末"), "weekly_schedule"),
        ),
    }
    for terms, key in rules.get(normalized_category, ()):
        if any(term in content for term in terms):
            return f"{normalized_category}:{key}"
    tokens = sorted(lexical_tokens(content))
    if tokens:
        topic = "_".join(tokens[:4])[:80]
    else:
        compact = _normalized_text(content)
        topic = hashlib.sha256(compact.encode("utf-8")).hexdigest()[:16]
    return f"{normalized_category}:topic:{topic}"[:128]


def lexical_tokens(text: str) -> set[str]:
    normalized = (text or "").lower()
    for term in TOKEN_STOP_TERMS:
        normalized = normalized.replace(term, " ")
    tokens = set(re.findall(r"[a-z0-9_]+", normalized))
    for segment in _cjk_segments(normalized):
        if len(segment) == 1:
            continue
        if len(segment) <= 4:
            tokens.add(segment)
        tokens.update(segment[index:index + 2] for index in range(len(segment) - 1))
        for keyword in (
            "学习",
            "时间",
            "清单",
            "图表",
            "视频",
            "课程",
            "截止",
            "专业",
            "学院",
            "校区",
            "宿舍",
            "图书馆",
            "周末",
        ):
            if keyword in segment:
                tokens.add(keyword)
    return {token for token in tokens if token and token not in TOKEN_STOP_TERMS}


def _relevance_score(
    row: UserMemory,
    query_tokens: set[str],
    normalized_query: str,
    query_domain: str | None,
    now: datetime,
) -> float:
    memory_text = _normalized_text(row.content)
    memory_tokens = lexical_tokens(row.content)
    exact = 0.0
    if normalized_query and 2 <= len(normalized_query) <= 32:
        exact = 1.0 if normalized_query in memory_text or memory_text in normalized_query else 0.0
    union = query_tokens | memory_tokens
    jaccard = len(query_tokens & memory_tokens) / len(union) if union else 0.0
    domain_match = 1.0 if query_domain and query_domain == row.category else 0.0
    updated_at = row.updated_at or row.created_at or now
    age_days = max(0, (now - updated_at).days)
    recency = 0.4 if age_days <= 30 else 0.2 if age_days <= 180 else 0.0
    confidence = max(0.0, min(1.0, float(row.confidence or 0.0)))
    return round(4.0 * exact + 3.0 * jaccard + 1.5 * domain_match + recency + 0.2 * confidence, 6)


def _query_domain(text: str) -> str | None:
    if any(term in text for term in ("学习", "复习", "清单", "图表", "视频", "习惯", "偏好")):
        return "LEARNING_PREFERENCE"
    if any(term in text for term in ("课程", "截止", "考试日期", "每周", "时间安排")):
        return "LONG_TERM_CONSTRAINT"
    if any(term in text for term in ("校区", "专业", "学院")):
        return "PROFILE"
    return None


def _normalized_text(text: str) -> str:
    return re.sub(r"[\s，,。.!！?？:：；;]+", "", (text or "").lower())


def _cjk_segments(text: str) -> list[str]:
    segments = []
    current = []
    for character in text:
        codepoint = ord(character)
        if (
            0x3400 <= codepoint <= 0x4DBF
            or 0x4E00 <= codepoint <= 0x9FFF
            or 0xF900 <= codepoint <= 0xFAFF
        ):
            current.append(character)
        elif current:
            segments.append("".join(current))
            current = []
    if current:
        segments.append("".join(current))
    return segments


def _settings_for_jobs():
    # Local import avoids constructing settings for requests that do not touch memory.
    from app.core.config import get_settings

    return get_settings()


def _remove_summary_source(raw: str, source_message_id: int) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in value.items():
        if isinstance(item, dict) and item.get("source_message_id") == source_message_id:
            result[key] = None
        elif isinstance(item, list):
            result[key] = [
                entry
                for entry in item
                if not isinstance(entry, dict) or entry.get("source_message_id") != source_message_id
            ]
        else:
            result[key] = item
    return result
