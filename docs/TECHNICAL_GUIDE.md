# CampusCare Python

CampusCare 是一个面向高校学生的 AI 陪伴与校务咨询原型。项目将流式对话、心理风险安全门、校园知识检索、可恢复澄清、多 Agent 协作、后台个案处置和可观测性整合在同一个 FastAPI 应用中；默认可接入本地 Ollama，也支持 OpenAI-compatible API。

## 核心能力

- 学生端 SSE 流式聊天，前端可展示打字机式输出。
- Basic Auth 登录，支持学生和管理员角色隔离。
- 事件驱动多 Agent 协作 runtime：Coordinator、Understanding、Safety、四个功能 Agent 和 Response 通过共享黑板、任务认领、依赖门禁和安全审查协作。
- 每轮先幂等持久化 USER 消息，再由唯一 `ContextBuilder` 装配单会话上下文；Understanding 生成五分类 `route_plan`，支持 `CHAT / ACADEMIC / CAMPUS / MENTAL / RISK` 和最多四个可依赖 workItem。
- 可恢复澄清：只有 Coordinator 能在功能 Agent 执行前选择白名单中的用户缺参；后续回复恢复原 `planId/workItemId/dependsOn/synthesisOrder`，新高风险信号始终优先打断澄清。
- 有界 AgenticRAG：原 query 完成 BM25/向量召回、等权 RRF、LLM Rerank、结构化 Grade 和确定性 Decide；仅对本地可检索缺口进行最多一次 Rewrite，第二轮后强制结束。
- 心理风险评估：高风险词典优先、LLM JSON 评估、关键词兜底。
- 后台报告：记录情绪标签、情绪分数、风险等级、置信度和摘要，但学生端不展示后台评估结果。
- 数据闭环：MySQL 保存会话、消息、Summary V2、显式用户记忆和 Trace Manifest；Redis 仅作为允许失败的短期缓存；高风险消息写入 Excel 台账并通过邮件发送预警。
- 上下文治理：统一近似 Token 预算、来源/hash/dropped reason 清单，以及 `SYSTEM_POLICY / APPLICATION_DIRECTIVE / REFERENCE_DATA / CURRENT_USER` 信任边界。
- 本地微调模型接入：支持通过 Ollama 加载 `mindbridge-qwen2.5-7b-ft-q4_k_m.gguf`。
- OpenAI-compatible API 接入：也可切换到云端模型。
- MCP 工具服务：只读对话 Server 暴露 `rag_search/get_current_weather`；风险写工具使用物理隔离的 Server、allowlist、timeout、熔断和幂等键。
- RAG 评测：181 条 golden set，分别执行 BM25、Hybrid、向量故障模式，并报告 Recall@5、MRR、NDCG@5、grade macro F1、provenance、过滤和 grounded-fact 门禁。

## 技术栈

```text
语言：Python
Web 框架：FastAPI
服务运行：Uvicorn / ASGI
数据库：MySQL，SQLAlchemy ORM，PyMySQL 驱动
短期记忆：Redis
配置管理：pydantic-settings，.env
AI 接入：Ollama，本地微调 GGUF 模型，OpenAI-compatible API，Mock Provider
Agent 编排：事件驱动黑板协作 runtime
RAG：Manifest 语料治理、BM25F、Ollama/OpenAI Embeddings、版本化 Chroma、RRF、LLM Rerank/Grade/Rewrite
流式输出：Server-Sent Events
文档解析：pypdf、Markdown 结构解析，可选 Docling
Excel 台账：openpyxl
邮件预警：SMTP / smtplib
前端：原生 HTML / CSS / JavaScript
认证：Basic Auth
工具协议：MCP
```

说明：当前 Python 版只保留事件驱动多 Agent runtime，入口在 `app/agents/event_driven_runtime.py`。共享返回类型定义在 `app/agents/result.py`。RAG 请求路径只读取 MySQL registry 的 ACTIVE collection，不创建索引、不写 embedding、不提交无关事务；单召回通道失败只进入 diagnostics，Grade 不可用时才返回 `DEGRADED`。

## 目录结构

```text
app/
├── agents/          # 事件驱动多 Agent runtime
├── api/             # FastAPI 路由
├── core/            # 配置、数据库、安全、启动初始化
├── knowledge/       # 内置校园心理知识库
├── mcp_tools/       # MCP 工具服务
├── models/          # SQLAlchemy 实体
├── rag_eval/        # RAG 评测脚本和数据集
├── schemas/         # Pydantic DTO
├── services/
│   ├── rag_pipeline.py       # 两轮有界 RAG 私有状态机
│   └── knowledge_ingestion/  # Catalog、解析器、Document IR、结构化分块
└── static/          # 原生前端页面

models/mindbridge-qwen2.5-7b-ft/
└── Modelfile        # Ollama 模型定义

scripts/
├── import_knowledge.py             # 同步 Manifest 语料
├── manage_knowledge_index.py       # 构建、验证和切换索引
├── reconcile_knowledge_corpus.py   # 对账受管语料
├── audit_knowledge_ingestion_cutover.py
├── run-dev.sh / start-ollama.sh
└── create-finetuned-model.sh / package-release.sh
```

## Agent loop

每轮对话默认进入事件驱动多 Agent 协作 runtime。Coordinator 维护共享黑板和任务板，专业 Agent 根据能力和置信度认领任务，发布 artifact，再由安全审查和最终采纳机制收敛输出：

```text
TURN_STARTED
-> CoordinatorAgent 创建理解与风险任务
-> UnderstandingAgent 发布 route_plan；SafetyAgent 发布 risk
-> Coordinator 对合法缺参发布唯一 clarification_request，或按 workItem fan-out
-> GeneralChatAgent 仅可见天气；Academic/Campus/Mental 仅可见本地 RAG
-> 上游失败时依赖项确定性结束为 FAILED/UPSTREAM_FAILED
-> Coordinator 收齐 specialist_result 后 fan-in
-> ResponseAgent 发布候选回复
-> SafetyAgent 审查候选回复
-> CoordinatorAgent FINAL_ACCEPTED
-> SSE 流式输出
```

各 Agent 分工：

- `CoordinatorAgent`：维护任务板、预算、安全门槛、冲突仲裁和最终采纳。
- `ContextBuilder`：唯一生产上下文入口，统一读取 Summary V2、近期消息、相关显式记忆、澄清状态和有限 Safety 元数据，并发布只读 turn-memory artifact。
- `UnderstandingAgent`：判断五类 Intent、workItem、依赖、已知参数和白名单缺参，发布严格 `route_plan` artifact。
- `SafetyAgent`：独立评估风险，必要时发布 `SAFETY_OVERRIDE`，并审查候选回复。
- `GeneralChatAgent`：普通对话与编程帮助；模型可自主选择天气 MCP，但看不到 RAG。
- `AcademicPlanningAgent`、`CampusAffairsAgent`、`PsychologicalSupportAgent`：私有 AgentLoop 只可见本地 `rag_search`，看不到天气和外网工具。
- `SkillManager`：动态加载、校验和匹配本地受信任 Skill；最终与其他上下文一起进入统一 Token 预算。
- `ResponseAgent`：消费同一 Context Packet 的 Response 视图、专业 Agent 结果和知识证据，经 `ContextBuilder.build_synthesis_prompt` 生成候选回复 Prompt，等待安全审查和采纳。当前不直接重新匹配 Skill，场景 Skill 在专业 Agent 执行时注入。

## 快速开始

推荐使用 Docker Compose 启动完整依赖。应用容器会自动执行 Alembic 迁移、同步 Manifest 知识语料，再启动 Uvicorn。默认聊天链路要求宿主机运行 Ollama 并准备 `qwen3:8b`；构建或启用 Hybrid 向量索引时还需要 `bge-m3:latest`：

```bash
cp .env.example .env
docker compose up -d --build
curl http://127.0.0.1:8080/actuator/health
```

浏览器访问 `http://127.0.0.1:8080/`。开发环境会自动创建以下演示账号：

```text
学生端：student / student123
管理端：admin / admin123
```

这些是演示凭据，不能直接用于生产环境。学生页面位于 `/student.html`，管理页面位于 `/admin.html`。

## 本地开发

建议使用 Python 3.10 至 3.12，并先启动 MySQL、Redis 和所选模型服务：

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
python scripts/import_knowledge.py
uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload
```

`requirements.txt` 已包含：

```text
chromadb
pymysql
redis
```

`AGENT_FRAMEWORK` 仍会读取环境变量，但当前只支持 `event_driven_multi_agent`。历史值或未知值会在状态接口中标记为 fallback，并实际使用事件驱动 runtime。

## MySQL 和 Redis 配置

系统默认使用 MySQL 保存完整业务数据和完整聊天消息，使用 Redis 保存短期对话记忆。启动服务前先创建数据库：

```sql
CREATE DATABASE mindbridge DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'mindbridge'@'%' IDENTIFIED BY 'mindbridge';
GRANT ALL PRIVILEGES ON mindbridge.* TO 'mindbridge'@'%';
FLUSH PRIVILEGES;
```

首次部署在空库执行：

```bash
alembic upgrade head
python scripts/import_knowledge.py
```

当前唯一迁移 head 是 `0015_three_layer_memory_v3`。`0014_five_intent_route_v3` 完成五类 Intent 切换；`0015` 增加情景记忆、画像状态、持久记忆任务及 UserMemory 版本治理字段。升级后先同步 Manifest，再离线构建索引：

```bash
python -m alembic heads
python scripts/import_knowledge.py
python scripts/reconcile_knowledge_corpus.py
python scripts/manage_knowledge_index.py build
python scripts/manage_knowledge_index.py verify
python scripts/manage_knowledge_index.py shadow
python scripts/manage_knowledge_index.py activate
```

从旧版 `create_all()` 数据库升级时，先备份数据库并确认旧结构与 `0001_current_schema_baseline` 一致，然后执行：

```bash
alembic stamp 0001_current_schema_baseline
alembic upgrade head
python scripts/import_knowledge.py
```

知识索引异常时优先执行 `python scripts/manage_knowledge_index.py rollback`，不删除 collection。V2 Chunk 异常时把目标文档切回 `legacy_char_v1`，重新生成并验证 READY 索引后再激活；解析器异常时改用 `pypdf_fast` 或旧 Manifest 专用解析器。管理员端故障期间可继续使用兼容的 `POST /api/admin/knowledge` 和 `POST /api/admin/knowledge/file`。

数据库迁移需要回滚时，先回滚应用版本并备份数据库、`KNOWLEDGE_ARTIFACT_DIR`、解析产物和审计记录，再执行目标 revision 的 downgrade。不得删除上传原件或历史 Artifact。应用启动不再用 `create_all()` 替代数据库升级；`create_schema()` 只保留给全新测试库。

`.env` 中配置连接：

```env
DATABASE_URL=mysql+pymysql://mindbridge:mindbridge@127.0.0.1:13306/mindbridge?charset=utf8mb4
REDIS_URL=redis://127.0.0.1:16379/0
REDIS_MEMORY_TTL_SECONDS=86400
REDIS_MEMORY_MAX_MESSAGES=40
CONTEXT_INPUT_MAX_TOKENS=28672
CONTEXT_RECENT_MESSAGE_LIMIT=8
CONTEXT_SUMMARY_MAX_TOKENS=700
CONTEXT_USER_MEMORY_MAX_ITEMS=5
CONTEXT_USER_MEMORY_MAX_TOKENS=500
CONTEXT_KNOWLEDGE_MAX_TOKENS=2500
CONTEXT_SAFETY_MAX_AGE_HOURS=72
TRACE_INCLUDE_PROMPT_CONTENT=false
```

ResponseAgent 使用独立生成预算，只有 provider 的语义终止原因是 STOP 才标记完成；首次 LENGTH 最多续写一次，两次仍不完整则失败且不写正式 Assistant 历史：

```env
OLLAMA_NUM_CTX=32768
AGENT_MODEL_DEFAULT_PROVIDER=ollama
AGENT_MODEL_DEFAULT_MODEL=qwen3:8b
AGENT_MODEL_RESPONSE_PROVIDER=ollama
AGENT_MODEL_RESPONSE_MODEL=qwen3:8b
AGENT_MODEL_RESPONSE_TEMPERATURE=0.25
AGENT_MODEL_RESPONSE_MAX_TOKENS=1536
AGENT_MODEL_RESPONSE_THINK=false
CHAT_RESPONSE_MAX_CONTINUATIONS=1
CHAT_RESPONSE_MAX_TOTAL_TOKENS=3072
```

`CHAT_HISTORY_LIMIT`、`MEMORY_COMPACTION_ENABLED`、`MEMORY_COMPACTION_RECENT_MESSAGES`、`MEMORY_SUMMARY_MAX_CHARS`、`PROMPT_TOTAL_MAX_CHARS` 和 `PROMPT_KNOWLEDGE_MAX_CHARS` 仅保留一个发布周期用于兼容旧环境变量，V2 生产链路不再读取它们。

完整聊天记录写入 MySQL 的 `chat_sessions`、`chat_messages` 等表。当前 USER 消息会在 Agent 运行前与 ChatTurn 原子关联；同一 `requestId` 重放不会创建第二条消息。Redis 只缓存每个会话最近 `REDIS_MEMORY_MAX_MESSAGES` 条短期上下文，并通过 `REDIS_MEMORY_TTL_SECONDS` 自动过期；miss、旧缓存或异常都会回退 MySQL，并在 Context Manifest 标记降级。

Summary 新写 schema v2，旧 JSON 只在内存中兼容转换。显式用户记忆只响应“请记住”等 opt-in 表达，使用中文 bigram/英文 token 相关性选择，并支持用户查看和软删除。摘要、记忆和 RAG 正文均作为转义后的 `REFERENCE_DATA`，不会被提升为裸 `system` 指令。

## Docker Compose 一键启动

仓库提供 `Dockerfile` 和 `docker-compose.yml`，会启动：

- `mysql`：MySQL 8.0，容器内端口 `3306`，宿主机映射 `13306`
- `redis`：Redis 7.2 Alpine，容器内端口 `6379`，宿主机映射 `16379`
- `app`：CampusCare FastAPI 服务，宿主机端口 `8080`

默认配置会让应用容器访问宿主机 Ollama：

```bash
docker compose up -d --build
```

如果 Ollama 已经有下列模型，容器即可使用真实本地聊天模型链路：

```text
qwen3:8b
bge-m3:latest
```

## Chroma 向量库与快照

应用启动时按 `app/knowledge/knowledge_manifest.yaml` 校验原始文件 SHA-256 并同步受管语料。Manifest 是受管文档元数据的唯一声明来源；移除的 MANIFEST 文档会软归档为 INACTIVE，管理员上传默认是 DRAFT，不会直接进入可用证据。

默认 embedding provider 是本机 Ollama `bge-m3:latest`。物理 collection 名称包含 corpus、taxonomy、provider、模型 digest、维度和 searchable-text schema 的签名；MySQL `knowledge_index_registry` 保存多实例共享的 ACTIVE/READY/previous 指针：

```env
KNOWLEDGE_VECTOR_ENABLED=true
KNOWLEDGE_VECTOR_REQUIRED=false
KNOWLEDGE_EMBEDDING_PROVIDER=ollama
KNOWLEDGE_EMBEDDING_MODEL=bge-m3:latest
KNOWLEDGE_EMBEDDING_BASE_URL=http://localhost:11434
KNOWLEDGE_VECTOR_COLLECTION_BASE=mindbridge_knowledge_v3
KNOWLEDGE_CANDIDATE_K=48
RAG_MODEL_PROVIDER=ollama
RAG_MODEL=qwen3:8b
RAG_PER_LIST_CANDIDATE_K=40
RAG_FUSED_CANDIDATE_LIMIT=24
RAG_GRADE_EVIDENCE_LIMIT=8
CHROMA_PERSIST_DIR=data/chroma
CHROMA_SNAPSHOT_DIR=data/chroma-snapshots
```

索引维护只能通过离线命令完成。`build` 校验真实模型 digest、生成新物理 collection 并停在 READY；`shadow` 不影响线上回答；只有 shadow 通过后才能 `activate`：

```bash
python scripts/manage_knowledge_index.py build --batch-size 32
python scripts/manage_knowledge_index.py verify
python scripts/manage_knowledge_index.py status
python scripts/manage_knowledge_index.py shadow
python scripts/manage_knowledge_index.py activate
python scripts/manage_knowledge_index.py rollback
```

管理员状态接口 `GET /api/admin/knowledge/status` 展示 ACTIVE、READY、previous、embedding backend 和降级原因。RAG 始终在同一批治理合格 chunk 上尝试 BM25 与向量召回；向量故障只标记 diagnostics，不会伪造证据。

## 知识入库 V2

Manifest 与管理员上传共用 Catalog、ArtifactStore 和 Pipeline。原件默认保存到 `data/knowledge-artifacts`；管理员上传先创建数据库任务并进入 DRAFT/PROCESSING，后台 worker 可在进程重启后恢复未完成任务。结构化分割默认使用 `structure_token_v2`，`legacy_char_v1` 仅用于兼容回放和回滚：

```env
KNOWLEDGE_ARTIFACT_DIR=data/knowledge-artifacts
KNOWLEDGE_INGESTION_WORKER_ENABLED=true
KNOWLEDGE_INGESTION_POLL_INTERVAL_SECONDS=1
KNOWLEDGE_INGESTION_BATCH_SIZE=2
KNOWLEDGE_INGESTION_MAX_ATTEMPTS=3
KNOWLEDGE_CHUNKING_PROFILE=structure_token_v2
KNOWLEDGE_LEGACY_MARKDOWN_BOOTSTRAP_ENABLED=false
```

管理员页面支持文档列表、元素与 Chunk 预览、元数据编辑、重处理、发布、下线、任务轮询和失败重试。对应 API 为：

```text
POST  /api/admin/knowledge/documents
GET   /api/admin/knowledge/documents
GET   /api/admin/knowledge/documents/{document_id}
GET   /api/admin/knowledge/documents/{document_id}/preview
PATCH /api/admin/knowledge/documents/{document_id}
POST  /api/admin/knowledge/documents/{document_id}/reprocess
POST  /api/admin/knowledge/documents/{document_id}/publish
POST  /api/admin/knowledge/documents/{document_id}/deactivate
GET   /api/admin/knowledge/jobs/{job_id}
POST  /api/admin/knowledge/jobs/{job_id}/retry
```

关闭旧 Markdown 启动扫描前必须运行只读门禁：

```bash
python scripts/audit_knowledge_ingestion_cutover.py --require-ready
```

只有报告中 `eligibleToDisableLegacyBootstrap=true`，并且旧 Markdown 已登记、管理员上传已迁移、回滚演练通过后，才能把 `KNOWLEDGE_LEGACY_MARKDOWN_BOOTSTRAP_ENABLED` 改为 `false`。2026-08-03 的正式审计已满足上述条件，当前默认值已关闭；如需历史回放，可临时显式设为 `true`。审计不会清理数据；清理重复 ACTIVE 文档前必须另行导出报告并备份。

## 工具队列、限流与死信

心理报告生成后，工具链不会阻塞学生端流式回复，而是写入 `tool_jobs` 队列表：

```text
EXCEL_REPORT
CASE_CREATE -> ALERT_SEND
```

Excel 写入使用进程内锁串行化，个案创建保持幂等；预警发送使用独立线程池并支持每分钟限流。失败任务会按延迟重试，超过 `TOOL_QUEUE_MAX_ATTEMPTS` 后进入 `dead_letter_records`。

```env
TOOL_QUEUE_ENABLED=true
TOOL_QUEUE_EXCEL_WORKERS=1
TOOL_QUEUE_EMAIL_WORKERS=2
ALERT_EMAIL_RATE_LIMIT_PER_MINUTE=30
ALERT_EMAIL_DELIVERY_MODE=log
```

`ALERT_EMAIL_DELIVERY_MODE=log` 适合本地演示；生产发邮件时改为 `smtp` 并配置 SMTP。

## 邮件预警配置

高风险消息会触发心理报告，并由后端通过 MCP 工具调用完成 Excel 台账写入和邮件预警。发送邮件前需要在 `.env` 中配置 SMTP：

```env
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=your-account@example.com
SMTP_PASSWORD=your-smtp-password
SMTP_USE_TLS=true
SMTP_USE_SSL=false
ALERT_EMAIL_FROM=your-account@example.com
ALERT_EMAIL_TO=counselor@example.com,admin@example.com
ALERT_EMAIL_SUBJECT_PREFIX=[CampusCare 高风险预警]
```

未配置 SMTP 或收件人时，系统不会中断聊天流程，但会在 `alert_records` 中写入 `FAILED` 记录，提示缺少的配置项。

## 接入本地微调 GGUF 模型

Python 版默认预留本地模型名：

```text
mindbridge-qwen2.5-7b-ft:latest
```

模型目录：

```text
models/mindbridge-qwen2.5-7b-ft/
```

需要放入的 GGUF 权重：

```text
models/mindbridge-qwen2.5-7b-ft/mindbridge-qwen2.5-7b-ft-q4_k_m.gguf
```

如果本机已经有其他位置的 GGUF 模型文件，可以通过 `UPSTREAM_GGUF` 指定路径并建立软链接：

```bash
UPSTREAM_GGUF=/path/to/mindbridge-qwen2.5-7b-ft-q4_k_m.gguf ./scripts/create-finetuned-model.sh
```

创建 Ollama 模型：

```bash
./scripts/create-finetuned-model.sh
```

启动 Ollama：

```bash
./scripts/start-ollama.sh
```

启动 Python 服务：

```bash
AI_PROVIDER=ollama ./scripts/run-dev.sh
```

查看模型接入状态：

```bash
curl -u student:student123 http://127.0.0.1:8080/api/agent/status
```

返回结果中的 `finetunedModel.ggufExists` 和 `finetunedModel.modelfileExists` 会显示模型资产是否就绪。
同时 `agentFramework.active` 会显示当前实际使用的 Agent 编排框架：

```text
event_driven_multi_agent
```

## 接入 OpenAI-compatible API

```bash
AI_PROVIDER=openai \
OPENAI_API_KEY=你的_API_Key \
OPENAI_MODEL=gpt-4o-mini \
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

OpenAI 流式请求默认发送 `stream_options.include_usage=true`，用于获取供应商返回的精确流式 Token 用量。不支持该参数的 OpenAI-compatible 服务可设置 `OPENAI_STREAM_INCLUDE_USAGE=false`，系统会明确标记估算或不可用，且不会静默重试生成。

如需让知识向量也使用 OpenAI-compatible embeddings，显式切换独立 provider 配置，并按前述流程构建新签名索引：

```env
KNOWLEDGE_VECTOR_ENABLED=true
KNOWLEDGE_VECTOR_REQUIRED=false
KNOWLEDGE_EMBEDDING_PROVIDER=openai
KNOWLEDGE_EMBEDDING_MODEL=text-embedding-3-small
KNOWLEDGE_EMBEDDING_BASE_URL=https://api.openai.com/v1
KNOWLEDGE_VECTOR_COLLECTION_BASE=mindbridge_knowledge_v3
KNOWLEDGE_CANDIDATE_K=48
CHROMA_PERSIST_DIR=data/chroma
```

OpenAI embedding 使用 `OPENAI_API_KEY`。provider、model 或索引维度变化都要求新建 READY collection；不能在请求路径原地覆盖 ACTIVE collection。

## 调用示例

学生流式聊天：

```bash
curl -N -u student:student123 \
  -H 'Content-Type: application/json' \
  -d '{"requestId":"00000000-0000-4000-8000-000000000001","message":"我最近很焦虑，晚上总是睡不着"}' \
  http://127.0.0.1:8080/api/chat/stream
```

高风险示例，会触发心理报告、风险个案创建和预警工具计划；Excel 保留为台账输出，邮件/log 是预警通道之一：

```bash
curl -N -u student:student123 \
  -H 'Content-Type: application/json' \
  -d '{"requestId":"00000000-0000-4000-8000-000000000002","message":"我不想活了，感觉撑不下去了"}' \
  http://127.0.0.1:8080/api/chat/stream
```

学生查看和删除自己明确保存的长期记忆：

```bash
curl -u student:student123 http://127.0.0.1:8080/api/user/memories
curl -X DELETE -u student:student123 \
  http://127.0.0.1:8080/api/user/memories/<memoryId>
```

管理员查看报告：

```bash
curl -u admin:admin123 http://127.0.0.1:8080/api/admin/reports
```

管理员追加知识库：

```bash
curl -u admin:admin123 \
  -H 'Content-Type: application/json' \
  -d '{"source":"sleep-guide","content":"失眠时可先固定起床时间，减少睡前屏幕刺激，必要时联系校心理中心。"}' \
  http://127.0.0.1:8080/api/admin/knowledge
```

追加知识库只写入 MySQL 元数据和分块，文档默认是 DRAFT。审核并激活语料后，必须通过 `manage_knowledge_index.py build/verify/shadow/activate` 离线生成并切换新索引；请求路径不会写 embedding 或重建 Chroma。

推荐使用管理页面或 `POST /api/admin/knowledge/documents` 创建可追踪的异步入库任务。上述 JSON 接口保留给兼容调用；新流程完成预览和审核后，再调用文档 `publish` API 发布。

## RAG 评测

Knowledge Evidence V3 会按子问题保留证据归属，并区分申请截止时间（`DEADLINE`）与提交后的办理时长（`PROCESSING_TIME`）。复合问题只有部分事实得到本地证据支持时，系统以 `PARTIAL` 回答已核实部分并明确列出缺失 facet；不会再次询问输入中已给出的校区，也不会用申请时间窗口推断办理工作日。Evidence Grader 每题最多读取 3 个证据块、全局最多读取 8 个唯一证据块；首轮异常判空最多触发一次 focused review，且与第二轮 Query Rewriter 互斥。

```bash
python -m app.rag_eval.runner
python -m app.rag_eval.runner --mode bm25
python -m app.rag_eval.runner --mode hybrid
python -m app.rag_eval.runner --mode vector-fault
```

默认测试与 `--suite all` 使用确定性 provider，不调用真实模型。显式真实模型质量验证使用独立数据库并设置：

```bash
RUN_LIVE_LLM_TESTS=1 KNOWLEDGE_VECTOR_REQUIRED=true python -m pytest -m live_llm -q
```

默认命令执行全部三种模式并启用硬门禁。Hybrid 评测前必须有与当前 embedding 配置匹配的 ACTIVE collection。评测报告输出到：

```text
target/rag-eval-report.json
```

## 单元测试

当前 `tests/` 里的测试主体使用 Python 标准库 `unittest` 组织，并由 pytest 执行完整回归：

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

## Agent Runtime Harness

线上对话通过 `MindBridgeAgentHarness` 组织一次 Agent run。Harness 不改变事件驱动 runtime 内部的多 Agent 协作方式，而是在外层统一管理：

- 校验已由 ChatTurn 原子持久化并关联的当前 USER 消息。
- 输入脱敏、澄清恢复和 ContextBuilder 注入。
- Agent runtime 调用和多 Agent 协作结果接入。
- 心理报告落库和工具计划生成。
- 助手消息持久化和 Summary V2 增量更新。
- Agent steps、知识召回摘要、风险结果和 Context Manifest 等 trace 数据输出。

因此 HTTP 层只负责认证和 SSE 流式输出，Agent 后处理逻辑集中在 runtime harness 内。

Trace 默认只保存来源 ID、预算、hash、Token 估算、降级与 dropped reasons，不复制完整长期记忆、知识正文或最终 Prompt。`TRACE_INCLUDE_PROMPT_CONTENT=true` 仅用于受控调试环境，并只在最终 response message 记录一份正文。

## Engineering Harness

项目提供一键工程 harness，用 mock AI、临时 SQLite、内存短期记忆和本地输出验证核心链路：

- Risk Safety Harness：高风险识别、报告生成、后台元数据不外显、工具队列入队。
- Agent Routing Harness：通过 `MindBridgeAgentHarness` 验证五分类、workItem fan-out/fan-in 和高风险抢占。
- Clarification Harness：验证学习计划参数提取、受控追问、跨轮恢复以及高风险中断。
- Standard Skills Harness：验证 `skills/*/SKILL.md` 标准 Skill 加载、选择逻辑和交接摘要模板渲染。
- RAG Harness：验证 BM25/向量 → RRF → Rerank → Grade → Decide → 单次 Rewrite，并运行关键知识召回回归。
- Turn Metrics Harness：验证端到端 TTFT、全链路 Token、续写、失败、幂等和 Trace 一致性。
- API Harness：健康检查、认证授权、SSE 聊天、管理员知识库接口。
- Tool Queue Harness：Excel / case / alert 依赖、幂等、限流和 dead letter。

```bash
python -m app.harness.runner --suite clarification
python -m app.harness.runner --suite metrics
python -m app.harness.runner --suite all
```

每个新 ChatTurn 的终态 generation JSON 使用 schema v2，并在 `turnMetrics` 中保存服务端端到端 TTFT、首个内容快照准备时间、整轮耗时、供应商调用明细及全链路 Token 汇总。顶层 `promptTokens`、`outputTokens` 和 `durationMs` 仍仅表示最终回答生成；`turnMetrics.tokenUsage` 才表示本轮所有 Agent 与最终回答调用的合计。指标不保存 Prompt、用户原文、模型正文或知识正文。

报告输出到：

```text
target/harness/harness-report.json
target/harness/rag-eval-report.json
```

## MCP 工具服务

MCP Python 包建议使用 Python 3.10 或 3.11 安装运行。

```bash
python -m app.mcp_tools.server
```

业务后端触发报告后处理时，默认通过异步工具队列复用同一套工具实现；关闭队列后会作为 MCP client 通过 stdio 启动同一个 MCP server。

## 三层记忆 V3

V3 使用 Redis 保存按用户与会话隔离的近期消息，MySQL 保存原始消息、累计摘要、情景片段副本、画像事实与持久任务，独立 Chroma collections 保存情景摘要和画像向量。Harness 每轮只调用一次 `ContextBuilder.build_base_context()`；Runtime、专业 Agent、Response 与 Safety 复用同一 `snapshotId`。

首次启用前先备份 MySQL 和 `data/memory-chroma`，确认当前 revision 后升级：

```bash
python -m alembic heads
python -m alembic current
python -m alembic upgrade head
```

关键配置及初始值如下。这些值是预算上限和触发阈值，不代表已经达到特定性能收益：

```text
MEMORY_V3_ENABLED=true
MEMORY_EPISODIC_ENABLED=true
MEMORY_PROFILE_EXTRACTION_ENABLED=true
MEMORY_WORKER_ENABLED=true
MEMORY_CHROMA_MODE=persistent
MEMORY_CHROMA_PATH=data/memory-chroma
MEMORY_EMBEDDING_PROVIDER=ollama
MEMORY_EMBEDDING_MODEL=bge-m3:latest
MEMORY_EMBEDDING_VERSION=bge-m3-v1
MEMORY_HISTORY_CANDIDATE_K=8
MEMORY_HISTORY_TOP_K=3
MEMORY_HISTORY_MAX_TOKENS=800
MEMORY_WORKING_MAX_TOKENS=2200
MEMORY_BASE_MAX_TOKENS=4200
MEMORY_COMPACTION_MIN_MESSAGES=4
MEMORY_COMPACTION_MAX_DELTA_TOKENS=1500
MEMORY_IDLE_FINALIZE_SECONDS=1800
MEMORY_PROFILE_MIN_CONFIDENCE=0.85
MEMORY_READ_DEADLINE_MS=800
```

单机应用使用 `persistent` 并把 `MEMORY_CHROMA_PATH` 挂载到持久卷。多实例或独立 worker 必须统一使用 `http` 模式并配置同一个 Chroma 服务；生产不会在远端故障时自动切到各实例自己的本地目录。

历史回填默认只预览。先检查数量，再显式执行；`--enqueue-history` 会为原始会话尾部创建持久收尾任务：

```bash
python -m scripts.backfill_memory_v3 --limit 1000
python -m scripts.backfill_memory_v3 --apply --enqueue-history --limit 1000
python -m scripts.backfill_memory_v3 --apply --user-id 123 --enqueue-history
```

回退读取时先设置 `MEMORY_V3_ENABLED=false`、`MEMORY_PROFILE_EXTRACTION_ENABLED=false` 和 `MEMORY_WORKER_ENABLED=false`，重启后保留新表与 Chroma 数据以便恢复。应用版本回退并确认没有旧版本进程写入后，才可执行 `alembic downgrade 0014_five_intent_route_v3`；降级会删除 V3 派生表，因此必须先备份。Redis 或 Chroma/Embedding 不可用时读取会回退到同用户 SQL 数据；任务保持 PENDING 并按退避重试，不会把索引失败伪装为成功。

暴露工具：

- `mindbridge_excel_report`
- `mindbridge_case_create`
- `mindbridge_alert_send`
- `mindbridge_alert_ack`
- `mindbridge_case_note_add`
- `mindbridge_alert_notify`

内置标准 Skills 位于 `skills/*/SKILL.md`，运行时由 `MindBridgeSkillRegistry` 加载：

- `supportive_response_baseline`：心理咨询与风险回复的基础共情、边界和学生端表达规则。
- `high_risk_safety_plan`：高风险时引导模型优先完成短期安全计划。
- `anxiety_grounding_support`：焦虑、惊恐、崩溃场景的稳定化和 grounding 指引。
- `sleep_routine_support`：失眠、睡眠节律紊乱场景的安全睡眠建议。
- `academic_stress_planning`：考试、作业、论文、绩点压力的下一步拆解。
- `referral_resource_guidance`：校内心理中心、辅导员、可信任支持人和紧急资源转介。
- `counselor_handoff_summary`：生成给辅导员/管理员看的个案交接摘要模板。

Skill 加载与匹配约定：

- `SkillManager` 初始化或显式 `refresh()` 时扫描文件；当前没有文件监听式热更新。支持读取带或不带 BOM 的 UTF-8 文件，读取、编码、YAML 和已校验字段错误统一进入 `SkillLoadError` 隔离路径。未迁移到 `matching_version: 2` 的自定义 Skill 仍可命名读取，但不会进入动态选择。
- `selection_mode` 明确区分 `baseline`、`scenario` 和 `fixed`。先按 enabled、Agent、intent 与风险硬过滤；Agent name 与 Skill name 不参与评分或向量相似度。`fixed` 只保留命名读取，baseline 独立处理。
- scenario 使用 `60 × D + 30 × G + 10 × C` 的规则分。默认达到 80 分才有资格；同组多项达标且前两项相差至少 5 分时直接选第一项，分差不足时只把高分近邻交给 Embedding。低分、排除、禁用和越域候选不会被语义阶段召回。
- Embedding 仅做高分近邻消歧，默认要求原始余弦至少 0.70 且前两项分差至少 0.05。不可用、超时、数量或维度错误、零向量及非有限数均拒选整个竞争组；不回退 priority 或规则次高项。Skill embedding 有独立 provider/model/base URL/version 配置，不依赖知识向量开关。
- 默认最多注入两个 scenario。`max_chars` 和 `total_chars` 都计入名称前缀与连接符；scenario 正文只会完整装入或整项移除，不截断 Markdown 流程。专业 Agent 的 `selectedSkillIds` 只记录实际进入 prompt 的项目。
- 按名称读取必需 Skill 时隔离无关文件错误，但目标缺失、目标无效或名称重复仍显式失败。交接摘要要求完整且非空的 text 代码块；模板字段只替换一次，字段值中的占位符字样按原文保留。
- `skillSelection` 以结构化白名单进入 SpecialistResult 和 trace，但不会进入其他专业 Agent 或 Response 的模型上下文。高风险主链路由 Coordinator、固定安全指令和审查机制控制，不依赖可选 Skill 恰好被选中。

Skill 选择初始配置（这些是专家设定的门槛，不是概率或已评测准确率）：

```env
SKILL_SCENARIO_ENABLED=true
SKILL_RULE_MIN_POINTS=80
SKILL_RULE_MIN_MARGIN_POINTS=5
SKILL_SEMANTIC_ENABLED=true
SKILL_SEMANTIC_MIN_SIMILARITY=0.70
SKILL_SEMANTIC_MIN_MARGIN=0.05
SKILL_SEMANTIC_BUDGET_MS=3000
SKILL_EMBEDDING_PROVIDER=ollama
SKILL_EMBEDDING_MODEL=bge-m3:latest
SKILL_EMBEDDING_BASE_URL=http://localhost:11434
SKILL_EMBEDDING_VERSION=v1
SKILL_MAX_MATCHES=2
SKILL_TOTAL_CHARS=5000
```

## Understanding 语义规划

生产路由使用单一 RoutePlan V3 路径，只包含 `CHAT`、`ACADEMIC`、`CAMPUS`、`MENTAL`、`RISK` 五类 Intent。当前轮高风险先硬抢占；除此之外，每个进入新路由的普通请求都调用一次共享 Understanding V3 结构化服务，同时给出上下文关系、原文分段、五类候选和依赖提示。每个分段再按固定的 LLM/Embedding/Rule 权重融合，最终 ID、参数、目标、DAG 与拓扑顺序由确定性代码生成。

`HARD_DATA` 表示下游必须消费上游结果，会生成 `dependsOn`；`ORDER_ONLY` 只影响 `synthesisOrder`。单纯的“先 A 再 B”不会自动创建硬依赖。模型失败、超时或结构校验失败时，整份回退到同轮规则草案；只有该失败路径实际拆出超过四个独立目标时才进入容量回退。生产请求不计算 shadow 计划，也没有规则快速旁路、旧新双实现或路由调参开关。

唯一正式路由数据集是 `app/evaluation/datasets/routing-v3.jsonl`，唯一正式 suite 是 `routing`。真实 Understanding 与统一 Embedding 后端评测必须显式开启；未开启或依赖不可用时报告状态为 `NOT_RUN`，不会用规则结果冒充模型指标：

```bash
RUN_ROUTING_MODEL_EVAL=1 python -m app.evaluation.runner --suite routing --profile full
```
