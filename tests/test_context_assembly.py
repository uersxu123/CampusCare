from copy import deepcopy
from dataclasses import replace
import json

import pytest

from app.core.config import Settings
from app.core.enums import IntentType, KnowledgeDomain, RiskLevel
from app.schemas.dtos import AiMessage
from app.services.context_builder import (
    ContextBuilder, ContextManifest, EpisodeMemory, SelectedUserMemory,
    TurnContextPacket,
)
from app.services.memory import MemoryMessage


@pytest.fixture
def packet():
    return TurnContextPacket(
        packet_version=3, user_id=1, session_id=1, session_public_id="session",
        current_message_id=10, current_input="current question", summary_version=1,
        summary_covered_until_message_id=1,
        structured_summary={"current_goal": {"text": "goal"},
                            "confirmed_facts": [{"text": "fact " * 500}],
                            "active_topics": [f"topic-{i}" for i in range(6)]},
        recent_messages=tuple(MemoryMessage(id=i, role="user", content=f"history-{i} " * 500)
                              for i in range(2, 8)),
        selected_user_memories=(SelectedUserMemory(
            memory_id=1, public_id="profile", category="preference", memory_key="format",
            content="profile " * 500, relevance_score=1, source_message_id=1,
        ),),
        relevant_history=(EpisodeMemory(
            episode_id=1, public_id="episode", session_id=1,
            source_start_message_id=1, source_end_message_id=1,
            summary_text="episode " * 500, content_hash="hash", version=1,
            relevance_score=1,
        ),),
        clarification_state={"intent": "CAMPUS", "details": "clarification " * 500},
        safety_context=None, manifest=ContextManifest(), intent_context_max_tokens=256,
    )


@pytest.fixture
def builder():
    settings = Settings(_env_file=None, ai_provider="mock").model_copy(update={
        "context_input_max_tokens": 256, "memory_base_max_tokens": 256,
        "context_summary_max_tokens": 1, "context_user_memory_max_tokens": 1,
        "context_knowledge_max_tokens": 1,
    })
    return ContextBuilder(None, settings, cache=object())


def test_memory_assembly_preserves_selected_content_and_is_independent(builder, packet):
    before = packet.as_payload()
    base = builder.render_base_memory(packet, "ACADEMIC", budget_tokens=1)
    assert [m["content"] for m in base["workingMemory"]] == [m.content for m in packet.recent_messages]
    assert base["conversationSummary"] == packet.structured_summary
    assert base["relevantHistory"][0]["summary"] == packet.relevant_history[0].summary_text
    assert base["userProfile"][0]["content"] == packet.selected_user_memories[0].content
    base["conversationSummary"]["confirmed_facts"].clear()
    assert packet.as_payload() == before
    private = replace(packet.selected_user_memories[0], sensitivity="MENTAL")
    assert builder.render_base_memory(replace(packet, selected_user_memories=(private,)), "CAMPUS")["userProfile"] == []


@pytest.mark.parametrize("synthesis", [False, True])
def test_agent_assembly_preserves_over_budget_evidence(builder, packet, synthesis):
    rows = [{"workItemId": "w1", "answerBrief": "answer " * 1000,
             "evidenceItems": [{"evidenceId": f"e{i}", "content": f"evidence-{i} " * 1000}
                               for i in range(3)]}]
    before = deepcopy(rows)
    if synthesis:
        built = builder.build_synthesis_prompt(packet=packet, system="system", route_plan={},
                                               specialist_results=rows)
        key = "specialistResults"
    else:
        built = builder.build_specialist_prompt(packet=packet, audience="ACADEMIC", system="system",
                                                work_item={}, dependency_results=rows, tool_schemas=[])
        key = "dependencyResults"
    assert json.loads(built.messages[-1].content)[key] == before
    assert list(built.knowledge_items) == before[0]["evidenceItems"]
    assert built.manifest.estimated_total_tokens > built.manifest.budget_tokens
    assert not built.manifest.protected_overflow
    assert not built.manifest.dropped_blocks
    assert rows == before


def test_specialist_assembles_all_selected_skills_without_budget_trimming(builder, packet):
    scenario = "应用 skill: scenario\n## Workflow\n" + "完整场景步骤。" * 200
    baseline = "应用 skill: baseline\n## Workflow\n必要基线。"
    built = builder.build_specialist_prompt(
        packet=packet, audience="MENTAL", system="system", work_item={}, dependency_results=[], tool_schemas=[],
        skill_items=[
            {"name": "baseline", "selection_mode": "baseline", "prompt_context": baseline},
            {"name": "scenario", "selection_mode": "scenario", "prompt_context": scenario, "rule_score": 0.9},
        ],
    )
    assert baseline in built.messages[0].content
    assert scenario in built.messages[0].content
    assert built.injected_skill_ids == ("baseline", "scenario")
    assert built.manifest.skill_ids == ("baseline", "scenario")
    assert not any(item.source_id == "scenario" and item.reason == "BUDGET_REJECTED" for item in built.manifest.dropped_blocks)


def test_legacy_response_assembly_does_not_apply_block_or_total_limits(builder, packet):
    knowledge = [{"reference_id": f"k{i}", "content": f"knowledge-{i} " * 1000} for i in range(2)]
    built = builder.build_response_prompt(
        packet=packet, system_messages=[AiMessage(role="system", content="system")],
        skill_items=[{"name": "skill", "prompt_context": "skill " * 500}],
        knowledge_items=knowledge, intent=IntentType.CAMPUS, risk=RiskLevel.LOW,
        domain=KnowledgeDomain.CAMPUS_SERVICE,
    )
    text = "\n".join(m.content for m in built.messages)
    assert list(built.knowledge_items) == knowledge
    for content in [*(k["content"] for k in knowledge), "skill " * 500,
                    packet.selected_user_memories[0].content, "fact " * 500,
                    *(m.content for m in packet.recent_messages)]:
        assert content in text
    assert built.manifest.estimated_total_tokens > built.manifest.budget_tokens
    assert not built.manifest.protected_overflow
    assert not built.manifest.dropped_blocks


def test_packet_views_preserve_selected_text_despite_small_budget(packet):
    understanding = packet.for_understanding(packet.current_input)
    assert understanding["recent_messages"] == [
        {"id": m.id, "role": m.role, "content": m.content} for m in packet.recent_messages[-4:]
    ]
    assert understanding["active_topics"] == packet.structured_summary["active_topics"]
    assert understanding["clarification_state"] == packet.clarification_state
    assert understanding["profile_hints"][0]["content"] == packet.selected_user_memories[0].content
    assert understanding["history_hints"][0]["summary"] == packet.relevant_history[0].summary_text
    assert understanding["context_truncated"] is False
    safety = packet.for_safety()
    assert len(safety["recent_messages"]) == 6
    assert safety["historical_risk_hints"][0]["summary"] == packet.relevant_history[0].summary_text
    assert safety["current_input"] == packet.current_input
    specialist = packet.for_specialist({"intent": "ACADEMIC"}, [])
    assert len(specialist["baseMemory"]["workingMemory"]) == len(packet.recent_messages)
    assert specialist["baseMemory"]["relevantHistory"][0]["summary"] == packet.relevant_history[0].summary_text
