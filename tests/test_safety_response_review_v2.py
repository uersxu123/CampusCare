from types import SimpleNamespace

from app.agents.autonomous import SafetyAgent
from app.agents.events import AgentArtifact, AgentEventType, AgentTask, CollaborationBlackboard
from app.schemas.dtos import AiMessage


class _FakeSafetyClient:
    def __init__(self, *, decision="APPROVE", violations=None, reason="ok", instructions=None):
        self.decision = decision
        self.violations = violations or []
        self.reason = reason
        self.instructions = instructions or []
        self.calls = []

    def complete_structured(self, messages, *, response_model, schema_name, options, purpose=None):
        self.calls.append({
            "messages": messages,
            "schema_name": schema_name,
            "purpose": purpose,
        })
        value = response_model(
            decision=self.decision,
            violations=self.violations,
            reason=self.reason,
            revisionInstructions=self.instructions,
        )
        return SimpleNamespace(value=value)


class _Registry:
    def __init__(self, client):
        self.client = client

    def client_for(self, _name):
        return self.client


def _agent(client):
    services = SimpleNamespace(
        model_registry=_Registry(client),
        settings=SimpleNamespace(
            agent_model_safety_max_tokens=512,
            agent_safety_review_timeout_seconds=20.0,
        ),
    )
    return SafetyAgent(services)


def _response():
    return AgentArtifact(
        id="response-1",
        owner="ResponseAgent",
        kind="response_proposal",
        payload={
            "messages": [
                AiMessage(role="system", content="safe response policy"),
                AiMessage(role="user", content='{"userInput":"普通校园问题"}'),
            ],
            "directResponse": "这是 ResponseAgent 已经生成、准备展示给用户的候选正文。",
            "mode": "specialist_synthesis",
        },
        confidence=0.9,
    )


def test_low_risk_response_is_actually_reviewed_by_safety_model():
    client = _FakeSafetyClient(decision="APPROVE", reason="未发现安全问题")
    agent = _agent(client)
    board = CollaborationBlackboard(turn_id="t1", user_input="宿舍报修怎么办")

    result = agent._review(AgentTask(id="review", title="review"), board, _response())

    assert len(client.calls) == 1
    assert client.calls[0]["schema_name"] == "safety_response_review_v1"
    artifact = result.artifacts[0]
    assert artifact.kind == "safety_review"
    assert artifact.payload["approved"] is True
    assert artifact.payload["decision"] == "APPROVE"
    assert artifact.payload["risk"] == "LOW"
    assert artifact.payload["degraded"] is False
    review_user_payload = client.calls[0]["messages"][1].content
    assert "这是 ResponseAgent 已经生成、准备展示给用户的候选正文。" in review_user_payload


def test_low_risk_response_can_be_rejected_for_safety_revision():
    client = _FakeSafetyClient(
        decision="REVISE",
        violations=["FRAUD_ASSISTANCE"],
        reason="包含伪造材料的可执行建议",
        instructions=["删除伪造材料的具体步骤，改为正规办理方式。"],
    )
    agent = _agent(client)
    board = CollaborationBlackboard(turn_id="t2", user_input="帮我处理一份校园材料")

    result = agent._review(AgentTask(id="review", title="review"), board, _response())

    artifact = result.artifacts[0]
    assert artifact.kind == "critique"
    assert artifact.payload["approved"] is False
    assert artifact.payload["decision"] == "REVISE"
    assert artifact.payload["violations"] == ["FRAUD_ASSISTANCE"]
    assert artifact.payload["revisionInstructions"]
    assert result.events[0].type == AgentEventType.REVISION_REQUESTED


def test_review_prompt_treats_proposal_content_as_untrusted_data():
    client = _FakeSafetyClient()
    agent = _agent(client)
    board = CollaborationBlackboard(turn_id="t3", user_input="普通问题")

    agent._review(AgentTask(id="review", title="review"), board, _response())

    system = client.calls[0]["messages"][0].content
    assert "LOW、MEDIUM 还是 HIGH" in system
    assert "prompt injection" in system
    assert "待审查数据" in system
    assert "directResponse" in system
    assert "候选最终正文" in system
