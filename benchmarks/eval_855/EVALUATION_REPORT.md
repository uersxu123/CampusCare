# MindBridge 855 子块本地 RAG 测评报告

运行日期：2026-09-13

## 评测对象

- 语料：49 个文档、272 个父块、855 个子块。
- 冻结题集：200 条学生视角问题，全部带文档级 Gold；其中 easy 34、medium 84、hard 82。
- 检索参数：每路候选 40、RRF k=60、融合候选 24、最终 Top 5。
- Dense 模型：Ollama `bge-m3:latest`，本机模型摘要 `790764642607`。
- 改写及 Qwen 精排：Ollama `qwen3:8b`，本机模型摘要 `500a1f067a9f`，`think=false`。
- 当前项目没有独立 Cross-Encoder，因此没有 Cross-Encoder 指标。

## Retrieval 全量结果

| Method | Recall@5 | HitRate@5 | MRR@5 | n |
|---|---:|---:|---:|---:|
| BM25 Only | 0.8950 | 0.9700 | 0.8486 | 200 |
| BGE-M3 Dense Only | 0.8975 | 0.9750 | 0.8976 | 200 |
| BM25 + BGE-M3 + RRF | **0.9242** | **0.9900** | **0.9320** | 200 |
| Rewrite + Hybrid RRF | 0.9258 | 0.9850 | 0.9289 | 200 |
| Rewrite + Hybrid + bundle Qwen rerank | 0.5458 | 0.6200 | 0.3928 | 200 |
| Cross-Encoder | 未实现 | 未实现 | 未实现 | 0 |
| Full production pipeline | 未由该独立脚本覆盖 | 未由该独立脚本覆盖 | 未由该独立脚本覆盖 | 0 |

Hybrid RRF 相比 Dense Only：Recall@5 提升 2.67 个百分点，HitRate@5 提升 1.50 个百分点，MRR@5 提升 3.44 个百分点。

Hybrid RRF 相比 BM25 Only：Recall@5 提升 2.92 个百分点，HitRate@5 提升 2.00 个百分点，MRR@5 提升 8.34 个百分点。

查询改写相比 Hybrid RRF：Recall@5 提升 0.17 个百分点，但 HitRate@5 降低 0.50 个百分点，MRR@5 降低 0.31 个百分点。当前结果不支持“改写整体显著提升”的结论。

测评包中的简化 Qwen rerank 将 HitRate@5 从 0.9850 降到 0.6200，共 76/200 题完全未命中 Gold。该实现不应作为生产精排方案或 Cross-Encoder 指标。

## Faithfulness 探索性结果

采用冻结 200 题中每隔 10 条抽取一条的固定 20 题样本。Generator 和 Judge 都是 `qwen3:8b`，属于 self-judge。

| Retrieval | Faithfulness | 有效 Faithfulness | Answer Relevancy | 有效 Relevancy |
|---|---:|---:|---:|---:|
| BGE-M3 Dense Only | 0.9396 | 15/20 | 0.8107 | 20/20 |
| BM25 + BGE-M3 + RRF | 0.8657 | 15/20 | 0.8039 | 20/20 |

按各自有效样本均值直接比较，Hybrid 相比 Dense 的 Faithfulness 为 -7.38 个百分点；只比较两边都成功评分的 12 条配对样本，差值为 -6.10 个百分点。Answer Relevancy 的 20 条配对差值为 -0.68 个百分点。由于两组 Faithfulness 各有 5 个 Judge 超时，且只有 20 条 self-judge 样本，这些差值不适合作为正式简历指标。

## 一致性验收

- MySQL 活跃数据：49 文档、272 Parent、855 Child。
- Chroma 活跃 collection：`mindbridge_knowledge_v3__e68e6e2ed134`，855 条向量。
- Chroma metadata 中的 `db_id` 与 MySQL 活跃 Child ID 完全一致，内容哈希也完全一致；缺失 0，额外 0。
- 旧 collection 没有被当前 ACTIVE 配置引用。
- 合并后子块平均 188.71 token，中位数 177，最大 480。
- 有 21 个子块超过 400 token；它们是原交付语料中保留的独立长块，合并逻辑没有截断。因而“合并预算不超过 400”成立，但“所有 Child 最大不超过 400”不成立。

## 主要失败样例

Hybrid RRF 完全未命中 2/200：

1. `real-cug-039`：“老师点名的时候让室友帮我答到，这算违纪吗？”Gold 为 doc-09/doc-10，Top 5 全部落到 doc-08。
2. `real-cug-094`：“如果申请助学金时被发现材料造假，会怎么处理？”Gold 为 doc-33，Top 5 落到 doc-34/doc-48/doc-42/doc-41。

Hybrid RRF 有 170/200 题完整覆盖全部 Gold，28/200 部分覆盖，2/200 完全未命中。

## 口径限制

本轮 Retrieval 使用测评包的独立 BM25 tokenizer 和重新计算的真实 BGE-M3 embedding。BM25 实现不是项目生产 BM25F 的逐字节复刻；Chroma/MySQL 做了独立一致性验收，但该脚本的 Dense 排名不是直接读取 Chroma 排名。

测评包的 Qwen rerank 是简化的编号排序提示，不等同于当前项目 `_rerank()` 中的结构化要点评估与 Soft Coverage。当前项目也没有独立 Cross-Encoder，因此不能把这一行写成 Cross-Encoder 或完整生产链路成绩。

当前全量数据不支持旧简历中的“Recall@5 提升 9%、Faithfulness 提升 7%”。可可靠引用的结果是：在本轮 200 题文档级 Gold 测评中，Hybrid RRF 相比 Dense Only 的 Recall@5 提升 2.67 个百分点，HitRate@5 达到 0.9900，MRR@5 达到 0.9320。
