"""One-pass fictional probes; no formal dataset, corpus writes or score tuning."""
from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
import json
from pathlib import Path

import httpx

from app.core.config import get_settings
from app.services.agent_models import AgentModelRegistry
from app.services.ai import StructuredCompletionOptions
from app.services.evidence_contract import capture_evidence_diagnostics
from app.services.intent_prompts import build_intent_prompt, UNDERSTANDING_SCHEMA_NAME
from app.services.routing_v5 import PlanningResultV6, validate_planning_result_v6
from app.services.turn_metrics import TurnMetricsCollector, bind_turn_metrics
from scripts.probe_context_workitems_followup import run_case


OUTPUT = Path("target/verification/smoke-followup-fixes-20260918/model-probes.json")
CASES = [
    {"id": "fictional-scoped-contact-with-layout", "intent": "CAMPUS",
     "question": "晨星学院的学生服务办公室电话是多少？",
     "evidence": "晨星学院学生服务办公室\n适用范围：北湖校区\n电话：76543210\n其他校区电话未在本通知公布。",
     "required": ["76543210", "北湖校区"]},
    {"id": "fictional-layout-approval", "intent": "CAMPUS",
     "question": "晨星学院社团场地申请要经过谁审核？",
     "evidence": "晨星学院社团场地申请须由所在学\n院审核后提交学生事务中心。",
     "required": ["学院", "学生事务中心"]},
]


def main():
    if OUTPUT.exists():
        raise RuntimeError("探针输出已存在，禁止覆盖或重跑")
    settings = get_settings()
    registry = AgentModelRegistry(settings)
    tags = httpx.get(settings.ollama_base_url.rstrip("/") + "/api/tags", timeout=10).json()
    report = {"startedAt": datetime.now(UTC).isoformat(),
              "scope": "独立合成 Planner / Specialist / Response；RAG 为固定合成工具，不是正式 E2E 或 Safety 验收",
              "modelDigests": {row["name"]: row["digest"] for row in tags["models"]},
              "profiles": {name: asdict(registry.profile_for(name)) for name in
                           ("UnderstandingAgent", "CampusAffairsAgent", "ResponseAgent")}, "cases": []}
    def save(row):
        report["cases"].append(row)
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps({"id": row["id"], "passed": row["passed"]}, ensure_ascii=False), flush=True)
    collector = TurnMetricsCollector("fictional-planner-object")
    try:
        question = "晨星学院助学金资格对成绩和体测有哪些要求，什么情形不能申请？"
        with bind_turn_metrics(collector):
            completion = registry.client_for("UnderstandingAgent").complete_structured(
                build_intent_prompt({}, question), response_model=PlanningResultV6,
                schema_name=UNDERSTANDING_SCHEMA_NAME,
                options=StructuredCompletionOptions(temperature=0.0,
                    max_tokens=registry.profile_for("UnderstandingAgent").max_tokens, repair_attempts=0),
                purpose="probe.business_object")
        value = completion.value
        validate_planning_result_v6(value, {"current:0"})
        passed = len(value.workItems) == 1 and value.workItems[0].intent == "CAMPUS"
        save({"id": "fictional-planner-object", "passed": passed, "output": value.model_dump(mode="json"),
              "metrics": collector.as_dict(), "metadata": asdict(completion.metadata)})
    except Exception as exc:
        save({"id": "fictional-planner-object", "passed": False, "error": type(exc).__name__,
              "metrics": collector.as_dict()})
    for case in CASES:
        collector = TurnMetricsCollector(case["id"])
        try:
            with bind_turn_metrics(collector), capture_evidence_diagnostics() as diagnostics:
                row = run_case(case, settings, registry)
            row["checks"]["noScopeContractError"] = row["specialist"]["reasonCode"] != "EVIDENCE_SCOPE_INVALID"
            row["passed"] = all(row["checks"].values())
            row["metrics"] = collector.as_dict()
            row["evidenceDiagnostics"] = diagnostics
        except Exception as exc:
            row = {"id": case["id"], "passed": False, "error": type(exc).__name__, "metrics": collector.as_dict()}
        save(row)
    report["completedAt"] = datetime.now(UTC).isoformat()
    report["passed"] = all(row["passed"] for row in report["cases"])
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
