"""当前 Response 生成入口到 ChatTurn/SSE/落库的隔离集成测试。"""
import asyncio
import json
import unittest
import uuid
from unittest.mock import patch

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import Base
from app.models.entities import AgentRunTrace, ChatMessage, ChatSession, PendingClarification, UserAccount
from app.schemas.dtos import ChatRequest
from app.services.ai import AiClient
from app.services.chat import ChatService, ChatTaskRegistry, _exception_reason, merge_continuation
from app.services.chat_turns import ChatTurnService
from app.services.routing_v5 import PlanningResultV6
from app.services.understanding import UnderstandingInvocationResult
from app.services.model_completion import (
    IncompleteGenerationError, ModelCompletion, ModelCompletionMetadata, ModelFinishReason, ModelUsage,
)


def completion(text, reason=ModelFinishReason.STOP):
    return ModelCompletion(text, ModelCompletionMetadata(
        provider="mock", model="mock", finish_reason=reason, semantic_finish_seen=True,
        transport_terminal_seen=True, terminal_signal="fixture_terminal",
        provider_finish_reason=reason.value, configured_output_limit=1536,
        usage=ModelUsage(output_tokens=len(text)),
    ))


class ChatCompletionStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        self.db = self.SessionLocal()
        self.user = UserAccount(username="completion-user", display_name="学生", password_hash="x")
        self.db.add(self.user)
        self.db.commit()
        self.settings = Settings(_env_file=None, ai_provider="mock",
            agent_model_specialist_provider="mock", chat_tools_enabled=False,
            knowledge_vector_enabled=False, tool_queue_enabled=False,
            memory_worker_enabled=False, route_fast_enabled=False,
            chat_turn_poll_interval_ms=20, chat_turn_snapshot_interval_ms=1,
            chat_turn_snapshot_min_chars=1, redis_socket_timeout_seconds=0.01)
        self.response_calls = []

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

    async def _run(self, outcomes, *, message="你好", session_id=None):
        original = AiClient.complete
        def generate(client, messages, **kwargs):
            purpose = kwargs.get("purpose", "")
            if not purpose.startswith("response.generate.runtime."):
                return original(client, messages, **kwargs)
            index = len(self.response_calls)
            self.response_calls.append(purpose)
            if index >= len(outcomes):
                raise AssertionError("出现未授权的额外 Response 请求")
            value = outcomes[index]
            if isinstance(value, Exception):
                raise value
            return value
        request_id = str(uuid.uuid4())
        with (
            patch("app.services.memory.RedisShortTermMemoryStore._connect", return_value=None),
            patch("app.services.stream_snapshots.ChatTurnSnapshotStore._connect", return_value=None),
            patch("app.services.ai.AiClient.complete", generate),
            patch.object(ChatService, "_run_model_generation",
                         side_effect=AssertionError("运行时结束后不得二次生成")),
        ):
            events = [event async for event in ChatService(self.db, self.settings).start_chat(
                self.user, ChatRequest(requestId=request_id, message=message, sessionId=session_id))]
        db = self.SessionLocal()
        return db, ChatTurnService(db, self.settings).get_owned(self.user.id, request_id), events

    def assert_failed_without_history(self, db, turn):
        self.assertEqual(turn.status, "FAILED")
        self.assertFalse(turn.completion_verified)
        self.assertIsNone(turn.assistant_message_id)
        self.assertEqual(db.query(ChatMessage).filter(
            ChatMessage.session_id == turn.session_id, ChatMessage.role == "ASSISTANT").count(), 0)
        trace = db.get(AgentRunTrace, turn.trace_id)
        self.assertIsNotNone(trace.finalized_at)
        self.assertFalse(json.loads(trace.generation_json)["completionVerified"])

    async def test_study_plan_followup_persists_question_and_streams_success(self):
        message = "帮我根据这学期的课程和截止时间制定一份学习计划。"
        decision = PlanningResultV6.model_validate({
            "schemaVersion": 6,
            "workItems": [{"intent": "ACADEMIC", "objective": "制定学习计划",
                           "taskText": message, "sourceRefs": ["current:0"],
                           "contextRefs": [], "dependsOn": []}],
        })
        with patch("app.services.understanding.UnderstandingService.classify",
                   return_value=UnderstandingInvocationResult(decision, 1, 1)):
            db, turn, _ = await self._run([], message=message)
        try:
            self.assertEqual(turn.status, "COMPLETED", (turn.error, turn.partial_content, turn.generation_metadata_json))
            self.assertIn("课程", turn.partial_content)
            session_id = db.get(ChatSession, turn.session_id).public_id
        finally:
            db.close()
        db, turn, events = await self._run([], message="数学", session_id=session_id)
        try:
            self.assertEqual(turn.status, "COMPLETED")
            self.assertTrue(turn.completion_verified)
            self.assertIn("目标日期", turn.partial_content)
            self.assertIsNotNone(turn.assistant_message_id)
            pending = db.query(PendingClarification).filter_by(session_id=turn.session_id).one()
            self.assertEqual(json.loads(pending.known_arguments_json), {"course": "数学"})
            self.assertEqual(pending.status, "WAITING_USER")
            self.assertFalse(any("event: error" in event for event in events))
            self.assertTrue(any("目标日期" in event for event in events))
            self.assertEqual(self.response_calls, [])
        finally:
            db.close()

    async def test_internal_exception_is_not_reported_as_provider_failure(self):
        with patch("app.services.turn_execution.TurnExecutionService.execute",
                   side_effect=TypeError("private fixture detail")):
            db, turn, events = await self._run([])
        try:
            self.assert_failed_without_history(db, turn)
            self.assertEqual(turn.error, "INTERNAL_EXECUTION_ERROR")
            self.assertIn("内部错误", "".join(events))
            self.assertNotIn("private fixture detail", "".join(events))
        finally:
            db.close()

    def test_provider_transport_failure_keeps_provider_error_code(self):
        reason, code = _exception_reason(httpx.ConnectError("offline"))
        self.assertEqual(reason, ModelFinishReason.ERROR)
        self.assertEqual(code, "PROVIDER_REQUEST_FAILED")

    async def test_length_then_stop_replaces_incomplete_candidate_once(self):
        db, turn, events = await self._run([
            completion("不应保存的截断片段", ModelFinishReason.LENGTH),
            completion("当前完整回答"),
        ])
        try:
            self.assertEqual(len(self.response_calls), 2)
            self.assertEqual(turn.status, "COMPLETED")
            self.assertTrue(turn.completion_verified)
            self.assertEqual(turn.partial_content, "当前完整回答")
            self.assertEqual(db.query(ChatMessage).filter(
                ChatMessage.session_id == turn.session_id, ChatMessage.role == "ASSISTANT").count(), 1)
            self.assertNotIn("不应保存的截断片段", "".join(events))
        finally:
            db.close()

    async def test_two_length_attempts_fail_without_assistant_message(self):
        db, turn, _ = await self._run([completion("截断", ModelFinishReason.LENGTH)] * 2)
        try:
            self.assertEqual(len(self.response_calls), 2)
            self.assert_failed_without_history(db, turn)
            self.assertEqual(turn.finish_reason, "LENGTH")
            self.assertNotIn("截断", turn.partial_content)
        finally:
            db.close()

    async def test_provider_eof_never_publishes_unverified_partial(self):
        error = IncompleteGenerationError("fixture eof",
            completion("", ModelFinishReason.PROVIDER_EOF).metadata)
        db, turn, events = await self._run([error, error])
        try:
            self.assertEqual(len(self.response_calls), 2)
            self.assert_failed_without_history(db, turn)
            self.assertEqual(turn.finish_reason, "PROVIDER_EOF")
            self.assertTrue(any("event: error" in event for event in events))
        finally:
            db.close()

    async def test_empty_stop_retries_once_then_persists_single_nonempty_message(self):
        db, turn, _ = await self._run([completion(""), completion("完整答复")])
        try:
            self.assertEqual(len(self.response_calls), 2)
            self.assertEqual(turn.status, "COMPLETED")
            self.assertEqual(turn.partial_content, "完整答复")
            self.assertEqual(db.query(ChatMessage).filter(
                ChatMessage.session_id == turn.session_id, ChatMessage.role == "ASSISTANT").count(), 1)
        finally:
            db.close()

    async def test_two_empty_stops_expose_failure_copy_without_success_history(self):
        db, turn, _ = await self._run([completion(""), completion("")])
        try:
            self.assertEqual(len(self.response_calls), 2)
            self.assert_failed_without_history(db, turn)
            self.assertTrue(turn.partial_content)
        finally:
            db.close()

    async def test_provider_error_recovery_is_bounded(self):
        db, turn, _ = await self._run([RuntimeError("offline")] * 2)
        try:
            self.assertEqual(len(self.response_calls), 2)
            self.assert_failed_without_history(db, turn)
            self.assertEqual(turn.finish_reason, "ERROR")
        finally:
            db.close()

    async def test_content_filter_clears_visible_content_without_retry(self):
        db, turn, events = await self._run([completion("不应展示的内容", ModelFinishReason.CONTENT_FILTER)])
        try:
            self.assertEqual(len(self.response_calls), 1)
            self.assert_failed_without_history(db, turn)
            self.assertEqual(turn.finish_reason, "CONTENT_FILTER")
            self.assertEqual(turn.partial_content, "")
            self.assertNotIn("不应展示的内容", "".join(events))
        finally:
            db.close()

    async def test_runtime_generates_once_and_does_not_call_legacy_stream(self):
        db, turn, _ = await self._run([completion("完整答复")])
        try:
            self.assertEqual(self.response_calls, ["response.generate.runtime.attempt1"])
            generation = json.loads(turn.generation_metadata_json)
            self.assertEqual(generation["source"], "APPLICATION")
            self.assertEqual(generation["continuationCount"], 0)
            self.assertIsNotNone(generation["turnMetrics"]["firstContentReadyMs"])
        finally:
            db.close()

    def test_overlap_merge_uses_longest_exact_suffix_prefix(self):
        # 保留历史流式工具的纯函数兼容，不把它当作当前运行时入口。
        self.assertEqual(merge_continuation("前文学院签字后提交住宿服务中心",
            "学院签字后提交住宿服务中心，然后审核"),
            "前文学院签字后提交住宿服务中心，然后审核")
