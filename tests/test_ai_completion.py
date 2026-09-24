import unittest
from unittest.mock import patch

from app.core.config import Settings
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient, parse_ollama_completion_payload, parse_openai_completion_payload
from app.services.agent_models import AgentModelRegistry
from app.services.model_completion import IncompleteGenerationError, ModelFinishReason
from app.services.turn_metrics import TurnMetricsCollector, bind_turn_metrics


class ProviderCompletionProtocolTests(unittest.TestCase):
    def test_openai_stream_usage_payload_respects_switch(self):
        enabled = AiClient(Settings(_env_file=None, ai_provider="openai", openai_stream_include_usage=True))
        disabled = AiClient(Settings(_env_file=None, ai_provider="openai", openai_stream_include_usage=False))

        self.assertEqual(enabled._openai_payload([], stream=True)["stream_options"], {"include_usage": True})
        self.assertNotIn("stream_options", disabled._openai_payload([], stream=True))
        self.assertNotIn("stream_options", enabled._openai_payload([], stream=False))

    def test_kimi_payload_forwards_thinking_mode(self):
        disabled = AiClient(Settings(_env_file=None, ai_provider="openai", openai_model="kimi-k2.6", ai_think=False))
        enabled = AiClient(Settings(_env_file=None, ai_provider="openai", openai_model="kimi-k2.6", ai_think=True))
        deepseek = AiClient(Settings(_env_file=None, ai_provider="openai", openai_model="deepseek-v4-flash"))

        self.assertEqual(disabled._openai_payload([], stream=False)["thinking"], {"type": "disabled"})
        self.assertEqual(enabled._openai_payload([], stream=False)["thinking"], {"type": "enabled"})
        self.assertNotIn("thinking", deepseek._openai_payload([], stream=False))

    def test_response_agent_ollama_payload_uses_explicit_profile(self):
        settings = Settings(
            _env_file=None,
            ai_provider="ollama",
            agent_model_response_max_tokens=1536,
            agent_model_response_think=False,
            ollama_num_ctx=16384,
            context_input_max_tokens=12000,
        )
        client = AgentModelRegistry(settings).client_for("ResponseAgent")
        payload = client._ollama_payload([], stream=True)

        self.assertEqual(payload["options"]["num_predict"], 1536)
        self.assertEqual(payload["options"]["num_ctx"], 16384)
        self.assertIs(payload["think"], False)

    def test_response_agent_rejects_invalid_context_budget(self):
        settings = Settings(
            _env_file=None,
            ai_provider="ollama",
            context_input_max_tokens=4000,
            context_model_safety_margin_tokens=512,
            agent_model_response_max_tokens=1024,
            ollama_num_ctx=4096,
        )

        with self.assertRaisesRegex(ValueError, "上下文预算"):
            AgentModelRegistry(settings).client_for("ResponseAgent")

    def test_ollama_stop_preserves_usage_and_hides_thinking(self):
        completion = parse_ollama_completion_payload(
            {
                "message": {"content": "完整回答", "thinking": "内部推理"},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 12,
                "eval_count": 8,
                "total_duration": 5_000_000,
            },
            provider="ollama",
            model="qwen3:8b",
            configured_output_limit=1536,
        )

        self.assertTrue(completion.verified_complete)
        self.assertEqual(completion.content, "完整回答")
        self.assertNotIn("内部推理", completion.content)
        self.assertEqual(completion.metadata.finish_reason, ModelFinishReason.STOP)
        self.assertEqual(completion.metadata.usage.prompt_tokens, 12)
        self.assertEqual(completion.metadata.usage.output_tokens, 8)
        self.assertTrue(completion.metadata.thinking_observed)

    def test_ollama_length_is_not_complete(self):
        completion = parse_ollama_completion_payload(
            {"message": {"content": "半句话"}, "done": True, "done_reason": "length"},
            provider="ollama",
            model="qwen3:8b",
            configured_output_limit=512,
        )

        self.assertFalse(completion.verified_complete)
        self.assertEqual(completion.metadata.finish_reason, ModelFinishReason.LENGTH)

    def test_ollama_thinking_only_is_empty_output(self):
        completion = parse_ollama_completion_payload(
            {
                "message": {"content": "", "thinking": "只有内部推理"},
                "done": True,
                "done_reason": "stop",
            },
            provider="ollama",
            model="qwen3:8b",
            configured_output_limit=512,
        )

        self.assertFalse(completion.verified_complete)
        self.assertEqual(completion.metadata.finish_reason, ModelFinishReason.EMPTY_OUTPUT)

    def test_ollama_missing_done_is_protocol_failure(self):
        with self.assertRaises(IncompleteGenerationError):
            parse_ollama_completion_payload(
                {"message": {"content": "连接提前结束"}},
                provider="ollama",
                model="qwen3:8b",
                configured_output_limit=512,
            )

    def test_openai_finish_reasons_are_normalized(self):
        stop = parse_openai_completion_payload(
            {"choices": [{"message": {"content": "完成"}, "finish_reason": "stop"}]},
            provider="openai",
            model="gpt-test",
            configured_output_limit=256,
        )
        length = parse_openai_completion_payload(
            {"choices": [{"message": {"content": "截断"}, "finish_reason": "length"}]},
            provider="openai",
            model="gpt-test",
            configured_output_limit=256,
        )
        filtered = parse_openai_completion_payload(
            {"choices": [{"message": {"content": ""}, "finish_reason": "content_filter"}]},
            provider="openai",
            model="gpt-test",
            configured_output_limit=256,
        )

        self.assertTrue(stop.verified_complete)
        self.assertEqual(length.metadata.finish_reason, ModelFinishReason.LENGTH)
        self.assertEqual(filtered.metadata.finish_reason, ModelFinishReason.CONTENT_FILTER)

    def test_openai_missing_finish_reason_is_protocol_failure(self):
        with self.assertRaises(IncompleteGenerationError):
            parse_openai_completion_payload(
                {"choices": [{"message": {"content": "没有语义终止"}, "finish_reason": None}]},
                provider="openai",
                model="gpt-test",
                configured_output_limit=256,
            )


class FakeStreamResponse:
    def __init__(self, lines):
        self.lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self.lines:
            yield line


class FakeAsyncClient:
    def __init__(self, lines):
        self.response = FakeStreamResponse(lines)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def stream(self, *_args, **_kwargs):
        return self.response


class ProviderStreamingMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_usage_only_frame_is_accepted_and_recorded(self):
        frames = [
            'data: {"choices":[{"delta":{"content":""},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":"回答"},"finish_reason":"stop"}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}',
            "data: [DONE]",
        ]
        settings = Settings(_env_file=None, ai_provider="openai", openai_model="test-model")
        collector = TurnMetricsCollector("request-stream")
        with (
            bind_turn_metrics(collector),
            patch("app.services.ai.httpx.AsyncClient", return_value=FakeAsyncClient(frames)),
        ):
            events = [
                event
                async for event in AiClient(settings, purpose_namespace="response").stream_events(
                    [AiMessage(role="user", content="测试")],
                    purpose="response.generate",
                )
            ]
            collector.mark_turn_finished("COMPLETED")

        self.assertEqual([event.kind for event in events], ["delta", "terminal"])
        self.assertEqual(events[-1].metadata.usage.prompt_tokens, 7)
        self.assertEqual(events[-1].metadata.usage.output_tokens, 3)
        call = collector.as_dict()["calls"][0]
        self.assertIsNotNone(call["ttftMs"])
        self.assertEqual(call["promptTokenSource"], "PROVIDER")
        self.assertEqual(call["outputTokenSource"], "PROVIDER")

    async def test_mock_stream_usage_is_estimated(self):
        collector = TurnMetricsCollector("request-mock")
        settings = Settings(_env_file=None, ai_provider="mock")
        with bind_turn_metrics(collector):
            events = [
                event
                async for event in AiClient(settings, purpose_namespace="response").stream_events(
                    [AiMessage(role="user", content="测试")],
                    purpose="response.generate",
                )
            ]
            collector.mark_turn_finished("COMPLETED")

        self.assertTrue(events)
        self.assertEqual(collector.as_dict()["tokenUsage"]["accuracy"], "ESTIMATED")

    async def test_stream_failure_before_delta_retains_failed_call(self):
        settings = Settings(_env_file=None, ai_provider="openai")
        collector = TurnMetricsCollector("request-failed")
        with (
            bind_turn_metrics(collector),
            patch(
                "app.services.ai.httpx.AsyncClient",
                return_value=FakeAsyncClient(['data: {"choices": "invalid"}']),
            ),
        ):
            with self.assertRaises(Exception):
                _ = [
                    event
                    async for event in AiClient(settings).stream_events(
                        [AiMessage(role="user", content="测试")],
                        purpose="response.generate",
                    )
                ]
            collector.mark_turn_finished("FAILED")

        call = collector.as_dict()["calls"][0]
        self.assertEqual(call["status"], "FAILED")
        self.assertIsNone(call["ttftMs"])

    async def test_usage_is_retained_when_stream_ends_without_finish_reason(self):
        frames = [
            'data: {"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":2}}',
            "data: [DONE]",
        ]
        settings = Settings(_env_file=None, ai_provider="openai")
        collector = TurnMetricsCollector("request-usage-eof")
        with (
            bind_turn_metrics(collector),
            patch("app.services.ai.httpx.AsyncClient", return_value=FakeAsyncClient(frames)),
        ):
            with self.assertRaises(IncompleteGenerationError):
                _ = [
                    event
                    async for event in AiClient(settings).stream_events(
                        [AiMessage(role="user", content="测试")],
                        purpose="response.generate",
                    )
                ]
            collector.mark_turn_finished("FAILED")

        call = collector.as_dict()["calls"][0]
        self.assertEqual(call["status"], "FAILED")
        self.assertEqual(call["promptTokens"], 9)
        self.assertEqual(call["outputTokens"], 2)
        self.assertEqual(call["promptTokenSource"], "PROVIDER")
        self.assertEqual(call["outputTokenSource"], "PROVIDER")


if __name__ == "__main__":
    unittest.main()
