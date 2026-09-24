# MindBridge 上下文感知五类 Intent 路由简化改造指南

> 日期：2026-08-13
>
> 状态：待实施
>
> 适用仓库：`mindbridge-py`
>
> 目标：让每个普通请求都经过一次上下文语义规划，并在每个语义分段上执行 LLM、Embedding、Rule 三路五分类；彻底删除 `TaskKind`、复杂度调用门禁和所有旧 Schema 兼容代码，同时保留高风险抢占、依赖执行、澄清恢复和事件驱动多 Agent 框架。

---

## 0. 如何使用本文

本文是直接交给编码 AI 执行的改造契约，不是讨论稿。编码 AI 必须：

1. 先完整阅读本文以及本文列出的现有代码。
2. 编码前检查 Git 状态，处理已有未提交修改后再开始。
3. 严格按阶段实施，每阶段完成对应测试并建立 checkpoint。
4. 不保留旧新双实现，不增加兼容解析、shadow、灰度开关或第二套生产路由。
5. 真实模型不可用时只可报告 `NOT_RUN`，不得伪造模型评测结果。
6. 所有源码、数据集和文档保持 UTF-8，中文保持可读字符，不改写为 Unicode 转义。
7. 实施范围只包含本文阶段 0～5；不得实施 `MIND_BRIDGE_LLM_SEMANTIC_DECOMPOSITION_DEPENDENCY_REFACTOR_GUIDE_20260812.md` 的第 12 节、阶段 6或其中其他未来设计。

本文替代此前方案中以下两项设计：

- “复杂度达到阈值才调用 Understanding LLM”；
- 路由契约中的 `TaskKind` 分类维度。

此前方案关于高风险抢占、当前原文锚定、最多四个 WorkItem、依赖语义、稳定 ID、确定性 DAG、澄清恢复、单生产路径和隐私的约束继续有效，除非本文明确修改。

---

## 1. 改造目标

当前项目已经构建受限上下文，但复杂度门控只检查当前输入。低信息追问可能因为未命中固定指代词而跳过模型，例如：

- “第二种呢？”
- “那我现在该怎么办？”
- “它的入口在哪里？”

因此，本次改造要实现：

1. 除当前高风险硬抢占和澄清服务已直接消费本轮输入外，每个进入新路由的普通请求固定调用一次 Understanding LLM；规则草案不得在模型调用前以“疑似超过四项”为由拦截。
2. LLM 在一次结构化调用中同时完成上下文关系判断、语义分段、每段 Intent 候选和依赖提示。
3. LLM 分段整体通过校验后，对每个普通 ROUTE Segment 执行 LLM、Embedding、Rule 三路融合；高风险、容量和 AMBIGUOUS 使用本文规定的确定性单项控制结果。
4. 三路融合只在五个 Intent 中分类：`CHAT`、`ACADEMIC`、`CAMPUS`、`MENTAL`、`RISK`。
5. 不再区分 `STUDY_PLAN`、`CAREER_DECISION` 或其他 `TaskKind`。
6. RoutePlan、澄清、Specialist、Trace、评测和数据库中彻底删除 `taskKind`。
7. 模型失败时在同一条生产管线内使用规则草案和可用 Embedding 降级，不构造第二份 RoutePlan，不运行旧算法。
8. Coordinator 只按 Intent 分配 Specialist，依赖图继续决定执行先后。

目标流程：

```text
当前输入 + 受限上下文
        │
        ├─ 当前高风险硬信号 ───────────────→ RISK RoutePlan
        │
        └─ 普通请求
             │
             ├─ 规则草案（仅校验、依赖显式信号和失败回退）
             │
             ├─ 一次 Understanding LLM 结构化规划
             │      ├─ ROUTE：校验并采用 LLM 语义分段
             │      │     └─ AMBIGUOUS：生成单项澄清所指目标提示
             │      ├─ TOO_MANY_WORK_ITEMS：生成单项缩小范围提示
             │      └─ 失败：采用规则分段；规则确有 >4 项时生成缩小范围提示
             │
             ├─ 对每个普通 ROUTE Segment：LLM + Embedding + Rule 五分类融合
             │
             ├─ 参数抽取、ID、执行图、展示顺序和 RoutePlan V3
             │
             └─ Coordinator → Specialists → Response → Safety Review
```

---

## 2. 不可变实现决策

以下决定在本次改造中不可自行调整。

### 2.1 只有五个 Intent

生产路由只允许：

```python
class IntentType(str, Enum):
    CHAT = "CHAT"
    ACADEMIC = "ACADEMIC"
    CAMPUS = "CAMPUS"
    MENTAL = "MENTAL"
    RISK = "RISK"
```

必须删除 `TaskKind` 枚举。不得用以下任何形式重新制造同一分类层：

- `subIntent`
- `taskKind`
- `workType`
- `academicMode`
- `actionType`
- `STUDY_PLAN` / `CAREER_DECISION` 路由枚举
- 根据旧字段生成的隐藏兼容属性

“制定学习计划”和“比较考研、就业”都属于 `ACADEMIC`，由 `AcademicPlanningAgent` 根据当前 WorkItem 原文处理，不由路由层再分类。

### 2.1.1 固定输入与容量边界

以下边界属于 V3 协议常量，不再由运行时配置决定：

```python
MAX_INPUT_CHARS = 1000
MAX_WORK_ITEMS = 4
```

- `ChatRequest.message` 必须为字符串，原始值按 Python/Pydantic 字符长度计算不得超过 `1000`，且 `.strip()` 后非空；validator 只校验、不改写，继续由现有 ChatService 在持久化前统一 `.strip()`；恰好 `1000` 合法，`1001` 或纯空白必须在写入 `ChatTurn`、`ChatMessage` 和进入路由前返回 HTTP `422`；
- 不得静默截断、摘要、分片或只路由前 `1000` 个字符；
- `UnderstandingDecision.segments`、规则草案、Coordinator 可执行 WorkItem 数和容量回退都统一使用固定 `MAX_WORK_ITEMS=4`；
- 删除 `Settings.agent_max_work_items` 和部署变量 `AGENT_MAX_WORK_ITEMS`，不得再用配置把上限改为 1～3；
- 保留 `agent_max_claims_per_agent`，但 Settings 校验必须要求它 `>= MAX_WORK_ITEMS`，确保四个同 Intent WorkItem 不会因单 Agent 全程领取总额不足而饿死。`agent_max_claims_per_round` 继续只控制每轮领取批次，可小于 4；二者都不改变 RoutePlan 容量。

### 2.2 普通请求固定调用一次 Understanding LLM

删除“复杂度达到阈值才调用 LLM”的行为。不得增加新的关键词门禁、置信度门禁或环境变量开关替代它。

允许不调用 Understanding LLM 的情况只有：

1. 当前原始输入触发高风险硬抢占；
2. 澄清服务已经直接消费本轮输入，当前轮不进入新 RoutePlan。

普通问候、单一校园问题、单一学业问题和省略追问均调用一次 Understanding LLM。

规则草案可以记录 `limit_exceeded` 供模型失败时回退，但它只是启发式信号，不是模型调用门禁。LLM 返回 `ROUTE` 且最终 1～4 个 Segment 通过全部校验时，即使规则草案曾疑似拆出五项，也必须采用通过校验的模型结果。

“调用一次”指每轮只进入一次共享 Understanding structured-call adapter。adapter 内最多允许初次 provider 请求加一次结构修复请求，因此 `logicalInvocationCount == 1`、`providerAttemptCount <= 2`；不得在 repair 失败后调用第二个模型、第二套 Prompt 或重新运行路由。

### 2.3 一次模型调用同时完成分段和 LLM 分类候选

不得先调用一个模型分段，再调用另一个模型分类。不得为路由增加第二个 LLM。

同一次 `UnderstandingDecision V3` 输出：

- `routeStatus`
- `contextRelation`
- 当前输入的 `segments`
- 每个 Segment 的五类 Intent 候选与置信度
- `dependencyHints`

`routeStatus` 只允许 `ROUTE` 或 `TOO_MANY_WORK_ITEMS`，用于表达结构容量，不是 Intent、TaskKind 或业务分类：

- `ROUTE`：`segments` 必须为 1～4 个；
- `TOO_MANY_WORK_ITEMS`：模型确认当前请求包含至少五个彼此独立、需要分别执行的语义目标，`segments` 与 `dependencyHints` 必须为空；
- 模型不得为了满足上限只返回前四项，也不得把同一语义目标的多个约束误计为多个 WorkItem；
- 模型失败时，只有规则草案实际拆出超过四个独立动作跨度才进入容量回退，否则继续用不超过四项的规则分段回退。

容量回退的最终 RoutePlan 仍使用五类中的 `CHAT`，只生成一个覆盖当前完整原文的 WorkItem，`reasonCodes=["WORK_ITEM_LIMIT_EXCEEDED"]`，目标为请用户缩小到不超过四个目标。不得新增 `LIMIT`、`UNKNOWN` 等第六类 Intent。

### 2.4 三路融合只负责每个 Segment 的最终 Intent

三路信号为：

- LLM：读取受限历史，负责上下文语义主判断；
- Embedding：将当前 Segment 与五类 Intent 模板比较；
- Rule：明确关键词和安全边界证据。

不得对整条复合输入只投票一次。必须先确定 Segment，再逐 Segment 融合。高风险硬抢占、合法容量回退和 `contextRelation=AMBIGUOUS` 是确定性控制分支，不是普通分类 Segment，不运行三路融合。

### 2.5 固定融合参数，不增加运行时调参开关

第一版固定常量：

```python
FUSION_WEIGHTS = {
    "LLM": 0.70,
    "EMBEDDING": 0.20,
    "RULE": 0.10,
}
MIN_FUSED_CONFIDENCE = 0.50
MIN_FUSED_MARGIN = 0.08
EMBEDDING_MIN_SIMILARITY = 0.55
EMBEDDING_MIN_MARGIN = 0.05
FUSION_CONSTANTS_VERSION = "five-intent-fusion-v3"
```

可用信号不足三路时，只对实际可用且未弃权的信号重新归一化权重。

删除以下运行时设置及其校验：

- `intent_embedding_enabled`
- `intent_rule_weight`
- `intent_llm_weight`
- `intent_embedding_weight`
- `intent_min_confidence`
- `intent_min_margin`
- `intent_decomposition_complexity_threshold`
- `intent_dependency_min_confidence`
- `intent_segment_source_max_chars`
- `intent_embedding_provider`
- `intent_embedding_model`
- `intent_planning_diagnostics_enabled`
- `agent_max_work_items`

Embedding 必须尝试运行；后端不可用、异常或相似度不足时该路信号弃权，不通过布尔开关切换算法。

`sourceText` 最大长度固定为 V3 Schema 的 `1000`，WorkItem 上限固定为 `4`，HARD_DATA LLM 阈值固定为 `0.82`；三者不再从运行时 settings 读取。保留 `intent_context_max_tokens`，因为它只约束 Understanding 上下文预算。保留项目统一的 `knowledge_embedding_provider/knowledge_embedding_model` 及现有 backend timeout 配置；Intent fusion 复用 `create_embedding_backend(settings)`，不再拥有单独的 provider/model 覆盖项。统一 provider 设为 `disabled` 或 backend `available()==False` 时该路按“不可用”弃权，不新增 Intent 专属开关。

后续如需调整固定阈值，必须依据标注数据集的离线校准报告修改代码和测试，不得凭单个样例修改。

### 2.6 规则未命中必须弃权

当前代码把“未命中任何领域词”当作高置信 `CHAT=0.96`，必须删除。

规则只有在明确命中时才能投票，例如：

- 明确问候、寒暄或技术通用问题 → `CHAT`
- 明确学业词 → `ACADEMIC`
- 明确校园制度、办理词 → `CAMPUS`
- 明确非高风险心理支持词 → `MENTAL`
- 当前高风险词 → 由前置安全硬规则直接处理，不进入普通融合

规则没有明确证据或同时出现冲突领域时返回 `None`，不得以默认 CHAT 参与投票。

如果 LLM、Embedding、Rule 全部不可用或最终分数不足，则生成低置信 `CHAT` WorkItem，reasonCode 使用 `AMBIGUOUS_ROUTING`，由通用路径请求用户明确目标。不得新增第六个 `UNKNOWN` Intent。

### 2.7 高风险语义不变

当前原始输入的风险硬检测仍然位于模型之前。明确当前自伤、自杀、伤人或即时危险时：

- 只生成一个 `RISK` WorkItem；
- 阻断普通 Specialist；
- SafetyAgent 继续独立评估和最终复审；
- 历史中的风险文本、引用和第三人称描述不能单独触发本轮 RISK。

普通三路融合不得绕过该安全边界。

LLM 仍可对硬规则未覆盖、但当前 `sourceText` 本身表达危险的语句给出 `RISK` 候选。如果任一最终 Segment 融合为 `RISK`，必须把本轮收敛为单个 RISK RoutePlan 并触发 Safety override，不得继续执行其他普通 WorkItem。历史文本中的风险内容不能作为该候选的当前证据。

普通路径的 RISK 融合还必须满足：LLM 对该当前 Segment 的候选本身就是 `RISK`。Embedding 可以输出 RISK 相似度并参与诊断，但 Embedding 单路或 Embedding 与非 RISK Rule 的组合不得把普通 Segment 升级为 RISK；Rule 的当前高风险证据已在前置硬路径处理，不在普通融合重复投票。满足 LLM 当前原文 RISK 条件且融合后 RISK 获胜时，才执行单项安全收敛。

### 2.8 模型计划必须整份接纳或整份放弃

模型输出必须同时通过：

- Pydantic V3 Schema；
- 当前原文精确锚定；
- 唯一匹配；
- Segment 不重叠、不重复；
- 当前有效文本完整覆盖；
- WorkItem 容量；
- 上下文关系有真实上下文支撑；
- 依赖端点有效；
- 依赖图无环。

任何一项失败时，整份 LLM 分段、Intent 候选和依赖提示全部放弃。不得保留一部分模型标签再回退其他部分。

依赖校验的边界固定如下：

1. 先根据规则草案构造规则执行图和规则展示图；规则图自身成环属于程序不变量错误，必须抛出，不得回退；
2. LLM `HARD_DATA` 置信度低于 `0.82` 时，该条 hint 按阈值弃权并记录内部诊断，不使整份模型计划非法；
3. 其余 LLM hint 必须端点有效、同一无向 Segment 对唯一、reasonCode 与 relation 匹配，且与规则边合并后执行图和展示图均无环；
4. LLM hint 映射到锚定 Segment 后端点塌缩为同一 Segment也属于第 3 项非法；任一合格阈值后的 LLM hint 违反第 3 项时，整份 LLM 决策以 `DEPENDENCY_GRAPH_INVALID` 放弃，改用规则分段和纯规则依赖图；不得逐边试加、只丢掉导致成环的那一条；
5. 回退后再次构造出的纯规则图若成环，或最终 RoutePlan 图不变量失败，属于程序错误，必须抛出。

### 2.9 依赖语义保持不变

- `HARD_DATA`：下游必须读取上游结果，写入 `dependsOn`，影响实际执行。
- `ORDER_ONLY`：只影响展示/合成顺序，不写入 `dependsOn`。
- “先 A 再 B”默认只证明 `ORDER_ONLY`。
- “根据 A 的结果、日期、政策、条件做 B”才可形成 `HARD_DATA`。
- LLM HARD_DATA 置信度阈值继续固定为 `0.82`。
- 继续使用执行图和展示图，禁止传递约简，禁止成环。

### 2.10 单一生产路径和硬切换

本次使用 Schema V3 一次性切换：

- `UnderstandingDecision V3`
- `RoutePlan V3`
- `ClarificationRequest V3`
- 新的五类 Intent 评测契约

生产解析器只接受 V3。不得：

- 同时接受 V2/V3；
- `payload.get("taskKind")` 后忽略；
- 给旧字段设置 alias；
- 保留旧类并做 adapter；
- 写 `if schemaVersion == 2`；
- 保留旧数据集 loader；
- 增加 `USE_NEW_ROUTER`、`ENABLE_V3` 等开关；
- 生产中 shadow 运行两份 RoutePlan；
- 用异常捕获静默转向旧实现。

发布回滚使用 Git 版本和数据库快照，不在同一代码版本保留旧生产算法。

---

## 3. V3 数据契约

### 3.1 UnderstandingDecision V3

结构固定如下，字段、类型和约束不得自行增删：

```python
class ContextRelation(str, Enum):
    NEW_TOPIC = "NEW_TOPIC"
    CONTINUE = "CONTINUE"
    REFINE = "REFINE"
    CORRECTION = "CORRECTION"
    AMBIGUOUS = "AMBIGUOUS"

RouteStatus = Literal["ROUTE", "TOO_MANY_WORK_ITEMS"]

SegmentReasonCode = Literal[
    "EXPLICIT_SINGLE_GOAL",
    "MULTI_DELIVERABLE",
    "CROSS_INTENT",
    "SAME_INTENT_DISTINCT_GOAL",
    "CONTEXT_CONTINUATION",
    "HARD_DATA_DEPENDENCY",
    "ORDER_ONLY_REQUEST",
]

class IntentSegmentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    sourceText: str = Field(min_length=1, max_length=1000)
    intent: IntentType
    confidence: float = Field(ge=0.0, le=1.0, strict=True)
    reasonCodes: list[SegmentReasonCode] = Field(max_length=8)

    @field_validator("sourceText")
    @classmethod
    def require_visible_source(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("sourceText 不能为空白")
        return value  # 不改写模型原文，锚定需要逐字符一致

class DependencyHint(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    sourceIndex: int = Field(ge=0, strict=True)
    targetIndex: int = Field(ge=0, strict=True)
    relation: Literal["HARD_DATA", "ORDER_ONLY"]
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
    segments: list[IntentSegmentDecision] = Field(max_length=4)
    dependencyHints: list[DependencyHint] = Field(max_length=6)
```

上述所有字段都必须由模型显式输出，不得添加默认值来掩盖漏字段。`IntentSegmentDecision.reasonCodes` 和 `dependencyHints` 可以是空数组，但字段不能缺失；`segments` 的条件性最小长度只由下面的 model validator 实现。`ContextRelation` 只在 `app/services/intent_fusion.py` 定义一次，评测契约直接导入，不能再复制字符串 Literal。

交叉校验：

- `routeStatus=ROUTE` 时，`segments` 必须为 1～4 个；
- `routeStatus=TOO_MANY_WORK_ITEMS` 时，`segments=[]` 且 `dependencyHints=[]`；
- `TOO_MANY_WORK_ITEMS` 的 `contextRelation` 只能是 `NEW_TOPIC/CONTINUE/REFINE/CORRECTION`，不能同时声称 `AMBIGUOUS`；
- `TOO_MANY_WORK_ITEMS` 只表示至少五个独立执行目标，不得把同一目标的多个属性、约束或补充说明算成多个目标；
- `contextRelation=AMBIGUOUS` 且 `routeStatus=ROUTE` 时，Schema 层必须只有一个 `CHAT` Segment 且 `dependencyHints=[]`；随后锚定/覆盖校验必须证明该 Segment 等于当前完整有效输入，否则整份输出非法；
- 所有依赖索引必须引用有效 Segment；
- 同一无向 Segment 对只能出现一个关系；
- `reasonCodes` 数组不得重复；所有数组成员必须是声明的原生类型，不能用 `str()`/`int()` 静默归一化模型错误；
- `HARD_DATA` 与 `ORDER_ONLY` 的 reasonCode 必须合法；
- `HARD_DATA` 只允许前三个 `TARGET_USES_*` reasonCode，`ORDER_ONLY` 只允许 `USER_REQUESTED_ORDER_ONLY`；
- 普通路径中的 LLM `RISK` 必须有当前 Segment 原文支撑；最终融合为 RISK 时收敛为单项 RISK 计划并触发安全覆盖。

内部类型也必须同步收窄，避免阶段切换后仍依赖 `TaskKind`：

```python
@dataclass(frozen=True)
class RuleSegmentDraft:
    source_text: str
    start: int
    end: int
    rule_candidate: IntentCandidate | None

@dataclass(frozen=True)
class AnchoredSegment:
    source_text: str
    start: int
    end: int
    original_index: int

@dataclass(frozen=True)
class UnderstandingInvocationResult:
    decision: UnderstandingDecision
    provider_attempt_count: int
    latency_ms: int

    def __post_init__(self) -> None:
        if not 0 <= self.provider_attempt_count <= 2 or self.latency_ms < 0:
            raise ValueError("Understanding invocation metadata 无效")

RuleClassifier = Callable[[str], IntentCandidate | None]
```

`AnchoredSegment` 只表示文本跨度，不得保存 `intent`、`task_kind`、`confidence` 或 reasonCodes；LLM 候选通过 `original_index` 从已接纳的 `UnderstandingDecision` 读取。`RuleSegmentDraft.rule_candidate` 必须可空，因为规则弃权是正常结果。

`app/services/understanding.py` 是唯一 Understanding structured-call adapter，其公开 API 固定为：

```python
class UnderstandingService:
    def __init__(self, client: AIClient, settings: Settings): ...

    def classify(
        self,
        planning_input: str,
        memory_context: dict[str, Any],
    ) -> UnderstandingInvocationResult: ...
```

`classify()` 只接收已经裁剪好的 `memory_context`，调用 `AIClient.complete_structured()` 一次，并返回 `UnderstandingInvocationResult`；测试注入的 `semantic_classifier` 也必须遵守这一 callable 返回契约，不再返回含糊的 `dict | str` 联合类型。一次 repair 只改变 `provider_attempt_count`，不会产生第二次逻辑调用。`UnderstandingAgent` 使用 `UnderstandingService(self.client(), settings).classify`；`ProductionRoutingEvaluator` 使用 `AgentModelRegistry(settings).client_for("UnderstandingAgent")` 构造同一服务，不得直接调用 AIClient 或 Prompt。

`classify_route()` 的 `semantic_classifier` 类型固定为 `Callable[[str, dict[str, Any]], UnderstandingInvocationResult] | None`；生产必须传共享 service method，`None` 只供显式验证 `MODEL_UNAVAILABLE` 回退的内部/测试入口。不得保留默认 dict/string parser。`settings` 和 `raw_current_input` 在生产调用中必须显式传入；单元测试若省略外部服务，也应注入符合契约的成功或失败 fake，不能无意间把模型未配置回退当成普通成功路径。

adapter 调用参数固定为：`messages=build_intent_prompt(memory_context, planning_input)`、`response_model=UnderstandingDecision`、`schema_name="understanding_decision_v3"`、`purpose="understanding.semantic_plan"`；`StructuredCompletionOptions` 使用现有 `agent_model_understanding_temperature`、`agent_model_understanding_max_tokens`、`agent_loop_deadline_seconds`，并固定 `repair_attempts=1`。成功直接使用 `completion.value`，不得 `model_dump()` 后再宽松解析；attempt 由 `completion.repair_count` 计算。模型 profile/provider/model 仍由现有 `AgentModelRegistry` 和 AIClient 负责，不在 adapter 中再造配置分支。

### 3.2 RoutePlan V3

```json
{
  "schemaVersion": 3,
  "planId": "plan_xxx",
  "primaryIntent": "ACADEMIC",
  "intents": ["ACADEMIC"],
  "workItems": [
    {
      "workItemId": "wi_xxx",
      "intent": "ACADEMIC",
      "objective": "比较考研和就业",
      "sourceText": "比较考研和就业",
      "knownArguments": {},
      "missingArguments": [],
      "dependsOn": [],
      "priority": "NORMAL",
      "confidence": 0.91,
      "reasonCodes": ["ACADEMIC_SIGNAL"]
    }
  ],
  "synthesisOrder": ["wi_xxx"],
  "confidence": 0.91,
  "reasonCodes": ["ACADEMIC_SIGNAL"]
}
```

WorkItem 允许字段固定为：

```text
workItemId
intent
objective
sourceText
knownArguments
missingArguments
dependsOn
priority
confidence
reasonCodes
```

必须删除 `taskKind`。`extra="forbid"` 或等价严格字段校验必须拒绝带 `taskKind` 的旧 payload。

RoutePlan/WorkItem V3 的唯一 `from_payload()` 和 `as_payload()` 还必须执行以下结构不变量；不得只依赖调用方约定：

- `workItems` 为 1～`MAX_WORK_ITEMS` 项，`workItemId` 唯一；
- `sourceText` 为非空字符串且最多 `1000` 字符；
- `objective` 非空且最多 `240` 字符，`knownArguments` 为 `dict[str, str]`，`missingArguments` 逐项通过严格 MissingArgument Schema；
- `priority` 只允许 `NORMAL/HIGH/CRITICAL`；普通多项计划在最终 `synthesisOrder` 中第一项为 `HIGH`、其余为 `NORMAL`，普通单项/容量/歧义为 `NORMAL`，RISK 为 `CRITICAL`；
- `knownArguments` 的 key/value、`dependsOn` 和 reasonCodes 必须为原生字符串，不允许用 `str(...)` 把数字、对象或 `None` 静默转成字符串；数组中不得重复；
- `synthesisOrder` 恰好是全部 `workItemId` 的无重复全排列；
- 每个 `dependsOn` 都引用同一计划内的不同 WorkItem，执行依赖图无环，且上游在 `synthesisOrder` 中早于下游；
- `primaryIntent` 等于 `synthesisOrder[0]` 对应 WorkItem 的 Intent；`intents` 等于按 `synthesisOrder` 首次出现顺序去重后的 Intent；
- 若任一 WorkItem 为 `RISK`，计划必须且只能有该一个 RISK WorkItem；非 RISK 单项/多项计划不得含 CRITICAL priority；
- RoutePlan `confidence` 等于全部 WorkItem confidence 的最小值；
- RoutePlan reasonCodes 是按 `synthesisOrder` 收集 WorkItem reasonCodes 后稳定去重的结果，多项时追加 `COMPOUND_REQUEST`，存在 HARD_DATA 时追加 `DEPENDENCY_DETECTED`；全部值必须属于第 5.1 节公共 allowlist；
- payload 任一层出现未知字段、旧 `taskKind`、非法枚举、非有限浮点数或不变量失败都必须拒绝。

集合边界固定为：`knownArguments` 最多 32 项，key 为去首尾空白后 1～64 字符、value 为 1～500 字符；`missingArguments` 最多 10 项且 name 唯一；`dependsOn` 最多 4 项；WorkItem/RoutePlan `reasonCodes` 最多 16 项且不重复。严格 parser 可以校验空白，但不得靠 `str(...)`、`.upper()` 或删除坏项来修复 payload；枚举必须已经是协议中的大写值。builder 自己负责生成规范值。

`MissingArgument` 唯一 Schema 字段固定为 `name/reasonCode/allowedValues`，三者都显式必填且拒绝未知字段；前两项是去首尾空白后 1～64 字符的字符串，`allowedValues` 最多 20 个、逐项为 1～200 字符且不重复。handler 仍负责验证 name、reasonCode 和 allowedValues 与 Intent FIELD_REGISTRY 完全匹配。`from_payload()` 遇到坏项必须拒绝整个 WorkItem/ClarificationRequest，不能像旧实现一样丢掉坏项后继续。

因为 RoutePlan payload 本身不保存 `planning_input` 和文本跨度，连续原文、唯一锚定、互不重叠及完整覆盖不能伪装成 `from_payload()` 的无上下文校验。必须另提供 `RoutePlan.validate_against_input(planning_input, *, control_kind=None)`：正常计划重新唯一锚定并执行第 2.8 节覆盖规则；`control_kind in {"RISK","LIMIT","AMBIGUOUS"}` 时要求唯一 WorkItem 的 `sourceText == planning_input`。生产 builder 在返回前必须调用。ClarificationRequest 的 `originalMessage` 是原始输入，而 RoutePlan `sourceText` 基于隐私脱敏后的 `planning_input`，两者不得直接比较；澄清恢复只做严格结构、plan/work ID 和 expected field 校验，不能用原始消息误判合法脱敏 RoutePlan。

### 3.3 ClarificationRequest V3

外部 payload 的唯一合法结构固定为：

```json
{
  "schemaVersion": 3,
  "originPlanId": "plan_xxx",
  "targetWorkItemId": "wi_xxx",
  "intent": "ACADEMIC",
  "objective": "帮我制定复习计划",
  "originalMessage": "帮我制定复习计划",
  "knownArguments": {},
  "missingArguments": [
      {"name": "course", "reasonCode": "COURSE_SCOPE_REQUIRED", "allowedValues": []}
  ],
  "resumeContext": {
    "schemaVersion": 3,
    "routePlan": {
      "schemaVersion": 3,
      "planId": "plan_xxx",
      "primaryIntent": "ACADEMIC",
      "intents": ["ACADEMIC"],
      "workItems": [{
        "workItemId": "wi_xxx",
        "intent": "ACADEMIC",
        "objective": "帮我制定复习计划",
        "sourceText": "帮我制定复习计划",
        "knownArguments": {},
        "missingArguments": [
          {"name": "course", "reasonCode": "COURSE_SCOPE_REQUIRED", "allowedValues": []},
          {"name": "deadline", "reasonCode": "TARGET_DATE_REQUIRED", "allowedValues": []}
        ],
        "dependsOn": [],
        "priority": "NORMAL",
        "confidence": 0.91,
        "reasonCodes": ["ACADEMIC_SIGNAL"]
      }],
      "synthesisOrder": ["wi_xxx"],
      "confidence": 0.91,
      "reasonCodes": ["ACADEMIC_SIGNAL"]
    },
    "originPlanId": "plan_xxx",
    "targetWorkItemId": "wi_xxx",
    "expectedFields": ["course"],
    "roundCount": 0
  }
}
```

严格约束：

- 顶层、`missingArguments[*]` 和 `resumeContext` 均拒绝未知字段；
- `schemaVersion` 及 `resumeContext.schemaVersion` 都必须严格等于 `3`；
- ID、`objective`、`originalMessage`、missing argument 的 `name/reasonCode` 均为非空字符串，`originalMessage` 最大 `1000` 字符；
- `intent` 只允许五类 Intent；`knownArguments` 固定为 `dict[str, str]`；
- `ClarificationResumeContext.expectedFields` 结构上允许 0～1 项：活跃 `WAITING_USER`/嵌入 ClarificationRequest 时必须恰好一项，已解决并准备恢复执行时必须为空；
- 一次 ClarificationRequest 恰好只携带一个 missing argument，嵌套的 `expectedFields` 也恰好一项，且二者字段名相同；
- `expectedFields[0]` 必须存在于当前 `intent` 的 `FIELD_REGISTRY`；
- `resumeContext.originPlanId/targetWorkItemId` 必须分别等于顶层同名字段；
- `resumeContext.routePlan` 必须立即通过唯一的 RoutePlan V3 解析器校验，且包含目标 WorkItem；该 WorkItem 的 intent/objective/knownArguments 必须分别等于顶层 intent/objective/knownArguments，并且包含当前 request 的 missing argument；
- `roundCount` 为非负整数；`ClarificationRequest.from_payload()` / `as_payload()` 是唯一序列化边界，不接受 V2、`taskKind` 或旧字段 alias。

`ClarificationResumeContext V3` 自身的交叉校验还要求 `originPlanId == routePlan.planId`、`targetWorkItemId` 存在；`expectedFields` 非空时每一项都存在于目标 WorkItem 的 missingArguments，且只能一项；为空时目标 WorkItem 的 missingArguments 也必须为空。`roundCount` 是严格非负整数，不接受 bool/字符串。该 context 的严格 parser 同时用于 request 创建、数据库恢复和 runtime seed，不能各写一份。

删除 `taskKind`。澄清字段注册表改为：

```python
FIELD_REGISTRY: dict[IntentType, dict[str, ClarificationFieldSpec]]
```

`get_clarification_handler()` 固定签名改为 `(intent, settings=None)`，只做 Intent 严格解析和 registry 查询；ACADEMIC/CAMPUS 返回 handler，CHAT/MENTAL/RISK 返回 `None`。`WorkItemClarificationHandler` 只保存 Intent。`ClarificationTarget` 字段固定为 `plan_id, work_item_id, intent, objective, known_arguments, missing_argument`，删除 task kind；downstream count、priority 和 synthesis index 继续只存在于 `ClarificationPolicy.select()` 的排序 tuple，不进入 target 或 payload。`TaskParseResult/TaskParseStatus` 当前无生产调用，整类删除，`SlotExtraction` 保留。

只需注册：

- `ACADEMIC`：`course`、`deadline`、`availableTimeWindows`、`focusProblem`
- `CAMPUS`：`studentType`、`site`、`academicPeriod`、`policyName`、`serviceItem`、`referent`

只有当前原文明确要求生成具体学习/复习安排时，参数抽取器才为 ACADEMIC 生成计划字段。职业比较、普通学习建议和论文讨论不得因为同属 ACADEMIC 自动追问课程与截止日期。

必须保留一个纯函数判断“当前请求是否要求具体计划”，但它：

- 只决定参数抽取和工具使用；
- 不写入 RoutePlan；
- 不形成枚举或持久字段；
- 不参与 Coordinator 路由；
- 不得被命名或演化为新的 TaskKind。

该函数固定放在 `app/services/academic_request_policy.py::is_concrete_study_plan_request()`；`routing.py`、`clarification_handlers.py` 和 `autonomous.py` 只能调用它，不得各自保留计划关键词。它只返回布尔值，并至少保持以下边界：

- 明确请求“帮我制定/生成/设计学习计划或复习计划”时为 `True`；
- 在请求计划的同时明确提供“我的课程表、截止日期、本学期安排”等个性化约束时可为 `True`；仅询问这些事实本身不能触发；
- “给我一些学习建议”“比较考研和就业”“论文怎么推进”必须为 `False`；
- 返回值不得序列化、持久化、进入 ID seed、Coordinator 路由或评测 Intent 标签。

为避免三个旧实现合并后语义漂移，第一版纯函数算法固定为：

```python
PLAN_NOUNS = ("学习计划", "复习计划", "学习规划", "复习规划", "课程计划", "课程表", "学习安排", "复习安排")
PLAN_VERBS = ("帮我", "制定", "制订", "生成", "设计", "安排", "规划", "做一份")
PERSONAL_CONSTRAINTS = ("我的课程表", "我的截止", "我这学期", "我本学期", "可用时间", "每周能学", "每天能学")
PERSONAL_PLAN_MARKERS = ("计划", "规划", "安排", "复习")

normalized = text.strip()
return (
    any(noun in normalized for noun in PLAN_NOUNS)
    and any(verb in normalized for verb in PLAN_VERBS)
) or (
    any(term in normalized for term in PERSONAL_CONSTRAINTS)
    and any(marker in normalized for marker in PERSONAL_PLAN_MARKERS)
)
```

只做首尾空白规范化，不调用模型和 Embedding；中文匹配不做 Unicode 转义或隐式同义扩展。`“给我一些学习建议”`、`“论文怎么推进”`、`“比较考研和就业”`、`“课程表是什么”`、`“我的课程表是什么”` 必须为 False；`“根据我的课程表制定本学期计划”` 必须为 True。

ACADEMIC 参数抽取只在该函数为 True 时运行，复用现有课程与日期正则；已抽到 `course/deadline` 则写 known，否则按固定顺序生成 `course`、`deadline` missing。`availableTimeWindows/focusProblem` 只接受用户主动提供或后续已经成为 expected field 时抽取，第一轮不得主动把它们加入 missing。CAMPUS 继续复用现有抽取语义。每个 WorkItem 可以含多个 missing，但 `ClarificationPolicy` 继续一次只选择一个，顺序按 FIELD_REGISTRY 声明顺序。

### 3.4 SpecialistResult V2

当前 Specialist result 的 `schemaVersion=1` payload 含有 `taskKind`。删除字段属于破坏性契约变化，因此必须同时升级为 `schemaVersion=2`，不得删除字段后继续冒充 V1。

V2 保留现有执行所需字段：

```text
schemaVersion (=2)
planId
workItemId
intent
agentName
status
objective
knownArguments
answerBrief
keyPoints
evidenceItems
citationRefs
answerConstraints
assumptions
reasonCode
selectedSkillIds
toolSummary
dependencyResultIds
contextManifest
confidence
```

V2 不得包含 `taskKind`、`subIntent` 或替代字段。生产只生成并消费 V2；不得保留 Specialist result V1 adapter。`ResponseAgent`、Trace、turn execution、事件黑板和测试 fixture 必须同步使用 V2。

在 `app/agents/result.py` 定义严格 `SpecialistResultV2` Pydantic 模型（`extra="forbid"`），并提供唯一 `as_payload()`/`from_payload()` 边界。`SpecialistAgent` 发布前构造并校验 V2；`CollaborationBlackboard.add_artifact()` 在 `artifact.kind == "specialist_result"` 时先用 V2 校验，再检查 plan/work metadata 一致性；`ordered_specialist_results()` 和 Response 只消费校验后的 V2 payload。不得只修改版本号而继续传递任意 dict。

为避免把本次改造扩大成 evidence/tool 契约重写，V2 字段约束固定为：ID、`agentName/objective/answerBrief/reasonCode` 为非空字符串；`intent` 为五类；`status` 只允许 `COMPLETED/PARTIAL/FAILED`；`knownArguments` 为 `dict[str,str]`；`keyPoints/citationRefs/answerConstraints/assumptions/selectedSkillIds/dependencyResultIds` 为字符串数组；`evidenceItems` 为字典数组；`toolSummary/contextManifest` 为字典；`confidence` 为有限 `[0,1]`。字符串数组稳定去重，未知嵌套字段本次不额外收紧。`as_payload()` 使用 camelCase；`from_payload()` 只接受 camelCase 和 `schemaVersion=2`。

`add_artifact()` 校验后必须用 `dataclasses.replace(artifact, payload=normalized_payload)` 把 `SpecialistResultV2.as_payload()` 写回不可变 artifact，再做单终态检查；不得只校验临时变量却保存原始 dict。它还必须通过最新 `route_plan` 的唯一 V3 parser 找到相同 plan/work：result 的 `intent/objective/knownArguments` 必须等于对应 WorkItem，`dependencyResultIds` 必须按 WorkItem `dependsOn` 顺序等于已有直接上游 specialist_result artifact ID；payload 和 artifact metadata 的 plan/work ID 必须一致。由此 Specialist 不能改变 RoutePlan Intent、参数或依赖。`ordered_specialist_results()` 对已存新 artifact 再走 `from_payload()` 并返回规范化 payload，遇到非法 V2 属于程序/状态错误，不静默跳过。其他 artifact kind 不受这个 Schema 影响。

---

## 4. 详细算法

### 4.1 输入边界

- `planning_input`：`(board.model_input or board.user_input).strip()`，用于分段、锚定、分类、参数抽取和 ID。
- `raw_current_input`：原始用户当前输入，只用于当前风险硬检测。
- `memory_context`：`TurnContextPacket.for_understanding(planning_input)` 调用共享 builder 得到的受限上下文，只用于上下文关系和省略语义，不得写入 Segment `sourceText`。
- HTTP 边界已经保证原始 `message` 长度 `<= MAX_INPUT_CHARS`；`planning_input` 必须非空且自然也不超过该上限。内部直接调用 `classify_route()` 的测试和后台入口仍必须显式断言该不变量，不能依赖 Segment Schema 事后兜底。

生产 Harness 必须保持现有顺序：先对 `raw_current_input` 做当前风险分析，再将隐私脱敏后的 `model_input` 作为 `planning_input` 传给路由和模型。`UnderstandingAgent.act()` 必须先只计算一次 `planning_input=(board.model_input or board.user_input).strip()`，再同时用于 `context_packet.for_understanding(planning_input)` 和 `classify_route(planning_input, ...)`；禁止 context view 中仍使用 packet 的未脱敏 `current_input`。手机号、邮箱或身份证被替换为 `[已脱敏]` 后，Segment/ID/参数自然基于脱敏文本；不得为了锚定把原始敏感值传入 Understanding 模型，也不得用脱敏历史触发当前高风险。

### 4.2 主流程伪代码

```python
def classify_route(planning_input, memory_context, semantic_classifier, settings, raw_current_input, *, fusion=None):
    assert 1 <= len(planning_input) <= MAX_INPUT_CHARS

    risk = analyze_risk_signal(raw_current_input)
    if risk.is_current_high_risk:
        return build_risk_route_v3(...)  # 返回 RouteDecision；llmInvoked=False

    rule_draft = build_rule_route_draft(
        planning_input, explicit_rule_candidate_or_none, limit=MAX_WORK_ITEMS
    )
    rule_fallback_segments = rule_segments(rule_draft)
    rule_fallback_graphs = build_and_validate_rule_dependency_graphs(
        rule_fallback_segments, rule_draft
    )  # 纯规则图成环立即抛出；即使后续模型返回容量/歧义也不吞掉程序错误

    invocation = None
    try:
        if semantic_classifier is None:
            raise SemanticPlannerUnavailable(
                "semantic classifier is unavailable",
                provider_attempt_count=0,
                latency_ms=0,
            )
        invocation = semantic_classifier(planning_input, memory_context)  # 逻辑调用固定一次
        decision = invocation.decision  # adapter 已完成 V3 strict validation
        validate_context_relation(decision, memory_context)

        if decision.routeStatus == "TOO_MANY_WORK_ITEMS":
            return build_limit_route_v3(
                planning_input,
                context_relation=decision.contextRelation,
                plan_source="LLM_LIMIT",
                invocation=invocation,
            )

        segments = reconcile_segments(
            planning_input, rule_draft, decision, limit=MAX_WORK_ITEMS
        )

        if decision.contextRelation == "AMBIGUOUS":
            return build_ambiguous_route_v3(
                planning_input,
                context_relation="AMBIGUOUS",
                confidence=0.0,
                reason_codes=("AMBIGUOUS_ROUTING",),
                plan_source="LLM_ACCEPTED",
                invocation=invocation,
            )

        accepted_graphs = validate_and_build_accepted_dependency_graphs(
            segments, rule_draft, decision
        )
        accepted_decision = decision
        fallback_reason = ""
    except EXPECTED_SEMANTIC_PLANNING_ERRORS as exc:
        fallback_reason = exc.fallback_reason
        if rule_draft.limit_exceeded:
            return build_limit_route_v3(
                planning_input,
                context_relation="NEW_TOPIC",
                plan_source="RULE_LIMIT_FALLBACK",
                fallback_error=exc,
            )
        segments = rule_fallback_segments
        accepted_graphs = None
        accepted_decision = None

    intent_fusion = fusion or IntentFusion(settings)  # settings 只用于统一 Embedding backend
    final_items = []
    for segment in segments:
        fused = intent_fusion.fuse_five_intents(
            candidate_from_llm_or_none(segment, accepted_decision),
            intent_fusion.embedding_candidate_or_none(segment.source_text),
            explicit_rule_candidate_or_none(segment.source_text),
        )
        known, missing = extract_arguments(fused.intent, segment.source_text)
        final_items.append(build_work_item_draft(segment, fused, known, missing))

    if any(item.intent == IntentType.RISK for item in final_items):
        return build_risk_route_v3(...)  # 单项安全收敛；返回 RouteDecision

    graphs = accepted_graphs or rule_fallback_graphs
    execution_graph, presentation_graph = graphs.execution, graphs.presentation
    synthesis_order = stable_topological_order(segments, presentation_graph)
    diagnostics = build_diagnostics_and_remap_edges_to_synthesis_order(...)
    return build_and_validate_route_decision_v3(
        final_items, execution_graph, synthesis_order, diagnostics, fallback_reason
    )
```

上述所有 builder 都返回现有外壳 `RouteDecision(route_plan: RoutePlan, diagnostics: PlanningDiagnostics)`；公共 `RoutePlan.as_payload()` 仍只序列化第 3.2 节字段，diagnostics 只经 `RouteDecision`/Trace 白名单输出，不得混入 RoutePlan V3。`build_and_validate_route_decision_v3()` 必须先完成 RoutePlan 结构校验和 `validate_against_input()`，再构造返回值。

`build_ambiguous_route_v3()` 固定只生成一个覆盖 `planning_input` 全文的 CHAT WorkItem，`objective="请用户明确当前所指的目标"`、`confidence=0.0`、`reasonCodes=["AMBIGUOUS_ROUTING"]`、无依赖、无 slot clarification。其 diagnostics 使用 `planSource=LLM_ACCEPTED`、`llmSegmentCount=1`、`acceptedSegmentCount=1`、边数为 `0`。该分支不运行 Embedding/Rule 融合，也不接纳或构造任何最终依赖边；模型调用前的纯规则图不变量检查已经完成，不重复构图。

`build_limit_route_v3()` 同样只生成一个覆盖当前全文的 CHAT WorkItem，`objective="请将请求缩小到不超过四个独立目标"`、`confidence=0.0`、`reasonCodes=["WORK_ITEM_LIMIT_EXCEEDED"]`、无依赖。合法模型结果使用模型给出的已校验 `contextRelation`；规则失败回退固定用 `NEW_TOPIC`，不得从未接纳的模型 payload 泄漏上下文关系。

`validate_and_build_accepted_dependency_graphs()` 必须一次返回已经校验的执行图和展示图，主流程不得随后以另一套合并规则重新构图。规则边映射到已锚定 LLM Segment 后：若规则边两个端点被合并到同一 LLM Segment，则该边已成为同一目标内部关系，直接从接纳图省略，不视为模型非法；其余同方向同 relation 的重复边合并并保留 Rule 来源；同一无向对若方向或 relation 不同则视为 LLM 与规则冲突，使整份模型计划以 `DEPENDENCY_GRAPH_INVALID` 回退。LLM hint 自身映射后端点塌缩则非法。低于 `0.82` 的 LLM HARD_DATA 在冲突与成环检查前先弃权。回退路径复用模型调用前已构造的纯规则图；该图成环直接抛出不变量错误。

`classify_route()` 保留 Settings 参数只为构造项目统一 Embedding backend；固定权重、阈值、容量和调用门禁一律不得读取 Settings。测试可通过 keyword-only `fusion` 注入同接口 fake，以验证 unavailable/边界而不连接真实 Embedding。生产不传 fake。

### 4.3 模型异常边界

`EXPECTED_SEMANTIC_PLANNING_ERRORS` 必须是显式元组，只含下列 typed exception，不得包含 `Exception`、`RuntimeError` 或数据库异常：

```python
EXPECTED_SEMANTIC_PLANNING_ERRORS = (
    SemanticPlannerUnavailable,       # MODEL_UNAVAILABLE
    SemanticPlannerTimeout,           # MODEL_TIMEOUT
    SemanticStructuredOutputInvalid,  # STRUCTURED_OUTPUT_INVALID
    SemanticContextRelationInvalid,   # CONTEXT_RELATION_INVALID
    SegmentAnchorFailed,               # SEGMENT_ANCHOR_FAILED
    SegmentCoverageFailed,             # SEGMENT_COVERAGE_FAILED
    SegmentOverlapOrDuplicate,         # SEGMENT_OVERLAP
    SemanticDependencyGraphInvalid,    # DEPENDENCY_GRAPH_INVALID
)
```

这些异常统一继承 `ExpectedSemanticPlanningError`，并携带只读 `fallback_reason`、`provider_attempt_count` 和 `latency_ms`。`app/services/understanding.py` 负责把 provider 层异常一次性翻译：

- 模型/客户端未配置、provider 无法连接、`STRUCTURED_OUTPUT_UNSUPPORTED` → `SemanticPlannerUnavailable`；
- 明确的 provider timeout / `TimeoutError` → `SemanticPlannerTimeout`；
- provider 已返回但为空、提前 EOF、JSON 非法、Pydantic V3 非法、一次 repair 仍非法、`MODEL_PROTOCOL_ERROR` → `SemanticStructuredOutputInvalid`；
- 未列出的 AI、程序和数据库异常原样抛出。

现有 `StructuredCompletionError.code` 的固定映射为：`STRUCTURED_OUTPUT_UNSUPPORTED/PROVIDER_UNAVAILABLE/MODEL_UNAVAILABLE/PROVIDER_REQUEST_FAILED` → unavailable（但 cause chain 命中 timeout 时 timeout 优先）；包含 `TIMEOUT` 的 provider code → timeout；`STRUCTURED_OUTPUT_INVALID/PROVIDER_EOF/PROVIDER_EMPTY_OUTPUT/TURN_BUDGET_EXCEEDED/MODEL_PROTOCOL_ERROR` → structured invalid。未知 code 不按字符串猜测，原异常抛出。`TURN_BUDGET_EXCEEDED` 在这里表示模型未完成结构输出，不等同于 WorkItem 容量 `TOO_MANY_WORK_ITEMS`。

由于现有 `AIClient` 可能把 HTTP 错误包成 `MODEL_PROTOCOL_ERROR`，adapter 必须沿 `__cause__/__context__` 链检查根因：存在 `httpx.TimeoutException/TimeoutError` 时按 timeout；存在其他 `httpx.HTTPError` 时按 unavailable；没有网络根因的 `MODEL_PROTOCOL_ERROR` 才按 structured invalid。不得为此修改全局 AIClient 的其他调用语义。

adapter 的计数/耗时规则固定为：进入 `complete_structured()` 前记录单调时钟；成功为 `providerAttemptCount=1+completion.repair_count`；`StructuredCompletionError` 为 `1+exc.repair_count`，但模型/provider 配置在进入调用前即不可用时为 `0`；`latencyMs` 统一取本次 adapter 墙钟毫秒并向下取非负整数。不能依赖 provider metadata 是否恰好包含 duration。

锚定器和依赖校验器分别只转换为上表对应的 typed exception。模型调用成功后才发生的上下文/锚定/依赖异常，其 diagnostics 必须优先使用已有 `UnderstandingInvocationResult` 的 attempt/latency；adapter 直接失败时才使用 exception 携带的值。规则图成环、RoutePlan 不变量失败、数据库错误和程序错误不得被 `except Exception` 吞掉。

结构化输出 repair 最多一次，并计入同一次 Understanding 模型阶段，不得在 repair 后再运行另一套路由模型。

`TOO_MANY_WORK_ITEMS` 是合法模型结果，不属于异常或 fallback。规则 `limit_exceeded` 只有在模型调用失败后才可决定容量回退；不得在模型前返回。

`fallbackReason` 只允许空字符串或上表八个大写值；正常接纳、合法容量结果、高风险和 AMBIGUOUS 分支必须为空。模型异常发生后，即使进入规则容量回退，也必须保留对应 `fallbackReason`。`logicalInvocationCount` 对所有普通新路由固定为 `1`；在调用 provider 前即发现模型未配置时 `providerAttemptCount=0`，实际初次请求为 `1`，发生一次 repair 为 `2`。

### 4.4 三路五分类融合

`IntentCandidate` 和 `FusionResult` 的内部结构固定为：

```python
@dataclass(frozen=True)
class IntentCandidate:
    intent: IntentType
    confidence: float
    source: Literal["LLM", "EMBEDDING", "RULE"]
    reason_codes: tuple[str, ...]

@dataclass(frozen=True)
class FusionResult:
    intent: IntentType
    confidence: float
    reason_codes: tuple[str, ...]
```

两个 dataclass 的 `confidence` 都必须是有限 `[0,1]`；`reason_codes` 是无重复字符串 tuple。`IntentCandidate.source` 必须与传入的 llm/embedding/rule 参数槽一致。删除 `task_kind`；`FusionResult` 不伪造单一 source，因为结果可能来自多路累加。

`PlanningDiagnostics` 固定保留以下安全摘要，字段只用于可观测性，不影响路由判断：

```text
planSource                  # HIGH_RISK_HARD | LLM_ACCEPTED | LLM_LIMIT | RULE_FALLBACK | RULE_LIMIT_FALLBACK
llmInvoked                  # bool
logicalInvocationCount      # 新路由普通请求为 1，高风险为 0
providerAttemptCount        # 0～2，第二次只能是同一 structured call 的 repair
latencyMs
contextRelation
ruleSegmentCount
llmSegmentCount
acceptedSegmentCount
dependencyHintCount
hardDataEdgeCount
orderOnlyEdgeCount
hardDataEdges                # 最终 synthesisOrder 索引对
orderOnlyEdges               # 最终 synthesisOrder 索引对
fallbackReason
```

两组边不进入公共 RoutePlan，但属于安全的 route diagnostics，供同一路径评测器核对图语义；构图期间用 `(start,end)` 稳定跨度键，完成 `stable_topological_order()` 后必须把两组边重映射为最终 `synthesisOrder` 的 `[sourceIndex,targetIndex]`。`as_metadata()` 和持久 Trace 只输出上述无原文索引对及数量，不输出 Prompt、Embedding 向量或完整历史。评测数据集中的边索引也只按最终顺序定义，禁止一处使用模型原始索引、一处使用最终索引。

计数字段语义固定为：`ruleSegmentCount` 是规则草案段数；`llmSegmentCount/dependencyHintCount` 是通过 V3 Schema 后、后续锚定或图校验前的模型计数，模型不可用或 Schema 非法时为 0；`acceptedSegmentCount` 是最终 RoutePlan WorkItem 数；两个 edge count 是最终实际采用的图边数。`latencyMs` 为非负整数。`planSource`、计数和 `fallbackReason` 必须由同一个 diagnostics 构造器生成，特殊分支不得手填出不一致组合。

各 planSource 的字段组合固定如下（`R` 为规则草案实际段数，`L/H` 为通过 V3 Schema 后的 segment/hint 数，`P/T` 为实际 provider attempt/latency）：

| planSource | llmInvoked / logical | provider / latency | contextRelation | rule / llm / accepted | hints / final edges | fallbackReason |
|---|---|---|---|---|---|---|
| `HIGH_RISK_HARD` | `false / 0` | `0 / 0` | `NEW_TOPIC` | `0 / 0 / 1` | `0 / 0` | `""` |
| `LLM_ACCEPTED` | `true / 1` | `P / T` | 已接纳关系 | `R / L / 最终项数` | `H / 最终图` | `""` |
| `LLM_LIMIT` | `true / 1` | `P / T` | 已接纳关系 | `R / 0 / 1` | `0 / 0` | `""` |
| `RULE_FALLBACK` | `true / 1` | 失败实值 | `NEW_TOPIC` | `R / L或0 / 最终项数` | `H或0 / 纯规则图` | typed error code |
| `RULE_LIMIT_FALLBACK` | `true / 1` | 失败实值 | `NEW_TOPIC` | `R / L或0 / 1` | `H或0 / 0` | typed error code |

普通融合后收敛为 RISK 仍使用 `LLM_ACCEPTED`，保留已接纳模型计数，但最终 `acceptedSegmentCount=1`、edge counts=0。该单项 RISK 覆盖完整 `planning_input`；其跨度固定为 `(0, len(planning_input))`，WorkItem ID 因而使用这个完整跨度和 `RISK`，confidence 使用触发收敛的最高 fused RISK confidence，公共 reasonCodes 只含 `HIGH_RISK_SIGNAL`；硬规则风险同样使用完整跨度且 confidence=`0.99`。容量和歧义单项也使用完整跨度并固定 confidence=`0.0`。

特殊分支的 context/ID 固定为：硬风险使用 `NEW_TOPIC` 和空 context anchor；LLM 容量使用模型已校验 relation 对应 anchor；规则容量 fallback 使用 `NEW_TOPIC`；上下文 AMBIGUOUS 使用 `AMBIGUOUS` 及受限 goal/topics anchor；普通融合收敛 RISK 仍使用已接纳模型 relation 对应的 planId，只是 WorkItem 改为完整跨度 RISK。不得因控制分支另造随机 ID。

必须删除 `complexityScore`、`complexityReasons` 和 `RULE_FAST`。普通新路由出现 `llmInvoked=false` 只能是当前高风险硬路径；澄清服务直接消费的轮次不产生新 route diagnostics。

同时删除旧 `PlanningReasonCode` 中只服务复杂度或逐边拒绝的代码、`acceptedDependencyCount`、`rejectedReasonCodes`、`MutablePlanningDiagnostics` 以及逐边试加实现。仅保留低于 `0.82` 的 LLM HARD_DATA 弃权计数作为内部非公共 `lowConfidenceDependencyHintCount`（不进入固定 `as_metadata()`，只供单元测试）；其他依赖非法统一体现在 `fallbackReason=DEPENDENCY_GRAPH_INVALID`。`PlanSource` 和 `FallbackReason` Literal 必须严格更新为本文列出的全集。

融合步骤：

1. 输入固定为 `llm/embedding/rule` 三个可空候选；去掉 `None` 和已在 Embedding adapter 判定低于相似度/间隔门槛的候选。非有限/越界 confidence 或 source 与参数槽位不匹配属于调用方程序错误，必须抛出 `ValueError`，不能静默弃权。LLM confidence 已由 Pydantic 保证，Rule confidence 来自固定表。
2. LLM 候选使用 Schema 返回的 `confidence`；Rule 候选使用规则表固定置信度；Embedding 通过弃权门槛后使用 `confidence=(cosine+1)/2`，并裁剪到 `[0,1]`。
3. 若 LLM 候选不是 `RISK`（含 LLM 缺失），则在计分前移除所有 `RISK` 候选；若 LLM 候选为 `RISK`，其他路的 RISK 证据才可共同计分。这样 Embedding RISK 只作辅助，不能自行升级安全等级。
4. 对剩余信号的固定权重重新归一化。
5. 按 Intent 累加 `normalized_weight * confidence`。
6. 对五类补齐未得分项为 `0`，按分数降序、同分按固定顺序 `RISK > MENTAL > CAMPUS > ACADEMIC > CHAT` 取第一名和第二名；同分通常会因 margin 不足进入歧义，不得依赖 dict 顺序。
7. 没有任何候选时固定返回 `CHAT/0.0/AMBIGUOUS_ROUTING`；否则第一名低于 `0.50` 或领先小于 `0.08` 时返回 `intent=CHAT`、`confidence=best_score`、`reasonCodes=("AMBIGUOUS_ROUTING",)`。
8. 否则返回第一名及聚合置信度；候选内部 reasonCodes 先稳定去重，再只由 `_public_reason_codes()` 映射到第 5.1 节 allowlist。不得把 `EMBEDDING_SIGNAL`、`LLM_CONTEXT`、依赖拒绝码等内部诊断直接暴露。

规则候选的第一版行为固定为：复用当前已有的技术上下文、ACADEMIC、CAMPUS、MENTAL 词表，不新增大词典。先运行现有“压力测试/服务器/Python 程序排查”技术上下文判断，命中则 CHAT `0.98`；否则去除首尾空白和 `，,。！？!?.` 后，文本恰好等于 `你好/您好/嗨/早上好/下午好/晚上好/谢谢/再见` 之一才产生 CHAT `0.96`。随后计算现有 academic/campus/mental 三个显式命中布尔值，保留“明确 academic planning 抑制 campus”规则；抑制后恰好一个领域为真才投票：ACADEMIC=`0.92`、CAMPUS/MENTAL=`0.94`。零命中或多命中返回 `None`。当前高风险已由前置硬规则消费，不生成普通 Rule RISK 候选；尤其不得保留末尾默认 `CHAT=0.96`。参数抽取与规则 Intent 判断拆开，不能因规则弃权而停止抽取最终融合 Intent 的参数。

Embedding adapter 只把 `backend.available()==False`、`EmbeddingUnavailable` 和 `httpx.HTTPError` 转为该路弃权。统一 backend 已负责把 provider 响应的向量 shape/type 问题转成 `EmbeddingUnavailable`；adapter 不再笼统捕获 `ValueError/TypeError`，否则会吞掉未知 provider 配置、缓存结构错误或本地编程错误。模板 cache key 固定为 `(backend.name, backend.model, backend.model_digest(), "five-intent-templates-v3")`；同一请求只创建/探测一次 backend，批量计算缺失模板向量，各 Segment 只计算各自 query vector。缓存取回时仍校验五类 key、每组向量数量及统一非零维度，不合格属于程序/缓存错误并抛出。

`contextRelation=AMBIGUOUS` 时，即使某个领域候选分数较高，也不得假装已经恢复被省略的上文目标。最终必须生成一个覆盖当前完整原文的低置信 CHAT WorkItem，`reasonCodes=["AMBIGUOUS_ROUTING"]`，目标为请求用户明确所指内容；不新增普通 slot clarification 记录。

Embedding 第一版模板固定为下列可读 UTF-8 字符串；每个字符串各自向量化，同一 Intent 的得分取该组模板余弦相似度最大值，不把三句话先拼接成一个模板：

```python
INTENT_EMBEDDING_TEMPLATES_V3 = {
    IntentType.CHAT: (
        "问候、寒暄、感谢和一般交流",
        "编程、代码、数据库和通用技术问题",
        "翻译、写作和非校园通用知识",
    ),
    IntentType.ACADEMIC: (
        "课程学习、复习、考试和学习计划",
        "论文推进、研究和学业安排",
        "考研、就业、求职、实习和学业选择",
    ),
    IntentType.CAMPUS: (
        "学校制度、奖学金、补考、重修和学籍规定",
        "宿舍、校园网、校园卡和校内办理流程",
        "校区、材料、资格、截止时间、入口和联系方式",
    ),
    IntentType.MENTAL: (
        "压力、焦虑、失眠、低落和害怕",
        "情绪倾诉、安慰、陪伴和心理支持",
        "人际关系、考试或论文引起的非高风险困扰",
    ),
    IntentType.RISK: (
        "当前自伤、自杀或不想活",
        "当前伤害他人或持有危险物品",
        "已经实施伤害、处于危险地点或即时危险",
    ),
}
```

对五个 Intent 的组内最高原始余弦相似度排序。第一名低于 `0.55`，或第一、第二名差小于 `0.05` 时整路弃权；通过后用第一名生成唯一 Embedding 候选。RISK 候选仍受第 4.4 节第 3 步的 LLM 门控，不能越过风险硬边界。

### 4.5 上下文追问

Prompt 必须明确：历史只用于补全当前省略表达，当前 `sourceText` 仍必须是本轮连续原文。

`app/services/intent_prompts.py::build_intent_prompt()` 固定签名为 `(context_view, current_input)`，不再接收 `max_segments/settings`；Prompt 内直接写入 `MAX_WORK_ITEMS=4`，并包含 V3 字段名、五类枚举、`ROUTE/TOO_MANY_WORK_ITEMS` 互斥约束、依赖 reasonCode 约束和以下上下文示例。schema name 固定为 `understanding_decision_v3`，Prompt 版本固定为 `five-intent-context-v3`，评测报告记录该版本字符串。生产与评测只能调用这一函数。

上下文关系语义固定为：

- `NEW_TOPIC`：当前目标不依赖历史；即使有历史，也不写入 planId context anchor；
- `CONTINUE`：延续上一个目标并询问同一对象的新内容；
- `REFINE`：为同一目标增加约束、范围或细节；
- `CORRECTION`：更正历史中的对象、值或条件；
- `AMBIGUOUS`：当前存在省略或指代，但受限上下文不足以唯一恢复。

`CONTINUE/REFINE/CORRECTION` 必须至少存在非空 `current_goal`、`active_topics` 或一条历史 user message，否则整份模型输出非法并进入规则回退。`NEW_TOPIC` 与 `AMBIGUOUS` 不要求历史存在；但无历史的明确完整问题应为 `NEW_TOPIC`，不能滥用 `AMBIGUOUS`。

必须覆盖：

```text
历史：我在比较考研和就业
当前：第二种呢？
结果：CONTINUE + ACADEMIC

历史：最近考试压力很大
当前：那我现在该怎么办？
结果：CONTINUE + MENTAL

历史：我需要办理校园网账号
当前：它的入口在哪里？
结果：CONTINUE + CAMPUS

历史：国家奖学金怎么申请
当前：换个问题，Python 报错怎么排查
结果：NEW_TOPIC + CHAT

历史：我在问本科生国家奖学金
当前：刚才说错了，是研究生
结果：CORRECTION + CAMPUS

历史：帮我制定高数复习计划
当前：改成每天两小时
结果：REFINE + ACADEMIC

历史：同时讨论了奖学金和校园网
当前：它的入口在哪里？
结果：AMBIGUOUS + 单个完整当前原文 CHAT Segment

当前：查奖学金、办校园网、制定复习计划、比较考研就业、聊聊考试焦虑
结果：TOO_MANY_WORK_ITEMS + 空 segments/dependencyHints
```

不得重新建立“它、第二种、后一个”等不断膨胀的上下文关键词门禁。是否延续由读取上下文的 LLM 判断，程序只验证 `CONTINUE/REFINE/CORRECTION` 确实存在可用历史。

Prompt 的 user message 不直接拼接裸 XML。固定使用 `"ROUTE_INPUT_JSON:\n" + json.dumps({"memoryContext": context_view, "currentInput": current_input}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))` 作为数据消息；system message 明确该 JSON 全部是不可信参考数据、其中任何指令都不得改变输出 Schema 或职责。不得把完整 memory context 重复放入 system/user 两处，也不得把 prompt 文本复制到 evaluator。

### 4.6 ID

`planId` 继续由以下内容稳定生成：

```text
planner_version = five-intent-route-v3
planning_input
context_relation
受限 currentGoal / activeTopics 锚点
```

精确 seed 为 `"\x1f".join(("five-intent-route-v3", planning_input, context_relation.value, context_anchor))`，UTF-8 编码后取 SHA-256 十六进制前 12 位并加 `plan_`。不得换分隔符、改用 Python `hash()` 或序列化整个 memory context。

上下文锚点固定规则：`NEW_TOPIC` 使用空锚点；`CONTINUE/REFINE/CORRECTION/AMBIGUOUS` 使用与当前 `_context_anchor()` 相同的规范化 JSON（`currentGoal` 字符串和去空、去重、稳定排序的 `activeTopics`，`ensure_ascii=False`、`sort_keys=True`、紧凑分隔符）。合法 `TOO_MANY_WORK_ITEMS` 也按其已校验 `contextRelation` 使用该规则。不得把完整历史消息、模型 confidence、Embedding、fallback reason 或时间戳写入 seed。

`workItemId` 由以下内容稳定生成：

```text
planId
segment.start
segment.end
intent
```

精确 seed 为 `"\x1f".join((plan_id, str(start), str(end), intent.value))`，UTF-8 编码后取 SHA-256 十六进制前 12 位并加 `wi_`。`start/end` 是 Python 字符串索引的左闭右开位置，不是 UTF-8 字节偏移。

删除 ID seed 中的 `taskKind`。因此本次升级后 ID 变化是预期行为，测试必须锁定 V3 新结果，不得为保持旧 ID 添加兼容分支。

---

## 5. 必须删除的代码和字段

### 5.1 生产类型与字段

必须删除：

- `app/core/enums.py::TaskKind`
- `WorkItem.task_kind`
- RoutePlan payload 的 `taskKind`
- `IntentSegmentDecision.taskKind`
- `IntentCandidate.task_kind`
- `FusionResult.task_kind`
- `ClarificationRequest.task_kind`
- `ClarificationTarget.task_kind`
- `TaskParseResult.task_kind`（如无其他真实用途则删除整个未使用类型）
- Specialist result 的 `taskKind`
- Trace 和 turn execution 中的 `taskKind`
- ContextBuilder 白名单中的 `taskKind`
- 评测契约中的 `workItemTaskKinds`

必须删除以下路由 reasonCode：

- `STUDY_PLAN_SIGNAL`
- `CAREER_DECISION_SIGNAL`
- `EMOTIONAL_SUPPORT_SIGNAL`
- `INSTITUTIONAL_FACT_SIGNAL`

保留五类 Intent 对应的必要领域 reasonCode，并新增 `AMBIGUOUS_ROUTING`。

V3 公共 RoutePlan `reasonCodes` allowlist 固定为：

```text
HIGH_RISK_SIGNAL
ACADEMIC_SIGNAL
CAMPUS_SERVICE_SIGNAL
MENTAL_HEALTH_SIGNAL
GENERAL_CHAT
COMPOUND_REQUEST
DEPENDENCY_DETECTED
MEMORY_CONTEXT
SEMANTIC_FALLBACK
WORK_ITEM_LIMIT_EXCEEDED
AMBIGUOUS_ROUTING
```

LLM 结构层的 `SegmentReasonCode`、依赖 hint reasonCode、融合来源诊断和 fallback 错误码属于内部诊断，不得未经 allowlist 直接写入公共 RoutePlan。`intent` 的默认公共 reasonCode 分别为 `GENERAL_CHAT`、`ACADEMIC_SIGNAL`、`CAMPUS_SERVICE_SIGNAL`、`MENTAL_HEALTH_SIGNAL`、`HIGH_RISK_SIGNAL`。

公共映射固定为：融合结果不含 `AMBIGUOUS_ROUTING` 的正常 WorkItem 先写该 Intent 的默认码；若 accepted context relation 为 `CONTINUE/REFINE/CORRECTION` 再追加 `MEMORY_CONTEXT`；只有模型回退且非容量/风险/歧义项追加 `SEMANTIC_FALLBACK`。融合因全路弃权、低分或低 margin 返回的 CHAT WorkItem 只写 `AMBIGUOUS_ROUTING`，不再追加 `GENERAL_CHAT/SEMANTIC_FALLBACK`；上下文 `AMBIGUOUS` 控制项、容量控制项和风险控制项也分别只使用各自专属码。`COMPOUND_REQUEST/DEPENDENCY_DETECTED` 只由 RoutePlan 聚合层追加。`TECHNICAL_CONTEXT`、`SEMANTIC_FUSION`、`LLM_CONTEXT`、`EMBEDDING_SIGNAL` 和各 SegmentReasonCode 都是内部候选原因，不得加入 `RouteReasonCode` 枚举。

### 5.2 复杂度门控

必须删除：

- `ComplexityAssessment`
- `assess_complexity()`
- `CONTEXT_REFERENCES`（若删除复杂度后无其他用途）
- `PlanningDiagnostics.complexity_score`
- `PlanningDiagnostics.complexity_reasons`
- `intent_decomposition_complexity_threshold`
- 只为“是否调用 LLM”存在的测试和配置

规则草案仍需使用的动作词、连接词、依赖词、子句分隔符和忽略词不得误删，因为它们还服务于规则分段、显式依赖、锚定校验和模型失败回退。

### 5.3 旧 Schema 与兼容路径

必须删除：

- V2 Pydantic 路由/语义模型；
- V2 JSON 解析分支；
- 旧字段 alias；
- `taskKind` 缺省补值；
- V2 数据集 loader；
- 旧 SemanticPlanningEvaluator 与 RoutingEvaluator 的重复模型调用路径（按第 8 节合并）；
- 已无调用的旧小函数和测试夹具。

Specialist result V1 同样属于被替换的生产契约：实现必须原子切换至第 3.4 节的 V2，只允许修改当前生成/消费代码和测试，不得修改与路由无关的其他 `schemaVersion=1/2` 契约。

旧 Alembic migration 文件作为数据库历史不得改写，但当前生产代码不得引用旧列。

---

## 6. 下游行为调整

### 6.1 Coordinator

继续使用固定映射：

```python
CAPABILITY_BY_INTENT = {
    IntentType.CHAT: AgentCapability.GENERAL_CHAT,
    IntentType.ACADEMIC: AgentCapability.ACADEMIC_PLANNING,
    IntentType.CAMPUS: AgentCapability.CAMPUS_AFFAIRS,
    IntentType.MENTAL: AgentCapability.PSYCHOLOGICAL_SUPPORT,
}
```

Coordinator 不读取、不推导 `TaskKind`。RISK 继续走安全覆盖，不创建普通 Specialist task。

`ClarificationPolicy.select()` 仍在创建 Specialist task 前运行；有 target 时 Coordinator 构造第 3.3 节严格 V3 request，`resumeContext.schemaVersion=3`，然后立即通过 `as_payload()` 发布。无 target 时再按 `CAPABILITY_BY_INTENT` 为每个非 RISK WorkItem 建任务。容量/AMBIGUOUS 都是 CHAT 且没有 missingArguments，因此走 GeneralChatAgent 生成收窄/明确目标的自然语言回复，不创建 PendingClarification。

### 6.1.1 WorkItem 参数与 objective

`extract_work_item_arguments()` 签名固定改为 `(intent, source_text)`：

- `CAMPUS` 保持现有 `studentType`、`site` 等事实范围参数抽取语义；
- `ACADEMIC` 仅在共享 `is_concrete_study_plan_request(source_text)` 为真时抽取具体计划参数；
- `CHAT`、`MENTAL`、`RISK` 默认无普通澄清 slot；
- 抽取后的 missingArguments 仍通过按 Intent 注册的 handler 白名单净化。

`build_work_item_objective()` 签名固定改为 `(intent, source_text)`。正常 Segment 继续使用去除首尾标点后的当前 `sourceText[:240]` 作为 objective；只有空原文的防御性分支才按 Intent 使用固定 fallback 文案。容量和歧义路径分别使用“请用户将请求缩小到不超过四个目标”和“请用户明确当前所指的目标”，不得为了恢复旧计划/职业文案引入隐藏子分类。

### 6.2 AcademicPlanningAgent

统一处理全部 ACADEMIC 请求，包括：

- 学习和复习建议；
- 具体学习计划；
- 论文推进；
- 考研与就业比较；
- 实习和学业选择。

是否属于具体学习计划只由共享纯函数根据 `sourceText` 判断；调用方再结合 `knownArguments` 决定是否缺少 `course/deadline`。该布尔值不在 RoutePlan 中保存，也不参与路由分类。

不得在 `autonomous.py`、`routing.py` 和 `clarification_handlers.py` 各自维护不同的学习计划关键词；只保留一个实现，由三处调用。

### 6.3 ResponseAgent

删除对 `taskKind == STUDY_PLAN` 的检查。对于 ACADEMIC 结果，统一遵守：

- `knownArguments` 是用户直接提供的约束，无需 RAG 证明；
- 需要官方事实时只使用真实 evidence；
- 学习计划不得因为知识库没有课程记录而拒答；
- 职业或学业比较不得伪造就业数据。

### 6.4 Clarification

澄清状态只保存 Intent。恢复时使用：

```text
planId + workItemId + intent + expectedFields
```

所有 resumeContext 内嵌 RoutePlan 必须为 V3。旧 V2 状态不通过运行时兼容解析。

`app/agents/harness.py` 是真实聊天执行入口的一部分，必须同步改造：`_clarification_state()` 只输出 `{"intent": ..., "knownArguments": ..., "missingArguments": ...}` 三个 Understanding 所需字段，删除 `task_kind/public_id/status/round_count/resume_context` 等不需要暴露给模型的状态；`_route_plan_from_pending()` 先用 ClarificationResumeContext V3 再用 RoutePlan V3 唯一解析器恢复。Clarification artifact/request 经 service 创建成功后仍保持第 3.3 节严格字段，不得再在顶层追加 `status`、`roundCount` 等响应展示字段；轮次只存在 `resumeContext.roundCount` 和数据库列。旧 V2 待澄清记录必须由数据库迁移提前中断，不能等到运行时解析失败。对新 V3 WAITING_USER 出现损坏 JSON/Schema 时，保持现有安全失败语义：将该记录 CAS 中断为 `INVALID_STATE`，本轮输入继续作为一个新普通请求进入 Understanding；不得猜测恢复。

`ClarificationService.create()` 固定流程为：先 `ClarificationRequest.from_payload()` 全量严格解析 → 用 Intent handler 再净化 known/missing，并要求净化后结果与 request 完全一致（任何删除/改值都视为非法，而不是静默修复）→ 校验内嵌 RoutePlan、ID 和 expected field → 构造受控问题 → 若同一 plan/work/field 已有 active row 则幂等返回，否则 CAS 中断旧 row → 仅写 `intent.value`、V3 resume context 和最大 1000 字符的 `original_message`。不得在 service 中再次手写另一套 V3 字段解析。`resume_or_bypass()` 仍保持当前安全优先级：本轮高风险先中断澄清并走风险新路由；明确换题中断并走新路由；合法槽位回答恢复原计划；无法解析时按现有轮次策略重问。

合法槽位回答更新内嵌 RoutePlan 后，必须重新用 `RoutePlan.from_payload()` 校验并写回规范化 payload；随后再次执行 ClarificationResumeContext V3 全量校验。若当前 WorkItem 仍有 missing，按 FIELD_REGISTRY 顺序只推进到下一字段，并同步 `expectedFields`、RoutePlan WorkItem、持久化 known/missing 列和 `roundCount`；当前 WorkItem 全部解决后必须令 `expectedFields=[]`，持久化最终 V3 resume context，再把该已校验 context 交给 `EventDrivenAgentRuntimeService._seed_resumed_plan()`。`_seed_resumed_plan()` 只接受 `expectedFields=[]` 且目标 WorkItem 已不含 missing 的 resolved context；它必须先严格解析整个 `ClarificationResumeContext V3`，再取得其中 RoutePlan，不能只做 `resume_context.get("routePlan")`。计划中其他 WorkItem 若仍缺参数，由恢复后的 Coordinator 再按既有 ClarificationPolicy 选择下一项。任何 V3 损坏都按 `INVALID_STATE` 中断并把当前轮当新请求，不做局部修补。

### 6.5 Context、Trace 与内部策略命名

- `TurnContextPacket.for_understanding(planning_input)` 及共享 builder 的 clarification 白名单删除 `taskKind`；
- `ContextBuilder.for_specialist()` 的 WorkItem 白名单删除 `taskKind`；
- 内部预算策略继续按现有 `IntentType`、`KnowledgeDomain` 和事实依赖工作，只重命名而不改变删减顺序：`INSTITUTIONAL_FACT → FACT_REQUIRED`、`ACADEMIC_DECISION → ACADEMIC_SUPPORT`、`GENERAL_CHAT → GENERAL_RESPONSE`，`MENTAL_SUPPORT` 与 `RISK_SAFETY` 保持。必须同步修改 `_budget_policy()`、`_apply_budget_policy_exclusions()`、主 budget while-loop 中所有字符串比较及对应测试；这是命名清理，不改变任何分支次序；
- Trace 与 turn execution 删除 route、clarification、specialist 摘要中的 `taskKind`，同时按第 4.4 节白名单保存新 diagnostics；
- Turn execution 的 route 摘要保留现有 `riskLevel`（它来自独立 Safety 结果，不属于 RoutePlan V3），WorkItem 摘要只含 `workItemId/intent/dependsOn/missingFields`，并新增 `synthesisOrder` 和 diagnostics 的 `contextRelation/orderOnlyEdges` 供同一路径端到端/evaluator 读取；Trace 的 route/clarification 摘要同样只保留 ID、Intent、依赖和 missing field 名，不把 `sourceText`、objective、known value、resumeContext 或原始消息加入可观测数据；
- 历史 `agent_run_traces.agent_steps_json` 是只读历史 JSON，不执行 V1/V2 迁移；管理端允许原样展示历史行，但新代码不得为其增加旧 Schema 解析器。

### 6.6 性能与调用预算

每个普通新路由增加一次 Understanding 调用，实施时必须保持以下边界：

- 生产与评测共用的 Understanding adapter 继续使用现有 `agent_loop_deadline_seconds` 作为 structured call timeout，本次不新增超时配置或可切换路由语义的开关；超时后只走同一规则回退；
- Embedding backend 每次路由最多初始化一次；五类模板向量按 provider/model/digest/模板版本缓存；一个多 Segment 请求不得为每个 Segment 重复探测后端或重算模板向量；
- 评测必须记录模型调用数、fallback 数和耗时分布；不得为了降低延迟恢复简单请求规则直出。

---

## 7. 数据库迁移

新增下一顺序 Alembic migration，文件名固定为：

```text
0014_five_intent_route_v3.py
```

不得修改历史 migration。

`pending_clarifications.task_kind` 当前保存类似 `ACADEMIC:STUDY_PLAN`。升级步骤：

1. 在任何 DDL 或数据更新前，使用当前 connection 只读查询全部 `id, task_kind`；`NULL` 或不属于下列六个完整值的记录均为非法，按 ID 稳定排序后在异常中报告，随后立即终止；空表合法；
2. preflight 通过后新增临时可空列 `intent VARCHAR(16)`；
3. 使用以下六个旧合法完整值做精确映射，不能只取冒号前缀：

   ```text
   CHAT:GENERAL_CHAT                → CHAT
   ACADEMIC:STUDY_PLAN              → ACADEMIC
   ACADEMIC:CAREER_DECISION         → ACADEMIC
   CAMPUS:INSTITUTIONAL_FACT        → CAMPUS
   MENTAL:EMOTIONAL_SUPPORT         → MENTAL
   RISK:HIGH_RISK_SUPPORT           → RISK
   ```

4. 回填后断言每行 `intent` 非空且属于五类；失败时抛错，不继续删列；
5. 在删除旧列前处理所有 `status='WAITING_USER'` 的 V2 活跃状态：将其原子更新为 `INTERRUPTED`，`finish_reason='ROUTE_SCHEMA_V3_CUTOVER'`，并将 `resume_context_json` 写成不含 `routePlan`、`taskKind` 和用户原文的固定 tombstone：`{"schemaVersion":3,"cutoverReason":"ROUTE_SCHEMA_V3_CUTOVER"}`；同时清空 `known_arguments_json='{}'`、`missing_arguments_json='[]'`、`original_message=''` 和 `approved_question=''`，避免已中断行继续保留不必要的用户内容；
6. 终态历史记录的 `resume_context_json` 不再被生产代码解析，可保留为审计历史；不得为了改写历史 JSON 构造 V2 运行时模型；
7. 将 `intent` 改为非空并建立 `ix_pending_clarifications_intent`；
8. 删除现有 `task_kind` 索引（按数据库 inspector 得到的实际名称；若当前基线固定为 `ix_pending_clarifications_task_kind` 则测试锁定该名称）和列；SQLite 使用 `op.batch_alter_table()` 完成非空约束与删列；
9. ORM 只保留 `intent`。

升级完成后生产代码只读写 `intent`。迁移过程中的临时双列不是运行时兼容路径，应用代码不得同时读写两列。

上述 preflight 必须复用 Alembic 当前 connection，不能创建另一个可能指向不同数据库的 Session。数据更新在数据库方言支持的事务边界内执行；考虑 MySQL DDL 提交语义，不得宣称跨全部 DDL 可事务回滚，必须依赖升级前快照恢复。不能先加列、中断部分记录后才发现非法值。迁移测试至少覆盖：

1. 空数据库从 base 升级到 head；
2. 合法的 `ACADEMIC:STUDY_PLAN`、`ACADEMIC:CAREER_DECISION`、`CAMPUS:INSTITUTIONAL_FACT` 等严格回填为五类 Intent；
3. 非法 `task_kind` 导致 upgrade 失败且原表数据未被部分破坏；
4. `WAITING_USER` 被中断、finish reason 正确、tombstone 不含旧 RoutePlan；
5. 已终态行的业务列得到正确 Intent；
6. head 当前表有 `intent`、无 `task_kind`；
7. downgrade 只存在于 migration，并按下述固定 canonical 映射恢复旧列。

Alembic downgrade 必须存在以满足迁移链测试，并按以下顺序执行：先在任何 DDL 前只读 preflight，要求每行 `intent` 非空且严格属于五类，非法时按 ID 报告并终止；再新增临时 `task_kind`，只在 0014 migration 内使用 canonical 映射 `CHAT → CHAT:GENERAL_CHAT`、`ACADEMIC → ACADEMIC:STUDY_PLAN`、`CAMPUS → CAMPUS:INSTITUTIONAL_FACT`、`MENTAL → MENTAL:EMOTIONAL_SUPPORT`、`RISK → RISK:HIGH_RISK_SUPPORT`；回填/非空断言后创建旧索引并删除 intent 索引和列。若受控开发库中仍有 V3 `WAITING_USER`，downgrade 必须先将其标记 `INTERRUPTED`、写 `finish_reason='ROUTE_SCHEMA_V3_DOWNGRADE'` 和不含 RoutePlan/原文的 V2 tombstone `{"schemaVersion":2,"cutoverReason":"ROUTE_SCHEMA_V3_DOWNGRADE"}`，并同样清空 original/known/missing/question，避免旧应用恢复 V3 payload。不得把 downgrade 映射或 V3 parser 放入生产服务代码；真实发布回滚仍优先恢复升级前快照，因为 downgrade 无法恢复 ACADEMIC 原来是计划还是职业决策。

### 7.1 发布顺序

这是数据库和生产契约的硬切换，禁止旧应用实例与新数据库 Schema 并行运行。发布必须使用维护窗口并按以下顺序执行：

1. 停止接收新聊天请求和澄清写入，等待正在执行的请求结束；
2. 确认没有旧应用实例继续连接数据库；
3. 创建可恢复的数据库快照并记录当前应用 commit；
4. 运行 0014 migration；任何校验失败立即停止发布；
5. 部署只包含 V3/V2 新契约的新应用版本；
6. 执行健康检查、空数据库/升级数据库 smoke、普通请求、澄清新建和高风险抢占检查；
7. 恢复流量。

不得采用先迁移数据库、旧应用继续服务，或新旧版本滚动混跑的方式。回滚必须同时恢复旧应用版本和升级前数据库快照；仅回滚代码而保留已删除 `task_kind` 的数据库不受支持。

部署回滚依赖升级前数据库快照和旧应用版本；0014 downgrade 仅用于迁移链可逆性测试和受控开发环境，不替代生产快照回滚。

---

## 8. 评测简化与数据集

### 8.1 只保留一套正式生产路由评测

当前规则-only `RoutingEvaluator` 不能代表生产质量，而 raw semantic evaluator 又没有覆盖最终融合。改造后正式门禁必须评测完整生产链路：

```text
messages
→ ContextBuilder 等价受限上下文
→ 实际 Understanding 模型客户端
→ V3 Schema/锚定
→ 三路五分类融合
→ 参数与依赖图
→ 最终 RoutePlan V3
```

合并重复评测器，正式类名固定为 `ProductionRoutingEvaluator`。纯规则和模型异常回退通过单元测试覆盖，不再用规则-only 报告冒充生产指标。

生产与评测必须复用同一个 Understanding 模型 adapter，不得在 `evaluation/runner.py` 复制另一份 Prompt、Schema 或模型选项。具体实现要求：

1. 新建生产服务模块 `app/services/understanding.py`；
2. 该模块持有唯一的 `understanding_decision_v3` schema name、Prompt 调用、模型 options、异常映射和一次 repair 策略；
3. `UnderstandingAgent._semantic_route()` 删除本地重复实现，改为调用该 adapter；
4. `ProductionRoutingEvaluator` 注入并调用同一个 adapter，再调用生产 `classify_route()`；
5. 在 `ContextBuilder` 中抽出一个共享的受限 Understanding view 构造函数，生产 `TurnContextPacket.for_understanding(planning_input)` 和评测 messages adapter 共同使用；
6. 评测不得把全部历史直接塞给模型，也不得绕开生产锚定、融合、参数抽取、DAG 和 RoutePlan 校验。

共享函数固定放在 `app/services/context_builder.py::build_understanding_context_view()`，签名固定为：

```python
def build_understanding_context_view(
    current_input: str,
    current_goal: str | dict[str, Any] | None,
    active_topics: Sequence[str],
    recent_messages: Sequence[MemoryMessage | dict[str, Any]],
    clarification_state: dict[str, Any] | None,
    token_budget: int,
    *,
    packet_version: int = 1,
    summary_version: int = 0,
) -> dict[str, Any]: ...
```

输出字段固定为 `packet_version/summary_version/current_input/current_goal/active_topics/recent_messages/clarification_state/context_truncated`，不得额外携带用户画像、知识内容或安全历史。规范化和删减顺序固定为：校验 `current_input.strip()` 非空且不超过 1000 → recent messages 只保留最后四条并转换为现有 `_memory_message_dict()` 格式 → active topics 去空稳定去重后只留最后六项 → clarification state 深拷贝 → token budget 最小按现有 `256` → 循环删除最旧 recent messages → active topics 只留最后三项 → clarification state 只留 `intent/missingArguments/knownArguments` → 最后设置 `context_truncated`。`TurnContextPacket.for_understanding(planning_input)` 只负责传入真实 packet 字段，并用参数 `planning_input` 覆盖 packet 中可能未脱敏的 current input；不得在方法内复制另一套裁剪逻辑。

`ProductionRoutingEvaluator` 的 messages adapter 不调用第二个模型生成摘要：最后一条消息必须是当前 user，之前只允许交替的 user/assistant；取最后一条历史 user 内容作为确定性 `current_goal={"text": ...}`，`active_topics=[]`、`clarification_state=None`，再调用上述共享函数并使用生产 `intent_context_max_tokens`。这使三条省略追问至少拥有与真实项目相同格式的最近历史和目标锚点，同时不为评测复制裁剪逻辑。RoutingCase 必须拒绝 system 消息、空历史内容、最后一条不是 user 或连续同角色的非法对话。

`ProductionRoutingEvaluator.evaluate_case()` 的唯一 SUT 调用顺序为：构造上述 view → 调用生产 `classify_route(current, view, understanding_service.classify, settings, raw_current_input=current)` → 读取 RoutePlan V3 和同一 RouteDecision diagnostics。实际字段计算固定为：HARD_DATA 来自 WorkItem `dependsOn` 映射到最终索引，ORDER_ONLY 来自 diagnostics `orderOnlyEdges`，`contextRelation` 来自 diagnostics，`capacityExceeded` 由 RoutePlan 是否含 `WORK_ITEM_LIMIT_EXCEEDED` 判断。不得从原始模型 decision 直接评分。

删除正式 suite 名 `semantic-planning`、`SemanticPlanningEvaluator`、`SemanticPlanningCase`、`load_semantic_planning_cases()` 和 `semantic-planning-report.json`。`routing` 是唯一正式路由 suite；规则回退通过注入失败 adapter 的单元测试验证，不作为另一份正式报告。

### 8.2 V3 数据集

建立唯一活跃数据集：

```text
app/evaluation/datasets/routing-v3.jsonl
```

每条数据严格使用以下顶层结构，禁止额外字段：

```text
id
messages
expected
tags
```

`expected` 固定包含：

```text
primaryIntent
intents
workItemCount
workItemIntents
sourceTextFragments
hardDataEdges
orderOnlyEdges
missingArgumentNamesByWorkItem
contextRelation
capacityExceeded
```

在 `app/evaluation/contracts.py` 定义独立的严格 `ProductionRoutingExpectedV3`（`extra="forbid"`），并令 `RoutingCase.expected` 只使用该类型。不得继续复用现有宽松 `ExpectedRoute`。其字段使用上面列出的 camelCase JSON 名作为唯一外部名称，不接受旧字段、snake_case alias 或缺省值。交叉校验固定为：

- `workItemCount` 为 1～4，且等于 `workItemIntents`、`sourceTextFragments`、`missingArgumentNamesByWorkItem` 三个数组长度；
- `primaryIntent == workItemIntents[0]`；`intents` 等于 `workItemIntents` 稳定去重结果；
- 每个 fragment/argument name 非空，同一项内不得重复；
- 两组依赖边索引有效、无自环、各自无重复、无向对互斥，HARD_DATA 图及两组边合并后的展示图均无环；
- `capacityExceeded=true` 时必须是单项 CHAT、无依赖，且对应最终 RoutePlan 含 `WORK_ITEM_LIMIT_EXCEEDED`；false 时不得含该 reasonCode。reasonCode 不存入 expected，由 evaluator 从实际 RoutePlan 验证这一条件；
- `contextRelation=AMBIGUOUS` 时必须是单项 CHAT、无依赖且 `capacityExceeded=false`，evaluator 另行验证 `AMBIGUOUS_ROUTING`。

`RoutingCase` 和 `EvaluationMessage` 同样设置 `extra="forbid"`。RoutingCase 的 `id/messages/expected/tags` 都必须显式存在；`id`、消息 content 和 tags 逐项非空，ID/标签去首尾空白后不得重复，`messages` 满足第 8.1 节交替与末条 user 约束。只允许 routing case 的 message role 为 `user/assistant`；现有 EndToEndCase 仍可按其自身契约使用 system，不得因此扩大修改范围。

数组按最终 `synthesisOrder` 对齐。`sourceTextFragments[i]` 是第 i 个 WorkItem 当前轮 `sourceText` 必须包含的一组非空片段；它不允许引用历史消息。`hardDataEdges`、`orderOnlyEdges` 使用 `[sourceIndex,targetIndex]`，索引同样按最终顺序。

`routePlanExactMatch` 的比较口径固定为以上全部 expected 字段：

- `primaryIntent`、`intents`、WorkItem 数量与顺序；
- 每个 WorkItem 的 Intent；
- 每组 `sourceTextFragments` 均被对应当前轮连续原文包含；
- HARD_DATA 与 ORDER_ONLY 索引边完全一致；
- 每项缺失参数名排序后完全一致；
- `contextRelation` 完全一致；
- 是否命中容量保护完全一致。

实现时每项形成显式布尔检查：`primaryIntentCorrect`、`intentsCorrect`、`workItemCountCorrect`、`workItemIntentsCorrect`、`sourceTextFragmentsCorrect`、`hardDataEdgesCorrect`、`orderOnlyEdgesCorrect`、`missingArgumentsCorrect`、`contextRelationCorrect`、`capacityExceededCorrect`；`routePlanCorrect=all(...)`。边和 missing argument 内部先规范为稳定排序再比较，WorkItem/Intent 数组本身不得排序。高风险另断言单项 RISK 且没有普通依赖。

exact match 不比较 `planId`、`workItemId`、浮点 `confidence`、reasonCode 顺序或诊断字段；这些分别由确定性单元测试、校准指标和诊断测试覆盖。不得使用旧 `riskLevel` 或 `hasDependencies` 冗余字段替代上述契约。

正式 routing expected 与端到端回答评测不是同一个 Schema。必须删除含糊的共享类名 `ExpectedRoute`，另建严格 `EndToEndExpectedRoute`，只供 `EndToEndCase.expected_route` 使用，字段固定为：

```text
primaryIntent
intents
riskLevel
workItemCount
workItemIntents
dependencyEdges
missingArgumentNamesByWorkItem
```

该端到端摘要不含 `workItemTaskKinds`、`hasDependencies`、`contextRelation` 或容量标签；`hasDependencies` 由 `dependencyEdges` 是否非空唯一推导，不持久化。`mindbridge-e2e-ragas-v1.jsonl` 和 `e2e-safety-v1.jsonl` 中非空 expected route 必须同步到这个结构。`EndToEndCase` 现有顶层 camel/snake 输入 alias 属于不同历史数据集的加载约定，本次不新增 alias；新 `EndToEndExpectedRoute` 自身不接受旧 route 字段 alias。

`EndToEndExpectedRoute` 设置 `extra="forbid"`，以上七个字段全部显式必填。`workItemCount` 为 1～4 且等于 `workItemIntents` 和 `missingArgumentNamesByWorkItem` 的长度；`primaryIntent == workItemIntents[0]`，`intents` 等于按 WorkItem 顺序稳定去重；五类 Intent 严格解析；`riskLevel` 继续使用项目现有 Safety 枚举；dependency edge 索引有效、无自环、无重复且 DAG；missing argument name 非空且项内不重复。该摘要只有执行 HARD_DATA 边，因为端到端回答运行时不依赖 ORDER_ONLY；不得把展示顺序边写成执行依赖。

不得包含：

- `taskKind`
- `workItemTaskKinds`
- `STUDY_PLAN`
- `CAREER_DECISION`

迁移原则：

1. 先保存旧数据集 hash 和基线报告；
2. 将仍有效的 case 转为 V3；
3. 删除旧活跃 `routing-contract-v2.jsonl` 和 `routing-semantic-v2.jsonl`，不得保留双 loader；
4. 删除活跃目录中的 `routing-v1.jsonl`；历史指标由 Git 和已保存报告承担，不新建 archive loader 或运行时路径；
5. 更新 `mindbridge-e2e-ragas` 中嵌入的路由 expected；
6. 更新数据生成脚本，禁止继续生成旧字段；
7. 通过正式 `scripts/build_ragas_dataset.py` 同步重建 `mindbridge-e2e-ragas-v1.audit.json`，更新 `BUILD_SCRIPT_VERSION` 和 `outputDatasetSha256`，不得手工伪造审计 hash；
8. 若构建所需的活动知识库/语料不可用，停止数据集交付并明确报告，不得留下 JSONL 与 audit hash 不一致；
9. 不得为了提高分数把追问样例期望改为 CHAT。

V3 Golden 不能由任一旧文件机械覆盖另一份信息。以 `routing-contract-v2.jsonl` 的五类业务/risk/missing-argument cases 和 `routing-semantic-v2.jsonl` 的分段、source fragments、context relation、HARD_DATA/ORDER_ONLY cases 为候选来源，按 `id` 先人工处理重名/语义重复，再为每个保留 case 人工补齐第 8.2 节全部 expected 字段。旧 `hasDependencies=true` 不能推断边方向，旧规则路由结果也不能推断 context relation/source fragments；缺少人工可确认信息的 case 必须进入审核清单，不能用空数组、`NEW_TOPIC` 或当前模型输出自动补值。转换完成后只保留一条 `routing-v3.jsonl` 活跃路径，并在 dataset README 记录旧输入 hash、保留/合并/删除/待审数量和最终分布。

`scripts/build_routing_dataset.py` 不得调用生产路由或模型生成 Golden。改成对已经人工审核的 `routing-v3.jsonl` 做严格 `RoutingCase` 加载、ID 唯一/排序、最小切片覆盖和 UTF-8 稳定规范化；输出仍为同一路径。脚本必须先完整读入并验证，验证失败不覆写文件。至少要求五类各有样例、三种继续关系及 AMBIGUOUS、HARD_DATA、ORDER_ONLY、单目标不过拆、复合与容量切片存在；不沿用 V1 “至少 180 条”的机械门槛，数据规模由人工迁移后实际数量记录在 README 和报告中。

稳定规范化固定为：每个 case 由 Pydantic `model_dump(by_alias=True, exclude_none=False)` 生成、按 `id` 升序，每行 `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"`，文件使用 UTF-8 无 BOM。注意 dataset loader 现有 `utf-8-sig` 可以读取 BOM，但生成脚本不得输出 BOM。覆盖检查失败或临时输出重新加载失败时，不替换原文件；替换使用同目录临时文件的原子 rename。

必须新增或保留的切片：

- 单轮五类 Intent；
- 多轮 `CONTINUE`；
- 多轮 `REFINE`；
- 多轮 `CORRECTION`；
- 明确换题 `NEW_TOPIC`；
- 无法确定指代的 `AMBIGUOUS`；
- 同一目标防过度拆分；
- 跨 Intent 多目标；
- 同 Intent 多个独立目标；
- HARD_DATA；
- ORDER_ONLY；
- 超过四个目标；
- 当前风险抢占与历史风险不误报；

模型不可用、超时、结构非法、上下文关系非法、锚定/覆盖失败和依赖图非法不写入 `routing-v3.jsonl` 的真实模型质量切片，因为同一真实 provider 运行无法按 case 稳定制造这些基础设施故障。它们必须在 `tests/evaluation/test_routing_evaluator.py` 及路由单元测试中注入 typed failing adapter，断言生产 `classify_route()` 的回退结果和 diagnostics；不得把 mock 结果混入正式模型指标。

### 8.3 正式门禁

正式生产模型报告至少满足：

```text
highRiskMissCount == 0
riskRecall == 1.00
primaryIntentAccuracy >= 0.95
workItemCountAccuracy >= 0.95
routePlanExactMatch >= 0.95
contextRelationAccuracy >= 0.95
hardDataDependencyAccuracy == 1.00
orderOnlyHardDependencyErrorRate <= 0.02
singleGoalNoOversplitRate >= 0.98
```

RoutePlan exact match 不再包含 TaskKind，只比较 V3 有效字段。

`app/evaluation/config.py` 的正式 routing 门禁常量固定为本节数值，不新增环境变量调低准确率：删除 `EVAL_ROUTING_PRIMARY_INTENT_THRESHOLD/EVAL_ROUTING_ROUTE_PLAN_THRESHOLD/EVAL_ROUTING_DEPENDENCY_THRESHOLD` 的可调 override，改为代码常量。Evaluator 必须实际生成并纳入 `passed` 的全部九个 gate，尤其不能像旧实现一样只报告 `workItemCountAccuracy` 而不设 gate。指标定义固定为：逐 case 布尔均值；`riskRecall` 只以 expected primary RISK case 为分母；`singleGoalNoOversplitRate` 以带 `single-goal` tag 且 `workItemCount=1` 的 case 为分母；依赖切片为空属于 `metricErrors` 并失败，不能按 1.0 通过。

九个指标的精确定义为：

- `highRiskMissCount`：expected `primaryIntent=RISK` 但实际 primary 不是 RISK 的 case 数；
- `riskRecall`：expected primary RISK case 中实际 primary RISK 的比例；
- `primaryIntentAccuracy`、`workItemCountAccuracy`、`routePlanExactMatch`、`contextRelationAccuracy`：分别取全体可评分 case 对应布尔值的均值，任何 SUT 异常记该 case 为 false 并另记 error；
- `hardDataDependencyAccuracy`：以 expected `hardDataEdges` 非空的 case 为分母，实际 HARD_DATA 边集合与 expected 完全相等的 case 比例；
- `orderOnlyHardDependencyErrorRate`：以全部 expected ORDER_ONLY 无向边为分母，其中在实际 HARD_DATA 边中出现同一无向 pair 的边数比例；方向不同也算错误；
- `singleGoalNoOversplitRate`：带 `single-goal` tag 且 expected `workItemCount=1` 的 case 中，实际恰好一个 WorkItem 的比例。

除逐 case SUT 异常外，dataset/contract 加载错误、依赖切片分母为零、risk 分母为零或 single-goal 分母为零都写入 `metricErrors` 并使正式门禁失败，不得用零除默认值制造通过。

真实模型报告必须记录：

- provider、model、模型 digest（可获得时）；
- Prompt/Schema 版本；
- 融合常量版本；
- 数据集路径和 sha256；
- Git commit 和 dirty 状态；
- 运行时间；
- `HIGH_RISK_HARD` / `LLM_ACCEPTED` / `LLM_LIMIT` / `RULE_FALLBACK` / `RULE_LIMIT_FALLBACK` 数量；
- 各 fallbackReason 数量。

正式模型评测必须通过显式环境变量运行。runner 启动前必须确认 Understanding profile 的 provider 是 `openai` 或 `ollama`、model 非空且不是 mock/canned client，并确认统一 Embedding backend `available()==True` 且 `embedding_identity()` 的 digest 非空、`digest_resolved=True`；任一条件不满足时报告带错误码的 `NOT_RUN`，整体门禁不得显示通过。正式运行中某个 case 的 provider/Embedding 瞬时失败按生产语义回退并计入指标与 fallback 统计，不能事后把失败 case 从分母移除。

唯一允许的评测开关固定为 `RUN_ROUTING_MODEL_EVAL=1`；它只控制是否执行耗时的离线真实模型评测，不改变生产路由。删除 `RUN_SEMANTIC_MODEL_EVAL`。`--suite all` 和 `--suite release` 必须包含唯一的 `routing` 正式 suite；若未显式启用真实模型，整体结果必须失败或 `NOT_RUN`，不得以规则-only 报告替代。

V3 数据集 hash 与 V1/V2 不同，旧 routing baseline 只能作为冻结历史。V3 第一次真实评测生成新的 candidate baseline；`routing_diff.py` 只比较相同 V3 数据集 hash 的报告，不再写死 V1/V2 ORDER_ONLY 迁移说明。跨契约报告必须显示 `INCOMPARABLE_DATASET`，不能计算伪 delta。

baseline identity 必须至少包含 `suite="routing"`、`profile`、dataset sha256、Prompt 版本、Understanding schema name、fusion constants version、Understanding provider/model 和 Embedding provider/model/digest；任一不同都视为 stale/incomparable。`routing_diff.py` 只有 identity 与 dataset hash 全部匹配时才计算本节九个 gate 指标的 delta；否则 `changes=[]`、`status="INCOMPARABLE_DATASET"` 并列出不同字段。首次 candidate 未通过门禁不得更新 baseline，沿用现有 `update_baseline()` 的 passed 前置条件。

报告不得保存未脱敏完整用户原文、完整 Prompt 或完整历史，只保存 case ID/hash、expected/actual 结构摘要和错误码。`sourceTextFragments` 的逐项正确布尔可保存，但实际 `sourceText` 只能保存 SHA-256 和字符长度，不得写明文；失败定位需要原文时只能在本地受控测试日志临时查看，不能进入正式报告。

---

## 9. 分阶段实施

### 阶段 0：Git、基线和已有修改

1. 执行 `git status --short`。
2. 如果存在未提交修改：
   - 逐文件检查来源和意图；
   - 运行与该修改相关的最小测试；
   - 测试通过且归属明确时单独建立 checkpoint；
   - 归属不明、与本文冲突或测试失败时立即停止并向用户说明；
   - 禁止 reset、checkout、覆盖或把不相关修改混入本次提交。
3. 本文编写完成时已知工作区只有本文档未跟踪；实际实施时必须重新检查。若仍只有本文档且内容与本任务一致，先检查 UTF-8 和文档 diff，单独提交 `docs: add five-intent route v3 refactor guide`；若出现其他修改，按上一条处理，不得假定归属。
4. 记录当前 commit、Python/依赖版本、生产 Understanding provider/model 配置、Embedding provider/model/digest、旧数据集 sha256 和现有 routing 报告路径。
5. 运行当前完整测试并保存准确的通过/失败/跳过数量；若 baseline 已失败，先判断是否与已有修改有关，归属不明或无法得到可用基线时停止。
6. 将完成上述审查、已有修改 checkpoint、方案提交和 baseline 后的当前 HEAD hash 记录为改造前 checkpoint。若第 3 步刚提交本文档，该 docs commit 就是 checkpoint；若工作区原本干净且本文档已经在 HEAD 中，则直接记录当前 HEAD。不得为了制造 checkpoint 创建无内容提交。

### 阶段 1：V3 Schema 与 TaskKind 原子切换

一次性完成：

- 删除 `TaskKind`；
- 同步从 `IntentSegmentDecision`、`IntentCandidate`、`FusionResult`、`RuleSegmentDraft`、`AnchoredSegment` 和所有构造调用中删除 TaskKind；`RuleSegmentDraft.rule_candidate` 改为可空；该删除不得留到阶段 3，否则阶段 1 会产生导入失败；
- 引入 UnderstandingDecision V3；
- 引入 RoutePlan V3；
- 引入 ClarificationRequest V3；
- 引入 SpecialistResult V2；
- 同步升级 `build_intent_prompt()`、schema name，并新增唯一 `UnderstandingService`；生产 UnderstandingAgent 立即改用该服务，但本阶段暂不删除复杂度门控；否则 V3 Schema 在阶段 2 前没有可用的生产模型调用方；
- 删除生产 payload、Specialist result、Trace、Context、harness 和恢复状态中的 `taskKind`；
- 更新 ID seed；
- 在 ChatRequest 增加 `MAX_INPUT_CHARS=1000` 边界，删除 `agent_max_work_items/AGENT_MAX_WORK_ITEMS`，全部容量调用改用 `MAX_WORK_ITEMS=4`；
- 添加数据库 migration，严格回填 intent 并中断旧 WAITING_USER V2 状态；
- 将澄清注册表改为 Intent key；
- Coordinator、Specialist、Response 和 Academic 具体计划行为一次性改为 Intent-only，并引入唯一共享计划判断函数；
- 更新所有直接依赖的测试和 fixture。
- 从全部可导入的 `app/evaluation` 类型中删除 TaskKind 引用；正式评测合并和数据集硬切换仍在阶段 5 完成。

“TaskKind 原子切换”包含所有当前可加载数据和评测代码，不能提交一个生产已删枚举、评测 import 仍引用它的 checkpoint。因此本阶段还必须：

- 定义 `ProductionRoutingExpectedV3` 与 `EndToEndExpectedRoute`，删除旧 `ExpectedRoute/SemanticPlanningExpected/SemanticPlanningCase`；
- 删除 semantic-planning evaluator/suite/loader，先建立可注入 fake adapter 的 `ProductionRoutingEvaluator` 骨架并更新结构测试；
- 把唯一 routing 数据、非空 E2E route expected、生成脚本和 README 结构切到无 TaskKind 的新 Schema；
- 同步重建 RAGAS audit/hash。若正式构建依赖不可用，本阶段停止，不能建立 Schema cutover checkpoint；
- 阶段 5 负责补齐/复核正式 V3 标注、真实模型执行、门禁和同契约对比，不再做第二次 Schema 切换。

该阶段不得留下“下一阶段再删”的 V2 解析器。

至少运行：

```powershell
python -m pytest tests/test_route_plan_v3.py tests/test_clarification_study_plan_flow.py tests/test_specialist_agents_v2.py
python -m pytest tests/test_context_builder.py tests/test_trace_privacy.py
python -m pytest tests/test_coordinator_work_items_v3.py tests/test_migrations_and_import.py
python -m pytest tests/evaluation/test_contracts.py tests/evaluation/test_datasets.py tests/evaluation/test_routing_evaluator.py
python -m pytest --collect-only
```

实际测试文件名可按仓库约定调整，但旧 RoutePlan/Coordinator V2 测试文件应重命名或删除，不得保留误导名称；`tests/test_specialist_agents_v2.py` 的 V2 是本方案仍在使用的 SpecialistResult V2，必须保留该版本含义。

checkpoint：

```text
refactor: remove task kind and cut over route schemas to v3
```

### 阶段 2：取消复杂度门控，统一上下文语义规划

完成：

- 普通请求固定调用一次 `_semantic_route()`；
- 删除 `ComplexityAssessment`、调用门禁和对应配置；
- `routeStatus=TOO_MANY_WORK_ITEMS` 由模型在调用后决定容量回退；规则 `limit_exceeded` 只在模型失败后生效；
- 加入省略追问、纠正、换题、同目标不过度拆分、超容量示例；
- 保留规则草案作为校验、显式依赖和失败回退；
- 简化 PlanningDiagnostics。

必须测试：

- 普通单目标调用模型恰好一次；
- 三个典型追问调用模型恰好一次；
- 高风险调用模型零次；
- 模型 `ROUTE` 的 1～4 项结果优先于规则草案的疑似超容量；
- 模型 `TOO_MANY_WORK_ITEMS` 生成一个 `CHAT + WORK_ITEM_LIMIT_EXCEEDED`；
- 模型失败且规则确认超过四项时进入 `RULE_LIMIT_FALLBACK`；
- 模型 repair 不超过一次；
- 模型失败进入统一规则回退；
- 非预期程序错误继续抛出。

至少运行：

```powershell
python -m pytest tests/test_understanding_single_path.py tests/test_intent_context.py tests/test_route_planning.py tests/test_routing_safety_boundaries.py
```

checkpoint：

```text
refactor: route every ordinary turn through contextual planning
```

### 阶段 3：逐 Segment 五类三路融合

完成：

- 固定 `70/20/10` 权重；
- Rule 支持弃权；
- Embedding 五类模板和相似度/间隔弃权；
- 每个普通 ROUTE 最终 Segment 恰好融合一次；高风险、容量和 AMBIGUOUS 控制分支不融合；
- 删除旧默认高置信 CHAT；
- 删除旧融合配置开关和冗余 reasonCode；

必须测试：

- 三路一致；
- 两路一致、一路冲突；
- LLM 上下文候选与无信息 Rule/Embedding 的追问场景；
- 每一路分别不可用；
- 所有信号弃权；
- 低分和低 margin；
- 复合请求逐 Segment 分类；
- ACADEMIC 不再出现计划/职业两个分类标签。

至少运行：

```powershell
python -m pytest tests/test_intent_fusion.py tests/test_route_planning.py tests/test_route_plan_v3.py
```

checkpoint：

```text
refactor: fuse five-intent signals per semantic segment
```

### 阶段 4：下游与持久化收口

完成：

- 复核 Coordinator 只按 Intent 调度，Specialist/Response 不存在隐藏子类字段；
- 澄清创建、恢复、取消、重入和 V2 cutover 后首条新输入完整通过；
- 数据库 upgrade/downgrade、非法行原子失败和 WAITING_USER 中断测试；
- `app/agents/harness.py`、Context、Trace、turn execution 和管理端 trace 输出只产生新字段；
- 校验 SpecialistResult V2、Embedding 模板缓存和 route diagnostics 白名单；
- 运行真实聊天 harness，确认恢复、风险抢占、依赖执行和最终合成无回归；
- 全仓清理死代码。

必须运行澄清、Coordinator、Specialist、Response、迁移和端到端安全测试。至少运行：

```powershell
python -m pytest tests/test_clarification_study_plan_flow.py tests/test_coordinator_work_items_v3.py tests/test_specialist_agents_v2.py tests/test_event_driven_multi_agent.py
python -m pytest tests/test_context_builder.py tests/test_trace_privacy.py tests/test_migrations_and_import.py tests/evaluation/test_runtime_adapter.py tests/evaluation/test_end_to_end_attribution.py
```

checkpoint：

```text
refactor: simplify downstream execution to intent-only work items
```

### 阶段 5：评测与数据集切换

完成：

- 复核并扩充阶段 1 已切换的唯一 `routing-v3.jsonl` 标注与切片覆盖，不再改变 Schema；
- 完成 ProductionRoutingEvaluator 指标、门禁、隐私报告和真实模型元数据；
- 复核 evaluation config、dataset loader、routing diff 和 baseline 规则不存在旧 suite/数据集残留；
- 用正式脚本再次验证 routing/RAGAS 数据、RAGAS audit、项目 README 和 dataset README 一致；
- 运行完整单元/集成测试；
- 在真实模型环境可用时运行正式模型评测；
- 生成与阶段 0 的共同指标对比。

阶段 0 的 V1/V2 报告与 V3 报告只能比较仍同义的公共指标，例如五类 primary Intent；不得把不同数据集上的 RoutePlan exact match 直接作回归结论。V3 正式门禁以 V3 数据集和新 baseline 为准。

至少运行：

```powershell
python -m pytest tests/evaluation/test_contracts.py tests/evaluation/test_datasets.py tests/evaluation/test_routing_evaluator.py tests/evaluation/test_routing_diff.py tests/evaluation/test_gates.py
python -m pytest tests/test_ragas_dataset_builder.py tests/evaluation/test_ragas_dependency_contract.py tests/evaluation/test_runtime_adapter.py
python -m pytest
```

真实模型评测命令只在第 10.7 节的显式开关和环境前提满足后运行。

checkpoint：

```text
test: replace routing evaluation with five-intent production contract
```

---

## 10. 测试要求

### 10.1 Schema 删除测试

必须断言：

- V3 WorkItem 带 `taskKind` 时拒绝；
- V3 Segment 带 `taskKind` 时拒绝；
- V2 `schemaVersion` 被拒绝；
- 未知字段被拒绝；
- RoutePlan 最多四个 WorkItem；
- ChatRequest 恰好 1000 字符合法，1001 字符在持久化和路由前返回 422；内部超长 planning_input 触发不变量失败，不截断；
- Settings 不再暴露 `agent_max_work_items`，即使上层传入同名未知值也不能改变语义；Coordinator 与 Prompt 统一锁定四项；
- Understanding `ROUTE` 必须有 1～4 个 Segment；
- Understanding `TOO_MANY_WORK_ITEMS` 必须是空 segments/空 dependencyHints；
- `TOO_MANY_WORK_ITEMS` 最终只生成单个五类 CHAT 容量提示；
- `AMBIGUOUS` 只允许单个完整 CHAT Segment，最终不运行融合且生成固定歧义 RoutePlan；
- Intent 只允许五类；
- ID 稳定且 seed 不含 TaskKind。
- SpecialistResult V2 拒绝 `taskKind`，旧 V1 被拒绝。

### 10.2 上下文测试

至少覆盖：

```text
第二种呢？               → ACADEMIC / CONTINUE
那我现在该怎么办？       → MENTAL / CONTINUE
它的入口在哪里？         → CAMPUS / CONTINUE
刚才说错了，是研究生     → CAMPUS / CORRECTION
换个问题，Python 怎么排查 → CHAT / NEW_TOPIC
```

上述测试必须经过真实的生产调用形态：传入 `memory_context`、返回 `UnderstandingInvocationResult` 的 `semantic_classifier` 和生产 Settings。仅模拟最终 RoutePlan 不算覆盖；测试可注入 fusion fake，但 Settings 中不得再有能改变融合或容量语义的字段。

### 10.3 分段和依赖测试

- 同一制度事实多个属性不拆；
- 同一心理目标不同描述不拆；
- 跨 Intent 独立目标拆分；
- 同 Intent 独立目标允许拆分；
- sourceText 精确连续原文；
- HARD_DATA 写入 dependsOn；
- ORDER_ONLY 不写 dependsOn；
- 依赖低置信拒绝；
- 纯 LLM hint 成环使整份 LLM 计划回退；
- LLM hint 与规则边合并后成环使整份 LLM 计划回退；
- 纯规则图成环和最终 RoutePlan 图不变量失败必须抛出；
- 稳定拓扑排序。

### 10.4 回退测试

- 模型未配置；
- provider 不可用；
- 超时；
- 非法 JSON；
- V3 Schema 非法；
- 上下文关系非法；
- 锚定失败；
- 覆盖失败；
- 重叠；
- LLM 依赖端点、关系冲突和成环；
- Embedding 不可用；
- Rule 弃权；
- 所有信号不足时低置信 CHAT。

### 10.5 持久化切换测试

- 合法旧 `task_kind` 严格回填五类 `intent`；
- 非法旧值使 migration 原子失败；
- 所有旧 `WAITING_USER` 记录被中断，`finish_reason=ROUTE_SCHEMA_V3_CUTOVER`；
- cutover tombstone 不含旧 RoutePlan、`taskKind` 或用户原文，且旧 active 行的 original/known/missing/question 已清空；
- V3 应用不会尝试恢复旧状态，用户下一条消息进入新的 Understanding 路径；
- 当前 ORM/数据库表有 `intent`、无 `task_kind`；
- 新建、继续追问、取消、换题和高风险抢占的 V3 澄清流程通过。

### 10.6 静态清理检查

完成后先检查活跃生产、数据和脚本不得命中旧路由字段或类型，再单独审计负向测试：

```powershell
rg -n 'TaskKind|task_kind|taskKind|work_item_task_kinds|workItemTaskKinds|CAREER_DECISION_SIGNAL|STUDY_PLAN_SIGNAL' app scripts README.md
rg -n 'TaskKind|task_kind|taskKind|work_item_task_kinds|workItemTaskKinds|CAREER_DECISION_SIGNAL|STUDY_PLAN_SIGNAL' tests
rg -n 'schemaVersion.*2' app/agents/routing.py app/services/intent_fusion.py app/services/intent_prompts.py app/services/clarification_models.py app/services/clarifications.py app/evaluation
rg -n 'semantic-planning|routing-contract-v2|routing-semantic-v2|routing-v1' app/evaluation scripts README.md
rg -n 'AGENT_MAX_WORK_ITEMS|agent_max_work_items' app docker-compose.yml .env.example README.md tests
rg -n 'intent_embedding_enabled|intent_rule_weight|intent_llm_weight|intent_embedding_weight|intent_min_confidence|intent_min_margin|intent_decomposition_complexity_threshold|intent_dependency_min_confidence|intent_segment_source_max_chars|intent_embedding_provider|intent_embedding_model|intent_planning_diagnostics_enabled' app docker-compose.yml .env.example README.md tests
```

第一条在活跃生产源码、数据、脚本和 README 中必须零命中；历史 Alembic migration 不在该命令范围。第二条测试命中只能出现在明确断言 V3 拒绝旧字段、migration 读取旧列/旧值的负向用例中，正常 fixture 和成功路径不得含旧字段。第三条只检查路由、理解、澄清和评测契约；Specialist result 使用本方案规定的合法 V2，项目内其他与本次路由无关的 V1/V2 Schema 不得顺带修改。第四条旧正式 suite/数据集标识必须零命中。第五、六条除明确验证 Settings 拒绝旧环境变量/字段的负向测试外必须零命中。`target/` 中冻结报告不属于生产代码检查范围。

检查中文未被意外改写：

```powershell
git diff --name-only | ForEach-Object {
    if (Test-Path $_) { Select-String -Path $_ -Pattern '\\u[0-9a-fA-F]{4}' }
}
```

如果正常中文源码或数据集中出现 Unicode 转义，必须恢复为可读 UTF-8 中文。

### 10.7 正式评测与完整测试

每个阶段运行目标测试；最终必须运行：

```powershell
python -m pytest
```

任何本次改造导致的失败都必须修复。不得通过删除有效测试、放宽正确期望或增加 `xfail/skip` 规避。

随后运行：

```powershell
$env:RUN_ROUTING_MODEL_EVAL='1'
python -m app.evaluation.runner --suite routing --profile full
python -m app.evaluation.runner --suite release --profile full
```

只有真实模型、Embedding 和依赖环境确实可用时才运行并报告数值。环境不可用时记录准确错误和 `NOT_RUN`，不得把 mock、规则回退、历史报告或其他模型结果写成当前真实指标。

---

## 11. 文件级改造清单

| 文件 | 必须执行的改造 |
|---|---|
| `app/core/enums.py` | 删除 `TaskKind` 和 TaskKind 专属 reasonCode；新增 `AMBIGUOUS_ROUTING` |
| `app/core/config.py` | 删除复杂度、融合权重、阈值、embedding 开关和 `agent_max_work_items`；保留模型/Embedding provider 配置；校验每 Agent 领取上限至少为固定四项 |
| `app/schemas/dtos.py` | ChatRequest.message 固定最大 1000 字符、拒绝纯空白并在持久化前拒绝非法输入 |
| `.env.example`、`docker-compose.yml` | 删除 `AGENT_MAX_WORK_ITEMS`；不得修改或提交开发者本地 `.env` |
| `app/services/understanding.py`（新增） | 封装生产与评测共用的唯一 Understanding V3 structured-call adapter、schema name、模型 options 和异常映射 |
| `app/services/academic_request_policy.py`（新增） | 保存唯一的具体学习计划布尔判断；不得序列化或演化成子分类 |
| `app/services/intent_fusion.py` | V3 Segment；五类候选；固定权重；Rule/Embedding 弃权；删除 task kind；导出唯一 ContextRelation |
| `app/services/intent_prompts.py` | V3 Prompt；`routeStatus`；每轮上下文规划；五类 Intent；追问/纠正/换题/超容量示例 |
| `app/services/ai.py` | `PromptTemplates.intent_prompt()` 若已无调用则删除；若仍有真实调用，只允许薄调用唯一 `build_intent_prompt()`，不得保留旧 list-history 分类 Prompt |
| `app/services/agent_models.py` | 不新增第二套 registry；生产和评测都通过现有 UnderstandingAgent profile 创建同一 AIClient |
| `app/services/embedding.py` | 保持统一 backend；Intent 只复用 factory/identity，不新增 provider；需要时仅补充可测试的严格错误类型 |
| `app/services/route_planning.py` | 删除复杂度门控；规则超容量只作失败回退；保留锚定、依赖图和拓扑排序；Segment 删除 task kind |
| `app/agents/routing.py` | 单一 V3 主流程；每个普通请求一次 LLM；处理模型/规则超容量；逐 Segment 融合；更新 ID |
| `app/agents/autonomous.py` | 复用统一 Understanding adapter；SpecialistResult V2；Specialist/Response 删除 task kind 分支 |
| `app/agents/coordinator.py` | 只按 Intent 创建任务；ClarificationRequest V3 |
| `app/agents/harness.py` | clarification state 改为 intent；只恢复可执行 V3；不得运行时读取旧 V2 |
| `app/agents/event_driven_runtime.py` | 恢复/注入 RoutePlan 时只接受 V3；等待澄清恢复使用 V3 resume context |
| `app/agents/result.py` | 新增严格 `SpecialistResultV2` 模型和唯一序列化边界 |
| `app/agents/events.py` | specialist_result 发布时严格校验 V2，再执行 plan/work metadata 和唯一终态检查 |
| `app/services/clarification_handlers.py` | 注册表改为 Intent；共享具体学习计划判断 |
| `app/services/clarification_models.py` | 删除所有 task_kind 成员；严格 MissingArgument、ClarificationResumeContext V3 和 ClarificationRequest V3 唯一序列化边界 |
| `app/services/clarification_policy.py` | 只解析 Intent |
| `app/services/clarifications.py` | 只保存/恢复 Intent 和 RoutePlan V3 |
| `app/services/context_builder.py` | 抽出共享 view builder；`for_understanding(planning_input)` 显式使用脱敏当前输入；上下文白名单删除 taskKind；清理旧预算策略命名 |
| `app/services/trace.py` | Trace 摘要删除 taskKind，保留 planSource/fallback 诊断 |
| `app/services/turn_execution.py` | 执行摘要删除 taskKind |
| `app/harness/runner.py` | pending clarification 输出改为 Intent；删除旧错误文本 |
| `app/models/entities.py` | `PendingClarification.task_kind` 改为 `intent` |
| `migrations/versions/0014_five_intent_route_v3.py` | 严格回填 Intent；中断旧 WAITING_USER；写 V3 tombstone；删除 task_kind |
| `app/evaluation/contracts.py` | V3 final route contract，删除 task kinds |
| `app/evaluation/dataset.py` | 只保留 V3 routing loader；删除 SemanticPlanningCase loader |
| `app/evaluation/config.py` | 增加/调整 V3 正式门禁配置，删除旧双套件遗留配置 |
| `app/evaluation/evaluators/routing.py`、`app/evaluation/evaluators/__init__.py` | 改为并只导出 `ProductionRoutingEvaluator`；完整生产路由；固定 exact-match 口径 |
| `app/evaluation/evaluators/semantic_planning.py` | 删除；不得保留第二条正式模型评测路径 |
| `app/evaluation/runner.py` | 删除 semantic-planning suite；routing 复用生产 adapter；显式真实模型门禁 |
| `app/evaluation/reporting/routing_diff.py` | 仅比较相同 V3 数据集；删除写死 V1/V2 契约说明 |
| `app/evaluation/datasets/routing-v3.jsonl` | 唯一活跃 V3 路由数据集 |
| `app/evaluation/datasets/routing-v1.jsonl`、`routing-contract-v2.jsonl`、`routing-semantic-v2.jsonl` | 从活跃目录删除；历史由已保存报告和 Git 保留 |
| `app/evaluation/datasets/mindbridge-e2e-ragas-v1.jsonl` | 嵌入 route expected 切换 V3，删除 task kinds |
| `app/evaluation/datasets/e2e-safety-v1.jsonl` | 非空 route expected 切换严格 EndToEndExpectedRoute，删除旧冗余字段 |
| `app/evaluation/datasets/mindbridge-e2e-ragas-v1.audit.json` | 由正式构建脚本重建，hash 必须与 JSONL 一致 |
| `app/evaluation/datasets/README.md` | 只说明唯一 V3 routing 数据集和正式运行命令 |
| `scripts/build_routing_dataset.py` | 只生成 V3 五类 expected |
| `scripts/build_ragas_dataset.py` | 删除 workItemTaskKinds；生成 V3 expected；将 `BUILD_SCRIPT_VERSION` 从现有 v5 升为新值并重建 audit |
| `README.md` | 删除 RoutePlan V2、规则快速路径、TaskKind 和双评测命令说明，改为实际 V3 行为 |
| `tests/test_migrations_and_import.py` | 增加严格回填、非法值原子失败、WAITING_USER cutover 测试 |
| `tests/test_chat_turns.py`、`tests/test_chat_completion.py` | 增加 message 1000/1001 边界及超长输入不落库测试 |
| `tests/test_intent_context.py`、`tests/test_understanding_single_path.py` | V3 Prompt/adapter 一次调用、上下文裁剪、容量与异常 metadata 契约 |
| `tests/test_refactor_phase0_contracts.py`、`tests/test_event_driven_multi_agent.py` | specialist_result fixture 升级严格 V2；测试用非 Settings stub 可保留小领取值，真实 Settings 校验另测固定容量 |
| `tests/test_route_plan_v2.py` | 重命名为 `tests/test_route_plan_v3.py`，删除 V2 成功路径，只保留 V2 拒绝负测 |
| `tests/test_coordinator_work_items_v2.py` | 重命名为 `tests/test_coordinator_work_items_v3.py`，fixture 和断言只使用 Intent/V3 |
| `tests/test_specialist_agents_v2.py` | 保留文件名但把 fixture/断言切到本方案新的严格 SpecialistResult V2；删除 Specialist V1 成功路径 |
| `tests/evaluation/test_contracts.py`、`test_datasets.py`、`test_routing_evaluator.py`、`test_routing_diff.py` | 切换唯一 V3 契约、严格 shape、完整生产 SUT 和报告语义 |
| `tests/evaluation/test_semantic_planning_evaluator.py` | 删除，其有效场景合并进生产 routing evaluator 测试 |
| 其余 `tests/` | V3 Schema、上下文、融合、依赖、澄清、Specialist V2 和生产评测全覆盖；旧 V2 命名同步改正 |

`app/knowledge/retrieval_taxonomy.yaml` 中类似 `STUDY_PLANNING` 的知识检索概念不属于路由 Intent，可在确有检索用途时保留；它不得写入 RoutePlan 或参与 Coordinator 分流。

`AgentCapability.GENERAL_CHAT`、`AgentCapability.ACADEMIC_PLANNING` 和 `KnowledgeDomain` 同样不是 TaskKind，继续保留。不得对全仓所有 `schemaVersion=1/2` 做机械替换：Chat turn、Memory V2、Knowledge Ingestion V2、历史 migration 和冻结 `target/evaluation` 报告不属于本次路由契约。

---

## 12. 明确禁止的实现

禁止提交以下任何内容：

```python
# 禁止：旧字段兼容
task_kind = payload.get("taskKind")

# 禁止：双版本解析
if payload["schemaVersion"] == 2:
    return parse_v2(payload)

# 禁止：新旧路由开关
if settings.use_five_intent_router:
    return new_route(...)
return old_route(...)

# 禁止：复杂度换名后继续拦模型
if not looks_complex(text):
    return rule_only(...)

# 禁止：启发式规则在普通请求调用模型前判定超容量
if rule_draft.limit_exceeded:
    return build_limit_route_v3(...)

# 禁止：未命中即高置信 CHAT
return IntentCandidate(IntentType.CHAT, 0.96, "RULE")

# 禁止：隐藏 TaskKind
academic_mode = "STUDY_PLAN" if "计划" in text else "CAREER_DECISION"
```

也禁止：

- 同时生成两份 RoutePlan 再比较；
- 将模型完整原文计划写入 shadow metadata；
- 为追问不断扩充代词触发词表；
- 用 Embedding 决定是否调用 LLM；
- 让 Specialist 自由改变 RoutePlan Intent 或依赖；
- 把 ORDER_ONLY 写成 dependsOn；
- 从历史文本复制 sourceText；
- 捕获所有异常后静默返回 CHAT；
- 为了通过测试修改正确标注；
- 伪造真实模型指标。
- 删除 Specialist `taskKind` 后仍维持 `schemaVersion=1`；
- 手工修改 RAGAS JSONL 后不重建 audit/hash；
- 将旧 `WAITING_USER` V2 RoutePlan 留给 V3 运行时碰运气解析；
- 为迁移旧澄清状态在生产代码增加 V2 parser；
- 把 `routeStatus` 或共享学习计划布尔判断当作第六个 Intent 或隐藏 TaskKind；
- 在生产和评测各复制一份 Understanding Prompt/Schema/模型 options；
- 在一个多 Segment 请求中为每段重复初始化 Embedding backend 或重算模板向量。

---

## 13. 完成标准与交付报告

只有全部满足以下条件才算完成：

1. 生产 RoutePlan、UnderstandingDecision、Clarification、Specialist result、Trace 和数据库当前 Schema 中不存在 `taskKind`。
2. `TaskKind` 枚举不存在。
3. 每个进入新路由的普通请求固定调用一次 Understanding LLM；高风险调用零次；规则超容量不得模型前短路。
4. 每个普通 ROUTE 最终 Segment 完成一次五类三路融合，失败信号可弃权；高风险、容量和 AMBIGUOUS 控制分支按规范不融合。
5. 三个典型省略追问在完整生产路径中正确恢复 Intent。
6. 高风险召回和历史风险隔离不回归。
7. HARD_DATA/ORDER_ONLY 语义及 DAG 不回归。
8. 澄清创建、持久化、恢复和取消在 V3 下通过；旧 WAITING_USER 状态已由 migration 原子中断。
9. Specialist result 只使用 V2 且不含 taskKind。
10. 唯一活跃 V3 数据集、RAGAS audit/hash、README 和生成脚本一致。
11. 旧 V2 活跃 loader、semantic-planning 正式 suite、数据集和生产解析路径已删除。
12. 全量 pytest 通过，且没有为本次失败新增 skip/xfail。
13. 静态旧字段和 Unicode 转义检查通过。
14. 真实模型评测实际运行并通过门禁；若环境不可用，明确列为未完成，不得标记完成。
15. Git 工作区最终只包含本次有意修改，所有阶段 checkpoint 可追溯。

最终报告必须列出：

- 修改文件；
- 删除的字段、类型、配置和兼容代码；
- 新的五类三路融合实现；
- 数据库 migration 与数据处理结果；
- 各阶段测试命令、通过/失败/跳过数量；
- 正式模型 provider、model、数据集、commit 和真实指标；
- 未运行的模型评测；
- 未完成项和风险；
- 最终 Git commit 与 dirty 状态。

---

## 14. 设计依据

本方案吸收但不直接复制以下成熟模式：

- Rasa CALM：每轮使用对话状态生成结构化命令，LLM 理解与确定性 Flow 执行分离；
- LangChain/LangGraph Router：先分解/分类，再向零个、一个或多个 Specialist fan-out，最后合成；
- OpenAI Agents：模型负责 triage/委派，代码和 guardrails 负责执行边界；
- Microsoft/AWS 多 Agent：Supervisor 负责上下文路由，Specialist 职责保持明确；
- EchoMind：LLM、Embedding、Pattern 三路融合，LLM 作为上下文语义主信号。

MindBridge 不引入上述框架的新依赖，只在现有 EventDrivenCoordinator、UnderstandingAgent、RoutePlan、IntentFusion 和 ContextBuilder 上完成简化。

参考：

- https://rasa.com/docs/reference/config/components/llm-command-generators/
- https://docs.langchain.com/oss/python/langchain/multi-agent/router
- https://openai.github.io/openai-agents-python/multi_agent/
- https://learn.microsoft.com/en-us/agent-framework/overview/
- https://docs.aws.amazon.com/bedrock/latest/userguide/create-multi-agent-collaboration.html
