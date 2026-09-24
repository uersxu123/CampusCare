# GovAgent 名称 Agent Skill 与 Prompt 改造实施指南

> 状态：待实施
>
> 适用仓库：`mindbridge-py`
>
> 实施范围：只覆盖品牌与核心名称、Specialist Agent 名称、Skill、Prompt 路由与前端文案
>
> 明确不包含：法律知识库接入、知识领域扩展、业务评测数据集重建、简历指标计算

---

## 0. 文档用途

本文是可以直接交给编码 AI 执行的改造契约。编码 AI 不应重新讨论整体架构，也不应借本次改名重写事件驱动 Runtime、RAG、MCP、ContextBuilder 或数据库模型。

实施前必须完成以下动作：

1. 完整阅读本文。
2. 检查 `git status --short`，识别并保护用户已有修改。
3. 阅读本文列出的核心文件，确认代码仍与本文描述一致；若不一致，先报告差异，不得盲目套用补丁。
4. 按阶段实施，每个阶段先完成最小测试，再进入下一阶段。
5. 所有源码和文档保存为 UTF-8，中文必须保持为可读字符，不得用 Unicode 码位转义代替正常中文。
6. 不运行数据库迁移，不删除历史 Trace，不改写用户数据，不修改知识库语料和评测数据集。
7. 不使用全仓库机械字符串替换完成 Agent 改名。

本文生成时观察到工作区已有以下未提交修改，编码 AI 必须把它们视为用户修改并避开覆盖：

- `INTERVIEW_STUDY_PROGRESS.md`
- `app/evaluation/evaluators/routing.py`
- `tests/evaluation/test_routing_evaluator.py`

实际实施时仍须重新检查 Git 状态，不能假定上述列表保持不变。

---

## 1. 改造目标

当前代码事实是：

- 产品品牌同时出现 `MindBridge` 和 `CampusCove`；
- Runtime 的真实主类是 `MindBridgeAgentHarness`；
- 候选回复由 `ResponseAgent` 形成；
- 任务创建、澄清与最终采纳由 `CoordinatorAgent` 和 `EventDrivenCoordinator` 协同完成；
- 四个功能 Agent 仍叫 `GeneralChatAgent`、`AcademicPlanningAgent`、`CampusAffairsAgent`、`PsychologicalSupportAgent`；
- Skill 和 Prompt 的主要业务语义仍是学生、学业、校园办事和辅导员；
- 简历中的项目名称和链路使用 `GovAgent`、`GovAgentHarness`、`SynthesisAgent`、`OrchestratorAgent`，并描述综合咨询、法律解读、政务规划和心理关怀。

本次改造后的目标是：

```text
GovAgentHarness
    -> UnderstandingAgent
    -> OrchestratorAgent 创建任务与控制执行
    -> GeneralConsultationAgent / GovernmentPlanningAgent /
       LegalInterpretationAgent / PsychologicalCareAgent
    -> SynthesisAgent 汇总候选回复
    -> SafetyAgent 复审
    -> OrchestratorAgent FINAL_ACCEPTED
```

必须同时满足以下约束：

- 新代码、Agent 状态接口、新 Trace 和 README 使用 GovAgent 新名称；
- 旧 Python 导入名称在一个兼容周期内仍然可用；
- 旧 Agent 名称作为输入时可以规范化到新名称；
- 历史 Trace 和历史 JSON 不做批量重写；
- `IntentType.CHAT / ACADEMIC / CAMPUS / MENTAL / RISK` 本阶段保持不变；
- RoutePlan、Artifact kind、任务依赖、Safety 门禁和最终流式生成契约保持不变；
- 法律知识库尚未接入时，法律 Agent 不得生成具体、无证据的法律结论。

---

## 2. 明确不做

编码 AI 不得把下列内容夹带进本次改造：

1. 不增加 `LEGAL`、`NATURAL_RESOURCES` 等新 `IntentType`。
2. 不修改现有 RoutePlan V3、UnderstandingDecision V3 和 SpecialistResultV2 的 Schema。
3. 不创建新的数据库迁移，不重命名数据库表或列。
4. 不重命名 `/student.html`、`/api/reports/me` 等真实路由；只修改用户可见文案。
5. 不导入用户提供的法律文档，不修改 `knowledge_manifest.yaml`，不重建向量索引。
6. 不重写 RAG 的 BM25F、Chroma、Rerank、Grade、Rewrite 或 Parent Expansion。
7. 不重写 MCP 协议、ToolExecutor、TTL、熔断、fallback 和风险工具队列。
8. 不重建业务评测集，不重新计算路由准确率、高风险召回率、忠实度和上下文精确率。
9. 不删除心理风险能力；简历仍保留“心理关怀”，Safety 与高风险链路必须继续工作。
10. 不把 `EventDrivenCoordinator` 改名为 `OrchestratorAgent`。前者是事件调度器，后者是 Agent 身份，两者职责不能混淆。

---

## 3. 不可破坏的现有架构

以下组件的名称和职责保持不变：

- `CollaborationBlackboard`
- `AgentTask`
- `AgentArtifact`
- `AgentEvent`
- `AgentTurnResult`
- `EventDrivenCoordinator`
- `ContextBuilder`
- `SkillManager`
- `ToolRegistry`
- `ToolExecutor`
- `TTLCache`
- `CircuitBreaker`
- `EvaluationRuntimeAdapter`
- `KnowledgeService`
- `AgentLoop`

以下行为必须保持：

- Blackboard 仍为追加式状态；
- 每个 WorkItem 仍只允许一个终态 `specialist_result`；
- Specialist 仍通过 `planId` 和 `workItemId` 与 RoutePlan 对齐；
- `response_proposal` 必须经过 `SafetyAgent` 复审后才能 `FINAL_ACCEPTED`；
- 普通 Specialist 只能看到允许的只读工具；
- 高风险写工具继续与普通对话 MCP 隔离；
- Harness 继续负责输入准备、上下文注入、澄清恢复、风险报告、工具计划、持久化和 Trace；
- TurnExecution 继续在 Runtime 完成后执行最终模型生成和 SSE 输出。

---

## 4. 目标命名词典

### 4.1 产品与角色名称

| 旧名称 | 新名称 | 兼容要求 |
| --- | --- | --- |
| MindBridge | GovAgent | 用户可见内容全部改用新名称 |
| CampusCove | GovAgent | 用户可见内容全部改用新名称 |
| MindBridge Python | GovAgent Python | README 和状态描述使用新名称 |
| 学生端 | 咨询端 | URL 暂时不改 |
| 学生 | 用户或咨询人 | 数据模型 `UserAccount` 不改 |
| 管理员后台 | 政务管理端 | 权限模型不改 |
| 校园知识库 | 政务知识库 | 本阶段只改展示，不接新语料 |
| 心理报告 | 风险关怀记录 | `PsychologicalReport` 数据模型不改 |
| 辅导员交接 | 风险个案交接 | 工具和模板方法需兼容 |

### 4.2 Runtime 与 Agent 名称

| 旧主名称 | 新主名称 | 新职责说明 |
| --- | --- | --- |
| `MindBridgeAgentHarness` | `GovAgentHarness` | 单轮 Agent Runtime 外层编排 |
| `CoordinatorAgent` | `OrchestratorAgent` | 任务创建、澄清、仲裁、最终采纳 |
| `ResponseAgent` | `SynthesisAgent` | 汇总 Specialist Artifact，发布 `response_proposal` |
| `GeneralChatAgent` | `GeneralConsultationAgent` | 综合咨询与允许的通用只读工具 |
| `AcademicPlanningAgent` | `GovernmentPlanningAgent` | 政务规划、任务拆解和实施建议 |
| `CampusAffairsAgent` | `LegalInterpretationAgent` | 法律、政策和政务规则解释 |
| `PsychologicalSupportAgent` | `PsychologicalCareAgent` | 非高风险心理关怀和支持 |

保留名称：

- `UnderstandingAgent`
- `SafetyAgent`
- `EventDrivenCoordinator`

### 4.3 内部 Intent 的临时业务映射

本阶段不迁移枚举值，必须建立明确的兼容映射：

| 内部 Intent | 本阶段业务含义 | 新 Specialist |
| --- | --- | --- |
| `CHAT` | 综合咨询 | `GeneralConsultationAgent` |
| `ACADEMIC` | 政务规划 | `GovernmentPlanningAgent` |
| `CAMPUS` | 法律解读与政务规则 | `LegalInterpretationAgent` |
| `MENTAL` | 心理关怀 | `PsychologicalCareAgent` |
| `RISK` | 高风险安全处置 | `SafetyAgent` 与 `SynthesisAgent` |

不得在新 Prompt、API 描述或 README 中继续把 `ACADEMIC` 描述为学业，也不得把 `CAMPUS` 描述为校园事务。内部枚举保留只是为了避免本阶段引入数据库和历史 Trace 迁移。

---

## 5. 阶段 A 建立统一身份与兼容层

### 5.1 新增 `app/core/product_identity.py`

建议新增一个纯常量与规范化模块，禁止该模块依赖 Agent、Service、数据库或 Web 层，避免循环依赖。

建议提供以下常量：

```python
PRODUCT_NAME = "GovAgent"
PRODUCT_CODE = "govagent"
LEGACY_PRODUCT_NAMES = ("MindBridge", "CampusCove")

ORCHESTRATOR_AGENT = "OrchestratorAgent"
UNDERSTANDING_AGENT = "UnderstandingAgent"
SAFETY_AGENT = "SafetyAgent"
GENERAL_CONSULTATION_AGENT = "GeneralConsultationAgent"
GOVERNMENT_PLANNING_AGENT = "GovernmentPlanningAgent"
LEGAL_INTERPRETATION_AGENT = "LegalInterpretationAgent"
PSYCHOLOGICAL_CARE_AGENT = "PsychologicalCareAgent"
SYNTHESIS_AGENT = "SynthesisAgent"
```

建议提供旧名称映射：

```python
LEGACY_AGENT_ALIASES = {
    "CoordinatorAgent": "OrchestratorAgent",
    "ResponseAgent": "SynthesisAgent",
    "GeneralChatAgent": "GeneralConsultationAgent",
    "AcademicPlanningAgent": "GovernmentPlanningAgent",
    "CampusAffairsAgent": "LegalInterpretationAgent",
    "PsychologicalSupportAgent": "PsychologicalCareAgent",
}
```

建议提供 Skill Agent 映射：

```python
LEGACY_SKILL_AGENT_ALIASES = {
    "response": "synthesis",
    "general_chat": "general_consultation",
    "academic_planning": "government_planning",
    "campus_affairs": "legal_interpretation",
    "psychological_support": "psychological_care",
}
```

必须实现两个无副作用函数：

```python
def canonical_agent_name(value: str) -> str: ...
def canonical_skill_agent(value: str) -> str: ...
```

行为要求：

- 去除首尾空白；
- 新名称原样返回；
- 已知旧名称映射为新名称；
- 未知名称原样返回，不静默映射为默认 Agent；
- 空值返回空字符串或显式抛出 `ValueError`，全项目必须统一一种策略；推荐返回空字符串，由调用者决定是否拒绝。

### 5.2 身份层单元测试

新增 `tests/test_govagent_identity.py`，至少覆盖：

1. 所有旧 Agent 名称均映射到预期新名称。
2. 所有新名称幂等返回。
3. 未知名称不会被错误映射。
4. Skill Agent 的旧 snake_case 名称可以规范化。
5. `PRODUCT_NAME == "GovAgent"`。
6. Agent 新名称不存在重复值。

### 5.3 阶段 A 验收

- 新模块无业务依赖和循环导入；
- 仅新增常量和测试时，全量现有测试仍应通过；
- 不修改 IntentType、数据库或 API Schema。

---

## 6. 阶段 B 改造 Harness 与核心 Agent 名称

### 6.1 `app/agents/harness.py`

执行顺序：

1. 将主类定义从 `MindBridgeAgentHarness` 改为 `GovAgentHarness`。
2. 更新类 docstring 中的产品名称。
3. 文件末尾提供兼容别名：

```python
MindBridgeAgentHarness = GovAgentHarness
```

4. 新生产代码必须导入 `GovAgentHarness`；旧别名只服务于外部兼容和过渡测试。
5. 不改变 `run()`、`dispatch_tools()`、返回 DTO 和事务边界。

同步修改：

- `app/services/chat.py`
- `app/services/turn_execution.py`
- `app/api/routes.py`
- Harness 相关测试和文档

注意：`chat.py` 当前在完成生成后重新实例化 Harness 用于后置工具分发。改名时不得遗漏第二处实例化，也不得改变后置工具失败不影响已完成回复的容错行为。

### 6.2 `app/agents/autonomous.py`

将类定义改为新主名称：

```text
ResponseAgent              -> SynthesisAgent
CoordinatorAgent           -> OrchestratorAgent
GeneralChatAgent           -> GeneralConsultationAgent
AcademicPlanningAgent      -> GovernmentPlanningAgent
CampusAffairsAgent         -> LegalInterpretationAgent
PsychologicalSupportAgent  -> PsychologicalCareAgent
```

每个 `AgentProfile.name` 必须使用新名称常量，禁止继续写散落字符串。

文件末尾保留旧类名兼容别名：

```python
ResponseAgent = SynthesisAgent
CoordinatorAgent = OrchestratorAgent
GeneralChatAgent = GeneralConsultationAgent
AcademicPlanningAgent = GovernmentPlanningAgent
CampusAffairsAgent = LegalInterpretationAgent
PsychologicalSupportAgent = PsychologicalCareAgent
```

兼容别名只能放在所有新类定义之后。运行时注册必须实例化新类，不能继续实例化旧别名后声称改名完成。

### 6.3 `SynthesisAgent` 的职责约束

只改身份与 Prompt，不改变以下行为：

- 仍只认领 `task.metadata.kind == "response"` 的任务；
- 仍等待 fan-in 完成；
- 仍调用 `ordered_specialist_results()`；
- 仍去重 evidence；
- 仍发布 `response_proposal`；
- 高风险时仍走确定性高风险 Prompt；
- 不直接执行外部工具；
- 不自行调用法律知识库；
- 不直接 `FINAL_ACCEPTED`。

删除或泛化当前仅适用于学习计划的分支：

```text
is_concrete_study_plan_request
STUDY_PLAN
课程记录
```

推荐将其替换为通用规划约束规则：如果 `knownArguments` 是用户直接提供的明确约束，规划 Agent 可以使用这些约束，无需把用户自己的要求当作外部事实进行 RAG 验证。不要再保留 `STUDY_PLAN` 字样。

相关文件：

- `app/services/academic_request_policy.py`：改为通用规划策略模块，或新增通用函数后保留旧函数兼容；
- `app/agents/autonomous.py`：Synthesis Prompt 使用通用规划约束措辞；
- 对应单元测试：从课程计划改为政务方案约束。

### 6.4 `OrchestratorAgent` 与 `EventDrivenCoordinator`

必须保持职责分离：

- `OrchestratorAgent` 是具有 Profile、名称和任务创建职责的 Agent 身份；
- `EventDrivenCoordinator` 是执行轮次、任务认领、预算控制和最终接纳的调度器。

修改点：

- `root_task.created_by` 写入 `OrchestratorAgent`；
- 澄清任务和澄清 Artifact owner 使用 `OrchestratorAgent`；
- `TURN_STARTED`、`TASK_CREATED`、`FINAL_ACCEPTED` 的 actor 使用新名称；
- 最终采纳原因改为“accepted after SynthesisAgent proposal and SafetyAgent review”；
- 不把调度器类改成 Agent；
- 不新增第二套 Orchestrator Runtime。

### 6.5 `app/agents/events.py`

当前存在硬编码约束：只有 `CoordinatorAgent` 可以发布 `clarification_request`。应调整为：

```python
canonical_agent_name(artifact.owner) == ORCHESTRATOR_AGENT
```

同时：

- `AgentTask.created_by` 默认值改为新名称常量；
- 新创建的 Task、Message、Artifact、Event 统一写入新名称；
- 读取或校验旧测试夹具、历史数据时允许旧名称通过规范化；
- 不在 dataclass 构造时自动重写所有输入值，避免读取历史 Trace 时发生不可见变更；
- 只在权限判断和新事件生产边界做规范化。

### 6.6 `app/agents/event_driven_runtime.py`

运行时必须导入并实例化新类：

```text
OrchestratorAgent
UnderstandingAgent
SafetyAgent
GeneralConsultationAgent
GovernmentPlanningAgent
LegalInterpretationAgent
PsychologicalCareAgent
SynthesisAgent
```

Agent 注册顺序保持现状，避免改变同置信度候选的稳定顺序。

### 6.7 `app/services/agent_models.py`

模型角色映射应以新名称为主：

```text
OrchestratorAgent -> coordinator profile
UnderstandingAgent -> understanding profile
SafetyAgent -> safety profile
GeneralConsultationAgent -> specialist profile
GovernmentPlanningAgent -> specialist profile
LegalInterpretationAgent -> specialist profile
PsychologicalCareAgent -> specialist profile
SynthesisAgent -> response profile
```

`client_for(agent_name)` 入口先调用 `canonical_agent_name()`，再查询角色。不得在字典中维护两套会逐渐漂移的完整配置。

所有针对 `ResponseAgent` 的特殊判断改为针对规范化后的 `SynthesisAgent`。错误信息、预算提示和 purpose 字符串使用新名称。

### 6.8 `app/services/tool_registry.py`

工具权限保持不变，只改变 Agent 身份：

| Agent | 允许工具 |
| --- | --- |
| `GeneralConsultationAgent` | `get_current_weather` |
| `GovernmentPlanningAgent` | `rag_search` |
| `LegalInterpretationAgent` | `rag_search` |
| `PsychologicalCareAgent` | `rag_search` |
| 其他 Agent | 无普通对话工具 |

`for_agent()` 必须先规范化 Agent 名称，使旧名称在兼容周期内得到相同权限。禁止因为改名导致 Specialist 看不到 RAG，或者让 `SynthesisAgent`、`SafetyAgent` 获得普通工具。

### 6.9 Trace 策略

`app/services/trace.py` 不负责迁移历史名称，但新运行写出的以下字段必须使用新名称：

- event `actor`
- task `createdBy`
- task `claimedBy`
- artifact `owner`
- SpecialistResult `agentName`
- tool diagnostics `agentName`

读取历史 Trace 时保留原值。若 UI 需要统一显示，可在展示层使用 `canonical_agent_name()`，不得批量更新数据库 JSON。

### 6.10 阶段 B 测试

重点更新和执行：

- `tests/test_event_driven_multi_agent.py`
- `tests/test_coordinator_work_items_v3.py`
- `tests/test_refactor_phase0_contracts.py`
- `tests/test_specialist_agents_v2.py`
- `tests/test_ai_completion.py`
- `tests/test_tool_calling_v2.py`
- `tests/test_mcp_tools_v2.py`
- `tests/test_trace_privacy.py`

必须新增或保留以下断言：

1. Runtime Registry 中出现全部新名称且没有重复 Agent。
2. 旧类名仍可导入，但实例的 `profile.name` 是新名称。
3. `response_proposal.owner == "SynthesisAgent"`。
4. `clarification_request.owner == "OrchestratorAgent"`。
5. `FINAL_ACCEPTED.actor == "OrchestratorAgent"`。
6. 最终采纳前必须存在关联当前 `responseArtifactId` 的 Safety Review。
7. 新旧 Specialist 名称取得完全相同的工具权限。
8. Safety、Understanding、Synthesis、Orchestrator 均无法取得普通工具。

---

## 7. 阶段 C 改造 Specialist Capability 与 Skill

### 7.1 Agent Capability

当前 `AgentCapability` 中存在校园业务名称。为了减少 RoutePlan 变化，本阶段可以采用以下两种方式之一，但全项目只能选择一种。

推荐方式：新增新 Capability 值，并提供旧字符串规范化。

```text
GENERAL_CHAT           -> GENERAL_CONSULTATION
ACADEMIC_PLANNING      -> GOVERNMENT_PLANNING
CAMPUS_AFFAIRS         -> LEGAL_INTERPRETATION
PSYCHOLOGICAL_SUPPORT  -> PSYCHOLOGICAL_CARE
RESPONSE               -> SYNTHESIS
COORDINATION           -> ORCHESTRATION
```

实现要求：

- 新任务只写新 Capability；
- `_has_required_capability()` 比较前规范化旧值；
- 历史 Trace 不重写；
- 不改变 WorkItem 的 Intent 字段；
- 不允许旧新 Capability 同时写入同一 Task。

如果编码 AI 判断 Capability 兼容层会显著扩大本阶段风险，可以保留旧 Capability 枚举值，但必须在文档中明确它们只是内部遗留代码。不能只改一半，造成 Profile 使用新值、Task 仍写旧值而无法认领。

### 7.2 Specialist Profile

新 Profile 建议如下：

```text
GeneralConsultationAgent
  intent: CHAT
  skill_agent: general_consultation
  tools: get_current_weather

GovernmentPlanningAgent
  intent: ACADEMIC
  skill_agent: government_planning
  tools: rag_search

LegalInterpretationAgent
  intent: CAMPUS
  skill_agent: legal_interpretation
  tools: rag_search

PsychologicalCareAgent
  intent: MENTAL
  skill_agent: psychological_care
  tools: rag_search
```

### 7.3 Skill 基础类型重命名

在 `app/services/skills.py` 中建议改为：

```text
MindBridgeSkill          -> GovAgentSkill
MindBridgeSkillRegistry  -> GovAgentSkillRegistry
MindBridgeSkillLibrary   -> GovAgentSkillLibrary
```

保留旧 Python 名称兼容别名一个周期：

```python
MindBridgeSkill = GovAgentSkill
MindBridgeSkillRegistry = GovAgentSkillRegistry
MindBridgeSkillLibrary = GovAgentSkillLibrary
```

`SkillManager` 名称保持不变。

`SkillMatch.skill` 类型使用新类型。错误信息和日志中的产品名使用 GovAgent。

### 7.4 SkillManager 匹配兼容

`SkillManager.match(agent, intent, text, risk)` 必须：

1. 对 `agent` 调用 `canonical_skill_agent()`；
2. 对 Skill Front Matter 中的 `agents` 逐项规范化；
3. 继续对 Intent 执行大写标准化；
4. 保留 enabled、risk gate、keyword、always_match、priority、max_matches 和总字符预算行为；
5. 不改变排序规则；
6. 不引入 LLM Skill 路由；
7. 不把 Skill 内容写入 Trace 原文。

### 7.5 Skill 迁移清单

#### 7.5.1 保留名称但修改内容

| Skill | 修改要求 |
| --- | --- |
| `high_risk_safety_plan` | 保留名称；将学生、辅导员、校园保卫改为用户、可信任联系人、现场安全人员或当地紧急服务 |
| `anxiety_grounding_support` | 保留名称；`agents` 改为 `psychological_care`，去除校园身份限定 |
| `sleep_routine_support` | 保留名称；`agents` 改为 `psychological_care`，去除学生作息限定 |

#### 7.5.2 重命名并重写内容

| 旧 Skill | 新 Skill |
| --- | --- |
| `supportive_response_baseline` | `psychological_care_baseline` |
| `referral_resource_guidance` | `public_service_referral_guidance` |
| `counselor_handoff_summary` | `risk_case_handoff_summary` |
| `campus_procedure_navigation` | `government_service_navigation` |
| `academic_stress_planning` | `government_planning_support` |
| `academic_warning_recovery` | `legal_clause_interpretation` |
| `dormitory_life_guidance` | `land_use_procedure_guidance` |
| `financial_aid_awards_guidance` | `land_approval_authority_guidance` |
| `further_study_career_decision` | `natural_resources_planning_support` |

#### 7.5.3 暂停加载

`thesis_research_progress` 与目标业务没有自然的一一对应关系。本阶段将其 Front Matter 的 `enabled` 设置为 `false`，不要为了凑数量将论文内容机械改名成政策材料分析。未来确认真实业务需求后再决定替换。

### 7.6 目标 Skill Front Matter

#### `government_service_navigation`

```yaml
name: government_service_navigation
agents: [legal_interpretation]
intents: [CAMPUS]
keywords: [政务, 办理, 审批, 备案, 材料, 流程, 权限, 主管部门]
enabled: true
optional: true
priority: 95
max_chars: 2600
```

Workflow 必须覆盖：识别事项、确认适用范围、检索证据、整理办理步骤、标注缺失条件、输出下一动作。不得写死具体法律结论。

#### `government_planning_support`

```yaml
name: government_planning_support
agents: [government_planning]
intents: [ACADEMIC]
keywords: [规划, 方案, 计划, 实施, 部门, 阶段, 任务, 风险]
enabled: true
optional: true
priority: 95
max_chars: 2600
```

Workflow 必须覆盖：目标、约束、参与主体、阶段、依赖、风险、交付物和复盘指标。

#### `legal_clause_interpretation`

```yaml
name: legal_clause_interpretation
agents: [legal_interpretation]
intents: [CAMPUS]
keywords: [法律, 条例, 办法, 规定, 条文, 依据, 适用, 责任]
enabled: true
optional: true
priority: 105
max_chars: 2800
```

Workflow 必须覆盖：识别问题、定位证据、说明适用主体与条件、区分原文与解释、标注证据缺口。法律知识库尚未接入时，具体条文问题必须返回证据不足，不能依靠模型记忆补齐。

#### `land_use_procedure_guidance`

```yaml
name: land_use_procedure_guidance
agents: [legal_interpretation]
intents: [CAMPUS]
keywords: [土地, 用地, 农用地转用, 征收, 划拨, 复垦, 基本农田]
enabled: true
optional: true
priority: 100
max_chars: 2800
```

#### `land_approval_authority_guidance`

```yaml
name: land_approval_authority_guidance
agents: [legal_interpretation]
intents: [CAMPUS]
keywords: [审批权限, 批准, 授权, 委托, 国务院, 省政府, 市政府, 县政府]
enabled: true
optional: true
priority: 105
max_chars: 2600
```

#### `natural_resources_planning_support`

```yaml
name: natural_resources_planning_support
agents: [government_planning]
intents: [ACADEMIC]
keywords: [自然资源, 国土空间, 耕地保护, 成片开发, 复垦, 实施方案]
enabled: true
optional: true
priority: 100
max_chars: 2800
```

#### `psychological_care_baseline`

```yaml
name: psychological_care_baseline
agents: [psychological_care, synthesis]
intents: [MENTAL, RISK]
keywords: [焦虑, 抑郁, 压力, 心理, 痛苦, 失眠]
enabled: true
optional: false
always_match: true
priority: 110
max_chars: 2000
```

#### `public_service_referral_guidance`

```yaml
name: public_service_referral_guidance
agents: [psychological_care, synthesis]
intents: [MENTAL, RISK]
keywords: [求助, 转介, 联系, 支持, 危险, 紧急]
enabled: true
optional: true
priority: 100
max_chars: 2200
```

#### `risk_case_handoff_summary`

该 Skill 保留 `text` 模板。模板字段建议改为：

```text
记录编号：{{report_id}}
咨询人：{{user}}
风险等级：{{risk_level}}
主要情绪：{{emotion}}
置信度：{{confidence}}
情况摘要：{{summary}}
建议后续动作：
{{next_steps}}
原始表达摘录：
{{content_excerpt}}
```

同时修改 `GovAgentSkill.validation_issues()` 中固定检查名称，将 `counselor_handoff_summary` 改为 `risk_case_handoff_summary`。

### 7.7 Skill Library 方法

建议提供新方法：

```text
response_skill_context   -> synthesis_skill_context
response_skill_names     -> synthesis_skill_names
counselor_handoff_summary -> risk_case_handoff_summary
```

旧方法保留为薄包装，内部调用新方法，不复制实现。

将 `_student_label()` 改为 `_user_label()`；旧方法如果没有外部使用，可以直接删除，但不得改数据库字段和 `UserAccount`。

### 7.8 Skill 内容质量要求

每个启用的 `SKILL.md` 必须包含：

- YAML Front Matter；
- 清晰的 `description`；
- `## Scope`；
- `## Required Context`；
- `## Workflow`；
- `## Response Contract`；
- `## Escalation and Boundaries`。

Skill 内容不得包含：

- 挂科、补考、重修、奖学金、宿舍、校园网、论文推进等旧主业务；
- 未经本地知识证据支持的具体法律结论；
- “始终回答”“必须给出确定结论”等破坏证据门禁的指令；
- 让 Specialist 越权调用风险写工具的指令；
- 与 Safety 固定规则冲突的心理危机场景指令。

### 7.9 阶段 C 测试

更新 `tests/test_skills.py` 并新增业务匹配矩阵：

| 输入 | Agent | Intent | 必须命中 |
| --- | --- | --- | --- |
| 帮我梳理用地审批办理步骤 | legal_interpretation | CAMPUS | government_service_navigation 或 land_use_procedure_guidance |
| 这条规定适用于什么主体 | legal_interpretation | CAMPUS | legal_clause_interpretation |
| 帮我制定分阶段实施方案 | government_planning | ACADEMIC | government_planning_support |
| 国土空间规划怎么拆解任务 | government_planning | ACADEMIC | natural_resources_planning_support |
| 最近工作压力很大且睡不着 | psychological_care | MENTAL | psychological_care_baseline，并可命中 sleep_routine_support |
| 我现在想伤害自己 | synthesis | RISK | psychological_care_baseline 与 high_risk_safety_plan |

必须验证：

- Skill 目录名等于 Front Matter `name`；
- 所有启用 Skill 状态不是 FAILED；
- `risk_case_handoff_summary` 存在合法 `text` 模板；
- 不同时加载旧目录和新目录导致重复匹配；
- 高风险时普通可选 Skill 仍受现有 risk gate 限制；
- Skill 总字符预算和最多匹配数量保持原行为。

---

## 8. 阶段 D 改造 Prompt 路由语义与前端文案

### 8.1 `app/services/intent_prompts.py`

修改系统身份：

```text
你是 GovAgent 的 UnderstandingAgent
```

保持输出 Schema 和五个内部 Intent 值不变，但重写五类解释：

- `CHAT`：综合咨询、一般知识、技术问题和普通交流；
- `ACADEMIC`：政务方案、实施规划、任务拆解、阶段安排；
- `CAMPUS`：法律条文、政策规定、审批权限、政务办理；
- `MENTAL`：非高风险心理关怀、压力、焦虑、失眠和情绪支持；
- `RISK`：当前自伤、他伤、即时危险或已经实施伤害。

必须替换现有校园示例，建议采用：

```text
历史“我在比较两个用地方案”，当前“第二种呢？”
    -> CONTINUE + ACADEMIC

历史“我需要办理农用地转用”，当前“由哪个层级审批？”
    -> CONTINUE + CAMPUS

历史“最近工作压力很大”，当前“那我现在该怎么办？”
    -> CONTINUE + MENTAL

历史“我在问审批权限”，当前“换个问题，Python 报错怎么排查”
    -> NEW_TOPIC + CHAT

历史“我问的是市级审批”，当前“刚才说错了，是省级审批”
    -> CORRECTION + CAMPUS

历史“帮我制定项目实施计划”，当前“改成三个月完成”
    -> REFINE + ACADEMIC

当前“解释土地征收规定、确认审批权限、制定实施方案、分析复垦要求、再聊聊工作焦虑”
    -> TOO_MANY_WORK_ITEMS
```

修改 Prompt 后必须提升版本号，例如：

```text
INTENT_PROMPT_VERSION = "govagent-five-intent-context-v1"
```

不得修改 `UNDERSTANDING_SCHEMA_NAME`，因为 Schema 本身没有变化。

### 8.2 `app/services/intent_fusion.py`

重写 `INTENT_EMBEDDING_TEMPLATES_V3` 的语义模板。常量名可以暂时保留以减少改动，但版本必须更新，避免复用旧校园 Embedding 缓存：

```text
FUSION_CONSTANTS_VERSION = "govagent-five-intent-fusion-v1"
```

推荐模板：

```text
CHAT:
  问候、感谢、一般交流和通用咨询
  编程、数据库、写作、翻译和非政务通用问题
  不要求政策依据的普通知识说明

ACADEMIC:
  制定政务实施方案、工作计划和阶段安排
  拆解目标、任务、责任主体、依赖和风险
  比较多个治理方案并形成执行建议

CAMPUS:
  查询法律、条例、办法、规定和具体条文
  核验审批权限、办理条件、材料、流程和责任
  土地管理、自然资源、征收、划拨、复垦和基本农田规则

MENTAL:
  压力、焦虑、失眠、低落和情绪支持
  非高风险倾诉、关怀和现实支持建议
  工作、人际或生活事件引发的心理困扰

RISK:
  保持现有高风险模板，不降低召回边界
```

权重、阈值、融合公式和 tie order 本阶段保持不变。

### 8.3 `app/agents/routing.py`

规则路由必须同步 Prompt 语义，避免 LLM、Embedding、Rule 三路使用不同业务定义。

需要删除或降权的旧主业务关键词：

- 奖学金
- 补考
- 重修
- 校园网
- 校园卡
- 校区
- 宿舍
- 考研
- 保研
- 课程复习
- 论文推进

需要新增的规则信号：

```text
ACADEMIC / 政务规划：
方案、规划、计划、阶段、目标、任务、分工、里程碑、实施、推进、复盘、风险清单

CAMPUS / 法律政务：
法律、条例、办法、规定、条文、第几条、依据、适用、责任、审批、批准、授权、委托、备案、材料、流程、权限、土地、用地、征收、划拨、复垦、基本农田

MENTAL / 心理关怀：
保留现有压力、焦虑、失眠、低落、痛苦等信号

RISK：
完全保留现有高风险硬信号和 Safety 抢占逻辑
```

规则 fallback 文案改为：

```text
CHAT     -> 回应综合咨询请求
ACADEMIC -> 处理政务规划与实施方案请求
CAMPUS   -> 核验法律政策或政务办理事实
MENTAL   -> 提供非高风险心理关怀
RISK     -> 处理当前高风险请求
```

### 8.4 `app/agents/autonomous.py` Specialist Prompt

`SpecialistAgent` 当前使用共用执行框架。应按新 Agent 身份生成清晰职责：

#### GeneralConsultationAgent

- 处理普通咨询、技术问题、解释和允许的天气工具；
- 不把普通问题强行解释为法律或心理问题；
- 用户明确要求法律依据时交由 LegalInterpretationAgent。

#### GovernmentPlanningAgent

- 输出目标、约束、阶段、任务、责任主体、依赖、风险和交付物；
- 区分用户给定约束与外部事实；
- 需要法律依据时调用 RAG；没有证据时只输出规划框架，不虚构审批要求。

#### LegalInterpretationAgent

- 先定位证据，再解释；
- 输出适用主体、行为、条件、程序、责任和例外；
- 区分“文档明确规定”和“基于上下文的解释”；
- 法律知识库尚未接入时，对具体条文结论返回 PARTIAL 或 FAILED；
- 不把模型常识包装成本地证据。

#### PsychologicalCareAgent

- 保留温和、具体、非诊断式支持；
- 不输出后台分数和风险标签；
- 把“学生、辅导员、学校心理中心、校园保卫”泛化为“用户、可信任联系人、专业心理援助机构、现场安全人员或当地紧急服务”；
- 高风险仍由 Safety 固定链路优先。

### 8.5 `SynthesisAgent` Prompt

普通模式 Prompt 必须明确：

1. 按 `synthesisOrder` 汇总所有 `specialist_result`。
2. 保留每个 WorkItem 的完成状态和证据边界。
3. 对 `PARTIAL` 和 `FAILED` 明确说明未解决部分。
4. 多个结果引用相同 evidenceId 时去重。
5. 政务规划不得覆盖法律 Agent 给出的限制。
6. 法律 Agent 没有证据时，Synthesis 不得补充具体条文。
7. 心理关怀与法律咨询同时出现时分段表达，不相互替代。

高风险模式 Prompt 必须保持当前“安全优先、建议联系现实支持和紧急服务”的行为，只做角色称谓泛化。

### 8.6 `app/services/ai.py`

检查并替换以下硬编码：

- “你是 MindBridge”改为“你是 GovAgent”；
- “学生最终看到”改为“用户最终看到”；
- 校园事务改为政务事项；
- 学校心理中心、辅导员和校园保卫做通用化处理；
- Mock/Fallback 文本同步新业务语境；
- 检测 `ResponseAgent` 的 system 字符串改为检测 `SynthesisAgent`，最好不要继续依赖自然语言子串判断，而应使用稳定 mode 或 purpose；若本阶段不重构该机制，至少同时兼容新旧子串。

不得改变 provider 请求协议、finish reason、续写行为和流式生成。

### 8.7 澄清字段

`app/services/clarification_handlers.py` 当前存在 `studentType` 等校园字段。由于本阶段不做数据库迁移，采用兼容读取、新写新字段策略。

建议新增通用字段：

```text
jurisdiction    所在地区或适用地域
matterType      事项类型
subjectRole     涉及主体身份
currentStage    当前办理或实施阶段
targetOutcome   希望达成的结果
landType        涉及土地类型，可选
```

处理原则：

- 新政务请求只生成新字段；
- 已持久化的旧澄清状态仍能恢复或明确失效，不得解析崩溃；
- 不在本阶段迁移旧澄清 JSON；
- 不为每个法律概念增加固定字段；
- 问题最多聚焦当前执行必需的信息。

### 8.8 Memory 关键词

`app/services/memory.py` 中旧的学业、宿舍、奖学金等启发式关键词必须同步，否则新 Prompt 虽已改名，记忆摘要仍会把政务问题归入旧主题。

推荐主题：

```text
GOVERNMENT_PLANNING：规划、方案、任务、分工、阶段、里程碑、实施
LEGAL_POLICY：法律、条例、办法、规定、审批、权限、材料、流程
NATURAL_RESOURCES：土地、用地、征收、划拨、复垦、基本农田、国土空间
MENTAL_CARE：压力、焦虑、失眠、低落、心理、痛苦
```

不修改 Memory 表结构，只修改新摘要和标签的生成语义。

### 8.9 API 状态

`GET /api/agent/status` 必须返回新名称和新描述：

```text
OrchestratorAgent
UnderstandingAgent
SafetyAgent
GeneralConsultationAgent
GovernmentPlanningAgent
LegalInterpretationAgent
PsychologicalCareAgent
SynthesisAgent
```

`runtimeHarness.name` 返回 `GovAgentHarness`。

`skills` 使用 `GovAgentSkillLibrary.status_items()`。

状态接口不得同时展示新旧 Agent，兼容别名不属于独立 Agent。

### 8.10 前端文案

需要检查：

- `app/static/index.html`
- `app/static/student.html`
- `app/static/student.js`
- `app/static/admin.html`
- `app/static/admin.js`
- `app/static/brand.js`
- `app/static/styles.css` 中仅与品牌文案相关的注释

用户可见文本统一为：

```text
产品：GovAgent 政务多智能体协同平台
用户页面：政务咨询端
后台页面：政务管理端
知识模块：政务知识管理
风险模块：风险关怀记录
```

推荐快捷问题：

- 建设项目涉及农用地转用，需要先确认哪些事项？
- 帮我把一个自然资源管理目标拆成分阶段实施方案。
- 某项规定的适用主体和办理条件应该怎样理解？
- 最近工作压力很大，想先理清情绪和下一步。

本阶段保留：

- HTML 文件名；
- `/student.js` 加载路径；
- 登录、会话、归档和 Markdown 安全渲染逻辑；
- `ROLE_USER` 和 `ROLE_ADMIN`；
- 现有 DOM id，除非文案修改确实需要新增元素。

### 8.11 工具通知与报告文案

检查：

- `app/services/tools.py`
- `app/services/tool_governance.py`
- `app/services/report.py`
- `app/services/assessment.py`
- 邮件标题、Excel Sheet 标题和风险交接模板

建议显示名称：

```text
MindBridge Risk Ledger -> GovAgent Risk Ledger
MindBridge 高风险预警 -> GovAgent 高风险预警
学生 -> 用户
辅导员或管理员 -> 指定跟进人员或管理员
校园保卫 -> 现场安全人员或当地紧急服务
```

以下内部模型本阶段不改名：

- `PsychologicalReport`
- `PsychologicalAssessmentService`
- 数据库 `reports` 相关表和字段

原因是这些名称属于持久化和领域模型，贸然改名会扩大迁移范围，且“心理关怀”仍属于简历功能。

### 8.12 README

README 必须同步以下内容：

- 标题和项目简介；
- 四类业务能力；
- Agent 拓扑；
- Harness 名称；
- Agent 状态示例；
- Skill 列表；
- 前端页面说明；
- 风险工具和台账名称；
- 示例问题。

README 不得宣称以下尚未完成内容：

- 法律文档已经进入当前知识库；
- 已经新增 LEGAL Intent；
- 已经完成新的政务评测；
- 已经取得简历中的质量指标。

---

## 9. 文件级修改矩阵

| 文件或目录 | 必做修改 | 禁止修改 |
| --- | --- | --- |
| `app/core/product_identity.py` | 新增品牌、Agent 名称和兼容映射 | 不依赖业务 Service |
| `app/agents/autonomous.py` | 新 Agent 类名、Profile、Prompt、旧类兼容别名 | 不改 Artifact 契约 |
| `app/agents/event_driven_runtime.py` | 实例化新 Agent | 不改执行顺序 |
| `app/agents/coordinator.py` | 新 actor/owner 名称和采纳说明 | 不重写调度算法 |
| `app/agents/events.py` | Orchestrator 权限校验和默认创建者 | 不自动改写历史对象 |
| `app/agents/registry.py` | 如修改 Capability，增加规范化比较 | 不改变候选排序 |
| `app/agents/harness.py` | `GovAgentHarness` 主类和旧别名 | 不改事务和返回 DTO |
| `app/services/agent_models.py` | 新名称映射，入口规范化 | 不改 provider 配置 |
| `app/services/tool_registry.py` | 新 Agent 权限及旧名兼容 | 不扩大工具权限 |
| `app/services/skills.py` | GovAgent Skill 类型、Registry、Library 和匹配兼容 | 不引入 LLM Skill Router |
| `skills/*/SKILL.md` | 按迁移表重命名和重写 | 不硬编码法律结论 |
| `app/services/intent_prompts.py` | GovAgent 身份和政务示例，提升 Prompt 版本 | 不改 Schema |
| `app/services/intent_fusion.py` | 新语义模板和缓存版本 | 不改融合权重阈值 |
| `app/agents/routing.py` | 新规则关键词和 fallback 描述 | 不降低高风险检测 |
| `app/services/ai.py` | 品牌和用户称谓、Mock/Fallback 文本 | 不改流式协议 |
| `app/services/memory.py` | 新主题关键词 | 不改表结构 |
| `app/services/clarification_handlers.py` | 新澄清字段与旧状态兼容 | 不做数据库迁移 |
| `app/services/chat.py` | 导入和使用 `GovAgentHarness` | 不改消息幂等 |
| `app/services/turn_execution.py` | 新 Harness 和 Synthesis 模型名 | 不改 SSE 生成链路 |
| `app/api/routes.py` | 新状态名称和描述 | 不改 API 路径 |
| `app/static/*` | GovAgent 文案和快捷问题 | 不改 DOM/API 契约 |
| `README.md` | 新名称和真实能力说明 | 不写未完成指标 |
| `tests/*` | 新名称、兼容性和不变量断言 | 不删除失败测试掩盖问题 |

---

## 10. 测试与验收方案

本节属于代码改造必要回归，不是此前暂缓的“业务评测数据集重建”。

### 10.1 第一层 静态检查

运行：

```powershell
rg -n "MindBridge|CampusCove" app README.md skills
rg -n "CoordinatorAgent|ResponseAgent|GeneralChatAgent|AcademicPlanningAgent|CampusAffairsAgent|PsychologicalSupportAgent" app tests
rg -n "学生|辅导员|校园|学业|奖学金|补考|重修|宿舍|论文" app/static app/services app/agents skills README.md
rg -n "\\u[0-9a-fA-F]{4}" app tests skills README.md
```

检查规则：

- 旧产品名只能出现在兼容映射、兼容别名测试和迁移说明中；
- 旧 Agent 名只能出现在兼容映射、旧别名定义和明确的兼容测试中；
- 旧校园词可以存在于历史迁移、固定测试夹具或未纳入本阶段的知识数据中，但不得继续出现在新用户界面、Agent Prompt 和启用 Skill 中；
- 正常中文源码和文档不得出现 Unicode 转义。

### 10.2 第二层 定向单元测试

建议依次运行：

```powershell
python -m pytest tests/test_govagent_identity.py -q
python -m pytest tests/test_skills.py -q
python -m pytest tests/test_event_driven_multi_agent.py -q
python -m pytest tests/test_coordinator_work_items_v3.py -q
python -m pytest tests/test_refactor_phase0_contracts.py -q
python -m pytest tests/test_specialist_agents_v2.py -q
python -m pytest tests/test_mcp_tools_v2.py tests/test_tool_calling_v2.py -q
python -m pytest tests/test_trace_privacy.py -q
python -m pytest tests/test_ai_completion.py tests/test_chat_completion.py -q
python -m pytest tests/test_student_markdown_contract.py -q
```

### 10.3 第三层 路由回归

即使不重建完整业务评测集，也必须新增小型确定性契约测试，覆盖：

| 输入 | 预期内部 Intent | 预期 Agent |
| --- | --- | --- |
| 你好，介绍一下你能做什么 | CHAT | GeneralConsultationAgent |
| 帮我制定三个月的项目实施计划 | ACADEMIC | GovernmentPlanningAgent |
| 农用地转用由哪个层级审批 | CAMPUS | LegalInterpretationAgent |
| 最近工作压力很大而且睡不着 | MENTAL | PsychologicalCareAgent |
| 我现在想伤害自己 | RISK | Safety 高风险路径 |
| 先解释审批要求，再根据要求制定方案 | CAMPUS + ACADEMIC | LegalInterpretationAgent 后 GovernmentPlanningAgent |

该测试只验证路由语义和依赖，不报告准确率。

### 10.4 第四层 全量测试

定向测试通过后运行：

```powershell
python -m pytest -q
```

若全量测试依赖外部模型、Redis、MySQL 或 Ollama，应区分：

- 代码失败；
- 环境不可用；
- 真实模型测试未运行。

不得把环境未运行写成测试通过，也不得伪造业务指标。

### 10.5 API 验收

`GET /api/agent/status` 必须满足：

- 产品描述使用 GovAgent；
- `runtimeHarness.name == "GovAgentHarness"`；
- Agent 列表只包含八个新主名称；
- Skill 列表使用新名称；
- 没有重复展示旧兼容 Agent；
- Loop 和 collaboration 架构描述保持 event-driven、claim-based 和 append-only-blackboard。

### 10.6 Trace 验收

构造一轮普通复合请求和一轮高风险请求，检查新 Trace：

- `TURN_STARTED.actor == "OrchestratorAgent"`；
- Specialist Task 的 `claimedBy` 为新名称；
- SpecialistResult `agentName` 为新名称；
- `response_proposal.owner == "SynthesisAgent"`；
- Safety Review 关联正确的 responseArtifactId；
- `FINAL_ACCEPTED.actor == "OrchestratorAgent"`；
- Trace 不记录完整 Skill Prompt 和敏感原始内容；
- 高风险请求不会因为改名获得普通工具。

### 10.7 前端验收

至少人工检查：

- 首页；
- 咨询端；
- 政务管理端；
- Agent 状态区域；
- Skill 状态区域；
- 风险记录空状态；
- 会话归档弹窗；
- 快捷问题；
- 移动端窄屏。

确认没有出现 MindBridge、CampusCove、学生端、校园事务等旧主品牌文案，同时登录、聊天、Markdown 渲染、归档和管理功能仍可用。

---

## 11. 推荐提交顺序

不要把全部改动压成一个巨大提交。推荐拆成以下独立批次：

### 提交一 身份常量和兼容层

- 新增 `product_identity.py`；
- 新增身份映射测试；
- 暂不改 Runtime 行为。

验收：现有测试全绿。

### 提交二 Harness 与核心 Agent 改名

- `GovAgentHarness`；
- `OrchestratorAgent`；
- `SynthesisAgent`；
- Runtime、模型注册、工具权限、Trace 和 API 状态同步；
- 保留旧名称兼容别名。

验收：事件驱动、MCP、Trace、生成测试通过。

### 提交三 Specialist 与 Skill 改造

- 四个新 Specialist；
- Capability 策略；
- GovAgent Skill 类型与 Library；
- Skill 目录和内容迁移；
- Skill 匹配矩阵测试。

验收：所有启用 Skill READY 或可解释 WARN，无 FAILED。

### 提交四 Prompt 路由与界面语义

- Understanding Prompt；
- Intent Embedding 模板与缓存版本；
- Rule 路由；
- Specialist/Synthesis Prompt；
- 澄清与 Memory 关键词；
- 前端、工具通知和 README 文案。

验收：定向路由契约、API、Trace、前端和全量测试通过。

---

## 12. 风险与防护

### 12.1 Agent 名称既是显示值也是协议值

风险：名称参与 Registry、Task owner、Artifact owner、Tool 权限、模型 profile 和 Trace。只改类名会导致权限丢失或任务无人认领。

防护：统一使用 `canonical_agent_name()`，并分别测试注册、任务认领、工具权限和 Trace。

### 12.2 Skill 名称存在直接引用

风险：`high_risk_safety_plan` 和交接摘要通过固定名称读取；只改目录会在运行时抛出 required skill not found。

防护：先更新 Library 方法和校验规则，再原子迁移目录；不得让新旧 Skill 同时启用。

### 12.3 Intent 枚举与业务含义暂时不一致

风险：内部仍是 ACADEMIC/CAMPUS，但展示已是政务规划/法律解读，后续开发者可能误用。

防护：在 `IntentType` 附近增加明确注释，在统一映射中定义业务标签，并在 README 记录这是第一阶段兼容策略。未来知识领域和评测改造时再单独决定是否迁移枚举。

### 12.4 Prompt 与 Rule 不一致

风险：只改 Understanding Prompt，不改 Embedding 模板和 Rule，会导致三路融合互相冲突。

防护：Prompt、Embedding Template、Rule Keyword 必须在同一提交中更新，并提升版本常量。

### 12.5 法律知识尚未接入

风险：Agent 改名为 LegalInterpretationAgent 后，模型可能凭自身知识回答具体法律问题。

防护：Skill 和 Prompt 明确要求本地证据；无证据时 PARTIAL、FAILED 或说明暂无法核验。本阶段只完成身份和行为边界，不宣称法律知识已就绪。

### 12.6 高风险安全能力被业务泛化削弱

风险：把校园求助词全部删除时，可能误删高风险固定回复和现实支持建议。

防护：保留 Safety 硬信号、风险报告、Safety Review、工具隔离和紧急求助逻辑，只把机构名称泛化。

### 12.7 前端文件名与产品语义不一致

风险：`student.html` 文件名仍存在，静态扫描会认为改造不完整。

防护：本阶段明确只改用户可见文案和页面标题，文件名属于兼容边界。不要为了视觉一致性破坏 URL。

---

## 13. 回滚方案

由于本阶段不修改数据库 Schema、知识库和评测数据，回滚应以提交为单位：

1. Prompt 或 Rule 出现语义回归：只回滚提交四，保留新 Agent 兼容层。
2. Skill 匹配异常：回滚提交三，恢复旧 Skill 目录和 Library 固定引用。
3. Agent 权限或任务认领异常：回滚提交二，新身份模块可以保留但不接入 Runtime。
4. 身份模块本身异常：回滚提交一。

禁止使用 `git reset --hard` 或覆盖用户未提交修改。回滚前必须再次检查 Git 状态，并只回退本次改造产生的提交或文件块。

---

## 14. 最终完成定义

只有同时满足以下条件，才能把前四项标记为完成：

1. 用户可见品牌统一为 GovAgent。
2. 代码中真实存在并运行 `GovAgentHarness`、`OrchestratorAgent` 和 `SynthesisAgent`。
3. Runtime 实例化四个新 Specialist Agent。
4. 新 Task、Artifact、Event、Trace 使用新 Agent 名称。
5. 旧名称仍能通过兼容层导入和解析，但不会作为独立 Agent 重复注册。
6. 普通工具权限和风险工具隔离没有变化。
7. 启用 Skill 已完成政务、自然资源和心理关怀语义迁移。
8. 新 Skill 的目录、Front Matter、固定模板引用和匹配测试一致。
9. Understanding Prompt、Embedding 模板和 Rule 路由采用一致的新业务语义。
10. 前端、API 状态、风险通知和 README 不再展示旧主品牌及校园主业务文案。
11. 法律知识未接入时，系统不会无证据生成具体法律结论。
12. 定向测试和全量可运行测试通过；环境未运行项被如实标记。
13. 未修改法律知识库、评测数据集、数据库迁移和简历指标。
14. 改动文件通过 UTF-8 和 Unicode 转义检查。

完成后，项目可以真实、统一地使用以下简历口径：

```text
GovAgent 政务多智能体协同平台通过 GovAgentHarness 组织单轮执行，
由 UnderstandingAgent 进行语义规划，OrchestratorAgent 创建和仲裁任务，
GeneralConsultationAgent、GovernmentPlanningAgent、LegalInterpretationAgent
和 PsychologicalCareAgent 分别处理功能工作项，SynthesisAgent 汇总候选回复，
SafetyAgent 复审后由 OrchestratorAgent 最终采纳。
```

此时仍不得声称法律知识库接入和新业务评测已经完成；这两部分属于后续独立阶段。
