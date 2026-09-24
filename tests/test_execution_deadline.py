from __future__ import annotations

from app.schemas.dtos import AiMessage
from app.services.agent_loop import AgentLoop
from app.services.execution_control import ExecutionBudget, bind_execution_budget


def test_expired_shared_budget_prevents_model_dispatch() -> None:
    now = [100.0]
    budget = ExecutionBudget.start(5.0, clock=lambda: now[0])
    now[0] = 105.0

    class Client:
        calls = 0

        def complete_with_tools(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("deadline 到期后不得 dispatch")

    client = Client()
    with bind_execution_budget(budget):
        result = AgentLoop(client=client, executor=object()).run(
            agent_name="test",
            messages=[AiMessage(role="user", content="合成输入")],
            tools=[],
        )

    assert result.stop_reason == "DEADLINE_EXCEEDED"
    assert result.model_rounds == 0
    assert client.calls == 0


def test_child_stage_cannot_reset_parent_budget() -> None:
    now = [20.0]
    parent = ExecutionBudget.start(10.0, clock=lambda: now[0])
    now[0] = 27.0
    with bind_execution_budget(parent):
        first_remaining = parent.remaining()
        now[0] = 29.5
        second_remaining = parent.remaining()
    assert first_remaining == 3.0
    assert second_remaining == 0.5
