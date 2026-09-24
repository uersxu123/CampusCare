import pytest
from types import SimpleNamespace

from app.core.config import Settings
from app.evaluation.contracts import EndToEndCase
from app.evaluation.runtime.adapter import (
    EvaluationRuntimeAdapter,
    _usable_evidence_from_execution,
)


@pytest.mark.asyncio
async def test_runtime_adapter_uses_real_turn_execution_and_shares_only_case_session():
    settings = Settings(
        _env_file=None,
        ai_provider="mock",
        agent_model_specialist_provider="mock",
        chat_tools_enabled=False,
        knowledge_vector_enabled=False,
        tool_queue_enabled=False,
        redis_socket_timeout_seconds=0.01,
    )
    case = EndToEndCase.model_validate({
        "id": "runtime-chat",
        "turns": ["你好", "谢谢"],
        "expected_action": "ANSWER",
        "reference": "自然回应即可。",
    })
    outcomes = await EvaluationRuntimeAdapter(settings, require_hybrid_retrieval=False).run_case(case)
    assert len(outcomes) == 2
    assert all(item.response and item.error_code is None for item in outcomes)
    assert all(item.route["primaryIntent"] == "CHAT" for item in outcomes)
    assert outcomes[-1].turn_metrics["status"] == "COMPLETED"


def test_adapter_uses_execution_usable_evidence_as_single_source_of_truth():
    execution_item = SimpleNamespace(chunk_id=6311, content="已发布证据")
    observed_item = SimpleNamespace(chunk_id=9999, content="观察副本")
    execution = SimpleNamespace(usable_evidence=(execution_item,))
    observer = SimpleNamespace(usable=[observed_item])

    usable = _usable_evidence_from_execution(execution, observer)

    assert usable == [execution_item]


def test_evaluation_settings_require_hybrid_retrieval_defaults_true():
    from app.evaluation.config import EvaluationSettings

    assert EvaluationSettings(_env_file=None).require_hybrid_retrieval is True
