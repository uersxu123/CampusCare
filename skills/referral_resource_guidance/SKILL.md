---
name: referral_resource_guidance
description: 帮助希望获得心理支持的学生选择校内外求助渠道，准备咨询预约、联系或说明情况的下一步。
agents: [psychological_support]
intents: [MENTAL]
keywords:
  domain: [心理咨询, 心理中心, 心理支持]
  goal: [求助渠道, 预约咨询, 联系支持, 准备求助]
  context: [校内外, 预约, 已有联系人]
enabled: true
optional: true
priority: 60
max_chars: 1800
matching_version: 2
selection_mode: scenario
selection_group: primary_strategy
matching:
  domain:
    strong:
      - id: psychological_support_context
        any_of: [心理咨询, 心理中心, 心理支持, 心理老师]
    weak:
      - id: broad_support_context
        any_of: [情绪支持, 心理困扰]
  goal:
    strong:
      - id: referral_goal
        any_of: [求助渠道, 预约咨询, 联系心理中心, 找心理咨询, 准备求助]
      - id: seek_psychological_help
        all_of:
          - [想找, 想联系, 预约, 寻求]
          - [心理咨询, 心理老师, 心理中心, 支持渠道]
    weak:
      - id: support_help
        any_of: [怎么求助, 找谁支持]
  context:
    - id: referral_arguments
      argument_keys: [campus, preferred_channel, availability]
  exclude:
    - id: ordinary_campus_consultation
      all_of:
        - [咨询, 求助]
        - [宿舍报修, 办事流程, 奖学金]
semantic_examples:
  - 我想找心理咨询，希望了解如何选择渠道并准备预约。
  - 情绪困扰持续了一段时间，想联系校内心理支持。
  - 希望找合适的心理老师，并整理第一次联系时要说明的情况。
---

# Referral Resource Guidance

## Workflow

- Normalize asking for support as a practical next step, not a personal failure.
- Recommend only the channels needed for the assigned goal; do not default to listing every possible person or service.
- Avoid inventing phone numbers, office hours, names, or policies not present in retrieved knowledge.
- When knowledge is insufficient, phrase resources generically and invite the student to use official campus channels.
- For immediate danger, prioritize emergency and nearby human support over ordinary appointment planning.
- Immediate danger remains governed by the fixed safety path. Do not create another WorkItem or ask ordinary missing-information questions during specialist execution.
