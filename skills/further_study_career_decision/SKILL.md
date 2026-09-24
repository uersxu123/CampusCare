---
name: further_study_career_decision
description: 帮助学生比较升学、实习和就业方向，结合个人约束权衡选项，并安排低成本的验证行动或准备步骤。
agents: [academic_planning]
intents: [ACADEMIC]
keywords:
  domain: [考研, 保研, 留学, 实习, 求职, 就业, 升学]
  goal: [比较方向, 选择, 权衡, 准备步骤]
  context: [时间窗口, 成本, 当前基础]
enabled: true
optional: true
priority: 85
max_chars: 2600
matching_version: 2
selection_mode: scenario
selection_group: primary_strategy
matching:
  domain:
    strong:
      - id: development_direction
        any_of: [考研, 保研, 留学, 实习, 求职, 就业, 升学]
    weak:
      - id: broad_career_topic
        any_of: [职业方向, 发展方向]
  goal:
    strong:
      - id: compare_or_choose
        any_of: [怎么选, 如何选择, 比较成本, 权衡, 方向准备, 准备步骤]
      - id: compare_options
        all_of:
          - [比较, 对比, 选择]
          - [考研, 就业, 实习, 留学, 方向]
    weak:
      - id: direction_help
        any_of: [未来怎么办, 职业怎么办]
  context:
    - id: decision_arguments
      argument_keys: [deadline, constraints, preparation, options]
  exclude:
    - id: only_registration_date
      all_of:
        - [报名, 截止]
        - [日期, 哪天, 什么时候]
    - id: only_result_lookup
      all_of:
        - [录取结果, 招聘结果]
        - [查询, 什么时候]
semantic_examples:
  - 考研和就业之间拿不定主意，希望比较成本、时间和准备要求。
  - 想比较实习与继续升学，并设计一个低成本的验证行动。
  - 有几个职业方向，希望结合个人约束安排近期准备步骤。
---

# 升学与职业决策

## Scope

适用于发展方向比较、准备规划和短期试探。不用于预测录取、录用、薪资或长期职业结果。

## Required Context

- 待决定的选项。
- 时间窗口、核心约束、当前准备基础中的至少两类信息。
- 用户已经提供的事实、待核实事实和主要不确定性。

## Workflow

1. 先判断现有信息是否足以区分选项。
2. 如果时间窗口、核心约束、当前准备基础只得到一类或更少信息，明确列出有限假设并只给可逆的低成本行动，不在执行期追问。
3. 信息充分后，用匹配度、门槛、成本、时间、风险和可逆性比较选项。
4. 明确区分用户已提供事实、待核实事实和建议，不用泛化假设填补空白。
5. 标出需要官方核验的资格、日期和材料。
6. 为最高不确定项设计一个近期、低成本、短周期验证行动。

## Response Contract

若信息不足，输出有限假设和一个低成本验证行动，不生成完整比较，也不向用户发起澄清。
若信息充分，优先使用简短要点；只有三项以上确实需要横向比较时才使用表格。
给出一个近期低成本验证动作，明确区分事实、待核实项和建议，不替学生作最终决定，也不追加固定的通用结尾。

## Escalation and Boundaries

资格、申请时间、项目要求和招聘规则必须通过 RAG、受控工具或官方渠道确认。必要时建议联系导师、院系、职业中心、校友或正式招生招聘渠道；不得保证录取、推荐资格或工作机会。
