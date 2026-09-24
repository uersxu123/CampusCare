from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app.agents.result import AgentRunResult
from app.core.config import Settings
from app.models.entities import AgentRunTrace, ChatSession, UserAccount
from app.services.context_builder import context_content_hash
from app.services.tool_call_details import trace_calls


SENSITIVE_KEYS = frozenset({
    "content", "messages", "prompt", "arguments", "originalInput", "sourceText", "taskText",
    "objective", "knownArguments", "evidenceNotes", "quote", "missingInfo", "answerText", "rawAnswer", "rawContract",
    "selectedUserMemories", "selected_user_memories", "structured_summary", "structuredSummary",
    "baseMemory", "base_memory", "workingMemory", "working_memory", "relevantHistory",
    "relevant_history", "userProfile", "user_profile", "conversationSummary",
    "conversation_summary", "directResponse", "answerBrief", "assessment",
})


class AgentTraceService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings

    def save_run(
        self,
        user: UserAccount,
        session: ChatSession,
        original_input: str,
        sanitized_input: str,
        memory_brief: str,
        agent_run: AgentRunResult,
        report_id: int | None,
    ) -> AgentRunTrace:
        include_prompt = bool(self.settings.trace_include_prompt_content)
        context_manifest = dict(agent_run.context_manifest)
        context_manifest["debug_prompt_content_enabled"] = include_prompt
        trace = AgentRunTrace(
            user_id=user.id,
            session_id=session.id,
            report_id=report_id,
            intent=agent_run.primary_intent.value,
            risk_level=agent_run.risk_level.value,
            original_input=original_input,
            sanitized_input=sanitized_input,
            memory_brief=memory_brief,
            agent_steps_json=_json(_agent_steps(agent_run)),
            evidence_items_json=_json([_trace_evidence(item) for item in agent_run.evidence_items]),
            response_messages_json=_json_response_messages(
                _trace_messages(agent_run.response_messages, include_prompt)
            ),
            assessment_json=_json(agent_run.assessment or {}),
            context_manifest_json=_json(context_manifest),
            generation_json="{}",
            tool_diagnostics_json=_json(_trace_tool_diagnostics(agent_run.tool_diagnostics)),
        )
        self.db.add(trace)
        self.db.commit()
        self.db.refresh(trace)
        return trace

    def create_minimal_trace(
        self,
        *,
        user: UserAccount,
        session: ChatSession,
        original_input: str,
        sanitized_input: str,
        intent: str = "CHAT",
        risk_level: str = "LOW",
    ) -> AgentRunTrace:
        trace = AgentRunTrace(
            user_id=user.id,
            session_id=session.id,
            report_id=None,
            intent=intent,
            risk_level=risk_level,
            original_input=original_input,
            sanitized_input=sanitized_input,
            memory_brief="",
            agent_steps_json="[]",
            evidence_items_json="[]",
            response_messages_json="[]",
            assessment_json="{}",
            context_manifest_json="{}",
            generation_json="{}",
            tool_diagnostics_json="{}",
        )
        self.db.add(trace)
        self.db.commit()
        self.db.refresh(trace)
        return trace

    def finalize_generation_trace(
        self,
        trace_id: int,
        generation: dict,
        tool_diagnostics: dict | None = None,
        *,
        commit: bool = False,
    ) -> None:
        trace = self.db.get(AgentRunTrace, trace_id)
        if trace is None:
            raise LookupError("生成 Trace 不存在")
        trace.generation_json = _json(generation)
        if tool_diagnostics is not None:
            trace.tool_diagnostics_json = _json(_trace_tool_diagnostics(tool_diagnostics))
        trace.finalized_at = datetime.now(UTC).replace(tzinfo=None)
        self.db.add(trace)
        self.db.flush()
        if commit:
            self.db.commit()


def _agent_steps(agent_run: AgentRunResult) -> list[dict[str, Any]]:
    entries = [asdict(item) for item in agent_run.steps]
    entries.extend({
        "kind": "agent_event",
        "type": getattr(event.type, "value", event.type),
        "actor": event.actor,
        "taskId": event.task_id,
        "artifactId": event.artifact_id,
        "message": event.message,
        "metadata": _safe(event.metadata),
    } for event in agent_run.collaboration_events)
    entries.extend({
        "kind": "agent_task",
        "id": task.id,
        "status": getattr(task.status, "value", task.status),
        "requiredCapabilities": sorted(task.required_capabilities),
        "claimedBy": list(task.claimed_by),
        "createdBy": task.created_by,
        "metadata": _safe(task.metadata),
    } for task in agent_run.collaboration_tasks)
    entries.extend(_trace_artifact(item) for item in agent_run.collaboration_artifacts)
    return entries


def _trace_artifact(artifact) -> dict[str, Any]:
    payload = artifact.payload if isinstance(artifact.payload, dict) else {}
    if artifact.kind == "route_plan":
        summary = {
            "schemaVersion": payload.get("schemaVersion"),
            "planId": payload.get("planId"),
            "primaryIntent": payload.get("primaryIntent"),
            "intents": list(payload.get("intents") or []),
            "synthesisOrder": list(payload.get("synthesisOrder") or []),
            "workItems": [
                {
                    "workItemId": item.get("workItemId"),
                    "intent": item.get("intent"),
                    "dependsOn": list(item.get("dependsOn") or []),
                    "missingFields": [arg.get("name") for arg in item.get("missingArguments", [])],
                }
                for item in payload.get("workItems", []) if isinstance(item, dict)
            ],
        }
    elif artifact.kind == "specialist_result":
        summary = {
            key: payload.get(key)
            for key in ("planId", "workItemId", "intent", "agentName", "status", "reasonCode", "confidence")
        }
        summary["evidenceIds"] = [item.get("evidenceId") for item in payload.get("evidenceItems", [])]
        summary["toolSummary"] = _trace_tool_summary(payload.get("toolSummary") or {})
        summary["skillSelection"] = _trace_skill_selection(payload.get("skillSelection") or {})
    elif artifact.kind == "clarification_request":
        summary = {
            "schemaVersion": payload.get("schemaVersion"),
            "originPlanId": payload.get("originPlanId"),
            "targetWorkItemId": payload.get("targetWorkItemId"),
            "intent": payload.get("intent"),
            "missingFields": [item.get("name") for item in payload.get("missingArguments", [])],
        }
    elif artifact.kind in {"risk", "safety_review", "critique"}:
        summary = {key: payload.get(key) for key in ("risk", "emotion", "confidence", "approved", "reason", "responseArtifactId")}
    elif artifact.kind == "response_proposal":
        messages = payload.get("messages") or []
        summary = {
            "mode": payload.get("mode"),
            "messageCount": len(messages),
            "messageHashes": [context_content_hash(getattr(item, "content", str(item))) for item in messages],
            "evidenceIds": [item.get("evidenceId") for item in payload.get("promptEvidence", []) if isinstance(item, dict)],
        }
    else:
        summary = {}
    return {
        "kind": "agent_artifact",
        "id": artifact.id,
        "owner": artifact.owner,
        "artifactKind": artifact.kind,
        "confidence": artifact.confidence,
        "taskId": artifact.task_id,
        "metadata": _route_metadata(artifact.metadata) if artifact.kind == "route_plan" else _safe(artifact.metadata),
        "payloadSummary": summary,
    }


def _trace_evidence(item: dict[str, Any]) -> dict[str, Any]:
    content = str(item.get("content") or "")
    return {
        "evidenceId": item.get("evidenceId"),
        "title": item.get("title"),
        "source": item.get("source"),
        "sourceUrl": item.get("sourceUrl"),
        "version": item.get("version"),
        "verifiedAt": item.get("verifiedAt"),
        "site": item.get("site"),
        "contentHash": hashlib.sha256(content.encode("utf-8")).hexdigest() if content else "",
        "contentLength": len(content),
    }


def _trace_tool_diagnostics(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "workItems": [
            {
                "workItemId": item.get("workItemId"),
                "agentName": item.get("agentName"),
                "status": item.get("status"),
                "reasonCode": item.get("reasonCode"),
                "toolSummary": _trace_tool_summary(item.get("toolSummary") or {}),
            }
            for item in value.get("workItems", []) if isinstance(item, dict)
        ],
        "totalCallCount": int(value.get("totalCallCount") or 0),
        "degraded": bool(value.get("degraded")),
        "errorCodes": [str(item) for item in value.get("errorCodes", [])],
    }


def _trace_skill_selection(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        "selectorVersion": value.get("selectorVersion"),
        "workItemId": value.get("workItemId"),
        "eligibleSkillIds": [str(item) for item in value.get("eligibleSkillIds", [])],
        "qualifiedSkillIds": [str(item) for item in value.get("qualifiedSkillIds", [])],
        "injectedSkillIds": [str(item) for item in value.get("injectedSkillIds", [])],
        "budgetRejectedSkillIds": [str(item) for item in value.get("budgetRejectedSkillIds", [])],
        "candidates": [
            {
                "skillId": item.get("skillId"),
                "group": item.get("group"),
                "rulePoints": item.get("rulePoints"),
                "matchedRuleIds": [str(rule_id) for rule_id in item.get("matchedRuleIds", [])],
                "excluded": bool(item.get("excluded")),
                "excludeRuleIds": [str(rule_id) for rule_id in item.get("excludeRuleIds", [])],
            }
            for item in value.get("candidates", []) if isinstance(item, dict)
        ],
        "groups": [
            {
                "group": item.get("group"),
                "decision": item.get("decision"),
                "selectedSkillId": item.get("selectedSkillId"),
                "nearSkillIds": [str(skill_id) for skill_id in item.get("nearSkillIds", [])],
                "ruleMarginPoints": item.get("ruleMarginPoints"),
                "semanticScores": {
                    str(skill_id): score for skill_id, score in (item.get("semanticScores") or {}).items()
                },
                "semanticMargin": item.get("semanticMargin"),
                "embeddingCalled": bool(item.get("embeddingCalled")),
                "embeddingCallCount": item.get("embeddingCallCount"),
                "cacheHits": item.get("cacheHits"),
                "elapsedMs": item.get("elapsedMs"),
            }
            for item in value.get("groups", []) if isinstance(item, dict)
        ],
    }


def _trace_tool_summary(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "usedTools": [str(item) for item in value.get("usedTools", [])],
        "callCount": int(value.get("callCount") or 0),
        "degraded": bool(value.get("degraded")),
        "errorCodes": [str(item) for item in value.get("errorCodes", [])],
        "calls": trace_calls(value.get("calls", [])),
    }


def _route_metadata(value: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "planSource", "llmInvoked", "logicalInvocationCount", "providerAttemptCount", "latencyMs", "contextRelation",
        "ruleSegmentCount", "llmSegmentCount", "acceptedSegmentCount",
        "dependencyHintCount", "hardDataEdgeCount", "orderOnlyEdgeCount", "hardDataEdges", "orderOnlyEdges", "fallbackReason",
        "routingMode", "routingScores", "degradedScores", "ruleScores", "embeddingScores",
        "fastRouteReason", "routingScoreLatencyMs",
    }
    return {key: _safe(item) for key, item in value.items() if key in allowed}


def _trace_messages(messages: list[Any], include_prompt: bool) -> list[dict[str, Any]]:
    rows = []
    for message in messages:
        content = str(getattr(message, "content", ""))
        row = {
            "role": str(getattr(message, "role", "")),
            "contentHash": context_content_hash(content),
            "contentLength": len(content),
        }
        if include_prompt:
            row["content"] = content
        rows.append(row)
    return rows


def _safe(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _safe(asdict(value))
    if hasattr(value, "model_dump"):
        return _safe(value.model_dump())
    if isinstance(value, dict):
        return {
            str(key): ("[REDACTED]" if str(key) in SENSITIVE_KEYS else _safe(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, str):
        return value[:500]
    return value


def _json(value: Any) -> str:
    return json.dumps(_safe(value), ensure_ascii=False, default=str)


def _json_response_messages(value: list[dict[str, Any]]) -> str:
    """Serialize the already-sanitized final prompt trace, including opt-in debug content."""
    return json.dumps(value, ensure_ascii=False, default=str)
