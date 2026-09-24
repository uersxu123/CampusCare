# MindBridge 通用追问识别、状态续接与参数解析优化实施指南

版本：2026-08-01  
适用项目：`mindbridge-py`  
目标读者：负责直接修改本仓库代码、测试、迁移和运行验证的编码 AI

## 1. 文档定位

本文件是一份可执行的代码改造指导，不是概念提案。编码 AI 必须先复核当前工作区和实际代码，再按本文件的阶段顺序实施、测试和汇报。

本次改造要解决两类问题：

1. 学习计划请求中的字段名称被错误当成字段值，导致系统漏问课程和截止时间。
2. 非学习计划问题虽然可能被 KnowledgeAgent 判断为需要澄清，但追问没有统一持久化、下一轮没有可靠续接。

本次目标不是让模型对所有模糊问题无限追问，而是建立一个受控、可验证、可扩展的通用追问框架，并首批支持：

- 学习计划参数追问。
- 校园知识查询的范围追问。
- 澄清期间的取消、过期、自然换题和高风险抢占。

## 2. 已确认的故障基线

### 2.1 可稳定复现的输入

```text
帮我根据这学期的课程和截止时间制定一份学习计划。
```

当前错误输出：

```text
请告诉我可用时间、当前难点。
```

正确输出至少应包含：

```text
请告诉我课程、截止时间、可用时间和当前难点。
```

### 2.2 当前错误解析结果

当前 `app/services/clarifications.py` 把上述输入解析为：

```json
{
  "course": "和截止时间制定一份学习计划",
  "deadline": "制定一份学习计划",
  "objective": "制定学习计划"
}
```

因此系统错误地认为 `course` 和 `deadline` 已经存在，只追问剩余两个字段。

### 2.3 根因

当前标签提取正则把赋值分隔符设为可选：

```python
"course": r"(?:课程|科目)[:：]?\s*([^，,；;。]+)"
```

只要句子中出现“课程”两个字，后续普通语句就可能被当作字段值。“截止时间”“可用时间”“当前难点”等字段存在相同风险。

### 2.4 当前架构缺口

- `STUDY_PLAN_FIELDS`、字段白名单、解析、校验和恢复输入全部写死在一个模块中。
- `ClarificationService.create()` 把 `task_kind` 写死为 `study_plan`。
- `KnowledgeAgent` 的 `CLARIFY` 只进入 ResponseAgent 的提示词，不会稳定转换成持久化的 `clarification_request`。
- 活跃追问会在普通路由之前接管下一条消息；只要用户没有明确说“取消”，新话题可能被误当作参数值。
- 现有测试覆盖了结构化正向输入，没有覆盖“字段名称出现在请求描述中”的负向样例。

当前全量基线为：

```text
167 passed
```

编码 AI 开始修改前必须重新运行并记录自己的真实基线。

## 3. 改造目标

完成后系统必须具备以下行为：

1. 只有明确赋值表达才会被识别为参数，字段名称本身不是字段值。
2. 每种追问任务由独立处理器负责解析、校验、问题生成和恢复输入。
3. 所有可跨轮续接的追问都使用统一 `clarification_request` 契约和 MySQL 状态。
4. KnowledgeAgent 需要用户补充范围时，追问能够保存，并在下一轮恢复原问题。
5. 用户提出明显新问题时，旧追问被中断，新问题继续正常路由。
6. 当前输入出现高风险信号时，风险链路永远优先，不能被普通追问拦截。
7. 追问只索取用户能够提供、且确实决定回答范围的信息。
8. 缺少本地证据不等于缺少用户参数，不能把知识库缺口转嫁给用户。
9. Trace 只记录任务类型、状态和字段名，不记录敏感字段值。
10. 不破坏现有 CHAT、CONSULT、RISK、RAG、会话、SSE 和工具队列行为。

## 4. 明确不做

本次不做以下工作：

- 不新增 PostgreSQL、消息队列或外部工作流系统。
- 不用 Chroma 或 Redis 代替 MySQL 保存追问事实状态。
- 不让模型自由定义数据库字段、状态或任务类型。
- 不把所有普通对话都强制转成结构化表单。
- 不为普通情绪支持增加不必要的审问式追问。
- 不通过追问用户来弥补学校官方资料缺失。
- 不重写整个 Agent runtime、ContextBuilder 或 SSE 生成链路。
- 不升级无关依赖，不进行全仓库格式化。

## 5. 不可破坏的系统不变量

### 5.1 安全优先

处理顺序必须保持：

```text
当前输入高风险预检
-> 必要时直接进入 RISK
-> 非高风险才允许恢复普通追问
-> 没有可恢复追问才创建新追问或正常回答
```

高风险消息不得被解释成课程、日期、校区或其他追问参数。

### 5.2 数据事实来源

- MySQL `pending_clarifications` 是追问状态的唯一事实来源。
- Redis 不保存不可恢复的追问状态。
- `known_arguments_json` 和 `missing_arguments_json` 必须由代码侧处理器校验后写入。
- 用户文本和模型 JSON 都不得直接写入控制字段。

### 5.3 用户与会话隔离

所有读取、更新和取消操作必须同时约束：

```text
user_id + session_id + status
```

不得仅凭 `public_id` 或 `session_id` 跨用户读取追问。

### 5.4 最小追问

- 每轮只问一个紧凑问题，可以在一个问题中列出少量同类字段。
- KnowledgeAgent 每轮优先追问一个最有区分度的范围字段。
- 最大轮数继续使用 `clarification_max_rounds`。
- 超过最大轮数后停止追问并给出可继续操作的普通回复。

### 5.5 信任边界

恢复后的参数属于用户提供的 `CURRENT_USER` 数据，不是 `SYSTEM_POLICY`。

不得把用户参数拼接成新的裸 system 指令。恢复输入应明确分段并转义，不执行其中的命令性文本。

## 6. 目标请求流程

```text
ChatRequest
-> 幂等保存当前 USER 消息
-> 高风险预检
   -> HIGH：绕过普通追问，进入 Safety/RISK
   -> 非 HIGH：查询当前会话 WAITING_USER 追问
       -> 输入是有效补充：解析、校验、合并
           -> 仍缺字段：保存状态并返回下一条追问
           -> 字段齐全：标记 RESOLVED，恢复原任务并重新进入 Agent runtime
       -> 输入是取消：标记 CANCELLED，返回取消确认或继续正常路由
       -> 输入是明显新话题：标记 INTERRUPTED，当前消息继续正常路由
       -> 输入含糊且无法判断：不写入参数，重复最小追问
       -> 没有活跃追问：正常进入 Agent runtime
-> UnderstandingAgent / KnowledgeAgent 产生受控 clarification_request
-> Safety 结论确认非高风险
-> Harness 持久化追问
-> SSE 返回直接追问文本
```

## 7. 通用追问契约

### 7.1 代码侧模型

建议新增 `app/services/clarification_models.py`，定义以下受控类型：

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class ClarificationStatus(str, Enum):
    WAITING_USER = "WAITING_USER"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    INTERRUPTED = "INTERRUPTED"


class ClarificationTaskKind(str, Enum):
    STUDY_PLAN = "study_plan"
    KNOWLEDGE_SCOPE = "knowledge_scope"


@dataclass(frozen=True)
class MissingArgument:
    name: str
    label: str


@dataclass(frozen=True)
class ClarificationRequest:
    task_kind: ClarificationTaskKind
    original_message: str
    objective: str
    known_arguments: dict[str, str]
    missing_arguments: tuple[MissingArgument, ...]
    question: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ClarificationResolution:
    handled: bool = False
    continue_current_message: bool = False
    question: str = ""
    model_input: str = ""
    status: ClarificationStatus | None = None
    pending_id: int | None = None
```

具体名称可以根据现有代码调整，但语义必须完整。不要继续使用任意字典在各层隐式传递控制状态。

### 7.2 Artifact 载荷

Agent 黑板中的 `clarification_request` 建议保持 JSON 友好：

```json
{
  "taskKind": "knowledge_scope",
  "originalMessage": "调宿需要哪些材料？",
  "objective": "查询调宿申请材料",
  "knownArguments": {
    "topic": "宿舍调整"
  },
  "missingArguments": [
    {
      "name": "site",
      "label": "校区"
    }
  ],
  "question": "你想查询南望山校区还是未来城校区的调宿规定？",
  "metadata": {
    "source": "knowledge_planner",
    "questionId": "q1"
  }
}
```

写库前必须再次通过任务处理器验证。Artifact 合法不代表可以绕过持久化层校验。

## 8. 处理器注册机制

建议新增 `app/services/clarification_handlers.py`，定义小型协议：

```python
class ClarificationHandler(Protocol):
    task_kind: ClarificationTaskKind

    def sanitize_known(self, values: dict[str, object]) -> dict[str, str]: ...

    def sanitize_missing(self, values: object) -> list[MissingArgument]: ...

    def parse_initial(self, text: str) -> ClarificationRequest | None: ...

    def parse_answer(
        self,
        text: str,
        expected_fields: list[str],
    ) -> dict[str, str]: ...

    def is_probable_answer(
        self,
        text: str,
        expected_fields: list[str],
    ) -> bool: ...

    def validate_combined(
        self,
        values: dict[str, str],
    ) -> tuple[dict[str, str], list[MissingArgument], str]: ...

    def build_question(self, missing: list[MissingArgument]) -> str: ...

    def build_model_input(
        self,
        original_message: str,
        values: dict[str, str],
    ) -> str: ...
```

注册表只能包含代码侧允许的处理器：

```python
HANDLERS = {
    ClarificationTaskKind.STUDY_PLAN.value: StudyPlanClarificationHandler(),
    ClarificationTaskKind.KNOWLEDGE_SCOPE.value: KnowledgeScopeClarificationHandler(),
}
```

如果数据库或模型返回未知 `task_kind`：

1. 不尝试动态导入处理器。
2. 将该记录标记为 `CANCELLED` 或 `INTERRUPTED`。
3. 当前消息继续正常路由。
4. Trace 记录 `UNKNOWN_TASK_KIND`，不记录原文。

## 9. 学习计划处理器

### 9.1 字段定义

保留首版四个必需字段：

```text
course
deadline
availableTimeWindows
focusProblem
```

`objective` 是系统生成的任务说明，不应作为用户必填字段。

### 9.2 初始触发

初始触发仍可使用受控词：

```text
学习计划、复习计划、学习规划、复习规划
```

不得仅因为一条普通消息由四个逗号片段组成就自动判为学习计划，除非结构和字段校验同时成立。

### 9.3 明确赋值语法

标签提取只能接受明确赋值：

```text
课程：高等数学
课程是高等数学
课程为高等数学
截止日期=9月20日
```

建议正则：

```python
LABELED_PATTERNS = {
    "course": r"(?:课程|科目)\s*(?:[:：=]|是|为)\s*([^，,；;。]+)",
    "deadline": r"(?:截止时间|截止日期|截止)\s*(?:[:：=]|是|为)\s*([^，,；;。]+)",
    "availableTimeWindows": r"(?:可用时间|空闲时间)\s*(?:[:：=]|是|为)\s*([^，,；;。]+)",
    "focusProblem": r"(?:当前难点|主要难点|难点)\s*(?:[:：=]|是|为)\s*([^，,；;。]+)",
}
```

以下内容必须返回空值：

```text
根据课程和截止时间制定计划
课程是什么？
请告诉我截止时间
当前难点需要之后再说
```

### 9.4 值校验

课程值必须：

- 非空且长度受限。
- 不是问句。
- 不以“和截止时间”“是什么”“有哪些”“制定学习计划”等元语言开头。
- 允许多个课程，例如“高数、大学物理、数据结构”。

截止时间必须至少包含一种可识别时间特征：

- `YYYY-MM-DD`、`YYYY/MM/DD`。
- `M月D日`。
- 今天、明天、后天。
- 本周、下周、周一至周日。
- 明确的考试周或用户认可的相对期限。

不要仅检查字符串非空。

现有 `_parse_point()` 只识别少量相对日期和小时，不能承担完整日期校验。编码 AI 应补齐年、月、日和星期解析，或将“是否像截止时间”和“是否能比较先后”拆成两个函数：

```text
looks_like_deadline(value) -> bool
parse_comparable_datetime(value, now) -> datetime | None
```

无法精确解析但语义合理时可以保留原文，但不得进行虚假的先后比较。

### 9.5 无标签的顺序输入

只有满足以下条件才允许按顺序映射：

- 当前确实存在活跃的 `study_plan` 追问。
- 分段数量与当前缺失字段数量完全一致；或只剩一个字段。
- 每个分段通过目标字段校验。

禁止继续使用：

```python
if expected and len(parts) <= len(expected):
```

因为它会把任意单句塞入第一个缺失字段。

### 9.6 正确恢复输入

字段完整后，生成稳定输入：

```text
原始任务：帮我制定学习计划。
已确认参数：
- 课程：高等数学、大学物理
- 截止时间：9月20日
- 可用时间：工作日晚上19:00到21:00
- 当前难点：高数积分和物理力学
请依据上述用户确认信息制定计划，不得修改时间，不得补造课程。
```

恢复输入必须经过隐私清洗，并作为当前用户任务传给 Agent runtime。

## 10. KnowledgeAgent 范围追问

### 10.1 追问与证据不足的边界

KnowledgeAgent 只有在缺少“由用户决定的范围”时才能 `CLARIFY`，例如：

- 校区：南望山还是未来城。
- 学年或学期：用户查询哪个时间范围。
- 具体制度或业务事项：调宿、退宿还是住宿费。
- 被省略的指代对象：用户说“那个规定”，上下文无法恢复。

以下情况不能追问用户来代替证据：

- 本地知识库没有今年奖学金截止日期。
- 官方材料没有联系电话。
- 两份制度文本相互冲突。
- 向量库或 embedding 服务故障。

这些情况应返回 `PARTIAL`、`NONE` 或现有降级状态，并明确无法核验。

### 10.2 结构化 Planner 契约

修改 `app/services/knowledge_agent/models.py`，把自由文本 `clarification_question` 升级成受控结构：

```python
class KnowledgeClarificationField(str, Enum):
    SITE = "site"
    ACADEMIC_PERIOD = "academicPeriod"
    POLICY_NAME = "policyName"
    SERVICE_ITEM = "serviceItem"
    REFERENT = "referent"


class PlannedClarification(StrictModel):
    field: KnowledgeClarificationField
    label: str = Field(min_length=1, max_length=30)
    question: str = Field(min_length=1, max_length=200)
    allowed_values: list[str] = Field(default_factory=list, max_length=8)
    reason_code: str = Field(min_length=1, max_length=64)
```

`PlannedQuestion` 使用：

```python
clarification: PlannedClarification | None = None
```

契约要求：

- `RETRIEVE` 和 `SKIP` 时 `clarification` 必须为空。
- `CLARIFY` 时 `clarification` 必须存在，`query_variants` 必须为空。
- `allowed_values` 只能来自代码侧 taxonomy、当前上下文或本地证据中的已知候选，不能由模型编造。
- `field` 必须是固定枚举。

完成切换后删除旧 `clarification_question` 字段，不长期维护双契约。同步更新 Planner、Policy、Orchestrator、Trace、评测和所有 fixture。

### 10.3 Policy 二次校验

`KnowledgePolicy` 必须拒绝：

- 未知字段。
- 问题包含用户没有提及且本地上下文没有支持的年份、校区、制度名。
- 追问要求用户提供官方事实答案。
- `allowed_values` 超出受控候选。
- 一次要求多个无关范围字段。

如果模型给出非法 `CLARIFY`，不能直接写库。应按现有结构化修复预算修复一次；仍失败则降级为 `NONE` 或 `FAILED_CLOSED`。

### 10.4 从 Knowledge artifact 转换为追问

修改 `KnowledgeAgent.act()`：

1. 正常发布 `knowledge_evidence`。
2. 当 `overall_grade == CLARIFY` 时，选择一个优先级最高、合法的 `PlannedClarification`。
3. 同时发布一个 `clarification_request` artifact。
4. `taskKind` 固定为 `knowledge_scope`。
5. `knownArguments` 至少保存原问题、领域、规范概念和已确认范围；所有键经过 allowlist。
6. `missingArguments` 只包含当前要追问的一个字段。

不要让 ResponseAgent 重新改写已经通过 Policy 审核的问题，否则可能改变范围或一次追问多个事项。

### 10.5 下一轮恢复

用户补充后生成：

```text
原始问题：调宿需要哪些材料？
已确认范围：
- 校区：未来城校区
请依据原始问题和已确认范围重新执行路由及本地知识检索。
```

恢复后必须重新运行 KnowledgeAgent，不能直接把旧的 `PARTIAL/CLARIFY` evidence 当作最终证据。

如果仍缺另一个必要范围，可以创建下一轮追问，但累计不得超过最大轮数。

## 11. ClarificationService 改造

### 11.1 保留的职责

`app/services/clarifications.py` 只负责：

- 根据用户、会话读取活跃记录。
- 调用注册处理器验证请求和回答。
- 创建、更新、完成、过期、中断记录。
- 控制最大轮数和版本。
- 返回统一 `ClarificationResolution`。

字段解析和任务专属规则移入 handler，避免服务再次膨胀成大杂烩。

### 11.2 create()

`create()` 必须：

1. 根据 `task_kind` 查找已注册处理器。
2. 重新清洗 `known_arguments` 和 `missing_arguments`。
3. 拒绝控制字段：`requestId`、`sessionId`、`userId`、`status`、`version`、`publicId`。
4. 如果存在活跃记录，将旧记录标记为 `INTERRUPTED`，而不是无解释覆盖。
5. 从请求读取真实 `task_kind`，不得写死 `study_plan`。
6. 问题文本必须由处理器或已通过 Policy 的知识澄清生成。

### 11.3 resume_or_bypass()

建议用一个明确入口取代 Harness 中分散判断：

```python
resolution = clarification_service.resume_or_bypass(
    user=user,
    session=session,
    text=original_input,
    high_risk=current_high_risk,
)
```

返回语义：

- `handled=True, question!=empty`：本轮只返回下一条追问。
- `handled=False, model_input!=empty`：澄清完成，用恢复输入继续 Agent runtime。
- `continue_current_message=True`：旧追问已中断，当前消息按新问题处理。
- 高风险：不修改成普通参数，旧追问可以标记 `INTERRUPTED`，当前消息进入 RISK。

### 11.4 自然换题

不能只依赖“换个问题”关键词。处理器先判断当前文本是否像有效答案：

- 有目标字段标签。
- 值符合目标字段类型。
- 分段数量和缺失字段数量匹配。
- 单字段追问下，短回答符合候选或格式。

若不符合，并且文本具有完整新请求特征，例如明显疑问句、另一个领域关键词或新的动作目标，则：

```text
旧记录 -> INTERRUPTED
当前消息 -> 正常路由
```

如果既不像答案也不像新问题，不要写入脏值；重复当前最小追问，并允许用户取消。

### 11.5 并发与幂等

- 同一会话同时到达两条消息时，不能让同一追问解析两次。
- 使用现有 `version` 做乐观并发控制，或在 MySQL 事务中对活跃记录加行锁。
- 更新条件至少包含 `id + version + status=WAITING_USER`。
- 冲突时重新读取状态；已完成记录不得再次完成。
- 同一 `requestId` 重放不得创建第二条追问。

## 12. Agent runtime 与 Harness 接入

### 12.1 UnderstandingAgent

`UnderstandingAgent` 继续负责识别学习计划初始请求，但调用改为：

```python
study_handler.parse_initial(board.user_input)
```

它不直接拼任意字典，也不负责写数据库。

### 12.2 KnowledgeAgent

KnowledgeAgent 根据已校验的 `CLARIFY` 结果发布统一 artifact。不得只把澄清问题塞进 `_knowledge_evidence_message()` 交给 ResponseAgent 自由生成。

### 12.3 Coordinator

当以下条件同时满足时，可以结束普通回答生成，减少无意义模型调用：

- 已存在合法 `clarification_request`。
- 当前路由不是 RISK。
- SafetyAgent 已明确完成本轮风险判断，且没有 `SAFETY_OVERRIDE`。

Coordinator 将追问视为受控直接响应，不要求 ResponseAgent 再生成同义句。

不得在 SafetyAgent 完成前提前返回普通追问。

### 12.4 Harness

Harness 负责：

1. 高风险预检。
2. 调用 `resume_or_bypass()`。
3. 对已解决追问注入标准化 `model_input`。
4. 对新 artifact 调用 `ClarificationService.create()`。
5. 将审核后的 `question` 作为 `direct_response`。
6. 保存 Trace。

Harness 不应知道每个任务的字段列表。

### 12.5 EventDrivenRuntime

`AgentRunResult.clarification_request` 可以保留，但应改为统一模型或经过统一序列化的字典。

`_to_result()` 只能转发黑板上最终合法的 `clarification_request`，不能从普通 Response 文本猜测追问状态。

## 13. 数据库与旧数据

### 13.1 是否需要迁移

当前 `pending_clarifications` 已包含：

- `task_kind`
- `original_message`
- `known_arguments_json`
- `missing_arguments_json`
- `approved_question`
- `round_count / max_rounds`
- `expires_at`
- `version`

并且状态是字符串，因此首选复用现有表，不新增迁移。

如果编码 AI 发现实际 schema 与模型不一致，必须先用 Alembic 和数据库 `DESCRIBE` 核验，再决定是否迁移；不得仅根据文档猜测。

### 13.2 旧脏数据兼容

读取旧记录时必须重新通过 handler 清洗 `known_arguments_json`。以下伪值应被删除：

```text
和截止时间制定一份学习计划
制定一份学习计划
是什么？
有哪些？
```

部署时建议执行一次受控修复：

```sql
UPDATE pending_clarifications
SET status = 'CANCELLED'
WHERE task_kind = 'study_plan'
  AND status = 'WAITING_USER';
```

生产环境执行前必须先查询命中数量、备份或确认可恢复性。不能在应用启动时无条件批量取消所有记录。

### 13.3 状态语义

- `WAITING_USER`：等待用户补充。
- `RESOLVED`：字段完整且已恢复任务。
- `CANCELLED`：用户明确取消或管理员受控取消。
- `EXPIRED`：超过 TTL。
- `INTERRUPTED`：用户换题、高风险抢占或新追问替代旧追问。

## 14. Trace 与日志

修改 `app/services/trace.py`，追问 Trace 只保存：

```json
{
  "taskKind": "knowledge_scope",
  "status": "WAITING_USER",
  "missingArguments": ["site"],
  "roundCount": 1,
  "source": "knowledge_planner",
  "reasonCode": "MISSING_SITE_SCOPE"
}
```

不得保存：

- 完整课程列表。
- 用户困难详情。
- 完整原始问题副本。
- 模型 Prompt。
- 敏感心理内容。

日志建议记录：

```text
clarification_created
clarification_resolved
clarification_reasked
clarification_cancelled
clarification_interrupted
clarification_expired
clarification_rejected_invalid_artifact
```

每条日志只带内部 ID、task kind、字段名、状态、轮数和原因码。

## 15. 文件级改造清单

### 新增

- `app/services/clarification_models.py`
  - 通用枚举、请求和结果模型。
- `app/services/clarification_handlers.py`
  - Handler 协议、注册表、StudyPlan 和 KnowledgeScope 实现。

### 修改

- `app/services/clarifications.py`
  - 收口为通用状态服务，移除学习计划硬编码。
- `app/services/knowledge_agent/models.py`
  - 增加结构化 `PlannedClarification`。
- `app/services/knowledge_agent/prompts.py`
  - 明确何时允许 CLARIFY，何时必须 NONE/PARTIAL。
- `app/services/knowledge_agent/policy.py`
  - 校验字段、候选值、问题和事实边界。
- `app/services/knowledge_agent/orchestrator.py`
  - 在最终 artifact 中保留合法的结构化澄清。
- `app/agents/autonomous.py`
  - UnderstandingAgent 使用 handler；KnowledgeAgent 发布通用 artifact；ResponseAgent 不改写受控问题。
- `app/agents/coordinator.py`
  - Safety 完成后允许受控追问直接收敛。
- `app/agents/event_driven_runtime.py`
  - 传递统一追问结果。
- `app/agents/harness.py`
  - 通用恢复、换题、高风险抢占和持久化。
- `app/services/trace.py`
  - 新增状态和 reason code，继续保护隐私。
- `tests/test_clarifications.py`
  - 参数解析、状态、换题和旧数据回归。
- `tests/test_event_driven_multi_agent.py`
  - Artifact 和 Coordinator 收敛。
- `tests/test_knowledge_planner.py`
  - 结构化 CLARIFY schema。
- `tests/test_knowledge_policy.py`
  - 允许和拒绝矩阵。
- `tests/test_knowledge_orchestrator.py`
  - CLARIFY 结果传递。
- `tests/test_routing_integration.py`
  - 多轮端到端续接。
- `app/harness/runner.py`
  - 增加通用追问 Harness 场景。

### 原则上不修改

- `app/services/chat.py` 的 SSE 协议。
- MySQL、Redis 和 Chroma 连接配置。
- 风险词典和 SafetyAgent 核心策略。
- 学生端 Markdown 渲染。
- 工具队列和报告逻辑。

## 16. 必须先写的回归测试

编码 AI 必须先让以下测试在旧代码上失败，再实现修复。

### 16.1 学习计划负向解析

```python
def test_field_names_in_request_are_not_values(self):
    payload = build_study_plan_clarification(
        "帮我根据这学期的课程和截止时间制定一份学习计划。",
        settings(),
    )

    self.assertEqual(payload["knownArguments"], {"objective": "制定学习计划"})
    self.assertEqual(
        [item["name"] for item in payload["missingArguments"]],
        ["course", "deadline", "availableTimeWindows", "focusProblem"],
    )
```

还必须覆盖：

```text
课程是什么？                  -> 不提取 course
请告诉我截止时间              -> 不提取 deadline
课程：高数                    -> 提取 course=高数
课程是高数                    -> 提取 course=高数
截止时间：9月20日             -> 提取 deadline=9月20日
截止时间：制定一份学习计划     -> 拒绝 deadline
```

### 16.2 顺序回答

- 缺四个字段、回答四段且全部合法：成功解析。
- 缺四个字段、只回答一句普通问题：不写入 course。
- 只缺 `focusProblem`、用户回答“积分题”：成功解析。
- 两个分段对应三个缺失字段：不按顺序乱填。
- 带标签回答优先于顺序映射。

### 16.3 时间校验

- 过去日期要求重新确认。
- 可用时间晚于截止时间要求重新确认。
- 无法精确比较的自然语言期限不伪造 datetime。
- `9月20日`、`2026-09-20`、`下周五` 能被识别为期限表达。
- 普通动词短语不能成为期限。

### 16.4 状态测试

- 创建后为 `WAITING_USER`。
- 字段齐全后为 `RESOLVED`。
- 用户说“取消”后为 `CANCELLED`。
- TTL 到期后为 `EXPIRED`。
- 明显换题后为 `INTERRUPTED`，当前消息继续路由。
- 新追问替换旧追问时旧记录为 `INTERRUPTED`。
- 未知 task kind 不接管消息。
- 并发重复回答只完成一次。

### 16.5 KnowledgeAgent 测试

允许的追问：

```text
“调宿规定是什么？”且必须区分校区
-> field=site
-> 只问校区
```

不允许的追问：

```text
“今年奖学金截止日期是什么？”但本地证据没有日期
-> 不得问用户“截止日期是什么”
-> 应为 NONE/PARTIAL 并说明无法核验
```

还必须覆盖：

- Planner 返回未知 clarification field 被 Policy 拒绝。
- Planner 编造校区候选被拒绝。
- CLARIFY 不携带查询。
- RETRIEVE 不携带 clarification。
- 多个缺失范围时只选择一个最高优先级问题。
- 用户回答范围后重新执行检索。

### 16.6 高风险与换题

- 等待课程字段时用户输入高风险内容，立即进入 RISK。
- 高风险文本不能写入 `known_arguments_json`。
- 等待校区时用户问“最近总失眠怎么办”，旧追问中断并进入心理支持路由。
- 等待截止日期时用户说“换个问题”，旧追问取消或中断。

### 16.7 隐私和隔离

- 用户 A 不能读取或恢复用户 B 的追问。
- 同一用户不同会话互不接管。
- Trace 不包含完整参数值。
- 控制字段无法由用户文本或模型 payload 注入。

## 17. 分阶段实施顺序

### 阶段 0：基线和工作区保护

1. 运行 `git status --short`。
2. 记录用户已有修改，不覆盖、不回滚。
3. 运行全量测试并记录真实结果。
4. 用当前原句复现错误解析和数据库记录。
5. 检查 Alembic head 和 `pending_clarifications` 实际表结构。

完成条件：编码 AI 能准确说明当前错误链路，不能只引用本文件。

### 阶段 1：测试先行

1. 添加第 16 节的核心失败测试。
2. 确认失败原因与本次目标一致。
3. 不先改断言来适配旧错误行为。

完成条件：至少有原句回归、顺序误填、换题和知识范围追问测试在旧实现上失败。

### 阶段 2：通用模型与 Handler

1. 新增通用模型。
2. 新增 Handler 协议和注册表。
3. 把原学习计划逻辑移动到 `StudyPlanClarificationHandler`。
4. 修复明确赋值、字段校验和时间校验。
5. 保持现有外部行为兼容，必要时保留薄包装函数供旧测试逐步迁移。

完成条件：学习计划单元测试全部通过，原句正确追问四项。

### 阶段 3：通用状态服务

1. 移除 `task_kind="study_plan"` 硬编码。
2. create/resume/cancel/expire/interrupted 全部调用注册处理器。
3. 增加旧脏数据再校验。
4. 增加并发保护。
5. 实现自然换题。

完成条件：状态、隔离、并发和换题测试通过。

### 阶段 4：KnowledgeAgent 接入

1. 修改 Planner schema 和 Prompt。
2. 修改 Policy 校验。
3. 修改 Orchestrator artifact。
4. KnowledgeAgent 发布统一 `clarification_request`。
5. 新增 `KnowledgeScopeClarificationHandler`。
6. 用户回答后重新执行原始知识请求。

完成条件：校园范围追问可跨轮续接；证据不足不会错误追问用户。

### 阶段 5：Coordinator 与 Harness 收敛

1. 保持 Safety 优先。
2. 对合法追问使用受控直接回复。
3. 避免 ResponseAgent 重写追问或进行无意义生成。
4. Trace 记录通用状态。

完成条件：一次追问只生成一个学生可见问题，不调用不必要的最终回答模型。

### 阶段 6：数据与真实服务验证

1. 查询现有 `WAITING_USER` 数据数量。
2. 在确认范围后取消开发环境旧脏记录。
3. 重建 Docker 应用。
4. 验证健康接口、登录、普通对话、学习计划、知识范围追问、换题和高风险抢占。
5. 运行全量测试和 Harness。

## 18. 测试与验证命令

编码 AI 应根据实际环境调整，但至少执行：

```powershell
python -m pytest tests/test_clarifications.py -q
python -m pytest tests/test_knowledge_planner.py -q
python -m pytest tests/test_knowledge_policy.py -q
python -m pytest tests/test_knowledge_orchestrator.py -q
python -m pytest tests/test_event_driven_multi_agent.py -q
python -m pytest tests/test_routing_integration.py -q
python -m pytest -q
python -m app.harness.runner --suite all
```

Docker 验证：

```powershell
docker compose up -d --build
docker compose ps
docker compose logs --tail 200 app
curl.exe -sS http://127.0.0.1:8080/actuator/health
```

数据库检查：

```sql
SELECT id, user_id, session_id, task_kind, status,
       known_arguments_json, missing_arguments_json,
       round_count, max_rounds
FROM pending_clarifications
ORDER BY id DESC
LIMIT 20;
```

编码检查：

```powershell
rg -n "\\u[0-9a-fA-F]{4}" app tests migrations
```

普通中文字符串、注释和 UI 文案不得被改成 Unicode 转义。

## 19. 真实验收场景

### 场景 A：本次故障原句

```text
用户：帮我根据这学期的课程和截止时间制定一份学习计划。
```

期望：

- `knownArguments` 不包含伪造的 course/deadline。
- 追问课程、截止时间、可用时间和当前难点。
- 数据库只保存合法字段。

### 场景 B：一次补齐

```text
用户：课程：高数和大学物理；截止时间：9月20日；可用时间：工作日晚上7点到9点；当前难点：积分和力学。
```

期望：

- 四个字段正确映射。
- 状态变为 `RESOLVED`。
- 系统依据原任务和确认参数生成学习计划。
- 不修改用户给出的时间。

### 场景 C：分轮补齐

```text
Assistant：请告诉我课程、截止时间、可用时间和当前难点。
用户：高数，9月20日
```

期望：

- 系统不得把两个值错误映射到四个字段。
- 应要求用户使用标签，或只追问仍无法确认的内容。

### 场景 D：校园范围追问

```text
用户：调宿需要哪些材料？
Assistant：你想查询南望山校区还是未来城校区的调宿规定？
用户：未来城。
```

期望：

- 第二轮恢复原问题和 `site=未来城`。
- 重新执行 KnowledgeAgent 检索。
- 最终事实只来自匹配范围的本地证据。

### 场景 E：知识缺口不是用户参数

```text
用户：今年奖学金截止日期是哪天？
```

本地证据没有日期时，期望：

- 不反问用户“截止日期是哪天”。
- 明确当前本地资料无法可靠确认。
- 提供安全的官方核验建议，但不编造入口、日期或电话。

### 场景 F：自然换题

```text
Assistant：请告诉我可用时间和当前难点。
用户：最近总是睡不着怎么办？
```

期望：

- 学习计划追问变为 `INTERRUPTED`。
- 当前消息正常进入睡眠/心理支持链路。
- “最近总是睡不着怎么办”不写入可用时间。

### 场景 G：高风险抢占

普通追问等待期间输入明确高风险内容。

期望：

- 立即进入 RISK。
- 不继续询问课程、校区或截止日期。
- 高风险内容不写入普通追问参数。
- 现有报告和工具计划继续工作。

## 20. 完成定义

以下条件全部满足才能宣布完成：

1. 原始故障输入稳定得到完整追问。
2. 字段名称、问句和任务描述不会被当成字段值。
3. 合法标签值和合法顺序回答仍能识别。
4. ClarificationService 不再写死学习计划字段或任务类型。
5. StudyPlan 与 KnowledgeScope 使用同一状态生命周期。
6. KnowledgeAgent 的合法 `CLARIFY` 可以跨轮恢复。
7. 本地证据不足不会转成对用户的错误事实追问。
8. 自然换题不会污染旧追问参数。
9. 高风险消息始终优先。
10. 用户和会话隔离测试通过。
11. Trace 不包含敏感参数正文。
12. 不必要的 ResponseAgent 追问改写被移除。
13. 全量 pytest 不低于修改前基线，且新增测试全部通过。
14. Engineering Harness 全部通过。
15. Docker 服务健康，MySQL 和 Redis 正常。
16. 所有修改文件保持 UTF-8，中文直接可读。

## 21. 明确禁止的错误实现

以下任一项出现即视为未完成：

- 仅把当前正则改得更长，却不增加字段值校验和负向测试。
- 用大模型自由抽取任意字段后直接写数据库。
- 继续把 `task_kind` 写死为 `study_plan`。
- 把 KnowledgeAgent 的任意 `clarification_question` 原样持久化。
- 用户换题时仍把完整新问题写入第一个缺失字段。
- 缺少官方证据时要求用户提供官方答案。
- 高风险输入先进入普通追问解析。
- 为通过测试而固定返回某一句追问。
- 删除或重建现有业务数据表。
- 清空所有用户会话、消息或报告。
- 修改无关 RAG、前端样式、工具队列或模型配置。
- 回滚用户当前工作区的既有修改。
- 将中文字符串改成 Unicode 转义。

## 22. 编码 AI 执行指令

可以将以下内容直接作为编码 AI 的主任务：

```text
请严格按照仓库根目录
MIND_BRIDGE_GENERAL_CLARIFICATION_OPTIMIZATION_IMPLEMENTATION_GUIDE_20260801.md
执行 MindBridge 通用追问改造。

开始前先读取该文档全文、AGENTS.md、当前 git 状态以及所有涉及文件，不得覆盖或回滚用户已有修改。先运行并记录测试基线，再为已确认故障添加失败回归测试。按“通用模型与 Handler -> 状态服务 -> KnowledgeAgent -> Coordinator/Harness -> Trace -> 真实验证”的顺序实施。

必须保持 Safety/RISK 优先、MySQL 状态权威、用户会话隔离、RAG 事实边界和 UTF-8 中文可读。禁止让模型自由定义字段并直接写库，禁止把知识库缺证据误判为需要用户补充事实。

每个阶段完成后运行对应测试。最终必须运行全量 pytest、Engineering Harness、Docker 健康检查和真实多轮对话验收。最终汇报实际修改文件、追问契约、支持的 task kind、数据库处理、测试结果、真实服务结果、已知限制和未完成项，不得只回复“已优化”。
```

## 23. 编码 AI 最终汇报模板

完成后必须按以下结构汇报：

1. 根因与最终行为变化。
2. 新增和修改的文件。
3. 通用追问契约与已支持的 task kind。
4. StudyPlan 字段解析和时间校验策略。
5. KnowledgeAgent CLARIFY 与证据不足的边界。
6. 自然换题、高风险抢占和最大轮数行为。
7. 数据库是否迁移、旧数据如何处理。
8. 并发、幂等、用户和会话隔离措施。
9. Trace 和隐私处理。
10. 新增测试清单及全量测试结果。
11. Harness、Docker、MySQL、Redis 和真实对话验证结果。
12. 仍存在的限制和下一步建议。

任何没有实际执行的验证必须明确标记为“未执行”，不能推测为通过。
