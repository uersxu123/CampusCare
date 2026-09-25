import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.agents.routing import classify_route
from app.core.config import Settings
from app.core.database import Base
from app.core.enums import IntentType
from app.models.entities import ChatSession, UserAccount
from app.services.clarification_models import ClarificationRequest, ClarificationResumeContext
from app.services.clarifications import ClarificationService
from app.services.routing_v5 import PlanningResultV6
from app.services.understanding import UnderstandingInvocationResult


def _understand_academic(text: str, _context: dict) -> UnderstandingInvocationResult:
    decision = PlanningResultV6.model_validate({
        "schemaVersion": 6,
        "workItems": [{
            "intent": "ACADEMIC", "objective": "制定学习计划", "taskText": text,
            "sourceRefs": ["current:0"], "contextRefs": [], "dependsOn": [],
        }],
    })
    return UnderstandingInvocationResult(decision, 1, 1)


def test_study_plan_clarification_resolves_course_and_deadline_across_turns():
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(engine)
    try:
        user = UserAccount(username="study-flow", display_name="学习用户", password_hash="x")
        db.add(user)
        db.flush()
        session = ChatSession(public_id="study-flow", title="学习计划", user_id=user.id)
        db.add(session)
        db.flush()

        settings = Settings(_env_file=None, ai_provider="mock", route_fast_enabled=False)
        plan = classify_route(
            "帮我根据这学期的课程和截止时间制定一份学习计划",
            semantic_classifier=_understand_academic,
            settings=settings,
        ).route_plan
        assert plan is not None and not plan.degraded
        item = plan.work_items[0]
        resume = ClarificationResumeContext(
            route_plan=plan,
            origin_plan_id=plan.plan_id,
            target_work_item_id=item.work_item_id,
            expected_fields=(item.missing_arguments[0].name,),
            round_count=0,
        )
        request = ClarificationRequest(
            origin_plan_id=plan.plan_id,
            target_work_item_id=item.work_item_id,
            intent=IntentType.ACADEMIC,
            objective=item.objective,
            original_message="帮我根据这学期的课程和截止时间制定一份学习计划",
            known_arguments=item.known_arguments,
            missing_arguments=(item.missing_arguments[0],),
            resume_context=resume,
        )
        service = ClarificationService(db, settings)
        service.create(user, session, request)

        first = service.resume(user, session, "数学吧")
        assert first.handled is True
        assert first.status.value == "WAITING_USER"
        assert "目标日期" in first.question
        pending = service.active(user.id, session.id)
        assert json.loads(pending.known_arguments_json) == {"course": "数学"}
        assert json.loads(pending.missing_arguments_json)[0]["name"] == "deadline"

        second = service.resume(user, session, "考试截止时间是8.8日下午17：00")
        assert second.handled is False
        assert second.status.value == "RESOLVED"
        work_item = second.resume_context["routePlan"]["workItems"][0]
        assert work_item["knownArguments"] == {"course": "数学", "deadline": "8月8日 17:00"}
        assert work_item["missingArguments"] == []
    finally:
        db.close()
        engine.dispose()
