from __future__ import annotations

from app.models.entities import KnowledgeDocument


TRANSITIONS = {
    "PENDING": {"PARSING", "FAILED"},
    "PARSING": {"PARSED", "FAILED"},
    "PARSED": {"CHUNKED", "FAILED"},
    "CHUNKED": {"READY", "FAILED"},
    "READY": {"PARSING", "FAILED"},
    "FAILED": {"PARSING"},
}


def transition_ingestion(document: KnowledgeDocument, target: str) -> None:
    current = document.ingestion_status or "PENDING"
    if target == current:
        return
    if target not in TRANSITIONS.get(current, set()):
        raise ValueError(f"非法知识处理状态转换：{current} -> {target}")
    document.ingestion_status = target
