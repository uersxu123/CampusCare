---
name: campus_procedure_navigation
description: 帮助学生梳理学籍手续、证明办理、事项申诉和跨部门办事流程，确认负责单位、办理顺序及待核验信息。
agents: [campus_affairs]
intents: [CAMPUS]
keywords:
  domain: [学籍手续, 在读证明, 休学, 复学, 事项申诉]
  goal: [办理步骤, 负责单位, 联系部门, 跨部门流程]
  context: [材料, 通知, 截止时间]
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
      - id: administrative_matter
        any_of: [学籍手续, 在读证明, 成绩证明, 休学, 复学, 事项申诉, 转专业]
    weak:
      - id: broad_procedure_topic
        any_of: [校园手续, 行政事项, 校务办理]
  goal:
    strong:
      - id: procedure_navigation_goal
        any_of: [办理步骤, 办理流程, 找哪个部门, 联系哪些部门, 负责单位, 主管部门]
      - id: navigate_matter
        all_of:
          - [办理, 申请, 申诉, 联系]
          - [步骤, 顺序, 部门, 流程]
    weak:
      - id: procedure_help
        any_of: [怎么办理, 去哪里办]
  context:
    - id: procedure_arguments
      argument_keys: [campus, deadline, notice, current_stage]
    - id: cross_department_context
      any_of: [跨部门, 多个部门, 联系哪些部门]
  exclude:
    - id: pure_dorm_repair
      all_of:
        - [宿舍, 寝室]
        - [报修, 漏水, 设施坏了]
semantic_examples:
  - 办理在读证明需要哪些步骤，应该联系哪个部门。
  - 想申请休学并确认跨部门的办理顺序和待核验材料。
  - 对学籍事项提出申诉，需要梳理负责单位和正式流程。
---

# 校园办事流程导航

## Scope

适用于校内行政事项的流程梳理和来源核验。不用于替代主管部门作资格认定、处分决定或审批承诺。

## Required Context

- 想达成的结果、所在校区或单位、当前办理阶段。
- 已有通知或材料，以及已知期限。

## Workflow

1. 识别主管部门和适用事项，区分咨询、申请、审核与申诉。
2. 通过 RAG 或受控工具核验资格、材料、渠道、时间和来源日期。
3. 将已核验步骤按顺序排列，第一项必须是可立即执行的动作。
4. 单独列出冲突、缺失或可能过期的信息。
5. 提醒保留提交记录、回执和书面答复。

## Response Contract

根据当前事项选取主管部门、必要材料、办理步骤和待确认项，不强制展开完整清单；不输出未核验的地址、电话、日期或资格结论。普通缺参由 Coordinator 管理，执行中不追加追问。

## Escalation and Boundaries

所有易变校规和联系方式必须来自最新官方信息。无法核验、来源冲突、涉及处分或例外审批时，明确不确定性并建议向主管部门取得书面确认。
