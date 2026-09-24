#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agents.routing import classify_route
from app.core.config import Settings
from app.core.enums import IntentType
from app.services.agent_models import AgentModelRegistry
from app.services.ai import close_shared_ollama_clients
from app.services.routing_v5 import PrimaryRoutingScorer, clear_prototype_vector_cache, warmup_fast_routing
from app.services.understanding import UnderstandingService

INTENTS = ("CHAT", "ACADEMIC", "CAMPUS", "MENTAL")
_FAST_PLAN_SOURCES = {"FAST_RULE_EMBEDDING"}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


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


def settings_for(args, fast_enabled: bool) -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        ai_provider="ollama",
        ollama_base_url=args.ollama_url,
        ollama_model=args.planner_model,
        agent_model_default_provider="ollama",
        agent_model_default_model=args.planner_model,
        agent_model_understanding_provider="ollama",
        agent_model_understanding_model=args.planner_model,
        agent_model_understanding_temperature=args.temperature,
        agent_model_understanding_max_tokens=args.max_tokens,
        agent_model_understanding_think=args.think,
        knowledge_vector_enabled=True,
        knowledge_embedding_provider="ollama",
        knowledge_embedding_model=args.embedding_model,
        knowledge_embedding_base_url=args.ollama_url,
        embedding_timeout_seconds=args.embedding_timeout,
        route_fast_enabled=fast_enabled,
        route_fast_min_score=args.fast_min_score,
        route_fast_competing_score=args.fast_competing_score,
        route_fast_margin=args.fast_margin,
        route_fast_embedding_timeout_seconds=args.fast_embedding_timeout,
        route_fast_warmup_enabled=True,
        route_fast_warmup_timeout_seconds=args.fast_warmup_timeout,
        route_degraded_rule_weight=args.rule_weight,
        route_degraded_embedding_weight=args.embedding_weight,
        route_degraded_min_score=args.degraded_threshold,
        route_rule_only_min_score=args.rule_only_threshold,
    )


def warm_planner(service: UnderstandingService) -> float:
    start = time.perf_counter()
    try:
        service.classify("你好", {})
    except Exception as exc:
        raise RuntimeError(f"Planner warmup failed: {type(exc).__name__}: {exc}") from exc
    return (time.perf_counter() - start) * 1000.0


def warm_scorer(scorer: PrimaryRoutingScorer) -> float:
    start = time.perf_counter()
    snap = scorer.score("国家助学金什么时候发？")
    elapsed = (time.perf_counter() - start) * 1000.0
    if not snap.embedding_available:
        raise RuntimeError("Routing embedding warmup failed: embedding unavailable")
    return elapsed


def run_mode(rows: list[dict[str, Any]], args, *, name: str, fast_enabled: bool) -> dict[str, Any]:
    # Keep A/B modes independent while still measuring a warm reusable client:
    # clear the process-level pool before each mode, then exclude planner warmup.
    close_shared_ollama_clients()
    settings = settings_for(args, fast_enabled)
    registry = AgentModelRegistry(settings)
    service = UnderstandingService(registry.client_for("UnderstandingAgent"), settings)
    scorer = None
    prototype_warmup_ms = 0.0
    scorer_warmup_ms = 0.0
    if fast_enabled:
        clear_prototype_vector_cache()
        warm_started = time.perf_counter()
        if not warmup_fast_routing(settings):
            raise RuntimeError("Fast routing prototype warmup failed")
        prototype_warmup_ms = (time.perf_counter() - warm_started) * 1000.0
        scorer = PrimaryRoutingScorer(settings)
        # This now measures a production-like warm request: prototype vectors are
        # already cached and the embedding model has been loaded by startup warmup.
        scorer_warmup_ms = warm_scorer(scorer)
    planner_warmup_ms = warm_planner(service)

    predictions = []
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
                routing_scorer=scorer,
            )
            payload = decision.as_payload()
            meta = decision.diagnostics.as_metadata()
            if decision.route_plan is None:
                pred = []
                primary = None
            else:
                pred = list(payload.get("intents") or [])
                primary = payload.get("primaryIntent")
        except Exception as exc:
            payload = {}
            meta = {}
            pred = []
            primary = None
            error = f"{type(exc).__name__}: {exc}"
        wall_ms = (time.perf_counter() - started) * 1000.0
        record = {
            "id": row["id"],
            "text": text,
            "gold": list(row.get("labels") or []),
            "pred": pred,
            "primary": primary,
            "slice": row.get("slice") or row.get("group") or "unknown",
            "difficulty": row.get("difficulty") or "unknown",
            "wallMs": wall_ms,
            "planSource": meta.get("planSource"),
            "llmInvoked": bool(meta.get("llmInvoked")),
            "providerAttemptCount": int(meta.get("providerAttemptCount") or 0),
            "diagnosticLatencyMs": int(meta.get("latencyMs") or 0),
            "routingScoreLatencyMs": int(meta.get("routingScoreLatencyMs") or 0),
            "fastRouteReason": meta.get("fastRouteReason", ""),
            "routingMode": meta.get("routingMode", ""),
            "error": error,
        }
        predictions.append(record)
        if args.progress_every and index % args.progress_every == 0:
            print(f"[{name}] {index}/{len(rows)}", flush=True)

    return {
        "name": name,
        "fastEnabled": fast_enabled,
        "plannerWarmupMs": planner_warmup_ms,
        "prototypeWarmupMs": prototype_warmup_ms,
        "routingScorerWarmupMs": scorer_warmup_ms,
        "config": {
            "plannerModel": args.planner_model,
            "embeddingModel": args.embedding_model,
            "fastMinScore": args.fast_min_score,
            "fastCompetingScore": args.fast_competing_score,
            "fastMargin": args.fast_margin,
            "fastEmbeddingTimeoutSeconds": args.fast_embedding_timeout,
            "ruleWeight": args.rule_weight,
            "embeddingWeight": args.embedding_weight,
        },
        "predictions": predictions,
        "metrics": compute_metrics(predictions),
    }


def compute_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tp = {k: 0 for k in INTENTS}
    fp = {k: 0 for k in INTENTS}
    fn = {k: 0 for k in INTENTS}
    exact = 0
    primary_hit = 0
    sample_p = sample_r = sample_f = 0.0
    latencies = []
    fast_rows = []
    multi_rows = []
    single_rows = []
    errors = 0
    planner_invoked = 0
    provider_attempts = 0
    plan_sources = Counter()
    fast_reasons = Counter()

    for row in rows:
        gold = set(row["gold"])
        pred = set(row["pred"])
        exact += pred == gold
        primary_hit += bool(row.get("primary") in gold)
        if len(gold) > 1:
            multi_rows.append(row)
        elif len(gold) == 1:
            single_rows.append(row)
        if row.get("error"):
            errors += 1
        latencies.append(float(row["wallMs"]))
        planner_invoked += bool(row.get("llmInvoked"))
        provider_attempts += int(row.get("providerAttemptCount") or 0)
        if row.get("planSource"):
            plan_sources[row["planSource"]] += 1
        if row.get("fastRouteReason"):
            fast_reasons[row["fastRouteReason"]] += 1
        if row.get("planSource") in _FAST_PLAN_SOURCES:
            fast_rows.append(row)
        for intent in INTENTS:
            if intent in gold and intent in pred:
                tp[intent] += 1
            elif intent not in gold and intent in pred:
                fp[intent] += 1
            elif intent in gold and intent not in pred:
                fn[intent] += 1
        p = safe_div(len(gold & pred), len(pred)) if pred else (1.0 if not gold else 0.0)
        r = safe_div(len(gold & pred), len(gold)) if gold else (1.0 if not pred else 0.0)
        f = safe_div(2 * p * r, p + r)
        sample_p += p; sample_r += r; sample_f += f

    per_class = {}
    for intent in INTENTS:
        p = safe_div(tp[intent], tp[intent] + fp[intent])
        r = safe_div(tp[intent], tp[intent] + fn[intent])
        f = safe_div(2 * p * r, p + r)
        per_class[intent] = {"precision": p, "recall": r, "f1": f, "tp": tp[intent], "fp": fp[intent], "fn": fn[intent]}
    TP, FP, FN = sum(tp.values()), sum(fp.values()), sum(fn.values())
    micro_p = safe_div(TP, TP + FP)
    micro_r = safe_div(TP, TP + FN)
    micro_f = safe_div(2 * micro_p * micro_r, micro_p + micro_r)
    n = len(rows)

    def subset_metrics(sub):
        if not sub:
            return {"n": 0}
        stp={k:0 for k in INTENTS}; sfp={k:0 for k in INTENTS}; sfn={k:0 for k in INTENTS}
        exact_n=top1_hit_n=0; sp=sr=sf=0.0
        for r in sub:
            gold=set(r["gold"]); pred=set(r["pred"]); exact_n += pred==gold; top1_hit_n += bool(r.get("primary") in gold)
            for k in INTENTS:
                if k in gold and k in pred: stp[k]+=1
                elif k not in gold and k in pred: sfp[k]+=1
                elif k in gold and k not in pred: sfn[k]+=1
            pp=safe_div(len(gold&pred),len(pred)) if pred else (1.0 if not gold else 0.0)
            rr=safe_div(len(gold&pred),len(gold)) if gold else (1.0 if not pred else 0.0)
            ff=safe_div(2*pp*rr,pp+rr); sp+=pp; sr+=rr; sf+=ff
        per={}
        for k in INTENTS:
            pp=safe_div(stp[k],stp[k]+sfp[k]); rr=safe_div(stp[k],stp[k]+sfn[k]); ff=safe_div(2*pp*rr,pp+rr)
            per[k]={"precision":pp,"recall":rr,"f1":ff}
        TP=sum(stp.values()); FP=sum(sfp.values()); FN=sum(sfn.values())
        mp=safe_div(TP,TP+FP); mr=safe_div(TP,TP+FN); mf=safe_div(2*mp*mr,mp+mr)
        return {
            "n": len(sub),
            "exactAccuracy": safe_div(exact_n,len(sub)),
            "top1HitAccuracy": safe_div(top1_hit_n,len(sub)),
            "microPrecision": mp, "microRecall": mr, "microF1": mf,
            "macroF1": statistics.fmean(v["f1"] for v in per.values()),
            "samplePrecision": safe_div(sp,len(sub)), "sampleRecall": safe_div(sr,len(sub)), "sampleF1": safe_div(sf,len(sub)),
            "avgLatencyMs": statistics.fmean(float(r["wallMs"]) for r in sub),
        }

    boundary_rows=[r for r in rows if r.get("difficulty")=="boundary"]

    return {
        "n": n,
        "exactAccuracy": safe_div(exact, n),
        "primaryHitAccuracy": safe_div(primary_hit, n),
        "microPrecision": micro_p,
        "microRecall": micro_r,
        "microF1": micro_f,
        "macroF1": statistics.fmean(x["f1"] for x in per_class.values()),
        "samplePrecision": safe_div(sample_p, n),
        "sampleRecall": safe_div(sample_r, n),
        "sampleF1": safe_div(sample_f, n),
        "perClass": per_class,
        "single": subset_metrics(single_rows),
        "multi": subset_metrics(multi_rows),
        "boundary": subset_metrics(boundary_rows),
        "errors": errors,
        "plannerInvocationRate": safe_div(planner_invoked, n),
        "plannerInvocationCount": planner_invoked,
        "providerAttemptCount": provider_attempts,
        "fastRouteRate": safe_div(len(fast_rows), n),
        "fastRouteCount": len(fast_rows),
        "fastRoutePrecision": safe_div(sum(set(r["gold"]) == set(r["pred"]) for r in fast_rows), len(fast_rows)) if fast_rows else None,
        "latency": {
            "avgMs": statistics.fmean(latencies) if latencies else 0.0,
            "p50Ms": percentile(latencies, 0.50),
            "p95Ms": percentile(latencies, 0.95),
            "minMs": min(latencies) if latencies else 0.0,
            "maxMs": max(latencies) if latencies else 0.0,
        },
        "planSourceCounts": dict(plan_sources),
        "fastRouteReasonCounts": dict(fast_reasons),
    }


def delta(a, b):
    return b - a


def build_report(baseline: dict[str, Any], candidate: dict[str, Any]) -> str:
    b, c = baseline["metrics"], candidate["metrics"]
    rows = [
        ("Exact Accuracy", b["exactAccuracy"], c["exactAccuracy"], "pct"),
        ("Macro-F1", b["macroF1"], c["macroF1"], "pct"),
        ("Micro-F1", b["microF1"], c["microF1"], "pct"),
        ("Single Exact", b["single"]["exactAccuracy"], c["single"]["exactAccuracy"], "pct"),
        ("Multi Exact", b["multi"]["exactAccuracy"], c["multi"]["exactAccuracy"], "pct"),
        ("Multi Precision (micro)", b["multi"]["microPrecision"], c["multi"]["microPrecision"], "pct"),
        ("Multi Recall (micro)", b["multi"]["microRecall"], c["multi"]["microRecall"], "pct"),
        ("Multi F1 (micro)", b["multi"]["microF1"], c["multi"]["microF1"], "pct"),
        ("Multi F1 (sample)", b["multi"]["sampleF1"], c["multi"]["sampleF1"], "pct"),
        ("Boundary F1 (macro)", b["boundary"]["macroF1"], c["boundary"]["macroF1"], "pct"),
        ("Boundary F1 (micro)", b["boundary"]["microF1"], c["boundary"]["microF1"], "pct"),
        ("Planner Invocation Rate", b["plannerInvocationRate"], c["plannerInvocationRate"], "pct"),
        ("Average Latency (ms)", b["latency"]["avgMs"], c["latency"]["avgMs"], "num"),
        ("P50 Latency (ms)", b["latency"]["p50Ms"], c["latency"]["p50Ms"], "num"),
        ("P95 Latency (ms)", b["latency"]["p95Ms"], c["latency"]["p95Ms"], "num"),
    ]
    out = ["# MindBridge V5.4-style Planner-only vs V5.5 Fast Route", "", f"- Dataset: {b['n']} cases", f"- Planner model: `{candidate['config']['plannerModel']}`", f"- Embedding model: `{candidate['config']['embeddingModel']}`", f"- Fast gate: min={candidate['config']['fastMinScore']}, competing={candidate['config']['fastCompetingScore']}, margin={candidate['config']['fastMargin']}", "", "| Metric | Planner-only | Fast Route | Delta |", "|---|---:|---:|---:|"]
    for name, x, y, kind in rows:
        if kind == "pct":
            out.append(f"| {name} | {x*100:.2f}% | {y*100:.2f}% | {(y-x)*100:+.2f} pp |")
        else:
            out.append(f"| {name} | {x:.2f} | {y:.2f} | {y-x:+.2f} |")
    frp = c["fastRoutePrecision"]
    out += ["", "## Fast Route", "", f"- Fast route count: **{c['fastRouteCount']} / {c['n']}** ({c['fastRouteRate']*100:.2f}%)", f"- Fast route precision: **{frp*100:.2f}%**" if frp is not None else "- Fast route precision: N/A", f"- Planner invocation reduction: **{(b['plannerInvocationRate']-c['plannerInvocationRate'])*100:.2f} pp**", f"- Prototype/model startup warmup: **{candidate.get('prototypeWarmupMs', 0.0):.2f} ms**", f"- Routing scorer warm request: **{candidate['routingScorerWarmupMs']:.2f} ms**", "", "## Per-class F1", "", "| Intent | Planner-only | Fast Route | Delta |", "|---|---:|---:|---:|"]
    for intent in INTENTS:
        x=b["perClass"][intent]["f1"]; y=c["perClass"][intent]["f1"]
        out.append(f"| {intent} | {x*100:.2f}% | {y*100:.2f}% | {(y-x)*100:+.2f} pp |")
    out += ["", "## Counts", "", f"- Planner-only plan sources: `{json.dumps(b['planSourceCounts'], ensure_ascii=False)}`", f"- Fast-route plan sources: `{json.dumps(c['planSourceCounts'], ensure_ascii=False)}`", f"- Fast reject reasons: `{json.dumps(c['fastRouteReasonCounts'], ensure_ascii=False)}`", f"- Errors: baseline={b['errors']}, candidate={c['errors']}", "", "## Interpretation guardrails", "", "- Fast Route 的第一优先级是 Precision，不是 Coverage。", "- 若总体 Exact/Macro-F1 下降明显，先提高 min-score / margin，不要为了降低 Planner 调用率牺牲正确率。", "- 延迟是本机 Ollama、模型是否已加载、CPU/GPU 状态下的测量值；至少重复 3 次再报告最终数字。"]
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="eval_fast_route/routing_eval_v5_business_208.jsonl")
    ap.add_argument("--output-dir", default="target/fast-route-eval")
    ap.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    ap.add_argument("--planner-model", default="qwen3:8b")
    ap.add_argument("--embedding-model", default="bge-m3:latest")
    ap.add_argument("--temperature", type=float, default=0.1)
    ap.add_argument("--max-tokens", type=int, default=768)
    ap.add_argument("--think", action="store_true")
    ap.add_argument("--embedding-timeout", type=float, default=30.0)
    ap.add_argument("--fast-warmup-timeout", type=float, default=30.0)
    ap.add_argument("--fast-embedding-timeout", type=float, default=3.0)
    ap.add_argument("--fast-min-score", type=float, default=0.95)
    ap.add_argument("--fast-competing-score", type=float, default=0.90)
    ap.add_argument("--fast-margin", type=float, default=0.08)
    ap.add_argument("--rule-weight", type=float, default=0.40)
    ap.add_argument("--embedding-weight", type=float, default=0.60)
    ap.add_argument("--degraded-threshold", type=float, default=0.90)
    ap.add_argument("--rule-only-threshold", type=float, default=0.90)
    ap.add_argument("--progress-every", type=int, default=20)
    ap.add_argument("--mode-order", choices=("planner-first", "fast-first"), default="planner-first")
    args = ap.parse_args()

    rows = load_jsonl(Path(args.dataset))
    out = Path(args.output_dir); out.mkdir(parents=True, exist_ok=True)

    if args.mode_order == "fast-first":
        print("Running fast-route candidate...", flush=True)
        candidate = run_mode(rows, args, name="fast_route", fast_enabled=True)
        print("Running planner-only baseline...", flush=True)
        baseline = run_mode(rows, args, name="planner_only", fast_enabled=False)
    else:
        print("Running planner-only baseline...", flush=True)
        baseline = run_mode(rows, args, name="planner_only", fast_enabled=False)
        print("Running fast-route candidate...", flush=True)
        candidate = run_mode(rows, args, name="fast_route", fast_enabled=True)

    for run in (baseline, candidate):
        (out/f"{run['name']}_predictions.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False)+"\n" for x in run["predictions"]), encoding="utf-8")
        (out/f"{run['name']}_metrics.json").write_text(json.dumps({k:v for k,v in run.items() if k != "predictions"}, ensure_ascii=False, indent=2), encoding="utf-8")
    comparison = {"baseline": {k:v for k,v in baseline.items() if k != "predictions"}, "candidate": {k:v for k,v in candidate.items() if k != "predictions"}}
    (out/"comparison.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8")
    (out/"FAST_ROUTE_EVAL_REPORT.md").write_text(build_report(baseline, candidate), encoding="utf-8")
    print(f"Done. Report: {out/'FAST_ROUTE_EVAL_REPORT.md'}")


if __name__ == "__main__":
    main()
