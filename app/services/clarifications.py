from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.entities import ChatSession, PendingClarification, UserAccount
from app.services.clarification_handlers import get_clarification_handler, is_new_topic
from app.services.clarification_models import ClarificationRequest, ClarificationResolution, ClarificationResumeContext, ClarificationStatus


logger = logging.getLogger(__name__)
CANCEL_TERMS = ("取消", "不做了", "算了", "换个问题", "换一个问题")


class ClarificationService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings

    def active(self, user_id: int, session_id: int) -> PendingClarification | None:
        return self.db.query(PendingClarification).filter(
            PendingClarification.user_id == user_id,
            PendingClarification.session_id == session_id,
            PendingClarification.status == ClarificationStatus.WAITING_USER.value,
        ).order_by(PendingClarification.id.desc()).first()

    def create(self, user: UserAccount, session: ChatSession, payload: dict | ClarificationRequest) -> PendingClarification:
        request = payload if isinstance(payload, ClarificationRequest) else ClarificationRequest.from_payload(payload)
        request = ClarificationRequest.from_payload(request.as_payload())
        handler = get_clarification_handler(request.intent, self.settings)
        if handler is None:
            raise ValueError("当前 Intent 不支持澄清")
        known = handler.sanitize_known(request.known_arguments)
        missing = handler.sanitize_missing(request.missing_arguments)
        if known != request.known_arguments or tuple(missing) != request.missing_arguments:
            raise ValueError("澄清参数未通过 Intent 白名单")
        question = handler.build_question(missing)
        if not question:
            raise ValueError("无法生成受控澄清问题")
        resume = request.resume_context
        expected = list(resume.expected_fields)
        existing = self.active(user.id, session.id)
        if existing:
            current = _load(existing.resume_context_json, {})
            if (current.get("originPlanId"), current.get("targetWorkItemId"), current.get("expectedFields")) == (
                request.origin_plan_id, request.target_work_item_id, expected,
            ):
                return existing
            self._cas_update(existing, status=ClarificationStatus.INTERRUPTED.value, finish_reason="REPLACED")
        now = datetime.now(UTC).replace(tzinfo=None)
        row = PendingClarification(
            public_id=uuid.uuid4().hex,
            user_id=user.id,
            session_id=session.id,
            status=ClarificationStatus.WAITING_USER.value,
            intent=request.intent.value,
            original_message=request.original_message,
            known_arguments_json=_dump(known),
            missing_arguments_json=_dump([missing[0].as_payload()]),
            resume_context_json=_dump(resume.as_payload()),
            approved_question=question,
            round_count=1,
            max_rounds=max(1, int(self.settings.clarification_max_rounds)),
            no_progress_count=0,
            finish_reason="",
            expires_at=now + timedelta(seconds=max(60, int(self.settings.clarification_ttl_seconds))),
            version=1,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def resume(self, user: UserAccount, session: ChatSession, text: str) -> ClarificationResolution:
        return self.resume_or_bypass(user=user, session=session, text=text, high_risk=False)

    def resume_or_bypass(self, *, user: UserAccount, session: ChatSession, text: str, high_risk: bool) -> ClarificationResolution:
        row = self.active(user.id, session.id)
        if row is None:
            return ClarificationResolution()
        now = datetime.now(UTC).replace(tzinfo=None)
        if row.expires_at <= now:
            return self._finish(row, ClarificationStatus.EXPIRED, continue_current_message=True, finish_reason="EXPIRED")
        if high_risk:
            return self._finish(row, ClarificationStatus.INTERRUPTED, continue_current_message=True, finish_reason="HIGH_RISK_PREEMPTED")
        cancel_term = next((term for term in CANCEL_TERMS if term in text), None)
        if cancel_term:
            remainder = re.sub(r"^[，,；;。\s]+", "", text.split(cancel_term, 1)[1]).strip()
            return self._finish(
                row, ClarificationStatus.CANCELLED, handled=not bool(remainder), continue_current_message=bool(remainder),
                question="已取消本次信息补充。" if not remainder else "", model_input=remainder, finish_reason="USER_CANCELLED",
            )
        if is_new_topic(text):
            return self._finish(row, ClarificationStatus.INTERRUPTED, continue_current_message=True, model_input=text, finish_reason="TOPIC_CHANGED")
        try:
            resume = ClarificationResumeContext.from_payload(_load(row.resume_context_json, {}))
        except ValueError:
            return self._finish(row, ClarificationStatus.INTERRUPTED, continue_current_message=True, finish_reason="INVALID_STATE")
        target = next(item for item in resume.route_plan.work_items if item.work_item_id == resume.target_work_item_id)
        handler = get_clarification_handler(target.intent, self.settings)
        if handler is None or len(resume.expected_fields) != 1:
            return self._finish(row, ClarificationStatus.INTERRUPTED, continue_current_message=True, finish_reason="INVALID_STATE")
        extraction_fields = list(dict.fromkeys([*resume.expected_fields, *handler.fields]))
        extraction = handler.extract_slots(text, extraction_fields)
        if not extraction.values:
            return self._reask(row)
        known = handler.sanitize_known({**target.known_arguments, **extraction.values})
        remaining = tuple(item for item in target.missing_arguments if item.name not in extraction.values)
        from app.agents.routing import RoutePlan, WorkItem
        updated_target = WorkItem(
            work_item_id=target.work_item_id,
            intent=target.intent,
            objective=target.objective,
            source_text=target.source_text,
            task_text=(target.task_text + "；补充信息：" + text)[:240],
            source_refs=target.source_refs,
            context_refs=tuple(dict.fromkeys((*target.context_refs, f"clarification:{row.id}"))),
            evidence_facets=target.evidence_facets,
            known_arguments=known,
            missing_arguments=remaining,
            depends_on=target.depends_on,
            priority=target.priority,
            confidence=target.confidence,
            reason_codes=target.reason_codes,
        )
        items = tuple(updated_target if item.work_item_id == target.work_item_id else item for item in resume.route_plan.work_items)
        plan = RoutePlan(
            resume.route_plan.plan_id, resume.route_plan.primary_intent, resume.route_plan.intents, items,
            resume.route_plan.synthesis_order, resume.route_plan.confidence, resume.route_plan.reason_codes,
        )
        plan = RoutePlan.from_payload(plan.as_payload())
        if remaining:
            next_field = remaining[0].name
            updated_resume = ClarificationResumeContext(plan, resume.origin_plan_id, resume.target_work_item_id, (next_field,), row.round_count)
            question = handler.build_question(list(remaining))
            self._cas_update(
                row, known_arguments_json=_dump(known), missing_arguments_json=_dump([item.as_payload() for item in remaining]),
                resume_context_json=_dump(updated_resume.as_payload()), no_progress_count=0, round_count=row.round_count + 1,
                approved_question=question,
            )
            return ClarificationResolution(True, question=question, status=ClarificationStatus.WAITING_USER, pending_id=row.id, pending=row, resolved_arguments=known, resume_context=updated_resume.as_payload())
        updated_resume = ClarificationResumeContext(plan, resume.origin_plan_id, resume.target_work_item_id, (), row.round_count)
        if not self._cas_update(
            row, status=ClarificationStatus.RESOLVED.value, known_arguments_json=_dump(known), missing_arguments_json="[]",
            resume_context_json=_dump(updated_resume.as_payload()), no_progress_count=0, finish_reason="RESOLVED",
        ):
            return ClarificationResolution(handled=True, pending_id=row.id, pending=row)
        return ClarificationResolution(
            handled=False, model_input=row.original_message, status=ClarificationStatus.RESOLVED, pending_id=row.id,
            pending=row, resolved_arguments=known, resume_context=updated_resume.as_payload(),
        )

    def _reask(self, row: PendingClarification) -> ClarificationResolution:
        next_no_progress = row.no_progress_count + 1
        next_round = row.round_count + 1
        if next_round > row.max_rounds or next_no_progress >= max(1, int(self.settings.clarification_max_no_progress)):
            return self._finish(
                row, ClarificationStatus.CANCELLED, handled=True, question="连续多次未识别到有效信息，本次信息补充已结束。",
                finish_reason="NO_PROGRESS_EXHAUSTED", updates={"round_count": next_round, "no_progress_count": next_no_progress},
            )
        self._cas_update(row, round_count=next_round, no_progress_count=next_no_progress)
        return ClarificationResolution(True, question=row.approved_question, status=ClarificationStatus.WAITING_USER, pending_id=row.id, pending=row)

    def _finish(self, row: PendingClarification, status: ClarificationStatus, *, handled: bool = False, continue_current_message: bool = False, question: str = "", model_input: str = "", finish_reason: str, updates: dict[str, object] | None = None) -> ClarificationResolution:
        self._cas_update(row, status=status.value, finish_reason=finish_reason, **(updates or {}))
        return ClarificationResolution(handled, continue_current_message, question, model_input, status, row.id, row)

    def _cas_update(self, row: PendingClarification, **values: object) -> bool:
        expected = row.version
        values["version"] = expected + 1
        values["updated_at"] = datetime.now(UTC).replace(tzinfo=None)
        updated = self.db.query(PendingClarification).filter(
            PendingClarification.id == row.id,
            PendingClarification.user_id == row.user_id,
            PendingClarification.session_id == row.session_id,
            PendingClarification.status == ClarificationStatus.WAITING_USER.value,
            PendingClarification.version == expected,
        ).update(values, synchronize_session=False)
        if updated:
            self.db.commit()
            self.db.refresh(row)
            return True
        self.db.rollback()
        return False


def _load(value: str, fallback):
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
