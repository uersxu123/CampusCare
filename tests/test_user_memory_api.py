import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.core.database import Base, get_db
from app.core.security import hash_password
from app.main import create_app
from app.models.entities import UserAccount, UserMemory


class UserMemoryApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        with self.SessionLocal() as db:
            self.user = UserAccount(
                username="memory-user",
                display_name="记忆用户",
                password_hash=hash_password("user123"),
                roles_csv="ROLE_USER",
            )
            self.other = UserAccount(
                username="memory-other",
                display_name="其他用户",
                password_hash=hash_password("other123"),
                roles_csv="ROLE_USER",
            )
            db.add_all([self.user, self.other])
            db.flush()
            own = UserMemory(
                public_id="own-memory",
                user_id=self.user.id,
                category="LEARNING_PREFERENCE",
                memory_key="LEARNING_PREFERENCE:learning_format",
                content="我喜欢清单学习",
                status="ACTIVE",
            )
            foreign = UserMemory(
                public_id="foreign-memory",
                user_id=self.other.id,
                category="PROFILE",
                memory_key="PROFILE:campus",
                content="其他用户记忆",
                status="ACTIVE",
            )
            deleted = UserMemory(
                public_id="deleted-memory",
                user_id=self.user.id,
                category="EXPLICIT",
                memory_key="EXPLICIT:topic:deleted",
                content="已删除记忆",
                status="DELETED",
            )
            db.add_all([own, foreign, deleted])
            db.commit()

        def override_db():
            with self.SessionLocal() as db:
                yield db

        get_settings.cache_clear()
        self.app = create_app()
        self.app.dependency_overrides[get_db] = override_db
        self.client = TestClient(self.app)
        self.auth = ("memory-user", "user123")

    def tearDown(self):
        self.client.close()
        self.app.dependency_overrides.clear()
        self.engine.dispose()

    def test_list_and_delete_are_user_isolated_soft_delete_operations(self):
        listed = self.client.get("/api/user/memories", auth=self.auth)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(
            [item["memoryId"] for item in listed.json()["items"]],
            ["own-memory"],
        )
        self.assertEqual(
            self.client.delete("/api/user/memories/foreign-memory", auth=self.auth).status_code,
            404,
        )
        deleted = self.client.delete("/api/user/memories/own-memory", auth=self.auth)
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.json()["deleted"])
        self.assertEqual(
            self.client.delete("/api/user/memories/own-memory", auth=self.auth).status_code,
            404,
        )
        with self.SessionLocal() as db:
            row = db.query(UserMemory).filter(UserMemory.public_id == "own-memory").one()
            self.assertEqual(row.status, "DELETED")

    def test_student_renderer_uses_text_content_for_memory_body(self):
        script = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "student.js"
        ).read_text(encoding="utf-8")
        self.assertIn("content.textContent = memory.content", script)
        self.assertNotIn("innerHTML", script)


if __name__ == "__main__":
    unittest.main()
