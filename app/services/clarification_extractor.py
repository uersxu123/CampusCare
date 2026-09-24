from __future__ import annotations

import json
import re
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.dtos import AiMessage
from app.services.ai import AiClient, StructuredCompletionOptions
from app.services.clarification_models import SlotExtraction


class ExtractedSlot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: Literal["course", "deadline", "availableTimeWindows", "focusProblem"]
    value: str = Field(min_length=1, max_length=200)
    confidence: float = Field(ge=0.0, le=1.0)


class SlotExtractionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slots: list[ExtractedSlot] = Field(default_factory=list, max_length=4)


class StructuredClarificationExtractor:
    def __init__(self, settings):
        self.client = AiClient(settings)

    def extract(
        self,
        text: str,
        expected_fields: list[str],
        validator: Callable[[str, str], bool],
    ) -> SlotExtraction:
        completion = self.client.complete_structured(
            [
                AiMessage(
                    role="system",
                    content=(
                        "你只从用户原文提取学习计划字段。只返回字段值和置信度；不得补造、改写或推断用户未说的值。"
                        "允许字段：course、deadline、availableTimeWindows、focusProblem。"
                    ),
                ),
                AiMessage(
                    role="user",
                    content=json.dumps(
                        {"text": text, "expectedFields": expected_fields},
                        ensure_ascii=False,
                    ),
                ),
            ],
            response_model=SlotExtractionOutput,
            schema_name="clarification_slot_extraction_v1",
            options=StructuredCompletionOptions(
                temperature=0.0,
                max_tokens=256,
                repair_attempts=0,
                timeout_seconds=5.0,
            ),
        )
        expected = set(expected_fields)
        normalized_source = _compact(text)
        values: dict[str, str] = {}
        invalid: dict[str, str] = {}
        for slot in completion.value.slots:
            if slot.field not in expected or slot.confidence < 0.65:
                continue
            value = slot.value.strip()
            if _compact(value) not in normalized_source or not validator(slot.field, value):
                invalid[slot.field] = value
                continue
            values[slot.field] = value
        unresolved = tuple(field for field in expected_fields if field not in values)
        return SlotExtraction(values, invalid, unresolved, "LLM")


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value).replace("：", ":")
