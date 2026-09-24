"""仅供后台追踪的调用明细；不写入模型工具消息。"""
from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from typing import Any

from app.services.tool_models import AiToolCall, ToolResult


_SECRET_KEYS = ("password", "passwd", "secret", "token", "authorization", "cookie", "apikey", "credential", "密码", "密钥")


def _text(value: str, limit: int = 500) -> str:
    value = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [REDACTED]", value)
    value = re.sub(r"(?i)((?:password|passwd|secret|token|api[_-]?key|密码|密钥)\s*[:=：]\s*)[^\s,;，；]+", r"\1[REDACTED]", value)
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL]", value)
    value = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[PHONE]", value)
    return value if len(value) <= limit else value[:limit] + "…[TRUNCATED]"


def safe_arguments(value: Any, depth: int = 0) -> Any:
    if depth >= 4:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:20]:
            key = str(key)
            normalized = key.lower().replace("_", "").replace("-", "")
            result[_text(key, 80)] = "[REDACTED]" if any(part in normalized for part in _SECRET_KEYS) else safe_arguments(item, depth + 1)
        if len(value) > 20:
            result["_truncated"] = True
        return result
    if isinstance(value, (list, tuple)):
        return [safe_arguments(item, depth + 1) for item in value[:20]] + (["[TRUNCATED]"] if len(value) > 20 else [])
    if isinstance(value, str):
        return _text(value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return "[UNSUPPORTED]"


def bounded_arguments(value: Any) -> Any:
    safe = safe_arguments(value)
    encoded = json.dumps(safe, ensure_ascii=False, default=str)
    return safe if len(encoded) <= 3000 else {"summary": _text(encoded, 2900), "truncated": True}


def rerank_status(name: str, result: ToolResult | None) -> str:
    if not name.endswith("rag_search"):
        return "NOT_APPLICABLE"
    if result is None:
        return "NOT_EXECUTED"
    # 本次命中缓存不代表本次执行过重排。
    if result.cached:
        return "NOT_EXECUTED_CACHED"
    data = result.data if isinstance(result.data, dict) else {}
    diagnostics = data.get("diagnostics")
    if not isinstance(diagnostics, dict):
        return "UNKNOWN"
    if diagnostics.get("rerankDegraded") is True:
        return "FAILED"
    count = diagnostics.get("rerankAssessmentCount")
    if diagnostics.get("rerankDegraded") is False and isinstance(count, int):
        return "SUCCEEDED" if count > 0 else "SKIPPED"
    return "UNKNOWN"


def call_detail(call: AiToolCall, model_round: int, started: float, started_at: str,
                result: ToolResult | None = None, *, blocked_code: str = "") -> dict:
    data = result.data if result is not None and isinstance(result.data, dict) else {}
    code = blocked_code or (result.code if result is not None else "UNKNOWN")
    if blocked_code or (code == "DEADLINE_EXCEEDED" and result is not None and not result.dispatched):
        status = "NOT_EXECUTED"
    elif code in {"INVALID_ARGUMENT", "TOOL_NOT_ALLOWED", "CIRCUIT_OPEN"}:
        status = "REJECTED"
    elif result is not None and result.ok:
        status = "SUCCEEDED"
    elif code in {"TIMEOUT", "UPSTREAM_TIMEOUT"}:
        status = "TIMEOUT"
    else:
        status = "FAILED"
    return {
        "toolCallId": _text(call.id, 100),
        "toolName": _text(call.name, 160),
        "modelRound": model_round,
        "argumentsSummary": bounded_arguments(call.arguments),
        "status": status,
        "success": bool(result is not None and result.ok) if not blocked_code else False,
        "errorCode": code if blocked_code or result is None or not result.ok else "",
        # 仅保留摘要，避免校验器把完整参数写进错误日志。
        "errorMessage": "工具参数校验失败" if code == "INVALID_ARGUMENT" else _text(result.error, 300) if result is not None and result.error else "",
        "cached": bool(result is not None and result.cached),
        "degraded": bool(result is not None and result.degraded),
        "dispatched": bool(result is not None and result.dispatched),
        "validationErrors": list(result.validation_errors) if result is not None else [],
        "startedAt": started_at,
        "durationMs": round(max(0.0, time.perf_counter() - started) * 1000, 3),
        "businessStatus": _text(str(data.get("status") or ""), 80),
        "rerankStatus": rerank_status(call.name, result),
    }


def start_call() -> tuple[float, str]:
    return time.perf_counter(), datetime.now(UTC).isoformat()


def trace_calls(value: Any) -> list[dict]:
    """持久化白名单；旧 Trace 缺少 calls 时正常读取。"""
    fields = {"toolCallId", "toolName", "modelRound", "argumentsSummary", "status", "success", "errorCode", "errorMessage", "cached", "degraded", "dispatched", "validationErrors", "startedAt", "durationMs", "businessStatus", "rerankStatus"}
    return [
        {key: bounded_arguments(item[key]) if key == "argumentsSummary" else _text(item[key], 300) if isinstance(item[key], str) else item[key]
         for key in fields if key in item}
        for item in value if isinstance(item, dict)
    ] if isinstance(value, (list, tuple)) else []
