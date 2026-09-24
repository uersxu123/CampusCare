from types import SimpleNamespace

import pytest

import app.agents.autonomous as autonomous
from app.agents.autonomous import (
    AcademicPlanningAgent,
    CampusAffairsAgent,
    PsychologicalSupportAgent,
)
from app.services.tool_models import AgentLoopResult, AiToolDefinition


class _Registry:
    def definitions_for_agent(self, _agent_name):
        return [
            AiToolDefinition(
                "chat_readonly__rag_search",
                "本地知识检索",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "facets": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
            )
        ]


class _Context:
    safety_context = None

    def for_specialist(self, work_item, dependency_results):
        return {
            "workItem": work_item,
            "dependencyResults": dependency_results,
        }


def _capture_system_prompt(monkeypatch, agent_cls, work_item, dependency_results=None):
    captured = {}
    runtime = SimpleNamespace(registry=_Registry(), executor=object())
    services = SimpleNamespace(
        tool_runtime=runtime,
        skill_manager=None,
        context_packet=_Context(),
        settings=SimpleNamespace(
            agent_loop_max_model_rounds=3,
            agent_loop_max_tool_calls=4,
            agent_loop_max_result_chars=12000,
            agent_loop_deadline_seconds=100,
        ),
        model_registry=SimpleNamespace(client_for=lambda _name: object()),
    )

    class _Loop:
        def __init__(self, **_kwargs):
            pass

        def run(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            captured["tools"] = kwargs["tools"]
            return AgentLoopResult("已完成", (), 1, "COMPLETED")

    monkeypatch.setattr(autonomous, "AgentLoop", _Loop)
    agent = agent_cls(services)
    agent._run_loop(work_item, dependency_results or [])
    return captured["messages"][0].content, captured["tools"]


def test_academic_prompt_contains_contrastive_rag_gate_examples(monkeypatch):
    system, tools = _capture_system_prompt(
        monkeypatch,
        AcademicPlanningAgent,
        {
            "objective": "确认挂科后的处理方式",
            "sourceText": "挂科以后学校是直接重修还是可以补考？",
            "knownArguments": {},
            "evidenceFacets": ["挂科后的补考或重修规定"],
        },
    )

    assert "【ACADEMIC 工具边界】" in system
    assert "还有20天考高数" in system
    assert "挂科以后学校是直接重修还是可以补考" in system
    assert "dependencyResults 已核验‘该课程可以参加补考’" in system
    assert "具体补考日期" in system
    assert "基于实际可见证据回答并明确缺口" in system
    assert "只有完成当前 WorkItem 仍需要新的校方事实时才调用 rag_search" in system
    assert [tool.name for tool in tools] == ["chat_readonly__rag_search"]


def test_campus_prompt_uses_task_text_without_legacy_facet_instructions(monkeypatch):
    system, tools = _capture_system_prompt(
        monkeypatch,
        CampusAffairsAgent,
        {
            "objective": "解释奖助兼得及休学复学处理",
            "sourceText": "拿了国家奖学金，休学后还能拿助学金吗？复学后怎么办？",
            "knownArguments": {},
            "evidenceFacets": ["奖助兼得", "休学期间发放", "复学后处理"],
        },
    )

    assert "【CAMPUS 工具边界】" in system
    assert "因病休学需要准备哪些材料" in system
    assert "query 使用完整的 workItem.taskText" in system
    assert "不生成 facets 或预先拆分查询" in system
    assert "每个 WorkItem 最多执行一次 rag_search" in system
    assert "不得扩展任务或换个说法重复查询" in system
    assert "已有资料只是原文被省略时优先回读" in system
    assert "整理成明天办理的待办清单" in system
    assert "帮我写一段休学申请理由" in system
    assert "才允许第二次检索" not in system
    assert [tool.name for tool in tools] == ["chat_readonly__rag_search"]


def test_mental_prompt_defaults_to_support_without_rag(monkeypatch):
    system, tools = _capture_system_prompt(
        monkeypatch,
        PsychologicalSupportAgent,
        {
            "objective": "缓解考试焦虑",
            "sourceText": "最近考试压力特别大，晚上一直睡不好，很焦虑。",
            "knownArguments": {},
            "evidenceFacets": [],
        },
    )

    assert "【MENTAL 工具边界】" in system
    assert "默认不依赖学校知识库" in system
    assert "不得为了获取一般心理学背景知识而调用 RAG" in system
    assert "最近考试压力特别大" in system
    assert "学校心理咨询中心怎么预约" in system
    assert "不重新查询挂科制度" in system
    # 工具仍然可用，真正是否调用由 LLM 根据当前 WorkItem 门控。
    assert [tool.name for tool in tools] == ["chat_readonly__rag_search"]


@pytest.mark.parametrize(
    "agent_cls",
    [AcademicPlanningAgent, CampusAffairsAgent, PsychologicalSupportAgent],
)
def test_common_gate_uses_injected_upstream_results_without_rechecking(monkeypatch, agent_cls):
    system, _tools = _capture_system_prompt(
        monkeypatch,
        agent_cls,
        {
            "objective": "处理当前工作项",
            "sourceText": "处理当前工作项",
            "knownArguments": {},
            "evidenceFacets": [],
        },
        dependency_results=[
            {
                "workItemId": "wi-upstream",
                "status": "COMPLETED",
                "reasonCode": "EVIDENCE_COMPLETE",
                "answerBrief": "上游已核验事实",
            }
        ],
    )

    assert "dependencyResults 只包含当前 WorkItem 的直接 HARD_DATA 上游结果" in system
    assert "PARTIAL 中明确缺失的事实不能视为已知或已核验" in system
    assert "不要为了保险、背景补充或重复确认 dependencyResults 中已经核验的结论而再次检索" in system


def test_domain_examples_are_isolated_per_specialist(monkeypatch):
    academic, _ = _capture_system_prompt(
        monkeypatch,
        AcademicPlanningAgent,
        {"objective": "确认制度", "sourceText": "挂科后怎么办", "knownArguments": {}, "evidenceFacets": []},
    )
    campus, _ = _capture_system_prompt(
        monkeypatch,
        CampusAffairsAgent,
        {"objective": "确认流程", "sourceText": "休学怎么办", "knownArguments": {}, "evidenceFacets": []},
    )
    mental, _ = _capture_system_prompt(
        monkeypatch,
        PsychologicalSupportAgent,
        {"objective": "情绪支持", "sourceText": "我很焦虑", "knownArguments": {}, "evidenceFacets": []},
    )

    assert "学校心理咨询中心怎么预约" not in academic
    assert "还有20天考高数" not in campus
    assert "因病休学需要准备哪些材料" not in mental
