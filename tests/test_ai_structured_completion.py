import json
import unittest
from unittest.mock import Mock, patch

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient, StructuredCompletionError, StructuredCompletionOptions
from app.services.turn_metrics import TurnMetricsCollector, bind_turn_metrics


class SampleOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(pattern="^(OK|STOP)$")


class NestedItem(BaseModel):
    value: str
    note: str | None = None


class NestedOutput(BaseModel):
    item: NestedItem


def response(payload):
    result = Mock()
    result.raise_for_status.return_value = None
    result.json.return_value = payload
    return result


class StructuredCompletionTests(unittest.TestCase):
    def test_openai_uses_strict_json_schema_and_purpose_options(self):
        settings = Settings(_env_file=None, ai_provider="openai", openai_model="test")
        with patch("app.services.ai.httpx.post", return_value=response({
            "choices": [{"message": {"content": '{"action":"OK"}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        })) as post:
            result = AiClient(settings).complete_structured(
                [AiMessage(role="user", content="test")],
                response_model=SampleOutput,
                schema_name="sample_v1",
                options=StructuredCompletionOptions(temperature=0.1, max_tokens=100),
            )
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertEqual(body["response_format"]["json_schema"]["schema"]["required"], ["action"])
        self.assertEqual(body["temperature"], 0.1)
        self.assertEqual(body["max_tokens"], 100)
        self.assertIsInstance(result.value, SampleOutput)
        self.assertEqual(result.metadata.usage.prompt_tokens, 3)

    def test_openai_strict_schema_requires_all_nested_properties(self):
        settings = Settings(_env_file=None, ai_provider="openai", openai_model="test")
        with patch("app.services.ai.httpx.post", return_value=response({
            "choices": [{"message": {"content": '{"item":{"value":"ok","note":null}}'}, "finish_reason": "stop"}],
        })) as post:
            result = AiClient(settings).complete_structured(
                [AiMessage(role="user", content="test")],
                response_model=NestedOutput,
                schema_name="nested_v1",
                options=StructuredCompletionOptions(temperature=0, max_tokens=100),
            )

        schema = post.call_args.kwargs["json"]["response_format"]["json_schema"]["schema"]
        nested = schema["$defs"]["NestedItem"]
        self.assertEqual(schema["required"], ["item"])
        self.assertEqual(nested["required"], ["value", "note"])
        self.assertFalse(nested["additionalProperties"])
        self.assertNotIn("default", nested["properties"]["note"])
        self.assertIsNone(result.value.item.note)

    def test_ollama_uses_schema_format_and_options(self):
        settings = Settings(_env_file=None, ai_provider="ollama", ollama_model="test")
        client = Mock()
        client.post.return_value = response({
            "message": {"content": '{"action":"OK"}'}, "done": True, "done_reason": "stop"
        })
        with patch("app.services.ai._shared_ollama_client", return_value=client):
            AiClient(settings).complete_structured(
                [AiMessage(role="user", content="test")],
                response_model=SampleOutput,
                schema_name="sample_v1",
                options=StructuredCompletionOptions(temperature=0.2, max_tokens=120),
            )
        body = client.post.call_args.kwargs["json"]
        self.assertEqual(body["format"], SampleOutput.model_json_schema())
        self.assertEqual(body["options"]["temperature"], 0.2)
        self.assertEqual(body["options"]["num_predict"], 120)
        self.assertFalse(body["stream"])

    def test_invalid_json_is_repaired_once(self):
        settings = Settings(_env_file=None, ai_provider="openai")
        replies = [
            response({"choices": [{"message": {"content": "not-json"}, "finish_reason": "stop"}]}),
            response({"choices": [{"message": {"content": '{"action":"OK"}'}, "finish_reason": "stop"}]}),
        ]
        collector = TurnMetricsCollector("repair-request")
        with (
            bind_turn_metrics(collector),
            patch("app.services.ai.httpx.post", side_effect=replies) as post,
        ):
            result = AiClient(settings, purpose_namespace="knowledge").complete_structured(
                [AiMessage(role="user", content="test")],
                response_model=SampleOutput,
                schema_name="sample_v1",
                options=StructuredCompletionOptions(temperature=0, max_tokens=100),
            )
            collector.mark_turn_finished("COMPLETED")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(result.repair_count, 1)
        calls = collector.as_dict()["calls"]
        self.assertEqual([call["purpose"] for call in calls], ["knowledge.sample_v1", "knowledge.sample_v1.repair1"])
        repair_payload = post.call_args.kwargs["json"]
        self.assertIn("invalid_output", repair_payload["messages"][-1]["content"])

    def test_second_invalid_output_fails_closed(self):
        settings = Settings(_env_file=None, ai_provider="openai")
        invalid = response({"choices": [{"message": {"content": '{"action":"BAD","extra":1}'}, "finish_reason": "stop"}]})
        with patch("app.services.ai.httpx.post", side_effect=[invalid, invalid]):
            with self.assertRaises(StructuredCompletionError) as raised:
                AiClient(settings).complete_structured(
                    [AiMessage(role="user", content="test")],
                    response_model=SampleOutput,
                    schema_name="sample_v1",
                    options=StructuredCompletionOptions(temperature=0, max_tokens=100),
                )
        self.assertEqual(raised.exception.code, "STRUCTURED_OUTPUT_INVALID")
        self.assertEqual(raised.exception.repair_count, 1)

    def test_incomplete_finish_reason_is_never_accepted(self):
        settings = Settings(_env_file=None, ai_provider="openai")
        with patch("app.services.ai.httpx.post", return_value=response({
            "choices": [{"message": {"content": '{"action":"OK"}'}, "finish_reason": "length"}]
        })):
            with self.assertRaises(StructuredCompletionError) as raised:
                AiClient(settings).complete_structured(
                    [AiMessage(role="user", content="test")],
                    response_model=SampleOutput,
                    schema_name="sample_v1",
                    options=StructuredCompletionOptions(temperature=0, max_tokens=100),
                )
        self.assertEqual(raised.exception.code, "TURN_BUDGET_EXCEEDED")

    def test_unknown_provider_does_not_fall_back_to_text(self):
        with self.assertRaises(StructuredCompletionError) as raised:
            AiClient(Settings(_env_file=None, ai_provider="custom")).complete_structured(
                [AiMessage(role="user", content="test")],
                response_model=SampleOutput,
                schema_name="sample_v1",
                options=StructuredCompletionOptions(temperature=0, max_tokens=100),
            )
        self.assertEqual(raised.exception.code, "STRUCTURED_OUTPUT_UNSUPPORTED")


if __name__ == "__main__":
    unittest.main()
