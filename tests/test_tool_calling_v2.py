from types import SimpleNamespace

import pytest

from app.schemas.dtos import AiMessage
from app.services.agent_loop import AgentLoop
from app.services.ai import (
    AiClient,
    _serialize_ollama_message,
    _serialize_openai_message,
    parse_ollama_tool_completion_payload,
    parse_openai_tool_completion_payload,
)
from app.services.model_completion import ModelCompletionMetadata, ModelFinishReason
from app.services.tool_models import AiToolCall, AiToolCompletion, AiToolDefinition, ToolResult


WEATHER = AiToolDefinition(
    name="readonly__get_current_weather",
    description="查询当前天气",
    input_schema={
        "type": "object",
        "properties": {"location": {"type": "string"}},
        "required": ["location"],
        "additionalProperties": False,
    },
)

RAG = AiToolDefinition(
    name="chat_readonly__rag_search",
    description="本地知识检索",
    input_schema={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
)


def _metadata(reason: ModelFinishReason) -> ModelCompletionMetadata:
    return ModelCompletionMetadata(
        provider="fake",
        model="fake",
        finish_reason=reason,
        semantic_finish_seen=True,
        transport_terminal_seen=True,
        terminal_signal="response",
        provider_finish_reason=reason.value.lower(),
        configured_output_limit=100,
    )


def test_ollama_tool_call_accepts_object_arguments_and_generates_missing_id() -> None:
    completion = parse_ollama_tool_completion_payload(
        {
            "done": True,
            "done_reason": "stop",
            "message": {
                "content": "",
                "tool_calls": [{"function": {"name": WEATHER.name, "arguments": {"location": "武汉"}}}],
            },
        },
        provider="ollama",
        model="qwen3:8b",
        configured_output_limit=1024,
        model_round=2,
    )

    assert completion.verified_complete
    assert completion.metadata.finish_reason == ModelFinishReason.TOOL_CALL
    assert completion.tool_calls[0].id.startswith("ollama_r2_0_")
    assert completion.tool_calls[0].arguments == {"location": "武汉"}


def test_openai_tool_call_rejects_duplicate_argument_keys() -> None:
    with pytest.raises(Exception):
        parse_openai_tool_completion_payload(
            {
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": None,
                        "tool_calls": [{
                            "id": "call-1",
                            "function": {"name": WEATHER.name, "arguments": '{"location":"武汉","location":"北京"}'},
                        }],
                    },
                }],
            },
            provider="openai",
            model="compatible",
            configured_output_limit=1024,
        )


def test_provider_specific_assistant_and_tool_message_serialization() -> None:
    assistant = AiMessage(
        role="assistant",
        tool_calls=[{"id": "call-1", "name": WEATHER.name, "arguments": {"location": "武汉"}}],
    )
    tool = AiMessage(role="tool", content='{"ok":true}', tool_call_id="call-1", name=WEATHER.name)

    ollama_assistant = _serialize_ollama_message(assistant)
    ollama_tool = _serialize_ollama_message(tool)
    openai_assistant = _serialize_openai_message(assistant)
    openai_tool = _serialize_openai_message(tool)

    assert ollama_assistant["tool_calls"][0]["function"]["arguments"] == {"location": "武汉"}
    assert "id" not in ollama_assistant["tool_calls"][0]
    assert ollama_tool["tool_name"] == WEATHER.name
    assert openai_assistant["tool_calls"][0]["id"] == "call-1"
    assert openai_tool["tool_call_id"] == "call-1"


def test_ai_client_projects_tools_into_ollama_and_openai_payloads() -> None:
    settings = SimpleNamespace(
        ai_provider="ollama",
        ollama_model="qwen3:8b",
        openai_model="compatible",
        ai_temperature=0.1,
        ai_max_tokens=1024,
        ollama_num_ctx=8192,
        ai_think=False,
        openai_stream_include_usage=False,
    )
    client = AiClient(settings)

    assert client._ollama_payload([AiMessage(role="user", content="天气")], stream=False, tools=[WEATHER])["tools"][0]["function"]["parameters"] == WEATHER.input_schema
    assert client._openai_payload([AiMessage(role="user", content="天气")], stream=False, tools=[WEATHER])["tools"][0]["function"]["name"] == WEATHER.name


class FakeClient:
    def __init__(self):
        self.messages = []

    def complete_with_tools(self, messages, tools, *, model_round, purpose):
        self.messages.append(list(messages))
        if model_round == 1:
            return AiToolCompletion(
                "",
                (AiToolCall("call-1", WEATHER.name, {"location": "武汉"}),),
                _metadata(ModelFinishReason.TOOL_CALL),
            )
        return AiToolCompletion("武汉当前晴朗。", (), _metadata(ModelFinishReason.STOP))


class FakeExecutor:
    def execute(self, **kwargs):
        assert kwargs["agent_name"] == "GeneralChatAgent"
        return ToolResult(True, "OK", data={"temperatureC": 31.0})


def test_agent_loop_pairs_assistant_tool_messages_and_returns_final_content() -> None:
    client = FakeClient()
    result = AgentLoop(client=client, executor=FakeExecutor()).run(
        agent_name="GeneralChatAgent",
        messages=[AiMessage(role="user", content="武汉天气")],
        tools=[WEATHER],
    )

    assert result.content == "武汉当前晴朗。"
    assert result.stop_reason == "COMPLETED"
    second_round = client.messages[1]
    assert second_round[-2].role == "assistant"
    assert second_round[-1].role == "tool"
    assert second_round[-1].tool_call_id == "call-1"


def test_agent_loop_keeps_rag_result_when_followup_model_call_errors() -> None:
    class FailingFollowupClient:
        def complete_with_tools(self, _messages, _tools, *, model_round, purpose):
            assert purpose == f"AcademicPlanningAgent.agent_loop.round{model_round}"
            if model_round == 1:
                return AiToolCompletion(
                    "",
                    (AiToolCall("call-rag", RAG.name, {"query": "奖学金条件"}),),
                    _metadata(ModelFinishReason.TOOL_CALL),
                )
            raise RuntimeError("provider unavailable")

    class RagExecutor:
        def execute(self, **kwargs):
            assert kwargs["tool_name"] == RAG.name
            return ToolResult(True, "OK", data={"status": "SUFFICIENT", "items": []})

    result = AgentLoop(client=FailingFollowupClient(), executor=RagExecutor()).run(
        agent_name="AcademicPlanningAgent",
        messages=[AiMessage(role="user", content="奖学金条件")],
        tools=[RAG],
    )

    assert result.stop_reason == "MODEL_ERROR"
    assert result.model_rounds == 2
    assert len(result.tool_results) == 1
    assert result.tool_results[0][1].code == "OK"
