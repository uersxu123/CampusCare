from __future__ import annotations

import json
import os

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import CallToolResult, TextContent

from app.chat_tools.weather import OpenMeteoWeatherService, WeatherToolError


mcp = FastMCP("CampusCare Readonly Chat Tools")


def _success(data: dict, telemetry: dict | None = None) -> CallToolResult:
    # Keep the MCP wire object parseable. Context projection and compression
    # happen only after the complete structured result has been validated and,
    # when required, persisted by AgentLoop.
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    structured = dict(data)
    if telemetry:
        structured["_mindbridgeTelemetry"] = telemetry
    return CallToolResult(
        isError=False,
        structuredContent=structured,
        content=[TextContent(type="text", text=text)],
    )


def _error(code: str, message: str) -> CallToolResult:
    structured = {"error": {"code": code, "message": message}}
    return CallToolResult(
        isError=True,
        structuredContent=structured,
        content=[TextContent(type="text", text=json.dumps(structured, ensure_ascii=False))],
    )


@mcp.tool(structured_output=False)
def get_current_weather(location: str) -> CallToolResult:
    """查询指定地点的当前天气；只接受地点文本。"""
    service = OpenMeteoWeatherService(
        geocoding_base_url=os.getenv("WEATHER_GEOCODING_BASE_URL", "https://geocoding-api.open-meteo.com/v1/search"),
        forecast_base_url=os.getenv("WEATHER_FORECAST_BASE_URL", "https://api.open-meteo.com/v1/forecast"),
        timeout_seconds=float(os.getenv("WEATHER_HTTP_TIMEOUT_SECONDS", "5")),
    )
    try:
        return _success(service.get_current_weather(location))
    except WeatherToolError as exc:
        return _error(exc.code, exc.message)


@mcp.tool(structured_output=False)
def rag_search(
    query: str,
    facets: list[str] | None = None,
    top_k: int | None = None,
    site: str | None = None,
    ctx: Context | None = None,
) -> CallToolResult:
    """按已验证任务句检索学生手册：BM25 与 ChromaDB 双路召回、RRF 融合、去重和一次相关性重排。"""
    from app.chat_tools.bootstrap import bootstrap_readonly_dependencies, bootstrap_report

    report = bootstrap_report() or bootstrap_readonly_dependencies()
    if not report.ready:
        return _error(report.error_code or "ACTIVE_INDEX_UNAVAILABLE", "本地知识索引尚未就绪")
    collector = None
    meta = getattr(getattr(ctx, "request_context", None), "meta", None) if ctx is not None else None
    request_id = str(_meta_value(meta, "requestId") or "mcp-rag")
    tool_call_id = str(_meta_value(meta, "toolCallId") or "")
    attempt = int(_meta_value(meta, "attempt") or 1)
    generation = int(_meta_value(meta, "serverGeneration") or 0)
    try:
        from app.services.rag_pipeline import close_rag_pipeline, get_rag_pipeline
        from app.services.turn_metrics import TurnMetricsCollector, bind_turn_metrics
        from app.services.retrieval_capture import capture_retrieval_candidates, retrieval_telemetry

        remaining_ms = _meta_value(meta, "remainingMs")
        collector = TurnMetricsCollector(request_id)
        pipeline = get_rag_pipeline()
        try:
            with bind_turn_metrics(collector), capture_retrieval_candidates() as captured:
                result = pipeline.search(
                    query=query,
                    facets=facets,
                    top_k=top_k if top_k is not None else int(pipeline.settings.rag_final_top_k),
                    site=site,
                    remaining_seconds=(float(remaining_ms) / 1000.0 if remaining_ms is not None else None),
                )
        finally:
            close_rag_pipeline(pipeline)
    except Exception:
        return _error("RAG_UNAVAILABLE", "本地知识检索暂时不可用")
    if isinstance(result, dict) and result.get("error"):
        error = result["error"]
        return _error(str(error.get("code") or "RAG_UNAVAILABLE"), str(error.get("message") or "本地知识检索失败"))
    collector.mark_turn_finished("COMPLETED")
    metrics = collector.as_dict()
    telemetry = {
        "schemaVersion": 1,
        "toolCallId": tool_call_id,
        "attempt": attempt,
        "serverGeneration": generation,
        "status": "COMPLETE",
        "calls": metrics.get("calls", []),
        "stageTimings": {},
        "tokenUsageCoverage": metrics.get("tokenUsage", {}).get("tokenUsageCoverage", "UNAVAILABLE"),
        "retrieval": retrieval_telemetry(captured),
    }
    return _success(result, telemetry)


mcp._tool_manager._tools["get_current_weather"].parameters = {
    "type": "object",
    "properties": {
        "location": {"type": "string", "minLength": 1, "maxLength": 80},
    },
    "required": ["location"],
    "additionalProperties": False,
}
mcp._tool_manager._tools["rag_search"].parameters = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 2, "maxLength": 500},
        "site": {"type": ["string", "null"], "maxLength": 64},
    },
    "required": ["query"],
    "additionalProperties": False,
}


# 扩展工具仅供独立开发验证；默认不加载，不改变现有 Agent 工具权限。
if os.getenv("MINDBRIDGE_EXTENSION_TOOLS_ENABLED", "").strip().lower() == "true":
    from app.chat_tools.extensions import register_extension_tools

    register_extension_tools(mcp)


def _meta_value(meta, name: str):
    if meta is None:
        return None
    if isinstance(meta, dict):
        return meta.get(name)
    return getattr(meta, name, None)


if __name__ == "__main__":
    import logging

    from app.chat_tools.bootstrap import bootstrap_readonly_dependencies

    logging.basicConfig(level=logging.INFO)
    bootstrap_readonly_dependencies()
    mcp.run(transport="stdio")
