from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.agents.factory import create_agent_runtime
from app.agents.result import AgentStep
from app.core.config import Settings
from app.core.enums import IntentType, MessageRole, RiskLevel
from app.models.entities import ChatMessage, ChatSession, PsychologicalReport, UserAccount
from app.schemas.dtos import AiMessage, ChatRequest
from app.services.assessment import PsychologyAssessment
from app.services.ai import has_high_risk_signal
from app.services.clarifications import ClarificationService
from app.services.clarification_models import ClarificationStatus
from app.services.mcp_client import MindBridgeMcpToolClient
from app.services.memory import RedisShortTermMemoryStore
from app.services.context_builder import ContextBuilder
from app.services.tool_queue import ToolQueueService
from app.services.user_memory import UserMemoryService
from app.services.trace import AgentTraceService


logger = logging.getLogger(__name__)


@dataclass
class AgentToolPlan:
    report_id: int | None
    risk_level: str | None

    @property
    def requires_tools(self) -> bool:
        return self.report_id is not None


@dataclass
class AgentHarnessOutcome:
    session: ChatSession
    original_input: str
    model_input: str
    primary_intent: IntentType
    intents: tuple[IntentType, ...]
    risk_level: str | None
    assessment: PsychologyAssessment | None
    response_messages: list[AiMessage]
    agent_steps: list[AgentStep]
    route_plan: dict
    route_diagnostics: dict
    specialist_results: list[dict]
    evidence_items: list[dict]
    tool_diagnostics: dict
    report_id: int | None
    tool_plan: AgentToolPlan
    trace_id: int | None
    clarification_request: dict | None = None
    direct_response: str = ""
    user_message_id: int | None = None
    context_manifest: dict = field(default_factory=dict)
    response_evidence_items: list[dict] = field(default_factory=list)
    response_generation_diagnostics: dict = field(default_factory=dict)
    business_status: str = "COMPLETED"
    business_error_codes: tuple[str, ...] = ()


class MindBridgeAgentHarness:
    """Runtime harness for one CampusCare agent turn.

    The harness owns business orchestration around the agent runtime. HTTP/SSE
    code can stay thin while this class manages input preparation, persistence,
    risk report creation, tool planning, and trace data.
    """

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.memory = RedisShortTermMemoryStore(settings)
        self.context_builder = ContextBuilder(db, settings, self.memory)

    def run(
        self,
        user: UserAccount,
        session: ChatSession,
        request: ChatRequest,
        current_message: ChatMessage,
    ) -> AgentHarnessOutcome:
        original_input = request.message.strip()
        self._validate_current_message(user, session, request, current_message, original_input)
        model_input = original_input
        clarification_service = ClarificationService(self.db, self.settings)
        clarification_state = None
        resume_context = None
        resolved_arguments = None
        runtime_input = original_input
        current_high_risk = has_high_risk_signal(original_input)
        resolution = clarification_service.resume_or_bypass(
            user=user,
            session=session,
            text=original_input,
            high_risk=current_high_risk,
        )
        trace_original_input = original_input
        trace_sanitized_input = original_input
        if resolution.handled and resolution.question:
            UserMemoryService(self.db, self.settings).remember_explicit(user.id, original_input, current_message.id)
            route_plan = _route_plan_from_pending(resolution.pending)
            primary_intent, intents = _intent_summary(route_plan)
            trace = AgentTraceService(self.db, self.settings).create_minimal_trace(
                user=user,
                session=session,
                original_input=trace_original_input,
                sanitized_input=trace_sanitized_input,
                intent=primary_intent.value,
            )
            return AgentHarnessOutcome(
                session=session,
                original_input=original_input,
                model_input=model_input,
                primary_intent=primary_intent,
                intents=intents,
                risk_level=None,
                assessment=None,
                response_messages=[],
                agent_steps=[],
                route_plan=route_plan,
                route_diagnostics={},
                specialist_results=[],
                evidence_items=[],
                tool_diagnostics={},
                report_id=None,
                tool_plan=AgentToolPlan(None, None),
                trace_id=trace.id,
                direct_response=resolution.question,
                user_message_id=current_message.id,
                context_manifest={},
                response_evidence_items=[],
                response_generation_diagnostics={},
                business_status="COMPLETED",
                business_error_codes=(),
            )
        if resolution.model_input:
            model_input = resolution.model_input
            if resolution.status == ClarificationStatus.RESOLVED:
                clarification_state = self._clarification_state(resolution.pending)
                resume_context = resolution.resume_context
                resolved_arguments = resolution.resolved_arguments
            elif resolution.continue_current_message:
                runtime_input = resolution.model_input
        UserMemoryService(self.db, self.settings).remember_explicit(user.id, original_input, current_message.id)
        packet = self.context_builder.build_base_context(
            user=user,
            session=session,
            current_message=current_message,
            model_input=model_input,
            clarification_state=clarification_state,
        )
        agent_run = create_agent_runtime(
            self.db,
            self.settings,
            context_builder=self.context_builder,
        ).run(
            user,
            session,
            runtime_input,
            model_input,
            current_message=current_message,
            clarification_state=clarification_state,
            resume_context=resume_context,
            resolved_arguments=resolved_arguments,
            context_packet=packet,
        )
        clarification = None
        direct_response = agent_run.direct_response
        if agent_run.clarification_request and agent_run.primary_intent != IntentType.RISK:
            clarification = dict(agent_run.clarification_request)
            try:
                pending = clarification_service.create(user, session, clarification)
            except ValueError as exc:
                logger.warning("Rejected invalid clarification artifact: %s", exc)
                clarification = None
                direct_response = (
                    "目前仍缺少必要范围，本次不再继续追问；"
                    "你可以补充完整范围后重新提问。"
                    if "最大轮数" in str(exc)
                    else ""
                )
                agent_run.direct_response = direct_response
            else:
                clarification["status"] = pending.status
                clarification["roundCount"] = pending.round_count
                clarification["missingArguments"] = json.loads(pending.missing_arguments_json or "[]")
                agent_run.clarification_request = clarification
                direct_response = pending.approved_question

        report = self._create_report(user, session, original_input, agent_run)
        risk_level = report.risk_level if report is not None else None
        trace = AgentTraceService(self.db, self.settings).save_run(
            user=user,
            session=session,
            original_input=trace_original_input,
            sanitized_input=trace_sanitized_input,
            memory_brief=agent_run.memory_brief,
            agent_run=agent_run,
            report_id=report.id if report is not None else None,
        )
        tool_plan = AgentToolPlan(report_id=report.id if report is not None else None, risk_level=risk_level)
        return AgentHarnessOutcome(
            session=session,
            original_input=original_input,
            model_input=model_input,
            primary_intent=agent_run.primary_intent,
            intents=agent_run.intents,
            risk_level=risk_level,
            assessment=agent_run.assessment,
            response_messages=agent_run.response_messages,
            agent_steps=agent_run.steps,
            route_plan=agent_run.route_plan,
            route_diagnostics=agent_run.route_diagnostics,
            specialist_results=agent_run.specialist_results,
            evidence_items=agent_run.evidence_items,
            tool_diagnostics=agent_run.tool_diagnostics,
            report_id=report.id if report is not None else None,
            tool_plan=tool_plan,
            trace_id=trace.id,
            clarification_request=clarification,
            direct_response=direct_response,
            user_message_id=current_message.id,
            context_manifest=agent_run.context_manifest,
            response_evidence_items=agent_run.response_evidence_items,
            response_generation_diagnostics=agent_run.response_generation_diagnostics,
            business_status=agent_run.business_status,
            business_error_codes=agent_run.business_error_codes,
        )

    def save_assistant_message(self, user: UserAccount, session: ChatSession, content: str) -> ChatMessage:
        # Compatibility path only persists the message. Durable memory work is
        # enqueued by ChatService._finalize_completed_turn in the same transaction.
        return self.save_message(user, session, MessageRole.ASSISTANT, content)

    async def dispatch_tools(self, tool_plan: AgentToolPlan) -> list[str]:
        if tool_plan.report_id is None:
            return []
        if self.settings.tool_queue_enabled:
            ToolQueueService(self.db, self.settings).enqueue_report(tool_plan.report_id, tool_plan.risk_level)
            return ["queued"]
        return await MindBridgeMcpToolClient(self.settings).handle_report(tool_plan.report_id, tool_plan.risk_level)

    def save_message(self, user: UserAccount, session: ChatSession, role: MessageRole, content: str) -> ChatMessage:
        message = ChatMessage(user_id=user.id, session_id=session.id, role=role.value, content=content)
        self.db.add(message)
        session.touch()
        self.db.add(session)
        self.db.commit()
        self.db.refresh(message)
        self.memory.append(session.public_id, role.value, content, message.id, user_id=user.id)
        return message

    @staticmethod
    def _validate_current_message(
        user: UserAccount,
        session: ChatSession,
        request: ChatRequest,
        current_message: ChatMessage,
        original_input: str,
    ) -> None:
        if (
            current_message.id is None
            or current_message.user_id != user.id
            or current_message.session_id != session.id
            or current_message.role.upper() != MessageRole.USER.value
            or current_message.content != original_input
            or request.sessionId != session.public_id
        ):
            raise ValueError("当前用户消息与会话或请求不一致")

    def _create_report(self, user: UserAccount, session: ChatSession, text: str, agent_run) -> PsychologicalReport | None:
        if not agent_run.requires_report or agent_run.assessment is None:
            return None
        report = PsychologicalReport(
            user_id=user.id,
            session_id=session.id,
            content=text,
            intent=_report_intent(agent_run).value,
            emotion=agent_run.assessment.emotion.value,
            emotion_score=agent_run.assessment.emotion_score,
            risk_level=agent_run.assessment.risk.value,
            confidence=agent_run.assessment.confidence,
            summary=agent_run.assessment.summary,
        )
        self.db.add(report)
        self.db.commit()
        self.db.refresh(report)
        return report

    @staticmethod
    def _clarification_state(pending) -> dict | None:
        if pending is None:
            return None
        try:
            known = json.loads(pending.known_arguments_json or "{}")
        except (TypeError, json.JSONDecodeError):
            known = {}
        try:
            missing = json.loads(pending.missing_arguments_json or "[]")
        except (TypeError, json.JSONDecodeError):
            missing = []
        return {
            "intent": pending.intent,
            "knownArguments": known if isinstance(known, dict) else {},
            "missingArguments": missing if isinstance(missing, list) else [],
        }


def _route_plan_from_pending(pending) -> dict:
    if pending is None:
        return {}
    try:
        resume = json.loads(pending.resume_context_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    from app.services.clarification_models import ClarificationResumeContext
    try:
        return ClarificationResumeContext.from_payload(resume).route_plan.as_payload()
    except ValueError:
        return {}


def _report_intent(agent_run) -> IntentType:
    if agent_run.risk_level == RiskLevel.HIGH or IntentType.RISK in agent_run.intents:
        return IntentType.RISK
    return IntentType.MENTAL


def _intent_summary(route_plan: dict) -> tuple[IntentType, tuple[IntentType, ...]]:
    try:
        primary = IntentType(str(route_plan.get("primaryIntent") or IntentType.CHAT.value))
    except ValueError:
        primary = IntentType.CHAT
    values: list[IntentType] = []
    for value in route_plan.get("intents", []):
        try:
            intent = IntentType(str(value))
        except ValueError:
            continue
        if intent not in values:
            values.append(intent)
    return primary, tuple(values or [primary])
