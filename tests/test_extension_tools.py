"""扩展工具契约与默认隔离的回归测试。"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.chat_tools.extensions import EXTENSION_TOOLS
from app.chat_tools.extensions.contracts import SCHEMAS, unavailable
from app.services.mcp_runtime import McpRuntime, McpServerConfig
from app.services.tool_executor import parse_call_tool_result
from app.services.tool_registry import ToolRegistry, validate_tool_arguments


ROOT = Path(__file__).resolve().parents[1]
VALID = {
    "get_my_courses": {},
    "get_my_timetable": {"start_date": "2026-09-01", "end_date": "2026-09-30"},
    "get_my_deadlines": {"start_date": "2026-09-01", "end_date": "2026-09-30", "types": ["EXAM", "ASSIGNMENT"]},
    "search_official_sources": {"query": "学校通知", "source_scope": "CURRENT_SCHOOL"},
    "get_my_application_status": {},
    "get_counseling_availability": {"start_date": "2026-09-01", "end_date": "2026-09-30", "mode": "ONLINE"},
}


@pytest.mark.parametrize("function", EXTENSION_TOOLS, ids=lambda fn: fn.__name__)
def test_valid_request_is_explicitly_not_configured(function):
    result = parse_call_tool_result(function(**VALID[function.__name__]))
    assert not result.ok
    assert result.code == "NOT_CONFIGURED"
    assert not result.cached
    assert result.data is None


@pytest.mark.parametrize("name,arguments", [
    ("get_my_courses", {"term_id": " "}),
    ("get_my_timetable", {"start_date": "2026-02-30", "end_date": "2026-03-01"}),
    ("get_my_timetable", {"start_date": "2026-09-02", "end_date": "2026-09-01"}),
    ("get_my_timetable", {"start_date": "2026-09-01", "end_date": "2026-10-02"}),
    ("get_my_deadlines", {"start_date": "2026-09-01", "end_date": "2026-09-01", "types": ["OTHER"]}),
    ("search_official_sources", {"query": "学校通知", "source_scope": "ANY_SITE"}),
    ("search_official_sources", {"query": "学校通知", "source_scope": "CURRENT_SCHOOL", "max_results": 4}),
    ("search_official_sources", {"query": "学校通知", "source_scope": "CURRENT_SCHOOL", "date_from": "2026-09-02", "date_to": "2026-09-01"}),
    ("get_my_application_status", {"application_id": ""}),
    ("get_counseling_availability", {"start_date": "2026-09-01", "end_date": "2026-09-01", "mode": "OTHER"}),
])
def test_invalid_business_arguments(name, arguments):
    assert parse_call_tool_result(unavailable(name, arguments)).code == "INVALID_ARGUMENT"


@pytest.mark.parametrize("name", VALID)
def test_schema_disallows_model_supplied_identity(name):
    with pytest.raises(ValueError):
        validate_tool_arguments(SCHEMAS[name], {**VALID[name], "student_id": "someone_else"})


@pytest.mark.parametrize("flag", [None, "false", "1"])
def test_default_server_does_not_import_or_register_extensions(flag):
    env = dict(os.environ)
    env.pop("MINDBRIDGE_EXTENSION_TOOLS_ENABLED", None)
    if flag is not None:
        env["MINDBRIDGE_EXTENSION_TOOLS_ENABLED"] = flag
    script = "import sys,json; from app.chat_tools.server import mcp; print(json.dumps({'tools': sorted(mcp._tool_manager._tools), 'loaded': any(x.startswith('app.chat_tools.extensions') for x in sys.modules)}))"
    completed = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, text=True, capture_output=True, check=True, timeout=20)
    state = json.loads(completed.stdout)
    assert state == {"tools": ["get_current_weather", "rag_search"], "loaded": False}


def test_explicit_stdio_discovery_calls_and_existing_agent_permissions():
    env = dict(os.environ)
    env["MINDBRIDGE_EXTENSION_TOOLS_ENABLED"] = "true"
    runtime = McpRuntime([McpServerConfig("chat_readonly", sys.executable, ("-m", "app.chat_tools.server"), env)], connect_timeout=10)
    try:
        discovered = runtime.list_tools_sync("chat_readonly")
        assert {tool.name for tool in discovered} == {*VALID, "rag_search", "get_current_weather"}
        registry = ToolRegistry()
        registry.register_tools("chat_readonly", discovered)
        assert not registry.errors
        assert [t.original_name for t in registry.for_agent("GeneralChatAgent")] == ["get_current_weather"]
        for agent in ("AcademicPlanningAgent", "CampusAffairsAgent", "PsychologicalSupportAgent"):
            assert [t.original_name for t in registry.for_agent(agent)] == ["rag_search"]
        for name, arguments in VALID.items():
            result = parse_call_tool_result(runtime.call_tool_sync("chat_readonly", name, arguments, 5))
            assert result.code == "NOT_CONFIGURED"
        invalid = runtime.call_tool_sync("chat_readonly", "get_my_timetable", {"start_date": "2026-02-30", "end_date": "2026-03-01"}, 5)
        assert parse_call_tool_result(invalid).code == "INVALID_ARGUMENT"
    finally:
        runtime.shutdown()
