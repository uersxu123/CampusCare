from types import SimpleNamespace

from app.agents.coordinator import EventDrivenCoordinator
from app.agents.events import AgentArtifact, CollaborationBlackboard
from app.agents.registry import AgentRegistry


def _coordinator(max_revisions=1):
    settings = SimpleNamespace(
        agent_max_rounds=12,
        agent_max_rounds_hard_limit=16,
        agent_max_claims_per_round=4,
        agent_max_claims_per_agent=4,
        agent_final_acceptance_min_confidence=0.6,
        agent_max_response_revisions=max_revisions,
    )
    return EventDrivenCoordinator(
        AgentRegistry([]),
        SimpleNamespace(name="CoordinatorAgent"),
        settings,
    )


def _response(response_id: str, text: str = "候选正文") -> AgentArtifact:
    return AgentArtifact(
        id=response_id,
        owner="ResponseAgent",
        kind="response_proposal",
        payload={
            "messages": [],
            "directResponse": text,
            "mode": "specialist_synthesis",
            "contextManifest": {"sources": []},
            "promptEvidence": [],
        },
        confidence=0.86,
    )


def _critique(critique_id: str, response_id: str) -> AgentArtifact:
    return AgentArtifact(
        id=critique_id,
        owner="SafetyAgent",
        kind="critique",
        payload={
            "approved": False,
            "decision": "REVISE",
            "violations": ["TEST"],
            "reason": "needs revision",
            "revisionInstructions": ["remove unsafe detail"],
            "responseArtifactId": response_id,
        },
        confidence=0.95,
        metadata={"responseArtifactId": response_id},
    )


def _risk(level="LOW") -> AgentArtifact:
    return AgentArtifact(
        id=f"risk-{level.lower()}",
        owner="SafetyAgent",
        kind="risk",
        payload={"risk": level},
        confidence=1.0,
    )


def test_first_safety_revise_schedules_exactly_one_response_revision():
    coordinator = _coordinator(max_revisions=1)
    board = (
        CollaborationBlackboard(turn_id="t1", user_input="普通问题")
        .add_artifact(_risk())
        .add_artifact(_response("response-v1"))
        .add_artifact(_critique("critique-v1", "response-v1"))
    )

    board = coordinator._ensure_response_review_and_revision(board)

    revision = board.tasks.get("task:revise-response:critique-v1")
    assert revision is not None
    assert revision.metadata["revisionOf"] == "response-v1"
    assert coordinator._response_revision_exhausted(board) is False


def test_second_safety_revise_does_not_create_third_response_or_runtime_fallback():
    coordinator = _coordinator(max_revisions=1)
    board = (
        CollaborationBlackboard(turn_id="t2", user_input="普通问题")
        .add_artifact(_risk())
        .add_artifact(_response("response-v1"))
        .add_artifact(_critique("critique-v1", "response-v1"))
        .add_artifact(_response("response-v2", "修订后的正文仍然有问题"))
        .add_artifact(_critique("critique-v2", "response-v2"))
    )

    board = coordinator._ensure_response_review_and_revision(board)

    assert "task:revise-response:critique-v2" not in board.tasks
    assert coordinator._response_revision_exhausted(board) is True
    assert board.latest_artifact("response_proposal").id == "response-v2"
    assert board.latest_artifact("response_proposal").payload["mode"] == "specialist_synthesis"
    assert not any(
        item.payload.get("mode") == "safety_terminal_fallback"
        for item in board.artifacts_by_kind("response_proposal")
    )
    assert board.final_artifact_id == ""
    assert board.accepted_artifact() is None


def test_coordinator_run_returns_immediately_after_second_safety_reject():
    coordinator = _coordinator(max_revisions=1)
    board = (
        CollaborationBlackboard(turn_id="t3", user_input="普通问题")
        .add_artifact(_risk())
        .add_artifact(_response("response-v1"))
        .add_artifact(_critique("critique-v1", "response-v1"))
        .add_artifact(_response("response-v2"))
        .add_artifact(_critique("critique-v2", "response-v2"))
    )

    # Isolate the run-loop terminal condition from unrelated task derivation.
    coordinator._ensure_root_task = lambda current: current
    coordinator._derive_missing_work = lambda current: current

    def must_not_reach_accept(_board):
        raise AssertionError("second Safety reject must return before final acceptance")

    coordinator._try_accept_final = must_not_reach_accept

    result = coordinator.run(board)

    assert result.final_artifact_id == ""
    assert result.accepted_artifact() is None
    assert result.latest_artifact("response_proposal").id == "response-v2"


def test_candidate_text_is_not_an_answer_until_matching_safety_approval_and_accept_final():
    coordinator = _coordinator(max_revisions=1)
    board = (
        CollaborationBlackboard(turn_id="t4", user_input="普通问题")
        .add_artifact(_risk())
        .add_artifact(_response("response-v1", "已经有正文，但还没审核"))
    )

    board = coordinator._try_accept_final(board)

    assert board.latest_artifact("response_proposal").payload["directResponse"]
    assert board.final_artifact_id == ""
    assert board.accepted_artifact() is None


def test_matching_safety_approval_marks_current_candidate_as_final_answer():
    coordinator = _coordinator(max_revisions=1)
    board = (
        CollaborationBlackboard(turn_id="t5", user_input="普通问题")
        .add_artifact(_risk())
        .add_artifact(_response("response-v2", "这一版已经通过安全审查"))
        .add_artifact(AgentArtifact(
            id="review-v2",
            owner="SafetyAgent",
            kind="safety_review",
            payload={"approved": True, "decision": "APPROVE", "responseArtifactId": "response-v2"},
            confidence=1.0,
            metadata={"responseArtifactId": "response-v2"},
        ))
    )

    board = coordinator._try_accept_final(board)

    accepted = board.accepted_artifact()
    assert board.final_artifact_id == "response-v2"
    assert accepted is not None
    assert accepted.id == "response-v2"
    assert accepted.payload["directResponse"] == "这一版已经通过安全审查"


def test_old_safety_approval_cannot_accept_a_newer_candidate():
    coordinator = _coordinator(max_revisions=1)
    board = (
        CollaborationBlackboard(turn_id="t6", user_input="普通问题")
        .add_artifact(_risk())
        .add_artifact(_response("response-v1"))
        .add_artifact(AgentArtifact(
            id="review-v1",
            owner="SafetyAgent",
            kind="safety_review",
            payload={"approved": True, "decision": "APPROVE", "responseArtifactId": "response-v1"},
            confidence=1.0,
            metadata={"responseArtifactId": "response-v1"},
        ))
        .add_artifact(_response("response-v2", "新的正文"))
    )

    board = coordinator._try_accept_final(board)

    assert board.final_artifact_id == ""
    assert board.accepted_artifact() is None
