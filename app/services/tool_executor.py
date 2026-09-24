from __future__ import annotations

import json
import inspect
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import replace
from enum import Enum
from typing import Any, Callable

from app.services.tool_models import ToolResult
from app.services.tool_registry import RegisteredTool, ToolRegistry, tool_argument_errors
from app.services.turn_metrics import current_turn_metrics


COUNTED_FAILURES = frozenset({
    "TIMEOUT", "MCP_UNAVAILABLE", "MCP_PROTOCOL_ERROR", "UPSTREAM_TIMEOUT", "UPSTREAM_UNAVAILABLE",
})
FALLBACK_CODES = COUNTED_FAILURES | {"CIRCUIT_OPEN"}


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class TTLCache:
    def __init__(self, max_items: int = 1000):
        self.max_items = max(1, max_items)
        self._items: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key: str) -> Any | None:
        now = time.monotonic()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at <= now:
                self._items.pop(key, None)
                return None
            self._items.move_to_end(key)
            return value

    def put(self, key: str, value: Any, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            return
        with self._lock:
            self._items[key] = (time.monotonic() + ttl_seconds, value)
            self._items.move_to_end(key)
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, recovery_seconds: float = 60.0):
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_seconds = max(0.0, recovery_seconds)
        self.state = CircuitState.CLOSED
        self.failures = 0
        self.opened_at = 0.0
        self._probe_active = False
        self._lock = threading.RLock()

    def allow_call(self) -> bool:
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True
            if self.state == CircuitState.OPEN:
                if time.monotonic() - self.opened_at < self.recovery_seconds:
                    return False
                self.state = CircuitState.HALF_OPEN
            if self._probe_active:
                return False
            self._probe_active = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self.state = CircuitState.CLOSED
            self.failures = 0
            self._probe_active = False

    def record_failure(self) -> None:
        with self._lock:
            self._probe_active = False
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.OPEN
                self.opened_at = time.monotonic()
                return
            self.failures += 1
            if self.failures >= self.failure_threshold:
                self.state = CircuitState.OPEN
                self.opened_at = time.monotonic()

    def release_probe(self) -> None:
        with self._lock:
            self._probe_active = False


class ToolExecutor:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        runtime,
        default_timeout: float = 15.0,
        cache_max_items: int = 1000,
        circuit_failure_threshold: int = 5,
        circuit_recovery_seconds: float = 60.0,
        fallbacks: dict[str, Callable[[ToolResult], ToolResult]] | None = None,
        access_scope: str = "default",
        index_version: str = "",
        rag_pipeline_version: str = "v1",
        max_argument_bytes: int = 16_384,
        result_store=None,
    ):
        self.registry = registry
        self.runtime = runtime
        self.default_timeout = max(0.01, default_timeout)
        self.cache = TTLCache(cache_max_items)
        self.failure_threshold = circuit_failure_threshold
        self.recovery_seconds = circuit_recovery_seconds
        self.fallbacks = fallbacks or {}
        self.access_scope = access_scope
        self.index_version = index_version
        self.rag_pipeline_version = rag_pipeline_version
        self.max_argument_bytes = max_argument_bytes
        self.result_store = result_store
        self._circuits: dict[tuple[str, str], CircuitBreaker] = {}
        self._lock = threading.RLock()

    def execute(
        self,
        *,
        agent_name: str,
        tool_name: str,
        arguments: dict,
        remaining_seconds: float,
        tool_call_id: str = "",
    ) -> ToolResult:
        if remaining_seconds <= 0:
            return ToolResult(False, "DEADLINE_EXCEEDED", error="执行预算已耗尽，工具未派发")
        deadline = time.monotonic() + remaining_seconds
        registered = self.registry.resolve_for_agent(agent_name, tool_name)
        if registered is None:
            return ToolResult(False, "TOOL_NOT_ALLOWED", error="工具不在当前 Agent 的允许列表")
        if not isinstance(arguments, dict):
            return ToolResult(False, "INVALID_ARGUMENT", error="工具 arguments 必须是 object")
        try:
            encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        except (TypeError, UnicodeEncodeError):
            return ToolResult(False, "INVALID_ARGUMENT", error="工具参数无法编码为 UTF-8 JSON")
        if len(encoded) > self.max_argument_bytes:
            return ToolResult(False, "INVALID_ARGUMENT", error="工具参数超过大小限制")
        validation_errors = tool_argument_errors(registered.validator, arguments)
        if validation_errors:
            return ToolResult(
                False,
                "INVALID_ARGUMENT",
                error="工具参数不符合 schema",
                validation_errors=tuple(validation_errors),
            )

        key = self._cache_key(registered, encoded.decode("utf-8"))
        cached = self.cache.get(key)
        if cached is not None:
            if registered.original_name == "rag_search":
                from app.services.retrieval_capture import merge_retrieval_telemetry
                merge_retrieval_telemetry({})
            if isinstance(cached, ToolResult):
                # A cache hit is a new logical execution for authorization and
                # persistence. Never reuse another request's readable execution
                # id even when the underlying public result bytes are identical.
                return replace(
                    cached,
                    cached=True,
                    execution_id="exec_" + uuid.uuid4().hex,
                    cached_from_execution_id=cached.execution_id,
                    persisted=False,
                )
            return ToolResult(True, "OK", data=cached, cached=True)

        circuit = self._circuit(registered)
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            return ToolResult(False, "DEADLINE_EXCEEDED", error="执行预算已耗尽，工具未派发")
        if not circuit.allow_call():
            return self._fallback(registered, ToolResult(
                False, "CIRCUIT_OPEN", degraded=True, retryable=True, error="工具熔断器处于 OPEN 状态"
            ))
        timeout = min(
            registered.per_tool_timeout or self.default_timeout,
            remaining_seconds,
        )
        try:
            raw = self._call_runtime(registered, arguments, timeout, tool_call_id)
            result = replace(parse_call_tool_result(raw), dispatched=True)
            execution_id = "exec_" + uuid.uuid4().hex
            raw_hash = ""
            if self.result_store is not None:
                from app.services.tool_result_store import canonical_hash
                raw_hash = canonical_hash(result.data)
            result = replace(result, execution_id=execution_id, raw_hash=raw_hash)
            if registered.original_name == "rag_search" and result.ok:
                from app.services.retrieval_capture import merge_retrieval_telemetry
                merge_retrieval_telemetry(result.telemetry)
            collector = current_turn_metrics()
            if collector is not None and result.telemetry:
                collector.merge_tool_telemetry(result.telemetry)
        except TimeoutError:
            result = ToolResult(
                False, "TIMEOUT", degraded=True, retryable=True, error="工具调用超时", dispatched=True
            )
        except Exception:
            result = ToolResult(
                False,
                "MCP_UNAVAILABLE",
                degraded=True,
                retryable=True,
                error="MCP 工具暂时不可用",
                dispatched=True,
            )

        if result.ok:
            circuit.record_success()
            if self._cacheable(registered, result.data):
                self.cache.put(key, result, registered.cache_ttl)
            return result
        if result.code in COUNTED_FAILURES:
            circuit.record_failure()
        else:
            circuit.release_probe()
        return self._fallback(registered, result) if result.code in FALLBACK_CODES else result

    def _call_runtime(
        self,
        tool: RegisteredTool,
        arguments: dict,
        timeout: float,
        tool_call_id: str,
    ):
        method = self.runtime.call_tool_sync
        parameters = inspect.signature(method).parameters
        if "request_id" not in parameters and not any(
            item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values()
        ):
            return method(tool.server_alias, tool.original_name, arguments, timeout)
        collector = current_turn_metrics()
        request_id = collector.request_id if collector is not None else ""
        return method(
            tool.server_alias,
            tool.original_name,
            arguments,
            timeout,
            request_id=request_id,
            tool_call_id=tool_call_id,
        )

    def circuit_for(self, server_alias: str, original_name: str) -> CircuitBreaker:
        with self._lock:
            return self._circuits.setdefault(
                (server_alias, original_name),
                CircuitBreaker(self.failure_threshold, self.recovery_seconds),
            )

    def _circuit(self, tool: RegisteredTool) -> CircuitBreaker:
        return self.circuit_for(tool.server_alias, tool.original_name)

    def _cache_key(self, tool: RegisteredTool, normalized_arguments: str) -> str:
        parts = [tool.server_alias, tool.original_name, normalized_arguments, self.access_scope, tool.tool_version]
        if tool.original_name == "rag_search":
            parts.extend([self.index_version, self.rag_pipeline_version])
        return "|".join(parts)

    @staticmethod
    def _cacheable(tool: RegisteredTool, data: Any) -> bool:
        if not tool.read_only or tool.cache_ttl <= 0:
            return False
        if tool.original_name == "rag_search" and isinstance(data, dict):
            diagnostics = data.get("diagnostics") or {}
            degraded = any(diagnostics.get(key) for key in (
                "bm25Degraded", "vectorDegraded", "rewriteDegraded", "rerankDegraded"
            ))
            return data.get("status") in {"OK", "EMPTY"} and not degraded
        return True

    def _fallback(self, tool: RegisteredTool, result: ToolResult) -> ToolResult:
        fallback = self.fallbacks.get(tool.original_name)
        if fallback is None:
            return result
        try:
            fallback_result = fallback(result)
        except Exception:
            return ToolResult(
                result.ok,
                result.code,
                data=result.data,
                cached=False,
                degraded=True,
                retryable=result.retryable,
                error=f"{result.error}; FALLBACK_FAILED",
            )
        return fallback_result


def parse_call_tool_result(raw: Any) -> ToolResult:
    is_error = bool(_read(raw, "isError", _read(raw, "is_error", False)))
    structured = _read(raw, "structuredContent", _read(raw, "structured_content", None))
    if is_error:
        error = structured.get("error") if isinstance(structured, dict) else None
        code = str(error.get("code") or "") if isinstance(error, dict) else ""
        message = str(error.get("message") or "") if isinstance(error, dict) else ""
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", code) or not message:
            return ToolResult(False, "MCP_PROTOCOL_ERROR", degraded=True, retryable=True, error="MCP 错误结构无效")
        return ToolResult(False, code, degraded=code in FALLBACK_CODES, retryable=code in COUNTED_FAILURES, error=message[:500])
    data = structured
    if data is None:
        content = _read(raw, "content", [])
        if not isinstance(content, list) or len(content) != 1 or not isinstance(_read(content[0], "text", None), str):
            return ToolResult(False, "MCP_PROTOCOL_ERROR", degraded=True, retryable=True, error="MCP 成功结果必须包含单个文本 JSON 块")
        text = _read(content[0], "text", "")
        try:
            data = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return ToolResult(False, "MCP_PROTOCOL_ERROR", degraded=True, retryable=True, error="MCP 成功结果无法解析")
    if not isinstance(data, dict):
        return ToolResult(False, "MCP_PROTOCOL_ERROR", degraded=True, retryable=True, error="MCP 成功结果必须是 object")
    telemetry = data.get("_mindbridgeTelemetry") if isinstance(data.get("_mindbridgeTelemetry"), dict) else {}
    if telemetry:
        data = {key: value for key, value in data.items() if key != "_mindbridgeTelemetry"}
    diagnostics = data.get("diagnostics") if isinstance(data.get("diagnostics"), dict) else {}
    degraded = any(bool(diagnostics.get(key)) for key in
                   ("bm25Degraded", "vectorDegraded", "rewriteDegraded", "rerankDegraded"))
    return ToolResult(True, "OK", data=data, telemetry=telemetry, degraded=degraded)


def _read(raw: Any, name: str, default: Any = None) -> Any:
    if isinstance(raw, dict):
        return raw.get(name, default)
    return getattr(raw, name, default)
