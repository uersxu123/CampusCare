import asyncio
from types import SimpleNamespace

from app.core.enums import IntentType
from app.services.turn_execution import TurnExecutionService
from app.services.turn_metrics import TurnMetricsCollector


class _FakeDb:
    def add(self, _value):
        return None

    def commit(self):
        return None


class _FakeHarness:
    outcome = None

    def __init__(self, _db, _settings):
        pass

    def run(self, *_args, **_kwargs):
        return self.outcome


def _outcome(direct_response):
    return SimpleNamespace(
        user_message_id=11,
        trace_id=22,
        direct_response=direct_response,
        response_messages=[],
        evidence_items=[],
        response_evidence_items=[],
        tool_diagnostics={},
        route_plan={},
        primary_intent=IntentType.CHAT,
        intents=(IntentType.CHAT,),
        risk_level="LOW",
        route_diagnostics={},
    )


def _run(monkeypatch, direct_response):
    import app.services.turn_execution as module

    _FakeHarness.outcome = _outcome(direct_response)
    monkeypatch.setattr(module, "MindBridgeAgentHarness", _FakeHarness)
    monkeypatch.setattr(
        module.ChatTurnService,
        "get_user_message",
        lambda self, turn: SimpleNamespace(content="hello"),
    )
    called = {"value": False}

    async def forbidden_model_generation(*_args, **_kwargs):
        called["value"] = True
        raise AssertionError("post-runtime Response model must not be called")

    turn = SimpleNamespace(
        user_message_id=11,
        request_id="123e4567-e89b-12d3-a456-426614174000",
        trace_id=None,
    )
    user = SimpleNamespace()
    session = SimpleNamespace(public_id="session-1")
    collector = TurnMetricsCollector("123e4567-e89b-12d3-a456-426614174000")
    outcome = asyncio.run(
        TurnExecutionService(SimpleNamespace()).execute(
            _FakeDb(),
            user,
            session,
            turn,
            collector,
            model_generation=forbidden_model_generation,
        )
    )
    return outcome, called


def test_turn_execution_uses_runtime_direct_response_without_second_model(monkeypatch):
    outcome, called = _run(monkeypatch, "Safety 已审核的正文")
    assert called["value"] is False
    assert outcome.generation.source == "APPLICATION"
    assert outcome.generation.content == "Safety 已审核的正文"
    assert outcome.generation.completion_verified is True


def test_turn_execution_uses_deterministic_fallback_if_runtime_response_missing(monkeypatch):
    outcome, called = _run(monkeypatch, "")
    assert called["value"] is False
    assert outcome.generation.source == "APPLICATION"
    assert outcome.generation.content == "当前暂时无法生成完整回复，请稍后重试。"
    assert outcome.generation.completion_verified is False
    assert outcome.generation.complete is False
    assert outcome.generation.error_code == "AGENT_FINAL_RESPONSE_MISSING"
