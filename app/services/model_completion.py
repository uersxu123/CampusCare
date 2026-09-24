from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


PROVIDER_REQUEST_FAILED = "PROVIDER_REQUEST_FAILED"
PROVIDER_EOF = "PROVIDER_EOF"
PROVIDER_EMPTY_OUTPUT = "PROVIDER_EMPTY_OUTPUT"
STRUCTURED_OUTPUT_INVALID = "STRUCTURED_OUTPUT_INVALID"
MODEL_PROTOCOL_ERROR = "MODEL_PROTOCOL_ERROR"
TURN_BUDGET_EXCEEDED = "TURN_BUDGET_EXCEEDED"
RETRYABLE_ZERO_OUTPUT_ERRORS = frozenset({
    PROVIDER_REQUEST_FAILED,
    PROVIDER_EOF,
    PROVIDER_EMPTY_OUTPUT,
    MODEL_PROTOCOL_ERROR,
})


class ModelFinishReason(str, Enum):
    STOP = "STOP"
    TOOL_CALL = "TOOL_CALL"
    DIRECT_RESPONSE = "DIRECT_RESPONSE"
    LENGTH = "LENGTH"
    CONTENT_FILTER = "CONTENT_FILTER"
    PROVIDER_EOF = "PROVIDER_EOF"
    EMPTY_OUTPUT = "EMPTY_OUTPUT"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class ModelUsage:
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None


@dataclass(frozen=True)
class ModelCompletionMetadata:
    provider: str
    model: str
    finish_reason: ModelFinishReason
    semantic_finish_seen: bool
    transport_terminal_seen: bool
    terminal_signal: str
    provider_finish_reason: str
    configured_output_limit: int
    usage: ModelUsage = field(default_factory=ModelUsage)
    thinking_observed: bool = False
    duration_ms: int = 0


@dataclass(frozen=True)
class ModelCompletion:
    content: str
    metadata: ModelCompletionMetadata

    @property
    def verified_complete(self) -> bool:
        return (
            bool(self.content.strip())
            and self.metadata.semantic_finish_seen
            and self.metadata.finish_reason == ModelFinishReason.STOP
        )


@dataclass(frozen=True)
class ModelStreamEvent:
    kind: Literal["delta", "terminal"]
    text: str = ""
    metadata: ModelCompletionMetadata | None = None

    def __post_init__(self) -> None:
        if self.kind == "terminal" and self.metadata is None:
            raise ValueError("terminal 事件必须包含完成元数据")
        if self.kind == "delta" and self.metadata is not None:
            raise ValueError("delta 事件不能包含完成元数据")


class ModelProtocolError(RuntimeError):
    def __init__(self, message: str, metadata: ModelCompletionMetadata | None = None):
        super().__init__(message)
        self.metadata = metadata


class IncompleteGenerationError(RuntimeError):
    def __init__(self, message: str, metadata: ModelCompletionMetadata | None = None):
        super().__init__(message)
        self.metadata = metadata
