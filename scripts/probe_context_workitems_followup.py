"""Independent synthetic production-agent probes; never reads the Smoke dataset."""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from app.agents.autonomous import CampusAffairsAgent, GeneralChatAgent, ResponseAgent
from app.agents.events import AgentTask, AgentArtifact, CollaborationBlackboard
from app.agents.routing import _build_v5_normal_plan
from app.services.agent_models import AgentModelRegistry
from app.services.routing_v5 import PlanningResultV6
from app.core.config import Settings
from app.services.execution_control import ExecutionBudget, bind_execution_budget
from app.services.tool_models import AiToolDefinition, ToolResult


# Fixed, fictional fixtures. These are not official evaluation questions or Gold.
CASES = [
    {"id": "synthetic-payment-versus-application", "intent": "CAMPUS",
     "question": "星河学校旅行补助的申请截止日期是什么？", "evidence": "星河学校旅行补助于每年8月9日发放。本通知未公布申请截止日期。",
     "forbidden": ["申请截止日期是8月9日", "申请截止日期为8月9日"], "needsGap": True},
    {"id": "synthetic-site-scope", "intent": "CAMPUS",
     "question": "星河学校北区事务办公室的联系电话是多少？", "evidence": "星河学校北区事务办公室电话：5550101。该号码仅适用于北区，南区号码尚未公布。",
     "required": ["5550101", "北区"]},
    {"id": "synthetic-known-and-missing", "intent": "CAMPUS",
     "question": "星河学校活动场地申请需要哪些材料，几个工作日可以办结？", "evidence": "星河学校活动场地申请须提交活动说明和负责人签字。本材料未规定办结工作日数。",
     "required": ["活动说明", "签字"], "needsGap": True},
    {"id": "synthetic-translation-no-tools", "intent": "CHAT",
     "question": "把‘清晨的花园’翻译成英文，只翻译。", "evidence": "", "noTools": True},
]


def run_case(case, settings, registry):
    planning = PlanningResultV6.model_validate({"schemaVersion": 6, "workItems": [{
        "intent": case["intent"], "objective": case["question"], "taskText": case["question"],
        "sourceRefs": ["current:0"], "contextRefs": [], "dependsOn": [],
    }]})
    plan = _build_v5_normal_plan(case["question"], {}, planning)
    board = CollaborationBlackboard(turn_id=case["id"], user_input=case["question"])
    board = board.add_artifact(AgentArtifact(id="route", owner="synthetic", kind="route_plan", payload=plan.as_payload()))
    calls = []
    def definitions(name):
        if name == "GeneralChatAgent":
            return [AiToolDefinition("chat_readonly__get_current_weather", "获取天气", {"type": "object"})]
        return [AiToolDefinition("chat_readonly__rag_search", "检索给定任务的政策原文", {
            "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False,
        })]
    class Executor:
        def execute(self, **kwargs):
            calls.append({"tool": kwargs["tool_name"], "arguments": kwargs["arguments"]})
            return ToolResult(True, "OK", data={"status": "OK", "items": [{
                "evidenceId": "synthetic-ev", "content": case["evidence"], "source": "fictional-fixture",
            }]}, dispatched=True)
    packet = SimpleNamespace(
        safety_context=None, manifest=SimpleNamespace(as_dict=lambda: {}),
        for_specialist=lambda work, deps: {"workItem": work, "dependencyResults": deps, "currentInput": case["question"]},
    )
    services = SimpleNamespace(settings=settings, model_registry=registry, context_packet=packet, skill_manager=None,
                               tool_runtime=SimpleNamespace(registry=SimpleNamespace(definitions_for_agent=definitions), executor=Executor()),
                               tool_call_details={})
    work = plan.as_payload()["workItems"][0]
    agent = GeneralChatAgent(services) if case["intent"] == "CHAT" else CampusAffairsAgent(services)
    with bind_execution_budget(ExecutionBudget.start(120)):
        result = agent.act(AgentTask(id="specialist", title="synthetic", metadata={
            "planId": plan.plan_id, "workItemId": work["workItemId"], "workItem": work, "routePlanArtifactId": "route",
        }), board)
        for artifact in result.artifacts:
            board = board.add_artifact(artifact)
        proposal = ResponseAgent(services).act(AgentTask(id="response", title="synthetic", metadata={"kind": "response"}), board).artifacts[0]
    specialist = next(a.payload for a in result.artifacts if a.kind == "specialist_result")
    answer = proposal.payload["directResponse"]
    checks = {
        "singleRag": sum(c["tool"].endswith("rag_search") for c in calls) <= 1,
        "requiredText": all(word in answer for word in case.get("required", [])),
        "forbiddenTextAbsent": all(word not in answer.replace(" ", "") for word in case.get("forbidden", [])),
        "gapPreserved": not case.get("needsGap") or specialist.get("answerStatus") in {"PARTIAL", "NONE"}
                        and bool(specialist.get("missingInfo")) and any(word in answer for word in ("未", "无法确认", "不明确", "没有", "不确定")),
        "noUnexpectedTools": not case.get("noTools") or not calls,
        "naturalAnswer": bool(answer.strip()) and not answer.lstrip().startswith('{"answerBrief"'),
        "noResponseFallback": not proposal.payload["generationDiagnostics"].get("fallbackUsed"),
        "validSpecialistContract": specialist["reasonCode"] not in {"ANSWER_CONTRACT_INVALID", "EVIDENCE_QUOTE_INVALID"},
    }
    return {"id": case["id"], "passed": all(checks.values()), "checks": checks, "calls": calls,
            "specialist": specialist, "response": answer, "diagnostics": proposal.payload["generationDiagnostics"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings()
    registry = AgentModelRegistry(settings)
    rows = []
    for case in CASES:
        try:
            row = run_case(case, settings, registry)
        except Exception as exc:
            row = {"id": case["id"], "passed": False, "error": type(exc).__name__, "detail": str(exc)}
        rows.append(row)
        print(json.dumps({"id": row["id"], "passed": row["passed"]}, ensure_ascii=False), flush=True)
    report = {"createdAt": datetime.now(UTC).isoformat(), "passed": all(r["passed"] for r in rows),
              "scope": "synthetic Specialist + Response; no database, no formal Smoke, no Safety assessment", "cases": rows}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
