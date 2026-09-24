# 给 ChatGPT Work 的执行提示词

你现在在我的本机/Work 环境中评测 MindBridge 路由 V5.5 Fast Route。请直接执行，不要只给建议。

项目已经完成 Rule + Embedding Fast Route 改造；本轮**不实现 Coreference/指代消解小模型**。Fast Route 参数固定候选为：

```text
route_fast_min_score = 0.95
route_fast_competing_score = 0.90
route_fast_margin = 0.08
route_fast_embedding_timeout_seconds = 3.0
Rule weight = 0.40
Embedding weight = 0.60
```

## 任务 1：环境和代码检查

1. 解压并进入项目根目录。
2. 检查 `ollama list`，确认实际可用的 Planner 模型和 Embedding 模型。
3. 优先使用项目原来配置的 Understanding/Planner 模型；如果名称与脚本默认 `qwen3:8b` 不同，用真实模型名覆盖命令参数，不要拉取新的大模型。
4. Embedding 优先使用本机已有 `bge-m3:latest`；若实际名称不同，使用 `ollama list` 中对应名称。
5. 不修改 Fast Route 阈值后再做首轮评测；先测当前固定配置。

## 任务 2：跑契约测试

先运行：

```powershell
$env:DATABASE_URL='sqlite+pysqlite:///:memory:'
$env:PYTHONPATH='.'
pytest -q tests/test_routing_v5.py tests/test_routing_v5_fast_path.py tests/evaluation/test_routing_evaluator.py tests/test_privacy_and_assessment.py
```

预期当前提交为 22 passed。如果失败，先定位是否环境依赖还是本次代码回归，并记录。

不要用旧 `test_route_plan_v3.py`、旧 Router-RISK 测试的失败来否定 V5.5；这些在原始 V5.4 上已经存在同样失败。若需要验证，分别在原始 V5.4 和新 V5.5 上跑同一组测试，比较“新增失败”而不是看绝对失败数。

## 任务 3：Fast Gate 校准检查

运行：

```powershell
python tools/tune_fast_route_gate.py `
  --embedding-model bge-m3:latest `
  --dataset eval_fast_route/routing_threshold_calibration_160.jsonl `
  --output-dir target/fast-route-tuning
```

重点报告当前固定 `0.95 / 0.90 / 0.08` 的：

- Fast Route Precision
- Fast Route Coverage
- accepted 数量
- reject reason 分布

同时给出 sweep 推荐，但**不要自动修改代码参数**。

优先目标：Fast Route Precision >= 98%。如果达不到，列出所有 false-fast 样本，分析是 Rule、Embedding、复杂度 Gate 还是边界 prototype 导致。

## 任务 4：208 条真实 Planner + Embedding A/B 对比

用相同模型、相同 208 条数据，运行 Planner-only 和 Fast Route。项目已经提供一键脚本：

```powershell
python tools/benchmark_fast_route_v55.py `
  --planner-model qwen3:8b `
  --embedding-model bge-m3:latest `
  --dataset eval_fast_route/routing_eval_v5_business_208.jsonl `
  --output-dir target/fast-route-eval-run1
```

如果本机 Planner 模型不是 `qwen3:8b`，替换为实际模型名。

至少完整跑 3 次：

```text
target/fast-route-eval-run1
target/fast-route-eval-run2
target/fast-route-eval-run3
```

不要只引用单次结果。前三次全部保留原始 predictions 和 metrics。

## 任务 5：汇总指标

为 Planner-only 与 Fast Route 分别汇总，至少给出：

1. Overall Exact Accuracy
2. Macro-F1
3. Micro Precision / Recall / F1
4. 160 条单标签 Exact / Top1 Hit
5. 48 条 Multi-label Exact
6. Multi-label micro Precision / Recall / F1
7. Multi-label sample F1
8. Boundary Macro-F1 / Micro-F1
9. CHAT / ACADEMIC / CAMPUS / MENTAL 各自 Precision / Recall / F1
10. Fast Route Precision
11. Fast Route Coverage
12. Planner Invocation Rate / Invocation Count
13. Planner 调用减少百分比
14. 平均单样本 wall latency
15. P50 latency
16. P95 latency
17. provider attempt count
18. planSource 分布
19. fastRouteReason 分布
20. error count

然后计算 Fast Route 相对 Planner-only 的差值（pp 或 ms）。

## 任务 6：和历史 V5.4 结果做 sanity check

`eval_fast_route/PREVIOUS_V3_VS_V5_4_REPORT.md` 是此前真实 Ollama 评测记录。历史 V5.4 在当时 208 条数据上的结果包括：Overall Exact 82.21%、Macro-F1 88.93%、Micro-F1 88.48%、平均单样本耗时约 836.35ms。

这些值只作为 sanity reference，不要求新环境复现完全相同数字，因为 Ollama 模型版本、量化、缓存和硬件状态可能变化。真正 A/B 结论必须以本次同一环境中的 `planner_only` vs `fast_route` 为准。

## 任务 7：重点检查 Fast Route 是否“错误优化”

单独导出：

- Fast Route 命中且预测错误的全部样本；
- Fast Route 命中 multi-label gold 的全部样本；
- Planner-only 正确但 Fast Route 版本错误的全部样本；
- Fast Route 因 `MULTIPLE_CANDIDATES` / `MARGIN_TOO_SMALL` / `RULE_EMBEDDING_CONFLICT` / `COMPLEX_OR_CONTEXT_DEPENDENT` 拒绝的代表样本；
- Fast Route 命中四类 Intent 的数量分布。

尤其检查：

```text
“休学期间助学金还发吗”
“考试压力让我焦虑失眠”
“和室友吵架很难受” vs “申请换宿舍”
“帮我翻译国家助学金申请条件”
“用 Python 写一个焦虑文本分类器”
```

避免专业关键词把真实 CHAT/MENTAL/CAMPUS 边界搞错。

## 任务 8：耗时解释

区分并报告：

- Routing scorer prototype cold warmup 耗时；
- warm cache 后每请求 Rule+Embedding 打分耗时；
- Fast Direct 请求平均/P50/P95；
- 进入 Planner 请求平均/P50/P95；
- Planner-only 总体耗时；
- Fast Route 总体耗时。

确认 prototype 在一个进程里只向量化一次；如果每个请求都重新 embed 全部 prototype，视为实现 bug。

确认 Fast Routing Embedding 超时预算不超过 3 秒；不要把 RAG/Knowledge 的 30 秒 embedding timeout 当 Fast Path 的 timeout。

## 最终交付

请生成：

```text
FAST_ROUTE_FINAL_EVAL_REPORT.md
fast_route_3run_summary.json
false_fast_cases.jsonl
planner_saved_cases.jsonl
latency_breakdown.json
```

报告最后只回答三个问题：

1. Fast Route 是否在准确率基本不下降的前提下明显减少 Planner 调用？
2. Fast Route 是否明显降低平均/P50/P95 耗时？
3. `0.95 / 0.90 / 0.08` 是否继续保留；如果建议调整，必须用 calibration + hold-out 数据说明原因，不要凭经验改。
