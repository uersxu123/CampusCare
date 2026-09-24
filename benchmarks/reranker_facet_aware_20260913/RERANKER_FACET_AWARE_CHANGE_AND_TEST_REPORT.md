# Reranker Facet-Aware 修改与测试报告

## 本次只做两项修改

1. **Reranker Prompt 升级**：输入 `originalQuery + evidenceFacets + candidates`，要求先判断单条候选与原问题的直接相关性，再在相关性接近时考虑不同 facet 的互补性。没有加入 Facet-aware constrained Top-K selector。
2. **Candidate → Facet 匹配判断**：Reranker 结构化输出新增 `assessments`，每个 candidate 必须返回 `relevanceLevel / matchedFacetIds / directSupport`。`matchedFacetIds` 只能引用本次输入的 `f1~f3`，服务端严格校验。

生产 Schema 已从 `rag_rerank_v3` 升级到 `rag_rerank_v4`。RAG response diagnostics 增加 `rerankAssessmentCount` 与 `rerankCandidateFacetMatches`，便于 trace 和后续评测。

## 代码测试

- `49 passed, 15 subtests passed in 26.00s`
- `python -m compileall -q app tests` → `COMPILE_OK`
- 覆盖 RAG、RoutePlan、Routing、IntentFusion、IntentContext、ContextBuilder、Clarification、Safety Boundaries。

## Held-out 150 检索消融

测试集仍使用已经冻结的真实学生 200 条数据：50 条 dev 训练 reranker proxy，150 条 held-out；其中 held-out 108 条单证据、42 条多证据。

| 方法 | Recall@5 | HitRate@5 | MRR@5 |
|---|---:|---:|---:|
| BM25_no_rewrite | 78.00% | 88.00% | 83.38% |
| Dense_LSA_no_rewrite | 73.00% | 86.67% | 81.80% |
| Hybrid_RRF_no_rewrite | 77.22% | 89.33% | 88.02% |
| Hybrid_RRF_RelevanceRerank_no_rewrite | 77.89% | 90.67% | 85.82% |
| SingleRewrite_Hybrid_RRF | 75.56% | 88.67% | 86.39% |
| SingleRewrite_Hybrid_RRF_RelevanceRerank | 77.89% | 91.33% | 86.82% |
| FacetMultiQuery_BM25_no_rewrite | 81.44% | 91.33% | 88.29% |
| FacetMultiQuery_Hybrid_RRF_no_rewrite | 79.67% | 91.33% | 88.58% |
| FacetMultiQuery_Hybrid_RRF_RelevanceRerank | 79.11% | 91.33% | 88.13% |
| FacetMultiQuery_Hybrid_RRF_FacetAwareRerank | 80.11% | 92.00% | 87.67% |
| FacetRewrite_Hybrid_RRF | 79.67% | 91.33% | 87.06% |
| FacetRewrite_Hybrid_RRF_RelevanceRerank | 79.44% | 92.00% | 87.49% |
| FacetRewrite_Hybrid_RRF_FacetAwareRerank | 80.00% | 92.67% | 90.17% |

## 只看“步骤 1+2”的增益

### 不做 facet rewrite

`FacetMultiQuery + Hybrid + RelevanceRerank` → `FacetAwareRerank`：

- Recall@5：79.11% → 80.11%（+1.00pp）
- HitRate@5：91.33% → 92.00%（+0.67pp）
- MRR@5：88.13% → 87.67%（-0.47pp）

多证据 42 条：Recall@5 51.59% → 52.78%；MRR@5 91.27% → 92.46%。

### 做 facet rewrite

`FacetRewrite + Hybrid + RelevanceRerank` → `FacetAwareRerank`：

- Recall@5：79.44% → 80.00%（+0.56pp）
- HitRate@5：92.00% → 92.67%（+0.67pp）
- MRR@5：87.49% → 90.17%（+2.68pp）

多证据 42 条：Recall@5 50.40% → 50.00%；MRR@5 90.87% → 94.05%。

## 结论

这两项修改**值得保留**：在相同候选集上，Candidate→Facet 信号让整体 HitRate / MRR 更稳定，尤其 `FacetRewrite` 链路 MRR@5 从 87.49% 升到 90.17%。但仅做 1、2 并不能稳定提高多证据 Recall；这与预期一致，因为本轮**没有做强制 facet coverage 的 Top-K selector**。也就是说，Reranker 更会判断“这条证据支持哪个 facet”，但最终仍然只是一个全局排序，多个高相关重复证据仍可能占据 Top5。

## 评测边界

本环境没有运行真实 BGE-M3 / 线上 LLM Reranker。本轮 Dense 仍是 char 2~4 gram TF-IDF → SVD 128 的离线 dense proxy；Reranker 使用固定 50 条 dev 上训练的 LightGBM LambdaRank。Facet-aware 版本只额外加入 candidate→facet 的匹配特征（最大 facet 匹配、匹配 facet 数），**没有使用 constrained Top-K selector**。因此这些数字用于本地消融和方向判断，不应直接写成“BGE-M3/LLM Reranker 实测指标”。
