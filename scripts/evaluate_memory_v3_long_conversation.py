from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import Base
from app.models.entities import ChatMessage, ChatSession, ConversationSummary, UserAccount
from app.services.context_builder import ContextBuilder
from app.services.memory_jobs import COMPACT_SESSION, MemoryJobService, MemoryWorker


class OfflineCache:
    client = None

    def load_recent_records_with_status(self, _session_id, **_kwargs):
        return [], "unavailable"

    def merge_records(self, *_args, **_kwargs):
        return False


class OfflineVectorStore:
    def upsert_episode(self, _episode):
        return None

    def upsert_profile(self, _memory):
        return None

    def delete_episode(self, _public_id):
        return None

    def delete_profile(self, _user_id, _public_id):
        return None


def run_evaluation() -> dict:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db: Session = factory()
    config = Settings(
        _env_file=None,
        ai_provider="mock",
        knowledge_vector_enabled=False,
        memory_embedding_provider="disabled",
        memory_chroma_mode="disabled",
        memory_worker_enabled=False,
        context_recent_message_limit=8,
        memory_compaction_min_messages=4,
        memory_compaction_max_delta_tokens=1500,
    )
    worker = MemoryWorker(
        config,
        session_factory=factory,
        vector_store_factory=lambda _settings: OfflineVectorStore(),
        worker_id="evaluation-worker",
    )
    user = UserAccount(username="memory-eval", display_name="评估用户", password_hash="x")
    db.add(user)
    db.flush()
    sessions = [
        ChatSession(public_id="memory-eval-a", title="前半段", user_id=user.id),
        ChatSession(public_id="memory-eval-b", title="后半段", user_id=user.id),
    ]
    db.add_all(sessions)
    db.commit()

    max_specialist_tokens = 0
    max_response_tokens = 0
    duplicate_current_inputs = 0
    duplicate_source_ids = 0
    overflow_count = 0
    snapshot_mismatches = 0
    cross_session_recall_turns = 0

    try:
        for turn_index in range(100):
            session = sessions[0 if turn_index < 50 else 1]
            if turn_index == 0:
                text = "我的目标是通过高等数学考试，我偏好清单式计划，每天可用两小时。"
            elif turn_index == 35:
                text = "更正：不是每天两小时，而是每天三小时，请继续用清单。"
            elif turn_index == 70:
                text = "请结合我之前的清单偏好和可用时间继续安排复习。"
            else:
                text = f"第{turn_index + 1}轮：继续高等数学复习清单，保留既有约束。"
            current = ChatMessage(
                user_id=user.id,
                session_id=session.id,
                role="USER",
                content=text,
            )
            db.add(current)
            db.commit()
            builder = ContextBuilder(db, config, OfflineCache())
            packet = builder.build_base_context(
                user=user,
                session=session,
                current_message=current,
                model_input=text,
            )
            specialist = builder.build_specialist_prompt(
                packet=packet,
                audience="ACADEMIC",
                system="只处理学习计划并遵守输入预算。",
                work_item={
                    "workItemId": f"work-{turn_index}",
                    "intent": "ACADEMIC",
                    "objective": "继续学习计划",
                    "sourceText": text,
                },
                dependency_results=[],
                tool_schemas=[{"name": "rag_search", "schema": {"query": "string"}}],
            )
            response = builder.build_synthesis_prompt(
                packet=packet,
                system="合成最终回答。",
                route_plan={"workItems": [{"sourceText": text}]},
                specialist_results=[{"workItemId": f"work-{turn_index}", "answerBrief": "按清单推进"}],
            )
            max_specialist_tokens = max(max_specialist_tokens, specialist.manifest.estimated_total_tokens)
            max_response_tokens = max(max_response_tokens, response.manifest.estimated_total_tokens)
            overflow_count += int(specialist.manifest.protected_overflow or response.manifest.protected_overflow)
            duplicate_current_inputs += int(
                sum(message.content.count(text) for message in specialist.messages) != 1
                or sum(message.content.count(text) for message in response.messages) != 1
            )
            specialist_payload = json.loads(specialist.messages[-1].content)
            response_payload = json.loads(response.messages[-1].content)
            if specialist_payload["snapshotId"] != response_payload["snapshotId"]:
                snapshot_mismatches += 1
            working_ids = [item["id"] for item in specialist_payload["baseMemory"]["workingMemory"]]
            episode_ids = [item["episodeId"] for item in specialist_payload["baseMemory"]["relevantHistory"]]
            profile_ids = [item["memoryId"] for item in specialist_payload["baseMemory"]["userProfile"]]
            all_ids = [f"message:{value}" for value in working_ids] + [
                f"episode:{value}" for value in episode_ids
            ] + [f"profile:{value}" for value in profile_ids]
            duplicate_source_ids += len(all_ids) - len(set(all_ids))
            if turn_index >= 50 and episode_ids:
                cross_session_recall_turns += 1

            assistant = ChatMessage(
                user_id=user.id,
                session_id=session.id,
                role="ASSISTANT",
                content=f"第{turn_index + 1}轮安排已更新。",
            )
            db.add(assistant)
            db.commit()
            MemoryJobService(db, config).enqueue(
                kind=COMPACT_SESSION,
                dedupe_key=f"eval-compact:{session.id}:{turn_index}",
                user_id=user.id,
                session_id=session.id,
                target_message_id=assistant.id,
            )
            if turn_index == 49:
                MemoryJobService(db, config).enqueue_finalize_session(
                    user_id=user.id,
                    session_id=session.id,
                    target_message_id=assistant.id,
                )
            db.commit()
            while worker.run_once():
                pass

        summaries = db.query(ConversationSummary).order_by(ConversationSummary.session_id).all()
        monotonic_summary_versions = all(row.version > 0 and row.covered_until_message_id > 0 for row in summaries)
        result = {
            "turns": 100,
            "maxSpecialistInputTokens": max_specialist_tokens,
            "maxResponseInputTokens": max_response_tokens,
            "applicationInputBudget": config.context_input_max_tokens,
            "baseMemoryBudget": config.memory_base_max_tokens,
            "protectedOverflowCount": overflow_count,
            "currentInputDuplicatePromptCount": duplicate_current_inputs,
            "duplicateSourceIdCount": duplicate_source_ids,
            "snapshotMismatchCount": snapshot_mismatches,
            "crossSessionRecallTurns": cross_session_recall_turns,
            "summaryVersions": [row.version for row in summaries],
            "summaryCursors": [row.covered_until_message_id for row in summaries],
            "summaryProgressValid": monotonic_summary_versions,
            "mode": "deterministic SQLite + SQL fallback; no external model or embedding service",
        }
        return result
    finally:
        db.close()
        engine.dispose()


def main() -> None:
    report = run_evaluation()
    destination = Path(__file__).resolve().parents[1] / "benchmarks" / "memory_v3_100_turn_report.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    if any((
        report["protectedOverflowCount"],
        report["currentInputDuplicatePromptCount"],
        report["duplicateSourceIdCount"],
        report["snapshotMismatchCount"],
    )):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
