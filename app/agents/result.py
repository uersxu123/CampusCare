from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.enums import IntentType, RiskLevel
from app.schemas.dtos import AiMessage
from app.services.assessment import PsychologyAssessment


class SkillCandidateDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    skillId: str
    group: str | None = None
    rulePoints: int = Field(ge=0, le=100)
    ruleScore: float = Field(ge=0.0, le=1.0)
    matchedRuleIds: list[str] = Field(default_factory=list, max_length=64)
    excluded: bool = False
    excludeRuleIds: list[str] = Field(default_factory=list, max_length=32)


class SkillGroupDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    group: str
    decision: str
    selectedSkillId: str | None = None
    highSkillIds: list[str] = Field(default_factory=list, max_length=32)
    nearSkillIds: list[str] = Field(default_factory=list, max_length=32)
    ruleMarginPoints: int | None = Field(default=None, ge=0, le=100)
    semanticScores: dict[str, float] = Field(default_factory=dict)
    semanticMargin: float | None = Field(default=None, ge=-2.0, le=2.0)
    embeddingCalled: bool = False
    embeddingCallCount: int = Field(default=0, ge=0, le=16)
    cacheHits: int = Field(default=0, ge=0, le=64)
    elapsedMs: int = Field(default=0, ge=0)


class SkillSelectionDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    selectorVersion: str
    workItemId: str = ""
    eligibleSkillIds: list[str] = Field(default_factory=list, max_length=64)
    candidates: list[SkillCandidateDiagnostic] = Field(default_factory=list, max_length=64)
    groups: list[SkillGroupDiagnostic] = Field(default_factory=list, max_length=32)
    qualifiedSkillIds: list[str] = Field(default_factory=list, max_length=32)
    injectedSkillIds: list[str] = Field(default_factory=list, max_length=32)
    budgetRejectedSkillIds: list[str] = Field(default_factory=list, max_length=32)


class SpecialistResultV2(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    # V2 remains readable for historical traces.  V3 is the context-workitems
    # contract and is the first version where answerStatus is authoritative.
    schemaVersion: Literal[2, 3]
    planId: str = Field(min_length=1)
    workItemId: str = Field(min_length=1)
    intent: IntentType
    agentName: str = Field(min_length=1)
    status: str
    objective: str = Field(min_length=1)
    knownArguments: dict[str, str]
    answerBrief: str = Field(min_length=1)
    answerStatus: Literal["FULL", "PARTIAL", "NONE", "NOT_REQUIRED"] | None = None
    evidenceNotes: list[dict[str, Any]] = Field(default_factory=list)
    missingInfo: list[str] = Field(default_factory=list)
    keyPoints: list[str]
    evidenceItems: list[dict[str, Any]]
    citationRefs: list[str]
    answerConstraints: list[str]
    assumptions: list[str]
    reasonCode: str = Field(min_length=1)
    selectedSkillIds: list[str]
    skillSelection: SkillSelectionDiagnostic | None = None
    toolSummary: dict[str, Any]
    dependencyResultIds: list[str]
    contextManifest: dict[str, Any]
    confidence: float = Field(ge=0.0, le=1.0, strict=True)

    @model_validator(mode="after")
    def validate_answer_contract(self) -> "SpecialistResultV2":
        if (
            self.schemaVersion == 3
            and self.status == "COMPLETED"
            and self.reasonCode != "OUT_OF_SCOPE_SKIPPED"
            and self.answerStatus is None
        ):
            raise ValueError("V3 specialist_result 必须包含合法 answerStatus")
        if self.answerStatus == "FULL" and self.missingInfo:
            raise ValueError("answerStatus=FULL 不能同时包含 missingInfo")
        return self

    @field_validator("status")
    @classmethod
    def require_status(cls, value: str) -> str:
        if value not in {"COMPLETED", "PARTIAL", "FAILED"}:
            raise ValueError("specialist_result status 无效")
        return value

    @field_validator(
        "keyPoints", "citationRefs", "answerConstraints", "assumptions", "selectedSkillIds", "dependencyResultIds", "missingInfo"
    )
    @classmethod
    def normalize_string_arrays(cls, value: list[str]) -> list[str]:
        if any(not isinstance(item, str) for item in value):
            raise ValueError("specialist_result 字符串数组无效")
        return list(dict.fromkeys(value))

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "SpecialistResultV2":
        return cls.model_validate(payload)

    def as_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


@dataclass
class AgentStep:
    step: int
    agent: str
    action: str
    observation: str


@dataclass
class AgentRunResult:
    primary_intent: IntentType
    intents: tuple[IntentType, ...]
    risk_level: RiskLevel
    assessment: PsychologyAssessment | None
    response_messages: list[AiMessage]
    steps: list[AgentStep]
    memory_brief: str
    route_plan: dict[str, Any]
    specialist_results: list[dict[str, Any]]
    evidence_items: list[dict[str, Any]]
    tool_diagnostics: dict[str, Any]
    route_diagnostics: dict[str, Any] = field(default_factory=dict)
    collaboration_events: list[Any] = field(default_factory=list)
    collaboration_tasks: list[Any] = field(default_factory=list)
    collaboration_artifacts: list[Any] = field(default_factory=list)
    clarification_request: dict[str, Any] | None = None
    context_manifest: dict[str, Any] = field(default_factory=dict)
    direct_response: str = ""
    response_evidence_items: list[dict[str, Any]] = field(default_factory=list)
    response_generation_diagnostics: dict[str, Any] = field(default_factory=dict)
    business_status: Literal["COMPLETED", "PARTIAL", "FAILED"] = "COMPLETED"
    business_error_codes: tuple[str, ...] = ()

    @property
    def requires_report(self) -> bool:
        return self.primary_intent in {IntentType.MENTAL, IntentType.RISK} or IntentType.MENTAL in self.intents
