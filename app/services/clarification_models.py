from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.core.enums import IntentType, MAX_INPUT_CHARS


class ClarificationStatus(str, Enum):
    WAITING_USER = "WAITING_USER"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    INTERRUPTED = "INTERRUPTED"


@dataclass(frozen=True)
class MissingArgument:
    name: str
    reason_code: str
    allowed_values: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not 1 <= len(self.name.strip()) <= 64:
            raise ValueError("missingArgument name 无效")
        if not isinstance(self.reason_code, str) or not 1 <= len(self.reason_code.strip()) <= 64:
            raise ValueError("missingArgument reasonCode 无效")
        if self.name != self.name.strip() or self.reason_code != self.reason_code.strip():
            raise ValueError("missingArgument 字段必须已规范化")
        if len(self.allowed_values) > 20 or len(self.allowed_values) != len(set(self.allowed_values)):
            raise ValueError("allowedValues 无效")
        if any(not isinstance(item, str) or item != item.strip() or not 1 <= len(item) <= 200 for item in self.allowed_values):
            raise ValueError("allowedValues 成员无效")

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "MissingArgument":
        if not isinstance(payload, dict) or set(payload) != {"name", "reasonCode", "allowedValues"}:
            raise ValueError("missingArgument 字段不完整或包含未知字段")
        if not isinstance(payload["name"], str) or not isinstance(payload["reasonCode"], str):
            raise ValueError("missingArgument 字段类型无效")
        values = payload["allowedValues"]
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise ValueError("allowedValues 必须是字符串数组")
        return cls(payload["name"], payload["reasonCode"], tuple(values))

    def as_payload(self) -> dict[str, object]:
        return {"name": self.name, "reasonCode": self.reason_code, "allowedValues": list(self.allowed_values)}


@dataclass(frozen=True)
class SlotExtraction:
    values: dict[str, str]
    invalid_values: dict[str, str]
    unresolved_fields: tuple[str, ...]
    source: str = "REGEX"


@dataclass(frozen=True)
class ClarificationResumeContext:
    route_plan: Any
    origin_plan_id: str
    target_work_item_id: str
    expected_fields: tuple[str, ...]
    round_count: int

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "ClarificationResumeContext":
        from app.agents.routing import RoutePlan

        fields = {"schemaVersion", "routePlan", "originPlanId", "targetWorkItemId", "expectedFields", "roundCount"}
        if not isinstance(payload, dict) or set(payload) != fields or payload.get("schemaVersion") != 3:
            raise ValueError("resumeContext 必须严格使用 schemaVersion=3")
        if not isinstance(payload["originPlanId"], str) or not payload["originPlanId"].strip():
            raise ValueError("originPlanId 无效")
        if not isinstance(payload["targetWorkItemId"], str) or not payload["targetWorkItemId"].strip():
            raise ValueError("targetWorkItemId 无效")
        if not isinstance(payload["expectedFields"], list) or len(payload["expectedFields"]) > 1 or any(not isinstance(item, str) or not item.strip() for item in payload["expectedFields"]):
            raise ValueError("expectedFields 无效")
        if type(payload["roundCount"]) is not int or payload["roundCount"] < 0:
            raise ValueError("roundCount 无效")
        if not isinstance(payload["routePlan"], dict):
            raise ValueError("routePlan 无效")
        plan = RoutePlan.from_payload(payload["routePlan"])
        if payload["originPlanId"] != plan.plan_id:
            raise ValueError("originPlanId 与 routePlan 不一致")
        target = next((item for item in plan.work_items if item.work_item_id == payload["targetWorkItemId"]), None)
        if target is None:
            raise ValueError("targetWorkItemId 不存在")
        missing_names = {item.name for item in target.missing_arguments}
        expected = tuple(payload["expectedFields"])
        if expected and expected[0] not in missing_names:
            raise ValueError("expectedFields 不在目标缺参中")
        if not expected and target.missing_arguments:
            raise ValueError("resolved resumeContext 的目标工作项仍有缺参")
        return cls(plan, payload["originPlanId"], payload["targetWorkItemId"], expected, payload["roundCount"])

    def as_payload(self) -> dict[str, object]:
        return {
            "schemaVersion": 3,
            "routePlan": self.route_plan.as_payload(),
            "originPlanId": self.origin_plan_id,
            "targetWorkItemId": self.target_work_item_id,
            "expectedFields": list(self.expected_fields),
            "roundCount": self.round_count,
        }


@dataclass(frozen=True)
class ClarificationRequest:
    origin_plan_id: str
    target_work_item_id: str
    intent: IntentType
    objective: str
    original_message: str
    known_arguments: dict[str, str]
    missing_arguments: tuple[MissingArgument, ...]
    resume_context: ClarificationResumeContext

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "ClarificationRequest":
        fields = {"schemaVersion", "originPlanId", "targetWorkItemId", "intent", "objective", "originalMessage", "knownArguments", "missingArguments", "resumeContext"}
        if not isinstance(payload, dict) or set(payload) != fields or payload.get("schemaVersion") != 3:
            raise ValueError("clarification_request 必须严格使用 schemaVersion=3")
        for name in ("originPlanId", "targetWorkItemId", "objective", "originalMessage"):
            if not isinstance(payload[name], str) or not payload[name].strip():
                raise ValueError(f"{name} 无效")
        if len(payload["originalMessage"]) > MAX_INPUT_CHARS:
            raise ValueError("originalMessage 过长")
        try:
            intent = IntentType(payload["intent"])
        except (TypeError, ValueError) as exc:
            raise ValueError("intent 无效") from exc
        known = payload["knownArguments"]
        if not isinstance(known, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in known.items()):
            raise ValueError("knownArguments 必须为字符串字典")
        missing = payload["missingArguments"]
        if not isinstance(missing, list) or len(missing) != 1:
            raise ValueError("每次澄清必须恰好包含一个 missingArgument")
        argument = MissingArgument.from_payload(missing[0])
        if not isinstance(payload["resumeContext"], dict):
            raise ValueError("resumeContext 无效")
        resume = ClarificationResumeContext.from_payload(payload["resumeContext"])
        if payload["originPlanId"] != resume.origin_plan_id or payload["targetWorkItemId"] != resume.target_work_item_id:
            raise ValueError("澄清身份与 resumeContext 不一致")
        if resume.expected_fields != (argument.name,):
            raise ValueError("expectedFields 与 missingArgument 不一致")
        target = next(item for item in resume.route_plan.work_items if item.work_item_id == resume.target_work_item_id)
        if (target.intent, target.objective, target.known_arguments) != (intent, payload["objective"], known):
            raise ValueError("澄清目标与 routePlan 不一致")
        if argument not in target.missing_arguments:
            raise ValueError("澄清缺参不在目标工作项中")
        return cls(payload["originPlanId"], payload["targetWorkItemId"], intent, payload["objective"], payload["originalMessage"], dict(known), (argument,), resume)

    def as_payload(self) -> dict[str, object]:
        payload = {
            "schemaVersion": 3,
            "originPlanId": self.origin_plan_id,
            "targetWorkItemId": self.target_work_item_id,
            "intent": self.intent.value,
            "objective": self.objective,
            "originalMessage": self.original_message,
            "knownArguments": dict(self.known_arguments),
            "missingArguments": [item.as_payload() for item in self.missing_arguments],
            "resumeContext": self.resume_context.as_payload(),
        }
        return ClarificationRequest.from_payload(payload)._raw_payload()

    def _raw_payload(self) -> dict[str, object]:
        return {
            "schemaVersion": 3,
            "originPlanId": self.origin_plan_id,
            "targetWorkItemId": self.target_work_item_id,
            "intent": self.intent.value,
            "objective": self.objective,
            "originalMessage": self.original_message,
            "knownArguments": dict(self.known_arguments),
            "missingArguments": [item.as_payload() for item in self.missing_arguments],
            "resumeContext": self.resume_context.as_payload(),
        }


@dataclass(frozen=True)
class ClarificationResolution:
    handled: bool = False
    continue_current_message: bool = False
    question: str = ""
    model_input: str = ""
    status: ClarificationStatus | None = None
    pending_id: int | None = None
    pending: Any | None = None
    resolved_arguments: dict[str, str] = field(default_factory=dict)
    resume_context: dict[str, object] = field(default_factory=dict)
