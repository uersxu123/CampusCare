# CampusCare Skill 级联匹配与按需注入改造方案

日期：2026-09-18

状态：方案文档，尚未实施。本文作为后续代码、配置迁移与验收的依据。

本次修订：补充 13 个现有 Skill 的逐项修改要求、通用与专用场景边界、描述编写标准、正文与澄清职责的一致性，以及对应验收样本。业务代码和 Skill 文件尚未按本方案修改。

## 1. 已确认的目标与取舍

路由模块已将用户请求拆解为带意图和目标的 WorkItem，专业 Agent 只为当前工作项选择适用 Skill。采用以下策略：

1. 用 Agent name、intent、启用状态和风险约束进行范围过滤。
2. 用当前任务与 Skill 的规则配置计算可解释匹配分数。
3. 没有高分候选时，不加载场景 Skill，不调用 Embedding。
4. 只有一个高分候选，或同组第一名明显领先时，直接选择。
5. 仅对同一竞争组中高分且接近的候选进行 Embedding 消歧。
6. Embedding 仍不明确、不可用或超出预算时，不加载该竞争组的场景 Skill。
7. 选中的 Skill 在上下文预算内按需注入专业 Agent，未选中时 Agent 继续使用基础指令和工具执行任务。

本方案主动接受部分漏选，以降低误注入和额外向量调用成本。低规则分表示证据不足，不表示已证明 Skill 不适用。不得在实现中增加“低分或关键词零命中时使用语义补召回”的分支。

“按需加载”在本期指按需选择并注入模型上下文，以及按需调用向量服务。注册表仍可在初始化或 refresh 时读取本地文件，不承诺文件正文的磁盘懒加载。

## 2. 当前代码基线

以下路径均相对本项目根目录。

| 位置 | 当前行为 | 本次改造 |
| --- | --- | --- |
| `app/agents/coordinator.py::_ensure_specialist_tasks` | 按 WorkItem 的 intent、objective、依赖创建任务 | 保留分发职责 |
| `app/agents/autonomous.py::SpecialistAgent._run_loop` | 拼接 taskText、objective、evidenceFacets、knownArguments 调用 match | 改为传递结构化匹配输入 |
| `app/services/skills.py::SkillManager.match` | 硬过滤后按 priority + 5 × 关键词命中数排序 | 改为规则门控、组内选择、按需消歧 |
| `app/services/skills.py::MindBridgeSkill.prompt_context` | 按字符截断正文 | 新场景注入路径完整纳入或整项拒绝 |
| `app/agents/event_driven_runtime.py` | 初始化 SkillManager，无 Settings 参数 | 注入配置和语义消歧依赖 |
| `app/services/embedding.py` | 已有 Ollama/OpenAI 后端、连接复用和文档/查询接口 | 复用实现，增加 Skill 专用工厂入口 |
| `app/services/context_builder.py` | 专业 Agent 的 Skill 已并入 system，再统一检查输入预算 | 支持在超预算时完整移除可选场景 Skill 后重建 |
| `app/agents/result.py::SpecialistResultV2` | 严格结果模型，已有 selectedSkillIds | 增加可选、结构化的选择诊断 |
| `app/services/trace.py` | 对专业结果和工具信息做白名单摘要 | 增加 Skill 诊断白名单 |

当前有 13 个标准 Skill。`description` 目前不参与匹配。`optional=false` 当前不代表必选；`always_match` 只绕过关键词门槛。Response 的当前合成主链路不重新匹配 Skill。

正常路由路径存在直接设置 `WorkItem.confidence=1.0` 的代码，因此不能将它乘入 Skill 分数或视为实际正确率。本次不改路由置信度定义。

## 3. 各字段的职责

| 字段 | 职责 | 不承担的职责 |
| --- | --- | --- |
| 当前 Agent 的 skill_agent，例如 academic_planning | 对照 Skill.agents 做精确范围过滤 | 不参与向量相似度 |
| intent，例如 ACADEMIC | 对照 Skill.intents 做精确范围过滤 | 不重复作为规则加分项 |
| Skill.name | 唯一标识、加载、去重和诊断 | 不靠名字命中判断适用性 |
| Skill.description | 描述处理的目标和适用场景，作为向量候选文本 | 不作为运行时指令正文 |
| Skill.matching | 场景、目标、条件及排除规则 | 不授予工具权限 |
| Skill.body | 选中后注入的执行指导 | 不整篇向量化做本期匹配 |

工具权限仍由工具注册表、Agent profile 和当前 WorkItem 的工具门禁控制。选择 Skill 不能扩大权限。

## 4. Skill 类型与竞争关系

新增明确的 `selection_mode`，不得根据 optional 或 always_match 隐式猜测类型：

| 类型 | 选择方式 | 预算与拒选 |
| --- | --- | --- |
| baseline | 通过范围过滤后按声明注入，单独记录原因 | 与场景排名分离；必要基线不得被场景挤出 |
| scenario | 本文的规则门控和语义消歧 | 可拒选、可因预算不足整项移除 |
| fixed | 仅由现有命名读取或固定流程调用 | 不进入场景候选集，保持必需模板错误显式失败 |

必要安全行为仍由 Coordinator、固定安全指令和安全审查保障，不依赖可选 Skill 被选中。

场景 Skill 新增 `selection_group`。同一 Agent、同一 intent 下，同组候选视为替代方案，每组最多选一个；不同组只在业务上确实可互补时才能同时注入。

第一版将所有现有 scenario 配到 `primary_strategy`。这样每个 WorkItem 最多选择一个场景策略，MENTAL 可再带一项基线，避免把流程冲突误当互补。后续只有经过组合用例验证的流程才能拆为独立组；不得为每个 Skill 自动生成独立组以逃避消歧。

| 现有 Skill | 新类型 | 初始竞争组 |
| --- | --- | --- |
| academic_stress_planning | scenario | primary_strategy |
| academic_warning_recovery | scenario | primary_strategy |
| thesis_research_progress | scenario | primary_strategy |
| further_study_career_decision | scenario | primary_strategy |
| campus_procedure_navigation | scenario | primary_strategy |
| dormitory_life_guidance | scenario | primary_strategy |
| financial_aid_awards_guidance | scenario | primary_strategy |
| anxiety_grounding_support | scenario | primary_strategy |
| sleep_routine_support | scenario | primary_strategy |
| referral_resource_guidance | scenario | primary_strategy |
| supportive_response_baseline | baseline | 不参与竞争 |
| high_risk_safety_plan | fixed | 不参与竞争 |
| counselor_handoff_summary | fixed | 不参与竞争 |

`referral_resource_guidance` 的 always_match 必须移除；求助资源指导改为明确规则触发。baseline/fixed 不需要伪造 rule_score=1。

竞争组只规定互斥关系，不解决配置自身的重复覆盖。通用与专用 Skill 的职责需按第 13.2 节先收紧；不得使用原 priority 让通用 Skill 无条件输给专用 Skill，也不得让 Embedding 为本可通过规则区分的任务承担全部选择工作。

## 5. 结构化匹配输入

在 `skills.py` 定义不可变的 `SkillMatchInput`，由专业 Agent 从已验证 WorkItem 构造：

```python
@dataclass(frozen=True)
class SkillMatchInput:
    agent: str
    intent: str
    task_text: str
    objective: str
    known_arguments: dict[str, str]
    work_item_id: str = ""
    risk: RiskLevel = RiskLevel.LOW
```

字段使用约定：

1. task_text 优先用 taskText，缺失时使用 sourceText；只使用当前工作项，不拼接整轮多目标原文。
2. D、G 的第一版规则只从 task_text 取证，避免模型生成的 objective 自己补出高分证据。objective 用于语义查询的补充，不重复加分。
3. C 从声明过的 known_arguments 键，或 task_text 中明确的条件规则取证。现有 `deadline`、`course` 等键需按实际业务匹配；不假定项目已抽取 available_time 等不存在的字段。
4. evidenceFacets 和依赖结果不作为第一版评分输入，防止检索背景或其他任务证据触发 Skill。
5. 做 Unicode NFKC、大小写和空白规范化；保持原始任务不变。相同证据、同义词和重复字段不能累加。
6. 空任务文本不调用语义服务，不选择场景 Skill。

## 6. 规则元数据与计算

### 6.1 配置示例

在 YAML frontmatter 增加 `matching_version: 2`、selection 字段、matching 规则和少量 semantic_examples。以下为论文推进 Skill 的起始配置示例，实施时需结合评测补齐表达覆盖：

```yaml
name: thesis_research_progress
description: 帮助学生解决论文、开题或毕设推进中的具体卡点，将当前阶段目标拆成可检查的产出、短期任务和检查点，并整理需要导师反馈的问题。
agents: [academic_planning]
intents: [ACADEMIC]
enabled: true
optional: true
priority: 90
max_chars: 2600
matching_version: 2
selection_mode: scenario
selection_group: primary_strategy
matching:
  domain:
    strong:
      - id: thesis_topic
        any_of: [论文, 开题, 毕设, 文献综述]
    weak:
      - id: general_writing
        any_of: [写作, 导师]
  goal:
    strong:
      - id: explicit_plan
        any_of: [写作计划, 研究计划, 拆解任务, 安排进度]
      - id: arrange_work
        all_of:
          - [安排, 拆分, 制定, 规划]
          - [任务, 进度, 每天, 计划]
      - id: request_advisor_feedback
        all_of:
          - [导师]
          - [整理进展, 汇报卡点, 反馈请求]
    weak:
      - id: generic_help
        any_of: [怎么办, 帮帮我]
  context:
    - id: explicit_deadline
      argument_keys: [deadline]
    - id: task_time_constraint
      all_of:
        - [下周, 明天, 本周]
        - [提交, 截止, 交论文, 开题]
  exclude:
    - id: only_define_term
      all_of:
        - [只解释, 只想知道]
        - [术语, 名词含义]
semantic_examples:
  - 下周要开题，希望安排每天的准备任务。
  - 论文写到一半推进困难，希望拆成几个可以完成的步骤。
```

### 6.2 原子规则语义

- `any_of`：任一非空字面短语命中即可。
- `all_of`：每个内层词组至少命中一个词，且这些词组必须在同一任务分句中成立。初始按中文、英文句号、问号、感叹号、分号及换行切分，逗号保留以支持自然表达。
- `argument_keys`：声明的键中至少一个有非空字符串值；只用于 context。
- 每条规则只允许一种操作符；命中结果是布尔值。同一规则重复出现仍只算一次。
- D/G 分别取命中的最高档，不累计规则条数。context 任一成立即为 1。
- exclude 命中即排除该候选；不使用“出现不字就排除”的粗略规则。
- 首版不开放任意 YAML 正则表达式或运行时代码。通过规则组合覆盖明确用例，承认无法完整理解否定、引用和转折；这些情况加入困难负例。
- 英文短词需使用单词边界，避免字符子串误命中；中文按规范化后的字面短语处理。
- 正文、description、name 中出现某个词不能让规则成立，取证来源仅为本节规定的输入字段。
- 排除规则针对整个 Skill 的适用范围，不能因为用户拒绝某一种输出形式就排除其他适用能力。例如“不要时间计划，帮我整理论文进展向导师求助”仍可能适用论文推进 Skill；不能仅用“不需要计划”作为整项排除规则。

### 6.3 评分公式

```text
D = 1.0（strong 命中），否则 0.5（weak 命中），否则 0
G = 1.0（strong 命中），否则 0.5（weak 命中），否则 0
C = 1.0（至少一项相关条件成立），否则 0

rule_score = round(0.6 * D + 0.3 * G + 0.1 * C, 6)
```

实现内部建议用整数 `rule_points = 60*D + 30*G + 10*C` 判定阈值和分差，输出再除以 100，避免 0.05 边界浮点误差。D/G 档位映射为整数贡献，不引入浮点排序不稳定。

这些是专家设定的初始权重。字段命名为 rule_score，不命名为适用概率。priority 不进入分数；也不乘以上游 WorkItem.confidence。

| task_text/条件 | D | G | C | 分数 |
| --- | --- | --- | --- | --- |
| 最近在写论文 | 1 | 0 | 0 | 0.60 |
| 论文怎么办啊 | 1 | 0.5 | 0 | 0.75 |
| 帮我把论文拆分成几个任务 | 1 | 1 | 0 | 0.90 |
| 同上，并存在 deadline | 1 | 1 | 1 | 1.00 |
| 只解释论文里的术语 | - | - | - | 被排除 |

每个 Skill 应描述自身的目标。例如心理支持的 G 可以是缓解惊恐、改善睡眠或寻求支持，而不统一要求“制定计划”。对于“我睡不着，想缓一缓”等隐含目标，使用明确场景规则覆盖并配测试，不用常量 G=1 凑高分。

## 7. 门控与竞争组选择算法

初始规则阈值为 80 分，接近阈值为 5 分。配置使用整数百分制，诊断展示 0 到 1 小数。

```python
for group in scenario_groups:
    high = sorted(
        (c for c in group if not c.excluded and c.rule_points >= 80),
        key=lambda c: (-c.rule_points, c.name),
    )
    if not high:
        reject_group("RULE_BELOW_THRESHOLD")
    elif len(high) == 1:
        choose(high[0], "RULE_SINGLE_HIGH")
    elif high[0].rule_points - high[1].rule_points >= 5:
        choose(high[0], "RULE_CLEAR_WINNER")
    else:
        near = [c for c in high if high[0].rule_points - c.rule_points < 5]
        disambiguate_or_reject(near)
```

上述代码为分支示意，正式实现从 Settings 读取 80 和 5，不将阈值写死。name 只保证排序展示稳定，同分时仍进入消歧，不能凭名字选中。

边界约定：

1. 分数等于接受阈值算达标；第一、第二名分差等于 5 分时算明确，少于 5 分才进入消歧。
2. 0.83/0.79 这类外部示例表示只有一项高分，不进入 Embedding；本期离散公式不一定生成这些精确分数。
3. 三个及以上高分近邻必须一起参与，不只比较前两项；否则可能遗漏真正的语义赢家。
4. Embedding 只能选择 near 集合成员，不能找回低分、排除、禁用或越域候选。
5. 语义拒选后不能退回按 priority、名字顺序、次高规则分强行挑选。
6. 某组拒选不撤销其他独立组已明确选中的 Skill，也不撤销必要基线。
7. max_matches 或可选预算为 0 时跳过场景选择和语义调用；baseline 由独立策略处理。

当前离散评分以 5 分为最小步长，因此默认“分差小于 5 分”实际主要覆盖同分竞争。如果评测认为 90/85 分也应消歧，将 margin 调为 10 分，并同步边界测试；不能在代码中偷偷改成小于等于。

## 8. Embedding 消歧

### 8.1 查询和候选文本

- 查询：当前 task_text，去重后的 objective，以及范围过滤后全部场景候选声明的相关 known_arguments 值。每个 WorkItem 构造一份统一查询，不随竞争组或单个候选变化，以便组间复用。使用统一模板和稳定字段顺序；不拼接 Agent name、Skill name、全会话历史或其他 WorkItem。
- 候选：description 加 semantic_examples，用固定模板合成为每个 Skill 一份文档。本期每个候选只存一个向量，避免示例数量越多越容易取最大值的偏差。
- 使用支持中文且与文档编码一致的模型。description 建议统一为清晰中文，说明任务目标和适用边界；不把否定描述当成可靠的语义排除，排除规则由前一阶段处理。
- 描述及示例按第 13.3 节同步修改，不能只把现有关键词列表翻译或扩写成一段话。更新后依描述内容哈希失效缓存，并重新验证语义门槛。
- 文档使用 embed_documents，查询使用 embed_query；复用已有后端的接口约定。

### 8.2 选择标准

```text
semantic_score = cosine(query_vector, skill_vector)

只有同时满足以下条件才选：
top1_semantic_score >= semantic_min_similarity
top1_semantic_score - top2_semantic_score >= semantic_min_margin
```

初始配置建议为原始余弦阈值 0.70、分差 0.05，仅用于开始离线调试。不同模型的相似度分布不同，启用真实环境前必须评测。不要混用原始 cosine 和 `(cosine + 1) / 2` 的阈值，不做 softmax 后宣称是概率。

校验向量数量、维度、一致模型身份、有限数值和非零范数。任一候选向量缺失或无效时，该组本次拒选，不能删掉坏候选后把余下一个自动当赢家。异常诊断必须与规则低分拒选区分。

### 8.3 后端、缓存与时延

新增小型 `app/services/skill_semantics.py`，负责描述向量缓存、相似度和超时处理；不引入新向量数据库。13 个 Skill 无需 ANN 索引，直接内存余弦比较即可。

在 embedding.py 增加 `create_skill_embedding_backend(settings)`，参考 memory 工厂，通过 Settings 副本复用现有具体后端、连接池与错误校验。Skill 使用独立开关、模型配置和超时，不依赖 KNOWLEDGE_VECTOR_ENABLED 是否开启，不修改调用方共享 Settings。

缓存键至少包含 provider、base_url、model、人工配置的 embedding_version、语义模板版本、候选文档内容哈希。保持进程级有界缓存与并发填充保护，因为 runtime 会重新构造 SkillManager。refresh 生成不可变的新注册表快照，当前匹配固定使用同一快照；旧缓存项可按 LRU 回收，不得混用新旧描述。

本期只在实际出现高分近邻组时请求向量。冷缓存先填充当前近邻候选的文档向量，再编码查询；热缓存只需一次查询向量请求。同一个 WorkItem 多组消歧复用查询向量。查询向量仅在当前调用生命周期保存，不做跨用户的原文缓存。

将 3000ms 作为初始消歧阶段预算，包含缓存等待、文档编码、查询编码和计算。各外部调用使用剩余预算缩短超时，并在每一步检查已用时间；超时不重试、不降级到 LLM。HTTP 分阶段 timeout 不等同严格墙钟上界，不在实现或简历中承诺未经测量的硬实时 SLA；超过截止时间的结果不得被采纳。

模型同名更新时通过 embedding_version 和进程重启失效缓存；不在每次请求上额外调用模型列表或健康检查。后续若接入真实模型 digest，需纳入相同缓存身份和调用预算。

## 9. 配置项

以下为拟新增设置，实际部署时同步到 docker-compose.yml 的应用环境配置和 README 示例：

| 配置 | 初始值 | 说明 |
| --- | --- | --- |
| SKILL_SCENARIO_ENABLED | true | false 时不加载场景 Skill，保留固定机制与基线 |
| SKILL_RULE_MIN_POINTS | 80 | 整数 0..100 |
| SKILL_RULE_MIN_MARGIN_POINTS | 5 | 整数 1..100 |
| SKILL_SEMANTIC_ENABLED | true | false 时仅拒绝歧义组，不影响规则明确项 |
| SKILL_SEMANTIC_MIN_SIMILARITY | 0.70 | 原始余弦阈值，需离线验证 |
| SKILL_SEMANTIC_MIN_MARGIN | 0.05 | 正数，禁止以零分差接受完全平局 |
| SKILL_SEMANTIC_BUDGET_MS | 3000 | 消歧阶段预算 |
| SKILL_EMBEDDING_PROVIDER | 空值继承现有 knowledge_embedding_provider | 只继承连接配置，不继承知识库开关 |
| SKILL_EMBEDDING_MODEL | 空值继承现有 knowledge_embedding_model | 文档和查询必须相同 |
| SKILL_EMBEDDING_BASE_URL | 空值继承现有 knowledge_embedding_base_url | 使用现有凭证机制，不新增日志明文凭证 |
| SKILL_EMBEDDING_VERSION | v1 | 模型同名更换或语义版本治理 |

明确配置 disabled provider 时不可偷偷回退到其他 provider。非法阈值、非有限数、负预算、未知模式和重复规则 ID 在加载阶段拒绝；不要靠请求时静默修正。

## 10. API 与诊断契约

### 10.1 核心接口

在 skills.py 增加 `select(request: SkillMatchInput) -> SkillSelectionResult`。结果包含预算处理后的 matches 和结构化 diagnostics；SkillManager 不保存 last_selection 等请求共享可变状态。

保留旧 `match(agent, intent, text, risk)` 签名作为薄适配器，将 text 作为 task_text，objective 和 known_arguments 置空，返回 select().matches。适配器使用同一新算法，不维护第二套旧打分分支。兼容期调用方仍可读 score，但其值改为 rule_score，必须更新整数分数语义的测试和说明。

`SkillMatch` 拟增加 rule_score、semantic_score、selection_source 和 matched_rule_ids；baseline 的分数为 null，selection_source 为 BASELINE。as_dict 中旧 score 暂保留为同值别名并标记弃用。加载与模板读取接口继续存在。

### 10.2 结果记录

`SpecialistResultV2` 增加带默认空值的类型化可选字段 `skillSelection`，保持历史 payload 可解析。新增的嵌套诊断模型使用严格枚举和有界数组，不将任意输入 dict 原样透传。所有生产者、严格字段断言和评测适配一起更新；不更改已有业务 status/reasonCode 的含义。

至少记录：

- workItemId、selectorVersion、配置版本或哈希。
- 通过范围过滤的候选 ID、各候选规则分与命中规则 ID。
- 竞争组、近邻候选 ID、规则分差、语义分与语义分差。
- embedding 是否实际调用、调用数、缓存命中、耗时、固定错误码。
- qualifiedSkillIds、最终 injectedSkillIds、预算移除 ID 和原因。
- 每组决定和最终是否注入场景 Skill。

建议原因码：NO_ELIGIBLE_SKILL、RULE_EXCLUDED、RULE_BELOW_THRESHOLD、RULE_SINGLE_HIGH、RULE_CLEAR_WINNER、SEMANTIC_SELECTED、SEMANTIC_LOW_SCORE、SEMANTIC_AMBIGUOUS、SEMANTIC_DISABLED、SEMANTIC_UNAVAILABLE、SEMANTIC_TIMEOUT、SEMANTIC_INVALID_VECTOR、BASELINE、BUDGET_REJECTED、SCENARIO_DISABLED。

未实际进入模型上下文的候选不能出现在 selectedSkillIds。无 Skill 是正常可选结果，不得造成专业 Agent FAILED、伪造工具失败或直接返回兜底回复。必要基线无法装入输入预算时继续按现有输入预算错误处理。

trace.py 使用显式白名单记录 ID、规则 ID、数值和原因码，不记录任务原文、参数值、Skill 正文或向量；不直接序列化含 prompt_context 的 SkillMatch.as_dict。异常字符串不能原样带入可能包含服务请求内容的日志。

当前 ContextBuilder 会将 specialist_results 整体复制到合成 payload，因此新增诊断必须在合成和依赖视图构建时剔除，避免进入 Response 或其他专业 Agent 的提示词。

## 11. 正文注入与预算

1. 匹配使用元数据，完成选择后才构建可注入正文；保留 max_chars、total_chars、max_matches 的容量约束。
2. baseline 单独处理，scenario 的名额不挤出 baseline；旧 max_matches 明确迁移为场景名额上限。默认值仍可为 2，但当前单一竞争组最多产出一项场景。
3. 第一版不开发复杂 Markdown 步骤裁剪器。场景 Skill 的正文必须完整，不满足单项 max_chars 或剩余总字符预算就整项跳过；名称前缀和连接符计入预算。
4. 装入顺序为必要 baseline，然后已被各组接受的 scenario。跨组预算冲突可按 rule_score、priority、name 稳定排序；priority 只在此安排已被接受项，不用于消解组内歧义。不同 selection_source 的语义分不与规则分混合排序。
5. 复用 ContextBuilder 和 AgentLoop 现有 Token 估算与输入预算，不新增另一套估算公式。
6. 在 build_specialist_prompt 接入结构化的可选 Skill 列表或等价的显式参数。先保留固定 system 和必要 baseline；如果完整请求仍 protected_overflow，按上述逆序移除完整 scenario 并重建，直到可执行或无可选项可移除。
7. 所有重建复用同一 ContextPacket，不重查记忆、不重跑路由、不再次调用 Embedding。返回实际纳入的 Skill ID，更新 selectedSkillIds 与诊断。
8. 固定模板读取保持完整模板约束和单次变量替换，交接摘要不走场景裁剪。

## 12. 文件级修改清单

| 文件 | 实施内容 |
| --- | --- |
| `app/services/skills.py` | 新元数据解析、规则校验、结构化输入/结果、评分、组内门控、兼容适配、完整正文装入 |
| `app/services/skill_semantics.py`（新增） | 候选描述构造、有界缓存、余弦消歧、超时与失败契约 |
| `app/services/embedding.py` | Skill 专用后端工厂，复用已有实现 |
| `app/core/config.py` | 上述配置及数值校验 |
| `app/agents/event_driven_runtime.py` | 将 Settings 和语义服务注入 SkillManager |
| `app/agents/autonomous.py` | 传当前 WorkItem、使用选择结果、记录实际注入 ID、拒选后正常执行 |
| `app/agents/result.py` | 可选的类型化 skillSelection 字段和向后读取兼容 |
| `app/services/context_builder.py` | 完整可选 Skill 的预算移除、诊断从模型视图剔除 |
| `app/services/trace.py` | 新增受限 Skill 诊断摘要 |
| `skills/*/SKILL.md` | 按第 13 节完成 13 项类型迁移、10 项场景规则与描述改写、示例和正文约束对齐 |
| `tests/test_skills.py`、`tests/test_skill_regressions.py` | 保留加载保障，迁移旧分数与 always_match 测试 |
| `tests/test_skill_selection.py`（新增） | 规则与门控决策表 |
| `tests/test_skill_semantics.py`（新增） | 向量、缓存、失败和调用次数测试 |
| `tests/test_specialist_agents_v2.py` 等集成测试 | WorkItem 隔离、注入、拒选与结果契约 |
| `tests/test_trace_privacy.py` | 新诊断不泄漏正文和用户值 |
| `app/evaluation/datasets/skill-selection-v2.jsonl`（新增） | 带预期候选、拒选和困难负例的离线数据 |
| `tools/evaluate_skill_selection.py`（新增） | 确定性规则评测与可选真实 embedding 评测 |
| `README.md`、`docker-compose.yml` | 新配置、实际算法、拒选语义、回退方法 |

不新增数据库表、迁移或向量 collection。若评测适配器发现严格字段名单，按新增可选字段做最小更新，不改 RAG 评分与路由策略。

## 13. 配置迁移与兼容

### 13.1 通用迁移要求

1. 标准库中的 13 项配置在同一轮迁移完成。scenario 必须 matching_version=2 且有非空 domain/goal 强规则，否则加载失败并隔离。
2. 保留 keywords 作为过渡期展示元数据，不参与 V2 计分；README 明确真正生效的是 matching，避免两个配置来源冲突。
3. V2 scenario 中 always_match=true 属于非法配置；baseline/fixed 删除 always_match 字段。旧 optional 的公开字段保留，但不负责决定 selection_mode。
4. 未声明新版本的自定义 Skill 可由注册表读取用于展示和明确命名读取，但不自动进入新场景选择，状态给出迁移警告；不得静默沿用旧算法。
5. 保持 UTF-8/BOM 读取、错误文件隔离、重复 name 检查、必需目标查找、模板单次替换等既有保障。
6. 禁用文件不能经选择器重新进入候选；fixed 命名读取的现有业务约定单独保留，不借本次迁移重新定义必需模板接口。

### 13.2 逐项修改清单

以下描述为目标写法，实施时可根据测试调整措辞，但必须保持目标、规则和正文一致。所有 name、agents、intents 原有有效标识保留，不为解决候选混淆随意改动路由映射。

| Skill | description 的目标内容 | 规则与范围修改 | 正文修改重点 |
| --- | --- | --- | --- |
| academic_stress_planning | 帮助学生处理日常课程、考试复习和作业负担，将学习任务缩成可执行的短期安排和停止点。 | 考试、课程、作业作为场景；安排复习、拆解学习任务作为目标。压力、论文、挂科单独出现不构成强目标证据；具体科研推进和学业补救任务由对应配置覆盖。 | 保留承接压力和小步骤规划；不要把全部学业问题都导向通用时间安排。 |
| academic_warning_recovery | 帮助存在课程不及格、补考、重修或学业预警问题的学生梳理补救动作，核验相关影响，并安排课程恢复步骤。 | 挂科、补考、重修、学业预警为核心场景；补救、核验影响、恢复课程为目标。毕业、学位、绩点本身不能直接构成强场景证据。 | 按当前 ACADEMIC/CAMPUS 工作项执行；恢复计划与政策核验按任务需要使用，不能把所有流程强制全部展开。 |
| thesis_research_progress | 帮助学生解决论文、开题或毕设推进中的具体卡点，将当前阶段目标拆成可检查的产出、短期任务和检查点，并整理需要导师反馈的问题。 | 论文、开题、毕设为核心场景；科研产出、任务拆解、进度安排、反馈请求为目标。写作、导师、实验仅作宽泛线索；仅解释术语或仅查询日期不视为推进任务。 | 保留科研诚信和来源要求；“需要确认的问题”应作为待核实项，不自动变成面向用户的新追问。 |
| further_study_career_decision | 帮助学生比较升学、实习和就业方向，结合个人约束权衡选项，并安排低成本的验证行动或准备步骤。 | 发展方向词作为场景；比较、选择、权衡及方向准备为目标。单纯查报名日期、录取结果或名词含义不能仅凭考研等词达标。 | 保留已有“信息不足时给有限假设和可逆行动、不在执行期追问”的约束。 |
| campus_procedure_navigation | 帮助学生梳理学籍手续、证明办理、事项申诉和跨部门办事流程，确认负责单位、办理顺序及待核验信息。 | 具体行政事项与流程目标联合匹配；政策、资格、材料不单独作为强领域证据。纯奖助材料、住宿报修、补考补救由对应专用 Skill 覆盖，真实跨部门流程仍可参与匹配。 | 按当前事项选择主管部门、材料、步骤等内容；纯规则解释不强制输出完整办理清单。 |
| financial_aid_awards_guidance | 帮助学生处理奖学金、助学金、困难资助和评优申请，核验资格、准备材料、查询办理进度或整理异议处理步骤。 | 奖助项目、困难资助、评优为场景；资格核验、申请材料、进度、异议为目标；退回、已有通知等为条件。资格、材料、荣誉单独出现不足以构成强场景。 | 保留事实核验和禁止结果保证；用户只问资格时不强制展开全部材料流程。 |
| dormitory_life_guidance | 帮助学生处理室友协调、住宿噪音、设施报修或调宿问题，确定即时动作、沟通记录和正式处理渠道。 | 宿舍问题与协调、报修、调宿等目标联合判断。宿舍仅作为所在地或背景时不得高分。 | 保留冲突处理和现实安全边界；不推断室友动机，不把学习或奖助任务转为住宿处理。 |
| anxiety_grounding_support | 帮助当下紧张、惊恐或思绪失控的学生尝试温和的稳定动作，把注意力转回眼前能够完成的一步。 | 焦虑、惊恐是场景；当下缓解、稳定、平复是目标。呼吸单词、知识性询问或仅作为睡眠背景的焦虑不自动形成强目标。 | 保留动作自愿、非诊断和症状严重时求助的边界；不强制呼吸练习。 |
| sleep_routine_support | 帮助受到入睡困难或作息紊乱影响的学生安排今晚可执行的睡前步骤和作息调整，减少睡眠困扰。 | 睡不着、失眠、作息紊乱是场景；改善入睡、调整作息是目标。睡眠科普、偶然提及熬夜不能自动达标。 | 保留温和建议、无用药推荐和持续困扰时求助；不因缺少持续时长就中断执行追问。 |
| referral_resource_guidance | 帮助希望获得心理支持的学生选择校内外求助渠道，准备咨询预约、联系或说明情况的下一步。 | 删除 always_match；寻找支持渠道、预约咨询、准备求助为目标。求助、咨询、支持等泛化词需结合心理支持场景；普通校园事项咨询不构成适用任务。 | 按用户目标推荐必要渠道，不默认列全家人、老师、心理中心等所有选项；紧急危险仍交固定安全链路。 |
| supportive_response_baseline | 为心理支持回复提供基本的共情、非诊断、表达边界和简洁行动要求。 | selection_mode=baseline；移除 always_match，不参与规则分和向量消歧。 | 将“最多追问一个问题”调整为遵循 Coordinator 的澄清职责；普通专业执行不追加缺参追问，固定安全分支的安全问题由其自身策略控制。 |
| high_risk_safety_plan | 为高风险固定处理流程提供当前安全与现实支持指导。 | selection_mode=fixed；不增加场景评分规则，不依赖词命中或相似度接受阈值。 | 保留必要安全步骤和边界；不因本次场景拒选机制删减安全指令。 |
| counselor_handoff_summary | 在风险报告生成后，为工作人员生成事实性的交接摘要与后续安排。 | selection_mode=fixed；继续命名读取，不做向量匹配。 | 保留完整 text 模板、占位符、字段单次替换与必需目标显式失败。 |

通用与专用场景的边界通过正向任务规则、窄范围排除规则和对照样本表达。不要仅因为文本包含“奖学金”就排除所有通用流程候选，也不要仅因为专用 Skill 存在就抑制通用 Skill：例如“休学后助学金如何处理，需要联系哪些部门”仍可能是真实跨事项流程，应围绕当前 WorkItem 判断。如果明确边界后仍有多个高分候选，再按既定语义门控处理；本次不新增“专用优先”打分或低分候补机制。

### 13.3 描述、规则和示例的编写标准

1. description 写清“什么场景、用户要做什么、Skill 提供什么处理流程”，一到两句即可。泛化的“提供帮助、给予支持”不足以区分候选。
2. 可以统一中文方便维护，但不宣称中文本身必然提高准确率。向量模型的中文能力、描述区分度和实际评测共同决定效果。
3. keywords 按 domain、goal、context 重新分配；不得原样复制为 domain.strong。规则不得因同一词在多个字段重复出现而提高档位。
4. 每个场景 Skill 配 2 至 3 个有代表性的正向 semantic_examples，保持相近数量和长度，说明目标而非堆砌关键词。只做同义改写不足以构成不同代表场景。
5. 负例和邻近 Skill 的对照任务放入评测集，不拼到语义候选文档里；Scope 的“不适用”声明不能替代可执行排除规则。
6. 对应同一场景、不同目标编写对照，例如“解释论文术语”和“拆分论文任务”。只有话题相同不能视为适用。
7. Required Context 表示执行时有用的信息，不自动成为选择硬门槛或专业 Agent 的追问清单。按第 6 节定义由相关条件提供 C 分，不为缺少非必需条件一律拒选。
8. 纯知识解释、日期查询等请求允许场景 Skill 全部拒选，由 Agent 和 RAG 直接回答；不为增加命中率强行扩大已有 Skill 的职责。
9. description、规则、正文、样本必须同步审阅。修改描述会影响向量缓存和阈值适用性；修改正文后确认 max_chars 足够容纳完整内容，不能靠截断通过预算。

### 13.4 正文与专业 Agent 执行约束

场景正文大部分保留，只修改与适用边界或运行时职责不一致的指令：

- 专业 Agent 处理已分派的 WorkItem，不能因为加载了 Skill 再次执行全局意图分类、扩大任务或创建额外工作项。
- 普通缺参由 Coordinator 在专业执行前决定是否澄清；执行中按基础指令说明缺失信息、使用适用的有限假设或完成已能处理的部分，不新增向用户追问。安全固定分支不受普通场景规则覆盖。
- 输出结构服从当前问题，不强制每次同时输出全部章节。保持现有专业结果 JSON 契约；Skill 中的“回复结构”指导 answerBrief 的内容，不覆盖最终结构化返回格式。
- 用户自行提供的学习期限是规划约束；校方资格、官方日期、办理规则是需核验的事实，不能把二者混为一谈。
- Skill 不能要求不存在的工具、额外权限或未分派的流程。正文中的 RAG 核验仅在 Agent 当前有权限且任务需要时执行。
- 心理支持 baseline 与场景正文减少重复要求；必要共情和边界由基线表达，场景描述和步骤突出自身处理目标。

## 14. 测试与验收

### 14.1 确定性规则测试

| 情况 | 预期 |
| --- | --- |
| Agent、intent、enabled 或 risk 不满足 | 不入候选，不请求向量 |
| 全低分，包括只有一个低分候选 | 不选场景，向量调用为 0 |
| 一个高分、其余低分 | 直接选高分，向量调用为 0 |
| 多个高分且领先达到 margin | 直接选第一项，向量调用为 0 |
| 多个高分且领先小于 margin | 只传高分近邻集合给语义服务 |
| 分数刚好等于 min，分差刚好等于 margin | 按第 7 节边界执行 |
| 同义词重复、原文重复、objective 重复 task_text | 不重复加分 |
| 只有 objective 写出规划目标，task_text 未体现 | 不凭 objective 获得强 G |
| 相同文字位于其他 WorkItem | 不污染当前选择 |
| 泛化词、明确拒绝、单纯背景提及 | 不误选高分；明确覆盖已知否定局限 |
| 高 priority、always_match 或低分候选的语义向量更近 | 不能越过门控 |
| 空文本、场景开关关闭、零场景预算 | 不选择场景，不请求向量 |

### 14.2 语义测试

使用可注入 fake backend 和受控时钟验证，不在普通单元测试中访问真实网络：

- 最高语义分达标且拉开差距时选择；低绝对分、平局或小分差时拒选。
- 三个以上近邻、规则第一名不是语义第一名、候选缺失等情况。
- disabled、HTTP 错误、超时、维度不同、零向量、NaN/Inf、向量数量不匹配均按组拒选。
- 拒选后不回退到 priority 或规则次高项，不调用 LLM，不做低分补召回。
- 冷缓存调用文档编码和查询编码，热缓存跳过文档编码；同工作项复用 query。
- 描述、模型、版本或 endpoint 改变时缓存失效；并发填充不混淆请求数据。
- 剩余预算不足不发起下一请求，已超时的结果不被采纳。

### 14.3 集成回归

- 选中正文只注入对应专业 Agent；未选中正文不出现，Response 不重新匹配。
- 无场景 Skill、语义失败均不阻止专业 Agent 正常工具调用和回答。
- baseline 不参与竞争；fixed 高风险与交接模板仍按原链路工作。
- 场景预算不足时整项移除，不截断步骤；selectedSkillIds 与实际 prompt 一致。
- 历史 SpecialistResult payload 仍可读取，新诊断不进入模型合成或依赖上下文。
- Trace 只含白名单诊断，不含用户原文、knownArguments 值、正文、请求向量或凭证。
- 原 Skill 加载、异常隔离、BOM、模板和重复名称测试继续覆盖；旧算法断言按新契约更新，不能删除保障测试换取通过。
- 对 13 个仓库 Skill 校验 selection_mode、范围声明和类型对应；10 个 scenario 均有规则及代表性任务，baseline/fixed 不进入场景竞争。
- 对基线和场景执行路径验证不追加普通缺参追问，同时保留高风险固定分支的安全处理能力。
- 对照注入后的完整 system 与模型返回契约，确认 Skill 的章节要求未覆盖专业 Agent 的结构化输出要求。

### 14.4 离线评测

数据以 WorkItem 为单位，包含 agent、intent、task_text、objective、known_arguments、可接受 Skill 集合和预期拒选原因。包含一个都不适用、规则低分但人工认为适用、多个候选语义接近、分数高但目标错误等困难样本。

先按任务/表达来源划分调参集和独立测试集，避免同一句话的改写分散到两边。只在调参集上调整规则和阈值，然后冻结版本评测测试集。

报告场景选择 precision、recall、空选比例、误加载率、漏加载率、语义触发比例、调用数、冷/热缓存耗时及匹配 P50/P95。兼容组可用集合评价，竞争组按可接受 ID 集合评价。没有实测前不写准确率、延迟下降或成本下降百分比。

规则分和原始余弦仍不是概率；本期不加入逻辑回归或概率校准模型。若未来需要概率，另行用独立标注和校准评测验证。

### 14.5 仓库 Skill 对照验收样本

下面是迁移后必须覆盖的起始样本，不是已经通过的测试结果。测试输入明确指定正确 agent/intent，并以当前 WorkItem 的任务文本构造；不依赖此次测试之外的路由猜测。除非注明，预期为对应场景经过规则直接选择；困难表达允许按评测集记录实际漏选，但不能将未通过项改标成正确来迎合实现。

| 场景 Skill | 应适用的任务 | 不应触发该 Skill 的对照任务 |
| --- | --- | --- |
| academic_stress_planning | 明天考试，帮我安排今晚各科复习任务。 | 帮我解释论文里的一个术语。 |
| academic_warning_recovery | 高数挂科了，帮我梳理补考和重修的补救步骤。 | 毕业典礼在什么时候？ |
| thesis_research_progress | 下周开题，帮我拆分每天的准备任务。 | 论文里的“置信区间”是什么意思？ |
| further_study_career_decision | 考研和就业怎么选，帮我比较成本和准备时间。 | 考研报名截止日期是哪天？ |
| campus_procedure_navigation | 办理在读证明需要哪些步骤，应该找哪个部门？ | 助学金申请需要准备哪些材料？ |
| financial_aid_awards_guidance | 助学金申请需要准备哪些材料？ | 办理在读证明需要哪些材料？ |
| dormitory_life_guidance | 宿舍水管漏水，怎么报修和联系负责人员？ | 我在宿舍整理助学金申请材料，需要准备什么？ |
| anxiety_grounding_support | 我现在很惊恐，想先缓下来，可以做什么？ | 焦虑这个词是什么意思？ |
| sleep_routine_support | 最近睡不着，帮我安排今晚的睡前步骤。 | 昨晚熬夜写论文，帮我拆分今天的写作任务。 |
| referral_resource_guidance | 我想找心理咨询，怎么选择求助渠道和准备预约？ | 我想咨询宿舍报修流程。 |

还需覆盖以下跨 Skill 或混合目标样本：

- “下周交论文，压力很大，帮我拆分论文写作任务”：论文推进应有明确证据，压力不能让通用学习规划获得同等强目标。
- “焦虑导致睡不着，想改善今晚的睡眠”：重点检查睡眠目标；焦虑仅作背景时不应自动给当下稳定 Skill 强 G。若规则仍形成真实高分竞争，允许语义消歧，失败则拒选。
- “现在惊恐得静不下来，先帮我缓解，睡眠的事以后再说”：目标是当下稳定；后置或明确暂缓的睡眠事项不能获得强目标分。若现有字面操作符不足以可靠覆盖，记录规则局限并改进任务短语规则，不谎称已实现通用否定理解。
- “不要时间计划，帮我整理论文进展，形成向导师的反馈请求”：不得用“不需要计划”一类泛化排除规则删除论文推进候选。
- “休学后助学金如何处理，需要联系哪些部门”：不对通用办事 Skill 使用全局“出现助学金即排除”规则；按已分派任务评估通用流程和奖助 Skill，真实歧义按语义门控拒选或选择。
- “我只是提到论文，没有要制定计划或处理进度”：没有充分适用目标时允许全部场景拒选，且 Embedding 调用次数为零。

这些用例同时用于审查规则分、语义近邻集合和最终注入内容。出现高分误选时先检查任务规则及范围，再调整阈值；不能一律提高阈值掩盖描述与规则的职责重叠。

## 15. 实施顺序与完成标准

1. 定义类型、配置校验、输入和诊断契约，确认所有生产者/消费者。
2. 实现规则评分和竞争组门控，用 fake 语义服务完成决策表测试。
3. 实现独立 embedding 工厂、描述缓存与语义拒选，补齐错误和调用次数测试。
4. 按第 13 节逐项迁移全部标准 Skill，先用第 14.5 节的正反例检查范围与目标，再评审规则、description、示例和正文是否对应同一能力；在配置质量合格后验证语义阈值。
5. 接入专业 Agent、完整注入预算与实际 ID 记录，补齐 trace 和模型视图隔离。
6. 运行相关 Skill、专业 Agent、Response、上下文预算、事件 runtime、trace 测试；通过后进行离线规则与真实 embedding 评测。
7. 更新 README、部署配置和实施报告，记录实际阈值、模型版本、验证结果及剩余漏选案例。

每一步独立验证后再推进，失败先解决对应契约。当前目录未发现 Git 仓库，实施前对将改动的文件保存到带时间戳的 target/verification 目录，记录备份清单；不得假定可用 git reset 回退。

运行时回退可以关闭 SKILL_SEMANTIC_ENABLED，使高分歧义组拒选；关闭 SKILL_SCENARIO_ENABLED 可停用全部场景注入。恢复旧版本行为须同时恢复配套代码和 Skill 元数据，不混用两个版本的配置。

实施完成必须同时满足：所有确定性拒选分支正确、低分向量调用为零、语义不明确不强选、无 Skill 不影响基本执行、必需固定流程正常、预算和注入 ID 一致、配置异常隔离、相关回归通过，以及真实语义阈值的验证结果有据可查。

## 16. 编码与交付约束

- 所有编辑文件保存为 UTF-8，优先无 BOM，保留中文直接可读字符。
- 使用局部修改，避免无关文件、编码或元数据的批量重写。
- 交付前扫描变更文件中的反斜杠 u 加四位十六进制形式，普通字符串和注释中的意外 Unicode 转义必须恢复为可读字符。
- 实施报告分清已实现、已验证、未验证；语义服务未实际运行时不能宣称真实 embedding 消歧效果已验证。
- 本文不代表代码已完成。后续编码以本文已确认的拒选边界和阶段职责为准，不扩展成全量语义召回或 LLM 技能路由。
