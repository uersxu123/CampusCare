from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

from app.services.mcp_runtime import create_mcp_runtime, get_mcp_runtime
from app.services.tool_executor import ToolExecutor
from app.services.tool_registry import ToolRegistry
from app.services.tool_result_store import ToolResultStore


class _CompositeRuntime:
    def __init__(self, mcp_runtime, store: ToolResultStore):
        self.mcp_runtime = mcp_runtime
        self.store = store

    def call_tool_sync(self, server_alias, name, arguments, timeout, **kwargs):
        if server_alias == "context_readonly" and name == "read_tool_evidence":
            try:
                result = self.store.read(
                    str(arguments.get("execution_id") or ""), evidence_ids=list(arguments.get("evidence_ids") or []),
                    cursor=arguments.get("cursor"),
                )
            except Exception:
                result = {"status": "STORE_UNAVAILABLE"}
            if result.get("status") != "OK":
                return {"isError": True, "structuredContent": {"error": {
                    "code": result["status"], "message": "当前授权原文不可读取，请保留未确认信息。",
                }}}
            return {"isError": False, "structuredContent": result}
        return self.mcp_runtime.call_tool_sync(server_alias, name, arguments, timeout, **kwargs)


@dataclass(frozen=True)
class ChatToolRuntime:
    registry: ToolRegistry
    executor: ToolExecutor
    runtime: object | None = None
    result_engine: object | None = None

    def close(self) -> None:
        if self.runtime is not None:
            self.runtime.shutdown()
        if self.result_engine is not None:
            self.result_engine.dispose()
        cache = getattr(getattr(self.executor, "result_store", None), "redis", None)
        if cache is not None:
            cache.close()


_bundle: ChatToolRuntime | None = None
_lock = threading.Lock()
_scoped_bundle: ContextVar[ChatToolRuntime | None] = ContextVar("chat_tool_runtime_scope", default=None)


def get_chat_tool_runtime(settings) -> ChatToolRuntime:
    scoped = _scoped_bundle.get()
    if scoped is not None:
        return scoped
    global _bundle
    with _lock:
        if _bundle is None:
            _bundle = create_chat_tool_runtime(settings, get_mcp_runtime(settings))
        return _bundle


def create_chat_tool_runtime(settings, runtime=None) -> ChatToolRuntime:
    runtime = runtime or create_mcp_runtime(settings)
    registry = ToolRegistry(
        schema_cache_seconds=float(getattr(settings, "chat_tools_schema_cache_seconds", 300.0))
    )
    registry.discover(
        runtime,
        "chat_readonly",
        per_tool_timeouts={"rag_search": float(getattr(settings, "rag_tool_call_timeout_seconds", 65.0))},
        cache_ttls={
            "rag_search": float(getattr(settings, "rag_cache_ttl_seconds", 300.0)),
            "get_current_weather": float(getattr(settings, "weather_cache_ttl_seconds", 120.0)),
        },
    )
    registry.register_tools("context_readonly", [{
        "name": "read_tool_evidence",
        "description": "分页读取已授权执行引用的原文；evidence_ids=[] 读取全部证据的第一页，nextCursor 非空时用 cursor 继续。不会重新检索。",
        "inputSchema": {"type": "object", "properties": {
            "execution_id": {"type": "string", "minLength": 1},
            "evidence_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
            "focus": {"type": "string", "maxLength": 240},
            "cursor": {"type": ["string", "null"]},
        }, "required": ["execution_id", "evidence_ids", "focus"], "additionalProperties": False},
    }], per_tool_timeouts={"read_tool_evidence": 5.0})
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    result_engine = create_engine(str(settings.database_url), pool_pre_ping=True)
    result_sessions = sessionmaker(bind=result_engine, autoflush=False, autocommit=False)
    redis_client = None
    if getattr(settings, "redis_memory_enabled", False):
        from redis import Redis
        redis_client = Redis.from_url(settings.redis_url,
            socket_timeout=settings.redis_socket_timeout_seconds,
            socket_connect_timeout=settings.redis_socket_timeout_seconds)
    result_store = ToolResultStore(session_factory=result_sessions, namespace="mindbridge", redis_client=redis_client,
                                  read_max_tokens=getattr(settings, "tool_evidence_read_max_tokens", 1536))
    index_signature = ""
    try:
        from app.models.entities import KnowledgeIndexRegistry
        with result_sessions() as db:
            registry_row = db.query(KnowledgeIndexRegistry).filter_by(
                logical_name=str(getattr(settings, "knowledge_vector_collection_base", ""))
            ).one_or_none()
            index_signature = str(getattr(registry_row, "active_signature", "") or "")
    except Exception:
        index_signature = ""
    dispatch_runtime = _CompositeRuntime(runtime, result_store)
    executor = ToolExecutor(
        registry=registry,
        runtime=dispatch_runtime,
        default_timeout=float(getattr(settings, "chat_tools_call_timeout_seconds", 15.0)),
        cache_max_items=int(getattr(settings, "chat_tools_cache_max_items", 1000)),
        circuit_failure_threshold=int(getattr(settings, "chat_tools_circuit_failure_threshold", 5)),
        circuit_recovery_seconds=float(getattr(settings, "chat_tools_circuit_recovery_seconds", 60.0)),
        index_version=index_signature,
        rag_pipeline_version="context-workitems-v7",
        result_store=result_store,
    )
    return ChatToolRuntime(registry, executor, runtime, result_engine)


@contextmanager
def isolated_chat_tool_runtime(settings) -> Iterator[ChatToolRuntime]:
    runtime = create_mcp_runtime(settings, strict_startup=True)
    bundle = create_chat_tool_runtime(settings, runtime)
    token = _scoped_bundle.set(bundle)
    try:
        yield bundle
    finally:
        _scoped_bundle.reset(token)
        bundle.close()
