# MindBridge V3 vs V5.4 完整路由对比报告

## 评测配置与范围

- 同一 Ollama 实例：`http://127.0.0.1:11434`；LLM：`qwen3:8b`；embedding：`bge-m3:latest`。
- 两版均为 temperature=0、think=false、max_tokens=768、num_ctx=16384；保留各版原生 Prompt、契约、校验、重试及降级策略。
- 208 条逐条评测，两版各 208 条且 ID 唯一、完全对齐；single 切片 160 条、multi 切片 48 条；boundary 难度 57 条。multi 中 38 条双标签、7 条三标签、3 条单标签；全部样本无历史 context。
- 调用各自 `classify_route → UnderstandingService.classify → AiClient`，得到最终 RoutePlan；评测范围是完整路由主链，不含下游 specialist 回答或 RAG 问答质量。
- V3：Planner → reconcile → LLM/Rule/Embedding IntentFusion → RoutePlan；V5.4：Planner → Minimal Validator → NORMAL，失败才进入 Rule+Embedding Global Degraded。没有强制改写原生路由来增加 embedding 调用。
- 原项目 app 源码未修改。外层观测器只记录 HTTP 请求及响应、关联样本 ID；所有预测由原项目产生。冒烟测试单独保存，不混入正式结果。
- V5.4 全部 208 条均为 NORMAL，主链 embedding 请求为 0；另通过该项目原生 OllamaEmbeddingBackend 发起独立请求，bge-m3 成功返回 1024 维向量。此连通性检查与正式路由指标完全分开，见 new_embedding_backend_probe.json。

## 指标定义

- Overall Accuracy 采用严格标签集合完全一致率，与全量 Exact Match 相同。另列单标签准确率及 Primary Hit，避免将不同口径混用。
- Macro-F1：四类 one-vs-rest F1 的算术平均；四类 P/R/F1 均基于全量 208 条，未预测出的标签计 FN，多出的标签计 FP。
- 保留套件的四类投影口径：RISK 等四类外标签不参与四类 P/R/F1，只有 RISK 的路由被投影为空预测，gold 标签计 FN；原始完整标签仍保存在 RoutePlan，并另核对未投影的 Exact Match。
- Multi-label P/R/F1：仅 48 条 multi 样本，使用 micro 聚合；另附 sample 平均。
- Boundary F1：57 条 difficulty=boundary 样本的四类 Macro-F1，并列 Micro-F1；这里不是分词或 span 边界检测。
- Planner success：最终 planSource=LLM_ACCEPTED / 208；structured output failure：最终 fallbackReason=STRUCTURED_OUTPUT_INVALID / 208，属于重试后的最终失败率，不代表首次输出无效率。
- Retry rate：HTTP 审计中同一样本真实 /api/chat 请求数 > 1 的样本数 / 208，包含结构化修复及 Planner 重试。
- Fallback rate：最终来源 RULE_FALLBACK 的样本数 / 208；路由抛出异常单独统计，不误记为已执行 fallback。附带原脚本将 ERROR 也计入 fallback，该原始口径另存 JSON 的 kit_fallback_rate_including_errors。错误样本保留在全部指标分母。

## 核心结果

| 指标 | V3 | V5.4 | 差值 |
|---|---:|---:|---:|
| Overall Accuracy / 全量 Exact Match | 63.94% | 82.21% | +18.27 pp |
| Macro-F1 | 71.06% | 88.93% | +17.87 pp |
| Micro-F1 | 73.60% | 88.48% | +14.88 pp |
| 单标签 Accuracy（160 条） | 70.62% | 91.25% | +20.62 pp |
| Primary Hit（主意图属于 gold） | 75.00% | 92.79% | +17.79 pp |
| Multi-label Precision（micro） | 85.54% | 97.33% | +11.79 pp |
| Multi-label Recall（micro） | 71.00% | 73.00% | +2.00 pp |
| Multi-label F1（micro） | 77.60% | 83.43% | +5.83 pp |
| Multi-label Exact Match | 41.67% | 52.08% | +10.42 pp |
| Multi-label Precision（sample 平均） | 83.33% | 98.26% | +14.93 pp |
| Multi-label Recall（sample 平均） | 72.22% | 75.00% | +2.78 pp |
| Multi-label F1（sample 平均） | 74.86% | 82.29% | +7.43 pp |
| Boundary F1（macro） | 53.80% | 84.23% | +30.43 pp |
| Boundary F1（micro） | 71.23% | 82.52% | +11.28 pp |
| Planner success rate | 96.15% | 100.00% | +3.85 pp |
| Structured output failure rate（最终） | 0.00% | 0.00% | +0.00 pp |
| Retry rate | 0.00% | 0.00% | +0.00 pp |
| Fallback rate | 3.37% | 0.00% | -3.37 pp |
| Routing exception rate | 0.48% | 0.00% | -0.48 pp |

## 四类 Precision / Recall / F1

| 类别 | V3 P | V3 R | V3 F1 | V5.4 P | V5.4 R | V5.4 F1 |
|---|---:|---:|---:|---:|---:|---:|
| CHAT | 95.00% | 37.25% | 53.52% | 97.96% | 94.12% | 96.00% |
| ACADEMIC | 59.38% | 52.05% | 55.47% | 86.15% | 76.71% | 81.16% |
| CAMPUS | 71.11% | 92.75% | 80.50% | 91.07% | 73.91% | 81.60% |
| MENTAL | 95.45% | 94.03% | 94.74% | 98.46% | 95.52% | 96.97% |

## 真实调用与可靠性证据

| 项目 | V3 | V5.4 |
|---|---:|---:|
| 有 LLM 调用的样本 | 208.00 | 208.00 |
| LLM HTTP 请求数 | 208.00 | 208.00 |
| LLM HTTP 200 次数 | 208.00 | 208.00 |
| Embedding 请求数 | 258.00 | 0.00 |
| Embedding 成功次数 | 258.00 | 0.00 |
| 调用 embedding 的样本数 | 208.00 | 0.00 |
| Embedding 向量总数 | 272.00 | 0.00 |
| 发生重试的样本数 | 0.00 | 0.00 |
| 输出触及 token 上限次数 | 0.00 | 0.00 |
| 平均单样本耗时（ms） | 3514.14 | 836.35 |

### V3

- 来源计数：`{"LLM_ACCEPTED": 200, "RULE_FALLBACK": 7, "ERROR": 1}`。
- 最终 fallback 原因：`{"": 201, "SEGMENT_COVERAGE_FAILED": 7}`。
- Embedding 维度：`[1024]`。
- HTTP 请求数与诊断 providerAttemptCount 不一致样本：`['X22']`。
- 未处理异常样本：1。
- 四类之外的路由：`[{"id": "C21", "intents": ["RISK"]}, {"id": "H29", "intents": ["RISK"]}, {"id": "H32", "intents": ["RISK"]}, {"id": "X43", "intents": ["RISK"]}]`。
- 未投影的完整 RoutePlan Exact Match：63.94%。

### V5.4

- 来源计数：`{"LLM_ACCEPTED": 208}`。
- 最终 fallback 原因：`{"": 208}`。
- Embedding 维度：`[]`。
- HTTP 请求数与诊断 providerAttemptCount 不一致样本：`[]`。
- 未处理异常样本：0。
- 四类之外的路由：`[]`。
- 未投影的完整 RoutePlan Exact Match：82.21%。

## 难度分层

| 难度 | 样本数 | V3 Exact | V5.4 Exact | V3 Macro-F1 | V5.4 Macro-F1 |
|---|---:|---:|---:|---:|---:|
| easy | 88 | 63.64% | 87.50% | 72.49% | 91.99% |
| medium | 38 | 86.84% | 97.37% | 83.72% | 97.21% |
| boundary | 57 | 56.14% | 68.42% | 53.80% | 84.23% |
| hard | 25 | 48.00% | 72.00% | 65.39% | 80.64% |

## 配对变化与结论

- 逐样本变化：`{"improved": 53, "both_wrong": 22, "regressed": 15}`；完整错误及改善/退化记录见 `case_comparison.jsonl`。
- 全量严格准确率变化 +18.27 个百分点；Macro-F1 变化 +17.87 个百分点。
- 使用 scikit-learn 独立核对了全量 Exact Match、Macro/Micro Precision、Recall、F1，均一致。
- 改善主要体现在 CHAT 召回率；但 CAMPUS 召回率从 92.75% 降至 73.91%，multi 切片 Exact Match 仍只有 52.08%。总体提升并不代表每一类、每个样本都改善。
- V3 的 X22 在 RoutePlan 校验时抛出“RoutePlan 未完整覆盖当前输入”；HTTP 审计证实它实际调用过 Planner，但附带 runner 未保存异常前诊断，因此其 providerAttemptCount 缺失。重试率使用真实 HTTP 计数，避免受该缺失影响。
- 结果只代表此 208 条离线单轮业务集、当前模型量化和参数下的测量，不证明生产流量或多轮上下文上的同等提升。
- 两版保留自身的重试机制：V3 在 structured completion 内最多一次修复；V5.4 在 routing 层最多两次 Planner 调用。重试差异属于被比较的架构行为。
- 延迟包含模型加载、HTTP、校验和 embedding；顺序执行、缓存和硬件状态可能影响耗时，未进行多轮重复实验。

## 可复核文件

- `old_full_preds.jsonl` / `new_full_preds.jsonl`：逐样本标签、RoutePlan、诊断、耗时。
- `old_http_audit.jsonl` / `new_http_audit.jsonl`：真实请求的模型、参数、状态、LLM 原始响应、embedding 数量/维度。
- `full_routing_metrics.json`：完整指标、计数、配置、原始 ZIP / 数据集 / 源码 SHA-256。
- `ollama_models.json`：可用模型与 digest；`run_audited.py` / `build_report.py`：观测与汇总代码。
- 数据集 SHA-256：`ccdfcf575cf5c60a23214d130cc96251f744ed278d5fa19cb849572be12045c9`。
