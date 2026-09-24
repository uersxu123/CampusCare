from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings
from app.services.chat_tool_runtime import create_chat_tool_runtime
from app.services.mcp_runtime import create_mcp_runtime
from app.services.turn_metrics import TurnMetricsCollector, bind_turn_metrics


def main() -> int:
    parser = argparse.ArgumentParser(description="运行独立的真实 hybrid RAG 工具探针")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = get_settings()
    runtime = create_mcp_runtime(settings, strict_startup=True)
    report: dict = {
        "schemaVersion": 1,
        "startedAt": datetime.now(UTC).isoformat(),
        "query": "本科生休学申请怎么办理？",
        "facets": ["申请流程"],
        "topK": 5,
        "formalSmokeCase": False,
    }
    try:
        bundle = create_chat_tool_runtime(settings, runtime)
        collector = TurnMetricsCollector("independent-rag-probe")
        with bind_turn_metrics(collector):
            result = bundle.executor.execute(
                agent_name="AcademicPlanningAgent",
                tool_name="chat_readonly__rag_search",
                arguments={"query": report["query"], "facets": report["facets"], "top_k": 5},
                remaining_seconds=float(settings.rag_tool_call_timeout_seconds),
                tool_call_id="independent-rag-probe-call",
            )
        collector.mark_turn_finished("COMPLETED" if result.ok else "FAILED")
        data = result.data if isinstance(result.data, dict) else {}
        diagnostics = data.get("diagnostics") if isinstance(data.get("diagnostics"), dict) else {}
        items = data.get("items") if isinstance(data.get("items"), list) else []
        report.update({
            "completedAt": datetime.now(UTC).isoformat(),
            "ok": result.ok,
            "code": result.code,
            "dispatched": result.dispatched,
            "status": data.get("status"),
            "itemCount": len(items),
            "contextIds": [str(item.get("contextId") or item.get("id") or "") for item in items if isinstance(item, dict)],
            "diagnostics": diagnostics,
            "turnMetrics": collector.as_dict(),
        })
        calls = collector.as_dict().get("calls", [])
        purposes = {str(item.get("purpose") or "") for item in calls}
        retrieval_mode = str(diagnostics.get("retrievalMode") or "").lower()
        passed = bool(
            result.ok
            and items
            and (retrieval_mode == "hybrid" or retrieval_mode.endswith("-hybrid"))
            and diagnostics.get("activeCollection")
            and diagnostics.get("indexVersion")
            and not diagnostics.get("vectorDegraded")
            and not diagnostics.get("bm25Degraded")
            and "rewriteDegraded" in diagnostics
            and "rerankDegraded" in diagnostics
            and any("rewrite" in purpose for purpose in purposes)
            and any("rerank" in purpose for purpose in purposes)
        )
        report["passed"] = passed
        report["warnings"] = (["RERANK_DEGRADED"] if diagnostics.get("rerankDegraded") else [])
        return_code = 0 if passed else 3
    except Exception as exc:
        report.update({
            "completedAt": datetime.now(UTC).isoformat(),
            "passed": False,
            "errorCode": type(exc).__name__,
            "error": str(exc),
        })
        return_code = 3
    finally:
        runtime.shutdown()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(args.output)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
