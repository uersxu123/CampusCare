from __future__ import annotations

import argparse
import json
from pathlib import Path


ROUTING_METRICS = (
    "highRiskMissCount",
    "riskRecall",
    "primaryIntentAccuracy",
    "workItemCountAccuracy",
    "routePlanExactMatch",
    "contextRelationAccuracy",
    "hardDataDependencyAccuracy",
    "orderOnlyHardDependencyErrorRate",
    "singleGoalNoOversplitRate",
)


def compare_routing_reports(baseline: dict, current: dict) -> dict:
    baseline_identity = baseline.get("routingIdentity") or {}
    current_identity = current.get("routingIdentity") or {}
    differences = sorted(
        key for key in set(baseline_identity) | set(current_identity)
        if baseline_identity.get(key) != current_identity.get(key)
    )
    baseline_hash = (baseline.get("dataset") or {}).get("sha256")
    current_hash = (current.get("dataset") or {}).get("sha256")
    if baseline_hash != current_hash and "datasetSha256" not in differences:
        differences.append("datasetSha256")
    comparable = bool(baseline_identity) and not differences
    changes = []
    if comparable:
        baseline_metrics = (baseline.get("metrics") or {}).get("routing") or {}
        current_metrics = (current.get("metrics") or {}).get("routing") or {}
        for name in ROUTING_METRICS:
            before = baseline_metrics.get(name)
            after = current_metrics.get(name)
            changes.append({
                "metric": name,
                "baseline": before,
                "current": after,
                "delta": after - before if isinstance(before, (int, float)) and isinstance(after, (int, float)) else None,
            })
    return {
        "schemaVersion": 3,
        "status": "COMPARABLE" if comparable else "INCOMPARABLE_DATASET",
        "baselineCommit": (baseline.get("code") or {}).get("gitCommit"),
        "currentCommit": (current.get("code") or {}).get("gitCommit"),
        "datasetSha256": current_hash,
        "datasetMatches": baseline_hash == current_hash,
        "identityDifferences": differences,
        "changes": changes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="比较两份同契约离线 routing 报告")
    parser.add_argument("baseline", type=Path)
    parser.add_argument("current", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    current = json.loads(args.current.read_text(encoding="utf-8"))
    result = compare_routing_reports(baseline, current)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
