"""修复后的只读预检与独立 Judge 探针；不运行正式 Smoke。"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import time
import urllib.request
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def environment():
    from app.core.config import get_settings
    from app.core.database import SessionLocal
    from app.chat_tools.bootstrap import bootstrap_readonly_dependencies
    from app.evaluation.config import EvaluationSettings
    from app.evaluation.dataset import dataset_descriptor, load_e2e_cases
    from app.evaluation.runner import _corpus_check, _validate_dataset_contract
    from app.services.mcp_runtime import create_mcp_runtime

    settings = get_settings()
    directory = ROOT / "app/evaluation/datasets/e2e-smoke-current-v2"
    descriptors, cases = [], []
    for name in ("business.jsonl", "safety.jsonl"):
        rows = load_e2e_cases(directory / name)
        cases.extend(rows)
        descriptors.append(dataset_descriptor(directory / name, rows))
    _validate_dataset_contract(cases)
    with SessionLocal() as db:
        corpus = _corpus_check(EvaluationSettings(), db, directory / "corpus-audit.json", descriptors)
    bootstrap = asdict(bootstrap_readonly_dependencies(strict=True))
    readiness = []
    for _ in range(3):
        started = time.monotonic()
        runtime = create_mcp_runtime(settings, strict_startup=True)
        try:
            tools = runtime.list_tools_sync("chat_readonly")
            readiness.append({"seconds": round(time.monotonic() - started, 3),
                              "tools": [tool.name for tool in tools]})
        finally:
            runtime.shutdown()
    with urllib.request.urlopen(settings.ollama_base_url + "/api/tags", timeout=10) as response:
        models = [item["name"] for item in json.load(response).get("models", [])]
    passed = (corpus["passed"] and bootstrap["ready"] and "qwen3:8b" in models
              and all("rag_search" in row["tools"] for row in readiness))
    return {"passed": bool(passed), "python": platform.python_version(), "corpus": corpus,
            "bootstrap": bootstrap, "mcpReadiness": readiness, "ollamaModels": models,
            "dataset": descriptors, "judgeContextTokens": settings.ollama_num_ctx,
            "inputMaxTokens": settings.context_input_max_tokens}


def judge_probe(output: Path):
    from app.core.config import get_settings
    from app.evaluation.config import EvaluationSettings
    from app.evaluation.contracts import EndToEndCase, EvaluationRuntimeOutcome
    from app.evaluation.judges.deepseek import DeepSeekJudge
    from app.services.turn_metrics import TurnMetricsCollector, bind_turn_metrics

    settings = get_settings()
    config = EvaluationSettings(_env_file=None, judge_provider="ollama",
                                judge_base_url=settings.ollama_base_url, judge_model="qwen3:8b",
                                judge_api_key="ollama-local", judge_max_retries=0,
                                output_dir=output.parent / "independent-judge-cache")
    case = EndToEndCase.model_validate({"id": "independent-judge-probe", "turns": ["向我问好"],
                                       "expected_action": "ANSWER", "reference": "简短友好地问好。"})
    outcome = EvaluationRuntimeOutcome(
        case_id=case.id, turn_index=0, response="你好！很高兴见到你。", route={"primaryIntent": "CHAT"},
        risk_level="LOW", action="ANSWER", knowledge_requested=False, knowledge_used=False,
        retrieved_context_ids=[], retrieved_contexts=[], usable_context_ids=[], usable_contexts=[],
        trace_id="synthetic-probe", turn_metrics={},
    )
    collector = TurnMetricsCollector("independent-judge-probe")
    with bind_turn_metrics(collector):
        result = DeepSeekJudge(settings, config).judge(case, outcome)
    with urllib.request.urlopen(settings.ollama_base_url + "/api/ps", timeout=10) as response:
        loaded = json.load(response)
    return {"passed": result.output is not None and result.error_code is None,
            "formalSmokeCase": False, "judgeProvider": config.judge_provider,
            "judgeModel": config.judge_model, "contextTokens": settings.ollama_num_ctx,
            "think": settings.ai_think, "result": asdict(result),
            "turnMetrics": collector.as_dict(), "loadedModels": loaded}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("environment", "judge"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {"startedAt": datetime.now(UTC).isoformat(), "mode": args.mode}
    try:
        report.update(environment() if args.mode == "environment" else judge_probe(args.output))
    except Exception as exc:
        report.update(passed=False, errorCode=type(exc).__name__, error=str(exc))
    report["finishedAt"] = datetime.now(UTC).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                      default=lambda value: value.model_dump() if hasattr(value, "model_dump") else str(value)) + "\n",
                           encoding="utf-8", newline="\n")
    print(json.dumps({"passed": report["passed"], "output": str(args.output),
                      "errorCode": report.get("errorCode")}, ensure_ascii=False))
    return 0 if report["passed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
