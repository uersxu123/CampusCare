from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Literal, Sequence

from app.services.intent_fusion import DependencyHint, IntentCandidate, UnderstandingDecision


ACTION_TERMS = ("查", "查询", "核对", "确认", "告诉", "解释", "比较", "分析", "制定", "安排", "规划", "申请", "办理", "评估", "建议", "总结")
COMPOUND_CONNECTORS = ("另外", "同时", "还要", "还想", "也想", "以及", "并且", "分别", "比较后")
DEPENDENCY_REFERENCES = ("根据结果", "根据这个结果", "根据日期", "根据条件", "根据要求", "基于结果", "用查到的", "确认后", "核验后")
CLAUSE_SEPARATORS = r"[，,、。！？；;\n]+"
IGNORABLE_REMAINDER_TERMS = ("请", "请问", "麻烦", "麻烦你", "帮我", "可以吗", "谢谢", "你好", "您好", "另外", "同时", "还有", "还要", "也", "以及", "并且", "然后", "再", "先", "最后")
HARD_DATA_MIN_CONFIDENCE = 0.82

DependencyRelation = Literal["HARD_DATA", "ORDER_ONLY"]
DependencyReasonCode = Literal["TARGET_USES_SOURCE_RESULT", "TARGET_USES_SOURCE_FACT", "TARGET_USES_SOURCE_CONSTRAINT", "USER_REQUESTED_ORDER_ONLY"]
PlanSource = Literal["HIGH_RISK_HARD", "LLM_ACCEPTED", "LLM_LIMIT", "RULE_FALLBACK", "RULE_LIMIT_FALLBACK", "FAST_RULE_EMBEDDING"]
FallbackReason = Literal["", "MODEL_UNAVAILABLE", "MODEL_TIMEOUT", "STRUCTURED_OUTPUT_INVALID", "CONTEXT_RELATION_INVALID", "SEGMENT_ANCHOR_FAILED", "SEGMENT_COVERAGE_FAILED", "SEGMENT_OVERLAP", "DEPENDENCY_GRAPH_INVALID", "PLAN_VALIDATION_FAILED"]


class ExpectedSemanticPlanningError(RuntimeError):
    fallback_reason: str = ""

    def __init__(self, message: str, provider_attempt_count: int = 0, latency_ms: int = 0):
        super().__init__(message)
        self.provider_attempt_count = provider_attempt_count
        self.latency_ms = latency_ms


class SemanticPlannerUnavailable(ExpectedSemanticPlanningError):
    fallback_reason = "MODEL_UNAVAILABLE"


class SemanticPlannerTimeout(ExpectedSemanticPlanningError):
    fallback_reason = "MODEL_TIMEOUT"


class SemanticStructuredOutputInvalid(ExpectedSemanticPlanningError):
    fallback_reason = "STRUCTURED_OUTPUT_INVALID"


class SemanticContextRelationInvalid(ExpectedSemanticPlanningError):
    fallback_reason = "CONTEXT_RELATION_INVALID"


class SegmentAnchorFailed(ExpectedSemanticPlanningError):
    fallback_reason = "SEGMENT_ANCHOR_FAILED"


class SegmentCoverageFailed(ExpectedSemanticPlanningError):
    fallback_reason = "SEGMENT_COVERAGE_FAILED"


class SegmentOverlapOrDuplicate(ExpectedSemanticPlanningError):
    fallback_reason = "SEGMENT_OVERLAP"


class SemanticDependencyGraphInvalid(ExpectedSemanticPlanningError):
    fallback_reason = "DEPENDENCY_GRAPH_INVALID"


EXPECTED_SEMANTIC_PLANNING_ERRORS = (
    SemanticPlannerUnavailable,
    SemanticPlannerTimeout,
    SemanticStructuredOutputInvalid,
    SemanticContextRelationInvalid,
    SegmentAnchorFailed,
    SegmentCoverageFailed,
    SegmentOverlapOrDuplicate,
    SemanticDependencyGraphInvalid,
)


@dataclass(frozen=True)
class RuleSegmentDraft:
    source_text: str
    start: int
    end: int
    rule_candidate: IntentCandidate | None

    @property
    def key(self) -> tuple[int, int]:
        return self.start, self.end


@dataclass(frozen=True)
class RuleDependencyDraft:
    source_span: tuple[int, int]
    target_span: tuple[int, int]
    relation: DependencyRelation
    reason_code: DependencyReasonCode


@dataclass(frozen=True)
class RuleRouteDraft:
    segments: tuple[RuleSegmentDraft, ...]
    action_spans: tuple[tuple[int, int], ...]
    dependencies: tuple[RuleDependencyDraft, ...]
    limit_exceeded: bool = False


@dataclass(frozen=True)
class AnchoredSegment:
    source_text: str
    start: int
    end: int
    original_index: int

    @property
    def key(self) -> tuple[int, int]:
        return self.start, self.end


@dataclass(frozen=True)
class DependencyGraph:
    nodes: tuple[tuple[int, int], ...]
    edges: tuple[tuple[tuple[int, int], tuple[int, int]], ...] = ()

    def parents(self, node: tuple[int, int]) -> tuple[tuple[int, int], ...]:
        return tuple(source for source, target in self.edges if target == node)

    def with_edge(self, source: tuple[int, int], target: tuple[int, int]) -> "DependencyGraph":
        edge = (source, target)
        return self if edge in self.edges else DependencyGraph(self.nodes, (*self.edges, edge))


@dataclass(frozen=True)
class DependencyGraphs:
    execution: DependencyGraph
    presentation: DependencyGraph


@dataclass(frozen=True)
class PlanningDiagnostics:
    plan_source: PlanSource
    llm_invoked: bool
    logical_invocation_count: int
    provider_attempt_count: int
    latency_ms: int
    context_relation: str
    rule_segment_count: int
    llm_segment_count: int
    accepted_segment_count: int
    dependency_hint_count: int
    hard_data_edges: tuple[tuple[int, int], ...]
    order_only_edges: tuple[tuple[int, int], ...]
    fallback_reason: FallbackReason
    low_confidence_dependency_hint_count: int = 0
    routing_mode: str = "NORMAL"
    degraded_scores: dict[str, float] | None = None
    rule_scores: dict[str, float | None] | None = None
    embedding_scores: dict[str, float] | None = None
    routing_scores: dict[str, float] | None = None
    fast_route_reason: str = ""
    routing_score_latency_ms: int = 0

    def as_metadata(self) -> dict[str, object]:
        metadata = {
            "planSource": self.plan_source,
            "llmInvoked": self.llm_invoked,
            "logicalInvocationCount": self.logical_invocation_count,
            "providerAttemptCount": self.provider_attempt_count,
            "latencyMs": self.latency_ms,
            "contextRelation": self.context_relation,
            "ruleSegmentCount": self.rule_segment_count,
            "llmSegmentCount": self.llm_segment_count,
            "acceptedSegmentCount": self.accepted_segment_count,
            "dependencyHintCount": self.dependency_hint_count,
            "hardDataEdgeCount": len(self.hard_data_edges),
            "orderOnlyEdgeCount": len(self.order_only_edges),
            "hardDataEdges": [list(edge) for edge in self.hard_data_edges],
            "orderOnlyEdges": [list(edge) for edge in self.order_only_edges],
            "fallbackReason": self.fallback_reason,
            "routingMode": self.routing_mode,
        }
        if self.degraded_scores is not None:
            metadata["degradedScores"] = dict(self.degraded_scores)
        if self.rule_scores is not None:
            metadata["ruleScores"] = dict(self.rule_scores)
        if self.embedding_scores is not None:
            metadata["embeddingScores"] = dict(self.embedding_scores)
        if self.routing_scores is not None:
            metadata["routingScores"] = dict(self.routing_scores)
        if self.fast_route_reason:
            metadata["fastRouteReason"] = self.fast_route_reason
        if self.routing_score_latency_ms:
            metadata["routingScoreLatencyMs"] = int(self.routing_score_latency_ms)
        return metadata


RuleClassifier = Callable[[str], IntentCandidate | None]


def build_rule_route_draft(planning_input: str, classifier: RuleClassifier, *, limit: int) -> RuleRouteDraft:
    spans = _action_spans(planning_input)
    segments = tuple(RuleSegmentDraft(planning_input[start:end], start, end, classifier(planning_input[start:end])) for start, end in spans)
    dependencies: list[RuleDependencyDraft] = []
    for index in range(1, len(spans)):
        source_span, target_span = spans[index - 1], spans[index]
        between = planning_input[source_span[1]:target_span[0]]
        target_text = planning_input[target_span[0]:target_span[1]]
        hard = any(term in between + target_text for term in DEPENDENCY_REFERENCES)
        ordered = bool(re.search(r"(?:先|首先).*(?:再|然后|之后|最后)", planning_input))
        if hard:
            dependencies.append(RuleDependencyDraft(source_span, target_span, "HARD_DATA", "TARGET_USES_SOURCE_RESULT"))
        elif ordered:
            dependencies.append(RuleDependencyDraft(source_span, target_span, "ORDER_ONLY", "USER_REQUESTED_ORDER_ONLY"))
    return RuleRouteDraft(segments, spans, tuple(dependencies), len(spans) > limit)


def rule_segments(draft: RuleRouteDraft) -> tuple[AnchoredSegment, ...]:
    return tuple(AnchoredSegment(item.source_text, item.start, item.end, index) for index, item in enumerate(draft.segments))


def validate_context_relation(decision: UnderstandingDecision, memory_context: dict | None) -> None:
    if decision.contextRelation.value not in {"CONTINUE", "REFINE", "CORRECTION"}:
        return
    view = memory_context or {}
    goal = view.get("current_goal")
    goal_text = goal.get("text") if isinstance(goal, dict) else goal if isinstance(goal, str) else ""
    topics = [item for item in view.get("active_topics", []) if isinstance(item, str) and item.strip()]
    recent = [item for item in view.get("recent_messages", []) if isinstance(item, dict) and item.get("role") == "user" and isinstance(item.get("content"), str) and item["content"].strip()]
    if not str(goal_text or "").strip() and not topics and not recent:
        raise SemanticContextRelationInvalid("context relation requires usable memory context")


def reconcile_segments(planning_input: str, rule_draft: RuleRouteDraft, decision: UnderstandingDecision, *, limit: int) -> tuple[AnchoredSegment, ...]:
    if len(decision.segments) > limit:
        raise SegmentCoverageFailed("segment count exceeds capacity")
    anchored: list[AnchoredSegment] = []
    for index, segment in enumerate(decision.segments):
        starts = [match.start() for match in re.finditer(re.escape(segment.sourceText), planning_input)]
        if len(starts) != 1:
            raise SegmentAnchorFailed("sourceText must have one exact current-input anchor")
        start = starts[0]
        anchored.append(AnchoredSegment(segment.sourceText, start, start + len(segment.sourceText), index))
    anchored.sort(key=lambda item: (item.start, item.original_index))
    normalized: set[str] = set()
    for index, item in enumerate(anchored):
        key = re.sub(r"[\s，,、。！？；;：:（）()\"']+", "", item.source_text)
        if not key or key in normalized:
            raise SegmentOverlapOrDuplicate("segments are duplicated")
        normalized.add(key)
        if index and item.start < anchored[index - 1].end:
            raise SegmentOverlapOrDuplicate("segments overlap")
    for action_start, action_end in rule_draft.action_spans:
        if not any(max(action_start, item.start) < min(action_end, item.end) for item in anchored):
            raise SegmentCoverageFailed("rule action span is not covered")
    covered = [False] * len(planning_input)
    for item in anchored:
        covered[item.start:item.end] = [True] * (item.end - item.start)
    remainder = "".join(char for index, char in enumerate(planning_input) if not covered[index])
    if _strip_ignorable(remainder):
        raise SegmentCoverageFailed("current input is not completely covered")
    return tuple(anchored)


def build_and_validate_rule_dependency_graphs(segments: Sequence[AnchoredSegment], draft: RuleRouteDraft) -> DependencyGraphs:
    nodes = tuple(item.key for item in segments)
    execution = DependencyGraph(nodes)
    presentation = DependencyGraph(nodes)
    for edge in draft.dependencies:
        source = _unique_covering_segment(segments, edge.source_span)
        target = _unique_covering_segment(segments, edge.target_span)
        if source == target:
            continue
        if edge.relation == "HARD_DATA":
            execution = execution.with_edge(source, target)
            presentation = presentation.with_edge(source, target)
        else:
            presentation = presentation.with_edge(source, target)
    if _has_cycle(execution) or _has_cycle(presentation):
        raise ValueError("rule dependency graph cannot contain a cycle")
    return DependencyGraphs(execution, presentation)


def validate_and_build_accepted_dependency_graphs(
    segments: Sequence[AnchoredSegment],
    draft: RuleRouteDraft,
    decision: UnderstandingDecision,
) -> tuple[DependencyGraphs, int]:
    graphs = build_and_validate_rule_dependency_graphs(segments, draft)
    execution, presentation = graphs.execution, graphs.presentation
    indexed = {item.original_index: item.key for item in segments}
    relation_by_pair: dict[frozenset[tuple[int, int]], tuple[tuple[int, int], tuple[int, int], str]] = {}
    for source, target in presentation.edges:
        relation = "HARD_DATA" if (source, target) in execution.edges else "ORDER_ONLY"
        relation_by_pair[frozenset((source, target))] = (source, target, relation)
    low_confidence = 0
    for hint in decision.dependencyHints:
        if hint.relation == "HARD_DATA" and hint.confidence < HARD_DATA_MIN_CONFIDENCE:
            low_confidence += 1
            continue
        source, target = indexed[hint.sourceIndex], indexed[hint.targetIndex]
        if source == target:
            raise SemanticDependencyGraphInvalid("LLM dependency endpoints collapsed")
        pair = frozenset((source, target))
        existing = relation_by_pair.get(pair)
        if existing and existing != (source, target, hint.relation):
            raise SemanticDependencyGraphInvalid("LLM dependency conflicts with accepted graph")
        relation_by_pair[pair] = (source, target, hint.relation)
    execution = DependencyGraph(tuple(item.key for item in segments))
    presentation = DependencyGraph(tuple(item.key for item in segments))
    for source, target, relation in relation_by_pair.values():
        if relation == "HARD_DATA":
            execution = execution.with_edge(source, target)
        presentation = presentation.with_edge(source, target)
    if _has_cycle(execution) or _has_cycle(presentation):
        raise SemanticDependencyGraphInvalid("accepted dependency graph contains a cycle")
    return DependencyGraphs(execution, presentation), low_confidence


def stable_topological_order(segments: Sequence[AnchoredSegment], graph: DependencyGraph) -> tuple[AnchoredSegment, ...]:
    by_key = {item.key: item for item in segments}
    indegree = {node: 0 for node in graph.nodes}
    children = {node: [] for node in graph.nodes}
    for source, target in graph.edges:
        indegree[target] += 1
        children[source].append(target)
    ready = sorted((node for node, degree in indegree.items() if degree == 0), key=lambda key: (key[0], by_key[key].original_index))
    ordered: list[AnchoredSegment] = []
    while ready:
        node = ready.pop(0)
        ordered.append(by_key[node])
        for target in children[node]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort(key=lambda key: (key[0], by_key[key].original_index))
    if len(ordered) != len(segments):
        raise ValueError("dependency graph cannot contain a cycle")
    return tuple(ordered)


def _action_spans(text: str) -> tuple[tuple[int, int], ...]:
    boundaries = list(re.finditer(CLAUSE_SEPARATORS, text))
    raw: list[tuple[int, int]] = []
    cursor = 0
    for match in boundaries:
        raw.append((cursor, match.start()))
        cursor = match.end()
    raw.append((cursor, len(text)))
    expanded: list[tuple[int, int]] = []
    for start, end in raw:
        points = [start, end]
        fragment = text[start:end]
        for term in (*COMPOUND_CONNECTORS, "然后", "之后", "最后", "再"):
            points.extend(start + match.start() for match in re.finditer(re.escape(term), fragment) if match.start() > 0)
        points = sorted(set(points))
        expanded.extend((points[index], points[index + 1]) for index in range(len(points) - 1))
    cleaned: list[tuple[int, int]] = []
    trim = " \t\r\n，,、。！？；;：:（）()\"'"
    prefixes = ("首先", "先", "然后", "之后", "最后", "再", *COMPOUND_CONNECTORS)
    for start, end in expanded:
        while start < end and text[start] in trim:
            start += 1
        while end > start and text[end - 1] in trim:
            end -= 1
        changed = True
        while changed:
            changed = False
            for prefix in prefixes:
                if text.startswith(prefix, start, end):
                    start += len(prefix)
                    while start < end and text[start] in trim:
                        start += 1
                    changed = True
        if start < end and _strip_ignorable(text[start:end]):
            cleaned.append((start, end))
    if len(cleaned) == 2 and _same_policy_fact(text, cleaned):
        return ((cleaned[0][0], cleaned[1][1]),)
    if len(cleaned) > 1:
        actionable = sum(any(term in text[start:end] for term in ACTION_TERMS) for start, end in cleaned)
        domains = [_domain_signals(text[start:end]) for start, end in cleaned]
        cross_domain = len(set().union(*domains)) >= 2 and sum(bool(items) for items in domains) >= 2
        explicit_sequence = bool(re.search(r"(?:先|首先).*(?:再|然后|之后|最后)", text))
        if actionable < 2 and not cross_domain and not explicit_sequence:
            return ((cleaned[0][0], cleaned[-1][1]),)
    return tuple(cleaned or [(0, len(text))])


def _same_policy_fact(text: str, spans: Sequence[tuple[int, int]]) -> bool:
    first, second = (text[start:end] for start, end in spans)
    return any(term in first for term in ("补考", "奖学金", "政策", "规定", "材料", "流程")) and not any(term in second for term in ACTION_TERMS) and any(term in second for term in ("截止", "日期", "材料", "地点", "电话", "条件"))


def _domain_signals(text: str) -> set[int]:
    groups = (
        ("学习", "复习", "考试", "课程", "论文", "考研", "就业", "实习", "计划", "规划"),
        ("材料", "流程", "资格", "截止", "政策", "规定", "补考", "重修", "奖学金", "校区", "宿舍"),
        ("焦虑", "睡不着", "失眠", "难受", "低落", "压力", "情绪", "害怕", "担心"),
        ("不想活", "自杀", "自伤", "伤害自己"),
    )
    return {index for index, group in enumerate(groups) if any(term in text for term in group)}


def _strip_ignorable(text: str) -> str:
    value = text
    for term in sorted(IGNORABLE_REMAINDER_TERMS, key=len, reverse=True):
        value = value.replace(term, "")
    return re.sub(r"[\s，,、。！？；;：:（）()\"']+", "", value)


def _unique_covering_segment(segments: Sequence[AnchoredSegment], span: tuple[int, int]) -> tuple[int, int]:
    matches = [item.key for item in segments if max(item.start, span[0]) < min(item.end, span[1])]
    if len(matches) != 1:
        raise SegmentCoverageFailed("dependency endpoint does not map to one segment")
    return matches[0]


def _has_cycle(graph: DependencyGraph) -> bool:
    dependencies = {node: set() for node in graph.nodes}
    for source, target in graph.edges:
        dependencies[target].add(source)
    visiting: set[tuple[int, int]] = set()
    visited: set[tuple[int, int]] = set()

    def visit(node: tuple[int, int]) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        if any(visit(parent) for parent in dependencies[node]):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in graph.nodes)
