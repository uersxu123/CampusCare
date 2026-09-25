from __future__ import annotations

from dataclasses import dataclass

from app.agents.routing import classify_route
from app.core.config import Settings
from app.core.enums import IntentType
from app.services.route_planning import SemanticPlannerUnavailable
from app.services.routing_v5 import (
    PRIMARY_INTENTS,
    PROTOTYPES,
    PrimaryEmbeddingRouter,
    PrimaryRoutingScorer,
    RoutingMode,
    RoutingScoreSnapshot,
    clear_prototype_vector_cache,
    decide_fast_route,
    requires_planner,
    warmup_fast_routing,
)
from app.services.understanding import UnderstandingInvocationResult
from app.services.routing_v5 import PlanningResultV5


@dataclass
class _FixedScorer:
    snapshot: RoutingScoreSnapshot
    calls: int = 0

    def score(self, _text: str) -> RoutingScoreSnapshot:
        self.calls += 1
        return self.snapshot


def _snapshot(scores, rule=None, embedding=None, *, latency=2):
    score_map = {intent: float(scores.get(intent, 0.0)) for intent in PRIMARY_INTENTS}
    rule_map = {intent: (rule or {}).get(intent) for intent in PRIMARY_INTENTS}
    emb_map = None if embedding is None else {intent: float(embedding.get(intent, 0.0)) for intent in PRIMARY_INTENTS}
    return RoutingScoreSnapshot(score_map, rule_map, emb_map, emb_map is not None, latency)


def _invocation(intent: str, text: str):
    return UnderstandingInvocationResult(
        PlanningResultV5.model_validate({"workItems": [{"sourceText": text, "intent": intent, "dependsOn": []}]}),
        1,
        7,
    )




def test_transform_risk_structure_requires_planner():
    assert requires_planner('把“我最近失眠睡不着”改写得更正式。', {}) is True
    assert requires_planner('把“宿舍换寝申请”改成一个邮件标题。', {}) is True
    assert requires_planner('翻译“国家助学金申请条件”', {}) is True
    assert requires_planner('把“补考没过怎么办”整理成一个问题标题。', {}) is True
    assert requires_planner('把“复习压力太大”改成一句更自然的表达。', {}) is True


def test_non_transform_simple_request_can_still_enter_fast_scoring():
    assert requires_planner('国家助学金申请条件是什么？', {}) is False
    assert requires_planner('申请换宿舍', {}) is False


def test_decide_fast_route_rejects_transform_even_with_high_topic_score():
    snapshot = _snapshot(
        {IntentType.MENTAL: 0.99, IntentType.CAMPUS: 0.20, IntentType.ACADEMIC: 0.20, IntentType.CHAT: 0.10},
        rule={IntentType.MENTAL: 0.99},
        embedding={IntentType.MENTAL: 0.99, IntentType.CAMPUS: 0.20, IntentType.ACADEMIC: 0.20, IntentType.CHAT: 0.10},
    )
    decision = decide_fast_route(
        '把“我最近失眠睡不着”改写得更正式。', snapshot, Settings(_env_file=None), {}
    )
    assert decision.accepted is False
    assert decision.intent is None
    assert decision.reason == 'COMPLEX_OR_CONTEXT_DEPENDENT'


def test_transform_bypasses_embedding_and_goes_to_planner():
    scorer = _FixedScorer(_snapshot(
        {IntentType.MENTAL: 0.99, IntentType.CAMPUS: 0.20, IntentType.ACADEMIC: 0.20, IntentType.CHAT: 0.10},
        rule={IntentType.MENTAL: 0.99},
        embedding={IntentType.MENTAL: 0.99, IntentType.CAMPUS: 0.20, IntentType.ACADEMIC: 0.20, IntentType.CHAT: 0.10},
    ))
    planner_calls = 0

    def planner(text, _context):
        nonlocal planner_calls
        planner_calls += 1
        return _invocation('CHAT', text)

    decision = classify_route(
        '把“我最近失眠睡不着”改写得更正式。',
        semantic_classifier=planner,
        settings=Settings(_env_file=None),
        routing_scorer=scorer,
    )

    assert planner_calls == 1
    assert scorer.calls == 0
    assert decision.route_plan is not None
    assert decision.route_plan.intents == (IntentType.CHAT,)
    assert decision.diagnostics.plan_source == 'LLM_ACCEPTED'
    assert decision.diagnostics.fast_route_reason == 'COMPLEX_OR_CONTEXT_DEPENDENT'


def test_title_transform_goes_to_planner_without_topic_scoring():
    scorer = _FixedScorer(_snapshot(
        {IntentType.CAMPUS: 0.99, IntentType.MENTAL: 0.20, IntentType.ACADEMIC: 0.20, IntentType.CHAT: 0.10},
        rule={IntentType.CAMPUS: 0.99},
        embedding={IntentType.CAMPUS: 0.99, IntentType.MENTAL: 0.20, IntentType.ACADEMIC: 0.20, IntentType.CHAT: 0.10},
    ))
    planner_calls = 0

    def planner(text, _context):
        nonlocal planner_calls
        planner_calls += 1
        return _invocation('CHAT', text)

    decision = classify_route(
        '把“宿舍换寝申请”改成一个邮件标题。',
        semantic_classifier=planner,
        settings=Settings(_env_file=None),
        routing_scorer=scorer,
    )

    assert planner_calls == 1
    assert scorer.calls == 0
    assert decision.route_plan is not None
    assert decision.route_plan.intents == (IntentType.CHAT,)
    assert decision.diagnostics.plan_source == 'LLM_ACCEPTED'


def test_knowledge_dependent_transform_still_goes_to_planner():
    scorer = _FixedScorer(_snapshot(
        {IntentType.CAMPUS: 0.99, IntentType.CHAT: 0.10, IntentType.MENTAL: 0.10, IntentType.ACADEMIC: 0.10},
        rule={IntentType.CAMPUS: 0.99},
        embedding={IntentType.CAMPUS: 0.99, IntentType.CHAT: 0.10, IntentType.MENTAL: 0.10, IntentType.ACADEMIC: 0.10},
    ))
    calls = 0

    def planner(text, _context):
        nonlocal calls
        calls += 1
        return _invocation('CAMPUS', text)

    decision = classify_route(
        '根据国家助学金政策判断我是否符合条件，再帮我改写成申请说明。',
        semantic_classifier=planner,
        settings=Settings(_env_file=None),
        routing_scorer=scorer,
    )

    assert calls == 1
    assert scorer.calls == 0
    assert decision.route_plan is not None
    assert decision.route_plan.intents == (IntentType.CAMPUS,)
    assert decision.diagnostics.plan_source == 'LLM_ACCEPTED'

def test_fast_route_direct_bypasses_planner():
    scorer = _FixedScorer(_snapshot(
        {IntentType.CAMPUS: 0.974, IntentType.ACADEMIC: 0.52, IntentType.MENTAL: 0.31, IntentType.CHAT: 0.20},
        rule={IntentType.CAMPUS: 0.98},
        embedding={IntentType.CAMPUS: 0.97, IntentType.ACADEMIC: 0.52, IntentType.MENTAL: 0.31, IntentType.CHAT: 0.20},
    ))
    planner_calls = 0

    def planner(*_):
        nonlocal planner_calls
        planner_calls += 1
        raise AssertionError("fast direct must bypass planner")

    decision = classify_route(
        "国家助学金什么时候发？",
        semantic_classifier=planner,
        settings=Settings(_env_file=None),
        routing_scorer=scorer,
    )

    assert planner_calls == 0
    assert scorer.calls == 1
    assert decision.route_plan is not None
    assert decision.route_plan.routing_mode is RoutingMode.NORMAL
    assert decision.route_plan.intents == (IntentType.CAMPUS,)
    assert decision.diagnostics.plan_source == "FAST_RULE_EMBEDDING"
    assert decision.diagnostics.llm_invoked is False


def test_multiple_candidates_escalate_to_planner():
    scorer = _FixedScorer(_snapshot(
        {IntentType.MENTAL: 0.97, IntentType.ACADEMIC: 0.96, IntentType.CAMPUS: 0.30, IntentType.CHAT: 0.20},
        rule={IntentType.MENTAL: 0.97, IntentType.ACADEMIC: 0.96},
        embedding={IntentType.MENTAL: 0.97, IntentType.ACADEMIC: 0.96, IntentType.CAMPUS: 0.30, IntentType.CHAT: 0.20},
    ))
    calls = 0

    def planner(text, _context):
        nonlocal calls
        calls += 1
        return UnderstandingInvocationResult(
            PlanningResultV5.model_validate({"workItems": [
                {"sourceText": "焦虑睡不着", "intent": "MENTAL", "dependsOn": []},
                {"sourceText": "办理休学", "intent": "ACADEMIC", "dependsOn": []},
            ]}),
            1,
            7,
        )

    decision = classify_route(
        "最近焦虑睡不着，同时想办理休学。",
        semantic_classifier=planner,
        settings=Settings(_env_file=None),
        routing_scorer=scorer,
    )
    assert calls == 1
    assert scorer.calls == 0  # cheap pre-gate skips embedding entirely
    assert decision.diagnostics.plan_source == "LLM_ACCEPTED"
    assert decision.diagnostics.fast_route_reason == "COMPLEX_OR_CONTEXT_DEPENDENT"


def test_context_dependent_query_escalates_even_with_high_score():
    scorer = _FixedScorer(_snapshot(
        {IntentType.CAMPUS: 0.99, IntentType.ACADEMIC: 0.20, IntentType.MENTAL: 0.20, IntentType.CHAT: 0.20},
        rule={IntentType.CAMPUS: 0.99},
        embedding={IntentType.CAMPUS: 0.99, IntentType.ACADEMIC: 0.20, IntentType.MENTAL: 0.20, IntentType.CHAT: 0.20},
    ))
    calls = 0

    def planner(text, _context):
        nonlocal calls
        calls += 1
        return _invocation("CAMPUS", text)

    decision = classify_route(
        "第二种呢？",
        semantic_classifier=planner,
        settings=Settings(_env_file=None),
        routing_scorer=scorer,
    )
    assert calls == 1
    assert scorer.calls == 0  # context-dependent requests do not pay scorer cost first
    assert decision.diagnostics.plan_source == "LLM_ACCEPTED"
    assert decision.diagnostics.fast_route_reason == "COMPLEX_OR_CONTEXT_DEPENDENT"


def test_parallel_goal_phrase_is_pre_gated_before_embedding():
    scorer = _FixedScorer(_snapshot(
        {IntentType.CAMPUS: 0.969, IntentType.MENTAL: 0.887, IntentType.ACADEMIC: 0.30, IntentType.CHAT: 0.20},
        rule={IntentType.CAMPUS: 0.96},
        embedding={IntentType.CAMPUS: 0.975, IntentType.MENTAL: 0.887, IntentType.ACADEMIC: 0.30, IntentType.CHAT: 0.20},
    ))
    calls = 0

    def planner(text, _context):
        nonlocal calls
        calls += 1
        return UnderstandingInvocationResult(
            PlanningResultV5.model_validate({"workItems": [
                {"sourceText": "刚入学很不适应", "intent": "MENTAL", "dependsOn": []},
                {"sourceText": "新生宿舍入住怎么办", "intent": "CAMPUS", "dependsOn": []},
            ]}),
            1,
            7,
        )

    decision = classify_route(
        "刚入学很不适应，也想知道新生宿舍入住怎么办。",
        semantic_classifier=planner,
        settings=Settings(_env_file=None),
        routing_scorer=scorer,
    )
    assert calls == 1
    assert scorer.calls == 0
    assert decision.route_plan is not None
    assert set(decision.route_plan.intents) == {IntentType.MENTAL, IntentType.CAMPUS}
    assert decision.diagnostics.fast_route_reason == "COMPLEX_OR_CONTEXT_DEPENDENT"


def test_complex_planner_failure_scores_only_after_planner_fails():
    scorer = _FixedScorer(_snapshot(
        {IntentType.MENTAL: 0.96, IntentType.ACADEMIC: 0.95, IntentType.CAMPUS: 0.20, IntentType.CHAT: 0.10},
        rule={IntentType.MENTAL: 0.97, IntentType.ACADEMIC: 0.96},
        embedding={IntentType.MENTAL: 0.953, IntentType.ACADEMIC: 0.943, IntentType.CAMPUS: 0.20, IntentType.CHAT: 0.10},
    ))
    planner_calls = 0

    def planner(*_):
        nonlocal planner_calls
        planner_calls += 1
        raise SemanticPlannerUnavailable("down")

    decision = classify_route(
        "最近焦虑睡不着，同时想办理休学。",
        semantic_classifier=planner,
        settings=Settings(_env_file=None),
        routing_scorer=scorer,
    )
    assert planner_calls == 2
    assert scorer.calls == 1  # delayed until fallback is actually needed
    assert decision.route_plan is not None
    assert decision.route_plan.routing_mode is RoutingMode.DEGRADED_BROADCAST


def test_planner_failure_reuses_single_score_snapshot():
    scorer = _FixedScorer(_snapshot(
        # Keep top1 below the current T0.92 Fast threshold so this test
        # exercises reject -> Planner retry -> degraded fallback snapshot reuse.
        {IntentType.CAMPUS: 0.91, IntentType.ACADEMIC: 0.20, IntentType.MENTAL: 0.20, IntentType.CHAT: 0.20},
        rule={IntentType.CAMPUS: 0.96},
        embedding={IntentType.CAMPUS: 0.8767, IntentType.ACADEMIC: 0.20, IntentType.MENTAL: 0.20, IntentType.CHAT: 0.20},
    ))
    planner_calls = 0

    def planner(*_):
        nonlocal planner_calls
        planner_calls += 1
        raise SemanticPlannerUnavailable("down")

    decision = classify_route(
        "国家助学金什么时候发？",
        semantic_classifier=planner,
        settings=Settings(_env_file=None),
        routing_scorer=scorer,
    )
    assert scorer.calls == 1
    assert planner_calls == 2
    assert decision.route_plan is not None
    assert decision.route_plan.routing_mode is RoutingMode.DEGRADED_DIRECT


def test_raw_current_input_no_longer_overrides_planning_input():
    settings = Settings(_env_file=None, route_fast_enabled=False)
    seen = []

    def planner(text, _context):
        seen.append(text)
        return _invocation("CAMPUS", text)

    decision = classify_route(
        "国家助学金休学以后还发吗？",
        semantic_classifier=planner,
        settings=settings,
        raw_current_input="这个休学以后还发吗？",
    )
    assert seen == ["国家助学金休学以后还发吗？"]
    assert decision.route_plan is not None


class _CountingBackend:
    name = "fake"
    model = "fake-v1"
    prototype_batches = 0
    query_batches = 0
    available_calls = 0

    def available(self):
        type(self).available_calls += 1
        return True

    def embed_documents(self, texts):
        prototype_count = sum(len(PROTOTYPES[intent]) for intent in PRIMARY_INTENTS)
        if len(texts) == prototype_count:
            type(self).prototype_batches += 1
        else:
            type(self).query_batches += 1
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text):
        return [1.0, 0.0]

    def model_digest(self):
        return "fake"


def test_prototype_vectors_are_shared_across_router_instances():
    clear_prototype_vector_cache()
    _CountingBackend.prototype_batches = 0
    _CountingBackend.query_batches = 0
    _CountingBackend.available_calls = 0
    settings = Settings(_env_file=None)

    PrimaryEmbeddingRouter(settings, backend=_CountingBackend()).scores("问题一")
    PrimaryEmbeddingRouter(settings, backend=_CountingBackend()).scores("问题二")

    assert _CountingBackend.prototype_batches == 1
    assert _CountingBackend.query_batches == 2
    assert _CountingBackend.available_calls == 0  # no redundant preflight request


def test_fast_embedding_backend_uses_short_timeout_budget():
    settings = Settings(
        _env_file=None,
        knowledge_vector_enabled=True,
        knowledge_embedding_provider="ollama",
        knowledge_embedding_model="bge-m3:latest",
        embedding_timeout_seconds=30.0,
        route_fast_embedding_timeout_seconds=3.0,
    )
    scorer = PrimaryRoutingScorer(settings)
    assert getattr(scorer.embedding_router.backend, "timeout") == 3.0


def test_fast_routing_warmup_populates_shared_prototype_cache():
    clear_prototype_vector_cache()
    _CountingBackend.prototype_batches = 0
    _CountingBackend.query_batches = 0
    _CountingBackend.available_calls = 0
    settings = Settings(_env_file=None, route_fast_warmup_enabled=True)
    router = PrimaryEmbeddingRouter(settings, backend=_CountingBackend())
    assert warmup_fast_routing(settings, embedding_router=router) is True
    assert _CountingBackend.prototype_batches == 1
    assert _CountingBackend.available_calls == 1  # startup-only probe
    # A separate router instance must reuse the warmed prototype cache.
    PrimaryEmbeddingRouter(settings, backend=_CountingBackend()).scores("国家助学金什么时候发？")
    assert _CountingBackend.prototype_batches == 1
    assert _CountingBackend.query_batches == 1


def test_meta_generation_examples_are_planner_owned():
    assert requires_planner('帮我写一封询问宿舍调换的示例邮件，不需要真的办理。', {}) is True
    assert requires_planner('给我写一个国家助学金申请邮件模板。', {}) is True
    assert requires_planner('提供一个心理咨询预约回复范例。', {}) is True


def test_explicit_multi_goal_markers_cover_common_phrases():
    assert requires_planner('有两个问题：实习求职怎么规划，我最近也很沮丧。', {}) is True
    assert requires_planner('实习求职怎么规划，我最近很沮丧，这几个都帮我处理一下。', {}) is True


def test_cross_domain_clause_risk_escalates_without_llm_or_embedding():
    assert requires_planner('实习求职怎么规划，我最近被拒很多次也很沮丧。', {}) is True
    assert requires_planner('国家助学金怎么申请，我最近焦虑得睡不着。', {}) is True
    assert requires_planner('绩点怎么计算，宿舍调换要准备什么材料。', {}) is True


def test_same_domain_clauses_do_not_escalate_just_because_of_punctuation():
    assert requires_planner('我最近压力很大，晚上也睡不好。', {}) is False
    assert requires_planner('国家助学金什么时候申请，申请材料有哪些。', {}) is False
    assert requires_planner('实习怎么准备，求职简历怎么优化。', {}) is False


def test_single_clause_boundary_question_is_not_forced_to_planner_by_domain_hints():
    # One clause may legitimately mention terms from more than one domain.
    # The clause-risk guard must not turn that into a pseudo multi-intent.
    assert requires_planner('休学期间国家助学金还发吗？', {}) is False
