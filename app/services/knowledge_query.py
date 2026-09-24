from __future__ import annotations

import hashlib
import math
import re
from dataclasses import asdict, dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path

import yaml

from app.core.enums import KnowledgeDomain


class KnowledgeFacet(str, Enum):
    ELIGIBILITY = "ELIGIBILITY"
    MATERIALS = "MATERIALS"
    STEPS = "STEPS"
    CHANNEL = "CHANNEL"
    DEADLINE = "DEADLINE"
    PROCESSING_TIME = "PROCESSING_TIME"
    CONTACT = "CONTACT"
    COST = "COST"
    SCOPE = "SCOPE"
    POLICY_BASIS = "POLICY_BASIS"


class ConceptMode(str, Enum):
    CONTROLLED = "CONTROLLED"
    OPEN = "OPEN"


@dataclass(frozen=True)
class CanonicalConcept:
    concept_id: str
    canonical: str
    aliases: tuple[str, ...]
    required: bool = True
    domain: str = ""
    preferred_tags: tuple[str, ...] = ()
    required_scope_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class QueryVariant:
    kind: str
    text: str
    weight: float


@dataclass(frozen=True)
class KnowledgeQuerySpec:
    question_id: str
    original_question: str
    normalized_question: str
    domain: str
    concept_mode: ConceptMode
    concepts: tuple[CanonicalConcept, ...]
    open_concept_text: str | None
    required_facets: tuple[KnowledgeFacet, ...]
    optional_facets: tuple[KnowledgeFacet, ...]
    site: str | None
    freshness_required: bool
    allowed_source_types: tuple[str, ...]
    preferred_source_types: tuple[str, ...]
    variants: tuple[QueryVariant, ...]
    requires_authoritative_local_info: bool

    def as_dict(self) -> dict:
        value = asdict(self)
        value["concept_mode"] = self.concept_mode.value
        value["required_facets"] = [item.value for item in self.required_facets]
        value["optional_facets"] = [item.value for item in self.optional_facets]
        return value


@dataclass(frozen=True)
class FacetDefinition:
    query_terms: tuple[str, ...]
    evidence_patterns: tuple[str, ...]


class KnowledgeTaxonomy:
    def __init__(self, path: Path):
        self.path = path
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("检索 taxonomy 必须是 YAML 对象")
        self.version = int(payload.get("version") or 0)
        if self.version <= 0:
            raise ValueError("检索 taxonomy version 必填")
        self.sites = self._load_sites(payload.get("sites"))
        self.facets = self._load_facets(payload.get("facet_terms"))
        self.concepts = self._load_concepts(payload.get("concepts"))

    def build_query_spec(
        self,
        question: str,
        *,
        domain_hint: str | None = None,
        context_question: str | None = None,
    ) -> KnowledgeQuerySpec:
        original = (question or "").strip()
        if not original:
            raise ValueError("知识查询问题不能为空")
        site = self._match_site(original)
        matches = self._match_concepts(original)
        normalized = original
        if not matches and context_question:
            context_matches = self._match_concepts(context_question)
            if context_matches:
                matches = context_matches
                normalized = f"{context_question.strip()} {original}".strip()
        facets = self._match_facets(original)
        if not facets and any(term in original for term in ("怎么办", "怎么弄", "如何")):
            facets.append(KnowledgeFacet.STEPS)

        if matches:
            concept_mode = ConceptMode.CONTROLLED
            open_text = None
            domain = self._domain_for_matches(matches, domain_hint)
        else:
            concept_mode = ConceptMode.OPEN
            open_text = self._open_concept(original)
            domain = self._valid_domain(domain_hint) or KnowledgeDomain.CAMPUS_SERVICE.value
        freshness = any(term in original for term in ("今年", "最新", "当前", "现在", "截止", "本学期"))
        variants = self._variants(original, matches, facets, open_text)
        explicit_authority = any(term in original for term in _INSTITUTIONAL_TERMS)
        authoritative_facets = {
            KnowledgeFacet.ELIGIBILITY,
            KnowledgeFacet.MATERIALS,
            KnowledgeFacet.CHANNEL,
            KnowledgeFacet.DEADLINE,
            KnowledgeFacet.PROCESSING_TIME,
            KnowledgeFacet.CONTACT,
            KnowledgeFacet.COST,
            KnowledgeFacet.SCOPE,
            KnowledgeFacet.POLICY_BASIS,
        }
        process_requires_authority = (
            KnowledgeFacet.STEPS in facets
            and (
                explicit_authority
                or any(item.concept_id == "COUNSELING_APPOINTMENT" for item in matches)
            )
        )
        eligibility_question = bool(matches) and any(
            term in original for term in ("还能", "能不能", "是否", "可不可以", "可以吗")
        )
        authoritative = (
            explicit_authority
            or freshness
            or bool(set(facets).intersection(authoritative_facets))
            or process_requires_authority
            or eligibility_question
        )
        return KnowledgeQuerySpec(
            question_id=hashlib.sha256(original.encode("utf-8")).hexdigest()[:16],
            original_question=original,
            normalized_question=normalized,
            domain=domain,
            concept_mode=concept_mode,
            concepts=tuple(matches),
            open_concept_text=open_text,
            required_facets=tuple(facets),
            optional_facets=(),
            site=site,
            freshness_required=freshness,
            allowed_source_types=(),
            preferred_source_types=("OFFICIAL_POLICY", "OFFICIAL_HANDBOOK", "OFFICIAL_SERVICE_GUIDE"),
            variants=tuple(variants[:3]),
            requires_authoritative_local_info=authoritative,
        )

    def evidence_matches_concepts(
        self,
        spec: KnowledgeQuerySpec,
        *,
        title: str,
        section_title: str,
        content: str,
        tags: tuple[str, ...] = (),
        metadata_only_allowed: bool = False,
    ) -> bool:
        section_fields = section_title.lower()
        content_fields = content.lower()
        passage_fields = " ".join((section_fields, content_fields))
        metadata_fields = " ".join((title, " ".join(tags))).lower()
        if spec.concept_mode == ConceptMode.CONTROLLED:
            required = [item for item in spec.concepts if item.required]
            return bool(required) and all(
                any(
                    term.lower() in content_fields
                    or (
                        term.lower() in section_fields
                        and any(anchor.lower() in content_fields for anchor in concept.preferred_tags)
                    )
                    or (metadata_only_allowed and term.lower() in metadata_fields)
                    for term in (concept.canonical, *concept.aliases)
                )
                or (
                    metadata_only_allowed
                    and any(tag.lower() in metadata_fields for tag in concept.preferred_tags)
                )
                for concept in required
            )
        phrase = (spec.open_concept_text or "").lower().strip()
        if len(phrase) < 2:
            return False
        metadata = " ".join((title, section_title, " ".join(tags))).lower()
        if phrase in metadata:
            return True
        fields = f"{passage_fields} {metadata_fields if metadata_only_allowed else ''}"
        tokens = _open_tokens(phrase)
        signals = {token for token in tokens if token in fields}
        return len(signals) >= max(2, math.ceil(len(tokens) * 0.6))

    def covered_facets(self, spec: KnowledgeQuerySpec, evidence_text: str) -> set[KnowledgeFacet]:
        covered: set[KnowledgeFacet] = set()
        for facet in (*spec.required_facets, *spec.optional_facets):
            definition = self.facets[facet]
            matched = any(pattern in evidence_text for pattern in definition.evidence_patterns)
            if facet == KnowledgeFacet.CONTACT and "电话" in spec.original_question:
                matched = bool(re.search(r"(?<!\d)(?:0\d{2,3}[-\s]?)?\d{7,8}(?!\d)", evidence_text))
            if matched:
                covered.add(facet)
        return covered

    def _load_sites(self, raw) -> dict[str, tuple[str, ...]]:
        if not isinstance(raw, dict):
            raise ValueError("taxonomy sites 必须是对象")
        aliases_seen: dict[str, str] = {}
        result = {}
        for site, config in raw.items():
            aliases = tuple(str(item).strip() for item in (config or {}).get("aliases", []) if str(item).strip())
            for alias in aliases:
                if alias in aliases_seen and aliases_seen[alias] != site:
                    raise ValueError(f"site alias 冲突: {alias}")
                aliases_seen[alias] = str(site)
            result[str(site)] = aliases
        return result

    def _load_facets(self, raw) -> dict[KnowledgeFacet, FacetDefinition]:
        if not isinstance(raw, dict):
            raise ValueError("taxonomy facet_terms 必须是对象")
        result = {}
        for name, config in raw.items():
            facet = KnowledgeFacet(str(name))
            query_terms = _bounded_terms((config or {}).get("query_terms", []), f"{name}.query_terms")
            evidence = _bounded_terms((config or {}).get("evidence_patterns", []), f"{name}.evidence_patterns")
            for pattern in evidence:
                re.compile(re.escape(pattern))
            result[facet] = FacetDefinition(query_terms, evidence)
        missing = set(KnowledgeFacet) - set(result)
        if missing:
            raise ValueError(f"taxonomy 缺少 facet: {sorted(item.value for item in missing)}")
        return result

    def _load_concepts(self, raw) -> tuple[CanonicalConcept, ...]:
        if not isinstance(raw, dict):
            raise ValueError("taxonomy concepts 必须是对象")
        valid_domains = {
            KnowledgeDomain.MENTAL_HEALTH.value,
            KnowledgeDomain.ACADEMIC.value,
            KnowledgeDomain.CAMPUS_SERVICE.value,
        }
        aliases_seen: dict[tuple[str, str], str] = {}
        canonicals_seen: set[tuple[str, str]] = set()
        result = []
        for concept_id, config in raw.items():
            canonical = str((config or {}).get("canonical") or "").strip()
            domain = str((config or {}).get("domain") or "").strip()
            if not canonical or domain not in valid_domains:
                raise ValueError(f"concept 配置无效: {concept_id}")
            canonical_key = (domain, canonical)
            if canonical_key in canonicals_seen:
                raise ValueError(f"同领域 canonical 重复: {canonical}")
            canonicals_seen.add(canonical_key)
            aliases = _bounded_terms((config or {}).get("aliases", []), f"{concept_id}.aliases")
            for alias in (canonical, *aliases):
                key = (domain, alias)
                if key in aliases_seen and aliases_seen[key] != concept_id:
                    raise ValueError(f"同领域 alias 冲突: {alias}")
                aliases_seen[key] = str(concept_id)
            result.append(
                CanonicalConcept(
                    concept_id=str(concept_id),
                    canonical=canonical,
                    aliases=aliases,
                    domain=domain,
                    preferred_tags=_bounded_terms(
                        (config or {}).get("preferred_tags", []), f"{concept_id}.preferred_tags"
                    ),
                    required_scope_fields=tuple(
                        field
                        for field in (config or {}).get("required_scope_fields", [])
                        if field in {"site", "academicPeriod", "policyName", "serviceItem", "referent"}
                    ),
                )
            )
        return tuple(result)

    def _match_site(self, question: str) -> str | None:
        for site, aliases in self.sites.items():
            if any(alias in question for alias in aliases):
                return site
        return None

    def _match_concepts(self, question: str) -> list[CanonicalConcept]:
        matches: list[tuple[int, CanonicalConcept]] = []
        for concept in self.concepts:
            length = max(
                (len(term) for term in (concept.canonical, *concept.aliases) if term in question),
                default=0,
            )
            if length:
                matches.append((length, concept))
        if not matches:
            return []
        longest = max(length for length, _ in matches)
        return [concept for length, concept in matches if length == longest]

    def _match_facets(self, question: str) -> list[KnowledgeFacet]:
        return [
            facet
            for facet, definition in self.facets.items()
            if any(term in question for term in definition.query_terms)
        ]

    def _domain_for_matches(self, matches: list[CanonicalConcept], hint: str | None) -> str:
        domains = {item.domain for item in matches}
        if len(domains) == 1:
            return next(iter(domains))
        return self._valid_domain(hint) or KnowledgeDomain.MIXED.value

    @staticmethod
    def _valid_domain(value: str | None) -> str | None:
        valid = {item.value for item in KnowledgeDomain}
        return value if value in valid else None

    def _open_concept(self, question: str) -> str | None:
        text = question
        for definition in self.facets.values():
            for term in definition.query_terms:
                text = text.replace(term, " ")
        for aliases in self.sites.values():
            for alias in aliases:
                text = text.replace(alias, " ")
        text = re.sub(r"[怎么如何是否能可以吗呢呀啊的了请帮我想了解一下相关学校校内本校，。！？、；：,.!?;:\s]", "", text)
        return text[:48] if len(text) >= 2 else None

    @staticmethod
    def _variants(
        original: str,
        concepts: list[CanonicalConcept],
        facets: list[KnowledgeFacet],
        open_text: str | None,
    ) -> list[QueryVariant]:
        result = [QueryVariant("ORIGINAL", original, 1.0)]
        facet_labels = {
            KnowledgeFacet.ELIGIBILITY: "申请条件",
            KnowledgeFacet.MATERIALS: "申请材料",
            KnowledgeFacet.STEPS: "办理流程",
            KnowledgeFacet.CHANNEL: "办理入口",
            KnowledgeFacet.DEADLINE: "申请截止时间",
            KnowledgeFacet.PROCESSING_TIME: "办理时长",
            KnowledgeFacet.CONTACT: "联系方式",
            KnowledgeFacet.COST: "费用",
            KnowledgeFacet.SCOPE: "适用范围",
            KnowledgeFacet.POLICY_BASIS: "政策依据",
        }
        facet_text = " ".join(facet_labels[item] for item in facets)
        if concepts:
            canonical = " ".join(item.canonical for item in concepts)
            result.append(QueryVariant("CANONICAL_FACET", f"{canonical} {facet_text}".strip(), 0.95))
            alias = next((item.aliases[0] for item in concepts if item.aliases), "")
            if alias:
                result.append(QueryVariant("ALIAS_FACET", f"{alias} {facet_text}".strip(), 0.85))
        elif open_text:
            result.append(QueryVariant("OPEN_CONCEPT", f"{open_text} {facet_text}".strip(), 0.8))
        return result


def _bounded_terms(raw, label: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise ValueError(f"{label} 必须是列表")
    values = tuple(str(item).strip() for item in raw if str(item).strip())
    if any(len(item) > 64 for item in values):
        raise ValueError(f"{label} 包含过长词项")
    if len(set(values)) != len(values):
        raise ValueError(f"{label} 包含重复词项")
    return values


def _open_tokens(text: str) -> list[str]:
    compact = re.sub(r"\s+", "", text)
    return [compact[index : index + 2] for index in range(max(0, len(compact) - 1))]


_INSTITUTIONAL_TERMS = (
    "依据",
    "出处",
    "原文",
    "官方",
    "资格",
    "申请",
    "办理",
    "材料",
    "流程",
    "期限",
    "截止",
    "入口",
    "电话",
    "地点",
    "费用",
    "收费",
    "预约",
    "校区",
    "规定",
    "政策",
    "文件",
)


@lru_cache(maxsize=1)
def get_knowledge_taxonomy() -> KnowledgeTaxonomy:
    path = Path(__file__).resolve().parents[1] / "knowledge" / "retrieval_taxonomy.yaml"
    return KnowledgeTaxonomy(path)


def build_query_spec(
    question: str,
    *,
    domain_hint: str | None = None,
    context_question: str | None = None,
) -> KnowledgeQuerySpec:
    return get_knowledge_taxonomy().build_query_spec(
        question,
        domain_hint=domain_hint,
        context_question=context_question,
    )
