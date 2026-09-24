from __future__ import annotations

import uuid
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app.agents.autonomous import (
    AcademicPlanningAgent,
    AgentRuntimeServices,
    CampusAffairsAgent,
    CoordinatorAgent,
    GeneralChatAgent,
    PsychologicalSupportAgent,
    ResponseAgent,
    SafetyAgent,
    UnderstandingAgent,
)
from app.agents.coordinator import EventDrivenCoordinator
from app.agents.events import AgentArtifact, AgentEvent, AgentEventType, CollaborationBlackboard
from app.agents.registry import AgentRegistry
from app.agents.result import AgentRunResult, AgentStep
from app.agents.routing import RoutePlan
from app.core.config import Settings
from app.core.enums import IntentType, RiskLevel
from app.models.entities import ChatMessage, ChatSession, UserAccount
from app.schemas.dtos import AiMessage
from app.services.agent_models import AgentModelRegistry
from app.services.ai import AiClient
from app.services.chat_tool_runtime import get_chat_tool_runtime
from app.services.context_builder import ContextBuilder
from app.services.knowledge import KnowledgeService
from app.services.memory import RedisShortTermMemoryStore
from app.services.skills import SkillManager
from app.services.tool_result_store import bind_tool_result_scope


class EventDrivenAgentRuntimeService:
    framework_name = "event_driven_multi_agent"

    def __init__(self, db: Session, settings: Settings, *, context_builder: ContextBuilder | None = None):
        self.db = db
        self.settings = settings
        self.ai = AiClient(settings)
        self.knowledge = KnowledgeService(db, settings)
        self.memory = RedisShortTermMemoryStore(settings)
        self.context_builder = context_builder or ContextBuilder(db, settings, self.memory)
        self.model_registry = AgentModelRegistry(settings)
        self.skill_manager = SkillManager(settings=settings)
        self.tool_runtime = get_chat_tool_runtime(settings) if settings.chat_tools_enabled else None

    def run(
        self,
        user: UserAccount,
        session: ChatSession,
        original_input: str,
        model_input: str,
        current_message: ChatMessage,
        context_packet,
        clarification_state: dict | None = None,
        resume_context: dict | None = None,
        resolved_arguments: dict | None = None,
    ) -> AgentRunResult:
        packet = context_packet
        if packet is None:
            raise ValueError("EventDriven Runtime 必须接收 Harness 已装配的 context_packet")
        services = AgentRuntimeServices(
            db=self.db,
            settings=self.settings,
            user=user,
            session=session,
            ai=self.ai,
            model_registry=self.model_registry,
            knowledge=self.knowledge,
            context_builder=self.context_builder,
            context_packet=packet,
            tool_runtime=self.tool_runtime,
            skill_manager=self.skill_manager,
        )
        coordinator_agent = CoordinatorAgent(services)
        agents = [
            UnderstandingAgent(services),
            SafetyAgent(services),
            GeneralChatAgent(services),
            AcademicPlanningAgent(services),
            CampusAffairsAgent(services),
            PsychologicalSupportAgent(services),
            ResponseAgent(services),
        ]
        board = CollaborationBlackboard(
            turn_id=uuid.uuid4().hex,
            user_id=user.id,
            session_id=session.public_id,
            user_input=original_input,
            model_input=model_input,
        )
        if resume_context and resolved_arguments is not None:
            board = self._seed_resumed_plan(board, resume_context)
        board = board.add_artifact(AgentArtifact(
            id=f"ContextBuilder:turn_memory:{uuid.uuid4().hex[:10]}",
            owner="ContextBuilder",
            kind="turn_memory",
            payload=packet.as_payload(),
            confidence=0.7 if packet.manifest.degraded_sources else 1.0,
        )).append_event(AgentEvent(
            type=AgentEventType.TURN_STARTED,
            actor=coordinator_agent.name,
            message="user turn published to shared task board",
        ))
        registry = AgentRegistry(agents)
        with bind_tool_result_scope(
            user_id=str(user.id),
            session_id=str(session.public_id),
            turn_id=str(board.turn_id),
        ):
            final_board = EventDrivenCoordinator(registry, coordinator_agent, self.settings).run(board)
        result = self._to_result(final_board)
        # 在全部模型调用结束后才合并，避免诊断信息改变 Response/Safety 输入。
        for item in result.tool_diagnostics.get("workItems", []):
            calls = services.tool_call_details.get(str(item.get("workItemId") or ""), [])
            item["toolSummary"]["calls"] = list(calls)
        return result

    @staticmethod
    def _seed_resumed_plan(
        board: CollaborationBlackboard,
        resume_context: dict,
    ) -> CollaborationBlackboard:
        from app.services.clarification_models import ClarificationResumeContext
        resolved = ClarificationResumeContext.from_payload(resume_context)
        if resolved.expected_fields:
            raise ValueError("仅允许恢复已解决的 ClarificationResumeContext")
        plan = resolved.route_plan
        board = board.add_artifact(AgentArtifact(
            id=f"ClarificationService:route_plan:{uuid.uuid4().hex[:10]}",
            owner="ClarificationService",
            kind="route_plan",
            payload=plan.as_payload(),
            confidence=1.0,
            metadata={"planId": plan.plan_id},
        ))
        return board.append_event(AgentEvent(
            type=AgentEventType.TURN_RESUMED,
            actor="ClarificationService",
            message="resumed original route plan after validated input",
            metadata={"planId": plan.plan_id, "resumed": True},
        ))

    def _to_result(self, board: CollaborationBlackboard) -> AgentRunResult:
        plan_artifact = board.latest_artifact("route_plan")
        if plan_artifact is None:
            plan_payload: dict[str, Any] = {}
            plan_diagnostics: dict[str, Any] = {}
            primary_intent = IntentType.RISK if self._select_risk(board) == RiskLevel.HIGH else IntentType.CHAT
            intents = (primary_intent,)
        else:
            plan = RoutePlan.from_payload(plan_artifact.payload)
            plan_payload = plan.as_payload()
            plan_diagnostics = dict(plan_artifact.metadata)
            primary_intent = plan.primary_intent
            intents = plan.intents
        risk = self._select_risk(board)
        if risk == RiskLevel.HIGH:
            primary_intent = IntentType.RISK
            intents = (IntentType.RISK,)

        memory = board.latest_artifact("turn_memory")
        risk_artifact = board.latest_artifact("risk")
        # Only an explicitly FINAL_ACCEPTED response may leave Agent Runtime.
        # A merely generated response_proposal can still be awaiting/rejected by SafetyAgent.
        accepted = board.accepted_artifact()
        clarification = board.latest_artifact("clarification_request")
        base_manifest = memory.payload.get("manifest", {}) if memory else {}
        context_manifest = accepted.payload.get("contextManifest", base_manifest) if accepted else base_manifest
        memory_brief = "无相关历史记忆。"
        if memory:
            goal = memory.payload.get("structured_summary", {}).get("current_goal")
            memory_brief = str(goal.get("text") if isinstance(goal, dict) else goal or memory_brief)

        specialist_results = board.ordered_specialist_results(plan_payload) if plan_payload else []
        evidence_items = _dedupe_evidence(specialist_results)
        tool_diagnostics = _tool_diagnostics(specialist_results)
        response_messages = list(accepted.payload.get("messages") or []) if accepted else []
        direct_response = str(accepted.payload.get("directResponse") or "") if accepted else ""
        response_evidence_items = [
            dict(item) for item in (accepted.payload.get("promptEvidence") or [])
            if isinstance(item, dict)
        ] if accepted else []
        generation_diagnostics = dict(accepted.payload.get("generationDiagnostics") or {}) if accepted else {}
        response_tool_summary = dict(generation_diagnostics.get("responseToolSummary") or {})
        if response_tool_summary:
            response_errors = [str(code) for code in response_tool_summary.get("errorCodes", []) if code]
            response_calls = int(response_tool_summary.get("callCount") or 0)
            tool_diagnostics["workItems"].append({
                "workItemId": "__response__",
                "agentName": "ResponseAgent",
                "status": "COMPLETED" if not response_errors else "PARTIAL",
                "reasonCode": "RESPONSE_CONTROLLED_REREAD",
                "toolSummary": {
                    "usedTools": list(response_tool_summary.get("usedTools", [])),
                    "callCount": response_calls,
                    "degraded": bool(response_tool_summary.get("degraded")),
                    "errorCodes": response_errors,
                    "retrievalDiagnostics": {},
                    "calls": list(generation_diagnostics.get("calls") or []),
                },
            })
            tool_diagnostics["totalCallCount"] = int(tool_diagnostics.get("totalCallCount") or 0) + response_calls
            tool_diagnostics["degraded"] = bool(tool_diagnostics.get("degraded")) or bool(response_tool_summary.get("degraded"))
            tool_diagnostics["errorCodes"] = list(dict.fromkeys([
                *tool_diagnostics.get("errorCodes", []), *response_errors,
            ]))
        business_status, business_error_codes = _business_outcome(
            specialist_results,
            has_terminal_response=bool(accepted or clarification),
            final_states=list(accepted.payload.get("finalWorkItemStates") or []) if accepted else [],
        )
        if not response_messages and not direct_response and clarification is None:
            response_messages = _fallback_messages(primary_intent, risk, board.model_input)

        return AgentRunResult(
            primary_intent=primary_intent,
            intents=intents,
            risk_level=risk,
            assessment=risk_artifact.payload.get("assessment") if risk_artifact else None,
            response_messages=response_messages,
            steps=self._events_to_steps(board),
            memory_brief=memory_brief,
            route_plan=plan_payload,
            specialist_results=specialist_results,
            evidence_items=evidence_items,
            tool_diagnostics=tool_diagnostics,
            route_diagnostics=plan_diagnostics,
            collaboration_events=list(board.events),
            collaboration_tasks=list(board.tasks.values()),
            collaboration_artifacts=list(board.artifacts),
            clarification_request=dict(clarification.payload) if clarification else None,
            context_manifest=dict(context_manifest or {}),
            direct_response=direct_response,
            response_evidence_items=response_evidence_items,
            response_generation_diagnostics=generation_diagnostics,
            business_status=business_status,
            business_error_codes=business_error_codes,
        )

    @staticmethod
    def _select_risk(board: CollaborationBlackboard) -> RiskLevel:
        order = {RiskLevel.LOW: 1, RiskLevel.MEDIUM: 2, RiskLevel.HIGH: 3}
        highest = RiskLevel.LOW
        for artifact in board.artifacts_by_kind("risk"):
            try:
                risk = RiskLevel(str(artifact.payload.get("risk", RiskLevel.LOW.value)).upper())
            except ValueError:
                risk = RiskLevel.LOW
            if order[risk] > order[highest]:
                highest = risk
        if any(event.type == AgentEventType.SAFETY_OVERRIDE for event in board.events):
            return RiskLevel.HIGH
        return highest

    @staticmethod
    def _events_to_steps(board: CollaborationBlackboard) -> list[AgentStep]:
        steps = []
        for index, event in enumerate(board.events, start=1):
            detail = event.message or _compact_json(event.metadata)
            if event.artifact_id:
                detail = f"{detail}; artifact={event.artifact_id}" if detail else f"artifact={event.artifact_id}"
            steps.append(AgentStep(index, event.actor, event.type.value, detail))
        return steps


def _dedupe_evidence(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for result in results:
        for item in result.get("evidenceItems", []):
            evidence_id = str(item.get("evidenceId") or "")
            if evidence_id and evidence_id not in seen:
                seen.add(evidence_id)
                items.append(dict(item))
    return items


def _business_outcome(
    results: list[dict[str, Any]],
    *,
    has_terminal_response: bool,
    final_states: list[dict[str, Any]] | None = None,
) -> tuple[str, tuple[str, ...]]:
    statuses = [str(item.get("status") or "FAILED") for item in results]
    final_by_id = {item.get("workItemId"): item for item in final_states or []}
    for index, item in enumerate(results):
        final = final_by_id.get(item.get("workItemId"), {})
        answer_status = final.get("answerStatus", item.get("answerStatus"))
        if item.get("schemaVersion") == 3 and statuses[index] != "FAILED":
            if answer_status in {"PARTIAL", "NONE", None}:
                statuses[index] = "PARTIAL"
            elif final.get("updatedByResponse") and not item.get("toolSummary", {}).get("errorCodes"):
                statuses[index] = "COMPLETED"
    error_codes = tuple(dict.fromkeys(
        str(item.get("reasonCode") or "AGENT_FAILED")
        for item in results
        if str(item.get("status") or "FAILED") != "COMPLETED"
    ))
    if not has_terminal_response:
        return "FAILED", error_codes or ("FINAL_RESPONSE_MISSING",)
    if not statuses or all(status == "COMPLETED" for status in statuses):
        return "COMPLETED", error_codes
    if all(status == "FAILED" for status in statuses):
        return "FAILED", error_codes
    return "PARTIAL", error_codes


def _tool_diagnostics(results: list[dict[str, Any]]) -> dict[str, Any]:
    work_items = []
    errors: list[str] = []
    total_calls = 0
    degraded = False
    for result in results:
        summary = dict(result.get("toolSummary") or {})
        item_errors = [str(code) for code in summary.get("errorCodes", []) if code]
        errors.extend(item_errors)
        total_calls += int(summary.get("callCount") or 0)
        degraded = degraded or bool(summary.get("degraded"))
        work_items.append({
            "workItemId": result.get("workItemId"),
            "agentName": result.get("agentName"),
            "status": result.get("status"),
            "reasonCode": result.get("reasonCode"),
            "toolSummary": {
                "usedTools": list(summary.get("usedTools", [])),
                "callCount": int(summary.get("callCount") or 0),
                "degraded": bool(summary.get("degraded")),
                "errorCodes": item_errors,
                "retrievalDiagnostics": dict(summary.get("retrievalDiagnostics") or {}),
            },
        })
    return {
        "workItems": work_items,
        "totalCallCount": total_calls,
        "degraded": degraded,
        "errorCodes": list(dict.fromkeys(errors)),
    }


def _fallback_messages(intent: IntentType, risk: RiskLevel, model_input: str) -> list[AiMessage]:
    if risk == RiskLevel.HIGH or intent == IntentType.RISK:
        system = "优先确认当前安全，并建议立即联系身边可信任的人或当地紧急支持。"
    else:
        system = "根据当前 route_plan 和 specialist_result 回答；不要编造工具结果或证据。"
    return [AiMessage(role="system", content=system), AiMessage(role="user", content=model_input)]


def _compact_json(value: Any) -> str:
    jsonable = _to_jsonable(value)
    return str(jsonable)[:240] if jsonable else ""


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if hasattr(value, "model_dump"):
        return _to_jsonable(value.model_dump())
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    return value
