from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Callable

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import SessionLocal
from app.models.entities import (
    ChatMessage,
    ChatSession,
    ChatTurn,
    ConversationEpisode,
    ConversationSummary,
    MemoryJob,
    UserMemory,
    UserProfileState,
)
from app.services.context_builder import estimate_tokens
from app.services.memory import (
    ConversationSummaryRepository,
    EMPTY_STRUCTURED_SUMMARY,
    normalize_summary_v2,
    update_summary_v2,
)
from app.services.memory_vector_store import MemoryVectorStore
from app.services.profile_extraction import ProfileExtractionService
from app.services.user_memory import derive_memory_key


logger = logging.getLogger(__name__)

PENDING = "PENDING"
RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"

COMPACT_SESSION = "COMPACT_SESSION"
UPDATE_PROFILE = "UPDATE_PROFILE"
INDEX_EPISODE = "INDEX_EPISODE"
INDEX_PROFILE = "INDEX_PROFILE"
FINALIZE_SESSION = "FINALIZE_SESSION"
DELETE_MEMORY = "DELETE_MEMORY"


class MemoryJobRetry(RuntimeError):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class MemoryJobService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings

    def enqueue_completed_turn(
        self,
        *,
        user_id: int,
        session_id: int,
        turn_id: int,
        target_message_id: int,
    ) -> list[MemoryJob]:
        state = self._profile_state(user_id)
        jobs = [
            self.enqueue(
                kind=COMPACT_SESSION,
                dedupe_key=f"{COMPACT_SESSION}:{session_id}:{turn_id}",
                user_id=user_id,
                session_id=session_id,
                turn_id=turn_id,
                target_message_id=target_message_id,
            )
        ]
        if self.settings.memory_profile_extraction_enabled:
            jobs.append(self.enqueue(
                kind=UPDATE_PROFILE,
                dedupe_key=f"{UPDATE_PROFILE}:{user_id}:{turn_id}",
                user_id=user_id,
                session_id=session_id,
                turn_id=turn_id,
                target_message_id=target_message_id,
                expected_version=state.profile_version,
                memory_epoch=state.memory_epoch,
            ))
        return jobs

    def enqueue_finalize_session(self, *, user_id: int, session_id: int, target_message_id: int) -> MemoryJob:
        state = self._profile_state(user_id)
        return self.enqueue(
            kind=FINALIZE_SESSION,
            dedupe_key=f"{FINALIZE_SESSION}:{session_id}:{target_message_id}",
            user_id=user_id,
            session_id=session_id,
            target_message_id=target_message_id,
            memory_epoch=state.memory_epoch,
        )

    def enqueue_profile_index(self, memory: UserMemory, *, state: UserProfileState | None = None) -> MemoryJob:
        state = state or self._profile_state(memory.user_id)
        return self.enqueue(
            kind=INDEX_PROFILE,
            dedupe_key=f"{INDEX_PROFILE}:{memory.public_id}:{memory.version}",
            user_id=memory.user_id,
            target_message_id=memory.source_message_id,
            expected_version=memory.version,
            memory_epoch=state.memory_epoch,
            payload={"memory_public_id": memory.public_id},
        )

    def enqueue_memory_delete(
        self,
        *,
        user_id: int,
        public_id: str,
        memory_epoch: int,
        document_kind: str = "profile",
    ) -> MemoryJob:
        return self.enqueue(
            kind=DELETE_MEMORY,
            dedupe_key=f"{DELETE_MEMORY}:{document_kind}:{user_id}:{public_id}:{memory_epoch}",
            user_id=user_id,
            memory_epoch=memory_epoch,
            payload={"document_kind": document_kind, "public_id": public_id},
        )

    def enqueue(
        self,
        *,
        kind: str,
        dedupe_key: str,
        user_id: int,
        session_id: int | None = None,
        turn_id: int | None = None,
        target_message_id: int | None = None,
        expected_version: int | None = None,
        memory_epoch: int = 0,
        payload: dict | None = None,
        run_after: datetime | None = None,
    ) -> MemoryJob:
        existing = self.db.query(MemoryJob).filter(MemoryJob.dedupe_key == dedupe_key).first()
        if existing is not None:
            return existing
        row = MemoryJob(
            public_id=uuid.uuid4().hex,
            dedupe_key=dedupe_key[:191],
            kind=kind,
            user_id=user_id,
            session_id=session_id,
            turn_id=turn_id,
            target_message_id=target_message_id,
            payload_json=json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")),
            status=PENDING,
            attempts=0,
            max_attempts=max(1, int(self.settings.memory_worker_max_attempts)),
            run_after=run_after or utcnow(),
            expected_version=expected_version,
            memory_epoch=memory_epoch,
        )
        try:
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
        except IntegrityError:
            existing = self.db.query(MemoryJob).filter(MemoryJob.dedupe_key == dedupe_key).first()
            if existing is None:
                raise
            return existing
        return row

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


class MemoryWorker:
    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: Callable[[], Session] = SessionLocal,
        vector_store_factory: Callable[[Settings], MemoryVectorStore] = MemoryVectorStore,
        worker_id: str | None = None,
    ):
        self.settings = settings
        self.session_factory = session_factory
        self.vector_store_factory = vector_store_factory
        self._vector_store = None
        self.worker_id = worker_id or f"memory-worker-{uuid.uuid4().hex[:10]}"
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.settings.memory_v3_enabled or not self.settings.memory_worker_enabled or self.thread is not None:
            return
        self.thread = threading.Thread(target=self._loop, name=self.worker_id, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
        close = getattr(self._vector_store, "close", None)
        if callable(close):
            close()

    def wake(self) -> None:
        self.wake_event.set()

    def run_once(self) -> bool:
        job_id = self._claim_one()
        if job_id is None:
            return False
        self._run_claimed(job_id)
        return True

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            worked = False
            try:
                for _ in range(max(1, int(self.settings.memory_worker_batch_size))):
                    if not self.run_once():
                        break
                    worked = True
                if not worked:
                    self._enqueue_idle_finalizers()
            except Exception:
                logger.exception("记忆后台任务调度失败")
            self.wake_event.wait(max(0.1, float(self.settings.memory_worker_poll_interval_seconds)))
            self.wake_event.clear()

    def _claim_one(self) -> int | None:
        db = self.session_factory()
        try:
            now = utcnow()
            db.query(MemoryJob).filter(
                MemoryJob.status == RUNNING,
                MemoryJob.lease_expires_at.is_not(None),
                MemoryJob.lease_expires_at < now,
            ).update({
                MemoryJob.status: PENDING,
                MemoryJob.lease_owner: None,
                MemoryJob.lease_expires_at: None,
                MemoryJob.last_error: "租约到期后恢复",
                MemoryJob.run_after: now,
                MemoryJob.updated_at: now,
            }, synchronize_session=False)
            db.commit()
            candidate_ids = [
                value for (value,) in (
                    db.query(MemoryJob.id)
                    .filter(MemoryJob.status == PENDING, MemoryJob.run_after <= now)
                    .order_by(MemoryJob.run_after.asc(), MemoryJob.id.asc())
                    .limit(8)
                    .all()
                )
            ]
            for job_id in candidate_ids:
                claimed = db.query(MemoryJob).filter(
                    MemoryJob.id == job_id,
                    MemoryJob.status == PENDING,
                    MemoryJob.run_after <= now,
                ).update({
                    MemoryJob.status: RUNNING,
                    MemoryJob.lease_owner: self.worker_id,
                    MemoryJob.lease_expires_at: now + timedelta(seconds=max(5, int(self.settings.memory_worker_lease_seconds))),
                    MemoryJob.attempts: MemoryJob.attempts + 1,
                    MemoryJob.updated_at: now,
                }, synchronize_session=False)
                db.commit()
                if claimed == 1:
                    return int(job_id)
            return None
        finally:
            db.close()

    def _run_claimed(self, job_id: int) -> None:
        db = self.session_factory()
        try:
            job = db.get(MemoryJob, job_id)
            if job is None or job.status != RUNNING or job.lease_owner != self.worker_id:
                return
            self._execute(db, job)
            job.status = SUCCEEDED
            job.lease_owner = None
            job.lease_expires_at = None
            job.last_error = ""
            job.finished_at = utcnow()
            job.updated_at = utcnow()
            db.add(job)
            db.commit()
        except Exception as exc:
            db.rollback()
            self._record_failure(db, job_id, exc)
        finally:
            db.close()

    def _execute(self, db: Session, job: MemoryJob) -> None:
        if job.kind == COMPACT_SESSION:
            self._compact_session(db, job)
        elif job.kind == FINALIZE_SESSION:
            self._finalize_session(db, job)
        elif job.kind == UPDATE_PROFILE:
            self._update_profile(db, job)
        elif job.kind == INDEX_EPISODE:
            self._index_episode(db, job)
        elif job.kind == INDEX_PROFILE:
            self._index_profile(db, job)
        elif job.kind == DELETE_MEMORY:
            self._delete_vector(db, job)
        else:
            raise ValueError(f"未知记忆任务类型: {job.kind}")

    def _compact_session(self, db: Session, job: MemoryJob) -> None:
        if job.session_id is None or job.target_message_id is None:
            raise ValueError("压缩任务缺少会话或消息边界")
        session = db.get(ChatSession, job.session_id)
        if session is None or session.user_id != job.user_id:
            raise ValueError("压缩任务用户与会话不匹配")
        repository = ConversationSummaryRepository(db)
        row = repository.get(session.id) or repository.create(session.id)
        messages = (
            db.query(ChatMessage)
            .filter(
                ChatMessage.user_id == job.user_id,
                ChatMessage.session_id == session.id,
                ChatMessage.id > row.covered_until_message_id,
                ChatMessage.id <= job.target_message_id,
            )
            .order_by(ChatMessage.id.asc())
            .all()
        )
        messages = self._without_suppressed_memories(db, job.user_id, messages)
        recent_limit = max(2, int(self.settings.context_recent_message_limit))
        compressible = messages[:-recent_limit]
        if not compressible:
            return
        token_count = sum(estimate_tokens(item.content) for item in compressible)
        if (
            len(compressible) < int(self.settings.memory_compaction_min_messages)
            and token_count < int(self.settings.memory_compaction_max_delta_tokens)
        ):
            return
        prior = normalize_summary_v2(row.summary_json)
        summary = update_summary_v2(prior, compressible)
        episode = self._upsert_episode(db, session, compressible, is_tail=False)
        if not repository.compare_and_swap(
            row.id,
            row.version,
            compressible[-1].id,
            summary,
            True,
            "RULE_FALLBACK",
        ):
            raise MemoryJobRetry("累计摘要版本冲突")
        self._retire_overlapping_tails(db, episode)
        MemoryJobService(db, self.settings).enqueue(
            kind=INDEX_EPISODE,
            dedupe_key=f"{INDEX_EPISODE}:{episode.public_id}:{episode.version}",
            user_id=job.user_id,
            session_id=session.id,
            target_message_id=episode.source_end_message_id,
            expected_version=episode.version,
            memory_epoch=job.memory_epoch,
            payload={"episode_public_id": episode.public_id},
        )

    def _finalize_session(self, db: Session, job: MemoryJob) -> None:
        if job.session_id is None or job.target_message_id is None:
            raise ValueError("收尾任务缺少会话或消息边界")
        session = db.get(ChatSession, job.session_id)
        if session is None or session.user_id != job.user_id:
            raise ValueError("收尾任务用户与会话不匹配")
        latest_end = db.query(ConversationEpisode.source_end_message_id).filter(
            ConversationEpisode.session_id == session.id,
            ConversationEpisode.status == "ACTIVE",
        ).order_by(ConversationEpisode.source_end_message_id.desc()).limit(1).scalar() or 0
        messages = (
            db.query(ChatMessage)
            .filter(
                ChatMessage.user_id == job.user_id,
                ChatMessage.session_id == session.id,
                ChatMessage.id > int(latest_end),
                ChatMessage.id <= job.target_message_id,
            )
            .order_by(ChatMessage.id.asc())
            .all()
        )
        messages = self._without_suppressed_memories(db, job.user_id, messages)
        if not messages:
            return
        episode = self._upsert_episode(db, session, messages, is_tail=True)
        MemoryJobService(db, self.settings).enqueue(
            kind=INDEX_EPISODE,
            dedupe_key=f"{INDEX_EPISODE}:{episode.public_id}:{episode.version}",
            user_id=job.user_id,
            session_id=session.id,
            target_message_id=episode.source_end_message_id,
            expected_version=episode.version,
            memory_epoch=job.memory_epoch,
            payload={"episode_public_id": episode.public_id},
        )

    def _upsert_episode(
        self,
        db: Session,
        session: ChatSession,
        messages: list[ChatMessage],
        *,
        is_tail: bool,
    ) -> ConversationEpisode:
        start_id = int(messages[0].id)
        end_id = int(messages[-1].id)
        public_id = f"ep-{session.id}-{start_id}-{end_id}-v1"
        independent = update_summary_v2(dict(EMPTY_STRUCTURED_SUMMARY), messages)
        summary_text = json.dumps(independent, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(summary_text.encode("utf-8")).hexdigest()
        episode = db.query(ConversationEpisode).filter(
            ConversationEpisode.session_id == session.id,
            ConversationEpisode.source_start_message_id == start_id,
            ConversationEpisode.source_end_message_id == end_id,
            ConversationEpisode.schema_version == 1,
        ).first()
        if episode is None:
            episode = ConversationEpisode(
                public_id=public_id,
                user_id=session.user_id,
                session_id=session.id,
                source_start_message_id=start_id,
                source_end_message_id=end_id,
                summary_text=summary_text,
                facts_json=summary_text,
                content_hash=digest,
                version=1,
                schema_version=1,
                status="ACTIVE",
                index_status="PENDING",
                embedding_version=self.settings.memory_embedding_version,
                is_tail=is_tail,
            )
            db.add(episode)
            db.flush()
        elif episode.content_hash != digest or episode.status != "ACTIVE":
            episode.summary_text = summary_text
            episode.facts_json = summary_text
            episode.content_hash = digest
            episode.version += 1
            episode.status = "ACTIVE"
            episode.index_status = "PENDING"
            episode.is_tail = is_tail
            episode.updated_at = utcnow()
            db.add(episode)
            db.flush()
        return episode

    def _retire_overlapping_tails(self, db: Session, episode: ConversationEpisode) -> None:
        tails = db.query(ConversationEpisode).filter(
            ConversationEpisode.session_id == episode.session_id,
            ConversationEpisode.id != episode.id,
            ConversationEpisode.status == "ACTIVE",
            ConversationEpisode.is_tail.is_(True),
            ConversationEpisode.source_start_message_id <= episode.source_end_message_id,
            ConversationEpisode.source_end_message_id >= episode.source_start_message_id,
        ).all()
        jobs = MemoryJobService(db, self.settings)
        for tail in tails:
            tail.status = "SUPERSEDED"
            tail.index_status = "PENDING_DELETE"
            tail.updated_at = utcnow()
            db.add(tail)
            jobs.enqueue_memory_delete(
                user_id=tail.user_id,
                public_id=tail.public_id,
                memory_epoch=0,
                document_kind="episode",
            )

    def _update_profile(self, db: Session, job: MemoryJob) -> None:
        if job.target_message_id is None:
            raise ValueError("画像任务缺少消息边界")
        jobs = MemoryJobService(db, self.settings)
        state = jobs._profile_state(job.user_id)
        if job.memory_epoch != state.memory_epoch:
            return
        messages = (
            db.query(ChatMessage)
            .filter(
                ChatMessage.user_id == job.user_id,
                ChatMessage.id > state.extracted_until_message_id,
                ChatMessage.id <= job.target_message_id,
                ChatMessage.role == "USER",
            )
            .order_by(ChatMessage.id.asc())
            .all()
        )
        candidates = ProfileExtractionService().extract(messages)
        changed = False
        for candidate in candidates:
            if candidate.confidence < float(self.settings.memory_profile_min_confidence):
                continue
            suppressed = db.query(UserMemory).filter(
                UserMemory.user_id == job.user_id,
                UserMemory.memory_key == candidate.memory_key,
                UserMemory.status == "DELETED",
            ).first()
            if suppressed is not None:
                continue
            active = db.query(UserMemory).filter(
                UserMemory.user_id == job.user_id,
                UserMemory.memory_key == candidate.memory_key,
                UserMemory.status == "ACTIVE",
            ).order_by(UserMemory.version.desc(), UserMemory.id.desc()).first()
            if active is not None and active.origin == "EXPLICIT":
                continue
            if active is not None and active.content == candidate.content:
                continue
            next_version = int(active.version + 1) if active is not None else 1
            if active is not None:
                active.status = "SUPERSEDED"
                active.index_status = "PENDING_DELETE"
                active.updated_at = utcnow()
                db.add(active)
                jobs.enqueue_memory_delete(
                    user_id=job.user_id,
                    public_id=active.public_id,
                    memory_epoch=state.memory_epoch,
                )
            memory = UserMemory(
                public_id=uuid.uuid4().hex,
                user_id=job.user_id,
                category=candidate.category,
                memory_key=candidate.memory_key,
                content=candidate.content,
                source_message_id=candidate.source_message_id,
                confidence=candidate.confidence,
                origin="EXTRACTED",
                version=next_version,
                updated_from_turn_id=job.turn_id,
                evidence_message_ids_json=json.dumps([candidate.source_message_id]),
                sensitivity=candidate.sensitivity,
                visibility_scope=candidate.visibility_scope,
                index_status="PENDING",
                memory_epoch=state.memory_epoch,
                status="ACTIVE",
            )
            db.add(memory)
            db.flush()
            jobs.enqueue_profile_index(memory, state=state)
            changed = True
        state.extracted_until_message_id = max(state.extracted_until_message_id, int(job.target_message_id))
        if changed:
            state.profile_version += 1
        state.updated_at = utcnow()
        db.add(state)

    def _index_episode(self, db: Session, job: MemoryJob) -> None:
        payload = _payload(job)
        episode = db.query(ConversationEpisode).filter(
            ConversationEpisode.public_id == payload.get("episode_public_id"),
            ConversationEpisode.user_id == job.user_id,
        ).first()
        if episode is None or episode.status != "ACTIVE" or episode.version != job.expected_version:
            return
        self._store().upsert_episode(episode)
        episode.index_status = "INDEXED"
        episode.embedding_version = self.settings.memory_embedding_version
        episode.updated_at = utcnow()
        db.add(episode)

    def _index_profile(self, db: Session, job: MemoryJob) -> None:
        state = db.query(UserProfileState).filter(UserProfileState.user_id == job.user_id).first()
        if state is None or state.memory_epoch != job.memory_epoch:
            return
        payload = _payload(job)
        memory = db.query(UserMemory).filter(
            UserMemory.user_id == job.user_id,
            UserMemory.public_id == payload.get("memory_public_id"),
        ).first()
        if (
            memory is None
            or memory.status != "ACTIVE"
            or memory.version != job.expected_version
            or memory.memory_epoch != state.memory_epoch
        ):
            return
        self._store().upsert_profile(memory)
        memory.index_status = "INDEXED"
        memory.updated_at = utcnow()
        db.add(memory)

    def _delete_vector(self, db: Session, job: MemoryJob) -> None:
        payload = _payload(job)
        public_id = str(payload.get("public_id") or "")
        if not public_id:
            return
        store = self._store()
        if payload.get("document_kind") == "episode":
            store.delete_episode(public_id)
        else:
            store.delete_profile(job.user_id, public_id)

    def _store(self):
        if self._vector_store is None:
            self._vector_store = self.vector_store_factory(self.settings)
        return self._vector_store

    def _record_failure(self, db: Session, job_id: int, exc: Exception) -> None:
        job = db.get(MemoryJob, job_id)
        if job is None:
            return
        job.last_error = f"{type(exc).__name__}: {exc}"[:2000]
        job.lease_owner = None
        job.lease_expires_at = None
        job.updated_at = utcnow()
        if job.attempts >= job.max_attempts:
            job.status = FAILED
            job.finished_at = utcnow()
        else:
            job.status = PENDING
            delay = float(self.settings.memory_worker_retry_base_seconds) * (2 ** max(0, job.attempts - 1))
            job.run_after = utcnow() + timedelta(seconds=min(delay, 3600.0))
        db.add(job)
        db.commit()

    @staticmethod
    def _without_suppressed_memories(
        db: Session,
        user_id: int,
        messages: list[ChatMessage],
    ) -> list[ChatMessage]:
        """Exclude deleted facts and their accepted replies from rebuilds.

        Deleted UserMemory rows remain as suppression markers. Matching the
        structured key also prevents a historical backfill from reviving an
        older wording of the same fact after its vector has been deleted.
        """
        tombstones = db.query(UserMemory).filter(
            UserMemory.user_id == user_id,
            UserMemory.status == "DELETED",
        ).all()
        if not tombstones:
            return messages
        suppressed_ids = {
            int(row.source_message_id)
            for row in tombstones
            if row.source_message_id is not None
        }
        keys_by_category: dict[str, set[str]] = {}
        for row in tombstones:
            if row.memory_key:
                keys_by_category.setdefault(row.category, set()).add(row.memory_key)
        for message in messages:
            if message.role.upper() != "USER":
                continue
            if any(
                derive_memory_key(category, message.content) in keys
                for category, keys in keys_by_category.items()
            ):
                suppressed_ids.add(int(message.id))
        if suppressed_ids:
            assistant_ids = db.query(ChatTurn.assistant_message_id).filter(
                ChatTurn.user_id == user_id,
                ChatTurn.user_message_id.in_(suppressed_ids),
                ChatTurn.assistant_message_id.is_not(None),
            ).all()
            suppressed_ids.update(int(value) for (value,) in assistant_ids if value is not None)
        return [message for message in messages if int(message.id) not in suppressed_ids]

    def _enqueue_idle_finalizers(self) -> None:
        db = self.session_factory()
        try:
            cutoff = utcnow() - timedelta(seconds=max(60, int(self.settings.memory_idle_finalize_seconds)))
            sessions = db.query(ChatSession).filter(
                ChatSession.updated_at <= cutoff,
                ChatSession.archived_at.is_(None),
            ).order_by(ChatSession.updated_at.asc()).limit(max(1, int(self.settings.memory_worker_batch_size))).all()
            service = MemoryJobService(db, self.settings)
            for session in sessions:
                target = db.query(ChatMessage.id).filter(ChatMessage.session_id == session.id).order_by(ChatMessage.id.desc()).limit(1).scalar()
                if target:
                    service.enqueue_finalize_session(
                        user_id=session.user_id,
                        session_id=session.id,
                        target_message_id=int(target),
                    )
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("闲置会话收尾扫描失败")
        finally:
            db.close()


def _payload(job: MemoryJob) -> dict:
    try:
        value = json.loads(job.payload_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


_worker: MemoryWorker | None = None


def get_memory_worker(settings: Settings) -> MemoryWorker:
    global _worker
    if _worker is None:
        _worker = MemoryWorker(settings)
    return _worker
