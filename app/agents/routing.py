from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from app.core.config import Settings
from app.core.enums import IntentType, MAX_INPUT_CHARS, MAX_WORK_ITEMS, RouteReasonCode
from app.services.academic_request_policy import is_concrete_study_plan_request
from app.services.clarification_handlers import get_clarification_handler
from app.services.clarification_models import MissingArgument
from app.services.intent_fusion import ContextRelation, IntentCandidate, IntentFusion, UnderstandingDecision, candidate_from_llm
from app.services.routing_v5 import (
    FIXED_DEGRADED_CLARIFICATION,
    GlobalDegradedRouter,
    PlanningResultV5,
    PlanningResultV6,
    PlannedWorkItem,
    PlannerValidationError,
    PrimaryRoutingScorer,
    RoutingMode,
    RoutingScoreSnapshot,
    decide_fast_route,
    requires_planner,
    validate_planning_result,
    validate_planning_result_v6,
)
from app.services.risk_signals import analyze_risk_signal
from app.services.route_planning import (
    EXPECTED_SEMANTIC_PLANNING_ERRORS,
    AnchoredSegment,
    DependencyGraphs,
    PlanningDiagnostics,
    build_and_validate_rule_dependency_graphs,
    build_rule_route_draft,
    reconcile_segments,
    rule_segments,
    stable_topological_order,
    validate_and_build_accepted_dependency_graphs,
    validate_context_relation,
)
from app.services.understanding import UnderstandingInvocationResult


ROUTE_PLAN_V3_FIELDS = {"schemaVersion", "planId", "primaryIntent", "intents", "workItems", "synthesisOrder", "confidence", "reasonCodes"}
ROUTE_PLAN_V5_FIELDS = ROUTE_PLAN_V3_FIELDS | {"degraded", "routingMode", "degradedReason"}
ROUTE_PLAN_V6_FIELDS = ROUTE_PLAN_V5_FIELDS
WORK_ITEM_FIELDS = {"workItemId", "intent", "objective", "taskText", "sourceText", "sourceRefs", "contextRefs", "evidenceFacets", "knownArguments", "missingArguments", "dependsOn", "priority", "confidence", "reasonCodes"}
LEGACY_WORK_ITEM_FIELDS = WORK_ITEM_FIELDS - {"evidenceFacets"}
V5_WORK_ITEM_FIELDS = WORK_ITEM_FIELDS - {"taskText", "sourceRefs", "contextRefs"}
V5_LEGACY_WORK_ITEM_FIELDS = V5_WORK_ITEM_FIELDS - {"evidenceFacets"}
PUBLIC_REASON_CODES = {item.value for item in RouteReasonCode}
ControlKind = Literal["RISK", "LIMIT", "AMBIGUOUS"]
_SITE_SCOPED_CAMPUS_TERMS = ("调宿", "换宿舍", "换寝", "调寝", "宿舍调整", "退宿", "入住")


@dataclass(frozen=True)
class WorkItem:
    work_item_id: str
    intent: IntentType
    objective: str
    source_text: str
    task_text: str = ""
    source_refs: tuple[str, ...] = ()
    context_refs: tuple[str, ...] = ()
    evidence_facets: tuple[str, ...] = ()
    known_arguments: dict[str, str] = field(default_factory=dict)
    missing_arguments: tuple[MissingArgument, ...] = ()
    depends_on: tuple[str, ...] = ()
    priority: str = "NORMAL"
    confidence: float = 1.0
    reason_codes: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "WorkItem":
        if not isinstance(payload, dict) or set(payload) not in {frozenset(WORK_ITEM_FIELDS), frozenset(LEGACY_WORK_ITEM_FIELDS), frozenset(V5_WORK_ITEM_FIELDS), frozenset(V5_LEGACY_WORK_ITEM_FIELDS)}:
            raise ValueError("workItem 字段不完整或包含未知字段")
        for name in ("workItemId", "intent", "objective", "sourceText", "priority"):
            if not isinstance(payload[name], str):
                raise ValueError(f"workItem {name} 类型无效")
        try:
            intent = IntentType(payload["intent"])
        except ValueError as exc:
            raise ValueError("workItem intent 无效") from exc
        facets = payload.get("evidenceFacets", [])
        known, missing, depends, reasons = (payload[name] for name in ("knownArguments", "missingArguments", "dependsOn", "reasonCodes"))
        if not isinstance(known, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in known.items()):
            raise ValueError("knownArguments 必须为字符串字典")
        if not all(isinstance(value, list) for value in (facets, missing, depends, reasons)):
            raise ValueError("workItem 集合字段类型无效")
        if any(not isinstance(value, str) for value in (*facets, *depends, *reasons)):
            raise ValueError("workItem 字符串数组类型无效")
        if not isinstance(payload["confidence"], (int, float)) or isinstance(payload["confidence"], bool):
            raise ValueError("workItem confidence 类型无效")
        item = cls(
            work_item_id=payload["workItemId"],
            intent=intent,
            objective=payload["objective"],
            task_text=str(payload.get("taskText") or payload["sourceText"]),
            source_text=payload["sourceText"],
            source_refs=tuple(str(value) for value in payload.get("sourceRefs", ())),
            context_refs=tuple(str(value) for value in payload.get("contextRefs", ())),
            evidence_facets=tuple(str(value).strip() for value in facets),
            known_arguments=dict(known),
            missing_arguments=tuple(MissingArgument.from_payload(value) for value in missing),
            depends_on=tuple(depends),
            priority=payload["priority"],
            confidence=float(payload["confidence"]),
            reason_codes=tuple(reasons),
        )
        item.validate()
        return item

    def validate(self) -> None:
        if not isinstance(self.work_item_id, str) or not self.work_item_id.strip():
            raise ValueError("workItemId 不能为空")
        if not isinstance(self.objective, str) or not 1 <= len(self.objective.strip()) <= 240:
            raise ValueError("objective 无效")
        if not isinstance(self.source_text, str) or not 1 <= len(self.source_text.strip()) <= MAX_INPUT_CHARS:
            raise ValueError("sourceText 无效")
        if not isinstance(self.task_text, str) or not 1 <= len((self.task_text or self.source_text).strip()) <= 240:
            raise ValueError("taskText 无效")
        if len(self.evidence_facets) > 3 or len(self.evidence_facets) != len(set(self.evidence_facets)):
            raise ValueError("evidenceFacets 无效")
        if any(not isinstance(value, str) or not 1 <= len(value.strip()) <= 180 or value != value.strip() for value in self.evidence_facets):
            raise ValueError("evidenceFacets 边界无效")
        if self.priority not in {"NORMAL", "HIGH", "CRITICAL"}:
            raise ValueError("priority 无效")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence 无效")
        if len(self.known_arguments) > 32 or any(not 1 <= len(key.strip()) <= 64 or key != key.strip() or not 1 <= len(value) <= 500 for key, value in self.known_arguments.items()):
            raise ValueError("knownArguments 边界无效")
        if len(self.missing_arguments) > 10 or len({item.name for item in self.missing_arguments}) != len(self.missing_arguments):
            raise ValueError("missingArguments 无效")
        handler = get_clarification_handler(self.intent)
        if self.missing_arguments and (handler is None or tuple(handler.sanitize_missing(list(self.missing_arguments))) != self.missing_arguments):
            raise ValueError("workItem 包含未注册的澄清字段")
        if len(self.depends_on) > MAX_WORK_ITEMS or len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("dependsOn 无效")
        if len(self.reason_codes) > 16 or len(self.reason_codes) != len(set(self.reason_codes)) or not set(self.reason_codes).issubset(PUBLIC_REASON_CODES):
            raise ValueError("reasonCodes 无效")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "workItemId": self.work_item_id,
            "intent": self.intent.value,
            "objective": self.objective,
            "taskText": self.task_text or self.source_text,
            "sourceText": self.source_text,
            "sourceRefs": list(self.source_refs),
            "contextRefs": list(self.context_refs),
            "evidenceFacets": list(self.evidence_facets),
            "knownArguments": dict(self.known_arguments),
            "missingArguments": [item.as_payload() for item in self.missing_arguments],
            "dependsOn": list(self.depends_on),
            "priority": self.priority,
            "confidence": self.confidence,
            "reasonCodes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class RoutePlan:
    plan_id: str
    primary_intent: IntentType
    intents: tuple[IntentType, ...]
    work_items: tuple[WorkItem, ...]
    synthesis_order: tuple[str, ...]
    confidence: float
    reason_codes: tuple[str, ...]
    schema_version: int = 3
    degraded: bool = False
    routing_mode: RoutingMode = RoutingMode.NORMAL
    degraded_reason: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "RoutePlan":
        if not isinstance(payload, dict):
            raise ValueError("route_plan payload 无效")
        version = payload.get("schemaVersion")
        expected = ROUTE_PLAN_V6_FIELDS if version == 6 else ROUTE_PLAN_V5_FIELDS if version == 5 else ROUTE_PLAN_V3_FIELDS if version in {3, 4} else None
        if expected is None or set(payload) != expected:
            raise ValueError("route_plan 字段或 schemaVersion 无效")
        for name in ("planId", "primaryIntent"):
            if not isinstance(payload[name], str):
                raise ValueError(f"route_plan {name} 类型无效")
        for name in ("intents", "workItems", "synthesisOrder", "reasonCodes"):
            if not isinstance(payload[name], list):
                raise ValueError(f"route_plan {name} 类型无效")
        try:
            mode = RoutingMode(str(payload.get("routingMode", "NORMAL")))
            plan = cls(
                str(payload["planId"]), IntentType(str(payload["primaryIntent"])),
                tuple(IntentType(str(value)) for value in payload["intents"]),
                tuple(WorkItem.from_payload(value) for value in payload["workItems"]),
                tuple(str(value) for value in payload["synthesisOrder"]), float(payload["confidence"]),
                tuple(str(value) for value in payload["reasonCodes"]), int(version),
                bool(payload.get("degraded", False)), mode, payload.get("degradedReason") if isinstance(payload.get("degradedReason"), str) else None,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("route_plan 枚举或字段值无效") from exc
        plan.validate()
        return plan

    def validate(self) -> None:
        if self.schema_version not in {3, 4, 5, 6}:
            raise ValueError("route_plan schemaVersion 无效")
        if self.schema_version in {5, 6}:
            if self.routing_mode == RoutingMode.NORMAL and self.degraded:
                raise ValueError("NORMAL 不能 degraded=true")
            if self.routing_mode != RoutingMode.NORMAL and not self.degraded:
                raise ValueError("DEGRADED_* 必须 degraded=true")
        if not isinstance(self.plan_id, str) or not self.plan_id.strip() or not 1 <= len(self.work_items) <= MAX_WORK_ITEMS:
            raise ValueError("route_plan 标识或工作项数量无效")
        ids = [item.work_item_id for item in self.work_items]
        if len(ids) != len(set(ids)) or set(ids) != set(self.synthesis_order) or len(self.synthesis_order) != len(ids):
            raise ValueError("synthesisOrder 必须是工作项完整无重复全排列")
        position = {item_id: index for index, item_id in enumerate(self.synthesis_order)}
        by_id = {item.work_item_id: item for item in self.work_items}
        for item in self.work_items:
            if item.work_item_id in item.depends_on or not set(item.depends_on).issubset(by_id):
                raise ValueError("dependsOn 引用无效")
            if any(position[parent] >= position[item.work_item_id] for parent in item.depends_on):
                raise ValueError("synthesisOrder 不是依赖拓扑序")
        _validate_dag(self.work_items)
        ordered = [by_id[item_id] for item_id in self.synthesis_order]
        ordered_intents = tuple(dict.fromkeys(item.intent for item in ordered))
        if self.primary_intent != ordered[0].intent or self.intents != ordered_intents:
            raise ValueError("RoutePlan intent 摘要不一致")
        if self.schema_version in {5, 6} and any(item.intent == IntentType.RISK for item in ordered):
            raise ValueError("V5/V6 Router 不生成 RISK WorkItem")
        if not math.isfinite(self.confidence) or self.confidence != min(item.confidence for item in ordered):
            raise ValueError("RoutePlan confidence 必须等于工作项最小值")

    def validate_against_input(self, planning_input: str, *, control_kind: ControlKind | None = None) -> None:
        if not isinstance(planning_input, str) or not planning_input.strip() or len(planning_input) > MAX_INPUT_CHARS:
            raise ValueError("planning_input 无效")
        # V5 intentionally removes the old exact-anchor/full-coverage hard contract.
        if self.schema_version in {5, 6}:
            return
        if control_kind:
            if len(self.work_items) != 1 or self.work_items[0].source_text != planning_input:
                raise ValueError("控制 RoutePlan 必须覆盖完整 planning_input")
            return
        spans: list[tuple[int, int]] = []
        for item in self.work_items:
            starts = [match.start() for match in re.finditer(re.escape(item.source_text), planning_input)]
            if len(starts) != 1:
                raise ValueError("RoutePlan sourceText 锚定失败")
            spans.append((starts[0], starts[0] + len(item.source_text)))
        spans.sort()
        if any(spans[index][0] < spans[index - 1][1] for index in range(1, len(spans))):
            raise ValueError("RoutePlan sourceText 重叠")
        covered = [False] * len(planning_input)
        for start, end in spans:
            covered[start:end] = [True] * (end - start)
        remainder = "".join(char for index, char in enumerate(planning_input) if not covered[index])
        for term in ("请", "请问", "麻烦", "帮我", "另外", "同时", "以及", "并且", "然后", "再", "先", "最后"):
            remainder = remainder.replace(term, "")
        if re.sub(r"[\s，,、。！？；;：:（）()\"']+", "", remainder):
            raise ValueError("RoutePlan 未完整覆盖当前输入")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        payload = {
            "schemaVersion": self.schema_version,
            "planId": self.plan_id,
            "primaryIntent": self.primary_intent.value,
            "intents": [item.value for item in self.intents],
            "workItems": [item.as_payload() for item in self.work_items],
            "synthesisOrder": list(self.synthesis_order),
            "confidence": self.confidence,
            "reasonCodes": list(self.reason_codes),
        }
        if self.schema_version in {5, 6}:
            payload.update({
                "degraded": self.degraded,
                "routingMode": self.routing_mode.value,
                "degradedReason": self.degraded_reason,
            })
        return payload


@dataclass(frozen=True)
class RouteDecision:
    route_plan: RoutePlan | None
    diagnostics: PlanningDiagnostics
    fixed_response: str | None = None

    def as_payload(self) -> dict[str, object]:
        if self.route_plan is None:
            return {"schemaVersion": 5, "fixedResponse": self.fixed_response or FIXED_DEGRADED_CLARIFICATION}
        return self.route_plan.as_payload()


def classify_route(
    planning_input: str,
    memory_context: dict[str, Any] | None = None,
    semantic_classifier: Callable[[str, dict[str, Any]], UnderstandingInvocationResult] | None = None,
    settings: Any | None = None,
    raw_current_input: str | None = None,
    *,
    fusion: IntentFusion | Any | None = None,
    routing_scorer: PrimaryRoutingScorer | Any | None = None,
) -> RouteDecision:
    del fusion  # V5 normal path no longer uses legacy IntentFusion.
    if not isinstance(planning_input, str):
        raise TypeError("planning_input 必须是字符串")
    user_input = planning_input.strip()
    if not 1 <= len(user_input) <= MAX_INPUT_CHARS:
        raise ValueError("planning_input 长度无效")
    # raw_current_input is compatibility/audit-only. It must never override the routing input.
    del raw_current_input
    context = memory_context or {}
    settings = settings or Settings(_env_file=None)
    max_attempts = int(getattr(settings, "route_planner_max_attempts", 2))
    max_attempts = max(1, min(max_attempts, 2))
    last_error: Exception | None = None
    provider_attempts = 0
    planner_latency_ms = 0

    snapshot: RoutingScoreSnapshot | None = None
    scorer: PrimaryRoutingScorer | Any | None = None
    fast_reason = "DISABLED"
    if bool(getattr(settings, "route_fast_enabled", True)):
        # Cheap structural-risk gate comes first. Multi-goal, context-dependent,
        # dependency-heavy, or text-transform requests go straight to Planner
        # instead of letting topic keywords drive Rule+Embedding Fast Direct.
        if requires_planner(user_input, context):
            fast_reason = "COMPLEX_OR_CONTEXT_DEPENDENT"
        else:
            scorer = routing_scorer or PrimaryRoutingScorer(settings)
            snapshot = scorer.score(user_input)
            fast = decide_fast_route(user_input, snapshot, settings, context)
            fast_reason = fast.reason
            if fast.accepted and fast.intent is not None:
                result = PlanningResultV5(workItems=[
                    PlannedWorkItem(sourceText=user_input, intent=fast.intent.value, dependsOn=[])
                ])
                plan = _build_v5_normal_plan(user_input, context, result)
                diagnostics = PlanningDiagnostics(
                    "FAST_RULE_EMBEDDING", False, 0, 0, snapshot.latency_ms,
                    "FAST_SINGLE_INTENT", 0, 0, 1, 0, (), (), "", 0,
                    "NORMAL", None,
                    {intent.value: score for intent, score in snapshot.rule_scores.items()},
                    ({intent.value: score for intent, score in snapshot.embedding_scores.items()}
                     if snapshot.embedding_scores is not None else None),
                    {intent.value: score for intent, score in snapshot.scores.items()},
                    fast.reason,
                    snapshot.latency_ms,
                )
                return RouteDecision(plan, diagnostics)

    for _attempt in range(max_attempts):
        try:
            if semantic_classifier is None:
                from app.services.route_planning import SemanticPlannerUnavailable
                raise SemanticPlannerUnavailable("semantic classifier is unavailable")
            invocation = semantic_classifier(user_input, context)
            if not isinstance(invocation, UnderstandingInvocationResult):
                raise TypeError("semantic_classifier 必须返回 UnderstandingInvocationResult")
            provider_attempts += invocation.provider_attempt_count
            planner_latency_ms += invocation.latency_ms
            result = invocation.decision
            if isinstance(result, PlanningResultV6):
                validate_planning_result_v6(result, {"current:0"})
            elif isinstance(result, PlanningResultV5):
                validate_planning_result(result)
            else:
                raise PlannerValidationError("planner 必须返回 PlanningResultV6 或兼容 V5")
            if len(result.workItems) > MAX_WORK_ITEMS:
                return _v5_limit_route(user_input, context, provider_attempts, planner_latency_ms)
            plan = _build_v5_normal_plan(user_input, context, result)
            score_latency = snapshot.latency_ms if snapshot is not None else 0
            diagnostics = PlanningDiagnostics(
                "LLM_ACCEPTED", True, _attempt + 1, provider_attempts, planner_latency_ms + score_latency,
                "CONTEXT_AWARE", 0, len(result.workItems), len(result.workItems),
                sum(len(item.dependsOn) for item in result.workItems), (), (), "", 0,
                "NORMAL", None,
                ({intent.value: score for intent, score in snapshot.rule_scores.items()} if snapshot is not None else None),
                ({intent.value: score for intent, score in snapshot.embedding_scores.items()}
                 if snapshot is not None and snapshot.embedding_scores is not None else None),
                ({intent.value: score for intent, score in snapshot.scores.items()} if snapshot is not None else None),
                fast_reason,
                score_latency,
            )
            return RouteDecision(plan, diagnostics)
        except (EXPECTED_SEMANTIC_PLANNING_ERRORS + (PlannerValidationError,)) as exc:
            last_error = exc
            provider_attempts += 0 if isinstance(exc, PlannerValidationError) else getattr(exc, "provider_attempt_count", 0)
            planner_latency_ms += 0 if isinstance(exc, PlannerValidationError) else getattr(exc, "latency_ms", 0)
            continue

    # If the cheap pre-gate skipped embedding, only pay scorer cost now after
    # the Planner has actually failed. This preserves degraded recovery without
    # taxing successful complex requests.
    if snapshot is None:
        scorer = scorer or routing_scorer or PrimaryRoutingScorer(settings)
        snapshot = scorer.score(user_input)
    degraded_router = GlobalDegradedRouter(settings)
    degraded = degraded_router.route(user_input, snapshot=snapshot)
    fallback_reason = getattr(
        last_error,
        "fallback_reason",
        "PLAN_VALIDATION_FAILED" if isinstance(last_error, PlannerValidationError) else "MODEL_UNAVAILABLE",
    )
    score_latency = snapshot.latency_ms if snapshot is not None else 0
    diagnostics = PlanningDiagnostics(
        "RULE_FALLBACK", True, max_attempts, provider_attempts, planner_latency_ms + score_latency,
        "GLOBAL_DEGRADED", 0, 0, len(degraded.selected), 0, (), (), fallback_reason, 0,
        degraded.mode.value if degraded.mode is not None else "DEGRADED_NO_MATCH",
        {intent.value: score for intent, score in degraded.scores.items()},
        {intent.value: score for intent, score in degraded.rule_scores.items()},
        ({intent.value: score for intent, score in degraded.embedding_scores.items()}
         if degraded.embedding_scores is not None else None),
        {intent.value: score for intent, score in degraded.scores.items()},
        fast_reason,
        score_latency,
    )
    if not degraded.selected:
        return RouteDecision(None, diagnostics, FIXED_DEGRADED_CLARIFICATION)
    plan = _build_v5_degraded_plan(user_input, context, degraded.selected, degraded.scores, degraded.mode, fallback_reason)
    return RouteDecision(plan, diagnostics)

def _build_v5_normal_plan(text: str, context: dict[str, Any], result: PlanningResultV5 | PlanningResultV6) -> RoutePlan:
    plan_id = _v5_plan_id(text, context, "NORMAL")
    ids = [f"W{i + 1}" for i in range(len(result.workItems))]
    items: list[WorkItem] = []
    for index, planned in enumerate(result.workItems):
        intent = IntentType(planned.intent)
        source_text = planned.sourceText if hasattr(planned, "sourceText") else text
        task_text = planned.taskText if hasattr(planned, "taskText") else source_text
        objective = planned.objective if hasattr(planned, "objective") else f"处理当前 {intent.value} 领域用户目标"
        known, missing = extract_work_item_arguments(intent, task_text)
        items.append(WorkItem(
            work_item_id=ids[index], intent=intent,
            objective=objective[:240], task_text=task_text, source_text=source_text,
            source_refs=tuple(getattr(planned, "sourceRefs", ())), context_refs=tuple(getattr(planned, "contextRefs", ())),
            evidence_facets=(), known_arguments=known, missing_arguments=missing,
            depends_on=tuple(ids[parent] for parent in planned.dependsOn),
            priority="NORMAL", confidence=1.0, reason_codes=(_default_reason(intent),),
        ))
    plan = RoutePlan(
        plan_id, items[0].intent, tuple(dict.fromkeys(item.intent for item in items)), tuple(items), tuple(ids),
        1.0, tuple(dict.fromkeys(code for item in items for code in item.reason_codes)),
        schema_version=6 if isinstance(result, PlanningResultV6) else 5,
        degraded=False, routing_mode=RoutingMode.NORMAL, degraded_reason=None,
    )
    plan.validate()
    return plan


def _build_v5_degraded_plan(text: str, context: dict[str, Any], intents: tuple[IntentType, ...], scores: dict[IntentType, float], mode: RoutingMode | None, reason: str) -> RoutePlan:
    if mode is None:
        raise ValueError("degraded plan requires direct or broadcast mode")
    plan_id = _v5_plan_id(text, context, mode.value)
    items: list[WorkItem] = []
    for index, intent in enumerate(intents):
        item_id = f"D{index + 1}"
        items.append(WorkItem(
            work_item_id=item_id, intent=intent,
            objective=f"降级模式下处理当前 {intent.value} 领域目标：{text}"[:240],
            source_text=text, evidence_facets=(), known_arguments={}, missing_arguments=(), depends_on=(),
            priority="NORMAL", confidence=float(scores[intent]), reason_codes=("SEMANTIC_FALLBACK", _default_reason(intent)),
        ))
    return RoutePlan(
        plan_id, items[0].intent, tuple(dict.fromkeys(item.intent for item in items)), tuple(items), tuple(item.work_item_id for item in items),
        min(item.confidence for item in items), tuple(dict.fromkeys(code for item in items for code in item.reason_codes)),
        schema_version=5, degraded=True, routing_mode=mode, degraded_reason=reason,
    )


def _v5_limit_route(text: str, context: dict[str, Any], attempts: int, latency_ms: int) -> RouteDecision:
    del context
    diagnostics = PlanningDiagnostics(
        "LLM_LIMIT", True, 1, attempts, latency_ms, "CONTEXT_AWARE",
        0, MAX_WORK_ITEMS + 1, 0, 0, (), (), "", 0,
    )
    return RouteDecision(
        None,
        diagnostics,
        f"一次最多处理 {MAX_WORK_ITEMS} 个独立目标。请先保留最想解决的几个问题，再分批发送。",
    )


def _v5_plan_id(text: str, context: dict[str, Any], mode: str) -> str:
    seed = json.dumps({"v": 5, "text": text, "mode": mode, "goal": context.get("current_goal"), "topics": context.get("active_topics", [])}, ensure_ascii=False, sort_keys=True, default=str)
    return "plan_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def explicit_rule_candidate_or_none(text: str) -> IntentCandidate | None:
    if any(term in text for term in ("压力测试", "压测")) or (any(term in text for term in ("服务器", "接口", "Python", "程序")) and any(term in text for term in ("脚本", "崩溃", "排查", "结果"))):
        return IntentCandidate(IntentType.CHAT, 0.98, "RULE", ("TECHNICAL_CONTEXT",))
    normalized = text.strip(" \t\r\n，,。！？!?.")
    if normalized in {"你好", "您好", "嗨", "早上好", "下午好", "晚上好", "谢谢", "再见"}:
        return IntentCandidate(IntentType.CHAT, 0.96, "RULE", ("GENERAL_CHAT",))
    planning = any(term in text for term in ("计划", "规划", "安排"))
    campus_fact = any(term in text for term in ("材料", "流程", "资格", "截止", "政策", "规定", "补考", "重修", "办理", "校区", "预约", *_SITE_SCOPED_CAMPUS_TERMS))
    campus = any(term in text for term in ("申请", "办理", "材料", "流程", "资格", "截止", "政策", "规定", "补考", "重修", "奖学金", "校区", "校园网", "校园卡", "心理中心", "预约", *_SITE_SCOPED_CAMPUS_TERMS)) and (not planning or campus_fact)
    mental = any(term in text for term in ("焦虑", "睡不着", "失眠", "难受", "低落", "压力", "情绪", "想聊聊", "害怕", "恐惧", "担心"))
    academic = any(term in text for term in ("学习", "复习", "考试", "课程", "论文", "考研", "就业", "实习", "求职", "计划", "规划", "安排"))
    if planning and academic:
        campus = False
    matched = [(IntentType.ACADEMIC, 0.92), (IntentType.CAMPUS, 0.94), (IntentType.MENTAL, 0.94)]
    active = [(intent, confidence) for (intent, confidence), hit in zip(matched, (academic, campus, mental)) if hit]
    if len(active) != 1:
        return None
    intent, confidence = active[0]
    return IntentCandidate(intent, confidence, "RULE", (_default_reason(intent),))


def extract_work_item_arguments(intent: IntentType, source_text: str) -> tuple[dict[str, str], tuple[MissingArgument, ...]]:
    known: dict[str, str] = {}
    missing: list[MissingArgument] = []
    if intent == IntentType.CAMPUS:
        if "国家奖学金" in source_text:
            known["awardName"] = "国家奖学金"
            student_type = next((value for value in ("本科生", "研究生") if value in source_text), "")
            if student_type:
                known["studentType"] = student_type
            else:
                missing.append(MissingArgument("studentType", "POLICY_SCOPE_REQUIRED", ("本科生", "研究生")))
        for site in ("南望山校区", "未来城校区"):
            if site in source_text or site.removesuffix("校区") in source_text:
                known["site"] = site
        if any(term in source_text for term in _SITE_SCOPED_CAMPUS_TERMS) and "site" not in known:
            missing.append(MissingArgument("site", "SITE_SCOPE_REQUIRED", ("南望山校区", "未来城校区")))
    elif intent == IntentType.ACADEMIC and is_concrete_study_plan_request(source_text):
        known, plan_missing = _study_plan_arguments(source_text)
        missing.extend(plan_missing)
    handler = get_clarification_handler(intent)
    return known, tuple(handler.sanitize_missing(missing) if handler else ())


def build_work_item_objective(intent: IntentType, source_text: str) -> str:
    fallback = {IntentType.CHAT: "回应通用对话请求", IntentType.ACADEMIC: "处理学业支持请求", IntentType.CAMPUS: "核验校园制度或办理事实", IntentType.MENTAL: "提供非高风险心理支持", IntentType.RISK: "处理当前高风险请求"}[intent]
    value = source_text.strip("，,；;。 ")
    return value[:240] if value else fallback


def _build_plan(plan_id: str, items: tuple[WorkItem, ...]) -> RoutePlan:
    reasons = list(dict.fromkeys(code for item in items for code in item.reason_codes))
    if len(items) > 1:
        reasons.append("COMPOUND_REQUEST")
    if any(item.depends_on for item in items):
        reasons.append("DEPENDENCY_DETECTED")
    plan = RoutePlan(plan_id, items[0].intent, tuple(dict.fromkeys(item.intent for item in items)), items, tuple(item.work_item_id for item in items), min(item.confidence for item in items), tuple(dict.fromkeys(reasons)))
    plan.validate()
    return plan


def _control_route(
    text: str, context: dict[str, Any], relation: ContextRelation, intent: IntentType, objective: str, confidence: float,
    reason: str, control: ControlKind, source: str, draft=None, invocation=None, error=None, *, accepted_count: int = 1,
    schema_segment_count: int = 0, schema_hint_count: int = 0,
) -> RouteDecision:
    plan_id = _plan_id(text, context, relation)
    segment = AnchoredSegment(text, 0, len(text), 0)
    item = WorkItem(
        work_item_id=_work_item_id(plan_id, segment, intent),
        intent=intent,
        objective=objective,
        source_text=text,
        evidence_facets=(),
        priority="CRITICAL" if intent == IntentType.RISK else "NORMAL",
        confidence=confidence,
        reason_codes=(reason,),
    )
    plan = _build_plan(plan_id, (item,))
    plan.validate_against_input(text, control_kind=control)
    attempts = invocation.provider_attempt_count if invocation else getattr(error, "provider_attempt_count", 0)
    latency = invocation.latency_ms if invocation else getattr(error, "latency_ms", 0)
    normal_llm = source != "HIGH_RISK_HARD"
    diagnostics = PlanningDiagnostics(
        source, normal_llm, 1 if normal_llm else 0, attempts, latency, relation.value,
        len(draft.segments) if draft else 0,
        schema_segment_count or (len(invocation.decision.segments) if invocation else 0), accepted_count,
        schema_hint_count or (len(invocation.decision.dependencyHints) if invocation else 0), (), (),
        getattr(error, "fallback_reason", "") if error else "",
    )
    return RouteDecision(plan, diagnostics)


def _context_anchor(context: dict[str, Any], relation: ContextRelation) -> str:
    if relation == ContextRelation.NEW_TOPIC:
        return ""
    goal = context.get("current_goal")
    goal_text = goal.get("text") if isinstance(goal, dict) else goal if isinstance(goal, str) else ""
    topics = sorted(set(item.strip() for item in context.get("active_topics", []) if isinstance(item, str) and item.strip()))
    return json.dumps({"activeTopics": topics, "currentGoal": str(goal_text or "")}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _plan_id(text: str, context: dict[str, Any], relation: ContextRelation) -> str:
    seed = "\x1f".join(("five-intent-route-v3", text, relation.value, _context_anchor(context, relation)))
    return "plan_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def _work_item_id(plan_id: str, segment: AnchoredSegment, intent: IntentType) -> str:
    seed = "\x1f".join((plan_id, str(segment.start), str(segment.end), intent.value))
    return "wi_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def _default_reason(intent: IntentType) -> str:
    return {IntentType.CHAT: "GENERAL_CHAT", IntentType.ACADEMIC: "ACADEMIC_SIGNAL", IntentType.CAMPUS: "CAMPUS_SERVICE_SIGNAL", IntentType.MENTAL: "MENTAL_HEALTH_SIGNAL", IntentType.RISK: "HIGH_RISK_SIGNAL"}[intent]


def _diagnostic_edges(ordered: tuple[AnchoredSegment, ...], graphs: DependencyGraphs) -> tuple[tuple[tuple[int, int], ...], tuple[tuple[int, int], ...]]:
    positions = {item.key: index for index, item in enumerate(ordered)}
    hard = tuple(sorted((positions[source], positions[target]) for source, target in graphs.execution.edges))
    hard_pairs = set(graphs.execution.edges)
    order = tuple(sorted((positions[source], positions[target]) for source, target in graphs.presentation.edges if (source, target) not in hard_pairs))
    return hard, order


def _validate_dag(items: tuple[WorkItem, ...]) -> None:
    dependencies = {item.work_item_id: set(item.depends_on) for item in items}
    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(node: str) -> None:
        if node in visiting:
            raise ValueError("workItem 依赖图不能包含环")
        if node in visited:
            return
        visiting.add(node)
        for parent in dependencies[node]:
            visit(parent)
        visiting.remove(node)
        visited.add(node)
    for node in dependencies:
        visit(node)


_STUDY_DATE_PATTERN = re.compile(r"(?<!\d)(?P<month>\d{1,2})\s*(?:月|[./-])\s*(?P<day>\d{1,2})\s*日?(?:[^0-9]{0,8}(?P<hour>\d{1,2})\s*[:：]\s*(?P<minute>\d{2}))?")
_STUDY_COURSE_NOISE = ("截止时间", "截止日期", "制定", "学习计划", "复习计划", "安排")


def _study_plan_arguments(text: str) -> tuple[dict[str, str], tuple[MissingArgument, ...]]:
    known: dict[str, str] = {}
    course_match = re.search(r"(?:课程|科目|学科)(?:名称)?\s*(?:是|为|有|包括)?\s*([^，,；;。]+)", text)
    if course_match:
        course = course_match.group(1).strip(" ，,；;。")
        if course and not any(term in course for term in _STUDY_COURSE_NOISE):
            known["course"] = course[:200]
    deadline = _extract_study_deadline(text)
    if deadline:
        known["deadline"] = deadline
    missing = []
    if "course" not in known:
        missing.append(MissingArgument("course", "COURSE_SCOPE_REQUIRED"))
    if "deadline" not in known:
        missing.append(MissingArgument("deadline", "TARGET_DATE_REQUIRED"))
    return known, tuple(missing)


def _extract_study_deadline(text: str) -> str:
    match = _STUDY_DATE_PATTERN.search(text)
    if not match:
        return ""
    month, day = int(match.group("month")), int(match.group("day"))
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return ""
    hour, minute = match.group("hour"), match.group("minute")
    if hour is None:
        return f"{month}月{day}日"
    if not 0 <= int(hour) <= 23 or not 0 <= int(minute) <= 59:
        return ""
    return f"{month}月{day}日 {int(hour):02d}:{int(minute):02d}"
