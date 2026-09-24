---
name: financial_aid_awards_guidance
description: 帮助学生处理奖学金、助学金、困难资助和评优申请，核验资格、准备材料、查询办理进度或整理异议处理步骤。
agents: [campus_affairs]
intents: [CAMPUS]
keywords:
  domain: [奖学金, 助学金, 困难资助, 评优]
  goal: [资格核验, 申请材料, 办理进度, 异议处理]
  context: [退回, 已有通知, 年度, 批次]
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
      - id: aid_or_award_program
        any_of: [奖学金, 助学金, 困难资助, 临时困难补助, 评优]
    weak:
      - id: broad_award_topic
        any_of: [奖助项目, 荣誉申请]
  goal:
    strong:
      - id: aid_application_goal
        any_of: [资格核验, 申请资格, 申请材料, 准备材料, 办理进度, 异议处理, 补正材料]
      - id: organize_aid_action
        all_of:
          - [申请, 查询, 核验, 整理]
          - [资格, 材料, 进度, 异议]
    weak:
      - id: aid_help
        any_of: [助学金怎么办, 奖学金怎么办]
  context:
    - id: aid_arguments
      argument_keys: [program, deadline, notice, batch, current_stage]
    - id: returned_or_notified
      any_of: [材料退回, 已有通知, 收到通知]
  exclude: []
semantic_examples:
  - 助学金申请需要准备哪些材料，并核验我是否符合资格。
  - 奖学金申请材料被退回，希望整理补正和异议处理步骤。
  - 想查询困难资助的办理进度和负责单位。
---

# 奖助与评优指引

## Scope

适用于奖助项目、困难支持和评优申请的准备与核验。不用于认定家庭经济情况、预测排名或保证获批。

## Required Context

- 项目名称或支持类型、所属年度或批次、当前阶段。
- 已有通知、材料和最关心的资格或进度问题。

## Workflow

1. 区分申请资格、材料完整性、评审进度和最终结果。
2. 使用 RAG 或受控工具核验最新通知、负责单位和来源日期。
3. 将材料分为已有、待准备和待官方确认三类。
4. 给出最近的提交或询问动作，并提醒保留记录。
5. 对逾期、退回或争议情况查找正式补正或异议渠道。

## Response Contract

根据当前问题选择资格、材料、进度或异议中的必要内容；用户只问资格时不强制展开全部材料流程。明确区分资格与获选可能性，不给出未经核验的阈值和日期，也不在执行中追加普通缺参追问。

## Escalation and Boundaries

资格、截止时间、名额、金额、发放时间和联系方式必须来自最新官方来源。无法核验或涉及隐私材料、退回、异议时，建议联系负责资助或评审人员并保留书面记录；不得建议伪造证明。
