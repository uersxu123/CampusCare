"""预留工具契约；未接入时不返回虚构业务数据。"""
from __future__ import annotations

import json
from datetime import date
from typing import TypedDict

from jsonschema import Draft202012Validator, FormatChecker
from mcp.types import CallToolResult, TextContent


class ExtensionData(TypedDict):
    """未来成功查询的共同载体；records 的字段见 RECORD_FIELDS。"""
    status: str
    records: list[dict]
    fetchedAt: str
    missingFields: list[str]
    truncated: bool


RECORD_FIELDS = {
    "get_my_courses": ("course_id", "course_name", "credits", "class_id", "term_id"),
    "get_my_timetable": ("course_id", "course_name", "start_at", "end_at", "location", "status"),
    "get_my_deadlines": ("event_id", "course_id", "type", "title", "due_at", "start_at", "status"),
    "search_official_sources": ("title", "url", "publisher", "published_at", "fetched_at", "excerpt"),
    "get_my_application_status": ("application_id", "service_name", "status", "current_step", "updated_at", "missing_materials"),
    "get_counseling_availability": ("slot_id", "start_at", "end_at", "location", "mode", "available", "booking_url"),
}


def optional_text(max_length: int = 80) -> dict:
    return {"type": ["string", "null"], "minLength": 1, "maxLength": max_length, "pattern": r"\S", "default": None}


def schema(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


DATE = {"type": "string", "format": "date"}
COURSE_IDS = {"type": ["array", "null"], "items": {"type": "string", "minLength": 1, "maxLength": 80, "pattern": r"\S"}, "minItems": 1, "maxItems": 50, "uniqueItems": True, "default": None}
SCHEMAS = {
    "get_my_courses": schema({"term_id": optional_text()}),
    "get_my_timetable": schema({"start_date": DATE, "end_date": DATE, "course_ids": COURSE_IDS}, ("start_date", "end_date")),
    "get_my_deadlines": schema({"start_date": DATE, "end_date": DATE, "course_ids": COURSE_IDS, "types": {"type": ["array", "null"], "items": {"enum": ["EXAM", "ASSIGNMENT"]}, "minItems": 1, "maxItems": 2, "uniqueItems": True, "default": None}}, ("start_date", "end_date")),
    "search_official_sources": schema({"query": {"type": "string", "minLength": 2, "maxLength": 500, "pattern": r"\S"}, "source_scope": {"enum": ["CURRENT_SCHOOL", "EDUCATION_AUTHORITIES"]}, "date_from": {"type": ["string", "null"], "format": "date", "default": None}, "date_to": {"type": ["string", "null"], "format": "date", "default": None}, "max_results": {"type": "integer", "minimum": 1, "maximum": 3, "default": 3}}, ("query", "source_scope")),
    "get_my_application_status": schema({"application_id": optional_text(), "service_type": optional_text(), "term_id": optional_text()}),
    "get_counseling_availability": schema({"start_date": DATE, "end_date": DATE, "campus": optional_text(), "mode": {"enum": ["ONLINE", "OFFLINE", None], "default": None}}, ("start_date", "end_date")),
}


def error(code: str, message: str) -> CallToolResult:
    payload = {"error": {"code": code, "message": message}}
    return CallToolResult(isError=True, structuredContent=payload, content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))])


def unavailable(tool_name: str, arguments: dict) -> CallToolResult:
    """先验证业务参数，再报告未接入；此处没有网络、数据库或认证副作用。"""
    errors = list(Draft202012Validator(SCHEMAS[tool_name], format_checker=FormatChecker()).iter_errors(arguments))
    if errors:
        field = ".".join(str(part) for part in errors[0].path) or "请求"
        return error("INVALID_ARGUMENT", f"{field} 参数无效，请按工具定义填写。")
    start = arguments.get("start_date") or arguments.get("date_from")
    end = arguments.get("end_date") or arguments.get("date_to")
    if start and end:
        days = (date.fromisoformat(end) - date.fromisoformat(start)).days
        if days < 0:
            return error("INVALID_ARGUMENT", "结束日期不能早于开始日期。")
        if tool_name != "search_official_sources" and days >= 31:
            return error("INVALID_ARGUMENT", "查询范围最多为31天（含起止日期）。")
    return error("NOT_CONFIGURED", f"{tool_name} 的真实数据接口尚未接入，当前仅提供工具契约。")
