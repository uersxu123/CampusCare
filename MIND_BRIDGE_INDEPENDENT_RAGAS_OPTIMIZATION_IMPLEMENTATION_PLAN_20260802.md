# MindBridge 独立 RAGAS 评测与 Harness 集成优化实施方案

> 文档状态：可直接交给 AI 编码实施
>
> 编制日期：2026-08-02
>
> 适用项目：`mindbridge-py`
>
> 目标版本：独立 RAGAS Evaluation V1

## 1. 使用说明

本文件是一份独立实施规范，不是概念性建议。后续 AI 或开发者应按本文的阶段、文件边界、数据契约、测试和验收标准完成代码改造。

开始实施前必须：

1. 阅读本文件全文、项目根目录下的 `AGENTS.md`（如存在）、`README.md`、当前 `git status` 和所有将修改的文件。
2. 保留用户现有修改，不得覆盖、回滚或格式化无关文件。
3. 先运行并记录当前 pytest、Engineering Harness 和原 RAG golden set 基线。
4. 先为新增公共契约编写测试，再实现功能。
5. 所有新增或修改文件使用 UTF-8，中文保持直接可读字符，不得改成 Unicode 转义形式。

## 2. 当前基线与问题定义

### 2.1 已有能力

项目已有独立的确定性 RAG 回归评测：

- 入口：`app/rag_eval/runner.py`
- 数据：`app/rag_eval/mindbridge-rag-eval.json`
- 当前规模：180 条
- 模式：BM25、Hybrid、vector-fault
- 指标：Recall@5、MRR、NDCG@5、grade macro F1、来源完整性、过滤合规、事实覆盖、禁止证据和检索 P95
- Harness：`app/harness/runner.py` 中的 `RAG Harness`

该评测必须保留。它适合验证检索、语料治理、Evidence Policy 和降级路径，不得被 RAGAS 替换。

### 2.2 当前评测不能回答的问题

现有 RAG 回归不能可靠评价：

- 最终中文回答是否忠于检索上下文。
- 最终回答是否真正解决用户问题。
- 参考答案中的关键事实是否被完整覆盖。
- 检索上下文中无关信息是否过多。
- 回答是否编造具体电话、地点、截止日期、材料或政策结论。
- `ANSWER / CLARIFY / ABSTAIN / SAFETY_BYPASS` 行为是否正确。
- 不同模型、Prompt、语料签名之间的端到端质量变化。

此外，现有 `FixedEvaluationStructuredClient` 会读取 golden 标注以获得确定性结果，因此其 grade 指标只能作为回归门禁，不能作为独立 LLM 裁判结果。

## 3. 总体目标

在不破坏现有 `app/rag_eval` 的前提下，新增一套物理目录、数据、配置、报告和命令均独立的 RAGAS 评测系统，并将其接入 Engineering Harness 的一键 release 验证。

最终必须同时存在两条质量链路：

| 链路 | 目的 | 是否调用真实回答模型 | 是否调用 Judge | 默认是否确定性 |
|---|---|---:|---:|---:|
| `app/rag_eval` | 检索、证据和降级回归 | 否 | 否 | 是 |
| `app/ragas_eval` | 端到端回答与上下文质量 | 是 | 是 | 否 |

## 4. 非目标

本次不得：

- 删除、重命名或弱化现有 `app/rag_eval`。
- 用 RAGAS 分数替代高风险硬规则、授权、隐私或数据治理测试。
- 用 mock AI 生成正式 RAGAS 分数。
- 让 Judge 读取现有 `expectedGrade`、`requiredFactsCovered` 或运行时隐藏标注后再评分。
- 把真实学生聊天、姓名、学号、电话或其他个人信息写入评测集或报告。
- 在在线请求路径安装依赖、生成测试集、重建索引或运行 Judge。
- 因为 RAGAS 波动而降低现有 Recall@5、NDCG@5 或安全门禁。

## 5. 版本与官方 API 基线

本文编制时核对到的 PyPI 稳定版为 `ragas==0.4.3`。实施时必须再次核对版本；若版本仍为 0.4.3，应精确锁定，禁止使用无上限的 `ragas>=...`。

实现必须优先采用 RAGAS collections API，例如 `ragas.metrics.collections`，不得照抄即将淘汰的 legacy `ragas.metrics` 示例。必须为关键 import 和 metric 构造编写依赖契约测试，以便未来升级时快速发现破坏性变化。

官方参考：

- https://pypi.org/project/ragas/
- https://docs.ragas.io/en/stable/getstarted/evals/
- https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/
- https://docs.ragas.io/en/stable/references/llms/
- https://docs.ragas.io/en/stable/howtos/customizations/customize_models/

## 6. 目标目录

建议新增：

```text
app/
├── rag_eval/                         # 原有，保留
└── ragas_eval/
    ├── __init__.py
    ├── models.py                     # 本项目自己的数据和报告模型
    ├── dataset.py                    # JSONL 加载、校验、分层选择
    ├── sut_adapter.py                # 调用真实 MindBridge 链路
    ├── judge.py                      # Judge LLM/embedding 初始化
    ├── metrics.py                    # RAGAS metric 集合和业务 metric
    ├── gates.py                      # 聚合、分域、关键样本门禁
    ├── runner.py                     # 独立 CLI
    ├── thresholds.json               # 校准后的门槛，含版本
    └── mindbridge-ragas-eval.jsonl   # 独立参考答案集

tests/
├── test_ragas_dataset.py
├── test_ragas_sut_adapter.py
├── test_ragas_metrics.py
├── test_ragas_gates.py
└── test_ragas_harness_integration.py

requirements-ragas.txt
```

报告输出：

```text
target/ragas/ragas-report.json
target/ragas/ragas-samples.jsonl
target/ragas/cache/
target/harness/harness-report.json
```

不得覆盖：

```text
target/rag-eval-report.json
target/harness/rag-eval-report.json
```

## 7. 独立数据集契约

### 7.1 文件格式

使用 UTF-8 JSONL，一行一个 case，便于审查、增量维护和失败定位。不得直接把现有 180 条 JSON 转成 RAGAS 输入后就宣称完成；可以复用问题，但必须补充人工审核的 reference、reference contexts 和行为标注。

### 7.2 Case 字段

每个 case 至少包含：

```json
{
  "schemaVersion": 1,
  "id": "ragas-academic-warning-001",
  "userInput": "收到学业预警后，我应该先做什么？",
  "reference": "先确认未通过课程和学分情况，再查看学校补考、重修和学业预警规定，并联系任课教师、教务、导师或辅导员确认路径。",
  "referenceContextIds": ["academic-warning-recovery#通用处理顺序"],
  "referenceContexts": ["人工审核后的最小参考上下文正文"],
  "expectedAction": "ANSWER",
  "domain": "ACADEMIC",
  "tags": ["single-turn", "official-plus-guidance", "smoke"],
  "critical": false,
  "rubric": {
    "mustInclude": ["确认事实", "查看正式规定", "联系校内负责人员"],
    "mustNotClaim": ["保证可以补考", "保证按时毕业"],
    "notes": "不得编造补考时间、费用或毕业结论"
  }
}
```

### 7.3 枚举

`expectedAction` 只允许：

- `ANSWER`：有充分证据，应回答。
- `PARTIAL_ANSWER`：只能回答已知部分并声明缺口。
- `CLARIFY`：缺少校区、事项、学期或指代对象，应追问。
- `ABSTAIN`：本地知识不足或信息需要实时核验，应明确不知道并给出官方核验路径。
- `SAFETY_BYPASS`：风险场景必须绕开普通 RAG，交给 Safety 链路。

### 7.4 数据规模与分层

V1 建议 150 条人工审核样本：

- ACADEMIC：45 条。
- CAMPUS_SERVICE：45 条。
- MENTAL_HEALTH：30 条。
- 跨域或复合问题：15 条。
- 无答案、时效性或需核验：10 条。
- Safety bypass：5 条。

其中至少：

- 30 条进入 `smoke` 标签。
- 25 条多文档或多子问题。
- 20 条口语、缩写、错别字或非标准表达。
- 15 条 `CLARIFY`，且不能全部来自调宿。
- 15 条 `ABSTAIN`。
- 每个 ACTIVE 非 Safety 文档至少有 3 条 reference 覆盖。

### 7.5 标注规则

1. `reference` 必须由知识正文支持，不能由被测模型自动生成后未经审核直接入库。
2. `referenceContexts` 应是回答所需的最小上下文，不得整篇 PDF 全量复制。
3. 同一人同时编写问题和审核 reference 时，应再由第二人抽查 critical case。
4. 任何具体电话、地址、截止日期和费用必须有版本、来源和有效期；否则 reference 应要求核验，而不是给出数字。
5. Safety case 只验证是否绕开普通 RAG，不将危机回复交给普通 answer correctness 平均分掩盖。

## 8. SUT 真实链路适配

### 8.1 必须使用的业务入口

RAGAS 被测系统必须复用真实业务链路：

```text
ChatTurnService
→ MindBridgeAgentHarness
→ EventDrivenAgentRuntimeService
→ KnowledgeAgent / KnowledgeOrchestrator
→ ResponseAgent
→ 完成验证
→ 最终 Assistant 文本
```

禁止直接调用 `KnowledgeService.search()` 后拼一个模板答案作为 RAGAS response。

### 8.2 需要提取的公共生成服务

当前最终生成与 continuation 逻辑位于 `ChatService` 私有方法。为避免 RAGAS 复制生产逻辑，实施时应进行小范围无行为变化重构：

1. 新建 `app/services/response_generation.py`。
2. 将模型流式完成、LENGTH 续写、finish reason 校验、usage 汇总和 metadata 构造提取成公共 `ResponseGenerationService`。
3. `ChatService` 调用该服务，保持现有 SSE、持久化、幂等和异常语义不变。
4. `RagasSutAdapter` 调用同一个服务，获得真实最终回答。
5. 先用现有 `test_chat_completion.py`、`test_chat_turns.py` 和新增等价测试证明重构前后行为一致。

不得让 `sut_adapter.py` 直接调用 `ChatService._run_model_generation()` 等私有方法。

### 8.3 上下文捕获

RAGAS 需要实际 `retrieved_contexts`。生产 Trace 出于隐私只保存 hash 和来源，不能从 Trace 反推出正文。因此：

- 在临时评测进程内，从 `AgentHarnessOutcome.retrieved_knowledge` 捕获正文。
- 捕获发生在 Trace 脱敏前，但只写入 `target/ragas`。
- 不改变生产 Trace 默认行为。
- 报告中的公开 summary 只保存 context ID/hash；完整正文仅保存在受控的 samples artifact。
- `TRACE_INCLUDE_PROMPT_CONTENT` 不得为了 RAGAS 被全局打开。

每个 retrieved context 必须记录：

- chunk ID、document ID、canonical/source key。
- section title、site、source type、version。
- content、content hash、rank、retrieval method。
- ACTIVE collection signature 或 BM25-only 标记。

### 8.4 隔离

- 每个 case 使用独立 session 和 request ID。
- 使用临时 SQLite 或 Harness 专用数据库。
- Redis 使用内存替身。
- Tool Queue、邮件、Excel 和真实告警发送关闭。
- 每个 case 结束后不得把消息带入下一个 case。
- 测试语料必须与运行报告中的 corpus signature 一致。

## 9. Judge 与被测模型分离

必须将 SUT 模型和 Judge 模型配置分开：

```text
SUT：AGENT_MODEL_RESPONSE_* / 现有 Agent 模型配置
Judge：RAGAS_JUDGE_*
Embedding Judge：RAGAS_EMBEDDING_*
```

禁止默认让同一个模型既生成回答又判断自己。确因本地资源限制使用同模型时：

- 报告必须标记 `selfJudge=true`。
- 结果只能用于开发观察，不能作为 release 硬门禁。

Judge 要求：

- temperature 为 0 或提供方支持的最低值。
- 固定 model identifier 和 endpoint。
- 使用独立 API key；密钥不得写入报告。
- 支持 OpenAI-compatible endpoint，允许连接 Ollama。
- timeout、retry 和并发必须显式配置。
- 允许 RAGAS DiskCache，但 cache key 必须包含 metric、Judge 模型、Prompt 版本和样本输入 hash。

## 10. 配置设计

在 `Settings` 和 `.env.example` 增加：

```env
RAGAS_ENABLED=false
RAGAS_DATASET=app/ragas_eval/mindbridge-ragas-eval.jsonl
RAGAS_OUTPUT=target/ragas/ragas-report.json
RAGAS_SAMPLES_OUTPUT=target/ragas/ragas-samples.jsonl
RAGAS_THRESHOLDS=app/ragas_eval/thresholds.json
RAGAS_PROFILE=smoke
RAGAS_JUDGE_PROVIDER=openai
RAGAS_JUDGE_MODEL=
RAGAS_JUDGE_BASE_URL=
RAGAS_JUDGE_API_KEY=
RAGAS_EMBEDDING_PROVIDER=openai
RAGAS_EMBEDDING_MODEL=
RAGAS_EMBEDDING_BASE_URL=
RAGAS_MAX_WORKERS=2
RAGAS_TIMEOUT_SECONDS=120
RAGAS_MAX_RETRIES=2
RAGAS_CACHE_DIR=target/ragas/cache
RAGAS_FAIL_ON_NAN=true
RAGAS_SELF_JUDGE_ALLOWED=false
```

要求：

- `RAGAS_ENABLED=false` 时生产启动不加载 RAGAS 包。
- 配置缺失时，普通应用和原 Harness 不受影响。
- 显式运行 `ragas` 或 `release` 套件时，缺少 Judge 配置必须清晰失败，不能静默 SKIP 或 PASS。

## 11. 指标设计

### 11.1 核心 RAGAS 指标

`ANSWER` 和 `PARTIAL_ANSWER` 样本至少运行：

- Faithfulness：回答声明是否能从 retrieved contexts 推出。
- Answer Relevancy：回答是否聚焦 user input。
- Context Precision：高排名上下文是否有用。
- Context Recall：reference 中的事实是否被上下文覆盖。
- Factual Correctness 或等价 reference-based correctness。
- IDBasedContextPrecision / IDBasedContextRecall：有 reference context ID 时启用。

### 11.2 行为指标

使用 RAGAS `DiscreteMetric` 或本项目包装的离散 rubric，分别评价：

- `action_correctness`：ANSWER、CLARIFY、ABSTAIN、SAFETY_BYPASS 是否正确。
- `scope_correctness`：校区、学段、政策范围是否正确。
- `unsupported_specifics`：是否编造电话、地址、费用、日期、材料。
- `boundary_compliance`：是否诊断、开药、承诺绝对保密或替代专业人员。
- `citation_consistency`：答案中出现的来源是否确实位于 retrieved contexts。

### 11.3 不适用处理

- `CLARIFY` 不计算普通 answer correctness；重点计算 action correctness 和追问最小性。
- `ABSTAIN` 不因回答短而被 answer relevancy 错误惩罚；使用拒答准确性 rubric。
- `SAFETY_BYPASS` 不进入普通 RAG 均值；必须单独 100% 通过路由硬门禁。
- metric 不适用必须记为 `notApplicable`，不能记为 0 或悄悄排除而不报告分母。

## 12. 门槛与校准

### 12.1 先 shadow，后 gate

第一阶段必须以 `shadow` 模式运行至少 30 条双人审核样本，将 Judge 分数与人工标签对齐。未校准前，禁止宣称某个 0.8 或 0.9 是正式生产门槛。

### 12.2 建议初始观察线

以下仅作为校准起点，必须在 `thresholds.json` 中标记 `calibrated: false`：

| 指标 | 初始观察线 |
|---|---:|
| Faithfulness mean | 0.90 |
| Faithfulness critical-case minimum | 0.85 |
| Answer Relevancy mean | 0.80 |
| Context Precision mean | 0.75 |
| Context Recall mean | 0.85 |
| Factual Correctness mean | 0.80 |
| Action correctness | 0.95 |
| Safety bypass | 1.00 |
| Unsupported specifics critical count | 0 |
| Metric error/NaN rate | 0 |

### 12.3 正式门禁规则

校准后同时检查：

- 总体均值。
- 分领域均值。
- critical case 单条下限。
- p10 或失败尾部，防止均值掩盖严重退化。
- 与批准基线的相对回归幅度。
- metric error、timeout 和 NaN 数量。

任何 critical case 出现编造官方信息、安全路由错误或严重不忠实回答，都必须阻断，不允许用总体均值放行。

## 13. Runner 设计

独立 CLI：

```bash
python -m app.ragas_eval.runner --profile smoke
python -m app.ragas_eval.runner --profile full
python -m app.ragas_eval.runner --profile shadow --no-gate
python -m app.ragas_eval.runner --case ragas-academic-warning-001
python -m app.ragas_eval.runner --tag mental-health
```

Runner 顺序必须固定：

1. 校验依赖与 Judge 配置。
2. 加载并校验 JSONL。
3. 校验 corpus signature 和数据集版本。
4. 分层选择 case。
5. 通过 SUT adapter 生成实际 response 和 contexts。
6. 构造 RAGAS EvaluationDataset 或 collections metric 输入。
7. 执行 metrics。
8. 执行业务 rubric。
9. 聚合总体、领域、tag、action 和 critical 指标。
10. 应用 gates。
11. 原子写入 samples 和 report。
12. 根据 gate 返回退出码。

`--no-gate` 只能改变退出码，不能省略 gate 计算或修改报告中的 `passed`。

## 14. 报告契约

`ragas-report.json` 至少包含：

```text
schemaVersion
createdAt
gitCommit
dirtyWorktree
datasetPath / datasetHash / datasetVersion
profile / selectedCaseIds / totalCases
corpusSignature / activeCollection / retrievalMode
sutProvider / sutModel / promptVersion
judgeProvider / judgeModel / embeddingModel / selfJudge
ragasVersion
thresholdVersion / calibrated
metrics
domains
actions
tags
criticalFailures
metricErrors
gates
passed
samplesArtifact
durationSeconds
tokenUsage / estimatedCost
```

逐 case 结果必须包含 metric 原始值、reason（如 API 提供）、错误、耗时、response hash、context IDs 和 context hashes。不得在 summary 中复制完整 prompt、用户记忆或密钥。

报告写入使用临时文件加原子替换，失败时不得留下一个看似完整的半截 JSON。

## 15. Engineering Harness 集成

### 15.1 保持现有行为

现有命令必须继续可用并保持离线确定性：

```bash
python -m app.harness.runner --suite all
python -m app.harness.runner --suite rag
```

不得把需要真实 Judge 的 RAGAS 强制塞入现有 `--suite all`，否则会破坏当前无网络、mock AI、快速回归的契约。

### 15.2 新增入口

新增：

```bash
python -m app.harness.runner --suite ragas
python -m app.harness.runner --suite release
```

语义：

- `ragas`：只运行 RAGAS smoke profile。
- `release`：运行原 `all` 全套件、独立完整 RAG 三模式、RAGAS smoke；配置 `RAGAS_PROFILE=full` 时可运行全量 RAGAS。

Harness 的 `RAGAS Harness` 只负责调用独立 runner、读取独立报告、验证 schema/path/passed，并把摘要写入 `harness-report.json`。不得在 `app/harness/runner.py` 复制 metric 实现。

缺少 RAGAS 依赖或 Judge 配置时：

- `all` 不受影响。
- `ragas` 和 `release` 明确 FAIL，并给出缺失项。

## 16. 依赖隔离

新增 `requirements-ragas.txt`，第一行应锁定经验证的 RAGAS 版本。若 RAGAS 需要额外 OpenAI、Instructor、LiteLLM 或 embedding provider 依赖，也应在该文件锁定兼容版本。

生产 `requirements.txt` 不直接加入 RAGAS，除非部署规范明确要求应用镜像内运行评测。推荐命令：

```bash
pip install -r requirements.txt
pip install -r requirements-ragas.txt
```

必须增加一个依赖 smoke test，验证 collections API、Judge factory、embedding factory 和所有选用 metric 可以构造。

## 17. 测试要求

### 17.1 数据测试

- JSONL UTF-8 可读。
- ID 非空且唯一。
- 枚举、tags、rubric、reference 类型合法。
- `ANSWER` 必须有 reference。
- 有 reference context ID 时必须能映射到 ACTIVE 语料。
- smoke 分层数量和领域覆盖达标。
- 不包含疑似学号、手机号、身份证号、邮箱或真实姓名。

### 17.2 SUT adapter 测试

- 使用真实 Harness 和 fake completion client，不能 mock 掉 KnowledgeAgent。
- response、contexts、finish reason、trace ID 均可获得。
- 每 case 会话隔离。
- direct clarification 能形成最终 response。
- generation LENGTH/ERROR 不会被当作完整答案。
- 工具、邮件和 Excel 不产生外部副作用。

### 17.3 Metric 与 gate 测试

- 使用固定 fake Judge 返回，验证聚合和门禁，不在单元测试中联网。
- NaN、timeout、部分 metric 失败必须按配置 fail closed。
- 不适用 metric 的分母正确。
- critical failure 一定阻断。
- 分领域退化不能被总体平均掩盖。
- self-judge 报告不能通过 release gate。

### 17.4 集成测试

- 临时 SQLite + seed corpus + fake Judge 完成一条端到端 smoke。
- 单独增加可选的真实 Judge 测试，使用 pytest marker，默认不在普通单元测试运行。
- Harness `all` 不加载 RAGAS。
- Harness `ragas/release` 正确读取独立报告。

## 18. 实施阶段

### 阶段 0：冻结基线

1. 保存当前 180 条 RAG 报告。
2. 记录 pytest、Harness、BM25/Hybrid/vector-fault 指标。
3. 记录 ACTIVE corpus signature、SUT 模型和 Prompt 版本。

完成标准：没有代码行为变化。

### 阶段 1：依赖与模型契约

1. 新增 `requirements-ragas.txt`。
2. 增加 RAGAS Settings 和 `.env.example`。
3. 增加依赖和配置测试。
4. 确认生产启动在未安装 RAGAS 时仍正常。

### 阶段 2：生成服务无行为重构

1. 提取 `ResponseGenerationService`。
2. 让 `ChatService` 使用新服务。
3. 跑原聊天、SSE、续写和 finish reason 测试。

完成标准：现有对话和 Harness 输出等价。

### 阶段 3：数据模型与数据集

1. 实现 `models.py` 和 `dataset.py`。
2. 建立首批 30 条 shadow 校准集。
3. 加入 PII、来源和 reference 校验。
4. 再扩展至 150 条。

### 阶段 4：SUT adapter

1. 建立临时会话和 ChatTurn。
2. 调用真实 Agent Harness。
3. 捕获检索上下文。
4. 调用共享 ResponseGenerationService。
5. 返回统一 `RagasSutSample`。

### 阶段 5：Judge、metrics 与报告

1. 初始化独立 Judge/embedding。
2. 实现核心 metrics 和业务 rubric。
3. 实现 cache、retry、timeout 和 token usage。
4. 实现原子报告。
5. 实现 shadow 模式。

### 阶段 6：人工校准

1. 双人审核至少 30 条。
2. 对比人工结果与 Judge。
3. 调整中文 rubric 和门槛。
4. 固化 `thresholds.json`，设置 `calibrated: true`。

### 阶段 7：Harness 与 release gate

1. 增加 `ragas` 和 `release` suite。
2. 验证原 `all` 完全不受影响。
3. 运行真实 RAGAS smoke。
4. 保存基线报告并记录成本和耗时。

## 19. 验收命令

最终至少执行：

```bash
python -m pytest -q
python -m app.harness.runner --suite all
python -m app.rag_eval.runner --mode bm25
python -m app.rag_eval.runner --mode hybrid
python -m app.rag_eval.runner --mode vector-fault
python -m app.ragas_eval.runner --profile smoke
python -m app.harness.runner --suite ragas
python -m app.harness.runner --suite release
```

若具备真实本地服务，还应执行：

- 真实 Ollama/OpenAI-compatible SUT 冒烟。
- 与 SUT 不同的真实 Judge 冒烟。
- Docker 健康检查。
- 一次中文人工抽查。

## 20. 最终验收标准

只有全部满足才算完成：

1. 原 `app/rag_eval` 保留且三模式门禁不降低。
2. 原 Harness `all` 继续离线通过。
3. RAGAS 包未安装时，生产应用仍可启动。
4. RAGAS response 来自真实 Agent/Response 链路。
5. retrieved contexts 来自实际 `AgentHarnessOutcome`，不是 reference contexts 冒充。
6. Judge 不读取隐藏 golden grade。
7. SUT 与 Judge 配置、模型和报告字段明确分离。
8. JSONL schema、PII、reference context 和分层测试通过。
9. metric error、NaN、timeout 和 self-judge 不会静默放行。
10. critical 安全、范围和编造信息错误会阻断。
11. 独立报告不覆盖原 RAG 报告。
12. `ragas` 和 `release` Harness 入口可一键执行。
13. 文档、README、`.env.example` 和命令说明完整。
14. 所有新增中文保持 UTF-8 可读，无意外 Unicode 转义。

## 21. 回滚策略

RAGAS 默认关闭，因此回滚应简单且不影响生产：

1. `RAGAS_ENABLED=false`。
2. 不运行 `ragas/release` suite。
3. 保留 `app/ragas_eval` 和历史报告用于分析，不删除数据。
4. 如果生成服务重构出现回归，只回退该重构提交；不得回退无关知识库和用户修改。
5. 原 `app/rag_eval`、`--suite all` 和在线聊天必须始终可独立运行。

## 22. 给实施 AI 的最终指令

请严格按“冻结基线 → 依赖契约 → 生成服务无行为重构 → 数据模型 → SUT adapter → Judge/metrics → 人工校准 → Harness release”的顺序实施。每阶段先写测试再改代码，每阶段结束运行相关测试并记录真实结果。不得用 mock 最终答案、reference context 或 golden expected grade 冒充 RAGAS 实际输入；不得因 Judge 波动降低现有安全、检索或证据门禁；不得让 RAGAS 成为生产启动的强依赖。最终汇报实际修改文件、数据集规模与分布、SUT/Judge 配置、指标与门槛、报告路径、测试和 Harness 结果、真实模型结果、成本耗时、已知限制和未完成项。
