from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import uuid
from concurrent.futures import Future
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@dataclass(frozen=True)
class McpServerConfig:
    alias: str
    command: str
    args: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.alias in {"risk", "mcp_tools"} or "app.mcp_tools.server" in " ".join(self.args):
            raise ValueError("普通对话 McpRuntime 不能配置风险工具 Server")


@dataclass
class _Request:
    operation: str
    future: Future
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    timeout: float = 5.0
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    tool_call_id: str = ""
    deadline_monotonic: float = 0.0
    server_generation: int = 0
    attempt: int = 0
    cancel_requested: bool = False
    dispatched: bool = False
    state: str = "QUEUED"
    metadata: dict[str, Any] = field(default_factory=dict)

    def remaining(self) -> float:
        return max(0.0, self.deadline_monotonic - time.monotonic())

    def finish(self, state: str, *, result: Any = None, error: BaseException | None = None) -> None:
        if self.future.done():
            return
        self.state = state
        if error is not None:
            self.future.set_exception(error)
        else:
            self.future.set_result(result)


@dataclass
class _Worker:
    queue: asyncio.Queue[_Request]
    ready: Future
    task: asyncio.Task
    generation: int


class McpRuntime:
    def __init__(
        self,
        configs: list[McpServerConfig],
        connect_timeout: float = 5.0,
        startup_timeout: float = 30.0,
        cleanup_timeout: float = 5.0,
    ):
        self.configs = {config.alias: config for config in configs}
        self.connect_timeout = max(0.1, connect_timeout)
        self.startup_timeout = max(self.connect_timeout, startup_timeout)
        self.cleanup_timeout = max(0.1, cleanup_timeout)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._workers: dict[str, _Worker] = {}
        self._generations: dict[str, int] = {}
        self._lock = threading.RLock()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            ready = threading.Event()

            def run() -> None:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
                ready.set()
                self._loop.run_forever()

            self._thread = threading.Thread(target=run, name="mindbridge-mcp", daemon=True)
            self._thread.start()
            ready.wait(timeout=self.startup_timeout)
            if self._loop is None:
                raise RuntimeError("MCP 后台事件循环启动失败")

    def list_tools_sync(self, server_alias: str):
        timeout = self.startup_timeout
        result = self._request(server_alias, _Request(
            "list", Future(), timeout=timeout, deadline_monotonic=time.monotonic() + timeout,
        ))
        return list(result.tools)

    def call_tool_sync(
        self,
        server_alias: str,
        tool_name: str,
        arguments: dict,
        timeout: float,
        *,
        request_id: str = "",
        tool_call_id: str = "",
        metadata: dict[str, Any] | None = None,
    ):
        timeout = max(0.01, timeout)
        return self._request(
            server_alias,
            _Request(
                "call",
                Future(),
                tool_name=tool_name,
                arguments=arguments,
                timeout=timeout,
                request_id=request_id or str(uuid.uuid4()),
                tool_call_id=tool_call_id,
                deadline_monotonic=time.monotonic() + timeout,
                metadata=dict(metadata or {}),
            ),
        )

    def shutdown(self) -> None:
        with self._lock:
            loop = self._loop
            if loop is None:
                return
            workers = list(self._workers.values())
            for worker in workers:
                request = _Request("stop", Future())
                loop.call_soon_threadsafe(worker.queue.put_nowait, request)
                try:
                    request.future.result(timeout=self.cleanup_timeout)
                except Exception:
                    pass
            self._workers.clear()
            loop.call_soon_threadsafe(loop.stop)
            if self._thread:
                self._thread.join(timeout=self.connect_timeout)
            self._loop = None
            self._thread = None

    def _request(self, alias: str, request: _Request):
        worker = self._ensure_worker(alias)
        assert self._loop is not None
        self._loop.call_soon_threadsafe(worker.queue.put_nowait, request)
        try:
            return request.future.result(timeout=max(0.1, request.timeout + self.cleanup_timeout))
        except TimeoutError:
            request.cancel_requested = True
            request.finish("TIMED_OUT", error=TimeoutError("MCP 请求超过截止时间"))
            self._abort_worker(alias, worker)
            raise

    def _abort_worker(self, alias: str, worker: _Worker) -> None:
        with self._lock:
            if self._workers.get(alias) is worker:
                self._workers.pop(alias, None)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(worker.task.cancel)

    def _ensure_worker(self, alias: str) -> _Worker:
        self.start()
        with self._lock:
            existing = self._workers.get(alias)
            if existing is not None:
                existing.ready.result(timeout=self.startup_timeout)
                return existing
            config = self.configs.get(alias)
            if config is None:
                raise KeyError(f"未知 MCP Server: {alias}")
            created: Future = Future()

            def create() -> None:
                assert self._loop is not None
                queue: asyncio.Queue[_Request] = asyncio.Queue()
                ready: Future = Future()
                generation = self._generations.get(alias, 0) + 1
                self._generations[alias] = generation
                task = self._loop.create_task(self._worker(config, queue, ready, generation))
                created.set_result(_Worker(queue, ready, task, generation))

            assert self._loop is not None
            self._loop.call_soon_threadsafe(create)
            worker = created.result(timeout=self.startup_timeout)
            self._workers[alias] = worker
        worker.ready.result(timeout=self.startup_timeout)
        return worker

    async def _worker(
        self,
        config: McpServerConfig,
        queue: asyncio.Queue[_Request],
        ready: Future,
        generation: int,
    ) -> None:
        stack: AsyncExitStack | None = None
        session: ClientSession | None = None

        async def connect() -> ClientSession:
            nonlocal stack, session
            stack = AsyncExitStack()
            try:
                read_stream, write_stream = await stack.enter_async_context(stdio_client(StdioServerParameters(
                    command=config.command,
                    args=list(config.args),
                    env=config.env or None,
                )))
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                await asyncio.wait_for(session.initialize(), timeout=self.startup_timeout)
                return session
            except Exception:
                await stack.aclose()
                stack = None
                session = None
                raise

        async def disconnect() -> None:
            nonlocal stack, session
            if stack is not None:
                await stack.aclose()
            stack = None
            session = None

        try:
            await connect()
            ready.set_result(True)
            while True:
                request = await queue.get()
                if request.operation == "stop":
                    await disconnect()
                    request.finish("SUCCEEDED", result=True)
                    return
                if request.cancel_requested or request.remaining() <= 0:
                    request.finish("TIMED_OUT", error=TimeoutError("MCP 请求在队列中超过截止时间"))
                    continue
                try:
                    if session is None:
                        await asyncio.wait_for(connect(), timeout=min(self.startup_timeout, request.remaining()))
                    assert session is not None
                    request.state = "DISPATCHED"
                    request.dispatched = True
                    request.attempt = 1
                    request.server_generation = generation
                    remaining = max(0.01, request.remaining())
                    if request.operation == "list":
                        result = await asyncio.wait_for(session.list_tools(), timeout=remaining)
                    else:
                        meta = {
                            **request.metadata,
                            "requestId": request.request_id,
                            "toolCallId": request.tool_call_id,
                            "attempt": request.attempt,
                            "serverGeneration": generation,
                            "remainingMs": max(0, int(remaining * 1000)),
                        }
                        result = await asyncio.wait_for(
                            session.call_tool(
                                request.tool_name,
                                request.arguments,
                                read_timeout_seconds=timedelta(seconds=remaining),
                                meta=meta,
                            ),
                            timeout=remaining,
                        )
                    request.finish("SUCCEEDED", result=result)
                except asyncio.CancelledError:
                    request.cancel_requested = True
                    request.finish("CANCELLED", error=TimeoutError("MCP 服务已取消并回收"))
                    raise
                except asyncio.TimeoutError as exc:
                    request.finish("TIMED_OUT", error=TimeoutError("MCP 工具调用超时"))
                    await disconnect()
                except Exception as exc:
                    request.finish("FAILED", error=exc)
                    if request.operation == "call":
                        await disconnect()
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
        finally:
            if not ready.done():
                ready.set_exception(RuntimeError("MCP 服务在就绪前退出"))
            if stack is not None:
                try:
                    await disconnect()
                except BaseException:
                    pass


_runtime: McpRuntime | None = None
_runtime_lock = threading.Lock()


def get_mcp_runtime(settings=None) -> McpRuntime:
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = create_mcp_runtime(settings)
        return _runtime


def create_mcp_runtime(settings=None, *, strict_startup: bool = False) -> McpRuntime:
    command = str(getattr(settings, "chat_tools_stdio_command", "") or sys.executable)
    configured = str(getattr(settings, "chat_tools_stdio_args", "") or "-m,app.chat_tools.server")
    args = tuple(item.strip() for item in configured.split(",") if item.strip())
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"DATABASE_URL", "PYTHONPATH"}
        or key.startswith(("CHAT_TOOLS_", "CHROMA_", "KNOWLEDGE_", "RAG_", "WEATHER_", "OLLAMA_"))
    }
    if settings is not None:
        env["DATABASE_URL"] = str(getattr(settings, "database_url", env.get("DATABASE_URL", "")))
        env["CHROMA_PERSIST_DIR"] = str(getattr(settings, "chroma_persist_dir", "data/chroma"))
    if strict_startup:
        env["CHAT_TOOLS_STRICT_STARTUP"] = "true"
    return McpRuntime(
        [McpServerConfig("chat_readonly", command, args, env=env)],
        connect_timeout=float(getattr(settings, "chat_tools_connect_timeout_seconds", 5.0)),
        startup_timeout=float(getattr(settings, "chat_tools_startup_timeout_seconds", 30.0)),
        cleanup_timeout=float(getattr(settings, "chat_tools_cleanup_timeout_seconds", 5.0)),
    )


def shutdown_mcp_runtime() -> None:
    global _runtime
    with _runtime_lock:
        if _runtime is not None:
            _runtime.shutdown()
            _runtime = None
