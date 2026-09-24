# MindBridge V7.5：Response-Safety 简单退出状态机最小化改造方案

## 1. 改造目标

本次只简化 ResponseAgent → SafetyAgent 的终止路径：

```text
Response v1
  → Safety APPROVE → FINAL_ACCEPTED
  → Safety REVISE  → Response v2
                        → Safety APPROVE → FINAL_ACCEPTED
                        → Safety REVISE  → Coordinator 直接结束 Runtime
                                             → direct_response = ""
                                             → TurnExecution 固定模板兜底
```

不再在 Agent Runtime 内创建 `safety_terminal_fallback` Response Artifact，也不再伪造一份配套 `safety_review` 来让 fallback 进入 `FINAL_ACCEPTED`。

## 2. 设计原则

1. **最多初稿 + 1 次安全修订**：沿用 `agent_max_response_revisions=1`。
2. **第二次 Safety 仍 REVISE 时立即终止**：Coordinator 不创建第三个 Response Task。
3. **被拒绝的正文仍留在 Blackboard 作为 Trace，但不算最终答案**。
4. **Runtime 只导出 `accepted_artifact()`**：没有 `FINAL_ACCEPTED` 时 `direct_response=""`。
5. **外层只做确定性模板兜底**：TurnExecution 发现 Runtime 没有 `direct_response` 时使用现有固定模板，不再调用第二个 Response LLM。

## 3. 最小代码改动

### 3.1 `app/agents/coordinator.py`

保留原有第一次 Safety REVISE 行为：

```text
critique-v1
→ task:revise-response:critique-v1
→ Response v2
```

第二次 REVISE 时：

```text
rejected_count > max_response_revisions
→ 不创建 revise-response task
→ `_response_revision_exhausted(board) == True`
→ `EventDrivenCoordinator.run()` 立即 `return board`
```

新增 `_response_revision_exhausted()`，并且只在以下条件同时成立时认为终止：

- 当前存在 `response_proposal`；
- 当前最新 `critique.approved == false`；
- `critique.responseArtifactId == 当前 response.id`；
- reject 数量超过 `max_response_revisions`。

这样历史 critique 只保留 Trace，不会误伤新的 Response Candidate。

删除 V7.4 的 `_install_safety_terminal_fallback()`。

### 3.2 Runtime / TurnExecution

不需要新增代码：

- `EventDrivenAgentRuntimeService._to_result()` 已经只读取 `board.accepted_artifact()`；
- 第二次 REVISE 后没有 `FINAL_ACCEPTED`，因此 `direct_response=""`；
- `TurnExecutionService` 已经在 `direct_response` 为空时输出固定模板：`当前暂时无法生成完整回复，请稍后重试。`；
- `model_generation` 仍不会被调用。

## 4. “有没有最终答案”的判断

`response_proposal.directResponse` 非空只代表**已经生成 Candidate**。

只有同时满足：

```text
current response_proposal exists
+ matching safety_review exists
+ safety_review.responseArtifactId == current response.id
+ safety_review.approved == true
+ response.confidence >= final_min_confidence
```

Coordinator 才调用：

```text
board.accept_final(response.id)
```

并设置：

```text
final_artifact_id = response.id
```

因此：

```text
latest_artifact("response_proposal") != None
    ≠ 有最终答案

accepted_artifact() != None
    = 有可交付最终答案
```

## 5. 测试范围

新增/调整测试覆盖：

1. 第一次 REVISE 只创建一次 Response revision；
2. 第二次 REVISE 不创建第三个 Response Task；
3. 第二次 REVISE 不在 Runtime 内创建 fallback Response；
4. Coordinator run-loop 识别 revision exhausted 后立即 return；
5. 有正文但没有匹配 Safety APPROVE 时 `accepted_artifact()` 仍为空；
6. matching Safety APPROVE 才能设置 `final_artifact_id`；
7. 老 Safety APPROVE 不能批准新 Response；
8. Runtime response 为空时 TurnExecution 使用固定模板且不调用第二个模型；
9. 原有 Event Runtime / Specialist / RAG / Routing 回归继续通过；
10. `compileall` 通过。
