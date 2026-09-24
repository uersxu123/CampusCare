from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Protocol

from sqlalchemy.orm import Session

from app.agents.harness import AgentHarnessOutcome, MindBridgeAgentHarness
from app.core.config import Settings
from app.models.entities import ChatSession, ChatTurn, UserAccount
from app.schemas.dtos import ChatRequest
from app.services.chat_turns import ChatTurnService
from app.services.model_completion import (
    ModelCompletionMetadata,
    ModelFinishReason,
)
from app.services.retrieval_capture import capture_retrieval_candidates
from app.services.execution_control import ExecutionBudget, bind_execution_budget
from app.services.turn_metrics import TurnMetricsCollector


@dataclass(frozen=True)
class CompletionAttempt:
    content: str
    metadata: ModelCompletionMetadata
    error_code: str = ""


@dataclass(frozen=True)
class GenerationOutcome:
    content: str
    source: Literal["MODEL", "APPLICATION"]
    complete: bool
    completion_verified: bool
    finish_reason: ModelFinishReason
    attempts: tuple[CompletionAttempt, ...]
    continuation_count: int
    error_code: str = ""
    business_status: Literal["COMPLETED", "PARTIAL", "FAILED"] = "COMPLETED"
    upstream_error_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class TurnExecutionOutcome:
    harness: AgentHarnessOutcome
    generation: GenerationOutcome
    trace_id: int
    route: dict[str, Any]
    retrieved_candidates: tuple[Any, ...]
    usable_evidence: tuple[Any, ...]
    turn_metrics: dict[str, Any]
    tool_diagnostics: dict[str, Any]
    prompt_evidence: tuple[dict[str, Any], ...] = ()
    retrieval_observation: str = "NOT_OBSERVED"


class TurnExecutionObserver(Protocol):
    def on_route(self, payload: dict) -> None: ...

    def on_retrieval(self, candidates: list, usable: list) -> None: ...

    def on_generation(self, outcome: GenerationOutcome) -> None: ...


class NoopTurnExecutionObserver:
    def on_route(self, payload: dict) -> None:
        return None

    def on_retrieval(self, candidates: list, usable: list) -> None:
        return None

    def on_generation(self, outcome: GenerationOutcome) -> None:
        return None


ModelGenerationRunner = Callable[..., Awaitable[GenerationOutcome]]


class TurnExecutionService:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def execute(
        self,
        db: Session,
        user: UserAccount,
        session: ChatSession,
        turn: ChatTurn,
        collector: TurnMetricsCollector,
        observer: TurnExecutionObserver | None = None,
        *,
        model_generation: ModelGenerationRunner,
    ) -> TurnExecutionOutcome:
        observer = observer or NoopTurnExecutionObserver()
        observer_event = getattr(observer, "on_event", None)
        if observer_event is not None:
            observer_event("turn_execution", "STARTED")
        current_message = ChatTurnService(db, self.settings).get_user_message(turn)
        harness = MindBridgeAgentHarness(db, self.settings)
        budget = ExecutionBudget.start(float(getattr(self.settings, "turn_execution_deadline_seconds", 120.0)))
        with bind_execution_budget(budget):
            with capture_retrieval_candidates() as captured_candidates:
                harness_outcome = harness.run(
                    user,
                    session,
                    ChatRequest(
                        requestId=turn.request_id,
                        sessionId=session.public_id,
                        message=current_message.content,
                    ),
                    current_message,
                )
        if harness_outcome.user_message_id != turn.user_message_id:
            raise ValueError("Agent 运行结果与 ChatTurn 用户消息不一致")
        if harness_outcome.trace_id is None:
            raise ValueError("新生成任务缺少 planning Trace")
        turn.trace_id = harness_outcome.trace_id
        db.add(turn)
        db.commit()

        route = self._route_payload(harness_outcome)
        candidates = _distinct_candidates(captured_candidates)
        usable = list(harness_outcome.evidence_items)
        observer.on_route(route)
        observer.on_retrieval(candidates, usable)

        # V7.3: ResponseAgent is the single final-generation point inside Agent Runtime.
        # Keep the model_generation argument for one-release call-site compatibility, but never
        # invoke a second Response model after the runtime has finished.
        del model_generation
        direct_content = (harness_outcome.direct_response or "").strip()
        diagnostics = dict(getattr(harness_outcome, "response_generation_diagnostics", {}) or {})
        fallback_used = bool(diagnostics.get("fallbackUsed"))
        response_verified = bool(direct_content) and not fallback_used
        content = direct_content or "当前暂时无法生成完整回复，请稍后重试。"
        business_status = str(getattr(harness_outcome, "business_status", "COMPLETED") or "COMPLETED")
        upstream_codes = tuple(getattr(harness_outcome, "business_error_codes", ()) or ())
        response_error = ""
        if fallback_used:
            response_error = str(diagnostics.get("finishReason") or "AGENT_RESPONSE_FALLBACK")
        elif not direct_content:
            response_error = "AGENT_FINAL_RESPONSE_MISSING"
        failure_reason = ModelFinishReason.ERROR
        if not response_verified:
            try:
                reported_reason = ModelFinishReason(str(diagnostics.get("finishReason") or "ERROR"))
            except ValueError:
                reported_reason = ModelFinishReason.ERROR
            if reported_reason not in {ModelFinishReason.STOP, ModelFinishReason.DIRECT_RESPONSE, ModelFinishReason.TOOL_CALL}:
                failure_reason = reported_reason
            if failure_reason == ModelFinishReason.CONTENT_FILTER:
                content = ""
        engineering_codes = tuple(
            code for code in upstream_codes
            if code in {
                "DEADLINE_EXCEEDED",
                "INPUT_BUDGET_EXCEEDED",
                "MODEL_ERROR",
                "MODEL_INCOMPLETE",
                "MODEL_ROUND_BUDGET_EXCEEDED",
                "TOOL_RESULT_BUDGET_EXCEEDED",
            }
        )
        generation = GenerationOutcome(
            content=content,
            source="APPLICATION",
            complete=business_status == "COMPLETED" and response_verified,
            completion_verified=response_verified,
            finish_reason=ModelFinishReason.DIRECT_RESPONSE if response_verified else failure_reason,
            attempts=(),
            continuation_count=0,
            error_code=response_error or (engineering_codes[0] if engineering_codes else ""),
            business_status=business_status,
            upstream_error_codes=upstream_codes,
        )
        observer.on_generation(generation)
        if observer_event is not None:
            observer_event(
                "turn_execution",
                "COMPLETED" if generation.completion_verified else "FAILED",
                errorCode=generation.error_code,
            )
        return TurnExecutionOutcome(
            harness=harness_outcome,
            generation=generation,
            trace_id=harness_outcome.trace_id,
            route=route,
            retrieved_candidates=tuple(candidates),
            usable_evidence=tuple(usable),
            turn_metrics=collector.as_dict(),
            tool_diagnostics=dict(harness_outcome.tool_diagnostics),
            prompt_evidence=tuple(harness_outcome.response_evidence_items),
            retrieval_observation=captured_candidates.status,
        )

    @staticmethod
    def _route_payload(outcome: AgentHarnessOutcome) -> dict[str, Any]:
        plan = outcome.route_plan or {}
        return {
            "primaryIntent": plan.get("primaryIntent") or outcome.primary_intent.value,
            "intents": list(plan.get("intents") or [item.value for item in outcome.intents]),
            "riskLevel": outcome.risk_level or "LOW",
            "planId": plan.get("planId"),
            "workItems": [
                {
                    "workItemId": item.get("workItemId"),
                    "intent": item.get("intent"),
                    "dependsOn": list(item.get("dependsOn") or []),
                    "missingFields": [
                        argument.get("name") for argument in item.get("missingArguments", [])
                        if isinstance(argument, dict) and argument.get("name")
                    ],
                }
                for item in plan.get("workItems", [])
                if isinstance(item, dict)
            ],
            "confidence": plan.get("confidence"),
            "synthesisOrder": list(plan.get("synthesisOrder") or []),
            "planSource": outcome.route_diagnostics.get("planSource"),
            "segmentCount": outcome.route_diagnostics.get("acceptedSegmentCount", len(plan.get("workItems", []))),
            "contextRelation": outcome.route_diagnostics.get("contextRelation", "NEW_TOPIC"),
            "orderOnlyEdges": list(outcome.route_diagnostics.get("orderOnlyEdges") or []),
            "fallbackReason": outcome.route_diagnostics.get("fallbackReason", ""),
        }


def _distinct_candidates(candidates: list[Any]) -> list[Any]:
    rows = []
    seen = set()
    for item in candidates:
        identity = (item.get("contextId") or item.get("chunkId") or item.get("content")) if isinstance(item, dict) else getattr(item, "chunk_id", None) or (
            getattr(item, "canonical_key", None),
            getattr(item, "source_key", None),
            getattr(item, "content", None),
        )
        if identity in seen:
            continue
        seen.add(identity)
        rows.append(item)
    return rows
