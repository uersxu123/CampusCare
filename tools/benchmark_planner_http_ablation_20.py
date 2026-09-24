#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.services.ai as ai_module
from app.agents.routing import classify_route
from app.core.config import Settings
from app.services.agent_models import AgentModelRegistry
from app.services.understanding import UnderstandingService

INTENTS = ("ACADEMIC", "CAMPUS", "MENTAL", "CHAT")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def select_fixed_20(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for intent in INTENTS:
        matches = [
            row for row in rows
            if (row.get("slice") or row.get("group")) == "single"
            and list(row.get("labels") or []) == [intent]
        ]
        if len(matches) < 5:
            raise RuntimeError(f"Not enough single-intent rows for {intent}: {len(matches)}")
        selected.extend(matches[:5])
    return selected


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * p
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


class _OneShotClient:
    """Compatibility adapter that deliberately recreates httpx.Client per call.

    This is used only inside the diagnostic runner to emulate the old top-level
    httpx.post behavior. It does not modify application source behavior.
    """

    def post(self, url: str, **kwargs):
        return httpx.post(url, trust_env=False, **kwargs)


def make_settings(args, base_url: str) -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        ai_provider="ollama",
        ollama_base_url=base_url,
        ollama_model=args.planner_model,
        ollama_num_ctx=args.num_ctx,
        ai_temperature=args.temperature,
        ai_max_tokens=args.max_tokens,
        ai_think=args.think,
        agent_model_default_provider="ollama",
        agent_model_default_model=args.planner_model,
        agent_model_understanding_provider="ollama",
        agent_model_understanding_model=args.planner_model,
        agent_model_understanding_temperature=args.temperature,
        agent_model_understanding_max_tokens=args.max_tokens,
        agent_model_understanding_think=args.think,
        route_fast_enabled=False,
    )


def run_variant(rows: list[dict[str, Any]], args, *, name: str, base_url: str, reuse_client: bool) -> dict[str, Any]:
    ai_module.close_shared_ollama_clients()
    original_helper = ai_module._shared_ollama_client
    if not reuse_client:
        ai_module._shared_ollama_client = lambda _base_url: _OneShotClient()

    try:
        settings = make_settings(args, base_url)
        registry = AgentModelRegistry(settings)
        service = UnderstandingService(registry.client_for("UnderstandingAgent"), settings)

        warm_start = time.perf_counter()
        service.classify("你好", {})
        warmup_ms = (time.perf_counter() - warm_start) * 1000.0

        records: list[dict[str, Any]] = []
        for index, row in enumerate(rows, 1):
            text = str(row["text"])
            started = time.perf_counter()
            error = None
            try:
                decision = classify_route(
                    text,
                    {},
                    semantic_classifier=service.classify,
                    settings=settings,
                    raw_current_input=text,
                    routing_scorer=None,
                )
                payload = decision.as_payload()
                meta = decision.diagnostics.as_metadata()
                pred = list(payload.get("intents") or []) if decision.route_plan is not None else []
            except Exception as exc:
                meta = {}
                pred = []
                error = f"{type(exc).__name__}: {exc}"
            wall_ms = (time.perf_counter() - started) * 1000.0
            records.append({
                "id": row["id"],
                "text": text,
                "gold": list(row.get("labels") or []),
                "pred": pred,
                "wallMs": wall_ms,
                "planSource": meta.get("planSource"),
                "llmInvoked": bool(meta.get("llmInvoked")),
                "providerAttemptCount": int(meta.get("providerAttemptCount") or 0),
                "error": error,
            })
            print(f"[{name}] {index:02d}/{len(rows)} {row['id']} {wall_ms:.2f} ms", flush=True)

        lat = [float(x["wallMs"]) for x in records]
        exact = sum(set(x["gold"]) == set(x["pred"]) for x in records)
        metrics = {
            "n": len(records),
            "meanMs": statistics.fmean(lat),
            "p50Ms": percentile(lat, 0.50),
            "p95Ms": percentile(lat, 0.95),
            "minMs": min(lat),
            "maxMs": max(lat),
            "exactAccuracy": exact / len(records),
            "errors": sum(bool(x["error"]) for x in records),
            "providerAttemptCount": sum(x["providerAttemptCount"] for x in records),
            "plannerInvocationCount": sum(bool(x["llmInvoked"]) for x in records),
        }
        return {
            "name": name,
            "baseUrl": base_url,
            "reuseClient": reuse_client,
            "warmupMs": warmup_ms,
            "metrics": metrics,
            "predictions": records,
        }
    finally:
        ai_module._shared_ollama_client = original_helper
        ai_module.close_shared_ollama_clients()


def build_report(runs: list[dict[str, Any]], args) -> str:
    lines = [
        "# Planner HTTP 20 条消融报告",
        "",
        "固定旧 208 集中的 A01-A05 / C01-C05 / M01-M05 / H01-H05；每种模式先 warmup 1 次，warmup 不计入 20 条正式耗时。",
        "",
        f"模型：`{args.planner_model}`；temperature={args.temperature}；think={str(args.think).lower()}；num_ctx={args.num_ctx}；max_tokens={args.max_tokens}。",
        "",
        "| Variant | Endpoint | Client | Mean ms | P50 ms | P95 ms | Exact | Errors |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for run in runs:
        m = run["metrics"]
        lines.append(
            f"| {run['name']} | `{run['baseUrl']}` | {'reuse' if run['reuseClient'] else 'fresh-per-request'} | "
            f"{m['meanMs']:.2f} | {m['p50Ms']:.2f} | {m['p95Ms']:.2f} | {m['exactAccuracy']*100:.2f}% | {m['errors']} |"
        )
    by_name = {r["name"]: r for r in runs}
    if "baseline_localhost_fresh" in by_name and "ipv4_fresh" in by_name and "ipv4_reuse" in by_name:
        b = by_name["baseline_localhost_fresh"]["metrics"]["meanMs"]
        i = by_name["ipv4_fresh"]["metrics"]["meanMs"]
        r = by_name["ipv4_reuse"]["metrics"]["meanMs"]
        lines += [
            "",
            "## Mean 消融",
            "",
            f"- localhost → IPv4（仍每请求新 Client）：{b:.2f} → {i:.2f} ms，变化 {i-b:+.2f} ms ({(i/b-1)*100:+.2f}%)。",
            f"- IPv4 fresh → IPv4 reusable Client：{i:.2f} → {r:.2f} ms，变化 {r-i:+.2f} ms ({(r/i-1)*100:+.2f}%)。",
            f"- 完整改动相对 baseline：{b:.2f} → {r:.2f} ms，变化 {r-b:+.2f} ms ({(r/b-1)*100:+.2f}%)。",
        ]
    lines += [
        "",
        "## 解释",
        "",
        "- `baseline_localhost_fresh` 仅用于复现旧连接行为，不代表修改后项目实际配置。",
        "- `ipv4_fresh` 隔离 endpoint 修复收益。",
        "- `ipv4_reuse` 对应修改后项目的同步 Ollama Planner 调用方式。",
        "- 20 条只是性能消融，不替代全量 208 的最终 Mean/质量指标。",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="eval_fast_route/routing_eval_v5_business_208.jsonl")
    ap.add_argument("--output-dir", default="target/planner-http-ablation-20")
    ap.add_argument("--planner-model", default="qwen3:8b")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-tokens", type=int, default=768)
    ap.add_argument("--num-ctx", type=int, default=16384)
    ap.add_argument("--think", action="store_true")
    args = ap.parse_args()

    rows = select_fixed_20(load_jsonl(Path(args.dataset)))
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "selected_20.jsonl").write_text(
        "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows), encoding="utf-8"
    )

    variants = [
        ("baseline_localhost_fresh", "http://localhost:11434", False),
        ("ipv4_fresh", "http://127.0.0.1:11434", False),
        ("ipv4_reuse", "http://127.0.0.1:11434", True),
    ]
    runs = [run_variant(rows, args, name=n, base_url=u, reuse_client=reuse) for n, u, reuse in variants]

    for run in runs:
        (out / f"{run['name']}_predictions.jsonl").write_text(
            "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in run["predictions"]), encoding="utf-8"
        )
        (out / f"{run['name']}_metrics.json").write_text(
            json.dumps({k: v for k, v in run.items() if k != "predictions"}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    summary = {r["name"]: {k: v for k, v in r.items() if k != "predictions"} for r in runs}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "PLANNER_HTTP_ABLATION_20_REPORT.md").write_text(build_report(runs, args), encoding="utf-8")
    print(f"Done: {out / 'PLANNER_HTTP_ABLATION_20_REPORT.md'}")


if __name__ == "__main__":
    main()
