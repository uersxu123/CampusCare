import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings, get_settings
from app.core.database import Base, get_db
from app.core.security import hash_password
from app.main import create_app
from app.models.entities import ChatMessage, ChatSession, ConversationSummary, UserAccount
from app.services.conversations import ConversationNotFoundError, StudentConversationService


class FakeMemory:
    def __init__(self, explode: bool = False):
        self.deleted = []
        self.explode = explode

    def delete(self, session_public_id: str) -> None:
        self.deleted.append(session_public_id)
        if self.explode:
            raise RuntimeError("redis unavailable")


class ConversationServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.settings = Settings(_env_file=None, knowledge_vector_enabled=False, ai_provider="mock")
        self.student = UserAccount(
            username="student-a", display_name="学生 A", password_hash="x", roles_csv="ROLE_USER"
        )
        self.other = UserAccount(
            username="student-b", display_name="学生 B", password_hash="x", roles_csv="ROLE_USER"
        )
        self.db.add_all([self.student, self.other])
        self.db.flush()
        now = datetime.now(UTC).replace(tzinfo=None)
        self.first = ChatSession(
            public_id="first", title="较早会话", user_id=self.student.id, created_at=now, updated_at=now
        )
        self.latest = ChatSession(
            public_id="latest",
            title="最近会话",
            user_id=self.student.id,
            created_at=now,
            updated_at=now + timedelta(minutes=1),
        )
        self.foreign = ChatSession(
            public_id="foreign", title="其他学生会话", user_id=self.other.id, created_at=now, updated_at=now
        )
        self.db.add_all([self.first, self.latest, self.foreign])
        self.db.flush()
        self.db.add_all(
            [
                ChatMessage(user_id=self.student.id, session_id=self.first.id, role="USER", content="第一条消息"),
                ChatMessage(
                    user_id=self.student.id,
                    session_id=self.latest.id,
                    role="USER",
                    content="这是一条很长的最近消息 " + "内容" * 50,
                ),
                ChatMessage(user_id=self.other.id, session_id=self.foreign.id, role="USER", content="不可见"),
            ]
        )
        self.db.add(
            ConversationSummary(
                session_id=self.latest.id,
                version=1,
                covered_until_message_id=0,
                summary_json="{}",
            )
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_list_active_is_owned_sorted_bounded_and_counted(self):
        response = StudentConversationService(self.db, self.settings, FakeMemory()).list_active(self.student.id)

        self.assertEqual([item.sessionId for item in response.items], ["latest", "first"])
        self.assertEqual(response.items[0].messageCount, 1)
        self.assertLessEqual(len(response.items[0].preview), 80)
        self.assertNotIn("foreign", [item.sessionId for item in response.items])

    def test_get_active_orders_messages_and_enforces_ownership(self):
        self.db.add(
            ChatMessage(user_id=self.student.id, session_id=self.first.id, role="ASSISTANT", content="第二条消息")
        )
        self.db.commit()
        service = StudentConversationService(self.db, self.settings, FakeMemory())

        response = service.get_active(self.student.id, self.first.public_id)

        self.assertEqual([message.content for message in response.messages], ["第一条消息", "第二条消息"])
        with self.assertRaises(ConversationNotFoundError):
            service.get_active(self.student.id, self.foreign.public_id)

    def test_archive_is_idempotent_and_preserves_messages_and_summary(self):
        memory = FakeMemory()
        service = StudentConversationService(self.db, self.settings, memory)
        message_count = self.db.query(ChatMessage).filter(ChatMessage.session_id == self.latest.id).count()
        summary_count = self.db.query(ConversationSummary).filter(ConversationSummary.session_id == self.latest.id).count()

        first = service.archive(self.student.id, self.latest.public_id)
        second = service.archive(self.student.id, self.latest.public_id)

        self.assertTrue(first.archived and second.archived)
        self.assertEqual(first.archivedAt, second.archivedAt)
        self.assertEqual(memory.deleted, ["latest", "latest"])
        self.assertEqual(self.db.query(ChatMessage).filter(ChatMessage.session_id == self.latest.id).count(), message_count)
        self.assertEqual(
            self.db.query(ConversationSummary).filter(ConversationSummary.session_id == self.latest.id).count(),
            summary_count,
        )
        self.assertNotIn("latest", [item.sessionId for item in service.list_active(self.student.id).items])
        with self.assertRaises(ConversationNotFoundError):
            service.get_active(self.student.id, self.latest.public_id)

    def test_cannot_archive_another_students_session(self):
        service = StudentConversationService(self.db, self.settings, FakeMemory())
        with self.assertRaises(ConversationNotFoundError):
            service.archive(self.student.id, self.foreign.public_id)
        self.assertIsNone(self.db.get(ChatSession, self.foreign.id).archived_at)

    def test_memory_cleanup_failure_does_not_rollback_archive(self):
        service = StudentConversationService(self.db, self.settings, FakeMemory(explode=True))

        response = service.archive(self.student.id, self.first.public_id)

        self.assertTrue(response.archived)
        self.assertIsNotNone(self.db.get(ChatSession, self.first.id).archived_at)


class ConversationApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        db = self.SessionLocal()
        self.student = UserAccount(
            username="student-api",
            display_name="API 学生",
            password_hash=hash_password("student123"),
            roles_csv="ROLE_USER",
        )
        self.other = UserAccount(
            username="other-api",
            display_name="其他学生",
            password_hash=hash_password("other123"),
            roles_csv="ROLE_USER",
        )
        self.admin = UserAccount(
            username="admin-api",
            display_name="管理员",
            password_hash=hash_password("admin123"),
            roles_csv="ROLE_ADMIN,ROLE_USER",
        )
        db.add_all([self.student, self.other, self.admin])
        db.flush()
        self.own = ChatSession(public_id="own-session", title="自己的会话", user_id=self.student.id)
        self.foreign = ChatSession(public_id="foreign-session", title="别人的会话", user_id=self.other.id)
        db.add_all([self.own, self.foreign])
        db.flush()
        db.add_all(
            [
                ChatMessage(user_id=self.student.id, session_id=self.own.id, role="USER", content="自己的消息"),
                ChatMessage(user_id=self.other.id, session_id=self.foreign.id, role="USER", content="别人的消息"),
            ]
        )
        db.commit()
        db.close()

        def override_db():
            session = self.SessionLocal()
            try:
                yield session
            finally:
                session.close()

        get_settings.cache_clear()
        self.app = create_app()
        self.app.dependency_overrides[get_db] = override_db
        self.client = TestClient(self.app)
        self.student_auth = ("student-api", "student123")
        self.admin_auth = ("admin-api", "admin123")

    def tearDown(self):
        self.client.close()
        self.app.dependency_overrides.clear()
        self.engine.dispose()

    def test_student_conversation_api_archive_and_admin_read(self):
        listed = self.client.get("/api/conversations", auth=self.student_auth)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([item["sessionId"] for item in listed.json()["items"]], ["own-session"])

        own = self.client.get("/api/conversations/own-session", auth=self.student_auth)
        foreign = self.client.get("/api/conversations/foreign-session", auth=self.student_auth)
        self.assertEqual(own.status_code, 200)
        self.assertEqual(foreign.status_code, 404)

        with patch("app.services.memory.RedisShortTermMemoryStore._connect", return_value=None):
            archived = self.client.post("/api/conversations/own-session/archive", auth=self.student_auth)
        self.assertEqual(archived.status_code, 200)
        self.assertTrue(archived.json()["archived"])
        self.assertEqual(self.client.get("/api/conversations", auth=self.student_auth).json()["items"], [])
        self.assertEqual(self.client.get("/api/conversations/own-session", auth=self.student_auth).status_code, 404)

        continued = self.client.post(
            "/api/chat/stream",
            auth=self.student_auth,
            json={
                "requestId": "00000000-0000-4000-8000-000000000001",
                "sessionId": "own-session",
                "message": "继续",
            },
        )
        self.assertEqual(continued.status_code, 409)

        invalid = self.client.post(
            "/api/chat/stream", auth=self.student_auth, json={"message": "你好"}
        )
        self.assertEqual(invalid.status_code, 422)

        admin = self.client.get("/api/admin/conversations/own-session", auth=self.admin_auth)
        self.assertEqual(admin.status_code, 200)
        self.assertTrue(admin.json()["archived"])
        self.assertEqual(len(admin.json()["messages"]), 1)

    def test_archive_foreign_session_and_forbidden_routes(self):
        self.assertEqual(
            self.client.post("/api/conversations/foreign-session/archive", auth=self.student_auth).status_code,
            404,
        )
        paths = {(route.path, method) for route in self.app.routes for method in getattr(route, "methods", set())}
        self.assertNotIn(("/api/conversations/{session_id}", "DELETE"), paths)
        self.assertFalse(any("restore" in path for path, _ in paths))


if __name__ == "__main__":
    unittest.main()
