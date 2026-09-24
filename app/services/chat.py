from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import time
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Literal

import httpx
from sqlalchemy.orm import Session, sessionmaker

from app.agents.harness import AgentHarnessOutcome, MindBridgeAgentHarness
from app.core.config import Settings
from app.models.entities import ChatMessage, ChatSession, ChatTurn, UserAccount
from app.schemas.dtos import AiMessage, ChatRequest, ChatStreamEvent
from app.services.ai import AiClient
from app.services.chat_turns import ChatTurnService, TERMINAL_TURN_STATUSES
from app.services.context_builder import estimate_tokens
from app.services.memory import RedisShortTermMemoryStore
from app.services.memory_jobs import MemoryJobService, get_memory_worker
from app.services.model_completion import (
    IncompleteGenerationError,
    ModelCompletionMetadata,
    ModelFinishReason,
    ModelProtocolError,
    ModelUsage,
    MODEL_PROTOCOL_ERROR,
    PROVIDER_EMPTY_OUTPUT,
    PROVIDER_EOF,
    PROVIDER_REQUEST_FAILED,
    RETRYABLE_ZERO_OUTPUT_ERRORS,
    TURN_BUDGET_EXCEEDED,
)
from app.services.stream_snapshots import ChatTurnSnapshotStore
from app.services.trace import AgentTraceService
from app.services.turn_execution import CompletionAttempt, GenerationOutcome, TurnExecutionService
from app.services.turn_metrics import (
    TurnMetricsCollector,
    bind_turn_metrics,
    mark_first_content_ready,
)
from app.services.user_memory import UserMemoryService


logger = logging.getLogger(__name__)
INTERNAL_EXECUTION_ERROR = "INTERNAL_EXECUTION_ERROR"
CONTINUATION_INSTRUCTION = (
    "仅从上一个回答的中断处继续。不要重复已有内容，不改变已给出的事实和结构，"
    "不新增引用资料中没有的学校规定。直接输出续写正文，不要写“继续”“接上文”等前缀。"
)


class ChatTaskRegistry:
    _tasks: dict[str, asyncio.Task] = {}

    @classmethod
    def start(cls, key: str, coroutine) -> asyncio.Task:
        task = cls._tasks.get(key)
        if task is not None and not task.done():
            coroutine.close()
            return task
        task = asyncio.create_task(coroutine, name=f"chat-turn:{key}")
        cls._tasks[key] = task
        task.add_done_callback(lambda completed, request_id=key: cls._discard(request_id, completed))
        return task

    @classmethod
    def active(cls, key: str) -> bool:
        task = cls._tasks.get(key)
        return task is not None and not task.done()

    @classmethod
    def _discard(cls, key: str, task: asyncio.Task) -> None:
        if cls._tasks.get(key) is task:
            cls._tasks.pop(key, None)
        try:
            task.result()
        except asyncio.CancelledError:
            logger.warning("Chat generation task cancelled request_id=%s", key)
        except Exception:
            logger.exception("Chat generation task failed request_id=%s", key)


class ChatService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.session_factory = sessionmaker(bind=db.get_bind(), autoflush=False, autocommit=False)
        self.snapshots = ChatTurnSnapshotStore(settings)

    def start_chat(self, user: UserAccount, request: ChatRequest):
        started_ns = time.perf_counter_ns()
        started_at = datetime.now(UTC)
        turn, created = ChatTurnService(self.db, self.settings).create_or_get(
            user, request.requestId, request.sessionId, request.message.strip()
        )
        if created:
            current_message = ChatTurnService(self.db, self.settings).get_user_message(turn)
            session = self.db.get(ChatSession, turn.session_id)
            if session is None:
                raise ValueError("生成任务关联的会话不存在")
            try:
                RedisShortTermMemoryStore(self.settings).append(
                    session.public_id,
                    current_message.role,
                    current_message.content,
                    current_message.id,
                    user_id=user.id,
                )
            except Exception as exc:
                logger.warning("User message cache append unavailable request_id=%s: %s", request.requestId, exc)
            collector = TurnMetricsCollector(
                request.requestId,
                started_ns=started_ns,
                started_at=started_at,
            )
            ChatTaskRegistry.start(request.requestId, self._generate(turn.id, user.id, collector))
        return self.stream_turn(user.id, request.requestId)

    async def stream_turn(self, user_id: int, request_id: str):
        initial = self._turn_state(user_id, request_id)
        yield sse("meta", self._event("meta", request_id, initial).model_dump())
        last_content = None
        interval = max(50, int(self.settings.chat_turn_poll_interval_ms)) / 1000
        while True:
            state = self.snapshots.read(request_id, user_id) or self._turn_state(user_id, request_id)
            content = str(state.get("content") or "")
            status = str(state.get("status") or "RECEIVED")
            if content != last_content:
                yield sse("snapshot", self._event("snapshot", request_id, state, content=content).model_dump())
                last_content = content
            if status in TERMINAL_TURN_STATUSES:
                if status in {"FAILED", "INTERRUPTED"}:
                    yield sse(
                        "error",
                        self._event(
                            "error",
                            request_id,
                            state,
                            message=_public_error_message(str(state.get("error") or "GENERATION_FAILED")),
                        ).model_dump(),
                    )
                yield sse("done", self._event("done", request_id, state).model_dump())
                return
            await asyncio.sleep(interval)

    async def _generate(
        self,
        turn_id: int,
        user_id: int,
        collector: TurnMetricsCollector,
    ) -> None:
        with bind_turn_metrics(collector):
            await self._generate_bound(turn_id, user_id, collector)

    async def _generate_bound(
        self,
        turn_id: int,
        user_id: int,
        collector: TurnMetricsCollector,
    ) -> None:
        db = self.session_factory()
        turn: ChatTurn | None = None
        session: ChatSession | None = None
        user: UserAccount | None = None
        harness: MindBridgeAgentHarness | None = None
        harness_outcome: AgentHarnessOutcome | None = None
        try:
            turn = db.get(ChatTurn, turn_id)
            user = db.get(UserAccount, user_id)
            session = db.get(ChatSession, turn.session_id) if turn else None
            if turn is None or user is None or session is None or turn.status not in {"RECEIVED", "GENERATING"}:
                return
            turn.status = "GENERATING"
            turn.updated_at = _now()
            db.commit()
            self._write_snapshot(turn, session.public_id, "")

            execution = await TurnExecutionService(self.settings).execute(
                db,
                user,
                session,
                turn,
                collector,
                model_generation=self._run_model_generation,
            )
            harness_outcome = execution.harness
            generation = execution.generation
            harness = MindBridgeAgentHarness(db, self.settings)
            turn = db.get(ChatTurn, turn_id)
            turn.trace_id = execution.trace_id
            turn.updated_at = _now()
            db.commit()

            if generation.source == "APPLICATION":
                self._persist_progress(db, turn, session.public_id, generation.content, force=True)

            if generation.completion_verified:
                collector.mark_turn_finished("COMPLETED")
                metadata = self._generation_metadata(generation, collector)
                assistant = self._finalize_completed_turn(db, turn, session, generation, metadata)
                self._write_snapshot(turn, session.public_id, generation.content)
                self._after_completed_turn(db, harness, harness_outcome, session, assistant)
                try:
                    await harness.dispatch_tools(harness_outcome.tool_plan)
                except Exception as exc:
                    logger.warning(
                        "Post-response tool dispatch failed request_id=%s report_id=%s: %s",
                        turn.request_id,
                        harness_outcome.report_id,
                        exc,
                        exc_info=True,
                    )
            else:
                collector.mark_turn_finished("FAILED")
                metadata = self._generation_metadata(generation, collector)
                self._finalize_failed_turn(db, turn, session, generation, metadata)
        except asyncio.CancelledError:
            db.rollback()
            if turn is not None and session is not None and user is not None:
                cancelled = self._failure_outcome(
                    turn.partial_content or "",
                    ModelFinishReason.CANCELLED,
                    "GENERATION_CANCELLED",
                )
                try:
                    self._ensure_trace(db, turn, user, session)
                    collector.mark_turn_finished("INTERRUPTED")
                    self._finalize_failed_turn(
                        db,
                        turn,
                        session,
                        cancelled,
                        self._generation_metadata(cancelled, collector),
                        interrupted=True,
                    )
                except Exception:
                    db.rollback()
                    logger.exception("Cancelled chat finalization failed turn_id=%s", turn_id)
            raise
        except Exception as exc:
            db.rollback()
            turn = db.get(ChatTurn, turn_id)
            if turn is not None:
                session = db.get(ChatSession, turn.session_id)
                user = db.get(UserAccount, turn.user_id)
            if turn is not None and session is not None and user is not None:
                self._ensure_trace(db, turn, user, session)
                reason, code = _exception_reason(exc)
                failed = self._failure_outcome(turn.partial_content or "", reason, code)
                collector.mark_turn_finished("FAILED")
                self._finalize_failed_turn(
                    db,
                    turn,
                    session,
                    failed,
                    self._generation_metadata(failed, collector),
                )
            logger.exception("Background chat generation failed turn_id=%s", turn_id)
        finally:
            db.close()

    async def _run_model_generation(
        self,
        db: Session,
        turn: ChatTurn,
        session_public_id: str,
        client: AiClient,
        messages: list[AiMessage],
    ) -> GenerationOutcome:
        first = await self._run_completion_attempt(
            client,
            messages,
            lambda content: self._persist_progress(db, turn, session_public_id, content),
            purpose="response.generate",
        )
        attempts = [first]
        content = first.content
        finish_reason = first.metadata.finish_reason
        continuation_count = 0

        if self._zero_output_retry_allowed(first):
            retry = await self._run_completion_attempt(
                client,
                messages,
                lambda retry_content: self._persist_progress(
                    db, turn, session_public_id, retry_content
                ),
                purpose="response.generate.retry1",
            )
            attempts.append(retry)
            content = retry.content
            finish_reason = retry.metadata.finish_reason

        if finish_reason == ModelFinishReason.LENGTH and content.strip() and self._continuation_allowed(first):
            continuation_messages = self._build_continuation_messages(messages, content, client)
            if continuation_messages is not None:
                remaining = max(1, self.settings.chat_response_max_total_tokens - _attempt_tokens(first))
                continuation_settings = copy.copy(client.settings)
                continuation_settings.ai_max_tokens = min(continuation_settings.ai_max_tokens, remaining)
                continuation_client = AiClient(
                    continuation_settings,
                    purpose_namespace=client.purpose_namespace,
                )
                continuation = await self._run_completion_attempt(
                    continuation_client,
                    continuation_messages,
                    lambda partial: self._persist_progress(
                        db,
                        turn,
                        session_public_id,
                        merge_continuation(content, partial),
                    ),
                    purpose="response.continuation",
                )
                attempts.append(continuation)
                continuation_count = 1
                content = merge_continuation(content, continuation.content)
                finish_reason = continuation.metadata.finish_reason

        verified = bool(content.strip()) and finish_reason == ModelFinishReason.STOP
        error_code = "" if verified else (attempts[-1].error_code or _error_code(finish_reason))
        if not content.strip() and error_code in RETRYABLE_ZERO_OUTPUT_ERRORS:
            content = "生成服务暂时不可用，请稍后重试。"
            self._persist_progress(db, turn, session_public_id, content)
        return GenerationOutcome(
            content=content,
            source="MODEL",
            complete=verified,
            completion_verified=verified,
            finish_reason=finish_reason,
            attempts=tuple(attempts),
            continuation_count=continuation_count,
            error_code=error_code,
        )

    async def _run_completion_attempt(
        self,
        client: AiClient,
        messages: list[AiMessage],
        progress,
        *,
        purpose: str,
    ) -> CompletionAttempt:
        content = ""
        terminal: ModelCompletionMetadata | None = None
        error_code = ""
        last_flush = time.monotonic()
        last_length = 0
        try:
            async for event in client.stream_events(messages, purpose=purpose):
                if terminal is not None:
                    raise ModelProtocolError("终止事件后仍收到模型数据")
                if event.kind == "delta":
                    content += event.text
                    elapsed_ms = (time.monotonic() - last_flush) * 1000
                    growth = len(content) - last_length
                    if (
                        elapsed_ms >= self.settings.chat_turn_snapshot_interval_ms
                        or growth >= self.settings.chat_turn_snapshot_min_chars
                    ):
                        progress(content)
                        last_flush = time.monotonic()
                        last_length = len(content)
                else:
                    terminal = event.metadata
        except IncompleteGenerationError as exc:
            error_code = PROVIDER_EOF
            terminal = exc.metadata or _fallback_metadata(client, ModelFinishReason.PROVIDER_EOF)
        except ModelProtocolError as exc:
            error_code = MODEL_PROTOCOL_ERROR
            terminal = exc.metadata or _fallback_metadata(client, ModelFinishReason.ERROR)
        progress(content)
        if terminal is None:
            terminal = _fallback_metadata(client, ModelFinishReason.PROVIDER_EOF)
            error_code = PROVIDER_EOF
        if not content.strip() and terminal.finish_reason in {ModelFinishReason.STOP, ModelFinishReason.LENGTH}:
            terminal = _replace_finish_reason(terminal, ModelFinishReason.EMPTY_OUTPUT)
            error_code = PROVIDER_EMPTY_OUTPUT
        if not content.strip() and not error_code:
            error_code = _error_code(terminal.finish_reason)
        return CompletionAttempt(content=content, metadata=terminal, error_code=error_code)

    def _zero_output_retry_allowed(self, attempt: CompletionAttempt) -> bool:
        return (
            bool(self.settings.ai_empty_output_retry_enabled)
            and not attempt.content.strip()
            and attempt.error_code in RETRYABLE_ZERO_OUTPUT_ERRORS
            and _attempt_tokens(attempt) < self.settings.chat_response_max_total_tokens
        )

    def _continuation_allowed(self, first: CompletionAttempt) -> bool:
        return (
            self.settings.chat_response_max_continuations >= 1
            and _attempt_tokens(first) < self.settings.chat_response_max_total_tokens
        )

    def _build_continuation_messages(
        self,
        original: list[AiMessage],
        partial: str,
        client: AiClient,
    ) -> list[AiMessage] | None:
        current_index = next((i for i in range(len(original) - 1, -1, -1) if original[i].role == "user"), None)
        if current_index is None:
            return None
        systems = [message for message in original if message.role == "system"]
        history = [
            message
            for index, message in enumerate(original)
            if message.role != "system" and index != current_index
        ]
        messages = [
            *systems,
            AiMessage(role="system", content=CONTINUATION_INSTRUCTION),
            *history,
            original[current_index],
            AiMessage(role="assistant", content=partial),
        ]
        if client.settings.ai_provider.lower() == "ollama":
            required = (
                sum(estimate_tokens(message.content) for message in messages)
                + int(client.settings.ai_max_tokens)
                + int(self.settings.context_model_safety_margin_tokens)
            )
            if required > int(self.settings.ollama_num_ctx):
                return None
        return messages

    def _finalize_completed_turn(
        self,
        db: Session,
        turn: ChatTurn,
        session: ChatSession,
        generation: GenerationOutcome,
        metadata: dict,
    ) -> ChatMessage:
        if not generation.completion_verified or generation.finish_reason not in {
            ModelFinishReason.STOP,
            ModelFinishReason.DIRECT_RESPONSE,
        }:
            raise ValueError("未验证的模型输出不能完成 ChatTurn")
        assistant = ChatMessage(
            user_id=turn.user_id,
            session_id=turn.session_id,
            role="ASSISTANT",
            content=generation.content,
        )
        db.add(assistant)
        db.flush()
        turn.assistant_message_id = assistant.id
        turn.status = "COMPLETED"
        turn.finish_reason = generation.finish_reason.value
        turn.completion_verified = True
        turn.generation_metadata_json = _json(metadata)
        turn.partial_content = generation.content
        turn.error = ""
        turn.completed_at = _now()
        turn.updated_at = _now()
        session.touch()
        if turn.trace_id is None:
            raise ValueError("完成 ChatTurn 前必须关联 Trace")
        AgentTraceService(db, self.settings).finalize_generation_trace(turn.trace_id, metadata, commit=False)
        db.add_all([turn, session])
        if self.settings.memory_v3_enabled:
            MemoryJobService(db, self.settings).enqueue_completed_turn(
                user_id=turn.user_id,
                session_id=turn.session_id,
                turn_id=turn.id,
                target_message_id=assistant.id,
            )
        db.commit()
        db.refresh(assistant)
        return assistant

    def _finalize_failed_turn(
        self,
        db: Session,
        turn: ChatTurn,
        session: ChatSession,
        generation: GenerationOutcome,
        metadata: dict,
        *,
        interrupted: bool = False,
    ) -> None:
        content = "" if generation.finish_reason == ModelFinishReason.CONTENT_FILTER else generation.content
        turn.status = "INTERRUPTED" if interrupted else "FAILED"
        turn.finish_reason = generation.finish_reason.value
        turn.completion_verified = False
        turn.generation_metadata_json = _json(metadata)
        turn.partial_content = content
        turn.error = generation.error_code or _error_code(generation.finish_reason)
        turn.completed_at = _now()
        turn.updated_at = _now()
        if turn.trace_id is None:
            raise ValueError("失败 ChatTurn 前必须关联 Trace")
        AgentTraceService(db, self.settings).finalize_generation_trace(turn.trace_id, metadata, commit=False)
        db.add(turn)
        db.commit()
        self._write_snapshot(turn, session.public_id, content)

    def _after_completed_turn(
        self,
        db: Session,
        harness: MindBridgeAgentHarness,
        outcome: AgentHarnessOutcome,
        session: ChatSession,
        assistant: ChatMessage,
    ) -> None:
        try:
            RedisShortTermMemoryStore(self.settings).append(
                session.public_id,
                assistant.role,
                assistant.content,
                assistant.id,
                user_id=assistant.user_id,
            )
        except Exception as exc:
            logger.warning("Assistant cache append unavailable session_id=%s: %s", session.id, exc)
        try:
            UserMemoryService(db, self.settings).mark_used(
                [int(item) for item in outcome.context_manifest.get("user_memory_ids", [])]
            )
        except Exception as exc:
            db.rollback()
            logger.warning("User memory usage update failed session_id=%s: %s", session.id, exc)
        if self.settings.memory_v3_enabled and self.settings.memory_worker_enabled:
            get_memory_worker(self.settings).wake()

    def _ensure_trace(
        self,
        db: Session,
        turn: ChatTurn,
        user: UserAccount,
        session: ChatSession,
    ) -> None:
        if turn.trace_id is not None:
            return
        current = ChatTurnService(db, self.settings).get_user_message(turn)
        trace = AgentTraceService(db, self.settings).create_minimal_trace(
            user=user,
            session=session,
            original_input=current.content,
            sanitized_input=current.content,
        )
        turn.trace_id = trace.id
        db.add(turn)
        db.commit()

    def _failure_outcome(
        self,
        content: str,
        reason: ModelFinishReason,
        error_code: str,
    ) -> GenerationOutcome:
        return GenerationOutcome(
            content=content,
            source="MODEL",
            complete=False,
            completion_verified=False,
            finish_reason=reason,
            attempts=(),
            continuation_count=0,
            error_code=error_code,
        )

    def _generation_metadata(
        self,
        generation: GenerationOutcome,
        collector: TurnMetricsCollector,
    ) -> dict:
        attempts = []
        for index, attempt in enumerate(generation.attempts, start=1):
            item = _metadata_dict(attempt.metadata)
            item.update(
                {
                    "attempt": index,
                    "outputChars": len(attempt.content),
                    "outputHash": _content_hash(attempt.content),
                }
            )
            attempts.append(item)
        last = generation.attempts[-1].metadata if generation.attempts else None
        output_tokens = sum(_attempt_tokens(item) for item in generation.attempts)
        return {
            "schemaVersion": 3,
            "source": generation.source,
            "provider": last.provider if last else "application",
            "model": last.model if last else "application",
            "finishReason": generation.finish_reason.value,
            "providerFinishReason": last.provider_finish_reason if last else "",
            "semanticFinishSeen": last.semantic_finish_seen if last else generation.completion_verified,
            "transportTerminalSeen": last.transport_terminal_seen if last else generation.completion_verified,
            "configuredOutputLimit": last.configured_output_limit if last else 0,
            "promptTokens": sum(
                item.metadata.usage.prompt_tokens or 0 for item in generation.attempts
            ) or None,
            "outputTokens": output_tokens or (estimate_tokens(generation.content) if generation.content else 0),
            "outputChars": len(generation.content),
            "outputHash": _content_hash(generation.content),
            "durationMs": sum(item.metadata.duration_ms for item in generation.attempts),
            "continuationCount": generation.continuation_count,
            "completionVerified": generation.completion_verified,
            "businessStatus": generation.business_status,
            "upstreamErrorCodes": list(generation.upstream_error_codes),
            "errorCode": generation.error_code,
            "attempts": attempts,
            "turnMetrics": collector.as_dict(),
        }

    def _persist_progress(
        self,
        db: Session,
        turn: ChatTurn,
        session_public_id: str,
        content: str,
        force: bool = False,
    ) -> None:
        turn.partial_content = content
        turn.updated_at = _now()
        db.add(turn)
        db.commit()
        if force or content:
            self._write_snapshot(turn, session_public_id, content)
        if content:
            mark_first_content_ready()

    def _write_snapshot(self, turn: ChatTurn, session_public_id: str, content: str) -> None:
        state = _turn_metadata_state(turn)
        self.snapshots.write(
            turn.request_id,
            self.snapshots.payload(
                turn.user_id,
                session_public_id,
                turn.public_id,
                turn.status,
                content,
                finish_reason=state["finishReason"],
                completion_verified=state["completionVerified"],
                partial=state["partial"],
                retryable=state["retryable"],
                continuation_count=state["continuationCount"],
                output_tokens=state["outputTokens"],
                error=turn.error,
            ),
        )

    def _turn_state(self, user_id: int, request_id: str) -> dict:
        db = self.session_factory()
        try:
            turn = ChatTurnService(db, self.settings).get_owned(user_id, request_id)
            session = db.get(ChatSession, turn.session_id)
            content = turn.partial_content or ""
            if turn.status == "COMPLETED" and turn.assistant_message_id:
                assistant = db.get(ChatMessage, turn.assistant_message_id)
                if assistant is not None:
                    content = assistant.content
            return {
                "userId": user_id,
                "sessionId": session.public_id if session else "",
                "turnId": turn.public_id,
                "status": turn.status,
                "content": content,
                "error": turn.error,
                **_turn_metadata_state(turn),
            }
        finally:
            db.close()

    @staticmethod
    def _event(
        event_type: str,
        request_id: str,
        state: dict,
        content: str | None = None,
        message: str | None = None,
    ) -> ChatStreamEvent:
        return ChatStreamEvent(
            type=event_type,
            requestId=request_id,
            turnId=str(state.get("turnId") or ""),
            sessionId=str(state.get("sessionId") or ""),
            status=str(state.get("status") or ""),
            content=content,
            message=message,
            finishReason=state.get("finishReason"),
            completionVerified=state.get("completionVerified"),
            partial=state.get("partial"),
            retryable=state.get("retryable"),
            continuationCount=state.get("continuationCount"),
            outputTokens=state.get("outputTokens"),
        )


def merge_continuation(existing: str, continuation: str) -> str:
    if not existing or not continuation:
        return existing + continuation
    tail = existing[-200:]
    head = continuation[:200]
    maximum = min(len(tail), len(head))
    overlap = 0
    for length in range(maximum, 7, -1):
        if tail[-length:] == head[:length]:
            overlap = length
            break
    return existing + continuation[overlap:]


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _turn_metadata_state(turn: ChatTurn) -> dict:
    try:
        metadata = json.loads(turn.generation_metadata_json or "{}")
    except (TypeError, json.JSONDecodeError):
        metadata = {}
    verified = bool(turn.completion_verified)
    return {
        "finishReason": turn.finish_reason,
        "completionVerified": verified,
        "partial": bool(turn.partial_content) and not verified,
        "retryable": turn.finish_reason in {
            ModelFinishReason.LENGTH.value,
            ModelFinishReason.PROVIDER_EOF.value,
            ModelFinishReason.CANCELLED.value,
            ModelFinishReason.ERROR.value,
        },
        "continuationCount": int(metadata.get("continuationCount") or 0),
        "outputTokens": metadata.get("outputTokens"),
    }


def _metadata_dict(metadata: ModelCompletionMetadata) -> dict:
    value = asdict(metadata)
    value["finish_reason"] = metadata.finish_reason.value
    return value


def _attempt_tokens(attempt: CompletionAttempt) -> int:
    return attempt.metadata.usage.output_tokens or estimate_tokens(attempt.content)


def _replace_finish_reason(
    metadata: ModelCompletionMetadata,
    reason: ModelFinishReason,
) -> ModelCompletionMetadata:
    return ModelCompletionMetadata(
        provider=metadata.provider,
        model=metadata.model,
        finish_reason=reason,
        semantic_finish_seen=metadata.semantic_finish_seen,
        transport_terminal_seen=metadata.transport_terminal_seen,
        terminal_signal=metadata.terminal_signal,
        provider_finish_reason=metadata.provider_finish_reason,
        configured_output_limit=metadata.configured_output_limit,
        usage=metadata.usage,
        thinking_observed=metadata.thinking_observed,
        duration_ms=metadata.duration_ms,
    )


def _fallback_metadata(client: AiClient, reason: ModelFinishReason) -> ModelCompletionMetadata:
    provider = client.settings.ai_provider.lower()
    model = client.settings.openai_model if provider == "openai" else client.settings.ollama_model
    return ModelCompletionMetadata(
        provider=provider,
        model=model,
        finish_reason=reason,
        semantic_finish_seen=False,
        transport_terminal_seen=False,
        terminal_signal="exception",
        provider_finish_reason="",
        configured_output_limit=client.settings.ai_max_tokens,
        usage=ModelUsage(),
    )


def _exception_reason(exc: Exception) -> tuple[ModelFinishReason, str]:
    metadata = getattr(exc, "metadata", None)
    if isinstance(metadata, ModelCompletionMetadata):
        return metadata.finish_reason, _error_code(metadata.finish_reason)
    if isinstance(exc, ModelProtocolError):
        return ModelFinishReason.ERROR, MODEL_PROTOCOL_ERROR
    if isinstance(exc, IncompleteGenerationError):
        return ModelFinishReason.PROVIDER_EOF, PROVIDER_EOF
    if isinstance(exc, httpx.HTTPError):
        return ModelFinishReason.ERROR, PROVIDER_REQUEST_FAILED
    return ModelFinishReason.ERROR, INTERNAL_EXECUTION_ERROR


def _error_code(reason: ModelFinishReason) -> str:
    return {
        ModelFinishReason.LENGTH: TURN_BUDGET_EXCEEDED,
        ModelFinishReason.PROVIDER_EOF: PROVIDER_EOF,
        ModelFinishReason.EMPTY_OUTPUT: PROVIDER_EMPTY_OUTPUT,
        ModelFinishReason.CONTENT_FILTER: "GENERATION_CONTENT_FILTER",
        ModelFinishReason.CANCELLED: "GENERATION_CANCELLED",
        ModelFinishReason.TOOL_CALL: "GENERATION_UNEXPECTED_TOOL_CALL",
        ModelFinishReason.ERROR: PROVIDER_REQUEST_FAILED,
    }.get(reason, MODEL_PROTOCOL_ERROR)


def _public_error_message(code: str) -> str:
    if code == INTERNAL_EXECUTION_ERROR:
        return "处理消息时发生内部错误，本轮未完成。请稍后再试。"
    if code == "GENERATION_CONTENT_FILTER":
        return "回答因安全策略未能生成，请调整问题后重试。"
    if code in {PROVIDER_REQUEST_FAILED, PROVIDER_EOF, PROVIDER_EMPTY_OUTPUT, MODEL_PROTOCOL_ERROR}:
        return "生成服务暂时不可用，请稍后重试。"
    return "回答未完整生成，以上内容可能不完整，请重试。"


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest() if content else ""


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC).replace(tzinfo=None)
