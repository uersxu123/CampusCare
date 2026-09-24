# MindBridge RAG V6：Soft Coverage Selection + Evidence Coverage

日期：2026-09-13

## 1. 本次只做什么

本次在上一版 `Facet-Aware Reranker` 基础上继续实现之前讨论的第 3、4 步：

1. 将最终 `ranked[:top_k]` 改为 **Adaptive Soft Coverage-Constrained TopK Selection**；
2. 输出明确的 **Evidence Coverage**（FULL / PARTIAL / NONE / UNKNOWN / NOT_APPLICABLE）。

没有新增第二层 Multi-Query，没有修改 UnderstandingAgent 的 WorkItem / evidenceFacets 规划逻辑，也没有强制每个 facet 塞一条低质量证据。

## 2. 生产代码行为

### 0 个 facet

完全退化为原来的 relevance-only TopK：

```text
Reranker -> TopK
coverageStatus = NOT_APPLICABLE
```

### 1 个 facet

如果正常 TopK 已经有 `HIGH/MEDIUM + directSupport=true + matchedFacetIds` 的证据，则不改排序；只有 TopK 完全缺少该 facet、但 reranker 候选池中存在可靠证据时，才最多预留 1 条。

因此单事实问题不会被强行做“多样性排序”。

### 2~3 个 facet

对可靠候选做轻量 greedy set-cover：优先选择能覆盖最多尚未覆盖 facet 的候选；同等覆盖下优先更高 relevance，再优先 reranker 原始排名靠前的候选。预留后，剩余位置仍按 reranker 全局相关性补齐，并保持 reranker 的相对顺序。

只有以下候选可用于覆盖：

```text
directSupport = true
AND relevanceLevel in {HIGH, MEDIUM}
AND facetId in matchedFacetIds
```

`LOW/NONE` 或 `directSupport=false` 的证据不会为了“凑满 coverage”被提升。

### Reranker 降级

如果 reranker 不可用，coverage 不会伪装成 UNCOVERED，而是：

```text
status = UNKNOWN
unknownFacets = [f1, f2, ...]
```

## 3. RAG response 新增输出

```json
{
  "evidenceCoverage": {
    "status": "FULL",
    "selectionMode": "multi-facet-soft-coverage",
    "selectionApplied": true,
    "coverageRatio": 1.0,
    "coveredFacets": ["f1", "f2", "f3"],
    "uncoveredFacets": [],
    "unknownFacets": [],
    "facets": [
      {
        "facetId": "f1",
        "text": "奖助兼得",
        "status": "COVERED",
        "evidenceIds": ["ev_chunk_123"]
      }
    ]
  }
}
```

Diagnostics 同步增加 `coverageStatus / coverageRatio / coveredFacetCount / uncoveredFacetCount / unknownFacetCount / coverageSelectionApplied`。

## 4. 代码测试

核心相关回归：

```text
55 passed, 15 subtests passed
```

其中新增覆盖测试包括：

- 多 facet：f3 原本排第 6 时能被软约束拉入 Top5；
- 单 facet：TopK 已覆盖时完全不改顺序；
- 单 facet：TopK 未覆盖时最多拉入 1 条可靠证据；
- `LOW` 或 indirect evidence 不会被强行提升；
- 单个 evidence 可同时覆盖两个 facet，仅占一个 TopK 位置；
- reranker 降级时 coverage=UNKNOWN，不伪报证据不足。

`python -m compileall -q app tests` 通过。

额外 Specialist/Agent 套件存在 6 个历史失败；使用未修改的 V5 包复跑后同样是这 6 个失败，因此不是本次 Soft Coverage 修改引入的回归。

## 5. 冻结 200 条数据集上的消融

数据集仍为上一轮冻结的 200 条“真实学生视角”问题：50 条 dev，150 条 held-out；held-out 含 108 条单证据问题、42 条多证据问题。没有为了本次算法修改测试题。

### Held-out 150 总体

| 方法 | Recall@5 | HitRate@5 | MRR@5 |
|---|---:|---:|---:|
| BM25 no rewrite | 78.00% | 88.00% | 83.38% |
| Facet Multi-Query + BM25 | 81.44% | 91.33% | 88.29% |
| Facet Multi-Query + Hybrid + Facet-Aware Rerank | 80.11% | 92.00% | 87.67% |
| **上项 + Soft Coverage** | **82.89%** | **94.00%** | **88.07%** |
| Facet Rewrite + Hybrid + Facet-Aware Rerank | 80.00% | 92.67% | 90.17% |
| **上项 + Soft Coverage** | **81.67%** | **94.00%** | **90.43%** |

对不 Rewrite 的 Facet Multi-Query 主链路，本次步骤 3+4 相比只做步骤 1+2：

```text
Recall@5   80.11% -> 82.89%   +2.78pp
HitRate@5  92.00% -> 94.00%   +2.00pp
MRR@5      87.67% -> 88.07%   +0.40pp
```

配对 Bootstrap（5000 次）中 Recall@5 delta 的 95% CI 约为 `[+0.89pp, +5.33pp]`；在当前 proxy benchmark 下为正向信号。

### 42 条多证据问题

| 方法 | Recall@5 | HitRate@5 | MRR@5 |
|---|---:|---:|---:|
| BM25 | 52.38% | 88.10% | 85.32% |
| Facet Multi-Query + BM25 | 57.54% | 92.86% | 90.08% |
| Facet Multi-Query + Hybrid + Facet-Aware Rerank | 52.78% | 95.24% | 92.46% |
| **上项 + Soft Coverage** | **57.94%** | **97.62%** | **92.94%** |
| Facet Rewrite + Hybrid + Facet-Aware Rerank | 50.00% | 95.24% | 94.05% |
| **上项 + Soft Coverage** | **53.57%** | **97.62%** | **94.52%** |

不 Rewrite 的 Facet 主链路中，多证据 Recall@5：

```text
52.78% -> 57.94%   +5.16pp
```

这正是本次修改想解决的目标：不是继续把“第一条正确证据”排得更高，而是减少某个 dominant facet 把其他证据目标挤出 Top5。

### 108 条单证据问题

Soft Coverage 没有伤害单问题；在当前 proxy 下反而略有提升：

```text
Facet Multi-Query + Facet-Aware Rerank
90.74% -> 92.59% Recall/HitRate
85.80% -> 86.17% MRR
```

原因是单 facet 模式只在正常 Top5 完全没有可靠支持时，最多补入 1 条候选；Top5 已覆盖时严格 no-op。

## 6. Coverage 侧指标

离线 proxy 的 held-out 结果：

- Facet Multi-Query + Soft Coverage：平均预测 facet coverage 93.0%；full coverage 92.67%；实际触发 selection 的 query 约 13.33%。
- Facet Rewrite + Soft Coverage：平均预测 facet coverage 92.0%；full coverage 91.33%；selection 触发率约 12.0%。

说明 Soft Coverage 不是每题都重排，大部分问题保持原 Reranker Top5，只在缺 facet 的少数 query 上介入。

## 7. 实际改善样本

Held-out 中，不 Rewrite Facet 主链路加入 Soft Coverage 后有 7 条 query 的 Gold Evidence Recall 提升，0 条下降。例如：

- `real-cug-024`：外校交换课程认定，从 Top5 全部 `doc-06` 变为补入 `doc-04`，Recall +0.5；
- `real-cug-127`：体育免修 vs 体测免测，从只命中 `doc-18` 变为补入 `doc-06`，Recall +0.5；
- `real-cug-148`：电动车电池拿回宿舍充电，从 Top5 全部 `doc-20` 变为补入 `doc-12`，Recall +0.333；
- `real-cug-200`：转入地大后的课程学分、档案、学籍处理，从 Top5 全部 `doc-21` 变为补入 `doc-04`，Recall +0.333。

## 8. 重要限制

本次 benchmark 仍然是架构消融，不是假装真实生产模型结果：

- Dense = 中文 char 2~4gram TF-IDF -> SVD128 -> cosine proxy，**不是 BGE-M3 实测**；
- Reranker = LightGBM LambdaRank proxy，**不是 Qwen/真实 LLM Reranker 实测**；
- benchmark 中 candidate->facet 判断使用字符 bigram overlap proxy，阈值只在 dev50 上选择为 0.12；生产代码使用 Reranker 的 `matchedFacetIds + directSupport + relevanceLevel`，没有这个 overlap 阈值。

因此这些数字适合证明“Soft Coverage Selection 这个架构方向是否值得保留”，不应写成真实 BGE-M3 / LLM 线上指标。

## 9. 当前建议

本次 3、4 建议保留。当前生产链路可描述为：

```text
UnderstandingAgent
  -> WorkItem + evidenceFacets
  -> Facet Multi-Query
  -> BM25 + Vector
  -> weighted RRF + dedup
  -> Facet-Aware Reranker
  -> Adaptive Soft Coverage Selection
  -> Evidence Coverage
  -> Parent / Reference Expansion
  -> Specialist answer
```

单问题自动退化为 relevance-first；只有 2~3 facet 的复杂 WorkItem 才真正启用多证据覆盖优化。
