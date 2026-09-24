# Specialist RAG Gate Prompt V7

## 目标

不增加 Gate Agent、不增加额外 LLM 调用，只利用 Specialist 已有 function-calling 回合，让模型决定当前 WorkItem 是否真的需要 `rag_search`。

## Prompt 结构

最终发送给 Specialist LLM 的仍然是一个 System Prompt：角色与可信上下文规则 → 公共 Tool Gate → 当前 Agent 的 domain-specific Tool Gate + few-shot → Evidence Facet 契约 → 纯学习计划规则（如适用） → 动态 Skill Prompt → 最终 Tool Guard。

## Agent-specific few-shot

### AcademicPlanningAgent
- 20 天高数复习计划：不 RAG。
- 挂科后补考还是重修：RAG。
- 上游已核验补考资格，当前只制定复习计划：不重复 RAG。
- 上游 PARTIAL：缺失事实是否触发 RAG 取决于它是否是当前 WorkItem 的必要事实。

### CampusAffairsAgent
- 因病休学材料：RAG。
- 奖助兼得 + 休学 + 复学三 Facet：只调用一次 RAG，全部 facets 一次传入。
- 已核验流程整理成待办：不 RAG。
- 写休学申请理由且无需新增校规：不 RAG。

### PsychologicalSupportAgent
- 一般考试焦虑/失眠支持：不 RAG。
- 学校心理咨询预约和地点：RAG。
- Academic 上游已经处理挂科制度，MENTAL 当前只处理焦虑：不重复 RAG。

## 未修改

Multi-Query / Rewrite、Hybrid Retrieval / RRF、Facet-Aware Reranker、Soft Coverage、RAG status mapping、AgentLoop、Coordinator / dependency 执行逻辑均未修改。
