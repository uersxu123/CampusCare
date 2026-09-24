from __future__ import annotations

from app.evaluation.runtime.capture import InMemoryTurnExecutionObserver


def test_observer_events_are_bounded_and_unknown_values_are_not_filled() -> None:
    observer = InMemoryTurnExecutionObserver(max_events=3)
    for index in range(8):
        observer.on_event(
            "tool",
            "FAILED",
            errorCode=f"E{index}",
            coverage="PARTIAL",
            prompt="敏感正文不得进入事件",
        )

    assert len(observer.events) == 3
    assert observer.dropped_event_count == 5
    assert all("prompt" not in event for event in observer.events)
    assert all("cost" not in event for event in observer.events)


def test_observer_preserves_original_error_chain_without_duplicate_counting() -> None:
    observer = InMemoryTurnExecutionObserver(max_events=8)
    observer.on_event("agent", "FAILED", errorCode="MODEL_ROUND_BUDGET_EXCEEDED", coverage="FULL")
    observer.on_event("turn", "FAILED", errorCode="UPSTREAM_AGENT_FAILED", coverage="FULL")

    assert [item["errorCode"] for item in observer.events] == [
        "MODEL_ROUND_BUDGET_EXCEEDED",
        "UPSTREAM_AGENT_FAILED",
    ]
