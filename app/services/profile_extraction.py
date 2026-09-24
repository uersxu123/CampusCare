from __future__ import annotations

import re
from dataclasses import dataclass

from app.models.entities import ChatMessage
from app.services.user_memory import SENSITIVE_PATTERNS, TEMPORARY_EMOTION_TERMS, derive_memory_key


@dataclass(frozen=True)
class ProfileCandidate:
    category: str
    memory_key: str
    content: str
    confidence: float
    source_message_id: int
    sensitivity: str = "NORMAL"
    visibility_scope: str = "ALL_AGENTS"


class ProfileExtractionService:
    """Conservative incremental extractor for stable user-authored facts.

    This deterministic extractor is the recovery path and default local
    implementation. A model extractor can be injected later, but its output must
    pass the same validation before it becomes ACTIVE.
    """

    def extract(self, messages: list[ChatMessage]) -> list[ProfileCandidate]:
        candidates: dict[str, ProfileCandidate] = {}
        for message in messages:
            if message.role.upper() != "USER":
                continue
            for candidate in self._from_text(message.id, message.content):
                previous = candidates.get(candidate.memory_key)
                if previous is None or candidate.source_message_id >= previous.source_message_id:
                    candidates[candidate.memory_key] = candidate
        return list(candidates.values())

    def _from_text(self, message_id: int, text: str) -> list[ProfileCandidate]:
        normalized = " ".join((text or "").split())
        if not normalized or self._forbidden(normalized):
            return []
        rows: list[tuple[str, str]] = []
        for label, pattern in (
            ("PROFILE", r"我的专业是(?P<value>[^，。！？；\n]{2,40})"),
            ("PROFILE", r"我(?:在|属于)(?P<value>[^，。！？；\n]{2,40}(?:校区|学院))"),
            ("LEARNING_PREFERENCE", r"我(?:喜欢|偏好|习惯)(?P<value>[^，。！？；\n]{2,80})"),
            ("LONG_TERM_CONSTRAINT", r"我每周(?P<value>[^。！？；\n]{2,100})"),
            ("LONG_TERM_CONSTRAINT", r"我(?:只能|通常只能)(?P<value>[^。！？；\n]{2,100})"),
        ):
            match = re.search(pattern, normalized)
            if match:
                value = match.group("value").strip("，,。 ")
                if value:
                    rows.append((label, value))
        result = []
        for category, value in rows:
            content = value if category == "PROFILE" else normalized[:300]
            key = derive_memory_key(category, normalized)
            result.append(ProfileCandidate(
                category=category,
                memory_key=key,
                content=content,
                confidence=0.9,
                source_message_id=message_id,
            ))
        return result

    @staticmethod
    def _forbidden(content: str) -> bool:
        return any(re.search(pattern, content, re.IGNORECASE) for pattern in SENSITIVE_PATTERNS) or any(
            term in content for term in TEMPORARY_EMOTION_TERMS
        )
