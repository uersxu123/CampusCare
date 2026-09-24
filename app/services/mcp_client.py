from __future__ import annotations

import asyncio
import os
import re
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from app.core.config import Settings
from app.core.enums import RiskLevel
from app.services.tool_executor import CircuitBreaker
from app.services.tool_registry import RISK_TOOL_NAMES, validate_tool_arguments


class McpToolError(RuntimeError):
    pass


class MindBridgeMcpToolClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._circuits = {
            name: CircuitBreaker(
                settings.risk_mcp_circuit_failure_threshold,
                settings.risk_mcp_circuit_recovery_seconds,
            )
            for name in RISK_TOOL_NAMES
        }

    async def handle_report(self, report_id: int, risk_level: str | None) -> list[str]:
        try:
            async with self._session() as session:
                schemas = await self._load_schemas(session)
                results = [
                    await self._call_tool(
                        session,
                        schemas,
                        "mindbridge_excel_report",
                        {"report_id": report_id, "idempotency_key": f"report:{report_id}:excel"},
                    ),
                ]
                case_id = None
                if risk_level in {RiskLevel.MEDIUM.value, RiskLevel.HIGH.value}:
                    case_result = await self._call_tool(
                        session,
                        schemas,
                        "mindbridge_case_create",
                        {"report_id": report_id, "idempotency_key": f"report:{report_id}:case"},
                    )
                    results.append(case_result)
                    case_id = self._extract_case_id(case_result)
                if risk_level == RiskLevel.HIGH.value:
                    if case_id is None:
                        raise McpToolError("高风险报告已创建，但无法解析 caseId，预警未发送")
                    results.append(await self._call_tool(
                        session,
                        schemas,
                        "mindbridge_alert_send",
                        {"case_id": case_id, "idempotency_key": f"report:{report_id}:alert"},
                    ))
                return results
        except McpToolError:
            raise
        except Exception as exc:
            raise McpToolError(f"MCP 工具调用异常：{type(exc).__name__}: {exc}") from exc

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[Any]:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise McpToolError("缺少 mcp 依赖，无法通过 MCP 调用 MindBridge 工具") from exc

        project_root = self.settings.project_root
        env = os.environ.copy()
        python_path = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(project_root) if not python_path else f"{project_root}{os.pathsep}{python_path}"

        server = StdioServerParameters(
            command=sys.executable,
            args=["-m", "app.mcp_tools.server"],
            env=env,
            cwd=str(project_root),
        )
        async with stdio_client(server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await asyncio.wait_for(
                    session.initialize(),
                    timeout=self.settings.risk_mcp_call_timeout_seconds,
                )
                yield session

    async def _load_schemas(self, session: Any) -> dict[str, dict[str, Any]]:
        try:
            listed = await asyncio.wait_for(
                session.list_tools(),
                timeout=self.settings.risk_mcp_call_timeout_seconds,
            )
        except TimeoutError as exc:
            raise McpToolError("风险 MCP tools/list 超时") from exc
        tools = getattr(listed, "tools", listed)
        schemas: dict[str, dict[str, Any]] = {}
        for tool in tools or []:
            name = str(getattr(tool, "name", ""))
            schema = getattr(tool, "inputSchema", getattr(tool, "input_schema", None))
            if name in RISK_TOOL_NAMES and isinstance(schema, dict):
                try:
                    validate_tool_arguments(schema, _minimal_valid_arguments(name))
                except ValueError as exc:
                    raise McpToolError(f"风险 MCP 工具 Schema 无效：{name}: {exc}") from exc
                schemas[name] = schema
        missing = sorted(RISK_TOOL_NAMES - schemas.keys())
        if missing:
            raise McpToolError(f"风险 MCP 缺少工具 Schema：{', '.join(missing)}")
        return schemas

    async def _call_tool(
        self,
        session: Any,
        schemas: dict[str, dict[str, Any]],
        name: str,
        arguments: dict[str, Any],
    ) -> str:
        if name not in RISK_TOOL_NAMES or name not in schemas:
            raise McpToolError(f"风险 MCP 工具不在独立 allowlist：{name}")
        try:
            validate_tool_arguments(schemas[name], arguments)
        except ValueError as exc:
            raise McpToolError(f"{name} 参数校验失败：{exc}") from exc
        circuit = self._circuits[name]
        if not circuit.allow_call():
            raise McpToolError(f"{name} 安全降级：CIRCUIT_OPEN")
        try:
            result = await asyncio.wait_for(
                session.call_tool(name, arguments=arguments),
                timeout=self.settings.risk_mcp_call_timeout_seconds,
            )
        except TimeoutError as exc:
            circuit.record_failure()
            raise McpToolError(f"{name} 安全降级：TIMEOUT") from exc
        except Exception as exc:
            circuit.record_failure()
            raise McpToolError(f"{name} 安全降级：MCP_UNAVAILABLE") from exc
        message = self._result_message(result)
        if getattr(result, "isError", False):
            circuit.release_probe()
            raise McpToolError(f"{name} 调用失败：{message}")
        circuit.record_success()
        return message

    def circuit_for(self, name: str) -> CircuitBreaker:
        return self._circuits[name]

    def _result_message(self, result: Any) -> str:
        parts = []
        for item in getattr(result, "content", []) or []:
            text = getattr(item, "text", None)
            parts.append(text if text is not None else str(item))
        if parts:
            return "\n".join(parts)
        structured = getattr(result, "structuredContent", None)
        return str(structured if structured is not None else result)

    def _extract_case_id(self, message: str) -> int | None:
        match = re.search(r"caseId=(\d+)", message)
        return int(match.group(1)) if match else None


def _minimal_valid_arguments(name: str) -> dict[str, Any]:
    key = "schema-validation"
    if name == "mindbridge_alert_send":
        return {"case_id": 1, "idempotency_key": key}
    return {"report_id": 1, "idempotency_key": key}
