# CampusCare

面向高校场景的 AI 助手，支持校园问答、学习规划、日常交流和心理支持，提供学生聊天界面与管理员后台。

CampusCare 将校园知识库、连续对话和后台跟进放在同一个应用中。学生可以查询办事流程、讨论学习安排，也可以继续之前的对话；管理员可以维护校园资料，查看咨询报告并跟进风险个案。

支持通过 Ollama 运行本地模型，也可以接入兼容 OpenAI 的 API。后端使用 Python 和 FastAPI，前端使用原生 HTML、CSS 和 JavaScript，可通过 Docker Compose 部署。

[功能介绍](#功能介绍) · [使用示例](#使用示例) · [快速启动](#快速启动) · [实现方式](#实现方式) · [开发文档](#开发文档)

## 功能介绍

### 校园知识问答

导入学生手册、办事指南等资料后，可以围绕奖助学金、学籍管理、校园服务等内容提问。回答时先检索相关资料，再交给模型组织回复。

管理员可在后台上传 PDF、Markdown 和 TXT 文件，设置所属领域、校区、版本及有效期，查看文档处理状态和分块预览，并重试失败的处理任务。检索结合关键词和向量召回，经过结果融合、重排与证据评估；资料不足时最多补充一次查询。

### 学习规划与日常交流

支持围绕课程、考试和学习安排进行讨论，也可以进行日常聊天和压力疏导。系统会区分聊天、学业、校务、心理支持与风险五类需求，由对应的 Agent 处理。

一条消息中包含多个需求时，系统可以拆分任务并处理它们之间的依赖。缺少必要信息时会先追问，用户补充后继续处理原来的问题。

### 会话与记忆管理

学生端支持新建会话、查看历史消息和归档会话，回复通过 SSE 流式显示。归档后，会话会从学生端列表中移除，后台仍按审计规则保留数据。

连续对话会使用近期消息和会话摘要。用户明确要求记住的长期信息单独保存，可在“我的记忆”中查看和删除；删除后不再用于后续对话。

### 风险识别与个案跟进

系统会评估输入中的风险信号，并在最终回复前进行安全审查。情绪、风险等级和摘要等评估信息展示在管理端，不作为学生端聊天标签显示。

管理员可查看咨询报告、审阅关联会话、确认预警并添加跟进记录。项目还提供 Excel 台账和邮件预警功能，默认邮件模式仅记录日志，配置后才会实际发送。

### 模型与工具接入

模型可选择本地 Ollama 或兼容 OpenAI 的服务；支持为不同 Agent 配置模型，也提供 Mock Provider 供开发和测试使用。

知识检索、天气查询等能力通过 MCP 工具接入。不同 Agent 只能使用分配给自己的工具，风险记录写入工具与聊天侧只读工具分开管理。场景 Skill 保存在本地 Markdown 文件中，可按具体业务调整处理指引。

## 使用示例

下面是部署后可以尝试的几组提问，校园事务类问题需要先导入相应资料。

| 场景 | 示例 | 可以观察的功能 |
| --- | --- | --- |
| 查询校园规定 | “申请助学金需要准备哪些材料？” | 从校园资料中检索相关内容 |
| 补充问题条件 | “我想了解转专业。” → 补充年级、专业等信息 | 缺参追问与上下文衔接 |
| 安排学习任务 | “周五考数学，周日考英语，每天有三小时，帮我安排复习。” | 根据目标与时间约束给出建议 |
| 处理多个需求 | “查一下奖学金申请条件，再帮我安排这周的复习。” | 校务咨询与学习规划分别处理 |
| 保存个人偏好 | “请记住，我更喜欢晚上学习。” | 在“我的记忆”中查看或删除已保存的信息 |

## 快速启动

下面以 Docker Compose 和本地 Ollama 为例。需要先安装 Docker、Docker Compose 和 Ollama。

### 1. 准备模型

安装并启动 Ollama，在宿主机终端执行：

```bash
ollama pull qwen3:8b
ollama pull bge-m3:latest
```

### 2. 配置并启动

在项目根目录复制环境配置：

```bash
# Linux / macOS
cp .env.example .env
```

```powershell
# Windows PowerShell
Copy-Item .env.example .env
```

```bash
docker compose up -d --build
curl http://127.0.0.1:8080/actuator/health
```

默认 Compose 配置通过 `host.docker.internal` 访问宿主机 Ollama。Linux 环境需配置相应宿主机映射；容器也需要能够连接 Ollama 的监听地址。

### 3. 打开工作台

| 入口 | 本地地址 | 开发演示账号 |
| --- | --- | --- |
| 登录页 | http://127.0.0.1:8080/ | 选择对应角色登录 |
| 学生端 | http://127.0.0.1:8080/student.html | `student / student123` |
| 管理端 | http://127.0.0.1:8080/admin.html | `admin / admin123` |

首次部署会执行数据库迁移和知识语料同步。需要启用向量检索时，继续按照[技术文档](docs/TECHNICAL_GUIDE.md)构建、验证并激活知识索引。默认邮件投递模式为 `log`，实际邮件发送需另行配置。

## 实现方式

对话入口位于 FastAPI 服务。每一轮先由 `ContextBuilder` 整理消息、摘要、记忆与澄清状态，再由 `Coordinator` 组织任务。`Understanding` 负责理解需求，`Safety` 负责风险评估，专业 Agent 完成各自的任务，最后由 `Response` 汇总，经过审查后返回前端。

这部分实现主要考虑了几个问题：

- **上下文从哪里来**：统一组装上下文，记录来源并控制 Token 预算，避免各个 Agent 分别拼接历史消息。
- **多个任务如何执行**：用共享黑板和任务板记录进度，按照任务依赖执行，再汇总结果。
- **检索何时结束**：BM25 与向量召回后进行 RRF 融合、LLM 重排和证据评估；最多检索两轮、改写一次查询。
- **工具失败如何处理**：为工具调用设置权限范围、超时、熔断和幂等控制。

| 部分 | 技术 |
| --- | --- |
| 服务端 | Python、FastAPI、Pydantic、SQLAlchemy、Alembic |
| 前端 | HTML、CSS、JavaScript、SSE、Markdown 渲染 |
| 模型 | Ollama、OpenAI-compatible API |
| 检索 | BM25、Chroma、RRF、LLM 重排与评估 |
| 存储 | MySQL 保存业务数据，Redis 提供短期缓存 |
| 工具与场景配置 | MCP、本地 Markdown Skill |
| 部署与验证 | Docker Compose、pytest / unittest、GitHub Actions |

## 代码导航

```text
app/
├── agents/           # Agent 协作与任务执行
├── api/              # API 路由
├── core/             # 配置、数据库与安全
├── knowledge/        # 校园知识语料与 Manifest
├── mcp_tools/        # 工具服务
├── services/         # 对话、记忆、RAG 与知识入库
├── evaluation/       # 评测配置与数据集
└── static/           # 登录页、学生端与管理端
migrations/           # 数据库迁移
scripts/              # 导入、索引管理与开发脚本
skills/               # 场景 Skill
tests/               # 自动化测试
docs/                # 技术说明
```

## 开发文档

首页只介绍主要功能和默认启动方式，具体配置见以下文档与目录。

| 内容 | 入口 |
| --- | --- |
| 本地开发、数据库与模型配置 | [技术文档](docs/TECHNICAL_GUIDE.md) |
| 知识入库与索引管理 | [知识入库](docs/TECHNICAL_GUIDE.md#知识入库-v2) · [索引管理](scripts/manage_knowledge_index.py) |
| Agent 分工与执行过程 | [Agent loop](docs/TECHNICAL_GUIDE.md#agent-loop) |
| 对话摘要与用户记忆 | [记忆设计](docs/TECHNICAL_GUIDE.md#三层记忆-v3) |
| 自动化测试与持续集成 | [tests](tests/) · [GitHub Actions](.github/workflows/test.yml) |
| 评测脚本与数据集 | [app/evaluation](app/evaluation/) · [RAG 评测](docs/TECHNICAL_GUIDE.md#rag-评测) |

## 当前状态

项目目前用于原型开发与演示。校园问答效果取决于导入资料和所用模型，心理支持功能不能替代专业咨询或医疗诊断。

仓库不包含本地会话数据、风险台账、密钥和模型权重。默认账号仅供开发演示，实际部署时需要更换，并配置相应的访问控制和数据保留策略。
