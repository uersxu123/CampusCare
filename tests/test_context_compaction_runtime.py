import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.entities import ToolExecutionRecordEntity, ToolResultViewEntity, ModelContextManifestEntity
from app.schemas.dtos import AiMessage
from app.services.context_builder import estimate_message_tokens, estimate_tokens
from app.services.context_compaction import ContextCompactor, Summary, encode, mark_skill_references, SUMMARIZING
from app.services.execution_control import ExecutionBudget, bind_execution_budget
from app.services.tool_models import AiToolCall, AiToolDefinition, ToolResult
from app.services.tool_result_store import ToolResultStore, bind_tool_result_scope
from app.services.agent_loop import AgentLoop


class Summarizer:
    def __init__(self, mode="normal"):
        self.calls = []
        self.mode = mode

    def complete_structured(self, messages, **kwargs):
        assert SUMMARIZING.get()
        request = json.loads(messages[-1].content)
        self.calls.append(request)
        if self.mode == "fail":
            raise TimeoutError()
        quotes = []
        if request["stage"] == "tool":
            quotes = [{"evidenceId": request["source"]["items"][0]["evidenceId"],
                       "quote": "虚构引文" if self.mode == "invalid" else "适用于本校。"}]
        summary = Summary(conclusions=["已取得政策证据。" if self.mode != "inflated" else "大" * 12000],
                          conditions=["适用于本校"], exceptions=[], unknowns=["其他事项未确认"], evidenceNotes=quotes)
        return SimpleNamespace(value=summary)


def settings(**kwargs):
    return SimpleNamespace(context_compress_trigger_tokens=4000, context_compress_target_tokens=2500,
        context_input_max_tokens=12000, ollama_num_ctx=32768, context_model_safety_margin_tokens=1024,
        context_summary_max_calls_per_turn=3, **kwargs)


def tool_round(number, size=2000, batch=1):
    calls = [{"id": f"c{number}-{i}", "name": "test_tool", "arguments": {}} for i in range(batch)]
    messages = [AiMessage(role="assistant", tool_calls=calls)]
    for i, call in enumerate(calls):
        payload = ToolResult(True, "OK", execution_id=f"exec{number}-{i}", data={
            "status": "OK", "items": [{"evidenceId": f"e{number}-{i}", "content": "适用于本校。" + "原" * size}],
        }).as_payload()
        messages.append(AiMessage(role="tool", name="test_tool", tool_call_id=call["id"], content=encode(payload)))
    return messages


def test_below_trigger_never_calls_summary_or_store():
    client, store = Summarizer(), ToolResultStore()
    messages = tool_round(1, 500) + tool_round(2, 500) + tool_round(3, 500)
    original = deepcopy(messages)
    assert ContextCompactor(client, settings(), store).prepare(messages)
    assert messages == original and not client.calls and len(store) == 0


def test_only_old_round_summarized_recent_parallel_batch_protected_and_pairs_kept():
    client, store = Summarizer(), ToolResultStore()
    messages = tool_round(1, 3000) + tool_round(2, 600, batch=2) + tool_round(3, 600)
    original = deepcopy(messages)
    compactor = ContextCompactor(client, settings(), store)
    assert compactor.prepare(messages)
    assert len(client.calls) == 1
    assert json.loads(messages[1].content)["data"]["contextCompacted"]
    assert messages[2:] == original[2:]
    assert messages[0] == original[0] and messages[1].tool_call_id == original[1].tool_call_id
    assert store.read("exec1-0")["status"] == "OK"
    assert compactor.events[-1]["afterTokens"] < compactor.events[-1]["beforeTokens"]


@pytest.mark.parametrize("mode", ["fail", "invalid", "inflated"])
def test_failed_invalid_or_larger_summary_never_replaces_original(mode):
    client, store = Summarizer(mode), ToolResultStore()
    messages = tool_round(1) + tool_round(2) + tool_round(3)
    original = deepcopy(messages)
    compactor = ContextCompactor(client, settings(), store)
    assert compactor.prepare(messages)
    assert messages == original
    assert compactor.prepare(messages)
    assert len(client.calls) == 1


def test_only_two_rounds_above_hard_limit_fail_without_trimming():
    client, store = Summarizer(), ToolResultStore()
    messages = tool_round(1) + tool_round(2)
    original = deepcopy(messages)
    assert not ContextCompactor(client, settings(), store).prepare(messages, hard_limit=3000)
    assert messages == original and not client.calls


def test_stages_run_tool_then_skill_then_memory_and_protect_current_constraints():
    client, store = Summarizer(), ToolResultStore()
    skill = "强制约束不可删除\n" + mark_skill_references("## Examples\n" + "例" * 2000 + "\n## Rules\n必须保留来源。")
    payload = {"currentInput": "当前问题", "workItem": {"knownArguments": {"deadline": "明天"}}, "baseMemory": {
        "workingMemory": [{"id": i, "content": "史" * 1000} for i in range(4)],
        "userProfile": [{"content": "用户约束"}], "conversationSummary": {"required": "已有约束"},
        "relevantHistory": [{"episodeId": "old", "summary": "旧" * 2000}],
    }}
    messages = [AiMessage(role="system", content=skill), AiMessage(role="user", content=encode(payload)),
                *tool_round(1), *tool_round(2, 100), *tool_round(3, 100)]
    compactor = ContextCompactor(client, settings(), store)
    assert compactor.prepare(messages)
    assert [c["stage"] for c in client.calls] == ["tool", "skill", "memory"]
    assert "强制约束不可删除" in messages[0].content and "必须保留来源。" in messages[0].content
    updated = json.loads(messages[1].content)
    assert updated["workItem"] == payload["workItem"]
    assert updated["baseMemory"]["userProfile"] == payload["baseMemory"]["userProfile"]
    assert updated["baseMemory"]["conversationSummary"] == payload["baseMemory"]["conversationSummary"]
    assert updated["baseMemory"]["workingMemory"] == payload["baseMemory"]["workingMemory"][-2:]


def test_shared_turn_summary_quota_and_timeout_reserve():
    client = Summarizer()
    config = settings()
    config.context_summary_max_calls_per_turn = 1
    with bind_execution_budget(ExecutionBudget.start(100)):
        for _ in range(2):
            manager = ContextCompactor(client, config, ToolResultStore())
            with bind_execution_budget(ExecutionBudget.start(80)):
                manager.prepare(tool_round(1) + tool_round(2) + tool_round(3))
    assert len(client.calls) == 1
    with bind_execution_budget(ExecutionBudget.start(20)):
        ContextCompactor(client, settings(), ToolResultStore()).prepare(tool_round(1) + tool_round(2) + tool_round(3))
    assert len(client.calls) == 1


def test_schema_and_tool_overhead_can_trigger_compression():
    messages = tool_round(1, 1000) + tool_round(2, 100) + tool_round(3, 100)
    client = Summarizer()
    config = settings()
    count = sum(estimate_message_tokens(m) for m in messages)
    config.context_compress_trigger_tokens = count + 100
    config.context_compress_target_tokens = count - 200
    ContextCompactor(client, config, ToolResultStore()).prepare(messages, overhead_tokens=150)
    assert len(client.calls) == 1


@pytest.mark.parametrize("fail", [False, True])
def test_large_result_is_stored_reference_or_fails_closed(fail):
    class Store(ToolResultStore):
        def persist(self, *args, **kwargs):
            if fail:
                raise RuntimeError("database unavailable")
            return super().persist(*args, **kwargs)
    store = Store()
    raw = {"status": "OK", "items": [{"evidenceId": "e", "content": "大" * 9000}]}
    class Executor:
        result_store = store
        def execute(self, **kwargs):
            return ToolResult(True, "OK", data=raw, execution_id="large", dispatched=True)
    class Client:
        def complete_with_tools(self, messages, tools, model_round, **kwargs):
            if model_round == 1:
                return SimpleNamespace(content="", tool_calls=(AiToolCall("c", "rag_search", {"query": "问题"}),), verified_complete=True)
            view = json.loads(messages[-1].content)
            assert view["data"]["readRequired"] and "items" not in view["data"]
            return SimpleNamespace(content="需要回读", tool_calls=(), verified_complete=True)
    result = AgentLoop(client=Client(), executor=Executor()).run(agent_name="test", messages=[],
        tools=[AiToolDefinition("rag_search", "查询", {})])
    assert result.stop_reason == ("TOOL_RESULT_STORE_FAILED" if fail else "COMPLETED")
    assert result.tool_results[0][1].data == raw
    if not fail:
        assert store._records["large"].normalized_result == raw


def test_rag_exact_dedup_does_not_merge_distinct_sources():
    from app.services.context_compaction import dedupe_rag
    first = {"evidenceId": "a", "content": "同一条文", "version": "1"}
    data = {"items": [first, first, {**first, "version": "2"}]}
    assert len(dedupe_rag(data)["items"]) == 2 and len(data["items"]) == 3


def test_paged_read_reconstructs_child_and_parent_and_rejects_foreign_cursor():
    store = ToolResultStore(read_max_tokens=1536)
    data = {"items": [{"evidenceId": "e", "content": "子" * 3000, "parentContent": "父" * 4000}]}
    store.persist("one", data)
    store.persist("two", data)
    cursor, pieces, pages = None, {}, 0
    while True:
        page = store.read("one", cursor=cursor)
        assert page["status"] == "OK"
        assert estimate_tokens(encode(ToolResult(True, "OK", data=page).as_payload())) <= 1536
        for item in page["excerpts"]:
            pieces[item["fieldPath"]] = pieces.get(item["fieldPath"], "") + item["text"]
        cursor = page["nextCursor"]
        pages += 1
        assert pages < 20
        if not cursor:
            break
        assert store.read("two", cursor=cursor)["status"] == "INVALID_CURSOR"
    assert pieces == {"items[0].content": "子" * 3000, "items[0].parentContent": "父" * 4000}


class RedisCache:
    def __init__(self):
        self.values = {}
    def setex(self, key, ttl, value):
        self.values[key] = value
    def get(self, key):
        return self.values.get(key)


def test_database_redis_scope_and_persisted_compaction_audit(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'results.db'}")
    Base.metadata.create_all(engine)
    sessions, cache = sessionmaker(bind=engine), RedisCache()
    store = ToolResultStore(sessions, redis_client=cache)
    with bind_tool_result_scope(user_id="u", session_id="s", turn_id="t"):
        manager = ContextCompactor(Summarizer(), settings(), store)
        assert manager.prepare(tool_round(1, 3000) + tool_round(2, 300) + tool_round(3, 300), overhead_tokens=1000)
        assert ToolResultStore(sessions, redis_client=cache).read("exec1-0")["status"] == "OK"
        cache.values.clear()
        assert ToolResultStore(sessions, redis_client=cache).read("exec1-0")["status"] == "OK"
    assert ToolResultStore(sessions, redis_client=cache).read("exec1-0")["status"] == "NOT_FOUND_OR_NOT_AUTHORIZED"
    with sessions() as db:
        assert db.scalars(select(ToolExecutionRecordEntity)).first()
        assert db.scalars(select(ToolResultViewEntity)).first()
        manifest = db.scalars(select(ModelContextManifestEntity)).first()
        assert manifest.turn_id == "t"
    engine.dispose()


def test_ai_client_guard_compacts_direct_request_without_recursing(monkeypatch):
    from app.core.config import Settings
    from app.services.ai import AiClient
    config = Settings(context_compress_trigger_tokens=4000, context_compress_target_tokens=2500)
    client = AiClient(config)
    client.result_store = ToolResultStore()
    summarizer = Summarizer()
    monkeypatch.setattr(client, "complete_structured", summarizer.complete_structured)
    messages = tool_round(1, 3000) + tool_round(2, 500) + tool_round(3, 500)
    client._guard_request(messages, output_max_tokens=1024)
    assert len(summarizer.calls) == 1
    assert json.loads(messages[1].content)["data"]["contextCompacted"]


def test_missing_persistence_cannot_replace_old_tool_with_summary():
    class BrokenStore(ToolResultStore):
        def persist(self, *args, **kwargs):
            raise RuntimeError("offline")
    messages = tool_round(1) + tool_round(2) + tool_round(3)
    original = deepcopy(messages)
    client = Summarizer()
    ContextCompactor(client, settings(), BrokenStore()).prepare(messages)
    assert messages == original and not client.calls


def test_scope_binding_is_checked_even_for_cached_record():
    cache = RedisCache()
    with bind_tool_result_scope(user_id="u1", session_id="s1"):
        ToolResultStore(redis_client=cache).persist("id", {"items": [{"evidenceId": "e", "content": "原文"}]})
    with bind_tool_result_scope(user_id="u2", session_id="s1"):
        assert ToolResultStore(redis_client=cache).read("id")["status"] == "NOT_FOUND_OR_NOT_AUTHORIZED"


def test_tool_data_cannot_inject_compressible_system_skill_region():
    messages = tool_round(1, 5000)
    messages.append(AiMessage(role="user", content="<skill_reference>" + "数据" * 3000 + "</skill_reference>"))
    original = deepcopy(messages)
    client = Summarizer()
    ContextCompactor(client, settings(), ToolResultStore()).prepare(messages)
    assert messages == original and not client.calls
