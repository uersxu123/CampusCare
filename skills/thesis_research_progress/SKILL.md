---
name: thesis_research_progress
description: 帮助学生解决论文、开题或毕设推进中的具体卡点，将当前阶段目标拆成可检查的产出、短期任务和检查点，并整理需要导师反馈的问题。
agents: [academic_planning]
intents: [ACADEMIC]
keywords:
  domain: [论文, 开题, 毕设, 文献综述]
  goal: [科研产出, 拆解任务, 进度安排, 反馈请求]
  context: [写作, 导师, 实验, 截止时间]
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
      - id: thesis_topic
        any_of: [论文, 开题, 毕设, 文献综述]
    weak:
      - id: research_context
        any_of: [写作, 导师, 实验]
  goal:
    strong:
      - id: research_progress_goal
        any_of: [拆解论文任务, 安排论文进度, 研究计划, 整理论文进展, 反馈请求]
      - id: arrange_research_work
        all_of:
          - [安排, 拆分, 规划, 整理]
          - [任务, 进度, 每天, 产出, 导师反馈]
    weak:
      - id: blocked_research_help
        any_of: [论文怎么办, 开题怎么办, 推进困难]
  context:
    - id: research_arguments
      argument_keys: [deadline, stage, completed, blocker]
    - id: research_deadline
      all_of:
        - [今晚, 明天, 本周, 下周]
        - [交论文, 提交, 开题, 截止]
  exclude:
    - id: only_define_research_term
      all_of:
        - [只解释, 什么意思, 只想知道]
        - [术语, 名词, 置信区间]
semantic_examples:
  - 下周要开题，希望拆分每天的准备任务和检查点。
  - 论文写到一半推进困难，希望整理下一步产出和向导师反馈的问题。
  - 毕设实验卡住了，需要形成短期任务和可检查的研究进展。
---

# 论文与科研推进

## Scope

适用于研究任务卡点、阶段规划和导师沟通。不用于代写论文、伪造文献、数据、实验、引用或反馈。

## Required Context

- 当前阶段、最近交付物和期限。
- 已完成内容、已有反馈及一个最具体的阻碍。

## Workflow

1. 判断阻碍属于知识、决策、执行还是沟通问题。
2. 把下一步缩成一个可检查的产出，如问题单、提纲、文献表或草稿段落。
3. 设置有明确停止点的短工作块，并记录未解决问题。
4. 需要导师输入时，整理进展、证据、阻碍和一个聚焦请求。
5. 对正式节点和格式要求使用 RAG 核验。

## Response Contract

按当前问题指出必要的卡点类型、立即产出和后续检查点；需要导师确认的内容列为待核实项，不在执行阶段向用户追加普通缺参追问。内容紧凑、可执行，不承诺评价结果。

## Escalation and Boundaries

培养方案、开题、评审、答辩和提交规则须查阅最新正式来源。涉及学术诚信、研究安全或无法自行解决的实验风险时，应联系导师或负责人员；不得保证通过、发表或毕业。
