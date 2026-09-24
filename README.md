# CampusCare

CampusCare 是一个面向高校学生的校园助手，主要用于校务咨询、学习规划和日常交流。后端使用 Python 和 FastAPI，前端是原生 HTML、CSS 和 JavaScript。模型可以通过 Ollama 在本地运行，也可以接入兼容 OpenAI 的 API。

项目分为学生端和管理端。学生端提供聊天、历史会话和记忆管理；管理端用于查看会话报告、风险记录和跟进个案。目前仍是原型项目，回答效果取决于模型和导入的校园资料。

## 主要功能

- **校园问答**：从已导入的校园资料中检索内容，辅助回答规章制度、办事流程等问题。
- **学习规划和日常交流**：根据问题类型交给对应的 Agent 处理，支持流式回复。
- **连续对话**：结合近期消息、会话摘要和用户记忆处理追问；信息不完整时可以先向用户确认。
- **风险识别**：评估输入中的风险信号，并在回复发出前进行安全审查。管理端可查看相关报告。
- **后台跟进**：支持个案处理、Excel 台账和邮件预警，邮件发送需要单独配置。

## 实现说明

一次对话从 FastAPI 接口进入，由 ContextBuilder 整理历史消息和记忆。Coordinator 负责组织任务，Understanding 判断需求，Safety 评估风险。聊天、学业、校务和心理支持 Agent 分别处理对应的问题，Response 汇总结果，经过安全审查后返回给前端。

校园知识检索同时使用 BM25 和向量召回，再进行结果融合、重排和证据评估。资料不足时最多改写一次查询，整个检索过程最多执行两轮。向量索引需要在导入资料后单独构建和激活。

MySQL 保存会话、消息和用户记忆，Redis 用作短期缓存，Chroma 保存向量索引。工具通过 MCP 接入，知识检索和天气查询等只读工具与风险记录写入工具分开管理。

| 部分 | 使用的技术 |
| --- | --- |
| 服务端 | Python、FastAPI、SQLAlchemy、Alembic |
| 页面 | HTML、CSS、JavaScript、SSE |
| 模型 | Ollama / OpenAI-compatible API |
| 检索 | BM25、Chroma、RRF、LLM 重排与评估 |
| 数据存储 | MySQL、Redis |
| 部署和测试 | Docker Compose、unittest、GitHub Actions |

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

详细配置、Agent 分工、索引管理和评测命令见[技术文档](docs/TECHNICAL_GUIDE.md)。自动化测试在 [tests](tests/) 目录，CI 配置见 [test.yml](.github/workflows/test.yml)。

## 使用说明

仓库不包含本地会话数据、风险台账、密钥和模型权重。上面的账号仅供开发演示，实际部署时需要更换。心理支持功能用于一般交流和辅助识别风险，不能替代专业咨询或医疗诊断。
