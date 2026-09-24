---
name: academic_warning_recovery
description: 帮助存在课程不及格、补考、重修或学业预警问题的学生梳理补救动作，核验相关影响，并安排课程恢复步骤。
agents: [academic_planning, campus_affairs]
intents: [ACADEMIC, CAMPUS]
keywords:
  domain: [挂科, 补考, 重修, 学业预警]
  goal: [补救, 核验影响, 恢复课程]
  context: [课程, 截止时间, 已有通知]
enabled: true
optional: true
priority: 90
max_chars: 2600
matching_version: 2
selection_mode: scenario
selection_group: primary_strategy
matching:
  domain:
    strong:
      - id: warning_or_failure
        any_of: [挂科, 补考, 重修, 学业预警, 课程不及格]
    weak:
      - id: grade_difficulty
        any_of: [低绩点, 成绩不及格]
  goal:
    strong:
      - id: recovery_goal
        any_of: [补救, 恢复课程, 核验影响, 补考步骤, 重修步骤]
      - id: organize_recovery
        all_of:
          - [梳理, 安排, 制定]
          - [补考, 重修, 恢复, 补救步骤]
    weak:
      - id: warning_help
        any_of: [挂科怎么办, 预警怎么办]
  context:
    - id: recovery_arguments
      argument_keys: [course, deadline, notice]
  exclude:
    - id: ceremony_date_only
      all_of:
        - [毕业典礼]
        - [什么时候, 日期]
semantic_examples:
  - 高数挂科后需要梳理补考、重修和近期复习的补救步骤。
  - 收到学业预警，希望核验影响并安排课程恢复行动。
  - 课程不及格，想确认正式补救路径和最近要做的事。
---

# 学业预警与补救

## Scope

适用于已发生或担心发生的课程不及格、补考、重修和学业预警。不用于替代学校对毕业、学位或学籍状态的正式认定。

## Required Context

- 已确认的课程或预警状态，以及信息来源。
- 最近的办理节点、学生最担心的影响和可用学习时间。

## Workflow

1. 区分已确认事实、学生推测和仍待核验的政策问题。
2. 先确定最近的官方动作，再安排课程复习和后续复盘。
3. 按紧迫度排列受影响课程，形成短期可执行清单。
4. 对资格、期限、毕业或学位影响使用 RAG 或受控工具核验。
5. 明确下一次检查点和当前任务需要联系的校内角色；待核实项不自动变成新的用户追问。

## Response Contract

先简短承接压力，再按当前问题选取“已知情况、下一步、待核验项”中的必要内容；优先提供少量动作，避免羞辱性表达和结果保证。普通缺参由 Coordinator 管理，执行中不追加追问。

## Escalation and Boundaries

校规、资格、期限及申诉必须基于最新官方来源；无法核验时明确说明不确定并建议联系教务、任课教师、导师或辅导员。若情绪或安全风险占主导，交由心理支持或固定安全分支优先处理。
