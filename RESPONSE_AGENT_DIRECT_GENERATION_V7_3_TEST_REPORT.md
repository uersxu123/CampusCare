# MindBridge V7.3 ResponseAgent Direct Generation 测试报告

## 1. 改造范围

本次按照 `RESPONSE_AGENT_DIRECT_GENERATION_MINIMAL_REFACTOR_PLAN_V7_3.md` 做最小化改造：

- `ResponseAgent` 在 Agent Runtime 内直接调用 Response 模型生成候选正文；
- `response_proposal.directResponse` 不再为空；
- `SafetyAgent` 的主要审查对象改为真实 `directResponse`；
- Runtime 只导出 `FINAL_ACCEPTED` 的 response artifact，未通过 Safety 的 proposal 不允许离开 Runtime；
- `TurnExecutionService` 不再执行 Runtime 之后的第二次 Response 模型调用；
- SSE 协议、前端、RAG、WorkItem/Blackboard schema 均未修改；
- Response 生成失败最多重试一次，两次仍失败时使用确定性 fallback。

## 2. 关键行为

### 2.1 正常路径

```text
Specialist Results
→ ResponseAgent.generate
→ response_proposal.directResponse
→ SafetyAgent review directResponse
→ FINAL_ACCEPTED
→ AgentRuntime.direct_response
→ TurnExecution APPLICATION
→ persist snapshot
→ SSE snapshot/done
```

### 2.2 生成失败

```text
attempt1 失败/非 STOP
→ attempt2
→ 仍失败/非 STOP
→ deterministic fallback
→ Safety Review
```

普通 fallback：`当前暂时无法生成完整回复，请稍后重试。`

HIGH 风险 fallback：要求确认当前安全、不要独处，并联系现实支持/紧急服务。

### 2.3 Safety 前禁止输出

`EventDrivenAgentRuntimeService._to_result()` 现在只读取 `board.accepted_artifact()`；不再使用“accepted 或 latest response_proposal”的回退方式。因此 Safety 未通过或尚未完成时，候选正文不会作为 `direct_response` 返回给 TurnExecution/SSE。

## 3. 新增/调整测试

新增：

- `tests/test_response_agent_direct_generation_v3.py`
  - Runtime 内直接生成正文；
  - 首次失败后二次重试；
  - 两次失败后确定性 fallback；
  - LENGTH/非 STOP 输出不直接暴露；
  - Runtime 只导出 FINAL_ACCEPTED response 的代码契约。
- `tests/test_turn_execution_no_second_response_model_v3.py`
  - Runtime 已有 direct response 时不调用 post-runtime model；
  - Runtime 异常缺少 direct response 时也不调用第二次模型，使用确定性 APPLICATION fallback。

调整：

- `tests/test_safety_response_review_v2.py`
  - Safety Review 输入包含真实 `directResponse`；
  - Safety prompt 明确候选最终正文是主要审查对象。

## 4. 已执行测试

测试环境数据库覆盖：

```bash
DATABASE_URL='sqlite+pysqlite:///:memory:'
```

### 4.1 V7.3 新增 + Safety + Event Runtime + Specialist/RAG 相关回归

```text
59 passed
```

覆盖：

- `test_event_driven_multi_agent.py`
- `test_specialist_agents_v2.py`
- `test_rag_pipeline_v2.py`
- `test_specialist_rag_gate_prompt.py`
- `test_safety_response_review_v2.py`
- `test_response_agent_direct_generation_v3.py`
- `test_turn_execution_no_second_response_model_v3.py`

### 4.2 Coordinator / Route 回归

```text
17 passed
```

覆盖：

- `test_coordinator_work_items_v3.py`
- `test_route_plan_v3.py`

### 4.3 Routing Safety Boundaries

```text
5 passed, 15 subtests passed
```

### 4.4 编译检查

```text
COMPILE_OK
```

命令：

```bash
python -m compileall -q app tests
```

## 5. 未完成的完整集成测试说明

尝试运行 `tests/test_chat_turns.py` / `tests/test_chat_completion.py` 时，当前执行环境缺少项目依赖 `mcp`，在导入：

```text
app.services.mcp_runtime
```

时出现：

```text
ModuleNotFoundError: No module named 'mcp'
```

这是测试环境依赖缺失，不是本次改动产生的代码断言失败。因此本报告不声称“全仓库 pytest 全部通过”。

## 6. 有意保留的最小化取舍

- 保留 `TurnExecutionService.execute(..., model_generation=...)` 参数作为一版兼容接口，但内部已经 `del model_generation`，不会调用第二个 Response 模型；这样避免同步改动 ChatService 与 evaluation adapter 的接口。
- 原 `ChatService._run_model_generation()` 暂时保留为兼容/历史代码，本次不做大范围删除；当前主执行链路已不再进入它。
- 不新增 continuation 状态机；ResponseAgent 采用一次重试 + deterministic fallback，避免把原外层生成器整套搬进 Agent Runtime。
- 不修改 SSE 协议。现有 APPLICATION 路径只在 Agent Runtime（含 Safety Review）完成后持久化一次正文，因此天然满足“Safety 之前不释放正文”。

## 7. 结论

本次改造完成了“ResponseAgent 是唯一最终模型生成点 + Safety 审查真实候选正文 + SSE 只释放已接受正文”的闭环，同时把代码改动控制在少量核心文件内。
