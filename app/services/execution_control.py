from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from typing import Callable


_CURRENT_BUDGET: ContextVar["ExecutionBudget | None"] = ContextVar("mindbridge_execution_budget", default=None)


@dataclass(frozen=True)
class ExecutionBudget:
    deadline_monotonic: float
    clock: Callable[[], float] = time.monotonic
    context_state: dict = field(default_factory=dict, compare=False)

    @classmethod
    def start(cls, seconds: float, *, clock: Callable[[], float] = time.monotonic) -> "ExecutionBudget":
        parent = _CURRENT_BUDGET.get()
        return cls(clock() + max(0.0, float(seconds)), clock,
                   parent.context_state if parent else {})

    def remaining(self) -> float:
        return max(0.0, self.deadline_monotonic - self.clock())

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def bounded_timeout(self, configured_seconds: float) -> float:
        return min(max(0.0, float(configured_seconds)), self.remaining())


def current_execution_budget() -> ExecutionBudget | None:
    return _CURRENT_BUDGET.get()


@contextmanager
def bind_execution_budget(budget: ExecutionBudget):
    token = _CURRENT_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _CURRENT_BUDGET.reset(token)


class ExecutionDeadlineExceeded(TimeoutError):
    code = "DEADLINE_EXCEEDED"


def remaining_timeout(configured_seconds: float = 60.0) -> float:
    budget = current_execution_budget()
    seconds = budget.bounded_timeout(configured_seconds) if budget else configured_seconds
    if seconds <= 0:
        raise ExecutionDeadlineExceeded("本轮剩余执行时间已耗尽")
    return seconds


def bounded_agent_execution(method):
    """A candidate and its FINALIZE share one local deadline within the turn."""
    @wraps(method)
    def run(self, *args, **kwargs):
        seconds = remaining_timeout(float(getattr(getattr(self.services, "settings", None), "agent_loop_deadline_seconds", 100.0)))
        parent = current_execution_budget()
        reserve = max(0.0, float(getattr(self, "execution_reserve_seconds", 0.0)))
        if parent is not None and reserve:
            seconds = min(seconds, max(0.0, parent.remaining() - reserve))
        with bind_execution_budget(ExecutionBudget.start(seconds)):
            return method(self, *args, **kwargs)
    return run
