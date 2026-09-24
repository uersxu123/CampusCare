# MindBridge V5.4 降级阈值校准（当前可复现部分）

## 1. 当前选择机制

V5.4 Global Degraded 不是固定 Top-K。CHAT / ACADEMIC / CAMPUS / MENTAL 四类先分别得到 score，随后保留所有 `score >= threshold` 的 Intent：0 个走固定澄清，1 个 Direct，2~4 个 Broadcast。

项目当前有两个独立阈值：

- `route_degraded_min_score=0.90`：Rule + Embedding 融合；
- `route_rule_only_min_score=0.90`：Embedding 不可用时 Rule-only。

## 2. 新建阈值校准集

新建 `routing_threshold_calibration_160.jsonl`，共 160 条，与原 208 条业务评测集分开，避免直接在测试集上调参：

- ACADEMIC 单类 25 条；
- CAMPUS 单类 25 条；
- MENTAL 单类 25 条；
- CHAT 单类 25 条（其中加入带专业领域词但真实目标仍是翻译/编程/润色的 hard negatives）；
- 多领域 30 条；
- 应 abstain 的上下文依赖/信息不足表达 30 条，Gold 为 `[]`。

其中 hard negatives 例如：

- `用 Python 写一个识别焦虑文本的分类器` → CHAT；
- `帮我把“国家助学金申请条件”翻译成英文` → CHAT；
- `帮我润色一句“我准备办理休学”` → CHAT；
- `我和室友关系很差很难受，同时想申请调宿` → MENTAL + CAMPUS；
- `这个怎么办 / 那第二种呢 / 还是刚才那个问题` → []（Planner 已失败时 fallback 不应猜历史指代）。

## 3. Rule-only 实测

当前容器无法访问用户本机 Ollama，因此融合阈值暂不能在这里得到真实 BGE-M3 分数；Rule-only 可以完全复现。

### 校准集 160 条

| 阈值 | Exact | Micro P | Micro R | Micro F1 | Macro F1 |
|---:|---:|---:|---:|---:|---:|
| 0.90 | 0.7688 | 0.9353 | 0.7927 | 0.8581 | 0.8462 |
| 0.95 | 0.7688 | 0.9353 | 0.7927 | 0.8581 | 0.8462 |
| 0.955 | 0.7562 | 0.9343 | 0.7805 | 0.8505 | 0.8394 |
| 0.965 | 0.2500 | 0.8824 | 0.1829 | 0.3030 | 0.2525 |
| 0.975 | 0.2062 | 0.8000 | 0.0244 | 0.0473 | 0.0426 |

30 条 Gold=[] 的 abstain 样例在 0.90~0.95 下均 100% 正确 abstain。

### 原 208 条验证集

| 阈值 | Exact | Micro P | Micro R | Micro F1 | Macro F1 |
|---:|---:|---:|---:|---:|---:|
| 0.90 | 0.6010 | 0.9628 | 0.6962 | 0.8080 | 0.7907 |
| 0.95 | 0.6010 | 0.9628 | 0.6962 | 0.8080 | 0.7907 |
| 0.955 | 0.5962 | 0.9676 | 0.6885 | 0.8045 | 0.7877 |
| 0.965 | 0.1106 | 0.9762 | 0.1577 | 0.2715 | 0.2259 |

### Rule-only 建议

`route_rule_only_min_score` 建议从 **0.90 调到 0.95**。

原因：0.90~0.95 在当前两套数据上指标完全相同，因为现有有效强规则最低主要就在 0.95；0.95 是同一性能平台上的最严格阈值，可以拒绝未来可能加入的 0.90~0.94 弱规则。阈值超过 0.95 后开始损失真实召回，超过 0.96 后出现断崖下降。

## 4. Rule + BGE-M3 融合阈值

融合阈值不能从 Rule-only 结果直接推导。必须用真实 `bge-m3` 对 calibration 数据生成四类 embedding score，再按项目真实公式 `0.4 * rule + 0.6 * embedding` 扫阈值。

已提供 `tune_degraded_threshold.py`。在能访问本机 Ollama 的环境运行：

```powershell
python tune_degraded_threshold.py `
  --project .\MindBridge_V7_5_Routing_V5_4_refactored `
  --calibration .\routing_threshold_calibration_160.jsonl `
  --validation .\routing_eval_v5_business_208.jsonl `
  --ollama-url http://localhost:11434 `
  --embedding-model bge-m3:latest `
  --min-threshold 0.80 `
  --max-threshold 0.98 `
  --step 0.005
```

脚本会输出 raw score、完整 threshold sweep 和最终推荐值。推荐规则透明地约束：整体 precision ≥ 0.90、多领域 recall ≥ 0.80、abstain accuracy ≥ 0.90，在满足这些条件的候选里选择 Macro-F1 / Micro-F1 最优阈值。
