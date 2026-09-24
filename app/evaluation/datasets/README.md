# MindBridge Evaluation Datasets

`routing-v3.jsonl` 是唯一活跃的正式路由数据集，严格覆盖 RoutePlan V3 的五类 Intent、上下文关系、原文分段、HARD_DATA、ORDER_ONLY、缺参和容量保护。数据均为合成数据，不含真实学生信息。

本次人工迁移使用的旧输入仅保存在 Git 历史中：

- 业务契约输入：242 条，SHA-256 `85308949b016694842b6f9ccf8530f8f3a23bde79fc52e58212449315b28d0f5`；
- 语义规划输入：160 条，SHA-256 `d074294198e58b4da6a33af8d4b2d3443d11c4a66f8a6f7fe4cc86f9822d0e39`；
- 更早的冻结输入：222 条，SHA-256 `bf39afcd15481bf2a3646efeba6ac5ff121f1618a7601c296affbfe105e4daac`。

迁移结果为 411 条：保留业务契约 242 条、语义规划 160 条，并新增 9 条 V3 上下文/容量控制样例；ID 重名合并 0 条、因语义重复删除 0 条、待审 0 条。最终 primary Intent 分布为 CAMPUS 231、CHAT 53、RISK 50、MENTAL 46、ACADEMIC 31。文件 SHA-256 为 `04a068e7c34c9aa4a699a9aeb98b1495946a779e2265b9aa280112d0901c7956`。

`mindbridge-e2e-ragas-v1.jsonl` 由 181 条 RAG golden cases 和已核验知识快照构建；`mindbridge-e2e-ragas-v1.audit.json` 记录来源指纹、构建版本与输出 hash。`e2e-safety-v1.jsonl` 独立保存高风险端到端安全用例，不进入普通 RAGAS 平均分。

规范化并校验唯一 V3 路由数据：

```powershell
python scripts/build_routing_dataset.py
```

运行正式路由评测：

```powershell
$env:RUN_ROUTING_MODEL_EVAL='1'
python -m app.evaluation.runner --suite routing --profile full
```

重建 RAGAS 数据与审计：

```powershell
python scripts/build_ragas_dataset.py
```

正式 Docker Golden 可改用 `--database-url-from-settings --chroma-persist-dir data/chroma`。Golden 字段只能进入 Evaluator 或 Judge，不得传入被测路由、Planner、Grader 或最终回答模型。
