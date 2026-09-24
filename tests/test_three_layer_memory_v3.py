from __future__ import annotations

import json
import tempfile
from types import SimpleNamespace
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import Base
from app.agents.autonomous import (
    AcademicPlanningAgent,
    CampusAffairsAgent,
    GeneralChatAgent,
    PsychologicalSupportAgent,
    ResponseAgent,
)
from app.agents.events import AgentTask, CollaborationBlackboard
from app.models.entities import (
    AgentRunTrace,
    ChatMessage,
    ChatSession,
    ChatTurn,
    ConversationEpisode,
    ConversationSummary,
    MemoryJob,
    UserAccount,
    UserMemory,
)
from app.services.context_builder import ContextBuilder
from app.services.chat import ChatService
from app.services.model_completion import ModelCompletion, ModelCompletionMetadata, ModelFinishReason, ModelUsage
from app.services.tool_models import AgentLoopResult
from app.services.memory import MemoryMessage
from app.services.turn_execution import GenerationOutcome
from app.services.memory_jobs import (
    COMPACT_SESSION,
    INDEX_PROFILE,
    PENDING,
    RUNNING,
    SUCCEEDED,
    UPDATE_PROFILE,
    MemoryJobService,
    MemoryWorker,
    utcnow,
)
from app.services.memory_retrieval import MemoryRetrievalService
from app.services.memory_vector_store import MemoryVectorStore
from app.services.user_memory import UserMemoryService


class UnavailableCache:
    client = None

    def load_recent_records_with_status(self, _session_id, **_kwargs):
        return [], "unavailable"

    def merge_records(self, *_args, **_kwargs):
        return False


class RecordingVectorStore:
    def __init__(self):
        self.episodes = []
        self.profiles = []
        self.deleted = []

    def upsert_episode(self, episode):
        self.episodes.append((episode.public_id, episode.version))

    def upsert_profile(self, memory):
        self.profiles.append((memory.public_id, memory.version, memory.memory_epoch))

    def delete_episode(self, public_id):
        self.deleted.append(("episode", public_id))

    def delete_profile(self, user_id, public_id):
        self.deleted.append(("profile", user_id, public_id))


class FailingQueryVectorStore:
    def query_episodes(self, **_kwargs):
        raise TimeoutError("embedding timeout")

    def query_profiles(self, **_kwargs):
        raise TimeoutError("embedding timeout")


class DeterministicEmbedding:
    name = "test"
    model = "deterministic-v1"

    def available(self):
        return True

    def embed_documents(self, texts):
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text):
        return [float(text.count("清单")), float(text.count("南望山")), float(len(text) % 17)]

    def model_digest(self):
        return "test-digest"


@pytest.fixture()
def database():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = factory()
    try:
        yield db, factory
    finally:
        db.close()
        engine.dispose()


def settings(**updates):
    values = {
        "_env_file": None,
        "ai_provider": "mock",
        "knowledge_vector_enabled": False,
        "memory_embedding_provider": "disabled",
        "memory_chroma_mode": "disabled",
        "memory_worker_enabled": False,
        "context_recent_message_limit": 2,
        "memory_compaction_min_messages": 1,
        "memory_compaction_max_delta_tokens": 1,
        "memory_worker_retry_base_seconds": 0.01,
    }
    values.update(updates)
    return Settings(**values)


def add_user_session(db: Session, username: str, public_id: str):
    user = UserAccount(username=username, display_name=username, password_hash="x")
    db.add(user)
    db.flush()
    session = ChatSession(public_id=public_id, title=public_id, user_id=user.id)
    db.add(session)
    db.flush()
    return user, session


def add_message(db: Session, user, session, role: str, content: str):
    row = ChatMessage(user_id=user.id, session_id=session.id, role=role, content=content)
    db.add(row)
    db.flush()
    return row


def test_packet_contains_four_memory_blocks_with_bridge_and_user_isolation(database, monkeypatch):
    db, _factory = database
    user, current_session = add_user_session(db, "u1", "current")
    _other, other_user_session = add_user_session(db, "u2", "foreign")
    previous_session = ChatSession(public_id="previous", title="previous", user_id=user.id)
    db.add(previous_session)
    db.flush()

    covered = add_message(db, user, current_session, "USER", "已经覆盖")
    bridge = add_message(db, user, current_session, "ASSISTANT", "中间约束：每天两小时")
    add_message(db, user, current_session, "USER", "最近一")
    add_message(db, user, current_session, "ASSISTANT", "最近二")
    current = add_message(db, user, current_session, "USER", "请继续给我清单计划")
    db.add(ConversationSummary(
        session_id=current_session.id,
        version=2,
        covered_until_message_id=covered.id,
        summary_json=json.dumps({"current_goal": "准备考试"}, ensure_ascii=False),
    ))
    source = add_message(db, user, previous_session, "USER", "我偏好清单")
    db.add(ConversationEpisode(
        public_id="episode-own",
        user_id=user.id,
        session_id=previous_session.id,
        source_start_message_id=source.id,
        source_end_message_id=source.id,
        summary_text="用户过去偏好清单式复习",
        facts_json="{}",
        content_hash="own",
        status="ACTIVE",
        index_status="PENDING",
        embedding_version="test",
    ))
    foreign_source = add_message(db, _other, other_user_session, "USER", "其他用户秘密")
    db.add(ConversationEpisode(
        public_id="episode-foreign",
        user_id=_other.id,
        session_id=other_user_session.id,
        source_start_message_id=foreign_source.id,
        source_end_message_id=foreign_source.id,
        summary_text="其他用户也喜欢清单",
        facts_json="{}",
        content_hash="foreign",
        status="ACTIVE",
        index_status="PENDING",
        embedding_version="test",
    ))
    db.add(UserMemory(
        public_id="profile-own",
        user_id=user.id,
        category="LEARNING_PREFERENCE",
        memory_key="LEARNING_PREFERENCE:learning_format",
        content="偏好清单式学习计划",
        confidence=1.0,
        status="ACTIVE",
        origin="EXPLICIT",
    ))
    db.commit()

    builder = ContextBuilder(db, settings(), UnavailableCache())
    packet = builder.build_base_context(
        user=user,
        session=current_session,
        current_message=current,
        model_input=current.content,
    )
    base = builder.render_base_memory(packet, "ACADEMIC")

    assert packet.packet_version == 3
    assert [item.id for item in packet.bridge_messages] == [bridge.id]
    assert [item.public_id for item in packet.relevant_history] == ["episode-own"]
    assert [item.public_id for item in packet.user_profile] == ["profile-own"]
    assert set(base) >= {"workingMemory", "conversationSummary", "relevantHistory", "userProfile"}
    assert "其他用户秘密" not in json.dumps(base, ensure_ascii=False)
    assert packet.current_message_id not in [item["id"] for item in base["workingMemory"]]

    specialist = builder.build_specialist_prompt(
        packet=packet,
        audience="ACADEMIC",
        system="专业系统提示",
        work_item={"intent": "ACADEMIC", "sourceText": current.content, "objective": "制定计划"},
        dependency_results=[],
        tool_schemas=[],
    )
    synthesis = builder.build_synthesis_prompt(
        packet=packet,
        system="最终合成",
        route_plan={"workItems": [{"sourceText": current.content}]},
        specialist_results=[{"workItemId": "w1", "answerBrief": "按清单执行"}],
    )
    assert packet.snapshot_id in specialist.messages[-1].content
    assert packet.snapshot_id in synthesis.messages[-1].content
    assert sum(message.content.count(current.content) for message in specialist.messages) == 1
    assert sum(message.content.count(current.content) for message in synthesis.messages) == 1
    assert specialist.manifest.estimated_total_tokens <= specialist.manifest.budget_tokens
    assert synthesis.manifest.estimated_total_tokens <= synthesis.manifest.budget_tokens

    captured_specialist_messages = []

    def fake_loop_run(_self, *, agent_name, messages, tools, finalize=None, accept_final=None):
        captured_specialist_messages.append((agent_name, messages))
        return AgentLoopResult("完成", (), 1, "COMPLETED")

    monkeypatch.setattr("app.agents.autonomous.AgentLoop.run", fake_loop_run)
    runtime = SimpleNamespace(
        registry=SimpleNamespace(definitions_for_agent=lambda _name: []),
        executor=SimpleNamespace(),
    )
    registry = SimpleNamespace(client_for=lambda _name: SimpleNamespace())
    services = SimpleNamespace(
        context_builder=builder,
        context_packet=packet,
        settings=settings(),
        tool_runtime=runtime,
        skill_manager=None,
        model_registry=registry,
        tool_call_details={},
    )
    for agent_class, intent in (
        (GeneralChatAgent, "CHAT"),
        (AcademicPlanningAgent, "ACADEMIC"),
        (CampusAffairsAgent, "CAMPUS"),
        (PsychologicalSupportAgent, "MENTAL"),
    ):
        agent_class(services)._run_loop(
            {"workItemId": intent, "intent": intent, "objective": "处理", "sourceText": current.content},
            [],
        )
    assert len(captured_specialist_messages) == 4
    for _agent_name, messages in captured_specialist_messages:
        payload = json.loads(messages[-1].content)
        assert payload["snapshotId"] == packet.snapshot_id
        assert set(payload["baseMemory"]) >= {
            "workingMemory", "conversationSummary", "relevantHistory", "userProfile",
        }

    class ResponseClient:
        def __init__(self):
            self.messages = None

        def complete(self, messages, *, purpose=None):
            self.messages = messages
            return ModelCompletion(
                content="最终回答",
                metadata=ModelCompletionMetadata(
                    provider="mock",
                    model="mock",
                    finish_reason=ModelFinishReason.STOP,
                    semantic_finish_seen=True,
                    transport_terminal_seen=True,
                    terminal_signal="done",
                    provider_finish_reason="stop",
                    configured_output_limit=512,
                    usage=ModelUsage(prompt_tokens=100, output_tokens=10),
                ),
            )

    response_client = ResponseClient()
    response_services = SimpleNamespace(
        context_builder=builder,
        context_packet=packet,
        model_registry=SimpleNamespace(client_for=lambda _name: response_client),
    )
    result = ResponseAgent(response_services).act(
        AgentTask(id="response", title="response", metadata={"kind": "response"}),
        CollaborationBlackboard(turn_id="turn", user_input=current.content),
    )
    response_payload = json.loads(response_client.messages[-1].content)
    assert response_payload["snapshotId"] == packet.snapshot_id
    assert result.artifacts[0].payload["contextManifest"]["audience"] == "RESPONSE"


def test_incremental_compaction_multiple_cycles_and_short_session_finalize(database):
    db, factory = database
    user, session = add_user_session(db, "compact", "compact-session")
    rows = []
    for index in range(3):
        rows.append(add_message(db, user, session, "USER", f"第{index}轮目标是复习数学"))
        rows.append(add_message(db, user, session, "ASSISTANT", f"第{index}轮建议"))
    db.commit()
    config = settings()
    vectors = RecordingVectorStore()
    worker = MemoryWorker(config, session_factory=factory, vector_store_factory=lambda _settings: vectors, worker_id="w1")
    MemoryJobService(db, config).enqueue(
        kind=COMPACT_SESSION,
        dedupe_key="compact:1",
        user_id=user.id,
        session_id=session.id,
        target_message_id=rows[-1].id,
    )
    db.commit()
    assert worker.run_once()
    db.expire_all()
    summary = db.query(ConversationSummary).filter_by(session_id=session.id).one()
    assert summary.covered_until_message_id == rows[-3].id
    first_version = summary.version
    assert db.query(ConversationEpisode).filter_by(session_id=session.id, is_tail=False).count() == 1

    more_user = add_message(db, user, session, "USER", "新的约束是每天两小时")
    more_assistant = add_message(db, user, session, "ASSISTANT", "已更新")
    db.commit()
    MemoryJobService(db, config).enqueue(
        kind=COMPACT_SESSION,
        dedupe_key="compact:2",
        user_id=user.id,
        session_id=session.id,
        target_message_id=more_assistant.id,
    )
    db.commit()
    while worker.run_once():
        db.expire_all()
        compact_job = db.query(MemoryJob).filter_by(dedupe_key="compact:2").one()
        if compact_job.status == SUCCEEDED:
            break
    db.expire_all()
    summary = db.query(ConversationSummary).filter_by(session_id=session.id).one()
    assert summary.version > first_version
    assert summary.covered_until_message_id > rows[-3].id
    assert db.query(ConversationEpisode).filter_by(session_id=session.id, is_tail=False).count() == 2

    short_session = ChatSession(public_id="short", title="short", user_id=user.id)
    db.add(short_session)
    db.flush()
    short_user = add_message(db, user, short_session, "USER", "短会话也要沉淀")
    short_assistant = add_message(db, user, short_session, "ASSISTANT", "好的")
    db.commit()
    MemoryJobService(db, config).enqueue_finalize_session(
        user_id=user.id,
        session_id=short_session.id,
        target_message_id=short_assistant.id,
    )
    db.commit()
    while worker.run_once():
        pass
    db.expire_all()
    tail = db.query(ConversationEpisode).filter_by(session_id=short_session.id, is_tail=True).one()
    assert tail.source_start_message_id == short_user.id
    assert db.query(ConversationSummary).filter_by(session_id=short_session.id).count() == 0


def test_profile_delete_invalidates_old_jobs_and_vectors(database):
    db, factory = database
    user, session = add_user_session(db, "profile", "profile-session")
    source = add_message(db, user, session, "USER", "我偏好用清单复习")
    db.commit()
    config = settings()
    jobs = MemoryJobService(db, config)
    jobs.enqueue(
        kind=UPDATE_PROFILE,
        dedupe_key="profile:update:1",
        user_id=user.id,
        session_id=session.id,
        target_message_id=source.id,
        memory_epoch=jobs._profile_state(user.id).memory_epoch,
    )
    db.commit()
    vectors = RecordingVectorStore()
    worker = MemoryWorker(config, session_factory=factory, vector_store_factory=lambda _settings: vectors, worker_id="profile-worker")
    assert worker.run_once()
    db.expire_all()
    memory = db.query(UserMemory).filter_by(user_id=user.id, status="ACTIVE").one()
    stale_index = db.query(MemoryJob).filter_by(kind="INDEX_PROFILE", status=PENDING).one()
    old_epoch = stale_index.memory_epoch

    assert UserMemoryService(db, config).delete(user.id, memory.public_id)
    db.expire_all()
    assert db.query(UserMemory).filter_by(public_id=memory.public_id).one().status == "DELETED"
    assert db.query(MemoryJob).filter_by(id=stale_index.id).one().memory_epoch == old_epoch
    while worker.run_once():
        pass
    assert vectors.profiles == []
    assert ("profile", user.id, memory.public_id) in vectors.deleted


def test_two_workers_claim_once_and_expired_lease_recovers(database):
    db, factory = database
    user, session = add_user_session(db, "workers", "workers-session")
    target = add_message(db, user, session, "USER", "待处理")
    config = settings(memory_worker_lease_seconds=5)
    row = MemoryJobService(db, config).enqueue(
        kind=COMPACT_SESSION,
        dedupe_key="workers:single",
        user_id=user.id,
        session_id=session.id,
        target_message_id=target.id,
    )
    db.commit()
    first = MemoryWorker(config, session_factory=factory, worker_id="worker-a")
    second = MemoryWorker(config, session_factory=factory, worker_id="worker-b")
    claimed_a = first._claim_one()
    claimed_b = second._claim_one()
    assert claimed_a == row.id
    assert claimed_b is None

    db.expire_all()
    crashed = db.get(MemoryJob, row.id)
    crashed.lease_expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert second._claim_one() == row.id


def test_completed_turn_commits_assistant_state_and_memory_jobs_together(database):
    db, _factory = database
    user, session = add_user_session(db, "commit", "commit-session")
    current = add_message(db, user, session, "USER", "完成这一轮")
    trace = AgentRunTrace(
        user_id=user.id,
        session_id=session.id,
        intent="CHAT",
        risk_level="LOW",
        original_input=current.content,
        sanitized_input=current.content,
    )
    db.add(trace)
    db.flush()
    turn = ChatTurn(
        public_id="commit-turn",
        request_id="commit-request",
        user_id=user.id,
        session_id=session.id,
        user_message_id=current.id,
        trace_id=trace.id,
        status="GENERATING",
    )
    db.add(turn)
    db.commit()
    config = settings()
    service = object.__new__(ChatService)
    service.settings = config
    outcome = GenerationOutcome(
        content="已经完成",
        source="APPLICATION",
        complete=True,
        completion_verified=True,
        finish_reason=ModelFinishReason.DIRECT_RESPONSE,
        attempts=(),
        continuation_count=0,
    )
    assistant = service._finalize_completed_turn(
        db,
        turn,
        session,
        outcome,
        {"schemaVersion": 2, "completionVerified": True},
    )
    db.expire_all()
    persisted_turn = db.get(ChatTurn, turn.id)
    assert persisted_turn.status == "COMPLETED"
    assert persisted_turn.assistant_message_id == assistant.id
    assert {row.kind for row in db.query(MemoryJob).filter_by(turn_id=turn.id).all()} == {
        COMPACT_SESSION,
        UPDATE_PROFILE,
    }


def test_real_chroma_memory_filters_upsert_query_and_delete(database):
    pytest.importorskip("chromadb")
    db, _factory = database
    user, session = add_user_session(db, "chroma-u1", "chroma-s1")
    other, other_session = add_user_session(db, "chroma-u2", "chroma-s2")
    source = add_message(db, user, session, "USER", "清单学习")
    other_source = add_message(db, other, other_session, "USER", "清单秘密")
    db.commit()
    own = ConversationEpisode(
        public_id="chroma-own", user_id=user.id, session_id=session.id,
        source_start_message_id=source.id, source_end_message_id=source.id,
        summary_text="偏好清单学习", facts_json="{}", content_hash="a", status="ACTIVE",
        index_status="PENDING", embedding_version="test-v1",
    )
    foreign = ConversationEpisode(
        public_id="chroma-foreign", user_id=other.id, session_id=other_session.id,
        source_start_message_id=other_source.id, source_end_message_id=other_source.id,
        summary_text="其他用户清单秘密", facts_json="{}", content_hash="b", status="ACTIVE",
        index_status="PENDING", embedding_version="test-v1",
    )
    profile = UserMemory(
        public_id="chroma-profile", user_id=user.id, category="LEARNING_PREFERENCE",
        memory_key="LEARNING_PREFERENCE:learning_format", content="偏好清单",
        status="ACTIVE", origin="EXPLICIT", version=1, memory_epoch=0,
    )
    db.add_all([own, foreign, profile])
    db.commit()

    with tempfile.TemporaryDirectory() as directory:
        config = settings(
            memory_chroma_mode="persistent",
            memory_chroma_path=directory,
            memory_embedding_provider="test",
            memory_embedding_version="test-v1",
        )
        store = MemoryVectorStore(config, backend=DeterministicEmbedding())
        store.upsert_episode(own)
        store.upsert_episode(foreign)
        store.upsert_profile(profile)
        hits = store.query_episodes(user_id=user.id, query_text="清单", top_k=8)
        assert [hit.document_id for hit in hits] == ["episode:chroma-own"]
        profile_hits = store.query_profiles(user_id=user.id, query_text="清单", top_k=5)
        assert [hit.document_id for hit in profile_hits] == [f"profile:{user.id}:chroma-profile"]
        store.delete_episode(own.public_id)
        store.delete_profile(user.id, profile.public_id)
        assert store.query_episodes(user_id=user.id, query_text="清单", top_k=8) == []
        assert store.query_profiles(user_id=user.id, query_text="清单", top_k=5) == []
        store.close()


def test_embedding_or_chroma_query_failure_falls_back_to_isolated_sql(database):
    db, _factory = database
    user, current_session = add_user_session(db, "fallback-u1", "fallback-current")
    other, other_session = add_user_session(db, "fallback-u2", "fallback-foreign")
    previous_session = ChatSession(public_id="fallback-previous", title="previous", user_id=user.id)
    db.add(previous_session)
    db.flush()
    own_source = add_message(db, user, previous_session, "USER", "我偏好清单学习")
    foreign_source = add_message(db, other, other_session, "USER", "其他用户清单秘密")
    current = add_message(db, user, current_session, "USER", "请按清单安排复习")
    db.add_all([
        ConversationEpisode(
            public_id="fallback-own",
            user_id=user.id,
            session_id=previous_session.id,
            source_start_message_id=own_source.id,
            source_end_message_id=own_source.id,
            summary_text="用户偏好清单学习",
            facts_json="{}",
            content_hash="fallback-own",
            status="ACTIVE",
            index_status="INDEXED",
            embedding_version="test-v1",
        ),
        ConversationEpisode(
            public_id="fallback-foreign",
            user_id=other.id,
            session_id=other_session.id,
            source_start_message_id=foreign_source.id,
            source_end_message_id=foreign_source.id,
            summary_text="其他用户清单秘密",
            facts_json="{}",
            content_hash="fallback-foreign",
            status="ACTIVE",
            index_status="INDEXED",
            embedding_version="test-v1",
        ),
        UserMemory(
            public_id="fallback-profile",
            user_id=user.id,
            category="LEARNING_PREFERENCE",
            memory_key="LEARNING_PREFERENCE:learning_format",
            content="偏好清单式学习计划",
            confidence=1.0,
            status="ACTIVE",
            origin="EXPLICIT",
            index_status="INDEXED",
            version=1,
            memory_epoch=0,
        ),
    ])
    db.commit()

    retrieval = MemoryRetrievalService(
        db,
        settings(memory_chroma_mode="persistent", memory_embedding_provider="test"),
        UnavailableCache(),
        FailingQueryVectorStore(),
    ).retrieve(
        user_id=user.id,
        session_id=current_session.id,
        session_public_id=current_session.public_id,
        current_message_id=current.id,
        query_text=current.content,
        summary_covered_until_message_id=0,
    )

    assert retrieval.chroma_status == "sql_fallback"
    assert "chroma_episodic" in retrieval.fallback_sources
    assert "chroma_profile" in retrieval.fallback_sources
    assert [item.public_id for item in retrieval.relevant_history] == ["fallback-own"]
    assert [item.public_id for item in retrieval.user_profile] == ["fallback-profile"]
    assert "其他用户" not in json.dumps(retrieval, ensure_ascii=False, default=str)
