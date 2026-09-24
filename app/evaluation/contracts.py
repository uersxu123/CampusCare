from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.enums import IntentType, RiskLevel
from app.services.intent_fusion import ContextRelation


ExpectedAction = Literal["ANSWER", "PARTIAL_ANSWER", "CLARIFY", "ABSTAIN", "SAFETY_BYPASS"]


class EvaluationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)

    @field_validator("content")
    @classmethod
    def require_nonblank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("routing message content 不得为空")
        return value


class ExpectedTools(BaseModel):
    required: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)

    @field_validator("required", "forbidden")
    @classmethod
    def validate_names(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item or "__" in item or "/" in item for item in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("Golden 工具名称无效")
        return normalized

    @model_validator(mode="after")
    def validate_disjoint(self):
        if set(self.required).intersection(self.forbidden):
            raise ValueError("required/forbidden 工具不得相交")
        return self


class AcceptableToolContract(ExpectedTools):
    action: ExpectedAction


class ProductionRoutingExpectedV3(BaseModel):
    model_config = ConfigDict(extra="forbid")
    primaryIntent: IntentType
    intents: list[IntentType]
    workItemCount: int = Field(ge=1, le=4)
    workItemIntents: list[IntentType]
    sourceTextFragments: list[list[str]]
    hardDataEdges: list[tuple[int, int]]
    orderOnlyEdges: list[tuple[int, int]]
    missingArgumentNamesByWorkItem: list[list[str]]
    contextRelation: ContextRelation
    capacityExceeded: bool

    @model_validator(mode="after")
    def validate_shape(self):
        _validate_route_shape(self.primaryIntent, self.intents, self.workItemCount, self.workItemIntents, self.missingArgumentNamesByWorkItem, self.hardDataEdges)
        if len(self.sourceTextFragments) != self.workItemCount or any(not row or any(not item.strip() for item in row) or len(row) != len(set(row)) for row in self.sourceTextFragments):
            raise ValueError("sourceTextFragments 无效")
        _require_dag(self.workItemCount, [*self.hardDataEdges, *self.orderOnlyEdges])
        hard_pairs = {frozenset(edge) for edge in self.hardDataEdges}
        if any(frozenset(edge) in hard_pairs for edge in self.orderOnlyEdges):
            raise ValueError("HARD_DATA 与 ORDER_ONLY 不得共享无向边")
        if self.capacityExceeded and not (self.workItemCount == 1 and self.workItemIntents == [IntentType.CHAT] and not self.hardDataEdges and not self.orderOnlyEdges):
            raise ValueError("capacityExceeded 结构无效")
        if self.contextRelation == ContextRelation.AMBIGUOUS and not (self.workItemCount == 1 and self.workItemIntents == [IntentType.CHAT] and not self.capacityExceeded and not self.hardDataEdges and not self.orderOnlyEdges):
            raise ValueError("AMBIGUOUS expected 结构无效")
        return self


class RoutingCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    messages: list[EvaluationMessage] = Field(min_length=1)
    expected: ProductionRoutingExpectedV3
    tags: list[str]

    @model_validator(mode="after")
    def validate_messages(self):
        if self.id != self.id.strip():
            raise ValueError("routing id 不得包含首尾空白")
        if self.messages[-1].role != "user" or any(self.messages[index].role == self.messages[index - 1].role for index in range(1, len(self.messages))):
            raise ValueError("routing messages 必须交替且最后一条为 user")
        normalized_tags = [item.strip() for item in self.tags]
        if any(not item for item in normalized_tags) or len(normalized_tags) != len(set(normalized_tags)):
            raise ValueError("tags 无效")
        self.tags = normalized_tags
        return self

    def sut_input(self) -> dict[str, Any]:
        return {"messages": [item.model_dump() for item in self.messages]}


class EndToEndExpectedRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")
    primaryIntent: IntentType
    intents: list[IntentType]
    riskLevel: RiskLevel
    workItemCount: int = Field(ge=1, le=4)
    workItemIntents: list[IntentType]
    dependencyEdges: list[tuple[int, int]]
    missingArgumentNamesByWorkItem: list[list[str]]

    @model_validator(mode="after")
    def validate_shape(self):
        _validate_route_shape(self.primaryIntent, self.intents, self.workItemCount, self.workItemIntents, self.missingArgumentNamesByWorkItem, self.dependencyEdges)
        return self


class EndToEndCase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str = Field(min_length=1)
    turns: list[str] = Field(min_length=1)
    expected_action: ExpectedAction = Field(validation_alias=AliasChoices("expectedAction", "expected_action"))
    acceptable_actions: list[ExpectedAction] = Field(
        default_factory=list,
        validation_alias=AliasChoices("acceptableActions", "acceptable_actions"),
    )
    reference: str = Field(min_length=1)
    reference_facts: list[str] = Field(default_factory=list, validation_alias=AliasChoices("referenceFacts", "reference_facts"))
    reference_context_ids: list[str] = Field(default_factory=list, validation_alias=AliasChoices("referenceContextIds", "reference_context_ids"))
    reference_contexts: list[str] = Field(default_factory=list, validation_alias=AliasChoices("referenceContexts", "reference_contexts"))
    forbidden_claims: list[str] = Field(default_factory=list, validation_alias=AliasChoices("forbiddenClaims", "forbidden_claims"))
    expected_route: EndToEndExpectedRoute | None = Field(default=None, validation_alias=AliasChoices("expectedRoute", "expected_route"))
    expected_tools: ExpectedTools = Field(default_factory=ExpectedTools, validation_alias=AliasChoices("expectedTools", "expected_tools"))
    acceptable_tool_contracts: list[AcceptableToolContract] = Field(
        default_factory=list,
        validation_alias=AliasChoices("acceptableToolContracts", "acceptable_tool_contracts"),
    )
    expected_document_keys: list[str] = Field(default_factory=list, validation_alias=AliasChoices("expectedDocumentKeys", "expected_document_keys"))
    metric_applicability: dict[str, bool] = Field(default_factory=dict, validation_alias=AliasChoices("metricApplicability", "metric_applicability"))
    critical: bool = False
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_acceptable_contracts(self):
        actions = self.allowed_actions
        contract_actions = [item.action for item in self.acceptable_tool_contracts]
        if len(actions) != len(set(actions)) or self.expected_action not in actions:
            raise ValueError("acceptableActions 必须唯一且包含 expectedAction")
        if len(contract_actions) != len(set(contract_actions)):
            raise ValueError("acceptableToolContracts action 不得重复")
        if any(action not in actions for action in contract_actions):
            raise ValueError("acceptableToolContracts action 必须属于 acceptableActions")
        return self

    @property
    def allowed_actions(self) -> list[ExpectedAction]:
        return self.acceptable_actions or [self.expected_action]

    def tools_for_action(self, action: str) -> ExpectedTools:
        return next(
            (item for item in self.acceptable_tool_contracts if item.action == action),
            self.expected_tools,
        )

    def sut_input(self) -> dict[str, Any]:
        return {"turns": list(self.turns)}


@dataclass
class EvaluationRuntimeOutcome:
    case_id: str
    turn_index: int
    response: str
    route: dict[str, Any]
    risk_level: str
    action: str
    knowledge_requested: bool
    knowledge_used: bool
    retrieved_context_ids: list[str]
    retrieved_contexts: list[str]
    usable_context_ids: list[str]
    usable_contexts: list[str]
    trace_id: str
    turn_metrics: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    error_code: str | None = None
    tool_diagnostics: dict[str, Any] = field(default_factory=dict)
    work_item_outcomes: list[dict[str, Any]] = field(default_factory=list)
    actual_tools: list[str] = field(default_factory=list)
    prompt_context_ids: list[str] = field(default_factory=list)
    prompt_contexts: list[str] = field(default_factory=list)
    prompt_contexts_observed: bool = False
    action_reason_codes: list[str] = field(default_factory=list)
    infra_error_codes: list[str] = field(default_factory=list)
    business_status: str = "COMPLETED"
    upstream_error_codes: list[str] = field(default_factory=list)
    retrieval_observation: str = "NOT_OBSERVED"
    action_source: str = "RUNTIME_STATUS"
    specialist_results: list[dict[str, Any]] = field(default_factory=list)
    evidence_diagnostics: list[dict[str, Any]] = field(default_factory=list)


def _validate_route_shape(primary, intents, count, item_intents, missing, edges):
    if count != len(item_intents) or count != len(missing):
        raise ValueError("route arrays must match workItemCount")
    if primary != item_intents[0] or intents != list(dict.fromkeys(item_intents)):
        raise ValueError("route intent summary is inconsistent")
    for names in missing:
        if any(not name.strip() for name in names) or len(names) != len(set(names)):
            raise ValueError("missing argument names are invalid")
    _require_dag(count, edges)


def _require_dag(count: int, edges):
    parents = {index: set() for index in range(count)}
    seen = set()
    for source, target in edges:
        if min(source, target) < 0 or max(source, target) >= count or source == target or (source, target) in seen:
            raise ValueError("dependency edge is invalid")
        seen.add((source, target))
        parents[target].add(source)
    visiting = set()
    visited = set()
    def visit(node):
        if node in visiting:
            raise ValueError("dependency graph must be acyclic")
        if node in visited:
            return
        visiting.add(node)
        for parent in parents[node]:
            visit(parent)
        visiting.remove(node)
        visited.add(node)
    for node in parents:
        visit(node)
