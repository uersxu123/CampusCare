# 给 ChatGPT Work：MindBridge Fast Route 性能优化版复测

请直接在我的本机环境执行，不要只给建议。

我提供的是上一轮 V5.5 Fast Route 的性能优化版。上一轮真实结果作为基线：

```text
Planner-only Mean/P50/P95 ≈ 687.77 / 647.86 / 917.32 ms
Fast Route Mean/P50/P95 ≈ 842.97 / 850.12 / 1119.64 ms
Warm Rule+Embedding scorer ≈ 213 ms
Fast Coverage = 8.17%（17/208）
Fast Precision = 94.12%
Planner invocation = 191/208
```

上一轮主要瓶颈：

1. 每请求临时创建 httpx.Client / SSLContext；
2. 明显复杂请求先算 Embedding 再进入 Planner；
3. prototype 冷启动由首请求承担；
4. X29“也想知道”未被复杂度 Gate 拦截。

本版已经修改：

- Embedding `httpx.Client` 进程级复用；
- 请求级不再先 `/api/tags` preflight；
- cheap complexity/context Pre-Gate 提前到 Embedding 前；
- 增加“也想/还想/又想”等并列目标结构；
- prototype 向量预归一化；
- startup 正式预热 BGE-M3 + prototype；
- benchmark 支持 A/B 顺序交替。

本轮仍固定：

```text
fast_min = 0.95
competing = 0.90
margin = 0.08
Fast Embedding timeout = 3s
warmup timeout = 30s
Rule/Embedding = 0.40/0.60
```

## 1. 先跑契约测试

```powershell
$env:DATABASE_URL='sqlite+pysqlite:///:memory:'
$env:PYTHONPATH='.'
pytest -q tests/test_routing_v5_fast_path.py tests/test_routing_v5.py tests/test_routing_v5_runtime.py tests/test_embedding_client_reuse.py tests/test_embedding_and_index.py
```

预期：30 passed。

## 2. 检查 Ollama

确认本机 Planner 和 Embedding 模型；优先使用：

```text
qwen3:8b
bge-m3:latest
```

如果实际名称不同，用本机已存在模型替代，不要下载新模型。

## 3. 跑三轮完整 208 A/B

为减少顺序偏差，交替运行：

### Run 1

```powershell
python tools/benchmark_fast_route_v55.py `
  --planner-model qwen3:8b `
  --embedding-model bge-m3:latest `
  --dataset eval_fast_route/routing_eval_v5_business_208.jsonl `
  --mode-order planner-first `
  --output-dir target/fast-route-opt-run1
```

### Run 2

```powershell
python tools/benchmark_fast_route_v55.py `
  --planner-model qwen3:8b `
  --embedding-model bge-m3:latest `
  --dataset eval_fast_route/routing_eval_v5_business_208.jsonl `
  --mode-order fast-first `
  --output-dir target/fast-route-opt-run2
```

### Run 3

```powershell
python tools/benchmark_fast_route_v55.py `
  --planner-model qwen3:8b `
  --embedding-model bge-m3:latest `
  --dataset eval_fast_route/routing_eval_v5_business_208.jsonl `
  --mode-order planner-first `
  --output-dir target/fast-route-opt-run3
```

不要手工把 Fast request timeout 放宽到 120 秒。本版 startup warmup 应使用独立 30 秒预算，实际请求保持 3 秒。

## 4. 必须验证性能优化是否真的生效

重点检查：

1. `prototypeWarmupMs` 单独记录；冷启动不能混进请求 latency；
2. `routingScorerWarmupMs` / warm request scorer latency 与上一轮约 213 ms 对比；
3. HTTP 审计确认同一进程请求阶段不会为每个 Query 新建连接导致大额固定成本；
4. 请求级不应每次先调用 `/api/tags`；
5. prototype 在同一进程仍只向量化一次；
6. `COMPLEX_OR_CONTEXT_DEPENDENT` 请求在 Planner 成功时应 `routingScoreLatencyMs=0`；
7. X29：`刚入学很不适应，也想知道新生宿舍入住怎么办。` 必须进入 Planner，不得 Fast Direct。

## 5. 汇总与上一轮对比

三轮都汇总：

- Exact / Macro-F1 / Micro-F1
- Single 160 Exact
- Multi 48 Exact / P/R/F1
- Boundary F1
- 四类 P/R/F1
- Fast Precision / Coverage
- Planner count/rate
- Mean / P50 / P95
- Fast Direct latency
- Fast-reject-entered-Planner latency
- Cheap Pre-Gate → Planner latency
- warm Rule+Embedding scorer latency
- prototype startup warmup latency
- planSource / fastRouteReason 分布

额外生成一张对比表：

```text
上一轮未优化 Fast V5.5
vs
本轮优化 Fast V5.5
vs
本轮 Planner-only
```

上一轮关键参考：

```text
Fast scorer ≈ 213ms
Fast Mean ≈ 842.97ms
Fast P50 ≈ 850.12ms
Fast P95 ≈ 1119.64ms
```

## 6. 校准先不要改生产参数

仍可以跑：

```powershell
python tools/tune_fast_route_gate.py `
  --embedding-model bge-m3:latest `
  --dataset eval_fast_route/routing_threshold_calibration_160.jsonl `
  --output-dir target/fast-route-opt-tuning
```

但这一步只报告候选，不自动修改 0.95/0.90/0.08。

先回答：在**相同固定阈值**下，代码级性能优化是否已经让 Fast Route 变得更快。

## 最终请生成

```text
FAST_ROUTE_OPTIMIZED_FINAL_REPORT.md
optimized_3run_summary.json
optimized_latency_breakdown.json
optimized_false_fast_cases.jsonl
optimized_pre_gate_cases.jsonl
```

报告最后明确回答：

1. warm scorer 是否从约 213ms 显著下降？下降多少？
2. Fast Route Mean/P50/P95 是否低于 Planner-only？
3. X29 是否修复，Fast Precision 是否改善？
4. 如果仍然更慢，剩余固定成本具体在哪里？
5. 是否值得继续保留 Fast Route，还是 Planner-only 对当前硬件/模型更划算？
