import json
import unittest
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import Base
from app.models.entities import (
    ChatMessage,
    ChatSession,
    ConversationSummary,
    PsychologicalReport,
    UserAccount,
    UserMemory,
)
from app.services.context_builder import ContextBuilder
from app.services.memory import MemoryMessage


class FakeContextCache:
    def __init__(self, records=None, status="hit"):
        self.records = list(records or [])
        self.status = status

    def load_recent_records_with_status(self, _session_public_id, *, user_id=None):
        del user_id
        return list(self.records), self.status


class ErrorContextCache:
    def load_recent_records_with_status(self, _session_public_id, *, user_id=None):
        del user_id
        raise RuntimeError("redis down")


class ContextBuilderTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.user = UserAccount(username="u", display_name="U", password_hash="x")
        self.other = UserAccount(username="v", display_name="V", password_hash="x")
        self.db.add_all([self.user, self.other])
        self.db.flush()
        self.session = ChatSession(public_id="s1", title="s1", user_id=self.user.id)
        self.other_session = ChatSession(public_id="s2", title="s2", user_id=self.other.id)
        self.db.add_all([self.session, self.other_session])
        self.db.commit()
        self.settings = Settings(
            _env_file=None,
            ai_provider="mock",
            knowledge_vector_enabled=False,
            context_recent_message_limit=2,
            context_safety_max_age_hours=72,
        )

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _message(self, content="当前输入"):
        row = ChatMessage(
            user_id=self.user.id,
            session_id=self.session.id,
            role="USER",
            content=content,
        )
        self.db.add(row)
        self.db.commit()
        return row

    def test_mysql_fallback_is_scoped_and_excludes_current_message(self):
        first = self._message("历史一")
        second = ChatMessage(
            user_id=self.user.id,
            session_id=self.session.id,
            role="ASSISTANT",
            content="历史二",
        )
        foreign = ChatMessage(
            user_id=self.other.id,
            session_id=self.other_session.id,
            role="USER",
            content="其他用户秘密",
        )
        self.db.add_all([second, foreign])
        self.db.commit()
        current = self._message()

        packet = ContextBuilder(
            self.db, self.settings, FakeContextCache(status="miss")
        ).build_base_context(
            user=self.user,
            session=self.session,
            current_message=current,
            model_input=current.content,
        )

        self.assertEqual(
            [item.id for item in packet.recent_messages],
            [first.id, second.id],
        )
        self.assertNotIn(current.id, packet.manifest.recent_message_ids)
        self.assertNotIn("其他用户秘密", [item.content for item in packet.recent_messages])
        self.assertIn("redis_recent_messages", packet.manifest.degraded_sources)

    def test_valid_cache_is_deduplicated_sorted_and_bounded(self):
        self._message("占位一")
        self._message("占位二")
        current = self._message()
        records = [
            MemoryMessage(current.id - 1, "user", "占位二"),
            MemoryMessage(current.id - 2, "user", "占位一"),
            MemoryMessage(current.id - 1, "user", "占位二"),
            MemoryMessage(current.id, "user", current.content),
        ]

        packet = ContextBuilder(
            self.db, self.settings, FakeContextCache(records)
        ).build_base_context(
            user=self.user,
            session=self.session,
            current_message=current,
            model_input=current.content,
        )

        self.assertEqual([item.content for item in packet.recent_messages], ["占位一", "占位二"])
        self.assertEqual(packet.manifest.degraded_sources, ())

    def test_legacy_and_broken_summaries_are_normalized_or_degraded(self):
        current = self._message()
        row = ConversationSummary(
            session_id=self.session.id,
            version=3,
            covered_until_message_id=0,
            summary_json=json.dumps(
                {"current_goal": "准备补考", "user_preferences": ["晚上学习"]},
                ensure_ascii=False,
            ),
        )
        self.db.add(row)
        self.db.commit()
        builder = ContextBuilder(self.db, self.settings, FakeContextCache(status="empty"))

        packet = builder.build_base_context(
            user=self.user,
            session=self.session,
            current_message=current,
            model_input=current.content,
        )
        self.assertEqual(packet.structured_summary["schema_version"], 2)
        self.assertEqual(packet.structured_summary["current_goal"]["text"], "准备补考")
        self.assertEqual(packet.summary_version, 3)

        row.summary_json = "{broken"
        self.db.commit()
        degraded = builder.build_base_context(
            user=self.user,
            session=self.session,
            current_message=current,
            model_input=current.content,
        )
        self.assertIn("conversation_summary", degraded.manifest.degraded_sources)
        self.assertEqual(degraded.structured_summary["schema_version"], 2)

    def test_memories_and_safety_are_selected_once_and_projected(self):
        source = self._message("请记住我喜欢清单")
        current = self._message("帮我做学习清单")
        self.db.add(
            UserMemory(
                public_id="m1",
                user_id=self.user.id,
                category="LEARNING_PREFERENCE",
                memory_key="learning_format",
                content="我喜欢清单学习",
                source_message_id=source.id,
                status="ACTIVE",
            )
        )
        report = PsychologicalReport(
            user_id=self.user.id,
            session_id=self.session.id,
            content="敏感报告正文",
            intent="RISK",
            emotion="HIGH_RISK",
            emotion_score=4.0,
            risk_level="HIGH",
            confidence=0.9,
            summary="内部摘要",
            created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1),
        )
        self.db.add(report)
        self.db.commit()

        packet = ContextBuilder(
            self.db, self.settings, FakeContextCache(status="miss")
        ).build_base_context(
            user=self.user,
            session=self.session,
            current_message=current,
            model_input=current.content,
        )

        self.assertEqual(packet.manifest.user_memory_ids, (1,))
        self.assertEqual(packet.safety_context.report_id, report.id)
        self.assertNotIn("selected_user_memories", packet.for_understanding(current.content))
        self.assertNotIn("safety_context", packet.for_response())
        self.assertEqual(packet.for_safety()["safety_context"]["risk_level"], "HIGH")
        self.assertNotIn("敏感报告正文", str(packet.for_safety()))

    def test_cache_error_falls_back_and_marks_degraded(self):
        history = self._message("数据库历史")
        current = self._message()

        packet = ContextBuilder(
            self.db, self.settings, ErrorContextCache()
        ).build_base_context(
            user=self.user,
            session=self.session,
            current_message=current,
            model_input=current.content,
        )

        self.assertEqual(packet.recent_messages[-1].id, history.id)
        self.assertIn("redis_recent_messages", packet.manifest.degraded_sources)

    def test_legacy_cache_without_message_ids_falls_back(self):
        history = self._message("可信数据库历史")
        current = self._message()
        cache = FakeContextCache([MemoryMessage(None, "user", "旧缓存无 ID")])

        packet = ContextBuilder(self.db, self.settings, cache).build_base_context(
            user=self.user,
            session=self.session,
            current_message=current,
            model_input=current.content,
        )

        self.assertEqual([item.id for item in packet.recent_messages], [history.id])
        self.assertIn("redis_recent_messages", packet.manifest.degraded_sources)

    def test_expired_and_foreign_safety_reports_are_not_loaded(self):
        current = self._message()
        expired = PsychologicalReport(
            user_id=self.user.id,
            session_id=self.session.id,
            content="过期正文",
            intent="RISK",
            emotion="HIGH_RISK",
            emotion_score=4.0,
            risk_level="HIGH",
            confidence=0.9,
            summary="过期",
            created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=73),
        )
        foreign = PsychologicalReport(
            user_id=self.other.id,
            session_id=self.other_session.id,
            content="其他用户正文",
            intent="RISK",
            emotion="HIGH_RISK",
            emotion_score=4.0,
            risk_level="HIGH",
            confidence=0.9,
            summary="其他用户",
        )
        self.db.add_all([expired, foreign])
        self.db.commit()

        packet = ContextBuilder(
            self.db, self.settings, FakeContextCache(status="empty")
        ).build_base_context(
            user=self.user,
            session=self.session,
            current_message=current,
            model_input=current.content,
        )

        self.assertIsNone(packet.safety_context)


if __name__ == "__main__":
    unittest.main()
