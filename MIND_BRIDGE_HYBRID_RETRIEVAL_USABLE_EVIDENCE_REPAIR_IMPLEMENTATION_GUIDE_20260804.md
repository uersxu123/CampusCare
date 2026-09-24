# MindBridge 混合检索与 Usable Evidence 修复实施指南

> 文档版本：2026-08-04
> 文档性质：面向代码改造 AI 的强约束实施指南
> 唯一上位方案：`MIND_BRIDGE_LAYERED_END_TO_END_EVALUATION_IMPLEMENTATION_GUIDE_20260803.md`

## 1. AI 执行约束

负责代码改造的 AI 必须遵守：

1. 本文档只继承唯一上位方案，不得使用仓库中的其他计划、指南或设计文档推翻、扩展或替换本文要求。
2. 每个阶段开始前，先读取该阶段列出的源文件和现有测试。
3. 不修改 Golden reference、reference contexts、expected action 或 expected grade 来规避失败。
4. 不把候选上下文直接当作 usable evidence，不绕过 Evidence Grader。
5. 不降低安全、Judge、NaN、语料指纹或 RAGAS 门禁。
6. 不在日志、报告、测试快照或提交内容中写入 API Key。
7. 不自动更新正式 baseline。
8. 保留上位方案规定的生产 BM25F 故障降级；正式 RAGAS、Full、Release 不得使用降级结果计分，必须 fail closed。
9. 只修改本文列出的文件，不做无关重构。
10. 文件保持 UTF-8，中文使用可读字符，不得转换为 Unicode 转义。

## 2. 目标

本次改造必须同时实现：

1. 生产 Turn Execution 已发布的 usable evidence 完整进入 `EvaluationRuntimeOutcome`。
2. `SUFFICIENT` 和 `PARTIAL` artifact 中获准发布的 `local_evidence` 不再因字段名错误丢失。
3. `NONE`、`FAILED_CLOSED` 和高风险路径仍不得发布 usable evidence。
4. 慢但成功的混合检索结果不再被事后清空。
5. Evidence Grader 不得用未要求的 facet 名称制造额外缺口。
6. 正式评测能证明实际运行 `Chroma vector + BM25F + RRF`，向量降级时停止计分。
7. `ragas-housing-change-materials-01` 的 usable context 包含 `knowledge:6311`。

## 3. 已确认根因

### 3.1 Artifact 字段合同不一致

Knowledge artifact 的真实字段是：

```text
status
overall_grade
local_evidence
retrieval_diagnostics
```

`ResponseAgent` 正确读取 `overall_grade`，所以最终回答能使用 `knowledge:6311`。`EventDrivenAgentRuntimeService._to_result()` 却读取不存在的 `grade`，导致：

```text
artifact.local_evidence 非空
→ AgentRunResult.retrieved_knowledge 为空
→ TurnExecutionObserver.usable 为空
→ EvaluationRuntimeOutcome.usable_contexts 为空
→ RAGAS 上下文指标 fail closed
```

### 3.2 成功检索结果被事后丢弃

当前搜索阈值为 4 秒，实测 Ollama embedding 加 Chroma 查询约为 4.5 至 6.7 秒。当前实现同步等待查询完成后，再因耗时超过阈值执行 `rows=[]`。这不会缩短延迟，只会丢弃正确结果并触发不必要的第二轮检索。

### 3.3 Grader 制造未要求缺口

当前 case 只要求 `MATERIALS`。Grader 已基于 `knowledge:6311` 生成完整材料 claims，却把以下未要求 facet 写入 `missing_facts`：

```text
PROCESSING_TIME
DEADLINE
COST
CONTACT
```

Policy 只清理未要求的 `missing_facets`，未处理这种枚举形式的 `missing_facts`，所以错误重算为 `PARTIAL`。

### 3.4 正式评测允许静默降级

本次复现实际为 `hybrid-v3`，没有降级。但当前配置和 evaluation isolation 都允许 `knowledge_vector_required=False`，向量故障后可能继续以 BM25F 结果计分。

生产允许明确降级，正式评测必须 fail closed。两类合同不得混用。

## 4. 修复后数据流

```text
KnowledgeService.search
  ├─ Chroma vector
  ├─ BM25F
  └─ RRF + rerank
          ↓
KnowledgeOrchestrator
  ├─ hard filter
  ├─ Evidence Grader
  └─ KnowledgeEvidenceArtifact
          ↓
ResponseAgent promptEvidence
          ↓
EventDrivenAgentRuntimeService
  ├─ 校验 KnowledgeEvidenceArtifact
  ├─ 发布 local_evidence
  └─ 发布 retrieval diagnostics
          ↓
TurnExecutionOutcome
          ↓
EvaluationRuntimeOutcome
  ├─ retrieved contexts：真实候选
  ├─ usable contexts：真实获准证据
  └─ retrieval diagnostics：真实混合检索状态
          ↓
RAGAS / Business Judge / Failure Attribution
```

## 5. Phase 1：修复证据传递

### 5.1 文件范围

- `app/agents/event_driven_runtime.py`
- `app/agents/result.py`
- `app/agents/harness.py`
- `app/services/turn_execution.py`
- `app/evaluation/runtime/capture.py`
- `app/evaluation/runtime/adapter.py`
- `app/evaluation/contracts.py`
- 对应 `tests/` 和 `tests/evaluation/` 测试

### 5.2 Artifact 读取合同

在 `EventDrivenAgentRuntimeService._to_result()` 中：

1. 优先选择 Coordinator 接受的 `promptEvidence`；不存在时使用原始 `knowledge_evidence`。
2. 使用 `KnowledgeEvidenceArtifact.model_validate()` 校验非空 payload。
3. 使用枚举字段 `artifact.status`、`artifact.overall_grade` 和 `artifact.local_evidence`。
4. 只有满足以下条件才构造 `retrieved_knowledge`：

```text
risk != HIGH
status != FAILED_CLOSED
overall_grade in {SUFFICIENT, PARTIAL}
local_evidence 非空
```

5. `NONE`、`CLARIFY`、`FAILED_CLOSED` 和高风险路径返回空 usable evidence。
6. 非空但合同无效的 payload 必须 fail closed，不得回退到候选上下文。

建议抽取唯一转换入口：

```python
def _published_knowledge_from_evidence(
    payload: dict,
    risk: RiskLevel,
) -> list[SearchResult]:
    ...
```

不得继续在多个位置用字符串分别判断 `grade` 或 `overall_grade`。

### 5.3 诊断字段贯穿

为 `AgentRunResult` 增加：

```python
retrieval_diagnostics: dict[str, Any] = field(default_factory=dict)
```

诊断信息依次传递到：

```text
AgentHarnessOutcome
TurnExecutionOutcome
EvaluationRuntimeOutcome
```

只允许以下白名单字段：

```text
retrievalMode
retrievalModes
vectorDegraded
vectorErrorCode
activeCollection
indexSignature
bm25CandidateCount
vectorCandidateCount
candidateCount
stageDurationMs
```

禁止传递 prompt、查询正文、上下文正文、连接串、完整异常堆栈或密钥。

### 5.4 单一事实源

执行成功时，Evaluation Adapter 必须使用 `execution.usable_evidence` 生成 usable contexts。Observer 只用于捕获已观察状态，不得成为第二个独立事实源。

返回前必须保证：

```text
harness.retrieved_knowledge
TurnExecutionOutcome.usable_evidence
observer.usable
```

三者来源、顺序和去重规则一致。

### 5.5 Phase 1 测试

至少覆盖：

1. `SUFFICIENT + local_evidence` 发布 usable evidence。
2. `PARTIAL + local_evidence` 发布已支持 evidence。
3. `NONE` 不发布 usable evidence。
4. `FAILED_CLOSED` 不发布 usable evidence。
5. `HIGH` 风险不发布 knowledge evidence。
6. 只有 `overall_grade` 的合法 payload 正常工作。
7. 只有旧错误字段 `grade` 的 payload 校验失败并 fail closed。
8. Adapter usable IDs 与 `TurnExecutionOutcome.usable_evidence` 完全一致。

## 6. Phase 2：修复 Grader 越界缺口

### 6.1 文件范围

- `app/services/knowledge_agent/policy.py`
- `app/services/knowledge_agent/prompts.py`
- `app/services/knowledge_agent/models.py`，仅在合同必要时修改
- `tests/test_knowledge_policy.py`
- `tests/test_knowledge_evidence_grader.py`
- `tests/test_knowledge_orchestrator.py`

### 6.2 Policy 规范化

在 `KnowledgePolicy.validate_and_recompute_grade()` 中：

1. 建立全部 `KnowledgeFacet.value` 的受控集合。
2. 只移除同时满足以下条件的 `missing_facts`：

```text
值与 KnowledgeFacet.value 完全一致
且该 facet 不在 question.required_facets 中
```

3. 记录诊断码：

```text
GRADER_UNREQUESTED_MISSING_FACT_FACETS_IGNORED
```

4. 普通自然语言 missing facts 不得自动删除。
5. 非法 chunk、claim 为空、存在冲突或 required facet 未覆盖时，保持 fail closed。
6. 规范化后所有 required facets 均有合法 claim、没有有效 missing facts、没有冲突时，重算为 `SUFFICIENT`。
7. 重算为 `SUFFICIENT` 时同步设置：

```text
next_action = STOP
retry_strategy = null
missing_facets = []
```

### 6.3 Prompt 最小补强

本文 Phase 2 明确授权只对 `EVIDENCE_GRADER_PROMPT_V2` 增加以下约束：

```text
missing_facts 只能描述当前 required_facets 范围内仍缺少的具体事实；
不得把未要求的 KnowledgeFacet 名称、额外服务问题或通用完整性要求写入 missing_facts。
```

不得重写整个 Prompt，不得把 coverage hint 当作事实证明，不得放宽 claim 与 chunk 引用要求。

### 6.4 Phase 2 测试

至少覆盖：

1. `MATERIALS` case 中未要求的四个 facet 名称被移除。
2. 材料 claims 完整后重算为 `SUFFICIENT`。
3. `missing_facts=["缺少申请表原件要求"]` 保留并得到 `PARTIAL`。
4. required facet 本身为 `PROCESSING_TIME` 时不得删除对应缺口。
5. 非法 chunk ID 仍抛出 `GRADER_CHUNK_NOT_ALLOWED_FOR_QUESTION`。
6. 无 claim 时仍为 `NONE`。

## 7. Phase 3：修复慢查询结果丢弃

### 7.1 文件范围

- `app/services/knowledge_agent/orchestrator.py`
- `app/services/knowledge_agent/models.py`
- `app/core/config.py`，仅在完成基准后调整
- `tests/test_knowledge_orchestrator.py`
- `tests/test_knowledge_query.py`
- `tests/test_knowledge_retrieval_regressions.py`

### 7.2 超时合同

删除“同步查询成功返回后，因为耗时超过阈值而执行 `rows=[]`”的行为。

将语义拆分为：

```text
timed_out：底层实际抛出 TimeoutError
slow：查询成功，但耗时超过 slow threshold
duration_ms：实际搜索耗时
```

为 `QueryExecution` 增加向后兼容默认字段：

```python
duration_ms: int = 0
slow: bool = False
```

规则：

1. 成功返回的 rows 必须保留。
2. `slow=True` 只写诊断，不触发 `round_timed_out`，不清空结果。
3. 实际 `TimeoutError` 才设置 `timed_out=True`。
4. `RuntimeError` 必须区分向量必需模式和生产可降级模式，不得统一伪装为 timeout。
5. 总体 deadline 仍保留，但不得无条件删除已合法发布 evidence。

### 7.3 参数调整

先运行至少 30 次真实混合检索，记录 embedding、Chroma 和 hybrid total 的 p50、p95、p99。只有得到基准后才允许调整：

```text
knowledge_search_timeout_seconds
knowledge_agent_deadline_seconds
```

候选初值可以是 10 秒和 40 秒，但不得跳过基准直接写入。

正式评测前执行一次不计分的 embedding 与 ACTIVE collection 预热。预热失败必须停止评测，不能切换 BM25F 后继续计分。

### 7.4 Phase 3 测试

至少覆盖：

1. 搜索超过阈值但成功返回时保留 rows，`slow=True`、`timed_out=False`。
2. 底层抛出 `TimeoutError` 时 rows 为空、`timed_out=True`。
3. 慢查询不自动触发 query rewrite。
4. 真实 evidence 不充分时仍允许 rewrite。
5. deadline exceeded 不删除已经发布的合法 evidence。

## 8. Phase 4：正式评测混合索引门禁

### 8.1 文件范围

- `app/evaluation/config.py`
- `app/evaluation/runner.py`
- `app/evaluation/contracts.py`
- `app/evaluation/runtime/isolation.py`
- `app/evaluation/runtime/adapter.py`
- 当前 summary 生成模块
- `tests/evaluation/`
- `.env.example`，只增加非敏感示例

### 8.2 新增配置

增加：

```python
require_hybrid_retrieval: bool = Field(
    True,
    validation_alias="EVAL_REQUIRE_HYBRID_RETRIEVAL",
)
```

`.env.example` 只增加：

```dotenv
EVAL_REQUIRE_HYBRID_RETRIEVAL=true
```

### 8.3 Isolation 行为

删除正式评测对 `knowledge_vector_required=False` 的无条件覆盖。

`EvaluationRuntimeAdapter` 显式接收 `require_hybrid_retrieval`。正式评测使用：

```text
knowledge_hybrid_v3_enabled = true
knowledge_vector_enabled = true
knowledge_vector_required = true
```

单元测试和专门的 vector-fault Harness 可以显式传入 `False`，不得依赖静默默认值。

### 8.4 Case 级门禁

实际执行知识检索的正式 case 必须满足：

```text
retrievalMode == hybrid-v3
vectorDegraded == false
activeCollection == runtime corpus active_collection
indexSignature == runtime corpus index_signature
```

任一条件失败时：

1. outcome 标记基础设施错误。
2. Failure Attribution 使用现有 `INFRA_ERROR`。
3. RAGAS 不计算该 case 质量分数。
4. summary `passed=false`。
5. `metricErrors` 使用稳定细分码 `HYBRID_RETRIEVAL_REQUIRED`。
6. 不更新 baseline。

不得误归因为 `RETRIEVAL_MISS` 或 `EVIDENCE_GRADE_ERROR`。

### 8.5 生产与评测边界

不得删除 `KnowledgeService` 的生产 BM25F fallback，不得改变 Engineering Harness `vector-fault` 的目标。

```text
生产故障合同：向量不可用时明确降级、记录原因、受控服务
正式评测合同：向量不可用时 fail closed、不计分、不更新基线
```

### 8.6 Phase 4 测试

至少覆盖：

1. `hybrid-v3 + vectorDegraded=false` 通过。
2. `degraded-bm25-only` 得到 `INFRA_ERROR`。
3. ACTIVE collection 不一致时 fail closed。
4. index signature 不一致时 fail closed。
5. 未执行知识检索的 CLARIFY、SAFETY_BYPASS 不误报。
6. 失败时不生成或覆盖 baseline。
7. Engineering Harness `vector-fault` 继续通过。

## 9. Phase 5：RAGAS 与失败归因回归

`app/evaluation/ragas_eval/dataset_adapter.py` 必须继续只使用：

```text
outcome.usable_contexts
outcome.usable_context_ids
```

禁止改用候选上下文、Golden reference contexts、MySQL 全量正文或 Chroma 全量导出。

保持上位方案的失败顺序：

```text
ROUTING_ERROR
KNOWLEDGE_DECISION_ERROR
RETRIEVAL_MISS
EVIDENCE_GRADE_ERROR
CONTEXT_TRANSFER_ERROR
GENERATION_GROUNDING_ERROR
ANSWER_QUALITY_ERROR
SAFETY_ERROR
JUDGE_ERROR
INFRA_ERROR
```

当前 case 修复后应满足：

```text
candidate_hit = true
usable_hit = true
context_transferred = true
```

混合索引门禁失败时直接归因 `INFRA_ERROR`，不得继续产生误导性的 evidence 或 generation 归因。

## 10. 强制实施顺序

1. 为字段合同缺陷补失败测试。
2. 完成 Phase 1，运行相关单测。
3. 为 grader 越界 missing facts 补失败测试。
4. 完成 Phase 2，运行 Knowledge Agent 测试。
5. 为慢查询结果丢弃补失败测试。
6. 完成 Phase 3，运行检索与 orchestrator 测试。
7. 增加诊断贯穿与正式评测门禁。
8. 完成 Phase 4，运行全部 evaluation 测试。
9. 运行单 case 真实 RAGAS。
10. 运行全量 pytest 和 Engineering Harness。
11. 执行编码、diff 和敏感信息扫描。
12. 只生成候选报告，不更新正式 baseline。

每个阶段完成后检查工作区是否出现用户并行修改。遇到冲突必须保留用户修改，不得回退或覆盖。

## 11. 测试矩阵

| 层级 | 场景 | 必须结果 |
|---|---|---|
| Artifact | `SUFFICIENT + evidence` | 发布 usable evidence |
| Artifact | `PARTIAL + evidence` | 发布已支持 evidence |
| Artifact | `NONE` | usable 为空 |
| Artifact | `FAILED_CLOSED` | usable 为空 |
| Safety | `HIGH` | 不发布 knowledge evidence |
| Grader | 未要求 facet 写入 missing facts | 精确清理并记录诊断 |
| Grader | 真实 required fact 缺失 | 保持 PARTIAL/NONE |
| Retrieval | 混合查询慢但成功 | 保留结果并标记 slow |
| Retrieval | 实际 TimeoutError | timed_out 且无结果 |
| Evaluation | `hybrid-v3` | 允许计分 |
| Evaluation | BM25F 降级 | INFRA_ERROR，不计分 |
| Evaluation | collection/signature 不匹配 | fail closed |
| RAGAS | usable contexts 非空 | 正常计算上下文指标 |
| Harness | vector fault | 生产降级合同继续通过 |

## 12. 当前 Case 验收

Case：`ragas-housing-change-materials-01`

必须满足：

```text
route.executionMode = KNOWLEDGE
knowledge_requested = true
retrievalMode = hybrid-v3
vectorDegraded = false
retrieved_context_ids 包含 knowledge:6311
usable_context_ids 包含 knowledge:6311
knowledge_used = true
artifact.overall_grade = SUFFICIENT
action = ANSWER
corpus.status = MATCH
```

以下均视为失败：

```text
usable contexts 为空
使用 candidate contexts 替代 usable contexts
degraded-bm25-only 仍继续计算 RAGAS
修改 Golden 规避失败
无条件删除 missing facts 让所有 case 变成 SUFFICIENT
```

## 13. 验证命令

主机运行真实 embedding 前：

```powershell
$env:KNOWLEDGE_EMBEDDING_BASE_URL='http://localhost:11434'
```

分阶段测试：

```powershell
python -m pytest -q tests/test_knowledge_policy.py
python -m pytest -q tests/test_knowledge_evidence_grader.py
python -m pytest -q tests/test_knowledge_orchestrator.py
python -m pytest -q tests/evaluation
```

真实单 case：

```powershell
python -m app.evaluation.runner `
  --suite ragas `
  --profile contract `
  --case ragas-housing-change-materials-01
```

全量回归：

```powershell
python -m pytest -q
python -m app.harness.runner --suite all
```

最终静态检查：

```powershell
rg -n '\\u[0-9a-fA-F]{4}' `
  app/agents `
  app/services/knowledge_agent `
  app/evaluation `
  tests `
  .env.example

git diff --check
```

敏感信息扫描只报告文件名或命中数量，不得打印疑似密钥内容。

## 14. 报告要求

最终报告至少包含：

```text
dataset SHA256
corpus fingerprint status
active collection
index signature
retrieval mode
vector degraded status
candidate context IDs
usable context IDs
artifact grade
metric errors
baseline status
```

报告写入新的 `target/evaluation/<run-id>/`，不得覆盖历史报告。

## 15. 禁止实现

1. 把 `observer.candidates` 复制到 `observer.usable`。
2. 在 RAGAS adapter 中使用 `reference_contexts` 作为 retrieved contexts。
3. 向量故障后静默切换 BM25F 并继续正式计分。
4. 将 `knowledge_vector_enabled=false` 来绕过向量超时。
5. 无条件清空全部 `missing_facts`。
6. 删除 Evidence Grader 或固定返回 `SUFFICIENT`。
7. 修改 Golden 来匹配错误运行结果。
8. 降低 RAGAS 或 metric error rate 门禁。
9. 在 `.env.example`、测试、报告或日志中写真实 Judge Key。
10. 更新正式 baseline 掩盖质量差异。

## 16. 完成定义

- [ ] Artifact 到 runtime 的字段合同统一为 `overall_grade`。
- [ ] `knowledge:6311` 同时存在于 candidate 和 usable IDs。
- [ ] Candidate 与 usable evidence 保持严格区分。
- [ ] 未要求 facet 不再制造虚假缺口。
- [ ] 真实缺口、冲突和非法引用仍 fail closed。
- [ ] 慢但成功的混合检索结果不再被清空。
- [ ] 正式评测可观测 retrieval mode 和 vector degraded 状态。
- [ ] 正式 RAGAS、Full、Release 遇到降级时不计分。
- [ ] 生产 vector-fault 降级回归仍通过。
- [ ] Corpus fingerprint 保持 `MATCH`。
- [ ] RAGAS 不再因 usable contexts 为空产生当前三项 ValueError。
- [ ] 全量 pytest 通过。
- [ ] Engineering Harness `--suite all` 通过。
- [ ] 未更新正式 baseline。
- [ ] 未泄露 API Key。
- [ ] Unicode 转义扫描与 `git diff --check` 通过。
