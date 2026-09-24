# MindBridge 预留 MCP 工具实施方案

日期：2026-09-13
状态：工具骨架已按本方案实现；默认关闭，未接入真实学校接口。

## 1. 最终目标与范围

本轮目标是预留六个 MCP 工具的定义、输入输出契约和实现骨架，方便面试时介绍扩展设计。真实学校接口、统一登录授权和真实数据返回都不是本轮完成条件。

工具默认不启用，不加入现有 Agent 可见列表，不参与当前聊天链路。当前系统行为保持原样。

这一范围取代之前方案中的集成改造：本轮不调整系统提示词、工具权限、纯学习计划分支、澄清流程、证据传递、AgentLoop、缓存身份隔离及数据库结构。先前要求删除纯计划分支等内容暂不实施，待未来实际启用工具时再单独讨论。

## 2. 六个预留工具

| 工具名称 | 用途 | 模型可见业务参数 | 预留结果字段 |
|---|---|---|---|
| get_my_courses | 查询本人已选课程 | term_id? | course_id、course_name、credits、class_id、term_id |
| get_my_timetable | 查询本人实际课表 | start_date、end_date、course_ids? | course_id、course_name、start_at、end_at、location、status |
| get_my_deadlines | 查询考试与作业节点 | start_date、end_date、course_ids?、types? | event_id、course_id、type、title、due_at/start_at、status |
| search_official_sources | 官方来源搜索和正文提取 | query、source_scope、date_from?、date_to?、max_results? | title、url、publisher、published_at、fetched_at、excerpt |
| get_my_application_status | 查询本人办事申请进度 | application_id?、service_type?、term_id? | application_id、service_name、status、current_step、updated_at、missing_materials |
| get_counseling_availability | 查询咨询可预约时段 | start_date、end_date、campus?、mode? | slot_id、start_at、end_at、location、mode、available、booking_url |

这些均为只读工具，不包含选课、提交申请、预约、发消息等操作。具体字段是本项目预留契约，不代表学校真实接口已采用该格式。

## 3. 接入位置与默认关闭方式

复用现有 app/chat_tools/server.py 和 FastMCP 注册机制，不创建第二套运行时，不修改后台线程、请求队列、stdio 会话管理或风险工具 Server。

建议仅新增以下目录：

- app/chat_tools/extensions/__init__.py：提供 register_extension_tools(mcp) 注册入口。
- app/chat_tools/extensions/contracts.py：参数/返回类型、统一未接入结果。
- app/chat_tools/extensions/academic.py：三个学业工具。
- app/chat_tools/extensions/official_sources.py：搜索与正文提取工具。
- app/chat_tools/extensions/applications.py：申请状态工具。
- app/chat_tools/extensions/counseling.py：咨询时段工具。
- tests/test_extension_tools.py：扩展工具的独立契约和隔离测试。

现有 server.py 仅增加一个默认关闭的条件注册入口，例如读取 MINDBRIDGE_EXTENSION_TOOLS_ENABLED，只有明确设为 true 时才导入扩展包并调用注册函数；未设置时完全不注册新工具。开关放在扩展入口附近，不修改现有配置类和运行时环境传递。

该开关仅用于独立启动 MCP Server 做开发验证，不配置到当前聊天应用。即便独立 MCP Server 开启了注册，也不等于专业 Agent 获得工具权限：本轮不改 Registry 和 AgentProfile 的现有白名单。

默认启动时，MCP 工具列表仍只有原有 rag_search 和 get_current_weather。扩展模块不被加载，不额外访问网络或数据库。

## 4. 实现深度：可注册、可校验、可明确返回未接入

本轮完成：

1. 六个工具的名称、描述和输入 schema。
2. 基本参数校验，例如日期范围、事件类型和结果数量上限。
3. 固定格式的返回契约。
4. 未配置真实接口时返回 NOT_CONFIGURED，明确说明对应学校接口尚未接入。
5. 留出后续替换为真实查询实现的函数位置，不提前建设复杂提供方管理框架。

沿用现有 MCP CallToolResult 的错误形式：isError=true，structuredContent.error 包含 code=NOT_CONFIGURED 和可读 message。不要返回成功但 records 为空，因为“尚未接入”不同于“确实没有课程/申请/时段”。

真实接口接通前，不发出学校业务请求，不要求用户提供教务密码，不制造真实查询结果。测试使用明确的 fixture；如未来需要面试演示样例，可单独加入显式 DEMO 数据模式，当前不默认实现或启用。

search_official_sources 本轮同样只预留“搜索 → 筛选官方来源 → 提取正文 → 统一返回”的函数组织和结果格式，不要求购买搜索服务、配置密钥或实现完整抓取系统。面试介绍时应区分此流程设计与已完成能力。

## 5. 身份假设与未来扩展位置

业务场景假设为嵌入学校官网，未来由学校统一登录提供学生身份及查询权限；这是假设的集成条件，不表示当前项目已经接通学校系统。

本轮不做身份绑定表、分系统授权、签名认证、缓存隔离改造或 MCP 身份传输。

六个工具的业务参数不让模型选择其他学生。因为当前工具只返回未接入，不读取个人数据，所以不需要提前把身份传递链路加入现有系统。

未来真正启用个人查询时，再从已登录用户获取身份，由后端注入查询，补上本人范围校验和缓存隔离。不能在没有可信用户身份时直接启用真实个人数据读取。

## 6. 保持现状的部分

- 不修改任何现有系统提示词及 Skill。
- 不调整 ToolRegistry.for_agent 和 AgentProfile.tool_permissions。
- 不删除 planning_only 或其他纯学习计划逻辑。
- 不改变 Understanding、Coordinator、澄清白名单和恢复流程。
- 不改 SpecialistResult、ContextBuilder、ResponseAgent 和证据聚合。
- 不调整 AgentLoop 调用轮次、工具调用数、结果预算、超时或终止逻辑。
- 不修改现有 MCP 后台线程、连接管理、执行器和缓存。
- 不改登录逻辑、数据库模型或迁移。
- 不接入新的第三方依赖、搜索账号或学校账号。

必要的生产代码改动仅限：新增扩展模块，以及 server.py 的默认关闭注册入口。方案文档和独立测试不进入聊天行为。

## 7. 实施与验收

按学业三工具、官方搜索、申请状态、咨询时段的顺序添加，最后统一验证。

验收标准：

1. 默认启动 MCP Server 时工具列表保持原样，新增六个工具不可见。
2. 默认关闭时不导入扩展实现，不触发任何新网络或数据库访问。
3. 在独立测试环境显式开启后，六个工具可列出，名称和参数 schema 正确。
4. 合法请求得到规范 NOT_CONFIGURED 结果；非法参数得到明确校验错误。
5. 当前 Agent 工具可见列表保持原样，既有聊天不会调用新增工具。
6. 运行现有 MCP/工具权限相关测试；本轮不改变提示词和业务流程，因此不新建大范围行为回归工程。
7. 检查变更清单，确保没有触及上述保持现状的模块。
8. 所有新增/编辑文件使用 UTF-8，中文保持可读，检查意外 Unicode 转义。

这些测试通过表示“工具骨架与默认关闭的注册机制完成”，不表示真实教务、办事、咨询或联网搜索已接通。

## 8. 面试时可以准确介绍的内容

完成实现后可以介绍：

“项目已有 MCP 工具框架，目前运行中的工具是本地知识检索和天气查询。我另外预留了六个只读扩展工具，包括课程、课表、考试作业节点、官方来源搜索、申请进度和咨询时段，定义了它们的输入输出，并采用默认关闭的注册方式，保证不影响现有聊天流程。学校接口暂未接通，后续接入统一登录身份和业务接口后，再开放给对应专业 Agent。”

可以展开讲工具划分、数据来源与 RAG 的区别、默认关闭的集成方式，以及未来启用时需要补齐的身份与证据链路。不宣称已经获取真实课程、办理状态或成功查询真实预约时段。

## 9. 本次实现与验证记录

已新增 app/chat_tools/extensions 下的六个模块（包含六个业务工具），现有生产文件仅修改 app/chat_tools/server.py，加入默认关闭的条件注册入口。新增 tests/test_extension_tools.py；提示词、权限、澄清、证据、身份和调用治理代码保持原样。

独立启动 MCP Server 时，可设置 MINDBRIDGE_EXTENSION_TOOLS_ENABLED=true 后运行 python -m app.chat_tools.server 检查工具发现与调用。正常应用无需设置此开关；本轮未向聊天运行时传递该开关，也未开放 Agent 权限。开关只控制注册，不代表已接通数据或自动获得缓存、熔断等执行器能力。

六个工具均包含业务 schema 和基本校验，合法请求返回 NOT_CONFIGURED。官方搜索尚未实现真实搜索和正文抓取，个人工具尚未实现真实身份注入或学校查询。

验证命令：python -m pytest tests/test_extension_tools.py tests/test_mcp_tools_v2.py tests/test_tool_calling_v2.py tests/test_risk_mcp_isolation_v2.py -q -rs

结果：45 passed，1 skipped。跳过的是未开启 WEATHER_INTEGRATION_TEST_ENABLED 的天气联网测试。已通过真实 stdio MCP 子进程验证八个工具的发现、六个新工具的错误返回和当前 Agent 权限不变；已通过独立进程验证默认关闭时扩展模块未加载。
