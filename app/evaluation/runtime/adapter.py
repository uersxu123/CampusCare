from __future__ import annotations

import json
import uuid
from contextlib import nullcontext
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.evaluation.contracts import EndToEndCase, EvaluationRuntimeOutcome
from app.evaluation.runtime.action_resolution import (
    context_id_from_item,
    normalize_tool_name,
    resolve_evaluation_action,
)
from app.evaluation.runtime.capture import InMemoryTurnExecutionObserver
from app.evaluation.runtime.isolation import EvaluationIsolation
from app.models.entities import ChatMessage, ChatSession, ChatTurn
from app.schemas.dtos import ChatRequest
from app.services.chat import ChatService
from app.services.chat_turns import ChatTurnService
from app.services.chat_tool_runtime import isolated_chat_tool_runtime
from app.services.model_completion import ModelFinishReason
from app.services.turn_execution import TurnExecutionService
from app.services.turn_metrics import TurnMetricsCollector, bind_turn_metrics
from app.services.evidence_contract import capture_evidence_diagnostics


class EvaluationRuntimeAdapter:
    def __init__(
        self,
        app_settings: Settings,
        source_db: Session | None = None,
        *,
        require_hybrid_retrieval: bool = True,
        isolation_mode: str = "isolated",
        storage_admin_url: str = "",
    ):
        self.app_settings = app_settings
        self.source_db = source_db
        self.require_hybrid_retrieval = require_hybrid_retrieval
        self.isolation_mode = isolation_mode
        self.storage_admin_url = storage_admin_url

    def worker_spec(self) -> dict[str, Any]:
        return {
            "settings": self.app_settings.model_dump(mode="json"),
            "useSourceDatabase": self.source_db is not None,
            "requireHybridRetrieval": self.require_hybrid_retrieval,
            "isolationMode": self.isolation_mode,
        }

    async def run_case(self, case: EndToEndCase) -> list[EvaluationRuntimeOutcome]:
        outcomes: list[EvaluationRuntimeOutcome] = []
        with EvaluationIsolation(
            self.app_settings,
            self.source_db,
            require_hybrid_retrieval=self.require_hybrid_retrieval,
            isolation_mode=self.isolation_mode,
            storage_admin_url=self.storage_admin_url,
        ) as isolation:
            tool_scope = (
                isolated_chat_tool_runtime(isolation.settings)
                if isolation.settings.chat_tools_enabled
                else nullcontext()
            )
            with tool_scope:
                session = ChatSession(
                    public_id=str(uuid.uuid4()),
                    title=f"evaluation:{case.id}",
                    user_id=isolation.user.id,
                )
                isolation.db.add(session)
                isolation.db.commit()
                isolation.db.refresh(session)
                for turn_index, text in enumerate(case.turns):
                    outcome = await self._execute_turn(isolation, session, case.id, turn_index, text)
                    outcomes.append(outcome)
                    if outcome.error_code:
                        break
        return outcomes

    async def _execute_turn(
        self,
        isolation: EvaluationIsolation,
        session: ChatSession,
        case_id: str,
        turn_index: int,
        text: str,
    ) -> EvaluationRuntimeOutcome:
        request_id = str(uuid.uuid4())
        turn, _created = ChatTurnService(isolation.db, isolation.settings).create_or_get(
            isolation.user,
            request_id,
            session.public_id,
            text,
        )
        turn.status = "GENERATING"
        isolation.db.commit()
        collector = TurnMetricsCollector(request_id)
        observer = InMemoryTurnExecutionObserver()
        chat = ChatService(isolation.db, isolation.settings)
        error_code = None
        generation = None
        execution = None
        with bind_turn_metrics(collector), capture_evidence_diagnostics() as evidence_diagnostics:
            try:
                execution = await TurnExecutionService(isolation.settings).execute(
                    isolation.db,
                    isolation.user,
                    session,
                    turn,
                    collector,
                    observer,
                    model_generation=chat._run_model_generation,
                )
                generation = execution.generation
                error_code = generation.error_code or None
                turn.trace_id = execution.trace_id
                if generation.completion_verified:
                    collector.mark_turn_finished("COMPLETED")
                    metadata = chat._generation_metadata(generation, collector)
                    chat._finalize_completed_turn(isolation.db, turn, session, generation, metadata)
                else:
                    collector.mark_turn_finished("FAILED")
                    metadata = chat._generation_metadata(generation, collector)
                    chat._finalize_failed_turn(isolation.db, turn, session, generation, metadata)
                    error_code = generation.error_code
            except Exception as exc:
                collector.mark_turn_finished("FAILED")
                generation = observer.generation
                error_code = f"{type(exc).__name__}"
        response = generation.content if generation is not None else ""
        candidates = list(execution.retrieved_candidates) if execution else list(observer.candidates)
        usable = _usable_evidence_from_execution(execution, observer)
        tool_diagnostics = dict(execution.tool_diagnostics) if execution else {}
        resolved = resolve_evaluation_action(observer.route, execution.harness if execution else None)
        actual_tools = _actual_tools(tool_diagnostics)
        prompt_evidence = list(execution.prompt_evidence) if execution else []
        infra_codes = list(dict.fromkeys([
            *resolved.infra_error_codes,
            *[str(code) for code in tool_diagnostics.get("errorCodes", []) if code],
        ]))
        retrieval_diags = [
            item.get("toolSummary", {}).get("retrievalDiagnostics", {})
            for item in tool_diagnostics.get("workItems", [])
            if item.get("toolSummary", {}).get("retrievalDiagnostics")
        ]
        hybrid_degraded = not retrieval_diags or any(
            bool(item.get("vectorDegraded"))
            or bool(item.get("bm25Degraded"))
            or not _is_hybrid_mode(item.get("retrievalMode"))
            or not item.get("activeCollection")
            or not item.get("indexVersion")
            for item in retrieval_diags
        )
        if self.require_hybrid_retrieval and "rag_search" in actual_tools and hybrid_degraded:
            infra_codes.append("HYBRID_RETRIEVAL_REQUIRED")
            error_code = error_code or "HYBRID_RETRIEVAL_REQUIRED"
        if resolved.reason_codes == ("ACTION_UNRESOLVED",):
            error_code = error_code or "ACTION_UNRESOLVED"
        warnings = [
            "SUPPRESSED_IN_EVALUATION"
            if execution and getattr(execution.harness.tool_plan, "requires_tools", False)
            else ""
        ]
        warnings = [item for item in warnings if item]
        if any(not context_id_from_item(item) for item in (*candidates, *usable, *prompt_evidence)):
            warnings.append("CONTEXT_ID_MISSING")
        return EvaluationRuntimeOutcome(
            retrieval_observation=getattr(execution, "retrieval_observation", "NOT_OBSERVED"),
            specialist_results=list(getattr(execution.harness, "specialist_results", ()) or ()) if execution else [],
            evidence_diagnostics=evidence_diagnostics,
            case_id=case_id,
            turn_index=turn_index,
            response=response,
            route=dict(observer.route),
            risk_level=str(observer.route.get("riskLevel") or "LOW"),
            action=resolved.action,
            knowledge_requested="rag_search" in actual_tools,
            knowledge_used=bool(usable),
            retrieved_context_ids=[_context_id(item) for item in candidates],
            retrieved_contexts=[_context_content(item) for item in candidates],
            usable_context_ids=[_context_id(item) for item in usable],
            usable_contexts=[_context_content(item) for item in usable],
            trace_id=str(turn.trace_id or ""),
            turn_metrics=collector.as_dict(),
            warnings=warnings,
            error_code=error_code,
            tool_diagnostics=tool_diagnostics,
            work_item_outcomes=[
                {
                    "workItemId": item.work_item_id,
                    "intent": item.intent,
                    "status": item.status,
                    "reasonCode": item.reason_code,
                    "knowledgeRequested": item.knowledge_requested,
                    "usableContextIds": list(item.usable_context_ids),
                    "toolErrorCodes": list(item.tool_error_codes),
                }
                for item in resolved.work_items
            ],
            actual_tools=actual_tools,
            prompt_context_ids=[context_id_from_item(item) for item in prompt_evidence if context_id_from_item(item)],
            prompt_contexts=[_context_content(item) for item in prompt_evidence],
            prompt_contexts_observed=execution is not None,
            action_reason_codes=list(resolved.reason_codes),
            infra_error_codes=infra_codes,
            business_status=generation.business_status if generation is not None else "FAILED",
            upstream_error_codes=list(generation.upstream_error_codes) if generation is not None else [],
        )


def _context_id(item: Any) -> str:
    return context_id_from_item(item)


def _usable_evidence_from_execution(execution: Any, _observer: Any = None) -> list[Any]:
    if execution is None:
        return []
    return list(execution.usable_evidence)


def _actual_tools(tool_diagnostics: dict[str, Any]) -> list[str]:
    tools: list[str] = []
    for item in tool_diagnostics.get("workItems", []):
        for name in item.get("toolSummary", {}).get("usedTools", []):
            normalized = normalize_tool_name(name)
            if normalized and normalized not in tools:
                tools.append(normalized)
    return tools


def _context_content(item: Any) -> str:
    return str(item.get("content", "")) if isinstance(item, dict) else str(getattr(item, "content", ""))


def _is_hybrid_mode(value: Any) -> bool:
    mode = str(value or "").strip().lower()
    return mode == "hybrid" or mode.endswith("-hybrid")
