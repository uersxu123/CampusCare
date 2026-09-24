from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from typing import Any

from app.agents.routing import classify_route
from app.core.config import Settings
from app.core.enums import IntentType
from app.evaluation.config import (
    ROUTING_PRIMARY_INTENT_MIN,
    ROUTING_ROUTE_PLAN_EXACT_MATCH_MIN,
    ROUTING_SINGLE_GOAL_NO_OVERSPLIT_MIN,
    ROUTING_WORK_ITEM_COUNT_MIN,
)
from app.evaluation.contracts import RoutingCase
from app.services.context_builder import build_understanding_context_view
from app.services.understanding import UnderstandingService

_FAST_PLAN_SOURCES = {"FAST_RULE_EMBEDDING"}


class ProductionRoutingEvaluator:
    def __init__(self, settings: Settings, understanding_service: UnderstandingService, *, fusion=None):
        self.settings = settings
        self.understanding_service = understanding_service
        self.fusion = fusion

    def evaluate(self, cases: list[RoutingCase]) -> dict:
        results = []
        errors = []
        for case in cases:
            try:
                results.append(self.evaluate_case(case))
            except Exception as exc:
                errors.append({"caseHash": _hash(case.id), "errorCode": type(exc).__name__})
                results.append(_failed_case(case))
        hard_rows = [row for row in results if row["expected"]["hardDataEdges"]]
        single_rows = [row for row in results if "single-goal" in row["tags"] and row["expected"]["workItemCount"] == 1]
        metric_errors = []
        if not hard_rows: metric_errors.append({"errorCode": "ROUTING_DATASET_MISSING_HARD_DATA_CASES"})
        if not single_rows: metric_errors.append({"errorCode": "ROUTING_DATASET_MISSING_SINGLE_GOAL_CASES"})
        metrics = {
            "primaryIntentAccuracy": _mean(row["primaryIntentCorrect"] for row in results),
            "workItemCountAccuracy": _mean(row["workItemCountCorrect"] for row in results),
            "routePlanExactMatch": _mean(row["routePlanCorrect"] for row in results),
            "hardDataDependencyAccuracy": _mean(row["hardDataEdgesCorrect"] for row in hard_rows),
            "singleGoalNoOversplitRate": _mean(row["actual"].get("workItemCount") == 1 for row in single_rows),
            "fastRouteRate": _mean(row["actual"].get("planSource") in _FAST_PLAN_SOURCES for row in results),
            "plannerInvocationRate": _mean(bool(row["actual"].get("llmInvoked")) for row in results),
            "fastRouteExactAccuracy": _mean(
                row["routePlanCorrect"]
                for row in results
                if row["actual"].get("planSource") in _FAST_PLAN_SOURCES
            ),
        }
        gates = {
            "primaryIntentAccuracy": metrics["primaryIntentAccuracy"] >= ROUTING_PRIMARY_INTENT_MIN,
            "workItemCountAccuracy": metrics["workItemCountAccuracy"] >= ROUTING_WORK_ITEM_COUNT_MIN,
            "routePlanExactMatch": metrics["routePlanExactMatch"] >= ROUTING_ROUTE_PLAN_EXACT_MATCH_MIN,
            "hardDataDependencyAccuracy": metrics["hardDataDependencyAccuracy"] == 1.0,
            "singleGoalNoOversplitRate": metrics["singleGoalNoOversplitRate"] >= ROUTING_SINGLE_GOAL_NO_OVERSPLIT_MIN,
        }
        plan_sources = Counter(row["actual"].get("planSource") for row in results)
        fallbacks = Counter(row["actual"].get("fallbackReason") for row in results if row["actual"].get("fallbackReason"))
        return {"metrics": metrics, "gates": gates, "metricErrors": metric_errors, "errors": errors, "passed": bool(results) and all(gates.values()) and not metric_errors and not errors, "planSourceCounts": dict(plan_sources), "fallbackReasonCounts": dict(fallbacks), "slices": self._slices(results), "results": results}

    def evaluate_case(self, case: RoutingCase) -> dict:
        current = case.messages[-1].content
        history = [item.model_dump() for item in case.messages[:-1]]
        previous_users = [item["content"] for item in history if item["role"] == "user"]
        view = build_understanding_context_view(
            current,
            {"text": previous_users[-1]} if previous_users else None,
            [], history, None, self.settings.intent_context_max_tokens,
        )
        decision = classify_route(
            current, view, self.understanding_service.classify, self.settings, current, fusion=self.fusion,
        )
        payload = decision.as_payload()
        metadata = decision.diagnostics.as_metadata()
        work_items = payload["workItems"]
        index_by_id = {item["workItemId"]: index for index, item in enumerate(work_items)}
        hard = sorted(
            (index_by_id[parent_id], target_index)
            for target_index, item in enumerate(work_items)
            for parent_id in item["dependsOn"]
        )
        order = [tuple(edge) for edge in metadata["orderOnlyEdges"]]
        actual = {
            "primaryIntent": payload["primaryIntent"], "intents": payload["intents"], "workItemCount": len(work_items),
            "workItemIntents": [item["intent"] for item in work_items],
            "sourceTextHashes": [{"sha256": hashlib.sha256(item["sourceText"].encode("utf-8")).hexdigest(), "length": len(item["sourceText"])} for item in work_items],
            "hardDataEdges": sorted(hard), "orderOnlyEdges": sorted(order),
            "missingArgumentNamesByWorkItem": [sorted(item["name"] for item in work["missingArguments"]) for work in work_items],
            "contextRelation": metadata["contextRelation"], "capacityExceeded": "WORK_ITEM_LIMIT_EXCEEDED" in payload["reasonCodes"],
            "planSource": metadata["planSource"], "fallbackReason": metadata["fallbackReason"],
            "llmInvoked": bool(metadata.get("llmInvoked")),
            "routingMode": metadata.get("routingMode", "NORMAL"),
            "fastRouteReason": metadata.get("fastRouteReason", ""),
            "latencyMs": int(metadata.get("latencyMs") or 0),
            "routingScoreLatencyMs": int(metadata.get("routingScoreLatencyMs") or 0),
        }
        expected = case.expected.model_dump(mode="json")
        fragments_correct = len(work_items) == len(expected["sourceTextFragments"]) and all(all(fragment in work_items[index]["sourceText"] for fragment in fragments) for index, fragments in enumerate(expected["sourceTextFragments"]))
        checks = {
            "primaryIntentCorrect": actual["primaryIntent"] == expected["primaryIntent"],
            "intentsCorrect": actual["intents"] == expected["intents"],
            "workItemCountCorrect": actual["workItemCount"] == expected["workItemCount"],
            "workItemIntentsCorrect": actual["workItemIntents"] == expected["workItemIntents"],
            "sourceTextFragmentsCorrect": fragments_correct,
            "hardDataEdgesCorrect": actual["hardDataEdges"] == sorted(tuple(edge) for edge in expected["hardDataEdges"]),
            "missingArgumentsCorrect": actual["missingArgumentNamesByWorkItem"] == expected["missingArgumentNamesByWorkItem"],
            "capacityExceededCorrect": actual["capacityExceeded"] == expected["capacityExceeded"],
        }
        expected_order_pairs = {frozenset(edge) for edge in expected["orderOnlyEdges"]}
        order_hard_errors = sum(frozenset(edge) in expected_order_pairs for edge in actual["hardDataEdges"])
        return {"caseHash": _hash(case.id), "tags": case.tags, "expected": expected, "actual": actual, **checks, "orderOnlyHardErrors": order_hard_errors, "routePlanCorrect": all(checks.values())}

    @staticmethod
    def _slices(results):
        grouped = defaultdict(list)
        for row in results:
            for tag in row["tags"]: grouped[tag].append(row)
        return {tag: {"caseCount": len(rows), "routePlanExactMatch": _mean(row["routePlanCorrect"] for row in rows)} for tag, rows in sorted(grouped.items())}


def _failed_case(case):
    expected = case.expected.model_dump(mode="json")
    return {"caseHash": _hash(case.id), "tags": case.tags, "expected": expected, "actual": {}, "primaryIntentCorrect": False, "workItemCountCorrect": False, "contextRelationCorrect": False, "hardDataEdgesCorrect": False, "orderOnlyHardErrors": 0, "routePlanCorrect": False}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _mean(values) -> float:
    rows = list(values)
    return sum(rows) / len(rows) if rows else 0.0
