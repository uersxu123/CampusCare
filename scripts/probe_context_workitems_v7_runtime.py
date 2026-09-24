from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import Settings
from app.core.database import SessionLocal
from app.evaluation.contracts import EndToEndCase
from app.evaluation.runtime.adapter import EvaluationRuntimeAdapter


async def _run() -> dict:
    settings = Settings(_env_file=None).model_copy(update={
        "memory_worker_enabled": False,
        "tool_queue_enabled": False,
    })
    case = EndToEndCase.model_validate({
        "id": "synthetic-context-workitems-v7-runtime",
        "turns": ["考试课程是高等数学，考试日期是 2026 年 9 月 21 日，每天能学两小时，请安排三天复习计划，不要查询学校政策。"],
        "expected_action": "ANSWER",
        "reference": "应按三天、每天两小时给出可执行复习计划，且不需要校方政策检索。",
        "expectedTools": {"required": [], "forbidden": ["rag_search", "read_tool_evidence"]},
        "tags": ["synthetic", "context-workitems-v7"],
    })
    source_db = SessionLocal()
    try:
        adapter = EvaluationRuntimeAdapter(
            settings,
            source_db=source_db,
            require_hybrid_retrieval=True,
            isolation_mode="mysql_isolated",
            storage_admin_url="mysql+pymysql://root:root@127.0.0.1:13306/mysql?charset=utf8mb4",
        )
        outcomes = await adapter.run_case(case)
    finally:
        source_db.close()
    outcome = outcomes[-1]
    conditions = {
        "oneOutcome": len(outcomes) == 1,
        "noRuntimeError": outcome.error_code is None,
        "naturalResponse": len(outcome.response.strip()) >= 20,
        "academicRoute": outcome.route.get("primaryIntent") == "ACADEMIC",
        "noKnowledgeTool": "rag_search" not in outcome.actual_tools,
        "noEvidenceRead": "read_tool_evidence" not in outcome.actual_tools,
    }
    return {
        "schemaVersion": 1,
        "probeVersion": "context-workitems-v7-runtime",
        "runAt": datetime.now(UTC).isoformat(),
        "passed": all(conditions.values()),
        "conditions": conditions,
        "outcome": {
            "caseId": outcome.case_id,
            "response": outcome.response,
            "route": outcome.route,
            "riskLevel": outcome.risk_level,
            "action": outcome.action,
            "actualTools": outcome.actual_tools,
            "errorCode": outcome.error_code,
            "businessStatus": outcome.business_status,
            "upstreamErrorCodes": outcome.upstream_error_codes,
            "workItemOutcomes": outcome.work_item_outcomes,
            "toolDiagnostics": outcome.tool_diagnostics,
            "promptContextIds": outcome.prompt_context_ids,
            "turnMetrics": outcome.turn_metrics,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Context WorkItems V7 隔离全链路合成探针")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = asyncio.run(_run())
    except Exception as exc:
        report = {
            "schemaVersion": 1,
            "probeVersion": "context-workitems-v7-runtime",
            "runAt": datetime.now(UTC).isoformat(),
            "passed": False,
            "errorType": type(exc).__name__,
            "error": str(exc),
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(args.output)
    print(json.dumps({"passed": report.get("passed"), "conditions": report.get("conditions")}, ensure_ascii=False))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
