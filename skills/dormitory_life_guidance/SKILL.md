---
name: dormitory_life_guidance
description: 帮助学生处理室友协调、住宿噪音、设施报修或调宿问题，确定即时动作、沟通记录和正式处理渠道。
agents: [campus_affairs]
intents: [CAMPUS]
keywords:
  domain: [室友冲突, 宿舍噪音, 设施故障, 调宿]
  goal: [协调, 报修, 调宿, 正式处理]
  context: [宿舍, 持续时间, 安全风险]
enabled: true
optional: true
priority: 95
max_chars: 2600
matching_version: 2
selection_mode: scenario
selection_group: primary_strategy
matching:
  domain:
    strong:
      - id: dormitory_problem
        any_of: [室友冲突, 宿舍噪音, 寝室噪音, 宿舍漏水, 水管漏水, 设施故障, 调宿]
    weak:
      - id: broad_dormitory_context
        any_of: [宿舍, 寝室, 室友]
  goal:
    strong:
      - id: dormitory_action_goal
        any_of: [怎么报修, 联系负责人员, 协调室友, 申请调宿, 正式处理渠道]
      - id: handle_dormitory_problem
        all_of:
          - [协调, 报修, 调宿, 投诉]
          - [室友, 宿舍, 寝室, 设施]
    weak:
      - id: dormitory_help
        any_of: [宿舍怎么办, 室友怎么办]
  context:
    - id: dormitory_arguments
      argument_keys: [campus, location, duration, attempted_actions]
  exclude:
    - id: dormitory_as_location_only
      all_of:
        - [在宿舍, 在寝室]
        - [申请材料, 学习计划, 写论文]
semantic_examples:
  - 宿舍水管漏水，需要知道如何报修并联系负责人员。
  - 室友长期制造噪音，希望先协调并保留正式处理记录。
  - 住宿冲突无法解决，想了解申请调宿的下一步渠道。
---

# 宿舍生活处理

## Scope

适用于住宿生活冲突、设施问题和调宿流程准备。不用于推断他人动机、编造宿舍规定或保证房源与审批结果。

## Required Context

- 具体问题、持续时间、已尝试的处理方式。
- 所在校区或住宿区域，以及是否存在即时安全风险。

## Workflow

1. 先区分日常协商、设施故障、规则问题、正式调宿与安全事件。
2. 在安全前提下给出一个低冲突的即时动作。
3. 问题持续时，整理事实、时间和已沟通记录，转入正式渠道。
4. 对报修、调宿、门禁和负责部门使用 RAG 或受控工具核验。
5. 明确后续检查点，并避免归因和对抗性表达。

## Response Contract

按当前问题选取即时动作、持续时处理和待核验信息，控制在少量步骤，不承诺处理时限和结果。普通缺参由 Coordinator 管理，执行中不追加追问。

## Escalation and Boundaries

暴力、跟踪、胁迫、火灾、电气危险或其他即时风险应优先转现实支持和紧急渠道。住宿规则、房源、联系方式与处理时限必须核验，无法确认时保守说明。
