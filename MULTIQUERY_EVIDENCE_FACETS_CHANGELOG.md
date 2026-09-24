# MindBridge Evidence Facets + Multi-Query Retrieval 修改与评测报告

日期：2026-09-13

## 1. 本次代码改造结论

本次改造没有把同一个校园业务问题强行拆成多个 WorkItem，而是在现有 Understanding/Intent → WorkItem → Specialist → RAG 架构内增加 `evidenceFacets`：

- UnderstandingAgent 仍负责业务 WorkItem 切分、Intent 和依赖关系。
- 一个 WorkItem 内允许 0～3 个 `evidenceFacets`，表示需要分别查证的证据目标。
- Specialist 仍只调用一次 `rag_search`。
- `rag_search` 内部保留原始问题 q0 作为锚点，并针对各 facet 生成检索 query。
- 多 query 分别执行 BM25 / Dense，之后 weighted RRF、chunk 去重、候选保留、coverage-aware rerank。
- 无 facets 时自动退化为旧的单 Query 路径，保持兼容。

典型例子：

> 我已经拿了国家奖学金，这学期准备因病休学，还能领国家助学金吗？复学后怎么办？

仍是 1 个 CAMPUS WorkItem，但内部规划：

1. 国家奖学金和国家助学金是否可以兼得；
2. 因病休学期间国家助学金如何处理；
3. 恢复学籍后国家助学金如何处理。

## 2. 主要修改文件

| 文件 | 修改内容 |
|---|---|
| `app/services/intent_fusion.py` | `IntentSegmentDecision` 增加 `evidenceFacets`，最多3项，去重和长度校验 |
| `app/services/intent_prompts.py` | Understanding Prompt 明确区分 WorkItem 与 evidence facet |
| `app/agents/routing.py` | `WorkItem` 增加 `evidence_facets`，payload 往返保持兼容 |
| `app/services/context_builder.py` | 把 facets 传给 Specialist |
| `app/services/clarifications.py` | 澄清恢复 WorkItem 时保留 facets |
| `app/agents/autonomous.py` | Specialist 一次 `rag_search` 携带完整 query + facets |
| `app/chat_tools/server.py` | `rag_search` 增加 `facets` 参数和 schema |
| `app/services/rag_pipeline.py` | 原问题锚点、facet rewrite、多查询召回、weighted RRF、去重、候选保留、coverage-aware rerank |
| `app/services/ai.py` | Mock structured completion 支持 facet rewrite schema |
| `tests/test_rag_pipeline_v2.py` | 增加 multi-facet、去重、rewrite fallback、rerank payload 测试 |
| `tests/test_route_plan_v3.py` | 增加 facets 从 Understanding 到 WorkItem 再到 payload 的测试 |

完整逐行差异见 `code_diff.txt`。

## 3. 自动化测试结果

执行：

```bash
DATABASE_URL=sqlite:////mnt/data/mindbridge_test.db python -m pytest -q \
  tests/test_route_plan_v3.py \
  tests/test_route_planning.py \
  tests/test_intent_fusion.py \
  tests/test_understanding_single_path.py \
  tests/test_rag_pipeline_v2.py \
  tests/test_coordinator_work_items_v3.py \
  tests/test_clarification_study_plan_flow.py \
  tests/test_context_builder.py
```

结果：

```text
55 passed in 15.96s
```

另外：

```bash
python -m compileall -q app
```

结果：`COMPILE_OK`。

当前执行环境缺少 `mcp` 和 `pymysql`，因此没有声称完整全仓测试全部通过；本轮与 Evidence Facets / Routing / RAG / Clarification / Context 直接相关的回归子集通过。数据库测试通过临时 SQLite URL 运行。

## 4. Benchmark 设计

沿用已经冻结的 200 条“真实学生问法”数据，不为了本次改造重新改题。

- 200 条总数据；
- 固定 50 条作为 reranker/dev；
- 剩余 150 条作为 held-out 主测试；
- held-out 中 108 条单证据，42 条多证据；
- Gold 用于评测，不输入 facet planner/retriever；
- 本轮 facet planner 是只看 query 的 deterministic proxy，用于模拟新的 UnderstandingAgent evidence-facet 输出；
- Dense 由于当前离线环境无法拉取 BGE-M3 权重，仍使用 char 2～4gram TF-IDF → TruncatedSVD(128) → cosine 的 Dense-LSA proxy；
- Reranker 是 50 条 dev 上训练的 LightGBM LambdaRank proxy，不等价于生产 LLM/Cross-Encoder reranker。

因此下面是**真实运行的离线消融数据**，但 Dense 数字不能表述为 BGE-M3 指标。

## 5. Held-out 150 主结果

| 方法 | Recall@5 | HitRate@5 | MRR@5 |
|---|---:|---:|---:|
| BM25，不改写 | 78.00% | 88.00% | 83.38% |
| Dense-LSA，不改写 | 73.00% | 86.67% | 81.80% |
| BM25 + Dense + RRF，不改写 | 77.22% | 89.33% | 88.02% |
| **BM25 + Dense + RRF + Rerank，不改写** | **81.67%** | **93.33%** | 89.19% |
| 单 Query Rewrite + BM25 | 76.78% | 86.67% | 82.97% |
| 单 Query Rewrite + Dense | 75.33% | 88.67% | 84.52% |
| 单 Query Rewrite + Hybrid RRF | 75.56% | 88.67% | 86.39% |
| 单 Query Rewrite + Hybrid + Rerank | 80.89% | **93.33%** | 87.91% |
| **Facet Multi-Query + BM25，不改写** | **81.44%** | 91.33% | 88.29% |
| Facet Multi-Query + Hybrid RRF，不改写 | 79.67% | 91.33% | 88.58% |
| Facet Rewrite + BM25 | 81.33% | 91.33% | 88.24% |
| Facet Rewrite + Dense | 75.78% | 88.67% | 86.13% |
| Facet Rewrite + Hybrid RRF | 79.67% | 91.33% | 87.06% |
| **Facet Rewrite + Hybrid + Coverage Rerank** | 80.33% | **93.33%** | **90.30%** |

### 最重要的比较

相对原始 BM25：

- `BM25_no_rewrite` Recall@5：78.00%
- `FacetMultiQuery_BM25_no_rewrite` Recall@5：81.44%
- **+3.44 个百分点**
- HitRate@5：88.00% → 91.33%，**+3.33pp**
- MRR@5：83.38% → 88.29%，**+4.91pp**

说明“上游 evidence facets → 多查询召回”本身是有效信号，尤其适合制度文档这种关键词非常强的语料。

## 6. 多证据问题结果（42 条）

这部分比总体 Recall 更能验证此次架构目标。

| 方法 | Multi-Evidence Recall@5 | HitRate@5 | MRR@5 |
|---|---:|---:|---:|
| BM25，不改写 | 52.38% | 88.10% | 85.32% |
| Dense-LSA，不改写 | 46.43% | 95.24% | 90.36% |
| Hybrid RRF，不改写 | 51.98% | 95.24% | 95.24% |
| Hybrid RRF + Rerank，不改写 | 53.57% | 95.24% | 93.65% |
| 单 Rewrite + BM25 | 52.78% | 88.10% | 85.12% |
| **Facet Multi-Query + BM25，不改写** | **57.54%** | 92.86% | 90.08% |
| Facet Multi-Query + Hybrid RRF | 53.57% | 95.24% | 92.86% |
| Facet Rewrite + BM25 | 57.14% | 92.86% | 91.07% |
| Facet Rewrite + Hybrid + Coverage Rerank | 51.19% | **97.62%** | **94.52%** |

核心发现：

- 多证据 Recall@5 从 BM25 的 **52.38% → 57.54%**，提升 **5.16pp**；
- 证明把复杂工作项提前规划为互补 facets，确实能提高“第二/第三条证据”的覆盖；
- 但当前 Hybrid + Coverage Rerank 的 HitRate/MRR 很高，Recall 反而没有最好，说明全局排序仍可能把同一证据面的相似 chunk 挤进 Top5，造成其它 facet 被挤掉。

## 7. 为什么最终 Full Pipeline 不是 Recall 第一

这是本次最值得保留的工程结论，而不是应该掩盖的数据。

当前测试中：

- **最高总体 Recall@5**：Hybrid + RRF + Rerank，不改写 = 81.67%；
- **几乎同等 Recall、但更简单**：Facet Multi-Query + BM25，不改写 = 81.44%；
- **最高 MRR@5**：Facet Rewrite + Hybrid + Coverage Rerank = 90.30%；
- **最高多证据 Recall@5**：Facet Multi-Query + BM25，不改写 = 57.54%。

说明不同目标存在 trade-off：

- Rerank 很擅长把“第一条正确证据”推到前面，因此 HitRate/MRR 上升；
- 但 Top5 是一个有限槽位，普通相关性排序不天然保证 3 个 facet 都至少占一个槽位；
- Weak Dense proxy 也会给制度中语义相近但业务含义不同的 chunk 加噪声；
- 单 Query Rewrite 在本制度语料上仍可能破坏 BM25 原始关键词信号。

## 8. 当前建议的生产策略

代码层面保留本次 evidence-facet 架构是值得的，但下一步不要简单规定“所有请求都必须 Rewrite + Hybrid”。

建议：

1. 简单单证据 WorkItem：保持原 query + Hybrid/Rerank 或高质量 BM25；
2. 多 facet WorkItem：q0 + facets 并行检索，优先保证每个 facet 的候选进入候选池；
3. 最终 Top5 增加真正的 **facet-aware constrained selection**，而不只是给 reranker prompt 写“注意覆盖”；
4. 使用真实 BGE-M3 后重新冻结复测 Dense/Hybrid；
5. 使用真实 Cross-Encoder 或独立 LLM reranker 后再决定生产默认策略。

本次数据甚至提示一个可探索的 adaptive policy：检测到 3 个 facet 时偏向 multi-query lexical retrieval，否则使用常规 hybrid + rerank。该结论目前只作为实验方向，不应直接硬编码，因为 facet planner 和 Dense/Reranker 仍是离线 proxy。

## 9. 面试中可以怎么讲

推荐表述：

> 我们原来是一条 WorkItem 对应一次单 Query Hybrid Retrieval。离线 Harness 发现复杂校园制度问题虽然 HitRate 较高，但多证据 Recall 明显不足，因此没有在 RAG 层再次拆业务任务，而是把证据规划前移到 UnderstandingAgent：一个业务 WorkItem 内生成最多三个 evidence facets。Specialist 仍只调用一次 RAG，RAG 内以原问题作为 anchor，对 facet 生成检索 query，并执行多路召回、weighted RRF、去重和 coverage-aware rerank。冻结的 150 条 held-out 中，纯 BM25 Recall@5 为 78.0%，Facet Multi-Query + BM25 达到 81.44%；在 42 条多证据问题上 Recall@5 从 52.38% 提升到 57.54%。同时我们也发现普通全局 rerank 更擅长提高首证据排名而非证据组覆盖，因此下一步采用 facet-aware constrained selection，而不是盲目堆更多检索链路。

注意：当前 Dense 是离线 LSA proxy，不能把其结果宣传为 BGE-M3 实测。

## 10. 输出文件

- `metrics.csv`：所有方法的整体指标
- `evidence_slice_metrics.csv`：单证据 / 多证据切片指标
- `per_query.csv`：逐问题逐方法结果
- `heldout_detailed.jsonl`：逐问题 Top5 详细 evidence
- `facet_plans.jsonl`：每条 Query 的 facet 规划结果
- `bootstrap_ci.csv`：95% Bootstrap CI
- `benchmark_meta.json`：实验配置与模型限制
- `code_diff.txt`：本次源码差异
