from __future__ import annotations

import argparse
import hashlib
import json

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.entities import ChatMessage, ChatSession, ConversationEpisode, ConversationSummary, UserMemory
from app.services.memory_jobs import INDEX_EPISODE, MemoryJobService


def backfill(*, apply: bool, user_id: int | None, enqueue_history: bool, limit: int) -> dict:
    settings = get_settings()
    db = SessionLocal()
    counts = {"profiles": 0, "legacy_episodes": 0, "history_jobs": 0}
    try:
        jobs = MemoryJobService(db, settings)
        profile_query = db.query(UserMemory).filter(UserMemory.status == "ACTIVE")
        if user_id is not None:
            profile_query = profile_query.filter(UserMemory.user_id == user_id)
        for memory in profile_query.order_by(UserMemory.id.asc()).limit(limit).all():
            counts["profiles"] += 1
            if apply:
                state = jobs._profile_state(memory.user_id)
                memory.memory_epoch = state.memory_epoch
                memory.index_status = "PENDING"
                jobs.enqueue_profile_index(memory, state=state)

        summary_query = db.query(ConversationSummary, ChatSession).join(
            ChatSession, ChatSession.id == ConversationSummary.session_id
        ).filter(ConversationSummary.covered_until_message_id > 0)
        if user_id is not None:
            summary_query = summary_query.filter(ChatSession.user_id == user_id)
        for summary, session in summary_query.order_by(ConversationSummary.id.asc()).limit(limit).all():
            first_id = db.query(ChatMessage.id).filter(
                ChatMessage.session_id == session.id,
                ChatMessage.id <= summary.covered_until_message_id,
            ).order_by(ChatMessage.id.asc()).limit(1).scalar()
            if first_id is None:
                continue
            counts["legacy_episodes"] += 1
            if apply:
                public_id = f"legacy-{session.id}-{first_id}-{summary.covered_until_message_id}"
                episode = db.query(ConversationEpisode).filter(ConversationEpisode.public_id == public_id).first()
                if episode is None:
                    text = summary.summary_json
                    episode = ConversationEpisode(
                        public_id=public_id,
                        user_id=session.user_id,
                        session_id=session.id,
                        source_start_message_id=int(first_id),
                        source_end_message_id=int(summary.covered_until_message_id),
                        summary_text=text,
                        facts_json=text,
                        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        version=max(1, int(summary.version)),
                        schema_version=0,
                        status="ACTIVE",
                        index_status="PENDING",
                        embedding_version=settings.memory_embedding_version,
                        is_tail=False,
                    )
                    db.add(episode)
                    db.flush()
                jobs.enqueue(
                    kind=INDEX_EPISODE,
                    dedupe_key=f"{INDEX_EPISODE}:{episode.public_id}:{episode.version}",
                    user_id=session.user_id,
                    session_id=session.id,
                    target_message_id=episode.source_end_message_id,
                    expected_version=episode.version,
                    payload={"episode_public_id": episode.public_id},
                )

        if enqueue_history:
            session_query = db.query(ChatSession)
            if user_id is not None:
                session_query = session_query.filter(ChatSession.user_id == user_id)
            for session in session_query.order_by(ChatSession.id.asc()).limit(limit).all():
                target = db.query(ChatMessage.id).filter(
                    ChatMessage.session_id == session.id,
                    ChatMessage.user_id == session.user_id,
                ).order_by(ChatMessage.id.desc()).limit(1).scalar()
                if target is None:
                    continue
                counts["history_jobs"] += 1
                if apply:
                    jobs.enqueue_finalize_session(
                        user_id=session.user_id,
                        session_id=session.id,
                        target_message_id=int(target),
                    )
        if apply:
            db.commit()
        else:
            db.rollback()
        return counts
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="回填 CampusCare 三层记忆派生数据；默认仅预览。")
    parser.add_argument("--apply", action="store_true", help="实际写入任务和 legacy episode")
    parser.add_argument("--user-id", type=int, default=None, help="仅处理指定用户")
    parser.add_argument("--enqueue-history", action="store_true", help="为原始会话尾部入队收尾任务")
    parser.add_argument("--limit", type=int, default=1000, help="每类记录的单次上限")
    args = parser.parse_args()
    result = backfill(
        apply=args.apply,
        user_id=args.user_id,
        enqueue_history=args.enqueue_history,
        limit=max(1, args.limit),
    )
    print(json.dumps({"applied": args.apply, **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
