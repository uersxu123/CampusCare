from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import platform
import subprocess
import sys
import uuid
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.evaluation.config import EvaluationSettings, get_evaluation_settings
from app.evaluation.contracts import EndToEndCase
from app.evaluation.dataset import (
    DatasetError,
    dataset_descriptor,
    filter_cases,
    load_e2e_cases,
    load_routing_cases,
)
from app.evaluation.evaluators.routing import ProductionRoutingEvaluator
from app.evaluation.evaluators.end_to_end import attribute_failure
from app.evaluation.judges.deepseek import DeepSeekJudge
from app.evaluation.judges.business import BUSINESS_JUDGE_PROMPT_VERSION
from app.evaluation.judges.safety import evaluate_safety_response
from app.evaluation.reporting.baseline import load_baseline, update_baseline
from app.evaluation.reporting.fingerprint import (
    compare_corpus_fingerprints,
    fingerprint_active_corpus,
    fingerprint_from_audit,
)
from app.evaluation.reporting.writer import EvaluationReportWriter, summary_document
from app.evaluation.runtime.adapter import EvaluationRuntimeAdapter
from app.services.intent_fusion import FUSION_CONSTANTS_VERSION
from app.services.intent_prompts import INTENT_PROMPT_VERSION, UNDERSTANDING_SCHEMA_NAME


QUALITY_FAILURE = 1
CONFIG_FAILURE = 2
INFRA_FAILURE = 3


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行 CampusCare 分层端到端评测")
    parser.add_argument(
        "--suite",
        choices=("routing", "retrieval", "e2e", "ragas", "e2e-ragas", "all", "release"),
        required=True,
    )
    parser.add_argument("--profile", choices=("contract", "smoke", "full"), default=None)
    parser.add_argument("--case")
    parser.add_argument("--tag")
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--safety-dataset", type=Path)
    parser.add_argument("--corpus-audit", type=Path)
    parser.add_argument("--isolation-mode", choices=("isolated", "mysql_isolated", "continuous"), default=None)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-workers", type=int)
    parser.add_argument("--no-gate", action="store_true")
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--resume")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_evaluation_settings()
    profile = args.profile or ("full" if args.suite == "release" else "smoke" if args.suite == "all" else settings.profile)
    run_id = args.resume or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    output_dir = _validated_output_dir(args.output, settings)
    writer = EvaluationReportWriter(output_dir, run_id)
    try:
        result = asyncio.run(_run(args, settings, writer, run_id, profile))
        quality_passed = bool(result.get("passed"))
        summary = summary_document(
            run_id=run_id,
            suite=args.suite,
            profile=profile,
            passed=quality_passed,
            **{key: value for key, value in result.items() if key != "passed"},
        )
        identity = _baseline_identity(summary)
        baseline = load_baseline(settings.resolve(settings.baseline_dir), identity)
        if baseline is None:
            summary["baselineStatus"] = "CANDIDATE"
        elif baseline.get("status") == "STALE":
            summary["baselineStatus"] = "STALE"
            summary["passed"] = False
            summary["regressions"] = [{"metric": "baseline", "reason": "BASELINE_STALE"}]
        else:
            summary["baselineStatus"] = "MATCHED"
            regressions = _compare_baseline_metrics(summary, baseline.get("summary", {}))
            summary["regressions"] = regressions
            summary["passed"] = summary["passed"] and not regressions
        writer.write_json("summary.json", summary)
        if args.update_baseline and summary["passed"]:
            update_baseline(settings.resolve(settings.baseline_dir), identity, summary)
        print(f"Evaluation report: {writer.run_dir / 'summary.json'}")
        return 0 if args.no_gate or summary["passed"] else QUALITY_FAILURE
    except DatasetError as exc:
        _write_error(writer, run_id, args.suite, profile, "DATASET_ERROR", str(exc))
        print(str(exc), file=sys.stderr)
        return CONFIG_FAILURE
    except (ValueError, FileNotFoundError) as exc:
        _write_error(writer, run_id, args.suite, profile, "CONFIG_ERROR", str(exc))
        print(str(exc), file=sys.stderr)
        return CONFIG_FAILURE
    except KeyboardInterrupt:
        writer.append_journal({"phase": "run", "status": "ABORTED", "errorCode": "KEYBOARD_INTERRUPT", "coverage": "PARTIAL"}, fsync=True)
        writer.write_json("summary.json", summary_document(
            run_id=run_id,
            suite=args.suite,
            profile=profile,
            passed=False,
            status="ABORTED",
            execution={"planned": None, "started": None, "finished": None, "aborted": 1, "notRun": None},
        ))
        return 130
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        code = "HYBRID_RETRIEVAL_REQUIRED" if "HYBRID_RETRIEVAL_REQUIRED" in str(exc) else "INFRA_ERROR"
        _write_error(writer, run_id, args.suite, profile, code, message)
        print(message, file=sys.stderr)
        return INFRA_FAILURE


async def _run(args, settings: EvaluationSettings, writer: EvaluationReportWriter, run_id: str, profile: str) -> dict:
    suites = _suite_members(args.suite)
    reports = {}
    datasets = []
    if "routing" in suites:
        import os
        from app.services.agent_models import AgentModelRegistry
        from app.services.embedding import create_embedding_backend, embedding_identity
        from app.services.understanding import UnderstandingService

        path = _dataset_path(args.dataset, settings, "routing-v3.jsonl", only_suite=len(suites) == 1)
        cases = filter_cases(load_routing_cases(path), case_id=args.case, tag=args.tag)
        app_settings = get_settings()
        registry = AgentModelRegistry(app_settings)
        profile_config = registry.profile_for("UnderstandingAgent")
        backend = create_embedding_backend(app_settings)
        identity = embedding_identity(backend)
        report_identity = {
            "model": {"provider": profile_config.provider, "name": profile_config.model, "digest": None},
            "embedding": identity.__dict__,
        }
        if os.getenv("RUN_ROUTING_MODEL_EVAL") != "1":
            report = {"passed": False, "status": "NOT_RUN", "metrics": {}, "metricErrors": [{"errorCode": "ROUTING_MODEL_EVAL_NOT_ENABLED"}], **report_identity}
        elif profile_config.provider not in {"openai", "ollama"} or not profile_config.model or profile_config.model.lower() in {"mock", "canned"}:
            report = {"passed": False, "status": "NOT_RUN", "metrics": {}, "metricErrors": [{"errorCode": "UNDERSTANDING_MODEL_UNAVAILABLE"}], **report_identity}
        elif not backend.available() or not identity.digest or not identity.digest_resolved:
            report = {"passed": False, "status": "NOT_RUN", "metrics": {}, "metricErrors": [{"errorCode": "EMBEDDING_BACKEND_UNAVAILABLE"}], **report_identity}
        else:
            service = UnderstandingService(registry.client_for("UnderstandingAgent"), app_settings)
            report = ProductionRoutingEvaluator(app_settings, service).evaluate(cases)
            report["status"] = "COMPLETED"
            report.update(report_identity)
        report["promptVersion"] = INTENT_PROMPT_VERSION
        report["schemaName"] = UNDERSTANDING_SCHEMA_NAME
        report["fusionConstantsVersion"] = FUSION_CONSTANTS_VERSION
        report["dataset"] = dataset_descriptor(path, cases)
        report["code"] = _code_identity()
        report["runAt"] = datetime.now(UTC).isoformat()
        writer.write_json("routing-report.json", report)
        reports["routing"] = report
        datasets.append(dataset_descriptor(path, cases))
    if "retrieval" in suites:
        report = _run_retrieval(profile)
        writer.write_json("retrieval-report.json", report)
        reports["retrieval"] = report
    if "e2e" in suites or "ragas" in suites:
        path = _dataset_path(args.dataset, settings, "mindbridge-e2e-ragas-v1.jsonl", only_suite=len(suites) == 1)
        base_cases = load_e2e_cases(path)
        combined_cases = list(base_cases)
        if "e2e" in suites:
            safety_path = settings.resolve(args.safety_dataset) if args.safety_dataset else settings.resolve(settings.dataset_dir) / "e2e-safety-v1.jsonl"
            combined_cases.extend(load_e2e_cases(safety_path))
        cases = filter_cases(combined_cases, case_id=args.case, tag=args.tag)
        cases = _profile_cases(cases, profile)
        base_selected = [item for item in cases if not item.id.startswith("safety-")]
        safety_selected = [item for item in cases if item.id.startswith("safety-")]
        datasets = [dataset_descriptor(path, base_selected)] if base_selected else []
        if safety_selected:
            safety_path = settings.resolve(args.safety_dataset) if args.safety_dataset else settings.resolve(settings.dataset_dir) / "e2e-safety-v1.jsonl"
            datasets.append(dataset_descriptor(safety_path, safety_selected))
        _validate_dataset_contract(cases)
        needs_corpus = any(
            bool(item.reference_context_ids) or "rag_search" in item.expected_tools.required
            for item in cases
        )
        needs_ragas = "ragas" in suites and any(
            item.expected_action != "SAFETY_BYPASS" for item in cases
        )
        if needs_ragas:
            from app.evaluation.ragas_eval.factories import require_ragas

            require_ragas()
        needs_business_judge = any(item.expected_action != "SAFETY_BYPASS" for item in cases) and "e2e" in suites
        if needs_business_judge and settings.judge_provider != "ollama" and not settings.judge_api_key.get_secret_value():
            raise RuntimeError("EVAL_JUDGE_API_KEY 为空，存在普通 e2e case 时必须 fail closed")
        source_db = SessionLocal() if needs_corpus else None
        try:
            audit_path = settings.resolve(args.corpus_audit or settings.corpus_audit) if (args.corpus_audit or settings.corpus_audit) else settings.resolve(settings.dataset_dir) / "mindbridge-e2e-ragas-v1.audit.json"
            corpus = _corpus_check(settings, source_db, audit_path, datasets) if source_db is not None else {}
            if source_db is not None and not corpus["passed"]:
                report = {"passed": False, "corpus": corpus, "metricErrors": [{"errorCode": "CORPUS_MISMATCH"}]}
                if "e2e" in suites:
                    writer.write_json("e2e-report.json", report)
                    reports["e2e"] = report
                if "ragas" in suites:
                    writer.write_json("ragas-report.json", report)
                    reports["ragas"] = report
            else:
                if source_db is not None and settings.require_hybrid_retrieval and needs_corpus:
                    _warmup_hybrid_retrieval(source_db)
                outcomes = await _execute_cases(
                    cases,
                    EvaluationRuntimeAdapter(
                        get_settings(),
                        source_db,
                        require_hybrid_retrieval=settings.require_hybrid_retrieval and needs_corpus,
                        isolation_mode=args.isolation_mode or settings.isolation_mode,
                        storage_admin_url=settings.storage_admin_url.get_secret_value(),
                    ),
                    timeout_seconds=settings.case_timeout_seconds,
                    cancel_grace_seconds=settings.case_cancel_grace_seconds,
                    cleanup_timeout_seconds=settings.case_cleanup_timeout_seconds,
                    writer=writer,
                )
                if "e2e" in suites:
                    report = _run_business_judge(cases, outcomes, settings, corpus)
                    writer.write_json("e2e-report.json", report)
                    reports["e2e"] = report
                if "ragas" in suites:
                    from app.evaluation.ragas_eval.runner import evaluate_ragas_cases

                    ragas_cases = [item for item in cases if item.expected_action != "SAFETY_BYPASS"]
                    ragas_outcomes = [item for item in outcomes if not item.case_id.startswith("safety-")]
                    gate_errors = [
                        {"caseId": item.case_id, "errorCode": item.error_code}
                        for item in ragas_outcomes
                        if item.error_code == "HYBRID_RETRIEVAL_REQUIRED"
                    ]
                    scorable_ids = {item.case_id for item in ragas_outcomes if not item.error_code}
                    report = (
                        await evaluate_ragas_cases(
                            [item for item in ragas_cases if item.id in scorable_ids],
                            [item for item in ragas_outcomes if item.case_id in scorable_ids],
                            settings,
                        )
                        if needs_ragas
                        else {"passed": True, "status": "notApplicable", "metrics": {}, "denominators": {}, "metricErrors": []}
                    )
                    if gate_errors:
                        report["metricErrors"].extend(gate_errors)
                        report["passed"] = False
                    report["corpus"] = corpus
                    writer.write_json("ragas-report.json", report)
                    reports["ragas"] = report
        finally:
            if source_db is not None:
                source_db.close()
    passed = all(bool(item.get("passed")) for item in reports.values())
    corpus_sections = [item.get("corpus") for item in reports.values() if item.get("corpus")]
    result = {
        "passed": passed,
        "dataset": (
            datasets[0]
            if len(datasets) == 1
            else {"components": datasets, "selectedCaseCount": sum(item["caseCount"] for item in datasets)}
        ),
        "code": _code_identity(),
        "sut": _sut_identity(),
        "judge": _judge_identity(settings),
        "environment": _environment_identity(args, settings),
        "ragas": {
            "version": "0.4.3",
            "embeddingProvider": settings.ragas_embedding_provider,
            "embeddingModel": settings.ragas_embedding_model,
            "pipelineVersion": "rag-pipeline-v2",
        },
        "corpus": corpus_sections[0] if corpus_sections else {},
        "metrics": {name: item.get("metrics", {}) for name, item in reports.items()},
        "denominators": {name: item.get("denominators", {}) for name, item in reports.items()},
        "gates": {name: item.get("gates", {}) for name, item in reports.items()},
        "metricErrors": [error for item in reports.values() for error in item.get("metricErrors", [])],
        "failedCases": _failed_case_ids(reports),
        "regressions": [],
    }
    if "routing" in reports:
        routing_report = reports["routing"]
        routing_dataset = routing_report.get("dataset") or {}
        model = routing_report.get("model") or {}
        embedding = routing_report.get("embedding") or {}
        result["routingIdentity"] = {
            "datasetSha256": routing_dataset.get("sha256"),
            "promptVersion": INTENT_PROMPT_VERSION,
            "schemaName": UNDERSTANDING_SCHEMA_NAME,
            "fusionConstantsVersion": FUSION_CONSTANTS_VERSION,
            "understandingProvider": model.get("provider"),
            "understandingModel": model.get("name"),
            "embeddingProvider": embedding.get("provider"),
            "embeddingModel": embedding.get("model"),
            "embeddingDigest": embedding.get("digest"),
        }
    return result


def _warmup_hybrid_retrieval(source_db) -> None:
    """Fail before scoring when the configured ACTIVE hybrid index is not ready."""
    from app.models.entities import KnowledgeIndexRegistry
    from app.services.knowledge import KnowledgeService

    app_settings = get_settings().model_copy(update={
        "knowledge_vector_enabled": True,
        "knowledge_vector_required": True,
    })
    registry = source_db.query(KnowledgeIndexRegistry).filter(
        KnowledgeIndexRegistry.logical_name == app_settings.knowledge_vector_collection_base
    ).one_or_none()
    if registry is None or not registry.active_collection or not registry.active_signature:
        raise RuntimeError("HYBRID_RETRIEVAL_REQUIRED: ACTIVE collection/signature 不可用")
    service = KnowledgeService(source_db, app_settings)
    if service.vector_store is None or not service.embedding_backend.available():
        raise RuntimeError("HYBRID_RETRIEVAL_REQUIRED: embedding 或 ACTIVE collection 预热失败")
    service._validate_active_index()
    service.embedding_backend.embed_query("评测混合检索预热")


def _suite_members(suite: str) -> tuple[str, ...]:
    if suite == "e2e-ragas":
        return ("e2e", "ragas")
    if suite == "all":
        return ("routing", "retrieval", "e2e", "ragas")
    if suite == "release":
        return ("routing", "retrieval", "e2e", "ragas")
    return (suite,)


def _dataset_path(custom: Path | None, settings: EvaluationSettings, default: str, *, only_suite: bool) -> Path:
    path = custom if custom is not None and only_suite else settings.dataset_dir / default
    return settings.resolve(path)


def _validated_output_dir(custom: Path | None, settings: EvaluationSettings) -> Path:
    allowed = settings.resolve(settings.output_dir).resolve()
    output = settings.resolve(custom).resolve() if custom is not None else allowed
    try:
        output.relative_to(allowed)
    except ValueError as exc:
        raise ValueError(f"评测输出必须位于 {allowed}") from exc
    return output


def _profile_cases(cases: list[EndToEndCase], profile: str) -> list[EndToEndCase]:
    if profile == "full":
        return cases
    limits = {"ANSWER": 8, "PARTIAL_ANSWER": 3, "CLARIFY": 3, "ABSTAIN": 3, "SAFETY_BYPASS": 3}
    if profile == "contract":
        limits = {key: 1 for key in limits}
    selected = []
    counts: dict[str, int] = defaultdict(int)
    for case in cases:
        if counts[case.expected_action] < limits[case.expected_action]:
            selected.append(case)
            counts[case.expected_action] += 1
    return selected


def _run_retrieval(profile: str) -> dict:
    from app.rag_eval.runner import evaluate_mode, load_dataset

    app_settings = get_settings()
    cases = load_dataset(app_settings.project_root / app_settings.rag_eval_dataset)
    if profile == "contract":
        cases = cases[:5]
    db = SessionLocal()
    try:
        modes = ("bm25", "hybrid", "vector-fault")
        reports = {mode: evaluate_mode(db, app_settings, cases, mode) for mode in modes}
        return {"passed": all(item["passed"] for item in reports.values()), "modes": reports}
    finally:
        db.close()


def _corpus_check(settings: EvaluationSettings, source_db, audit: Path, datasets: list[dict]) -> dict:
    payload = json.loads(audit.read_text(encoding="utf-8-sig"))
    expected_hashes = payload.get("datasetSha256") or {}
    for descriptor in datasets:
        name = Path(descriptor["path"]).name
        if expected_hashes.get(name) != descriptor["sha256"]:
            raise DatasetError(f"corpus audit 与数据集不匹配: {name}")
    expected_count = payload.get("outputCaseCount")
    selected_count = sum(int(item.get("caseCount") or 0) for item in datasets)
    if expected_count is not None and int(expected_count) != selected_count:
        raise DatasetError(f"corpus audit caseCount 不匹配: expected={expected_count} actual={selected_count}")
    golden = fingerprint_from_audit(audit)
    runtime = fingerprint_active_corpus(
        source_db,
        manifest_path=get_settings().project_root / "app" / "knowledge" / "knowledge_manifest.yaml",
    )
    return compare_corpus_fingerprints(golden, runtime)


def _validate_dataset_contract(cases: list[EndToEndCase]) -> None:
    for case in cases:
        if case.expected_route and case.expected_route.primaryIntent.value == "RISK":
            raise DatasetError(f"{case.id}: Router V5.5 不支持 RISK 业务路由 Gold")
        if case.expected_action == "SAFETY_BYPASS":
            route = case.expected_route
            if route is None or route.riskLevel.value != "HIGH" or route.primaryIntent.value != "MENTAL":
                raise DatasetError(f"{case.id}: 安全题必须使用 MENTAL + HIGH + SAFETY_BYPASS 合同")
            if "rag_search" not in case.expected_tools.forbidden:
                raise DatasetError(f"{case.id}: 安全题必须禁止 rag_search")


async def _execute_cases(
    cases: list[EndToEndCase],
    adapter: EvaluationRuntimeAdapter,
    *,
    timeout_seconds: float | None = None,
    cancel_grace_seconds: float = 1.0,
    cleanup_timeout_seconds: float = 5.0,
    writer: EvaluationReportWriter | None = None,
):
    outcomes = []
    if timeout_seconds is not None and hasattr(adapter, "worker_spec"):
        from app.evaluation.runtime.supervisor import EvaluationCaseSupervisor

        supervisor = EvaluationCaseSupervisor(
            timeout_seconds=timeout_seconds,
            cancel_grace_seconds=cancel_grace_seconds,
            cleanup_timeout_seconds=cleanup_timeout_seconds,
        )
        for case in cases:
            if writer is not None:
                writer.append_journal({"caseId": case.id, "phase": "case", "status": "STARTED", "started": 1}, fsync=True)
            case_outcomes, _supervision = await asyncio.to_thread(supervisor.run_case, case, adapter)
            outcomes.extend(case_outcomes)
            if writer is not None:
                for outcome in case_outcomes:
                    writer.append_case(outcome.__dict__, fsync=True)
                writer.append_journal({
                    "caseId": case.id,
                    "phase": "case",
                    "status": _supervision.status,
                    "finished": 1 if _supervision.status == "COMPLETED" else 0,
                    "aborted": 0 if _supervision.status == "COMPLETED" else 1,
                    "elapsedMs": int(_supervision.elapsed_seconds * 1000),
                    "errorCode": _supervision.error_code,
                    "resourceCleanup": _supervision.resource_cleanup,
                    "providerCancellation": _supervision.provider_cancellation,
                    "coverage": "FULL" if _supervision.status == "COMPLETED" else "PARTIAL",
                }, fsync=True)
        return outcomes
    for case in cases:
        if writer is not None:
            writer.append_journal({"caseId": case.id, "phase": "case", "status": "STARTED", "started": 1}, fsync=True)
        case_outcomes = await adapter.run_case(case)
        outcomes.extend(case_outcomes)
        if writer is not None:
            for outcome in case_outcomes:
                writer.append_case(outcome.__dict__, fsync=True)
            writer.append_journal({"caseId": case.id, "phase": "case", "status": "COMPLETED", "finished": 1}, fsync=True)
    return outcomes


def _run_business_judge(cases, outcomes, settings, corpus) -> dict:
    final = {item.case_id: item for item in outcomes}
    judge = (
        DeepSeekJudge(get_settings(), settings)
        if any(case.expected_action != "SAFETY_BYPASS" for case in cases)
        else None
    )
    rows = []
    errors = []
    scores: dict[str, list[float]] = defaultdict(list)
    judge_results = []
    for case in cases:
        outcome = final.get(case.id)
        if outcome is None or outcome.error_code:
            errors.append({"caseId": case.id, "errorCode": outcome.error_code if outcome else "OUTCOME_MISSING"})
            if outcome is not None:
                rows.append({
                    "caseId": case.id,
                    "errorCode": outcome.error_code,
                    "attribution": _outcome_attribution(
                        case,
                        outcome,
                        business_passed=None,
                        safety_passed=None,
                    ),
                    "passed": False,
                })
            continue
        if case.expected_action == "SAFETY_BYPASS":
            safety = evaluate_safety_response(outcome.response)
            attribution = _outcome_attribution(case, outcome, business_passed=None, safety_passed=safety["passed"])
            rows.append({"caseId": case.id, "safety": safety, "attribution": attribution, "passed": safety["passed"]})
            if not safety["passed"]:
                errors.append({"caseId": case.id, "errorCode": "SAFETY_ERROR"})
            continue
        if judge is None:
            raise RuntimeError("Business Judge 未初始化")
        judged = judge.judge(case, outcome)
        judge_results.append(judged)
        observed_action = judged.output.observed_action if judged.output is not None else "UNRESOLVED"
        evaluated = replace(outcome, action=observed_action, action_source="JUDGE_RESPONSE")
        contract = _business_contract(case, evaluated, judged)
        attribution = _outcome_attribution(
            case,
            evaluated,
            business_passed=contract["passed"] if not judged.error_code and not contract["judgeIssues"] else None,
            safety_passed=None,
            judge_error=bool(judged.error_code or contract["judgeIssues"]),
        )
        row = {
            "caseId": case.id,
            **judged.__dict__,
            "output": judged.output.model_dump() if judged.output else None,
            "attribution": attribution,
            "observedAction": observed_action,
            "actionSource": "JUDGE_RESPONSE",
            "runtimeAction": outcome.action,
            "contract": contract,
            "passed": contract["passed"],
        }
        rows.append(row)
        if judged.error_code:
            errors.append({"caseId": case.id, "errorCode": judged.error_code})
        elif contract["judgeIssues"]:
            errors.append({"caseId": case.id, "errorCode": "JUDGE_CONTRACT_INCONSISTENT"})
        elif judged.output:
            for name in ("relevance", "accuracy", "completeness", "helpfulness", "action_correctness"):
                scores[name].append(float(getattr(judged.output, name)))
    metrics = {name: sum(values) / len(values) for name, values in scores.items() if values}
    judge_count = len(judge_results)
    metrics.update({
        "judgeErrorRate": _ratio(sum(bool(item.error_code) for item in judge_results)
                                 + sum(bool(row.get("contract", {}).get("judgeIssues")) for row in rows), judge_count),
        "judgeRetryRate": _ratio(sum(item.retry_count > 0 for item in judge_results), judge_count),
        "judgeRepairCount": sum(item.repair_count for item in judge_results),
    })
    denominators = {name: len(values) for name, values in scores.items()}
    denominators.update({
        "judgeErrorRate": judge_count,
        "judgeRetryRate": judge_count,
        "judgeRepairCount": judge_count,
    })
    observed = _observed_metrics(cases, outcomes, rows, judge_results)
    metrics.update(observed["metrics"])
    denominators.update(observed["denominators"])
    return {
        "schemaVersion": 3,
        "passed": not errors and all(row["passed"] for row in rows),
        "corpus": corpus,
        "metrics": metrics,
        "denominators": denominators,
        "metricErrors": errors,
        "results": rows,
        "execution": observed["execution"],
    }


def _failed_case_ids(reports: dict) -> list[str]:
    return list(dict.fromkeys(
        row.get("id") or row.get("caseId") or row.get("case_id")
        for report in reports.values() for row in report.get("results", [])
        if (row.get("passed") is False or row.get("error") or row.get("errorCode") or row.get("error_code"))
        and (row.get("id") or row.get("caseId") or row.get("case_id"))
    ))


def _business_contract(case, outcome, judged) -> dict:
    action_allowed = outcome.action in case.allowed_actions
    tools_satisfied = _tools_match(case, outcome) if outcome.action != "UNRESOLVED" else None
    route_matches = _route_matches(case, outcome) if case.expected_route is not None else True
    issues = []
    output = judged.output
    if output is not None:
        if not action_allowed and output.action_correctness > 0:
            issues.append("ACTION_SCORE_CONFLICT")
        if output.verdict == "PASS" and (not action_allowed or output.unsupported_claims
                                          or output.action_correctness == 0):
            issues.append("VERDICT_CONFLICT")
    return {
        "actionAllowed": action_allowed, "toolsSatisfied": tools_satisfied,
        "routeMatches": route_matches, "judgeIssues": issues,
        "passed": bool(judged.passed and action_allowed and tools_satisfied and route_matches and not issues),
    }


def _observed_metrics(cases, outcomes, rows, judge_results) -> dict:
    final = {item.case_id: item for item in outcomes}
    for row in rows:
        case_id = row.get("caseId")
        if case_id in final and row.get("observedAction"):
            final[case_id] = replace(final[case_id], action=row["observedAction"], action_source="JUDGE_RESPONSE")
    route_expected = [case for case in cases if case.expected_route is not None]
    route_observed = [case for case in route_expected if final.get(case.id) and final[case.id].route]
    route_correct = sum(_route_matches(case, final[case.id]) for case in route_observed)
    known_actions = {"ANSWER", "PARTIAL_ANSWER", "CLARIFY", "ABSTAIN", "SAFETY_BYPASS"}
    action_observed = [case for case in cases if final.get(case.id) and final[case.id].action in known_actions]
    action_correct = sum(final[case.id].action in case.allowed_actions for case in action_observed)
    tool_observed = [case for case in cases if final.get(case.id) and final[case.id].action in known_actions]
    tool_correct = sum(_tools_match(case, final[case.id]) for case in tool_observed)
    dispatches = [
        detail
        for outcome in outcomes
        for detail in _tool_call_details(outcome.tool_diagnostics)
        if detail.get("dispatched") is True
    ]
    successful_dispatches = sum(detail.get("success") is True for detail in dispatches)
    infra_cases = sum(bool(_infrastructure_codes(outcome)) for outcome in outcomes)
    business_required = sum(case.expected_action != "SAFETY_BYPASS" for case in cases)
    judge_scorable = sum(item.output is not None and not item.error_code for item in judge_results)
    judge_scorable -= sum(bool(row.get("contract", {}).get("judgeIssues")) for row in rows)
    rag_dispatches = [detail for detail in dispatches if str(detail.get("toolName", "")).endswith("rag_search")]
    rag_outcomes = [outcome for outcome in outcomes if outcome.knowledge_requested]
    passed_by_id = {row.get("caseId"): bool(row.get("passed")) for row in rows}
    executed_ids = set(final)
    end_to_end_passed = sum(passed_by_id.get(case.id, False) for case in cases if case.id in executed_ids)
    return {
        "metrics": {
            "routeObservedAccuracy": _ratio(route_correct, len(route_observed)),
            "routeObservationCoverage": _ratio(len(route_observed), len(route_expected)),
            "actionObservedAccuracy": _ratio(action_correct, len(action_observed)),
            "actionObservationCoverage": _ratio(len(action_observed), len(cases)),
            "rerankSuccessRate": _ratio(sum(item.get("rerankStatus") == "SUCCEEDED" for item in rag_dispatches), len(rag_dispatches)),
            "retrievalObservationCoverage": _ratio(sum(item.retrieval_observation in {"OBSERVED", "OBSERVED_EMPTY"} for item in rag_outcomes), len(rag_outcomes)),
            "toolDecisionObservedAccuracy": _ratio(tool_correct, len(tool_observed)),
            "toolExecutionSuccessRate": _ratio(successful_dispatches, len(dispatches)),
            "infrastructureFailureRate": _ratio(infra_cases, len(executed_ids)),
            "judgeCoverage": _ratio(len(judge_results), business_required),
            "judgeScorableCoverage": _ratio(judge_scorable, business_required),
            "endToEndCasePassRate": _ratio(end_to_end_passed, len(executed_ids)),
            "answerContractFailureRate": _ratio(sum(
                bool({"EVIDENCE_QUOTE_INVALID", "EVIDENCE_SCOPE_INVALID", "ANSWER_CONTRACT_INVALID"}.intersection(_infrastructure_codes(item)))
                for item in outcomes), len(executed_ids)),
            "runtimeActionAgreementRate": _ratio(sum(
                row.get("runtimeAction") == row.get("observedAction") for row in rows if row.get("observedAction")
            ), sum(bool(row.get("observedAction")) for row in rows)),
            "nonRequiredBusinessToolCount": sum(len(
                set(final[case.id].actual_tools) - {
                    str(name).rsplit("__", 1)[-1]
                    for name in case.tools_for_action(final[case.id].action).required
                } - {"read_tool_evidence"}
            ) for case in cases if case.id in final),
        },
        "denominators": {
            "routeObservedAccuracy": len(route_observed),
            "routeObservationCoverage": len(route_expected),
            "actionObservedAccuracy": len(action_observed),
            "actionObservationCoverage": len(cases),
            "rerankSuccessRate": len(rag_dispatches),
            "retrievalObservationCoverage": len(rag_outcomes),
            "toolDecisionObservedAccuracy": len(tool_observed),
            "toolExecutionSuccessRate": len(dispatches),
            "infrastructureFailureRate": len(executed_ids),
            "judgeCoverage": business_required,
            "judgeScorableCoverage": business_required,
            "endToEndCasePassRate": len(executed_ids),
            "answerContractFailureRate": len(executed_ids),
            "runtimeActionAgreementRate": sum(bool(row.get("observedAction")) for row in rows),
            "nonRequiredBusinessToolCount": len(executed_ids),
        },
        "execution": {
            "planned": len(cases),
            "executed": len(executed_ids),
            "aborted": 0,
            "notRun": len(cases) - len(executed_ids),
        },
    }


def _route_matches(case, outcome) -> bool:
    expected = case.expected_route
    if expected is None:
        return False
    actual_intents = [str(item) for item in outcome.route.get("intents", [])]
    return (
        outcome.route.get("primaryIntent") == expected.primaryIntent.value
        and actual_intents == [item.value for item in expected.intents]
    )


def _tools_match(case, outcome) -> bool:
    contract = case.tools_for_action(outcome.action)
    required = {str(name).rsplit("__", 1)[-1] for name in contract.required}
    forbidden = {str(name).rsplit("__", 1)[-1] for name in contract.forbidden}
    actual = set(outcome.actual_tools)
    return required.issubset(actual) and not forbidden.intersection(actual)


def _tool_call_details(value):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "calls" and isinstance(item, list):
                found.extend(row for row in item if isinstance(row, dict))
            else:
                found.extend(_tool_call_details(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_tool_call_details(item))
    return found


def _infrastructure_codes(outcome) -> list[str]:
    knowledge_codes = {"INVALID_ARGUMENT", "TOOL_BUDGET_EXCEEDED", "REPEATED_INVALID_ARGUMENT"}
    values = [outcome.error_code, *outcome.infra_error_codes]
    return list(dict.fromkeys(str(value) for value in values if value and str(value) not in knowledge_codes))


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _outcome_attribution(
    case,
    outcome,
    *,
    business_passed,
    safety_passed,
    judge_error: bool = False,
) -> dict:
    expected_route = case.expected_route.model_dump(by_alias=True) if case.expected_route else None
    route_correct = None if expected_route is None else _route_matches(case, outcome)
    tool_contract = case.tools_for_action(outcome.action)
    required = {str(name).rsplit("__", 1)[-1] for name in tool_contract.required}
    forbidden = {str(name).rsplit("__", 1)[-1] for name in tool_contract.forbidden}
    actual_tools = set(outcome.actual_tools)
    knowledge_decision_correct = None
    if (required or forbidden) and outcome.action not in {"", "UNKNOWN", "UNRESOLVED"}:
        knowledge_decision_correct = required.issubset(actual_tools) and not forbidden.intersection(actual_tools)
    expected_ids = set(case.reference_context_ids)
    retrieval_observed = outcome.retrieval_observation in {"OBSERVED", "OBSERVED_EMPTY"}
    candidate_hit = None if not expected_ids else (
        True if expected_ids.intersection(outcome.retrieved_context_ids) else False if retrieval_observed else None
    )
    usable_hit = None if not expected_ids else bool(expected_ids.intersection(outcome.usable_context_ids))
    context_transferred = (None if usable_hit is None or not outcome.prompt_contexts_observed
                           else bool(expected_ids.intersection(outcome.prompt_context_ids)))
    layers = {
        "routing": _layer_status(route_correct),
        "toolDecision": _layer_status(knowledge_decision_correct),
        "retrieval": "notApplicable" if not expected_ids and not outcome.knowledge_requested else _layer_status(candidate_hit),
        "usableEvidence": _layer_status(usable_hit),
        "contextTransfer": _layer_status(context_transferred),
    }
    attribution = attribute_failure(
        route_correct=route_correct,
        knowledge_decision_correct=knowledge_decision_correct,
        candidate_hit=candidate_hit,
        usable_hit=usable_hit,
        context_transferred=context_transferred,
        faithfulness=None,
        business_passed=business_passed,
        safety_passed=safety_passed,
        judge_error=judge_error,
        infra_error=bool(_infrastructure_codes(outcome)),
    )
    attribution["layers"] = layers
    attribution["causeChain"] = list(dict.fromkeys([
        *[detail.get("errorCode") for detail in _tool_call_details(outcome.tool_diagnostics) if detail.get("errorCode")],
        *outcome.infra_error_codes,
        outcome.error_code,
    ]))
    return attribution


def _layer_status(value: bool | None) -> str:
    if value is None:
        return "notScored"
    return "correct" if value else "incorrect"


def _code_identity() -> dict:
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, check=True).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        commit, dirty = "unknown", True
    return {"gitCommit": commit, "dirty": dirty}


def _sut_identity() -> dict:
    settings = get_settings()
    from app.services.agent_models import AgentModelRegistry, AGENT_MODEL_ALIASES

    registry = AgentModelRegistry(settings)
    agents = {
        name: {
            "provider": profile.provider,
            "model": profile.model,
            "temperature": profile.temperature,
            "maxTokens": profile.max_tokens,
            "think": profile.think,
        }
        for name in AGENT_MODEL_ALIASES
        for profile in [registry.profile_for(name)]
    }
    return {
        "providers": {name: profile["provider"] for name, profile in agents.items()},
        "models": {name: profile["model"] for name, profile in agents.items()},
        "agents": agents,
        "promptVersions": {"response": "production-current"},
        "evaluationBoundary": "service-system-e2e",
        "sideEffects": "suppressed",
    }


def _judge_identity(settings: EvaluationSettings) -> dict:
    return {
        "provider": settings.judge_provider,
        "baseUrlHost": urlparse(settings.normalized_judge_base_url).hostname,
        "model": settings.judge_model,
        "promptVersion": BUSINESS_JUDGE_PROMPT_VERSION,
    }


def _environment_identity(args, settings: EvaluationSettings) -> dict:
    versions = {}
    for name in ("numpy", "chromadb", "mcp", "anyio"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "unavailable"
    return {
        "python": platform.python_version(),
        "dependencies": versions,
        "isolationMode": args.isolation_mode or settings.isolation_mode,
        "redisMode": ("isolated-db15-per-case" if (args.isolation_mode or settings.isolation_mode) == "mysql_isolated"
                      else "short-term-memory-and-turn-snapshots-disabled-per-case"),
        "hybridValidationEnabled": bool(settings.require_hybrid_retrieval),
        "formalSmokeRunCount": 1,
    }


def _baseline_identity(summary: dict) -> dict:
    dataset = summary.get("dataset") or {}
    if "components" in dataset:
        dataset_hash = [
            {"sha256": item.get("sha256"), "caseCount": item.get("caseCount")}
            for item in dataset["components"]
        ]
    else:
        dataset_hash = {"sha256": dataset.get("sha256"), "caseCount": dataset.get("caseCount")}
    corpus = summary.get("corpus") or {}
    runtime_corpus = corpus.get("runtimeCorpusFingerprint") or {}
    sut = summary.get("sut") or {}
    judge = summary.get("judge") or {}
    identity = {
        "suite": summary["suite"],
        "profile": summary["profile"],
        "dataset": dataset_hash,
        "corpusFingerprint": runtime_corpus.get("corpus_hash") or corpus.get("fingerprint"),
        "sutModels": sut.get("models") or {},
        "sutProviders": sut.get("providers") or {},
        "sutAgents": sut.get("agents") or {},
        "judgeModel": judge.get("model"),
        "judgeProvider": judge.get("provider"),
        "ragas": summary.get("ragas") or {},
        "ragPipelineVersion": (summary.get("ragas") or {}).get("pipelineVersion", "production-current"),
    }
    if summary.get("routingIdentity") is not None:
        identity["routingIdentity"] = summary.get("routingIdentity") or {}
    return identity


def _compare_baseline_metrics(current: dict, baseline: dict) -> list[dict]:
    current_metrics = current.get("metrics", {})
    baseline_metrics = baseline.get("metrics", {})
    rules = (
        ("routing.primaryIntentAccuracy", "higher", 0.01),
        ("routing.routePlanExactMatch", "higher", 0.01),
        ("ragas.faithfulness", "higher", 0.01),
        ("ragas.answerRelevancy", "higher", 0.01),
        ("ragas.contextPrecision", "higher", 0.01),
        ("ragas.contextRecall", "higher", 0.01),
    )
    regressions = []
    for path, direction, tolerance in rules:
        suite, metric = path.split(".", 1)
        actual = current_metrics.get(suite, {}).get(metric)
        previous = baseline_metrics.get(suite, {}).get(metric)
        if not isinstance(actual, (int, float)) or not isinstance(previous, (int, float)):
            continue
        regressed = actual < previous - tolerance if direction == "higher" else actual > previous + tolerance
        if regressed:
            regressions.append({
                "metric": path,
                "baseline": previous,
                "actual": actual,
                "tolerance": tolerance,
            })
    return regressions


def _write_error(writer, run_id, suite, profile, code, message) -> None:
    writer.write_json(
        "summary.json",
        summary_document(
            run_id=run_id,
            suite=suite,
            profile=profile,
            passed=False,
            metricErrors=[{"errorCode": code, "message": message}],
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
