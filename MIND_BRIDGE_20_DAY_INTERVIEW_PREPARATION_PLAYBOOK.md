# MindBridge 大厂日常实习 20 天项目面试训练手册

> 版本：1.0  
> 制定日期：2026-08-04  
> 项目目录：`mindbridge-py`  
> 训练周期：20 天，每天 3 小时，总计 60 小时  
> 目标岗位：AI 应用后端、Python 后端、Agent/RAG 方向日常实习

---

## 1. 文档用途

这不是一份阅读清单，而是一份可以交给任意 AI 教练执行的训练协议。

学生更换 AI 后，应把以下内容一起提供给新 AI：

1. 当前项目仓库。
2. 本文档。
3. 最近一天的学习记录和验收结果。
4. 学生目前能够独立讲出的内容，以及仍然答不出的追问。

新 AI 必须先阅读本文档，再检查当前仓库。不得仅凭本文档中的旧行号或旧结论教学，因为项目可能继续变化。代码事实以当前工作区为准，本文档负责规定学习目标、范围、方法和验收标准。

---

## 2. 最终目标与现实边界

### 2.1 20 天后的目标

学生应达到以下水平：

- 能在 90 秒内说清项目背景、核心问题、技术方案和个人工作。
- 能在 5 分钟内沿一次聊天请求讲清端到端调用链。
- 能脱离 README 解释事件驱动多 Agent、Harness、Skill、Agentic RAG、上下文记忆、SSE 和分层评测。
- 面试官对任一简历 bullet 连续追问 2 到 3 层时，能用具体类、数据结构、失败场景和设计取舍作答。
- 能在 1 分钟内定位核心代码入口，在 5 分钟内找到某项行为由哪个模块负责。
- 能解释至少一个自己真正完成或完整复盘过的工程问题，包括复现、根因、方案、实现、验证和遗留问题。
- 不知道答案时，能区分“代码事实”“合理推断”“待验证方案”，而不是编造。

### 2.2 20 天内不追求的目标

- 不逐行读完全部代码。
- 不背诵所有 SQLAlchemy 实体字段、环境变量或 API。
- 不系统学习 pytest 高级 fixture、插件和参数化技巧。
- 不深入前端样式、Excel 格式、SMTP 细节和每个 PDF 解析边界。
- 不宣称精通所有新增模块。
- 不把“AI 生成过代码”等同于“自己理解并完成过设计”。

### 2.3 时间是否足够

60 小时足以达到“项目主链熟悉、核心亮点可深挖、常见追问可回答”的实习面试水平，但前提是严格执行口述、代码定位、失败场景和模拟面试，不把时间耗在被动阅读上。

---

## 3. 简历内容核对与优化

### 3.1 当前简历中必须修正的地方

| 当前表述 | 仓库事实与风险 | 建议写法 |
|---|---|---|
| `CampusCareAgentHarness` | 当前真实类名是 `MindBridgeAgentHarness`，位于 `app/agents/harness.py` | 改为 `MindBridgeAgentHarness` |
| Harness 统一处理“消息持久化” | Harness 确实负责输入校验、澄清恢复、运行时调用、风险报告、工具计划和 Trace；但正式 ChatTurn 终态与 Assistant 消息提交还涉及 `ChatService` | 写成“统一编排输入准备、澄清恢复、Agent 执行、风险报告、工具计划和 Trace，并与 ChatService 协作完成消息终态持久化” |
| Skill 动态注入 system prompt | Skill 会生成 `prompt_context`，经过匹配、字符预算和 ContextBuilder/ResponseAgent 上下文装配。直接说“注入 system prompt”容易被追问信任边界 | 写成“按意图、领域、关键词和风险等级选择 Skill，并在预算约束下装配到 Response 上下文” |
| 181 条数据集取得 96.7% 路由准确率 | 96.7% 路由准确率来自当前 180 条 `routing-v1` 报告；181 条是 RAG/RAGAS 数据集。不能混成同一个数据集 | 分开写：“180 条路由集取得 96.7% 路由准确率、100% 高风险召回率；另构建 181 条 RAG 端到端数据集” |
| LLM-as-Judge 全量效果已经验证 | 仓库有 DeepSeek Judge、RAGAS 和 E2E 评测框架，但最近可见部分报告只是单 case 调试且并未全部通过门禁 | 可以写“实现 LLM-as-Judge 与 RAGAS 评测链路”，但没有全量通过报告前，不写未经复现的全量 Judge 分数 |
| 保证事实可靠性 | 系统能提高和约束可靠性，不能绝对保证 LLM 正确 | 改成“提升事实可靠性”或“通过证据门禁降低无依据回答风险” |

### 3.2 推荐项目简介

```text
面向高校学生服务场景构建多 Agent 智能平台，覆盖意图路由、校务政策咨询、
学业规划、心理支持、风险识别与高风险预警闭环。系统采用事件驱动多 Agent
Runtime、受策略约束的 Agentic RAG、分层上下文治理和端到端评测，提高复杂
问答的可追溯性、可恢复性与事实可靠性。
```

### 3.3 推荐技术栈写法

```text
Python、FastAPI、SQLAlchemy、MySQL、Redis、Docker Compose、Chroma、
Ollama/OpenAI-compatible API、SSE、MCP；工程实践包括 Agent Runtime Harness、
Skill Registry、Agentic RAG、Trace 与分层端到端评测。
```

`Harness Engineering`、`Skill`、`Agentic RAG` 更适合放在工程能力或项目亮点中，不建议与 Python、MySQL 并列成传统基础技术栈。

### 3.4 推荐简历 bullets

下面的版本与当前仓库证据更一致。只有真正能够回答本文档对应验收题后，才能保留相应 bullet。

```text
• 事件驱动多 Agent 协作：基于共享黑板、任务认领、事件流与 Artifact 发布机制，
  编排 Coordinator、Understanding、Safety、Knowledge、Response Agent，支持高风险
  安全抢占、候选响应审查、受控修订和最终结果采纳。

• 受控 Agentic RAG：实现 Planner → Query Rewriter → Retrieval → Evidence Grader
  → Prompt Evidence Guard 闭环，通过 QuerySpec、检索轮次/LLM 调用/总查询数预算、
  证据硬过滤与受控重试，降低无依据回答和失控检索风险。

• Agent Runtime Harness：实现 MindBridgeAgentHarness，统一编排输入校验与脱敏、
  澄清恢复、上下文注入、Agent Runtime 执行、风险报告、工具计划和 Trace，并与
  ChatService 协作完成 ChatTurn 终态及消息持久化。

• 动态 Skill 加载与路由：设计基于 SKILL.md 与 YAML Front Matter 的 Skill Registry，
  支持运行时扫描、校验与故障隔离；依据 Agent、意图、领域、关键词及风险等级评分，
  在数量和字符预算约束下将匹配 Skill 装配到响应上下文。

• 上下文与记忆管理：使用 MySQL 持久化会话与消息、Redis 缓存近期消息，结合增量
  摘要与 ContextBuilder Token 预算装配 Summary、近期对话、显式记忆、澄清状态、
  安全元数据和知识证据，支持缓存失败回退、跨轮恢复与会话隔离。

• 分层端到端评测：构建路由、检索、E2E Business Judge 与 RAGAS 分层评测，并通过
  Trace 进行失败归因；当前 180 条路由集取得 96.7% 路由准确率、100% 高风险召回率
  和 0 高风险漏判，另维护 181 条 RAG 端到端数据集与语料指纹校验。
```

### 3.5 面试中必须主动说明的指标边界

当前路由报告不仅有亮点，也有不足：

- Route Accuracy：96.7%。
- Route Macro F1：约 95.8%。
- Risk Recall：100%。
- 高风险漏判：0。
- Full TurnPlan Exact Match：70%。
- Clarification Accuracy：约 55.6%。
- Knowledge Agent False Positive Rate：约 10.3%。
- Knowledge Agent False Negative Rate：约 4.8%。

面试时推荐说法：

```text
路由大类准确率达到 96.7%，高风险召回率为 100%，说明安全主门禁有效；但完整
TurnPlan 精确匹配只有 70%，澄清准确率约 55.6%，说明细粒度 task kind、执行模式
和澄清策略仍有优化空间。我会先通过混淆矩阵和 tag slice 定位误差，而不是只看总准确率。
```

这比只报漂亮数字更可信。

---

## 4. 项目主线地图

### 4.1 一次聊天请求的主调用链

```text
POST /api/chat/stream
→ FastAPI 认证、会话可用性检查
→ ChatService.start_chat
→ ChatTurnService.create_or_get（requestId 幂等）
→ 保存或复用 USER 消息，尽力写入 Redis
→ 后台启动 ChatService._generate
→ MindBridgeAgentHarness.run
→ 输入校验、隐私脱敏、澄清恢复、显式记忆
→ EventDrivenAgentRuntimeService.run
→ ContextBuilder.build_base_context
→ CollaborationBlackboard + TURN_STARTED
→ EventDrivenCoordinator.run
→ Understanding / Safety / Knowledge / Response Agent
→ Artifact、Safety Review、FINAL_ACCEPTED
→ Harness 保存风险报告、工具计划和 Trace
→ ChatService 调用模型流式生成或返回 direct response
→ 持久化快照与终态，正式写入 Assistant 消息
→ StreamingResponse 轮询快照并输出 meta/snapshot/error/done SSE
```

### 4.2 核心模块地图

| 能力 | 核心文件 | 必须掌握的入口 |
|---|---|---|
| FastAPI 与 SSE | `app/main.py`、`app/api/routes.py`、`app/services/chat.py` | `create_app`、`chat_stream`、`start_chat`、`stream_turn` |
| 幂等与重连 | `app/services/chat_turns.py`、`app/services/chat.py` | `create_or_get`、`ensure_user_message`、`_turn_state` |
| Harness | `app/agents/harness.py` | `MindBridgeAgentHarness.run` |
| 多 Agent Runtime | `app/agents/event_driven_runtime.py` | `EventDrivenAgentRuntimeService.run`、`_to_result` |
| 黑板与事件 | `app/agents/events.py` | `CollaborationBlackboard`、`AgentTask`、`AgentArtifact`、`AgentEvent` |
| Coordinator | `app/agents/coordinator.py` | `run`、`_derive_missing_work`、`_try_accept_final` |
| 专业 Agent | `app/agents/autonomous.py` | Understanding、Safety、Knowledge、Response、Coordinator |
| 路由与 TurnPlan | `app/agents/routing.py`、`app/core/enums.py` | `classify_route`、`RouteDecision` |
| Skill | `app/services/skills.py`、`skills/*/SKILL.md` | `MindBridgeSkillRegistry`、`SkillManager.match` |
| 上下文 | `app/services/context_builder.py` | `build_base_context`、`build_response_prompt` |
| 记忆 | `app/services/memory.py`、`app/services/user_memory.py` | Redis recent、Summary CAS、显式记忆 |
| Agentic RAG | `app/services/knowledge_agent/` | Planner、Policy、Orchestrator、Grader、Rewriter |
| 混合检索 | `app/services/knowledge.py`、`app/services/vector_store.py` | BM25F、Chroma query、RRF、rerank、degradation |
| 风险闭环 | `app/services/risk_signals.py`、`app/services/assessment.py`、`app/services/tools.py`、`app/mcp_tools/server.py` | 风险识别、报告、工具队列/MCP |
| Trace 与指标 | `app/services/trace.py`、`app/services/turn_metrics.py` | planning trace、generation finalize、全链路 token/耗时 |
| 分层评测 | `app/evaluation/`、`app/rag_eval/` | routing、retrieval、e2e、ragas、reporting |
| 部署 | `Dockerfile`、`docker-compose.yml`、`migrations/` | app/MySQL/Redis、Alembic、Chroma 数据目录 |

---

## 5. 学习优先级

### P0：必须能够深入回答

1. 聊天请求端到端调用链。
2. requestId 幂等、SSE 快照、断线重连和生成终态。
3. Harness 的职责与 Runtime 的职责为什么分开。
4. 黑板、Task、Claim、Artifact、Event、Final Accepted 的关系。
5. CHAT / CONSULT / RISK 与 TurnPlan 的作用。
6. Planner、Policy、Retrieval、Grader、Rewriter、Prompt Guard 的闭环。
7. BM25F、Embedding、Chroma、RRF 和降级到 BM25-only。
8. MySQL、Redis、Summary、ContextBuilder 和 Token 预算。
9. 高风险为什么抢占普通知识链路，怎样形成告警闭环。
10. 180 条路由评测和 181 条 RAG 数据集分别测什么。

### P1：需要掌握原理和项目落点

- FastAPI 依赖注入、Pydantic、SQLAlchemy Session 与事务。
- Python `async/await`、异步生成器、dataclass、Enum、Protocol。
- Skill Front Matter 校验、匹配评分、预算和故障隔离。
- Trace、隐私脱敏、TTFT、Token 汇总。
- Docker Compose、Alembic、Chroma ACTIVE collection。
- MCP 与异步工具队列的关系。

### P2：知道用途即可

- pytest 高级语法。
- 前端 HTML/CSS 细节。
- openpyxl 和 SMTP 实现细节。
- 每一个 Skill 的全文。
- 知识入库 V2 的全部解析器边界。
- 所有评测指标的数学证明。

pytest 不能完全跳过。最低要求是会运行单测、读懂 Arrange/Act/Assert、知道 Mock 隔离模型和外部服务，并能为自己的小改动补一个回归用例。

---

## 6. 任意 AI 教练必须遵守的教学协议

### 6.1 开始教学前

AI 必须完成以下动作：

1. 阅读本文档和当前 `README.md`。
2. 执行只读文件检索，确认当天涉及的类和方法仍存在。
3. 查看 `git status --short`，不得覆盖或回滚学生已有改动。
4. 询问或读取学生上一次进度记录。
5. 先做 5 分钟口头复习，再开始新内容。

### 6.2 教学方式

AI 不得一开始就给完整答案，应采用以下顺序：

1. 让学生预测模块输入、输出、副作用和失败路径。
2. 让学生沿代码定位证据。
3. 对错误理解进行纠正。
4. 追问为什么这样设计，以及替代方案的代价。
5. 给出一个异常场景，让学生推演系统行为。
6. 最后要求学生脱离代码重新讲一遍。

每次只问一个主要问题。学生使用“为了提高性能”“为了安全”“用了多 Agent”这类空泛表述时，必须继续追问具体机制。

### 6.3 事实等级

AI 和学生回答时都要标注或明确区分：

- `代码事实`：能指向当前文件、类、方法或报告。
- `设计解释`：根据代码结构解释设计意图。
- `合理推断`：代码没有直接说明，但工程上可能成立。
- `改进建议`：当前尚未实现的方案。

禁止把合理推断或建议说成当前已经实现。

### 6.4 每日固定 3 小时结构

除阶段模拟日外，每天使用以下结构：

- 00:00-00:20：无笔记复述昨天内容。
- 00:20-01:05：沿真实代码阅读主链。
- 01:05-01:45：补充技术原理与设计取舍。
- 01:45-02:25：动手定位、画图、运行测试或做小实验。
- 02:25-02:50：AI 压力追问。
- 02:50-03:00：填写进度记录与次日漏洞。

### 6.5 每日验收规则

当天标记为完成，必须同时满足：

- 能不看文档讲出当天主链。
- 能定位至少 3 个关键类或方法。
- 能回答至少 2 个“为什么”和 1 个失败场景。
- 完成当天指定产出。
- AI 评分至少 7/10，且没有 P0 事实错误。

未通过时，次日开始先补 30 分钟，不得直接假装完成。

### 6.6 AI 评分标准

| 维度 | 分值 | 标准 |
|---|---:|---|
| 代码事实 | 3 | 类、方法、数据流和职责准确 |
| 原理理解 | 2 | 能解释技术机制而非背名词 |
| 设计取舍 | 2 | 能比较替代方案及代价 |
| 异常处理 | 2 | 能推演失败、降级、幂等和恢复 |
| 表达结构 | 1 | 先结论、再链路、再证据 |

---

## 7. 20 天详细执行方案

## Day 1：建立项目全景与面试基线

**目标**：知道项目解决什么问题、核心模块如何连接，并记录当前能力基线。

**代码与资料**：

- `README.md`
- `requirements.txt`
- `docker-compose.yml`
- `app/main.py`
- `app/api/routes.py`

**3 小时安排**：

1. 20 分钟：学生在不看 README 的情况下讲项目，AI 原样记录事实错误。
2. 45 分钟：阅读 README 的核心能力、技术栈、Agent loop、Harness、评测章节。
3. 40 分钟：画系统组件图，包含 Browser、FastAPI、MySQL、Redis、Chroma、模型、MCP。
4. 40 分钟：定位启动入口、路由注册、后台 worker 和静态文件。
5. 25 分钟：准备 90 秒项目介绍。
6. 10 分钟：建立学习进度记录。

**必须回答**：

- 项目最核心的业务问题是什么？
- 为什么它不是一个普通的“调用大模型接口”项目？
- MySQL、Redis、Chroma 分别保存什么？
- 哪些能力是在线请求链路，哪些是离线构建链路？

**产出**：一张系统架构图、90 秒项目介绍初稿、当前弱项清单。

**验收**：能在 90 秒内说清“场景、问题、方案、亮点、结果”，不堆砌技术名词。

## Day 2：FastAPI、SSE 与聊天入口

**目标**：从 HTTP 请求追到 `ChatService`，理解 SSE 为什么适合当前场景。

**代码**：

- `app/main.py:create_app`
- `app/api/routes.py:chat_stream`
- `app/api/routes.py:reconnect_chat_stream`
- `app/services/chat.py:start_chat`
- `app/services/chat.py:stream_turn`

**原理**：FastAPI 依赖注入、ASGI、`async def`、异步生成器、`StreamingResponse`、SSE 事件格式。

**动手任务**：

- 画出浏览器发起请求后得到 `meta → snapshot* → error? → done` 的时序图。
- 找出管理员为什么不能发起学生聊天。
- 找出会话归属和归档状态在哪里检查。

**压力追问**：

- 为什么不用普通 JSON 一次性返回？
- 为什么不用 WebSocket？
- SSE 断开后，服务端生成任务是否一定停止？
- `stream_turn` 为什么轮询快照，而不是直接消费模型流？

**验收**：能比较 SSE、WebSocket、轮询三种方案，并结合当前实现说明取舍。

## Day 3：ChatTurn 幂等、快照与断线恢复

**目标**：把“可恢复性”讲成具体机制，而不是抽象口号。

**代码**：

- `app/services/chat_turns.py`
- `app/models/entities.py:ChatTurn`
- `app/services/chat.py:ChatTaskRegistry`
- `app/services/chat.py:_generate_bound`
- `app/services/chat.py:_persist_progress`
- `app/services/chat.py:_finalize_completed_turn`
- `app/services/chat.py:_finalize_failed_turn`
- `app/services/stream_snapshots.py`

**动手任务**：推演四个场景：

1. 同一 `requestId` 连续提交两次。
2. 客户端中途断开后重新连接。
3. 模型输出到一半发生异常。
4. 应用重启后存在旧的 `GENERATING` Turn。

**必须讲清**：

- `requestId` 如何形成幂等键。
- USER 消息为什么要在 Agent 执行前持久化。
- 快照与正式 Assistant 消息有什么区别。
- 只有哪些 finish reason 能完成 ChatTurn。
- 为什么 partial content 不能直接作为正式历史。

**验收**：面试官给任一失败时间点，学生能说明数据库状态、客户端事件和是否允许重试。

## Day 4：Harness Engineering 与职责边界

**目标**：准确解释为什么在 HTTP 层与 Agent Runtime 之间增加 Harness。

**代码**：

- `app/agents/harness.py:MindBridgeAgentHarness`
- `app/agents/factory.py`
- `app/harness/runner.py`
- `app/services/chat.py:_generate_bound`

**必须拆分三层职责**：

- HTTP/SSE：认证、协议和流式事件。
- Harness：一轮业务编排、澄清、隐私、报告、工具计划、Trace。
- Runtime：Agent 协作、任务与 Artifact 收敛。

**动手任务**：制作 Harness 输入/输出表，列出 `AgentHarnessOutcome` 每个关键字段由谁产生、下游谁消费。

**压力追问**：

- Harness 与 Service Layer 有什么区别？
- 为什么不把这些逻辑都放 Coordinator？
- 为什么不让路由函数直接调用 KnowledgeAgent？
- Harness 失败后 ChatService 如何收尾？

**验收**：能纠正“CampusCareAgentHarness”和“所有持久化都由 Harness 完成”这两个不准确说法。

## Day 5：共享黑板、事件、任务和 Artifact

**目标**：掌握事件驱动多 Agent 的数据模型。

**代码**：

- `app/agents/events.py`
- `app/agents/registry.py`
- `app/agents/result.py`
- `app/agents/event_driven_runtime.py:run`

**必须掌握的对象**：

- `CollaborationBlackboard`
- `AgentTask`
- `AgentClaim`
- `AgentArtifact`
- `AgentEvent`
- `AgentTurnResult`

**动手任务**：为一个“我挂科后还能拿学位吗”请求，手工写出可能出现的 Task、Claim、Artifact 和 Event 顺序。

**压力追问**：

- 共享黑板和 Agent 之间直接互调有什么差别？
- Artifact 为什么需要 `kind`、`owner`、`confidence`？
- 事件日志和最终结果各有什么用途？
- 共享状态是否存在并发写冲突？当前实现是真并行还是事件驱动顺序协作？

**验收**：能画出六个核心对象的关系，不能只说“多个 Agent 相互协作”。

## Day 6：Coordinator、任务认领与安全抢占

**目标**：理解系统如何收敛，而不是无限 Agent 循环。

**代码**：

- `app/agents/coordinator.py`
- `app/agents/autonomous.py:CoordinatorAgent`
- `app/agents/autonomous.py:SafetyAgent`
- `app/agents/registry.py:AgentRegistry`

**重点方法**：

- `EventDrivenCoordinator.run`
- `_derive_missing_work`
- `_claim_candidates`
- `_ensure_risk_route`
- `_try_accept_final`

**动手任务**：画普通 CONSULT 与高风险 RISK 的两条不同任务图，标注安全抢占发生的位置。

**压力追问**：

- 如何防止 Agent 无限循环？
- `max_steps`、预算和任务状态分别限制什么？
- SafetyAgent 为什么既做前置风险判断又做候选响应审查？
- Coordinator 是业务 Agent 还是控制平面？
- 多 Agent 相比单 Agent 增加了哪些延迟和复杂度？

**验收**：能说出多 Agent 的收益、成本和适用边界，不能回答“多 Agent 一定更智能”。

## Day 7：意图路由、TurnPlan 与澄清恢复

**目标**：理解路由不只是三分类，还决定任务类型和执行模式。

**代码**：

- `app/agents/routing.py`
- `app/core/enums.py`
- `app/services/clarifications.py`
- `app/services/clarification_extractor.py`
- `app/services/clarification_models.py`
- `tests/test_turn_plan_routing.py`

**必须掌握**：

- `route`
- `riskLevel`
- `primaryDomain` / `secondaryDomains`
- `taskKind`
- `executionMode`
- `knowledgeNeed`
- `freshnessRequired`

**动手任务**：给 10 个问题手工标注完整 TurnPlan，再与代码结果或数据集期望比较。

**压力追问**：

- 为什么 Route Accuracy 高但 Full TurnPlan Exact Match 只有 70%？
- 什么情况下需要澄清而不是直接检索？
- 澄清未完成时为什么要保存 resume context？
- 新输入出现高风险信号时为什么必须绕过旧澄清流程？

**验收**：能从混淆矩阵解释错误类型，不只会报 96.7%。

## Day 8：动态 Skill Registry 与受预算注入

**目标**：能够从一个 `SKILL.md` 追到 Response Prompt 上下文。

**代码**：

- `app/services/skills.py`
- `skills/supportive_response_baseline/SKILL.md`
- `skills/high_risk_safety_plan/SKILL.md`
- `skills/academic_stress_planning/SKILL.md`
- `tests/test_skills.py`

**必须掌握**：

- YAML Front Matter 的字段。
- Registry 扫描与 UTF-8 读取。
- validation warning 与 error。
- 单个坏 Skill 的故障隔离。
- Agent、route、domain、keyword、risk 的过滤。
- priority、命中加分、`max_matches` 和 `total_chars`。

**动手任务**：手算三个 Skill 对一个输入的匹配分数，并解释为什么最多只选择有限个 Skill。

**压力追问**：

- Skill 和 Tool 有什么区别？
- Skill 和普通 Prompt 模板有什么区别？
- 为什么不能把所有 Skill 全部塞进上下文？
- Skill 内容是否属于可信 system 指令？如何防止其挤占上下文？
- 修改 Skill 是否需要重新训练模型？

**验收**：能准确说“在预算约束下装配到响应上下文”，不再笼统说“动态注入 system prompt”。

## Day 9：ContextBuilder、分层记忆与 Token 预算

**目标**：回答多轮对话为什么不会无限膨胀，以及 Redis 故障时怎么办。

**代码**：

- `app/services/context_builder.py`
- `app/services/memory.py`
- `app/services/user_memory.py`
- `app/models/entities.py:ConversationSummary`
- `app/models/entities.py:UserMemory`
- `tests/test_context_builder.py`
- `tests/test_context_budget.py`

**必须掌握的上下文来源**：

- System Policy。
- 当前用户消息。
- 近期消息。
- Summary V2。
- 显式用户记忆。
- 澄清状态。
- 有限安全元数据。
- Skill。
- RAG 证据。

**动手任务**：画出上下文预算不足时的保留/裁剪顺序，并为每一类标可信边界。

**压力追问**：

- Redis 为什么不是事实源？
- Redis miss、旧缓存或异常时如何回退？
- 为什么摘要要用 compare-and-swap？
- “请记住”与自动推断用户画像有什么隐私差别？
- 知识正文为什么是 `REFERENCE_DATA` 而不是 system 指令？

**验收**：能解释 Token 预算、信任边界、缓存回退和会话隔离四件事。

## Day 10：阶段一综合验收

**目标**：验证学生是否真正掌握在线聊天主链。

**安排**：

- 30 分钟：无资料画完整请求时序图。
- 45 分钟：AI 随机指定一个行为，学生现场定位代码。
- 45 分钟：围绕 SSE、幂等、Harness、多 Agent、Skill、Context 连续追问。
- 30 分钟：分析一个高风险请求和一个澄清恢复请求。
- 20 分钟：确定 Day 19 的个人深挖改造题。
- 10 分钟：修订 90 秒介绍。

**阶段门槛**：

- 总分至少 35/50。
- 不得混淆 Harness、Runtime 和 ChatService 职责。
- 不得把 Redis 当持久化事实源。
- 不得把 Skill 说成无条件拼接。
- 不得说 SSE 断开一定会取消服务端生成。

未达标时，Day 11 前先补弱项，但总周期不延长，压缩 P2 内容。

## Day 11：RAG 基础与 QuerySpec

**目标**：理解 RAG 的核心不是“向量库”，而是从问题到可验证证据的完整链路。

**代码**：

- `app/services/knowledge_query.py`
- `app/knowledge/retrieval_taxonomy.yaml`
- `app/services/knowledge_agent/models.py`
- `app/services/knowledge_agent/fast_planner.py`

**原理**：Chunk、Embedding、稀疏检索、稠密检索、Top-K、metadata filter、query expansion。

**动手任务**：对“休学需要哪些材料，多久能办完”构造 QuerySpec，区分文档范围、site、facet、required fact 和禁止推断内容。

**压力追问**：

- 为什么只做向量相似度不够？
- 学校政策中的数字、材料名称和专有词为什么适合 BM25？
- QuerySpec 如何限制模型随意改写查询？
- 元数据过滤太严或太松分别有什么后果？

**验收**：能从用户问题推导检索约束，而不是只背 RAG 定义。

## Day 12：Planner、Policy 与硬预算

**目标**：理解“Agentic”不等于让 LLM 无限自主循环。

**代码**：

- `app/services/knowledge_agent/planner.py`
- `app/services/knowledge_agent/policy.py`
- `app/services/knowledge_agent/orchestrator.py`
- `app/services/knowledge_agent/prompts.py`

**必须掌握的预算**：

- 最大子问题数。
- 最大检索轮次。
- 每问题/每轮/全局最大查询数。
- 最大语义调用数和 provider 请求数。
- deadline。
- schema repair 次数。
- 最大证据数。

**动手任务**：画出一次复杂问题从 Planner 到停止的状态机，并标出每个停止条件。

**压力追问**：

- Planner 输出为什么必须用严格 Pydantic schema？
- LLM 输出不合法时为什么只允许有限 repair？
- 哪些规则适合放 Policy 而不是 Prompt？
- 预算耗尽时应该返回错误、部分证据还是澄清？

**验收**：能解释“策略约束的 Agentic RAG”中的“策略约束”具体是什么。

## Day 13：BM25F、Chroma、RRF 与故障降级

**目标**：掌握混合检索及其工程降级路径。

**代码**：

- `app/services/knowledge.py`
- `app/services/vector_store.py`
- `app/services/embedding.py`
- `app/services/knowledge_scoring.py`
- `app/services/knowledge_index.py`

**必须掌握**：

- BM25F 为什么对标题、章节、标签、正文分字段计分。
- Embedding 与 Chroma query 的输入输出。
- RRF 使用名次而非直接混合原始分数的原因。
- rerank 与 RRF 的差别。
- `bm25-only`、`hybrid-v3`、`degraded-bm25-only`。
- `KNOWLEDGE_VECTOR_REQUIRED` 对失败语义的影响。

**动手任务**：手算两个排名列表的简化 RRF，并推演 Ollama Embedding、Chroma、ACTIVE 指针分别故障时的系统行为。

**压力追问**：

- 为什么不能直接把 BM25 score 和 cosine similarity 相加？
- RRF 权重如何影响结果？
- 向量不可用时为什么仍保留 evidence grader？
- 在线请求为什么不应创建索引或提交 embedding？
- ACTIVE/READY/previous collection 解决了什么发布问题？

**验收**：能完整讲出“检索成功”和“三类向量故障”路径。

## Day 14：Evidence Grader、Query Rewriter 与 Prompt Guard

**目标**：理解检索到内容不等于拥有可用于回答的证据。

**代码**：

- `app/services/knowledge_agent/evidence_grader.py`
- `app/services/knowledge_agent/query_rewriter.py`
- `app/services/knowledge_agent/policy.py:PromptEvidenceGuard`
- `app/services/knowledge_agent/orchestrator.py`
- `tests/test_knowledge_evidence_grader.py`
- `tests/test_knowledge_query_rewriter.py`

**必须掌握**：

- `SUFFICIENT`、`PARTIAL`、`NONE` 的意义。
- supported claims、contradictions、missing facts。
- 硬过滤与 LLM grader 的职责差异。
- 何时重写查询，何时停止，何时请求澄清。
- Prompt Guard 如何对最终证据再次对账。

**动手任务**：给一组“相关但不支持具体结论”的检索结果，判断是否允许回答，并写出安全的部分回答或澄清问题。

**压力追问**：

- Evidence Grader 自己也可能判断错，系统如何降低风险？
- 为什么需要 deterministic hard filter 加 semantic grader？
- PARTIAL 证据能否回答？边界是什么？
- Query Rewriter 如何避免脱离原问题？
- 为什么 Prompt 生成前还要做一次 evidence reconciliation？

**验收**：能解释证据归属、覆盖度和 grounded answer，而不是说“加 grader 减少幻觉”。

## Day 15：风险识别、安全回复与 MCP 预警闭环

**目标**：从风险输入追到学生回复、后台报告和工具执行。

**代码**：

- `app/services/risk_signals.py`
- `app/services/assessment.py`
- `app/agents/autonomous.py:SafetyAgent`
- `app/agents/coordinator.py:_ensure_risk_route`
- `app/services/tools.py`
- `app/services/tool_queue.py`
- `app/services/tool_governance.py`
- `app/mcp_tools/server.py`
- `skills/high_risk_safety_plan/SKILL.md`

**动手任务**：画高风险输入的双通道结果：学生端安全回复与后台报告/告警工具链。

**压力追问**：

- 为什么高风险先用确定性信号，再使用模型判断？
- 为什么 RISK 不进入普通 RAG？
- 为什么学生端不能显示后台风险分数和标签？
- MCP 和内部工具队列是什么关系？
- 告警发送失败如何重试、限流、审计或进入死信？

**验收**：能讲清高风险召回为何是硬门禁，以及 100% 召回不代表系统已经完美。

## Day 16：MySQL、Redis、Chroma、Docker 与事务边界

**目标**：补齐 AI 应用后端的基础设施解释能力。

**代码与配置**：

- `app/core/database.py`
- `app/models/entities.py`
- `migrations/`
- `docker-compose.yml`
- `Dockerfile`
- `app/services/vector_store.py`

**必须掌握**：

- SQLAlchemy Session 生命周期。
- ChatTurn、ChatMessage、ChatSession 的关系。
- USER 消息、planning trace、Assistant 消息分别何时 commit。
- Redis 允许失败的原因。
- Chroma 持久化目录和 collection registry。
- Alembic 为什么不能用 `create_all()` 替代。
- Docker Compose 中 app、MySQL、Redis 与宿主机 Ollama 的网络关系。

**动手任务**：画部署图和一次成功请求的关键事务时间线。

**压力追问**：

- 模型调用期间为什么不保持一个超长数据库事务？
- 数据库提交成功但 Redis 写失败怎么办？
- 两个应用实例如何共享 ACTIVE collection 指针？
- 服务扩容十倍后可能首先出现哪些瓶颈？

**验收**：能从工程可靠性而不是组件定义回答问题。

## Day 17：Trace、隐私、指标与故障归因

**目标**：掌握“可追溯”具体保存什么，以及哪些内容故意不保存。

**代码**：

- `app/services/trace.py`
- `app/services/turn_metrics.py`
- `app/services/privacy.py`
- `app/services/retrieval_capture.py`
- `tests/test_trace_privacy.py`
- `tests/test_turn_metrics.py`

**必须掌握**：

- planning trace 与 generation finalize。
- Agent steps、Artifact、context manifest、retrieval diagnostics。
- TTFT、首快照时间、总耗时、provider 调用、Token 汇总。
- 为什么 Trace 默认不保存 Prompt、用户原文、模型正文和知识正文。
- content hash、source ref、dropped reason 的作用。

**动手任务**：设计一份“回答错误”排障树，依次判断路由、检索、grader、prompt、模型生成和工具执行。

**压力追问**：

- 可观测性与隐私之间如何取舍？
- 没有保存正文，如何定位问题？
- TTFT 高可能由哪些阶段造成？
- 如何区分检索失败和生成失败？

**验收**：能用 Trace 字段对一个失败 case 做分层归因。

## Day 18：分层端到端评测与指标复现

**目标**：能解释 96.7% 和 100% 是怎样算出来的，并理解评测局限。

**代码与报告**：

- `app/evaluation/runner.py`
- `app/evaluation/evaluators/routing.py`
- `app/evaluation/evaluators/retrieval.py`
- `app/evaluation/evaluators/end_to_end.py`
- `app/evaluation/judges/deepseek.py`
- `app/evaluation/ragas_eval/`
- `app/evaluation/datasets/routing-v1.jsonl`
- `app/evaluation/datasets/mindbridge-e2e-ragas-v1.jsonl`
- `target/evaluation/*/routing-report.json`

**安全、无需真实 Judge 的核心命令**：

```powershell
python -m app.evaluation.runner --suite routing --profile full
```

如果环境未配置完整，AI 应先运行单 case 或只读已有报告，不得擅自消耗付费模型额度。E2E Judge 需要独立配置 `EVAL_JUDGE_API_KEY`，必须得到学生明确同意后再运行。

**必须掌握**：

- Accuracy、Macro F1、Recall、混淆矩阵。
- Recall@K、MRR、NDCG。
- Business Judge 与 RAGAS 的差异。
- corpus fingerprint 为什么必须匹配。
- gate、baseline、slice、failed case。
- 180 路由数据与 181 RAG 数据不能混淆。

**动手任务**：从报告中挑一个误分类 case，定位规则根因并提出改进，但不允许为单 case 硬编码。

**压力追问**：

- 为什么 Accuracy 96.7% 仍可能掩盖问题？
- 高风险 Recall 为什么比 Precision 更重要？
- LLM-as-Judge 有哪些偏差，如何降低？
- RAGAS 分数高是否一定说明业务回答正确？
- 为什么最近单 case E2E/RAGAS 报告不能代表 181 条全量效果？

**验收**：能现场解释报告的优势、弱点和下一步优化方向。

## Day 19：个人深挖改造与工程故事

**目标**：形成一个可信的“我真正解决了什么问题”故事。

Day 10 必须从以下方向选择一个，范围控制在 3 小时内可完成或可完整复盘：

### 方向 A：SSE 幂等与重连

- 增加一个边界回归测试。
- 验证重复 `requestId` 不重复创建消息或生成任务。
- 解释快照、终态和重连一致性。

### 方向 B：路由误差分析

- 从 routing report 选择一类真实误差。
- 说明误差来自规则、taxonomy、task kind 还是 clarification。
- 做一个通用修正并运行相关 slice，避免 case-specific hardcode。

### 方向 C：RAG 故障降级

- 为 embedding/Chroma/ACTIVE 指针故障补一个明确诊断或测试。
- 验证 `degraded-bm25-only` 与 `vectorRequired` 两种语义。
- 解释为什么降级后仍执行 evidence policy。

**工程故事模板**：

```text
背景与影响 → 如何稳定复现 → 证据和根因 → 考虑过的方案 → 最终取舍
→ 修改了哪些模块 → 如何测试与评测 → 结果 → 尚未解决的问题
```

**验收**：

- 至少有一个代码 diff 或一份完整复盘记录。
- 至少运行一个聚焦测试。
- 能回答“为什么不是另一个方案”。
- 不把 AI 给出的代码冒充为自己理解过的代码。

## Day 20：最终模拟面试与简历定稿

**目标**：完成项目面试闭环。

**3 小时安排**：

1. 15 分钟：90 秒项目介绍 + 5 分钟架构介绍。
2. 45 分钟：在线链路压力面试。
3. 45 分钟：Agentic RAG 与评测压力面试。
4. 30 分钟：个人工程故事深挖。
5. 20 分钟：现场代码定位与故障推演。
6. 15 分钟：根据表现删除或降级无法支撑的简历表述。
7. 10 分钟：形成最终复习清单。

**最终通过条件**：

- 90 秒介绍不超过 220 字且结构清楚。
- 5 分钟主链无 P0 事实错误。
- 30 个随机追问正确率至少 80%。
- 任意简历 bullet 能连续回答两层为什么和一个失败场景。
- 能诚实说明 180/181 数据集、评测指标和当前不足。
- 个人深挖故事有代码或报告证据。

---

## 8. 四次阶段检查

| 检查点 | 必须具备的能力 | 不通过后的处理 |
|---|---|---|
| Day 5 | 能画 HTTP → ChatService → Harness → Runtime 主链；解释黑板对象 | 压缩 Day 6 的原理时间，先补调用链 |
| Day 10 | 能独立讲 SSE、幂等、Harness、Agent、Skill、Context | P2 内容全部暂停，优先修正主线事实 |
| Day 15 | 能讲完整 Agentic RAG 和高风险闭环 | Day 16 仅保留数据库事务和 Docker 主线 |
| Day 20 | 能承受 40 分钟项目深挖 | 删除无法支撑的简历 bullet，不靠背稿掩盖 |

---

## 9. 高频追问题库

### 9.1 架构与后端

1. 为什么选择 FastAPI？
2. FastAPI 的依赖注入在项目中解决了什么问题？
3. 为什么使用 SSE 而不是 WebSocket？
4. 客户端断开是否会取消后台生成？
5. 相同 requestId 为什么不会重复生成？
6. 快照和正式 Assistant 消息为什么分开？
7. 为什么 USER 消息要先持久化？
8. 模型输出被截断时怎么处理？
9. 为什么只有 STOP 或 DIRECT_RESPONSE 才能完成 Turn？
10. 数据库事务如何跨模型长调用划分？

### 9.2 Harness 与多 Agent

11. Harness 和 Runtime 的区别是什么？
12. Harness 为什么不能直接替代 Coordinator？
13. 多 Agent 比单 Agent 好在哪里？
14. 多 Agent 增加了哪些延迟和故障点？
15. 黑板模式与 Agent 直接互相调用有什么不同？
16. Task、Claim、Artifact、Event 分别是什么？
17. 谁决定最终回答被接受？
18. 如何避免无限循环？
19. SafetyAgent 为什么需要两次参与？
20. 当前实现是真并行多 Agent 吗？如果不是，为什么仍叫事件驱动？

### 9.3 路由、Skill 与上下文

21. CHAT、CONSULT、RISK 之后还需要 TurnPlan 做什么？
22. 为什么路由准确率高，TurnPlan exact match 仍较低？
23. 什么场景需要澄清？
24. 高风险输入为什么绕过澄清恢复？
25. Skill 与 Tool 的区别是什么？
26. Skill 如何加载、验证和隔离坏文件？
27. Skill 如何匹配和排序？
28. 为什么最多只注入有限个 Skill？
29. 上下文超预算时先丢什么？
30. 如何防止知识正文或历史消息变成高权限指令？

### 9.4 RAG

31. BM25F 与向量检索各自擅长什么？
32. 为什么使用 Chroma？替换成其他向量库需要改什么？
33. 为什么使用 RRF？
34. 为什么不直接相加 BM25 和 cosine 分数？
35. Planner 为什么要拆子问题？
36. Query Rewriter 如何防止偏离原问题？
37. Evidence Grader 判断错怎么办？
38. PARTIAL 证据是否可以生成回答？
39. Prompt Guard 和 Grader 是否重复？
40. Embedding 或 Chroma 故障如何降级？
41. 为什么在线请求不能重建索引？
42. ACTIVE/READY/previous collection 有什么意义？

### 9.5 安全、记忆与工具

43. 为什么 Redis 不是事实源？
44. Redis 故障时会发生什么？
45. 自动摘要如何避免并发覆盖？
46. 显式记忆为什么需要用户 opt-in？
47. 高风险为什么优先确定性规则？
48. 100% 高风险召回率是否意味着安全系统完美？
49. MCP 和内部函数调用的区别是什么？
50. 告警失败如何重试和审计？

### 9.6 评测与工程质量

51. 96.7% 路由准确率如何计算？
52. 为什么还要看 Macro F1？
53. 高风险场景为什么更关心 Recall？
54. Recall@5、MRR、NDCG@5 分别衡量什么？
55. LLM-as-Judge 有什么偏差？
56. RAGAS 和业务 Judge 为什么都需要？
57. 为什么必须校验 corpus fingerprint？
58. Trace 不保存正文，如何排障？
59. 你如何证明一次改动没有让别的 slice 退化？
60. 当前项目最明显的技术债是什么？

---

## 10. 标准回答结构

### 10.1 技术问题

```text
先给结论 → 说明当前项目怎么做 → 指出关键类/数据流 → 解释为什么这样设计
→ 给出失败场景和处理 → 补充替代方案与代价
```

### 10.2 指标问题

```text
数据集和样本量 → 指标定义 → 当前结果 → 说明通过了什么门禁
→ 指出指标不能证明什么 → 给出错误 slice 和下一步优化
```

### 10.3 个人贡献问题

```text
业务问题 → 我负责的边界 → 关键决策 → 最难问题 → 验证方法 → 结果和不足
```

### 10.4 不知道的问题

```text
当前代码事实是……；我暂时不能确认的是……；如果现场排查，我会先看……，
再通过……验证。一个可能方案是……，但它还不是当前实现。
```

---

## 11. 每日进度记录模板

每天结束时，把下面内容追加到单独的 `INTERVIEW_STUDY_PROGRESS.md`。新 AI 必须先读最新记录。

```markdown
## Day X - YYYY-MM-DD

### 今日主题

### 我能脱离文档讲出的调用链

### 今天定位过的文件和方法

### 我答错或答不深的问题

### 今日动手产出/测试命令

### AI 评分
- 代码事实：/3
- 原理理解：/2
- 设计取舍：/2
- 异常处理：/2
- 表达结构：/1
- 总分：/10

### 明天必须先补的 3 个漏洞
1.
2.
3.

### 当前简历 bullets 状态
- 已能支撑：
- 尚不能支撑：
```

---

## 12. 换 AI 时直接使用的总提示词

```text
你是我的中国大厂 AI 后端日常实习项目面试教练。

请先完整阅读仓库根目录的
MIND_BRIDGE_20_DAY_INTERVIEW_PREPARATION_PLAYBOOK.md，
再阅读 INTERVIEW_STUDY_PROGRESS.md 的最新一天，并检查当前仓库中今天涉及的
类、方法和测试是否仍然存在。代码事实以当前工作区为准，不要覆盖或回滚已有改动。

我现在进行到 Day X。请严格执行当天 3 小时课程和验收规则：
1. 先让我无笔记复述，不要直接总结。
2. 每次只问一个主要问题。
3. 追问到具体类、方法、数据流、失败路径和设计取舍。
4. 明确区分代码事实、设计解释、合理推断和改进建议。
5. 要求我完成当天的图、代码定位、实验或测试。
6. 结束时按 10 分制评分，并生成当天进度记录。

如果我只是背概念或堆技术名词，请继续追问。若当天没有通过验收，明确指出，
不要为了推进进度而判定完成。
```

---

## 13. 每日压力面试提示词

```text
围绕我今天学习的 MindBridge 模块进行压力面试。先从项目代码事实开始，然后依次
追问技术原理、设计取舍、失败场景、替代方案。每个主题至少追问两层为什么，并要求
我指出关键类或方法。不要一次给多个问题，不要替我回答。最后指出我的回答中所有
无法由当前仓库支撑的内容。
```

---

## 14. 常用验证命令

先在 `mindbridge-py` 目录执行。命令是否可用取决于本地依赖和服务状态，AI 不得伪造成功结果。

```powershell
# 全部单测，时间允许时运行
python -m pytest -q

# 聊天、幂等与生成
python -m pytest tests/test_chat_turns.py tests/test_chat_completion.py -q

# 路由与 TurnPlan
python -m pytest tests/test_routing_integration.py tests/test_turn_plan_routing.py -q

# Skill
python -m pytest tests/test_skills.py -q

# Context 与预算
python -m pytest tests/test_context_builder.py tests/test_context_budget.py -q

# Agentic RAG
python -m pytest tests/test_knowledge_orchestrator.py tests/test_knowledge_policy.py `
  tests/test_knowledge_evidence_grader.py tests/test_knowledge_query_rewriter.py -q

# 分层评测合同测试
python -m pytest tests/evaluation -q

# 不需要真实 Judge 的路由全量评测
python -m app.evaluation.runner --suite routing --profile full

# Engineering Harness
python -m app.harness.runner --suite all
```

涉及 Docker、真实模型、真实 Judge 或可能产生费用的命令，AI 必须先说明前提和成本，再获得学生同意。

---

## 15. 最终简历保留规则

每个 bullet 必须通过“三证据”检查：

1. **代码证据**：能指出实现文件、核心类和调用链。
2. **运行证据**：有测试、报告、Trace 或可复现实验。
3. **口述证据**：能回答两层为什么、一个失败场景和一个替代方案。

缺任意一项时，按以下方式处理：

- 只读过代码：改成“熟悉”或不写个人贡献。
- 有代码无评测：不写量化结果。
- 有评测但无法解释：先删除数字，补学后再恢复。
- 不是自己负责：使用“参与”而不是“设计并实现”。

最终目标不是让简历看起来技术名词最多，而是让每一行都经得起追问。

