from __future__ import annotations

from dataclasses import dataclass

from app.agents.routing import RoutePlan
from app.core.enums import IntentType
from app.services.clarification_handlers import get_clarification_handler
from app.services.clarification_models import MissingArgument


PRIORITY_ORDER = {"NORMAL": 2, "HIGH": 3, "CRITICAL": 4}


@dataclass(frozen=True)
class ClarificationTarget:
    plan_id: str
    work_item_id: str
    intent: IntentType
    objective: str
    known_arguments: dict[str, str]
    missing_argument: MissingArgument


class ClarificationPolicy:
    def select(self, route_plan: dict, *, asked_fields: set[tuple[str, str]] | None = None) -> ClarificationTarget | None:
        asked_fields = asked_fields or set()
        try:
            plan = RoutePlan.from_payload(route_plan)
        except ValueError:
            return None
        downstream = _downstream_counts(plan.as_payload()["workItems"])
        candidates: list[tuple[int, int, int, ClarificationTarget]] = []
        for order, item in enumerate(plan.work_items):
            handler = get_clarification_handler(item.intent)
            if handler is None:
                continue
            for argument in item.missing_arguments:
                if (item.work_item_id, argument.name) in asked_fields:
                    continue
                target = ClarificationTarget(plan.plan_id, item.work_item_id, item.intent, item.objective, dict(item.known_arguments), argument)
                candidates.append((downstream.get(item.work_item_id, 0), PRIORITY_ORDER[item.priority], -order, target))
        if not candidates:
            return None
        candidates.sort(key=lambda value: value[:3], reverse=True)
        return candidates[0][3]


def _downstream_counts(work_items: list[dict]) -> dict[str, int]:
    children: dict[str, set[str]] = {}
    for item in work_items:
        current = item["workItemId"]
        children.setdefault(current, set())
        for parent in item["dependsOn"]:
            children.setdefault(parent, set()).add(current)

    def descendants(node: str, seen: set[str]) -> set[str]:
        result: set[str] = set()
        for child in children.get(node, set()):
            if child not in seen:
                result.add(child)
                result.update(descendants(child, {*seen, child}))
        return result

    return {node: len(descendants(node, {node})) for node in children}
