# MindBridge 端到端系统质量增量优化实施方案

> 文档状态：可直接交给后续 AI 编码实施
>
> 编制日期：2026-08-04
>
> 适用项目：`mindbridge-py`
>
> 实施性质：现有架构内的小范围修复、测试补强和参数校准
>
> 核心约束：保留全部现有 Agent，不重构整体架构

## 1. 使用说明

本文用于指导后续 AI 或开发者对 MindBridge 的端到端系统质量进行增量改造。本文不是架构重构提案，也不是要求替换现有 Agent、Coordinator、事件循环、共享黑板、知识库或模型供应商。

执行者必须先完整阅读本文，再开始修改代码。不得只根据章节标题选取部分内容实施。

开始实施前必须：

1. 阅读项目根目录 `AGENTS.md`（如存在）、`README.md`、本文以及所有计划修改的文件。
2. 执行 `git status --short`，确认工作区已有改动；不得回滚、覆盖或格式化无关文件。
3. 记录相关测试基线，先补失败测试，再修改生产代码。
4. 每个阶段只处理本文规定的一个问题，测试通过后再进入下一阶段。
5. 所有编辑文件保存为 UTF-8，优先使用 UTF-8 无 BOM。
6. 中文文字保持直接可读，不得改写为 `\uXXXX` Unicode 转义。
7. 不得通过降低安全、证据、召回或质量门禁来制造“通过”结果。
8. 不得在实施过程中自动执行全量 `e2e-ragas/full`；只有在本文规定的目标回归和相关测试通过后，才允许人工确认后运行全量评测。

## 2. 架构冻结声明

### 2.1 必须保留的 Agent

以下 Agent 必须全部保留，名称、职责边界和注册关系不得删除、合并、替换或绕过：

- `UnderstandingAgent`
- `SafetyAgent`
- `KnowledgeAgent`
- `ResponseAgent`
- `CoordinatorAgent`

必须保留：

- `EventDrivenAgentRuntimeService`
- `EventDrivenCoordinator`
- `CollaborationBlackboard`
- `AgentRegistry`
- 当前事件驱动任务板与 Artifact 协作方式
- `KnowledgeOrchestrator` 内部的 Planner、Retriever、Grader、Rewriter 和 Policy
- 高风险请求由 Safety 规则优先处置的现有原则

### 2.2 外部协议冻结

不得进行以下变化：

- 不改变公开 API 请求或响应结构。
- 不增加新的 Agent。
- 不建立第二套工作流、状态机、消息总线或 Agent 编排器。
- 不改变现有 Agent Artifact 的 kind 和主要消费关系。
- 不改变 Clarification 的跨轮恢复入口。
- 不增加数据库表或迁移，除非后续获得用户单独批准。
- 不替换当前 MySQL、Chroma、BM25F、BGE-M3 或 RRF 混合检索架构。
- 不删除 `FAILED_CLOSED` 安全语义。
- 不允许未审核证据直接进入最终事实回答。

### 2.3 允许的小范围修改

允许的改动仅限：

- 调整现有路由规则、taxonomy cue、Knowledge Need 校验和语义分类结果的确定性约束。
- 扩展现有 Fast Planner 的覆盖范围。
- 在现有模型调用封装内补充有界重试、空输出识别和错误码规范化。
- 在现有 Knowledge Orchestrator 内补充 Grader 重试、失败诊断和候选保留。
- 调整现有 Query Rewrite、Top-K、融合、重排或 evidence policy 的局部规则。
- 扩充 trace/turn metrics 中的阶段诊断，不保存 Prompt 或知识正文。
- 新增和修改单元测试、契约测试、目标 E2E 回归测试。
- 增加默认关闭或可回滚的配置开关。

## 3. 本次系统基线

基线运行：

```text
runId = 20260804T075118Z-f108fd0d
suite = e2e-ragas
profile = full
普通 case = 181
safety case = 10
```

本方案只使用该运行中的系统执行结果、路由结果、检索诊断、Turn Metrics 和最终回答分析系统问题。LLM-as-Judge 和 RAGAS 自身的 Provider、超时、结构化输出问题不属于本文改造范围。

### 3.1 知识链路漏斗

151 条按数据集定义应使用知识库回答的 case，实际结果如下：

| 阶段 | 数量 | 占比 |
|---|---:|---:|
| 未触发知识检索 | 46 | 30.5% |
| 生成或混合检索运行失败 | 34 | 22.5% |
| 最终候选未命中标注 reference chunk | 27 | 17.9% |
| 候选全部被 Evidence Grader 过滤 | 20 | 13.2% |
| 命中 reference 后又被过滤 | 5 | 3.3% |
| 最终保留标注 reference 作为可用证据 | 19 | 12.6% |

说明：reference 命中采用严格 chunk ID 口径。相邻 chunk 或同文档等价证据可能被记为未命中，因此后续测试必须同时记录 chunk-level 和 document-level 命中，不得只优化一个数字。

### 3.2 系统调用稳定性

| 调用阶段 | 调用数 | 失败数 | 当前现象 |
|---|---:|---:|---|
| `safety.complete` | 170 | 15 | 低风险请求也承担较高远程调用成本 |
| `knowledge.knowledge_plan_v2` | 24 | 22 | 大量依赖 deterministic fallback |
| `knowledge.evidence_grade_v2` | 87 | 7 | 失败后会进入 FAILED_CLOSED |
| Evidence Grader repair | 22 | 3 | repair 仍可能耗尽约 60 秒 |
| `response.generate` | 122 | 14 | 出现空回复、ERROR 和 PROVIDER_EOF |

普通 case 中存在：

- 22 条空回复。
- 34 条明确返回“本轮知识核验流程未能可靠完成”。
- 48 条包含“暂时没有检索到能够核实”的降级话术。

### 3.3 延迟基线

```text
单轮 P50 = 55.1 秒
单轮 P95 = 123.6 秒
单轮最大值 = 172.6 秒
向量检索 P50 = 5.1 秒
Safety 调用 P50 = 13.4 秒
Evidence Grader P50 = 24.0 秒
Response Generate P50 = 25.6 秒
```

### 3.4 不得破坏的已有质量

当前独立路由集结果：

```text
Route Accuracy = 96.7%
Route Macro-F1 = 95.8%
Risk Recall = 100%
High Risk Miss Count = 0
```

任何改造不得使 Risk Recall 低于 100%，不得产生新的高风险漏判。

## 4. 根因与改造边界

### 4.1 P0：学校制度请求没有进入 KnowledgeAgent

当前 `_execution_contract()` 将 `KNOWLEDGE_GUIDANCE` 固定映射为：

```text
executionMode = RESPONSE_ONLY
knowledgeNeed = NONE
```

这会使以下请求绕过知识库：

- “毕业退宿怎么交钥匙？”
- “大学生医保依据学校哪份办法？”
- “大学生医保缴费标准是多少？”
- “学生人事档案如何管理？”
- “学生体质健康标准如何实施？”

这些请求虽然被语义模型归为 `KNOWLEDGE_GUIDANCE`，但文本和 taxonomy 已明确包含校内制度、费用、材料、办理流程或官方规定信号。

改造边界：

- 不改变 `UnderstandingAgent`。
- 不新增路由 Agent。
- 只在 `app/agents/routing.py` 的确定性分类和 semantic contract 校验中增加权威本地信息保护规则。
- 继续允许纯通用建议走 `RESPONSE_ONLY`，例如普通时间管理、情绪陪伴和职业选择。
- 高风险优先级保持不变。

### 4.2 P0：模型调用失败被串行放大

Safety、Knowledge Planner、Evidence Grader 和 Response Generate 均依赖当前模型调用封装。任一阶段失败都可能造成空回复或 FAILED_CLOSED。

改造边界：

- 不更换模型供应商。
- 不建立新模型网关。
- 只在现有 `AiClient`、structured completion 和生成服务中统一错误码、补充有界重试。
- Streaming 已经输出可见内容后禁止整轮重试，防止重复回答。
- 只有在零可见 token、请求幂等且仍在 Turn Budget 内时，允许一次重试。

### 4.3 P0：Evidence Grader 故障被表现为“没有资料”

典型 case `ragas-appeal-materials-13`：

```text
reference chunk = knowledge:5829
retrieved candidates 包含 knowledge:5829
Evidence Grader Provider 调用失败
usable evidence 被清空
最终回复声称没有检索到可核实资料
```

当前 fail-closed 原则必须保留，但必须区分：

- `NO_RETRIEVAL_RESULT`
- `EVIDENCE_INSUFFICIENT`
- `EVIDENCE_GRADER_UNAVAILABLE`
- `EVIDENCE_POLICY_REJECTED`

改造边界：

- Grader 不可用时仍不得用未审核证据回答事实。
- 允许在原 Turn Budget 内进行一次相同输入的有界重试。
- 重试失败后仍保持 FAILED_CLOSED，但不得把原因伪装成“知识库没有资料”。
- 在现有 `knowledge_evidence` payload 和 turn metrics 中做加法式诊断扩展，不新增 Artifact kind。
- trace 只保存错误码、chunk ID/hash、数量和阶段，不保存候选正文。

### 4.4 P1：候选召回和最终保留不足

改造必须先用失败 case 建立回归集，再调整参数。禁止直接全局提高 Top-K 后宣称修复。

需要分别验证：

1. Query Spec 是否识别制度名称、校区、材料、费用、期限和办理动作。
2. Query Rewrite 是否保留用户原始核心实体。
3. BM25、向量召回中目标文档是否出现。
4. RRF/重排后目标文档和目标 chunk 排名。
5. hard filter 是否因 scope、source type、freshness 或 facet 错误删除有效内容。
6. Grader 是否把已支持的部分事实错误降级为 NONE。

第一阶段禁止修改 chunking 和重新灌库。只有在 document-level 已命中、chunk-level 长期无法命中的证据充分时，才允许单独提出 chunking 变更方案并等待用户批准。

### 4.5 P1：空回复和错误终止

系统必须保证任何正常 HTTP/Chat turn 都有可见、可解释的终态内容。

需要区分：

- Provider 返回 STOP 但 content 为空。
- Streaming 在首 token 前 EOF。
- Streaming 已输出部分内容后 EOF。
- ModelProtocolError。
- 生成完成但 finalize 阶段丢失 content。

改造边界：

- 零 token 时允许一次有界重试。
- 已有部分内容时不得整轮重放；应保留已验证部分或进入现有失败回复。
- 重试仍为空时返回现有安全降级文案，禁止返回空字符串。
- 不改变 `ResponseAgent` 的职责和 Coordinator 最终采纳关系。

### 4.6 P2：串行延迟过高

本轮不引入并行 Agent 执行或新异步架构。延迟优化只能来自：

- 提高 Fast Planner 覆盖率，减少不必要的 LLM Planner 调用。
- 避免相同输入的重复 schema repair。
- 为每个现有阶段设置可观测的预算和提前停止条件。
- 对确定性足够的低风险分类复用已有规则结果，但 SafetyAgent 必须仍执行并发布风险 Artifact。
- 减少无效 Query Rewrite 和重复 Evidence Grade。
- 缓存仅限同一输入、同一模型、同一 Prompt 版本和同一知识索引签名，不得跨版本复用。

## 5. 分阶段实施计划

## 阶段 0：冻结基线和建立目标回归集

### 目标

把本轮失败模式转成稳定、低成本、可重复运行的测试，不依赖全量外部 Judge。

### 修改范围

- `tests/test_turn_plan_routing.py`
- `tests/test_routing_integration.py`
- `tests/test_knowledge_retrieval_regressions.py`
- `tests/test_knowledge_orchestrator.py`
- `tests/test_chat_completion.py`
- 可新增 `tests/fixtures/e2e_system_quality_regressions.json`

### 必须覆盖的 case

知识触发：

```text
ragas-housing-checkout-09
ragas-medical-policy-39
ragas-medical-cost-43
ragas-archive-policy-86
ragas-water-electricity-87
ragas-physical-health-88
```

Evidence Grader 故障：

```text
ragas-appeal-materials-13
ragas-suspension-eligibility-23
ragas-housing-wrong-scope-90
ragas-credits-graduation-check-131
ragas-library-overdue-policy-138
```

检索/过滤：

```text
ragas-appeal-synonym-15
ragas-discipline-policy-18
ragas-scholarship-materials-30
ragas-safety-policy-85
ragas-network-account-sharing-154
```

空回复：

```text
ragas-housing-change-materials-01
ragas-appeal-steps-14
ragas-panic-breathing-67
ragas-career-transition-options-97
ragas-suspension-health-flow-128
```

### 测试要求

- Fixture 只保存用户问题、预期执行模式、预期知识需求、预期文档 key、预期结果类型和允许错误码。
- 不在 fixture 中复制真实密钥、Prompt 或大段知识正文。
- 单元测试必须使用 fake client/fake knowledge；检索回归可使用现有测试数据库或明确的 fixture corpus。
- 阶段 0 的新增测试应在生产代码修改前失败，证明能够复现问题。

## 阶段 1：修复权威本地信息的 Knowledge Need

### 目标

让学校制度事实稳定进入 `KnowledgeAgent`，同时不把普通建议全部送入知识库。

### 建议实现

在现有 routing 模块内增加单一、可测试的确定性保护函数，例如：

```python
def requires_authoritative_local_knowledge(text, contextual_text, primary_domain) -> bool:
    ...
```

该函数必须复用：

- `get_knowledge_taxonomy().build_query_spec(...)`
- `spec.requires_authoritative_local_info`
- 已识别 concept
- 现有 institutional action/fact cues

规则优先级：

```text
HIGH_RISK > 明确 Clarification Scope 缺失 > 权威本地信息 > 通用建议
```

如果语义分类返回 `KNOWLEDGE_GUIDANCE + RESPONSE_ONLY + NONE`，但 deterministic spec 要求 authoritative local info，则将其约束为：

```text
taskKind = INSTITUTIONAL_FACT
executionMode = KNOWLEDGE 或必要时 CLARIFY
knowledgeNeed = REQUIRED
```

不得仅通过扩大一个宽泛关键词列表实现。必须同时满足本地权威信息信号或 taxonomy concept，防止“怎么办”一词把所有心理支持问题送入知识库。

### 验收

- 上述 6 条知识触发 case 全部进入 Knowledge。
- 普通学习计划、时间管理、情绪陪伴、职业选择仍为 RESPONSE_ONLY。
- 原 180 条路由集 Route Accuracy 不低于 96.0%。
- Risk Recall 保持 100%，High Risk Miss Count 保持 0。

## 阶段 2：统一系统模型错误码和空输出恢复

### 目标

消除空字符串终态，确保可重试故障有一致错误码和有界恢复。

### 修改范围

- `app/services/ai.py`
- `app/services/model_completion.py`
- `app/services/chat.py`
- `app/services/turn_execution.py`
- `tests/test_ai_completion.py`
- `tests/test_ai_structured_completion.py`
- `tests/test_chat_completion.py`
- `tests/test_turn_metrics.py`

### 实现要求

统一至少以下错误：

```text
PROVIDER_REQUEST_FAILED
PROVIDER_EOF
PROVIDER_EMPTY_OUTPUT
STRUCTURED_OUTPUT_INVALID
MODEL_PROTOCOL_ERROR
TURN_BUDGET_EXCEEDED
```

重试条件必须同时满足：

1. 尚未向用户发布任何可见 token。
2. 当前调用类型允许幂等重试。
3. Turn Budget 未耗尽。
4. 错误属于明确的 retryable set。
5. 最多重试一次，除非现有配置更严格。

如果 Provider 返回 STOP 但 content 为空，必须视为 `PROVIDER_EMPTY_OUTPUT`，不得记录为成功 completion。

### 验收

- Fake Provider 的 empty STOP、EOF-before-token、timeout 和 malformed response 均有独立测试。
- 空输出重试成功时只保存一次最终消息。
- 空输出重试失败时返回非空降级响应。
- partial stream 不重复输出内容。
- Turn Metrics 不再使用无法定位的通用 `ERROR` 作为主要错误码。

## 阶段 3：提高 Fast Planner 覆盖率

### 目标

减少 `knowledge_plan_v2` 远程调用和失败，但保留 Knowledge Planner 及其 fallback 架构。

### 修改范围

- `app/services/knowledge_agent/fast_planner.py`
- `app/services/knowledge_agent/policy.py`
- `app/knowledge/retrieval_taxonomy.yaml`
- `tests/test_knowledge_fast_planner.py`
- `tests/test_knowledge_planner.py`
- `tests/test_knowledge_policy.py`

### 实现要求

Fast Planner 只扩展到可以确定性表达的单一制度事实：

- MATERIALS
- STEPS
- CHANNEL
- DEADLINE
- PROCESSING_TIME
- COST
- ELIGIBILITY
- POLICY_BASIS
- CONTACT

复合问题、指代不明、校区范围缺失和多 domain 请求继续使用现有 LLM Planner 或 Clarification。

Fast Planner 输出必须继续经过 `KnowledgePolicy.validate_plan()`，不得绕过 policy。

### 验收

- 固定制度事实回归 case 不调用 `knowledge_plan_v2`。
- 复合问题和必要澄清仍走原有 Planner/Clarification。
- Agent、Artifact 和事件顺序不变。

## 阶段 4：Evidence Grader 故障隔离和诊断

### 目标

保留 fail-closed，同时避免把 Grader 服务故障描述成知识库无资料。

### 修改范围

- `app/services/knowledge_agent/orchestrator.py`
- `app/services/knowledge_agent/evidence_grader.py`
- `app/services/knowledge_agent/models.py`
- `app/services/knowledge_agent/policy.py`
- `app/agents/autonomous.py`
- `tests/test_knowledge_evidence_grader.py`
- `tests/test_knowledge_orchestrator.py`
- `tests/test_event_driven_runtime_evidence.py`

### 实现要求

允许对现有 payload 做加法式扩展：

```json
{
  "status": "FAILED_CLOSED",
  "stop_reason": "GRADER_FAILED",
  "failure_stage": "evidence_grade_v2",
  "failure_code": "PROVIDER_REQUEST_FAILED",
  "retrieval_succeeded": true,
  "candidate_count": 4,
  "retry_count": 1
}
```

约束：

- 不加入候选正文。
- 不把未审核候选加入 `local_evidence`。
- Grader 一次重试成功后必须继续走现有 Policy 校验。
- Grader 最终失败后 ResponseAgent 应输出“证据审核服务暂时不可用，请稍后重试”，而不是“知识库没有资料”。
- `NO_EVIDENCE`、`POLICY_REJECTED` 和 `GRADER_FAILED` 使用不同用户文案和 trace code。

### Evidence Policy 校准

对 11 条 Grader 正常完成但全部过滤的 case，逐条核对：

- required facets 是否正确。
- hard filter 后是否仍有官方来源。
- Grader claim 是否引用 allowed chunk。
- Policy 是否因未要求的 facet 将 PARTIAL 降为 NONE。
- scope/freshness/source type 是否被错误解释。

每修一类规则必须添加对应测试。禁止整体放宽 grade 阈值。

## 阶段 5：检索召回增量优化

### 目标

优先提升目标文档召回和有效 chunk 保留，不改变混合检索架构。

### 修改范围

- `app/services/knowledge.py`
- `app/services/knowledge_query.py`
- `app/services/knowledge_agent/query_rewriter.py`
- `app/knowledge/retrieval_taxonomy.yaml`
- `tests/test_knowledge_query.py`
- `tests/test_knowledge_query_rewriter.py`
- `tests/test_knowledge_retrieval_regressions.py`
- `tests/test_rag_eval_metrics.py`

### 实施顺序

1. 先输出目标 case 的 BM25、vector、RRF 和最终候选排名摘要。
2. 判断问题属于 query、taxonomy、scope filter、fusion、rerank 还是 final Top-K。
3. 每次只调整一个变量。
4. 对命中提升和误召回同时写断言。
5. 在固定回归集通过后，再运行完整 retrieval suite。

### 允许的局部调整

- 为明确制度别名增加 taxonomy synonym。
- Query Rewrite 保留学校、校区、制度名、材料、费用、期限等强实体。
- 对 authoritative document key 提供小幅、可解释的 rerank boost。
- 在不增加 Prompt 正文的前提下记录各阶段目标文档排名。
- 小幅调整 final candidate Top-K，但必须记录延迟和 precision 变化。

### 禁止事项

- 不删除 BM25、vector 或 RRF 任一分支。
- 不把全量 Top-K 无限制增大。
- 不为测试 case 硬编码 chunk ID。
- 不将 reference answer 或 reference context 注入生产查询。
- 不因某个 case 失败重新灌库或修改文档正文。

## 阶段 6：最终回答非空保证和降级文案

### 目标

任何系统 turn 都必须返回非空、与真实失败阶段一致的响应。

### 修改范围

- `app/services/chat.py`
- `app/services/chat_turns.py`
- `app/agents/autonomous.py`
- `app/services/turn_execution.py`
- `tests/test_chat_completion.py`
- `tests/test_chat_turns.py`
- `tests/test_event_driven_runtime_evidence.py`

### 文案语义必须区分

```text
检索无结果：当前知识库未检索到可核实资料。
证据不足：已找到相关资料，但不足以支持所问事实。
证据审核故障：已完成检索，但证据审核服务暂时不可用。
生成服务故障：生成服务暂时不可用，请稍后重试。
范围缺失：仅在确实缺少用户可提供的校区、事项等范围时澄清。
```

不得把系统故障写成用户信息不足，不得要求用户重复提供已经明确给出的校区或事项。

### 验收

- 所有目标空回复 case 返回非空终态。
- Grader failure 文案不再声称“没有检索到”。
- 已明确校区的 case 不再次询问校区。
- PARTIAL evidence 继续保留已核实事实并明确缺口。

## 阶段 7：性能与可观测性收尾

### 目标

不改变 Agent 架构的前提下减少无效模型调用，并让每次失败可以定位到具体阶段。

### 修改范围

- `app/services/turn_metrics.py`
- `app/services/trace.py`
- `app/services/knowledge_agent/orchestrator.py`
- `tests/test_turn_metrics.py`
- `tests/test_turn_metrics_harness.py`
- `tests/test_trace_privacy.py`

### 必须记录

- stage/purpose
- provider/model
- status
- normalized error code
- retry count
- duration
- candidate count
- usable evidence count
- plan source：FAST、LLM 或 DETERMINISTIC_FALLBACK
- stop reason

不得记录：

- API key
- Authorization header
- Prompt 正文
- 用户原文
- 知识正文
- 模型完整响应正文

## 6. 文件级变更地图

| 文件/模块 | 允许改动 | 禁止改动 |
|---|---|---|
| `app/agents/routing.py` | authoritative local guard、cue 校准、contract validation | 重写路由架构、删除 semantic fallback |
| `app/agents/autonomous.py` | 失败文案、加法式 manifest 诊断 | 删除或合并 Agent |
| `app/agents/event_driven_runtime.py` | 原则上不改；仅必要的加法式字段透传 | 改事件循环、Agent 注册和执行模型 |
| `app/agents/coordinator.py` | 原则上不改；只允许诊断透传 | 改 Coordinator 仲裁职责 |
| `app/services/ai.py` | 错误码、空输出检测、有界重试 | 新建 Provider 层或替换供应商 |
| `app/services/knowledge_agent/fast_planner.py` | 扩展确定性制度问题覆盖 | 替代 Knowledge Planner |
| `app/services/knowledge_agent/orchestrator.py` | Grader retry、failure reason、诊断 | 新建 Orchestrator 或绕过 Policy |
| `app/services/knowledge_agent/policy.py` | 基于失败测试的局部校准 | 全局放宽证据要求 |
| `app/services/knowledge.py` | query/fusion/rerank 参数的小幅调整 | 替换混合检索架构 |
| `app/services/chat.py` | 非空终态和失败语义 | 绕过 ResponseAgent/Coordinator |
| `tests/**` | 新增单元、契约、回归测试 | 用 mock 分数冒充真实全量结果 |

## 7. 测试策略

### 7.1 每阶段测试顺序

```text
失败单测
  -> 对应生产代码修复
  -> 目标单测
  -> 相邻模块测试
  -> 目标 E2E case
  -> routing/retrieval 回归
  -> full E2E（最后且需人工确认）
```

### 7.2 推荐测试命令

阶段 1：

```powershell
python -m pytest tests/test_turn_plan_routing.py tests/test_routing_integration.py -q
```

阶段 2：

```powershell
python -m pytest tests/test_ai_completion.py tests/test_ai_structured_completion.py tests/test_chat_completion.py tests/test_turn_metrics.py -q
```

阶段 3 和 4：

```powershell
python -m pytest tests/test_knowledge_fast_planner.py tests/test_knowledge_planner.py tests/test_knowledge_evidence_grader.py tests/test_knowledge_orchestrator.py tests/test_knowledge_policy.py -q
```

阶段 5：

```powershell
python -m pytest tests/test_knowledge_query.py tests/test_knowledge_query_rewriter.py tests/test_knowledge_retrieval_regressions.py tests/test_rag_eval_metrics.py -q
```

阶段 6 和 7：

```powershell
python -m pytest tests/test_chat_completion.py tests/test_chat_turns.py tests/test_event_driven_runtime_evidence.py tests/test_turn_metrics.py tests/test_turn_metrics_harness.py tests/test_trace_privacy.py -q
```

### 7.3 目标 E2E 运行

必须先逐 case 运行，不得一开始执行 full：

```powershell
python -m app.evaluation.runner --suite e2e-ragas --profile full --case <case-id> --no-gate
```

每个阶段至少覆盖该阶段列出的 5 个代表 case。目标 case 全部通过系统契约后，再运行 smoke/contract；全量运行必须最后执行。

## 8. 验收标准

### 8.1 硬门禁

以下任一项不满足，不得宣称完成：

- 所有现有 Agent 保留。
- EventDriven Coordinator 和 Blackboard 协议未改变。
- Risk Recall = 100%。
- High Risk Miss Count = 0。
- 不允许未审核证据进入最终事实回答。
- 所有目标空回复 case 返回非空终态。
- Grader 故障与无检索结果使用不同错误码和文案。
- 中文源码和测试保持 UTF-8 可读文本。

### 8.2 阶段性系统目标

以下目标用于判断优化是否有效，不允许通过删除 case 或修改 reference 达成：

| 指标 | 当前基线 | 第一阶段目标 |
|---|---:|---:|
| 应使用知识的 case 触发 Knowledge | 105/151 | >= 135/151 |
| 严格 reference candidate hit | 32/117 | >= 55/117 |
| 严格 reference usable hit | 19/117 | >= 40/117 |
| 普通 case 空回复 | 22/181 | <= 3/181 |
| “知识核验流程未可靠完成” | 34/181 | <= 10/181 |
| 单轮 P50 | 55.1 秒 | <= 35 秒 |
| 单轮 P95 | 123.6 秒 | <= 90 秒 |

说明：如果 document-level hit 已明显提升而 chunk-level hit 仍低，应如实报告两者，不得为了满足数字修改 reference chunk。

## 9. 配置与回滚

如确需增加配置，只允许增加少量、默认保守的开关：

```text
KNOWLEDGE_AUTHORITATIVE_ROUTE_GUARD_ENABLED=true
AI_EMPTY_OUTPUT_RETRY_ENABLED=true
KNOWLEDGE_GRADER_RETRY_ENABLED=true
```

要求：

- 开关名称可根据现有命名规范调整。
- 默认值不得降低安全性。
- 关闭开关后必须恢复到改造前行为。
- 不允许为每个 case 增加单独开关。
- 不允许通过环境变量硬编码测试答案、chunk ID 或文档 key。

## 10. 后续 AI 执行纪律

后续 AI 每个阶段必须输出：

1. 本阶段根因。
2. 修改文件列表。
3. 新增或更新的测试。
4. 实际执行的测试命令和结果。
5. 指标前后对比。
6. 未解决问题。
7. 是否触碰架构冻结边界。

后续 AI 不得：

- 看到大量失败后直接重写 `autonomous.py`、`coordinator.py` 或 `event_driven_runtime.py`。
- 新增一个所谓“E2EAgent”“RagasAgent”或“RecoveryAgent”。
- 把 SafetyAgent、KnowledgeAgent 或 ResponseAgent 合并。
- 用新的固定流水线替换 EventDrivenCoordinator。
- 删除 FAILED_CLOSED。
- 把 Provider 故障当作无证据。
- 为提高通过率降低 evidence threshold。
- 修改数据集 expected action、reference 或 forbidden claim 来适配当前输出。
- 只改 Prompt、不补测试便宣称完成。
- 在一个阶段中同时大范围修改路由、检索、生成和 UI。

## 11. 停止条件

发生以下情况时，执行 AI 必须停止当前阶段并报告，不得自行扩大范围：

- 修复要求新增、删除或合并 Agent。
- 修复要求改变事件循环或 Artifact 主协议。
- 修复要求数据库迁移。
- 修复要求重新灌库或改变 chunking。
- 修复可能降低 Risk Recall 或放宽高风险处置。
- 修复需要更换模型供应商或 embedding 模型。
- 目标测试与现有安全测试发生无法协调的契约冲突。
- 工作区中用户改动与计划修改发生不可安全合并的冲突。

## 12. 推荐实施顺序

严格按以下顺序执行：

```text
冻结基线与失败测试
  -> Knowledge Need 权威信息保护
  -> 模型错误码与空输出恢复
  -> Fast Planner 覆盖
  -> Evidence Grader 故障隔离
  -> 检索召回回归优化
  -> 非空终态与降级文案
  -> 性能和可观测性
  -> 目标 E2E
  -> routing/retrieval 全回归
  -> 人工确认后执行 full E2E
```

该顺序不能颠倒。先修评测分数、后修系统运行错误，会继续得到不可解释的结果；先扩大召回、后修 Knowledge Need，则大量请求仍不会进入 KnowledgeAgent。

## 13. 完成定义

只有同时满足以下条件，才可以宣布本轮增量优化完成：

1. 全部现有 Agent、Coordinator、Blackboard 和事件驱动架构保留。
2. 目标制度事实请求稳定进入 KnowledgeAgent。
3. 空输出得到有界重试或非空降级响应。
4. Evidence Grader 故障不再伪装成“没有资料”。
5. 检索失败可定位到 query、召回、融合、过滤或 grader 阶段。
6. 目标回归 case 全部通过。
7. 路由和高风险硬门禁无回归。
8. 指标改善来自生产代码行为，不来自修改测试答案或降低门禁。
9. 所有改动有对应测试和实际执行记录。
10. 全量 E2E 由用户明确授权后执行，并保留完整报告。

本文的目标不是把 MindBridge 改造成另一套系统，而是在现有多 Agent、事件驱动和严格证据边界内，修复知识触发、调用稳定性、证据审核、检索召回和空回复这几个已经被真实 E2E 数据证明的问题。
