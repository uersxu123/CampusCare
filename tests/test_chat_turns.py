import asyncio
import json
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import Base
from app.models.entities import ChatMessage, ChatSession, ChatTurn, UserAccount
from app.schemas.dtos import ChatRequest
from app.services.chat import ChatService, ChatTaskRegistry
from app.services.chat_turns import ChatTurnNotFoundError, ChatTurnService
from app.services.model_completion import (
    ModelCompletionMetadata,
    ModelFinishReason,
    ModelStreamEvent,
)
from app.services.stream_snapshots import ChatTurnSnapshotStore


class FakeRedis:
    def __init__(self):
        self.values = {}

    def setex(self, key, ttl, value):
        self.values[key] = value

    def get(self, key):
        return self.values.get(key)


class ChatTurnTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        self.db = self.SessionLocal()
        self.user = UserAccount(username="u", display_name="U", password_hash="x")
        self.other = UserAccount(username="v", display_name="V", password_hash="x")
        self.db.add_all([self.user, self.other])
        self.db.commit()
        self.settings = Settings(
            _env_file=None,
            ai_provider="mock",
            knowledge_vector_enabled=False,
            tool_queue_enabled=False,
            chat_turn_poll_interval_ms=20,
            chat_turn_snapshot_interval_ms=10,
            chat_turn_snapshot_min_chars=1,
            redis_socket_timeout_seconds=0.01,
        )

    async def asyncTearDown(self):
        for task in list(ChatTaskRegistry._tasks.values()):
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        ChatTaskRegistry._tasks.clear()
        self.db.close()
        self.engine.dispose()

    async def test_duplicate_request_and_disconnected_reader_use_one_generation(self):
        calls = 0

        async def slow_stream(_client, _messages, **_kwargs):
            nonlocal calls
            calls += 1
            for chunk in ("完整", "回答", "内容"):
                await asyncio.sleep(0.03)
                yield ModelStreamEvent(kind="delta", text=chunk)
            yield ModelStreamEvent(
                kind="terminal",
                metadata=ModelCompletionMetadata(
                    provider="mock",
                    model="mock",
                    finish_reason=ModelFinishReason.STOP,
                    semantic_finish_seen=True,
                    transport_terminal_seen=True,
                    terminal_signal="mock_terminal",
                    provider_finish_reason="stop",
                    configured_output_limit=1536,
                ),
            )

        request_id = str(uuid.uuid4())
        request = ChatRequest(requestId=request_id, message="请解释这个问题")
        with (
            patch("app.services.memory.RedisShortTermMemoryStore._connect", return_value=None),
            patch("app.services.stream_snapshots.ChatTurnSnapshotStore._connect", return_value=None),
            patch("app.services.ai.AiClient._mock_stream_events", slow_stream),
        ):
            service = ChatService(self.db, self.settings)
            first_reader = service.start_chat(self.user, request)
            first_event = await anext(first_reader)
            self.assertIn("event: meta", first_event)
            await first_reader.aclose()

            second_reader = service.start_chat(self.user, request)
            events = []
            async for event in second_reader:
                events.append(event)

        db = self.SessionLocal()
        try:
            turn = ChatTurnService(db, self.settings).get_owned(self.user.id, request_id)
            self.assertEqual(turn.status, "COMPLETED")
            self.assertTrue(turn.completion_verified)
            self.assertEqual(turn.finish_reason, "DIRECT_RESPONSE")
            self.assertTrue(turn.partial_content)
            self.assertEqual(calls, 0)
            metrics = json.loads(turn.generation_metadata_json)["turnMetrics"]
            self.assertEqual(
                sum(call["purpose"] == "response.generate" for call in metrics["calls"]),
                0,
            )
            self.assertEqual(metrics["tokenUsage"]["providerCallCount"], len(metrics["calls"]))
            self.assertEqual(
                db.query(ChatMessage).filter(ChatMessage.session_id == turn.session_id, ChatMessage.role == "USER").count(),
                1,
            )
            self.assertEqual(
                db.query(ChatMessage).filter(ChatMessage.session_id == turn.session_id, ChatMessage.role == "ASSISTANT").count(),
                1,
            )
            self.assertTrue(any("event: snapshot" in event and turn.partial_content in event for event in events))
        finally:
            db.close()

    async def test_completed_turn_falls_back_to_mysql_and_foreign_user_is_hidden(self):
        session = ChatSession(public_id="s", title="s", user_id=self.user.id)
        self.db.add(session)
        self.db.flush()
        assistant = ChatMessage(user_id=self.user.id, session_id=session.id, role="ASSISTANT", content="最终内容")
        self.db.add(assistant)
        self.db.flush()
        request_id = str(uuid.uuid4())
        turn = ChatTurn(
            public_id=uuid.uuid4().hex,
            request_id=request_id,
            user_id=self.user.id,
            session_id=session.id,
            assistant_message_id=assistant.id,
            status="COMPLETED",
            partial_content="旧部分",
        )
        self.db.add(turn)
        self.db.commit()

        with patch("app.services.stream_snapshots.ChatTurnSnapshotStore._connect", return_value=None):
            service = ChatService(self.db, self.settings)
            events = [event async for event in service.stream_turn(self.user.id, request_id)]
            self.assertTrue(any("最终内容" in event for event in events))
            with self.assertRaises(ChatTurnNotFoundError):
                service._turn_state(self.other.id, request_id)

    async def test_runtime_failure_keeps_one_pre_persisted_user_message(self):
        class FailingRuntime:
            def run(self, *_args, **_kwargs):
                raise RuntimeError("route failed")

        request_id = str(uuid.uuid4())
        request = ChatRequest(requestId=request_id, message="路由前也必须保存")
        with (
            patch("app.services.memory.RedisShortTermMemoryStore._connect", return_value=None),
            patch("app.services.stream_snapshots.ChatTurnSnapshotStore._connect", return_value=None),
            patch("app.agents.harness.create_agent_runtime", return_value=FailingRuntime()),
        ):
            service = ChatService(self.db, self.settings)
            events = [event async for event in service.start_chat(self.user, request)]
            replay = [event async for event in service.start_chat(self.user, request)]

        db = self.SessionLocal()
        try:
            turn = ChatTurnService(db, self.settings).get_owned(self.user.id, request_id)
            self.assertEqual(turn.status, "FAILED")
            self.assertIsNotNone(turn.user_message_id)
            self.assertEqual(
                db.query(ChatMessage)
                .filter(
                    ChatMessage.session_id == turn.session_id,
                    ChatMessage.role == "USER",
                )
                .count(),
                1,
            )
            self.assertTrue(any("event: error" in event for event in events))
            self.assertTrue(any("event: done" in event for event in replay))
        finally:
            db.close()

    async def test_message_length_boundary_rejects_before_any_database_write(self):
        valid = ChatRequest(requestId=str(uuid.uuid4()), message="中" * 1000)
        self.assertEqual(len(valid.message), 1000)
        before = self.db.query(ChatMessage).count()
        with self.assertRaises(ValidationError):
            ChatRequest(requestId=str(uuid.uuid4()), message="中" * 1001)
        self.assertEqual(self.db.query(ChatMessage).count(), before)

    def test_create_or_get_atomically_links_user_message(self):
        request_id = str(uuid.uuid4())
        service = ChatTurnService(self.db, self.settings)

        turn, created = service.create_or_get(self.user, request_id, None, "原子消息")
        replay, replay_created = service.create_or_get(self.user, request_id, None, "原子消息")

        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(replay.id, turn.id)
        message = service.get_user_message(turn)
        self.assertEqual(message.content, "原子消息")
        self.assertEqual(
            self.db.query(ChatMessage)
            .filter(ChatMessage.user_id == self.user.id, ChatMessage.role == "USER")
            .count(),
            1,
        )

    def test_stale_active_turns_become_interrupted(self):
        session = ChatSession(public_id="stale-session", title="s", user_id=self.user.id)
        self.db.add(session)
        self.db.flush()
        turn = ChatTurn(
            public_id=uuid.uuid4().hex,
            request_id=str(uuid.uuid4()),
            user_id=self.user.id,
            session_id=session.id,
            status="GENERATING",
            partial_content="已保存部分",
            updated_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1),
        )
        self.db.add(turn)
        self.db.commit()

        count = ChatTurnService(self.db, self.settings).mark_stale_interrupted()
        self.db.refresh(turn)
        self.assertEqual(count, 1)
        self.assertEqual(turn.status, "INTERRUPTED")
        self.assertEqual(turn.partial_content, "已保存部分")
        metrics = json.loads(turn.generation_metadata_json)["turnMetrics"]
        self.assertEqual(metrics["status"], "INCOMPLETE")
        self.assertEqual(metrics["incompleteReason"], "PROCESS_RESTART")
        self.assertNotIn("serverE2eTtftMs", metrics)

    def test_snapshot_store_checks_user_ownership(self):
        fake = FakeRedis()
        with patch("app.services.stream_snapshots.ChatTurnSnapshotStore._connect", return_value=fake):
            store = ChatTurnSnapshotStore(self.settings)
        request_id = str(uuid.uuid4())
        payload = store.payload(self.user.id, "s", "t", "GENERATING", "当前完整快照")
        store.write(request_id, payload)

        self.assertEqual(store.read(request_id, self.user.id)["content"], "当前完整快照")
        self.assertIsNone(store.read(request_id, self.other.id))


if __name__ == "__main__":
    unittest.main()
