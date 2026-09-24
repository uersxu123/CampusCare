"""Validate and stably normalize the reviewed five-intent routing V3 dataset."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.evaluation.contracts import RoutingCase
from app.evaluation.dataset import load_routing_cases


OUTPUT = PROJECT_ROOT / "app/evaluation/datasets/routing-v3.jsonl"


def build() -> list[RoutingCase]:
    cases = load_routing_cases(OUTPUT)
    tags = {tag for case in cases for tag in case.tags}
    intents = {intent.value for case in cases for intent in case.expected.workItemIntents}
    required_tags = {"continue", "refine", "correction", "ambiguous", "hard-data", "order-only", "single-goal", "compound", "capacity"}
    if intents != {"CHAT", "ACADEMIC", "CAMPUS", "MENTAL", "RISK"}:
        raise ValueError("routing-v3 必须覆盖五类 Intent")
    if not required_tags.issubset(tags):
        raise ValueError(f"routing-v3 缺少切片: {sorted(required_tags - tags)}")
    return sorted(cases, key=lambda item: item.id)


def main() -> int:
    cases = build()
    content = "".join(json.dumps(case.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for case in cases)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="routing-v3-", suffix=".jsonl", dir=OUTPUT.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        load_routing_cases(Path(temporary))
        os.replace(temporary, OUTPUT)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f"wrote {len(cases)} cases to {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
