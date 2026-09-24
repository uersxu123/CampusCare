# MindBridge V7.4 Response-Safety Revision Limit 测试报告

## 1. 实际改动

本次只修改：

- `app/agents/coordinator.py`
- `app/core/config.py`
- 新增 `tests/test_response_safety_revision_limit_v4.py`

未修改 RAG、Specialist、ResponseAgent 生成逻辑、SafetyAgent 模型审查逻辑、SSE、前端和数据库 schema。

## 2. 新行为

```text
初稿 v1
-> Safety REVISE
-> 允许一次 Response 修订 v2
-> Safety 再次 REVISE
-> 不再生成 v3
-> deterministic safe fallback
-> matching deterministic safety_review
-> 统一 _try_accept_final()
-> FINAL_ACCEPTED
```

默认：

```text
agent_max_response_revisions = 1
```

## 3. 新增测试

`tests/test_response_safety_revision_limit_v4.py`：5 个测试。

覆盖：

- 第一次 REVISE 创建一次 revision task；
- 第二次 REVISE 安装 terminal fallback；
- 不创建第三个 Response revision task；
- terminal fallback 仍通过原 final acceptance gate；
- 有候选正文但无匹配 APPROVE 时，不存在 accepted artifact；
- 旧 Safety Review 不能接受新 Candidate。

## 4. 已执行回归

### V7.4 + V7.3 Response/Safety/RAG/Event Runtime

```text
64 passed
```

覆盖：

- `test_event_driven_multi_agent.py`
- `test_specialist_agents_v2.py`
- `test_rag_pipeline_v2.py`
- `test_specialist_rag_gate_prompt.py`
- `test_safety_response_review_v2.py`
- `test_response_agent_direct_generation_v3.py`
- `test_turn_execution_no_second_response_model_v3.py`
- `test_response_safety_revision_limit_v4.py`

### Coordinator / Route

```text
17 passed
```

### Routing Safety Boundaries

```text
5 passed, 15 subtests passed
```

### 编译

```text
python -m compileall -q app tests
COMPILE_OK
```

## 5. Chat 层完整集成测试限制

运行：

```text
tests/test_chat_turns.py
tests/test_chat_completion.py
```

得到 `7 passed, 9 failed`。失败链路首先进入：

```text
ModuleNotFoundError: No module named 'mcp'
```

发生于 `app.services.mcp_runtime` 导入阶段。随后若干旧 ChatCompletion 测试仍按 Runtime 外模型生成的旧语义做断言，因此在 V7.3/V7.4 架构下也不再是当前主链路的有效预期。

因此本报告不声称“全仓库 pytest 全部通过”。本次新增/相关回归测试均通过。

## 6. 关键验收结论

- 初稿最多只允许一次 Safety-driven regeneration；
- 第二次 Safety REVISE 后不会创建 response-v3；
- fallback 不再调用生成模型；
- fallback 仍走统一 `final_artifact_id` 接收机制；
- Blackboard 中“有正文”不等于“有最终答案”；
- Runtime 只导出 `accepted_artifact()`。
