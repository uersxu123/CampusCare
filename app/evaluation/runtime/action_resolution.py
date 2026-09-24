from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResolvedWorkItem:
    work_item_id: str
    intent: str
    status: str
    reason_code: str
    usable_context_ids: tuple[str, ...]
    knowledge_requested: bool
    tool_error_codes: tuple[str, ...]
    result: dict[str, Any]


@dataclass(frozen=True)
class ResolvedAction:
    action: str
    reason_codes: tuple[str, ...] = ()
    work_items: tuple[ResolvedWorkItem, ...] = ()
    infra_error_codes: tuple[str, ...] = ()


def normalize_tool_name(name: str) -> str:
    value = str(name or "").strip()
    return value.rsplit("__", 1)[-1].rsplit("/", 1)[-1]


def context_id_from_item(item: Any) -> str:
    """Return the cross-run identity without conflating it with evidenceId."""
    if isinstance(item, dict):
        context_id = item.get("contextId")
        if context_id:
            return str(context_id)
        context_id = item.get("context_id")
        if context_id:
            return str(context_id)
        chunk_id = item.get("chunkId") or item.get("chunk_id")
        if chunk_id is not None:
            return f"knowledge:{chunk_id}"
        evidence_id = str(item.get("evidenceId") or item.get("evidence_id") or "")
    else:
        context_id = getattr(item, "context_id", None)
        if context_id:
            return str(context_id)
        chunk_id = getattr(item, "chunk_id", None)
        if chunk_id is not None:
            return f"knowledge:{chunk_id}"
        evidence_id = str(getattr(item, "evidence_id", "") or "")
    if evidence_id.startswith("ev_chunk_") and evidence_id[9:].isdigit():
        return f"knowledge:{evidence_id[9:]}"
    return ""


def _item_context_ids(result: dict[str, Any]) -> tuple[str, ...]:
    values = []
    for item in result.get("evidenceItems") or result.get("evidence_items") or []:
        context_id = context_id_from_item(item)
        if context_id and context_id not in values:
            values.append(context_id)
    return tuple(values)


def _resolved_work_item_outcomes(harness: Any) -> tuple[ResolvedWorkItem, ...]:
    if harness is None:
        return ()
    rows = []
    final_states = {str(item.get("workItemId")): item for item in
                    (getattr(harness, "response_generation_diagnostics", {}) or {}).get("finalWorkItemStates", [])}
    for result in getattr(harness, "specialist_results", None) or []:
        result = dict(result)
        final_state = final_states.get(str(result.get("workItemId")))
        if final_state:
            result["finalAnswerStatus"] = final_state.get("answerStatus")
        summary = dict(result.get("toolSummary") or {})
        used_tools = [normalize_tool_name(name) for name in summary.get("usedTools", [])]
        rows.append(ResolvedWorkItem(
            work_item_id=str(result.get("workItemId") or ""),
            intent=str(result.get("intent") or ""),
            status=str(result.get("status") or "").upper(),
            reason_code=str(result.get("reasonCode") or "").upper(),
            usable_context_ids=_item_context_ids(result),
            knowledge_requested="rag_search" in used_tools,
            tool_error_codes=tuple(str(code) for code in summary.get("errorCodes", []) if code),
            result=result,
        ))
    return tuple(rows)


def work_item_outcomes(harness: Any) -> list[dict[str, Any]]:
    """Return the privacy-safe contract representation used in reports."""
    return [
        {
            "workItemId": item.work_item_id,
            "intent": item.intent,
            "status": item.status,
            "reasonCode": item.reason_code,
            "answerStatus": item.result.get("answerStatus"),
            "finalAnswerStatus": item.result.get("finalAnswerStatus", item.result.get("answerStatus")),
            "knowledgeRequested": item.knowledge_requested,
            "usableContextIds": list(item.usable_context_ids),
            "toolErrorCodes": list(item.tool_error_codes),
        }
        for item in _resolved_work_item_outcomes(harness)
    ]


def resolve_evaluation_action(route: dict[str, Any] | None, harness: Any) -> ResolvedAction:
    route = route or {}
    primary = str(route.get("primaryIntent") or "").upper()
    risk = str(route.get("riskLevel") or "").upper()
    if primary == "RISK" or risk == "HIGH":
        return ResolvedAction("SAFETY_BYPASS", ("RISK_BYPASS",))
    clarification = getattr(harness, "clarification_request", None) if harness is not None else None
    if isinstance(clarification, dict) and clarification:
        return ResolvedAction("CLARIFY", ("CLARIFICATION_REQUEST",))
    items = _resolved_work_item_outcomes(harness)
    if not items:
        return ResolvedAction("ABSTAIN", ("ACTION_UNRESOLVED",))
    mapped: list[ResolvedWorkItem] = []
    reason_codes: list[str] = []
    infra_codes: list[str] = []
    for item in items:
        status, reason, usable = item.status, item.reason_code, bool(item.usable_context_ids)
        if reason == "GRADE_UNAVAILABLE":
            infra_codes.append("GRADE_UNAVAILABLE")
        if status == "FAILED" and reason == "TOOL_UNAVAILABLE":
            infra_codes.extend(item.tool_error_codes or ("TOOL_UNAVAILABLE",))
        reason_codes.append(reason or f"STATUS_{status or 'UNKNOWN'}")
        mapped.append(item)
    # Re-evaluate from the explicit mapping, preserving the fixed aggregate order.
    result_kinds = []
    for item in mapped:
        if item.result.get("schemaVersion") == 3:
            answer_status = item.result.get("finalAnswerStatus", item.result.get("answerStatus"))
            if item.status == "FAILED":
                result_kinds.append("FAILED")
            elif answer_status in {"FULL", "NOT_REQUIRED"}:
                result_kinds.append("ANSWERED")
            elif answer_status == "PARTIAL":
                result_kinds.append("PARTIAL")
            else:
                result_kinds.append("ABSTAINED")
            continue
        if item.reason_code in {"RETRIEVAL_COMPLETED", "RETRIEVAL_EMPTY", "RETRIEVAL_DEGRADED"}:
            result_kinds.append("UNRESOLVED")
            continue
        if item.status == "COMPLETED" and item.reason_code == "EVIDENCE_COMPLETE":
            result_kinds.append("ANSWERED" if item.usable_context_ids else "ABSTAINED")
        elif item.status == "COMPLETED" and item.reason_code in {"NO_TOOL_REQUIRED", "TOOL_COMPLETE", ""}:
            result_kinds.append("ANSWERED")
        elif item.status == "PARTIAL" and item.reason_code in {"EVIDENCE_PARTIAL", "EVIDENCE_CONFLICT"} and item.usable_context_ids:
            result_kinds.append("PARTIAL")
        elif item.status in {"PARTIAL", "FAILED"} and item.reason_code in {"EVIDENCE_INSUFFICIENT", "GRADE_UNAVAILABLE", "TOOL_UNAVAILABLE"}:
            result_kinds.append("ABSTAINED")
        else:
            result_kinds.append("FAILED" if item.status == "FAILED" else "ABSTAINED")
    if "UNRESOLVED" in result_kinds:
        action = "UNRESOLVED"
    elif all(kind == "ANSWERED" for kind in result_kinds):
        action = "ANSWER"
    elif any(kind in {"ANSWERED", "PARTIAL"} for kind in result_kinds) and any(kind in {"PARTIAL", "ABSTAINED", "FAILED"} for kind in result_kinds):
        action = "PARTIAL_ANSWER"
    else:
        action = "ABSTAIN" if any(kind in {"ABSTAINED", "FAILED"} for kind in result_kinds) else "ABSTAIN"
    return ResolvedAction(action, tuple(dict.fromkeys(reason_codes)), tuple(mapped), tuple(dict.fromkeys(infra_codes)))
