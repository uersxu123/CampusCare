from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from app.agents.autonomous import (ResponseAgent, SpecialistAgent, _validated_response_updates,
                                  _validated_model_evidence_notes, _answer_contract_error)
from app.core.config import Settings
from app.evaluation.config import EvaluationSettings
from app.evaluation.judges.business import BusinessJudgeOutput
from app.evaluation.judges.deepseek import DeepSeekJudge
from app.schemas.dtos import AiMessage
from app.services.agent_loop import AgentLoop
from app.services.agent_models import AgentModelRegistry
from app.services.ai import StructuredCompletionOptions
from app.services.chat_tool_runtime import isolated_chat_tool_runtime
from app.services.intent_prompts import UNDERSTANDING_SCHEMA_NAME, build_intent_prompt
from app.services.knowledge import CandidateSearchResult, KnowledgeSearchResult
from app.services.rag_pipeline import RagPipeline
from app.services.routing_v5 import PlanningResultV6, validate_planning_result_v6
from app.services.tool_models import AiToolDefinition, ToolResult
from app.services.tool_result_store import ToolResultStore


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    return value


def _metadata(value: Any) -> dict[str, Any]:
    return _jsonable(value) if value is not None else {}


class CapturingClient:
    def __init__(self, client):
        self.client = client
        self.calls: list[dict[str, Any]] = []

    def complete_with_tools(self, *args, **kwargs):
        started = time.monotonic()
        completion = self.client.complete_with_tools(*args, **kwargs)
        self.calls.append({
            "kind": "tools",
            "elapsedMs": int((time.monotonic() - started) * 1000),
            "metadata": _metadata(completion.metadata),
            "toolCallCount": len(completion.tool_calls),
        })
        return completion

    def complete_structured(self, *args, **kwargs):
        started = time.monotonic()
        completion = self.client.complete_structured(*args, **kwargs)
        self.calls.append({
            "kind": "structured",
            "elapsedMs": int((time.monotonic() - started) * 1000),
            "metadata": _metadata(completion.metadata),
            "repairCount": completion.repair_count,
        })
        return completion


class SyntheticEvidenceExecutor:
    def __init__(self):
        self.result_store = ToolResultStore()

    def execute(self, *, tool_name: str, arguments: dict, **_kwargs) -> ToolResult:
        if not tool_name.endswith("read_tool_evidence"):
            return ToolResult(False, "TOOL_NOT_ALLOWED", error="合成探针只允许原文回读")
        if arguments.get("execution_id") != "exec-response-probe":
            return ToolResult(False, "EVIDENCE_NOT_FOUND", error="执行引用无效", dispatched=True)
        return ToolResult(
            True,
            "OK",
            data={
                "status": "OK",
                "executionId": "exec-response-probe",
                "excerpts": [{
                    "evidenceId": "ev-approval",
                    "text": "申请材料经学院审核签字后，提交学生工作处审批。",
                }],
            },
            execution_id="exec-response-probe",
            raw_hash="sha256:response-probe",
            dispatched=True,
        )


class SyntheticKnowledge:
    def __init__(self):
        self.rows = tuple(
            KnowledgeSearchResult(index, "合成学生手册", content, 1.0, source_key=f"synthetic-{index}")
            for index, content in enumerate((
                "申请人应提交申请表。",
                "奖学金资金通常在公示完成后发放。",
                "本段介绍图书借阅期限。",
                "本段与奖学金申请无关。",
            ), start=1)
        )

    def search_candidates(self, **_kwargs) -> CandidateSearchResult:
        return CandidateSearchResult(self.rows, tuple(reversed(self.rows)), self.rows, {
            "retrievalMode": "hybrid",
            "bm25Degraded": False,
            "vectorDegraded": False,
            "activeCollection": "synthetic",
            "indexVersion": "synthetic-v1",
        })

    def expand_context(self, child):
        return child


def _run_probe(name: str, function, report: dict[str, Any]) -> None:
    started = time.monotonic()
    try:
        details = function()
        report["probes"].append({
            "name": name,
            "passed": bool(details.pop("passed", True)),
            "elapsedMs": int((time.monotonic() - started) * 1000),
            **_jsonable(details),
        })
    except Exception as exc:
        report["probes"].append({
            "name": name,
            "passed": False,
            "elapsedMs": int((time.monotonic() - started) * 1000),
            "errorType": type(exc).__name__,
            "error": str(exc),
        })


def main() -> int:
    parser = argparse.ArgumentParser(description="Context WorkItems V7 独立合成与本机服务探针")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings(_env_file=None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "schemaVersion": 1,
        "probeVersion": "context-workitems-v7",
        "startedAt": datetime.now(UTC).isoformat(),
        "configuration": {
            "model": settings.ollama_model,
            "think": settings.ai_think,
            "plannerMaxTokens": settings.agent_model_understanding_max_tokens,
            "specialistMaxTokens": settings.agent_model_specialist_max_tokens,
            "responseMaxTokens": settings.agent_model_response_max_tokens,
            "numCtx": settings.ollama_num_ctx,
            "inputMaxTokens": settings.context_input_max_tokens,
        },
        "probes": [],
    }
    registry = AgentModelRegistry(settings)

    def environment_probe():
        tags = httpx.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags", timeout=10).json()
        models = sorted(str(item.get("name") or "") for item in tags.get("models", []))
        required = {"qwen3:8b", "bge-m3:latest"}
        return {"passed": required.issubset(models), "ollamaModels": models, "requiredModels": sorted(required)}

    def planner_probe():
        client = CapturingClient(registry.client_for("UnderstandingAgent"))
        text = (
            "请先核对本科生国家奖学金的申请资格，但不要推断我已经符合；"
            "再根据核对结果列出申请材料清单；另外给我安排未来三天每天两小时的复习计划；"
            "最后帮我写一句礼貌询问辅导员截止日期的话，其中材料清单依赖资格核对。"
        )
        completion = client.complete_structured(
            build_intent_prompt({}, text),
            response_model=PlanningResultV6,
            schema_name=UNDERSTANDING_SCHEMA_NAME,
            options=StructuredCompletionOptions(
                temperature=settings.agent_model_understanding_temperature,
                max_tokens=settings.agent_model_understanding_max_tokens,
                repair_attempts=0,
                timeout_seconds=settings.agent_loop_deadline_seconds,
            ),
            purpose="probe.context_workitems_v7.planner_four_items",
        )
        validate_planning_result_v6(completion.value, {"current:0"})
        items = completion.value.workItems
        conditions = {
            "fourItems": len(items) == 4,
            "hasNegation": any("不要推断" in item.taskText for item in items),
            "hasDependency": any(item.dependsOn for item in items),
            "allHaveSources": all(item.sourceRefs for item in items),
        }
        return {"passed": all(conditions.values()), "conditions": conditions,
                "output": completion.value.model_dump(mode="json"), "calls": client.calls}

    def specialist_probe():
        client = CapturingClient(registry.client_for("CampusAffairsAgent"))
        tool = AiToolDefinition(
            "context_readonly__read_tool_evidence",
            "读取已授权原文；当前证据已完整可见时不要调用。",
            {"type": "object", "properties": {"execution_id": {"type": "string"}}, "required": ["execution_id"]},
        )
        messages = [
            AiMessage(role="system", content=(
                "你是 Specialist 合同探针。只使用给定证据，当前证据已完整可见，不调用工具。"
                "只输出严格 JSON：answerBrief、answerStatus、evidenceNotes、missingInfo。"
                "材料有证据，截止日期没有证据，因此必须是 PARTIAL；quote 必须逐字来自证据。"
            )),
            AiMessage(role="user", content=json.dumps({
                "taskText": "确认申请材料和本学年截止日期",
                "evidence": [{"evidenceId": "ev-material", "content": "申请人应提交申请表和成绩单。"}],
            }, ensure_ascii=False)),
        ]

        class NoDispatch:
            def execute(self, **_kwargs):
                return ToolResult(False, "UNEXPECTED_TOOL_CALL", error="已有完整证据", dispatched=True)

        result = AgentLoop(
            client=client,
            executor=NoDispatch(),
            max_model_rounds=1,
            max_tool_calls=0,
            output_max_tokens=settings.agent_model_specialist_max_tokens,
        ).run(agent_name="CampusAffairsAgent", messages=messages, tools=[tool])
        natural_error = ""
        try:
            contract = SpecialistAgent.AnswerContract.model_validate_json(result.content)
        except Exception as exc:
            natural_error = str(exc)
            finalized = client.complete_structured(
                [
                    AiMessage(role="system", content=(
                        "这是有界 FINALIZE 轮。不得调用工具或增加事实。只输出严格 JSON："
                        "answerBrief、answerStatus、evidenceNotes、missingInfo。材料有证据、截止日期未知，"
                        "所以为 PARTIAL；quote 必须逐字来自 actualVisibleEvidence。"
                    )),
                    AiMessage(role="user", content=json.dumps({
                        "draftAnswer": result.content,
                        "actualVisibleEvidence": [{
                            "evidenceId": "ev-material",
                            "text": "申请人应提交申请表和成绩单。",
                        }],
                    }, ensure_ascii=False)),
                ],
                response_model=SpecialistAgent.AnswerContract,
                schema_name="specialist_answer_contract_v3",
                options=StructuredCompletionOptions(
                    temperature=0.0,
                    max_tokens=settings.agent_model_specialist_max_tokens,
                    repair_attempts=0,
                    timeout_seconds=settings.agent_loop_deadline_seconds,
                ),
                purpose="probe.context_workitems_v7.specialist_finalize",
            )
            contract = finalized.value
        notes = _validated_model_evidence_notes(contract, [{
            "evidenceId": "ev-material", "content": "申请人应提交申请表和成绩单。",
        }])
        quote_ok = bool(notes) and _answer_contract_error(contract, True, notes) is None
        conditions = {
            "completed": result.stop_reason == "COMPLETED",
            "partial": contract.answerStatus == "PARTIAL",
            "knownAndMissing": bool(contract.evidenceNotes) and bool(contract.missingInfo),
            "quoteLocated": quote_ok,
            "boundedCalls": len(client.calls) <= 2,
            "naturalOrFinalize": len(client.calls) == 1 or bool(natural_error),
        }
        return {"passed": all(conditions.values()), "conditions": conditions,
                "naturalOutput": result.content, "naturalContractError": natural_error,
                "output": contract.model_dump(mode="json"), "calls": client.calls}

    def response_reread_probe():
        client = CapturingClient(registry.client_for("ResponseAgent"))
        tool = AiToolDefinition(
            "context_readonly__read_tool_evidence",
            "读取指定执行引用中的证据原文。",
            {"type": "object", "properties": {
                "execution_id": {"type": "string"},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "focus": {"type": "string"},
            }, "required": ["execution_id", "evidence_ids", "focus"], "additionalProperties": False},
        )
        messages = [
            AiMessage(role="system", content=(
                "你是 Response 回读探针。要合成两个工作项：复习计划和申请流程。"
                "申请流程缺少审批去向，必须先调用 read_tool_evidence，execution_id=exec-response-probe，"
                "evidence_ids=[ev-approval]，focus=审批去向；读到后再给出最终中文回答，不泄露内部结构。"
            )),
            AiMessage(role="user", content=json.dumps({
                "workItems": [
                    {"answerBrief": "每天复习两小时，先做错题。", "answerStatus": "NOT_REQUIRED"},
                    {"answerBrief": "申请表需学院审核。", "answerStatus": "PARTIAL",
                     "missingInfo": ["学院审核后的审批去向"], "executionId": "exec-response-probe"},
                ]
            }, ensure_ascii=False)),
        ]
        result = AgentLoop(
            client=client,
            executor=SyntheticEvidenceExecutor(),
            max_model_rounds=3,
            max_tool_calls=2,
            max_calls_per_tool=2,
            output_max_tokens=settings.agent_model_response_max_tokens,
        ).run(agent_name="ResponseAgent", messages=messages, tools=[tool])
        read_evidence = [{
            "evidenceId": "ev-approval",
            "content": "申请材料经学院审核签字后，提交学生工作处审批。",
        }]
        states = [
            {"workItemId": "wi-plan", "answerStatus": "NOT_REQUIRED", "missingInfo": [], "evidenceRefs": []},
            {"workItemId": "wi-apply", "answerStatus": "PARTIAL", "missingInfo": ["学院审核后的审批去向"],
             "evidenceRefs": []},
        ]
        finalized = client.complete_structured(
            [
                AiMessage(role="system", content=(
                    "这是 Response 最后结构化封装轮，不得调用工具或添加新事实。"
                    "answerText 保留两个工作项的自然语言答案；workItemUpdates 必须把 wi-apply 更新为 FULL、"
                    "missingInfo 置空并引用 ev-approval；不要更新 wi-plan。"
                )),
                AiMessage(role="user", content=json.dumps({
                    "draftAnswer": result.content,
                    "workItemStates": states,
                    "actualReadEvidence": read_evidence,
                }, ensure_ascii=False)),
            ],
            response_model=ResponseAgent.FinalContract,
            schema_name="response_final_contract_v1",
            options=StructuredCompletionOptions(
                temperature=0.0,
                max_tokens=settings.agent_model_response_max_tokens,
                repair_attempts=0,
                timeout_seconds=settings.agent_loop_deadline_seconds,
            ),
            purpose="probe.context_workitems_v7.response_finalize",
        )
        updates = _validated_response_updates(finalized.value.workItemUpdates, states, read_evidence)
        conditions = {
            "completed": result.stop_reason == "COMPLETED",
            "readOnce": len(result.tool_results) == 1 and result.tool_results[0][0].name.endswith("read_tool_evidence"),
            "usesExcerpt": "学生工作处" in finalized.value.answerText,
            "coversBoth": "复习" in finalized.value.answerText and "申请" in finalized.value.answerText,
            "hasFinalRound": result.model_rounds >= 2,
            "structuredUpdate": len(updates) == 1 and updates[0]["answerStatus"] == "FULL",
            "threeCallsTotal": len(client.calls) == 3,
        }
        return {"passed": all(conditions.values()), "conditions": conditions,
                "naturalOutput": result.content, "output": finalized.value.answerText,
                "workItemUpdates": updates, "modelRounds": result.model_rounds,
                "calls": client.calls, "callDetails": result.call_details}

    def rerank_probe():
        client = CapturingClient(registry.client_for("CampusAffairsAgent"))
        result = RagPipeline(knowledge=SyntheticKnowledge(), client=client, settings=settings).search(
            query="奖学金申请材料和截止日期是什么？", top_k=4, remaining_seconds=60,
        )
        diagnostics = result.get("diagnostics", {})
        assessments = diagnostics.get("rerankCandidateFacetMatches", [])
        ids = [str(item.get("evidenceId") or "") for item in assessments]
        conditions = {
            "singleQuery": result.get("executedQueries") == ["奖学金申请材料和截止日期是什么？"],
            "allIdsOnce": len(ids) == 4 and len(set(ids)) == 4,
            "notDegraded": diagnostics.get("rerankDegraded") is False,
            "singleModelCall": len(client.calls) == 1,
        }
        return {"passed": all(conditions.values()), "conditions": conditions,
                "assessmentIds": ids, "diagnostics": diagnostics, "calls": client.calls}

    def persistence_probe():
        store = ToolResultStore()
        raw = {"items": [{"evidenceId": "ev-large", "content": "完整原文" * 5000}]}
        store.persist("exec-large-probe", raw, persist_reason="LARGE_RESULT")
        reread = store.read("exec-large-probe", evidence_ids=["ev-large"])
        conditions = {
            "stored": len(store) == 1,
            "exactReread": reread.get("excerpts", [{}])[0].get("text") == raw["items"][0]["content"],
        }
        return {"passed": all(conditions.values()), "conditions": conditions,
                "rawChars": len(raw["items"][0]["content"]), "readStatus": reread.get("status")}

    def live_hybrid_probe():
        with isolated_chat_tool_runtime(settings) as bundle:
            definition = next(
                item for item in bundle.registry.definitions_for_agent("CampusAffairsAgent")
                if item.name.endswith("rag_search")
            )
            result = bundle.executor.execute(
                agent_name="CampusAffairsAgent",
                tool_name=definition.name,
                arguments={"query": "本科生休学申请怎么办理？", "top_k": 5},
                remaining_seconds=80,
                tool_call_id="probe-live-hybrid",
            )
            data = result.data if isinstance(result.data, dict) else {}
            diagnostics = data.get("diagnostics") if isinstance(data.get("diagnostics"), dict) else {}
            conditions = {
                "ok": result.ok,
                "hasEvidence": bool(data.get("items")),
                "bm25": not bool(diagnostics.get("bm25Degraded")),
                "vector": not bool(diagnostics.get("vectorDegraded")),
                "collection": bool(diagnostics.get("activeCollection")),
                "index": bool(diagnostics.get("indexVersion")),
                "v7": diagnostics.get("pipelineVersion") == "context-workitems-v7",
            }
            return {"passed": all(conditions.values()), "conditions": conditions,
                    "status": data.get("status"), "evidenceIds": [item.get("evidenceId") for item in data.get("items", [])],
                    "diagnostics": diagnostics, "toolCode": result.code}

    def judge_probe():
        eval_settings = EvaluationSettings(
            _env_file=None,
            judge_provider="ollama",
            judge_base_url=settings.ollama_base_url,
            judge_model="qwen3:8b",
            judge_max_retries=0,
            judge_allow_prompt_fallback=False,
            judge_timeout_seconds=120,
        )
        judge = DeepSeekJudge(settings.model_copy(update={"ai_think": False}), eval_settings)
        client = CapturingClient(judge.client)
        judge.client = client
        messages = [
            AiMessage(role="system", content="你是独立评测 Judge。根据给定答案输出严格评分 JSON。"),
            AiMessage(role="user", content=json.dumps({
                "question": "申请材料是什么？",
                "answer": "需要申请表。",
                "evidence": "申请人应提交申请表。",
                "expectedAction": "ANSWER",
            }, ensure_ascii=False)),
        ]
        result = judge._judge_once(messages, StructuredCompletionOptions(
            temperature=0.0, max_tokens=eval_settings.judge_max_tokens,
            repair_attempts=0, timeout_seconds=eval_settings.judge_timeout_seconds,
        ))
        conditions = {
            "singleCall": len(client.calls) == 1,
            "noRepair": result.repair_count == 0,
            "structured": result.structured_output_mode == "json_schema",
            "scorable": result.error_code is None and result.output is not None,
        }
        return {"passed": all(conditions.values()), "conditions": conditions,
                "result": _jsonable(result), "calls": client.calls}

    for name, function in (
        ("environment", environment_probe),
        ("large_result_persistence", persistence_probe),
        ("planner_four_work_items", planner_probe),
        ("specialist_known_and_missing", specialist_probe),
        ("response_controlled_reread", response_reread_probe),
        ("rerank_every_candidate_once", rerank_probe),
        ("live_mcp_hybrid_retrieval", live_hybrid_probe),
        ("judge_single_structured_call", judge_probe),
    ):
        _run_probe(name, function, report)

    report["finishedAt"] = datetime.now(UTC).isoformat()
    report["passed"] = all(item["passed"] for item in report["probes"])
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(args.output)
    print(json.dumps({item["name"]: item["passed"] for item in report["probes"]}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
