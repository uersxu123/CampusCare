---
name: sleep_routine_support
description: 帮助受到入睡困难或作息紊乱影响的学生安排今晚可执行的睡前步骤和作息调整，减少睡眠困扰。
agents: [psychological_support]
intents: [MENTAL]
keywords:
  domain: [睡不着, 失眠, 入睡困难, 作息紊乱]
  goal: [改善入睡, 调整作息, 睡前步骤]
  context: [今晚, 持续时间, 焦虑背景]
enabled: true
optional: true
priority: 75
max_chars: 1800
matching_version: 2
selection_mode: scenario
selection_group: primary_strategy
matching:
  domain:
    strong:
      - id: sleep_disruption
        any_of: [睡不着, 失眠, 入睡困难, 作息紊乱, 睡眠困扰]
    weak:
      - id: broad_sleep_context
        any_of: [睡眠, 熬夜]
  goal:
    strong:
      - id: sleep_improvement_goal
        any_of: [改善入睡, 调整作息, 睡前步骤, 今晚怎么睡, 想睡着]
      - id: arrange_sleep_routine
        all_of:
          - [安排, 调整, 改善]
          - [睡眠, 作息, 睡前, 入睡]
    weak:
      - id: sleep_help
        any_of: [睡不着怎么办, 失眠怎么办]
  context:
    - id: sleep_arguments
      argument_keys: [duration, bedtime, wake_time]
    - id: tonight_context
      any_of: [今晚, 最近几晚, 这几天]
  exclude:
    - id: sleep_knowledge_only
      all_of:
        - [睡眠科普, 睡眠原理]
        - [解释, 是什么]
semantic_examples:
  - 最近睡不着，希望安排今晚能执行的睡前步骤。
  - 作息越来越乱，想逐步调整入睡和起床时间。
  - 焦虑影响今晚睡眠，希望先改善入睡而不是处理其他目标。
---

# Sleep Routine Support

## Workflow

- Validate that sleep disruption can make study, mood, and relationships harder.
- Suggest a small tonight-only routine: reduce stimulation, write down worries, prepare the next day's first step, or use a calming activity.
- Avoid rigid sleep rules, blame, or claims that one technique will fix everything.
- If sleep problems persist for more than one to two weeks, worsen quickly, or involve safety concerns, encourage professional campus support.
- Do not recommend medication, supplements, or medical treatment plans.
- Do not stop execution merely because duration is missing. Follow the Coordinator's clarification decision and do not add ordinary missing-information questions.
