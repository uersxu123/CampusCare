from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.services.model_completion import ModelCompletionMetadata, ModelFinishReason


@dataclass(frozen=True)
class AiToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]

    def provider_payload(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


@dataclass(frozen=True)
class AiToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

    def message_payload(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass(frozen=True)
class AiToolCompletion:
    content: str
    tool_calls: tuple[AiToolCall, ...]
    metadata: ModelCompletionMetadata

    @property
    def verified_complete(self) -> bool:
        if self.tool_calls:
            return self.metadata.semantic_finish_seen and self.metadata.finish_reason == ModelFinishReason.TOOL_CALL
        return (
            bool(self.content.strip())
            and self.metadata.semantic_finish_seen
            and self.metadata.finish_reason == ModelFinishReason.STOP
        )


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    code: str
    data: Any = None
    cached: bool = False
    degraded: bool = False
    retryable: bool = False
    error: str = ""
    dispatched: bool = False
    validation_errors: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    telemetry: dict[str, Any] = field(default_factory=dict)
    execution_id: str = ""
    raw_hash: str = ""
    persisted: bool = False
    cached_from_execution_id: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "data": self.data,
            "cached": self.cached,
            "degraded": self.degraded,
            "retryable": self.retryable,
            "error": self.error,
            "dispatched": self.dispatched,
            "validationErrors": list(self.validation_errors),
            "executionId": self.execution_id,
            "rawHash": self.raw_hash,
            "persisted": self.persisted,
            "cachedFromExecutionId": self.cached_from_execution_id,
        }


@dataclass(frozen=True)
class AgentLoopResult:
    content: str
    tool_results: tuple[tuple[AiToolCall, ToolResult], ...] = field(default_factory=tuple)
    model_rounds: int = 0
    stop_reason: str = "COMPLETED"
    call_details: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    budget_stop_reason: str = ""
    finalize_used: bool = False
    visible_tool_evidence: tuple[dict[str, Any], ...] | None = None
    tool_result_views: tuple[dict[str, Any], ...] | None = None
    compression_events: tuple[dict[str, Any], ...] = ()


def parse_json_object(raw: str) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError(f"JSON 参数包含重复键: {key}")
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError("工具 arguments 必须是 JSON object")
    return value
