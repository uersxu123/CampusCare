# MindBridge V5.5 Fast Route 性能优化说明

## 优化背景

上一轮真实 Ollama A/B：

- Planner-only mean ≈ 687.77 ms
- Fast Route mean ≈ 842.97 ms
- Fast scorer warm request ≈ 212~214 ms
- Fast Coverage = 8.17%（17/208）
- 91.83% 请求在支付 scorer 成本后仍进入 Planner

profile 显示每次新建 `httpx.Client` / SSLContext 是 scorer 固定成本的重要来源；另外复杂请求原实现先做 Embedding，再由 Gate 判断“必须 Planner”。

## 本次代码优化

### 1. Embedding HTTP Client 进程级复用

`app/services/embedding.py`

- Ollama / OpenAI Embedding backend 改为复用进程级 `httpx.Client`。
- cache key：provider + base_url + timeout + trust_env。
- 保留 Fast 3 秒与 Knowledge 30 秒两个独立 Client，避免超时语义互相污染。
- 进程退出时统一 close。

目标：消除每请求重复构造 transport / SSLContext / connection pool 的固定成本。

### 2. 去掉请求级 `/api/tags` preflight

`PrimaryEmbeddingRouter.scores()` 不再每个轻量 Router 实例先调用 `backend.available()`。

正式请求直接调用 `/api/embed`，失败由现有异常处理 fail-open 到 Planner。

`/api/tags` 只保留在 startup warmup / model digest 等真正需要的位置。

### 3. Cheap Pre-Gate 前移到 Embedding 之前

`app/agents/routing.py`

原链路：

```text
Rule + Embedding → requires_planner → Planner
```

改为：

```text
Cheap structural-risk pre-gate
├─ 多目标/上下文依赖/强依赖/文本变换风险 → Planner（不算 Embedding）
└─ 可能简单 → Rule + Embedding → Fast Gate
```

如果 Planner 后续真的失败，才补算一次 Rule + Embedding 进入 degraded，仍保留可用性。

当前 208 集上静态 Pre-Gate 命中 30/208（14.42%）；这些请求在 Planner 成功时不再支付 Fast scorer 成本。

### 4. 补并列目标表达

增加：

```text
也想 / 还想 / 又想 / 并想 / 并且想
```

从而让上一轮 false-fast：

```text
刚入学很不适应，也想知道新生宿舍入住怎么办。
```

在 Embedding 前直接进入 Planner。

### 5. Prototype 向量预归一化

prototype 初始化时一次性归一化；请求时 Query 只归一化一次，然后 cosine 退化为 dot product。

容器内 74 × 1024 维纯 Python 微基准：

- 原始 cosine mean ≈ 6.656 ms
- 预归一化 dot mean ≈ 2.873 ms
- CPU 部分约 2.32× 加速

该数字只是本地 CPU 微基准，不等价于 Ollama 端到端延迟。

### 6. 正式 Startup Warmup

新增：

```python
route_fast_warmup_enabled = True
route_fast_warmup_timeout_seconds = 30.0
```

应用 startup：

1. 用 `/api/tags` 做一次 startup-only 快速可达性检查；
2. 用 warmup budget 加载 BGE-M3 + 74 prototype；
3. prototype 放进进程级缓存；
4. 正式请求仍使用 3 秒 Fast Embedding timeout。

这样避免第一个用户承担 7~10 秒模型冷加载，也不需要评测脚本手工把 prototype timeout 改成 120 秒。

### 7. Prototype cache key 加入 base_url

避免同一 provider/model、不同 embedding endpoint 时错误复用 prototype vectors。

### 8. 评测脚本同步优化

`tools/benchmark_fast_route_v55.py`

- 正式调用 `warmup_fast_routing()`；
- 单独记录 `prototypeWarmupMs` 和 warm request scorer latency；
- 新增 `--mode-order planner-first|fast-first`，支持交替 A/B 顺序。

`tune_fast_route_gate.py` 也先走正式 warmup。

## 参数

本次不修改用户已确定的首轮参数：

```text
route_fast_min_score = 0.95
route_fast_competing_score = 0.90
route_fast_margin = 0.08
route_fast_embedding_timeout_seconds = 3.0
```

先验证“实现成本下降”再决定是否调整阈值。

## 测试结果

直接相关回归：

```text
30 passed
```

覆盖 Fast Route、V5 Router、Runtime、Embedding Client reuse、Embedding/Index、warmup、prototype cache、Pre-Gate、Planner-failure degraded。

更宽周边测试：

```text
优化前 V5.5：17 failed / 27 passed / 3 subtests passed
优化后 V5.5：17 failed / 27 passed / 3 subtests passed
```

失败集合来自旧 V3/RISK/旧单路径契约，没有新增周边回归。

## 仍需真实 Ollama 复测

当前执行环境无法访问用户本机 Ollama，因此不能在这里宣称优化后的 Mean/P50/P95 已下降。

下一轮重点比较上一轮真实值：

```text
warm scorer ≈ 213 ms
Fast overall ≈ 843 ms
Planner-only ≈ 688 ms
```

如果 shared Client 生效，warm scorer 应明显下降；最终是否整体快于 Planner-only 必须以同机 3 轮 A/B 为准。
