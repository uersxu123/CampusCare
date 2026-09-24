from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from jsonschema import validators

from app.services.tool_models import AiToolDefinition


RISK_TOOL_NAMES = frozenset({
    "mindbridge_excel_report",
    "mindbridge_case_create",
    "mindbridge_alert_send",
})
SPECIALIST_AGENTS = frozenset({
    "AcademicPlanningAgent",
    "CampusAffairsAgent",
    "PsychologicalSupportAgent",
})


@dataclass(frozen=True)
class RegisteredTool:
    model_name: str
    server_alias: str
    original_name: str
    description: str
    input_schema: dict[str, Any]
    validator: Any
    per_tool_timeout: float | None = None
    cache_ttl: float = 0.0
    read_only: bool = True
    tool_version: str = "1"

    def model_definition(self) -> AiToolDefinition:
        return AiToolDefinition(self.model_name, self.description, self.input_schema)


def validate_tool_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
    if not isinstance(arguments, dict):
        raise ValueError("工具 arguments 必须是 object")
    validator_cls = validators.validator_for(schema)
    validator_cls.check_schema(schema)
    errors = sorted(validator_cls(schema).iter_errors(arguments), key=lambda item: list(item.path))
    if errors:
        raise ValueError(errors[0].message)


def tool_argument_errors(validator: Any, arguments: Any) -> list[dict[str, Any]]:
    if not isinstance(arguments, dict):
        return [{"path": "$", "constraint": "type", "expected": "object"}]
    errors = sorted(validator.iter_errors(arguments), key=lambda item: list(item.absolute_path))
    return [
        {
            "path": ".".join(str(part) for part in item.absolute_path) or "$",
            "constraint": str(item.validator or "schema"),
            "expected": _safe_expected(item.validator_value),
        }
        for item in errors[:8]
    ]


def _safe_expected(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [item for item in value[:8] if isinstance(item, (str, int, float, bool, type(None)))]
    return str(value)[:120]


class ToolRegistry:
    def __init__(self, *, schema_cache_seconds: float = 300.0):
        self.schema_cache_seconds = max(0.0, schema_cache_seconds)
        self._tools: dict[str, RegisteredTool] = {}
        self._server_expiry: dict[str, float] = {}
        self.errors: list[dict[str, str]] = []

    def register_tools(
        self,
        server_alias: str,
        tools: list[Any],
        *,
        per_tool_timeouts: dict[str, float] | None = None,
        cache_ttls: dict[str, float] | None = None,
    ) -> None:
        if not server_alias or not re.fullmatch(r"[a-z0-9_]+", server_alias):
            raise ValueError("server_alias 无效")
        if server_alias in {"risk", "mcp_tools"}:
            raise ValueError("普通对话 Registry 不能连接风险 MCP Server")
        timeouts = per_tool_timeouts or {}
        ttls = cache_ttls or {}
        for raw in tools:
            name = str(_value(raw, "name") or "").strip()
            description = str(_value(raw, "description") or "").strip()
            schema = _value(raw, "inputSchema") or _value(raw, "input_schema")
            if not name or name in RISK_TOOL_NAMES or not isinstance(schema, dict):
                self.errors.append({"tool": name, "code": "INVALID_TOOL_SCHEMA"})
                continue
            try:
                validator_cls = validators.validator_for(schema)
                validator_cls.check_schema(schema)
                validator = validator_cls(schema)
            except Exception:
                self.errors.append({"tool": name, "code": "INVALID_TOOL_SCHEMA"})
                continue
            model_name = f"{server_alias}__{_normalize_name(name)}"
            if model_name in self._tools:
                raise ValueError(f"工具别名冲突: {model_name}")
            self._tools[model_name] = RegisteredTool(
                model_name=model_name,
                server_alias=server_alias,
                original_name=name,
                description=description,
                input_schema=dict(schema),
                validator=validator,
                per_tool_timeout=timeouts.get(name),
                cache_ttl=float(ttls.get(name, 0.0)),
            )
        self._server_expiry[server_alias] = time.monotonic() + self.schema_cache_seconds

    def discover(self, runtime, server_alias: str, **kwargs) -> None:
        self.register_tools(server_alias, runtime.list_tools_sync(server_alias), **kwargs)

    def for_agent(self, agent_name: str) -> list[RegisteredTool]:
        if agent_name == "GeneralChatAgent":
            allowed = {"get_current_weather"}
        elif agent_name in SPECIALIST_AGENTS:
            allowed = {"rag_search", "read_tool_evidence"}
        elif agent_name == "ResponseAgent":
            allowed = {"read_tool_evidence"}
        else:
            allowed = set()
        return sorted(
            (tool for tool in self._tools.values() if tool.original_name in allowed),
            key=lambda item: item.model_name,
        )

    def definitions_for_agent(self, agent_name: str) -> list[AiToolDefinition]:
        return [tool.model_definition() for tool in self.for_agent(agent_name)]

    def resolve_for_agent(self, agent_name: str, model_name: str) -> RegisteredTool | None:
        return next((tool for tool in self.for_agent(agent_name) if tool.model_name == model_name), None)

    def all_tools(self) -> list[RegisteredTool]:
        return sorted(self._tools.values(), key=lambda item: item.model_name)


def _normalize_name(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_").lower()
    if not normalized:
        raise ValueError("工具名无法规范化")
    return normalized


def _value(raw: Any, name: str) -> Any:
    if isinstance(raw, dict):
        return raw.get(name)
    return getattr(raw, name, None)
