# CampusCove 前端品牌、历史会话与单向归档优化执行方案

> 文档用途：本文件用于指导 AI 在现有 MindBridge Python 项目中完成代码改造。执行者必须先完整阅读本文件，再检查当前代码和工作区状态，然后按阶段实施、测试和交付。
>
> 项目目录：`D:\BaiduNetdiskDownload\mindbridge-pytest\mindbridge-py`
>
> 核心原则：前端对用户展示 `CampusCove` 品牌，后端内部继续保留 `MindBridge` 命名；学生只能查看活跃历史会话并单向归档，不提供恢复和永久删除；本轮不实现跨会话记忆。

---

## 1. 背景与当前基线

当前项目已经具备以下基础能力：

- FastAPI 后端和静态 HTML、CSS、JavaScript 前端。
- `chat_sessions` 保存会话，`chat_messages` 保存消息。
- `conversation_summaries` 保存单个会话的增量结构化摘要。
- 学生发送请求时可携带 `sessionId`，继续同一个会话。
- 管理员可通过报告进入对应会话并只读查看历史消息。
- Redis 保存单个会话的短期消息缓存。
- MySQL 保存会话、消息、摘要、心理报告、Agent Trace 和审计数据。
- Ollama、Engineering Harness、RAG、MCP 工具队列已经接入。

当前缺口：

- 登录页、学生端、管理员端仍展示 `MindBridge`。
- 学生端没有历史会话列表。
- 页面刷新或点击“新会话”后，学生无法从 UI 再次打开旧会话。
- 学生没有归档能力。
- 会话模型没有归档状态。
- 现有管理员会话读取接口不适合作为学生接口复用，因为其权限和数据可见范围不同。

执行前必须先运行并记录当前基线：

```powershell
python -m pytest -q
python -m app.harness.runner
docker compose ps
```

若基线本身失败，先判断是否为环境问题或现有回归，不得把基线失败误归因于本轮改造。

---

## 2. 已确认的产品决策

本轮需求已经确定，执行 AI 不得自行扩大范围。

### 2.1 必须完成

1. 前端品牌由 `MindBridge` 调整为 `CampusCove`。
2. 学生端显示当前学生自己的活跃历史会话。
3. 学生可以打开旧会话并在同一 `sessionId` 下继续对话。
4. 学生可以归档活跃会话。
5. 归档后会话立即从学生端历史列表消失。
6. 归档会话不能继续发送消息。
7. 归档只改变可见状态，不删除任何持久数据。
8. 管理员仍可通过现有后台记录审阅归档会话，并能看到归档标记。
9. 保留现有单会话记忆、摘要、风险安全、RAG、Skill、MCP 和工具队列行为。

### 2.2 明确不做

- 不实现跨会话记忆。
- 不创建用户画像或 `user_memories` 表。
- 不把其他会话摘要注入当前会话。
- 不提供学生端归档列表。
- 不提供学生端恢复功能。
- 不提供管理员恢复功能。
- 不提供永久删除会话或消息的接口。
- 不删除心理报告、风险个案、Agent Trace、工具任务或审计记录。
- 不重命名 Python 类、包、数据库表、Redis Key、MCP 工具或 Harness。
- 不重构 Agent 运行时、KnowledgeAgent、RAG、SkillManager 或风险路由。
- 不改动 Ollama、OpenAI、MCP 或联网搜索配置。

### 2.3 品牌约定

用户可见名称：

| 场景 | 文案 |
|---|---|
| 产品名 | `CampusCove` |
| 学生端英文标题 | `CampusCove Student Companion` |
| 学生端中文副标题 | `校园陪伴与事务助手` |
| 管理端英文标题 | `CampusCove Support Console` |
| 助手消息角色 | `Cove` |
| Logo 缩写 | `CC` |

后端内部继续保留：

- `MindBridgeAgentHarness`
- `MindBridgeSkillLibrary`
- `MindBridgeMcpToolClient`
- `mindbridge:*` Redis Key
- MindBridge MCP 工具名称
- FastAPI 内部服务和 Harness 名称
- 数据库表名及已有迁移标识
- AI 系统提示词中的内部身份

不要为了品牌修改创建大规模后端重命名提交。

---

## 3. 总体架构

```text
学生浏览器
  ├─ GET  /api/conversations
  │    └─ 只返回当前学生未归档会话
  ├─ GET  /api/conversations/{sessionId}
  │    └─ 只返回当前学生未归档会话及消息
  ├─ POST /api/conversations/{sessionId}/archive
  │    └─ 设置 archived_at，不删除记录
  └─ POST /api/chat/stream
       └─ 已归档 sessionId 必须拒绝

MySQL
  ├─ chat_sessions.archived_at
  ├─ chat_messages                 保留
  ├─ conversation_summaries        保留
  ├─ psychological_reports         保留
  ├─ agent_run_traces              保留
  └─ 风险、工具和审计记录          保留

Redis
  └─ 归档成功后清理该会话短期缓存，持久数据不受影响
```

会话可见状态只有两种：

```text
ACTIVE   = archived_at IS NULL
ARCHIVED = archived_at IS NOT NULL
```

学生只接触 `ACTIVE` 状态。系统不暴露学生恢复路径。

---

## 4. 数据库和模型改造

### 4.1 ChatSession 字段

在 `app/models/entities.py` 的 `ChatSession` 增加：

```python
archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
```

建议增加便于复用的只读属性：

```python
@property
def archived(self) -> bool:
    return self.archived_at is not None
```

不要增加硬删除级联，不要修改消息、摘要、报告和 Trace 的外键语义。

### 4.2 Alembic 迁移

新增修订：

```text
0005_chat_session_archiving
```

要求：

- `down_revision` 指向当前真实 head。
- 增加 `chat_sessions.archived_at DATETIME NULL`。
- 为归档过滤和更新时间排序增加索引。
- 推荐索引名：`ix_chat_sessions_user_archive_updated`。
- 推荐索引字段：`user_id, archived_at, updated_at`。
- 不回填已有数据，历史会话默认均为活跃状态。
- 不复制其他项目的迁移历史。
- 必须兼容 MySQL 8 和 Harness 使用的 SQLite。

建议迁移结构：

```python
def upgrade():
    with op.batch_alter_table("chat_sessions") as batch:
        batch.add_column(sa.Column("archived_at", sa.DateTime(), nullable=True))
        batch.create_index(
            "ix_chat_sessions_user_archive_updated",
            ["user_id", "archived_at", "updated_at"],
            unique=False,
        )
```

实际编写前先检查已有索引，避免名称冲突。迁移测试应验证 upgrade、head 和已有数据保留。

---

## 5. 后端 DTO 契约

在 `app/schemas/dtos.py` 增加学生会话 DTO。字段命名与现有 API 保持 camelCase。

### 5.1 会话列表项

```python
class ConversationListItemResponse(BaseModel):
    sessionId: str
    title: str
    preview: str
    messageCount: int
    createdAt: datetime
    updatedAt: datetime
```

### 5.2 会话列表响应

```python
class ConversationListResponse(BaseModel):
    items: list[ConversationListItemResponse]
```

第一版最多返回最近 50 条活跃会话，不必引入复杂游标编码。接口保留 `limit` 参数，服务端约束 `1 <= limit <= 100`。如果未来数据量明显增大，再独立增加游标分页。

### 5.3 会话详情响应

扩展现有 `ConversationResponse`，保持管理员接口兼容：

```python
class ConversationResponse(BaseModel):
    sessionId: str
    title: str
    archived: bool = False
    archivedAt: datetime | None = None
    createdAt: datetime | None = None
    updatedAt: datetime | None = None
    messages: list[ConversationMessageResponse]
```

如果直接扩展会导致已有测试大量无关变化，也可以新增学生专用详情 DTO；但管理员响应最终必须能显示归档状态。

### 5.4 归档响应

```python
class ConversationArchiveResponse(BaseModel):
    sessionId: str
    archived: bool
    archivedAt: datetime
```

---

## 6. 会话服务层

新增独立服务文件：

```text
app/services/conversations.py
```

建议类名：

```python
class StudentConversationService:
    ...
```

不要把学生会话列表和归档逻辑塞进 `ReportService`。`ReportService` 面向管理员报告和审阅；学生会话属于不同权限边界。

### 6.1 list_active

```python
def list_active(self, user_id: int, limit: int = 30) -> ConversationListResponse:
    ...
```

查询条件：

```text
ChatSession.user_id == user_id
ChatSession.archived_at IS NULL
```

排序：

```text
updated_at DESC, id DESC
```

列表项规则：

- `title` 使用现有会话标题；空标题时回退为“新会话”。
- `preview` 取最后一条消息，规范化空白并截断到 80 个字符。
- 不在 preview 中暴露风险等级、报告标识或 Agent 内部状态。
- `messageCount` 返回会话消息总数。
- 查询应避免对每条会话分别执行一次消息查询。优先使用子查询、聚合查询或最多两次批量查询。

### 6.2 get_active

```python
def get_active(self, user_id: int, public_id: str) -> ConversationResponse:
    ...
```

必须同时校验：

```text
public_id 匹配
user_id 匹配当前用户
archived_at IS NULL
```

不存在、属于别人或已归档时，学生接口统一返回 404，不向调用者泄露会话是否属于其他用户。消息按 `created_at ASC, id ASC` 排序。

### 6.3 archive

```python
def archive(self, user_id: int, public_id: str) -> ConversationArchiveResponse:
    ...
```

行为：

1. 只允许归档当前用户自己的会话。
2. 不允许修改其他用户的会话。
3. 设置 `archived_at` 为 UTC 无时区时间，与项目现有时间存储习惯一致。
4. 不删除任何 MySQL 数据。
5. 归档成功后清理该会话 Redis 短期缓存。
6. 重复归档同一条属于当前用户的会话应幂等返回成功。
7. 不创建恢复方法。

为了支持 Redis 缓存清理，在 `RedisShortTermMemoryStore` 增加：

```python
def delete(self, session_public_id: str) -> None:
    ...
```

要求 Redis 不可用时只记录 warning，不影响 MySQL 归档结果。Harness 中的 `InMemoryShortTermMemoryStore` 同步实现 `delete()`，避免测试替身与生产接口再次不一致。

---

## 7. 学生 API

在 `app/api/routes.py` 增加以下路由。全部使用 `current_user`，不得使用 `require_admin`。

### 7.1 活跃会话列表

```http
GET /api/conversations?limit=30
```

约束：

- `limit` 默认 30，最小 1，最大 100。
- 只返回当前用户未归档会话。
- 管理员账号仍受现有角色边界约束；不应通过学生接口读取任意学生会话。

### 7.2 活跃会话详情

```http
GET /api/conversations/{session_id}
```

只返回当前用户未归档会话。不要复用 `/api/admin/conversations/{session_id}` 的授权逻辑。

### 7.3 单向归档

```http
POST /api/conversations/{session_id}/archive
```

不接受 `archived=false`，不提供通用 PATCH，避免无意间形成恢复能力。

### 7.4 禁止新增的接口

本轮不得新增：

```http
DELETE /api/conversations/{session_id}
POST   /api/conversations/{session_id}/restore
PATCH  /api/conversations/{session_id}  # 若可反向修改 archived 状态
GET    /api/conversations?status=archived
```

---

## 8. 聊天入口的归档硬约束

只在前端隐藏归档会话是不够的。必须修改 `app/agents/harness.py` 的 `_resolve_session()`：

当请求包含 `sessionId` 时，查询条件必须加入：

```python
ChatSession.archived_at.is_(None)
```

推荐异常：

```python
class ArchivedConversationError(ValueError):
    pass
```

或者在服务层使用明确的领域异常。`/api/chat/stream` 应将已归档会话映射为可识别的 HTTP 错误，推荐：

```http
409 Conflict
```

```json
{
  "detail": "该会话已归档，不能继续发送消息。"
}
```

但不能因为区分归档错误而泄露其他用户会话。其他用户的 `sessionId` 仍返回 404。

新会话创建时 `archived_at` 默认为 `NULL`。

归档与正在运行的流式回答：

- 前端在 `state.sending === true` 时禁用会话切换、新会话和归档按钮。
- 后端保证归档后不能开启新一轮对话。
- 本轮不增加分布式运行锁，不重构流式生命周期。

---

## 9. 管理员只读兼容

现有管理员接口：

```http
GET /api/admin/conversations/{session_id}
```

继续允许管理员读取归档会话。响应增加：

```json
{
  "archived": true,
  "archivedAt": "2026-07-29T12:00:00"
}
```

管理员前端在会话标题附近显示只读状态：

```text
已归档
```

不要增加管理员恢复按钮、删除按钮或状态编辑入口。

---

## 10. 前端品牌实现

涉及文件：

- `app/static/index.html`
- `app/static/student.html`
- `app/static/admin.html`
- `app/static/app.js`
- `app/static/student.js`
- `app/static/admin.js`
- `app/static/styles.css`

建议新增：

```text
app/static/brand.js
```

内容示例：

```javascript
window.CAMPUSCOVE_BRAND = Object.freeze({
  product: "CampusCove",
  assistant: "Cove",
  studentTitle: "CampusCove Student Companion",
  adminTitle: "CampusCove Support Console"
});
```

HTML 应保留正确的静态回退文案，JavaScript 配置用于动态消息角色等场景。不要让页面在 JavaScript 未加载时重新显示 MindBridge。

必须替换的用户可见位置：

- 浏览器 `<title>`。
- 登录页主标题。
- 学生端品牌标题和 Logo。
- 管理员端品牌标题和 Logo。
- 学生端助手消息角色。
- 管理员会话详情中的助手消息角色。

保留 `mindbridge.auth` 认证存储键，避免无意义地让现有登录会话失效。资源版本查询参数应更新，防止浏览器继续使用旧 CSS 和 JS 缓存。

---

## 11. 学生端界面改造

### 11.1 桌面布局

将当前学生端左栏改为会话导航，不再长期展示“QUICK SIGNAL”和“CARE LOOP”。

```text
┌────────────────────────────────────────────────────────────┐
│ CC  CampusCove                 服务状态       用户 / 退出  │
├───────────────────┬────────────────────────────────────────┤
│ + 新建会话        │ 当前会话标题                           │
│                   ├────────────────────────────────────────┤
│ 最近对话          │                                        │
│  考试压力...   ⋯  │               消息区域                 │
│  宿舍问题...   ⋯  │                                        │
│  论文安排...   ⋯  │                                        │
│                   ├────────────────────────────────────────┤
│                   │ 输入框                       发送按钮   │
└───────────────────┴────────────────────────────────────────┘
```

会话项展示：

- 标题，最多两行。
- 最后一条消息预览，最多两行。
- 友好的更新时间。
- 当前选中状态。
- 三点菜单按钮，工具提示为“会话操作”。
- 菜单中唯一破坏性操作为“归档”。

不得展示：

- 风险等级。
- 情绪标签或评分。
- 心理报告状态。
- Agent、RAG、Skill 或工具运行细节。
- 已归档会话。

### 11.2 新会话空状态

原快捷表达移动到新会话空状态中，只在当前没有消息时显示。建议保留 3 至 4 个紧凑快捷入口，不创建嵌套卡片。

“新建会话”只将前端切换为空状态，不立即写数据库。用户发送第一条消息时，由现有后端逻辑创建会话。

### 11.3 移动端

- 会话列表做成左侧抽屉。
- 顶部使用历史会话图标按钮打开抽屉，并提供 `aria-label="打开历史会话"`。
- 抽屉打开时有遮罩，点击遮罩或按 Escape 关闭。
- 抽屉宽度使用响应式约束，例如 `min(86vw, 340px)`。
- 输入区、消息区和抽屉不得重叠。
- 不允许长标题把菜单按钮挤出容器。

### 11.4 归档确认

使用原生 `<dialog>` 或现有风格的可访问模态框，不使用简单的永久删除措辞。确认文案固定为：

> 归档后，这条会话将从你的历史记录中移除，并且无法在学生端恢复。会话数据仍会按照系统安全与审计规则保留。确认归档吗？

按钮：

- `取消`
- `确认归档`

归档不是删除，不使用“彻底删除”“清空记录”等文本。

---

## 12. 学生端状态机

扩展 `app/static/student.js`：

```javascript
const state = {
  sessionId: null,
  conversations: [],
  loadingConversations: false,
  loadingMessages: false,
  archivingSessionId: null,
  sending: false,
  profile: null,
  modelName: "mock"
};
```

### 12.1 初始化

```text
验证登录
  -> 加载 profile
  -> 加载 agent status
  -> 加载活跃会话列表
  -> 默认显示新会话空状态
```

不要在初始化时自动打开最近会话，避免用户误以为新输入一定属于旧上下文。

### 12.2 打开历史会话

1. 若正在发送消息，禁止切换。
2. 设置 `loadingMessages=true`。
3. 请求会话详情。
4. 清空并安全渲染历史消息。
5. 设置 `state.sessionId`。
6. 高亮当前会话。
7. 更新会话标题和状态。
8. 完成后恢复输入。

消息正文必须使用 `textContent`，不得将模型内容直接写入 `innerHTML`。

### 12.3 发送消息

沿用现有 SSE：

```text
meta -> token* -> done
```

规则：

- 发送期间禁用会话切换、新会话和归档。
- 新会话收到 `meta.sessionId` 后设置当前会话 ID。
- `done` 后重新加载会话列表，使标题、preview、计数和更新时间与数据库一致。
- 旧会话发送成功后仍保持当前选择。
- 请求失败时保留清晰错误状态，不生成假历史记录。

### 12.4 新建会话

1. 若正在发送，拒绝切换。
2. `state.sessionId = null`。
3. 清空消息区。
4. 显示欢迎与快捷表达。
5. 清除历史列表选中状态。
6. 不归档当前会话，不删除任何数据。

### 12.5 归档

归档当前会话：

1. 打开确认对话框。
2. 发送归档请求。
3. 从本地会话列表移除。
4. 将 `state.sessionId` 设为 `null`。
5. 显示新会话空状态。
6. 显示简短成功提示。

归档非当前会话：

1. 打开确认对话框。
2. 发送归档请求。
3. 从列表移除。
4. 当前聊天保持不变。

失败时不得先从列表永久移除；还原归档按钮的可用状态并显示可理解错误。

---

## 13. 单会话记忆边界

本轮保留现有单会话记忆：

- 重新打开旧的活跃会话后，继续传递相同 `sessionId`。
- `PreRouteMemoryLoader` 仍只读取当前会话摘要和消息。
- `ConversationSummaryService` 仍只更新当前 `session_id` 的摘要。
- 创建新会话后，不加载其他会话内容。
- 归档后清理 Redis 短期缓存，但保留 MySQL 摘要和消息。

执行 AI 必须检查代码，确保没有引入以下行为：

- 按 `user_id` 汇总多个会话摘要。
- 把其他会话消息加入模型 history。
- 从多个会话提取用户偏好。
- 创建跨会话向量索引。
- 因打开历史列表而将旧会话内容自动注入新会话。

历史列表是 UI 导航能力，不等于跨会话记忆。

---

## 14. 错误处理和安全要求

### 14.1 权限

必须测试并保证：

- 学生 A 看不到学生 B 的会话。
- 学生 A 不能读取学生 B 的会话详情。
- 学生 A 不能归档学生 B 的会话。
- 学生 A 不能继续学生 B 的会话。
- 已归档会话不能通过手工构造请求继续发送。

### 14.2 数据保留

归档前后以下记录数量不得减少：

- `chat_messages`
- `conversation_summaries`
- `psychological_reports`
- `agent_run_traces`
- 风险个案、工具和审计记录

### 14.3 输出安全

- 历史列表不输出后台风险元数据。
- preview 使用普通消息正文，做长度限制和空白规范化。
- 前端使用 `textContent` 渲染用户和模型内容。
- 归档接口不接受任意 user ID。
- 日志不额外打印完整聊天正文。

### 14.4 错误状态

| 场景 | 推荐状态 |
|---|---|
| 未认证 | `401` |
| 管理员调用学生聊天能力 | 保持现有 `403` |
| 会话不存在或不属于当前学生 | `404` |
| 已归档会话继续发送 | `409` |
| 参数非法 | `422` |
| Redis 缓存清理失败 | 归档仍成功，记录 warning |

---

## 15. 测试计划

### 15.1 模型和服务单元测试

新增或扩展测试，至少覆盖：

1. `ChatSession.archived_at` 默认是 `None`。
2. 迁移后已有会话仍为活跃状态。
3. 会话列表只返回当前用户活跃会话。
4. 会话列表按 `updated_at DESC, id DESC` 排序。
5. preview、messageCount 和 limit 正确。
6. 详情消息按时间和 ID 顺序返回。
7. 归档设置 `archived_at`，不删除消息。
8. 归档不删除 `conversation_summaries`。
9. 重复归档幂等。
10. Redis 删除失败不回滚 MySQL 归档。
11. Harness 内存替身实现 `delete()`。

### 15.2 API 权限测试

至少覆盖：

1. 学生只能列出自己的活跃会话。
2. 学生能读取自己的活跃会话。
3. 学生无法读取其他学生的会话。
4. 学生能归档自己的会话。
5. 学生无法归档其他学生的会话。
6. 已归档会话不再出现在列表中。
7. 学生无法读取已归档会话详情。
8. 学生无法继续已归档会话，返回 409。
9. 管理员仍可读取与报告关联的归档会话。
10. 不存在恢复或 DELETE 路由。

### 15.3 会话行为测试

1. 打开旧会话后继续发送，返回相同 `sessionId`。
2. 新会话第一条消息创建新 `sessionId`。
3. 新会话不读取旧会话摘要。
4. 历史列表加载不改变 Agent 运行上下文。
5. 归档一条非当前会话不影响当前会话。

### 15.4 Harness

更新 Engineering Harness 的 API Suite，至少增加：

- 创建两条学生会话。
- 列表可见。
- 读取详情。
- 归档其中一条。
- 归档后列表不可见。
- 归档后消息仍在数据库。
- 归档后不能继续聊天。
- 管理员仍能读取归档会话。

不得为了让 Harness 通过而削弱现有安全断言。原有六套件必须继续全部 PASS。

---

## 16. 前端视觉和交互验证

代码完成后必须启动真实服务并验证，不得只依赖静态阅读。

### 16.1 桌面视口

至少验证：

- `1440 x 900`
- `1280 x 720`

检查：

- 品牌、状态和账户区域不重叠。
- 历史栏宽度稳定。
- 长标题和长 preview 不挤压菜单按钮。
- 消息区和输入区可正常滚动。
- 归档对话框文本完整。
- 当前会话高亮清楚但不过度装饰。

### 16.2 移动视口

至少验证：

- `390 x 844`
- `360 x 800`

检查：

- 历史抽屉可以打开和关闭。
- 遮罩、抽屉和聊天区域层级正确。
- 输入框和发送按钮不溢出。
- 会话菜单可点击。
- 归档对话框不会超出视口。
- 页面无横向滚动条。

若环境提供浏览器自动化，使用截图和 DOM 检查验证；若无法使用，必须说明未执行的视觉验证项目。

---

## 17. 推荐实施顺序

严格按以下顺序执行，阶段完成后立即运行相关测试。

### 阶段 1：迁移和模型

- 增加 `archived_at`。
- 新增 Alembic 迁移与索引。
- 增加迁移测试。

验收：迁移专项测试通过，新旧数据库可升级，已有会话数据不变。

### 阶段 2：DTO 和会话服务

- 增加列表、详情、归档 DTO。
- 新增 `StudentConversationService`。
- 增加 Redis `delete()` 和 Harness 替身实现。
- 增加服务单元测试。

验收：列表、所有权、归档幂等和数据保留测试通过。

### 阶段 3：API 和聊天硬约束

- 增加三个学生接口。
- 补归档会话继续发送的后端阻断。
- 扩展管理员详情归档标记。
- 增加 API 权限测试。

验收：越权、归档可见性和 409 行为符合契约。

### 阶段 4：前端品牌

- 增加品牌配置。
- 更新三端标题、Logo 和消息角色。
- 更新静态资源版本参数。

验收：用户可见页面不再显示旧品牌，后端内部命名无大规模变更。

### 阶段 5：学生历史会话 UI

- 改造会话侧栏和移动抽屉。
- 实现列表、详情、新会话、旧会话继续对话。
- 实现单向归档和确认对话框。
- 增加加载、空、错误和禁用状态。

验收：学生能完成完整历史会话工作流，且看不到归档会话。

### 阶段 6：管理员标记和完整验证

- 管理员会话详情显示“已归档”。
- 扩展 Harness。
- 运行完整测试和真实服务验证。

验收：所有自动化、视觉验证和真实 Ollama 对话通过。

---

## 18. 执行命令

执行 AI 应根据环境调整，但至少完成以下验证：

```powershell
python -m pytest tests/test_migrations_and_import.py -q
python -m pytest -q
python -m app.harness.runner
```

Docker 重建：

```powershell
$env:AI_PROVIDER='ollama'
$env:OLLAMA_MODEL='qwen3:8b'
$env:KNOWLEDGE_VECTOR_ENABLED='false'
docker compose up -d --build
docker compose ps
docker compose logs --tail 200 app
```

健康检查：

```powershell
curl.exe -sS http://127.0.0.1:8080/actuator/health
```

必须确认：

- Alembic 到达最新 head。
- 知识导入成功。
- Uvicorn 监听 `8080`。
- `/actuator/health` 返回 `UP`。
- `/api/agent/status` 仍显示真实 Ollama 配置。
- 至少完成一次真实流式对话。
- 归档后的 `sessionId` 无法继续发送。

---

## 19. 编码和编辑约束

- 所有修改文件保存为 UTF-8，优先 UTF-8 无 BOM。
- 中文正文、注释和 UI 文案必须直接可读。
- 不得把正常中文替换为 `\u4e2d\u6587` 形式。
- 不使用宽泛脚本重写整个文件，避免破坏编码。
- 使用 `apply_patch` 进行手工代码编辑。
- 保留用户现有未提交改动，不得回滚无关文件。
- 不进行无关格式化、依赖升级和大规模重构。

完成前扫描改动文件：

```powershell
rg -n "\\u[0-9a-fA-F]{4}" app migrations tests
```

若普通中文字符串或注释出现 Unicode 转义，必须转换回可读中文。

---

## 20. 最终验收标准

全部满足才可宣布完成：

### 品牌

- 登录页、学生端、管理员端显示 `CampusCove`。
- 学生消息角色显示“我”，助手消息角色显示 `Cove`。
- Logo 显示 `CC`。
- 用户可见静态页面不再出现 `MindBridge`。
- 后端内部 MindBridge 类名、MCP、Redis Key 和表名保持不变。

### 历史会话

- 学生能看到自己的活跃历史会话。
- 学生不能看到其他学生会话。
- 学生能打开旧会话并查看完整历史。
- 学生能在旧会话继续对话，并沿用相同 `sessionId`。
- 新建会话不会继承其他会话上下文。

### 归档

- 学生能单向归档自己的活跃会话。
- 归档会话立即从学生历史列表消失。
- 学生看不到归档列表。
- 学生没有恢复按钮和恢复 API。
- 系统没有永久删除会话 API。
- 已归档会话不能继续发送消息。
- 归档不会删除消息、摘要、报告、Trace 或审计数据。
- 管理员仍能只读审阅归档会话并看到归档标记。

### 回归

- 完整 `pytest` 通过。
- Engineering Harness 六套件全部通过。
- RAG 指标不低于现有 Harness 门槛。
- 风险安全路由不被削弱。
- MCP 工具队列不受影响。
- Docker 服务健康。
- 真实 Ollama 流式请求成功。
- 桌面和移动端无明显重叠、溢出和状态错乱。

---

## 21. 交付说明模板

执行完成后，AI 的最终说明必须包含：

1. 实际修改的功能摘要。
2. 新增迁移版本和当前 Alembic head。
3. 新增 API 列表。
4. 学生端归档的准确语义。
5. 明确说明没有实现恢复、永久删除和跨会话记忆。
6. `pytest` 结果。
7. 六套 Engineering Harness 结果和关键 RAG 指标。
8. Docker、健康检查和真实 Ollama 验证结果。
9. 视觉验证的桌面和移动视口。
10. 任何未执行或仍有风险的项目。

不得仅说“已优化”或“测试通过”，必须给出可核验的结果和报告路径。
