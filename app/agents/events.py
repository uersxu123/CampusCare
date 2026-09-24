from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any


class AgentEventType(str, Enum):
    TURN_STARTED = "TURN_STARTED"
    TASK_CREATED = "TASK_CREATED"
    TASK_CLAIMED = "TASK_CLAIMED"
    TASK_RELEASED = "TASK_RELEASED"
    TASK_CLOSED = "TASK_CLOSED"
    MESSAGE_SENT = "MESSAGE_SENT"
    ARTIFACT_PUBLISHED = "ARTIFACT_PUBLISHED"
    CRITIQUE_PUBLISHED = "CRITIQUE_PUBLISHED"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    SAFETY_OVERRIDE = "SAFETY_OVERRIDE"
    FINAL_ACCEPTED = "FINAL_ACCEPTED"
    ROUND_STARTED = "ROUND_STARTED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    USER_INPUT_REQUIRED = "USER_INPUT_REQUIRED"
    TURN_RESUMED = "TURN_RESUMED"


class TaskStatus(str, Enum):
    OPEN = "OPEN"
    CLAIMED = "CLAIMED"
    BLOCKED = "BLOCKED"
    CLOSED = "CLOSED"


class TaskPriority(str, Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


PRIORITY_ORDER = {
    TaskPriority.LOW: 1,
    TaskPriority.NORMAL: 2,
    TaskPriority.HIGH: 3,
    TaskPriority.CRITICAL: 4,
}


@dataclass(frozen=True)
class AgentTask:
    id: str
    title: str
    description: str = ""
    priority: TaskPriority = TaskPriority.NORMAL
    status: TaskStatus = TaskStatus.OPEN
    required_capabilities: frozenset[str] = field(default_factory=frozenset)
    created_by: str = "CoordinatorAgent"
    claimed_by: tuple[str, ...] = field(default_factory=tuple)
    depends_on: tuple[str, ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)

    def claim(self, agent_name: str) -> "AgentTask":
        if agent_name in self.claimed_by:
            return self
        return replace(self, status=TaskStatus.CLAIMED, claimed_by=(*self.claimed_by, agent_name))

    def reopen(self) -> "AgentTask":
        return replace(self, status=TaskStatus.OPEN)

    def close(self) -> "AgentTask":
        return replace(self, status=TaskStatus.CLOSED)


@dataclass(frozen=True)
class AgentClaim:
    agent: str
    task_id: str
    confidence: float
    reason: str


@dataclass(frozen=True)
class AgentMessage:
    id: str
    sender: str
    recipient: str
    content: str
    task_id: str = ""
    kind: str = "REQUEST"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentArtifact:
    id: str
    owner: str
    kind: str
    payload: dict[str, Any]
    confidence: float = 1.0
    task_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentEvent:
    type: AgentEventType
    actor: str
    task_id: str = ""
    artifact_id: str = ""
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentTurnResult:
    messages: tuple[AgentMessage, ...] = field(default_factory=tuple)
    artifacts: tuple[AgentArtifact, ...] = field(default_factory=tuple)
    tasks: tuple[AgentTask, ...] = field(default_factory=tuple)
    events: tuple[AgentEvent, ...] = field(default_factory=tuple)
    close_task: bool = True


@dataclass(frozen=True)
class CollaborationBlackboard:
    turn_id: str
    user_id: int | None = None
    session_id: str = ""
    user_input: str = ""
    model_input: str = ""
    tasks: dict[str, AgentTask] = field(default_factory=dict)
    messages: tuple[AgentMessage, ...] = field(default_factory=tuple)
    artifacts: tuple[AgentArtifact, ...] = field(default_factory=tuple)
    events: tuple[AgentEvent, ...] = field(default_factory=tuple)
    final_artifact_id: str = ""

    def add_task(self, task: AgentTask) -> "CollaborationBlackboard":
        tasks = dict(self.tasks)
        tasks[task.id] = task
        return replace(self, tasks=tasks)

    def update_task(self, task: AgentTask) -> "CollaborationBlackboard":
        return self.add_task(task)

    def append_event(self, event: AgentEvent) -> "CollaborationBlackboard":
        return replace(self, events=(*self.events, event))

    def send_message(self, message: AgentMessage) -> "CollaborationBlackboard":
        return replace(self, messages=(*self.messages, message)).append_event(
            AgentEvent(
                type=AgentEventType.MESSAGE_SENT,
                actor=message.sender,
                task_id=message.task_id,
                message=message.content,
                metadata={"recipient": message.recipient, "kind": message.kind},
            )
        )

    def add_artifact(self, artifact: AgentArtifact) -> "CollaborationBlackboard":
        if artifact.kind == "clarification_request" and artifact.owner != "CoordinatorAgent":
            raise ValueError("只有 CoordinatorAgent 可以发布 clarification_request")
        if artifact.kind == "specialist_result":
            from app.agents.result import SpecialistResultV2
            from app.agents.routing import RoutePlan

            result = SpecialistResultV2.from_payload(artifact.payload)
            normalized = result.as_payload()
            artifact = replace(artifact, payload=normalized)
            payload_plan = result.planId
            payload_work = result.workItemId
            metadata_plan = str(artifact.metadata.get("planId") or "")
            metadata_work = str(artifact.metadata.get("workItemId") or "")
            if not payload_plan or not payload_work or (payload_plan, payload_work) != (metadata_plan, metadata_work):
                raise ValueError("specialist_result 的 planId/workItemId 必须在 payload 与 metadata 中一致")
            route_artifact_id = str(artifact.metadata.get("routePlanArtifactId") or "")
            route_artifact = self.artifact_by_id(route_artifact_id) if route_artifact_id else self.latest_artifact("route_plan")
            if route_artifact is None or route_artifact.kind != "route_plan":
                raise ValueError("specialist_result 缺少当前 route_plan")
            plan = RoutePlan.from_payload(route_artifact.payload)
            if plan.plan_id != payload_plan:
                raise ValueError("specialist_result planId 与 route_plan 不一致")
            work_item = next((item for item in plan.work_items if item.work_item_id == payload_work), None)
            if work_item is None:
                raise ValueError("specialist_result workItemId 不存在")
            if (result.intent, result.objective, result.knownArguments) != (
                work_item.intent, work_item.objective, work_item.known_arguments,
            ):
                raise ValueError("specialist_result 不得改变 RoutePlan 工作项")
            expected_dependencies = []
            for dependency_id in work_item.depends_on:
                upstream = self.latest_artifact_for_work_item("specialist_result", dependency_id)
                if upstream is None:
                    raise ValueError("specialist_result 缺少直接上游结果")
                expected_dependencies.append(upstream.id)
            if result.dependencyResultIds != expected_dependencies:
                raise ValueError("specialist_result dependencyResultIds 与 RoutePlan 不一致")
            if self.latest_artifact_for_work_item("specialist_result", payload_work) is not None:
                raise ValueError("每个 workItem 只允许一个终态 specialist_result")
        event_type = AgentEventType.CRITIQUE_PUBLISHED if artifact.kind == "critique" else AgentEventType.ARTIFACT_PUBLISHED
        return replace(self, artifacts=(*self.artifacts, artifact)).append_event(
            AgentEvent(
                type=event_type,
                actor=artifact.owner,
                task_id=artifact.task_id,
                artifact_id=artifact.id,
                message=artifact.kind,
                metadata={"confidence": artifact.confidence},
            )
        )

    def apply_turn_result(self, task: AgentTask, agent_name: str, result: AgentTurnResult) -> "CollaborationBlackboard":
        board = self
        for message in result.messages:
            board = board.send_message(message)
        for artifact in result.artifacts:
            board = board.add_artifact(artifact)
        for follow_up in result.tasks:
            if follow_up.id not in board.tasks:
                board = board.add_task(follow_up).append_event(
                    AgentEvent(
                        type=AgentEventType.TASK_CREATED,
                        actor=agent_name,
                        task_id=follow_up.id,
                        message=follow_up.title,
                    )
                )
        if result.close_task:
            board = board.update_task(task.close()).append_event(
                AgentEvent(type=AgentEventType.TASK_CLOSED, actor=agent_name, task_id=task.id, message=task.title)
            )
        else:
            board = board.update_task(task.reopen())
        for event in result.events:
            board = board.append_event(event)
        return board

    def open_tasks(self) -> list[AgentTask]:
        return [task for task in self.tasks.values() if task.status == TaskStatus.OPEN]

    def artifacts_by_kind(self, kind: str) -> list[AgentArtifact]:
        return [artifact for artifact in self.artifacts if artifact.kind == kind]

    def artifacts_for_work_item(self, kind: str, work_item_id: str) -> list[AgentArtifact]:
        return [
            artifact
            for artifact in self.artifacts
            if artifact.kind == kind
            and str(artifact.payload.get("workItemId") or artifact.metadata.get("workItemId") or "") == work_item_id
        ]

    def latest_artifact_for_work_item(self, kind: str, work_item_id: str) -> AgentArtifact | None:
        values = self.artifacts_for_work_item(kind, work_item_id)
        return values[-1] if values else None

    def completed_work_item_ids(self, plan_id: str) -> set[str]:
        return {
            str(artifact.payload.get("workItemId"))
            for artifact in self.artifacts_by_kind("specialist_result")
            if artifact.payload.get("planId") == plan_id
            and artifact.payload.get("status") in {"COMPLETED", "PARTIAL", "FAILED"}
        }

    def ordered_specialist_results(self, route_plan: dict[str, Any]) -> list[dict[str, Any]]:
        from app.agents.result import SpecialistResultV2
        return [
            SpecialistResultV2.from_payload(artifact.payload).as_payload()
            for work_item_id in route_plan.get("synthesisOrder", [])
            if (artifact := self.latest_artifact_for_work_item("specialist_result", str(work_item_id))) is not None
        ]

    def dependencies_closed(self, task: AgentTask) -> bool:
        return all(
            dependency in self.tasks and self.tasks[dependency].status == TaskStatus.CLOSED
            for dependency in task.depends_on
        )

    def artifact_by_id(self, artifact_id: str) -> AgentArtifact | None:
        if not artifact_id:
            return None
        return next((artifact for artifact in self.artifacts if artifact.id == artifact_id), None)

    def latest_artifact(self, kind: str, owner: str | None = None) -> AgentArtifact | None:
        for artifact in reversed(self.artifacts):
            if artifact.kind == kind and (owner is None or artifact.owner == owner):
                return artifact
        return None

    def messages_for(self, agent_name: str) -> list[AgentMessage]:
        return [message for message in self.messages if message.recipient in {agent_name, "*"}]

    def has_artifact(self, kind: str) -> bool:
        return self.latest_artifact(kind) is not None

    def accepted_artifact(self) -> AgentArtifact | None:
        if not self.final_artifact_id:
            return None
        return next((artifact for artifact in self.artifacts if artifact.id == self.final_artifact_id), None)

    def accept_final(self, artifact_id: str, actor: str, reason: str) -> "CollaborationBlackboard":
        return replace(self, final_artifact_id=artifact_id).append_event(
            AgentEvent(
                type=AgentEventType.FINAL_ACCEPTED,
                actor=actor,
                artifact_id=artifact_id,
                message=reason,
            )
        )
