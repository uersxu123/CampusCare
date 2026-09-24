# MindBridge LLM 语义任务分解与依赖图改造实施指南

> 文档日期：2026-08-12
>
> 目标：在保留现有 `RoutePlan V2`、高风险抢占、澄清恢复和事件驱动多 Agent 框架的前提下，让 LLM 真正参与复杂请求的工作项分解与依赖识别，并补齐下游执行、评测、可观测性和隐私适配。
>
> 目标读者：负责直接修改、测试和验收代码的 AI 或开发人员。
>
> 适用项目：`mindbridge-py`。
>
> 实施方式：按本文阶段和文件清单增量改造，禁止一次性重写整个 Agent Runtime。

---

## 1. 给编码 AI 的执行结论

本任务不是简单调整 `INTENT_LLM_WEIGHT`，也不是只修改 `_segments()`。当前核心问题是：规则先决定 segment 数量和边界，LLM 只能按数组下标修正已有段的标签；LLM 返回的额外 segment、`sourceText` 和 `dependencyHints` 没有真正生成工作项和依赖图。

编码 AI 必须遵守以下结论：

1. 保留当前五类 Intent：`CHAT / ACADEMIC / CAMPUS / MENTAL / RISK`。
2. 保留 `RoutePlan schemaVersion=2` 的公开字段和严格校验；第一阶段不做数据库迁移。
3. 当前轮高风险硬规则始终最先执行；一旦命中，必须只输出一个 `RISK/HIGH_RISK_SUPPORT` 工作项，不调用普通语义分解。
4. LLM 只生成内部、受校验的“理解候选”，不得生成最终 `planId`、`workItemId`、任意澄清字段或未经校验的 `dependsOn`。
5. 对复杂请求，LLM 可以决定候选 segment 数量、语义边界、Intent、TaskKind、上下文关系和依赖提示。
6. 最终 `WorkItem`、稳定 ID、已知参数、缺失参数、DAG、拓扑顺序和 `RoutePlan V2` 必须由确定性代码生成。
7. `dependsOn` 只表达硬数据依赖，不能表达普通的回答顺序。用户说“先 A 再 B”不自动等价于 B 依赖 A。
8. 执行顺序由 `dependsOn` 决定；最终展示顺序由 `synthesisOrder` 决定。`synthesisOrder` 必须是合法的稳定拓扑顺序。
9. 简单明确请求保留规则快速路径；复杂度评分达到固定阈值的请求调用一次 Understanding LLM。
10. LLM、Embedding 或中间 Schema 任一失败时，必须回退确定性规则计划，不得返回半合法 RoutePlan。
11. 提高分段能力时，必须同步修复同一 Agent 最多处理 3 个任务而 RoutePlan 最多允许 4 项的容量冲突。
12. 正式路由评测必须增加“带生产 Understanding 模型的评测路径”；现有 `RoutingEvaluator` 只调用 `classify_route()`，不能代表生产 LLM 路由质量。
13. 生产代码只保留一套新的路由规划实现，不保留 shadow、旧新双计划、按开关切换旧算法或 V1/V2 双解析分支。
14. 简单请求快速通过、复杂请求调用 LLM、LLM 失败回退规则草案，都是同一套规划器内部的门控和容错，不得实现成两套可长期切换的生产路由。

### 1.1 本文与旧文档的关系

本指南承接以下已有设计，但覆盖其中关于“先按规则分段、再逐段融合”的实现方式：

- `MIND_BRIDGE_CONTEXT_AWARE_INTENT_FUSION_IMPLEMENTATION_GUIDE_20260807.md`
- `MIND_BRIDGE_FIVE_INTENT_EVENT_DRIVEN_SPECIALIST_MCP_RAG_REFACTOR_PLAN_20260806.md`

旧文档中的以下边界继续有效：

- RISK 硬抢占；
- RoutePlan 由程序生成；
- LLM 不生成任意 ID；
- 澄清字段使用注册表白名单；
- Coordinator、Specialist、Response 和 RAG 的权限隔离；
- 先用冻结的基线报告和离线模型评测完成对比，再一次性切换到新的单一生产实现。

本文新增或修正：

- LLM segment 可以真正成为工作项候选；
- `dependencyHints` 必须被消费；
- 依赖关系区分硬数据依赖和普通顺序；
- 分段增强后的 Coordinator 容量、依赖交接、报告、Trace 和评测适配。

### 1.2 第一阶段不可变实现决策

以下决策用于消除编码 AI 的自由发挥空间。若后文出现“建议”“可选”或与本节冲突的内容，以本节为准，并在编码前先修正文档冲突，不得自行选择另一种实现。

1. **本次范围仅包含第 17 节阶段 0～5。** 第 12 节的 `dependencyHandoff`、结构化上游事实传递、动态重规划和独立分支跨轮恢复均为后续独立项目，不得在本次实现。
2. **生产环境只有一套路由规划代码。** 删除现有 fusion shadow 计算、完整 `shadowRoutePlan` metadata、`intent_fusion_shadow_mode` 以及新增 decomposition shadow 的设想。不得在同一次请求中同时生成旧计划和新计划。
3. **不保留旧算法运行时开关。** 新实现通过全部测试和离线对比后直接替换旧的 `_segments() -> _apply_intent_fusion() -> _merge_equivalent()` 编排。回滚依靠版本控制和部署版本，不依靠生产代码中的旧新双分支。
4. **单一规划器内部允许确定性门控和容错。** 简单请求使用规则快速结果；复杂请求调用一次 Understanding LLM；模型不可用、结构化输出非法或锚定失败时使用本轮已经生成的规则草案。这不是兼容路径，不得复制 RoutePlan Builder。
5. **不做 DAG 传递约简。** 第一阶段下游只接收直接依赖结果，所有验证通过且语义上直接声明的 `HARD_DATA` 边都必须保留。只有未来支持祖先依赖结果闭包后，才能重新评估传递约简。
6. **依赖协议只包含 `HARD_DATA` 和 `ORDER_ONLY`。** 没有 dependency hint 即表示独立，禁止输出或保存 `INDEPENDENT` 关系。
7. **LLM 不生成最终 objective。** 内部 Schema 不含 objective；最终 objective、参数、ID、优先级和 RoutePlan 全部由确定性代码生成。
8. **LLM 接纳采用整份接纳或整份回退。** 不拼接部分 LLM segments 与部分规则 segments；回退规则边界时同时丢弃全部 LLM dependency hints。
9. **接纳后的 LLM segments 不再做语义合并。** 重复、重叠、包含、漏拆、越界或超过容量时拒绝整份 LLM 计划；不得按相同 Intent、TaskKind、Agent 或 objective 合并。
10. **评测对比只存在于测试和报告。** 使用阶段 0 保存的基线报告与新实现报告做离线比较，不把基线算法复制进生产模块。
11. **正式代码改造前必须检查并提交 Git 基线。** 先审查全部未提交代码的归属，按独立关注点运行相应测试并分别建立 checkpoint 提交；不得把知识入库、路由改造或其他不相关修改混成一个提交。无法判断归属、测试失败或包含凭据时停止并报告，不得擅自提交、覆盖或 stash。
12. **第一阶段固定参数。** `agent_model_understanding_max_tokens=768`、复杂度阈值为 `2`、LLM 硬依赖阈值为 `0.82`、最大 segment 数为运行时 `agent_max_work_items` 且协议硬上限为 `4`。
13. **不新增模型或第三方运行时依赖。** 复用现有 Understanding 模型客户端、Pydantic、Embedding backend、pytest 和评测框架；不得为了分段引入第二个 LLM、外部工作流引擎、图数据库或新的网络服务。

### 1.3 文档修订时的 Git 快照

本指南修订时（2026-08-12）检查到：

```text
HEAD: 715c01e feat: complete specialist agent runtime refactor

未提交代码：
M app/services/knowledge_import.py
M app/services/knowledge_ingestion/pipeline.py
M tests/test_knowledge_ingestion_pipeline.py
M tests/test_migrations_and_import.py

未跟踪文档：
?? MIND_BRIDGE_LLM_SEMANTIC_DECOMPOSITION_DEPENDENCY_REFACTOR_GUIDE_20260812.md
```

以上知识入库修改与本路由任务无关。编码 AI 开始前必须重新执行 Git 检查；如果这些修改仍存在，先审查 diff，确认不含凭据或临时文件，运行 `tests/test_knowledge_ingestion_pipeline.py` 和 `tests/test_migrations_and_import.py`，通过后作为独立知识入库 checkpoint 提交，再开始路由改造。方案文档可以单独提交或与路由改造的第一个文档提交一起提交，但不得混入知识入库 checkpoint。若状态已经变化，以重新检查的结果为准。

---

## 2. 当前代码事实与已确认缺口

### 2.1 当前生产链路

```text
用户输入
  -> ContextBuilder.for_understanding()
  -> UnderstandingAgent
  -> classify_route()
       -> 当前轮风险硬检测
       -> _segments() 规则分段
       -> _classify_segment() 关键词分类
       -> 可选 _apply_intent_fusion()
       -> _merge_equivalent()
       -> RoutePlan V2
  -> SafetyAgent 独立风险评估
  -> Coordinator
       -> 高风险抢占
       -> ClarificationPolicy
       -> 按 workItem 创建 Specialist task
       -> dependsOn 门禁
       -> ResponseAgent fan-in
       -> SafetyAgent 复审
```

### 2.2 当前分段缺口

`app/agents/routing.py::_segments()` 主要只支持：

- “先……再/然后/之后……”；
- 少量心理词与校园词混合时按标点或“也”拆分；
- 其他请求整体保留为一个 segment。

因此以下情况容易漏拆：

- 没有“先/再”的并列任务；
- 心理 + 学业、心理 + 校园等跨域请求；
- 三至四个任务；
- 同一 Intent 下多个独立交付目标；
- 隐含依赖和多轮上下文依赖。

### 2.3 当前 LLM 参与度缺口

当前 `_apply_intent_fusion()` 的问题：

1. 高置信非 CHAT 规则在调用 LLM 前短路。
2. `IntentFusion.fuse()` 内部再次对高置信非 CHAT 规则短路。
3. 融合循环遍历规则 `classified`，LLM segment 只能按索引附着到规则段。
4. 规则只有一段、LLM 返回多段时，多出的 LLM segment 被丢弃。
5. LLM 的 `sourceText` 不进入最终工作项。
6. LLM 的 `dependencyHints` 只做 Schema 校验，没有生成 `dependsOn`。
7. Intent/TaskKind 被 LLM 改写后，`knownArguments/missingArguments` 仍沿用原规则类型的抽取结果。

### 2.4 当前依赖缺口

当前代码只要全文匹配“先……再/然后/之后”，就把所有工作项串成链。这会混淆两种关系：

- 回答顺序：用户希望先听 A，再听 B，但 B 不需要 A 的结果；
- 硬数据依赖：B 必须消费 A 产出的事实或约束。

当前 `dependsOn` 的执行语义很重：

- 上游任务未关闭，下游不能领取；
- 上游 `FAILED`，下游直接 `FAILED/UPSTREAM_FAILED`；
- 下游只能接收依赖结果摘要。

因此依赖边必须保守，误加边的影响大于漏掉普通展示顺序。

### 2.5 当前下游容量和契约缺口

1. `RoutePlan` 最多允许 4 个工作项，但 `agent_max_claims_per_agent` 默认是 3；四个同 Intent 工作项可能导致第 4 项永远无人领取。
2. `agent_max_work_items` 配置没有真正控制 `routing.py`；路由使用固定 `MAX_WORK_ITEMS = 4`。
3. 同一 Agent 在一个 Coordinator round 中最多领取一个任务；无依赖的同 Agent 工作项仍逐轮串行。
4. `RoutePlan.validate()` 检查 DAG 无环，但没有验证 `synthesisOrder` 是拓扑顺序。
5. 下游依赖结果只有 `answerBrief/reasonCode/citationRefs`，缺少明确的已支持事实、未解决事实和约束。
6. `PARTIAL` 上游会直接开放下游，但下游没有结构化方式判断自己所需事实是否真的可用。
7. 任意工作项缺参会暂停整个计划，包括与该工作项无依赖的分支。

### 2.6 当前评测与 Trace 缺口

1. `RoutingEvaluator` 直接调用 `classify_route()`，不传生产 settings 和 semantic classifier，因此只评测规则路径。
2. 222 条正式路由数据中，多工作项和依赖样本集中在固定的“先查校园事实，再做学业计划”模板。
3. 缺少 ORDER_ONLY、隐式依赖、同 Intent 多项、三四节点 DAG、局部依赖和 LLM 非法输出样本。
4. 当前 fusion shadow 会再次计算整份 RoutePlan，并可能把完整 `shadowRoutePlan` 写入 artifact metadata；新实现必须删除这套生产 shadow 机制，离线评测报告也不得保存未脱敏原文。

---

## 3. 目标架构

### 3.1 总体结构

```text
ContextBuilder.for_understanding()
  -> 输入边界
       -> raw_current_input：只供当前轮高风险硬检测
       -> planning_input：脱敏后实际参与规划的当前输入
  -> 当前轮高风险硬检测
  -> RuleDraftBuilder
       -> 规则候选 segments
       -> 显式硬依赖候选
       -> 复杂度特征
  -> ComplexityGate
       -> SIMPLE_FAST_PATH：直接使用规则候选
       -> COMPLEX：调用一次 Understanding LLM
  -> UnderstandingDecision V2 严格校验
  -> SegmentReconciler
       -> 原文锚定
       -> 去重/重叠/遗漏检查
       -> 每个 segment 进行固定的规则 + LLM + 可用 Embedding 意图融合
       -> 重新抽取 known/missing arguments
  -> DependencyGraphBuilder
       -> 规则硬依赖
       -> 经过阈值和白名单校验的 LLM HARD_DATA hint
       -> 环检测
       -> HARD_DATA 执行图
       -> HARD_DATA + ORDER_ONLY 展示图
       -> 稳定拓扑排序
  -> DeterministicRoutePlanBuilder
       -> 稳定 planId/workItemId
       -> RoutePlan V2
       -> RoutePlan.validate()
  -> Coordinator
```

### 3.2 强制模块边界

第一阶段必须新增一个文件，避免继续把所有逻辑堆入 `routing.py`：

```text
app/agents/routing.py
  - RoutePlan / WorkItem 公开契约
  - classify_route() 编排入口
  - 最终 RoutePlan Builder

app/services/intent_fusion.py
  - UnderstandingDecision V2
  - IntentCandidate / IntentFusion
  - LLM 中间模型

app/services/route_planning.py               # 新增
  - RuleRouteDraft
  - complexity assessment
  - segment anchoring/reconciliation
  - dependency graph construction
  - stable topological sort
  - planning diagnostics

app/services/intent_prompts.py
  - UnderstandingDecision V2 Prompt
```

依赖方向必须保持单向：

```text
autonomous.py
  -> routing.py
       -> route_planning.py
       -> intent_fusion.py
```

`route_planning.py` 不得反向导入 Coordinator、Specialist、RAG 或数据库服务。

### 3.3 输入术语和唯一数据流

全文统一使用以下名称：

- `raw_current_input`：原始用户输入，只供当前轮高风险硬检测和既有安全链路使用；不得写入默认 Trace。
- `planning_input`：`(board.model_input or board.user_input).strip()` 的结果，是规则草案、复杂度、LLM Prompt、sourceText 锚定、参数抽取和 ID 生成共同使用的唯一规划文本。
- `memory_context`：`ContextBuilder.for_understanding()` 生成的受限上下文，只用于判断 `CONTINUE/REFINE/CORRECTION` 和补全省略语义，不得成为当前工作项的 `sourceText`。

同一轮内不得分别用不同规范化版本进行规则分段和 LLM 锚定。第一阶段只接受 `sourceText` 在 `planning_input` 中的精确连续子串；不实现折叠空白、全半角标点替换或语义近似匹配。

---

## 4. 内部 UnderstandingDecision V2 契约

### 4.1 为什么升级内部版本

当前 `UnderstandingDecision schemaVersion=1` 的 `dependencyHints: list[list[int]]` 无法表达关系类型、方向置信度和拒绝原因。应升级内部协议为 V2；这不等于升级公开 `RoutePlan V2`。

### 4.2 强制 Pydantic 模型

```python
ContextRelation = Literal[
    "NEW_TOPIC",
    "CONTINUE",
    "REFINE",
    "CORRECTION",
    "AMBIGUOUS",
]

DependencyRelation = Literal[
    "HARD_DATA",
    "ORDER_ONLY",
]

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
    model_config = ConfigDict(extra="forbid")

    sourceText: str = Field(min_length=1, max_length=1000)
    intent: IntentType
    taskKind: TaskKind
    confidence: float = Field(ge=0.0, le=1.0)
    reasonCodes: list[SegmentReasonCode] = Field(default_factory=list, max_length=8)


class DependencyHint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sourceIndex: int = Field(ge=0)
    targetIndex: int = Field(ge=0)
    relation: DependencyRelation
    confidence: float = Field(ge=0.0, le=1.0)
    reasonCode: Literal[
        "TARGET_USES_SOURCE_RESULT",
        "TARGET_USES_SOURCE_FACT",
        "TARGET_USES_SOURCE_CONSTRAINT",
        "USER_REQUESTED_ORDER_ONLY",
    ]


class UnderstandingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schemaVersion: Literal[2]
    contextRelation: ContextRelation
    segments: list[IntentSegmentDecision] = Field(min_length=1, max_length=4)
    dependencyHints: list[DependencyHint] = Field(default_factory=list, max_length=6)
```

### 4.3 模型级校验

`UnderstandingDecision` 的 `model_validator` 必须完整检查：

1. Intent/TaskKind 组合只能是当前注册的五类组合。
2. segment 数量为 1..`ROUTE_PLAN_MAX_WORK_ITEMS`。
3. `sourceIndex/targetIndex` 必须引用有效、不同的 segment。
4. 同一 `(sourceIndex, targetIndex)` 最多有一个关系。
5. 同一无向节点对最多声明一种关系；`0 -> 1` 和 `1 -> 0` 视为同一节点对，不得同时出现。
6. `HARD_DATA` 不能直接由模型转成最终边，只表示候选。
7. 模型校验阶段不接受或生成 `objective/workItemId/dependsOn/knownArguments/missingArguments`。
8. `reasonCodes` 必须来自上面的固定枚举，未知字符串使整份 LLM 计划无效。
9. `HARD_DATA` 的 reasonCode 只能是前三种 `TARGET_USES_*`；`ORDER_ONLY` 的 reasonCode 只能是 `USER_REQUESTED_ORDER_ONLY`。
10. Schema 层不因多条合法边形成环而拒绝整份 segment 计划；构图层按确定性顺序拒绝闭环边并保留其余合法边。
11. `contextRelation` 为 `CONTINUE/REFINE/CORRECTION` 时，`memory_context` 必须至少包含非空 `current_goal.text`、`active_topics` 或最近一条用户消息；否则整份 LLM 计划无效并回退规则草案。
12. `AMBIGUOUS` 不得据此把低信息 CHAT 强行改成其他 Intent；只有 `CONTINUE/REFINE` 且满足第 11 条时才允许上下文覆盖。

### 4.4 原子升级策略

禁止同时支持 V1/V2 两种生产输出。

编码改造必须原子升级 Prompt、`response_model`、`schema_name` 和解析代码：

- 单元测试保留少量 V1 输出作为非法输入，验证会回退规则；
- 生产代码只解析 V2，不保留 V1/V2 双解析；
- 删除旧的 `list[list[int]]` 结构和未调用的旧 `_semantic_plan()`；
- `schema_name` 固定为 `understanding_decision_v2`，purpose 固定为 `understanding.semantic_plan`，repair purpose 固定为 `understanding.semantic_plan.repair1`。

---

## 5. LLM Prompt 必须明确的语义

修改 `app/services/intent_prompts.py::build_intent_prompt()`。

### 5.1 必须写入 system prompt 的规则

1. 只识别和规划，不回答用户问题。
2. `planning_input` 是唯一可切分的当前表达；历史只用于补全省略语义。
3. 每个 `sourceText` 必须来自 `<current_user>` 的连续原文，不得复制历史消息，不得改写原文。
4. 普通单一目标只输出一个 segment。
5. 只有多个可独立交付、需要不同执行过程、不同工具/证据，或存在真实数据依赖时才拆分。
6. 同一制度事实的多个属性通常不拆，例如“补考材料和截止日期”。
7. 同一 Intent 也可以拆分，但必须确实是独立目标，不得仅因为有两个问号或两个名词就拆。
8. `HARD_DATA`：target 不读取 source 的结果就无法可靠完成。
9. `ORDER_ONLY`：用户只指定说明顺序，target 不需要 source 的结果。
10. 两个任务可以独立完成时不要输出依赖关系，`dependencyHints=[]` 即表示独立。
11. “先 A 再 B”默认只证明顺序；只有“根据 A 的结果/日期/政策/条件做 B”等才是 HARD_DATA。
12. `sourceIndex` 是上游，`targetIndex` 是下游。
13. 不允许为了让结果看起来复杂而过度拆分。
14. segments 数量不得超过本次 Prompt 明确传入的运行时上限，且绝对不超过四个。
15. 当前高风险由外部硬规则控制；模型不能根据历史风险单独生成当前 RISK。
16. 不输出 objective、ID、参数、回答正文或自由推理文本。

### 5.2 必须提供的少量示例

Prompt 中加入短小、结构化的正反例，不加入长篇自然语言推理。

#### 示例 A：硬数据依赖

```text
输入：查一下奖学金截止日期，并根据这个日期安排申请计划
segments：CAMPUS、ACADEMIC
关系：0 -> 1 HARD_DATA
```

#### 示例 B：只有回答顺序

```text
输入：先告诉我调宿流程，再给我一些复习建议
segments：CAMPUS、ACADEMIC
关系：0 -> 1 ORDER_ONLY
```

#### 示例 C：同一事实不拆

```text
输入：补考需要哪些材料，截止日期是什么
segments：一个 CAMPUS/INSTITUTIONAL_FACT
```

#### 示例 D：跨域独立

```text
输入：我最近很焦虑，也想问补考需要什么材料
segments：MENTAL、CAMPUS
dependencyHints：[]
```

#### 示例 E：上下文延续

```text
历史目标：国家奖学金申请条件
当前：那研究生呢？
segments：一个 CAMPUS/INSTITUTIONAL_FACT
contextRelation：CONTINUE
sourceText：那研究生呢？
```

### 5.3 Token 与模型参数

当前 `agent_model_understanding_max_tokens=384` 对四段、多个关系和严格 JSON 偏紧。第一阶段固定：

- 默认和本次测试值提高到 768；
- 配置校验允许范围为 384..1024，但本次不得自行改成其他默认值；
- temperature 保持 0.1；
- `repair_attempts=1` 保留；
- Understanding 仍只调用一次主请求，Schema repair 算同一阶段受控重试；
- 不启用自由推理文本输出。

---

## 6. 规则草案与复杂度门控

### 6.1 规则草案仍然必要

规则草案承担：

- LLM 失败回退；
- 显式高风险以外的简单快速路径；
- 对 LLM 候选逐段提供独立分类信号；
- 提取高置信度显式硬依赖；
- 生成规划诊断和离线评测输入。

不要删除 `_classify_segment()`，但应停止让它单独垄断复杂请求的边界。

规则草案必须使用 span 作为身份，不使用会因排序变化的数组下标：

```python
@dataclass(frozen=True)
class RuleSegmentDraft:
    source_text: str
    start: int
    end: int
    rule_candidate: IntentCandidate

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
```

`source_text` 必须满足 `planning_input[start:end] == source_text`。规则依赖的端点必须引用 `action_spans` 中的 span；不得引用 objective、Intent 或数组位置。

### 6.2 新增 ComplexityAssessment

必须在 `route_planning.py` 定义：

```python
@dataclass(frozen=True)
class ComplexityAssessment:
    should_call_llm: bool
    score: int
    reason_codes: tuple[str, ...]
```

确定性复杂度特征固定如下，每个 reasonCode 每轮最多计分一次：

| 信号 | reasonCode | 固定分值 |
|---|---|---:|
| 两个以上动作或目标连接词 | `MULTI_ACTION_SIGNAL` | +2 |
| 两个以上 Intent 领域信号 | `CROSS_INTENT_SIGNAL` | +2 |
| “根据结果、基于、用查到的”等依赖指代 | `DEPENDENCY_REFERENCE_SIGNAL` | +2 |
| “另外、同时、还要、分别、比较后”等复合词 | `COMPOUND_CONNECTOR_SIGNAL` | +1 |
| 至少包含两个由 `，,、。！？；;\n` 分隔的非空子句，或 `planning_input` 长度至少 80 个 Unicode 字符 | `LONG_MULTI_CLAUSE_SIGNAL` | +1 |
| 当前输入包含“这个、那个、上述、前面”等省略指代 | `CONTEXT_REFERENCE_SIGNAL` | +2 |
| 规则草案已拆出多段 | `RULE_COMPOUND_SIGNAL` | +2 |

第一阶段固定 `score >= 2` 调用 LLM。动作词、连接词、领域词和依赖指代词必须在 `route_planning.py` 中声明为不可变常量元组，并由参数化测试锁定。

第一阶段初始词表固定为：

```python
ACTION_TERMS = (
    "查", "查询", "核对", "确认", "告诉", "解释", "比较", "分析",
    "制定", "安排", "规划", "申请", "办理", "评估", "建议", "总结",
)
COMPOUND_CONNECTORS = (
    "另外", "同时", "还要", "还想", "也想", "以及", "并且", "分别", "比较后",
)
DEPENDENCY_REFERENCES = (
    "根据结果", "根据这个结果", "根据日期", "根据条件", "根据要求",
    "基于结果", "用查到的", "确认后", "核验后",
)
CONTEXT_REFERENCES = ("这个", "那个", "上述", "前面", "之前的", "刚才的")
CLAUSE_SEPARATORS = r"[，,、。！？；;\n]+"
IGNORABLE_REMAINDER_TERMS = (
    "请", "请问", "麻烦", "麻烦你", "帮我", "可以吗", "谢谢", "你好", "您好",
    "另外", "同时", "还有", "还要", "也", "以及", "并且", "然后", "再", "先", "最后",
)
```

- `MULTI_ACTION_SIGNAL`：不同非空子句中均命中 ACTION_TERMS，或者 COMPOUND_CONNECTORS 两侧各命中至少一个 ACTION_TERMS；同一子句内同义动词重复只算一次。
- `CROSS_INTENT_SIGNAL`：`planning_input` 同时命中现有领域词表中的至少两个非 CHAT Intent；不得把 CHAT 当作第二个领域。
- `DEPENDENCY_REFERENCE_SIGNAL`、`COMPOUND_CONNECTOR_SIGNAL`、`CONTEXT_REFERENCE_SIGNAL`：分别命中对应固定词表任一项即计一次。
- `LONG_MULTI_CLAUSE_SIGNAL`：按 `CLAUSE_SEPARATORS` 切分后至少两个非空子句，或者 Python `len(planning_input) >= 80`。
- `RULE_COMPOUND_SIGNAL`：规则草案产生至少两个互不重叠的 `action_spans`。
- 动作 span 按子句生成，不按单个动词生成；同一制度事实的“查材料和截止日期”仍是一个 span。
- 覆盖检查把未覆盖片段中的 `IGNORABLE_REMAINDER_TERMS`、Unicode 空白和 `，,、。！？；;：:（）()"'` 删除；删除后仍有字符即为 `SEGMENT_COVERAGE_FAILED`。不得动态扩展白名单来迁就单个测试。

复杂度门控不得调用 Embedding；Embedding 只允许在 LLM segments 已接纳后作为最终分类融合信号使用，避免为了判断是否调用 LLM 额外增加一次外部依赖和延迟。

以下情况可直接走规则快速路径：

- 单一、短、明确的技术或通用聊天；
- 单一校园事实，目标和范围清楚；
- 单一学习建议或单一心理支持；
- 没有上下文指代、复合连接词、跨域信号和依赖信号；
- 规则候选有明确而非默认 CHAT 的高置信证据。

### 6.3 不再使用固定 0.92/0.94 作为“无需 LLM”的充分条件

当前规则置信度是硬编码常量，不是校准后的概率。复杂度门控不能继续使用：

```python
all(item[0] != CHAT and item[4] >= 0.90)
```

需要删除 `routing.py::_apply_intent_fusion()` 和 `IntentFusion.fuse()` 中两个重复的非 CHAT 高置信短路，改为根据复杂度和信号质量决定。

### 6.4 规则置信度本次冻结

本次只删除 `routing.py::_apply_intent_fusion()` 和 `IntentFusion.fuse()` 中重复的非 CHAT `>= 0.90` 短路，不修改 `_classify_segment()` 现有规则置信度常量。不要顺带调整 0.92、0.94、0.96、0.98 等数值。

规则置信度重新校准属于完成本改造、收集真实数据后的独立任务。以下仅为未来研究方向，本次禁止据此改数值：

- 多个专属关键词且组合明确：0.90..0.96；
- 单个宽泛关键词，例如“申请”“压力”“计划”：0.68..0.82；
- 未命中而默认 CHAT：0.55..0.65；
- 明确技术上下文：0.92..0.98；
- 当前轮风险：独立硬规则，不参加普通置信融合。

后续调整必须通过数据集重新校准，不能在本次改造中凭单个样例修改数值。

---

## 7. Segment 接纳与融合算法

### 7.1 禁止继续按规则数组下标套 LLM 结果

必须删除以下语义：

```text
for index, rule_segment in enumerate(rule_segments):
    llm_segment = llm_segments[index]
```

规则和 LLM 的分段数量可以不同，数组下标不是可靠的语义对齐方式。

### 7.2 原文锚定

为每个 LLM segment 计算当前输入中的字符 span：

```python
@dataclass(frozen=True)
class AnchoredSegment:
    source_text: str
    start: int
    end: int
    intent: IntentType
    task_kind: TaskKind
    confidence: float
    reason_codes: tuple[str, ...]
```

接纳规则：

1. `sourceText` 必须是 `planning_input` 的精确连续子串。
2. 第一阶段禁止折叠空白、替换全半角标点、模糊匹配或语义匹配。
3. 同一 `sourceText` 在 `planning_input` 出现多次且无法唯一定位时，拒绝整份 LLM 计划并回退规则草案。
4. segment spans 不得重叠。
5. 不得一个 segment 完全包含另一个 segment。
6. 去除标点后不得重复。
7. 历史文本即使语义相关，也不能成为当前 segment 的 `sourceText`。
8. `RuleRouteDraft` 必须保存规则识别出的 `action_spans`；每个 action span 必须与至少一个 LLM span 相交，否则记为 `SEGMENT_COVERAGE_FAILED` 并回退规则草案。
9. 未覆盖文本只能由固定白名单中的礼貌语、连接词、空白和标点组成。白名单在 `route_planning.py` 中定义并由测试锁定，不使用“明显动作目标”这类主观判断。

不要让模型直接输出字符下标。中文、emoji、代理对和模型计数差异会使下标不可靠；下标由程序通过 `sourceText` 映射生成。

### 7.3 选择 LLM 边界还是规则边界

固定策略：

1. `SIMPLE_FAST_PATH`：直接使用规则 segments，不调用 LLM。
2. `COMPLEX` 且 LLM V2 完整合法、原文锚定通过：以 LLM segments 作为候选边界。
3. LLM 非法、超时、漏掉明显规则工作项或 sourceText 无法锚定：整体回退规则边界。
4. 不把“半数合法”的 LLM segments 与规则 segments 临时拼接；第一版整份计划接纳或整份回退，避免重复和遗漏。
5. 任一 `rule_draft.action_spans` 未被覆盖时拒绝整份 LLM 计划。
6. 使用规则边界回退时，必须同时丢弃本次全部 LLM `dependencyHints`，只使用规则依赖候选。

### 7.4 每个接纳 segment 重新分类

对接纳后的每一个 segment 独立生成：

- rule candidate；
- LLM candidate；
- 可用时的 embedding candidate。

然后融合 Intent/TaskKind。禁止沿用 LLM 分段之前的规则 tuple。

固定决策顺序：

1. RISK 不在此阶段参与，已经由当前轮硬规则处理。
2. 规则与 LLM 一致：接受该组合，合并 reasonCodes。
3. 规则和 LLM 不一致、Embedding 与其中一方一致：按配置权重和真实置信度选择。
4. 当前输入是 `CONTINUE/REFINE`，规则为低信息 CHAT，而 LLM 给出高置信非 CHAT：允许上下文 LLM 覆盖。
5. 无法达到最低置信和最小分差：回退该 segment 的规则候选，并记录 `LOW_CONFIDENCE_FALLBACK`。
6. 任意结果必须重新检查 Intent/TaskKind 注册组合。

融合分数固定复用现有 `intent_rule_weight / intent_llm_weight / intent_embedding_weight / intent_min_confidence / intent_min_margin`；只删除 enabled/shadow 布尔开关，不删除这些数值配置。Embedding unavailable 时从候选集合移除并对剩余来源权重重新归一化；LLM candidate 仅取当前 AnchoredSegment 自带的分类，不跨 segment 复用。

### 7.5 重新计算目标和参数

确定最终 Intent/TaskKind 后，必须使用最终 segment 重新生成：

- `objective`；
- `knownArguments`；
- `missingArguments`；
- `priority`；
- `reasonCodes`。

不得复用融合前其他类型的参数抽取结果。

必须把现有参数逻辑提取为：

```python
def extract_work_item_arguments(
    intent: IntentType,
    task_kind: TaskKind,
    source_text: str,
) -> tuple[dict[str, str], tuple[MissingArgument, ...]]:
    ...
```

`MissingArgument` 仍必须经过 clarification handler 注册表清洗；LLM 不能直接生成缺参字段。

第一阶段参数抽取只读取当前工作项的 `source_text`，不得从其他 segment 或整段 `planning_input` 向当前项复制参数。上下文补全仍由现有受限 `memory_context` 交给 Specialist 使用，不在路由层把历史自由文本写入 `knownArguments`。

`objective` 固定通过 `build_work_item_objective(task_kind, source_text)` 生成：去除首尾 `，,；;。` 和空白后截取前 240 个字符；只有清洗结果为空时才使用当前 `_classify_segment()` 已有的 TaskKind fallback 文案。不得使用 LLM 文本生成 objective。

最终字段固定规则：

- RISK 工作项 priority=`CRITICAL`；
- 非 RISK 多工作项中 `synthesisOrder` 第一项 priority=`HIGH`，其余为 `NORMAL`；单一非 RISK 项为 `NORMAL`；
- RoutePlan confidence 取最终 workItems confidence 的最小值；
- RoutePlan reasonCodes 按 `synthesisOrder` 聚合工作项 reasonCodes 并稳定去重；工作项超过一个时追加 `COMPOUND_REQUEST`；`execution_graph` 至少有一条边时追加 `DEPENDENCY_DETECTED`，只有 ORDER_ONLY 时不追加；
- `primaryIntent` 固定等于 `workItems[0].intent`，`intents` 按最终 workItems 顺序稳定去重。

### 7.6 禁止接纳后合并

删除当前“相邻且 intent/taskKind 相同就自动合并”的逻辑。

接纳后的 LLM segments 不执行任何语义合并：

- source span 重复、重叠或包含：拒绝整份 LLM 计划；
- 同一事实被模型重复输出：拒绝整份 LLM 计划；
- 相同 Intent、TaskKind、Agent 或 objective：均不能成为合并理由；
- 依赖 Hint 必须先从 LLM 原索引映射到 `AnchoredSegment(start, end)` 标识，再进行排序，禁止排序后继续使用原数组下标。

规则草案内部也不得通过 `classified[:limit]` 静默截断。若确定检测到的独立交付目标超过运行时上限，返回一个确定性的 `CHAT/GENERAL_CHAT` 工作项，objective 为“请用户将请求拆分为不超过 N 个目标”，reasonCode 为 `WORK_ITEM_LIMIT_EXCEEDED`。该项由 GeneralChatAgent 直接解释容量限制并请用户拆分，不调用 Campus、Academic、Mental Specialist 或 RAG 工具。

---

## 8. 依赖关系设计与确定性构图

### 8.1 两种内部关系

| 关系 | 含义 | 是否生成 dependsOn | 是否影响 synthesisOrder |
|---|---|---:|---:|
| `HARD_DATA` | 下游必须读取上游结果才能可靠完成 | 是 | 是 |
| `ORDER_ONLY` | 只要求回答或展示顺序 | 否 | 是 |

没有候选关系即表示独立，独立节点按原文 span 保持稳定顺序。公开 `RoutePlan V2` 仍只保存 `dependsOn` 和 `synthesisOrder`，不增加 relation 字段。

内部类型必须固定为：

```python
DependencyRelation = Literal["HARD_DATA", "ORDER_ONLY"]
DependencyReasonCode = Literal[
    "TARGET_USES_SOURCE_RESULT",
    "TARGET_USES_SOURCE_FACT",
    "TARGET_USES_SOURCE_CONSTRAINT",
    "USER_REQUESTED_ORDER_ONLY",
]
```

### 8.2 规则硬依赖

确定性规则只识别高精度信号，例如：

- “根据 A 的结果/日期/条件/要求做 B”；
- “用查到的 A 来安排 B”；
- “确认是否符合 A 后，再制定 B”；
- “把前面核验的信息整理成 B”。

单独出现以下词不能建立硬依赖：

- 先、再、然后、之后；
- 同时、另外、还有；
- 第一、第二。

这些只能产生 `ORDER_ONLY` 或原文顺序提示。

### 8.3 合并规则和 LLM 依赖候选

必须新增以下强类型结构：

```python
@dataclass(frozen=True)
class DependencyCandidate:
    source_key: tuple[int, int]  # AnchoredSegment(start, end)
    target_key: tuple[int, int]
    relation: DependencyRelation
    confidence: float
    source: Literal["RULE", "LLM"]
    reason_code: DependencyReasonCode
```

构图顺序：

1. LLM 原始索引先映射为 `AnchoredSegment(start, end)`；无法映射使整份 LLM 计划无效。
2. 规则依赖端点必须映射到唯一覆盖对应 action span 的最终 segment。无法唯一映射或两个端点映射到同一 segment 时，说明 LLM 边界吞并了规则识别的依赖任务，拒绝整份 LLM 计划并使用规则草案。
3. 完成端点映射后添加通过验证的规则 `HARD_DATA` 边。
4. 将 LLM `HARD_DATA` 按 `confidence` 降序、source span、target span 排序。
5. 只接纳 `confidence >= 0.82` 的 LLM 硬边；低于阈值的边忽略并记录 `DEPENDENCY_LOW_CONFIDENCE_REJECTED`。
6. 每加入一条硬边立即检查是否形成环；形成环则拒绝该条 LLM 边并记录 `DEPENDENCY_CYCLE_REJECTED`，其余 segment 计划仍可接纳。
7. 规则硬边出现环说明实现错误，必须抛出不变量异常并使测试失败，不得静默删边或回退。
8. `execution_graph` 只包含接纳的 `HARD_DATA` 边，且不得做传递约简。
9. `presentation_graph` 先复制 `execution_graph`，再按规则优先、LLM confidence 降序、span 升序加入 `ORDER_ONLY`。
10. 同一无向节点对存在 `HARD_DATA` 时忽略 `ORDER_ONLY`。ORDER_ONLY 与硬边方向冲突或加入后形成环时拒绝顺序边，记录 `ORDER_CONFLICT_REJECTED` 或 `ORDER_CYCLE_REJECTED`。
11. `dependsOn` 只从 `execution_graph` 生成；`synthesisOrder` 从 `presentation_graph` 生成。

### 8.4 稳定拓扑排序

对 `presentation_graph` 的最多四个节点实现确定性 Kahn 拓扑排序：

- 入度为 0 的节点按原文 span 起点排序；
- span 相同再按锚定前的 LLM segment index 排序；该索引只用于稳定 tie-break，不用于依赖身份或 ID；
- 输出顺序同时用于 `workItems` 和 `synthesisOrder`。

这样保证：

- `primaryIntent == workItems[0].intent`；
- `intents` 按最终工作项顺序去重；
- 所有依赖的上游都在下游之前；
- 无依赖任务保持用户表达顺序。

### 8.5 RoutePlan.validate() 必须增强

除现有校验外，增加：

```text
tuple(item.workItemId for item in workItems) == synthesisOrder

对于每个 workItem.dependsOn 中的 parent：
position(parent) < position(workItem)
```

即 `synthesisOrder` 必须是拓扑顺序。

`workItems` 数组顺序必须与 `synthesisOrder` 完全一致；第一阶段不允许数组顺序与展示顺序分离。虽然公开 RoutePlan 不保存 ORDER_ONLY 边，但 Builder 必须先按 presentation_graph 排序，再生成 workItems 和 synthesisOrder；反序列化后的 `RoutePlan.validate()` 只能验证公开 HARD_DATA 拓扑关系，ORDER_ONLY 的正确性由 `route_planning` 单元测试覆盖。

继续保留：

- ID 唯一；
- 引用必须位于 plan 内；
- 禁止自依赖；
- 禁止环；
- RISK 只能有一个工作项。

### 8.6 上游失败与 PARTIAL

第一阶段保持当前硬行为：

- 上游 `FAILED` -> 下游 `FAILED/UPSTREAM_FAILED`；
- 上游 `COMPLETED` -> 正常开放下游；
- 上游 `PARTIAL` -> 保持当前 Coordinator 的开放行为；下游收到现有 `status/reasonCode/answerBrief/citationRefs` 后，Prompt 必须明确把该结果当作不完整摘要，不得把缺失事实推断为已核验。第一阶段不新增结构化缺口字段。

第二阶段再补充 `UPSTREAM_INSUFFICIENT`，详见第 12 节。不要在第一阶段同时引入复杂的工作项 reopen 和动态重规划。

---

## 9. 稳定 ID 与计划身份

### 9.1 当前问题

当前 `workItemId` 依赖 `objective`。旧实现一旦接纳自由生成的 objective，或未来 objective 生成规则变化，就会导致 ID 不稳定；新规则彻底移除这一依赖。

### 9.2 新 ID 规则

必须使用以下唯一算法，不得把 LLM confidence、reasonCodes、objective 或最近消息正文加入 ID：

```text
planner_version = "semantic-route-v2"

context_anchor = JSON(
  {
    "currentGoal": current_goal.text 或 "",
    "activeTopics": active_topics 转成字符串后去空白、去重、Unicode 码点升序,
  },
  ensure_ascii=False,
  sort_keys=True,
  separators=(",", ":"),
)

plan_seed = UTF8(
  planner_version + "\x1f"
  + planning_input + "\x1f"
  + context_anchor
)

planId = "plan_" + sha256(plan_seed).hexdigest()[:12]

work_item_seed = UTF8(
  planId + "\x1f"
  + str(start) + "\x1f"
  + str(end) + "\x1f"
  + intent.value + "\x1f"
  + taskKind.value
)

workItemId = "wi_" + sha256(work_item_seed).hexdigest()[:12]
```

约束：

- 不使用自由文本 objective 生成 ID；
- `current_goal` 是对象时只读取其 `text`；不存在时使用空字符串；
- `contextRelation=NEW_TOPIC` 时 context anchor 固定为空目标和空 topics，避免无关历史改变 ID；
- `start/end` 使用 Python 字符串切片语义，`start` 包含、`end` 不包含；
- 澄清恢复继续保存并恢复原 RoutePlan，不重新生成 ID；
- ID 只用于计划内身份，不作为跨用户授权依据。

---

## 10. 配置改造

### 10.1 单一生产路径配置

在 `app/core/config.py` 和 `.env.example` 增加：

```text
INTENT_DECOMPOSITION_COMPLEXITY_THRESHOLD=2
INTENT_DEPENDENCY_MIN_CONFIDENCE=0.82
INTENT_SEGMENT_SOURCE_MAX_CHARS=1000
INTENT_PLANNING_DIAGNOSTICS_ENABLED=true
```

语义：

- 新规划器始终是唯一生产编排，不增加 `intent_decomposition_enabled` 和 `intent_decomposition_shadow_mode`。
- 删除 `intent_fusion_enabled`、`intent_fusion_shadow_mode` 及 `UnderstandingAgent.act()` 中反转开关、再次调用 `classify_route()` 的代码；同步更新使用这些配置的测试和 `.env.example`。
- 复杂度达到阈值时固定调用一次 `_semantic_route()`。LLM 计划接纳后，最终 segment 固定执行规则 + LLM + 可用 Embedding 的分类融合；模型或 Embedding 不可用按本文的确定性规则降级，不由布尔开关产生另一种算法。
- 生产回滚使用版本控制或部署版本回滚，不在代码里保留旧路由分支。

### 10.2 现有配置调整

1. `agent_model_understanding_max_tokens` 默认从 384 调到 768。
2. `agent_max_claims_per_agent` 默认从 3 调到 4。
3. Settings validator 要求：
   - `1 <= agent_max_work_items <= ROUTE_PLAN_MAX_WORK_ITEMS`；
   - `agent_max_claims_per_agent >= agent_max_work_items`；
   - decomposition 阈值固定默认值为 2 且必须为正整数；
   - dependency confidence 位于 `[0, 1]`。
   - `agent_model_understanding_max_tokens` 位于 `[384, 1024]`；
   - `agent_max_rounds >= 12` 且不超过 `agent_max_rounds_hard_limit`。
4. `routing.py` 构建计划时读取 `settings.agent_max_work_items`，不再始终切固定四项。
5. `RoutePlan.validate()` 仍使用协议硬上限 4，不能受不同实例配置影响。

### 10.3 `.env` 和发布策略

`.env.example` 只增加固定参数，不增加 enabled/shadow 开关：

```text
INTENT_DECOMPOSITION_COMPLEXITY_THRESHOLD=2
INTENT_DEPENDENCY_MIN_CONFIDENCE=0.82
INTENT_SEGMENT_SOURCE_MAX_CHARS=1000
INTENT_PLANNING_DIAGNOSTICS_ENABLED=true
AGENT_MODEL_UNDERSTANDING_MAX_TOKENS=768
AGENT_MAX_CLAIMS_PER_AGENT=4
```

发布前必须先完成阶段 0 的基线报告和阶段 4 的新实现报告。通过门禁后部署新的单一实现；若出现回归，回滚整个部署版本。不得通过环境变量在生产中切回保留于同一代码版本的旧算法。

---

## 11. Coordinator、Specialist 和 Response 必须适配的内容

### 11.1 Coordinator 容量

修改 `app/agents/coordinator.py`：

1. 确保同一 Agent 在整个 plan 中可以处理最多 `agent_max_work_items` 个工作项。
2. 保留“同一 Agent 在一个 round 最多领取一个任务”的当前确定性行为，第一阶段不引入线程并发。
3. 添加四个同 Intent 工作项的测试，证明所有任务最终都能关闭，不触发 `BUDGET_EXHAUSTED`。
4. `max_rounds` 必须覆盖最坏情况：Understanding + Safety + 四个同 Agent 串行任务 + Response + Review/Revision。
5. 若配置不能满足最坏路径，Settings 启动校验应拒绝，而不是运行时静默耗尽。

最低轮次预算固定为 12，不得在本次改造中降低。

### 11.2 同 Agent 多工作项

不要重新加入“相同 Intent 自动合并”。例如两个校园目标可以分别调用 Campus Agent，只要它们：

- 需要不同证据；
- 具有独立交付结果；
- 不是同一政策事实的多个属性。

### 11.3 Specialist 依赖 Prompt

修改 `SpecialistAgent._run_loop()` 的 system prompt：

- 只处理当前工作项；
- `dependencyResults` 是受控的上游结果摘要；
- `PARTIAL` 上游中的缺失事实不能当作已知事实；
- 必须区分用户已提供约束、上游已核验事实和未核验假设；
- 依赖结果不能扩大工具权限；
- 不得重新回答上游工作项，除非下游目标需要引用它。

### 11.4 ResponseAgent 依赖感知

修改 Response system prompt：

1. 按 `synthesisOrder` 覆盖所有工作项。
2. 不重复展开已经被下游吸收的上游内容。
3. 对 `PARTIAL/FAILED` 明确说明边界。
4. 下游因 `UPSTREAM_FAILED` 未执行时，不得写成已完成。
5. 独立分支必须分别覆盖，不能只回答 primaryIntent。
6. 多个工作项引用同一 evidenceId 时去重展示。

### 11.5 心理报告 Intent 修复

当前只要 `intents` 包含 MENTAL 就可能创建报告，但报告 `intent` 使用 `primary_intent`。跨域分段增多后可能出现报告 intent 写成 CAMPUS/ACADEMIC。

修改 `MindBridgeAgentHarness._create_report()`：

- 高风险使用 `RISK`；
- 非高风险但包含 MENTAL 评估时使用 `MENTAL`；
- 不再无条件写 `agent_run.primary_intent`。

增加 `CAMPUS + MENTAL` 和 `ACADEMIC + MENTAL` 测试。

### 11.6 Skill 匹配稳定性

当前 Skill 匹配主要使用 `objective`。新方案虽然由程序确定性生成 objective，但只依赖 objective 仍可能丢失原始目标和参数。

只修改 `app/agents/autonomous.py` 中调用 `SkillManager.match()` 的输入，固定组合为：

```text
sourceText + objective + knownArguments
```

Intent、TaskKind 和 Agent 名仍作为确定性过滤条件，objective 只作语义辅助。

---

## 12. 后续独立项目：结构化依赖交接（本次禁止实现）

本节仅记录后续方向，不是本次编码任务的需求或验收项。编码 AI 读到本节时不得创建、修改或测试以下结构。未来独立任务再增强 `specialist_result`，避免把硬依赖完全建立在自由文本 `answerBrief` 上。

### 12.1 后续候选 handoff 结构

未来独立任务必须先重新设计并评审 `specialist_result` 协议；本次不得修改其 schemaVersion。候选形态如下，仅供未来讨论：

```json
{
  "dependencyHandoff": {
    "supportedFacts": [
      {
        "claim": "申请截止日期为……",
        "evidenceIds": ["ev_..."],
        "confidence": 0.91
      }
    ],
    "constraints": ["适用于研究生"],
    "unresolvedNeeds": ["未核验学院级补充要求"],
    "conflicts": []
  }
}
```

约束：

- Campus/Mental/需 RAG 的 Academic 工作项：`supportedFacts` 只能来自 RAG `assessment.supportedClaims`，evidenceIds 必须真实存在。
- 纯 STUDY_PLAN：没有知识事实时 `supportedFacts=[]`，用户约束放入 `constraints`。
- 不允许 Specialist 模型生成不存在的 evidenceId。
- Trace 只记录 claim hash、数量和 evidenceIds，不记录完整敏感正文。

### 12.2 ContextBuilder 适配

`for_specialist()` 的 `dependencyResults` 增加：

- `dependencyHandoff.supportedFacts`
- `constraints`
- `unresolvedNeeds`
- `conflicts`

继续限制字符预算；只传当前工作项直接依赖的上游，不传整个 plan 的所有结果。

### 12.3 PARTIAL 策略

第二阶段新增：

- 上游 PARTIAL 且仍有 supportedFacts：下游可以执行，但结果最多为 PARTIAL，除非下游完全不依赖缺失项。
- 上游 PARTIAL 且没有可用 supportedFacts、只有 unresolvedNeeds：下游确定性返回 `PARTIAL/UPSTREAM_INSUFFICIENT`。
- 上游 CONFLICT：下游不得自行选边，返回 `PARTIAL/UPSTREAM_CONFLICT` 或生成条件化方案。

第一版不要尝试让 Coordinator 动态重新规划 DAG。

---

## 13. 澄清恢复影响与边界

### 13.1 第一阶段保持全局暂停

当前任意 workItem 出现合法缺参，Coordinator 会在创建 Specialist task 前暂停整个 plan。第一阶段继续保持，理由是：

- 现有 resumeContext 已能保留完整 RoutePlan；
- 不需要持久化已执行的独立分支；
- 不引入跨轮部分 DAG 恢复和结果失效问题。

### 13.2 必须验证

LLM 分段后：

1. `knownArguments/missingArguments` 必须由最终 segment 重新计算。
2. ClarificationPolicy 仍只选择注册字段。
3. 有多个缺参工作项时，按下游数量、优先级和稳定顺序每轮只问一个字段。
4. 恢复后保留原 `planId/workItemId/dependsOn/synthesisOrder`。
5. 澄清期间出现当前高风险时，仍打断原计划。
6. LLM 不参与澄清恢复时的重新路由；恢复使用原计划。

### 13.3 后续独立优化（本次禁止实现）

“独立分支先执行、仅阻塞分支等待澄清”必须作为独立项目处理，届时需要：

- 持久化已完成 workItem 结果；
- 恢复部分 DAG；
- 处理知识版本和结果过期；
- 避免跨轮重复工具调用；
- 重新定义最终 Response 时机。

不纳入本次第一阶段。

---

## 14. Trace、指标和隐私

### 14.1 规划诊断模型

必须内部增加固定枚举和诊断对象：

```python
PlanSource = Literal["RULE_FAST", "LLM_ACCEPTED", "RULE_FALLBACK"]
FallbackReason = Literal[
    "",
    "MODEL_UNAVAILABLE",
    "MODEL_TIMEOUT",
    "STRUCTURED_OUTPUT_INVALID",
    "SEGMENT_ANCHOR_FAILED",
    "SEGMENT_COVERAGE_FAILED",
    "SEGMENT_OVERLAP",
]

@dataclass(frozen=True)
class PlanningDiagnostics:
    plan_source: PlanSource
    llm_invoked: bool
    complexity_score: int
    complexity_reasons: tuple[str, ...]
    rule_segment_count: int
    llm_segment_count: int
    accepted_segment_count: int
    dependency_hint_count: int
    accepted_dependency_count: int
    rejected_reason_codes: tuple[str, ...]
    fallback_reason: FallbackReason
```

`RouteDecision` 固定增加 `diagnostics: PlanningDiagnostics | None = None`；`RouteDecision.as_payload()` 仍只返回公开 RoutePlan V2。不得另外创建第二种 classify_route 返回类型，也不得把 diagnostics 放入 RoutePlan payload。

预期降级异常必须定义为独立类型，不复用通用 `ValueError`：

```python
class SemanticPlanningFallbackError(RuntimeError): ...
class SemanticPlannerUnavailable(SemanticPlanningFallbackError): ...
class SemanticPlannerTimeout(SemanticPlanningFallbackError): ...
class SemanticStructuredOutputInvalid(SemanticPlanningFallbackError): ...
class SegmentAnchorFailed(SemanticPlanningFallbackError): ...
class SegmentCoverageFailed(SemanticPlanningFallbackError): ...
class SegmentOverlapFailed(SemanticPlanningFallbackError): ...

EXPECTED_SEMANTIC_PLANNING_ERRORS = (SemanticPlanningFallbackError,)
```

`StructuredCompletionError`、JSON/Pydantic 校验异常只能在 `_semantic_route()` 或 V2 解析边界转换成相应的上述异常，不能让外层捕获所有 `ValueError`。

`WORK_ITEM_LIMIT_EXCEEDED` 是规则规划结果，不是 LLM fallback reason：此时 `plan_source=RULE_FAST`、`fallback_reason=""`，公开 RoutePlan reasonCodes 包含该码。`llm_invoked=False`。

fallback reason 归一化固定如下：

- semantic classifier 为 `None`、模型未配置或 provider 明确不可用：`MODEL_UNAVAILABLE`；
- 超时异常：`MODEL_TIMEOUT`；
- JSON、Pydantic Schema、V1 输出、未知字段、非法索引或关系：`STRUCTURED_OUTPUT_INVALID`；
- sourceText 不存在或不唯一：`SEGMENT_ANCHOR_FAILED`；
- action span/非白名单剩余文本未覆盖：`SEGMENT_COVERAGE_FAILED`；
- segment 重复、重叠或包含：`SEGMENT_OVERLAP`。

同一次失败同时满足多项时使用上述从上到下第一个已经捕获的阶段错误；不得根据异常消息自由生成 fallback reason。

所有新增内部 reason code 必须集中定义在 `route_planning.py` 的 Literal/Enum 中，第一阶段固定包括：

```text
MULTI_ACTION_SIGNAL
CROSS_INTENT_SIGNAL
DEPENDENCY_REFERENCE_SIGNAL
COMPOUND_CONNECTOR_SIGNAL
LONG_MULTI_CLAUSE_SIGNAL
CONTEXT_REFERENCE_SIGNAL
RULE_COMPOUND_SIGNAL
LOW_CONFIDENCE_FALLBACK
SEGMENT_ANCHOR_FAILED
SEGMENT_COVERAGE_FAILED
SEGMENT_OVERLAP
WORK_ITEM_LIMIT_EXCEEDED
DEPENDENCY_LOW_CONFIDENCE_REJECTED
DEPENDENCY_CYCLE_REJECTED
ORDER_CONFLICT_REJECTED
ORDER_CYCLE_REJECTED
```

写入公开 RoutePlan 的 reasonCode 必须先映射到 `app/core/enums.py::RouteReasonCode` 白名单；只用于诊断的拒绝码不得进入公开 RoutePlan payload。需要公开的新码只能通过扩展该枚举加入，禁止散落自由字符串。

本次唯一必须新增的公开 `RouteReasonCode` 是 `WORK_ITEM_LIMIT_EXCEEDED`。复杂度 reason、锚定失败、依赖边拒绝和 fallback reason 均只写 PlanningDiagnostics，不加入 WorkItem 或 RoutePlan reasonCodes。现有 `SEMANTIC_FALLBACK` 在发生 RULE_FALLBACK 时继续加入公开 plan reasonCodes，用于保持现有消费方兼容。

### 14.2 Trace 只保存脱敏摘要

删除现有完整 `shadowRoutePlan` metadata 和所有 shadow diff 字段。`route_plan` artifact metadata 只允许保存：

- planSource
- fallbackReason
- complexityScore
- complexityReasons
- ruleSegmentCount
- llmSegmentCount
- acceptedSegmentCount
- dependencyHintCount
- acceptedDependencyCount
- accepted/rejected reason codes

禁止保存到默认 Trace：

- 完整 `sourceText`
- 完整 objective
- 完整历史上下文
- LLM 自由推理
- 未脱敏 knownArguments

### 14.3 Turn metrics

继续使用 purpose：

```text
understanding.semantic_plan
understanding.semantic_plan.repair1
```

指标必须包含：

- Understanding 调用率；
- 调用耗时 p50/p95；
- Schema repair 率；
- fallback 率；
- 平均 segment 数；
- 平均依赖边数；
- 整轮 token 和延迟增量。

---

## 15. 正式评测改造

### 15.1 拆成两套评测

#### A. Deterministic routing contract suite

直接测试纯代码：

- 风险抢占；
- 规则快速路径；
- LLM 不可用回退；
- sourceText 锚定；
- Schema 校验；
- 稳定 ID；
- 参数重新抽取；
- DAG、拓扑顺序和循环拒绝；
- RoutePlan V2 严格契约。

该套测试不访问真实模型和网络，必须稳定进入 CI。

#### B. Semantic planning model suite

通过 `UnderstandingAgent._semantic_route()` 或专用 model adapter 调用实际配置模型，评测：

- 分段准确率；
- 过度拆分率；
- 漏拆率；
- Intent/TaskKind；
- 依赖方向；
- HARD_DATA 与 ORDER_ONLY 区分；
- 上下文延续；
- Schema 成功率；
- 延迟和 token。

不能继续用当前不传 semantic classifier 的 `RoutingEvaluator` 代表生产结果。

### 15.2 固定新增语义评测契约

不得立即修改现有 `routing-v1.jsonl` 和 `ExpectedRoute` 契约。必须新增：

- `app/evaluation/datasets/routing-semantic-v2.jsonl`；
- `SemanticPlanningExpected` 和 `SemanticPlanningCase` 独立契约；
- `app/evaluation/evaluators/semantic_planning.py`；
- `tests/evaluation/test_semantic_planning_evaluator.py`。

固定契约为：

```python
class SemanticPlanningExpected(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    context_relation: ContextRelation = Field(alias="contextRelation")
    work_item_count: int = Field(alias="workItemCount", ge=1, le=4)
    work_item_intents: list[IntentType] = Field(alias="workItemIntents")
    work_item_task_kinds: list[TaskKind] = Field(alias="workItemTaskKinds")
    source_text_fragments: list[list[str]] = Field(alias="sourceTextFragments")
    hard_data_edges: list[tuple[int, int]] = Field(default_factory=list, alias="hardDataEdges")
    order_only_edges: list[tuple[int, int]] = Field(default_factory=list, alias="orderOnlyEdges")

class SemanticPlanningCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    messages: list[EvaluationMessage] = Field(min_length=1)
    expected: SemanticPlanningExpected
    tags: list[str] = Field(default_factory=list)
```

`sourceTextFragments[i]` 中每个字符串都必须出现在第 i 个实际 sourceText 中，用于允许同义分段边界但禁止漏目标；各列表不能为空。契约 validator 必须校验四个按工作项排列的数组长度一致、边索引有效、HARD_DATA 与 ORDER_ONLY 节点对不重叠。数据加载器新增 `load_semantic_planning_cases()`，不得复用 `RoutingCase` 强行塞入可选字段。

真实模型 suite 只在显式设置 `RUN_SEMANTIC_MODEL_EVAL=1` 时运行；默认 `pytest` 和 CI 不访问模型或网络。没有可用模型时必须报告“未运行真实模型门禁”，不得伪造通过结果，也不得因此跳过确定性测试。

每份真实模型评测报告必须记录 provider、model、模型摘要或 digest、关键配置、数据集版本、Git commit 和运行时间；样本明细只保存脱敏 hash、expected/actual 结构摘要和错误码。

### 15.3 必须新增的数据切片

每个切片至少 10 条；安全、HARD_DATA、ORDER_ONLY 和非法输出切片每类至少 20 条：

1. 单任务不过度拆分；
2. 无“先/再”的双任务；
3. 三任务；
4. 四任务；
5. 同 Intent 多工作项；
6. 跨 Intent 独立任务；
7. 显式 HARD_DATA；
8. 隐式 HARD_DATA；
9. ORDER_ONLY；
10. 局部依赖 DAG，例如 `A -> C`，B 独立；
11. 多父节点，例如 `A -> C`、`B -> C`；
12. LLM 循环依赖；
13. sourceText 不在当前输入；
14. 重复/重叠 segment；
15. 上下文 CONTINUE/REFINE/CORRECTION；
16. 心理 + 校园；
17. 心理 + 学业；
18. 高风险混入复杂请求；
19. 同一政策事实多个属性不拆；
20. 技术文本中的“压力测试”等安全边界。

### 15.4 核心指标和门禁

固定上线门禁：

| 指标 | 门禁 |
|---|---:|
| 当前轮高风险漏检 | 0 |
| RoutePlan schema 合法率 | 100% |
| 循环依赖接纳数 | 0 |
| sourceText 越界接纳数 | 0 |
| 单任务不过度拆分准确率 | >= 98% |
| 复杂请求 workItem count 准确率 | >= 92% |
| workItem Intent/TaskKind exact match | >= 95% |
| HARD_DATA 依赖方向准确率 | >= 95% |
| ORDER_ONLY 误转硬依赖率 | <= 2% |
| 模型/Schema 失败规则回退成功率 | 100% |
| 四个同 Agent 工作项完成率 | 100% |
| 澄清恢复身份保持率 | 100% |

离线报告必须将阶段 0 的基线报告与新实现报告比较：

- workItem count、Intent/TaskKind 和依赖结构差异；
- 最终回答覆盖率；
- Specialist/RAG 调用数增量；
- 整轮 p95 延迟；
- token 成本；
- BUDGET_EXHAUSTED 数量。

该比较由评测工具读取两份报告完成，不得在生产请求中重新运行旧路由算法。

---

## 16. 逐文件改造清单

### 16.1 第一阶段必须修改

| 文件 | 修改内容 |
|---|---|
| `app/services/intent_fusion.py` | 升级 UnderstandingDecision V2；新增 DependencyHint；去除重复的高置信非 CHAT 短路；保留三路分类融合 |
| `app/services/intent_prompts.py` | 改写分段、同事实不拆、HARD_DATA/ORDER_ONLY、无关系即独立、上下文和 sourceText 约束 Prompt |
| `app/services/route_planning.py` | 新增规则草案、复杂度门控、精确原文锚定、整份 segment 接纳、执行图/展示图、稳定拓扑排序和诊断；禁止传递约简 |
| `app/agents/routing.py` | 重构 classify_route 编排；使用最终 segment 重算参数；稳定 ID；读取 agent_max_work_items；增强 RoutePlan 拓扑校验 |
| `app/agents/autonomous.py` | Understanding 调用 V2；新 purpose；删除现有 shadow 双计划；从 RouteDecision 读取脱敏 diagnostics；Specialist/Response 依赖 Prompt 适配 |
| `app/core/config.py` | 删除 fusion enabled/shadow 配置；新增固定阈值和校验；Understanding token；claims/workItems/rounds 容量一致性 |
| `app/core/enums.py` | 只新增公开 `RouteReasonCode.WORK_ITEM_LIMIT_EXCEEDED`；其他规划诊断码留在 route_planning 内部 |
| `.env.example` | 删除 fusion enabled/shadow 配置；补充阈值、Understanding max tokens 和 claims 配置 |
| `app/agents/coordinator.py` | 容量适配；四个同 Agent 工作项可完成；保持依赖门禁和失败传播 |
| `app/agents/harness.py` | 心理报告 intent 修复 |
| `app/agents/autonomous.py` 的 Skill 调用点 | 匹配文本固定使用 `sourceText + objective + knownArguments`；不修改通用 SkillManager 排名算法 |
| `app/services/trace.py` | 规划诊断脱敏；删除完整 shadow RoutePlan 和 shadow diff metadata |
| `app/services/turn_execution.py` | route 摘要保持 V2 兼容，固定增加 planSource/segmentCount 等非敏感诊断 |
| `app/evaluation/contracts.py` | 新增独立 `SemanticPlanningExpected`，不修改 routing-v1 契约 |
| `app/evaluation/evaluators/semantic_planning.py` | 新增生产模型语义规划评测器；现有 routing evaluator 保持确定性评测职责 |
| `app/evaluation/datasets/routing-semantic-v2.jsonl` | 新增复杂分段、依赖和反过度拆分数据集 |
| `README.md` | 更新 UnderstandingAgent、复杂请求和依赖语义说明 |

### 16.2 第一阶段必须新增或重点修改的测试

| 测试文件 | 覆盖内容 |
|---|---|
| `tests/test_route_planning.py` | 新模块纯函数：复杂度、锚定、执行图/展示图、稳定拓扑、禁止传递约简 |
| `tests/test_intent_fusion.py` | V2 Schema、分类融合、上下文覆盖、非法输出回退 |
| `tests/test_route_plan_v2.py` | LLM 真分段、参数重算、稳定 ID、拓扑顺序、同 Intent 多项 |
| `tests/test_understanding_single_path.py` | 简单请求 0 次模型调用；复杂请求 1 次主调用；最多 1 次 repair；不存在第二份 RoutePlan 和 shadow metadata |
| `tests/test_coordinator_work_items_v2.py` | 四个同 Agent、局部 DAG、多父节点、上游失败 |
| `tests/test_specialist_agents_v2.py` | PARTIAL 依赖 Prompt、依赖结果限制、权限不扩大 |
| `tests/test_clarification_study_plan_flow.py` | 多工作项缺参、恢复保持依赖与 ID |
| `tests/test_trace_privacy.py` | 不存在 shadow metadata；diagnostics 不记录 sourceText/objective/参数 |
| `tests/test_routing_safety_boundaries.py` | 复杂请求内 RISK 抢占、历史风险不误触发 |
| `tests/evaluation/test_semantic_planning_evaluator.py` | 新模型规划报告、切片指标、显式环境变量门禁和离线默认行为 |

### 16.3 后续独立项目可能修改的文件（本次禁止修改）

| 文件 | 修改内容 |
|---|---|
| `app/agents/autonomous.py` | specialist_result 增加 dependencyHandoff；从 RAG assessment 构建受证据约束的 supportedFacts |
| `app/services/context_builder.py` | 向直接下游传递受限 handoff |
| `app/agents/coordinator.py` | PARTIAL/CONFLICT 的确定性下游策略 |
| `app/services/trace.py` | handoff 脱敏摘要 |
| `app/evaluation/runtime/action_resolution.py` | 新 reasonCode 聚合 |
| Specialist/Response 测试 | UPSTREAM_INSUFFICIENT、UPSTREAM_CONFLICT、证据 ID 白名单 |

---

## 17. 强制实现步骤与每步退出标准

### 阶段 0：冻结基线

动作：

1. 执行 `git status --short` 和 `git log -1 --oneline`，记录当前 commit。
2. 审查全部未提交文件，按知识入库、路由、其他关注点分组，检查是否包含凭据、临时输出或不应提交的文件。
3. 对每组现有代码修改运行对应测试；通过后分别建立 checkpoint 提交。路由相关基线提交信息使用 `chore: checkpoint before semantic route refactor`；其他组使用能准确描述该组内容的提交信息。
4. 不得把不同关注点混入同一提交，不得提交失败测试、凭据或临时产物；无法确认修改归属或提交范围时停止并报告。完成 checkpoint 后再开始路由代码改造。
5. 运行现有 routing、intent fusion、coordinator、specialist、clarification、trace 测试。
6. 保存当前 `routing-v1` 数据集基线报告，报告记录 commit 和配置。
7. 记录生产配置模型下的 Understanding 延迟和失败率；模型不可用时明确记录未运行，不得伪造。

退出标准：

- 当前基线 commit、测试结果和报告可复现；
- 改造前已有代码修改已经按关注点分别 checkpoint，或确认不存在；
- 没有把知识入库改动、方案文档和路由代码混入同一提交。

### 阶段 1：内部协议和纯函数

动作：

1. 实现 UnderstandingDecision V2。
2. 新建 `route_planning.py`。
3. 实现精确 sourceText anchoring、固定复杂度信号、执行图/展示图和稳定拓扑排序；禁止实现传递约简。
4. 为所有纯函数写单元测试。

退出标准：

- 不接生产路由也能独立测试全部规划函数；
- 非法 segment、非法索引、循环、重复和重叠全部被拒绝；
- 纯函数不依赖数据库、Coordinator 或网络。

### 阶段 2：接入 classify_route

动作：

1. 将风险硬检测保留在最前。
2. 生成 rule draft 和 complexity assessment。
3. 复杂请求调用 semantic classifier。
4. 接纳 LLM segments 后逐段重新分类和抽取参数。
5. 构建 DAG 和 RoutePlan V2。
6. 删除旧的按下标 `_apply_intent_fusion()` 路径和无调用 `_semantic_plan()`。

退出标准：

- RoutePlan V2 外部字段不变；
- 所有旧 RoutePlan V2 测试继续通过，只有明确改变的复杂用例更新预期；
- 规则回退与旧简单请求行为一致；
- 复杂请求能使用 LLM segment 数量和依赖。

### 阶段 3：运行时适配

动作：

1. 修复 max work items/max claims 配置。
2. 增加四个同 Agent 工作项测试。
3. 更新 Specialist/Response dependency Prompt。
4. 修复心理报告 intent。
5. 调整 Skill 匹配输入。
6. 验证 Clarification resume。

退出标准：

- 不出现因同 Agent 四项导致的预算耗尽；
- 上游失败传播正确；
- 无依赖分支均能产生最终 specialist_result；
- 心理报告 intent 正确；
- 工具权限没有扩大。

### 阶段 4：Trace、离线对比和正式模型评测

动作：

1. 删除现有 fusion shadow、双计划计算和完整 `shadowRoutePlan` metadata。
2. 接入单一路径的脱敏 PlanningDiagnostics。
3. 新增独立 semantic planning model suite。
4. 新增 `routing-semantic-v2.jsonl` 并扩充复杂依赖数据。
5. 分别读取阶段 0 基线报告和新实现报告，在离线评测工具中生成差异报告。

退出标准：

- 第 15.4 节门禁全部通过；
- 生产代码和 Trace 中不存在 shadow 计划、旧新路由双计算或完整 sourceText/objective；
- p95 延迟和模型调用增量在接受范围；
- 没有高风险回归。

### 阶段 5：单路径发布与版本回滚

动作：

1. 确认旧 `_segments() -> _apply_intent_fusion() -> _merge_equivalent()` 生产编排和 shadow 代码均已删除。
2. 通过全部确定性测试和第 15.4 节可执行的模型门禁后，部署新的单一生产实现。
3. 观察 fallback、over-split、workItem 数、BUDGET_EXHAUSTED、RAG 调用量、延迟和 token。

回滚：

- 回滚到改造前经过验证的 Git commit 或上一部署制品；
- RoutePlan V2 和数据库协议未变化，因此不执行数据库回滚；
- 不在当前代码版本中通过环境变量切换回旧路由算法。

### 后续独立项目：结构化依赖交接

第 12 节不属于本次阶段 0～5。必须在本次改造稳定并单独批准后另行实施，不得由编码 AI顺带完成。

---

## 18. 关键伪代码

### 18.1 classify_route

```python
def classify_route(
    planning_input,
    memory_payload=None,
    semantic_classifier=None,
    settings=None,
    *,
    raw_current_input=None,
):
    planning_input = planning_input.strip()
    context = memory_payload or {}
    risk_text = raw_current_input if raw_current_input is not None else planning_input
    planning_diagnostics = MutablePlanningDiagnostics()

    risk = analyze_risk_signal(risk_text)
    if risk.explicit_current_self_harm or risk.indirect_current_danger:
        return RouteDecision(build_single_risk_plan(planning_input))

    limit = configured_work_item_limit(settings)
    rule_draft = build_rule_route_draft(planning_input, limit=limit)
    complexity = assess_complexity(planning_input, context, rule_draft, settings)

    candidate_segments = rule_draft.segments
    accepted_llm_decision = None
    fallback_reason = ""
    if complexity.should_call_llm:
        try:
            if semantic_classifier is None:
                raise SemanticPlannerUnavailable()
            validated_llm_decision = validate_understanding_v2(
                semantic_classifier(planning_input, context)
            )
            candidate_segments = reconcile_segments(
                planning_input,
                rule_draft,
                validated_llm_decision,
                limit,
            )
            accepted_llm_decision = validated_llm_decision
        except EXPECTED_SEMANTIC_PLANNING_ERRORS as exc:
            fallback_reason = normalized_fallback_reason(exc)
            candidate_segments = rule_draft.segments

    parsed_segments = []
    for segment in candidate_segments:
        fused = classify_final_segment(segment, context, settings)
        known, missing = extract_work_item_arguments(
            fused.intent,
            fused.task_kind,
            segment.source_text,
        )
        parsed_segments.append(build_parsed_segment(segment, fused, known, missing))

    execution_graph, presentation_graph = build_dependency_graphs(
        parsed_segments,
        rule_draft.dependencies,
        accepted_llm_decision.dependencyHints if accepted_llm_decision else [],
        settings,
        planning_diagnostics,
    )
    ordered = stable_topological_order(parsed_segments, presentation_graph)
    plan = build_route_plan_v2(planning_input, context, ordered, execution_graph)
    plan.validate()
    return RouteDecision(
        plan,
        diagnostics=finalize_planning_diagnostics(
            planning_diagnostics,
            complexity,
            rule_draft,
            accepted_llm_decision,
            fallback_reason,
        ),
    )
```

`build_rule_route_draft()` 必须先识别实际目标数量再应用容量检查，不得先截断。`SIMPLE_FAST_PATH` 不调用 semantic classifier；`COMPLEX` 最多调用一次 `_semantic_route()`，其中结构化 repair 最多一次且属于同一次受控模型阶段。`semantic_classifier is None`、模型未配置或调用失败时直接使用已经生成的规则草案。只有赋给 `accepted_llm_decision` 的完整计划才能提供分类候选和 dependencyHints；任何整份回退都不得继续使用该 LLM 输出的标签或依赖。

`UnderstandingAgent.act()` 必须调用：

```python
classify_route(
    board.model_input or board.user_input,
    context,
    semantic_classifier=self._semantic_route,
    settings=self.services.settings,
    raw_current_input=board.user_input,
)
```

保留 `classify_route(text, ...)` 的单文本调用形式供纯函数测试和内部评测使用；这只是同一实现的参数默认值，不是旧路由兼容分支。生产调用必须显式传入原始输入供风险硬检测。

`EXPECTED_SEMANTIC_PLANNING_ERRORS` 必须是明确异常元组，只包含模型不可用、模型超时、结构化输出非法、锚定失败、覆盖失败和重叠失败。`_semantic_route()` 不得再把所有异常统一包装为普通 `ValueError`，必须保留或转换成上述明确异常。规则 DAG 成环、Builder 不变量失败、RoutePlan.validate() 失败和程序缺陷必须继续抛出，使测试或请求显式失败；不得用 `except Exception` 掩盖代码错误。

### 18.2 依赖构图

```python
def build_dependency_graphs(segments, rule_candidates, llm_hints, settings, diagnostics):
    execution_graph = empty_graph(len(segments))

    for edge in verified_rule_hard_edges(rule_candidates):
        execution_graph.add(edge)
    assert_acyclic(execution_graph)

    llm_edges = sorted(
        verified_llm_hard_edges(llm_hints, settings),
        key=lambda item: (-item.confidence, item.source_key, item.target_key),
    )
    for edge in llm_edges:
        if execution_graph.would_cycle(edge):
            diagnostics.reject(edge, "DEPENDENCY_CYCLE_REJECTED")
            continue
        execution_graph.add(edge)

    presentation_graph = execution_graph.copy()
    for edge in sorted_verified_order_edges(rule_candidates, llm_hints, settings):
        if conflicts_with_hard_edge(edge, execution_graph):
            diagnostics.reject(edge, "ORDER_CONFLICT_REJECTED")
        elif presentation_graph.would_cycle(edge):
            diagnostics.reject(edge, "ORDER_CYCLE_REJECTED")
        else:
            presentation_graph.add(edge)

    return execution_graph, presentation_graph
```

### 18.3 生成 dependsOn

```python
for node in stable_topological_order(presentation_graph):
    item.depends_on = tuple(
        work_item_id[parent]
        for parent in sorted(execution_graph.parents(node), key=topological_position)
    )
```

---

## 19. 必须覆盖的具体测试样例

### 19.1 分段

```text
“你好”
=> 1 CHAT

“补考需要什么材料，截止日期是什么”
=> 1 CAMPUS，不得拆成两个属性任务

“查一下奖学金截止时间，并根据结果帮我安排申请计划”
=> 2 项：CAMPUS -> ACADEMIC HARD_DATA

“我最近很焦虑，也想制定期末复习计划”
=> 2 项：MENTAL、ACADEMIC，无硬依赖

“我最近很低落，还想问补考材料”
=> 2 项：MENTAL、CAMPUS，无硬依赖

“查补考政策、核对奖学金资格，同时给我制定英语复习计划”
=> 根据独立交付目标产生 3 项；前两项不能仅因同属 CAMPUS 自动合并
```

### 19.2 依赖

```text
“先告诉我调宿流程，再给我复习建议”
=> 2 项，ORDER_ONLY，不生成 dependsOn

“先查奖学金截止日期，再根据日期安排申请计划”
=> 第二项 dependsOn 第一项

“查课程截止日期和奖学金要求，再综合两者制定计划”
=> 第三项同时 dependsOn 第一、第二项

“查补考政策，同时制定英语复习计划，最后根据补考日期调整复习节奏”
=> 3 项；普通英语复习计划独立；调整复习节奏只依赖补考事实，禁止全链式依赖或把两个学习目标合并
```

### 19.3 非法 LLM 输出

- schemaVersion 错误；
- 未知字段；
- sourceText 来自历史；
- sourceText 是当前输入的改写而非原文；
- segment 重复；
- segment 重叠；
- 超过四段；
- 未知 Intent/TaskKind；
- dependency index 越界；
- 自依赖；
- 循环依赖；
- 同一节点对同时声明 HARD_DATA 和 ORDER_ONLY，或反向重复声明关系；
- LLM 超时或空输出。

所有情况必须生成合法规则回退计划，并记录标准化 fallback reason。

### 19.4 运行时

- 简单明确请求不调用 `_semantic_route()`；
- 复杂请求只调用一次主 `_semantic_route()`，结构化 repair 最多一次；
- 单轮只构建并发布一份 RoutePlan，不执行旧算法对照，不存在 shadow metadata；
- 四个 Campus 工作项全部执行并进入 Response；
- 四个 Academic 工作项不因 claims=3 卡死；
- 两个不同 Agent 的无依赖项均完成；
- 上游未关闭时下游不可领取；
- 上游 FAILED 后下游关闭为 UPSTREAM_FAILED；
- 所有终态结果到齐前不创建 Response；
- Response 覆盖全部独立分支；
- 高风险覆盖所有普通任务。

### 19.5 澄清和报告

- 一个独立工作项缺参时第一阶段全 plan 暂停；
- 澄清恢复后 ID 和依赖完全一致；
- `CAMPUS + MENTAL` 创建的心理报告 intent 为 MENTAL；
- `RISK + 其他内容` 只保留 RISK。

---

## 20. 禁止实现方式

编码 AI 不得采用以下捷径：

1. 只把 `INTENT_LLM_WEIGHT` 从 0.40 调高。
2. 继续用规则 segment 数量遍历 LLM segments。
3. 让 LLM 直接输出最终 RoutePlan V2 和任意 workItemId。
4. 把所有“先/再”都变成硬依赖。
5. 把所有相同 Intent 的任务自动合并。
6. 为每个逗号、问号或属性生成工作项。
7. LLM 失败时返回部分 LLM 工作项与部分规则工作项的临时拼接。
8. 在生产代码中保留或新增 shadow、旧新双计划、旧算法开关、V1/V2 双解析。
9. 为提高吞吐在同一改造中引入线程并发或异步黑板合并。
10. 让 Specialist 因依赖关系获得新的工具权限。
11. 让 RAG 再次拆分用户问题，形成路由层和检索层两套任务规划。
12. 修改或覆盖当前工作区中与本任务无关的知识入库改动。
13. 使用 `classified[:limit]`、`segments[:limit]` 等方式静默丢弃超过容量的目标。
14. 对 DAG 做传递约简，导致直接依赖结果不再传给下游。
15. 使用 `except Exception` 把规则构图、Builder 或 RoutePlan 不变量错误降级成普通模型失败。
16. 为本改造新增第二个 Understanding 模型、外部规划服务、工作流引擎或图数据库。

---

## 21. 验收命令

每个阶段先运行定向测试，再运行完整测试。命令可按项目环境调整：

```powershell
python -m pytest -q tests/test_route_planning.py tests/test_intent_fusion.py tests/test_route_plan_v2.py
python -m pytest -q tests/test_understanding_single_path.py
python -m pytest -q tests/test_coordinator_work_items_v2.py tests/test_specialist_agents_v2.py
python -m pytest -q tests/test_clarification_study_plan_flow.py tests/test_routing_safety_boundaries.py tests/test_trace_privacy.py
python -m pytest -q tests/evaluation
python -m pytest -q
```

还必须运行现有 routing-v1 报告和新增 semantic planning suite，并保存报告，不以单元测试通过代替真实模型评测。真实模型 suite 通过 `RUN_SEMANTIC_MODEL_EVAL=1` 显式运行；模型环境不可用时必须把该门禁标为“未执行”，不能标为通过。

编码完成前检查：

```powershell
rg -n "\\u[0-9a-fA-F]{4}" app tests *.md
git diff --check
git status --short
```

若发现正常中文字符串或注释被改成 Unicode escape，必须恢复为可读 UTF-8 中文。

---

## 22. 最终完成定义

只有同时满足以下条件才算本改造完成：

1. 简单请求继续走快速路径，不无条件增加 LLM 调用。
2. 复杂请求的最终工作项边界可以来自 LLM，而不是受规则 segment 数量限制。
3. LLM 多返回或少返回 segment 时不会发生下标错位。
4. 最终 Intent/TaskKind 确定后会重新抽取 known/missing arguments。
5. `dependencyHints` 被确定性消费，且只有 HARD_DATA 生成 dependsOn。
6. “先 A 再 B”不会默认生成硬依赖。
7. DAG 无环，synthesisOrder 是稳定拓扑顺序。
8. 同 Intent 多工作项不会被无条件合并。
9. 四个同 Agent 工作项不会因 claim 上限卡死。
10. 上游失败、PARTIAL 和最终 fan-in 行为有测试覆盖。
11. 澄清恢复保留原计划身份和依赖。
12. 跨域心理报告 intent 正确。
13. 生产代码中不存在 shadow、旧新双计划和旧算法开关；Trace 不记录完整 sourceText/objective/敏感参数。
14. 正式模型评测覆盖复杂分段和依赖，而不是只测规则函数。
15. 所有安全、工具权限、证据白名单和风险抢占测试通过。
16. 新增和修改文件保持 UTF-8，中文不使用无意义 Unicode escape。
17. 发布制品可通过 Git commit 或上一部署版本回滚，无需数据库回滚；同一版本中不保留旧路由实现。
18. 所有显式、有效的直接 HARD_DATA 边均被保留，未执行传递约简。
19. 超过工作项容量时不会静默截断或遗漏目标。

达到以上标准后，再单独实施结构化依赖交接和独立分支跨轮恢复，不在主改造未稳定时扩大范围。
