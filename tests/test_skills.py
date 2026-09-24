import tempfile
import unittest
from pathlib import Path

from app.core.enums import RiskLevel
from app.services.skills import MindBridgeSkillRegistry, SkillLoadError, SkillManager


def write_skill(root: Path, name: str, text: str) -> None:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")


class SkillRegistryTests(unittest.TestCase):
    def test_registry_loads_unmigrated_skill_for_named_access_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, "demo_skill", """---
name: demo_skill
description: Use for a clear and sufficiently described demo scenario.
---
# Demo
## Workflow
- Do one thing.
""")
            skill = MindBridgeSkillRegistry(root).get_required("demo_skill")
            self.assertEqual(skill.name, "demo_skill")
            self.assertEqual(SkillManager(root).match("response", "CHAT", "demo"), [])
            self.assertTrue(any("matching_version=2" in item.message for item in skill.validation_issues()))

    def test_skill_requires_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, "bad", "# Missing metadata")
            with self.assertRaises(SkillLoadError):
                MindBridgeSkillRegistry(root).get_required("bad")

    def test_repository_skills_have_complete_target_frontmatter(self):
        scenario_names = {
            "academic_stress_planning", "academic_warning_recovery", "thesis_research_progress",
            "further_study_career_decision", "campus_procedure_navigation", "financial_aid_awards_guidance",
            "dormitory_life_guidance", "anxiety_grounding_support", "sleep_routine_support",
            "referral_resource_guidance",
        }
        skills = MindBridgeSkillRegistry().list_skills()
        self.assertEqual(len(skills), 13)
        for skill in skills:
            with self.subTest(skill=skill.name):
                self.assertEqual(skill.matching_version, 2)
                self.assertNotIn("always_match", skill.metadata)
                if skill.name in scenario_names:
                    self.assertEqual(skill.selection_mode, "scenario")
                    self.assertEqual(skill.selection_group, "primary_strategy")
                    self.assertTrue(skill.matching)
                    self.assertGreaterEqual(len(skill.semantic_examples), 2)
                    self.assertIsInstance(skill.metadata["keywords"], dict)
                elif skill.name == "supportive_response_baseline":
                    self.assertEqual(skill.selection_mode, "baseline")
                else:
                    self.assertEqual(skill.selection_mode, "fixed")

    def test_repository_positive_and_negative_examples(self):
        manager = SkillManager()
        cases = [
            ("academic_planning", "ACADEMIC", "明天考试，帮我安排今晚各科复习任务。", "academic_stress_planning"),
            ("academic_planning", "ACADEMIC", "高数挂科了，帮我梳理补考和重修的补救步骤。", "academic_warning_recovery"),
            ("academic_planning", "ACADEMIC", "下周开题，帮我拆分每天的准备任务。", "thesis_research_progress"),
            ("academic_planning", "ACADEMIC", "考研和就业怎么选，帮我比较成本和准备时间。", "further_study_career_decision"),
            ("campus_affairs", "CAMPUS", "办理在读证明需要哪些步骤，应该找哪个部门？", "campus_procedure_navigation"),
            ("campus_affairs", "CAMPUS", "助学金申请需要准备哪些材料？", "financial_aid_awards_guidance"),
            ("campus_affairs", "CAMPUS", "宿舍水管漏水，怎么报修和联系负责人员？", "dormitory_life_guidance"),
            ("psychological_support", "MENTAL", "我现在很惊恐，想先缓下来，可以做什么？", "anxiety_grounding_support"),
            ("psychological_support", "MENTAL", "最近睡不着，帮我安排今晚的睡前步骤。", "sleep_routine_support"),
            ("psychological_support", "MENTAL", "我想找心理咨询，怎么选择求助渠道和准备预约？", "referral_resource_guidance"),
        ]
        for agent, intent, text, expected in cases:
            with self.subTest(expected=expected):
                names = {item.skill.name for item in manager.match(agent, intent, text, RiskLevel.LOW)}
                self.assertIn(expected, names)
        self.assertEqual(manager.match("academic_planning", "ACADEMIC", "论文里的置信区间是什么意思？"), [])
        dorm_names = {item.skill.name for item in manager.match("campus_affairs", "CAMPUS", "我在宿舍整理助学金申请材料，需要准备什么？")}
        self.assertNotIn("dormitory_life_guidance", dorm_names)

    def test_fixed_skills_remain_named_access_only(self):
        manager = SkillManager()
        self.assertNotIn("high_risk_safety_plan", {item.skill.name for item in manager.match("response", "RISK", "我不想活了", RiskLevel.HIGH)})
        self.assertIn("High Risk Safety Plan", MindBridgeSkillRegistry().get_required("high_risk_safety_plan").body)
        self.assertIn("{{report_id}}", MindBridgeSkillRegistry().template_for("counselor_handoff_summary"))

    def test_career_body_preserves_no_execution_question_contract(self):
        skill = MindBridgeSkillRegistry().get_required("further_study_career_decision")
        self.assertIn("不在执行期追问", skill.body)
        self.assertIn("只有三项以上确实需要横向比较时才使用表格", skill.body)


if __name__ == "__main__":
    unittest.main()
