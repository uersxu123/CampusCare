"""显式授权后的单次 Smoke：原题集不变，每次冻结当前代码并使用独立结果目录。"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import traceback


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "app/evaluation/datasets/e2e-smoke-context-workitems-v7"
OUTPUT = ROOT / "target/evaluation/smoke-context-workitems-v7"
OLD = OUTPUT / "20260917T200738Z-contextv7"
PREVIOUS = OUTPUT / "20260917T223244Z-followup-4814f4"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        raise ValueError("非法运行 ID")
    run = OUTPUT / args.run_id
    run.mkdir(parents=True, exist_ok=True)
    if (run / "started.json").exists():
        raise RuntimeError("该次 Smoke 已启动，禁止重跑")
    os.environ.update({
        "EVAL_JUDGE_PROVIDER": "ollama", "EVAL_JUDGE_BASE_URL": "http://127.0.0.1:11434",
        "EVAL_JUDGE_MODEL": "qwen3:8b", "EVAL_JUDGE_MAX_RETRIES": "0",
        "EVAL_JUDGE_REPETITIONS": "1", "EVAL_JUDGE_ALLOW_PROMPT_FALLBACK": "false",
        "EVAL_REQUIRE_HYBRID_RETRIEVAL": "true", "EVAL_MAX_WORKERS": "1",
        "EVAL_ISOLATION_MODE": "mysql_isolated", "EVAL_OUTPUT_DIR": str(run),
        "MEMORY_WORKER_ENABLED": "false", "TOOL_QUEUE_ENABLED": "false",
        "CHAT_TOOLS_STRICT_STARTUP": "true",
    })
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    import httpx
    import redis
    from app.core.config import get_settings
    from app.core.database import SessionLocal
    from app.evaluation.config import EvaluationSettings
    from app.evaluation.dataset import dataset_descriptor, load_e2e_cases
    from app.evaluation.runner import _corpus_check, _warmup_hybrid_retrieval, _run, parse_args
    from app.evaluation.reporting.writer import EvaluationReportWriter, summary_document
    from app.evaluation.runtime.isolation import _redis_database_url
    from app.services.agent_models import AgentModelRegistry

    app = get_settings()
    evaluation = EvaluationSettings()
    if not evaluation.storage_admin_url.get_secret_value():
        evaluation = evaluation.model_copy(update={"storage_admin_url": __import__("pydantic").SecretStr(
            "mysql+pymysql://root:root@127.0.0.1:13306/mysql?charset=utf8mb4")})
    before = {"runId": args.run_id, "checkedAt": datetime.now(UTC).isoformat(), "ready": False}
    def counts():
        with SessionLocal() as db:
            return {table: db.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar_one()
                    for table in ("user_accounts", "chat_sessions", "chat_messages", "psychological_reports", "agent_run_traces")}
    try:
        old = json.loads((OLD / "freeze-manifest.json").read_text(encoding="utf-8"))
        hashes = {name: sha(DATASET / name) for name in old["dataset"]["files"]}
        assert hashes == old["dataset"]["files"], "冻结评测集被修改"
        paths = [DATASET / "business.jsonl", DATASET / "safety.jsonl"]
        loaded = [load_e2e_cases(path) for path in paths]
        assert [len(items) for items in loaded] == [13, 3]
        assert app.ai_think is False and app.ai_provider == "ollama"
        registry = AgentModelRegistry(app)
        profiles = {name: registry.profile_for(name) for name in (
            "UnderstandingAgent", "SafetyAgent", "AcademicPlanningAgent", "CampusAffairsAgent", "ResponseAgent")}
        assert all(profile.provider == "ollama" and profile.model == "qwen3:8b" and profile.think is False
                   for profile in profiles.values()), "应用模型配置不符"
        tags = httpx.get(app.ollama_base_url.rstrip("/") + "/api/tags", timeout=10).json()
        before["models"] = {row["name"]: row["digest"] for row in tags["models"]}
        assert "qwen3:8b" in before["models"] and "bge-m3:latest" in before["models"]
        with SessionLocal() as db:
            before["corpus"] = _corpus_check(evaluation, db, DATASET / "corpus-audit.json",
                [dataset_descriptor(path, items) for path, items in zip(paths, loaded)])
            assert before["corpus"]["passed"], "语料指纹不一致"
            _warmup_hybrid_retrieval(db)
        before["hybridReady"] = True
        redis_client = redis.Redis.from_url(_redis_database_url(app.redis_url, 15), socket_timeout=3)
        before["redisDb15Size"] = redis_client.dbsize()
        assert redis_client.ping() and before["redisDb15Size"] == 0, "Redis DB15 非空，拒绝清理未知数据"
        redis_client.close()
        admin = create_engine(evaluation.storage_admin_url.get_secret_value())
        with admin.connect() as connection:
            before["mysqlReady"] = connection.execute(text("SELECT 1")).scalar_one() == 1
        admin.dispose()
        before["businessCounts"] = counts()
        before["database"] = make_url(app.database_url).render_as_string(hide_password=True)
        before["ready"] = True
        freeze = {"runId": args.run_id, "frozenAt": datetime.now(UTC).isoformat(),
            "datasetFiles": hashes,
            "runtimeFiles": {path.relative_to(ROOT).as_posix(): sha(path)
                             for path in (ROOT / "app").rglob("*.py")},
            "launcherHash": sha(Path(__file__)), "models": before["models"],
            "applicationProfiles": {name: {"provider": p.provider, "model": p.model, "think": p.think,
                                            "maxTokens": p.max_tokens} for name, p in profiles.items()},
            "judge": {"model": evaluation.judge_model, "think": app.ai_think,
                      "maxTokens": evaluation.judge_max_tokens, "maxRetries": 0, "repairAttempts": 0,
                      "repetitions": 1, "allowPromptFallback": False, "cache": "fresh per-run directory"},
            "isolation": "per-case MySQL and initially empty Redis DB15; worker disabled; no external notifications",
            "knownLimitations": ["601 tests passed, 1 Safety fallback false-positive failed, 1 weather integration skipped",
                                 "Synthetic model probes: 2/3 passed; Planner still split one eligibility goal into two work items",
                                 "Newly authorized measurement, not a release certification"],
            "verificationEvidence": {name: sha(ROOT / "target/verification/smoke-followup-fixes-20260918" / name)
                                     for name in ("full-final.xml", "model-probes.json", "storage-probe.json", "delivery-manifest.json")},
            "previousRunArtifacts": {name: sha(PREVIOUS / name) for name in (
                "summary.json", "e2e-report.json", "cases.jsonl", "freeze-manifest.json", "E2E_SMOKE_V2_REPORT.md")},
            "priorArtifacts": {name: sha(OLD / name) for name in (
                "summary.json", "e2e-report.json", "cases.jsonl", "freeze-manifest.json")}}
        write(run / "freeze-manifest.json", freeze)
    except Exception as exc:
        before["error"] = f"{type(exc).__name__}: {exc}"
    write(run / "preflight.json", before)
    print(json.dumps(before, ensure_ascii=False, default=str), flush=True)
    if not before["ready"]:
        write(run / "summary.json", {"runId": args.run_id, "passed": False, "status": "BLOCKED", "preflight": before})
        write(run / "e2e-report.json", {"passed": False, "execution": {"planned": 16, "executed": 0, "notRun": 16},
                                       "metricErrors": [{"errorCode": "PREFLIGHT_BLOCKED", "message": before.get("error")}]})
        (run / "cases.jsonl").touch()
        (run / "E2E_SMOKE_V2_REPORT.md").write_text("# E2E Smoke 阻塞\n\n未执行任何样本。原因：" + before.get("error", "") + "\n", encoding="utf-8")
        return 2
    if not args.execute:
        return 0
    with (run / "started.json").open("x", encoding="utf-8") as start:
        json.dump({"startedAt": datetime.now(UTC).isoformat(), "authorization": "用户本轮授权一次 16 条 Smoke"}, start, ensure_ascii=False)
    runner_args = parse_args(["--suite", "e2e", "--profile", "full", "--dataset", str(paths[0]),
        "--safety-dataset", str(paths[1]), "--corpus-audit", str(DATASET / "corpus-audit.json"),
        "--isolation-mode", "mysql_isolated", "--max-workers", "1", "--output", str(run)])
    writer = EvaluationReportWriter(OUTPUT, args.run_id)
    print("FORMAL_SMOKE_STARTED " + str(run), flush=True)
    with (run / "execution.log").open("w", encoding="utf-8", buffering=1) as log, redirect_stdout(log), redirect_stderr(log):
        try:
            result = asyncio.run(_run(runner_args, evaluation, writer, args.run_id, "full"))
            writer.write_json("summary.json", summary_document(run_id=args.run_id, suite="e2e", profile="full", **result))
        except Exception as exc:
            traceback.print_exc()
            writer.write_json("summary.json", {"runId": args.run_id, "passed": False, "status": "ABORTED",
                                               "error": f"{type(exc).__name__}: {exc}"})
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    after = {"completedAt": datetime.now(UTC).isoformat(), "businessCounts": counts(),
             "codeUnchanged": all(sha(ROOT / name) == digest for name, digest in freeze["runtimeFiles"].items()),
             "datasetUnchanged": all(sha(DATASET / name) == digest for name, digest in hashes.items()),
             "priorArtifactsUnchanged": all(sha(OLD / name) == digest for name, digest in freeze["priorArtifacts"].items()),
             "previousRunArtifactsUnchanged": all(sha(PREVIOUS / name) == digest for name, digest in freeze["previousRunArtifacts"].items())}
    after["businessCountsUnchanged"] = after["businessCounts"] == before["businessCounts"]
    write(run / "postflight.json", after)
    print(json.dumps({"runId": args.run_id, "passed": summary.get("passed"), "postflight": after}, ensure_ascii=False))
    return 0 if summary.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
