from __future__ import annotations

import json

from app.schemas.dtos import AiMessage
from app.services.agent_loop import AgentLoop
from app.services.context_builder import _drop_largest_result, estimate_message_tokens
from app.services.model_completion import ModelCompletionMetadata, ModelFinishReason
from app.services.tool_models import AiToolCall, AiToolCompletion, AiToolDefinition, ToolResult


TOOL = AiToolDefinition(
    name="chat_readonly__rag_search",
    description="本地知识检索",
    input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
)


def _metadata(reason: ModelFinishReason) -> ModelCompletionMetadata:
    return ModelCompletionMetadata(
        provider="fake",
        model="fake",
        finish_reason=reason,
        semantic_finish_seen=True,
        transport_terminal_seen=True,
        terminal_signal="test",
        provider_finish_reason=reason.value,
        configured_output_limit=100,
    )


class _CaptureClient:
    def __init__(self):
        self.calls = 0
        self.tool_payload = None

    def complete_with_tools(self, messages, _tools, *, model_round, purpose):
        self.calls += 1
        if model_round == 1:
            return AiToolCompletion(
                "",
                (AiToolCall("call-1", TOOL.name, {"query": "休学流程"}),),
                _metadata(ModelFinishReason.TOOL_CALL),
            )
        self.tool_payload = json.loads(messages[-1].content)
        return AiToolCompletion("已依据证据回答。", (), _metadata(ModelFinishReason.STOP))


def test_below_storage_threshold_result_is_appended_without_truncation() -> None:
    original_data = {
        "status": "SUFFICIENT",
        "diagnostics": {"raw": "诊断" * 500},
        "items": [
            {"evidenceId": "ev-1", "content": "甲" * 2000},
            {"evidenceId": "ev-2", "content": "乙" * 2000},
        ],
    }

    class Executor:
        def execute(self, **_kwargs):
            return ToolResult(True, "OK", data=original_data, dispatched=True)

    client = _CaptureClient()
    result = AgentLoop(
        client=client,
        executor=Executor(),
    ).run(
        agent_name="AcademicPlanningAgent",
        messages=[AiMessage(role="user", content="休学流程")],
        tools=[TOOL],
    )

    assert result.stop_reason == "COMPLETED"
    assert client.tool_payload["ok"] is True
    assert client.tool_payload["code"] == "OK"
    assert client.tool_payload["data"]["status"] == "SUFFICIENT"
    assert client.tool_payload["data"] == original_data
    included = client.tool_payload["data"]["items"]
    assert included
    assert included[0]["evidenceId"] == "ev-1"
    assert "contentTruncated" not in included[0]
    assert original_data["items"][0]["content"] == "甲" * 2000
    assert "truncation" not in original_data


def test_minimal_tool_result_that_cannot_fit_stops_before_followup_model() -> None:
    class Client(_CaptureClient):
        pass

    class Executor:
        def execute(self, **_kwargs):
            return ToolResult(True, "OK", data={"status": "OK", "items": [{"evidenceId": "ev-1"}]})

    client = Client()
    result = AgentLoop(
        client=client,
        executor=Executor(),
        max_single_result_chars=40,
    ).run(
        agent_name="AcademicPlanningAgent",
        messages=[AiMessage(role="user", content="休学流程")],
        tools=[TOOL],
    )

    assert client.calls == 1
    assert result.stop_reason == "TOOL_RESULT_BUDGET_EXCEEDED"
    assert result.tool_results[0][1].data["items"][0]["evidenceId"] == "ev-1"


def test_assistant_tool_arguments_are_included_in_next_request_budget() -> None:
    class Client:
        def __init__(self):
            self.calls = 0

        def complete_with_tools(self, _messages, _tools, *, model_round, purpose):
            self.calls += 1
            if self.calls > 1:
                raise AssertionError("超预算后不应发起下一轮模型请求")
            return AiToolCompletion(
                "",
                (AiToolCall("call-large", TOOL.name, {"query": "中" * 1000}),),
                _metadata(ModelFinishReason.TOOL_CALL),
            )

    class Executor:
        def execute(self, **_kwargs):
            return ToolResult(True, "OK", data={"status": "OK"})

    client = Client()
    result = AgentLoop(
        client=client,
        executor=Executor(),
        input_max_tokens=450,
        model_context_tokens=4096,
        output_max_tokens=16,
        input_safety_margin_tokens=0,
    ).run(
        agent_name="AcademicPlanningAgent",
        messages=[AiMessage(role="user", content="休学流程")],
        tools=[TOOL],
    )

    assert client.calls == 1
    assert result.stop_reason == "INPUT_BUDGET_EXCEEDED"
    assistant = AiMessage(
        role="assistant",
        tool_calls=[{"id": "call-large", "name": TOOL.name, "arguments": {"query": "中" * 1000}}],
    )
    assert estimate_message_tokens(assistant) > 450


def test_nested_evidence_compression_keeps_both_branch_identities() -> None:
    payload = {
        "specialistResults": [
            {
                "workItemId": "academic",
                "intent": "ACADEMIC",
                "status": "PARTIAL",
                "reasonCode": "MODEL_ROUND_BUDGET_EXCEEDED",
                "answerBrief": "甲" * 900,
                "evidenceItems": [{"evidenceId": "ev-a", "content": "证" * 900}],
            },
            {
                "workItemId": "campus",
                "intent": "CAMPUS",
                "status": "FAILED",
                "reasonCode": "TOOL_UNAVAILABLE",
                "answerBrief": "乙" * 900,
                "evidenceItems": [{"evidenceId": "ev-b", "content": "据" * 900}],
            },
        ]
    }
    dropped = []
    operations = 0
    while _drop_largest_result(payload, dropped):
        operations += 1
        assert operations < 30

    branches = payload["specialistResults"]
    assert [(item["workItemId"], item["status"], item["reasonCode"]) for item in branches] == [
        ("academic", "PARTIAL", "MODEL_ROUND_BUDGET_EXCEEDED"),
        ("campus", "FAILED", "TOOL_UNAVAILABLE"),
    ]
    assert [item["evidenceItems"][0]["evidenceId"] for item in branches] == ["ev-a", "ev-b"]
    assert all(item["evidenceItems"][0]["contentTruncated"] for item in branches)
    assert len(dropped) <= 4
