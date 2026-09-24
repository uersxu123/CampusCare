from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from app.core.enums import IntentType
from app.services.clarification_models import MissingArgument, SlotExtraction


CONTROL_FIELDS = {"requestId", "sessionId", "userId", "status", "version", "publicId"}


@dataclass(frozen=True)
class ClarificationFieldSpec:
    label: str
    reason_codes: frozenset[str]
    allowed_values: tuple[str, ...] = ()
    validator: Callable[[str], bool] = lambda value: bool(value)


def _short_value(value: str) -> bool:
    return bool(value.strip()) and len(value.strip()) <= 200 and "?" not in value and "？" not in value


FIELD_REGISTRY: dict[IntentType, dict[str, ClarificationFieldSpec]] = {
    IntentType.ACADEMIC: {
        "course": ClarificationFieldSpec("课程范围", frozenset({"COURSE_SCOPE_REQUIRED"}), validator=_short_value),
        "deadline": ClarificationFieldSpec("目标日期", frozenset({"TARGET_DATE_REQUIRED"}), validator=_short_value),
        "availableTimeWindows": ClarificationFieldSpec("可用时间", frozenset({"AVAILABLE_TIME_REQUIRED"}), validator=_short_value),
        "focusProblem": ClarificationFieldSpec("当前难点", frozenset({"FOCUS_PROBLEM_REQUIRED"}), validator=_short_value),
    },
    IntentType.CAMPUS: {
        "studentType": ClarificationFieldSpec("学生类型", frozenset({"POLICY_SCOPE_REQUIRED"}), ("本科生", "研究生")),
        "site": ClarificationFieldSpec("校区", frozenset({"SITE_SCOPE_REQUIRED"}), ("南望山校区", "未来城校区")),
        "academicPeriod": ClarificationFieldSpec("学年或学期", frozenset({"ACADEMIC_PERIOD_REQUIRED"}), validator=_short_value),
        "policyName": ClarificationFieldSpec("具体制度", frozenset({"POLICY_NAME_REQUIRED"}), validator=_short_value),
        "serviceItem": ClarificationFieldSpec("业务事项", frozenset({"SERVICE_ITEM_REQUIRED"}), validator=_short_value),
        "referent": ClarificationFieldSpec("指代对象", frozenset({"REFERENT_REQUIRED"}), validator=_short_value),
    },
}
KNOWLEDGE_FACT_FIELDS = frozenset({"officialDeadline", "materials", "phone", "location", "processingTime", "policyText", "eligibility"})


class WorkItemClarificationHandler:
    def __init__(self, intent: IntentType):
        self.intent = intent
        self.fields = FIELD_REGISTRY.get(intent, {})

    def sanitize_known(self, values: object) -> dict[str, str]:
        if not isinstance(values, dict):
            return {}
        clean: dict[str, str] = {}
        for name, value in values.items():
            if not isinstance(name, str) or not isinstance(value, str) or name in CONTROL_FIELDS:
                continue
            text = _clean(value)
            spec = self.fields.get(name)
            if spec is None:
                if text and len(text) <= 500:
                    clean[name] = text
                continue
            normalized = self._normalize_allowed(spec, text)
            if normalized and spec.validator(normalized):
                clean[name] = normalized
        return clean

    def sanitize_missing(self, values: object) -> list[MissingArgument]:
        result: list[MissingArgument] = []
        seen: set[str] = set()
        for raw in values if isinstance(values, (list, tuple)) else []:
            try:
                item = raw if isinstance(raw, MissingArgument) else MissingArgument.from_payload(raw)
            except (TypeError, ValueError):
                continue
            spec = self.fields.get(item.name)
            if spec is None or item.name in KNOWLEDGE_FACT_FIELDS or item.name in seen or item.reason_code not in spec.reason_codes:
                continue
            if item.allowed_values and item.allowed_values != spec.allowed_values:
                continue
            result.append(MissingArgument(item.name, item.reason_code, spec.allowed_values))
            seen.add(item.name)
        return result

    def extract_slots(self, text: str, expected_fields: list[str]) -> SlotExtraction:
        values: dict[str, str] = {}
        invalid: dict[str, str] = {}
        for name in expected_fields:
            spec = self.fields.get(name)
            if spec is None:
                continue
            value = self._extract_value(name, text, spec)
            if value and spec.validator(value):
                values[name] = value
            elif value:
                invalid[name] = value
        return SlotExtraction(values, invalid, tuple(name for name in expected_fields if name not in values))

    def build_question(self, missing: list[MissingArgument]) -> str:
        if not missing:
            return ""
        item = missing[0]
        spec = self.fields[item.name]
        return f"请确认{spec.label}：{'还是'.join(item.allowed_values)}？" if item.allowed_values else f"请补充{spec.label}。"

    def _extract_value(self, name: str, text: str, spec: ClarificationFieldSpec) -> str:
        if spec.allowed_values:
            return next((allowed for allowed in spec.allowed_values if allowed in text or allowed.removesuffix("校区") in text), "")
        labeled = re.search(rf"(?:{re.escape(spec.label)}|{re.escape(name)})\s*(?:[:：=]|是|为)\s*([^，,；;。]+)", text)
        if labeled:
            return _clean(labeled.group(1))
        if self.intent == IntentType.ACADEMIC:
            if name == "course":
                match = re.search(r"(?:^|[，,；;。])\s*(?:我选|就选|选择)?\s*([^，,；;。]+?)(?:吧|然后|截止|考试截止|$)", text)
                if match:
                    value = _clean(match.group(1))
                    if value and not any(term in value for term in ("考试", "截止时间", "学习计划")):
                        return value
            if name == "deadline":
                match = re.search(r"(?<!\d)(\d{1,2})\s*(?:月|[./-])\s*(\d{1,2})\s*日?(?:[^0-9]{0,8}(\d{1,2})\s*[:：]\s*(\d{2}))?", text)
                if match:
                    month, day = int(match.group(1)), int(match.group(2))
                    if 1 <= month <= 12 and 1 <= day <= 31:
                        if match.group(3) is None:
                            return f"{month}月{day}日"
                        hour, minute = int(match.group(3)), int(match.group(4))
                        if 0 <= hour <= 23 and 0 <= minute <= 59:
                            return f"{month}月{day}日 {hour:02d}:{minute:02d}"
        return _clean(text) if len(self.fields) == 1 else ""

    @staticmethod
    def _normalize_allowed(spec: ClarificationFieldSpec, value: str) -> str:
        if not spec.allowed_values:
            return value
        return next((allowed for allowed in spec.allowed_values if value in {allowed, allowed.removesuffix("校区")}), "")


def get_clarification_handler(intent: IntentType | str, settings: object | None = None) -> WorkItemClarificationHandler | None:
    del settings
    try:
        parsed = intent if isinstance(intent, IntentType) else IntentType(intent)
    except (TypeError, ValueError):
        return None
    return WorkItemClarificationHandler(parsed) if parsed in FIELD_REGISTRY else None


def is_new_topic(text: str) -> bool:
    value = _clean(text)
    return value in {"换个问题", "换一个问题"} or any(term in value for term in ("算了", "换个", "我最近", "想聊聊"))


def validate_study_plan_times(arguments: dict[str, str], settings: object | None = None) -> str:
    del settings
    fields = FIELD_REGISTRY[IntentType.ACADEMIC]
    return "" if all(fields[name].validator(_clean(value)) for name, value in arguments.items() if name in fields) else "学习计划参数无效。"


def _clean(value: object) -> str:
    normalized = str(value or "").replace("：", ":").replace("\x00", "")
    return re.sub(r"\s+", " ", normalized).strip(" \t\r\n，,；;。")
