import unittest
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.entities import UserAccount, UserMemory
from app.services.user_memory import UserMemoryService, derive_memory_key, lexical_tokens


class UserMemoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.user = UserAccount(username="u", display_name="U", password_hash="x")
        self.other = UserAccount(username="v", display_name="V", password_hash="x")
        self.db.add_all([self.user, self.other])
        self.db.commit()
        self.service = UserMemoryService(self.db)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_only_explicit_non_sensitive_memory_is_saved_and_isolated(self):
        self.assertIsNone(self.service.remember_explicit(self.user.id, "我今天很难过"))
        self.assertIsNone(self.service.remember_explicit(self.user.id, "请记住我的手机号是13800138000"))
        saved = self.service.remember_explicit(self.user.id, "请记住我喜欢用清单学习")

        self.assertIsNotNone(saved)
        self.assertEqual(
            [item.content for item in self.service.active_for_prompt(self.user.id, "学习计划")],
            ["我喜欢用清单学习"],
        )
        self.assertEqual(self.service.active_for_prompt(self.other.id, "学习计划"), [])

    def test_correction_supersedes_old_memory_and_inactive_rows_are_excluded(self):
        old = self.service.remember_explicit(self.user.id, "请记住我喜欢晚上学习")
        new = self.service.remember_explicit(self.user.id, "请记住更正：我喜欢早上学习")
        self.db.refresh(old)

        self.assertEqual(old.status, "SUPERSEDED")
        self.assertEqual([item.id for item in self.service.active_for_prompt(self.user.id, "学习")], [new.id])

        new.status = "DELETED"
        expired = UserMemory(
            public_id="expired",
            user_id=self.user.id,
            category="EXPLICIT",
            content="过期信息",
            status="ACTIVE",
            confidence=1.0,
            expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1),
        )
        self.db.add(expired)
        self.db.commit()
        self.assertEqual(self.service.active_for_prompt(self.user.id, "学习"), [])

    def test_memory_key_correction_is_precise_within_category(self):
        time_memory = self.service.remember_explicit(self.user.id, "请记住我喜欢晚上学习")
        format_memory = self.service.remember_explicit(self.user.id, "请记住我喜欢用清单学习")
        corrected = self.service.remember_explicit(self.user.id, "请记住更正：我喜欢早上学习")
        self.db.refresh(time_memory)
        self.db.refresh(format_memory)

        self.assertEqual(time_memory.memory_key, "LEARNING_PREFERENCE:study_time")
        self.assertEqual(format_memory.memory_key, "LEARNING_PREFERENCE:learning_format")
        self.assertEqual(corrected.memory_key, "LEARNING_PREFERENCE:study_time")
        self.assertEqual(time_memory.status, "SUPERSEDED")
        self.assertEqual(format_memory.status, "ACTIVE")

    def test_chinese_relevance_threshold_and_same_turn_exclusion(self):
        relevant = self.service.remember_explicit(
            self.user.id,
            "请记住我喜欢用清单学习",
            source_message_id=10,
        )
        self.service.remember_explicit(self.user.id, "请记住我在南望山校区")

        selected = self.service.active_for_prompt(
            self.user.id,
            "帮我用清单安排复习",
            exclude_source_message_id=99,
        )
        unrelated = self.service.active_for_prompt(self.user.id, "今天天气怎么样")
        same_turn = self.service.active_for_prompt(
            self.user.id,
            "帮我用清单安排复习",
            exclude_source_message_id=10,
        )

        self.assertEqual(selected[0].id, relevant.id)
        self.assertEqual(unrelated, [])
        self.assertNotIn(relevant.id, [item.id for item in same_turn])
        self.assertIn("学习", lexical_tokens("请记住我喜欢清单学习"))

    def test_list_delete_and_mark_used_are_governed(self):
        memory = self.service.remember_explicit(self.user.id, "请记住我喜欢用图表学习")
        self.assertEqual([item.id for item in self.service.list_active(self.user.id)], [memory.id])
        self.assertEqual(self.service.list_active(self.other.id), [])

        self.service.mark_used([memory.id])
        self.db.refresh(memory)
        first_used = memory.last_used_at
        self.assertIsNotNone(first_used)
        self.service.mark_used([memory.id])
        self.db.refresh(memory)
        self.assertEqual(memory.last_used_at, first_used)

        self.assertFalse(self.service.delete(self.other.id, memory.public_id))
        self.assertTrue(self.service.delete(self.user.id, memory.public_id))
        self.assertFalse(self.service.delete(self.user.id, memory.public_id))
        self.assertEqual(self.service.list_active(self.user.id), [])

    def test_memory_key_fallback_is_stable_and_bounded(self):
        first = derive_memory_key("EXPLICIT", "长期希望回答更简洁")
        self.assertEqual(first, derive_memory_key("EXPLICIT", "长期希望回答更简洁"))
        self.assertLessEqual(len(first), 128)


if __name__ == "__main__":
    unittest.main()
