from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.evaluation.contracts import EndToEndCase, RoutingCase


T = TypeVar("T", bound=BaseModel)


class DatasetError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_jsonl(path: Path, model: type[T]) -> list[T]:
    if not path.is_file():
        raise DatasetError(f"评测数据集不存在: {path}")
    rows: list[T] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                payload = json.loads(raw)
                row = model.model_validate(payload)
            except (json.JSONDecodeError, ValidationError) as exc:
                raise DatasetError(f"{path}:{line_number} 数据无效: {exc}") from exc
            case_id = str(getattr(row, "id"))
            if case_id in seen:
                raise DatasetError(f"数据集 case id 重复: {case_id}")
            seen.add(case_id)
            rows.append(row)
    if not rows:
        raise DatasetError(f"评测数据集为空: {path}")
    return rows


def load_routing_cases(path: Path) -> list[RoutingCase]:
    return load_jsonl(path, RoutingCase)


def load_e2e_cases(path: Path) -> list[EndToEndCase]:
    return load_jsonl(path, EndToEndCase)


def filter_cases(cases: list[T], *, case_id: str | None = None, tag: str | None = None) -> list[T]:
    selected = [
        item
        for item in cases
        if (case_id is None or getattr(item, "id") == case_id)
        and (tag is None or tag in getattr(item, "tags", []))
    ]
    if (case_id or tag) and not selected:
        raise DatasetError("筛选条件没有匹配任何评测 case")
    return selected


def dataset_descriptor(path: Path, cases: list[BaseModel]) -> dict:
    distribution: Counter[str] = Counter()
    for case in cases:
        action = getattr(case, "expected_action", None)
        expected = getattr(case, "expected", None)
        route = getattr(expected, "primaryIntent", None) or getattr(expected, "primary_intent", None)
        value = getattr(action or route, "value", action or route or "UNKNOWN")
        distribution[str(value)] += 1
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "caseCount": len(cases),
        "distribution": dict(sorted(distribution.items())),
    }
