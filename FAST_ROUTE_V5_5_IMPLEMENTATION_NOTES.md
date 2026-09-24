# MindBridge Routing V5.5 Fast Route - Implementation Notes

## 本次实现

- 在 Planner 前增加 Rule + Embedding 二路融合 Fast Route；
- Fast Gate：`min_score=0.95`、`competing=0.90`、`margin=0.08`；
- 多目标、依赖、明显上下文承接请求保守升级 Planner；
- Rule/Embedding 强冲突禁止 Fast Direct；
- Planner 失败复用入口 `RoutingScoreSnapshot`，不重复 embedding；
- Fast Routing Embedding 独立 3 秒预算；
- routing prototype 使用进程级共享 cache + Lock + provider/model/prototype hash key；
- 新增 `FAST_RULE_EMBEDDING` planSource 和 Fast Route diagnostics；
- Trace allowlist 放行 routing/fast diagnostics；
- Routing Evaluator 移除旧 RISK gate，并增加 Fast/Planner 调用统计；
- 后端 `PrivacySanitizer` 业务调用删除，Memory/Context/Harness 保留用户原始文本；前端 DOMPurify 不动；
- 本轮不实现 Coreference。

## 新增评测

- `tests/test_routing_v5_fast_path.py`
- `tools/tune_fast_route_gate.py`
- `tools/benchmark_fast_route_v55.py`
- `eval_fast_route/routing_threshold_calibration_160.jsonl`
- `eval_fast_route/routing_eval_v5_business_208.jsonl`

## 本地可执行结果

使用：

```text
DATABASE_URL=sqlite+pysqlite:///:memory:
PYTHONPATH=.
```

本轮核心测试：

```text
22 passed
```

覆盖：

- V5 Router 原测试；
- Fast Direct；
- 多候选升级 Planner；
- 上下文依赖升级 Planner；
- Planner 失败 snapshot 复用；
- `raw_current_input` 不再覆盖 routing input；
- 跨 Router prototype cache；
- Fast Embedding 3 秒预算；
- Routing evaluator；
- 原始输入不脱敏保存。

## 周边旧测试基线对照

对同一批 legacy/周边测试：

```text
原始 V5.4：25 failed, 33 passed, 3 subtests passed
V5.5 候选：25 failed, 33 passed, 3 subtests passed
```

失败集中在旧 V3 / Router-RISK / schemaVersion=3 / “普通请求必调一次 Planner”等历史断言，本次没有新增该组失败。

## 当前环境无法完成的测试

当前容器无法访问用户本机 Ollama，因此没有伪造以下结果：

- BGE-M3 实际 Fast Route score；
- 208 条真实 LLM Planner A/B；
- Fast Route 真实 Planner 调用减少比例；
- 本机平均/P50/P95 latency。

这些由 `WORK_MODE_FAST_ROUTE_EVAL_PROMPT.md` 在 Work + 本机 Ollama 环境执行。
