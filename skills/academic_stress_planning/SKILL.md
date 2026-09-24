---
name: academic_stress_planning
description: 帮助学生处理日常课程、考试复习和作业负担，将学习任务缩成可执行的短期安排和停止点。
agents: [academic_planning]
intents: [ACADEMIC]
keywords:
  domain: [考试, 课程, 作业, 复习]
  goal: [安排复习, 拆解学习任务, 制定学习计划]
  context: [压力, 截止时间, 可用时间]
enabled: true
optional: true
priority: 70
max_chars: 1800
matching_version: 2
selection_mode: scenario
selection_group: primary_strategy
matching:
  domain:
    strong:
      - id: everyday_study_load
        any_of: [考试, 课程, 作业, 复习]
    weak:
      - id: broad_academic_pressure
        any_of: [学习压力, 学业压力, 功课]
  goal:
    strong:
      - id: study_plan_goal
        any_of: [安排复习, 拆解学习任务, 制定学习计划, 复习计划]
      - id: arrange_study_tasks
        all_of:
          - [安排, 拆分, 规划]
          - [复习, 学习任务, 作业, 各科]
    weak:
      - id: generic_study_help
        any_of: [学习怎么办, 功课怎么办]
  context:
    - id: study_deadline_argument
      argument_keys: [deadline, available_time, course]
    - id: near_study_deadline
      all_of:
        - [今晚, 明天, 本周, 下周]
        - [考试, 作业, 截止]
  exclude:
    - id: only_explain_term
      all_of:
        - [只解释, 什么意思]
        - [术语, 名词, 概念]
semantic_examples:
  - 明天有考试，希望安排今晚各科的复习任务和停止时间。
  - 作业和课程任务堆在一起，希望拆成这周能完成的小步骤。
  - 最近学习负担很重，需要一个短期可执行的复习安排。
---

# Academic Stress Planning

## Workflow

- Acknowledge the pressure without equating grades with personal worth.
- Help the student choose one narrow task, one time block, and one realistic stopping point for the assigned WorkItem.
- Prefer concrete planning over motivational slogans.
- Encourage contacting a teacher, advisor, classmate, counselor, or academic support channel when the load is no longer manageable alone.
- If the student expresses hopelessness or danger, defer to high-risk safety planning.
- Do not reclassify the global intent, create another WorkItem, or ask an ordinary missing-information question during execution; clarification belongs to the Coordinator.
