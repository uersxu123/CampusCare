from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.enums import IntentType, MAX_INPUT_CHARS, MAX_WORK_ITEMS
from app.services.embedding import EmbeddingBackend, EmbeddingUnavailable, create_embedding_backend


class ContextRelation(str, Enum):
    NEW_TOPIC = "NEW_TOPIC"
    CONTINUE = "CONTINUE"
    REFINE = "REFINE"
    CORRECTION = "CORRECTION"
    AMBIGUOUS = "AMBIGUOUS"


RouteStatus = Literal["ROUTE", "TOO_MANY_WORK_ITEMS"]
DependencyRelation = Literal["HARD_DATA", "ORDER_ONLY"]
SegmentReasonCode = Literal[
    "EXPLICIT_SINGLE_GOAL",
    "MULTI_DELIVERABLE",
    "CROSS_INTENT",
    "SAME_INTENT_DISTINCT_GOAL",
    "CONTEXT_CONTINUATION",
    "HARD_DATA_DEPENDENCY",
    "ORDER_ONLY_REQUEST",
]
CandidateSource = Literal["RULE", "EMBEDDING", "LLM"]


class IntentSegmentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    sourceText: str = Field(min_length=1, max_length=MAX_INPUT_CHARS)
    intent: IntentType
    confidence: float = Field(ge=0.0, le=1.0, strict=True)
    reasonCodes: list[SegmentReasonCode] = Field(max_length=8)
    # Evidence facets are retrieval goals inside one business WorkItem.  They are
    # deliberately not additional WorkItems and are capped to keep Top-K coverage feasible.
    evidenceFacets: list[str] = Field(default_factory=list, max_length=3)

    @field_validator("sourceText")
    @classmethod
    def require_visible_source(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("sourceText 不能为空白")
        return value

    @field_validator("reasonCodes")
    @classmethod
    def require_unique_reasons(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("reasonCodes 不得重复")
        return value

    @field_validator("evidenceFacets")
    @classmethod
    def validate_evidence_facets(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("evidenceFacets 必须为字符串数组")
            item = item.strip()
            if not 1 <= len(item) <= 180:
                raise ValueError("evidenceFacet 长度无效")
            if item not in normalized:
                normalized.append(item)
        return normalized


class DependencyHint(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    sourceIndex: int = Field(ge=0, strict=True)
    targetIndex: int = Field(ge=0, strict=True)
    relation: DependencyRelation
    confidence: float = Field(ge=0.0, le=1.0, strict=True)
    reasonCode: Literal[
        "TARGET_USES_SOURCE_RESULT",
        "TARGET_USES_SOURCE_FACT",
        "TARGET_USES_SOURCE_CONSTRAINT",
        "USER_REQUESTED_ORDER_ONLY",
    ]


class UnderstandingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schemaVersion: Literal[3]
    routeStatus: RouteStatus
    contextRelation: ContextRelation
    segments: list[IntentSegmentDecision] = Field(max_length=MAX_WORK_ITEMS)
    dependencyHints: list[DependencyHint] = Field(max_length=6)

    @model_validator(mode="after")
    def validate_contract(self) -> "UnderstandingDecision":
        if self.routeStatus == "ROUTE" and not self.segments:
            raise ValueError("ROUTE 必须包含一到四个 segments")
        if self.routeStatus == "TOO_MANY_WORK_ITEMS":
            if self.segments or self.dependencyHints:
                raise ValueError("TOO_MANY_WORK_ITEMS 必须使用空 segments/dependencyHints")
            if self.contextRelation == ContextRelation.AMBIGUOUS:
                raise ValueError("容量结果不能同时为 AMBIGUOUS")
            return self
        if self.contextRelation == ContextRelation.AMBIGUOUS:
            if len(self.segments) != 1 or self.segments[0].intent != IntentType.CHAT or self.dependencyHints:
                raise ValueError("AMBIGUOUS 必须是单个 CHAT segment 且没有依赖")
        undirected: set[tuple[int, int]] = set()
        hard_reasons = {
            "TARGET_USES_SOURCE_RESULT", "TARGET_USES_SOURCE_FACT", "TARGET_USES_SOURCE_CONSTRAINT",
        }
        for hint in self.dependencyHints:
            if hint.sourceIndex >= len(self.segments) or hint.targetIndex >= len(self.segments):
                raise ValueError("dependencyHints 必须引用有效 segment")
            if hint.sourceIndex == hint.targetIndex:
                raise ValueError("dependencyHints 不能自引用")
            pair = tuple(sorted((hint.sourceIndex, hint.targetIndex)))
            if pair in undirected:
                raise ValueError("同一 segment 对只能声明一个依赖关系")
            undirected.add(pair)
            if hint.relation == "HARD_DATA" and hint.reasonCode not in hard_reasons:
                raise ValueError("HARD_DATA reasonCode 无效")
            if hint.relation == "ORDER_ONLY" and hint.reasonCode != "USER_REQUESTED_ORDER_ONLY":
                raise ValueError("ORDER_ONLY reasonCode 无效")
        return self


@dataclass(frozen=True)
class IntentCandidate:
    intent: IntentType
    confidence: float
    source: CandidateSource
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("IntentCandidate confidence 无效")
        if any(not isinstance(item, str) or not item for item in self.reason_codes):
            raise ValueError("IntentCandidate reason_codes 无效")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("IntentCandidate reason_codes 不得重复")


@dataclass(frozen=True)
class FusionResult:
    intent: IntentType
    confidence: float
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("FusionResult confidence 无效")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("FusionResult reason_codes 不得重复")


FUSION_WEIGHTS = {"LLM": 0.70, "EMBEDDING": 0.20, "RULE": 0.10}
MIN_FUSED_CONFIDENCE = 0.50
MIN_FUSED_MARGIN = 0.08
EMBEDDING_MIN_SIMILARITY = 0.55
EMBEDDING_MIN_MARGIN = 0.05
FUSION_CONSTANTS_VERSION = "five-intent-fusion-v3"

INTENT_EMBEDDING_TEMPLATES_V3: dict[IntentType, tuple[str, ...]] = {
    IntentType.CHAT: ("问候、寒暄、感谢和一般交流", "编程、代码、数据库和通用技术问题", "翻译、写作和非校园通用知识"),
    IntentType.ACADEMIC: ("课程学习、复习、考试和学习计划", "论文推进、研究和学业安排", "考研、就业、求职、实习和学业选择"),
    IntentType.CAMPUS: ("学校制度、奖学金、补考、重修和学籍规定", "宿舍、校园网、校园卡和校内办理流程", "校区、材料、资格、截止时间、入口和联系方式"),
    IntentType.MENTAL: ("压力、焦虑、失眠、低落和害怕", "情绪倾诉、安慰、陪伴和心理支持", "人际关系、考试或论文引起的非高风险困扰"),
    IntentType.RISK: ("当前自伤、自杀或不想活", "当前伤害他人或持有危险物品", "已经实施伤害、处于危险地点或即时危险"),
}
_TEMPLATE_VECTOR_CACHE: dict[tuple[str, str, str, str], dict[IntentType, list[list[float]]]] = {}
_TIE_ORDER = {IntentType.RISK: 0, IntentType.MENTAL: 1, IntentType.CAMPUS: 2, IntentType.ACADEMIC: 3, IntentType.CHAT: 4}


class IntentFusion:
    def __init__(self, settings: Any, backend: EmbeddingBackend | None = None):
        self.settings = settings
        self.backend = backend or create_embedding_backend(settings)
        self._available: bool | None = None
        self._templates: dict[IntentType, list[list[float]]] | None = None

    def fuse_five_intents(
        self,
        llm: IntentCandidate | None,
        embedding: IntentCandidate | None,
        rule: IntentCandidate | None,
    ) -> FusionResult:
        slots = {"LLM": llm, "EMBEDDING": embedding, "RULE": rule}
        for source, candidate in slots.items():
            if candidate is not None and candidate.source != source:
                raise ValueError("IntentCandidate source 与参数槽位不一致")
        if llm is None or llm.intent != IntentType.RISK:
            slots = {source: item for source, item in slots.items() if item is None or item.intent != IntentType.RISK}
        active = [(source, item) for source, item in slots.items() if item is not None]
        if not active:
            return FusionResult(IntentType.CHAT, 0.0, ("AMBIGUOUS_ROUTING",))
        total_weight = sum(FUSION_WEIGHTS[source] for source, _ in active)
        scores = {intent: 0.0 for intent in IntentType}
        reasons: dict[IntentType, list[str]] = {intent: [] for intent in IntentType}
        for source, candidate in active:
            scores[candidate.intent] += FUSION_WEIGHTS[source] / total_weight * candidate.confidence
            reasons[candidate.intent].extend(candidate.reason_codes)
        ordered = sorted(scores.items(), key=lambda item: (-item[1], _TIE_ORDER[item[0]]))
        (best_intent, best_score), (_, second_score) = ordered[:2]
        if best_score < MIN_FUSED_CONFIDENCE or best_score - second_score < MIN_FUSED_MARGIN:
            return FusionResult(IntentType.CHAT, best_score, ("AMBIGUOUS_ROUTING",))
        return FusionResult(best_intent, min(1.0, best_score), tuple(dict.fromkeys(reasons[best_intent])))

    def embedding_candidate_or_none(self, text: str) -> IntentCandidate | None:
        try:
            if self._available is None:
                self._available = self.backend.available()
            if not self._available:
                return None
            vectors = self._template_vectors()
            query = self.backend.embed_query(text)
            scores = {
                intent: max(_cosine(query, vector) for vector in rows)
                for intent, rows in vectors.items()
            }
            ordered = sorted(scores.items(), key=lambda item: (-item[1], _TIE_ORDER[item[0]]))
            (intent, similarity), (_, second) = ordered[:2]
            if similarity < EMBEDDING_MIN_SIMILARITY or similarity - second < EMBEDDING_MIN_MARGIN:
                return None
            return IntentCandidate(intent, max(0.0, min(1.0, (similarity + 1.0) / 2.0)), "EMBEDDING", ("EMBEDDING_SIGNAL",))
        except (EmbeddingUnavailable, httpx.HTTPError):
            return None

    def _template_vectors(self) -> dict[IntentType, list[list[float]]]:
        if self._templates is not None:
            return self._templates
        key = (self.backend.name, self.backend.model, self.backend.model_digest(), "five-intent-templates-v3")
        cached = _TEMPLATE_VECTOR_CACHE.get(key)
        if cached is None:
            texts = [text for values in INTENT_EMBEDDING_TEMPLATES_V3.values() for text in values]
            raw = self.backend.embed_documents(texts)
            cached = {}
            offset = 0
            dimension = 0
            for intent, values in INTENT_EMBEDDING_TEMPLATES_V3.items():
                rows = raw[offset:offset + len(values)]
                if len(rows) != len(values):
                    raise ValueError("Intent 模板向量数量不完整")
                for row in rows:
                    if not row:
                        raise ValueError("Intent 模板向量不能为空")
                    dimension = dimension or len(row)
                    if len(row) != dimension:
                        raise ValueError("Intent 模板向量维度不一致")
                cached[intent] = rows
                offset += len(values)
            if set(cached) != set(IntentType) or offset != len(raw):
                raise ValueError("Intent 模板缓存结构无效")
            _TEMPLATE_VECTOR_CACHE[key] = cached
        self._templates = cached
        return cached


def candidate_from_llm(segment: IntentSegmentDecision) -> IntentCandidate:
    return IntentCandidate(segment.intent, segment.confidence, "LLM", tuple(segment.reasonCodes))


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return -1.0
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
    return dot / norm if norm else -1.0
