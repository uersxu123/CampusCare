from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable
from unittest.mock import patch


class HarnessFailure(AssertionError):
    pass


@dataclass
class CheckResult:
    name: str
    passed: bool
    details: dict = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)


@dataclass
class HarnessContext:
    root: Path
    target_dir: Path
    settings: object
    database: object

    def session(self):
        return self.database.SessionLocal()


class InMemoryShortTermMemoryStore:
    _messages: dict[str, list[object]] = {}

    def __init__(self, settings):
        self.settings = settings

    def load_recent(self, session_public_id: str) -> list[object]:
        from app.schemas.dtos import AiMessage

        return [
            AiMessage(role=message.role, content=message.content)
            for message in self.load_recent_records(session_public_id)
        ]

    def load_recent_records(self, session_public_id: str) -> list[object]:
        limit = self.settings.redis_memory_max_messages
        return list(self._messages.get(session_public_id, []))[-limit:]

    def messages_from_rows(self, rows: list[object]) -> list[object]:
        from app.schemas.dtos import AiMessage

        return [AiMessage(role=row.role.lower(), content=row.content) for row in rows]

    def append(self, session_public_id: str, role: str, content: str, message_id: int | None = None) -> None:
        from app.services.memory import MemoryMessage

        values = self._messages.setdefault(session_public_id, [])
        values.append(
            MemoryMessage(
                id=message_id,
                role=role.lower(),
                content=content,
            )
        )
        del values[:-self.settings.redis_memory_max_messages]

    def replace(self, session_public_id: str, messages: list[object]) -> None:
        from app.services.memory import MemoryMessage

        self._messages[session_public_id] = [
            MemoryMessage(id=None, role=message.role, content=message.content)
            for message in list(messages)[-self.settings.redis_memory_max_messages:]
        ]

    def delete(self, session_public_id: str) -> None:
        self._messages.pop(session_public_id, None)

    @classmethod
    def reset(cls) -> None:
        cls._messages.clear()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run MindBridge engineering harness checks.")
    parser.add_argument(
        "--suite",
        action="append",
        choices=["risk", "routing", "clarification", "skills", "rag", "metrics", "api", "tool-queue", "all"],
        default=None,
        help="Harness suite to run. Can be supplied multiple times.",
    )
    parser.add_argument("--json", action="store_true", help="Print only JSON output.")
    args = parser.parse_args(argv)

    configure_environment()
    context = build_context()
    install_harness_patches()
    reset_database(context)

    suites = resolve_suites(args.suite)
    results: list[CheckResult] = []
    for name, fn in suites:
        reset_database(context)
        InMemoryShortTermMemoryStore.reset()
        results.append(run_check(name, fn, context))

    report = write_report(context, results)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)
    return 0 if all(result.passed for result in results) else 1


def configure_environment() -> None:
    root = Path(__file__).resolve().parents[2]
    target_dir = root / "target" / "harness"
    target_dir.mkdir(parents=True, exist_ok=True)
    db_path = target_dir / "mindbridge-harness.sqlite3"
    for suffix in ["", "-wal", "-shm"]:
        candidate = Path(f"{db_path}{suffix}")
        if candidate.exists():
            candidate.unlink()

    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ["AI_PROVIDER"] = "mock"
    os.environ["AGENT_MODEL_DEFAULT_PROVIDER"] = "mock"
    os.environ["AGENT_MODEL_COORDINATOR_PROVIDER"] = "mock"
    os.environ["AGENT_MODEL_UNDERSTANDING_PROVIDER"] = "mock"
    os.environ["AGENT_MODEL_SAFETY_PROVIDER"] = "mock"
    os.environ["AGENT_MODEL_SPECIALIST_PROVIDER"] = "mock"
    os.environ["AGENT_MODEL_RESPONSE_PROVIDER"] = "mock"
    os.environ["RAG_MODEL_PROVIDER"] = "mock"
    os.environ["CHAT_TOOLS_ENABLED"] = "false"
    os.environ["AGENT_FRAMEWORK"] = "event_driven_multi_agent"
    os.environ["KNOWLEDGE_VECTOR_ENABLED"] = "false"
    os.environ["KNOWLEDGE_VECTOR_REQUIRED"] = "false"
    os.environ["TOOL_QUEUE_ENABLED"] = "false"
    os.environ["ALERT_EMAIL_DELIVERY_MODE"] = "log"
    os.environ["EXCEL_PATH"] = str((target_dir / "mindbridge-risk-ledger.xlsx").as_posix())
    os.environ["RAG_EVAL_OUTPUT"] = str((target_dir / "rag-eval-report.json").as_posix())


def build_context() -> HarnessContext:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.config import get_settings
    import app.core.database as database

    get_settings.cache_clear()
    settings = get_settings()
    if getattr(database, "engine", None) is not None:
        database.engine.dispose()
    database.engine = create_engine(settings.database_url, connect_args={"check_same_thread": False}, pool_pre_ping=True)
    database.SessionLocal = sessionmaker(bind=database.engine, autoflush=False, autocommit=False)
    return HarnessContext(
        root=Path(__file__).resolve().parents[2],
        target_dir=Path(__file__).resolve().parents[2] / "target" / "harness",
        settings=settings,
        database=database,
    )


def install_harness_patches() -> None:
    import app.agents.event_driven_runtime as runtime_module
    import app.agents.harness as harness_module
    import app.services.memory as memory_module

    harness_module.RedisShortTermMemoryStore = InMemoryShortTermMemoryStore
    memory_module.RedisShortTermMemoryStore = InMemoryShortTermMemoryStore
    runtime_module.RedisShortTermMemoryStore = InMemoryShortTermMemoryStore


def reset_database(context: HarnessContext) -> None:
    from app.core.bootstrap import seed_data

    context.database.Base.metadata.drop_all(bind=context.database.engine)
    context.database.Base.metadata.create_all(bind=context.database.engine)
    db = context.session()
    try:
        seed_data(db)
    finally:
        db.close()


def resolve_suites(requested: list[str] | None) -> list[tuple[str, Callable[[HarnessContext], dict]]]:
    all_suites: list[tuple[str, Callable[[HarnessContext], dict]]] = [
        ("Risk Safety Harness", run_risk_safety_harness),
        ("Agent Routing Harness", run_agent_routing_harness),
        ("Clarification Harness", run_clarification_harness),
        ("Standard Skills Harness", run_standard_skills_harness),
        ("RAG Harness", run_rag_harness),
        ("Turn Metrics Harness", run_turn_metrics_harness),
        ("API Harness", run_api_harness),
        ("Tool Queue Harness", run_tool_queue_harness),
    ]
    if not requested or "all" in requested:
        return all_suites
    selected = set(requested)
    aliases = {
        "risk": "Risk Safety Harness",
        "routing": "Agent Routing Harness",
        "clarification": "Clarification Harness",
        "skills": "Standard Skills Harness",
        "rag": "RAG Harness",
        "metrics": "Turn Metrics Harness",
        "api": "API Harness",
        "tool-queue": "Tool Queue Harness",
    }
    names = {aliases[item] for item in selected}
    return [suite for suite in all_suites if suite[0] in names]


def run_check(name: str, fn: Callable[[HarnessContext], dict], context: HarnessContext) -> CheckResult:
    try:
        return CheckResult(name=name, passed=True, details=fn(context))
    except HarnessFailure as exc:
        return CheckResult(name=name, passed=False, failures=[str(exc)])
    except Exception as exc:
        return CheckResult(
            name=name,
            passed=False,
            failures=[f"{type(exc).__name__}: {exc}", traceback.format_exc()],
        )


def run_risk_safety_harness(context: HarnessContext) -> dict:
    from app.core.enums import RiskLevel, ToolJobKind
    from app.models.entities import AgentRunTrace, ChatTurn, PsychologicalReport, ToolJob, UserAccount
    from app.schemas.dtos import ChatRequest
    from app.services.chat import ChatService

    context.settings.tool_queue_enabled = True
    db = context.session()
    observed = []
    try:
        user = db.query(UserAccount).filter(UserAccount.username == "student").one()
        cases = [
            {
                "id": "high-risk-cn",
                "message": "我不想活了，想结束生命。",
                "expects_report": True,
                "expects_risk": RiskLevel.HIGH.value,
                "expects_alert": True,
            },
            {
                "id": "high-risk-en",
                "message": "I want to kill myself and I feel unsafe tonight.",
                "expects_report": True,
                "expects_risk": RiskLevel.HIGH.value,
                "expects_alert": True,
            },
            {
                "id": "consult-low",
                "message": "我最近压力很大，连续几天失眠，想找人聊聊。",
                "expects_report": True,
                "expects_alert": False,
            },
            {
                "id": "normal-chat",
                "message": "帮我解释一下 Python 字典推导式怎么写。",
                "expects_report": False,
                "expects_alert": False,
            },
            {
                "id": "dorm-change-rag",
                "message": "调宿申请怎么办，需要准备什么材料？",
                "expects_report": False,
                "expects_alert": False,
                "expects_clarification": True,
            },
            {
                "id": "dorm-change-partial-evidence",
                "message": "南望山校区宿舍调整线上怎么办，办理时限通常是几个工作日？",
                "expects_report": False,
                "expects_alert": False,
                "expects_retrieval": True,
                "expects_partial_processing_time": True,
            },
        ]

        for case in cases:
            service = ChatService(db, context.settings)
            request_id = str(uuid.uuid4())
            events, assistant = collect_chat_stream(
                service,
                user,
                ChatRequest(requestId=request_id, message=case["message"]),
            )
            report = (
                db.query(PsychologicalReport)
                .filter(PsychologicalReport.content == case["message"])
                .order_by(PsychologicalReport.id.desc())
                .first()
            )
            token_text = assistant.strip()
            expect(any(event["event"] == "meta" for event in events), f"{case['id']} did not emit meta event")
            expect(sum(event["event"] == "done" for event in events) == 1, f"{case['id']} did not emit exactly one done event")
            expect(not any(event["event"] == "error" for event in events), f"{case['id']} emitted an error event")
            expect(bool(token_text), f"{case['id']} did not stream assistant content")
            turn = db.query(ChatTurn).filter(ChatTurn.user_id == user.id, ChatTurn.request_id == request_id).one()
            trace = db.get(AgentRunTrace, turn.trace_id)
            expect(turn.status == "COMPLETED", f"{case['id']} turn status is {turn.status}")
            expect(turn.completion_verified, f"{case['id']} completion was not verified")
            expect(turn.finish_reason in {"STOP", "DIRECT_RESPONSE"}, f"{case['id']} invalid finish reason {turn.finish_reason}")
            expect(trace is not None and trace.finalized_at is not None, f"{case['id']} trace was not finalized")
            generation = json.loads(trace.generation_json)
            expect(generation.get("finishReason") == turn.finish_reason, f"{case['id']} turn/trace finish reason mismatch")
            if case.get("expects_clarification"):
                diagnostics = json.loads(trace.tool_diagnostics_json)
                expect(not diagnostics.get("workItems"), f"{case['id']} called a specialist before clarification")
                expect("校区" in token_text, f"{case['id']} did not ask for the missing site scope")
            elif case.get("expects_retrieval"):
                diagnostics = json.loads(trace.tool_diagnostics_json)
                work_items = diagnostics.get("workItems", [])
                expect(bool(work_items), f"{case['id']} produced no specialist diagnostics")
                expect(
                    all(item.get("agentName") in {"CampusAffairsAgent", "AcademicPlanningAgent"} for item in work_items),
                    f"{case['id']} routed retrieval to an unauthorized specialist",
                )
                evidence = json.loads(trace.evidence_items_json)
                expect(
                    all(item.get("evidenceId") for item in evidence),
                    f"{case['id']} emitted an evidence item without an ID",
                )
            expect((report is not None) == case["expects_report"], f"{case['id']} report expectation failed")
            if report is not None:
                expected_risk = case.get("expects_risk")
                if expected_risk:
                    expect(report.risk_level == expected_risk, f"{case['id']} expected {expected_risk}, got {report.risk_level}")
                jobs = db.query(ToolJob).filter(ToolJob.report_id == report.id).all()
                has_alert = any(job.kind == ToolJobKind.ALERT_SEND.value for job in jobs)
                expect(has_alert == case["expects_alert"], f"{case['id']} alert job expectation failed")
                expect(
                    any(job.kind == ToolJobKind.EXCEL_REPORT.value for job in jobs),
                    f"{case['id']} did not enqueue Excel report job",
                )
                if case["expects_alert"]:
                    expect(
                        any(job.kind == ToolJobKind.CASE_CREATE.value for job in jobs),
                        f"{case['id']} did not enqueue case creation job",
                    )
            forbidden = ["风险等级", "报告ID", "emotionScore", "HIGH_RISK"]
            expect(not any(term in token_text for term in forbidden), f"{case['id']} exposed backend risk metadata")
            observed.append(
                {
                    "id": case["id"],
                    "report": report is not None,
                    "assistantChars": len(token_text),
                    "finishReason": turn.finish_reason,
                }
            )
    finally:
        context.settings.tool_queue_enabled = False
        db.close()
    return {"cases": observed}


def run_agent_routing_harness(context: HarnessContext) -> dict:
    from app.agents.harness import MindBridgeAgentHarness
    from app.core.enums import IntentType, RiskLevel
    from app.models.entities import AgentRunTrace, ChatSession, UserAccount
    from app.schemas.dtos import ChatRequest
    from app.services.chat_turns import ChatTurnService

    context.settings.agent_framework = "event_driven_multi_agent"
    db = context.session()
    observed = []
    try:
        user = db.query(UserAccount).filter(UserAccount.username == "student").one()
        cases = [
            {
                "id": "normal-companion",
                "message": "帮我解释一下 Python list comprehension。",
                "intent": IntentType.CHAT.value,
                "specialist": "GeneralChatAgent",
            },
            {
                "id": "academic-plan",
                "message": "下月三门考试怎么安排复习？",
                "intent": IntentType.ACADEMIC.value,
                "specialist": "AcademicPlanningAgent",
            },
            {
                "id": "campus-procedure",
                "message": "补考要交什么材料？",
                "intent": IntentType.CAMPUS.value,
                "specialist": "CampusAffairsAgent",
            },
            {
                "id": "mental-support",
                "message": "最近焦虑睡不着，想找人聊聊。",
                "intent": IntentType.MENTAL.value,
                "specialist": "PsychologicalSupportAgent",
            },
            {
                "id": "risk-counselor",
                "message": "我不想活了，觉得撑不下去了。",
                "intent": IntentType.RISK.value,
                "risk": RiskLevel.HIGH.value,
                "specialist": None,
            },
        ]
        for case in cases:
            session = ChatSession(public_id=uuid.uuid4().hex, user_id=user.id, title=case["id"])
            db.add(session)
            db.commit()
            db.refresh(session)
            request = ChatRequest(
                requestId=str(uuid.uuid4()),
                message=case["message"],
                sessionId=session.public_id,
            )
            turn, _ = ChatTurnService(db, context.settings).create_or_get(
                user,
                request.requestId,
                request.sessionId,
                request.message,
            )
            current_message = ChatTurnService(db, context.settings).get_user_message(turn)
            result = MindBridgeAgentHarness(db, context.settings).run(
                user, session, request, current_message
            )
            step_agents = [step.agent for step in result.agent_steps]
            trace = db.get(AgentRunTrace, result.trace_id)
            expect(trace is not None, f"{case['id']} did not persist an Agent trace")
            diagnostics = json.loads(trace.tool_diagnostics_json or "{}")
            expect(
                result.primary_intent.value == case["intent"],
                f"{case['id']} expected intent {case['intent']}, got {result.primary_intent.value}",
            )
            if "risk" in case:
                expect(result.risk_level == case["risk"], f"{case['id']} expected risk {case['risk']}, got {result.risk_level}")
            for agent in ("UnderstandingAgent", "SafetyAgent", "ResponseAgent", "CoordinatorAgent"):
                expect(agent in step_agents, f"{case['id']} did not run {agent}")
            specialist = case["specialist"]
            if specialist is None:
                expect(not result.specialist_results, f"{case['id']} should suppress ordinary specialists")
                expect(not diagnostics.get("workItems"), f"{case['id']} should expose no ordinary tool diagnostics")
            else:
                expect(specialist in step_agents, f"{case['id']} did not run {specialist}")
                expect(len(result.specialist_results) == 1, f"{case['id']} did not publish exactly one specialist result")
                expect(
                    result.specialist_results[0].get("agentName") == specialist,
                    f"{case['id']} specialist result owner mismatch",
                )
            observed.append(
                {
                    "id": case["id"],
                    "intent": result.primary_intent.value,
                    "risk": result.risk_level,
                    "steps": step_agents,
                    "specialistResults": len(result.specialist_results),
                    "toolCalls": diagnostics.get("totalCallCount", 0),
                }
            )
    finally:
        db.close()
    return {"cases": observed}


def run_clarification_harness(context: HarnessContext) -> dict:
    from app.agents.harness import MindBridgeAgentHarness
    from app.core.enums import IntentType
    from app.models.entities import ChatSession, PendingClarification, UserAccount
    from app.schemas.dtos import ChatRequest
    from app.services.chat_turns import ChatTurnService

    db = context.session()
    harness = MindBridgeAgentHarness(db, context.settings)

    def create_session(user, title):
        session = ChatSession(public_id=uuid.uuid4().hex, user_id=user.id, title=title)
        db.add(session)
        db.commit()
        db.refresh(session)
        return session

    def run_turn(user, session, message):
        request = ChatRequest(requestId=str(uuid.uuid4()), message=message, sessionId=session.public_id)
        turn, _ = ChatTurnService(db, context.settings).create_or_get(
            user,
            request.requestId,
            request.sessionId,
            request.message,
        )
        current = ChatTurnService(db, context.settings).get_user_message(turn)
        outcome = harness.run(user, session, request, current)
        if outcome.direct_response:
            harness.save_assistant_message(user, session, outcome.direct_response)
        return outcome

    observed = []
    try:
        user = db.query(UserAccount).filter(UserAccount.username == "student").one()

        award_session = create_session(user, "clarification-award")
        first = run_turn(user, award_session, "国家奖学金怎么申请？")
        pending = (
            db.query(PendingClarification)
            .filter(PendingClarification.user_id == user.id, PendingClarification.session_id == award_session.id)
            .order_by(PendingClarification.id.desc())
            .first()
        )
        expect(pending is not None and pending.status == "WAITING_USER", "student type clarification was not persisted")
        expect(
            [item["name"] for item in json.loads(pending.missing_arguments_json)] == ["studentType"],
            "clarification selected a field outside the intent registry",
        )
        expect(first.direct_response == "请确认学生类型：本科生还是研究生？", "handler did not generate the approved question")
        original_resume = json.loads(pending.resume_context_json)
        second = run_turn(user, award_session, "本科生。")
        db.refresh(pending)
        expect(pending.status == "RESOLVED", "student type clarification did not resolve")
        expect(second.route_plan.get("planId") == original_resume.get("originPlanId"), "clarification generated a new plan")
        expect(
            second.route_plan["workItems"][0]["workItemId"] == original_resume.get("targetWorkItemId"),
            "clarification generated a new work item",
        )
        observed.append({"id": "student-type", "status": pending.status, "rounds": pending.round_count})

        fact_session = create_session(user, "clarification-official-fact")
        fact = run_turn(user, fact_session, "补考要交什么材料？")
        waiting = (
            db.query(PendingClarification)
            .filter(PendingClarification.user_id == user.id, PendingClarification.session_id == fact_session.id)
            .first()
        )
        expect(waiting is None, "official knowledge fact was incorrectly converted into user clarification")
        expect(fact.primary_intent == IntentType.CAMPUS, "official fact did not continue to the Campus specialist")
        observed.append({"id": "official-fact", "intent": fact.primary_intent.value})

        risk_session = create_session(user, "clarification-risk")
        run_turn(user, risk_session, "国家奖学金怎么申请？")
        risk_row = (
            db.query(PendingClarification)
            .filter(PendingClarification.user_id == user.id, PendingClarification.session_id == risk_session.id)
            .order_by(PendingClarification.id.desc())
            .first()
        )
        risk = run_turn(user, risk_session, "我不想活了，想伤害自己。")
        db.refresh(risk_row)
        expect(risk_row.status == "INTERRUPTED", "high risk input did not interrupt old clarification")
        expect(risk.primary_intent == IntentType.RISK, "high risk input was intercepted by ordinary clarification")
        observed.append({"id": "risk-preemption", "status": risk_row.status, "intent": risk.primary_intent.value})
    finally:
        db.close()
    return {"cases": observed}


def run_standard_skills_harness(context: HarnessContext) -> dict:
    from app.core.enums import EmotionLabel, IntentType, RiskLevel
    from app.models.entities import PsychologicalReport, UserAccount
    from app.services.skills import MindBridgeSkillLibrary, SkillManager

    expected = {
        "supportive_response_baseline",
        "high_risk_safety_plan",
        "anxiety_grounding_support",
        "sleep_routine_support",
        "academic_stress_planning",
        "academic_warning_recovery",
        "thesis_research_progress",
        "further_study_career_decision",
        "campus_procedure_navigation",
        "financial_aid_awards_guidance",
        "dormitory_life_guidance",
        "referral_resource_guidance",
        "counselor_handoff_summary",
    }
    skills = MindBridgeSkillLibrary.list_skills()
    names = {skill.name for skill in skills}
    missing = sorted(expected - names)
    expect(not missing, f"missing standard skills: {missing}")

    statuses = MindBridgeSkillLibrary.status_items()
    failed = [item for item in statuses if item["status"] != "READY"]
    expect(not failed, f"standard skill load failures: {failed}")
    expect(all(item["path"].endswith("/SKILL.md") for item in statuses), "skill status did not expose SKILL.md paths")

    selected_names = [
        match.skill.name
        for match in SkillManager().match(
            "psychological_support",
            IntentType.MENTAL.value,
            "我现在很焦虑，想先缓下来。",
            RiskLevel.LOW,
        )
    ]
    expect(len(selected_names) <= 2, f"mental selected too many skills: {selected_names}")
    for name in ["supportive_response_baseline", "anxiety_grounding_support"]:
        expect(name in selected_names, f"mental response did not select {name}")
    expect("referral_resource_guidance" not in selected_names, "referral was selected without referral intent")

    context_text = "\n\n".join(
        match.prompt_context
        for match in SkillManager().match(
            "psychological_support",
            IntentType.MENTAL.value,
            "我现在很焦虑，想先缓下来。",
            RiskLevel.LOW,
        )
    )
    response_names = MindBridgeSkillLibrary.response_skill_names(
        IntentType.MENTAL,
        RiskLevel.LOW,
        "我最近焦虑、失眠，考试压力也很大。",
    )
    expect("应用 skill: anxiety_grounding_support" in context_text, "response context did not include standard skill body")
    expect(response_names == ["supportive_response_baseline"], "ResponseAgent selected a specialist-only skill")

    high_risk_names = MindBridgeSkillLibrary.response_skill_names(
        IntentType.RISK,
        RiskLevel.HIGH,
        "我不想活了。",
    )
    expect(high_risk_names == ["supportive_response_baseline"], "fixed high-risk skill entered dynamic selection")
    expect(
        "应用 skill: high_risk_safety_plan" in MindBridgeSkillLibrary.high_risk_safety_plan_prompt(),
        "fixed high-risk skill named lookup changed",
    )

    report = PsychologicalReport(
        id=7,
        user_id=42,
        session_id=1,
        content="我不想活了，觉得撑不下去。",
        intent=IntentType.RISK.value,
        emotion=EmotionLabel.HIGH_RISK.value,
        emotion_score=4.0,
        risk_level=RiskLevel.HIGH.value,
        confidence=0.95,
        summary="检测到明确高风险表达",
    )
    user = UserAccount(
        id=42,
        username="student",
        display_name="测试学生",
        password_hash="unused",
        roles_csv="ROLE_USER",
    )
    handoff = MindBridgeSkillLibrary.counselor_handoff_summary(report, user)
    for term in ["应用 skill: counselor_handoff_summary", "报告ID：7", "测试学生 (student)", "立即跟进"]:
        expect(term in handoff, f"handoff summary missing {term}")

    return {
        "skills": sorted(names),
        "selectedMentalSkills": selected_names,
        "selectedHighRiskSkills": high_risk_names,
        "handoffChars": len(handoff),
    }


def run_rag_harness(context: HarnessContext) -> dict:
    from app.rag_eval.runner import evaluate_mode

    db = context.session()
    try:
        dataset_path = context.root / context.settings.rag_eval_dataset
        cases = json.loads(dataset_path.read_text(encoding="utf-8"))
        mode_report = evaluate_mode(db, context.settings, cases, "bm25")
        expect(len(cases) >= 80, f"RAG dataset is too small: {len(cases)}")
        expect(mode_report["passed"], f"RAG BM25 gates failed: {mode_report['gates']}")
        report = {"createdAt": datetime.now(UTC).isoformat(), "mode": mode_report}
        output = context.target_dir / "rag-eval-report.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return mode_report["metrics"] | {"report": str(output)}
    finally:
        db.close()


def run_turn_metrics_harness(context: HarnessContext) -> dict:
    from app.models.entities import AgentRunTrace, ChatTurn, UserAccount
    from app.schemas.dtos import ChatRequest
    from app.services.chat import ChatService
    from app.services.model_completion import (
        ModelCompletionMetadata,
        ModelFinishReason,
        ModelProtocolError,
        ModelStreamEvent,
    )

    def terminal(reason: ModelFinishReason) -> ModelStreamEvent:
        provider_reason = {
            ModelFinishReason.STOP: "stop",
            ModelFinishReason.LENGTH: "length",
        }.get(reason, "error")
        return ModelStreamEvent(
            kind="terminal",
            metadata=ModelCompletionMetadata(
                provider="mock",
                model="mock",
                finish_reason=reason,
                semantic_finish_seen=True,
                transport_terminal_seen=True,
                terminal_signal="metrics_harness",
                provider_finish_reason=provider_reason,
                configured_output_limit=1536,
            ),
        )

    async def stop_stream(_client, _messages):
        yield ModelStreamEvent(kind="delta", text="确定性回答内容")
        yield terminal(ModelFinishReason.STOP)

    continuation_calls = 0

    async def continuation_stream(_client, _messages):
        nonlocal continuation_calls
        continuation_calls += 1
        if continuation_calls == 1:
            yield ModelStreamEvent(kind="delta", text="第一段尚未结束")
            yield terminal(ModelFinishReason.LENGTH)
        else:
            yield ModelStreamEvent(kind="delta", text="，第二段完成。")
            yield terminal(ModelFinishReason.STOP)

    async def failed_stream(_client, _messages):
        raise ModelProtocolError(
            "metrics harness provider failure",
            ModelCompletionMetadata(
                provider="mock",
                model="mock",
                finish_reason=ModelFinishReason.ERROR,
                semantic_finish_seen=False,
                transport_terminal_seen=False,
                terminal_signal="provider_error",
                provider_finish_reason="",
                configured_output_limit=1536,
            ),
        )
        yield  # pragma: no cover

    duplicate_calls = 0

    async def duplicate_stream(_client, _messages):
        nonlocal duplicate_calls
        duplicate_calls += 1
        yield ModelStreamEvent(kind="delta", text="幂等回答内容")
        yield terminal(ModelFinishReason.STOP)

    db = context.session()
    observed = []
    privacy_checked = True
    try:
        user = db.query(UserAccount).filter(UserAccount.username == "student").one()

        def execute(case_id: str, message: str, stream_impl=None, request_id: str | None = None):
            request_id = request_id or str(uuid.uuid4())
            service = ChatService(db, context.settings)
            request = ChatRequest(requestId=request_id, message=message)
            if stream_impl is None:
                events, assistant = collect_chat_stream(service, user, request)
            else:
                with patch("app.services.ai.AiClient._mock_stream_events", stream_impl):
                    events, assistant = collect_chat_stream(service, user, request)
            db.expire_all()
            turn = db.query(ChatTurn).filter(ChatTurn.request_id == request_id, ChatTurn.user_id == user.id).one()
            trace = db.get(AgentRunTrace, turn.trace_id)
            generation = json.loads(turn.generation_metadata_json or "{}")
            trace_generation = json.loads(trace.generation_json or "{}") if trace is not None else {}
            metrics = generation.get("turnMetrics") or {}
            event_names = [event["event"] for event in events]

            expect(event_names and event_names[0] == "meta", f"{case_id} SSE did not start with meta")
            expect(event_names[-1] == "done", f"{case_id} SSE did not end with done")
            expect(event_names.count("done") == 1, f"{case_id} emitted duplicate done events")
            expect(generation.get("schemaVersion") == 2, f"{case_id} generation schema is not v2")
            expect(metrics.get("schemaVersion") == 1, f"{case_id} turn metrics schema is not v1")
            expect(metrics == trace_generation.get("turnMetrics"), f"{case_id} ChatTurn/Trace metrics mismatch")
            usage = metrics.get("tokenUsage") or {}
            expect(
                usage.get("totalTokens") == (usage.get("promptTokens") or 0) + (usage.get("outputTokens") or 0),
                f"{case_id} token total is inconsistent",
            )
            expect(
                usage.get("providerCallCount") == len(metrics.get("calls") or []),
                f"{case_id} provider call count is inconsistent",
            )
            for field in ("firstContentReadyMs", "serverTurnDurationMs"):
                value = metrics.get(field)
                if value is not None:
                    expect(value >= 0, f"{case_id} has negative {field}")
            if metrics.get("serverE2eTtftMs") is not None:
                expect(metrics["finalModelTtftMs"] >= 0, f"{case_id} has negative model TTFT")
                expect(
                    metrics["serverE2eTtftMs"] >= metrics["finalModelTtftMs"],
                    f"{case_id} server TTFT is below model TTFT",
                )
                expect(
                    metrics["serverTurnDurationMs"] >= metrics["serverE2eTtftMs"],
                    f"{case_id} turn duration is below server TTFT",
                )
            forbidden_keys = {"message", "messages", "content", "userInput", "promptText", "knowledgeText"}
            expect(not (_nested_keys(metrics) & forbidden_keys), f"{case_id} metrics contain private text keys")
            serialized_metrics = json.dumps(metrics, ensure_ascii=False)
            expect(message not in serialized_metrics, f"{case_id} metrics contain the request body")
            expect(not assistant or assistant not in serialized_metrics, f"{case_id} metrics contain the answer body")
            return turn, metrics, events, request_id

        normal_turn, normal, _events, _request_id = execute(
            "normal_generation",
            "请解释 Python 字典推导式。",
            stop_stream,
        )
        expect(normal_turn.status == "COMPLETED", "normal_generation did not complete")
        expect(normal.get("finalModelTtftMs") is not None, "normal_generation model TTFT is missing")
        expect(normal.get("serverE2eTtftMs") is not None, "normal_generation server TTFT is missing")
        expect(
            sum(call.get("purpose") == "response.generate" for call in normal.get("calls", [])) == 1,
            "normal_generation response call count is not one",
        )
        observed.append(_metrics_case("normal_generation", normal))

        continuation_turn, continuation, _events, _request_id = execute(
            "continuation",
            "请完整说明列表推导式。",
            continuation_stream,
        )
        expect(continuation_turn.status == "COMPLETED", "continuation did not complete")
        response_purposes = [
            call.get("purpose") for call in continuation.get("calls", []) if str(call.get("purpose", "")).startswith("response.")
        ]
        expect(
            response_purposes == ["response.generate", "response.continuation"],
            f"continuation response calls are invalid: {response_purposes}",
        )
        expect(continuation_calls == 2, f"continuation provider executed {continuation_calls} times")
        observed.append(_metrics_case("continuation", continuation))

        direct_turn, direct, _events, _request_id = execute(
            "direct_response",
            "帮我制定学习计划",
        )
        expect(direct_turn.status == "COMPLETED", "direct_response did not complete")
        expect(direct.get("finalModelTtftMs") is None, "direct_response fabricated model TTFT")
        expect(direct.get("serverE2eTtftMs") is None, "direct_response fabricated server TTFT")
        expect(direct.get("firstContentReadyMs") is not None, "direct_response ready time is missing")
        observed.append(_metrics_case("direct_response", direct))

        failed_turn, failed, failed_events, _request_id = execute(
            "provider_failure",
            "请回答一个普通问题。",
            failed_stream,
        )
        expect(failed_turn.status == "FAILED", "provider_failure did not fail")
        expect(any(event["event"] == "error" for event in failed_events), "provider_failure emitted no SSE error")
        failed_response = next(
            (call for call in failed.get("calls", []) if call.get("purpose") == "response.generate"),
            None,
        )
        expect(failed_response is not None, "provider_failure lost the response call")
        expect(failed_response.get("status") == "FAILED", "provider_failure call status is not FAILED")
        expect(failed.get("finalModelTtftMs") is None, "provider_failure fabricated TTFT before a delta")
        observed.append(_metrics_case("provider_failure", failed))

        duplicate_request_id = str(uuid.uuid4())
        duplicate_turn, duplicate, _events, _request_id = execute(
            "duplicate_request",
            "请解释集合。",
            duplicate_stream,
            duplicate_request_id,
        )
        first_count = duplicate["tokenUsage"]["providerCallCount"]
        first_started_at = duplicate["startedAt"]
        _replay_turn, replay, _events, _request_id = execute(
            "duplicate_request_replay",
            "请解释集合。",
            duplicate_stream,
            duplicate_request_id,
        )
        expect(duplicate_turn.status == "COMPLETED", "duplicate_request did not complete")
        expect(duplicate_calls == 1, f"duplicate_request generated {duplicate_calls} times")
        expect(replay["tokenUsage"]["providerCallCount"] == first_count, "duplicate_request increased call count")
        expect(replay["startedAt"] == first_started_at, "duplicate_request reset T0")
        observed.append(_metrics_case("duplicate_request", replay))
    finally:
        db.close()
    return {
        "caseCount": len(observed),
        "cases": observed,
        "invariantsChecked": 12,
        "privacyChecked": privacy_checked,
    }


def run_api_harness(context: HarnessContext) -> dict:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models.entities import ChatMessage, ChatSession, ConversationSummary

    context.settings.tool_queue_enabled = False
    app = create_app()
    student_auth = basic_auth("student", "student123")
    admin_auth = basic_auth("admin", "admin123")
    observed = {}
    with TestClient(app) as client:
        health = client.get("/actuator/health")
        expect(health.status_code == 200 and health.json()["status"] == "UP", "health endpoint failed")
        observed["health"] = health.json()

        profile = client.get("/api/profile", headers=student_auth)
        expect(profile.status_code == 200, f"student profile failed: {profile.status_code}")
        expect(profile.json()["username"] == "student", "student profile returned wrong user")

        agent_status = client.get("/api/agent/status", headers=student_auth)
        expect(agent_status.status_code == 200, f"agent status failed: {agent_status.status_code}")
        status_skills = agent_status.json()["skills"]
        expect(len(status_skills) >= 7, f"agent status exposed too few standard skills: {len(status_skills)}")
        expect(all(skill["path"].endswith("/SKILL.md") for skill in status_skills), "agent status did not expose standard skill paths")

        admin_chat = client.post(
            "/api/chat/stream",
            headers=admin_auth,
            json={"requestId": str(uuid.uuid4()), "message": "hello"},
        )
        expect(admin_chat.status_code == 403, f"admin chat should be forbidden, got {admin_chat.status_code}")

        chat = client.post(
            "/api/chat/stream",
            headers=student_auth,
            json={"requestId": str(uuid.uuid4()), "message": "帮我解释一下 Python 函数。"},
        )
        expect(chat.status_code == 200, f"student chat stream failed: {chat.status_code}")
        expect("event: meta" in chat.text and "event: done" in chat.text, "chat stream missing meta/done events")
        observed["chatStreamChars"] = len(chat.text)
        first_events = parse_sse(chat.text)
        first_session_id = next(
            event["data"]["sessionId"] for event in first_events if event["event"] == "meta"
        )

        second_chat = client.post(
            "/api/chat/stream",
            headers=student_auth,
            json={"requestId": str(uuid.uuid4()), "message": "再帮我说明一下列表推导式。"},
        )
        expect(second_chat.status_code == 200, f"second student chat failed: {second_chat.status_code}")
        second_session_id = next(
            event["data"]["sessionId"]
            for event in parse_sse(second_chat.text)
            if event["event"] == "meta"
        )

        conversations = client.get("/api/conversations", headers=student_auth)
        expect(conversations.status_code == 200, f"conversation list failed: {conversations.status_code}")
        visible_ids = [item["sessionId"] for item in conversations.json()["items"]]
        expect(first_session_id in visible_ids and second_session_id in visible_ids, "new conversations were not listed")

        detail = client.get(f"/api/conversations/{first_session_id}", headers=student_auth)
        expect(detail.status_code == 200, f"conversation detail failed: {detail.status_code}")
        expect(len(detail.json()["messages"]) >= 2, "conversation detail omitted messages")

        db = context.session()
        try:
            session = db.query(ChatSession).filter(ChatSession.public_id == first_session_id).one()
            message_count = db.query(ChatMessage).filter(ChatMessage.session_id == session.id).count()
            summary_count = db.query(ConversationSummary).filter(ConversationSummary.session_id == session.id).count()
        finally:
            db.close()

        archived = client.post(f"/api/conversations/{first_session_id}/archive", headers=student_auth)
        expect(archived.status_code == 200 and archived.json()["archived"], "conversation archive failed")
        after_archive = client.get("/api/conversations", headers=student_auth).json()["items"]
        expect(first_session_id not in [item["sessionId"] for item in after_archive], "archived conversation remained visible")
        expect(second_session_id in [item["sessionId"] for item in after_archive], "archive removed another conversation")
        expect(
            client.get(f"/api/conversations/{first_session_id}", headers=student_auth).status_code == 404,
            "student could still read archived conversation",
        )
        expect(
            client.post(
                "/api/chat/stream",
                headers=student_auth,
                json={"requestId": str(uuid.uuid4()), "sessionId": first_session_id, "message": "继续"},
            ).status_code
            == 409,
            "student could continue archived conversation",
        )
        admin_detail = client.get(f"/api/admin/conversations/{first_session_id}", headers=admin_auth)
        expect(admin_detail.status_code == 200 and admin_detail.json()["archived"], "admin could not review archived conversation")

        db = context.session()
        try:
            session = db.query(ChatSession).filter(ChatSession.public_id == first_session_id).one()
            expect(
                db.query(ChatMessage).filter(ChatMessage.session_id == session.id).count() == message_count,
                "archive deleted chat messages",
            )
            expect(
                db.query(ConversationSummary).filter(ConversationSummary.session_id == session.id).count() == summary_count,
                "archive deleted conversation summary",
            )
        finally:
            db.close()
        observed["conversationArchive"] = {
            "archivedSessionId": first_session_id,
            "activeSessionId": second_session_id,
            "preservedMessages": message_count,
            "preservedSummaries": summary_count,
        }

        student_reports = client.get("/api/admin/reports", headers=student_auth)
        expect(student_reports.status_code == 403, f"student should not read admin reports: {student_reports.status_code}")

        admin_reports = client.get("/api/admin/reports", headers=admin_auth)
        expect(admin_reports.status_code == 200, f"admin reports failed: {admin_reports.status_code}")

        ingest = client.post(
            "/api/admin/knowledge",
            headers=admin_auth,
            json={"source": "harness-note", "content": "考试焦虑时可以先做呼吸练习，并联系辅导员获得支持。"},
        )
        expect(ingest.status_code == 200, f"knowledge ingest failed: {ingest.status_code} {ingest.text}")
        expect(ingest.json()["chunks"] >= 1, "knowledge ingest did not create chunks")

        status = client.get("/api/admin/knowledge/status", headers=admin_auth)
        expect(status.status_code == 200, f"knowledge status failed: {status.status_code}")
        expect(status.json()["databaseChunks"] >= 1, "knowledge status returned no chunks")
        observed["knowledgeStatus"] = {
            "databaseChunks": status.json()["databaseChunks"],
            "vectorAvailable": status.json()["vectorAvailable"],
        }
    return observed


def run_tool_queue_harness(context: HarnessContext) -> dict:
    from app.core.enums import EmotionLabel, IntentType, RiskCaseStatus, RiskLevel, ToolJobKind, ToolJobStatus, ToolStatus
    from app.models.entities import DeadLetterRecord, PsychologicalReport, ToolJob, ChatSession, UserAccount
    from app.services.tool_queue import RateLimiter, ToolQueueService, ToolQueueWorker
    from app.services.tools import ToolOrchestrationService

    context.settings.tool_queue_enabled = True
    db = context.session()
    worker = ToolQueueWorker(context.settings)
    try:
        user = db.query(UserAccount).filter(UserAccount.username == "student").one()
        session = ChatSession(public_id=uuid.uuid4().hex, user_id=user.id, title="tool-queue-harness")
        db.add(session)
        db.commit()
        db.refresh(session)
        report = PsychologicalReport(
            user_id=user.id,
            session_id=session.id,
            content="我不想活了，想结束生命。",
            intent=IntentType.RISK.value,
            emotion=EmotionLabel.HIGH_RISK.value,
            emotion_score=4.0,
            risk_level=RiskLevel.HIGH.value,
            confidence=0.95,
            summary="harness high risk case",
        )
        db.add(report)
        db.commit()
        db.refresh(report)

        jobs = ToolQueueService(db, context.settings).enqueue_report(report.id, report.risk_level)
        expect(len(jobs) == 3, f"expected 3 jobs for high risk report, got {len(jobs)}")
        excel_job = next(job for job in jobs if job.kind == ToolJobKind.EXCEL_REPORT.value)
        case_job = next(job for job in jobs if job.kind == ToolJobKind.CASE_CREATE.value)
        alert_job = next(job for job in jobs if job.kind == ToolJobKind.ALERT_SEND.value)
        expect(alert_job.depends_on_job_id == case_job.id, "alert job does not depend on case creation job")
        expect(not worker._dependency_ready(db, alert_job), "alert dependency should not be ready before case creation success")

        tools = ToolOrchestrationService(db, context.settings)
        excel_record = tools.write_excel(report)
        expect(excel_record.status == ToolStatus.SUCCESS.value, f"Excel write failed: {excel_record.message}")
        second_excel_record = tools.write_excel(report)
        expect(second_excel_record.id == excel_record.id, "Excel write is not idempotent")

        case_record = tools.create_case(report)
        second_case_record = tools.create_case(report)
        expect(second_case_record.id == case_record.id, "case creation is not idempotent")

        case_job.status = ToolJobStatus.SUCCESS.value
        db.add(case_job)
        db.commit()
        expect(worker._dependency_ready(db, alert_job), "alert dependency was not ready after case creation success")

        alert_record = tools.send_case_alert(case_record)
        expect(alert_record.status == ToolStatus.SUCCESS.value, f"alert notify failed: {alert_record.message}")
        db.refresh(case_record)
        expect(case_record.status == RiskCaseStatus.ALERT_SENT.value, "case did not move to ALERT_SENT after alert")

        limiter = RateLimiter(1)
        first_allowed, _ = limiter.allow()
        second_allowed, retry_after = limiter.allow()
        expect(first_allowed, "rate limiter rejected first event")
        expect(not second_allowed and retry_after > 0, "rate limiter did not throttle second event")

        dead_job = ToolJob(
            report_id=report.id,
            kind=ToolJobKind.EXCEL_REPORT.value,
            status=ToolJobStatus.RUNNING.value,
            attempts=3,
            max_attempts=3,
        )
        db.add(dead_job)
        db.commit()
        db.refresh(dead_job)
        worker._fail_or_dead_letter(db, dead_job.id, RuntimeError("harness failure"))
        db.refresh(dead_job)
        dead_letter = db.query(DeadLetterRecord).filter(DeadLetterRecord.job_id == dead_job.id).first()
        expect(dead_job.status == ToolJobStatus.DEAD.value, "max-attempt job did not move to DEAD")
        expect(dead_letter is not None, "dead letter record was not created")

        return {
            "reportId": report.id,
            "excelJobId": excel_job.id,
            "caseJobId": case_job.id,
            "alertJobId": alert_job.id,
            "caseId": case_record.id,
            "excelPath": excel_record.file_path,
            "deadLetterId": dead_letter.id,
        }
    finally:
        worker.stop()
        context.settings.tool_queue_enabled = False
        db.close()


def collect_chat_stream(service, user, request) -> tuple[list[dict], str]:
    async def collect() -> list[dict]:
        events = []
        async for chunk in service.start_chat(user, request):
            events.extend(parse_sse(chunk))
        return events

    events = asyncio.run(collect())
    snapshots = [event["data"].get("content", "") for event in events if event["event"] == "snapshot"]
    assistant = snapshots[-1] if snapshots else ""
    return events, assistant


def _metrics_case(case_id: str, metrics: dict) -> dict:
    usage = metrics.get("tokenUsage") or {}
    return {
        "id": case_id,
        "status": metrics.get("status"),
        "providerCallCount": usage.get("providerCallCount"),
        "tokenAccuracy": usage.get("accuracy"),
        "promptTokens": usage.get("promptTokens"),
        "outputTokens": usage.get("outputTokens"),
        "totalTokens": usage.get("totalTokens"),
        "finalModelTtftMs": metrics.get("finalModelTtftMs"),
        "serverE2eTtftMs": metrics.get("serverE2eTtftMs"),
        "firstContentReadyMs": metrics.get("firstContentReadyMs"),
        "serverTurnDurationMs": metrics.get("serverTurnDurationMs"),
    }


def _nested_keys(value) -> set[str]:
    if isinstance(value, dict):
        keys = set(value)
        for item in value.values():
            keys.update(_nested_keys(item))
        return keys
    if isinstance(value, (list, tuple)):
        keys = set()
        for item in value:
            keys.update(_nested_keys(item))
        return keys
    return set()


def parse_sse(chunk: str) -> list[dict]:
    events = []
    for block in chunk.strip().split("\n\n"):
        if not block:
            continue
        event_name = ""
        data = {}
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line.removeprefix("event: ").strip()
            elif line.startswith("data: "):
                data = json.loads(line.removeprefix("data: ").strip())
        events.append({"event": event_name, "data": data})
    return events


def basic_auth(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise HarnessFailure(message)


def write_report(context: HarnessContext, results: list[CheckResult]) -> dict:
    report = {
        "createdAt": datetime.now(UTC).isoformat(),
        "environment": {
            "databaseUrl": context.settings.database_url,
            "aiProvider": context.settings.ai_provider,
            "agentFramework": context.settings.agent_framework,
            "knowledgeVectorEnabled": context.settings.knowledge_vector_enabled,
        },
        "passed": all(result.passed for result in results),
        "results": [
            {
                "name": result.name,
                "passed": result.passed,
                "details": result.details,
                "failures": result.failures,
            }
            for result in results
        ],
    }
    output = context.target_dir / "harness-report.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    report["reportPath"] = str(output)
    return report


def print_report(report: dict) -> None:
    print("MindBridge Engineering Harness")
    print(f"Report: {report['reportPath']}")
    print("")
    for result in report["results"]:
        status = "PASS" if result["passed"] else "FAIL"
        print(f"[{status}] {result['name']}")
        if result["passed"] and result["details"]:
            compact = json.dumps(result["details"], ensure_ascii=False, default=str)
            print(f"       {compact[:900]}")
        for failure in result["failures"]:
            print(f"       {failure}")
    print("")
    print("Overall: PASS" if report["passed"] else "Overall: FAIL")


if __name__ == "__main__":
    sys.exit(main())
