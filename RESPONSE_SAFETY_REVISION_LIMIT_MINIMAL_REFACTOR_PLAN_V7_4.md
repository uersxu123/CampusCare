# MindBridge V7.4：Response→Safety 修订上限最小化改造方案

## 1. 目标

在 V7.3 的 `ResponseAgent -> SafetyAgent -> Coordinator` 闭环上只补一个明确终止条件：

- 首稿 `response-v1` 如果被 Safety `REVISE`，允许 ResponseAgent **最多再生成 1 次**；
- 修订稿 `response-v2` 如果仍被 Safety `REVISE`，**不再创建 response-v3**；
- Coordinator 直接安装一条代码内固定的 deterministic safe fallback，并沿用现有 `_try_accept_final()` 作为唯一最终接收闸门；
- 不修改 RAG、WorkItem、Blackboard schema、SSE 协议、前端和 TurnExecution 主结构。

默认配置：

```text
agent_max_response_revisions = 1
```

含义是：**初稿 + 最多 1 次安全修订**。

---

## 2. 修改前问题

V7.3 中 Safety 每次输出 `REVISE`，Coordinator 都会继续创建新的 `revise-response` Task。虽然全局 `max_rounds` / `max_claims_per_agent` 能最终阻止无限循环，但 Response-Safety 自身没有清晰、独立的修订上限。

原链路：

```text
response-v1
  -> Safety REVISE
  -> response-v2
  -> Safety REVISE
  -> response-v3
  -> ...
  -> 依赖全局 Agent budget 结束
```

---

## 3. 修改后链路

```text
response-v1
  -> Safety REVISE
  -> critique-v1
  -> Coordinator 创建 revise-response
  -> response-v2
  -> Safety REVISE
  -> critique-v2
  -> Coordinator 检测 revision budget exhausted
  -> deterministic safe fallback
  -> matching deterministic safety_review
  -> _try_accept_final()
  -> FINAL_ACCEPTED
  -> Runtime direct_response
  -> TurnExecution / SSE
```

如果 `response-v2` Safety APPROVE，则正常接受 `response-v2`，不会进入 fallback。

---

## 4. 最小代码改动

### 4.1 `app/core/config.py`

新增：

```python
agent_max_response_revisions: int = 1
```

并限制在 `[0, 2]`，默认仍为 1。

### 4.2 `app/agents/coordinator.py`

`EventDrivenCoordinator.__init__()` 读取：

```python
self.max_response_revisions
```

`_ensure_response_review_and_revision()` 只处理**针对当前最新 response_proposal 的 critique**，避免旧 critique 对新正文重复派活。

统计本 Turn 的 Safety `critique` 数量：

- 第 1 个 critique：允许创建一次 Response revision Task；
- 第 2 个 critique：超过默认 `max_response_revisions=1`，不再创建第三个 Response Task，转 `_install_safety_terminal_fallback()`。

### 4.3 Deterministic terminal fallback

普通场景：

```text
当前回复未能通过安全复审，我不能继续提供其中可能有风险的具体内容。
如果你愿意，我可以改为帮助你了解安全、合规的替代处理方式。
```

HIGH 风险场景：

```text
我不能继续提供可能增加当前风险的具体内容。请先确认自己处于安全环境，不要独处，
并尽快联系身边可信任的人、学校心理中心、校园保卫或当地紧急服务。
```

fallback 是代码固定文本，不再调用 Qwen。

Coordinator 创建新的 `response_proposal`：

```text
mode = safety_terminal_fallback
generationDiagnostics.reason = SAFETY_REVISION_LIMIT
terminalSafetyFallback = true
```

同时创建与该 fallback `responseArtifactId` 精确绑定的 deterministic `safety_review`，然后仍由原来的 `_try_accept_final()` 判断是否可设置 `final_artifact_id`。

这样没有新增第二套“最终答案出口”。

---

## 5. Coordinator 如何判断“有没有最终答案”

这是本次必须保持清晰的状态语义。

### 5.1 `response_proposal` 不等于最终答案

只要 ResponseAgent 已经生成过正文，Blackboard 里就可能存在：

```text
response_proposal.directResponse = "一大段正文"
```

但这只表示：

> **候选正文已经存在。**

它还不能离开 Runtime。

### 5.2 `safety_review` 也必须与当前正文版本绑定

Coordinator 只接受：

```text
review.metadata.responseArtifactId == current_response.id
and review.payload.approved == true
```

因此：

```text
response-v1 + review-v1 APPROVE
```

不能批准后续的：

```text
response-v2
```

### 5.3 真正“有最终答案”的唯一判据

Coordinator 的 `_try_accept_final()` 必须同时满足：

```text
1. 当前存在 response_proposal
2. 当前存在 safety_review
3. safety_review.responseArtifactId == 当前 response.id
4. safety_review.approved == true
5. response.confidence >= final_min_confidence
```

然后才调用：

```python
board.accept_final(response.id, ...)
```

此时 Blackboard 才会有：

```text
final_artifact_id = response.id
```

所以在架构语义上：

```text
有正文 != 有答案

有 response_proposal != 有最终答案

只有 final_artifact_id != ""
并且 accepted_artifact() 能取到对应 response
才叫“这个 Turn 已经有可交付答案”
```

Runtime 出口也只读取：

```python
accepted = board.accepted_artifact()
```

未审核、被 REVISE 或仅存在于 Blackboard 上的 candidate，不会被导出为 `direct_response`。

---

## 6. 测试计划

新增 `tests/test_response_safety_revision_limit_v4.py`，覆盖：

1. 第一次 Safety REVISE 只创建一次 revision Task；
2. 第二次 Safety REVISE 不创建第三个 Response Task；
3. 第二次 REVISE 转 deterministic terminal fallback；
4. fallback 仍通过统一 `_try_accept_final()` 进入 `FINAL_ACCEPTED`；
5. 仅存在正文但没有匹配 Safety APPROVE 时，`accepted_artifact()` 必须为空；
6. 旧版本 Safety APPROVE 不得接受新版本正文。

并回归 V7.3 Response/Safety/Coordinator/RAG 测试。

---

## 7. 面试表述

> ResponseAgent 生成正文后只产生 candidate response，并不代表系统已经有最终答案。Coordinator 的最终接收条件是当前 candidate 必须有与其 artifact id 精确绑定的 Safety APPROVE，并且置信度达到阈值，之后才设置 `final_artifact_id`。Runtime 出口只读取 `accepted_artifact()`。
>
> Safety 第一次拒绝时允许 ResponseAgent 按 critique 修订一次；修订稿如果第二次仍被拒绝，就不再继续随机生成，而是由 Coordinator 切到 deterministic safe fallback，再沿用同一个 final acceptance gate 接收，从而保证修订回路和延迟都有明确上界。
