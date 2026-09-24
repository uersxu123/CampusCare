"""Single request token budgeting used by AgentLoop and context builders.

The estimator is deliberately conservative and deterministic when a provider
tokenizer is unavailable.  It counts message envelopes and tool schemas in
addition to message text, and never silently drops protected content.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.services.context_builder import estimate_tokens as estimate_text_tokens


def estimate_tokens(value: Any) -> int:
    if value is None:
        return 0
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return estimate_text_tokens(value, message_overhead=0)


def estimate_messages(messages: Iterable[Any], tools: Iterable[Any] = ()) -> int:
    total = 0
    for message in messages:
        if hasattr(message, "model_dump"):
            payload = message.model_dump(exclude_none=True)
        elif hasattr(message, "__dict__"):
            payload = {k: v for k, v in vars(message).items() if v is not None}
        else:
            payload = message
        total += estimate_tokens(payload) + 4
    for tool in tools:
        payload = tool.provider_payload() if hasattr(tool, "provider_payload") else tool
        total += estimate_tokens(payload) + 8
    return total


@dataclass(frozen=True)
class BudgetConfig:
    model_context_tokens: int = 32768
    input_max_tokens: int = 28672
    output_max_tokens: int = 1024
    safety_margin_tokens: int = 1024
    compress_trigger_tokens: int = 24000
    compress_target_tokens: int = 20000
    reread_reserve_tokens: int = 3072
    large_result_tokens: int = 8000

    @property
    def input_budget(self) -> int:
        return min(self.input_max_tokens, self.model_context_tokens - self.output_max_tokens - self.safety_margin_tokens)

    def validate(self) -> None:
        if not 0 < self.compress_target_tokens < self.compress_trigger_tokens < self.input_budget:
            raise ValueError("context compression thresholds must satisfy 0 < target < trigger < input budget")
        if self.reread_reserve_tokens < 0 or self.large_result_tokens <= 0:
            raise ValueError("invalid context budget reserve or large-result threshold")


@dataclass
class BudgetManifest:
    before_tokens: int
    after_tokens: int
    budget: int
    protected_tokens: int = 0
    compression_actions: list[dict[str, Any]] = field(default_factory=list)
    reread_events: list[dict[str, Any]] = field(default_factory=list)
    overflow_reason: str | None = None


def fit_budget(messages: list[Any], tools: list[Any], config: BudgetConfig, protected_roles: set[str] | None = None) -> tuple[bool, BudgetManifest]:
    config.validate()
    before = estimate_messages(messages, tools)
    protected_roles = protected_roles or {"system", "user"}
    protected = sum(estimate_tokens(getattr(m, "content", "")) + 4 for m in messages if getattr(m, "role", "") in protected_roles)
    manifest = BudgetManifest(before, before, config.input_budget, protected_tokens=protected)
    if before <= config.input_budget:
        return True, manifest
    # 兼容旧调用方的硬预算检查，不进行隐式截断。
    # 带 LLM 的生产压缩入口统一由 ContextCompactor.prepare 负责。
    manifest.overflow_reason = "PROTECTED_CONTENT_EXCEEDS_BUDGET" if protected >= config.input_budget else "INPUT_BUDGET_EXCEEDED"
    return False, manifest
