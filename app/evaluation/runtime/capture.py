from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.turn_execution import GenerationOutcome


@dataclass
class InMemoryTurnExecutionObserver:
    route: dict[str, Any] = field(default_factory=dict)
    candidates: list[Any] = field(default_factory=list)
    usable: list[Any] = field(default_factory=list)
    generation: GenerationOutcome | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    max_events: int = 32
    dropped_event_count: int = 0

    def on_event(self, phase: str, status: str, **metadata: Any) -> None:
        if len(self.events) >= self.max_events:
            self.dropped_event_count += 1
            return
        allowed = {key: value for key, value in metadata.items() if key in {"elapsedMs", "remainingMs", "errorCode", "coverage"}}
        self.events.append({"phase": phase, "status": status, **allowed})

    def on_route(self, payload: dict) -> None:
        self.route = dict(payload)
        self.on_event("route", "COMPLETED")

    def on_retrieval(self, candidates: list, usable: list) -> None:
        self.candidates = list(candidates)
        self.usable = list(usable)
        self.on_event("retrieval", "COMPLETED")

    def on_generation(self, outcome: GenerationOutcome) -> None:
        self.generation = outcome
        self.on_event("generation", "COMPLETED" if outcome.completion_verified else "FAILED", errorCode=outcome.error_code)
