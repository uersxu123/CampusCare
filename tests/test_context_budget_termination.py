from __future__ import annotations

import json
import multiprocessing

from app.schemas.dtos import AiMessage
from app.services.agent_loop import AgentLoop
from app.services.context_builder import _drop_largest_result


class _Unused:
    pass


def _exercise_fixed_points(queue) -> None:
    loop = AgentLoop(
        client=_Unused(),
        executor=_Unused(),
        input_max_tokens=256,
        output_max_tokens=1,
        input_safety_margin_tokens=0,
        model_context_tokens=256,
    )
    tool_message = AiMessage(role="tool", content="x" * 271, tool_call_id="call-1", name="tool")
    fit = loop._fit_request_budget([tool_message], 250)

    payload = {
        "specialistResults": [
            {"workItemId": "a", "content": "甲" * 311},
            {"workItemId": "b", "content": "b" * 2000},
        ]
    }
    dropped = []
    operations = 0
    while _drop_largest_result(payload, dropped):
        operations += 1
        if operations > 20:
            raise AssertionError("裁剪操作超出推导边界")
    queue.put({
        "fit": fit,
        "toolLength": len(tool_message.content),
        "toolMarkerCount": tool_message.content.count("[工具结果已按输入预算截断]"),
        "resultLengths": [len(item["content"]) for item in payload["specialistResults"]],
        "resultMarkerCounts": [item["content"].count("[已按输入预算截断]") for item in payload["specialistResults"]],
        "droppedCount": len(dropped),
        "operations": operations,
        "json": json.dumps(payload, ensure_ascii=False),
    })


def test_271_and_311_fixed_points_terminate_in_guarded_process() -> None:
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    process = context.Process(target=_exercise_fixed_points, args=(queue,))
    process.start()
    process.join(5)
    if process.is_alive():
        process.terminate()
        process.join(2)
        raise AssertionError("预算裁剪子进程在超时内未退出")

    assert process.exitcode == 0
    result = queue.get(timeout=1)
    assert result["fit"] is False
    assert result["toolLength"] == 271
    assert result["toolMarkerCount"] == 0
    assert result["resultLengths"] == [300, 300]
    assert result["resultMarkerCounts"] == [1, 1]
    assert result["droppedCount"] == 2
    assert result["operations"] <= 6
    assert "甲" in result["json"]


def test_agent_loop_budget_fit_does_not_mutate_caller_messages() -> None:
    class NeverCalledClient:
        def complete_with_tools(self, *_args, **_kwargs):
            raise AssertionError("预算溢出时不应调用模型")

    original = AiMessage(role="tool", content="中" * 271, tool_call_id="call-1", name="tool")
    loop = AgentLoop(
        client=NeverCalledClient(),
        executor=_Unused(),
        input_max_tokens=256,
        output_max_tokens=1,
        input_safety_margin_tokens=0,
        model_context_tokens=256,
    )

    result = loop.run(agent_name="test", messages=[original], tools=[])

    assert result.stop_reason == "INPUT_BUDGET_EXCEEDED"
    assert original.content == "中" * 271


def test_exhausted_result_is_not_removed_and_other_candidate_still_shrinks() -> None:
    payload = {
        "dependencyResults": [
            {"workItemId": "at-floor", "content": "a" * 300, "status": "PARTIAL"},
            {"workItemId": "shrinkable", "content": "中" * 1000, "status": "COMPLETED"},
        ]
    }
    dropped = []

    while _drop_largest_result(payload, dropped):
        pass

    assert [item["workItemId"] for item in payload["dependencyResults"]] == ["at-floor", "shrinkable"]
    assert [len(item["content"]) for item in payload["dependencyResults"]] == [300, 300]
    assert [item["status"] for item in payload["dependencyResults"]] == ["PARTIAL", "COMPLETED"]
    assert len(dropped) == 1


def test_small_and_mixed_inputs_keep_content_and_have_bounded_operations() -> None:
    payload = {
        "specialistResults": [
            {"workItemId": "small", "content": "无需裁剪 mixed 中文 text"},
            {"workItemId": "boundary", "content": "中A" * 151},
        ]
    }
    original_small = payload["specialistResults"][0]["content"]
    dropped = []
    operations = 0
    while _drop_largest_result(payload, dropped):
        operations += 1
        assert operations <= 4

    assert payload["specialistResults"][0]["content"] == original_small
    assert len(payload["specialistResults"][1]["content"]) == 300
    assert payload["specialistResults"][1]["content"].count("[已按输入预算截断]") == 1
