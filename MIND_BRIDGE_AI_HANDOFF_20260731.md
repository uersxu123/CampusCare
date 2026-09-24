# MindBridge 项目上下文与问题分析交接文档

更新时间：2026-07-31  
项目目录：`D:\BaiduNetdiskDownload\mindbridge-pytest\mindbridge-py`

## 1. 交接目标

本文件用于把当前一轮排查和配置工作的完整上下文交给其他 AI，后续需要基于本文件继续完成：

1. 重构不同领域的系统提示词。
2. 修复路由误判和风险识别边界。
3. 收紧知识库检索触发条件。
4. 优化 Skill，减少模板化回复。
5. 修复学生端 Markdown 展示。
6. 补充相应的自动化测试并完成 Docker 验证。

目前上述代码修复尚未实施，本轮只完成了问题分析、Embedding 配置切换、索引构建和项目重启。

明确范围约束：现有 SafetyAgent 候选回复方案审查链路保持原样。本轮不增加模型最终正文生成后的二次安全审查，不改变流式生成顺序，也不因安全审查调整 `completion_verified` 的现有语义。

## 2. 项目概况

项目是一个面向学生的 FastAPI 校园陪伴与心理支持系统，主要组件包括：

- FastAPI / Uvicorn
- MySQL
- Redis
- Ollama
- 事件驱动多 Agent Runtime
- 本地 Skill
- BM25F + Chroma + RRF Hybrid RAG
- 原生 HTML / CSS / JavaScript 学生端

Docker Compose 服务：

- `mindbridge-py-app-1`
- `mindbridge-py-mysql-1`
- `mindbridge-py-redis-1`

当前访问地址：

- 学生端：`http://127.0.0.1:8080/student.html`
- 服务入口：`http://127.0.0.1:8080`
- 健康检查：`http://127.0.0.1:8080/actuator/health`

演示账号：

- 学生：`student / student123`
- 管理员：`admin / admin123`

## 3. 本轮已经完成的配置操作

### 3.1 Embedding 已固定为 bge-m3

新增了本地 `.env`：

```env
KNOWLEDGE_VECTOR_ENABLED=true
KNOWLEDGE_VECTOR_REQUIRED=false
KNOWLEDGE_EMBEDDING_PROVIDER=ollama
KNOWLEDGE_EMBEDDING_MODEL=bge-m3:latest
KNOWLEDGE_EMBEDDING_BASE_URL=http://host.docker.internal:11434
KNOWLEDGE_HYBRID_V3_ENABLED=true
```

本机 Ollama 已确认存在：

- `bge-m3:latest`
- `qwen3:8b`

### 3.2 已重建应用镜像

执行过：

```powershell
docker compose up -d --build app
```

当前应用运行的是仓库最新代码，不再是此前使用 `text-embedding-3-small` 的旧镜像。

### 3.3 已构建并激活 bge-m3 向量索引

执行过：

```powershell
docker exec mindbridge-py-app-1 python scripts/manage_knowledge_index.py build --batch-size 32
docker exec mindbridge-py-app-1 python scripts/manage_knowledge_index.py verify
docker exec mindbridge-py-app-1 python scripts/manage_knowledge_index.py shadow
docker exec mindbridge-py-app-1 python scripts/manage_knowledge_index.py activate
```

索引结果：

```text
Provider: ollama
Model: bge-m3:latest
Embedding dimension: 1024
Collection: mindbridge_knowledge_v3__ea3c41ad2329
Index signature: ea3c41ad2329839461240c6e64de4cdd8d394539395e234e3a4dcebc19b987a8
State: ACTIVE
Vector chunks: 530
Retrieval mode: hybrid-v3
Vector available: true
```

### 3.4 当前服务状态

项目已经重启并验证：

```text
Application health: UP
MySQL: healthy
Redis: healthy
Embedding: ollama / bge-m3:latest
Retrieval mode: hybrid-v3
Vector index: ACTIVE
```

注意：直接同时执行 `docker compose restart` 时，应用曾在 MySQL 尚未恢复前启动并退出。随后执行 `docker compose up -d app`，等待 MySQL 和 Redis 健康后应用已正常恢复。

## 4. 用户反馈的异常回答

用户输入：

```text
我在考研、就业和实习之间拿不定主意，帮我梳理一下。
```

模型回复：

```markdown
**选项比较表：**

| 选项 | 依据 | 未知项 | 下一步行动 |
|------|------|--------|------------|
| 考研 | 适合希望深入学术、提升专业能力、或对当前就业市场不确定的学生。 | 目标院校的录取率、专业匹配度、考试难度、未来就业方向。 | 核对目标院校的招生简章、历年分数线，联系学长学姐了解备考经验。 |
| 就业 | 适合希望尽早积累工作经验、明确职业方向、或对行业有清晰认知的学生。 | 岗位的稳定性、发展空间、薪资水平、工作与生活的平衡。 | 分析目标岗位的 JD，补齐简历证据，进行真实投递并获取反馈。 |
| 实习 | 适合希望了解行业、积累实际经验、或为未来求职或升学做铺垫的学生。 | 实习内容是否与专业相关、是否影响课程、实习后是否有助于求职或升学。 | 确认实习时间安排、工作内容，联系目标公司或平台申请短期实习或远程实习。 |

**验证行动建议：**

- 本周内选择一至两个方向进行信息收集和低成本验证。
- 下周根据反馈进行复盘。

**安全通用建议：**

- 不要急于做决定。
- 可以同时准备多个方向。
- 具体问题建议联系职业中心、导师或相关机构。
```

用户认为该输出“看着怪怪的”。

## 5. 本轮 Trace 证据

对应 Trace：

```text
Trace ID: 477
Session ID: 80e8a75fba2c489084b2b363e0203f82
Intent: CONSULT
Primary domain: ACADEMIC
Risk: LOW
Model: qwen3:8b
Temperature: 0.25
Finish reason: STOP
Completion verified: true
Prompt tokens: 883
Output tokens: 440
Output chars: 1200
```

上下文清单：

```text
Skill:
- further_study_career_decision

Knowledge references:
- knowledge:540
- knowledge:541

Retrieval:
- hybrid-v3
- embedding provider: ollama
- embedding model: bge-m3:latest
- vector degraded: false
- candidate count: 48
- accepted prompt evidence: 2
```

结论：本轮 Embedding 和 RAG 工作正常，异常主要来自系统提示词、Skill 契约和前端展示方式。

## 6. 本轮实际命中的知识库内容

文档：

```text
canonical key: further-study-career-development
title: 升学、就业与实习决策
```

### knowledge:540

章节：`决策维度`

```text
面对考研、保研、就业、求职或实习选择时，不应只比较单一结果。
可以从目标、能力、时间、成本、风险和备选方案等维度整理信息。
```

排名：

```text
BM25 rank: 1
Vector rank: 1
Accepted for prompt: true
```

### knowledge:541

章节：`行动计划`

```text
先完成信息收集，再做小规模验证。
考研可以核对目标专业、考试科目和历年要求；
就业可以分析岗位描述、补齐简历证据并进行真实投递；
实习可以确认时间安排、工作内容和是否影响课程。
每周设置可以观察的产出。
```

排名：

```text
BM25 rank: 2
Vector rank: 2
Accepted for prompt: true
```

回答中的“核对招生简章、分析 JD、真实投递、本周验证、下周复盘”主要来自这两个知识块。

“选项、依据、未知项、下一步”的表格结构主要来自 Skill。

“安全通用建议”主要来自系统提示词，不是知识库原文。

## 7. 已确认的问题与范围约束

### 7.1 学业和校园事务被套用心理关怀提示词

文件：

```text
app/services/ai.py
PromptTemplates.answer_system_prompt()
```

当前逻辑只区分：

- `CHAT`：日常陪伴与校园生活助手
- 非 `CHAT`：校园心理关怀智能体

因此以下领域全部使用同一心理关怀提示词：

- `ACADEMIC`
- `CAMPUS_SERVICE`
- `MENTAL_HEALTH`

提示词包含：

```text
你是 MindBridge，一个面向学生的校园心理关怀智能体。
回答要共情、谨慎、非评判……
知识不足时明确说明并给出安全通用建议。
```

可能导致：

- 考研、就业回答出现“安全通用建议”
- 调宿、奖助、政策问题过度共情
- 论文润色进入 `support mode`
- 正常发展咨询带有心理报告式口吻

建议改为“领域 × 风险”提示词矩阵：

- `CHAT`
- `ACADEMIC`
- `CAMPUS_SERVICE`
- `MENTAL_HEALTH`
- `RISK`

### 7.2 多条 System Message 存在身份冲突

文件：

```text
app/agents/autonomous.py
ResponseAgent.act()
```

第一条 System Message 说：

```text
你是 MindBridge
```

第二条又说：

```text
你是 ResponseAgent。你根据黑板上的意图、风险、上下文和安全约束提出候选回复 prompt……
```

可能导致：

- 输出“候选回复如下”
- 输出“根据黑板信息”
- 输出一段 Prompt 而不是最终正文
- 暴露 ResponseAgent、Skill、风险标签等内部信息

建议合并为一条面向学生最终输出的 System Prompt，并明确：

```text
直接输出学生最终看到的回复正文。
不得解释内部 Agent、Prompt、Skill、路由、风险标签或检索流程。
```

### 7.3 缺少统一输出格式契约

全局 System Prompt 没有明确规定：

- 输出纯文本还是 Markdown
- 默认长度
- 是否使用标题、表格
- 缺少上下文时何时追问
- 事实、建议和未知项如何区分
- 避免重复和报告式结尾

模型只能依赖 Skill 的局部契约，因此容易机械复制 Skill 模板。

### 7.4 Skill 强制产生模板化结构

文件：

```text
skills/further_study_career_decision/SKILL.md
```

当前 Response Contract：

```text
以简短比较表或要点呈现“选项、依据、未知项、下一步”；
给出一个近期验证动作，不替学生作最终决定。
```

它直接诱导了四列表格。

该 Skill 虽然在 Required Context 中要求：

- 时间窗口
- 不可妥协约束
- 当前准备基础
- 已有证据
- 主要不确定性

但运行链路没有强制检查这些信息，也没有触发澄清问题。模型因此用泛化假设填满表格。

建议修改为：

1. 先判断个性化信息是否充分。
2. 信息不足时只问一个最有区分度的问题。
3. 表格设为可选，不强制四列。
4. 优先输出决策维度和一个近期验证动作。
5. 避免固定追加“安全通用建议”。

### 7.5 学业通用问题触发了过度 RAG

文件：

```text
app/services/agentic_rag.py
decide_retrieval_need()

app/services/knowledge_query.py
KnowledgeTaxonomy.build_query_spec()
```

当前逻辑会把识别到学业概念的问题标为：

```text
requires_authoritative_local_info = true
```

所以“考研还是就业”也被判定为：

```text
INSTITUTIONAL_FACT_OR_PROCESS
```

这会导致：

- 个性化决策问题也检索知识库
- Skill 和知识库重复强化同一模板
- 通用建议被包装成学校权威信息
- 知识不足时错误要求用户提供校区或通知

建议区分：

- 价值权衡、陪伴、改写：默认不检索
- 校规、资格、日期、材料、电话、地点：必须检索
- 混合问题：只对事实子问题检索

### 7.6 安全审查链路保持原样

文件：

```text
app/agents/autonomous.py
SafetyAgent._review_response()

app/services/chat.py
ChatService._run_model_generation()
```

当前 SafetyAgent 审查 ResponseAgent 生成的候选回复方案，协调器通过后再由 ChatService 调用模型并流式生成正文。

根据当前实施要求，这套审查时序保持不变：

1. 不增加模型最终正文生成后的二次安全审查。
2. 不为了安全复核缓冲、延迟或回放流式正文。
3. 不新增安全复核状态字段。
4. `completion_verified` 继续表示模型输出完整性，不扩展为最终正文安全审批状态。
5. 路由与风险分类仍需修复，但不得借此重构 SafetyAgent 的候选方案审查流程。

### 7.7 路由规则存在明显误判

文件：

```text
app/agents/routing.py
app/services/ai.py
app/services/assessment.py
```

使用当前规则验证得到：

```text
输入：帮我写一个服务器压力测试脚本
结果：CONSULT / MENTAL_HEALTH

输入：我在论文中分析自杀预防政策
结果：RISK / HIGH

输入：这部电影里的角色说他不想活了，这句怎么翻译？
结果：RISK / HIGH

输入：我最近真的撑不下去了，想彻底消失
纯规则结果：CHAT / LOW
```

最后一个输入通常还有 SafetyAgent 的模型评估，但模型失败时，当前关键词 heuristic 也可能漏掉。

缺失的语境判断包括：

- 技术语境中的“压力”
- 引用、翻译和第三人称表达
- 否定表达
- 学术讨论中的高风险词
- 第一人称间接危险表达
- 行为意图、时间和可用手段

此外，`PromptTemplates.intent_prompt()` 中的旧定义把普通学习和校园事务归类为 `CHAT`，而当前规则路由把它们归类为 `CONSULT`。该函数目前没有被调用，但属于未来可能重新引入的不一致代码。

### 7.8 心理分类器输入缺少清晰信任边界

`psychology_prompt()` 将历史消息、旧风险元数据和当前输入直接拼接进一个 User Message：

```text
最近上下文：...
同会话历史风险元数据：...
当前输入：...
```

最终回答链路已经有 `REFERENCE_DATA` 信任边界，但风险评估链路没有。

可能导致：

- 历史消息被误认为当前状态
- 引用内容被误认为用户本人表达
- 用户通过提示注入要求输出 LOW
- 旧高风险状态过度影响当前消息

建议把以下内容分开标记：

- `HISTORY_REFERENCE`
- `PREVIOUS_SAFETY_METADATA`
- `CURRENT_USER`

并明确只有 `CURRENT_USER` 可以决定本轮即时风险，历史只用于辅助。

### 7.9 实时和易变信息缺少事实边界

`CHAT` 提示词只要求自然、准确、直接回答，没有明确：

- 没有实时数据时不得预测天气
- 不知道当前日期时不得编造日期
- 没有工具结果时不得声称已查询
- 校规、截止时间和联系方式必须核验

当前历史会话中已经出现过：

- 未经查询的天气预测
- “今天是 2023 年 10 月 10 日”之类错误日期

建议增加统一规则：

```text
实时、易变和机构事实必须来自本轮工具或检索证据。
没有证据时明确说明无法确认，不得用模型记忆补齐。
```

### 7.10 上下文预算和 Skill 可选性存在缺陷

文件：

```text
app/services/context_builder.py
app/services/skills.py
```

`build_response_prompt()` 接收 `intent` 后立即执行：

```python
del intent
```

所以预算裁剪没有根据领域和任务类型调整。

`_skill_optional()` 依赖 Skill 字典中的 `optional` 字段，但 `SkillMatch.as_dict()` 没有输出该字段。

结果是普通 Skill 实际上很难按预期作为可选内容被裁剪。

可能导致：

- Skill、知识、摘要和历史互相挤占
- 长输入触发 `protected_overflow`
- 返回固定的超预算回复
- 不相关但高分的 Skill 占据 Prompt

### 7.11 前端没有渲染 Markdown

文件：

```text
app/static/student.js
```

历史消息和流式消息都使用：

```javascript
bubble.textContent = content;
assistant.textContent = eventData.content || "";
```

CSS 仅使用：

```css
white-space: pre-wrap;
```

因此：

- `**加粗**` 原样显示
- Markdown 表格原样显示
- 标题和列表不渲染
- 历史会话预览也包含 Markdown 符号

不能直接改成不受控的 `innerHTML`，否则存在 XSS 风险。

建议：

1. 选择安全 Markdown 渲染方案。
2. 进行 HTML Sanitization。
3. 历史消息和流式完成消息复用同一渲染函数。
4. 流式过程中可先用纯文本，收到 `done` 后再渲染。
5. 为表格、代码块、列表和长链接补充 CSS。

## 8. 建议实施顺序

### P0：修复路由与风险分类边界

1. 加固风险分类器的当前输入边界。
2. 修复间接高风险在模型失败时被直接降为普通聊天的问题。
3. 区分技术语境、学术讨论、翻译引用、否定表达和第三人称表达。
4. 保持现有 SafetyAgent 候选回复方案审查链路和流式生成时序不变。

### P1：重构回答 Prompt

1. 建立 `CHAT / ACADEMIC / CAMPUS_SERVICE / MENTAL_HEALTH / RISK` 提示词。
2. 合并冲突的 System Message。
3. 去除学生侧 Prompt 中的 ResponseAgent 和黑板术语。
4. 增加统一输出契约。
5. 加入实时信息和机构事实边界。

### P1：修复知识检索触发

1. 通用决策和改写默认跳过 RAG。
2. 只有事实型子问题要求权威证据。
3. 修复知识不足时统一询问“校区”的错误兜底。
4. 检查 `min_evidence_score` 当前被固定为 `0.0` 是否符合预期。

### P2：优化 Skill

1. 减少固定表格和固定三段式。
2. 在 Required Context 缺失时触发单个澄清问题。
3. 为 Skill 增加可选性元数据并接入预算裁剪。
4. 避免 Skill 与知识文档重复承担同一结构指导。

### P2：修复前端展示

1. 安全渲染 Markdown。
2. 统一流式和历史消息渲染。
3. 补充表格和代码块样式。

### P2：补充测试

至少覆盖：

- 学业咨询不得使用心理关怀话术
- 校园事务不得输出风险式结尾
- 心理咨询保持共情和边界
- 现有 SafetyAgent 候选回复方案审查行为保持不变
- 引用高风险词不应自动认定本人高风险
- 间接高风险不能在模型失败时直接降为普通聊天
- “服务器压力测试”必须进入普通技术问答
- 实时天气、日期无工具时不得编造
- 通用考研就业决策不应强制执行本地 RAG
- Markdown 历史消息与流式消息渲染一致
- Prompt 中不得泄露 Agent、Skill、路由和风险元数据
- 长上下文预算裁剪符合领域优先级

## 9. 关键文件索引

```text
app/services/ai.py
  基础意图、风险评估和回答 System Prompt

app/agents/routing.py
  规则路由和语义路由兜底

app/agents/autonomous.py
  UnderstandingAgent、SafetyAgent、KnowledgeAgent、ResponseAgent

app/agents/coordinator.py
  Skill 选择和任务编排

app/services/assessment.py
  心理风险模型解析和 heuristic 兜底

app/services/agentic_rag.py
  RAG 是否执行、证据评分和停止条件

app/services/knowledge_query.py
  QuerySpec、领域概念和权威信息判断

app/services/context_builder.py
  Prompt 组装、信任边界和上下文预算

app/services/skills.py
  Skill 加载、匹配和 Prompt 注入

skills/further_study_career_decision/SKILL.md
  本轮固定表格结构的主要来源

app/knowledge/further-study-and-career-development.md
  本轮命中的知识文档

app/services/chat.py
  最终模型生成、续写和持久化

app/static/student.js
  学生端消息渲染

app/static/styles.css
  消息气泡和 Markdown 相关样式

tests/
  需要补充路由、Prompt、安全和前端行为回归
```

## 10. 实施时的约束

1. 所有编辑文件必须保存为 UTF-8，优先无 BOM。
2. 中文字符串、注释和 UI 文本必须保留为直接可读中文。
3. 不得把中文改写成 Unicode 转义。
4. 完成前检查修改文件：

```powershell
rg -n "\\u[0-9a-fA-F]{4}" <changed-files>
```

5. 当前目录不是 Git 仓库，无法依赖 `git diff` 或 Git 回滚。
6. 不要破坏现有 MySQL 数据和已经激活的 bge-m3 向量索引。
7. 修改应用代码后需要重新构建：

```powershell
docker compose up -d --build app
```

8. 如果同时重启全部容器，需要确保 MySQL、Redis 健康后再启动应用：

```powershell
docker compose up -d mysql redis
docker compose up -d app
```

## 11. 建议交给下一位 AI 的任务描述

可以将本文件发送给下一位 AI，并附上以下指令：

```text
请阅读这份 MindBridge 交接文档，先检查文档中的问题是否与当前代码一致。
然后按 P0、P1、P2 顺序实施修复，不要只改一条提示词。

要求：
1. 保持现有 SafetyAgent 候选回复方案审查链路、流式生成时序和 completion_verified 语义不变。
2. 优先修复路由误判、风险识别上下文边界和 heuristic 漏判。
3. 将系统提示词重构为领域和风险组合，不再把所有 CONSULT 当成心理咨询。
4. 修复 RAG 过度触发和 Skill 模板化。
5. 为学生端加入安全 Markdown 渲染。
6. 补齐单元测试和关键集成测试。
7. 重新构建 Docker 应用，验证健康状态、bge-m3 ACTIVE 索引和代表性对话。
8. 保持所有中文源码为 UTF-8 直接字符，不使用 Unicode 转义。
```
