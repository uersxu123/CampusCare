from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path


def compare_reports(v1: dict, v2: dict, *, latency_tolerance: float) -> dict:
    v1_metrics = _bm25_metrics(v1)
    v2_metrics = _bm25_metrics(v2)
    baseline_recall = float(v1_metrics.get("recallAt10", v1_metrics.get("recallAt5", 0.0)))
    candidate_recall = float(v2_metrics.get("recallAt10", 0.0))
    baseline_citation = v1_metrics.get("citationPageAccuracy")
    candidate_citation = v2_metrics.get("citationPageAccuracy")
    baseline_latency = float(v1_metrics.get("searchP95Seconds", 0.0))
    candidate_latency = float(v2_metrics.get("searchP95Seconds", 0.0))
    citation_passed = (
        candidate_citation is not None
        and float(candidate_citation) >= 0.95
        and (baseline_citation is None or float(candidate_citation) >= float(baseline_citation))
    )
    latency_limit = baseline_latency * latency_tolerance if baseline_latency > 0 else 4.0
    gates = {
        "profile": _profile(v2) == "structure_token_v2",
        "dataset": v1.get("dataset") == v2.get("dataset"),
        "caseCount": int(v1.get("totalCases", 0)) == int(v2.get("totalCases", 0)),
        "recallAt10": candidate_recall >= baseline_recall,
        "citationPageAccuracy": citation_passed,
        "v2Bm25Gate": bool(v2.get("modes", {}).get("bm25", {}).get("passed")),
    }
    return {
        "schemaVersion": 2,
        "createdAt": datetime.now(UTC).isoformat(),
        "baseline": {
            "profile": _profile(v1),
            "recallAt10": baseline_recall,
            "citationPageAccuracy": baseline_citation,
            "searchP95Seconds": baseline_latency,
        },
        "candidate": {
            "profile": _profile(v2),
            "recallAt10": candidate_recall,
            "citationPageAccuracy": candidate_citation,
            "searchP95Seconds": candidate_latency,
        },
        "latencyTolerance": latency_tolerance,
        "latencyLimitSeconds": latency_limit,
        "latencyGateEnabled": False,
        "observations": {
            "searchP95WithinTolerance": candidate_latency <= latency_limit,
            "searchP95DeltaSeconds": candidate_latency - baseline_latency,
        },
        "gates": gates,
        "passed": all(gates.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="比较知识入库 V1/V2 固定评测并执行质量激活门禁；延迟仅记录。")
    parser.add_argument("v1", type=Path)
    parser.add_argument("v2", type=Path)
    parser.add_argument("--latency-tolerance", type=float, default=1.20)
    parser.add_argument("--output", type=Path, default=Path("target/knowledge-ingestion-v1-v2-comparison.json"))
    args = parser.parse_args()
    if args.latency_tolerance < 1.0:
        raise SystemExit("latency tolerance 不能小于 1.0")
    report = compare_reports(
        _read(args.v1),
        _read(args.v2),
        latency_tolerance=args.latency_tolerance,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "passed": report["passed"], "gates": report["gates"]}, ensure_ascii=False))
    return 0 if report["passed"] else 2


def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"评测报告必须是对象：{path}")
    return value


def _bm25_metrics(report: dict) -> dict:
    value = report.get("modes", {}).get("bm25", {}).get("metrics", {})
    if not isinstance(value, dict):
        raise ValueError("评测报告缺少 bm25 metrics")
    return value


def _profile(report: dict) -> str:
    return str(report.get("ingestionBaseline", {}).get("profile") or "unknown")


if __name__ == "__main__":
    raise SystemExit(main())
