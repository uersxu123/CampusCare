from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import Settings
from app.evaluation.config import EvaluationSettings


RUNTIME_FILES = (
    "app/agents/autonomous.py",
    "app/agents/event_driven_runtime.py",
    "app/agents/result.py",
    "app/agents/routing.py",
    "app/chat_tools/server.py",
    "app/evaluation/config.py",
    "app/evaluation/judges/business.py",
    "app/evaluation/judges/deepseek.py",
    "app/evaluation/runner.py",
    "app/evaluation/runtime/adapter.py",
    "app/evaluation/runtime/isolation.py",
    "app/models/entities.py",
    "app/services/agent_loop.py",
    "app/services/chat_tool_runtime.py",
    "app/services/context_budget.py",
    "app/services/context_builder.py",
    "app/services/intent_prompts.py",
    "app/services/rag_pipeline.py",
    "app/services/routing_v5.py",
    "app/services/tool_executor.py",
    "app/services/tool_models.py",
    "app/services/tool_registry.py",
    "app/services/tool_result_store.py",
    "app/services/trace.py",
    "app/services/understanding.py",
    "migrations/versions/0016_context_workitems_v7.py",
)

PROBE_FILES = (
    "scripts/freeze_context_workitems_v7.py",
    "scripts/probe_context_workitems_v7.py",
    "scripts/probe_context_workitems_v7_runtime.py",
    "scripts/probe_context_workitems_v7_storage.py",
    "scripts/write_context_workitems_v7_report.py",
    "tests/test_context_workitems_v7.py",
    "tests/test_evaluation_mysql_isolation_url.py",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="冻结 Context WorkItems V7 正式 Smoke 合同")
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--probe-evidence", type=Path, required=True)
    parser.add_argument("--storage-evidence", type=Path, required=True)
    parser.add_argument("--runtime-evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    settings = Settings(_env_file=None)
    evaluation = EvaluationSettings(
        _env_file=None,
        judge_provider="ollama",
        judge_base_url=settings.ollama_base_url,
        judge_model="qwen3:8b",
        judge_max_retries=0,
        judge_repetitions=1,
        judge_allow_prompt_fallback=False,
        max_workers=1,
        isolation_mode="mysql_isolated",
    )
    evidence_files = (args.probe_evidence, args.storage_evidence, args.runtime_evidence)
    evidence = [json.loads(path.read_text(encoding="utf-8")) for path in evidence_files]
    if not all(item.get("passed") is True for item in evidence):
        raise RuntimeError("必要探针未全部通过，拒绝冻结")
    dataset_files = tuple(args.dataset_dir / name for name in (
        "business.jsonl", "safety.jsonl", "corpus-audit.json", "adaptation-changes.json", "README.md",
    ))
    source_files = tuple(root / name for name in (*RUNTIME_FILES, *PROBE_FILES))
    missing = [str(path) for path in (*dataset_files, *source_files, *evidence_files) if not path.is_file()]
    if missing:
        raise FileNotFoundError("冻结文件缺失: " + ", ".join(missing))
    payload = {
        "schemaVersion": 1,
        "freezeId": "context-workitems-v7-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        "frozenAt": datetime.now(UTC).isoformat(),
        "encoding": "UTF-8 without BOM",
        "dataset": {
            "version": "e2e-smoke-context-workitems-v7",
            "businessCaseCount": 13,
            "safetyCaseCount": 3,
            "files": {path.name: sha256(path) for path in dataset_files},
            "questionsAndBusinessGoldPreservedFrom": "e2e-smoke-current-v2",
        },
        "runtimeFiles": {str(path.relative_to(root)).replace("\\", "/"): sha256(path) for path in source_files},
        "probeEvidence": {path.name: sha256(path) for path in evidence_files},
        "models": {
            "application": "qwen3:8b",
            "judge": "qwen3:8b",
            "embedding": "bge-m3:latest",
            "judgeThink": False,
        },
        "budgets": {
            "ollamaNumCtx": settings.ollama_num_ctx,
            "inputMaxTokens": settings.context_input_max_tokens,
            "safetyMarginTokens": settings.context_model_safety_margin_tokens,
            "plannerOutputMaxTokens": settings.agent_model_understanding_max_tokens,
            "specialistOutputMaxTokens": settings.agent_model_specialist_max_tokens,
            "responseOutputMaxTokens": settings.agent_model_response_max_tokens,
            "toolLargeResultTokens": settings.tool_result_large_tokens,
            "rereadReserveTokens": settings.context_reread_reserve_tokens,
        },
        "toolPermissions": {
            "GeneralChatAgent": ["get_current_weather"],
            "AcademicPlanningAgent": ["rag_search", "read_tool_evidence"],
            "CampusAffairsAgent": ["rag_search", "read_tool_evidence"],
            "PsychologicalSupportAgent": ["rag_search", "read_tool_evidence"],
            "ResponseAgent": ["read_tool_evidence"],
            "highRiskResponse": [],
        },
        "judgeContract": {
            "provider": evaluation.judge_provider,
            "model": evaluation.judge_model,
            "temperature": evaluation.judge_temperature,
            "maxRetries": evaluation.judge_max_retries,
            "repairAttempts": 0,
            "repetitions": evaluation.judge_repetitions,
            "allowPromptFallback": evaluation.judge_allow_prompt_fallback,
        },
        "execution": {
            "maxWorkers": evaluation.max_workers,
            "isolationMode": evaluation.isolation_mode,
            "redis": "database 15, cleaned per case",
            "failedCaseRerun": False,
            "formalRunCountAuthorized": 1,
        },
        "corpus": json.loads((args.dataset_dir / "corpus-audit.json").read_text(encoding="utf-8"))["referenceSource"],
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
