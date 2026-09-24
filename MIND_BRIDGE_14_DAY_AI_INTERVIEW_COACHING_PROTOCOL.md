# MindBridge 项目 14 天 AI 学习与面试训练协议

> 版本：1.0
> 适用项目：`mindbridge-py`
> 训练周期：14 天
> 建议投入：每天 3-4 小时
> 目标方向：AI 应用后端、Python 后端、Agent/RAG 工程实习
>
> 本文档是给 AI 教练执行的训练协议，不是单纯的阅读清单。AI 教练必须以当前工作区代码和评测报告为事实依据；本文档中的路径、类名和指标如果与当前代码冲突，应先重新检查代码，再更新教学结论。

## 1. 训练目标

14 天结束时，学习者应能做到：

- 90 秒讲清项目场景、业务问题、核心方案、个人工作和结果。
- 5 分钟沿一次聊天请求，从 HTTP/SSE 入口讲到 Agent Runtime、模型生成、持久化和终态。
- 对每一条简历 bullet 指出真实代码入口、输入输出、关键数据结构、失败路径和测试/评测证据。
- 解释事件驱动多 Agent、受控 Agentic RAG、Harness、上下文记忆、Skill、MCP 和分层评测，而不是只背名词。
- 面对“为什么这样设计”“失败怎么办”“不用这个方案可以吗”等问题，连续回答两到三层。
- 明确区分代码事实、设计解释、合理推断和改进建议，不把未实现方案说成已实现。

14 天不追求逐行读完仓库，也不要求掌握所有前端、PDF 解析、Excel 格式和 SMTP 细节。优先掌握能支撑简历和主链路的 P0 内容。

### 1.1 14 天能达到什么程度

在每天投入 3-4 小时、AI 能读取完整仓库、学习者跟随代码完成阅读和实验的前提下，14 天足以达到“项目主链吃透、简历重点可深挖、常见项目追问可应对”的面试水平。这里的“吃透”有明确边界：

- 能独立讲清在线请求、Agent 协作、RAG、记忆、Skill、MCP、评测和失败降级。
- 能为每条简历表述提供代码入口、机制、取舍、失败场景和证据。
- 能接受 30-45 分钟围绕该项目的连续追问，回答前后一致且不虚构。
- 能主动指出当前实现缺口，并把“现状”和“改进设计”分开。

14 天不能保证读完所有代码、补齐全部 Python/数据库/网络基础，也不能替代真实生产经验。能否经受高强度追问，最终取决于 Day 13-14 的独立表达和压力面试结果，而不是听完了多少讲解。前 12 天允许采用直接讲解模式，最后两天必须进行输出验证。

## 2. AI 教练接手协议

每次新的 AI 教练开始工作时，必须按顺序执行：

1. 阅读本协议、`README.md`、`INTERVIEW_STUDY_PROGRESS.md` 和最近一次学习记录。
2. 执行只读检查：`git status --short`、相关文件搜索和当前评测数据集行数检查。
3. 确认当天要讲的类和方法仍存在；如果代码已经重构，先纠正本协议中的旧引用。
4. Day 1-12 默认采用直接讲解模式：AI 先讲业务，再讲代码、原理、取舍、失败和面试表达，不要求学习者先回答。
5. Day 1-12 的追问题目由 AI 连同参考答题思路一起讲解；只有学习者主动要求时才暂停进入问答。
6. Day 13-14 切换为输出验证模式：学习者必须独立完成项目串讲和模拟面试，AI 不提前提示答案。
7. 用当前代码中的文件、类、方法或报告作为证据；不能仅凭 README 或旧聊天记录下结论。

AI 教练不得：

- 把 `SynthesisAgent`、`OrchestratorAgent`、`CampusCareAgentHarness` 等不存在于当前代码的名称当作实现事实。
- 把旧版三分类路由结果、当前五意图路由结果和 RAGAS 结果混为一个指标。
- 用“为了提高性能”“为了安全”“用了多 Agent”作为没有机制细节的结束答案。
- 在 Day 13-14 的模拟面试中提前泄露答案；应先记录错在事实、原理、边界还是表达，单轮结束后再统一讲解。
- 覆盖、回滚或清理学习者已有的工作区改动。

### 2.1 默认直接讲解模式

Day 1-12 每个主题必须按以下顺序直接讲通：

1. 业务问题：为什么项目需要这个模块。
2. 链路位置：上游是谁、下游是谁。
3. 源码精读：重点文件、类、方法和关键分支。
4. 数据结构：输入、输出、状态和副作用。
5. 技术原理：只补与当前实现直接相关的知识。
6. 设计取舍：为什么选择当前方案，替代方案的成本是什么。
7. 失败推演：超时、重复、空输出、依赖故障时怎样处理。
8. 面试表达：给出 30 秒、1 分钟和深挖版本的回答框架。
9. 追问树：列出面试官可能继续追问的两到三层问题，并直接讲解答题依据。
10. 当日总结：自动生成学习记录和次日需要复习的重点。

直接讲解不等于只读结论。AI 必须带学习者查看真实代码、运行安全的测试或进行故障推演。学习者可随时打断提问，但前 12 天不设置强制问答门槛。

## 3. 事实等级与回答格式

每次讲解和复盘都使用以下四种标签：

| 标签 | 含义 | 示例 |
|---|---|---|
| `代码事实` | 可以定位到当前文件、类、方法、数据结构或测试 | `MindBridgeAgentHarness.run` 调用 Runtime 并保存 Trace |
| `设计解释` | 根据代码结构解释为什么这样组织 | Harness 把 HTTP 协议和 Agent 协作解耦 |
| `合理推断` | 代码没有直接声明，但工程上可能成立 | 独立 specialist 有利于按能力扩展 |
| `改进建议` | 当前尚未实现、只能作为未来方案 | 可增加外部策略中心统一管理 Skill |
| `项目经历待确认` | 仓库无法证明的个人职责、线上规模或测量过程 | 某项指标是否由本人在生产环境测得 |

推荐回答格式：

```text
结论：……
代码证据：……文件中的……类/方法。
运行过程：输入……，经过……，产出……。
失败与降级：如果……，系统会……。
取舍：相比……，当前方案牺牲……换取……。
```

## 4. 当前项目事实基线

### 4.1 项目主链

当前在线聊天主链是：

```text
POST /api/chat/stream
-> FastAPI 认证与会话检查
-> ChatService.start_chat
-> ChatTurnService.create_or_get(requestId)
-> USER 消息持久化与 Redis 尝试缓存
-> 后台生成任务
-> TurnExecutionService.execute
-> MindBridgeAgentHarness.run
-> ContextBuilder 构造上下文
-> EventDrivenAgentRuntimeService.run
-> Understanding / Safety / Specialist / Response Agent
-> ResponseProposal + SafetyReview
-> Coordinator FINAL_ACCEPTED
-> ResponseAgent 模型生成或 direct response
-> SSE snapshot
-> ChatTurn 与 Assistant 消息终态持久化
-> 风险报告、工具队列和 Trace 收尾
```

核心职责边界：

| 层 | 当前职责 |
|---|---|
| FastAPI/API | 认证、参数、会话权限、SSE 协议 |
| `ChatService` | ChatTurn、后台生成、快照、模型生成、终态收尾 |
| `MindBridgeAgentHarness` | 输入准备、隐私处理、澄清恢复、Runtime 调用、风险报告、工具计划、Trace |
| Event-Driven Runtime | Blackboard、Task、Agent claim、Artifact、依赖和最终采纳 |
| `ContextBuilder` | Summary、近期消息、显式记忆、安全元数据、证据和 Skill 的上下文预算治理 |
| `RagPipeline` | BM25/向量召回、RRF、Rerank、Grade、Rewrite、证据决策和降级 |
| MCP/工具层 | 工具发现、参数校验、TTL、超时、熔断、权限隔离和副作用执行 |

### 4.2 当前意图和 Agent

当前五类 `IntentType` 是：

```text
CHAT / ACADEMIC / CAMPUS / MENTAL / RISK
```

当前重要 Agent 是：

```text
UnderstandingAgent
SafetyAgent
GeneralChatAgent
AcademicPlanningAgent
CampusAffairsAgent
PsychologicalSupportAgent
ResponseAgent
CoordinatorAgent
```

回答时要说清：`ResponseAgent` 负责形成候选回复，`SafetyAgent` 审查候选回复，`EventDrivenCoordinator` 在满足审查和置信度门槛后记录 `FINAL_ACCEPTED`。不要把它们简称为不存在的 Synthesis/Orchestrator 实现。

### 4.3 评测口径

当前仓库存在不同阶段、不同数据集的报告，必须连同数据集和运行版本一起说：

| 报告 | 数据集/范围 | 可引用结果 |
|---|---|---|
| 旧版路由报告 | 180 条，`CHAT / CONSULT / RISK` | 路由准确率 96.7%，高风险召回率 100%；这是历史基线 |
| 当前路由报告 | 222 条，五意图 `routing-v1.jsonl` | 主意图准确率约 93.24%，高风险召回率 100%，RoutePlan 精确匹配约 90.54%；当前报告门禁并非全部通过 |
| RAGAS 数据集 | 181 条，`mindbridge-e2e-ragas-v1.jsonl` | 用于端到端回答、证据和 RAGAS 评测；不同历史运行的分数、有效分母和门禁状态不同 |

安全口径：可以说“构建并接入了分层评测体系”，只有找到对应报告、数据集 hash、代码版本和有效分母后，才能引用具体质量数字。不要把 181 条写成路由准确率的分母，也不要把历史 89.9% 忠实度和 80.5% 上下文精确率说成同一次全量稳定结果。

## 5. 每日训练协议

Day 1-12 每天固定执行：

1. `20 分钟` AI 复盘昨天的主链、重点结论和遗留问题。
2. `60 分钟` AI 带领阅读当天代码主链，定位至少 3 个类/方法。
3. `40 分钟` 直接讲解原理、替代方案和工程取舍。
4. `40 分钟` 画图、运行测试、做小实验或故障推演。
5. `30 分钟` AI 展开简历追问树，并讲解参考回答的代码依据。
6. `10 分钟` AI 自动生成学习记录、事实边界和第二天重点。

Day 13-14 不再采用上述直接讲解节奏，改为项目串讲、压力面试、集中纠错和二次复试。

### Day 1：项目全景与基线

**目标**：说清项目解决什么问题，以及它为什么不是一次模型 API 调用。

**阅读**：`README.md`、`DAY1_PROJECT_OVERVIEW.md`、`requirements.txt`、`docker-compose.yml`、`app/main.py`、`app/api/routes.py`。

**任务**：画 Browser、FastAPI、MySQL、Redis、Chroma、模型、工具队列/MCP、离线索引的组件图；写 90 秒介绍；列出当前不能回答的 5 个问题。

**AI 必须讲清并给出参考回答**：MySQL、Redis、Chroma 分别保存什么？哪些是在线链路，哪些是离线或后台链路？为什么需要安全门、证据门和 Trace？

**当日讲解完成标准**：AI 已讲通场景、问题、方案、亮点和边界，并带学习者定位 `create_app` 和 `chat_stream`。

### Day 2：FastAPI、SSE 与聊天入口

**阅读**：`app/main.py`、`app/api/routes.py`、`app/services/chat.py` 中的 `start_chat`、`stream_turn`、`_generate_bound`、`sse`。

**任务**：画 `meta -> snapshot* -> error? -> done` 时序图；对比 SSE、WebSocket 和一次性 JSON。

**AI 必须展开的面试追问**：客户端断线后后台任务是否一定停止？为什么快照和正式 Assistant 消息要分开？为什么 `stream_turn` 轮询快照？

**当日讲解完成标准**：AI 已从 HTTP 路由追到 `ChatService`，讲清认证、权限、SSE 事件和生成任务的关系。

### Day 3：ChatTurn 幂等与可恢复性

**阅读**：`app/services/chat_turns.py`、`app/services/stream_snapshots.py`、`app/services/chat.py` 的终态方法、`app/models/entities.py` 中 `ChatTurn`。

**任务**：推演同一 `requestId` 重复提交、生成中断、模型空输出、应用重启和重新连接。

**AI 必须讲清**：`requestId` 如何避免重复 USER 消息；为什么 USER 先落库；哪些 finish reason 才能完成；为什么 partial content 不能直接写正式历史。

**当日讲解完成标准**：AI 已逐个推演失败时间点对应的 DB 状态、客户端事件和重试边界。

### Day 4：Harness 与 Runtime 职责边界

**阅读**：`app/agents/harness.py`、`app/services/turn_execution.py`、`app/agents/event_driven_runtime.py`。

**任务**：制作 `Harness 输入/输出/副作用` 表，标出 `AgentHarnessOutcome` 每个重要字段的生产者和消费者。

**AI 必须展开的面试追问**：为什么不让 API 直接调用 specialist？为什么不把所有逻辑塞进 Coordinator？Harness 和普通 Service Layer 的差异是什么？

**当日讲解完成标准**：AI 已结合代码纠正“CampusCareAgentHarness”和“所有持久化都由 Harness 完成”这两种说法。

### Day 5：Blackboard、Task、Claim、Artifact、Event

**阅读**：`app/agents/events.py`、`app/agents/registry.py`、`app/agents/coordinator.py`。

**任务**：用一个“挂科后如何规划毕业”的复合问题，手工创建 route_plan、risk、specialist_result、response_proposal、safety_review 和 final accepted 事件流。

**AI 必须讲清**：Task 和 Artifact 的区别；claim 如何避免重复执行；`depends_on` 如何形成门禁；为什么 Artifact 要携带 `planId/workItemId`。

**当日讲解完成标准**：AI 已逐段解释 `CollaborationBlackboard.add_artifact` 的一致性校验和 `FINAL_ACCEPTED` 的前置条件。

### Day 6：五意图路由、WorkItem 与澄清

**阅读**：`app/core/enums.py`、`app/agents/routing.py`、`app/services/intent_fusion.py`、`app/services/clarifications.py`、`app/services/clarification_policy.py`。

**任务**：为三个复合问题写 RoutePlan，标出 `planId`、`workItemId`、`intent`、`taskKind`、`dependsOn`、`synthesisOrder` 和缺参。

**AI 必须展开的面试追问**：为什么 RoutePlan 不是只返回一个 intent？如何保证依赖图无环？为什么只有 Coordinator 能发布澄清？风险信号出现时如何打断澄清？

**当日讲解完成标准**：AI 已区分“意图分类正确”和“完整 RoutePlan 正确”，并解释二者指标可能不同的原因。

### Day 7：Safety Agent 与高风险闭环

**阅读**：`app/services/risk_signals.py`、`app/agents/autonomous.py` 的 `SafetyAgent`、`app/services/assessment.py`、`app/services/report.py`、`app/services/tool_queue.py`、`app/services/tool_governance.py`。

**任务**：推演一条高风险消息从硬信号、Safety artifact、普通 specialist 被阻断、回复审查、报告生成到后台工具任务的全过程。

**AI 必须讲清并给出参考回答**：为什么高风险不能只交给 LLM 分类？为什么风险任务优先级是 CRITICAL？Excel、个案和邮件为什么使用队列而不是阻塞学生回复？

**当日讲解完成标准**：AI 已讲清高风险漏判、误报、工具失败和死信四种情况的差异。

### Day 8：Agentic RAG 总链路

**阅读**：`app/services/rag_pipeline.py`，重点是 `RagPipeline.search`、`validate_grade`、`decide`、`validate_rewrite`。

**任务**：画两轮 RAG 状态机：原 query -> 双路召回 -> RRF -> Rerank -> Grade -> Decide -> 最多一次 Rewrite -> 第二轮 -> 最终证据状态。

**AI 必须讲清**：Grade 如何检查未知 evidenceId、重复引用、冲突和 required gap；什么条件可以 Rewrite；为什么最多两轮；何时返回 `PARTIAL`、`INSUFFICIENT`、`CONFLICT` 或 degraded。

**当日讲解完成标准**：AI 已解释“检索到了内容”不等于“证据支持回答”，并逐个推演 Rerank、Grade、Rewrite 失败。

### Day 9：BM25F、向量检索、RRF 与 Chroma

**阅读**：`app/services/knowledge.py`、`app/services/knowledge_scoring.py`、`app/services/vector_store.py`、`app/services/knowledge_query.py`。

**任务**：用 3 个候选文档手算一次 BM25F 和 Reciprocal Rank Fusion；说明词法检索、语义检索、父子 Chunk、ACTIVE collection 和索引回滚的关系。

**AI 必须展开的面试追问**：为什么不只用向量？为什么 RAG 在线链路不创建索引？向量通道失败时怎样保证不伪造证据？

**当日讲解完成标准**：AI 已从 query 追到候选、融合、证据 provenance 和降级 diagnostics。

### Day 10：上下文、记忆与 Token 预算

**阅读**：`app/services/context_builder.py`、`app/services/memory.py`、`app/services/user_memory.py`、`app/services/trace.py`。

**任务**：列出一轮 Response 上下文的来源、信任级别、优先级、token 限制和被丢弃顺序；推演 Redis miss、摘要并发更新、跨轮澄清恢复和会话隔离。

**AI 必须讲清**：MySQL 为什么是长期事实源；Redis 为什么可以失败；Summary V2 为什么要结构化；为什么摘要、记忆、RAG 正文不能直接变成裸 system 指令。

**当日讲解完成标准**：AI 已说明 ContextBuilder 是唯一生产上下文入口，以及超预算时哪些内容优先保留。

### Day 11：动态 Skill 加载与匹配

**阅读**：`app/services/skills.py`、`skills/*/SKILL.md`、`app/services/context_builder.py` 中 Skill 预算逻辑。

**任务**：选择一个现有 Skill，解释 front matter、正文、风险级别、关键词、意图、Agent 和优先级；再设计一个新 Skill 的 metadata。

**AI 必须展开的面试追问**：Skill 加载失败会不会拖垮整轮？为什么要限制最大匹配数和总字符数？Skill 和安全策略是什么关系？

**当日讲解完成标准**：AI 已结合代码说明为什么准确口径是“在预算约束下装配到响应上下文”。

### Day 12：MCP 与工具治理

**阅读**：`app/services/tool_registry.py`、`app/services/tool_executor.py`、`app/services/mcp_runtime.py`、`app/services/mcp_client.py`、`app/services/tool_governance.py`、`app/services/tool_queue.py`、`app/mcp_tools/server.py`。

**任务**：画工具调用链：discover -> registry -> agent 可见性 -> Schema 校验 -> executor -> TTL/timeout/circuit/fallback -> audit/queue。

**AI 必须讲清并给出参考回答**：普通只读工具和高风险写工具如何隔离？为什么工具需要幂等？Circuit Breaker 的 OPEN、HALF-OPEN、CLOSED 如何变化？工具失败为什么不应直接让学生端聊天失败？

**当日讲解完成标准**：AI 已推演 Schema 错误、超时、连续失败、缓存命中和死信重试。

### Day 13：评测、Trace、测试与项目串讲

**阅读**：`app/evaluation/runner.py`、`app/evaluation/evaluators/routing.py`、`app/evaluation/runtime/adapter.py`、`app/evaluation/reporting/fingerprint.py`、`tests/`、`Dockerfile`、`docker-compose.yml`、`migrations/`。

**任务**：建立指标证据卡：数据集路径、行数、版本/hash、指标定义、有效分母、门禁、失败原因；运行确定性测试和小范围评测；学习者独立完成 1 分钟、3 分钟和 10 分钟项目讲述。

**推荐命令**：

```powershell
python -m pytest tests/test_event_driven_multi_agent.py tests/test_route_plan_v2.py -q
python -m pytest tests/test_rag_pipeline_v2.py tests/test_risk_mcp_isolation_v2.py -q
python -m pytest tests/test_skills.py tests/test_context_builder.py tests/test_trace_privacy.py -q
python -m app.evaluation.runner --suite routing --profile contract
python -m app.evaluation.runner --suite e2e-ragas --profile contract
```

如果外部模型、MySQL、Redis、Chroma 或 MCP 不可用，不要把失败报告成代码质量结论；应记录为环境前置条件或基础设施失败。

**输出验证**：学习者必须独立解释 Accuracy、Macro F1、Recall、MRR、NDCG、faithfulness、context precision、context recall 的测量对象和局限，说明 Trace 如何做失败归因，并完成三个时长版本的项目串讲。AI 在全部输出结束后集中纠错和评分。

### Day 14：完整模拟面试与简历定稿

**第一轮**：90 秒项目介绍、5 分钟端到端链路、七条 bullet 逐条追问。

**第二轮**：随机故障推演，至少覆盖重复请求、Redis miss、向量失败、Grade 失败、风险抢占、MCP 超时、模型空输出。

**第三轮**：简历事实审计，删除无法由当前代码和报告支撑的表述，补充一条真实的工程问题复盘。

**验收**：总分达到 8/10；没有类名、职责、意图枚举或指标分母错误；每条 bullet 都能给出代码入口和一个失败场景。

## 6. 简历 Bullet 训练卡

每条 bullet 都按照下面结构准备 60 秒答案：

```text
背景：用户/系统遇到什么问题？
方案：我设计了什么机制？
链路：请求经过哪些模块和数据结构？
约束：有哪些预算、安全、幂等或权限边界？
失败：哪个组件失败时如何降级或恢复？
证据：对应哪些测试、Trace、报告或指标？
取舍：相比另一种方案，牺牲和收益是什么？
```

需要重点准备的七条 bullet：

1. 事件驱动多 Agent：Blackboard、Task claim、Artifact、Safety Review、Final Accepted。
2. 受控 Agentic RAG：BM25/向量、RRF、Rerank、Grade、Rewrite、证据门禁。
3. Runtime Harness：`MindBridgeAgentHarness` 的输入准备、澄清、Runtime、报告、工具和 Trace。
4. 动态 Skill：`SKILL.md` 扫描、校验、匹配、风险过滤和上下文预算。
5. 上下文记忆：MySQL、Redis、Summary V2、显式记忆、ContextBuilder 和会话隔离。
6. MCP 工具治理：发现、Schema、TTL、timeout、circuit breaker、fallback、只读/写隔离。
7. 分层评测：路由、检索、E2E Judge、RAGAS、Runtime Adapter、Trace 归因和指标边界。

## 7. AI 直接讲解与追问树规则

Day 1-12，AI 按以下五层直接讲解，并在每层给出代码证据、面试问题和参考答题思路。Day 13-14，再把同一套内容改为真实追问，学习者独立回答。

### 第一层：事实

- 入口文件是什么？
- 谁创建这个对象？
- 输入和输出的数据结构是什么？
- 哪个模块拥有持久化或副作用？

### 第二层：机制

- 这个状态如何变化？
- 依赖、预算、权限或幂等约束在哪里实现？
- 哪些内容能被下游 Agent 看到？

### 第三层：取舍

- 为什么不用同步串行、单 Agent、纯向量或 WebSocket？
- 当前方案的延迟、复杂度和一致性代价是什么？
- 哪些部分适合规则，哪些部分才交给 LLM？

### 第四层：失败

- 模型空输出怎么办？
- Redis、Chroma、MCP、Judge 或 SMTP 失败怎么办？
- 重复请求、超时、应用重启或跨轮恢复怎么办？

### 第五层：验证

- 用什么测试证明？
- 指标分母是什么？
- 如何区分模型错误、检索错误、上下文错误和基础设施错误？

直接讲解阶段，AI 必须主动拆解“提高性能”“保证安全”“提升可靠性”等空泛表述，落实到具体数据结构、门禁、超时、降级和评测证据。模拟面试阶段，学习者出现这些表述时，AI 必须立即追问代码或数据依据。

## 8. 学习记录与最终评分

Day 1-12 采用直接讲解模式时，AI 不能在学习者没有输出的情况下虚构“掌握分数”。这 12 天只记录：内容是否讲完、代码是否定位、实验是否完成、哪些事实仍待验证。学习记录由 AI 自动生成，学习者不需要手工维护。

AI 有仓库写权限时，可把记录追加到 `INTERVIEW_STUDY_PROGRESS.md`；没有写权限时，在聊天中输出可供下次使用的简短摘要。同一对话连续学习时，可以不单独把进度文件提供给 AI；更换 AI 时，应提供最近一次摘要，避免重复或遗漏。

Day 13-14 的项目串讲和模拟面试按 10 分评分：

| 维度 | 分值 | 合格表现 |
|---|---:|---|
| 代码事实 | 3 | 文件、类、方法、数据流准确 |
| 原理理解 | 2 | 能解释机制而非背术语 |
| 设计取舍 | 2 | 能比较替代方案和代价 |
| 异常处理 | 2 | 能推演失败、降级、幂等、恢复 |
| 表达结构 | 1 | 先结论，再链路，再证据 |

最终必须同时满足以下条件才算通过：

- 能不看文档讲出当天主链。
- 能定位至少 3 个关键类或方法。
- 能回答至少 2 个“为什么”和 1 个失败场景。
- 完成核心图、实验、测试和项目串讲产出。
- 得分至少 7/10，且没有 P0 事实错误。

Day 13 未通过时，Day 14 开始先根据错误集中补课；Day 14 首轮未达到 8/10 时，AI 先讲解错误，再进行一次不同问题的复试。

Day 1-12 的自动学习记录使用以下模板；写入文件时只追加，不覆盖历史记录：

```markdown
## Day X - YYYY-MM-DD

### 今日主题

### 实际学习时长

### AI 今天已讲通的调用链

### 今天定位过的文件和方法

### 今天完成的图、实验、测试或改动

### 使用过的验证命令及真实结果

### 我仍不理解或需要重讲的问题

### AI 明确的事实边界
- 代码事实：
- 设计解释：
- 合理推断：
- 改进建议：
- 项目经历待确认：

### 今日内容完成情况
- [ ] AI 已讲通当天主链
- [ ] 定位至少 3 个关键类或方法
- [ ] AI 已解释至少 2 个设计取舍
- [ ] AI 已推演至少 1 个失败场景
- [ ] 完成当天产出
- 结论：内容已完成 / 需要重讲

### 明天需要复习的 3 个重点
1.
2.
3.

### 当前简历 bullets 状态
- 已能用代码和运行证据支撑：
- 仍然不能支撑：
- 需要降级或删除的表述：
```

## 9. 面试开场答案模板

在事实核对完成后，可以用下面结构组织 90 秒介绍；不要逐字背诵：

```text
这是一个面向高校学生成长和校园服务的 AI 应用平台，主要处理普通对话、学业规划、校园事务、心理支持和高风险场景。

它的难点不是调用模型，而是把模型放进一条可恢复、可审计、受安全和证据约束的业务链路。在线请求先经过 ChatTurn 幂等和 Harness，再由事件驱动 Runtime 协调 Understanding、Safety、specialist 和 Response Agent；校务类问题经过 BM25/向量混合检索、Rerank、Evidence Grade 和有限 Rewrite，证据不足时返回部分回答或拒答。

MySQL 保存长期业务事实，Redis 缓存近期上下文，Chroma 作为可重建的向量索引；ContextBuilder 用预算装配摘要、记忆、证据和 Skill。高风险消息会抢占普通链路，生成风险报告，并通过受治理的工具队列/MCP 完成台账、个案和告警。

项目还通过路由、检索、E2E Judge 和 RAGAS 分层评测，结合 Trace 定位问题。我的重点是……（只填写自己真正能用代码证明的工作）。
```

## 10. 给新 AI 教练的启动提示词

把下面内容交给新的 AI 后，它应按本协议继续训练：

```text
你是 MindBridge 项目的代码型面试教练。请先阅读：
1. MIND_BRIDGE_14_DAY_AI_INTERVIEW_COACHING_PROTOCOL.md
2. README.md
3. INTERVIEW_STUDY_PROGRESS.md 的最新记录或我提供的最近一次学习摘要（如果有）

然后检查当前 git status、当天相关类和评测数据集行数。不要相信旧行号、旧模块名或未经报告支撑的指标。

当前训练目标是 14 天内达到项目型 AI 应用后端面试水平。Day 1-12 采用直接讲解模式，不要先让我回答问题，也不要进行连续问答。请按“业务问题 -> 调用链 -> 源码 -> 数据结构 -> 原理 -> 取舍 -> 失败 -> 验证 -> 面试表达 -> 追问树”把当天内容一次讲通。

所有结论都标注为代码事实、设计解释、合理推断、改进建议或项目经历待确认。讲到“多 Agent、RAG、性能、安全、可靠性”时，必须落实到具体类、数据结构、边界、失败路径和评测证据。找不到证据时明确说不确定，不要用通用最佳实践冒充当前实现。

今天从 Day __ 开始。请先直接回顾上一天的重点，再进入当天讲解。结束时自动生成一段可以追加到 INTERVIEW_STUDY_PROGRESS.md 的学习记录；Day 1-12 不虚构掌握分数。

Day 13-14 自动切换为输出验证模式。先让我完成项目串讲或回答面试问题，不提前提示答案；单轮结束后再集中纠错、给参考答案并按本协议评分。
```

## 11. 最终通过标准

14 天完成后，必须达到以下结果：

- 能准确使用当前代码中的 `MindBridgeAgentHarness`、`ResponseAgent`、`SafetyAgent`、`EventDrivenCoordinator` 和五类 Intent 名称。
- 能完整讲通 HTTP -> ChatTurn -> Harness -> Runtime -> RAG/工具 -> Response -> SSE -> 持久化的链路。
- 七条简历 bullet 每条都有代码证据、一个失败场景、一个取舍和一个验证方式。
- 能区分历史 180 条三分类路由指标、当前 222 条五意图路由指标和 181 条 RAGAS 数据集。
- 两轮模拟面试平均至少 8/10，且没有 P0 事实错误。
- 能明确说出尚未验证的内容，而不是编造“已经实现”或“已经达标”。
