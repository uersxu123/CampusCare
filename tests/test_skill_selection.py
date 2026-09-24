from dataclasses import replace

import yaml

from app.core.config import Settings
from app.core.enums import RiskLevel
from app.services.skill_semantics import SemanticDecision
from app.services.skills import SkillManager, SkillMatchInput


class FakeSemanticService:
    def __init__(self, selected_id=None, reason="SEMANTIC_AMBIGUOUS", scores=None):
        self.selected_id = selected_id
        self.reason = reason
        self.scores = scores or {}
        self.calls = []

    def disambiguate(self, query, candidates, **_kwargs):
        self.calls.append((query, [item.skill_id for item in candidates]))
        scores = self.scores or {item.skill_id: 0.8 for item in candidates}
        return SemanticDecision(self.selected_id, scores, self.reason, 0.01, True, 2, 0, 3)


def write_scenario(root, name, *, agents=("academic_planning",), intents=("ACADEMIC",), enabled=True,
                   priority=50, domain="课程", goal="安排", context_key=None, exclude=None, group="primary_strategy"):
    matching = {
        "domain": {"strong": [{"id": f"{name}_d", "any_of": [domain]}], "weak": []},
        "goal": {"strong": [{"id": f"{name}_g", "any_of": [goal]}], "weak": []},
        "context": ([{"id": f"{name}_c", "argument_keys": [context_key]}] if context_key else []),
        "exclude": ([{"id": f"{name}_x", "any_of": [exclude]}] if exclude else []),
    }
    metadata = {
        "name": name, "description": f"{name} 用于明确场景和目标，并提供对应的处理流程。",
        "agents": list(agents), "intents": list(intents), "enabled": enabled, "optional": True,
        "priority": priority, "max_chars": 500, "matching_version": 2, "selection_mode": "scenario",
        "selection_group": group, "keywords": {"domain": [domain], "goal": [goal], "context": []},
        "matching": matching, "semantic_examples": [f"{domain}{goal}示例一", f"{domain}{goal}示例二"],
    }
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False) +
                    "---\n## Workflow\n- 完整处理当前任务。\n", encoding="utf-8")


def manager(root, fake=None, **updates):
    settings = Settings(_env_file=None, skill_embedding_provider="disabled", **updates)
    return SkillManager(root, settings=settings, semantic_service=fake)


def request(text="课程安排", **updates):
    base = SkillMatchInput("academic_planning", "ACADEMIC", text, work_item_id="wi-1")
    return replace(base, **updates)


def test_hard_filters_agent_intent_enabled_and_risk(tmp_path):
    write_scenario(tmp_path, "ok")
    write_scenario(tmp_path, "disabled", enabled=False)
    write_scenario(tmp_path, "wrong_agent", agents=("campus_affairs",))
    write_scenario(tmp_path, "wrong_intent", intents=("CAMPUS",))
    fake = FakeSemanticService()
    skill_manager = manager(tmp_path, fake)
    assert [item.skill.name for item in skill_manager.select(request()).matches] == ["ok"]
    assert skill_manager.select(request(risk=RiskLevel.HIGH)).matches == ()
    assert fake.calls == []


def test_all_low_scores_reject_without_embedding(tmp_path):
    write_scenario(tmp_path, "low", domain="课程", goal="安排")
    fake = FakeSemanticService()
    result = manager(tmp_path, fake).select(request("只提到课程"))
    assert result.matches == ()
    assert result.diagnostics["groups"][0]["decision"] == "RULE_BELOW_THRESHOLD"
    assert fake.calls == []


def test_single_high_score_selects_without_embedding(tmp_path):
    write_scenario(tmp_path, "high")
    write_scenario(tmp_path, "low", domain="论文", goal="拆分")
    fake = FakeSemanticService()
    result = manager(tmp_path, fake).select(request())
    assert [item.skill.name for item in result.matches] == ["high"]
    assert result.matches[0].rule_score == 0.9
    assert fake.calls == []


def test_rule_margin_selects_winner_without_embedding_or_priority_override(tmp_path):
    write_scenario(tmp_path, "winner", context_key="deadline", priority=1)
    write_scenario(tmp_path, "runner", priority=999)
    fake = FakeSemanticService()
    result = manager(tmp_path, fake).select(request(known_arguments={"deadline": "明天"}))
    assert [item.skill.name for item in result.matches] == ["winner"]
    assert result.diagnostics["groups"][0]["ruleMarginPoints"] == 10
    assert fake.calls == []


def test_near_high_candidates_only_go_to_embedding(tmp_path):
    write_scenario(tmp_path, "first")
    write_scenario(tmp_path, "second", priority=999)
    write_scenario(tmp_path, "low", domain="论文", goal="拆分")
    fake = FakeSemanticService("second", "SEMANTIC_SELECTED", {"first": 0.72, "second": 0.82})
    result = manager(tmp_path, fake).select(request(objective="课程安排", known_arguments={"private": "不应进入查询"}))
    assert [item.skill.name for item in result.matches] == ["second"]
    assert fake.calls[0][1] == ["first", "second"]
    assert "academic_planning" not in fake.calls[0][0]
    assert "first" not in fake.calls[0][0]
    assert "不应进入查询" not in fake.calls[0][0]


def test_semantic_rejection_never_falls_back_to_priority_or_runner_up(tmp_path):
    write_scenario(tmp_path, "first", priority=1)
    write_scenario(tmp_path, "second", priority=999)
    fake = FakeSemanticService(None, "SEMANTIC_LOW_SCORE", {"first": 0.6, "second": 0.59})
    result = manager(tmp_path, fake).select(request())
    assert result.matches == ()
    assert result.diagnostics["groups"][0]["decision"] == "SEMANTIC_LOW_SCORE"


def test_explicit_exclude_and_objective_cannot_supply_rule_evidence(tmp_path):
    write_scenario(tmp_path, "excluded", exclude="只解释")
    write_scenario(tmp_path, "objective_only", domain="论文", goal="拆分")
    result = manager(tmp_path, FakeSemanticService()).select(request("课程安排，只解释", objective="论文拆分"))
    assert result.matches == ()
    rows = {item["skillId"]: item for item in result.diagnostics["candidates"]}
    assert rows["excluded"]["excluded"] is True
    assert rows["objective_only"]["rulePoints"] == 0


def test_scenario_switch_and_zero_budgets_skip_embedding(tmp_path):
    write_scenario(tmp_path, "one")
    write_scenario(tmp_path, "two")
    for options in ({"skill_scenario_enabled": False}, {"skill_max_matches": 0}, {"skill_total_chars": 0}):
        fake = FakeSemanticService()
        assert manager(tmp_path, fake, **options).select(request()).matches == ()
        assert fake.calls == []
