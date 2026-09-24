from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from app.services.skills import MindBridgeSkillRegistry, SkillLoadError, SkillManager, _render_template


def write_skill(root, name="demo", body=None, **metadata):
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = {"name": name, "description": "用于验证加载、预算以及配置异常隔离的测试技能。", **metadata}
    path.write_text("---\n" + yaml.safe_dump(fields, allow_unicode=True) + "---\n" +
                    (body if body is not None else "## Workflow\n完整的业务操作指引。"), encoding="utf-8")
    return path


def scenario_fields(**extra):
    return {
        "agents": ["response"], "intents": ["CHAT"], "matching_version": 2,
        "selection_mode": "scenario", "selection_group": "primary_strategy",
        "semantic_examples": ["示例任务一", "示例任务二"],
        "matching": {
            "domain": {"strong": [{"id": "d", "any_of": ["场景"]}], "weak": []},
            "goal": {"strong": [{"id": "g", "any_of": ["处理"]}], "weak": []},
            "context": [], "exclude": [],
        },
        **extra,
    }


def test_scenario_context_is_complete_or_removed(tmp_path):
    body = "## Workflow\n" + "完整步骤。" * 30
    write_skill(tmp_path, "demo", body=body, max_chars=500, **scenario_fields())
    assert SkillManager(tmp_path, total_chars=20).match("response", "CHAT", "场景处理") == []
    matches = SkillManager(tmp_path, total_chars=500).match("response", "CHAT", "场景处理")
    assert len(matches) == 1
    assert matches[0].prompt_context.endswith("完整步骤。")
    assert "完整步骤。" * 30 in matches[0].prompt_context


def test_invalid_encoding_does_not_break_other_skills(tmp_path):
    path = write_skill(tmp_path, "bad")
    path.write_bytes(bytes([255, 254, 255]))
    write_skill(tmp_path, "good")
    manager = SkillManager(tmp_path)
    assert "bad" in manager.errors
    assert manager.registry.get_required("good").name == "good"
    assert len(manager.registry.status_items()) == 2


def test_read_failure_uses_skill_error_contract(tmp_path):
    path = write_skill(tmp_path)
    with patch.object(Path, "read_text", side_effect=PermissionError("access denied")):
        with pytest.raises(SkillLoadError):
            MindBridgeSkillRegistry(tmp_path)._load_skill_file(path)


def test_required_lookup_is_not_blocked_by_unrelated_bad_file(tmp_path):
    write_skill(tmp_path, "aaa_bad", selection_mode="unknown", matching_version=2)
    write_skill(tmp_path, "required")
    assert MindBridgeSkillRegistry(tmp_path).get_required("required").name == "required"


def test_required_lookup_rejects_invalid_target(tmp_path):
    write_skill(tmp_path, "required", max_chars=0)
    with pytest.raises(SkillLoadError):
        MindBridgeSkillRegistry(tmp_path).get_required("required")


def test_unclosed_handoff_template_is_rejected_during_refresh(tmp_path):
    write_skill(tmp_path, "counselor_handoff_summary", body="## Workflow\n```text\n报告：{{summary}}")
    assert "counselor_handoff_summary" in SkillManager(tmp_path).errors


def test_template_values_are_not_reinterpreted_as_placeholders():
    result = _render_template("摘要：{{summary}}\n下一步：{{next_steps}}",
                              {"summary": "用户原话含有 {{next_steps}}", "next_steps": "联系辅导员"})
    assert result == "摘要：用户原话含有 {{next_steps}}\n下一步：联系辅导员"


@pytest.mark.parametrize("options", [{"max_matches": -1}, {"total_chars": -1}])
def test_negative_manager_limits_are_rejected(tmp_path, options):
    with pytest.raises(ValueError):
        SkillManager(tmp_path, **options)


def test_utf8_bom_is_accepted(tmp_path):
    path = write_skill(tmp_path)
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    assert MindBridgeSkillRegistry(tmp_path).get_required("demo").name == "demo"


def test_duplicate_required_name_fails_explicitly(tmp_path):
    path = write_skill(tmp_path, "required")
    duplicate = tmp_path / "alias" / "SKILL.md"
    duplicate.parent.mkdir()
    duplicate.write_bytes(path.read_bytes())
    with pytest.raises(SkillLoadError, match="duplicate"):
        MindBridgeSkillRegistry(tmp_path).get_required("required")


def test_scenario_always_match_unknown_mode_and_duplicate_rule_are_isolated(tmp_path):
    write_skill(tmp_path, "always", always_match=True, **scenario_fields())
    write_skill(tmp_path, "mode", matching_version=2, selection_mode="mystery")
    duplicate = scenario_fields()
    duplicate["matching"]["goal"]["strong"][0]["id"] = "d"
    write_skill(tmp_path, "duplicate", **duplicate)
    manager = SkillManager(tmp_path)
    assert set(manager.errors) == {"always", "mode", "duplicate"}


def test_refresh_recovers_corrected_skill_and_clears_error(tmp_path):
    write_skill(tmp_path, selection_mode="invalid", matching_version=2)
    manager = SkillManager(tmp_path)
    assert "demo" in manager.errors
    write_skill(tmp_path, **scenario_fields())
    manager.refresh()
    assert manager.errors == {}


def test_manager_isolates_duplicate_names_and_status_reports_duplicate(tmp_path):
    path = write_skill(tmp_path, "first")
    duplicate = tmp_path / "second" / "SKILL.md"
    duplicate.parent.mkdir()
    duplicate.write_bytes(path.read_bytes())
    manager = SkillManager(tmp_path)
    assert "second" in manager.errors
    duplicate_status = next(item for item in manager.registry.status_items() if item["path"].endswith("/second/SKILL.md"))
    assert duplicate_status["status"] == "FAILED"
