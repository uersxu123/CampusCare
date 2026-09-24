from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.entities import ChatMessage, ConversationEpisode, UserMemory, UserProfileState
from app.services.memory import MemoryMessage, RedisShortTermMemoryStore
from app.services.memory_vector_store import MemoryVectorStore
from app.services.user_memory import RankedUserMemory, UserMemoryService, lexical_tokens


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetrievedEpisode:
    episode_id: int
    public_id: str
    session_id: int
    source_start_message_id: int
    source_end_message_id: int
    summary_text: str
    content_hash: str
    version: int
    relevance_score: float
    is_tail: bool


@dataclass(frozen=True)
class RetrievedProfileFact:
    memory_id: int
    public_id: str
    category: str
    memory_key: str | None
    content: str
    relevance_score: float
    source_message_id: int | None
    origin: str
    version: int
    sensitivity: str
    visibility_scope: str
    memory_epoch: int


@dataclass(frozen=True)
class MemoryRetrievalResult:
    recent_messages: tuple[MemoryMessage, ...]
    bridge_messages: tuple[MemoryMessage, ...]
    relevant_history: tuple[RetrievedEpisode, ...]
    user_profile: tuple[RetrievedProfileFact, ...]
    profile_version: int
    redis_status: str
    chroma_status: str
    fallback_sources: tuple[str, ...]
    summary_lag_count: int
    context_load_ms: int


class MemoryRetrievalService:
    def __init__(
        self,
        db: Session,
        settings: Settings,
        cache: RedisShortTermMemoryStore | None = None,
        vector_store: MemoryVectorStore | None = None,
    ):
        self.db = db
        self.settings = settings
        self.cache = cache or RedisShortTermMemoryStore(settings)
        self.vector_store = vector_store

    def retrieve(
        self,
        *,
        user_id: int,
        session_id: int,
        session_public_id: str,
        current_message_id: int,
        query_text: str,
        summary_covered_until_message_id: int,
    ) -> MemoryRetrievalResult:
        started = time.perf_counter()
        deadline = started + max(0.05, float(self.settings.memory_read_deadline_ms) / 1000.0)
        fallbacks: list[str] = []
        recent, bridge, redis_status = self._working_memory(
            user_id=user_id,
            session_id=session_id,
            session_public_id=session_public_id,
            current_message_id=current_message_id,
            covered_until=summary_covered_until_message_id,
            fallbacks=fallbacks,
        )
        history, chroma_status = self._history(
            user_id=user_id,
            current_session_id=session_id,
            query_text=query_text,
            fallbacks=fallbacks,
            allow_vector=time.perf_counter() < deadline,
        )
        profile, profile_version, profile_chroma_status = self._profile(
            user_id=user_id,
            current_message_id=current_message_id,
            query_text=query_text,
            fallbacks=fallbacks,
            allow_vector=time.perf_counter() < deadline,
        )
        if chroma_status == "disabled":
            chroma_status = profile_chroma_status
        elif profile_chroma_status not in {"hit", "empty", "disabled"}:
            chroma_status = profile_chroma_status
        return MemoryRetrievalResult(
            recent_messages=recent,
            bridge_messages=bridge,
            relevant_history=history,
            user_profile=profile,
            profile_version=profile_version,
            redis_status=redis_status,
            chroma_status=chroma_status,
            fallback_sources=tuple(dict.fromkeys(fallbacks)),
            summary_lag_count=len(bridge),
            context_load_ms=max(0, int((time.perf_counter() - started) * 1000)),
        )

    def _working_memory(
        self,
        *,
        user_id: int,
        session_id: int,
        session_public_id: str,
        current_message_id: int,
        covered_until: int,
        fallbacks: list[str],
    ) -> tuple[tuple[MemoryMessage, ...], tuple[MemoryMessage, ...], str]:
        limit = max(2, int(self.settings.context_recent_message_limit))
        rows = (
            self.db.query(ChatMessage)
            .filter(
                ChatMessage.user_id == user_id,
                ChatMessage.session_id == session_id,
                ChatMessage.id < current_message_id,
            )
            .order_by(ChatMessage.id.asc())
            .all()
        )
        sql_recent_rows = rows[-limit:]
        sql_recent = tuple(_message(row) for row in sql_recent_rows)
        cache_records, cache_status = self._cache_records(session_public_id, user_id)
        usable: dict[int, MemoryMessage] = {}
        for item in cache_records:
            if item.id is None or item.id <= 0 or item.id >= current_message_id:
                continue
            if item.role.lower() not in {"user", "assistant"}:
                continue
            usable[int(item.id)] = MemoryMessage(int(item.id), item.role.lower(), item.content)
        cached = tuple(usable[key] for key in sorted(usable))
        expected_ids = tuple(row.id for row in sql_recent_rows)
        cached_by_id = {item.id: item for item in cached}
        cache_complete = bool(expected_ids) and all(
            message_id in cached_by_id
            and cached_by_id[message_id].content == row.content
            and cached_by_id[message_id].role == row.role.lower()
            for message_id, row in zip(expected_ids, sql_recent_rows)
        )
        if not expected_ids:
            recent = ()
            effective_status = "empty"
        elif cache_complete:
            recent = tuple(cached_by_id[message_id] for message_id in expected_ids)
            effective_status = "hit"
        else:
            recent = sql_recent
            effective_status = f"{cache_status}_sql_fallback"
            fallbacks.append("redis_recent_messages")
            self._safe_cache_backfill(session_public_id, user_id, list(sql_recent))

        recent_ids = {item.id for item in recent if item.id is not None}
        lag = [
            _message(row)
            for row in rows
            if row.id > int(covered_until or 0) and row.id not in recent_ids
        ]
        bridge = self._fit_bridge(lag)
        if len(bridge) < len(lag):
            fallbacks.append("summary_lag_emergency_compaction")
        elif bridge:
            fallbacks.append("summary_lag_bridge")
        return recent, bridge, effective_status

    def _cache_records(self, session_public_id: str, user_id: int) -> tuple[list[MemoryMessage], str]:
        loader = getattr(self.cache, "load_recent_records_with_status", None)
        if not callable(loader):
            return [], "unavailable"
        try:
            try:
                records, status = loader(session_public_id, user_id=user_id)
            except TypeError:
                records, status = loader(session_public_id)
            return list(records), str(status)
        except Exception as exc:
            logger.warning("Redis 工作记忆读取失败: %s", exc)
            return [], "error"

    def _safe_cache_backfill(self, session_public_id: str, user_id: int, records: list[MemoryMessage]) -> None:
        replacer = getattr(self.cache, "merge_records", None)
        if not callable(replacer) or not records:
            return
        try:
            replacer(session_public_id, records, user_id=user_id)
        except Exception as exc:
            logger.warning("Redis 工作记忆安全回填失败: %s", exc)

    def _fit_bridge(self, rows: list[MemoryMessage]) -> tuple[MemoryMessage, ...]:
        budget = max(0, int(self.settings.memory_working_max_tokens))
        recent_budget = max(0, budget - 600)
        selected: list[MemoryMessage] = []
        used = 0
        for item in reversed(rows):
            tokens = _rough_tokens(item.content)
            if selected and used + tokens > recent_budget:
                continue
            if not selected and tokens > recent_budget:
                item = MemoryMessage(item.id, item.role, item.content[-max(200, recent_budget * 3):])
                tokens = _rough_tokens(item.content)
            selected.append(item)
            used += tokens
        return tuple(reversed(selected))

    def _history(
        self,
        *,
        user_id: int,
        current_session_id: int,
        query_text: str,
        fallbacks: list[str],
        allow_vector: bool,
    ) -> tuple[tuple[RetrievedEpisode, ...], str]:
        if not self.settings.memory_v3_enabled or not self.settings.memory_episodic_enabled:
            return (), "disabled"
        now = datetime.now(UTC).replace(tzinfo=None)
        candidates = (
            self.db.query(ConversationEpisode)
            .filter(
                ConversationEpisode.user_id == user_id,
                ConversationEpisode.session_id != current_session_id,
                ConversationEpisode.status == "ACTIVE",
                or_(ConversationEpisode.expires_at.is_(None), ConversationEpisode.expires_at > now),
            )
            .order_by(ConversationEpisode.updated_at.desc(), ConversationEpisode.id.desc())
            .limit(max(1, int(self.settings.memory_sql_fallback_candidate_limit)))
            .all()
        )
        if not candidates:
            return (), "empty"
        by_public_id = {row.public_id: row for row in candidates}
        scores: dict[str, float] = {}
        status = "empty"
        store = self.vector_store
        try:
            if not allow_vector:
                raise _ReadDeadlineExceeded()
            if not any(row.index_status == "INDEXED" for row in candidates):
                raise _IndexNotReady()
            store = store or MemoryVectorStore(self.settings)
            self.vector_store = store
            hits = store.query_episodes(
                user_id=user_id,
                query_text=query_text,
                top_k=max(1, int(self.settings.memory_history_candidate_k)),
                exclude_session_id=current_session_id,
            )
            for hit in hits:
                public_id = hit.document_id.removeprefix("episode:")
                row = by_public_id.get(public_id)
                if row is None:
                    continue
                if int(hit.metadata.get("user_id", -1)) != user_id:
                    continue
                if int(hit.metadata.get("version", -1)) != int(row.version):
                    continue
                if str(hit.metadata.get("content_hash", "")) != row.content_hash:
                    continue
                scores[public_id] = hit.score
            status = "hit" if scores else "empty"
        except _ReadDeadlineExceeded:
            status = "sql_fallback"
            fallbacks.append("memory_read_deadline")
        except _IndexNotReady:
            status = "sql_fallback"
            fallbacks.append("chroma_episodic_not_ready")
        except Exception as exc:
            logger.warning("Chroma 情景记忆查询失败，使用 SQL 回退: %s", exc)
            status = "sql_fallback"
            fallbacks.append("chroma_episodic")
        if not scores:
            scores = {row.public_id: _lexical_score(query_text, row.summary_text) for row in candidates}
        ranked = sorted(
            (row for row in candidates if scores.get(row.public_id, 0.0) > 0.0),
            key=lambda row: (scores[row.public_id], row.updated_at, row.id),
            reverse=True,
        )
        selected: list[RetrievedEpisode] = []
        used = 0
        max_tokens = max(0, int(self.settings.memory_history_max_tokens))
        for row in ranked:
            tokens = _rough_tokens(row.summary_text)
            if selected and used + tokens > max_tokens:
                continue
            selected.append(RetrievedEpisode(
                episode_id=row.id,
                public_id=row.public_id,
                session_id=row.session_id,
                source_start_message_id=row.source_start_message_id,
                source_end_message_id=row.source_end_message_id,
                summary_text=row.summary_text,
                content_hash=row.content_hash,
                version=row.version,
                relevance_score=round(scores[row.public_id], 6),
                is_tail=bool(row.is_tail),
            ))
            used += tokens
            if len(selected) >= int(self.settings.memory_history_top_k):
                break
        return tuple(selected), status

    def _profile(
        self,
        *,
        user_id: int,
        current_message_id: int,
        query_text: str,
        fallbacks: list[str],
        allow_vector: bool,
    ) -> tuple[tuple[RetrievedProfileFact, ...], int, str]:
        state = self.db.query(UserProfileState).filter(UserProfileState.user_id == user_id).first()
        profile_version = int(state.profile_version if state else 0)
        ranked = UserMemoryService(self.db).active_for_prompt(
            user_id=user_id,
            current_text=query_text,
            limit=max(0, int(self.settings.context_user_memory_max_items)),
            exclude_source_message_id=current_message_id,
        )
        if not ranked:
            return (), profile_version, "empty"
        vector_scores: dict[str, float] = {}
        status = "empty"
        try:
            if not allow_vector:
                raise _ReadDeadlineExceeded()
            if not any(item.row.index_status == "INDEXED" for item in ranked):
                raise _IndexNotReady()
            store = self.vector_store or MemoryVectorStore(self.settings)
            hits = store.query_profiles(
                user_id=user_id,
                query_text=query_text,
                top_k=max(int(self.settings.context_user_memory_max_items) * 2, 1),
            )
            active = {item.public_id: item for item in ranked}
            for hit in hits:
                public_id = str(hit.metadata.get("memory_public_id") or "")
                item = active.get(public_id)
                if item is None or int(hit.metadata.get("user_id", -1)) != user_id:
                    continue
                if int(hit.metadata.get("version", -1)) != int(item.row.version):
                    continue
                if int(hit.metadata.get("memory_epoch", -1)) != int(item.row.memory_epoch):
                    continue
                vector_scores[public_id] = hit.score
            status = "hit" if vector_scores else "empty"
        except _ReadDeadlineExceeded:
            fallbacks.append("memory_read_deadline")
            status = "sql_fallback"
        except _IndexNotReady:
            fallbacks.append("chroma_profile_not_ready")
            status = "sql_fallback"
        except Exception as exc:
            logger.warning("Chroma 用户画像查询失败，使用 SQL 最新版本: %s", exc)
            fallbacks.append("chroma_profile")
            status = "sql_fallback"
        ordered = sorted(
            ranked,
            key=lambda item: (
                item.row.origin == "EXPLICIT",
                vector_scores.get(item.public_id, item.score),
                item.row.updated_at,
                item.id,
            ),
            reverse=True,
        )
        facts = tuple(self._profile_fact(item, vector_scores.get(item.public_id)) for item in ordered)
        return facts, profile_version, status

    @staticmethod
    def _profile_fact(item: RankedUserMemory, vector_score: float | None) -> RetrievedProfileFact:
        row = item.row
        return RetrievedProfileFact(
            memory_id=row.id,
            public_id=row.public_id,
            category=row.category,
            memory_key=row.memory_key,
            content=row.content,
            relevance_score=round(vector_score if vector_score is not None else item.score, 6),
            source_message_id=row.source_message_id,
            origin=row.origin,
            version=row.version,
            sensitivity=row.sensitivity,
            visibility_scope=row.visibility_scope,
            memory_epoch=row.memory_epoch,
        )


def _message(row: ChatMessage) -> MemoryMessage:
    return MemoryMessage(id=row.id, role=row.role.lower(), content=row.content)


def _lexical_score(query: str, document: str) -> float:
    query_tokens = lexical_tokens(query)
    document_tokens = lexical_tokens(document)
    if not query_tokens or not document_tokens:
        return 0.0
    overlap = len(query_tokens & document_tokens)
    if overlap <= 0:
        return 0.0
    return overlap / math.sqrt(len(query_tokens) * len(document_tokens))


def _rough_tokens(text: str) -> int:
    cjk = sum(1 for char in text if 0x3400 <= ord(char) <= 0x9FFF)
    return cjk + math.ceil((len(text) - cjk) / 4) + 4


class _IndexNotReady(RuntimeError):
    pass


class _ReadDeadlineExceeded(RuntimeError):
    pass
