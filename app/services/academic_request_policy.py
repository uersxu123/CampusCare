from __future__ import annotations


PLAN_NOUNS = ("学习计划", "复习计划", "学习规划", "复习规划", "课程计划", "课程表", "学习安排", "复习安排")
PLAN_VERBS = ("帮我", "制定", "制订", "生成", "设计", "安排", "规划", "做一份")
PERSONAL_CONSTRAINTS = ("我的课程表", "我的截止", "我这学期", "我本学期", "可用时间", "每周能学", "每天能学")
PERSONAL_PLAN_MARKERS = ("计划", "规划", "安排", "复习")


def is_concrete_study_plan_request(text: str) -> bool:
    normalized = text.strip()
    return (
        any(noun in normalized for noun in PLAN_NOUNS)
        and any(verb in normalized for verb in PLAN_VERBS)
    ) or (
        any(term in normalized for term in PERSONAL_CONSTRAINTS)
        and any(marker in normalized for marker in PERSONAL_PLAN_MARKERS)
    )
