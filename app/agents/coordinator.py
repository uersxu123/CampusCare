from __future__ import annotations

import uuid
from collections import defaultdict

from app.agents.autonomous import CoordinatorAgent
from app.agents.events import (
    AgentArtifact,
    AgentEvent,
    AgentEventType,
    AgentTask,
    CollaborationBlackboard,
    PRIORITY_ORDER,
    TaskPriority,
    TaskStatus,
)
from app.agents.registry import AgentCapability, AgentRegistry
from app.agents.routing import RoutePlan
from app.core.config import Settings
from app.core.enums import IntentType, RiskLevel
from app.services.clarification_models import ClarificationRequest, ClarificationResumeContext
from app.services.clarification_policy import ClarificationPolicy
from app.services.risk_signals import analyze_risk_signal
from app.services.execution_control import current_execution_budget


CAPABILITY_BY_INTENT = {
    IntentType.CHAT: AgentCapability.GENERAL_CHAT,
    IntentType.ACADEMIC: AgentCapability.ACADEMIC_PLANNING,
    IntentType.CAMPUS: AgentCapability.CAMPUS_AFFAIRS,
    IntentType.MENTAL: AgentCapability.PSYCHOLOGICAL_SUPPORT,
}


class EventDrivenCoordinator:
    def __init__(
        self,
        registry: AgentRegistry,
        coordinator_agent: CoordinatorAgent,
        settings: Settings,
        skill_manager=None,
        clarification_policy: ClarificationPolicy | None = None,
    ):
        del skill_manager
        self.registry = registry
        self.coordinator_agent = coordinator_agent
        self.settings = settings
        self.max_rounds = min(
            int(getattr(settings, "agent_max_rounds", 12)),
            int(getattr(settings, "agent_max_rounds_hard_limit", 16)),
        )
        self.max_claims_per_round = int(getattr(settings, "agent_max_claims_per_round", 4))
        self.max_claims_per_agent = int(getattr(settings, "agent_max_claims_per_agent", 4))
        self.final_min_confidence = float(getattr(settings, "agent_final_acceptance_min_confidence", 0.6))
        # Minimal V7.5 guard: allow at most one ResponseAgent regeneration after a Safety REVISE.
        self.max_response_revisions = max(0, int(getattr(settings, "agent_max_response_revisions", 1)))
        self.clarification_policy = clarification_policy or ClarificationPolicy()

    def run(self, board: CollaborationBlackboard) -> CollaborationBlackboard:
        board = self._ensure_root_task(board)
        claim_counts: dict[str, int] = defaultdict(int)
        for round_number in range(1, self.max_rounds + 1):
            budget = current_execution_budget()
            if budget is not None and budget.expired():
                return board.append_event(AgentEvent(
                    type=AgentEventType.BUDGET_EXHAUSTED,
                    actor=self.coordinator_agent.name,
                    message="turn execution deadline exceeded",
                    metadata={"errorCode": "DEADLINE_EXCEEDED"},
                ))
            board = board.append_event(AgentEvent(
                type=AgentEventType.ROUND_STARTED,
                actor=self.coordinator_agent.name,
                message=f"round={round_number}",
                metadata={"round": round_number},
            ))
            board = self._derive_missing_work(board)
            if board.latest_artifact("clarification_request"):
                return self._mark_user_input_required(board)
            if self._response_revision_exhausted(board):
                return board
            board = self._try_accept_final(board)
            if board.final_artifact_id:
                return board
            candidates = self._claim_candidates(board, claim_counts)
            if not candidates:
                break
            for task, candidate in candidates:
                budget = current_execution_budget()
                if budget is not None and budget.expired():
                    return board.append_event(AgentEvent(
                        type=AgentEventType.BUDGET_EXHAUSTED,
                        actor=self.coordinator_agent.name,
                        task_id=task.id,
                        message="turn execution deadline exceeded before dispatch",
                        metadata={"errorCode": "DEADLINE_EXCEEDED"},
                    ))
                current = board.tasks.get(task.id, task)
                claimed = current.claim(candidate.agent.profile.name)
                board = board.update_task(claimed).append_event(AgentEvent(
                    type=AgentEventType.TASK_CLAIMED,
                    actor=candidate.agent.profile.name,
                    task_id=task.id,
                    message=candidate.decision.reason,
                    metadata={"confidence": candidate.decision.confidence},
                ))
                result = candidate.agent.act(claimed, board)
                board = board.apply_turn_result(claimed, candidate.agent.profile.name, result)
                claim_counts[candidate.agent.profile.name] += 1
            board = self._derive_missing_work(board)
            if board.latest_artifact("clarification_request"):
                return self._mark_user_input_required(board)
            if self._response_revision_exhausted(board):
                return board
            board = self._try_accept_final(board)
            if board.final_artifact_id:
                return board
        return board.append_event(AgentEvent(
            type=AgentEventType.BUDGET_EXHAUSTED,
            actor=self.coordinator_agent.name,
            message="event-driven agent budget exhausted before final acceptance",
        ))

    def _ensure_root_task(self, board: CollaborationBlackboard) -> CollaborationBlackboard:
        if board.tasks:
            return board
        root = self.coordinator_agent.root_task(board)
        return board.add_task(root).append_event(AgentEvent(
            type=AgentEventType.TASK_CREATED,
            actor=self.coordinator_agent.name,
            task_id=root.id,
            message=root.title,
        ))

    def _derive_missing_work(self, board: CollaborationBlackboard) -> CollaborationBlackboard:
        # Safety is independent from routing. A HIGH risk artifact can therefore short-circuit
        # ordinary work even if the planner has not produced a RoutePlan yet.
        if board.latest_artifact("route_plan") is None and board.latest_artifact("route_control") is None:
            board = self._ensure_artifact_task(
                board, "route_plan", "task:understand", "Understand user turn",
                AgentCapability.UNDERSTANDING, TaskPriority.HIGH,
            )
        board = self._ensure_artifact_task(
            board, "risk", "task:assess-safety", "Assess safety risk",
            AgentCapability.SAFETY,
            TaskPriority.CRITICAL if _hard_high_risk(board.user_input) else TaskPriority.HIGH,
        )

        risk_artifact = board.latest_artifact("risk")
        if risk_artifact is None:
            return board
        risk = _risk_value(board)
        plan_artifact = board.latest_artifact("route_plan")
        plan = RoutePlan.from_payload(plan_artifact.payload) if plan_artifact is not None else None

        if risk == RiskLevel.HIGH or (plan is not None and plan.primary_intent == IntentType.RISK):
            board = self._apply_safety_override(board, plan)
            board = self._ensure_response_task(board, priority=TaskPriority.CRITICAL)
            return self._ensure_response_review_and_revision(board)

        # Global degraded 0-intent is a program-level fixed clarification, not a fake CHAT work item.
        route_control = board.latest_artifact("route_control")
        if route_control is not None and plan is None:
            board = self._ensure_route_control_response(board, route_control)
            return self._ensure_response_review_and_revision(board)

        if plan_artifact is None or plan is None:
            return board

        target = self.clarification_policy.select(plan.as_payload())
        if target is not None:
            if board.latest_artifact("clarification_request") is None:
                missing = target.missing_argument
                request = ClarificationRequest(
                    origin_plan_id=plan.plan_id,
                    target_work_item_id=target.work_item_id,
                    intent=target.intent,
                    objective=target.objective,
                    original_message=board.user_input,
                    known_arguments=target.known_arguments,
                    missing_arguments=(missing,),
                    resume_context=ClarificationResumeContext(
                        plan, plan.plan_id, target.work_item_id, (missing.name,), 0,
                    ),
                )
                board = board.add_artifact(AgentArtifact(
                    id=f"CoordinatorAgent:clarification_request:{uuid.uuid4().hex[:10]}",
                    owner="CoordinatorAgent",
                    kind="clarification_request",
                    payload=request.as_payload(),
                    confidence=1.0,
                    metadata={
                        "planId": plan.plan_id,
                        "workItemId": target.work_item_id,
                        "field": missing.name,
                    },
                ))
            return board

        board = self._ensure_specialist_tasks(board, plan, plan_artifact.id)
        if plan.synthesis_order and plan.synthesis_order == tuple(
            item for item in plan.synthesis_order if item in board.completed_work_item_ids(plan.plan_id)
        ):
            board = self._ensure_response_task(board, priority=TaskPriority.HIGH)
        return self._ensure_response_review_and_revision(board)

    def _ensure_route_control_response(
        self,
        board: CollaborationBlackboard,
        route_control: AgentArtifact,
    ) -> CollaborationBlackboard:
        if board.latest_artifact("response_proposal") is not None:
            return board
        text = str(route_control.payload.get("fixedResponse") or "").strip()
        if not text:
            return board
        return board.add_artifact(AgentArtifact(
            id=f"CoordinatorAgent:response_proposal:{uuid.uuid4().hex[:10]}",
            owner="CoordinatorAgent",
            kind="response_proposal",
            payload={
                "messages": [],
                "directResponse": text,
                "mode": "route_clarification",
                "contextManifest": {},
                "promptEvidence": [],
                "generationDiagnostics": {
                    "attempts": 0,
                    "finishReason": "PROGRAM_TEMPLATE",
                    "fallbackUsed": False,
                },
            },
            confidence=1.0,
            metadata={"routeControlArtifactId": route_control.id},
        ))

    def _ensure_response_review_and_revision(self, board: CollaborationBlackboard) -> CollaborationBlackboard:
        response = board.latest_artifact("response_proposal")
        review = board.latest_artifact("safety_review")
        if response and (review is None or review.metadata.get("responseArtifactId") != response.id):
            board = self._ensure_task(board, AgentTask(
                id=f"task:review-response:{response.id}",
                title="Review candidate response safety",
                priority=TaskPriority.HIGH,
                required_capabilities=frozenset({AgentCapability.SAFETY.value}),
                created_by=self.coordinator_agent.name,
                metadata={"kind": "safety_review", "responseArtifactId": response.id},
            ))

        critique = board.latest_artifact("critique")
        if not critique or critique.payload.get("approved") is not False:
            return board

        # Only react to a critique for the current candidate. Older critiques remain on the
        # blackboard for traceability but must not schedule another revision for a newer response.
        if response is None or str(critique.payload.get("responseArtifactId") or "") != response.id:
            return board

        rejected_count = sum(
            1
            for item in board.artifacts_by_kind("critique")
            if item.payload.get("approved") is False
        )
        if rejected_count > self.max_response_revisions:
            # V7.5 minimal terminal path: do not create another Response task and do not
            # synthesize a fallback inside Agent Runtime. EventDrivenCoordinator.run() sees
            # the exhausted revision state and returns the Blackboard immediately.
            return board

        return self._ensure_task(board, AgentTask(
            id=f"task:revise-response:{critique.id}",
            title="Revise response after safety critique",
            priority=TaskPriority.CRITICAL,
            required_capabilities=frozenset({AgentCapability.RESPONSE.value}),
            created_by=self.coordinator_agent.name,
            metadata={"kind": "response", "revisionOf": critique.payload.get("responseArtifactId", "")},
        ))

    def _response_revision_exhausted(self, board: CollaborationBlackboard) -> bool:
        """Return True only when the current candidate has exceeded the Safety revision budget.

        Historical critiques remain on the Blackboard for traceability, so terminal detection
        must be bound to the current responseArtifactId rather than critique count alone.
        """
        response = board.latest_artifact("response_proposal")
        critique = board.latest_artifact("critique")
        if response is None or critique is None or critique.payload.get("approved") is not False:
            return False
        if str(critique.payload.get("responseArtifactId") or "") != response.id:
            return False
        rejected_count = sum(
            1
            for item in board.artifacts_by_kind("critique")
            if item.payload.get("approved") is False
        )
        return rejected_count > self.max_response_revisions

    def _ensure_specialist_tasks(self, board: CollaborationBlackboard, plan: RoutePlan, route_plan_artifact_id: str) -> CollaborationBlackboard:
        task_ids = {item.work_item_id: f"task:specialist:{plan.plan_id}:{item.work_item_id}" for item in plan.work_items}
        for item in plan.work_items:
            if item.intent == IntentType.RISK:
                continue
            board = self._ensure_task(board, AgentTask(
                id=task_ids[item.work_item_id],
                title=f"Resolve {item.intent.value} work item",
                description=item.objective,
                priority=TaskPriority(item.priority),
                required_capabilities=frozenset({CAPABILITY_BY_INTENT[item.intent].value}),
                created_by=self.coordinator_agent.name,
                depends_on=tuple(task_ids[parent] for parent in item.depends_on),
                metadata={
                    "kind": "specialist",
                    "planId": plan.plan_id,
                    "workItemId": item.work_item_id,
                    "intent": item.intent.value,
                    "workItem": item.as_payload(),
                    "routePlanArtifactId": route_plan_artifact_id,
                },
            ))
        return board

    def _ensure_response_task(self, board: CollaborationBlackboard, priority: TaskPriority) -> CollaborationBlackboard:
        if board.latest_artifact("response_proposal") is not None:
            return board
        return self._ensure_task(board, AgentTask(
            id="task:propose-response",
            title="Synthesize specialist results",
            priority=priority,
            required_capabilities=frozenset({AgentCapability.RESPONSE.value}),
            created_by=self.coordinator_agent.name,
            metadata={"kind": "response"},
        ))

    def _apply_safety_override(self, board: CollaborationBlackboard, plan: RoutePlan | None) -> CollaborationBlackboard:
        tasks = dict(board.tasks)
        changed = False
        for task_id, task in tasks.items():
            if task.metadata.get("kind") == "specialist" and task.status != TaskStatus.CLOSED:
                tasks[task_id] = AgentTask(
                    **{
                        **task.__dict__,
                        "status": TaskStatus.BLOCKED,
                        "metadata": {**task.metadata, "blockedReason": "SAFETY_OVERRIDE"},
                    }
                )
                changed = True
        if changed:
            board = CollaborationBlackboard(**{**board.__dict__, "tasks": tasks})
        if not any(event.type == AgentEventType.SAFETY_OVERRIDE for event in board.events):
            board = board.append_event(AgentEvent(
                type=AgentEventType.SAFETY_OVERRIDE,
                actor=self.coordinator_agent.name,
                message="HIGH risk blocks ordinary specialist work",
                metadata={"planId": plan.plan_id if plan is not None else ""},
            ))
        return board

    def _ensure_artifact_task(
        self,
        board: CollaborationBlackboard,
        artifact_kind: str,
        task_id: str,
        title: str,
        capability: AgentCapability,
        priority: TaskPriority,
    ) -> CollaborationBlackboard:
        if not board.user_input or board.latest_artifact(artifact_kind) is not None:
            return board
        return self._ensure_task(board, AgentTask(
            id=task_id,
            title=title,
            description=board.user_input,
            priority=priority,
            required_capabilities=frozenset({capability.value}),
            created_by=self.coordinator_agent.name,
            metadata={"kind": artifact_kind},
        ))

    def _ensure_task(self, board: CollaborationBlackboard, task: AgentTask) -> CollaborationBlackboard:
        if task.id in board.tasks:
            return board
        return board.add_task(task).append_event(AgentEvent(
            type=AgentEventType.TASK_CREATED,
            actor=self.coordinator_agent.name,
            task_id=task.id,
            message=task.title,
        ))

    def _claim_candidates(self, board: CollaborationBlackboard, claim_counts: dict[str, int]):
        candidates = []
        for task in board.open_tasks():
            if not board.dependencies_closed(task):
                continue
            for candidate in self.registry.candidate_decisions_for(task, board):
                if claim_counts.get(candidate.agent.profile.name, 0) < self.max_claims_per_agent:
                    candidates.append((task, candidate))
        candidates.sort(
            key=lambda item: (PRIORITY_ORDER[item[0].priority], item[1].decision.confidence, item[1].agent.profile.name),
            reverse=True,
        )
        selected = []
        selected_tasks: set[str] = set()
        selected_agents: set[str] = set()
        for task, candidate in candidates:
            name = candidate.agent.profile.name
            if task.id in selected_tasks or name in selected_agents:
                continue
            selected.append((task, candidate))
            selected_tasks.add(task.id)
            selected_agents.add(name)
            if len(selected) >= self.max_claims_per_round:
                break
        return selected

    def _try_accept_final(self, board: CollaborationBlackboard) -> CollaborationBlackboard:
        if board.final_artifact_id:
            return board
        response = board.latest_artifact("response_proposal")
        review = board.latest_artifact("safety_review")
        if response is None or review is None:
            return board
        if review.metadata.get("responseArtifactId") != response.id or not review.payload.get("approved"):
            return board
        if response.confidence < self.final_min_confidence:
            return board
        return board.accept_final(
            response.id,
            self.coordinator_agent.name,
            "accepted after ResponseAgent synthesis and SafetyAgent review",
        )

    def _mark_user_input_required(self, board: CollaborationBlackboard) -> CollaborationBlackboard:
        if any(event.type == AgentEventType.USER_INPUT_REQUIRED for event in board.events):
            return board
        return board.append_event(AgentEvent(
            type=AgentEventType.USER_INPUT_REQUIRED,
            actor=self.coordinator_agent.name,
            message="route plan paused before specialist execution",
        ))


def _risk_value(board: CollaborationBlackboard) -> RiskLevel:
    order = {RiskLevel.LOW: 1, RiskLevel.MEDIUM: 2, RiskLevel.HIGH: 3}
    highest = RiskLevel.LOW
    for artifact in board.artifacts_by_kind("risk"):
        try:
            value = RiskLevel(str(artifact.payload.get("risk") or RiskLevel.LOW.value).upper())
        except ValueError:
            value = RiskLevel.LOW
        if order[value] > order[highest]:
            highest = value
    return RiskLevel.HIGH if any(event.type == AgentEventType.SAFETY_OVERRIDE for event in board.events) else highest


def _hard_high_risk(text: str) -> bool:
    signal = analyze_risk_signal(text)
    return signal.explicit_current_self_harm or signal.indirect_current_danger
