from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Callable, Iterator
from typing import Any

from app.services.model_completion import ModelCompletionMetadata


TOKEN_PROVIDER = "PROVIDER"
TOKEN_ESTIMATED = "ESTIMATED"
TOKEN_UNAVAILABLE = "UNAVAILABLE"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _milliseconds(later_ns: int, earlier_ns: int) -> int:
    return max(0, int((later_ns - earlier_ns) // 1_000_000))


@dataclass
class ModelCallMetric:
    sequence: int
    purpose: str
    provider: str
    model: str
    stream: bool
    started_offset_ms: int
    duration_ms: int | None = None
    ttft_ms: int | None = None
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    estimated_prompt_tokens: int | None = None
    estimated_output_tokens: int | None = None
    prompt_token_source: str = TOKEN_UNAVAILABLE
    output_token_source: str = TOKEN_UNAVAILABLE
    finish_reason: str = ""
    status: str = "STARTED"
    error_code: str = ""
    _started_ns: int = field(default=0, repr=False)
    _first_delta_ns: int | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ModelCallHandle:
    collector: TurnMetricsCollector | None
    sequence: int = 0
    noop: bool = False


class TurnMetricsCollector:
    def __init__(
        self,
        request_id: str,
        *,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
        utc_now: Callable[[], datetime] = _utc_now,
        started_ns: int | None = None,
        started_at: datetime | None = None,
    ) -> None:
        self.request_id = request_id
        self._clock_ns = clock_ns
        self._utc_now = utc_now
        self._started_ns = clock_ns() if started_ns is None else started_ns
        self._started_at = started_at or utc_now()
        self._calls: list[ModelCallMetric] = []
        self._first_content_ready_ns: int | None = None
        self._finished_ns: int | None = None
        self._completed_at: datetime | None = None
        self._status = "STARTED"
        self._final_model_ttft_ms: int | None = None
        self._server_e2e_ttft_ms: int | None = None
        self._orchestration_before_model_ms: int | None = None
        self._external_calls: dict[tuple[str, int, int], dict[str, Any]] = {}
        self._token_usage_coverage = "COMPLETE"

    def start_model_call(
        self,
        purpose: str,
        provider: str,
        model: str,
        stream: bool,
        estimated_prompt_tokens: int | None,
    ) -> ModelCallHandle:
        now_ns = self._clock_ns()
        metric = ModelCallMetric(
            sequence=len(self._calls) + 1,
            purpose=purpose,
            provider=provider,
            model=model,
            stream=stream,
            started_offset_ms=_milliseconds(now_ns, self._started_ns),
            estimated_prompt_tokens=_nonnegative(estimated_prompt_tokens),
            _started_ns=now_ns,
        )
        self._calls.append(metric)
        return ModelCallHandle(self, metric.sequence)

    def mark_model_first_delta(self, handle: ModelCallHandle, text: str) -> None:
        if not text or handle.collector is not self:
            return
        metric = self._metric(handle)
        if metric is None or metric._first_delta_ns is not None:
            return
        now_ns = self._clock_ns()
        metric._first_delta_ns = now_ns
        metric.ttft_ms = _milliseconds(now_ns, metric._started_ns)
        if metric.purpose == "response.generate" and self._server_e2e_ttft_ms is None:
            self._final_model_ttft_ms = metric.ttft_ms
            self._server_e2e_ttft_ms = _milliseconds(now_ns, self._started_ns)
            self._orchestration_before_model_ms = metric.started_offset_ms

    def finish_model_call(
        self,
        handle: ModelCallHandle,
        *,
        metadata: ModelCompletionMetadata | None = None,
        estimated_output_tokens: int | None = None,
        status: str = "COMPLETED",
        error_code: str = "",
    ) -> None:
        if handle.collector is not self:
            return
        metric = self._metric(handle)
        if metric is None or metric.status != "STARTED":
            return
        metric.duration_ms = _milliseconds(self._clock_ns(), metric._started_ns)
        metric.estimated_output_tokens = _nonnegative(estimated_output_tokens)
        metric.status = status if status in {"COMPLETED", "FAILED", "CANCELLED"} else "FAILED"
        metric.error_code = error_code
        if metadata is not None:
            metric.finish_reason = metadata.finish_reason.value
            metric.thinking_tokens = _nonnegative(metadata.usage.thinking_tokens)
        provider_usage_is_exact = metric.provider.lower() != "mock"
        prompt_usage = _nonnegative(metadata.usage.prompt_tokens) if metadata is not None else None
        output_usage = _nonnegative(metadata.usage.output_tokens) if metadata is not None else None
        metric.prompt_tokens, metric.prompt_token_source = _resolve_token_value(
            prompt_usage,
            metric.estimated_prompt_tokens,
            provider_usage_is_exact,
        )
        metric.output_tokens, metric.output_token_source = _resolve_token_value(
            output_usage,
            metric.estimated_output_tokens,
            provider_usage_is_exact,
        )

    def mark_first_content_ready(self) -> None:
        if self._first_content_ready_ns is None:
            self._first_content_ready_ns = self._clock_ns()

    def mark_turn_finished(self, status: str) -> None:
        if self._finished_ns is not None:
            return
        self._finished_ns = self._clock_ns()
        self._completed_at = self._utc_now()
        self._status = status

    def merge_tool_telemetry(self, envelope: dict[str, Any]) -> None:
        tool_call_id = str(envelope.get("toolCallId") or "")
        attempt = int(envelope.get("attempt") or 0)
        coverage = str(envelope.get("tokenUsageCoverage") or "UNAVAILABLE")
        if coverage != "COMPLETE":
            self._token_usage_coverage = "PARTIAL" if self._external_calls else coverage
        for raw in envelope.get("calls") or []:
            if not isinstance(raw, dict):
                continue
            sequence = int(raw.get("sequence") or 0)
            key = (tool_call_id, attempt, sequence)
            if not tool_call_id or sequence <= 0 or key in self._external_calls:
                continue
            item = dict(raw)
            item.update({
                "toolCallId": tool_call_id,
                "attempt": attempt,
                "serverGeneration": int(envelope.get("serverGeneration") or 0),
                "sourceProcess": "mcp",
            })
            self._external_calls[key] = item

    def as_dict(self) -> dict:
        calls = sorted(self._calls, key=lambda item: item.sequence)
        call_dicts = [self._call_dict(item) for item in calls]
        external_calls = list(self._external_calls.values())
        all_calls = [*call_dicts, *external_calls]
        prompt_tokens = sum(item.get("promptTokens") or 0 for item in all_calls)
        output_tokens = sum(item.get("outputTokens") or 0 for item in all_calls)
        thinking_tokens = sum(item.get("thinkingTokens") or 0 for item in all_calls)
        exact_calls = sum(
            item.get("promptTokenSource") == TOKEN_PROVIDER and item.get("outputTokenSource") == TOKEN_PROVIDER
            for item in all_calls
        )
        unavailable_calls = sum(
            TOKEN_UNAVAILABLE in {item.get("promptTokenSource"), item.get("outputTokenSource")}
            for item in all_calls
        )
        estimated_calls = len(all_calls) - exact_calls - unavailable_calls
        sources = {
            source
            for item in all_calls
            for source in (item.get("promptTokenSource"), item.get("outputTokenSource"))
        }
        if all_calls and exact_calls == len(all_calls):
            accuracy = "EXACT"
        elif TOKEN_PROVIDER in sources:
            accuracy = "MIXED"
        elif all_calls and unavailable_calls == 0:
            accuracy = "ESTIMATED"
        else:
            accuracy = "UNAVAILABLE"
        total_unavailable = unavailable_calls
        coverage = self._token_usage_coverage
        if total_unavailable:
            coverage = "PARTIAL" if all_calls else "UNAVAILABLE"
        return {
            "schemaVersion": 2,
            "requestId": self.request_id,
            "status": self._status,
            "startedAt": self._started_at.astimezone(UTC).isoformat(),
            "completedAt": self._completed_at.astimezone(UTC).isoformat() if self._completed_at else None,
            "finalModelTtftMs": self._final_model_ttft_ms,
            "serverE2eTtftMs": self._server_e2e_ttft_ms,
            "firstContentReadyMs": self._offset(self._first_content_ready_ns),
            "serverTurnDurationMs": self._offset(self._finished_ns),
            "orchestrationBeforeModelMs": self._orchestration_before_model_ms,
            "tokenUsage": {
                "promptTokens": prompt_tokens,
                "outputTokens": output_tokens,
                "thinkingTokens": thinking_tokens,
                "totalTokens": prompt_tokens + output_tokens,
                "accuracy": accuracy,
                "providerCallCount": len(all_calls),
                "exactCallCount": exact_calls,
                "estimatedCallCount": estimated_calls,
                "unavailableCallCount": total_unavailable,
                "knownTokensTotal": prompt_tokens + output_tokens,
                "tokenUsageCoverage": coverage,
                "includesEmbeddings": False,
            },
            "calls": all_calls,
        }

    def _metric(self, handle: ModelCallHandle) -> ModelCallMetric | None:
        index = handle.sequence - 1
        if index < 0 or index >= len(self._calls):
            return None
        metric = self._calls[index]
        return metric if metric.sequence == handle.sequence else None

    def _offset(self, value_ns: int | None) -> int | None:
        return _milliseconds(value_ns, self._started_ns) if value_ns is not None else None

    @staticmethod
    def _call_dict(item: ModelCallMetric) -> dict:
        return {
            "sequence": item.sequence,
            "stage": _call_stage(item.purpose),
            "purpose": item.purpose,
            "provider": item.provider,
            "model": item.model,
            "stream": item.stream,
            "startedOffsetMs": item.started_offset_ms,
            "durationMs": item.duration_ms,
            "ttftMs": item.ttft_ms,
            "promptTokens": item.prompt_tokens,
            "outputTokens": item.output_tokens,
            "thinkingTokens": item.thinking_tokens,
            "estimatedPromptTokens": item.estimated_prompt_tokens,
            "estimatedOutputTokens": item.estimated_output_tokens,
            "promptTokenSource": item.prompt_token_source,
            "outputTokenSource": item.output_token_source,
            "finishReason": item.finish_reason,
            "status": item.status,
            "errorCode": item.error_code,
            "normalizedErrorCode": item.error_code or None,
            "retryCount": _call_retry_count(item.purpose),
        }


_current_collector: ContextVar[TurnMetricsCollector | None] = ContextVar(
    "turn_metrics_collector",
    default=None,
)


@contextmanager
def bind_turn_metrics(collector: TurnMetricsCollector) -> Iterator[TurnMetricsCollector]:
    token = _current_collector.set(collector)
    try:
        yield collector
    finally:
        _current_collector.reset(token)


def current_turn_metrics() -> TurnMetricsCollector | None:
    return _current_collector.get()


def start_model_call(
    purpose: str,
    provider: str,
    model: str,
    stream: bool,
    estimated_prompt_tokens: int | None,
) -> ModelCallHandle:
    collector = current_turn_metrics()
    if collector is None:
        return ModelCallHandle(None, noop=True)
    return collector.start_model_call(purpose, provider, model, stream, estimated_prompt_tokens)


def mark_model_first_delta(handle: ModelCallHandle, text: str) -> None:
    if handle.collector is not None:
        handle.collector.mark_model_first_delta(handle, text)


def finish_model_call(
    handle: ModelCallHandle,
    *,
    metadata: ModelCompletionMetadata | None = None,
    estimated_output_tokens: int | None = None,
    status: str = "COMPLETED",
    error_code: str = "",
) -> None:
    if handle.collector is not None:
        handle.collector.finish_model_call(
            handle,
            metadata=metadata,
            estimated_output_tokens=estimated_output_tokens,
            status=status,
            error_code=error_code,
        )


def mark_first_content_ready() -> None:
    collector = current_turn_metrics()
    if collector is not None:
        collector.mark_first_content_ready()


def _resolve_token_value(
    provider_value: int | None,
    estimated_value: int | None,
    provider_usage_is_exact: bool,
) -> tuple[int | None, str]:
    if provider_value is not None and provider_usage_is_exact:
        return provider_value, TOKEN_PROVIDER
    if estimated_value is not None:
        return estimated_value, TOKEN_ESTIMATED
    if provider_value is not None:
        return provider_value, TOKEN_ESTIMATED
    return None, TOKEN_UNAVAILABLE


def _nonnegative(value: int | None) -> int | None:
    if value is None:
        return None
    return max(0, int(value))


def _call_stage(purpose: str) -> str:
    value = str(purpose or "")
    if value.startswith("response."):
        return "response"
    if value.startswith("knowledge.") or value in {"planner", "grader", "grader_retry"}:
        return "knowledge"
    if value.startswith("safety."):
        return "safety"
    return value.split(".", 1)[0] or "unknown"


def _call_retry_count(purpose: str) -> int:
    value = str(purpose or "")
    return 1 if "retry" in value.lower() or "repair" in value.lower() else 0
