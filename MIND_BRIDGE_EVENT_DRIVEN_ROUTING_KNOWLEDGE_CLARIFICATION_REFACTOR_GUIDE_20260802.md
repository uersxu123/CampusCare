# MindBridge 事件驱动路由、KnowledgeAgent 与澄清机制统一改造实施指南

> 版本：V1.0
> 日期：2026-08-02
> 状态：代码改造指导文档，尚未实施本文变更
> 适用仓库：`mindbridge-py`
> 主要读者：负责实施改造、测试和验收的代码 AI 或开发者
> 架构边界：保留事件驱动多 Agent 协作、Blackboard、AgentTask、AgentArtifact、AgentEvent、Coordinator、PendingClarification、ContextBuilder、RRF、Trace 和 SSE 现状。
> 明确不做：本次不增加最终正文审查，不重写 SSE，不替换 RRF/embedding，不移除现有 Agent。

---

## 1. 文档优先级与使用方式

仓库已有多份路由、KnowledgeAgent、上下文和澄清专项文档。部分旧文档描述的是历史架构或已经被当前代码取代的方案，例如：

- 允许所有非高风险 `CONSULT` 先进入 KnowledgeAgent，再由 KnowledgeAgent 判断是否检索；
- 使用已经删除的 `app/services/agentic_rag.py`；
- 使用当前代码中已经不存在的 Web 兜底；
- 把 `None` 同时作为“没有命中任务”和“任务参数已经完整”的返回值。

在本轮“路由、KnowledgeAgent、澄清机制联合改造”范围内，执行 AI 应遵循以下优先级：

1. 当前用户明确要求；
2. 仓库根目录和上级目录的 `AGENTS.md`；
3. 当前生产代码与数据库模型；
4. 本文档；
5. 其他历史专项方案，仅作为背景资料。

遇到本文与当前代码不一致时，不得盲目照抄本文伪代码。应先确认当前实现和测试契约，再以“不破坏系统不变量”为原则做最小兼容调整。

执行方式：

1. 严格按阶段实施；
2. 每个阶段先补测试，再改生产代码；
3. 每个阶段独立验证；
4. 不允许一次性重写整个 Agent Runtime；
5. 不允许为了通过测试删除安全、证据或隔离约束；
6. 未经用户明确要求，不提交 Git commit，不覆盖无关工作区修改。

---

## 2. 已确认的生产故障基线

### 2.1 首页四个快捷问题实测

四个问题均在全新会话中测试，`recentMessageIds=[]`，可以排除历史上下文串扰。

| 快捷问题 | 页面观测耗时 | 服务端耗时 | 实际结果 |
|---|---:|---:|---|
| 学习计划 | 约 28.7 秒 | 26.566 秒 | 正确追问四个字段，但错误执行两次 Knowledge Planner |
| 校园办事 | 约 3.0 秒 | 0.825 秒 | 正确、确定性追问校区 |
| 升学就业 | 约 29.8 秒 | 27.188 秒 | 错误追问“你提到的对象具体指什么” |
| 压力倾诉 | 约 12.3 秒 | 10.017 秒 | 错误追问“你提到的对象具体指什么” |

### 2.2 学习计划重复追问

用户回答：

```text
数学，截止8号12点，每天8：00-16：00，高数基础不太会
```

当前解析结果为 `{}`。原因不是用户没有回答，而是无标签顺序解析采用全有或全无策略，其中 deadline 未通过正则后，课程、可用时间、难点也一起被丢弃。

### 2.3 KnowledgeAgent 生产路径没有到达检索

最近有完整 Knowledge 诊断的 17 个 `CONSULT` 中：

- 13 个 `FAILED_CLOSED`；
- 4 个进入澄清；
- 0 个执行本地查询。

多个明确要求学校正式规定的问题也在 Planner 阶段失败，`local_queries=0`。这说明生产瓶颈位于 RRF 之前。

### 2.4 RRF 本身基线正常

现有离线评测中 Hybrid-v3：

- Recall@8 约 0.994；
- MRR 约 0.95；
- P95 搜索耗时约 1.15 秒；
- 整体 gate 通过。

因此本次不得重写 RRF、BGE-M3、Chroma collection 或 BM25F。需要修复的是进入检索前的路由和 Planner 门槛。

### 2.5 当前测试无法阻止真实模型故障

当前主测试集为 217 passed，但大量路由与 Knowledge 测试使用 `ai_provider="mock"`，并且旧测试明确固化了：

```text
needs_knowledge == (route == CONSULT)
```

该契约必须修改。Mock 测试通过不能替代真实 Ollama 编排验收。

---

## 3. 根因结论

本次不是单个正则或单条 prompt 的问题，而是以下职责耦合：

1. `IntentType.CONSULT` 同时承担了“咨询意图”和“必须进入知识流程”两个含义；
2. Coordinator 自行按 `CONSULT` 重算 Knowledge 需要，没有服从 Route Artifact；
3. 已经存在受控澄清时，Coordinator 仍继续创建 KnowledgeAgent 任务；
4. Knowledge Planner 是所有低风险 CONSULT 的必经点；
5. Planner 可以把普通咨询错误转换为 `referent` 澄清；
6. 澄清回答使用全有或全无解析；
7. 澄清完成后不是恢复原任务，而是把合成文本重新送入通用路由；
8. 完整学习计划参数会因为 `parse_initial()` 返回 `None` 而丢失任务类型；
9. 本地模型单次 Planner 输出预算过大，且 deadline 只是调用前检查，不是硬超时。

---

## 4. 不可破坏的系统不变量

### 4.1 事件驱动协作不变量

- Agent 通过 `AgentTask` 认领工作；
- Agent 通过 `AgentArtifact` 发布结构化结果；
- Coordinator 根据 Blackboard 当前状态创建后续任务；
- 业务 Agent 不直接互相调用；
- 不把运行时改造成固定的 `if/elif` Service 串联；
- 允许确定性规则决定任务是否创建，但实际执行仍由 Agent 认领。

### 4.2 安全不变量

- 当前明确自伤、自残、伤人或即时危险必须抢占普通路由；
- 引用、翻译、论文、电影和第三人称语境不能仅凭风险词误判为本人即时危险；
- PendingClarification 不能阻止高风险新输入进入 Risk 路径；
- 本次不增加最终正文审查，保留现有 SafetyAgent 前置风险评估和 response proposal 审查。

### 4.3 知识事实不变量

- 学校特定事实必须来自本轮本地证据；
- 不得用模型常识补齐日期、电话、地点、材料、资格、政策或办理时长；
- 无证据或证据不足时必须明确降级；
- 不恢复互联网或 Web 搜索兜底；
- RRF、证据来源追踪、官方来源过滤和 Evidence Guard 必须保留。

### 4.4 数据与隔离不变量

- PendingClarification 必须按 user + session 隔离；
- ChatTurn、Trace、Memory 和会话历史必须保持用户隔离；
- 用户澄清原文继续遵循现有隐私脱敏策略；
- 数据库迁移必须向后兼容已有 PendingClarification 记录。

### 4.5 代码与编码不变量

- 编辑文件使用 UTF-8，优先无 BOM；
- 中文字符串、注释和 UI 文本保持直接可读字符；
- 禁止把中文改写为 `\uXXXX`；
- 不覆盖无关工作区修改；
- 不恢复已经删除的 `agentic_rag.py` 和 `web_search.py`。

---

## 5. 目标职责边界

### 5.1 UnderstandingAgent / Route Agent

负责：

- 识别 `CHAT / CONSULT / RISK`；
- 识别主领域和复合领域；
- 识别具体 `task_kind`；
- 生成结构化 `execution_mode`；
- 声明 Knowledge 需要级别；
- 对已知任务生成结构化缺失参数声明。

不负责：

- 执行检索；
- 评估证据覆盖；
- 生成校园事实；
- 让所有 CONSULT 默认进入 KnowledgeAgent。

### 5.2 Coordinator

负责：

- 根据 Artifact 和显式 guard 创建任务；
- 维护轮数、认领预算和终止条件；
- 在需要用户输入时终止本轮；
- 在澄清恢复后从保存的任务计划继续；
- 在 Knowledge 完成后创建 Response 任务。

不负责：

- 根据 `intent == CONSULT` 私自重算 Knowledge 需要；
- 在已有受控澄清时继续派发 Knowledge 工作。

### 5.3 KnowledgeAgent

负责：

- 对明确的 Knowledge 任务生成查询计划；
- 优先执行确定性 Fast Plan；
- 复杂问题才使用 LLM Planner；
- 执行本地 BM25 + embedding + RRF；
- 评估证据、必要时改写查询并执行有限第二轮；
- 发布 `KnowledgeEvidenceArtifact`；
- 对证据不足执行 fail-closed。

不负责：

- 学习计划字段解析；
- 普通情绪陪伴；
- 通用职业决策；
- 无依据地产生 `referent` 澄清；
- 决定整轮对话是否结束。

### 5.4 ClarificationService

负责：

- 保存任务暂停状态；
- 逐字段提取和验证用户回答；
- 保存所有有效字段；
- 只追问无效或缺失字段；
- 恢复原始任务计划；
- 处理取消、过期、新话题和高风险抢占。

不负责：

- 重新决定业务领域；
- 把恢复后的学习计划重新送入 Knowledge Planner；
- 用正则替代所有语义理解。

### 5.5 ResponseAgent

负责：

- 对 `RESPONSE_ONLY` 任务生成回答 prompt；
- 对 `KNOWLEDGE` 任务使用已验证证据生成回答 prompt；
- 使用 task-specific prompt profile；
- 不要求所有 CONSULT 必须先存在 Knowledge Artifact。

---

## 6. 新的核心契约

### 6.1 新增枚举

修改 `app/core/enums.py`，新增：

```python
class TaskKind(str, Enum):
    GENERAL_CHAT = "GENERAL_CHAT"
    STUDY_PLAN = "STUDY_PLAN"
    CAREER_DECISION = "CAREER_DECISION"
    EMOTIONAL_SUPPORT = "EMOTIONAL_SUPPORT"
    INSTITUTIONAL_FACT = "INSTITUTIONAL_FACT"
    KNOWLEDGE_GUIDANCE = "KNOWLEDGE_GUIDANCE"
    HIGH_RISK_SUPPORT = "HIGH_RISK_SUPPORT"


class ExecutionMode(str, Enum):
    DIRECT = "DIRECT"
    CLARIFY = "CLARIFY"
    RESPONSE_ONLY = "RESPONSE_ONLY"
    KNOWLEDGE = "KNOWLEDGE"
    HIGH_RISK = "HIGH_RISK"


class KnowledgeNeed(str, Enum):
    NONE = "NONE"
    OPTIONAL = "OPTIONAL"
    REQUIRED = "REQUIRED"
```

现有 `IntentType`、`RiskLevel` 和 `KnowledgeDomain` 不删除。

### 6.2 扩展 RouteDecision

修改 `app/agents/routing.py`：

```python
@dataclass(frozen=True)
class RouteDecision:
    route: IntentType
    risk_level: RiskLevel
    primary_domain: KnowledgeDomain | None
    secondary_domains: tuple[KnowledgeDomain, ...]
    task_kind: TaskKind
    execution_mode: ExecutionMode
    knowledge_need: KnowledgeNeed
    is_compound: bool
    freshness_required: bool
    memory_version: int
    confidence: float
    reason_codes: tuple[RouteReasonCode, ...]
```

兼容期内保留 payload 字段：

```python
"needs_knowledge": self.execution_mode == ExecutionMode.KNOWLEDGE
```

禁止继续使用：

```python
self.route == IntentType.CONSULT
```

作为 `needs_knowledge` 的定义。

### 6.3 新增 TurnPlan Artifact

UnderstandingAgent 除 `route` 外发布 `turn_plan`：

```json
{
  "schemaVersion": 1,
  "intent": "CONSULT",
  "primaryDomain": "ACADEMIC",
  "secondaryDomains": [],
  "taskKind": "STUDY_PLAN",
  "executionMode": "CLARIFY",
  "knowledgeNeed": "NONE",
  "resumeExecutionMode": "RESPONSE_ONLY",
  "freshnessRequired": false,
  "confidence": 0.95,
  "reasonCodes": ["STUDY_PLAN_SIGNAL"]
}
```

`turn_plan` 是 Coordinator 派发任务的唯一业务事实源。`route` 保留用于兼容 Trace、Response profile 和现有调用方。

### 6.4 快捷问题期望契约

| 输入 | taskKind | executionMode | knowledgeNeed |
|---|---|---|---|
| 帮我根据这学期的课程和截止时间制定一份学习计划。 | STUDY_PLAN | CLARIFY | NONE |
| 数学，9月20日，每晚7-9点，积分基础薄弱 | STUDY_PLAN | RESPONSE_ONLY | NONE |
| 我想了解调宿申请要怎么办理，需要准备什么？ | INSTITUTIONAL_FACT | CLARIFY | REQUIRED |
| 南望山校区调宿需要哪些材料？ | INSTITUTIONAL_FACT | KNOWLEDGE | REQUIRED |
| 我在考研、就业和实习之间拿不定主意，帮我梳理一下。 | CAREER_DECISION | RESPONSE_ONLY | NONE |
| 我最近压力很大，晚上总是睡不着，想和你聊聊。 | EMOTIONAL_SUPPORT | RESPONSE_ONLY | NONE |
| 当前本人明确高风险表达 | HIGH_RISK_SUPPORT | HIGH_RISK | NONE |

---

## 7. 阶段 1：测试先行和契约固定

### 7.1 新增测试文件

新增：

```text
tests/test_turn_plan_routing.py
tests/test_coordinator_execution_modes.py
tests/test_knowledge_fast_planner.py
tests/test_clarification_slot_extraction.py
tests/test_quick_prompt_orchestration.py
```

不要一开始删除旧测试。先新增目标测试，使其在生产代码修改前失败。

### 7.2 路由测试矩阵

必须逐项断言：

- `intent`；
- `primary_domain`；
- `task_kind`；
- `execution_mode`；
- `knowledge_need`；
- `needs_knowledge` 兼容字段。

### 7.3 Coordinator 不变量测试

必须覆盖：

```python
if board.latest_artifact("clarification_request"):
    assert no open knowledge task
    assert no KnowledgeAgent claim
```

```python
if execution_mode == RESPONSE_ONLY:
    assert ResponseAgent task exists
    assert KnowledgeAgent task does not exist
```

```python
if execution_mode == KNOWLEDGE:
    assert KnowledgeAgent task exists
    assert ResponseAgent waits for knowledge_evidence
```

### 7.4 修改旧错误契约

重点检查并修改：

```text
tests/test_preroute_memory_and_routing.py
tests/test_routing_integration.py
tests/test_event_driven_multi_agent.py
```

删除所有等价于以下逻辑的断言：

```python
needs_knowledge == (route == IntentType.CONSULT)
```

改为按 `execution_mode` 判断。

### 7.5 阶段完成门槛

- 新目标测试已经存在并能准确失败；
- 旧测试尚未通过篡改 mock 返回值掩盖行为；
- 未修改 Agent Runtime 主流程；
- Git diff 只包含测试和最小契约准备。

---

## 8. 阶段 2：Route Agent 与 TurnPlan

### 8.1 文件范围

主要修改：

```text
app/core/enums.py
app/agents/routing.py
app/agents/autonomous.py
app/services/context_builder.py（仅在需要提供路由视图字段时）
tests/test_turn_plan_routing.py
tests/test_routing_safety_boundaries.py
tests/test_preroute_memory_and_routing.py
```

### 8.2 分类顺序

`classify_route()` 应按以下顺序执行：

```text
1. 当前高风险硬规则
2. 已恢复任务计划
3. 确定性 task_kind 识别
4. 领域识别
5. execution_mode 与 knowledge_need 决策
6. 仅在低置信度时调用 semantic fallback
```

### 8.3 task_kind 规则

至少支持：

```text
学习计划/复习计划/学习规划/复习规划
    -> STUDY_PLAN

考研/保研/就业/实习/求职 + 选择/拿不定/比较/梳理
    -> CAREER_DECISION

压力/焦虑/睡不着/失眠/难受 + 想聊聊/倾诉/陪我聊
    -> EMOTIONAL_SUPPORT

校园事项 + 条件/材料/步骤/入口/期限/电话/地点/规定
    -> INSTITUTIONAL_FACT
```

规则不得把“领域命中”直接等价为“知识命中”。

### 8.4 Knowledge 需要判定

`KnowledgeNeed.REQUIRED` 的主要信号：

- 学校或校区特定事实；
- 资格、政策、材料、办理流程、截止日期、地点、电话、入口；
- 用户明确要求“依据正式规定”“根据学校文件”；
- 实时或易变事实；
- 回答必须引用本地证据。

`KnowledgeNeed.NONE` 的主要信号：

- 情绪陪伴；
- 普通建议；
- 职业选择梳理；
- 学习计划生成；
- 改写、翻译、头脑风暴；
- 不依赖学校事实的通用方法。

### 8.5 semantic fallback 契约

语义模型输出应包含：

```json
{
  "route": "CHAT|CONSULT",
  "primary_domain": "...",
  "secondary_domains": [],
  "task_kind": "...",
  "execution_mode": "...",
  "knowledge_need": "...",
  "is_compound": false,
  "confidence": 0.0
}
```

代码侧必须重新验证组合是否合法。例如：

- `EMOTIONAL_SUPPORT + KNOWLEDGE` 默认非法，除非用户明确问学校心理中心事实；
- `STUDY_PLAN + KNOWLEDGE` 默认非法；
- `INSTITUTIONAL_FACT + RESPONSE_ONLY` 在缺少证据需求时非法；
- `HIGH_RISK + KNOWLEDGE` 非法。

### 8.6 UnderstandingAgent 发布 Artifact

修改 `UnderstandingAgent.act()`：

1. 发布兼容 `route`；
2. 发布 `turn_plan`；
3. 当 `execution_mode=CLARIFY` 时发布结构化 `clarification_request`；
4. 不在这里调用 Knowledge Planner；
5. 完整学习计划必须发布 `task_arguments` 或将结构化参数放入 `turn_plan`，不得返回 `None` 丢失任务类型。

### 8.7 阶段完成门槛

- 四个快捷问题的 `turn_plan` 全部正确；
- 无真实高风险的学习计划不误判为 RISK；
- 当前安全边界测试全部通过；
- 尚未修改 RRF；
- `needs_knowledge` 由 execution mode 派生。

---

## 9. 阶段 3：Coordinator 条件调度和短路

### 9.1 文件范围

```text
app/agents/coordinator.py
app/agents/events.py（如新增 USER_INPUT_REQUIRED / TURN_RESUMED）
app/agents/autonomous.py
tests/test_coordinator_execution_modes.py
tests/test_event_driven_multi_agent.py
tests/test_routing_integration.py
```

### 9.2 新增辅助读取函数

```python
def _turn_plan(board) -> dict:
    ...


def _execution_mode(board) -> ExecutionMode:
    ...
```

不得在多个位置各自根据 intent 重算 mode。

### 9.3 `_derive_missing_work()` 目标逻辑

伪代码：

```python
ensure route task
ensure risk task

if route or risk missing:
    return board

ensure turn_plan compatibility artifact
ensure skill artifact

if high risk:
    ensure response task
    return board

if clarification_request exists:
    ensure controlled clarification response
    append USER_INPUT_REQUIRED if needed
    return board

mode = execution_mode(board)

if mode == KNOWLEDGE:
    ensure knowledge task
    if knowledge_evidence exists:
        ensure response task
    return board

if mode in {RESPONSE_ONLY, DIRECT}:
    ensure response task or controlled direct response
    return board
```

### 9.4 受控澄清终止规则

当 Blackboard 同时具备：

```text
route
risk
turn_plan.executionMode == CLARIFY
clarification_request
controlled response_proposal
```

Coordinator 应允许现有 Safety proposal review 完成，然后接受该 response proposal。本轮不得创建：

- `task:gather-knowledge`；
- `task:propose-response`；
- Planner 调用；
- RRF 查询。

### 9.5 Knowledge 任务条件

唯一条件：

```python
mode == ExecutionMode.KNOWLEDGE
and risk != RiskLevel.HIGH
and clarification_request is None
```

禁止保留：

```python
intent == IntentType.CONSULT
```

作为 Knowledge task 的创建条件。

### 9.6 Response 任务条件

```python
mode in {RESPONSE_ONLY, HIGH_RISK}
or (
    mode == KNOWLEDGE
    and knowledge_evidence is not None
)
```

### 9.7 并发边界

本阶段不改为并发 LLM 调用。使用同一个本地 Ollama 模型时，优先减少调用数。可以保留一个 round 内多个 Agent candidate 的事件语义，但不得为了“看起来像多 Agent”让不必要 Agent 执行。

### 9.8 阶段完成门槛

- 学习计划首轮 Planner 调用数为 0；
- 升学就业 Planner 调用数为 0；
- 压力倾诉 Planner 调用数为 0；
- 校园办事缺校区时 Planner 调用数为 0；
- 事件 Trace 仍能展示 AgentTask、AgentArtifact 和最终接受过程。

---

## 10. 阶段 4：通用澄清状态和逐字段提取

### 10.1 文件范围

```text
app/services/clarification_models.py
app/services/clarification_handlers.py
app/services/clarifications.py
app/models/entities.py
migrations/（新增向后兼容迁移）
tests/test_clarifications.py
tests/test_clarification_slot_extraction.py
```

可选新增：

```text
app/services/clarification_extractor.py
```

### 10.2 区分任务未命中、待澄清和已就绪

新增：

```python
class TaskParseStatus(str, Enum):
    NOT_MATCHED = "NOT_MATCHED"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    READY = "READY"


@dataclass(frozen=True)
class TaskParseResult:
    status: TaskParseStatus
    task_kind: ClarificationTaskKind | None
    known_arguments: dict[str, str]
    missing_arguments: tuple[MissingArgument, ...]
    validation_errors: dict[str, str]
```

不得继续使用同一个 `None` 同时表示“不是学习计划”和“学习计划已完整”。

### 10.3 逐字段提取契约

新增：

```python
@dataclass(frozen=True)
class SlotExtraction:
    values: dict[str, str]
    invalid_values: dict[str, str]
    unresolved_fields: tuple[str, ...]
```

无标签顺序输入时，每个字段独立校验。某个字段失败不得回滚其他字段。

### 10.4 提取顺序

```text
1. 显式标签正则
2. 已知 expected_fields 的顺序映射
3. 日期/时间专用解析
4. 对仍未识别字段执行一次结构化 LLM extractor
5. 确定性 validator 最终验证
```

LLM 只允许返回字段值和置信度，不允许：

- 补造用户未说的值；
- 修改时间；
- 判断是否需要 Knowledge；
- 决定追问轮数；
- 直接生成最终计划。

### 10.5 日期支持范围

至少支持：

```text
8号
8号12点
本月8号
下个月8号中午12点
8月8日12:00
明天晚上七点
周五12点
下周五中午
考试周前
本月底
```

无法唯一确定的日期使用 `NEEDS_CONFIRMATION`，不得直接把全部字段判为无效。

### 10.6 追问策略

- 首轮可合并询问多个缺失字段；
- 用户回答后保存所有有效字段；
- 后续只询问 invalid 或 unresolved 字段；
- 已确认字段不得重复询问；
- 后续每轮优先问一个最有区分度的字段；
- 达到最大轮数时，能安全生成部分结果则说明缺失后继续；无法执行时再结束。

### 10.7 PendingClarification 持久化扩展

推荐新增字段：

```python
resume_context_json: Mapped[str] = mapped_column(Text, default="{}")
```

示例：

```json
{
  "schemaVersion": 1,
  "originTaskId": "task:study-plan",
  "originArtifactId": "UnderstandingAgent:turn_plan:...",
  "taskKind": "STUDY_PLAN",
  "resumeExecutionMode": "RESPONSE_ONLY",
  "intent": "CONSULT",
  "primaryDomain": "ACADEMIC"
}
```

禁止把控制元数据放入 `known_arguments_json`。

数据库迁移要求：

- 非空默认 `{}`；
- 旧记录可正常读取；
- 缺少 resume context 的旧记录采用兼容构造；
- 不删除旧 pending 数据。

### 10.8 取消和新话题

输入：

```text
算了，我最近压力很大
```

应产生：

```text
CLARIFICATION_CANCELLED
remaining_text = 我最近压力很大
continue_current_message = True
```

随后剩余文本继续走新一轮 Route Agent。不得只返回“已取消”并丢弃后半句。

### 10.9 referent 处理边界

Knowledge 范围澄清中的 `referent` 只有在以下条件同时满足时才合法：

1. 当前输入包含真实指代词；
2. 最近上下文无法恢复先行词；
3. 缺少指代确实阻止后续 Knowledge 查询。

真实指代词示例：

```text
这个
那个
它
上述
前面那个
这个规定
那件事
这种情况
```

以下输入不得产生 referent：

```text
考研、就业和实习怎么选
我最近压力很大
学习计划怎么做
调宿需要哪些材料
```

### 10.10 阶段完成门槛

- 故障原句至少保留 course、availableTimeWindows、focusProblem；
- deadline 若仍歧义，只追问 deadline；
- 一次补齐和分轮补齐均能 RESOLVED；
- 取消并提出新问题不会丢失新问题；
- Pending user/session 隔离测试通过；
- 无关新话题不会污染已知 slots。

---

## 11. 阶段 5：澄清任务恢复而不是重新路由

### 11.1 文件范围

```text
app/agents/harness.py
app/agents/event_driven_runtime.py
app/agents/events.py
app/services/clarification_models.py
app/services/clarifications.py
app/services/context_builder.py
tests/test_quick_prompt_orchestration.py
tests/test_event_driven_multi_agent.py
```

### 11.2 扩展 ClarificationResolution

```python
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
```

### 11.3 Harness 接入

`MindBridgeAgentHarness.run()` 在 `resolution.status == RESOLVED` 时：

1. 保留现有隐私处理；
2. 不把用户回答直接当成普通新问题；
3. 将 `resume_context` 和 `resolved_arguments` 传给 Runtime；
4. `model_input` 只作为 Response prompt 中的可信结构化任务输入，不作为重新判断 Knowledge 需求的唯一文本。

### 11.4 Runtime 预置 Artifact

扩展 `EventDrivenAgentRuntimeService.run()`：

```python
def run(
    ...,
    clarification_state=None,
    resume_context=None,
    resolved_arguments=None,
):
```

恢复时预置：

```text
ClarificationService:turn_plan
ClarificationService:task_arguments
TURN_RESUMED event
```

如果 resume context 已包含合法 route 和 turn plan，UnderstandingAgent 不得再次覆盖。

### 11.5 学习计划恢复路径

```text
用户补充四个字段
→ ClarificationService RESOLVED
→ TURN_RESUMED
→ turn_plan.executionMode = RESPONSE_ONLY
→ Coordinator 创建 ResponseAgent task
→ ResponseAgent 使用 task_arguments
```

禁止：

```text
RESOLVED
→ 重新 classify_route(合成文本)
→ CONSULT
→ KnowledgeAgent
```

### 11.6 校园范围恢复路径

```text
调宿问题缺少 site
→ WAITING_USER
→ 用户回答未来城
→ RESOLVED
→ TURN_RESUMED
→ turn_plan.executionMode = KNOWLEDGE
→ KnowledgeAgent
```

### 11.7 阶段完成门槛

- 学习计划 RESOLVED 后 KnowledgeAgent 调用数为 0；
- 校园 site RESOLVED 后 KnowledgeAgent 正常认领；
- 恢复事件在 Trace 中可观察；
- 不产生第二个错误的新 task kind；
- clarification 原始输入继续脱敏。

---

## 12. 阶段 6：KnowledgeAgent Fast Plan 与 Planner fallback

### 12.1 文件范围

新增：

```text
app/services/knowledge_agent/fast_planner.py
tests/test_knowledge_fast_planner.py
```

修改：

```text
app/agents/autonomous.py
app/services/knowledge_agent/orchestrator.py
app/services/knowledge_agent/planner.py
app/services/knowledge_agent/policy.py
app/services/knowledge_agent/models.py
app/services/knowledge_query.py
app/core/config.py
.env.example
tests/test_knowledge_orchestrator.py
tests/test_knowledge_policy.py
tests/test_routing_integration.py
```

### 12.2 KnowledgeAgent 认领条件

`KnowledgeAgent.decide()` 必须读取 `turn_plan`：

```python
if execution_mode != ExecutionMode.KNOWLEDGE:
    return AgentDecision(False, reason="turn does not require knowledge")
```

不得继续使用 `intent == CONSULT` 作为充分条件。

### 12.3 DeterministicKnowledgePlanner

职责：

- 使用现有 taxonomy `build_query_spec()`；
- 识别 canonical concept；
- 识别 site；
- 识别 required facets；
- 对单一事实问题生成一组受控查询；
- 不调用 LLM；
- 不生成最终回答。

接口：

```python
class DeterministicKnowledgePlanner:
    def plan(self, view: KnowledgeView) -> KnowledgePlan | None:
        ...
```

Fast Plan 适用条件：

```text
单一问题
单一领域
taxonomy 能识别 concept
required scope 已齐全
不存在未解析指代
facets 可由 taxonomy 和用户原句确定
```

### 12.4 Fast Plan 查询生成

至少生成：

1. 一个 lexical query；
2. 一个 semantic query；
3. 必要时一个 official-terms query。

示例：

```json
{
  "overall_action": "RETRIEVE",
  "questions": [
    {
      "question_id": "q1",
      "original_question": "南望山校区调宿需要什么材料？",
      "standalone_question": "南望山校区调宿需要什么材料？",
      "action": "RETRIEVE",
      "task_type": "INSTITUTIONAL_FACT",
      "domain": "CAMPUS_SERVICE",
      "required_facets": ["MATERIALS"],
      "site": "NANWANGSHAN",
      "query_variants": [
        {
          "kind": "LEXICAL",
          "text": "南望山校区 宿舍调整 申请材料"
        },
        {
          "kind": "SEMANTIC",
          "text": "南望山校区调宿需要准备哪些材料？"
        }
      ]
    }
  ]
}
```

### 12.5 Orchestrator 顺序

修改 `KnowledgeOrchestrator.run()`：

```python
required_scope_plan = policy.required_scope_plan(view)
if required_scope_plan is not None:
    return finalize_scope_clarification(required_scope_plan)

fast_plan = fast_planner.plan(view)
if fast_plan is not None:
    return execute_validated_plan(fast_plan)

if not planner_budget_available:
    return deterministic_fallback_or_failed_closed(view)

llm_plan = planner.plan(view)
return execute_validated_plan(llm_plan)
```

### 12.6 LLM Planner 允许条件

仅以下情况使用：

- `is_compound=True`；
- 存在多个独立制度事实子问题；
- taxonomy 无法识别 concept；
- 多领域事实部分需要拆分；
- 存在真实未解析指代；
- Fast Plan 通过不了 policy，但原问题仍明确需要证据。

### 12.7 Planner 失败降级

当 LLM Planner 失败：

```text
如果原始问题可构造受控 deterministic query
    使用原始问题 + domain/site/facet filter 继续本地检索
否则
    FAILED_CLOSED
```

禁止因为 Planner JSON 错误而对明确校园事实直接 `local_queries=0`。

### 12.8 referent policy

在 `KnowledgePolicy._validate_clarification()` 增加：

```python
if clarification.field == KnowledgeClarificationField.REFERENT:
    if not has_unresolved_reference(view.current_question, view.recent_context):
        raise KnowledgePolicyError("REFERENT_NOT_GROUNDED", question_id)
```

Policy repair 不能再次复用被拒绝的 referent。

### 12.9 配置

新增或统一：

```python
knowledge_fast_plan_enabled: bool = True
knowledge_planner_max_tokens: int = 480
knowledge_planner_timeout_seconds: float = 8.0
knowledge_planner_policy_repair_enabled: bool = True
knowledge_planner_max_policy_repairs: int = 1
knowledge_allow_deterministic_fallback: bool = True
```

删除 `planner.py` 中硬编码的 `max_tokens=1400`。统一从 KnowledgeAgent profile 或专用配置读取。

注意：当前 `knowledge_agent_deadline_seconds` 只是 admission check。实现 AI 应为单次 Planner provider call 增加真实请求超时，且达到 deadline 后禁止开始 repair。

### 12.10 不修改的检索内容

不得修改：

- hybrid-v3 RRF 公式；
- BM25F 权重；
- BGE-M3 embedding model；
- Chroma collection 激活机制；
- index signature；
- Evidence Guard 的来源校验；
- RAG eval 现有阈值，除非新代码暴露真实评测缺陷且有证据。

### 12.11 阶段完成门槛

- 范围完整的单一校园事实 Planner LLM 调用数为 0；
- `local_queries >= 1`；
- `retrievalMode=hybrid-v3`；
- 明确复杂问题才允许 Planner；
- Planner 失败时可确定性降级；
- 无真实指代时绝不发布 referent。

---

## 13. 阶段 7：ResponseAgent task profile

### 13.1 文件范围

```text
app/agents/autonomous.py
app/services/ai.py
app/services/context_builder.py
tests/test_response_prompt_profiles.py
tests/test_quick_prompt_orchestration.py
```

### 13.2 ResponseAgent.decide()

目标条件：

```python
if mode == RESPONSE_ONLY:
    claim
elif mode == HIGH_RISK:
    claim
elif mode == KNOWLEDGE and knowledge_evidence:
    claim
else:
    wait
```

普通 CONSULT 不再因为缺少 `knowledge_evidence` 而等待。

### 13.3 STUDY_PLAN profile

使用 `task_arguments`：

```text
课程
截止时间
可用时间
当前难点
```

Prompt 必须要求：

- 不重复询问已确认字段；
- 计划不得超过用户可用时间；
- 计划安排在截止时间之前；
- 不补造课程；
- 当前难点必须映射为优先任务；
- 时间仍有歧义时只说明该歧义，不擅自修改。

### 13.4 CAREER_DECISION profile

Prompt 必须要求：

- 给出比较维度；
- 区分用户已知信息和待确认信息；
- 给出低成本验证动作；
- 最多追问一个真正影响选择的问题；
- 不把一般建议包装成学校事实；
- 不进入 Knowledge，除非用户明确追问学校政策、资格、日期或具体招聘事实。

### 13.5 EMOTIONAL_SUPPORT profile

Prompt 必须要求：

- 先回应当前感受；
- 不诊断、不报告后台风险标签；
- 不连续多轮只做泛化追问；
- 最近两轮已经询问原因时，本轮至少提供一个低负担支持动作；
- 只有用户询问心理中心预约、地点、电话等校园事实时才需要 Knowledge。

可以在 ContextBuilder 派生轻量 `recent_assistant_acts`，但本阶段不新增新的 EmotionalAgent。

### 13.6 本次不做最终正文审查

明确不新增：

```text
RESPONSE_GENERATED -> FINAL_RESPONSE_REVIEW -> FINAL_ACCEPTED
```

继续使用当前：

```text
ResponseProposal -> SafetyAgent 现有审查 -> Coordinator 接受 -> ChatService 生成正文
```

不得借本次改造扩大到最终正文审核、二次生成或额外模型调用。

### 13.7 阶段完成门槛

- 升学就业只需一次 Response 生成；
- 压力倾诉只需 Safety + Response，不调用 Planner；
- 学习计划参数完整后能生成计划；
- 无证据的学校事实仍不允许模型补造。

---

## 14. Trace、指标和兼容字段

### 14.1 TurnPlan 可观测性

Trace 的 route artifact summary 增加：

```text
task_kind
execution_mode
knowledge_need
resume_execution_mode
```

### 14.2 澄清指标

增加或记录：

```text
clarification_task_kind
clarification_round
recognized_field_count
invalid_field_count
remaining_field_count
extraction_source: REGEX|ORDERED|LLM|MIXED
resumed: true|false
```

不得记录未脱敏的敏感原文。

### 14.3 Knowledge 指标

增加：

```text
plan_source: REQUIRED_SCOPE|FAST_PLAN|LLM_PLAN|DETERMINISTIC_FALLBACK
planner_skipped_reason
planner_timeout
policy_repair_count
local_queries
retrieval_rounds
retrieval_mode
```

### 14.4 性能验收以调用路径优先

不可只断言绝对耗时，因为 Ollama 冷启动、硬件和并发会影响结果。必须先断言：

- 不需要 Planner 的请求 Planner 调用数为 0；
- 不需要 RAG 的请求 local query 为 0；
- 明确校园事实范围完整时 local query 大于 0；
- 固定澄清不执行 Response 模型生成。

在调用路径正确后，再观察：

- 固定追问目标小于 2 秒；
- Hybrid RRF P95 保持在现有可接受范围；
- 普通回答只保留必要的模型调用。

---

## 15. 端到端验收矩阵

### 15.1 首页快捷问题

| 场景 | Planner | Local Query | Response Model | 预期 |
|---|---:|---:|---:|---|
| 学习计划首轮 | 0 | 0 | 0 | 询问课程、截止时间、可用时间、当前难点 |
| 校园办事首轮 | 0 | 0 | 0 | 询问校区 |
| 升学就业 | 0 | 0 | 1 | 给比较框架或一个有效关键问题 |
| 压力倾诉 | 0 | 0 | 1 | 共情并提供有价值的支持，不问 referent |

### 15.2 学习计划一次补齐

输入：

```text
数学，9月20日，每天晚上7点到9点，高数基础薄弱
```

预期：

- Pending 状态 RESOLVED；
- 四个字段全部保存；
- `TURN_RESUMED`；
- `executionMode=RESPONSE_ONLY`；
- KnowledgeAgent 调用数为 0；
- 最终生成学习计划。

### 15.3 学习计划部分歧义

输入：

```text
数学，截止8号12点，每天8：00-16：00，高数基础不太会
```

预期：

- 数学、可用时间、难点被保存；
- 只确认 deadline；
- 不重复询问全部四项；
- 不调用 Knowledge Planner。

### 15.4 校园范围恢复

对话：

```text
用户：调宿需要哪些材料？
助手：你想查询哪个校区？
用户：未来城。
```

预期：

- site 规范化为 `FUTURE_CITY`；
- 恢复 `INSTITUTIONAL_FACT`；
- `executionMode=KNOWLEDGE`；
- Fast Plan；
- `local_queries >= 1`；
- hybrid-v3；
- 有证据才回答具体材料。

### 15.5 referent 正负例

负例：

```text
我在考研、就业和实习之间拿不定主意
我最近压力很大
调宿需要哪些材料
```

不得产生 referent。

正例：

```text
这个怎么办？
前面那个规定什么时候截止？
```

只有上下文无法恢复先行词时才允许 referent。

### 15.6 取消并换题

对话：

```text
助手：请告诉我学习计划的截止时间。
用户：算了，我最近压力很大。
```

预期：

- 原 pending CANCELLED；
- 新输入继续路由；
- taskKind 为 EMOTIONAL_SUPPORT；
- 不回复“请告诉我截止时间”；
- 不丢失“我最近压力很大”。

### 15.7 高风险抢占

Pending 状态下出现当前本人明确高风险表达：

- pending INTERRUPTED；
- Risk route 抢占；
- KnowledgeAgent 不运行；
- 不继续原澄清。

---

## 16. 测试与验证命令

执行 AI 应优先使用当前项目已经配置的本地环境，不得擅自安装依赖或修改系统环境。

### 16.1 静态测试

```powershell
python -m pytest -q
```

### 16.2 分阶段测试

```powershell
python -m pytest -q tests/test_turn_plan_routing.py
python -m pytest -q tests/test_coordinator_execution_modes.py
python -m pytest -q tests/test_clarifications.py tests/test_clarification_slot_extraction.py
python -m pytest -q tests/test_knowledge_fast_planner.py tests/test_knowledge_orchestrator.py tests/test_knowledge_policy.py
python -m pytest -q tests/test_quick_prompt_orchestration.py
```

### 16.3 Docker 服务状态

```powershell
docker compose ps
```

### 16.4 真实 Ollama 验收

真实验收必须记录：

- 服务端 turn duration；
- provider call purposes；
- Planner 调用数；
- policy repair 调用数；
- local query 数；
- retrieval mode；
- 最终回答；
- clarification task kind 和 round。

真实模型验收不能使用精确文本匹配，应使用路径和语义不变量。

### 16.5 编码检查

修改完成后检查本次变更文件：

```powershell
git diff --check
rg -n '\\u[0-9a-fA-F]{4}' <本次修改文件列表>
```

发现普通中文字符串或注释被改成 Unicode escape 时，必须恢复成直接中文。

---

## 17. 分 PR / 分阶段交付建议

### PR 1：TurnPlan 契约与 Coordinator 短路

内容：

- 新增枚举；
- 扩展 RouteDecision；
- UnderstandingAgent 发布 turn_plan；
- Coordinator 按 execution mode 创建任务；
- 澄清后不再创建 Knowledge task；
- 更新路由和集成测试。

验收重点：四个快捷问题的 Planner 路径正确。

### PR 2：澄清逐字段提取

内容：

- TaskParseResult；
- SlotExtraction；
- 日期增强；
- 逐字段保存；
- 可选结构化 LLM fallback；
- 只问剩余字段。

验收重点：故障原句不再导致 `{}`。

### PR 3：Pending 任务恢复

内容：

- resume context 数据迁移；
- ClarificationResolution 扩展；
- TURN_RESUMED；
- Runtime 预置 turn plan；
- 学习计划恢复直达 Response；
- 校园范围恢复直达 Knowledge。

验收重点：恢复后不重新走错路由。

### PR 4：Knowledge Fast Plan

内容：

- DeterministicKnowledgePlanner；
- Orchestrator 快慢双路径；
- referent grounded policy；
- Planner 超时、token 和 fallback；
- Knowledge 测试和 RAG 集成测试。

验收重点：明确校园事实能够真正到达 hybrid-v3。

### PR 5：Response task profiles 与完整回归

内容：

- STUDY_PLAN；
- CAREER_DECISION；
- EMOTIONAL_SUPPORT；
- 快捷问题 E2E；
- 真实 Ollama 回归；
- Trace 和调用预算验收。

验收重点：回答体验和调用数同时达标。

---

## 18. 风险与回滚策略

### 18.1 兼容开关

建议新增：

```python
turn_plan_routing_enabled: bool = True
clarification_slot_extractor_enabled: bool = True
clarification_llm_fallback_enabled: bool = True
knowledge_fast_plan_enabled: bool = True
knowledge_allow_deterministic_fallback: bool = True
```

开关用于灰度和快速定位，不应长期维护两套完整架构。

### 18.2 回滚粒度

- Route/Coordinator 可独立回滚；
- Clarification 数据迁移只新增字段，回滚代码时旧字段可保留；
- Fast Plan 可通过开关关闭，回到 LLM Planner；
- 不需要回滚 RRF 索引；
- 不删除新 Trace 字段，旧代码可忽略。

### 18.3 禁止回滚方式

- 不得 `git reset --hard`；
- 不得覆盖用户已有未提交修改；
- 不得删除 PendingClarification 生产数据；
- 不得通过把所有问题改成 CHAT 来规避 Knowledge 错误；
- 不得通过关闭 Evidence Guard 来让测试通过。

---

## 19. Definition of Done

只有同时满足以下条件，本轮改造才算完成：

### 19.1 架构

- Blackboard、Task、Artifact、Event、Coordinator 和 Agent 认领机制仍在；
- `turn_plan` 是调度唯一事实源；
- Coordinator 不再根据 CONSULT 自行推导 Knowledge；
- 澄清是明确终态；
- 澄清恢复从原任务继续。

### 19.2 路由

- 四个首页问题 task kind 和 execution mode 正确；
- 情绪倾诉和职业决策不进入 Knowledge；
- 学习计划首轮不进入 Knowledge；
- 校园事实范围完整后进入 Knowledge。

### 19.3 澄清

- 多样表达可逐字段累积；
- 一个字段无效不丢失其他字段；
- 不重复询问已确认字段；
- 完成后恢复原任务；
- 取消、新话题、高风险抢占正确。

### 19.4 Knowledge

- 简单事实使用 Fast Plan；
- 复杂事实才使用 LLM Planner；
- Planner 失败有确定性降级；
- 无真实指代不产生 referent；
- 范围完整的事实问题 `local_queries >= 1`；
- hybrid-v3 和 Evidence Guard 保持有效。

### 19.5 性能

- 学习计划固定追问不再等待 Planner；
- 校园范围追问不调用 Planner；
- 升学就业和压力倾诉不调用 Planner；
- 不通过增加更多并发 LLM 调用伪造性能优化。

### 19.6 测试

- 全量 pytest 通过；
- 新增的路由、Coordinator、澄清、Fast Plan 和快捷问题测试通过；
- 至少完成一次真实 Ollama + bge-m3 + hybrid-v3 验收；
- Mock 和真实模型测试的职责边界明确；
- `git diff --check` 通过；
- 无意外 Unicode escape。

---

## 20. 明确禁止的错误实现

执行 AI 不得采用以下做法：

1. 继续保留 `CONSULT => needs_knowledge`；
2. 让 KnowledgeAgent 接收所有 CONSULT 后再决定 SKIP；
3. 有澄清 Artifact 后仍运行 Planner；
4. 用更长 Planner prompt 修复错误路由；
5. 用更大的 `max_tokens` 修复 Planner JSON；
6. 把升学就业或情绪倾诉硬编码成固定最终答案；
7. 只扩大 deadline 正则而保留全有或全无解析；
8. 让 LLM extractor 跳过确定性校验；
9. 把 resume context 塞入用户 known arguments；
10. 澄清完成后重新从通用路由开始；
11. 为没有显式指代的问题生成 referent；
12. Planner 失败后直接跳过所有本地检索；
13. 重写或关闭 RRF 来掩盖上游问题；
14. 恢复 Web 搜索；
15. 移除安全高风险抢占；
16. 增加最终正文审查，本次明确不做；
17. 顺带重构 SSE；
18. 删除现有 Trace 或调用指标；
19. 为通过测试修改 mock，使 mock 永远返回期望答案；
20. 覆盖工作区无关修改或修改文件编码。

---

## 21. 可直接交给代码 AI 的执行指令

```text
你正在修改 MindBridge Python 项目。

目标：按照 MIND_BRIDGE_EVENT_DRIVEN_ROUTING_KNOWLEDGE_CLARIFICATION_REFACTOR_GUIDE_20260802.md，
在不破坏事件驱动多 Agent 协作架构的前提下，重构路由、Coordinator、KnowledgeAgent 和澄清恢复机制。

必须保留：
- CollaborationBlackboard
- AgentTask / AgentArtifact / AgentEvent
- EventDrivenCoordinator
- UnderstandingAgent / SafetyAgent / KnowledgeAgent / ResponseAgent
- PendingClarification
- ContextBuilder
- BM25 + BGE-M3 + RRF
- Evidence Guard
- Trace / Turn Metrics
- SSE 现状

明确不做：
- 不增加最终正文审查
- 不恢复 Web 搜索
- 不重写 RRF
- 不移除 Agent
- 不把系统改成固定顺序 Service 流水线

执行要求：
1. 先阅读 AGENTS.md、本文档和当前代码。
2. 先运行 git status，保护已有用户修改。
3. 严格按本文阶段实施，每阶段测试先行。
4. RouteDecision 新增 task_kind、execution_mode、knowledge_need。
5. turn_plan 成为 Coordinator 调度的唯一业务事实源。
6. 澄清出现后立即短路，不再创建 Knowledge 任务。
7. 澄清完成后通过 resume context 恢复原任务，不重新走通用路由。
8. KnowledgeAgent 只认领 execution_mode=KNOWLEDGE 的任务。
9. 简单事实优先 Deterministic Fast Plan，复杂问题才使用 LLM Planner。
10. Planner 失败时优先确定性降级，不能让明确事实请求永远 local_queries=0。
11. 无真实未解析指代时禁止 referent。
12. 学习计划逐字段提取，一个字段失败不得丢失其他字段。
13. 不修改 RRF、embedding、Chroma 激活和 Evidence Guard 核心逻辑。
14. 不增加最终正文审查。
15. 修改文件保持 UTF-8，中文不得写成 Unicode escape。

每完成一个阶段：
- 运行该阶段定向测试；
- 运行全量 python -m pytest -q；
- 检查调用路径和 Trace；
- 汇报修改文件、测试结果、剩余风险；
- 在进入下一阶段前确认没有扩大范围。

如果当前代码已经实现本文某项要求，不要重复重写；以测试和生产 trace 验证后标记为已满足。
如果本文与当前生产代码冲突，先说明冲突和最小兼容方案，不得擅自推倒重构。
```

---

## 22. 编码 AI 阶段汇报模板

```text
阶段：

完成内容：
- 待填写

修改文件：
- 待填写

新增或更新测试：
- 待填写

关键行为变化：
- 待填写

模型调用路径变化：
- Planner：
- Local Query：
- Response：

测试结果：
- 定向测试：
- 全量测试：
- 真实 Ollama：

未解决问题：
- 待填写

工作区保护说明：
- 是否保留已有用户修改：
- 是否发现编码问题：
- 是否修改了范围外文件：
```

---

## 23. 最终预期语义

完成本方案后，系统应具备以下稳定语义：

```text
Route Agent 决定任务是什么以及应该走哪一种执行模式；
Coordinator 根据 TurnPlan Artifact 创建必要任务；
ClarificationService 持久化暂停状态并恢复原任务；
KnowledgeAgent 只处理明确的知识任务；
简单事实由确定性 Fast Plan 直达 RRF；
复杂事实才使用 LLM Planner；
Evidence Grader 决定是否需要有限第二轮；
ResponseAgent 处理普通回答、任务回答和有证据的事实回答；
SafetyAgent 保持现有前置风险评估与 proposal 审查；
所有关键状态都通过 Artifact、Event 和 Trace 可观察。
```

这不是把多 Agent 改成硬编码流水线，而是让事件驱动协作拥有明确、可验证、可恢复的控制语义。
