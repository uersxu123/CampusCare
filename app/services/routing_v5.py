from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import IntentType, MAX_INPUT_CHARS, MAX_WORK_ITEMS
from app.services.embedding import EmbeddingBackend, EmbeddingUnavailable, create_embedding_backend

PrimaryIntent = Literal["CHAT", "ACADEMIC", "CAMPUS", "MENTAL"]
PRIMARY_INTENTS = (IntentType.CHAT, IntentType.ACADEMIC, IntentType.CAMPUS, IntentType.MENTAL)
FIXED_INTENT_ORDER = {IntentType.CHAT: 0, IntentType.ACADEMIC: 1, IntentType.CAMPUS: 2, IntentType.MENTAL: 3}


class PlannedWorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sourceText: str = Field(min_length=1, max_length=1000)
    intent: PrimaryIntent
    dependsOn: list[int] = Field(default_factory=list, max_length=4)

    @field_validator("sourceText")
    @classmethod
    def source_text_must_be_visible(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("sourceText 不能为空白")
        return value


class PlanningResultV5(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Intentionally no max_length: MAX_WORK_ITEMS is business control, not schema failure.
    workItems: list[PlannedWorkItem] = Field(min_length=1)


class PlannedWorkItemV6(BaseModel):
    """Planner V6 semantic contract.

    V5 remains readable for old traces and fixtures.  V6 adds only semantic
    task/source references; Python still owns ids, arguments and control data.
    """
    model_config = ConfigDict(extra="forbid")

    intent: PrimaryIntent
    objective: str = Field(min_length=1, max_length=80)
    taskText: str = Field(min_length=1, max_length=240)
    sourceRefs: list[str] = Field(min_length=1, max_length=8)
    contextRefs: list[str] = Field(default_factory=list, max_length=8)
    dependsOn: list[int] = Field(default_factory=list, max_length=4)

    @field_validator("objective", "taskText")
    @classmethod
    def text_must_be_visible(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Planner 文本不能为空白")
        return value.strip()

    @field_validator("sourceRefs", "contextRefs")
    @classmethod
    def refs_must_be_unique(cls, value: list[str]) -> list[str]:
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError("来源引用必须为非空字符串")
        if len(value) != len(set(value)):
            raise ValueError("来源引用不能重复")
        return value


class PlanningResultV6(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schemaVersion: Literal[6] = 6
    workItems: list[PlannedWorkItemV6] = Field(min_length=1, max_length=4)


def validate_planning_result_v6(result: PlanningResultV6, source_catalog: set[str] | None = None) -> None:
    if not isinstance(result, PlanningResultV6) or not result.workItems:
        raise PlannerValidationError("planner V6 workItems 不能为空")
    count = len(result.workItems)
    catalog = source_catalog or set()
    graph: dict[int, tuple[int, ...]] = {}
    for index, item in enumerate(result.workItems):
        if catalog and any(ref not in catalog for ref in item.sourceRefs + item.contextRefs):
            raise PlannerValidationError("Planner 来源引用不在服务端目录中")
        deps = tuple(item.dependsOn)
        if len(deps) != len(set(deps)) or any(dep < 0 or dep >= count for dep in deps):
            raise PlannerValidationError("dependsOn index 无效")
        if index in deps or any(dep >= index for dep in deps):
            raise PlannerValidationError("V6 仅允许依赖前序工作项")
        graph[index] = deps
    visiting: set[int] = set(); visited: set[int] = set()
    def visit(node: int) -> None:
        if node in visiting:
            raise PlannerValidationError("dependsOn 不能形成环")
        if node in visited:
            return
        visiting.add(node)
        for parent in graph[node]:
            visit(parent)
        visiting.remove(node); visited.add(node)
    for node in graph:
        visit(node)


class RoutingMode(str, Enum):
    NORMAL = "NORMAL"
    DEGRADED_DIRECT = "DEGRADED_DIRECT"
    DEGRADED_BROADCAST = "DEGRADED_BROADCAST"


class PlannerValidationError(ValueError):
    pass


def validate_planning_result(result: PlanningResultV5) -> None:
    if not isinstance(result, PlanningResultV5) or not result.workItems:
        raise PlannerValidationError("planner workItems 不能为空")
    count = len(result.workItems)
    graph: dict[int, tuple[int, ...]] = {}
    for index, item in enumerate(result.workItems):
        if not item.sourceText.strip() or len(item.sourceText) > MAX_INPUT_CHARS:
            raise PlannerValidationError("sourceText 无效")
        deps = tuple(item.dependsOn)
        if len(deps) != len(set(deps)):
            raise PlannerValidationError("dependsOn 不能重复")
        if any(dep < 0 or dep >= count for dep in deps):
            raise PlannerValidationError("dependsOn index 越界")
        if index in deps:
            raise PlannerValidationError("WorkItem 不能依赖自己")
        # Keep the graph simple/deterministic in the first V5 implementation.
        if any(dep >= index for dep in deps):
            raise PlannerValidationError("V5 默认只允许依赖前面的 WorkItem")
        graph[index] = deps
    visiting: set[int] = set()
    visited: set[int] = set()
    def visit(node: int) -> None:
        if node in visiting:
            raise PlannerValidationError("dependsOn 不能形成环")
        if node in visited:
            return
        visiting.add(node)
        for parent in graph[node]:
            visit(parent)
        visiting.remove(node)
        visited.add(node)
    for node in graph:
        visit(node)


RULE_WEIGHT = 0.40
EMBEDDING_WEIGHT = 0.60
DEGRADED_INTENT_THRESHOLD = 0.90
RULE_ONLY_INTENT_THRESHOLD = 0.90
FIXED_DEGRADED_CLARIFICATION = "我暂时没能可靠判断你想处理的问题类型。请把你最想解决的问题再具体说明一下。"


@dataclass(frozen=True)
class DegradedRoutingResult:
    selected: tuple[IntentType, ...]
    scores: dict[IntentType, float]
    rule_scores: dict[IntentType, float | None]
    embedding_scores: dict[IntentType, float] | None
    embedding_available: bool

    @property
    def mode(self) -> RoutingMode | None:
        if len(self.selected) == 1:
            return RoutingMode.DEGRADED_DIRECT
        if len(self.selected) > 1:
            return RoutingMode.DEGRADED_BROADCAST
        return None


_STRONG_RULE_TERMS: dict[IntentType, tuple[str, ...]] = {
    IntentType.ACADEMIC: (
        "学习计划", "复习计划", "复习安排", "时间管理", "学习方法", "学习规划", "课程学习安排",
        "挂科", "学业预警", "学业警示", "补考", "缓考", "重修", "重新修读", "课程考核",
        "选课", "课程修读", "培养方案", "学分", "绩点", "学籍异动", "办理休学", "休学申请",
        "申请复学", "恢复学籍", "请假", "销假", "毕业条件", "结业", "学位", "考研", "保研", "推免",
        "就业手续", "毕业生就业", "实习规划", "毕业论文", "答辩", "科研", "学术诚信", "学术规范",
        "图书馆", "借书", "还书", "学费", "课堂规范", "体测", "体质健康", "志愿服务", "社会实践",
        "美育实践", "学习倦怠", "学业倦怠", "学习耗竭", "学业耗竭",
    ),
    IntentType.CAMPUS: (
        "国家奖学金", "国家励志奖学金", "英才奖学金", "社会奖学金", "国家助学金", "社会助学金",
        "家庭经济困难认定", "困难认定", "贫困认定", "临时困难补助", "重大疾病救助基金", "助学贷款",
        "国家助学贷款", "生源地贷款", "基层就业学费补偿", "贷款代偿", "应征入伍资助", "退伍资助",
        "综合素质测评", "综合测评", "优秀毕业生", "学业帮扶", "学习支持中心", "校内学业支持资源",
        "纪律处分", "处分申诉", "学生申诉", "处分复核", "学生管理规定", "宿舍", "调宿", "换宿舍",
        "换寝", "调寝", "入住办理", "退宿", "校园网", "校园网络", "校园卡", "校车", "校园交通",
        "校园电动车", "电动自行车", "无人机", "学生档案", "档案转递", "大学生医保", "医保报销",
        "医疗报销", "学生社团", "学生组织", "校园安全管理", "宿舍安全",
    ),
    IntentType.MENTAL: (
        "特别焦虑", "焦虑", "心慌", "惊恐", "恐慌", "情绪紧张", "失眠", "睡不着", "睡不好", "睡眠问题",
        "情绪低落", "很难过", "崩溃", "痛苦", "无助", "什么都不想做", "情绪很差", "不适应大学",
        "新生不适应", "想家", "入学适应", "室友关系", "室友矛盾", "人际关系", "同学关系", "家庭关系",
        "家庭压力", "恋爱问题", "分手难受", "心理咨询", "心理中心", "心理辅导", "咨询隐私", "心理咨询保密",
    ),
    IntentType.CHAT: (
        "Python", "Java", "HashMap", "数据库", "算法题", "操作系统", "KV cache", "翻译", "润色", "工作邮件",
    ),
}

_FACET_TERMS = ("材料", "流程", "申请", "办理", "资格", "截止", "政策", "规定", "条件", "渠道", "地点", "电话", "预约")


def primary_rule_scores(text: str) -> dict[IntentType, float | None]:
    text = str(text or "").strip()
    scores: dict[IntentType, float | None] = {intent: None for intent in PRIMARY_INTENTS}
    normalized = text.strip(" \t\r\n，,。！？!?.")
    if normalized in {"你好", "您好", "嗨", "早上好", "下午好", "晚上好", "谢谢", "感谢", "再见", "你是谁", "你能做什么"}:
        scores[IntentType.CHAT] = 0.98

    for intent, terms in _STRONG_RULE_TERMS.items():
        if any(term in text for term in terms):
            scores[intent] = max(scores[intent] or 0.0, 0.96)

    # High-value boundary combinations from routing taxonomy.
    if any(term in text for term in ("焦虑", "失眠", "睡不着", "恐慌", "情绪低落", "崩溃", "很难受")):
        scores[IntentType.MENTAL] = max(scores[IntentType.MENTAL] or 0.0, 0.97)
    if any(term in text for term in ("复习计划", "学习计划", "学习方法", "时间管理")):
        scores[IntentType.ACADEMIC] = max(scores[IntentType.ACADEMIC] or 0.0, 0.97)
    if any(term in text for term in ("休学", "复学")):
        if any(term in text for term in ("助学金", "奖学金", "资助")):
            scores[IntentType.CAMPUS] = max(scores[IntentType.CAMPUS] or 0.0, 0.97)
        if any(term in text for term in ("怎么办理", "怎么申请", "条件", "学籍", "复学")) or "想休学" in text:
            scores[IntentType.ACADEMIC] = max(scores[IntentType.ACADEMIC] or 0.0, 0.95)
    if any(term in text for term in ("室友", "寝室")):
        if any(term in text for term in ("难受", "沟通", "吵架", "矛盾")):
            scores[IntentType.MENTAL] = max(scores[IntentType.MENTAL] or 0.0, 0.96)
        if any(term in text for term in ("调宿", "换宿舍", "换寝", "调寝", "申请")):
            scores[IntentType.CAMPUS] = max(scores[IntentType.CAMPUS] or 0.0, 0.96)
    if "心理" in text and any(term in text for term in ("咨询", "预约", "保密", "隐私")):
        scores[IntentType.MENTAL] = max(scores[IntentType.MENTAL] or 0.0, 0.98)
    if any(term in text for term in ("学校有哪些学业帮扶", "学习支持中心", "辅导员帮助", "导师帮助")):
        scores[IntentType.CAMPUS] = max(scores[IntentType.CAMPUS] or 0.0, 0.96)

    # Facet words never create a domain by themselves.
    if scores[IntentType.CAMPUS] is not None and not any(term in text for term in _STRONG_RULE_TERMS[IntentType.CAMPUS]):
        if all(term not in text for term in ("宿舍", "校园", "医保", "奖学金", "助学金", "处分", "社团", "档案")):
            scores[IntentType.CAMPUS] = None
    if normalized and all(scores[i] is None for i in PRIMARY_INTENTS) and not any(term in text for term in _FACET_TERMS):
        # Do not make CHAT a universal default. Only clear generic/technical requests get evidence.
        if re.search(r"\b(?:Python|Java|SQL|Redis|MySQL|Linux|HTTP|API)\b", text, re.I):
            scores[IntentType.CHAT] = 0.96
    return scores


ACADEMIC_EXAMPLES = (
    "帮我制定这学期的学习计划", "离考试还有两周怎么安排复习时间", "高数基础差怎么复习", "这门课挂科以后可以补考吗",
    "补考没过以后是不是需要重修", "收到学业预警以后应该怎么办", "下学期应该怎么选课", "培养方案要求修多少学分",
    "我的绩点怎么计算", "本科生怎么办理休学", "休学以后怎么申请复学", "学生请假和销假有什么规定",
    "毕业需要满足哪些条件", "考研和就业之间应该怎么规划", "保研和推免需要怎么准备", "我该怎么安排实习和求职计划",
    "毕业论文应该怎么推进", "答辩前应该做哪些准备", "课程考试作弊和学术诚信有什么规定", "图书馆借书还书有什么规定",
    "学校学费什么时候缴", "大学生体测怎么要求", "志愿服务时长怎么认定", "最近学习倦怠帮我调整学习安排",
)
CAMPUS_EXAMPLES = (
    "国家奖学金申请条件是什么", "国家奖学金和国家助学金可以同时拿吗", "休学期间国家助学金还会不会发", "家庭经济困难学生怎么认定",
    "临时困难补助怎么申请", "国家助学贷款怎么办理", "综合素质测评怎么计算", "学校有哪些学业帮扶资源",
    "学习支持中心能提供什么帮助", "宿舍调换需要什么条件和材料", "我想换寝室应该怎么申请", "新生宿舍入住怎么办理",
    "校园网账号怎么办理", "校园卡相关服务去哪里办理", "机动车进校园有什么要求", "学生档案怎么转递",
    "大学生医保怎么参保", "住院医保怎么报销", "受到处分以后怎么提出学生申诉", "学生社团活动有什么管理规定",
)
MENTAL_EXAMPLES = (
    "我最近特别焦虑心里一直很慌", "我突然很恐慌不知道怎么稳定下来", "考试一想到就很紧张害怕", "最近晚上总是失眠睡不着",
    "压力太大导致每天都睡不好", "我情绪很低落什么都不想做", "最近特别难过和无助", "刚上大学很不适应总是想家",
    "我和室友关系很差不知道怎么沟通", "跟同学冲突让我很难受", "家庭关系让我压力很大", "分手以后一直很难受",
    "学校心理咨询中心怎么预约", "我想找心理老师聊聊", "心理咨询会保护我的隐私吗", "挂科以后我一直很低落和恐慌",
)
CHAT_EXAMPLES = (
    "你好", "谢谢你", "再见", "陪我随便聊聊", "你是谁", "你能做什么", "帮我把这段话翻译成英文", "帮我润色这段文字",
    "Python这个报错怎么排查", "Java的HashMap怎么实现", "数据库索引为什么能提高查询速度", "这道算法题怎么做", "解释一下KV cache", "帮我写普通工作邮件",
)
PROTOTYPES: dict[IntentType, tuple[str, ...]] = {
    IntentType.CHAT: CHAT_EXAMPLES,
    IntentType.ACADEMIC: ACADEMIC_EXAMPLES,
    IntentType.CAMPUS: CAMPUS_EXAMPLES,
    IntentType.MENTAL: MENTAL_EXAMPLES,
}


def split_for_fallback(query: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"[，。！？；\n]", query) if part.strip()]
    return parts or [query.strip()]


PROTOTYPE_VERSION = "routing-v5.5-fast-opt-1"
_PROTOTYPE_TEXT_HASH = hashlib.sha256(
    "\x1f".join(
        f"{intent.value}:{text}"
        for intent in PRIMARY_INTENTS
        for text in PROTOTYPES[intent]
    ).encode("utf-8")
).hexdigest()
_PROTOTYPE_VECTOR_CACHE: dict[tuple[str, str, str, str], dict[IntentType, list[list[float]]]] = {}
_PROTOTYPE_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class RoutingScoreSnapshot:
    scores: dict[IntentType, float]
    rule_scores: dict[IntentType, float | None]
    embedding_scores: dict[IntentType, float] | None
    embedding_available: bool
    latency_ms: int = 0


@dataclass(frozen=True)
class FastRouteDecision:
    accepted: bool
    intent: IntentType | None
    reason: str


class PrimaryEmbeddingRouter:
    def __init__(self, settings: Any, backend: EmbeddingBackend | None = None):
        self.settings = settings
        self.backend = backend or create_embedding_backend(settings)
        self._vectors: dict[IntentType, list[list[float]]] | None = None

    def scores(self, text: str) -> dict[IntentType, float] | None:
        try:
            # Avoid a separate /api/tags preflight on every lightweight router
            # instance. The actual embedding request is the authoritative
            # availability check and already has the fast-route timeout budget.
            if str(getattr(self.backend, "name", "")).lower() == "disabled":
                return None
            vectors = self._prototype_vectors()
            clauses = split_for_fallback(text)
            query_vectors = [_unit_vector(row) for row in self.backend.embed_documents(clauses)]
            result: dict[IntentType, float] = {}
            for intent, rows in vectors.items():
                similarity = max(_dot_unit(qv, pv) for qv in query_vectors for pv in rows)
                result[intent] = max(0.0, min(1.0, (similarity + 1.0) / 2.0))
            return result
        except (EmbeddingUnavailable, httpx.HTTPError, ValueError):
            return None

    def _prototype_vectors(self) -> dict[IntentType, list[list[float]]]:
        if self._vectors is not None:
            return self._vectors
        key = (
            str(getattr(self.backend, "name", type(self.backend).__name__)),
            str(getattr(self.backend, "base_url", "")),
            str(getattr(self.backend, "model", "")),
            f"{PROTOTYPE_VERSION}:{_PROTOTYPE_TEXT_HASH}",
        )
        cached = _PROTOTYPE_VECTOR_CACHE.get(key)
        if cached is not None:
            self._vectors = cached
            return cached
        with _PROTOTYPE_CACHE_LOCK:
            cached = _PROTOTYPE_VECTOR_CACHE.get(key)
            if cached is None:
                texts = [text for intent in PRIMARY_INTENTS for text in PROTOTYPES[intent]]
                raw = self.backend.embed_documents(texts)
                offset = 0
                vectors: dict[IntentType, list[list[float]]] = {}
                for intent in PRIMARY_INTENTS:
                    size = len(PROTOTYPES[intent])
                    rows = raw[offset:offset + size]
                    if len(rows) != size or any(not row for row in rows):
                        raise ValueError("Embedding prototype vectors incomplete")
                    # Store unit vectors once so request-time scoring only needs
                    # dot products; prototype norms never need to be recomputed.
                    vectors[intent] = [_unit_vector(row) for row in rows]
                    offset += size
                _PROTOTYPE_VECTOR_CACHE[key] = vectors
                cached = vectors
        self._vectors = cached
        return cached


class PrimaryRoutingScorer:
    def __init__(self, settings: Any, embedding_router: PrimaryEmbeddingRouter | None = None):
        self.settings = settings
        if embedding_router is not None:
            self.embedding_router = embedding_router
        else:
            fast_timeout = float(getattr(settings, "route_fast_embedding_timeout_seconds", 3.0))
            knowledge_timeout = float(getattr(settings, "embedding_timeout_seconds", fast_timeout))
            timeout = max(0.1, min(fast_timeout, knowledge_timeout))
            routing_settings = settings.model_copy(update={"embedding_timeout_seconds": timeout}) if hasattr(settings, "model_copy") else settings
            self.embedding_router = PrimaryEmbeddingRouter(routing_settings)

    def score(self, user_input: str) -> RoutingScoreSnapshot:
        started = time.perf_counter()
        rule = primary_rule_scores(user_input)
        embedding = self.embedding_router.scores(user_input)
        rw = float(getattr(self.settings, "route_degraded_rule_weight", RULE_WEIGHT))
        ew = float(getattr(self.settings, "route_degraded_embedding_weight", EMBEDDING_WEIGHT))
        scores: dict[IntentType, float] = {}
        for intent in PRIMARY_INTENTS:
            r = rule[intent]
            if embedding is None:
                scores[intent] = r or 0.0
            elif r is None:
                scores[intent] = embedding[intent]
            else:
                denom = rw + ew
                scores[intent] = ((rw * r + ew * embedding[intent]) / denom) if denom > 0 else embedding[intent]
        return RoutingScoreSnapshot(
            scores=scores,
            rule_scores=rule,
            embedding_scores=embedding,
            embedding_available=embedding is not None,
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
        )




def warmup_fast_routing(settings: Any, embedding_router: PrimaryEmbeddingRouter | None = None) -> bool:
    """Preload the routing embedding model/prototypes before serving requests.

    Warmup uses its own wider budget; request-time routing still uses the short
    `route_fast_embedding_timeout_seconds` fail-fast budget. Failure is non-fatal
    and simply leaves Fast Route to fail open to the Planner later.
    """
    if not bool(getattr(settings, "route_fast_enabled", True)):
        return False
    if not bool(getattr(settings, "route_fast_warmup_enabled", True)):
        return False
    try:
        if embedding_router is None:
            warmup_timeout = max(0.1, float(getattr(settings, "route_fast_warmup_timeout_seconds", 30.0)))
            routing_settings = (
                settings.model_copy(update={"embedding_timeout_seconds": warmup_timeout})
                if hasattr(settings, "model_copy")
                else settings
            )
            embedding_router = PrimaryEmbeddingRouter(routing_settings)
        # Startup-only reachability probe: fail quickly when the embedding
        # service is absent, but allow the actual cold model/prototype load to
        # use the wider warmup budget. Request-time scoring has no preflight.
        if not embedding_router.backend.available():
            return False
        embedding_router._prototype_vectors()
        return True
    except (EmbeddingUnavailable, httpx.HTTPError, ValueError):
        return False


class GlobalDegradedRouter:
    def __init__(
        self,
        settings: Any,
        embedding_router: PrimaryEmbeddingRouter | None = None,
        scorer: PrimaryRoutingScorer | None = None,
    ):
        self.settings = settings
        self.scorer = scorer or PrimaryRoutingScorer(settings, embedding_router=embedding_router)

    def route(self, user_input: str, snapshot: RoutingScoreSnapshot | None = None) -> DegradedRoutingResult:
        snapshot = snapshot or self.scorer.score(user_input)
        threshold = float(getattr(self.settings, "route_degraded_min_score", DEGRADED_INTENT_THRESHOLD))
        rule_only_threshold = float(getattr(self.settings, "route_rule_only_min_score", RULE_ONLY_INTENT_THRESHOLD))
        cutoff = threshold if snapshot.embedding_available else rule_only_threshold
        selected = tuple(
            intent
            for intent, score in sorted(
                snapshot.scores.items(),
                key=lambda item: (-item[1], FIXED_INTENT_ORDER[item[0]]),
            )
            if score >= cutoff
        )
        return DegradedRoutingResult(
            selected,
            snapshot.scores,
            snapshot.rule_scores,
            snapshot.embedding_scores,
            snapshot.embedding_available,
        )


_CONTEXT_DEPENDENT_MARKERS = (
    "这个", "那个", "它", "第二个", "第二种", "第三个", "第三种",
    "前面那个", "刚才那个", "之前那个", "还是那个", "继续", "接着",
)
_EXPLICIT_MULTI_GOAL_MARKERS = (
    "同时", "另外还", "顺便", "还有一个问题", "有两个问题", "有几个问题",
    "分别", "也想", "还想", "又想", "并想", "并且想",
    "这几个都", "都帮我处理",
)
_EXPLICIT_DEPENDENCY_PHRASES = ("再根据", "基于这个结果", "基于上面的结果", "然后根据", "根据前面的结果")
_ACTION_TERMS_FAST = ("办理", "申请", "查询", "核对", "制定", "安排", "规划", "预约", "报销", "选课", "休学", "复学")

# High-precision transform-risk structural signals. These signals do not
# classify an intent. They only mark requests that are unsafe for the cheap
# Rule+Embedding fast path, so the LLM Planner can resolve the real intent.
_QUOTED_PAYLOAD_RE = re.compile(r"[“\"「『'].*?[”\"」』']", re.S)
_TRANSFORM_TEXT_ACTIONS = (
    "翻译", "译成", "改写", "润色", "缩写", "扩写", "格式化",
    "改得", "整理成", "压缩成",
)
_TRANSFORM_TARGET_PATTERNS = (
    re.compile(r"(?:改成|改为|写成|转换成|转换为|转成|整理成|压缩成).{0,20}(?:邮件标题|FAQ\s*标题|问题标题|标题|英文|中文|日语|正式(?:一点|一些)?|礼貌(?:一点|一些)?|委婉(?:一点|一些)?|自然(?:一点|一些)?|简短(?:一点|一些)?|一句话|要点|Markdown|表格|JSON|列表|表达|问题)", re.I),
    re.compile(r"(?:生成|起|拟).{0,8}(?:邮件标题|FAQ\s*标题|问题标题|标题)", re.I),
)
_TRANSFORM_PAYLOAD_CUES = ("这段话", "这句话", "这段文字", "这段内容", "以下内容", "下面内容", "下面这段", "这封邮件", "这份文案")
_META_GENERATION_PATTERNS = (
    re.compile(r"(?:帮我)?写一(?:封|份|个).{0,24}(?:邮件|模板|示例|标题|回复|文案)"),
    re.compile(r"(?:给我|帮我)(?:写|拟|生成).{0,24}(?:邮件|模板|示例|标题|回复|文案)"),
    re.compile(r"(?:给我|提供).{0,12}(?:一个|一份|一封).{0,20}(?:模板|示例|邮件范例|回复范例)"),
)

# Small, high-precision domain hints used only to detect implicit cross-domain
# clause structure. They are NOT an intent classifier and produce no route.
_STRUCTURAL_DOMAIN_HINTS: dict[IntentType, tuple[str, ...]] = {
    IntentType.ACADEMIC: (
        "学习计划", "复习", "选课", "学分", "绩点", "挂科", "补考", "重修",
        "休学", "复学", "考研", "保研", "实习", "求职", "就业", "毕业论文",
    ),
    IntentType.CAMPUS: (
        "奖学金", "助学金", "困难认定", "助学贷款", "宿舍", "换寝", "调宿",
        "校园网", "校园卡", "医保", "报销", "处分", "档案", "社团",
    ),
    IntentType.MENTAL: (
        "焦虑", "失眠", "睡不着", "沮丧", "情绪低落", "难受", "崩溃",
        "不适应", "压力很大", "室友矛盾",
    ),
}
_CLAUSE_SPLIT_RE = re.compile(r"[，,；;。！？!?\n]+")


def _mask_quoted_payloads(text: str) -> tuple[str, bool]:
    had_quoted_payload = bool(_QUOTED_PAYLOAD_RE.search(text))
    return _QUOTED_PAYLOAD_RE.sub(" <PAYLOAD> ", text), had_quoted_payload


def _has_transform_risk_structure(outer: str, had_quoted_payload: bool) -> bool:
    """Return True when a text-operation request should bypass Fast Route.

    This is deliberately an eligibility guard, not an intent classifier.
    The final intent (often CHAT, but not always) is left to the Planner.
    """
    if any(action in outer for action in _TRANSFORM_TEXT_ACTIONS):
        return True
    if any(pattern.search(outer) for pattern in _TRANSFORM_TARGET_PATTERNS):
        return True
    if any(pattern.search(outer) for pattern in _META_GENERATION_PATTERNS):
        return True
    # A bare “总结某领域” can be a real domain question. Treat summary as a
    # transform-risk signal only when the user explicitly supplies/refers to
    # text content to transform.
    return "总结" in outer and (had_quoted_payload or any(cue in outer for cue in _TRANSFORM_PAYLOAD_CUES))


def _split_structural_clauses(text: str) -> list[str]:
    return [part.strip() for part in _CLAUSE_SPLIT_RE.split(text) if part.strip()]


def _clause_domain_hints(clause: str) -> set[IntentType]:
    return {
        intent
        for intent, terms in _STRUCTURAL_DOMAIN_HINTS.items()
        if any(term in clause for term in terms)
    }


def _has_cross_domain_clause_risk(text: str) -> bool:
    """Detect implicit cross-domain multi-goal structure with cheap rules only.

    We intentionally use only clauses with exactly one strong domain hint. This
    keeps the gate conservative: ambiguous clauses do not become pseudo-intent
    classifications, while two separate clauses pointing to different domains
    are enough to bypass Fast Route and let the Planner resolve the request.
    """
    clauses = _split_structural_clauses(text)
    if len(clauses) < 2:
        return False

    unambiguous_domains: set[IntentType] = set()
    for clause in clauses:
        hints = _clause_domain_hints(clause)
        if len(hints) == 1:
            unambiguous_domains.update(hints)
            if len(unambiguous_domains) >= 2:
                return True
    return False


def requires_planner(text: str, context: dict[str, Any] | None = None) -> bool:
    del context  # reserved for future high-precision context-aware guards
    value = str(text or "").strip()
    if not value:
        return True

    outer, had_quoted_payload = _mask_quoted_payloads(value)

    # Text transformation/generation structure is intentionally NOT classified
    # by the cheap gate. Domain words inside the payload can make Rule and
    # Embedding agree on the wrong topic with high confidence. Escalate to the
    # Planner and let it decide the real intent from the full instruction.
    if _has_transform_risk_structure(outer, had_quoted_payload):
        return True

    if any(marker in value for marker in _CONTEXT_DEPENDENT_MARKERS):
        return True
    if any(marker in value for marker in _EXPLICIT_MULTI_GOAL_MARKERS):
        return True
    if any(marker in value for marker in _EXPLICIT_DEPENDENCY_PHRASES):
        return True
    if re.search(r"先.+(?:再|然后|之后|最后).+", value):
        return True
    if value.count("？") + value.count("?") >= 2:
        return True
    # Punctuation is only a clause boundary signal. Escalate only when two
    # different clauses carry different strong domain hints; a comma alone is
    # never enough to force Planner.
    if _has_cross_domain_clause_risk(value):
        return True
    action_hits = [term for term in _ACTION_TERMS_FAST if term in value]
    if len(set(action_hits)) >= 2 and any(connector in value for connector in ("和", "以及", "再", "然后", "同时", "另外")):
        return True
    return False

def decide_fast_route(
    text: str,
    snapshot: RoutingScoreSnapshot,
    settings: Any,
    context: dict[str, Any] | None = None,
) -> FastRouteDecision:
    if not bool(getattr(settings, "route_fast_enabled", True)):
        return FastRouteDecision(False, None, "DISABLED")
    # Keep the decision function self-contained for diagnostic runners.
    # Structural-risk requests, including text transforms, are Planner-owned.
    if requires_planner(text, context):
        return FastRouteDecision(False, None, "COMPLEX_OR_CONTEXT_DEPENDENT")
    if not snapshot.embedding_available or snapshot.embedding_scores is None:
        return FastRouteDecision(False, None, "EMBEDDING_UNAVAILABLE")

    ordered = sorted(snapshot.scores.items(), key=lambda item: (-item[1], FIXED_INTENT_ORDER[item[0]]))
    top1_intent, top1_score = ordered[0]
    _, top2_score = ordered[1]
    min_score = float(getattr(settings, "route_fast_min_score", 0.92))
    competing_score = float(getattr(settings, "route_fast_competing_score", 0.90))
    margin = float(getattr(settings, "route_fast_margin", 0.08))

    if top1_score < min_score:
        return FastRouteDecision(False, None, "TOP1_TOO_LOW")
    competing = [intent for intent, score in snapshot.scores.items() if score >= competing_score]
    if len(competing) != 1:
        return FastRouteDecision(False, None, "MULTIPLE_CANDIDATES")
    if top1_score - top2_score < margin:
        return FastRouteDecision(False, None, "MARGIN_TOO_SMALL")

    strong_rule_intents = {
        intent for intent, score in snapshot.rule_scores.items()
        if score is not None and score >= competing_score
    }
    embedding_top1 = max(
        snapshot.embedding_scores,
        key=lambda intent: (snapshot.embedding_scores[intent], -FIXED_INTENT_ORDER[intent]),
    )
    if len(strong_rule_intents) > 1:
        return FastRouteDecision(False, None, "RULE_EMBEDDING_CONFLICT")
    if strong_rule_intents:
        strong_intent = next(iter(strong_rule_intents))
        if strong_intent != top1_intent:
            return FastRouteDecision(False, None, "RULE_EMBEDDING_CONFLICT")
        if embedding_top1 != strong_intent and snapshot.embedding_scores[embedding_top1] >= competing_score:
            return FastRouteDecision(False, None, "RULE_EMBEDDING_CONFLICT")

    return FastRouteDecision(True, top1_intent, "HIGH_CONFIDENCE_SINGLE_INTENT")


def clear_prototype_vector_cache() -> None:
    with _PROTOTYPE_CACHE_LOCK:
        _PROTOTYPE_VECTOR_CACHE.clear()

def _unit_vector(vector: list[float]) -> list[float]:
    if not vector:
        return []
    norm = math.sqrt(sum(value * value for value in vector))
    if not norm:
        return [0.0 for _ in vector]
    inv = 1.0 / norm
    return [value * inv for value in vector]


def _dot_unit(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return -1.0
    # Both sides are pre-normalized, so cosine similarity is a dot product.
    return sum(a * b for a, b in zip(left, right))
