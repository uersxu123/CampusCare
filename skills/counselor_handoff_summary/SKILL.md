---
name: counselor_handoff_summary
description: 在风险报告生成后，为工作人员生成事实性的交接摘要与后续安排，保持固定模板读取语义。
agents: [report]
intents: [RISK]
keywords:
  domain: [风险报告]
  goal: [工作人员交接]
  context: [固定模板]
enabled: true
optional: false
priority: 100
max_chars: 2600
matching_version: 2
selection_mode: fixed
---

# Counselor Handoff Summary

## Workflow

- Write for counselors or administrators, not for the student.
- Preserve the student's original meaning while avoiding unnecessary dangerous detail.
- Include the report identity, student identity, risk level, emotion label, confidence, model summary, follow-up actions, and a bounded excerpt of the student's expression.
- Make the first follow-up action about current location, whether the student is accompanied, and immediate safety.
- Keep the handoff factual and actionable; do not add diagnosis or unsupported assumptions.

## Output Template

```text
应用 skill: counselor_handoff_summary
报告ID：{{report_id}}
学生：{{student}}
风险等级：{{risk_level}}
情绪标签：{{emotion}}
置信度：{{confidence}}
模型摘要：{{summary}}
建议跟进：
{{next_steps}}
学生原始表达：
{{content_excerpt}}
```
