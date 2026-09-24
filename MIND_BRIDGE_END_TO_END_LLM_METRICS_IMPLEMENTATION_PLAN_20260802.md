# MindBridge 端到端首 Token 时间与 Token 花费统计实施方案

> 文档状态：可直接交给 AI 编码实施
>
> 编制日期：2026-08-02
>
> 适用项目：`mindbridge-py`
>
> 目标版本：Turn Metrics V1

## 1. 使用说明

本文件是代码实施规范，不是概念性建议。后续 AI 或开发者应按照本文定义的指标口径、数据契约、文件边界、实施顺序、测试和验收标准完成改造。

开始实施前必须：

1. 阅读本文全文、项目根目录 `AGENTS.md`（如存在）、`README.md`、当前 `git status` 及所有待修改文件。
2. 当前工作区存在用户未提交修改，必须保留并在其基础上工作，不得回滚、覆盖或格式化无关内容。
3. 先运行并记录相关测试基线，再修改代码。
4. 先写指标契约和聚合器测试，再接入生产链路。
5. 所有文件以 UTF-8 保存，中文保持直接可读字符，不得改写为 Unicode 转义。
6. 不得把 Prompt、用户原文、模型输出正文、知识正文或个人信息写入指标。

## 2. 当前基线

### 2.1 已有能力

项目已有以下局部观测能力：

- `app/services/model_completion.py` 定义 `ModelUsage` 和 `ModelCompletionMetadata`，可承载输入、输出、推理 Token 和单次模型请求总耗时。
- `app/services/ai.py` 使用单调时钟统计 Ollama/OpenAI 单次请求的 `duration_ms`。
- Ollama 从 `prompt_eval_count`、`eval_count` 读取实际用量。
- OpenAI 非流式调用从 `usage.prompt_tokens`、`usage.completion_tokens` 读取实际用量。
- `app/services/chat.py` 汇总最终回答和一次续写的 `promptTokens`、`outputTokens`、`durationMs`。
- `app/services/knowledge_agent/orchestrator.py` 单独记录 planner、grader、rewriter 的用量和耗时。
- 最终回答元数据写入 `ChatTurn.generation_metadata_json` 和 `AgentRunTrace.generation_json`。
- SSE `done` 事件当前可返回最终回答的 `outputTokens`。

### 2.2 当前缺口

当前数据不能称为端到端指标，原因如下：

1. 没有记录请求进入 `ChatService.start_chat()` 的单调时钟起点。
2. 没有记录最终回答模型第一个非空 delta 到达的时间。
3. 没有记录第一个非空内容快照可读取的时间。
4. `durationMs` 只覆盖最终回答模型调用，不覆盖落库、上下文构建、路由、安全评估、RAG 和编排。
5. UnderstandingAgent 的语义路由调用和 SafetyAgent 的心理评估调用会丢弃 usage 元数据。
6. KnowledgeAgent 虽然保存局部调用明细，但没有与其他 Agent 和最终回答按同一 `requestId` 汇总。
7. OpenAI 流式请求没有声明 `stream_options.include_usage=true`，兼容服务通常不会返回流式 usage。
8. 当前 OpenAI 流解析假设每个数据帧都有 `choices[0]`，无法处理标准的 usage-only 结束帧。
9. `estimate_tokens()` 是上下文预算近似值，不等于供应商计费用量。
10. 没有模型价格表，因此不存在人民币或美元金额成本计算。

## 3. V1 目标

Turn Metrics V1 必须完成：

1. 按 `requestId` 聚合一次 ChatTurn 内的全部 LLM 供应商请求。
2. 覆盖语义路由、风险评估、Knowledge planner/grader/rewriter、结构化修复、最终回答和续写。
3. 记录最终回答模型 TTFT。
4. 记录服务端端到端 TTFT。
5. 记录首个非空快照准备完成时间。
6. 记录整轮生成耗时。
7. 汇总输入、输出、推理和总 Token，并标注数据是精确、混合、估算还是不可用。
8. 将结果持久化到现有 ChatTurn/Trace JSON，不新增数据库表。
9. 保持现有 SSE 事件顺序、断线重连、幂等请求、生成完成校验和隐私行为不变。
10. 对直接回复、失败、取消、供应商 EOF、内容过滤和续写路径给出明确指标语义。
11. 新增独立 `Turn Metrics Harness`，接入现有 `--suite all` 一键工程验证和统一 JSON 报告。

## 4. V1 非目标

本阶段不得：

- 把浏览器渲染时间冒充服务端 TTFT。
- 新建独立 metrics 数据库表或引入遥测平台。
- 根据未经版本化的价格常量计算人民币或美元金额。
- 删除 KnowledgeAgent 现有 `llm_calls` 诊断记录。
- 将 KnowledgeAgent 局部 `llm_calls` 再次加到全链路汇总，造成重复计数。
- 为统计指标改变模型选择、Prompt、RAG 决策或业务响应内容。
- 因供应商不返回 usage 而把估算值伪装成精确值。
- 将 `thinking_tokens` 再加到 `output_tokens` 上；OpenAI 的 reasoning tokens 通常是 completion tokens 的子集。
- 在默认 Engineering Harness 中使用绝对 TTFT/P95 阈值作为发布硬门禁。
- 让默认 Harness 调用真实外部模型或依赖公网，从而破坏一键验证的确定性。

## 5. 指标口径

### 5.1 时间线

```text
T0  ChatService.start_chat() 开始处理新 requestId
 |
 |  用户消息落库、上下文构建、路由、安全评估、RAG、Agent 编排
 |
T1  最终回答模型请求发出
 |
T2  最终回答模型返回第一个非空 delta
 |
 |  ChatService 按 150ms 或 30 字符阈值聚合内容
 |
T3  第一个非空内容快照写入成功或进入数据库回退路径
 |
T4  最终模型终止、ChatTurn 被持久化为终态
```

V1 指标定义：

| 指标 | 公式 | 说明 |
|---|---|---|
| `finalModelTtftMs` | `T2 - T1` | 最终回答模型自身 TTFT，不含前置编排 |
| `serverE2eTtftMs` | `T2 - T0` | 服务端收到本轮请求到最终回答首个模型 token |
| `firstContentReadyMs` | `T3 - T0` | 首个非空快照可供 SSE 轮询读取的时间 |
| `serverTurnDurationMs` | `T4 - T0` | 从新请求开始到 ChatTurn 即将写入终态 |
| `orchestrationBeforeModelMs` | `T1 - T0` | 进入最终回答模型前的所有服务端工作 |

约束：

- 所有耗时使用 `time.perf_counter_ns()` 或等价单调时钟计算，不使用墙上时间做时长相减。
- `startedAt`、`completedAt` 仅用于日志关联，使用 UTC ISO 8601。
- 毫秒结果取非负整数。
- `serverE2eTtftMs >= finalModelTtftMs`。
- `firstContentReadyMs >= serverE2eTtftMs`，除非走应用直接回复。
- 直接回复没有模型 token，因此 `finalModelTtftMs` 和 `serverE2eTtftMs` 必须为 `null`，但 `firstContentReadyMs` 必须有值。
- 最终模型在第一个 delta 前失败时，两项 TTFT 均为 `null`。

### 5.2 “端到端”的边界

V1 的端到端边界是服务端：从 `ChatService.start_chat()` 到最终回答模型首个非空 delta。它不包含浏览器网络接收、DOM 更新和绘制。

浏览器真实首字渲染必须命名为 `clientFirstRenderMs`，只能由 `performance.now()` 在前端测量。该能力放在第二阶段，禁止由后端推算。

### 5.3 Token 口径

每个供应商请求单独记录：

- `promptTokens`
- `outputTokens`
- `thinkingTokens`
- `totalTokens = promptTokens + outputTokens`
- `promptTokenSource`
- `outputTokenSource`

Token 来源枚举：

- `PROVIDER`：供应商返回的实际 usage。
- `ESTIMATED`：通过统一估算函数计算。
- `UNAVAILABLE`：无法得到可靠值。

整轮 `accuracy` 枚举：

- `EXACT`：每个供应商请求的输入和输出 Token 均来自供应商。
- `MIXED`：至少有一个精确值，同时至少有一个估算或缺失值。
- `ESTIMATED`：没有精确值，但所有调用均有估算值。
- `UNAVAILABLE`：没有可形成有效总量的数据。

规则：

1. 聚合单位是供应商请求，不是 Agent 步骤。
2. 结构化输出修复会再次请求供应商，必须计为独立调用。
3. 最终回答续写是独立调用，必须累加。
4. `thinkingTokens` 单独展示，但不得重复计入 `totalTokens`。
5. 供应商只返回部分 usage 时，缺失部分允许估算，但整轮准确度不得为 `EXACT`。
6. 失败请求只要已发给供应商，也必须保留调用记录；没有 usage 时标记估算或不可用。
7. mock provider 的 Token 默认视为 `ESTIMATED`，不得标记为真实计费数据。

## 6. 总体设计

### 6.1 文件边界

新增核心文件：

```text
app/services/turn_metrics.py
```

它负责所有指标数据结构、单调时钟、上下文绑定、调用记录和汇总。业务文件只负责在确定的生命周期节点调用该模块，不得各自实现一套计时或 Token 求和逻辑。

建议修改范围：

| 文件 | 责任 |
|---|---|
| `app/services/turn_metrics.py` | 新增指标契约、Collector、ContextVar 和汇总逻辑 |
| `app/services/ai.py` | 在公共完成入口记录每次供应商请求、TTFT、usage 和失败 |
| `app/services/agent_models.py` | 给 AiClient 注入 Agent purpose namespace |
| `app/services/chat.py` | 建立/释放 Turn 指标上下文，记录 T0/T3/T4，持久化汇总 |
| `app/core/config.py` | 增加 OpenAI 流式 usage 开关 |
| `.env.example` | 记录新配置 |
| `app/harness/runner.py` | 新增 Turn Metrics Harness、`metrics` suite 别名和统一报告明细 |
| `README.md` | 补充一键指标验证命令、suite 说明和报告位置 |
| `tests/test_turn_metrics.py` | Collector 单元测试 |
| `tests/test_ai_completion.py` | usage-only 流帧和调用记录测试 |
| `tests/test_chat_completion.py` | 端到端聚合、续写、失败和直接回复测试 |
| `tests/test_chat_turns.py` | 幂等、断线重连和快照时序回归 |

首版不需要 Alembic 迁移。指标放入现有 JSON 字段，待未来确有 SQL 聚合、索引或报表需求时再独立建表。

### 6.2 请求级上下文

使用 `contextvars.ContextVar` 绑定当前 `TurnMetricsCollector`。原因：

- `_generate()` 是按 requestId 创建的独立 asyncio Task。
- 同一 Task 内同步 Agent 调用和异步最终生成都会继承上下文。
- AiClient 不需要层层传递 collector 参数。
- 并发请求不会共享可变全局统计对象。

禁止使用单一模块级可变 Collector，也禁止只按“当前用户”关联指标。

示意接口：

```python
@contextmanager
def bind_turn_metrics(collector: TurnMetricsCollector): ...

def current_turn_metrics() -> TurnMetricsCollector | None: ...

def mark_first_content_ready() -> None: ...
```

`ContextVar` 没有绑定时，所有观测函数必须安全 no-op，保证离线任务、单元测试和非聊天调用不受影响。

### 6.3 AiClient purpose

为 `AiClient` 增加只读 `purpose_namespace`，默认值为 `"application"`：

```python
AiClient(settings, purpose_namespace="understanding")
AiClient(settings, purpose_namespace="safety")
AiClient(settings, purpose_namespace="knowledge")
AiClient(settings, purpose_namespace="response")
```

`AgentModelRegistry.client_for()` 根据 Agent 名称注入 namespace。这样无需在 Autonomous Agent 的每个调用点手工传 Collector。

调用 purpose 规则：

| 调用 | purpose |
|---|---|
| Understanding 普通完成 | `understanding.complete` |
| Safety 普通完成 | `safety.complete` |
| Knowledge 结构化完成 | `knowledge.<schema_name>` |
| 最终回答首次生成 | `response.generate` |
| 最终回答续写 | `response.continuation` |
| 无法识别的调用 | `<namespace>.unspecified` |

`complete()`、`complete_structured()` 和 `stream_events()` 可接受可选 `purpose` 覆盖值，但默认从 namespace 生成。生产聊天链路不应出现裸 `unspecified`。

## 7. 数据契约

### 7.1 单次模型调用

`turn_metrics.py` 中建议使用 dataclass，不引入新的 Pydantic 层：

```python
@dataclass
class ModelCallMetric:
    sequence: int
    purpose: str
    provider: str
    model: str
    stream: bool
    started_offset_ms: int
    duration_ms: int | None = None
    ttft_ms: int | None = None
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    estimated_prompt_tokens: int | None = None
    estimated_output_tokens: int | None = None
    prompt_token_source: str = "UNAVAILABLE"
    output_token_source: str = "UNAVAILABLE"
    finish_reason: str = ""
    status: str = "STARTED"
    error_code: str = ""
```

约束：

- 不保存 messages、Prompt、输出正文或正文 hash。
- `sequence` 在一次 Turn 内从 1 递增。
- `ttft_ms` 只适用于流式调用。
- `status` 至少支持 `COMPLETED`、`FAILED`、`CANCELLED`。
- `duration_ms` 优先采用 AiClient 观测到的完整请求时长；供应商 metadata 的时长用于交叉校验，不得重复相加。

### 7.2 Turn 汇总

最终写入 `generation_metadata_json` 的结构升级为 schemaVersion 2。保留所有现有字段，并新增 `turnMetrics`：

```json
{
  "schemaVersion": 2,
  "source": "MODEL",
  "finishReason": "STOP",
  "promptTokens": 1200,
  "outputTokens": 380,
  "durationMs": 2100,
  "turnMetrics": {
    "schemaVersion": 1,
    "requestId": "request-id",
    "status": "COMPLETED",
    "startedAt": "2026-08-02T02:00:00+00:00",
    "completedAt": "2026-08-02T02:00:03+00:00",
    "finalModelTtftMs": 240,
    "serverE2eTtftMs": 1840,
    "firstContentReadyMs": 1990,
    "serverTurnDurationMs": 3020,
    "orchestrationBeforeModelMs": 1600,
    "tokenUsage": {
      "promptTokens": 2480,
      "outputTokens": 610,
      "thinkingTokens": 80,
      "totalTokens": 3090,
      "accuracy": "EXACT",
      "providerCallCount": 5,
      "exactCallCount": 5,
      "estimatedCallCount": 0,
      "unavailableCallCount": 0
    },
    "calls": []
  }
}
```

说明：

- 顶层旧 `promptTokens`、`outputTokens`、`durationMs` 继续表示最终回答生成，保证兼容。
- `turnMetrics.tokenUsage` 才表示整轮全部 LLM 调用。
- `calls` 存放精简的单次调用指标，不保存内容。
- `AgentRunTrace.generation_json` 写入相同结构。
- 旧记录没有 `turnMetrics` 时，读取方必须正常工作。
- 现有 `schemaVersion: 1` 读取逻辑不得因升级而失败。

### 7.3 字段命名

Python dataclass 内部使用 snake_case；持久化 JSON 使用现有 Chat generation 风格的 camelCase。序列化只允许在 `turn_metrics.py` 中实现一次，禁止调用方手工转换字段名。

## 8. 详细实现要求

### 8.1 新增 `app/services/turn_metrics.py`

实现以下能力：

1. `TurnMetricsCollector`：保存 T0、墙上时间、调用序列、首次事件和终态。
2. `ModelCallHandle`：一个供应商请求的生命周期句柄。
3. `bind_turn_metrics()`：ContextVar 上下文管理器，必须在 `finally` 中 reset token。
4. `start_model_call()`：无上下文时返回 no-op handle。
5. `mark_model_first_delta()`：只接受第一个非空 delta，后续调用不覆盖。
6. `finish_model_call()`：记录 metadata、估算值、状态和错误。
7. `mark_first_content_ready()`：只记录第一次非空内容准备完成。
8. `mark_turn_finished()`：只记录一次终态。
9. `as_dict()`：生成隐私安全、确定性排序的 JSON 数据。

Collector 必须支持注入：

```python
clock_ns: Callable[[], int]
utc_now: Callable[[], datetime]
```

单元测试不得依赖真实 sleep。

Token 估算可复用 `app.services.context_builder.estimate_tokens()`：

- Prompt 估算为所有 message 的估算和。
- 输出估算基于最终可见 content。
- 估算只在供应商相应字段缺失时使用。
- 聚合结果必须保留来源状态。

### 8.2 修改 `app/services/agent_models.py`

`client_for(agent_name)` 创建 AiClient 时注入 namespace：

```python
namespace = AGENT_MODEL_ALIASES.get(agent_name, ...)
return AiClient(settings, purpose_namespace=namespace)
```

ResponseAgent 的上下文窗口校验逻辑保持原样。

### 8.3 修改 `app/services/ai.py`

#### 普通完成

`complete()` 每次调用执行：

1. 创建 call handle，记录 purpose、provider、model、messages 估算和 `stream=False`。
2. 正常返回前使用 completion metadata 完成记录。
3. 捕获包含 metadata 的协议异常并记录失败。
4. 捕获没有 metadata 的异常时仍记录 duration、估算 Prompt 和 `UNAVAILABLE` usage，然后原样抛出。
5. 不改变现有异常类型和回退行为。

#### 结构化完成

必须在 `complete_structured()` 的 `while True` 内、每次 `_complete_with_schema()` 前后建立独立 call handle。

原因：一次语义结构化调用可能发生一次 schema repair，实际产生两次供应商请求。若只在 `complete_structured()` 外层记录，会少算调用次数和 Token。

每次记录包含：

- purpose：`<namespace>.<schema_name>`
- repair index：可编码在独立字段或 purpose 后缀，但必须可区分第一次与修复请求。
- provider completion metadata。
- schema 校验失败不抹掉已发生的供应商用量。

#### 流式完成

`stream_events()` 必须：

1. 在开始迭代供应商流前创建 call handle。
2. 第一次收到 `kind="delta"` 且 `text` 非空时记录模型 TTFT。
3. 收到 terminal event 时记录 usage、finish reason 和完整 duration。
4. 流异常或调用方取消时记录失败/取消，随后保持原异常语义。
5. 不因为埋点吞掉 `IncompleteGenerationError`、`ModelProtocolError` 或 `CancelledError`。
6. 不缓存完整输出两份；只为缺失 output usage 保留必要的累计字符或估算状态。

#### OpenAI 流式 usage

配置开启时，流式 payload 增加：

```json
"stream_options": {"include_usage": true}
```

解析循环必须先读取 usage，再判断 choices：

```python
if isinstance(data.get("usage"), dict):
    usage = _openai_usage(data["usage"])

choices = data.get("choices") or []
if not choices:
    continue
choice = choices[0]
```

usage-only 帧不是错误。必须继续保留：

- `finish_reason` 语义终止校验。
- `[DONE]` 传输终止校验。
- 内容过滤、长度终止和 EOF 行为。

新增配置：

```python
openai_stream_include_usage: bool = True
```

`.env.example` 增加：

```text
OPENAI_STREAM_INCLUDE_USAGE=true
```

对于不支持该参数的 OpenAI-compatible 服务，运维可以显式关闭。禁止在请求失败后静默重试一次完整生成，因为这可能重复计费和生成。

### 8.4 修改 `app/services/chat.py`

#### T0 和幂等

在 `start_chat()` 入口立即读取单调时钟和 UTC 时间，但只有 `created=True` 的新 ChatTurn 才启动新 Collector。

重复 `requestId` 或断线重连：

- 不得重置 T0。
- 不得创建第二个 Collector。
- 不得重复统计模型调用。
- 继续读取原 ChatTurn 和原指标。

将 T0 参数传给 `_generate()`，并在 `_generate()` 最外层绑定 Collector。绑定范围必须覆盖：

- `MindBridgeAgentHarness.run()`
- 全部 Agent LLM 调用
- RAG 结构化调用
- 最终模型流
- 失败终态持久化

#### 最终回答 purpose

调用 `_run_completion_attempt()` 时明确传入：

- 首次：`response.generate`
- 续写：`response.continuation`

续写创建新 AiClient 时保留原 `purpose_namespace`。

#### T3

`_persist_progress()` 只有在 `content` 非空且快照/数据库状态已更新后调用 `mark_first_content_ready()`。只记录第一次，不被后续快照覆盖。

直接回复调用同一路径，因此能够得到 `firstContentReadyMs`。

#### T4 和持久化

在生成成功或失败即将构造最终 metadata 时：

1. 调用 `collector.mark_turn_finished(status)`。
2. 使用现有 `_generation_metadata()` 构造兼容字段。
3. 设置 `schemaVersion=2`。
4. 加入 `turnMetrics=collector.as_dict()`。
5. 通过现有 `_finalize_completed_turn()` 或 `_finalize_failed_turn()` 原子写入 ChatTurn 和 Trace。

不要在 `_after_completed_turn()` 之后才停止计时。会话摘要更新和工具派发发生在用户回答终态之后，不属于本轮用户可见生成时延；若未来需要，应作为 post-response 指标单独统计。

#### SSE

V1 不向学生 SSE 暴露完整 `calls`。可以保持现有 DTO 不变。

若产品需要展示汇总，只允许在 `done` 中增加以下非敏感字段，并需同步 DTO 和测试：

- `serverE2eTtftMs`
- `serverTurnDurationMs`
- `totalTokens`
- `tokenAccuracy`

默认实施选择：仅持久化，不修改学生 UI。

### 8.5 Trace 与 KnowledgeAgent

KnowledgeAgent 现有 `llm_calls` 保留，用于解释 Knowledge Orchestrator 内部行为。

全链路汇总只读取新的 Turn Collector，不得执行：

```text
turn collector totals + knowledge diagnostics llm_calls totals
```

否则 Knowledge 调用会重复计数。

Trace 服务不应重新计算指标，只负责原样持久化已经冻结的 generation metadata。

### 8.6 接入 Engineering Harness

端到端指标必须进入现有 `app/harness/runner.py`，作为独立的确定性 suite，而不是只依赖 pytest。

#### Suite 注册

在 argparse choices 中加入：

```text
metrics
```

在 `resolve_suites()` 中注册：

```python
("Turn Metrics Harness", run_turn_metrics_harness)
```

别名映射：

```python
"metrics": "Turn Metrics Harness"
```

该 suite 必须包含在默认 `all_suites` 中，因此以下两个命令都可工作：

```powershell
python -m app.harness.runner --suite metrics
python -m app.harness.runner --suite all
```

推荐将 `Turn Metrics Harness` 放在 `RAG Harness` 之后、`API Harness` 之前。这样指标链路先验证内部 ChatService/Trace 契约，再由 API Harness 验证 HTTP/SSE 外层行为。

#### 执行环境

默认 Harness 已固定使用：

- mock AI
- 临时 SQLite
- 内存短期记忆
- 禁用向量强依赖
- 禁用外部工具队列

Turn Metrics Harness 必须复用同一 `HarnessContext`、`reset_database()` 和 `collect_chat_stream()`，不得自行连接生产数据库、Redis 或真实模型。

默认 suite 不测试真实延迟性能，只测试指标的生成、数学关系、生命周期和隐私。真实供应商性能属于可选 live 检查，不得进入 `--suite all`。

#### 必测场景

`run_turn_metrics_harness(context)` 至少运行以下确定性场景：

| case | 路径 | 必须验证 |
|---|---|---|
| `normal_generation` | 正常 mock 流式 STOP | 两项 TTFT、首内容、总耗时、调用数、Token 汇总和 Trace 一致性 |
| `continuation` | 第一次 LENGTH、第二次 STOP | `response.generate` 与 `response.continuation` 各一次，Token 只累加一次 |
| `direct_response` | 规则澄清或应用直接回复 | 模型 TTFT 为 null，首内容时间和终态存在 |
| `provider_failure` | 首 delta 前或后供应商失败 | 终态失败，已发生调用保留，指标状态不伪造为完整 |
| `duplicate_request` | 相同 requestId 重复提交/重连 | 只执行一次生成，providerCallCount 不增长，T0 不重置 |

场景注入必须经过正式指标观测入口。禁止直接 patch `AiClient.stream_events()` 后再声称验证了流式埋点；这会绕过被测代码。可采用以下方式之一：

1. patch mock provider 的私有响应生成方法或 terminal metadata 构造，使公共 `stream_events()` 继续执行；
2. 为 Harness 注入只实现 provider 层协议的确定性 client，并让 AiClient 公共观测层包裹它；
3. 使用 Collector 可注入时钟和供应商事件 fixture，但最终持久化必须仍经过 `ChatService`。

不得为了 Harness 在生产代码中加入“检测到测试环境后改变业务结果”的分支。

#### 硬门禁

以下失败必须让 suite 返回非零：

1. generation JSON 缺少 `turnMetrics` 或 schema 版本错误。
2. 正常模型回答缺少 `finalModelTtftMs`、`serverE2eTtftMs` 或 `serverTurnDurationMs`。
3. 任意耗时为负数。
4. `serverE2eTtftMs < finalModelTtftMs`。
5. 正常模型路径 `serverTurnDurationMs < serverE2eTtftMs`。
6. Token 总数不等于输入与输出之和。
7. thinking Token 被重复计入 total。
8. continuation 调用缺失或重复计数。
9. ChatTurn 和 AgentRunTrace 的 `turnMetrics` 不一致。
10. 重复 requestId 导致第二次模型生成或 Token 增加。
11. 指标结构包含用户原文、Prompt、回答正文或知识正文。
12. SSE 不再满足 `meta -> snapshot* -> error? -> done`。

以下内容只记录，不作为默认硬门禁：

- TTFT 必须低于某个绝对毫秒数。
- P50/P95/P99 延迟。
- mock provider 下的 `EXACT` accuracy。
- 不同开发机器之间的耗时对比。

#### Harness 报告

结果写入现有：

```text
target/harness/harness-report.json
```

`Turn Metrics Harness` 的 `details` 建议结构：

```json
{
  "caseCount": 5,
  "cases": [
    {
      "id": "normal_generation",
      "status": "COMPLETED",
      "providerCallCount": 3,
      "tokenAccuracy": "ESTIMATED",
      "finalModelTtftMs": 1,
      "serverE2eTtftMs": 8,
      "serverTurnDurationMs": 12
    }
  ],
  "invariantsChecked": 12,
  "privacyChecked": true
}
```

报告不得复制完整 `calls` 中可能无限增长的内容，也不得包含 request message、Prompt 或回答正文。允许输出 purpose、provider、model、计数和数值型汇总。

#### README

实现时更新 README 的 Engineering Harness 列表，增加：

```text
Turn Metrics Harness：验证端到端 TTFT、全链路 Token、续写、失败、幂等和 Trace 一致性。
```

同时增加单 suite 命令：

```powershell
python -m app.harness.runner --suite metrics
```

现有 `--suite all` 和报告路径保持不变。

## 9. 异常和特殊路径

| 场景 | TTFT | Token | 终态要求 |
|---|---|---|---|
| 正常 STOP | 记录 | 精确或估算 | `COMPLETED` |
| LENGTH 后续写 STOP | 首次生成首 delta | 两次调用累加 | `COMPLETED` |
| 两次 LENGTH | 首次生成首 delta | 两次调用累加 | `FAILED` |
| PROVIDER_EOF 且有 partial | 有首 delta则记录 | 已知部分保留 | `FAILED` |
| 首 delta 前网络失败 | `null` | Prompt 估算、输出不可用 | `FAILED` |
| CONTENT_FILTER | 可能为 `null` | usage 可用则保留 | `FAILED`，不得泄露 partial |
| asyncio 取消 | 已发生则保留 | 已发生调用保留 | `INTERRUPTED` |
| 应用直接回复 | 模型 TTFT 和 E2E TTFT 为 `null` | 前置调用照常累计 | 正常终态 |
| 纯规则直接澄清 | 两项 TTFT 为 `null` | 通常为 0 | 正常终态 |
| Redis 不可用 | 不影响模型 TTFT | 正常统计 | `firstContentReadyMs` 以数据库回退可读为准 |
| 重连 | 不重新计时 | 不重复统计 | 返回原终态指标 |
| 进程崩溃后 stale 恢复 | 无法恢复内存时间线 | 标记不完整 | `INTERRUPTED` |

stale 恢复元数据必须允许：

```json
"turnMetrics": {
  "schemaVersion": 1,
  "status": "INCOMPLETE",
  "incompleteReason": "PROCESS_RESTART"
}
```

禁止根据 `created_at` 和当前时间伪造 TTFT。

## 10. 测试方案

### 10.1 `tests/test_turn_metrics.py`

使用可控 fake clock 覆盖：

1. 首次 delta 只记录一次。
2. 多次调用 sequence 稳定递增。
3. 精确 Token 聚合。
4. 精确值与估算值混合时 accuracy 为 `MIXED`。
5. 全估算时 accuracy 为 `ESTIMATED`。
6. `thinkingTokens` 不重复计入 total。
7. 结构化修复两次请求计为两次。
8. 失败调用仍进入 calls。
9. 无 ContextVar 时所有 helper no-op。
10. 嵌套或并发 ContextVar 不串 requestId。
11. `as_dict()` 不包含 message/content/prompt 等敏感键。
12. 所有时间字段非负且满足定义的不变量。

### 10.2 `tests/test_ai_completion.py`

新增：

1. OpenAI payload 在开关启用时包含 `stream_options.include_usage=true`。
2. 关闭开关时不发送该字段。
3. usage-only 帧且 `choices=[]` 不抛协议错误。
4. usage-only 帧后 `[DONE]` 仍产生合法 terminal metadata。
5. 流式首个空 delta 不记录 TTFT，首个非空 delta 才记录。
6. 普通完成失败保留调用记录。
7. mock usage 标记为估算。

### 10.3 `tests/test_chat_completion.py`

新增或扩展：

1. 正常回答的 generation schemaVersion 为 2 且包含 `turnMetrics`。
2. 一次最终生成时，全链路调用数和 Token 总数正确。
3. LENGTH + continuation 时两次响应调用均被统计。
4. Knowledge 诊断存在时不会在全链路 totals 中重复计算。
5. 直接回复的两项 TTFT 为 null，`firstContentReadyMs` 有值。
6. EOF、内容过滤、取消和异常路径仍持久化不完整指标。
7. 顶层旧 generation 字段仍存在且语义不变。
8. Trace generation 与 ChatTurn generation 一致。

注意：现有测试直接 patch `AiClient.stream_events` 公共方法。这样会绕过公共入口的指标埋点。新增指标集成测试应优先 patch provider 私有实现或注入测试 client；若必须 patch公共方法，应由 fixture 显式触发测试指标事件，不能误以为生产埋点已执行。

### 10.4 `tests/test_chat_turns.py`

覆盖：

1. 同一 `requestId` 的重复提交只产生一份指标。
2. reader 断开不取消后台统计。
3. 重连不会重置 T0 或增加 providerCallCount。
4. Redis 不可用时指标仍能随数据库终态保存。
5. SSE 顺序继续满足 `meta -> snapshot* -> error? -> done`。

### 10.5 Harness 测试

对 `app/harness/runner.py` 增加或扩展测试，覆盖：

1. `--suite metrics` 能被 argparse 接受。
2. `resolve_suites(["metrics"])` 只返回 `Turn Metrics Harness`。
3. `resolve_suites(["all"])` 包含该 suite。
4. suite 任一硬门禁失败时 `CheckResult.passed=False`，主进程退出码为 1。
5. suite 通过时 details 可 JSON 序列化且不含正文。
6. `harness-report.json` 包含 `Turn Metrics Harness` 结果。
7. 正常、续写、直接回复、失败和 duplicate request 五个 case 均被实际执行。

Harness 内不得断言绝对耗时上限。测试只断言字段存在、非负、顺序关系和确定性的计数。

### 10.6 回归命令

实施 AI 必须根据项目实际 Python 环境执行，至少包括：

```powershell
python -m pytest tests/test_turn_metrics.py -q
python -m pytest tests/test_ai_completion.py tests/test_ai_structured_completion.py -q
python -m pytest tests/test_chat_completion.py tests/test_chat_turns.py -q
python -m pytest tests/test_knowledge_orchestrator.py tests/test_trace_privacy.py -q
python -m app.harness.runner --suite metrics
python -m app.harness.runner --suite all
python -m pytest -q
```

如果仓库存在独立 Engineering Harness，还必须执行其当前 README 规定的 release/basic 命令，不得猜测命令或伪造结果。

## 11. 实施顺序

### 阶段 0：冻结基线

1. 读取工作树状态和现有改动。
2. 运行相关测试并记录真实结果。
3. 确认 OpenAI/Ollama 当前测试 fixture 契约。

### 阶段 1：指标契约

1. 新增 `tests/test_turn_metrics.py`。
2. 新增 `turn_metrics.py` 的 dataclass、Collector、ContextVar 和序列化。
3. 只运行 Collector 测试直至通过。

### 阶段 2：AiClient 全调用观测

1. 给 AiClient 增加 namespace。
2. 在普通、结构化、流式公共入口接入调用句柄。
3. 确保 schema repair 每次供应商请求独立计数。
4. 修复 OpenAI usage-only 流帧兼容。
5. 运行 AI completion 和 Knowledge structured completion 测试。

### 阶段 3：ChatTurn 生命周期

1. 在新请求入口记录 T0。
2. 在后台生成 Task 中绑定 Collector。
3. 标注首次回答和续写 purpose。
4. 在首次非空 progress 后记录 T3。
5. 成功和失败都持久化 schemaVersion 2 指标。
6. 运行 chat completion/turn tests。

### 阶段 4：Engineering Harness

1. 在 runner CLI、suite 列表和 alias 中注册 `metrics`。
2. 实现五个确定性 Turn Metrics case。
3. 将结构、不变量、计数、幂等、Trace 一致性和隐私设为硬门禁。
4. 将耗时数值作为观察结果写入统一报告，不设置绝对性能阈值。
5. 单独运行 `--suite metrics`，通过后再运行 `--suite all`。

### 阶段 5：隐私、回归和文档

1. 验证 Trace 不含 Prompt 和正文。
2. 验证旧 JSON 兼容。
3. 运行完整 pytest 和 Harness。
4. 更新 README 中的观测字段、Turn Metrics Harness 和 `--suite metrics` 命令，但不添加学生端功能介绍。
5. 检查改动文件编码和 Unicode 转义。

不得跳过阶段 1 直接在 `chat.py` 中堆叠字典和计时变量。

## 12. 验收标准

以下条件必须全部满足：

1. 每个新 ChatTurn 的终态 generation JSON 包含 `turnMetrics.schemaVersion=1`。
2. 正常模型回答同时具有 `finalModelTtftMs` 和 `serverE2eTtftMs`。
3. `serverE2eTtftMs >= finalModelTtftMs >= 0`。
4. `serverTurnDurationMs >= serverE2eTtftMs`。
5. 直接回复不伪造模型 TTFT。
6. 路由、安全、Knowledge、最终回答和续写均进入同一 requestId 汇总。
7. schema repair 和续写不会漏算，也不会重复算。
8. OpenAI usage-only 帧被正确解析。
9. Token 总量带有 `EXACT/MIXED/ESTIMATED/UNAVAILABLE` 状态。
10. 推理 Token 不被重复加入总量。
11. 重复请求和重连不会产生第二份统计。
12. Redis 故障不阻断指标随数据库终态落盘。
13. 指标 JSON 不包含用户输入、Prompt、模型正文、知识正文或个人信息。
14. 现有 SSE、完成校验、安全、RAG、上下文预算和隐私测试全部通过。
15. 所有新增或修改文件保持 UTF-8，中文未被转义。
16. `python -m app.harness.runner --suite metrics` 返回 0 并写入统一报告。
17. `python -m app.harness.runner --suite all` 包含 `Turn Metrics Harness` 且整体通过。
18. 默认 Harness 不连接真实模型、不依赖公网，也不以绝对耗时阈值阻断发布。

## 13. 代码审查重点

审查者必须重点检查：

- 是否把最终模型总耗时错误命名为 TTFT。
- 是否从 HTTP meta 事件开始计为首 token。
- 是否把第一批 30 字符快照时间当作模型 TTFT。
- 是否使用 `datetime.now()` 直接计算耗时。
- 是否遗漏结构化修复请求。
- 是否把 Knowledge `llm_calls` 与 Collector 重复相加。
- 是否在异常路径丢失已经发生的用量。
- 是否将估算值标记为精确值。
- 是否重复计算 reasoning/thinking tokens。
- 是否因埋点改变异常传播、完成校验或 SSE 顺序。
- 是否在全局变量中共享 Collector 导致并发串数据。
- 是否把 Prompt 或正文写入指标。
- 是否通过 patch `AiClient.stream_events()` 绕过正式埋点后仍宣称 Harness 覆盖了指标链路。
- 是否把开发机上的绝对毫秒值设成默认 release 硬门禁。
- 是否注册了 `metrics` 但遗漏加入 `--suite all` 或统一报告。

## 14. 第二阶段：浏览器真实端到端首字时间

只有明确需要浏览器体验指标时才实施：

1. `student.js` 在提交前记录 `performance.now()`。
2. `consumeChatStream()` 第一次处理非空 snapshot 并更新 DOM 后记录 `clientFirstRenderMs`。
3. 使用独立、受鉴权、幂等的前端指标回传接口。
4. 服务端按 `requestId + clientMeasurementId` 去重。
5. 不接受客户端回传的 Token 数作为权威数据。
6. 浏览器指标与服务端指标分字段保存，不进行伪精确合并。

该阶段预计再修改 `student.js`、DTO/routes 和一个服务层入口。V1 未完成前不得提前实现。

## 15. 金额成本扩展

若后续“Token 花费”需要换算人民币或美元，必须新增版本化价格配置，至少区分：

- provider 和精确 model ID。
- 输入 Token 单价。
- 输出 Token 单价。
- cached input Token 单价（供应商支持时）。
- 价格币种、单位和生效时间。
- 未知模型的 `costStatus=UNAVAILABLE`。

金额计算不得硬编码在 `chat.py`，不得使用模型名称模糊匹配后静默套价，也不得对 `ESTIMATED` Token 输出“实际费用”。

## 16. 交付要求

实施完成后的最终报告必须包含：

1. 实际修改文件。
2. 最终指标 JSON 示例，示例不得含真实用户内容。
3. 正常、续写、直接回复、失败四条路径的真实测试结果。
4. duplicate request/重连不重复统计的 Harness 结果。
5. `--suite metrics` 和 `--suite all` 的真实退出码及报告路径。
6. OpenAI 流式 usage 是否为供应商精确返回。
7. 全链路 Token accuracy 的含义和当前限制。
8. 未执行的测试及原因。
9. 已知限制，例如进程崩溃前未落盘的内存时间线无法恢复。

交付前执行编码检查：

```powershell
git diff --check
rg -n '\\u[0-9a-fA-F]{4}' app tests .env.example README.md
```

若第二条命中正常源码字符串、注释或 UI 中文，必须恢复为直接可读中文；协议测试明确需要匹配 Unicode 转义的场景应逐条人工确认。
