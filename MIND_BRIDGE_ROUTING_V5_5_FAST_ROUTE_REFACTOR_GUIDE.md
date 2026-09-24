# MindBridge V5.5.4：规则/向量快速路由改造实施指南

> 目标：在当前已完成的 Routing V5.4 基础上，以最小改动增加 Rule + Embedding 快速路由：
>
> 1. 在 LLM Planner 之前增加 Rule + Embedding 快速路由；
> 2. 只有“非常明确的简单单意图请求”允许绕过 Planner；
> 3. 多意图、同领域多目标、依赖关系、上下文依赖请求继续进入 Planner；
> 4. Planner 失败后复用前面已经计算好的 Rule + Embedding 分数，执行现有 V5.4 的 Direct / Broadcast / 固定兜底；
> 5. Embedding prototype 不进入 ChromaDB，使用进程级共享向量缓存；
> 6. 本轮不实现指代消解小模型，相关能力后续单独设计。
>
> 本文档面向代码 AI，可直接作为代码改造规格。除本文明确要求外，不扩大改造范围。

本次改造后：

```text
                          用户输入
                              │
                              ▼
                    Rule + Embedding 打分
                              │
                              ▼
                        Fast Route Gate
                        │             │
                 明确简单单意图       其他情况
                        │             │
                        ▼             ▼
                  NORMAL Fast      LLM Planner
                  1 WorkItem        │
                                    ├── 成功 → NORMAL
                                    │
                                    └── 连续失败
                                            │
                                            ▼
                                 复用前面的 score snapshot
                                            │
                                 ┌──────────┼──────────┐
                                 │          │          │
                                 0          1          N
                                 │          │          │
                               固定       Direct    Broadcast
                               兜底
```

---

# 29. 同一 Intent 多个目标也必须 Planner

例如：

```text
我想办理休学，再告诉我复学流程和复学后的选课安排。
```

即使：

```text
ACADEMIC = 0.98
```

仍然：

```text
requires_planner = true
```

因为 Planner 还负责：

```text
WorkItem decomposition
dependency
```

Fast Router 不能替代这些能力。

---

# 30. Planner 失败后必须复用入口分数

禁止：

```text
入口 Rule + Embedding
→ Planner
→ Planner 失败
→ 再算一遍 Rule + Embedding
```

必须：

```text
snapshot = scorer.score(routing_input)

Fast 不通过
↓
Planner
↓
失败
↓
直接 reuse snapshot
```

这样：

- 少一次 Embedding；
- 分数一致；
- Trace 更容易解释；
- 避免同一请求前后结果漂移。

---

# 31. Embedding prototype 不放 ChromaDB

## 结论

本次：

```text
不要放 ChromaDB。
```

当前：

```python
PROTOTYPES = {
    CHAT: (...),
    ACADEMIC: (...),
    CAMPUS: (...),
    MENTAL: (...),
}
```

继续保留。

---

# 32. 为什么不放 Chroma

## 原因 1：数据量太小

这里只是：

```text
几十 ~ 一百多条固定 prototype
```

直接：

```text
query vector
×
prototype vector matrix
→ cosine
```

成本非常低。

不需要 ANN Vector DB。

---

## 原因 2：路由和知识库应该解耦

如果 prototype 也依赖 Chroma：

```text
Chroma 挂了
→ RAG 挂
→ Router Fast Path 也挂
```

没有必要。

更好的架构：

```text
Routing prototype
→ 内存向量缓存

RAG knowledge chunks
→ Chroma
```

---

## 原因 3：prototype 是代码配置，不是知识资产

它们本质上是：

```text
routing classifier semantic anchors
```

不是：

```text
学校政策知识文档
```

所以它们应该与：

```text
routing taxonomy / routing code version
```

一起版本管理。

---

## 原因 4：需要完整计算四个 Intent 分数

Fast / Degraded Router 需要：

```text
CHAT
ACADEMIC
CAMPUS
MENTAL
```

四类完整 score map。

当前实现：

```python
for intent in PRIMARY_INTENTS:

    similarity = max(
        cosine(query, prototype)
        ...
    )
```

非常直接。

Chroma TopK 检索反而需要额外保证：

```text
每个 Intent 都有候选
```

否则不适合生成稳定的四类 score map。

---

# 33. 什么时候才考虑把 prototype 放 Vector DB

只有未来出现：

```text
上千 / 上万 prototype
动态租户 taxonomy
prototype 需要在线增删
不同学校有完全不同路由 taxonomy
```

才考虑：

```text
专门 routing vector index
```

即使未来这么做，也建议：

```text
单独 routing collection
```

不要和：

```text
RAG knowledge collection
```

混在一起。

---

# 34. 当前真正要解决的是 prototype 缓存生命周期

当前 `PrimaryEmbeddingRouter`：

```python
self._vectors = None
```

只缓存到 Router 实例。

同时当前：

```python
create_agent_runtime(...)
```

每次调用都会：

```python
EventDrivenAgentRuntimeService(db, settings)
```

因此不能把：

```text
“放到 EventDrivenAgentRuntimeService”
```

等价理解为“应用级缓存”。

---

# 35. 推荐：进程级 prototype vector cache

第一版采用模块级、线程安全 cache：

```python
_PROTOTYPE_CACHE_LOCK = threading.Lock()

_PROTOTYPE_VECTOR_CACHE: dict[
    tuple[str, str, str],
    dict[IntentType, list[list[float]]],
] = {}
```

key 建议：

```text
(provider, model, prototype_version_or_hash)
```

例如：

```python
PROTOTYPE_VERSION = "routing-v5.5.1"
```

或计算 prototype 文本 hash。

`PrimaryEmbeddingRouter._prototype_vectors()`：

```text
1. 先查 process cache
2. 命中直接复用
3. 未命中获取 lock
4. double-check
5. embedding prototypes
6. 写 cache
7. 返回
```

不要在持有 DB Session 的对象上做全局 singleton。

---

# 36. Runtime 仍可注入 scorer，但不要把它当缓存边界

为了测试和依赖注入，仍可让：

```python
AgentRuntimeServices
```

携带：

```python
primary_routing_scorer
```

但其职责只是：

```text
本次请求复用同一 scorer / snapshot
```

真正跨请求复用 prototype vector 的能力来自：

```text
process-level prototype cache
```

验证测试必须创建：

```text
两个不同 EventDrivenAgentRuntimeService / 两个不同 PrimaryEmbeddingRouter
```

并确认同一 cache key 下：

```text
prototype embedding 总共只发生一次
```

---

# 37. 可选：模型 / prototype 变化时重建缓存

第一版内存缓存即可。

但缓存 key 至少要隐含绑定：

```text
embedding provider
embedding model
prototype code version
```

如果服务进程重启：

```text
重新 embedding 一次
```

完全可以接受。

当前不需要：

```text
Chroma
Redis vector cache
数据库持久化 prototype vectors
```

避免过度设计。

---

# 38. diagnostics、PlanSource 与 Trace 对齐

## 38.1 PlanSource

修改：

```text
app/services/route_planning.py
```

把：

```python
PlanSource = Literal[
    "HIGH_RISK_HARD",
    "LLM_ACCEPTED",
    "LLM_LIMIT",
    "RULE_FALLBACK",
    "RULE_LIMIT_FALLBACK",
    "FAST_RULE_EMBEDDING",
]
```

Fast Route：

```text
planSource = FAST_RULE_EMBEDDING
llmInvoked = false
logicalInvocationCount = 0
providerAttemptCount = 0
routingMode = NORMAL
fallbackReason = ""
```

## 38.2 FallbackReason

现有 V5.4 `classify_route()` 会使用：

```text
PLAN_VALIDATION_FAILED
```

因此同步把该值加入 `FallbackReason` Literal。

Fast reject：

```text
TOP1_TOO_LOW
MULTIPLE_CANDIDATES
...
```

不能写入 `fallbackReason`。

---

## 38.3 PlanningDiagnostics 新字段

增加默认字段：

```python
routing_scores: dict[str, float] | None = None


fast_route_reason: str = ""
```

`as_metadata()` 输出：

```text
routingScores
fastRouteReason
```

正常 Fast Route 的 score：

```text
routingScores
```

不要伪装成：

```text
degradedScores
```

---

## 38.4 Trace 白名单必须同步

当前：

```text
app/services/trace.py::_route_metadata()
```

只允许旧字段。

必须加入非敏感诊断字段：

```text
routingMode
routingScores
degradedScores
ruleScores
embeddingScores
fastRouteReason
```

否则：

```text
PlanningDiagnostics.as_metadata()
虽然生成成功
↓
Trace 保存时仍会被白名单静默删除
```

如 `turn_execution.py` 的 route summary 需要展示 plan source / mode，
同步补充非敏感摘要即可，不要写原文。

---

# 39. 不要默认 Trace 原始 rewrittenText

为避免把完整对话内容重复写入 metadata：

默认只写：

```text
coreferenceInvoked=true
coreferenceResolved=true
fastRouteReason=...
```

不要默认记录：

```text
originalInput
rewrittenInput
```

若项目已有受控 Debug 配置，可以仅 Debug 时记录。

---


# 41. 测试：Fast Route

新增：

```text
tests/test_routing_v5_fast_path.py
```

至少覆盖：

### F1 明确 CAMPUS

```text
国家助学金什么时候发？
```

模拟分数：

```text
CAMPUS      0.97
ACADEMIC    0.60
MENTAL      0.30
CHAT        0.20
```

期望：

```text
Planner calls = 0
NORMAL
1 CAMPUS WorkItem
planSource=FAST_RULE_EMBEDDING
```

---

### F2 明确 ACADEMIC

```text
怎么办理休学？
```

高置信 ACADEMIC。

期望：

```text
Fast Direct
```

---

### F3 多领域

```text
焦虑睡不着，同时想办理休学。
```

```text
MENTAL 0.97
ACADEMIC 0.96
```

期望：

```text
Planner calls = 1
禁止 Fast Broadcast
```

---

### F4 Top1 高但 Top2 竞争

```text
CAMPUS   0.96
ACADEMIC 0.92
```

期望：

```text
Planner
```

---

### F5 Margin 不够

```text
CAMPUS   0.951
ACADEMIC 0.900
```

margin：

```text
0.051 < 0.08
```

期望：

```text
Planner
```

---

### F6 同领域多个目标

```text
办理休学，再告诉我复学流程和复学后的选课。
```

即使：

```text
ACADEMIC 0.98
```

期望：

```text
Planner
```

---

### F7 Rule / Embedding 冲突

Rule：

```text
ACADEMIC 0.97
```

Embedding Top1：

```text
CAMPUS 0.96
```

期望：

```text
Planner
```

---

### F8 Embedding unavailable

Rule：

```text
CAMPUS 0.98
```

Embedding：

```text
None
```

期望：

```text
正常阶段不 Fast
→ Planner
```

---

### F9 Planner 失败复用 snapshot

要求 mock 计数：

```text
PrimaryRoutingScorer.score()
只调用 1 次
```

Planner 连续失败后：

```text
不再次 embedding
```

---

### F10 Planner 失败多领域

入口 snapshot：

```text
MENTAL 0.96
CAMPUS 0.95
```

Planner 连续失败：

```text
DEGRADED_BROADCAST
```

---

### F11 Planner 失败 0 Intent

全部：

```text
< 0.90
```

期望：

```text
固定程序兜底文本
```

### F12 Fast Embedding timeout

Embedding 超过：

```text
route_fast_embedding_timeout_seconds
```

期望：

```text
Fast reject
Planner 正常调用
不得等待知识检索默认长 timeout
```

### F13 跨 Runtime prototype cache

创建两个不同 Runtime / Router 实例。

期望：

```text
prototype embed_documents(prototype_texts)
仅发生 1 次
query embedding 正常分别发生
```

### F14 Trace 字段落盘

期望 trace route metadata 实际包含：

```text
planSource
routingMode
routingScores
fastRouteReason
```

而不是只在 `PlanningDiagnostics` 内存对象中存在。

---

# 42. 现有测试需要注意

当前：

```text
tests/test_routing_v5.py
```

里很多测试默认：

```text
每次都先调用 Planner
```

Fast Route 加入后，测试可能因为输入太简单直接 Fast 而失败。

不要为了让旧测试通过而禁用新架构。

正确做法：

对“专门测试 Planner”的测试显式：

```python
settings.route_fast_enabled = False
```

或者注入一个：

```text
不会 Fast accept 的 RoutingScoreSnapshot
```

这样区分：

```text
Planner contract test
Fast Route test
Degraded test
```

---

# 42.5 Routing Evaluator 对齐

当前：

```text
app/evaluation/evaluators/routing.py
```

仍把：

```text
RISK
riskRecall
highRiskMissCount
```

当作 Router 指标。

V5.5.1 必须删除 Router 对 RISK 的要求。

Routing evaluator 只评：

```text
CHAT
ACADEMIC
CAMPUS
MENTAL
```

Safety 继续由独立 Safety evaluator 评测。

建议新增：

```text
fastRoutePrecision
fastRouteCoverage
plannerInvocationRate
plannerInvocationReduction
degradedRecoveryRate
```

其中最重要 gate：

```text
fastRoutePrecision
```

第一版宁可 Fast Coverage 低，也不要为了减少 Planner 调用牺牲总体正确率。

多轮指代数据单独打 tag：

```text
```

不要把 上下文承接问题误算成 Safety 或 RISK routing failure。

---

# 43. 推荐改造文件清单

## 新增

```text
tests/test_routing_v5_fast_path.py
```

## 修改

```text
app/core/config.py

app/services/agent_models.py

app/services/routing_v5.py

app/agents/routing.py

app/agents/autonomous.py

app/agents/event_driven_runtime.py

app/services/route_planning.py
```

必要时修改：

```text
app/evaluation/evaluators/routing.py
tests/test_routing_v5.py
```

---

# 44. 推荐实施顺序

## Phase 0：只审计，不改行为

确认：

```text
classify_route() 所有调用点
GlobalDegradedRouter 所有调用点
PrimaryEmbeddingRouter 生命周期
UnderstandingAgent / Planner 当前调用位置
PlanningDiagnostics 所有构造位置
routing evaluator 依赖
```

---

## Phase 1：抽取 PrimaryRoutingScorer

从：

```text
GlobalDegradedRouter
```

拆出：

```text
score()
```

确保原有 degraded 测试仍通过。

---

## Phase 2：进程级 Embedding prototype 缓存

实现：

```text
process-level cache
+ lock
+ provider/model/prototype-version key
```

验证两个不同 Router / Runtime 实例：

```text
同一 cache key 下 prototype embed 只发生一次
```

---

## Phase 3：Fast Routing Embedding 短超时

增加：

```python
route_fast_embedding_timeout_seconds: float = 3.0
```

Fast Embedding timeout / unavailable：

```text
Fast Gate reject
→ 立即 Planner
```

---

## Phase 4：FastRouteGate

实现：

```text
Top1 >= 0.95
Competing >= 0.90
Margin >= 0.08
复杂度 guard
上下文依赖 guard
Rule / Embedding conflict guard
Embedding availability guard
```

---

## Phase 5：接入 classify_route()

顺序：

```text
score once
→ Fast Gate
→ Planner
→ reuse snapshot degraded
```

---

## Phase 6：Trace / Evaluator 对齐

完成：

```text
PlanSource Literal
FallbackReason Literal
trace.py route metadata allowlist
Routing evaluator 去除 RISK 路由指标
Fast Route metrics
```

---

## Phase 7：评测与阈值调优

重点指标：

```text
Fast Route Precision
Fast Route Coverage
Planner Invocation Reduction
Primary Intent Accuracy
Multi-domain Recall
Planner Failure Recovery Rate
```

优先级：

```text
Precision
>
总体 Accuracy 不下降
>
Planner 调用降低
>
Coverage
```


# 45. 第一版阈值不要自动调整

当前固定：

```text
Fast min = 0.95
Competing = 0.90
Margin = 0.08
Degraded = 0.90
```

先跑 routing-v5 数据集。

不要第一版就：

```text
动态阈值
自适应阈值
在线学习
概率校准
```

等评测结果出来再决定。

---

# 46. 关于“置信度”的用词

Embedding：

```text
semantic similarity score
```

Rule：

```text
rule evidence strength
```

Fusion：

```text
routing score
```

不要对外描述为：

```text
95% 概率
```

因为当前没有做 probability calibration。

简历 / 面试可以说：

```text
高置信路由分数
```

但进一步解释时要说明：

```text
这是工程阈值分数，不是统计概率。
```

---

# 47. AI 修改代码时必须遵守的约束

把下面内容原样交给代码 AI：

```text
请基于当前项目代码实施本方案，不要重写整个 Router。

约束：

1. V5.4 RoutePlan schemaVersion=5 不变；
2. 不增加二级 Intent；
3. 不增加新的业务 Agent；
4. 本轮不实现 Coreference / 指代消解小模型；
5. SafetyAgent 保持现有独立执行机制；
6. Fast Route 只允许高精度单 Intent Direct；
7. 正常多 Intent 必须进入 LLM Planner，不得 Broadcast；
8. DEGRADED_BROADCAST 只用于 Planner 连续失败；
9. Planner 失败后必须复用入口 Rule + Embedding snapshot；
10. Embedding prototype 不进入 ChromaDB；
11. prototype vectors 必须使用真正跨 Runtime 的进程级共享缓存；
12. Fast Embedding 必须使用独立短超时，故障后立即继续 Planner；
13. 不改 RAG；
14. 不改 evidenceFacets Stage A 兼容策略；
15. 不删除 V3/V4 兼容代码；
16. Routing evaluator 不再要求 Router 输出 RISK；
17. 每完成一个 Phase 都运行相关测试并汇报：
    - 修改文件
    - 关键 diff
    - 测试结果
    - 未完成项
18. 如果旧测试与 Fast Route 新行为冲突，应显式关闭 Fast Route 来测试 Planner contract，
    不要破坏新架构来迎合旧测试。
```

---

# 47.5 本轮必须通过的源码级回归检查

代码 AI 完成后，必须明确回答下面 6 个问题。

```text
[1] Fast Route 是否只放行明确的简单单意图？
    期望：YES

[2] 两个不同 PrimaryEmbeddingRouter / Runtime 是否会重复 embed prototype？
    期望：NO（同 cache key 下）

[3] Fast Routing Embedding 是否仍使用 30 秒 timeout？
    期望：NO
    Fast timeout 应 <= 3 秒；知识/RAG Embedding 可继续保留自己的长 timeout

[4] Planner 失败后是否会重新计算 Rule + Embedding？
    期望：NO
    应复用入口 snapshot

[5] 新 routing diagnostics 是否真正进入 Trace？
    期望：YES

[6] Routing Evaluator 是否仍要求 RISK case / riskRecall？
    期望：NO
```

如果任意一项不满足，不算本轮改造完成。

---


# 48. 最终验收链路

最终必须能够解释下面 4 条链路。

## A. 简单明确问题

```text
“国家助学金什么时候发？”
↓
Rule + Embedding
↓
CAMPUS >= 0.95
且唯一高分、margin 足够、无冲突
↓
Fast Direct
↓
1 CAMPUS WorkItem
↓
Planner 不调用
```

## B. 多领域复杂问题

```text
“最近焦虑睡不着，同时想办理休学。”
↓
Rule + Embedding 多个高分
↓
Fast reject
↓
Planner
↓
MENTAL + ACADEMIC WorkItems
```

## C. 上下文依赖请求

```text
“第二种呢？”
↓
Fast complexity/context guard reject
↓
Planner + ContextView
```

本轮不做 Coreference。

## D. Planner 故障

```text
Rule + Embedding snapshot
↓
Fast reject
↓
Planner failure × 2
↓
reuse snapshot
↓
0 → 固定兜底
1 → DEGRADED_DIRECT
N → DEGRADED_BROADCAST
```


# 49. 最终架构职责边界

```text
Rule + Embedding Fast Router
= 这是一个非常明确的简单单意图吗？

LLM Planner
= 这个复杂请求应该拆成几个 WorkItem？
  每个 WorkItem 属于哪个一级 Intent？
  是否存在强依赖？

Global Degraded
= Planner 已经失败后，系统还能怎么继续？

Specialist
= 这个领域问题具体怎么处理？

RAG
= 为了回答这个 WorkItem，需要检索什么证据？
```

这些职责不要重新混在一起。

---

# 50. 最终推荐版本号

本次代码审查修正版建议标记为：

```text
Routing V5.5.4
```

含义：

```text
V5.4
= Context-aware LLM Planner
+ Global Degraded Rule/Embedding

V5.5.4
= Rule/Embedding Fast Path
+ Fast Embedding Fail-Fast Budget
+ Score Snapshot Reuse
+ Process-level Prototype Vector Cache
+ Trace / Evaluator Contract Alignment
```

这是在 V5.4 上的增量优化，不需要重新推翻既有架构。
