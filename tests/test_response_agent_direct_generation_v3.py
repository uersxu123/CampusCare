from types import SimpleNamespace

from app.agents.autonomous import ResponseAgent
from app.agents.events import AgentTask, CollaborationBlackboard
from app.services.model_completion import (
    ModelCompletion,
    ModelCompletionMetadata,
    ModelFinishReason,
    ModelUsage,
)


def _metadata(reason=ModelFinishReason.STOP):
    return ModelCompletionMetadata(
        provider="mock",
        model="qwen3:8b",
        finish_reason=reason,
        semantic_finish_seen=reason == ModelFinishReason.STOP,
        transport_terminal_seen=True,
        terminal_signal="done",
        provider_finish_reason=reason.value,
        configured_output_limit=1536,
        usage=ModelUsage(prompt_tokens=10, output_tokens=10),
    )


class _FakeResponseClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def complete(self, messages, *, purpose=None):
        self.calls.append({"messages": messages, "purpose": purpose})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Registry:
    def __init__(self, client):
        self.client = client

    def client_for(self, _name):
        return self.client


def _agent(client):
    manifest = SimpleNamespace(as_dict=lambda: {"sources": []})
    services = SimpleNamespace(
        model_registry=_Registry(client),
        context_packet=SimpleNamespace(manifest=manifest),
    )
    return ResponseAgent(services)


def _task():
    return AgentTask(id="response-task", title="respond", metadata={"kind": "response"})


def test_response_agent_generates_direct_response_inside_runtime():
    client = _FakeResponseClient([
        ModelCompletion(content="这是已经生成的最终候选正文。", metadata=_metadata())
    ])
    result = _agent(client).act(_task(), CollaborationBlackboard(turn_id="t1", user_input="你好"))

    artifact = result.artifacts[0]
    assert artifact.kind == "response_proposal"
    assert artifact.payload["directResponse"] == "这是已经生成的最终候选正文。"
    assert artifact.payload["generationDiagnostics"]["fallbackUsed"] is False
    assert len(client.calls) == 1
    assert client.calls[0]["purpose"] == "response.generate.runtime.attempt1"


def test_response_agent_retries_once_then_succeeds():
    client = _FakeResponseClient([
        RuntimeError("temporary failure"),
        ModelCompletion(content="第二次成功。", metadata=_metadata()),
    ])
    result = _agent(client).act(_task(), CollaborationBlackboard(turn_id="t2", user_input="你好"))

    artifact = result.artifacts[0]
    assert artifact.payload["directResponse"] == "第二次成功。"
    assert artifact.payload["generationDiagnostics"]["attempts"] == 2
    assert artifact.payload["generationDiagnostics"]["fallbackUsed"] is False
    assert len(client.calls) == 2


def test_response_agent_uses_deterministic_fallback_after_two_failures():
    client = _FakeResponseClient([RuntimeError("down"), RuntimeError("still down")])
    result = _agent(client).act(_task(), CollaborationBlackboard(turn_id="t3", user_input="你好"))

    artifact = result.artifacts[0]
    assert artifact.payload["directResponse"] == ResponseAgent.NORMAL_FALLBACK
    assert artifact.payload["generationDiagnostics"]["fallbackUsed"] is True
    assert artifact.confidence == 0.7
    assert len(client.calls) == 2


def test_non_stop_completion_is_not_exposed_and_falls_back():
    client = _FakeResponseClient([
        ModelCompletion(content="被截断的候选", metadata=_metadata(ModelFinishReason.LENGTH)),
        ModelCompletion(content="仍然被截断", metadata=_metadata(ModelFinishReason.LENGTH)),
    ])
    result = _agent(client).act(_task(), CollaborationBlackboard(turn_id="t4", user_input="你好"))

    artifact = result.artifacts[0]
    assert artifact.payload["directResponse"] == ResponseAgent.NORMAL_FALLBACK
    assert artifact.payload["generationDiagnostics"]["finishReason"] == "LENGTH"
    assert artifact.payload["generationDiagnostics"]["fallbackUsed"] is True



def test_runtime_exports_only_final_accepted_response_source_contract():
    from pathlib import Path

    source = Path("app/agents/event_driven_runtime.py").read_text(encoding="utf-8")
    assert "accepted = board.accepted_artifact()" in source
    assert 'board.accepted_artifact() or board.latest_artifact("response_proposal")' not in source
