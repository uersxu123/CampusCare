# 专业 Agent 后续 LLM 异常兜底改造方案

## 1. 文档目的

本文档用于指导代码 Agent 对专业 Agent 的后续 LLM 异常进行最小化兜底改造。

目标是：当专业 Agent 已经成功执行 RAG，但随后用于生成专业结论的 LLM 发生异常或未完整结束时，保留已经获得的 RAG 结果，并发布一个明确标记为部分完成的现有 `specialist_result` Artifact。

本方案不改变当前事件驱动多 Agent 架构，不新增 Artifact 类型，不新增重试链路，不修改 Coordinator、ResponseAgent 或数据库结构。

## 2. 当前实现与问题

### 2.1 当前调用链

```text
SpecialistAgent._run_loop()
  -> AgentLoop.run()
  -> 专业 LLM 决定调用 rag_search
  -> ToolExecutor 执行 RAG
  -> RAG ToolResult 放回模型上下文
  -> 专业 LLM 生成最终工作项内容
  -> SpecialistAgent 统一组装 specialist_result
```

关键代码位置：

- `app/services/agent_loop.py:70-125`：模型轮次、工具调用和 `AgentLoopResult`。
- `app/agents/autonomous.py:293-390`：专业 Agent 的工具循环及结果映射。
- `app/agents/autonomous.py:260-290`：发布 `specialist_result` Artifact。
- `app/agents/coordinator.py:85-88`：执行 Agent 并将返回的 Artifact 应用到 Blackboard。
- `app/agents/events.py:225-231`：`COMPLETED`、`PARTIAL`、`FAILED` 都会被视为工作项已闭合。

### 2.2 现有故障行为

`AgentLoop.run()` 调用 `client.complete_with_tools()` 时没有捕获模型异常。如果 RAG 已执行成功，而下一轮模型调用抛出超时、连接错误或协议异常：

1. 已执行的 RAG 结果仍只存在于 `AgentLoop` 的局部变量中。
2. 异常从 `AgentLoop` 向上抛出。
3. `SpecialistAgent.act()` 无法返回 `AgentTurnResult`。
4. `specialist_result` Artifact 不会发布。
5. 已经取得的 RAG 片段随本轮 Agent 失败而丢失。

另外，模型返回 `MODEL_INCOMPLETE` 时，`AgentLoopResult` 会保留 `tool_results`，但 `SpecialistAgent._run_loop()` 目前先按 RAG 状态返回，再检查 `stop_reason`。这可能把“证据充分但模型没有成功生成答案”误标为 `COMPLETED / EVIDENCE_COMPLETE`。

## 3. 改造目标

### 3.1 目标行为

当专业 Agent 的 LLM 在 RAG 之后发生异常或未完整结束时：

- `AgentLoop` 不抛出该模型异常，而是返回带有已有 `tool_results` 的 `AgentLoopResult`。
- `SpecialistAgent` 仍然发布现有的 `specialist_result` Artifact。
- Artifact 的业务状态至少为 `PARTIAL`，不能为 `COMPLETED`。
- `reasonCode` 明确记录为 `MODEL_ERROR` 或 `MODEL_INCOMPLETE`。
- 如果 RAG 结果经过验证且可以安全引用，则保留 `evidenceItems` 和 `citationRefs`。
- 如果 RAG 状态为 `DEGRADED`，继续按现有规则清空可引用证据。
- Coordinator 可以正常关闭该工作项并继续执行 ResponseAgent。
- ResponseAgent 必须能够看到 `PARTIAL` 结果，并遵守“不能把部分结果写成已核验事实”的现有约束。

### 3.2 非目标

本次改造不得：

- 新增 `rag_evidence` 等独立 Artifact 类型。
- 改变当前 Blackboard、Coordinator、ResponseAgent 的整体调度方式。
- 增加自动重试或备用模型调用。
- 修改 `RoutePlan v2`、`specialist_result` 的顶层结构。
- 让未经验证的 RAG 片段直接变成最终确定性结论。
- 把模型异常转换为用户澄清请求。

## 4. 目标状态契约

### 4.1 AgentLoopResult

模型异常时返回：

```text
content       = 当前已有的模型内容，通常为空字符串
tool_results  = 异常发生前已经完成的所有工具结果
rounds        = 当前模型轮次
stop_reason   = "MODEL_ERROR"
```

模型没有完整结束但没有抛异常时，继续使用现有的：

```text
stop_reason = "MODEL_INCOMPLETE"
```

异常信息不得直接写入用户可见回答；如需记录，只放入内部 `toolSummary.errorCodes` 或日志，且不得记录完整 prompt、密钥或敏感上下文。

### 4.2 specialist_result

专业 Agent 发生后续模型异常时，使用现有 Artifact 类型，建议形成如下语义：

```json
{
  "status": "PARTIAL",
  "reasonCode": "MODEL_ERROR",
  "answerBrief": "专业 Agent 生成未完成，仅保留已取得的核验证据。",
  "evidenceItems": [],
  "citationRefs": [],
  "confidence": 0.0
}
```

如果 RAG 状态为 `SUFFICIENT`、`PARTIAL` 或 `CONFLICT`，且片段属于 RAG 返回的真实 `items`，可以保留对应的 `evidenceItems` 和 `citationRefs`；但整个 `specialist_result.status` 仍必须是 `PARTIAL`，原因仍必须是模型故障：

```json
{
  "status": "PARTIAL",
  "reasonCode": "MODEL_ERROR",
  "evidenceItems": [{"evidenceId": "ev_chunk_123"}],
  "citationRefs": ["ev_chunk_123"]
}
```

当 RAG 状态为 `DEGRADED` 时，继续清空 `evidenceItems` 和 `citationRefs`，不要把未完成评级的片段当作可引用证据。

## 5. 建议实施步骤

### Step 1：在 AgentLoop 保留模型异常前的工具结果

修改 `app/services/agent_loop.py`：

1. 只包住 `client.complete_with_tools(...)` 这一处模型调用。
2. 捕获普通 `Exception`，不要捕获 `BaseException`。
3. 返回 `AgentLoopResult`，不要重新抛出。
4. 保留当前 `results`、已完成轮次和当前已有内容。
5. `stop_reason` 使用 `MODEL_ERROR`。
6. 不要改变工具执行异常的既有 `ToolResult(False, code, ...)` 处理。

伪代码：

```python
try:
    completion = self.client.complete_with_tools(...)
except Exception:
    return AgentLoopResult(
        "",
        tuple(results),
        model_round,
        "MODEL_ERROR",
    )
```

如果项目已有统一日志工具，应记录 Agent 名称、模型轮次和异常类型；不要记录完整用户内容或完整异常上下文。

### Step 2：调整 SpecialistAgent 的状态判断顺序

修改 `app/agents/autonomous.py:_run_loop()`：

1. 先提取 `result.tool_results`、`tool_summary` 和 `rag_result`。
2. 先判断 `result.stop_reason` 是否为 `MODEL_ERROR` 或 `MODEL_INCOMPLETE`。
3. 如果是模型故障，状态固定为 `PARTIAL`，原因固定为对应的 `reasonCode`。
4. 再根据 RAG 返回状态决定证据是否保留。
5. 只有模型正常完成时，才使用现有的 `SUFFICIENT -> COMPLETED` 映射。

建议的判断优先级：

```text
RAG 工具失败
  -> FAILED / TOOL_UNAVAILABLE

专业 LLM MODEL_ERROR
  -> PARTIAL / MODEL_ERROR

专业 LLM MODEL_INCOMPLETE
  -> PARTIAL / MODEL_INCOMPLETE

RAG 正常且专业 LLM 正常完成
  -> 按 SUFFICIENT/PARTIAL/INSUFFICIENT/CONFLICT 映射

没有 RAG 工具且模型正常完成
  -> COMPLETED / NO_TOOL_REQUIRED 或 TOOL_COMPLETE
```

注意：如果 `rag_result` 存在但其 `ToolResult.ok` 为 `False`，仍优先返回现有的 `FAILED / TOOL_UNAVAILABLE`，不要被后续模型状态覆盖。

### Step 3：保证 Coordinator 能继续工作项闭合

不修改 Coordinator。确认 `specialist_result.status == "PARTIAL"` 时：

- `board.completed_work_item_ids()` 仍会将该工作项视为已闭合。
- 依赖它的后续工作项可以继续执行。
- ResponseAgent 会收到该 `specialist_result`。

如果实际代码验证发现 `PARTIAL` 结果没有关闭任务，只在 `SpecialistAgent.act()` 返回的 `AgentTurnResult` 中保持现有 `close_task` 行为，不要引入新的调度分支。

## 6. 证据保留规则

模型异常时的证据策略必须明确：

| RAG 状态 | `specialist_result.status` | 是否保留证据 | 说明 |
|---|---|---:|---|
| `SUFFICIENT` | `PARTIAL` | 是 | 证据充分，但答案生成失败 |
| `PARTIAL` | `PARTIAL` | 是 | 只能作为部分依据 |
| `INSUFFICIENT` | `PARTIAL` | 谨慎 | 仅保留真实片段，不得形成完整结论 |
| `CONFLICT` | `PARTIAL` | 是 | 必须保留冲突语义，不能给确定答案 |
| `DEGRADED` | `PARTIAL` | 否 | 评级不可用，沿用现有保守策略 |

不能因为 `RAG == SUFFICIENT` 就把整体状态设置为 `COMPLETED`。`SUFFICIENT` 只表示证据状态，不表示专业 Agent 的 LLM 已成功完成。

## 7. 测试要求

建议在 `tests/test_specialist_agents_v2.py` 和 `tests/test_tool_calling_v2.py` 增加最小测试集：

1. RAG 成功，第二轮模型抛 `RuntimeError`：
   - `AgentLoop.run()` 不抛异常。
   - 返回 `stop_reason == "MODEL_ERROR"`。
   - `tool_results` 仍包含 RAG 结果。
2. RAG 为 `SUFFICIENT`，模型抛异常：
   - 发布一个 `specialist_result`。
   - `status == "PARTIAL"`。
   - `reasonCode == "MODEL_ERROR"`。
   - 保留 `evidenceItems` 和 `citationRefs`。
3. RAG 为 `DEGRADED`，模型抛异常：
   - `status == "PARTIAL"`。
   - 证据列表为空。
4. RAG 成功，模型返回 `MODEL_INCOMPLETE`：
   - 不能被标记为 `COMPLETED`。
5. RAG 工具本身失败：
   - 继续保持 `FAILED / TOOL_UNAVAILABLE` 的现有行为。
6. 现有专业 Agent、Coordinator、ResponseAgent 测试全部回归通过。

## 8. 完成标准

改造完成后应满足：

- 专业 Agent 后续 LLM 异常不会导致整个 Agent Runtime 直接崩溃。
- 已执行的 RAG 结果不会因为模型异常而丢失。
- 任何模型异常都不会生成 `COMPLETED` 的专业结果。
- RAG 证据的保留遵守 `DEGRADED` 清空规则。
- Coordinator 和 ResponseAgent 无需架构改造即可继续处理。
- 无新增依赖、无新增 Artifact 类型、无自动重试。

