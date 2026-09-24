---
name: anxiety_grounding_support
description: 帮助当下紧张、惊恐或思绪失控的学生尝试温和的稳定动作，把注意力转回眼前能够完成的一步。
agents: [psychological_support]
intents: [MENTAL]
keywords:
  domain: [焦虑, 惊恐, 恐慌, 思绪失控]
  goal: [缓解, 稳定, 平复, 静下来]
  context: [当下, 身体紧张, 注意力]
enabled: true
optional: true
priority: 80
max_chars: 1800
matching_version: 2
selection_mode: scenario
selection_group: primary_strategy
matching:
  domain:
    strong:
      - id: acute_anxiety_state
        any_of: [焦虑, 惊恐, 恐慌, 思绪失控, 静不下来]
    weak:
      - id: broad_tension_state
        any_of: [紧张, 心慌, 崩溃]
  goal:
    strong:
      - id: immediate_stabilization_goal
        any_of: [先缓下来, 先缓解, 稳定下来, 平复下来, 静下来, 当下缓解]
      - id: request_grounding_action
        all_of:
          - [现在, 当下, 先]
          - [可以做什么, 怎么办, 帮我缓解]
    weak:
      - id: anxiety_help
        any_of: [焦虑怎么办, 惊恐怎么办]
  context:
    - id: acute_anxiety_arguments
      argument_keys: [symptoms, duration, immediate_context]
  exclude:
    - id: define_anxiety_only
      all_of:
        - [焦虑这个词, 焦虑的定义]
        - [什么意思, 是什么]
semantic_examples:
  - 我现在很惊恐，想先缓下来，请给我一个温和的稳定动作。
  - 思绪停不下来，希望把注意力拉回眼前能完成的一步。
  - 当下非常紧张心慌，想先平复再处理其他事情。
---

# Anxiety Grounding Support

## Workflow

- Name the anxiety or panic experience without treating it as a diagnosis.
- Offer one immediate grounding action, such as slower breathing, noticing five visible objects, or placing both feet on the floor.
- Keep body instructions gentle and optional; do not imply the student is failing if the method does not work.
- Help the student separate the next small action from the whole problem.
- If symptoms are severe, repeated, or interfere with class, sleep, or safety, suggest contacting the school counseling center or a trusted adult.
- Do not force breathing exercises. Do not reclassify intent, create another WorkItem, or ask ordinary missing-information questions; follow the Coordinator's clarification decision.
