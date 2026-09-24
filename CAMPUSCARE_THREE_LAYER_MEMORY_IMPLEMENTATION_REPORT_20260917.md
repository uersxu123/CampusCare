# CampusCare 三层记忆改造实施与验收报告

日期：2026-09-17

## 结论

批准方案中的代码、迁移、持久任务、Chroma 记忆集合、画像治理、回填工具、配置和确定性评估已经落地。核心记忆与真实 Agent 输入链路测试通过；本机真实 Chroma 0.5.23 集成通过；隔离 SQLite 迁移完成升降级往返。

当前不能声明“生产环境完整验证完成”：真实 MySQL 仍在 `0013_specialist_trace_contract`，尚未执行 `0015`；没有可用的真实 Redis 故障环境和线上 Ollama/OpenAI Embedding/画像模型服务。本报告将这些项目列为环境验证项，不把 SQL fallback 或 mock 结果冒充真实服务结果。

## 分阶段实施清单

### 阶段 0

- 已核对实际迁移 head 和数据库 current：代码 head 为 `0015_three_layer_memory_v3`，当前 MySQL 为 `0013_specialist_trace_contract`。
- 项目不是 Git 工作树，没有初始化 Git。
- 原文件备份位于 `D:\download\CampusCare\_codex_backups\three_layer_memory_20260917`。

### 阶段 1：统一装配和真实模型输入

- Harness 每轮只调用一次 `ContextBuilder.build_base_context()`，Runtime 必须接收 `context_packet`。
- `TurnContextPacket` 升级为 V3，携带 snapshot、摘要游标、桥接消息、历史 episode、画像版本和诊断 manifest。
- 专业 Agent 和 ResponseAgent 的真实调用分别使用 `build_specialist_prompt()`、`build_synthesis_prompt()`。
- 四块基础记忆统一为工作记忆、累计摘要、相关历史和用户画像；当前输入只注入一次。
- Understanding、Safety 从同一 packet 派生有界视图。
- 每轮 AgentLoop 将模型消息、工具 schema 和工具结果一起纳入预算；Ollama 输入预算按上下文窗口、输出预算和安全余量计算。

### 阶段 2：持久任务、压缩和短会话

- 新增 `memory_jobs`、`conversation_episodes`、`user_profile_states` 及 UserMemory 版本治理字段。
- 助手消息、ChatTurn COMPLETED、Trace 和记忆任务在同一事务提交。
- Worker 支持原子领取、租约、过期恢复、重试退避、幂等键和同用户画像行级串行化。
- 增量摘要使用覆盖游标和 CAS；摘要滞后区间通过 SQL bridge 补齐。
- 短会话通过 FINALIZE_SESSION 形成 tail episode；归档事务会触发收尾。
- Redis miss、过期、脏内容或缺尾时回退 SQL，并以合并方式安全回填，不删除重建列表。

### 阶段 3：Chroma 历史和画像治理

- 使用独立 episode/profile collections、显式 embedding、稳定文档 ID 和显式 `$and` 复合过滤。
- Chroma 结果回到 SQL 校验用户、状态、版本、hash 和 memory epoch；异常时使用同用户 SQL 回退。
- 自动画像只从用户消息增量提取；显式记忆和更正优先。
- 删除会递增 memory epoch、失效相关 episode、清理摘要来源、删除向量并重建剩余画像索引。
- 删除 tombstone 会抑制同键历史表述及其已接受助手回复，防止旧任务和历史回填复活已遗忘事实。

### 阶段 4：回填、部署和评估

- 新增默认 dry-run 的 `scripts/backfill_memory_v3.py`，支持按用户、限量、画像索引、legacy episode 和历史收尾任务。
- README 与 Docker Compose 已加入开关、预算、Chroma、Embedding、Worker、迁移、回填和回退说明。
- 新增 100 轮确定性评估脚本和 JSON 报告。

## 数据库迁移

迁移文件：`migrations/versions/0015_three_layer_memory_v3.py`

隔离验证结果：

1. 空 SQLite 数据库升级到 `0015_three_layer_memory_v3`。
2. 降级到 `0014_five_intent_route_v3`。
3. 再升级到 `0015_three_layer_memory_v3`。
4. 最终 `alembic current` 为 `0015_three_layer_memory_v3 (head)`。

生产升级前必须先备份并在 MySQL 8 隔离实例执行同样往返验证。当前真实 MySQL 未修改。

## 配置、启动、回填和回退

启动前：

```powershell
alembic current
alembic upgrade head
```

应用内 Worker 默认随 FastAPI 生命周期启动；多实例生产环境应只启用明确数量的 Worker，并使用 HTTP Chroma 服务，避免各实例读取不同本地目录。配置项和推荐初始值见 README 的“三层记忆 V3”章节。

回填先预览：

```powershell
python -m scripts.backfill_memory_v3 --limit 1000
python -m scripts.backfill_memory_v3 --apply --enqueue-history --limit 1000
python -m scripts.backfill_memory_v3 --apply --user-id 123 --enqueue-history
```

读取回退时关闭 `MEMORY_V3_ENABLED`、`MEMORY_PROFILE_EXTRACTION_ENABLED` 和 `MEMORY_WORKER_ENABLED` 后重启。先保留 V3 表和 Chroma 数据；只有完成备份并确认没有新版本进程写入后，才执行：

```powershell
alembic downgrade 0014_five_intent_route_v3
```

## 验证结果

### 已通过

- `python -m compileall -q app tests scripts migrations`
- 核心记忆、用户画像/API、事件驱动多 Agent、Response、无第二次回答、安全复审：`37 passed`
- 新增三层记忆专项：`7 passed`
- 真实 ChromaDB 0.5.23：显式向量、upsert、用户 `$and` 过滤、query、delete 均通过。
- Redis 不可用/脏缓存、Chroma/Embedding 查询异常均验证 SQL 回退；故障没有被标记为索引成功。
- 两 Worker 同任务领取排他、租约过期恢复、重试状态、成功轮次事务入队通过。
- UTF-8 严格解码、无 BOM、无 U+FFFD 替换字符、无意外 Unicode 转义序列：全部通过。

全量回归：

```text
412 passed, 42 failed, 1 skipped, 47 warnings
```

剩余失败集中于仓库既有乱码夹具、旧路由/Coordinator 契约、旧直连生成假设、知识库 corpus 快照，以及旧测试直接调用 Runtime 而不传 `context_packet`。其中缓存旧测试期望 Redis 同 ID 文本覆盖 SQL，这与本方案“脏缓存回退 SQL 事实源”要求有意不同。未为了让旧测试通过而恢复 Runtime 内部二次装配或允许脏缓存覆盖事实源。

### 100 轮评估

- 100 轮，两个会话。
- specialist 最大输入 1018 tokens；Response 最大输入 991 tokens。
- protected overflow 0；当前输入重复 0；来源 ID 重复 0；snapshot 不一致 0。
- 第二会话跨会话召回 50/50。
- 摘要版本 `[23, 23]`，游标 `[92, 192]`，单调推进有效。

这是 SQLite + SQL fallback 的确定性评估，不包含在线模型质量、真实 Embedding 延迟或生产负载 p95。

## 仍需环境验证

- 在 MySQL 8 隔离副本验证 `0013 → 0015 → 0014 → 0015`，再安排生产升级。
- 真实 Redis 服务的断连、恢复、并发回填和 WATCH 冲突压测。
- 生产形态 HTTP Chroma 的多实例一致性、备份恢复和冷启动测试。
- 真实 Ollama/OpenAI Embedding 超时、维度/模型切换和延迟测试。
- 若启用模型画像提炼，验证线上模型结构输出、成本、敏感事实拒绝和 p95 可见时间。
- 在修复仓库现有 42 项非记忆回归后再次执行全量绿色门禁。

因此当前状态是：代码实施完成，核心与本地集成验证完成，生产依赖环境验证尚未完成。
