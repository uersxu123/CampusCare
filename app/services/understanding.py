from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings
from app.services.ai import AiClient, StructuredCompletionError, StructuredCompletionOptions
from app.services.intent_prompts import UNDERSTANDING_SCHEMA_NAME, build_intent_prompt
from app.services.route_planning import SemanticPlannerTimeout, SemanticPlannerUnavailable, SemanticStructuredOutputInvalid
from app.services.routing_v5 import PlanningResultV5, PlanningResultV6


@dataclass(frozen=True)
class UnderstandingInvocationResult:
    decision: PlanningResultV5 | PlanningResultV6
    provider_attempt_count: int
    latency_ms: int

    def __post_init__(self) -> None:
        if not 0 <= self.provider_attempt_count <= 2 or self.latency_ms < 0:
            raise ValueError("Understanding invocation metadata 无效")


class UnderstandingService:
    def __init__(self, client: AiClient, settings: Settings):
        self.client = client
        self.settings = settings

    def classify(self, planning_input: str, memory_context: dict[str, Any]) -> UnderstandingInvocationResult:
        started = time.monotonic()
        try:
            completion = self.client.complete_structured(
                build_intent_prompt(memory_context, planning_input),
                response_model=PlanningResultV6,
                schema_name=UNDERSTANDING_SCHEMA_NAME,
                options=StructuredCompletionOptions(
                    temperature=float(self.settings.agent_model_understanding_temperature),
                    max_tokens=int(self.settings.agent_model_understanding_max_tokens),
                    repair_attempts=0,
                    timeout_seconds=float(self.settings.agent_loop_deadline_seconds),
                ),
                purpose="understanding.route_planner_v6",
            )
        except StructuredCompletionError as exc:
            latency = max(0, int((time.monotonic() - started) * 1000))
            causes = list(_causes(exc))
            if "TIMEOUT" in exc.code.upper() or any(isinstance(item, (TimeoutError, httpx.TimeoutException)) for item in causes):
                raise SemanticPlannerTimeout("understanding model timed out", 1, latency) from exc
            if any(isinstance(item, httpx.HTTPError) for item in causes):
                raise SemanticPlannerUnavailable("understanding provider unavailable", 1, latency) from exc
            code = exc.code.upper()
            if code in {"MODEL_UNAVAILABLE", "STRUCTURED_OUTPUT_UNSUPPORTED"}:
                # 本地配置/能力拒绝没有派发请求；HTTP 拒绝已由上面的 cause 分支计数。
                raise SemanticPlannerUnavailable("understanding model unavailable", 0, latency) from exc
            if code in {"PROVIDER_UNAVAILABLE", "PROVIDER_REQUEST_FAILED"}:
                raise SemanticPlannerUnavailable("understanding model unavailable", 1, latency) from exc
            if code in {"STRUCTURED_OUTPUT_INVALID", "PROVIDER_EOF", "PROVIDER_EMPTY_OUTPUT", "TURN_BUDGET_EXCEEDED", "MODEL_PROTOCOL_ERROR"}:
                raise SemanticStructuredOutputInvalid("understanding structured output invalid", 1, latency) from exc
            raise
        except TimeoutError as exc:
            latency = max(0, int((time.monotonic() - started) * 1000))
            raise SemanticPlannerTimeout("understanding model timed out", 1, latency) from exc
        latency = max(0, int((time.monotonic() - started) * 1000))
        return UnderstandingInvocationResult(completion.value, 1, latency)


def _causes(exc: BaseException):
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__
