import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agents.events import AgentArtifact
from app.agents.result import AgentRunResult
from app.core.config import Settings
from app.core.database import Base
from app.core.enums import IntentType, RiskLevel
from app.models.entities import ChatSession, UserAccount
from app.schemas.dtos import AiMessage
from app.services.trace import AgentTraceService


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = UserAccount(username="trace-user", display_name="追踪用户", password_hash="x")
    db.add(user)
    db.flush()
    session = ChatSession(public_id="trace-session", title="trace", user_id=user.id)
    db.add(session)
    db.commit()
    return db, user, session


def _run(secret: str) -> AgentRunResult:
    result_payload = {
        "planId": "plan-1", "workItemId": "wi-1", "intent": "CAMPUS",
        "agentName": "CampusAffairsAgent", "status": "COMPLETED", "reasonCode": "EVIDENCE_COMPLETE",
        "answerBrief": secret,
        "evidenceItems": [{"evidenceId": "ev-1", "title": "奖学金", "content": secret, "source": "official"}],
        "toolSummary": {"usedTools": ["chat_readonly__rag_search"], "callCount": 1, "degraded": False, "errorCodes": []},
        "confidence": 0.9,
    }
    artifact = AgentArtifact(
        id="result-1", owner="CampusAffairsAgent", kind="specialist_result",
        payload=result_payload, metadata={"planId": "plan-1", "workItemId": "wi-1"},
    )
    route_artifact = AgentArtifact(
        id="route-1", owner="UnderstandingAgent", kind="route_plan",
        payload={"schemaVersion": 2, "planId": "plan-1", "primaryIntent": "CAMPUS", "intents": ["CAMPUS"], "workItems": [], "synthesisOrder": [], "confidence": 0.9, "reasonCodes": []},
        metadata={
            "planSource": "LLM_ACCEPTED", "acceptedSegmentCount": 1,
            "sourceText": secret, "objective": secret, "knownArguments": {"secret": secret},
            "shadowRoutePlan": {"sourceText": secret},
        },
    )
    return AgentRunResult(
        primary_intent=IntentType.CAMPUS,
        intents=(IntentType.CAMPUS,),
        risk_level=RiskLevel.LOW,
        assessment=None,
        response_messages=[AiMessage(role="system", content=secret)],
        steps=[],
        memory_brief="",
        route_plan={"schemaVersion": 2, "planId": "plan-1", "primaryIntent": "CAMPUS"},
        specialist_results=[result_payload],
        evidence_items=result_payload["evidenceItems"],
        tool_diagnostics={
            "workItems": [{
                "workItemId": "wi-1", "agentName": "CampusAffairsAgent", "status": "COMPLETED",
                "reasonCode": "EVIDENCE_COMPLETE", "toolSummary": result_payload["toolSummary"],
            }],
            "totalCallCount": 1, "degraded": False, "errorCodes": [],
        },
        collaboration_artifacts=[route_artifact, artifact],
    )


def test_trace_uses_new_columns_and_redacts_evidence_and_tool_payloads():
    db, user, session = _db()
    secret = "完整证据正文与工具参数不应进入追踪"
    trace = AgentTraceService(db, Settings(_env_file=None, trace_include_prompt_content=False)).save_run(
        user, session, "输入", "输入", "", _run(secret), None,
    )

    assert secret not in trace.evidence_items_json
    assert secret not in trace.tool_diagnostics_json
    assert secret not in trace.agent_steps_json
    assert "shadowRoutePlan" not in trace.agent_steps_json
    assert "LLM_ACCEPTED" in trace.agent_steps_json
    evidence = json.loads(trace.evidence_items_json)[0]
    assert set(evidence) >= {"evidenceId", "contentHash", "contentLength"}
    assert "arguments" not in trace.tool_diagnostics_json


def test_debug_trace_only_exposes_final_response_prompt_content():
    db, user, session = _db()
    secret = "仅调试时记录的最终提示"
    trace = AgentTraceService(db, Settings(_env_file=None, trace_include_prompt_content=True)).save_run(
        user, session, "输入", "输入", "", _run(secret), None,
    )

    assert secret in trace.response_messages_json
    assert secret not in trace.evidence_items_json
    assert secret not in trace.agent_steps_json


def test_minimal_trace_initializes_new_payload_columns():
    db, user, session = _db()
    trace = AgentTraceService(db, Settings(_env_file=None)).create_minimal_trace(
        user=user, session=session, original_input="输入", sanitized_input="输入", intent="ACADEMIC",
    )

    assert trace.evidence_items_json == "[]"
    assert trace.tool_diagnostics_json == "{}"


def test_v7_task_and_evidence_text_fields_are_redacted_from_safe_trace():
    from app.services.trace import _safe

    secret = "不应进入追踪的任务与引文"
    payload = _safe({
        "workItem": {"taskText": secret, "objective": secret, "knownArguments": {"身份": secret}},
        "evidenceNotes": [{"evidenceId": "ev-1", "quote": secret}],
        "missingInfo": [secret],
        "answerText": secret,
    })
    assert secret not in json.dumps(payload, ensure_ascii=False)


def test_skill_selection_trace_uses_only_explicit_whitelist():
    from app.services.trace import _trace_skill_selection

    secret = "用户原文与 Skill 正文都不能进入追踪"
    safe = _trace_skill_selection({
        "selectorVersion": "skill-cascade-v2", "workItemId": "wi-1",
        "eligibleSkillIds": ["demo"], "qualifiedSkillIds": ["demo"], "injectedSkillIds": ["demo"],
        "budgetRejectedSkillIds": [],
        "taskText": secret, "prompt_context": secret, "vector": [1.0, 2.0],
        "candidates": [{"skillId": "demo", "group": "primary_strategy", "rulePoints": 90,
                        "matchedRuleIds": ["d", "g"], "excluded": False, "secret": secret}],
        "groups": [{"group": "primary_strategy", "decision": "RULE_SINGLE_HIGH",
                    "selectedSkillId": "demo", "nearSkillIds": [], "ruleMarginPoints": None,
                    "semanticScores": {}, "semanticMargin": None, "embeddingCalled": False,
                    "embeddingCallCount": 0, "cacheHits": 0, "elapsedMs": 1, "error": secret}],
    })
    rendered = json.dumps(safe, ensure_ascii=False)
    assert secret not in rendered
    assert "demo" in rendered


def test_skill_selection_is_removed_from_other_agent_model_view():
    from app.services.context_builder import _specialist_result_model_view

    payload = {"workItemId": "wi-1", "answerBrief": "可见答案", "skillSelection": {"selectorVersion": "v2"}}
    assert _specialist_result_model_view(payload) == {"workItemId": "wi-1", "answerBrief": "可见答案"}
