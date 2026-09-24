from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Hashable, Iterable

from app.services.knowledge_query import get_knowledge_taxonomy


@dataclass(frozen=True)
class RetrievalScore:
    bm25_rank: int | None = None
    vector_rank: int | None = None
    reciprocal_rank_score: float = 0.0
    rerank_relevance: float = 0.0
    entity_match: float = 0.0
    facet_match: float = 0.0
    scope_match: float = 0.0
    authority_tiebreak: float = 0.0


@dataclass(frozen=True)
class EvidenceProvenance:
    seed_chunk_id: int | None
    child_chunk_ids: tuple[int, ...]
    page_numbers: tuple[int, ...]
    content_hashes: tuple[str, ...]


class KnowledgeTokenizer:
    version = "taxonomy-cjk-v1"
    _stopwords = {
        "怎么",
        "需要",
        "一下",
        "了解",
        "可以",
        "相关",
        "请问",
        "学校",
        "校内",
    }

    def __init__(self):
        self.taxonomy = get_knowledge_taxonomy()
        concept_terms = []
        for concept in self.taxonomy.concepts:
            for term in (concept.canonical, *concept.aliases):
                concept_terms.append((term, f"concept:{concept.concept_id}"))
        self._concept_terms = tuple(sorted(concept_terms, key=lambda item: len(item[0]), reverse=True))

    def tokenize(self, text: str) -> list[str]:
        tokens: list[str] = []
        lowered = (text or "").lower()
        for term, token in self._concept_terms:
            if term.lower() in lowered:
                tokens.append(token)
        for facet, definition in self.taxonomy.facets.items():
            if any(term.lower() in lowered for term in definition.query_terms) or any(
                pattern.lower() in lowered for pattern in definition.evidence_patterns
            ):
                tokens.append(f"facet:{facet.value}")
        segments = re.split(r"[\s，。！？、；：,.!?;:()（）\[\]【】<>《》]+", lowered)
        for segment in segments:
            if not segment:
                continue
            tokens.extend(re.findall(r"[a-z0-9_\-]+", segment))
            chinese_parts = re.findall(r"[一-龿]+", segment)
            for part in chinese_parts:
                if part in self._stopwords:
                    continue
                for width in (2, 3):
                    tokens.extend(
                        part[index : index + width]
                        for index in range(max(0, len(part) - width + 1))
                        if part[index : index + width] not in self._stopwords
                    )
        return tokens


def bm25f_scores(query: str, chunks: list, tokenizer: KnowledgeTokenizer | None = None) -> dict[int, float]:
    tokenizer = tokenizer or KnowledgeTokenizer()
    query_counts = Counter(tokenizer.tokenize(query))
    if not query_counts or not chunks:
        return {}
    fields = {
        "tags": 3.4,
        "section": 3.0,
        "title": 2.4,
        "content": 1.0,
    }
    field_documents: dict[str, list[tuple[int, Counter[str], int]]] = {name: [] for name in fields}
    for chunk in chunks:
        if chunk.id is None:
            continue
        document = chunk.document
        values = {
            "tags": " ".join(_json_tags(document.tags_json)) if document else "",
            "section": chunk.section_title or "",
            "title": document.title if document else chunk.source,
            "content": chunk.content,
        }
        for name, value in values.items():
            counts = Counter(tokenizer.tokenize(value))
            field_documents[name].append((int(chunk.id), counts, sum(counts.values())))
    scores: dict[int, float] = Counter()
    for name, weight in fields.items():
        for chunk_id, score in _bm25_field(query_counts, field_documents[name]).items():
            scores[chunk_id] += score * weight
    return dict(scores)


def reciprocal_rank_fusion(
    rankings: Iterable[tuple[Iterable[Hashable], float]],
    *,
    k: int = 60,
) -> dict[Hashable, float]:
    scores: dict[Hashable, float] = Counter()
    for keys, weight in rankings:
        for rank, key in enumerate(keys, start=1):
            scores[key] += max(0.0, weight) / (k + rank)
    return dict(scores)


def _bm25_field(
    query_counts: Counter[str],
    documents: list[tuple[int, Counter[str], int]],
) -> dict[int, float]:
    if not documents:
        return {}
    document_frequency: Counter[str] = Counter()
    for _chunk_id, counts, _length in documents:
        document_frequency.update(counts.keys())
    average_length = sum(length for _chunk_id, _counts, length in documents) / len(documents) or 1.0
    scores = {}
    for chunk_id, counts, length in documents:
        score = 0.0
        for term, query_frequency in query_counts.items():
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            idf = math.log(
                1.0 + (len(documents) - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5)
            )
            denominator = frequency + 1.5 * (1.0 - 0.75 + 0.75 * length / average_length)
            score += idf * (1.0 + math.log(query_frequency)) * frequency * 2.5 / denominator
        if score > 0:
            scores[chunk_id] = score
    return scores


def _json_tags(raw: str) -> list[str]:
    import json

    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [str(item) for item in value] if isinstance(value, list) else []
