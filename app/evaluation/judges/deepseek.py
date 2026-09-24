from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from pathlib import Path
from statistics import median

from pydantic import ValidationError

from app.core.config import Settings
from app.evaluation.config import EvaluationSettings
from app.evaluation.contracts import EndToEndCase, EvaluationRuntimeOutcome
from app.evaluation.judges.business import (
    BUSINESS_JUDGE_PROMPT_VERSION,
    BusinessJudgeOutput,
    business_judge_messages,
)
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient, StructuredCompletionError, StructuredCompletionOptions
from app.services.model_completion import ModelFinishReason
from app.services.context_builder import estimate_message_tokens, estimate_tokens


@dataclass(frozen=True)
class BusinessJudgeResult:
    output: BusinessJudgeOutput | None
    structured_output_mode: str
    repair_count: int
    judge_model: str
    judge_prompt_version: str
    error_code: str | None = None
    retry_count: int = 0

    @property
    def passed(self) -> bool:
        return self.error_code is None and self.output is not None and self.output.verdict == "PASS"


class DeepSeekJudge:
    def __init__(self, app_settings: Settings, eval_settings: EvaluationSettings):
        self.eval_settings = eval_settings
        self._context_tokens = app_settings.ollama_num_ctx
        self._think = app_settings.ai_think
        provider = eval_settings.judge_provider.lower()
        if provider not in {"openai", "ollama"}:
            raise ValueError(f"不支持的 Judge provider: {provider}")
        judge_settings = app_settings.model_copy(
            update={
                "ai_provider": provider,
                "openai_base_url": eval_settings.normalized_judge_base_url,
                "openai_api_key": eval_settings.judge_api_key.get_secret_value(),
                "openai_model": eval_settings.judge_model,
                "ai_temperature": eval_settings.judge_temperature,
                "ai_max_tokens": eval_settings.judge_max_tokens,
                "ollama_base_url": eval_settings.normalized_judge_base_url.removesuffix("/v1") if provider == "ollama" else app_settings.ollama_base_url,
                "ollama_model": eval_settings.judge_model if provider == "ollama" else app_settings.ollama_model,
            }
        )
        self.client = AiClient(judge_settings, purpose_namespace="evaluation.business_judge")

    def judge(self, case: EndToEndCase, outcome: EvaluationRuntimeOutcome) -> BusinessJudgeResult:
        if self.eval_settings.judge_provider != "ollama" and not self.eval_settings.judge_api_key.get_secret_value():
            return self._error("JUDGE_API_KEY_MISSING", "json_schema", 0)
        messages = business_judge_messages(case, outcome)
        input_estimate = sum(estimate_message_tokens(item) for item in messages)
        input_estimate += estimate_tokens(json.dumps(BusinessJudgeOutput.model_json_schema(), ensure_ascii=False))
        input_budget = min(28672, self._context_tokens - self.eval_settings.judge_max_tokens - 1024)
        if input_estimate > input_budget:
            return self._error("JUDGE_INPUT_BUDGET_EXCEEDED", "json_schema", 0)
        options = StructuredCompletionOptions(
            temperature=self.eval_settings.judge_temperature,
            max_tokens=self.eval_settings.judge_max_tokens,
            repair_attempts=0,
            timeout_seconds=self.eval_settings.judge_timeout_seconds,
        )
        results = [self._cached_judge(messages, options, repetition) for repetition in range(self.eval_settings.judge_repetitions)]
        failed = next((item for item in results if item.error_code or item.output is None), None)
        if failed is not None:
            return failed
        outputs = [item.output for item in results if item.output is not None]
        actions = {item.observed_action for item in outputs}
        if len(actions) != 1:
            return self._error("JUDGE_ACTION_DISAGREEMENT", "json_schema", 0)
        numeric_fields = (
            "relevance", "accuracy", "completeness", "helpfulness", "action_correctness",
            "source_consistency", "policy_grounding", "emotional_boundary", "safety",
        )
        payload = {}
        for field in numeric_fields:
            values = [getattr(item, field) for item in outputs if getattr(item, field) is not None]
            payload[field] = median(values) if values else None
        payload.update({
            "observed_action": outputs[0].observed_action,
            "verdict": "PASS" if all(item.verdict == "PASS" for item in outputs) else "FAIL",
            "reasons": list(dict.fromkeys(reason for item in outputs for reason in item.reasons))[:3],
            "unsupported_claims": list(dict.fromkeys(claim for item in outputs for claim in item.unsupported_claims))[:5],
        })
        modes = {item.structured_output_mode for item in results}
        return BusinessJudgeResult(
            output=BusinessJudgeOutput.model_validate(payload),
            structured_output_mode=next(iter(modes)) if len(modes) == 1 else "mixed",
            repair_count=sum(item.repair_count for item in results),
            retry_count=sum(item.retry_count for item in results),
            judge_model=self.eval_settings.judge_model,
            judge_prompt_version=BUSINESS_JUDGE_PROMPT_VERSION,
        )

    def _cached_judge(
        self,
        messages: list[AiMessage],
        options: StructuredCompletionOptions,
        repetition: int,
    ) -> BusinessJudgeResult:
        cache_path = self._cache_path(messages, repetition)
        if cache_path.is_file():
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                return BusinessJudgeResult(
                    output=BusinessJudgeOutput.model_validate(payload["output"]),
                    structured_output_mode=str(payload["structuredOutputMode"]),
                    repair_count=int(payload.get("repairCount", 0)),
                    retry_count=int(payload.get("retryCount", 0)),
                    judge_model=self.eval_settings.judge_model,
                    judge_prompt_version=BUSINESS_JUDGE_PROMPT_VERSION,
                )
            except (OSError, KeyError, ValueError, json.JSONDecodeError, ValidationError):
                pass
        result = self._judge_with_retries(messages, options)
        if result.output is not None and result.error_code is None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps({
                "schemaVersion": 1,
                "output": result.output.model_dump(),
                "structuredOutputMode": result.structured_output_mode,
                "repairCount": result.repair_count,
                "retryCount": result.retry_count,
            }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8", newline="\n")
        return result

    def _judge_with_retries(self, messages, options) -> BusinessJudgeResult:
        result = self._error("JUDGE_REQUEST_ERROR", "json_schema", 0)
        for retry in range(self.eval_settings.judge_max_retries + 1):
            result = self._judge_once(messages, options)
            if result.error_code not in {"JUDGE_REQUEST_ERROR", "PROVIDER_ERROR", "JUDGE_INCOMPLETE"}:
                return BusinessJudgeResult(**{**result.__dict__, "retry_count": retry})
        return BusinessJudgeResult(**{**result.__dict__, "retry_count": self.eval_settings.judge_max_retries})

    def _judge_once(self, messages, options) -> BusinessJudgeResult:
        try:
            completion = self.client.complete_structured(
                messages,
                response_model=BusinessJudgeOutput,
                schema_name=BUSINESS_JUDGE_PROMPT_VERSION,
                options=options,
            )
            if completion.metadata.finish_reason != ModelFinishReason.STOP:
                return self._error("JUDGE_INCOMPLETE", "json_schema", completion.repair_count)
            return BusinessJudgeResult(
                output=completion.value,
                structured_output_mode="json_schema",
                repair_count=completion.repair_count,
                judge_model=self.eval_settings.judge_model,
                judge_prompt_version=BUSINESS_JUDGE_PROMPT_VERSION,
            )
        except StructuredCompletionError as exc:
            if exc.metadata is not None and exc.metadata.finish_reason == ModelFinishReason.LENGTH:
                return self._error("JUDGE_OUTPUT_TRUNCATED", "json_schema", exc.repair_count)
            if exc.code not in {
                "PROVIDER_ERROR",
                "UNSUPPORTED_PROVIDER",
                "STRUCTURED_OUTPUT_UNSUPPORTED",
                "INVALID_JSON",
                "SCHEMA_VALIDATION",
            }:
                return self._error(exc.code, "json_schema", exc.repair_count)
        except Exception:
            return self._error("JUDGE_REQUEST_ERROR", "json_schema", 0)
        if self.eval_settings.judge_allow_prompt_fallback:
            return self._prompted_json(messages)
        return self._error("JUDGE_INVALID_STRUCTURED_OUTPUT", "json_schema", 0)

    def _cache_path(self, messages: list[AiMessage], repetition: int) -> Path:
        payload = {
            "model": self.eval_settings.judge_model,
            "provider": self.eval_settings.judge_provider,
            "endpoint": self.eval_settings.normalized_judge_base_url,
            "maxTokens": self.eval_settings.judge_max_tokens,
            "temperature": self.eval_settings.judge_temperature,
            "contextTokens": self._context_tokens if self.eval_settings.judge_provider == "ollama" else None,
            "think": self._think if self.eval_settings.judge_provider == "ollama" else None,
            "promptVersion": BUSINESS_JUDGE_PROMPT_VERSION,
            "allowPromptFallback": self.eval_settings.judge_allow_prompt_fallback,
            "metric": "business",
            "repetition": repetition,
            "messages": [item.model_dump() for item in messages],
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return self.eval_settings.resolve(self.eval_settings.output_dir) / "cache" / "judge" / f"{digest}.json"

    def _prompted_json(self, messages: list[AiMessage]) -> BusinessJudgeResult:
        schema = json.dumps(BusinessJudgeOutput.model_json_schema(), ensure_ascii=False)
        prompted = [
            messages[0],
            AiMessage(role="system", content=f"只输出一个符合以下 JSON Schema 的 JSON 对象，不要代码块：{schema}"),
            messages[1],
        ]
        repair_count = 0
        for attempt in range(1):
            try:
                completion = self.client.complete(prompted, purpose="evaluation.business_judge.prompted_json")
                payload = _single_json_object(completion.content)
                output = BusinessJudgeOutput.model_validate(payload)
                return BusinessJudgeResult(
                    output=output,
                    structured_output_mode="prompted_json",
                    repair_count=repair_count,
                    judge_model=self.eval_settings.judge_model,
                    judge_prompt_version=BUSINESS_JUDGE_PROMPT_VERSION,
                )
            except (ValueError, json.JSONDecodeError, ValidationError) as exc:
                return self._error("JUDGE_INVALID_STRUCTURED_OUTPUT", "prompted_json", repair_count)
            except Exception:
                return self._error("JUDGE_REQUEST_ERROR", "prompted_json", repair_count)
        return self._error("JUDGE_INVALID_STRUCTURED_OUTPUT", "prompted_json", repair_count)

    def _error(self, code: str, mode: str, repair_count: int) -> BusinessJudgeResult:
        return BusinessJudgeResult(
            output=None,
            structured_output_mode=mode,
            repair_count=repair_count,
            judge_model=self.eval_settings.judge_model,
            judge_prompt_version=BUSINESS_JUDGE_PROMPT_VERSION,
            error_code=code,
        )


def _single_json_object(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    decoder = json.JSONDecoder()
    payload, end = decoder.raw_decode(text)
    if not isinstance(payload, dict) or text[end:].strip():
        raise ValueError("Judge 必须只返回一个 JSON 对象")
    return payload
