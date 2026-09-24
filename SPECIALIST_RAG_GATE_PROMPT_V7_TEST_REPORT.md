# Specialist RAG Gate Prompt V7 测试报告

## 修改范围

仅修改 `app/agents/autonomous.py` 中 Specialist 系统提示词组织方式，并新增 `tests/test_specialist_rag_gate_prompt.py`。未修改检索、Reranker、Soft Coverage、AgentLoop、Coordinator 和状态映射。

## 新增 Prompt Gate 测试

命令：

`DATABASE_URL=sqlite+pysqlite:///:memory: python -m pytest -q tests/test_specialist_rag_gate_prompt.py`

结果：`7 passed in 0.39s`

覆盖：Academic/Campus/Mental 各自的 domain prompt、对比式 few-shot、dependencyResults 不重复检索、Evidence Facet 不等于强制 RAG、多 Facet 只调用一次 RAG、不同 Agent 示例隔离。

## 相关回归测试

结果：`33 passed in 0.67s`

覆盖：新增 Prompt Gate、纯学习计划代码级禁用 RAG、Specialist 缺参关闭、ContextBuilder、RoutePlan、Tool Calling。

## 完整 Specialist 旧测试

结果：`6 failed, 12 passed`。

这 6 个失败与 V6 已存在的问题一致：5 个来自旧业务状态 `SUFFICIENT/PARTIAL/INSUFFICIENT/CONFLICT/DEGRADED` 与当前生产 `OK/EMPTY` 的状态契约漂移；1 个来自 `DEGRADED + MODEL_ERROR` 时仍携带候选 evidence。此次 Prompt 修改未触及该逻辑，因此没有新增相关失败。

## Live LLM Gate 测试

当前容器访问 `http://127.0.0.1:11434/api/tags` 返回 `Connection refused`，因此没有伪造 Qwen3:8b 的真实 tool-choice 准确率。本次能确认的是 Prompt 结构与代码契约测试通过；真实 LLM 的“该查/不该查”命中率应在 Ollama 可用环境继续跑场景集。
