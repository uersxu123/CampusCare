from __future__ import annotations

import json
import logging
import time
import unicodedata
import uuid
from types import SimpleNamespace
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from typing import Callable, Protocol

from app.services.tool_call_details import call_detail, start_call
from app.schemas.dtos import AiMessage
from app.services.tool_models import (
    AgentLoopResult,
    AiToolCall,
    AiToolCompletion,
    AiToolDefinition,
    ToolResult,
)
from app.services.context_builder import estimate_message_tokens, estimate_tokens
from app.services.execution_control import ExecutionBudget, bind_execution_budget, current_execution_budget
from app.services.context_compaction import ContextCompactor, dedupe_rag, encode, object_payload, stored_reference
from app.services.tool_result_store import canonical_hash


logger = logging.getLogger(__name__)


_TOOL_RESULT_TRUNCATION_MARKER = "\n[工具结果已按输入预算截断]"


class ToolExecutorProtocol(Protocol):
    def execute(
        self,
        *,
        agent_name: str,
        tool_name: str,
        arguments: dict,
        remaining_seconds: float,
        tool_call_id: str = "",
    ) -> ToolResult: ...


class AgentLoop:
    def __init__(
        self,
        *,
        client,
        executor: ToolExecutorProtocol,
        max_model_rounds: int = 3,
        max_tool_calls: int = 4,
        max_parallel_tools: int = 2,
        max_calls_per_tool: int = 2,
        max_rag_calls: int = 2,
        max_result_chars: int = 0,
        max_single_result_chars: int = 0,
        deadline_seconds: float = 100.0,
        input_max_tokens: int = 28672,
        input_safety_margin_tokens: int = 1024,
        output_max_tokens: int = 1024,
        model_context_tokens: int = 32768,
        tool_result_large_tokens: int = 8000,
        final_answer_reserve_seconds: float = 0.0,
        rag_followup_min_seconds: float = 15.0,
        trusted_rag_query: str | None = None,
        readable_execution_ids: tuple[str, ...] = (),
        final_response_model=None,
    ):
        self.client = client
        self.executor = executor
        self.max_model_rounds = max(1, max_model_rounds)
        self.max_tool_calls = max(0, max_tool_calls)
        self.max_parallel_tools = max(1, max_parallel_tools)
        self.max_calls_per_tool = max(1, max_calls_per_tool)
        self.max_rag_calls = max(0, int(max_rag_calls))
        self.max_result_chars = max(0, max_result_chars)
        self.max_single_result_chars = max(0, max_single_result_chars)
        self.deadline_seconds = max(0.1, deadline_seconds)
        self.input_max_tokens = max(256, int(input_max_tokens))
        self.input_safety_margin_tokens = max(0, int(input_safety_margin_tokens))
        self.output_max_tokens = max(1, int(output_max_tokens))
        self.model_context_tokens = max(self.input_max_tokens, int(model_context_tokens))
        self.tool_result_large_tokens = max(1, int(tool_result_large_tokens))
        self.final_answer_reserve_seconds = max(0.0, final_answer_reserve_seconds)
        self.rag_followup_min_seconds = max(0.0, rag_followup_min_seconds)
        self.trusted_rag_query = trusted_rag_query
        self.readable_execution_ids = frozenset(readable_execution_ids)
        self.final_response_model = final_response_model
        settings = getattr(client, "settings", SimpleNamespace())
        self.compactor = ContextCompactor(client, settings, getattr(executor, "result_store", None))
        if hasattr(client, "context_compactor"):
            client.context_compactor = self.compactor
            client.result_store = self.compactor.store

    def run(
        self,
        *,
        agent_name: str,
        messages: list[AiMessage],
        tools: list[AiToolDefinition],
        finalize: Callable | None = None,
        accept_final: Callable[[str], bool] | None = None,
    ) -> AgentLoopResult:
        started = time.monotonic()
        shared_budget = current_execution_budget()
        remaining = (
            (lambda: self._remaining(started, shared_budget))
            if shared_budget is not None
            else (lambda: self._remaining(started))
        )
        # Budget fitting must never mutate the caller's audit copy.
        conversation = deepcopy(messages)
        allowed = {tool.name for tool in tools}
        dispatched_fingerprints: set[str] = set()
        invalid_fingerprints: set[str] = set()
        dispatch_counts: Counter[str] = Counter()
        results: list[tuple[AiToolCall, ToolResult]] = []
        result_chars = 0
        rag_dispatch_count = 0
        last_content = ""
        details: list[dict] = []
        budget_stop_reason = ""
        finalizing = False
        finalize_used = False
        tool_schema_tokens = estimate_tokens(json.dumps(
            [tool.provider_payload() for tool in tools],
            ensure_ascii=False,
            separators=(",", ":"),
        )) if tools else 0

        def finish(content: str, rounds: int, reason: str) -> AgentLoopResult:
            return AgentLoopResult(content, tuple(results), rounds, reason, tuple(details), budget_stop_reason,
                                   finalize_used, tuple(_visible_tool_evidence(conversation)),
                                   tuple({**payload, "toolCallId": message.tool_call_id, "toolName": message.name}
                                         for message in conversation if message.role == "tool"
                                         if (payload := object_payload(message)) is not None),
                                   tuple(self.compactor.events))

        def not_executed(calls, code: str) -> None:
            for pending in calls:
                tick, timestamp = start_call()
                details.append(call_detail(pending, model_round, tick, timestamp, blocked_code=code))

        for model_round in range(1, self.max_model_rounds + 1):
            if remaining() <= 0:
                return finish("", model_round - 1, "DEADLINE_EXCEEDED")
            if remaining() <= self.final_answer_reserve_seconds:
                finalizing = True
                budget_stop_reason = budget_stop_reason or "FINAL_ANSWER_TIME_RESERVED"
            finalizing = finalizing or model_round == self.max_model_rounds or not tools
            readable_ids = self.readable_execution_ids | _readable_tool_executions(conversation)
            round_tools = [] if finalizing else [
                tool for tool in tools
                if not tool.name.endswith("read_tool_evidence") or readable_ids
            ]
            round_tools = [tool for tool in round_tools
                           if not tool.name.endswith("rag_search") or rag_dispatch_count < self.max_rag_calls]
            if finalizing:
                conversation.insert(0, AiMessage(role="system", content=(
                    "现在进入最终回答阶段，不得调用工具。仅依据已取得且实际可见的证据回答；"
                    "保留未知和适用范围，不得补造缺失事实。"
                )))
            overhead = 0 if finalizing else tool_schema_tokens
            if finalizing and self.final_response_model is not None:
                overhead += estimate_tokens(encode(self.final_response_model.model_json_schema()))
            with bind_execution_budget(ExecutionBudget.start(remaining())):
                if not self._fit_request_budget(conversation, overhead):
                    return finish(last_content, model_round - 1, "INPUT_BUDGET_EXCEEDED")
            # Budget fitting may have persisted a previously memory-only result.
            readable_ids = self.readable_execution_ids | _readable_tool_executions(conversation)
            if not finalizing:
                round_tools = [tool for tool in tools
                               if (not tool.name.endswith("read_tool_evidence") or readable_ids)
                               and (not tool.name.endswith("rag_search") or rag_dispatch_count < self.max_rag_calls)]
            if remaining() <= 0:
                return finish(last_content, model_round - 1, "DEADLINE_EXCEEDED")
            try:
                with bind_execution_budget(ExecutionBudget.start(remaining())):
                    if finalizing and finalize is not None:
                        finalize_used = True
                        content = finalize(conversation, tuple(results), model_round)
                        if remaining() <= 0:
                            return finish("", model_round, "DEADLINE_EXCEEDED")
                        return finish(content, model_round, "COMPLETED")
                    completion: AiToolCompletion = self.client.complete_with_tools(
                        conversation, round_tools, model_round=model_round,
                        purpose=f"{agent_name}.agent_loop.round{model_round}",
                    )
            except Exception as exc:
                logger.warning(
                    "Agent model completion failed agent=%s round=%s error_type=%s",
                    agent_name,
                    model_round,
                    type(exc).__name__,
                )
                reason = "DEADLINE_EXCEEDED" if remaining() <= 0 else "ANSWER_CONTRACT_INVALID" if finalize_used else "MODEL_ERROR"
                return finish(last_content, model_round, reason)
            last_content = completion.content
            if remaining() <= 0:
                not_executed(completion.tool_calls, "DEADLINE_EXCEEDED")
                return finish(last_content, model_round, "DEADLINE_EXCEEDED")
            if not completion.verified_complete:
                not_executed(completion.tool_calls, "MODEL_INCOMPLETE")
                return finish(completion.content, model_round, "MODEL_INCOMPLETE")
            if not completion.tool_calls:
                if accept_final is not None and not accept_final(completion.content):
                    if finalizing:
                        return finish(completion.content, model_round, "ANSWER_CONTRACT_INVALID")
                    conversation.append(AiMessage(role="assistant", content=completion.content))
                    finalizing = True
                    continue
                return finish(completion.content, model_round, "COMPLETED")
            if finalizing:
                not_executed(completion.tool_calls, "FINALIZE_TOOL_CALL_REJECTED")
                return finish("", model_round, "FINALIZE_TOOL_CALL_REJECTED")
            if len(completion.tool_calls) > self.max_parallel_tools:
                not_executed(completion.tool_calls, "PARALLEL_TOOL_BUDGET_EXCEEDED")
                return finish("", model_round, "PARALLEL_TOOL_BUDGET_EXCEEDED")

            rag_count_before_round = rag_dispatch_count
            model_calls = tuple(
                replace(call, arguments={**call.arguments, "query": self.trusted_rag_query})
                if self.trusted_rag_query and rag_dispatch_count == 0 and call.name.endswith("rag_search") else call
                for call in completion.tool_calls
            )
            calls_payload = [call.message_payload() for call in model_calls]
            conversation.append(AiMessage(role="assistant", content=completion.content, tool_calls=calls_payload))
            for call_index, call in enumerate(model_calls):
                call_remaining = remaining()
                if call_remaining <= 0:
                    not_executed(completion.tool_calls[call_index:], "DEADLINE_EXCEEDED")
                    return finish(last_content, model_round, "DEADLINE_EXCEEDED")
                tick, timestamp = start_call()
                fingerprint = _fingerprint(call)
                blocked = ""
                if call.name not in allowed:
                    result = ToolResult(False, "TOOL_NOT_ALLOWED", error="工具不在当前 Agent 的允许列表")
                elif call.name.endswith("read_tool_evidence") and str(call.arguments.get("execution_id") or "") not in readable_ids:
                    blocked = "NOT_FOUND_OR_NOT_AUTHORIZED"
                elif finalizing:
                    blocked = budget_stop_reason or "TOOL_BUDGET_EXCEEDED"
                elif call_remaining <= self.final_answer_reserve_seconds:
                    blocked = "FINAL_ANSWER_TIME_RESERVED"
                elif call.name.endswith("rag_search") and rag_dispatch_count >= self.max_rag_calls:
                    blocked = "RAG_CALL_BUDGET_EXCEEDED"
                elif call.name.endswith("rag_search") and rag_dispatch_count > rag_count_before_round:
                    blocked = "RAG_REQUIRES_PREVIOUS_RESULT"
                elif (call.name.endswith("rag_search") and rag_dispatch_count > 0
                      and call_remaining <= self.final_answer_reserve_seconds + self.rag_followup_min_seconds):
                    blocked = "RAG_FOLLOWUP_TIME_RESERVED"
                elif sum(dispatch_counts.values()) >= self.max_tool_calls or dispatch_counts[call.name] >= self.max_calls_per_tool:
                    blocked = "TOOL_BUDGET_EXCEEDED"
                elif fingerprint in dispatched_fingerprints:
                    blocked = "DUPLICATE_TOOL_CALL"
                else:
                    result = self.executor.execute(
                        agent_name=agent_name,
                        tool_name=call.name,
                        arguments=call.arguments,
                        remaining_seconds=max(0.0, call_remaining - self.final_answer_reserve_seconds),
                        tool_call_id=call.id,
                    )
                    dispatched = result.dispatched or result.code not in {
                        "INVALID_ARGUMENT", "TOOL_NOT_ALLOWED", "CIRCUIT_OPEN", "DEADLINE_EXCEEDED",
                    }
                    if dispatched:
                        dispatch_counts[call.name] += 1
                        dispatched_fingerprints.add(fingerprint)
                    if call.name.endswith("rag_search") and dispatched:
                        rag_dispatch_count += 1
                if blocked:
                    budget_stop_reason = budget_stop_reason or blocked
                    finalizing = True
                    result = ToolResult(False, blocked, error="本次不再派发工具，请基于已有证据完成回答并说明缺口。")
                details.append(call_detail(call, model_round, tick, timestamp, result, blocked_code=blocked))
                if result.code == "INVALID_ARGUMENT":
                    if fingerprint in invalid_fingerprints:
                        budget_stop_reason = "REPEATED_INVALID_ARGUMENT"
                        finalizing = True
                    invalid_fingerprints.add(fingerprint)
                if call.name.endswith("rag_search") and result.ok:
                    result = replace(result, data=dedupe_rag(result.data))
                payload = result.as_payload()
                result_tokens = estimate_tokens(encode(payload))
                is_large = result_tokens > self.tool_result_large_tokens
                if is_large and not call.name.endswith("read_tool_evidence"):
                    if not result.execution_id:
                        result = replace(result, execution_id="exec_" + uuid.uuid4().hex)
                    if not self._persist_result(result, call, "LARGE_RESULT"):
                        results.append((call, result))
                        not_executed(model_calls[call_index + 1:], "TOOL_RESULT_STORE_FAILED")
                        return finish("", model_round, "TOOL_RESULT_STORE_FAILED")
                    result = replace(result, persisted=True, raw_hash=canonical_hash(result.data))
                    payload = result.as_payload()
                    payload = stored_reference(payload, result_tokens)
                results.append((call, result))
                rendered = encode(payload)
                if self.max_single_result_chars > 0 and len(rendered) > self.max_single_result_chars:
                    not_executed(completion.tool_calls[call_index + 1:], "TOOL_RESULT_BUDGET_EXCEEDED")
                    return finish(last_content, model_round, "TOOL_RESULT_BUDGET_EXCEEDED")
                result_chars += len(rendered)
                if self.max_result_chars > 0 and result_chars > self.max_result_chars:
                    not_executed(completion.tool_calls[call_index + 1:], "TOOL_RESULT_BUDGET_EXCEEDED")
                    return finish("", model_round, "TOOL_RESULT_BUDGET_EXCEEDED")
                conversation.append(AiMessage(
                    role="tool",
                    content=rendered,
                    tool_call_id=call.id,
                    name=call.name,
                ))
        return finish("", self.max_model_rounds, "MODEL_ROUND_BUDGET_EXCEEDED")

    def _persist_result(self, result: ToolResult, call: AiToolCall, reason: str) -> bool:
        store = getattr(self.executor, "result_store", None)
        if store is None or not result.execution_id:
            return False
        try:
            store.persist(
                result.execution_id, result.data, persist_reason=reason,
                tool_call_id=call.id, tool_name=call.name,
                arguments=call.arguments,
                business_status=result.code, quality_status="DEGRADED" if result.degraded else "VERIFIED",
                error_code="" if result.ok else result.code,
                index_signature=str(getattr(self.executor, "index_version", "") or ""),
            )
            return True
        except Exception:
            logger.exception("Persisting tool evidence failed execution_id=%s", result.execution_id)
            return False

    def _fit_request_budget(self, conversation: list[AiMessage], tool_schema_tokens: int) -> bool:
        hard = min(self.input_max_tokens, self.model_context_tokens - self.output_max_tokens
                   - self.input_safety_margin_tokens)
        return self.compactor.prepare(conversation, overhead_tokens=tool_schema_tokens,
                                      output_tokens=self.output_max_tokens, hard_limit=hard)

    def _remaining(self, started: float, shared_budget=None) -> float:
        local_remaining = max(0.0, self.deadline_seconds - (time.monotonic() - started))
        return min(local_remaining, shared_budget.remaining()) if shared_budget is not None else local_remaining


def _readable_tool_executions(messages) -> frozenset[str]:
    ids = set()
    for message in messages:
        if message.role != "tool":
            continue
        try:
            payload = json.loads(message.content)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("ok") and payload.get("persisted") and payload.get("executionId"):
            ids.add(str(payload["executionId"]))
    return frozenset(ids)


def _visible_tool_evidence(messages) -> list[dict]:
    evidence = []
    for message in messages:
        if message.role != "tool":
            continue
        try:
            payload = json.loads(message.content)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or not payload.get("ok"):
            continue
        data = payload.get("data")
        if not isinstance(data, dict):
            continue
        for item in [*data.get("items", []), *data.get("excerpts", [])]:
            if isinstance(item, dict):
                evidence.append({**item, "executionId": item.get("executionId") or data.get("executionId") or payload.get("executionId"),
                                 "persisted": bool(item.get("persisted") or payload.get("persisted")),
                                 "rawHash": item.get("rawHash") or data.get("rawHash") or payload.get("rawHash") or "",
                                 "content": item.get("content") or item.get("text") or item.get("snippet") or ""})
    return evidence


def _fingerprint(call: AiToolCall) -> str:
    if call.name.endswith("rag_search"):
        arguments = dict(call.arguments)
        query = arguments.get("query")
        if isinstance(query, str):
            arguments["query"] = " ".join(unicodedata.normalize("NFKC", query).split()).casefold().rstrip("。？！?!")
        return f"rag_search:{json.dumps(arguments, ensure_ascii=False, sort_keys=True)}"
    if call.name.endswith("read_tool_evidence"):
        arguments = {key: call.arguments.get(key) for key in ("execution_id", "cursor")}
        arguments["evidence_ids"] = sorted(set(call.arguments.get("evidence_ids") or []))
        return f"{call.name}:{json.dumps(arguments, ensure_ascii=False, sort_keys=True)}"
    return f"{call.name}:{json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"


def _halving_operations(length: int, minimum_length: int) -> int:
    operations = 0
    current = max(0, int(length))
    while current > minimum_length:
        current = max(minimum_length, current // 2)
        operations += 1
    return operations


def _truncate_with_marker(text: str, *, minimum_length: int, marker: str) -> str:
    target_length = max(minimum_length, len(text) // 2)
    body_length = max(0, target_length - len(marker))
    return text[:body_length] + marker


def _serialize_tool_result(payload: dict, max_chars: int) -> str | None:
    def render(value: dict) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    original = render(payload)
    if len(original) <= max_chars:
        return original

    view = deepcopy(payload)
    data = view.get("data")
    if isinstance(data, dict):
        removed_fields = []
        for key in ("diagnostics", "debug", "raw", "metadata", "telemetry"):
            if key in data:
                data.pop(key, None)
                removed_fields.append(key)
        items = data.get("items")
        original_count = len(items) if isinstance(items, list) else 0
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                for field in ("content", "text", "snippet", "answer"):
                    text = item.get(field)
                    if not isinstance(text, str):
                        continue
                    original_length = len(text)
                    while len(render(view)) > max_chars and len(text) > 96:
                        text = _truncate_with_marker(text, minimum_length=96, marker="\n[工具证据已截断]")
                        item[field] = text
                        item[f"{field}Truncated"] = True
                        item[f"{field}OriginalChars"] = original_length
            while len(render(view)) > max_chars and len(items) > 1:
                items.pop()
        data["truncation"] = {
            "truncated": True,
            "originalChars": len(original),
            "originalItemCount": original_count,
            "includedItemCount": len(items) if isinstance(items, list) else original_count,
            "removedFields": removed_fields,
        }
    if len(render(view)) <= max_chars:
        return render(view)

    if isinstance(data, dict):
        minimal_data: dict = {}
        for key in ("status", "code", "reason"):
            if key in data:
                minimal_data[key] = data[key]
        items = data.get("items")
        if isinstance(items, list):
            minimal_items = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                minimal = {
                    key: item[key]
                    for key in ("evidenceId", "contextId", "id", "sourceId")
                    if key in item
                }
                for field in ("content", "text", "snippet", "answer"):
                    if isinstance(item.get(field), str):
                        minimal[field] = _truncate_with_marker(
                            item[field],
                            minimum_length=48,
                            marker="\n[工具证据已截断]",
                        )
                        minimal[f"{field}Truncated"] = True
                        break
                minimal_items.append(minimal)
            minimal_data["items"] = minimal_items
        minimal_data["truncation"] = {
            "truncated": True,
            "originalChars": len(original),
            "originalItemCount": len(items) if isinstance(items, list) else 0,
            "includedItemCount": len(minimal_data.get("items", [])),
        }
        view["data"] = minimal_data
        while len(render(view)) > max_chars and len(minimal_data.get("items", [])) > 1:
            minimal_data["items"].pop()
            minimal_data["truncation"]["includedItemCount"] = len(minimal_data["items"])
    rendered = render(view)
    return rendered if len(rendered) <= max_chars else None
