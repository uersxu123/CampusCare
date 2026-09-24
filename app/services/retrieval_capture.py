from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


class RetrievalCapture(list):
    observed = False
    incomplete = False

    @property
    def status(self) -> str:
        if self.incomplete:
            return "PARTIAL" if self.observed else "NOT_OBSERVED"
        return ("OBSERVED" if self else "OBSERVED_EMPTY") if self.observed else "NOT_OBSERVED"


_captured_retrieval: ContextVar[RetrievalCapture | None] = ContextVar("captured_retrieval", default=None)


@contextmanager
def capture_retrieval_candidates() -> Iterator[RetrievalCapture]:
    rows = RetrievalCapture()
    token = _captured_retrieval.set(rows)
    try:
        yield rows
    finally:
        _captured_retrieval.reset(token)


def record_retrieval_candidates(candidates: list) -> None:
    rows = _captured_retrieval.get()
    if rows is not None:
        rows.observed = True
        rows.extend(candidates)


def merge_retrieval_telemetry(telemetry: dict) -> None:
    rows = _captured_retrieval.get()
    if rows is None:
        return
    observation = telemetry.get("retrieval") or {}
    candidates = observation.get("candidates")
    if observation.get("status") in {"OBSERVED", "OBSERVED_EMPTY"} and isinstance(candidates, list):
        rows.observed = True
        rows.extend(dict(item) for item in candidates if isinstance(item, dict))
    else:
        rows.incomplete = True


def retrieval_telemetry(rows: RetrievalCapture) -> dict:
    candidates = {}
    for item in rows:
        if isinstance(item, dict):
            row = dict(item)
        else:
            chunk_id = getattr(item, "chunk_id", None)
            row = {"chunkId": chunk_id, "content": getattr(item, "content", ""),
                   "contextId": f"knowledge:{chunk_id}" if chunk_id is not None else ""}
        key = row.get("contextId") or (row.get("chunkId"), row.get("content"))
        candidates.setdefault(key, row)
    return {"status": rows.status, "candidates": list(candidates.values())}
