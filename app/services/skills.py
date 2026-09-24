from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.core.config import Settings
from app.core.enums import IntentType, RiskLevel
from app.models.entities import PsychologicalReport, UserAccount
from app.services.embedding import create_skill_embedding_backend
from app.services.skill_semantics import SemanticCandidate, SkillSemanticService, build_semantic_document


logger = logging.getLogger(__name__)
TEXT_TEMPLATE_PATTERN = re.compile(
    r"^```text[^\S\r\n]*\r?\n(?P<template>.*?)^```[^\S\r\n]*(?:\r?\n|\Z)",
    re.MULTILINE | re.DOTALL,
)
SELECTION_MODES = {"baseline", "scenario", "fixed"}
SELECTOR_VERSION = "skill-cascade-v2"
_CLAUSE_SPLIT = re.compile(r"[。！？!?；;\r\n]+")
_ENGLISH_TOKEN = re.compile(r"^[A-Za-z0-9_-]+$")


class SkillLoadError(RuntimeError):
    pass


@dataclass(frozen=True)
class SkillValidationIssue:
    level: str
    message: str


@dataclass(frozen=True)
class MindBridgeSkill:
    name: str
    description: str
    body: str
    path: Path
    agents: tuple[str, ...] = ("response",)
    intents: tuple[str, ...] = ("CHAT",)
    keywords: tuple[str, ...] = ()
    enabled: bool = True
    optional: bool = True
    priority: int = 50
    max_chars: int = 2000
    matching_version: int | None = None
    selection_mode: str | None = None
    selection_group: str | None = None
    semantic_examples: tuple[str, ...] = ()
    matching: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def prompt_context(self, limit: int | None = None, *, complete: bool = False) -> str:
        prefix = f"应用 skill: {self.name}\n"
        body = self.body.strip()
        if complete:
            if len(body) > self.max_chars:
                return ""
            context = prefix + body
            return context if limit is None or len(context) <= limit else ""
        body_limit = self.max_chars if limit is None else min(self.max_chars, max(0, limit - len(prefix)))
        rendered = body[:body_limit].rstrip()
        return prefix + rendered if rendered else ""

    def validation_issues(self) -> list[SkillValidationIssue]:
        issues: list[SkillValidationIssue] = []
        if self.path.parent.name != self.name:
            issues.append(SkillValidationIssue("WARN", f"目录名 {self.path.parent.name} 与 skill name {self.name} 不一致"))
        if "## Workflow" not in self.body:
            issues.append(SkillValidationIssue("WARN", "建议包含 ## Workflow 小节，便于人工审阅和模型稳定加载"))
        if len(self.description) < 20:
            issues.append(SkillValidationIssue("WARN", "description 太短，可能无法准确表达触发场景"))
        if self.name == "counselor_handoff_summary":
            template = TEXT_TEMPLATE_PATTERN.search(self.body)
            if template is None or not template.group("template").strip():
                issues.append(SkillValidationIssue("ERROR", "counselor_handoff_summary 必须包含完整且非空的 text 模板"))
        if self.max_chars <= 0:
            issues.append(SkillValidationIssue("ERROR", "max_chars 必须大于 0"))
        if self.matching_version != 2:
            issues.append(SkillValidationIssue("WARN", "未迁移到 matching_version=2，不参与动态选择"))
        return issues


@dataclass(frozen=True)
class SkillMatchInput:
    agent: str
    intent: str
    task_text: str
    objective: str = ""
    known_arguments: dict[str, str] = field(default_factory=dict)
    work_item_id: str = ""
    risk: RiskLevel = RiskLevel.LOW


@dataclass(frozen=True)
class SkillMatch:
    skill: MindBridgeSkill
    rule_score: float | None
    semantic_score: float | None
    selection_source: str
    matched_rule_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    prompt_context: str

    @property
    def score(self) -> float | None:
        return self.rule_score

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.skill.name,
            "score": self.rule_score,
            "rule_score": self.rule_score,
            "semantic_score": self.semantic_score,
            "selection_source": self.selection_source,
            "matched_rule_ids": list(self.matched_rule_ids),
            "reason_codes": list(self.reason_codes),
            "prompt_context": self.prompt_context,
            "optional": self.skill.optional,
            "priority": self.skill.priority,
            "selection_mode": self.skill.selection_mode,
        }


@dataclass(frozen=True)
class SkillSelectionResult:
    matches: tuple[SkillMatch, ...]
    diagnostics: dict[str, Any]


@dataclass(frozen=True)
class _RuleCandidate:
    skill: MindBridgeSkill
    points: int
    matched_rule_ids: tuple[str, ...]
    excluded: bool
    exclude_rule_ids: tuple[str, ...]


class MindBridgeSkillRegistry:
    def __init__(self, root: Path | None = None):
        self.root = root or Path(__file__).resolve().parents[2] / "skills"

    def list_skills(self) -> list[MindBridgeSkill]:
        if not self.root.exists():
            return []
        return [self._load_skill_file(path) for path in sorted(self.root.glob("*/SKILL.md"))]

    def status_items(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        items = []
        seen_names: dict[str, Path] = {}
        for skill_file in sorted(self.root.glob("*/SKILL.md")):
            try:
                skill = self._load_skill_file(skill_file)
                issues = skill.validation_issues()
            except SkillLoadError as exc:
                items.append({"name": skill_file.parent.name, "status": "FAILED", "description": str(exc),
                              "path": skill_file.relative_to(self.root.parent).as_posix(),
                              "issues": [{"level": "ERROR", "message": str(exc)}]})
                continue
            duplicate_of = seen_names.get(skill.name)
            if duplicate_of is not None:
                issues.append(SkillValidationIssue("ERROR", f"skill name {skill.name} 与 {duplicate_of.parent.name} 重复"))
            else:
                seen_names[skill.name] = skill_file
            has_error = any(issue.level == "ERROR" for issue in issues)
            items.append({"name": skill.name, "status": "FAILED" if has_error else "READY" if not issues else "WARN",
                          "description": skill.description, "path": skill.path.relative_to(self.root.parent).as_posix(),
                          "issues": [{"level": issue.level, "message": issue.message} for issue in issues],
                          "metadata": skill.metadata})
        return items

    def get_required(self, name: str) -> MindBridgeSkill:
        found = []
        for path in sorted(self.root.glob("*/SKILL.md")):
            try:
                skill = self._load_skill_file(path)
            except SkillLoadError as exc:
                if path.parent.name == name:
                    raise
                logger.warning("Skill isolated during required lookup: path=%s error=%s", path, exc)
                continue
            if skill.name == name:
                errors = [issue.message for issue in skill.validation_issues() if issue.level == "ERROR"]
                if errors:
                    raise SkillLoadError("; ".join(errors))
                found.append(skill)
        if len(found) > 1:
            raise SkillLoadError(f"duplicate required standard skill: {name}")
        if found:
            return found[0]
        raise SkillLoadError(f"required standard skill not found: {name}")

    def template_for(self, name: str) -> str:
        skill = self.get_required(name)
        match = TEXT_TEMPLATE_PATTERN.search(skill.body)
        if match is None or not match.group("template").strip():
            raise SkillLoadError(f"standard skill {name} does not define a text template")
        return match.group("template").strip()

    def _load_skill_file(self, path: Path) -> MindBridgeSkill:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise SkillLoadError(f"{path} cannot be read as UTF-8: {exc}") from exc
        metadata, body = _split_frontmatter(text, path)
        name = str(metadata.get("name") or path.parent.name).strip()
        description = str(metadata.get("description", "")).strip()
        if not name or not description or not body.strip():
            raise SkillLoadError(f"{path} is missing required name, description, or body")
        try:
            if "always_match" in metadata:
                metadata["always_match"] = _bool(metadata["always_match"], "always_match")
            raw_version = metadata.get("matching_version")
            matching_version = int(raw_version) if raw_version is not None else None
            raw_mode = metadata.get("selection_mode")
            selection_mode = str(raw_mode).strip().lower() if raw_mode is not None else None
            if selection_mode is not None and selection_mode not in SELECTION_MODES:
                raise ValueError(f"unknown selection_mode: {selection_mode}")
            if matching_version == 2 and selection_mode is None:
                raise ValueError("matching_version=2 requires selection_mode")
            examples = _string_tuple(metadata.get("semantic_examples", []))
            if selection_mode == "scenario":
                if metadata.get("always_match"):
                    raise ValueError("scenario skill cannot use always_match")
                if not str(metadata.get("selection_group") or "").strip():
                    raise ValueError("scenario skill requires selection_group")
                _validate_matching(metadata.get("matching"))
                if not examples:
                    raise ValueError("scenario skill requires semantic_examples")
            return MindBridgeSkill(
                name=name, description=description, body=body.strip(), path=path,
                agents=_string_tuple(metadata.get("agents", ["response"])),
                intents=tuple(item.upper() for item in _string_tuple(metadata.get("intents", ["CHAT"]))),
                keywords=_legacy_keywords(metadata.get("keywords", [])),
                enabled=_bool(metadata.get("enabled", True)), optional=_bool(metadata.get("optional", True), "optional"),
                priority=int(metadata.get("priority", 50)), max_chars=int(metadata.get("max_chars", 2000)),
                matching_version=matching_version, selection_mode=selection_mode,
                selection_group=str(metadata.get("selection_group") or "").strip() or None,
                semantic_examples=examples, matching=dict(metadata.get("matching") or {}), metadata=metadata,
            )
        except (TypeError, ValueError) as exc:
            raise SkillLoadError(f"{path} has invalid frontmatter: {exc}") from exc


class SkillManager:
    def __init__(self, root: Path | None = None, max_matches: int | None = None, total_chars: int | None = None,
                 *, settings: Settings | None = None, semantic_service: SkillSemanticService | None = None):
        self.settings = settings or Settings()
        self.max_matches = self.settings.skill_max_matches if max_matches is None else max_matches
        self.total_chars = self.settings.skill_total_chars if total_chars is None else total_chars
        if any(type(value) is not int or value < 0 for value in (self.max_matches, self.total_chars)):
            raise ValueError("max_matches and total_chars must be non-negative integers")
        self.registry = MindBridgeSkillRegistry(root)
        self.semantic_service = semantic_service or self._create_semantic_service()
        self._skills: tuple[MindBridgeSkill, ...] = ()
        self.errors: dict[str, str] = {}
        self.refresh()

    def _create_semantic_service(self) -> SkillSemanticService | None:
        if not self.settings.skill_semantic_enabled:
            return None
        backend = create_skill_embedding_backend(self.settings)
        return SkillSemanticService(
            backend,
            provider=self.settings.skill_embedding_provider or self.settings.knowledge_embedding_provider,
            base_url=self.settings.skill_embedding_base_url or self.settings.knowledge_embedding_base_url,
            model=self.settings.skill_embedding_model or self.settings.knowledge_embedding_model,
            embedding_version=self.settings.skill_embedding_version,
            min_similarity=self.settings.skill_semantic_min_similarity,
            min_margin=self.settings.skill_semantic_min_margin,
            budget_ms=self.settings.skill_semantic_budget_ms,
            cache_max_items=self.settings.skill_semantic_cache_max_items,
        )

    def refresh(self) -> None:
        valid, errors, seen_names = [], {}, {}
        if self.registry.root.exists():
            for path in sorted(self.registry.root.glob("*/SKILL.md")):
                try:
                    skill = self.registry._load_skill_file(path)
                    previous = seen_names.get(skill.name)
                    if previous is not None:
                        raise SkillLoadError(f"skill name {skill.name} duplicated by {previous.parent.name}")
                    seen_names[skill.name] = path
                    issues = [issue.message for issue in skill.validation_issues() if issue.level == "ERROR"]
                    if issues:
                        raise SkillLoadError("; ".join(issues))
                    valid.append(skill)
                except SkillLoadError as exc:
                    errors[path.parent.name] = str(exc)
                    logger.warning("Skill isolated: name=%s error=%s", path.parent.name, exc)
        self._skills, self.errors = tuple(valid), errors

    def match(self, agent: str, intent: str, text: str, risk: RiskLevel = RiskLevel.LOW) -> list[SkillMatch]:
        return list(self.select(SkillMatchInput(agent, intent, text, risk=risk)).matches)

    def select(self, request: SkillMatchInput) -> SkillSelectionResult:
        agent, intent = request.agent.strip().lower(), request.intent.strip().upper()
        risk = request.risk if isinstance(request.risk, RiskLevel) else RiskLevel(str(request.risk))
        diagnostics: dict[str, Any] = {
            "selectorVersion": SELECTOR_VERSION, "workItemId": request.work_item_id,
            "eligibleSkillIds": [], "candidates": [], "groups": [], "qualifiedSkillIds": [],
            "injectedSkillIds": [], "budgetRejectedSkillIds": [],
        }
        eligible = [skill for skill in self._skills if self._in_scope(skill, agent, intent, risk)]
        migrated = [skill for skill in eligible if skill.matching_version == 2]
        diagnostics["eligibleSkillIds"] = [skill.name for skill in migrated]
        baselines = [skill for skill in migrated if skill.selection_mode == "baseline"]
        scenario_skills = [skill for skill in migrated if skill.selection_mode == "scenario"]
        accepted: list[tuple[_RuleCandidate, str, float | None]] = []
        if not self.settings.skill_scenario_enabled or not request.task_text.strip() or self.max_matches == 0 or self.total_chars == 0:
            reason = "SCENARIO_DISABLED" if not self.settings.skill_scenario_enabled else "RULE_BELOW_THRESHOLD"
            if scenario_skills:
                diagnostics["groups"].append({"group": "*", "decision": reason})
        else:
            candidates = [_score_skill(skill, request) for skill in scenario_skills]
            diagnostics["candidates"] = [{
                "skillId": item.skill.name, "group": item.skill.selection_group, "rulePoints": item.points,
                "ruleScore": item.points / 100.0, "matchedRuleIds": list(item.matched_rule_ids),
                "excluded": item.excluded, "excludeRuleIds": list(item.exclude_rule_ids),
            } for item in candidates]
            groups: dict[str, list[_RuleCandidate]] = {}
            for candidate in candidates:
                groups.setdefault(candidate.skill.selection_group or "", []).append(candidate)
            query, query_cache = _semantic_query(request, scenario_skills), {}
            for group_name in sorted(groups):
                chosen = self._select_group(group_name, groups[group_name], query, query_cache, diagnostics)
                if chosen is not None:
                    accepted.append(chosen)
        diagnostics["qualifiedSkillIds"] = [item[0].skill.name for item in accepted]
        matches: list[SkillMatch] = []
        remaining = self.total_chars
        for skill in sorted(baselines, key=lambda item: (-item.priority, item.name)):
            separator = 2 if matches else 0
            context = skill.prompt_context(remaining - separator, complete=True)
            if not context:
                diagnostics["budgetRejectedSkillIds"].append(skill.name)
                continue
            matches.append(SkillMatch(skill, None, None, "BASELINE", (), ("BASELINE",), context))
            remaining -= len(context) + separator
        ordered = sorted(accepted, key=lambda item: (-item[0].points, -item[0].skill.priority, item[0].skill.name))
        scenario_count = 0
        for candidate, source, semantic_score in ordered:
            if scenario_count >= self.max_matches:
                diagnostics["budgetRejectedSkillIds"].append(candidate.skill.name)
                continue
            separator = 2 if matches else 0
            context = candidate.skill.prompt_context(remaining - separator, complete=True)
            if not context:
                diagnostics["budgetRejectedSkillIds"].append(candidate.skill.name)
                continue
            matches.append(SkillMatch(candidate.skill, candidate.points / 100.0, semantic_score, source,
                                      candidate.matched_rule_ids, (source,), context))
            remaining -= len(context) + separator
            scenario_count += 1
        diagnostics["injectedSkillIds"] = [item.skill.name for item in matches]
        return SkillSelectionResult(tuple(matches), diagnostics)

    def _select_group(self, group_name, candidates, query, query_cache, diagnostics):
        high = sorted((item for item in candidates if not item.excluded and item.points >= self.settings.skill_rule_min_points),
                      key=lambda item: (-item.points, item.skill.name))
        group_diag: dict[str, Any] = {
            "group": group_name, "highSkillIds": [item.skill.name for item in high], "nearSkillIds": [],
            "ruleMarginPoints": high[0].points - high[1].points if len(high) > 1 else None,
            "semanticScores": {}, "semanticMargin": None, "embeddingCalled": False,
            "embeddingCallCount": 0, "cacheHits": 0, "elapsedMs": 0,
        }
        diagnostics["groups"].append(group_diag)
        if not high:
            group_diag["decision"] = "RULE_BELOW_THRESHOLD"
            return None
        if len(high) == 1:
            group_diag.update(decision="RULE_SINGLE_HIGH", selectedSkillId=high[0].skill.name)
            return high[0], "RULE_SINGLE_HIGH", None
        if high[0].points - high[1].points >= self.settings.skill_rule_min_margin_points:
            group_diag.update(decision="RULE_CLEAR_WINNER", selectedSkillId=high[0].skill.name)
            return high[0], "RULE_CLEAR_WINNER", None
        near = [item for item in high if high[0].points - item.points < self.settings.skill_rule_min_margin_points]
        group_diag["nearSkillIds"] = [item.skill.name for item in near]
        if not self.settings.skill_semantic_enabled or self.semantic_service is None:
            group_diag["decision"] = "SEMANTIC_DISABLED"
            return None
        semantic = self.semantic_service.disambiguate(
            query,
            [SemanticCandidate(item.skill.name, build_semantic_document(item.skill.description, item.skill.semantic_examples)) for item in near],
            query_cache=query_cache,
        )
        group_diag.update({"decision": semantic.reason, "selectedSkillId": semantic.selected_id,
                           "semanticScores": semantic.scores, "semanticMargin": semantic.margin,
                           "embeddingCalled": semantic.embedding_called, "embeddingCallCount": semantic.embedding_call_count,
                           "cacheHits": semantic.cache_hits, "elapsedMs": semantic.elapsed_ms})
        if semantic.selected_id is None:
            return None
        chosen = next(item for item in near if item.skill.name == semantic.selected_id)
        return chosen, "SEMANTIC_SELECTED", semantic.scores[semantic.selected_id]

    @staticmethod
    def _in_scope(skill: MindBridgeSkill, agent: str, intent: str, risk: RiskLevel) -> bool:
        if not skill.enabled or agent not in {item.lower() for item in skill.agents} or intent not in skill.intents:
            return False
        if risk == RiskLevel.HIGH and intent != IntentType.RISK.value:
            return False
        return skill.selection_mode != "fixed"


class MindBridgeSkillLibrary:
    @staticmethod
    def registry() -> MindBridgeSkillRegistry:
        return MindBridgeSkillRegistry()

    @staticmethod
    def list_skills() -> list[MindBridgeSkill]:
        return MindBridgeSkillLibrary.registry().list_skills()

    @staticmethod
    def status_items() -> list[dict]:
        return MindBridgeSkillLibrary.registry().status_items()

    @staticmethod
    def response_skill_context(intent: IntentType, risk: RiskLevel, text: str) -> str:
        route = IntentType.RISK.value if risk == RiskLevel.HIGH else intent.value
        return "\n\n".join(match.prompt_context for match in SkillManager().match("response", route, text, risk))

    @staticmethod
    def response_skill_names(intent: IntentType, risk: RiskLevel, text: str) -> list[str]:
        route = IntentType.RISK.value if risk == RiskLevel.HIGH else intent.value
        return [match.skill.name for match in SkillManager().match("response", route, text, risk)]

    @staticmethod
    def high_risk_safety_plan_prompt() -> str:
        return MindBridgeSkillLibrary.registry().get_required("high_risk_safety_plan").prompt_context()

    @staticmethod
    def counselor_handoff_summary(report: PsychologicalReport, user: UserAccount | None) -> str:
        template = MindBridgeSkillLibrary.registry().template_for("counselor_handoff_summary")
        student = _student_label(user, report.user_id)
        urgency = "立即跟进" if report.risk_level == RiskLevel.HIGH.value else "尽快跟进"
        next_steps = [f"{urgency}，确认学生当前位置、身边是否有人陪伴，以及当前是否安全。",
                      "联系学生本人或其可用的现实支持人，并记录已采取的联系方式。",
                      "必要时联系校园保卫、心理中心值班老师或当地紧急救助。",
                      "将后续安排、接手人和下一次复访时间写入个案备注。"]
        return _render_template(template, {"report_id": str(report.id), "student": student,
            "risk_level": report.risk_level, "emotion": report.emotion, "confidence": f"{report.confidence:.2f}",
            "summary": report.summary, "next_steps": "\n".join(f"- {step}" for step in next_steps),
            "content_excerpt": _truncate(report.content, 700)})


def _score_skill(skill: MindBridgeSkill, request: SkillMatchInput) -> _RuleCandidate:
    text = _normalize(request.task_text)
    clauses = tuple(item for item in (_normalize(part) for part in _CLAUSE_SPLIT.split(request.task_text)) if item)
    matching = skill.matching
    exclude_ids = tuple(rule["id"] for rule in matching.get("exclude", []) if _rule_matches(rule, text, clauses, request.known_arguments))
    matched: list[str] = []
    domain_level = goal_level = 0
    for level, value in (("strong", 2), ("weak", 1)):
        hits = [rule["id"] for rule in matching.get("domain", {}).get(level, []) if _rule_matches(rule, text, clauses, request.known_arguments)]
        if hits and domain_level == 0:
            domain_level, matched = value, [*matched, *hits]
        hits = [rule["id"] for rule in matching.get("goal", {}).get(level, []) if _rule_matches(rule, text, clauses, request.known_arguments)]
        if hits and goal_level == 0:
            goal_level, matched = value, [*matched, *hits]
    context_hits = [rule["id"] for rule in matching.get("context", []) if _rule_matches(rule, text, clauses, request.known_arguments)]
    matched.extend(context_hits)
    points = (60 if domain_level == 2 else 30 if domain_level == 1 else 0)
    points += 30 if goal_level == 2 else 15 if goal_level == 1 else 0
    points += 10 if context_hits else 0
    return _RuleCandidate(skill, points, tuple(dict.fromkeys(matched)), bool(exclude_ids), exclude_ids)


def _rule_matches(rule: dict[str, Any], text: str, clauses: tuple[str, ...], known_arguments: dict[str, str]) -> bool:
    if "any_of" in rule:
        return any(_contains(text, phrase) for phrase in rule["any_of"])
    if "all_of" in rule:
        return any(all(any(_contains(clause, phrase) for phrase in alternatives) for alternatives in rule["all_of"]) for clause in clauses)
    return any(str(known_arguments.get(key) or "").strip() for key in rule.get("argument_keys", []))


def _contains(text: str, phrase: str) -> bool:
    needle = _normalize(phrase)
    if not needle:
        return False
    if _ENGLISH_TOKEN.fullmatch(needle):
        return re.search(rf"(?<![A-Za-z0-9_]){re.escape(needle)}(?![A-Za-z0-9_])", text) is not None
    return needle in text


def _semantic_query(request: SkillMatchInput, skills: list[MindBridgeSkill]) -> str:
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    def add(label: str, value: str) -> None:
        normalized = _normalize(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            rows.append((label, value.strip()))
    add("当前任务", request.task_text)
    add("目标", request.objective)
    relevant_keys = sorted({key for skill in skills for rule in skill.matching.get("context", []) for key in rule.get("argument_keys", [])})
    for key in relevant_keys:
        value = str(request.known_arguments.get(key) or "").strip()
        if value:
            add("相关条件", value)
    return "\n".join(f"{label}：{value}" for label, value in rows)


def _validate_matching(value: Any) -> None:
    if not isinstance(value, dict) or set(value) - {"domain", "goal", "context", "exclude"}:
        raise ValueError("scenario matching must be a mapping with known sections")
    seen: set[str] = set()
    rules: list[tuple[dict[str, Any], str]] = []
    for section in ("domain", "goal"):
        levels = value.get(section)
        if not isinstance(levels, dict) or set(levels) - {"strong", "weak"}:
            raise ValueError(f"matching.{section} must contain strong/weak lists")
        for level in ("strong", "weak"):
            rows = levels.get(level, [])
            if not isinstance(rows, list):
                raise ValueError(f"matching.{section}.{level} must be a list")
            rules.extend((row, section) for row in rows)
    for section in ("context", "exclude"):
        rows = value.get(section, [])
        if not isinstance(rows, list):
            raise ValueError(f"matching.{section} must be a list")
        rules.extend((row, section) for row in rows)
    for rule, section in rules:
        if not isinstance(rule, dict) or not str(rule.get("id") or "").strip():
            raise ValueError("every matching rule requires an id")
        rule_id = str(rule["id"]).strip()
        if rule_id in seen:
            raise ValueError(f"duplicate matching rule id: {rule_id}")
        seen.add(rule_id)
        operators = [key for key in ("any_of", "all_of", "argument_keys") if key in rule]
        if len(operators) != 1 or set(rule) != {"id", operators[0]}:
            raise ValueError(f"rule {rule_id} must use exactly one supported operator")
        operator, data = operators[0], rule[operators[0]]
        if operator == "argument_keys" and section != "context":
            raise ValueError(f"rule {rule_id} uses argument_keys outside context")
        if operator in {"any_of", "argument_keys"}:
            if not isinstance(data, list) or not data or any(not str(item).strip() for item in data):
                raise ValueError(f"rule {rule_id} has an invalid {operator}")
        elif not isinstance(data, list) or not data or any(not isinstance(group, list) or not group or any(not str(item).strip() for item in group) for group in data):
            raise ValueError(f"rule {rule_id} has an invalid all_of")


def _split_frontmatter(text: str, path: Path) -> tuple[dict, str]:
    match = re.match(r"\A---\s*\r?\n(?P<yaml>.*?)\r?\n---\s*(?:\r?\n|\Z)(?P<body>.*)\Z", text, re.DOTALL)
    if match is None:
        raise SkillLoadError(f"{path} is missing or has invalid YAML frontmatter")
    try:
        metadata = yaml.safe_load(match.group("yaml")) or {}
    except yaml.YAMLError as exc:
        raise SkillLoadError(f"{path} has invalid YAML frontmatter: {exc}") from exc
    if not isinstance(metadata, dict):
        raise SkillLoadError(f"{path} frontmatter must be a mapping")
    return metadata, match.group("body").strip()


def _string_tuple(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if not isinstance(value, list):
        raise ValueError("list field must be a list or comma-separated string")
    return tuple(str(item).strip() for item in value if str(item).strip())


def _legacy_keywords(value) -> tuple[str, ...]:
    if isinstance(value, dict):
        return tuple(str(item).strip() for key in ("domain", "goal", "context") for item in value.get(key, []) if str(item).strip())
    return _string_tuple(value)


def _bool(value, field_name: str = "enabled") -> bool:
    if isinstance(value, bool):
        return value
    if str(value).lower() in {"true", "1", "yes"}:
        return True
    if str(value).lower() in {"false", "0", "no"}:
        return False
    raise ValueError(f"{field_name} must be boolean")


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).lower().split())


def _render_template(template: str, values: dict[str, str]) -> str:
    return re.sub(r"\{\{([^{}]+)\}\}", lambda match: values.get(match.group(1), match.group(0)), template)


def _student_label(user: UserAccount | None, user_id: int) -> str:
    if user is None:
        return f"userId={user_id}"
    return f"{user.display_name} ({user.username})" if user.display_name else user.username


def _truncate(text: str, limit: int) -> str:
    normalized = " ".join((text or "").split())
    return normalized if len(normalized) <= limit else f"{normalized[:limit - 3]}..."
