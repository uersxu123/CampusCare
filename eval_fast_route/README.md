# Fast Route Evaluation Kit

本目录用于比较：

- `planner_only`：关闭 Fast Route，作为 V5.4-style Planner-only 基线；
- `fast_route`：开启 Rule + Embedding Fast Route 的 V5.5 候选。

数据：

- `routing_eval_v5_business_208.jsonl`：208 条 hold-out 业务评测集；
- `routing_threshold_calibration_160.jsonl`：160 条阈值校准集；
- `PREVIOUS_V3_VS_V5_4_REPORT.md`：此前 V3 vs V5.4 真实 Ollama 评测报告，仅作历史参照；
- `PREVIOUS_THRESHOLD_TUNING.md`：此前 degraded threshold 校准记录。

## 1. 单元 / 契约测试

Linux/macOS:

```bash
DATABASE_URL='sqlite+pysqlite:///:memory:' PYTHONPATH=. pytest -q \
  tests/test_routing_v5.py \
  tests/test_routing_v5_fast_path.py \
  tests/evaluation/test_routing_evaluator.py \
  tests/test_privacy_and_assessment.py
```

PowerShell:

```powershell
$env:DATABASE_URL='sqlite+pysqlite:///:memory:'
$env:PYTHONPATH='.'
pytest -q tests/test_routing_v5.py tests/test_routing_v5_fast_path.py tests/evaluation/test_routing_evaluator.py tests/test_privacy_and_assessment.py
```

当前交付环境结果：`22 passed`。

## 2. 先验证 Fast Gate 阈值

```bash
python tools/tune_fast_route_gate.py \
  --embedding-model bge-m3:latest \
  --dataset eval_fast_route/routing_threshold_calibration_160.jsonl
```

固定生产候选仍是：

- min score = `0.95`
- competing = `0.90`
- margin = `0.08`

脚本只给建议，不自动改配置。

## 3. Planner-only vs Fast Route 完整对比

```bash
python tools/benchmark_fast_route_v55.py \
  --planner-model qwen3:8b \
  --embedding-model bge-m3:latest \
  --dataset eval_fast_route/routing_eval_v5_business_208.jsonl \
  --output-dir target/fast-route-eval-run1
```

建议至少重复 3 次，输出到 run1/run2/run3。

报告包括：

- Overall Exact Accuracy
- Macro-F1 / Micro-F1
- 单标签 Exact
- Multi-label Exact / Precision / Recall / F1
- Boundary Macro/Micro F1
- 四类 per-class F1
- Fast Route Precision / Coverage
- Planner Invocation Rate
- 平均 / P50 / P95 latency
- planSource / Fast reject reason 分布

## 4. 评测原则

Fast Route 第一优先级是 `Precision`。

如果 Fast Route 减少了 Planner 调用，但显著降低 Overall Accuracy / Macro-F1，不接受该配置。优先提高 `route_fast_min_score` 或 `route_fast_margin`，不要为了 Coverage 牺牲路由正确率。
