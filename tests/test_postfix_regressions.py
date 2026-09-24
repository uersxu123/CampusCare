"""独立审查探针：只用合成输入和假模型，不运行正式 Smoke。"""
from __future__ import annotations

import json
from types import SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import patch

from app.evaluation.reporting.writer import EvaluationReportWriter
from app.schemas.dtos import AiMessage
from app.services.agent_loop import AgentLoop, _serialize_tool_result
from app.services.execution_control import ExecutionBudget, bind_execution_budget
from app.services.model_completion import ModelCompletionMetadata, ModelFinishReason
from app.services.tool_executor import ToolExecutor
from app.services.tool_models import AiToolCall, AiToolCompletion, ToolResult
from app.services.tool_registry import ToolRegistry


def test_budget_fitting_must_preserve_tool_json():
    original = ToolResult(True, "OK", data={
        "status": "OK", "items": [{"evidenceId": "synthetic-1", "content": "证" * 1000}],
    })
    serialized = _serialize_tool_result(original.as_payload(), 6000)
    assert serialized is not None
    json.loads(serialized)
    messages = [
        AiMessage(role="system", content="中" * 600),
        AiMessage(role="tool", content=serialized, tool_call_id="call-1"),
    ]
    loop = AgentLoop(client=None, executor=None, input_max_tokens=1000,
                     model_context_tokens=4096, output_max_tokens=16,
                     input_safety_margin_tokens=0)
    assert loop._fit_request_budget(messages, 0) is False
    # 最近两轮受保护，不能为了硬预算切断原文或 JSON。
    assert messages[-1].content == serialized
    json.loads(messages[-1].content)


def test_model_returning_after_deadline_must_not_dispatch_tool():
    now = [0.0]

    class Client:
        def complete_with_tools(self, *args, **kwargs):
            now[0] = 2.0
            metadata = ModelCompletionMetadata(
                provider="fake", model="fake", finish_reason=ModelFinishReason.TOOL_CALL,
                semantic_finish_seen=True, transport_terminal_seen=True,
                terminal_signal="test", provider_finish_reason="tool_calls",
                configured_output_limit=32,
            )
            return AiToolCompletion("", (
                AiToolCall("call-1", "chat_readonly__rag_search", {"query": "合成检索"}),
            ), metadata)

    class Runtime:
        def __init__(self):
            self.calls = []

        def call_tool_sync(self, server, name, arguments, timeout, **kwargs):
            self.calls.append({"timeout": timeout})
            return {"isError": False, "structuredContent": {"status": "OK", "items": []}}

    registry = ToolRegistry()
    registry.register_tools("chat_readonly", [{
        "name": "rag_search", "description": "合成工具",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}},
                        "required": ["query"], "additionalProperties": False},
    }])
    runtime = Runtime()
    executor = ToolExecutor(registry=registry, runtime=runtime)
    loop = AgentLoop(client=Client(), executor=executor)
    with bind_execution_budget(ExecutionBudget.start(1.0, clock=lambda: now[0])):
        result = loop.run(agent_name="AcademicPlanningAgent",
                          messages=[AiMessage(role="user", content="合成问题")],
                          tools=registry.definitions_for_agent("AcademicPlanningAgent"))
    assert result.stop_reason == "DEADLINE_EXCEEDED"
    assert runtime.calls == [], f"预算耗尽后仍派发工具：{runtime.calls}"


def test_identical_terminal_delivery_must_be_idempotent(tmp_path):
    writer = EvaluationReportWriter(tmp_path, "synthetic-review")
    outcome = {"case_id": "synthetic-case", "turn_index": 0,
               "response": "同一个结果", "error_code": None}

    class FakeDatetime:
        times = iter([
            datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
        ])

        @classmethod
        def now(cls, _timezone):
            return next(cls.times)

    with patch("app.evaluation.reporting.writer.datetime", FakeDatetime):
        assert writer.append_case(outcome) is True
        assert writer.append_case(outcome) is False


def test_recovered_outcome_is_idempotent_and_changed_response_conflicts(tmp_path):
    import pytest
    row = {"case_id": "synthetic", "turn_index": 0, "response": "原结果", "error_code": None}
    first = EvaluationReportWriter(tmp_path, "recovered")
    first.append_case(row)
    recovered = EvaluationReportWriter(tmp_path, "recovered")
    assert len(recovered.recover_cases()["records"]) == 1
    assert recovered.append_case(row) is False
    with pytest.raises(ValueError, match="terminal conflict"):
        recovered.append_case({**row, "response": "不同结果"})


def test_second_tool_is_not_dispatched_after_first_exhausts_budget():
    from app.services.tool_models import AiToolDefinition
    now = [0.0]
    calls = [AiToolCall("a", "synthetic", {"n": 1}), AiToolCall("b", "synthetic", {"n": 2})]

    class Client:
        def complete_with_tools(self, *args, **kwargs):
            return SimpleNamespace(content="", verified_complete=True, tool_calls=calls)

    class Executor:
        count = 0

        def execute(self, **kwargs):
            self.count += 1
            now[0] = 2.0
            return ToolResult(True, "OK", data={"value": "已完成"}, dispatched=True)

    executor = Executor()
    with bind_execution_budget(ExecutionBudget.start(1.0, clock=lambda: now[0])):
        result = AgentLoop(client=Client(), executor=executor).run(
            agent_name="synthetic", messages=[],
            tools=[AiToolDefinition("synthetic", "合成", {"type": "object"})],
        )
    assert executor.count == 1
    assert result.stop_reason == "DEADLINE_EXCEEDED"
    assert len(result.tool_results) == 1
    assert [item["status"] for item in result.call_details] == ["SUCCEEDED", "NOT_EXECUTED"]


def test_executor_rejects_zero_budget_without_touching_registry():
    class Registry:
        def resolve_for_agent(self, *args):
            raise AssertionError("过期请求不应进入执行路径")

    result = ToolExecutor(registry=Registry(), runtime=None).execute(
        agent_name="synthetic", tool_name="synthetic", arguments={}, remaining_seconds=0,
    )
    assert result.code == "DEADLINE_EXCEEDED"
    assert result.dispatched is False


def test_two_recent_tool_results_remain_valid_and_unchanged_on_overflow():
    fixed = json.dumps({"required": "甲" * 300}, ensure_ascii=False)
    shrinkable = json.dumps({"ok": True, "code": "OK", "data": {
        "status": "OK", "items": [{"evidenceId": "ev", "content": "乙" * 1000}],
    }}, ensure_ascii=False)
    messages = [AiMessage(role="tool", content=fixed), AiMessage(role="tool", content=shrinkable)]
    loop = AgentLoop(client=None, executor=None, input_max_tokens=800,
                     model_context_tokens=4096, output_max_tokens=16, input_safety_margin_tokens=0)
    assert not loop._fit_request_budget(messages, 0)
    assert messages[0].content == fixed
    assert messages[1].content == shrinkable
    assert json.loads(messages[1].content)["data"]["items"][0]["evidenceId"] == "ev"


def test_local_judge_honors_existing_context_and_think_configuration():
    from app.core.config import Settings
    from app.evaluation.config import EvaluationSettings
    from app.evaluation.judges.deepseek import DeepSeekJudge
    settings = Settings(_env_file=None, ollama_num_ctx=16384, ai_think=False)
    evaluation = EvaluationSettings(_env_file=None, judge_provider="ollama",
                                    judge_base_url="http://127.0.0.1:11434", judge_model="qwen3:8b")
    judge = DeepSeekJudge(settings, evaluation)
    payload = judge.client._ollama_payload([AiMessage(role="user", content="合成")], stream=False)
    assert payload["options"]["num_ctx"] == 16384
    assert payload["options"]["num_predict"] == evaluation.judge_max_tokens
    assert payload["think"] is False
    assert payload["model"] == "qwen3:8b"
