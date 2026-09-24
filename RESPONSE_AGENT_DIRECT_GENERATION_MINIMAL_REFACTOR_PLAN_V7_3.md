# MindBridge V7.3：ResponseAgent 直接生成正文最小化改造方案

## 1. 改造目标

本次只解决一件事：**让 ResponseAgent 在 Agent Runtime 内生成最终候选正文，并让 SafetyAgent 审查这段真实正文；Safety 通过后才由现有 SSE 链路向用户释放内容。**

不重构多 Agent 编排、不改 RAG、不改 WorkItem/Blackboard 数据模型、不重写 SSE 协议，也不引入新的 ResponseGenerationService。

## 2. 当前问题

V7.2 的实际链路是：

```text
Specialist Results
→ ResponseAgent 只生成 response_proposal Prompt（directResponse 为空）
→ SafetyAgent 审查 Prompt/上下文
→ Coordinator FINAL_ACCEPTED
→ Agent Runtime 结束
→ TurnExecution 再调用 Response 模型生成真正正文
→ SSE 输出
```

问题是 SafetyAgent 审查的并不是最终展示给用户的正文；Runtime 外还存在第二个 Response 模型生成点。

## 3. 目标链路

```text
Specialist Results
→ ResponseAgent 生成 candidateResponse
→ response_proposal.directResponse = candidateResponse
→ SafetyAgent 审查 directResponse
→ APPROVE / 原有 REVISE 流程
→ Coordinator FINAL_ACCEPTED
→ Agent Runtime 返回 direct_response
→ TurnExecution 仅做结果封装与持久化，不再二次调用 Response 模型
→ SSE 在 Safety 之后输出
```

## 4. 最小改动范围

### 4.1 `app/agents/autonomous.py`

修改 `ResponseAgent.act()`：

1. 保留现有 synthesis Prompt 构造；
2. 使用 `self.client().complete()` 在 Runtime 内直接生成正文；
3. 只有 `verified_complete=True` 才接受模型正文；
4. 首次失败、异常、空输出或非 STOP 时，最多重试一次；
5. 两次仍失败时返回确定性 fallback，保证 `directResponse` 永远非空；
6. 把轻量 `generationDiagnostics` 写入 `response_proposal`，用于 Trace，不改变现有 Artifact 类型。

普通 fallback：

```text
当前暂时无法生成完整回复，请稍后重试。
```

HIGH 风险 fallback：

```text
我现在无法可靠生成完整回复。请先确认自己当前处于安全环境，不要独处，并尽快联系身边可信任的人、学校心理中心、校园保卫或当地紧急服务。
```

不新增复杂 continuation 状态机；V7.3 只保留“一次重试 + 确定性 fallback”，以控制改动面。

### 4.2 `SafetyAgent._review()`

不改变现有结构化 `APPROVE/REVISE` 协议，只调整审查语义：

- `directResponse` 现在是真正候选正文，是 Safety 的主要审查对象；
- `proposalMessages` 只作为上下文，不再被视为“最终生成方案”；
- LOW / MEDIUM / HIGH 仍全部实际审查。

### 4.3 `app/services/turn_execution.py`

切断 Runtime 之后的 Response 模型调用：

- `harness_outcome.direct_response` 非空：按现有 `APPLICATION` 路径处理；
- 若 Runtime 异常地没有返回正文：直接生成确定性 APPLICATION fallback；
- 保留 `model_generation` 参数作为兼容参数，但不再调用，避免同步修改评测/调用方接口。

这样可以实现“执行路径上删除二次模型生成”，同时减少改动文件数量。

### 4.4 SSE

**不修改 SSE 协议和前端。**

现有 `ChatService` 对 `APPLICATION` 结果是在 `TurnExecution.execute()` 完成后才 `_persist_progress(..., force=True)`；此时 Agent Runtime 已完成 Safety Review 和 FINAL_ACCEPTED，因此用户不会收到 Safety 前的候选正文。

实际表现变为：

```text
meta
→ 等待 Agent Runtime（含 Response 生成 + Safety Review）
→ snapshot（已审核正文）
→ done
```

这是服务端 buffer 后再释放，不做未经审查的 token streaming。

## 5. 明确不做的事情

本次不做：

- 不增加新 Agent；
- 不引入新的 ResponseGenerationService；
- 不修改 RAG、Facet、Coverage；
- 不修改数据库结构；
- 不修改 SSE event 名称；
- 不修改前端；
- 不增加多阶段 continuation；
- 不重构 Safety revision 状态机，继续使用现有 Coordinator 的 REVISE 流程和全局轮次预算。

## 6. 测试计划

新增/调整测试覆盖：

1. ResponseAgent 成功生成后 `directResponse` 为真实正文；
2. 首次生成失败时最多重试一次；
3. 两次失败后使用确定性 fallback，且仍可进入 Safety Review；
4. Safety Reviewer 的输入包含真实 `directResponse`；
5. TurnExecution 不再调用 Runtime 后的 `model_generation`；
6. 原有 Safety 普通场景审查、Specialist、Coordinator、RAG 回归测试继续通过；
7. `compileall` 通过。

## 7. 验收标准

满足以下条件即完成：

```text
ResponseAgent directResponse != ""
SafetyAgent 审查 directResponse
FINAL_ACCEPTED 后 Runtime 返回 direct_response
TurnExecution 不再调用第二次 Response LLM
SSE 只输出 Safety 之后的 APPLICATION 正文
模型失败时有确定性 fallback
相关自动化测试通过
```
