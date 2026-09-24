import asyncio
import unittest
from datetime import UTC, datetime, timedelta

from app.services.model_completion import (
    ModelCompletionMetadata,
    ModelFinishReason,
    ModelUsage,
)
from app.services.turn_metrics import (
    TurnMetricsCollector,
    bind_turn_metrics,
    current_turn_metrics,
    finish_model_call,
    mark_first_content_ready,
    mark_model_first_delta,
    start_model_call,
)


class FakeClock:
    def __init__(self):
        self.value = 1_000_000_000

    def __call__(self):
        return self.value

    def advance_ms(self, milliseconds: int):
        self.value += milliseconds * 1_000_000


def metadata(*, prompt=10, output=4, thinking=2, provider="openai"):
    return ModelCompletionMetadata(
        provider=provider,
        model="test-model",
        finish_reason=ModelFinishReason.STOP,
        semantic_finish_seen=True,
        transport_terminal_seen=True,
        terminal_signal="fixture",
        provider_finish_reason="stop",
        configured_output_limit=100,
        usage=ModelUsage(prompt_tokens=prompt, output_tokens=output, thinking_tokens=thinking),
        duration_ms=99,
    )


class TurnMetricsCollectorTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.wall_time = datetime(2026, 8, 2, 2, 0, tzinfo=UTC)
        self.collector = TurnMetricsCollector(
            "request-1",
            clock_ns=self.clock,
            utc_now=lambda: self.wall_time + timedelta(milliseconds=(self.clock.value - 1_000_000_000) // 1_000_000),
        )

    def test_first_delta_and_first_content_are_recorded_once(self):
        with bind_turn_metrics(self.collector):
            self.clock.advance_ms(20)
            handle = start_model_call(
                purpose="response.generate",
                provider="openai",
                model="test-model",
                stream=True,
                estimated_prompt_tokens=12,
            )
            self.clock.advance_ms(30)
            mark_model_first_delta(handle, "")
            mark_model_first_delta(handle, "首个 token")
            self.clock.advance_ms(10)
            mark_model_first_delta(handle, "后续 token")
            finish_model_call(handle, metadata=metadata(), estimated_output_tokens=5)
            self.clock.advance_ms(5)
            mark_first_content_ready()
            self.clock.advance_ms(5)
            mark_first_content_ready()
            self.collector.mark_turn_finished("COMPLETED")

        result = self.collector.as_dict()
        self.assertEqual(result["finalModelTtftMs"], 30)
        self.assertEqual(result["serverE2eTtftMs"], 50)
        self.assertEqual(result["orchestrationBeforeModelMs"], 20)
        self.assertEqual(result["firstContentReadyMs"], 65)
        self.assertEqual(result["serverTurnDurationMs"], 70)

    def test_sequences_exact_totals_and_thinking_not_double_counted(self):
        with bind_turn_metrics(self.collector):
            first = start_model_call("understanding.complete", "openai", "m1", False, 8)
            self.clock.advance_ms(2)
            finish_model_call(first, metadata=metadata(prompt=10, output=4, thinking=3))
            second = start_model_call("response.generate", "openai", "m2", True, 9)
            self.clock.advance_ms(3)
            mark_model_first_delta(second, "回答")
            finish_model_call(second, metadata=metadata(prompt=20, output=6, thinking=5))
            self.collector.mark_turn_finished("COMPLETED")

        result = self.collector.as_dict()
        self.assertEqual([item["sequence"] for item in result["calls"]], [1, 2])
        usage = result["tokenUsage"]
        self.assertEqual(usage["promptTokens"], 30)
        self.assertEqual(usage["outputTokens"], 10)
        self.assertEqual(usage["thinkingTokens"], 8)
        self.assertEqual(usage["totalTokens"], 40)
        self.assertEqual(usage["accuracy"], "EXACT")

    def test_mixed_estimated_and_unavailable_accuracy(self):
        with bind_turn_metrics(self.collector):
            exact = start_model_call("safety.complete", "openai", "m1", False, 6)
            finish_model_call(exact, metadata=metadata(prompt=5, output=None), estimated_output_tokens=3)
            failed = start_model_call("knowledge.plan_v1", "openai", "m2", False, 7)
            finish_model_call(failed, status="FAILED", error_code="NETWORK")
            self.collector.mark_turn_finished("FAILED")

        result = self.collector.as_dict()
        self.assertEqual(result["tokenUsage"]["accuracy"], "MIXED")
        self.assertEqual(result["calls"][0]["promptTokenSource"], "PROVIDER")
        self.assertEqual(result["calls"][0]["outputTokenSource"], "ESTIMATED")
        self.assertEqual(result["calls"][1]["outputTokenSource"], "UNAVAILABLE")

    def test_all_mock_calls_are_estimated(self):
        with bind_turn_metrics(self.collector):
            handle = start_model_call("response.generate", "mock", "mock", True, 11)
            mark_model_first_delta(handle, "回答")
            finish_model_call(
                handle,
                metadata=metadata(prompt=12, output=5, thinking=None, provider="mock"),
                estimated_output_tokens=4,
            )
            self.collector.mark_turn_finished("COMPLETED")

        usage = self.collector.as_dict()["tokenUsage"]
        self.assertEqual(usage["accuracy"], "ESTIMATED")
        self.assertEqual(usage["estimatedCallCount"], 1)

    def test_failed_call_is_retained_and_payload_is_privacy_safe(self):
        with bind_turn_metrics(self.collector):
            handle = start_model_call("knowledge.plan_v1.repair1", "ollama", "model", False, 9)
            finish_model_call(handle, status="FAILED", error_code="PROVIDER_ERROR")
            self.collector.mark_turn_finished("FAILED")

        result = self.collector.as_dict()
        self.assertEqual(result["calls"][0]["status"], "FAILED")
        self.assertEqual(result["calls"][0]["stage"], "knowledge")
        self.assertEqual(result["calls"][0]["normalizedErrorCode"], "PROVIDER_ERROR")
        self.assertEqual(result["calls"][0]["retryCount"], 1)
        keys = _all_keys(result)
        for forbidden in ("messages", "message", "content", "userInput", "user_input", "promptText"):
            self.assertNotIn(forbidden, keys)

    def test_helpers_are_noop_without_context(self):
        handle = start_model_call("application.unspecified", "mock", "mock", False, 1)
        mark_model_first_delta(handle, "token")
        finish_model_call(handle, estimated_output_tokens=1)
        mark_first_content_ready()
        self.assertIsNone(current_turn_metrics())
        self.assertTrue(handle.noop)


class TurnMetricsContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_contexts_do_not_mix_request_ids(self):
        async def observe(request_id: str):
            collector = TurnMetricsCollector(request_id)
            with bind_turn_metrics(collector):
                await asyncio.sleep(0)
                self.assertEqual(current_turn_metrics().request_id, request_id)
            return collector.request_id

        self.assertEqual(
            await asyncio.gather(observe("request-a"), observe("request-b")),
            ["request-a", "request-b"],
        )


def _all_keys(value):
    if isinstance(value, dict):
        return set(value).union(*( _all_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_all_keys(item) for item in value)) if value else set()
    return set()


if __name__ == "__main__":
    unittest.main()
