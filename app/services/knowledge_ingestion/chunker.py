from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Protocol

from app.services.knowledge_ingestion.models import DocumentElement, ParsedDocument


class TokenCounter(Protocol):
    name: str
    version: str

    def count(self, text: str) -> int: ...


class UnicodeLexicalTokenCounter:
    name = "unicode-lexical"
    version = "1.0"
    _pattern = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9_]+|[^\s]")

    def count(self, text: str) -> int:
        return len(self._pattern.findall(text or ""))


@dataclass(frozen=True)
class ChunkProfile:
    name: str = "structure_token_v2"
    child_target_tokens: int = 320
    child_max_tokens: int = 480
    child_min_tokens: int = 80
    parent_max_tokens: int = 1200
    overlap_tokens: int = 48

    @classmethod
    def from_settings(cls, settings) -> "ChunkProfile":
        return cls(
            name=getattr(settings, "knowledge_chunking_profile", "structure_token_v2"),
            child_target_tokens=getattr(settings, "knowledge_child_target_tokens", 320),
            child_max_tokens=getattr(settings, "knowledge_child_max_tokens", 480),
            child_min_tokens=getattr(settings, "knowledge_child_min_tokens", 80),
            parent_max_tokens=getattr(settings, "knowledge_parent_max_tokens", 1200),
            overlap_tokens=getattr(settings, "knowledge_overlap_tokens", 48),
        )


@dataclass(frozen=True)
class StructuredChunk:
    content: str
    element_ids: tuple[int, ...]
    page_numbers: tuple[int, ...]
    heading_paths: tuple[tuple[str, ...], ...]
    token_count: int
    parent_index: int | None = None
    fallback: bool = False


@dataclass(frozen=True)
class ChunkPlan:
    parents: tuple[StructuredChunk, ...]
    children: tuple[StructuredChunk, ...]
    tokenizer_name: str
    tokenizer_version: str
    profile_name: str


@dataclass(frozen=True)
class _Unit:
    content: str
    element_id: int
    page_number: int | None
    heading_path: tuple[str, ...]
    token_count: int
    fallback: bool = False


def chunk_document(parsed: ParsedDocument, profile: ChunkProfile, token_counter: TokenCounter) -> ChunkPlan:
    if profile.child_target_tokens <= 0 or profile.child_max_tokens < profile.child_target_tokens:
        raise ValueError("invalid_chunk_profile")
    groups: list[list[_Unit]] = []
    current_group: list[_Unit] = []
    current_path: tuple[str, ...] | None = None
    for element in parsed.elements:
        if element.element_type in {"TITLE", "HEADING", "TABLE", "PAGE_HEADER", "PAGE_FOOTER"}:
            continue
        path = element.heading_path
        if current_group and path != current_path:
            groups.append(current_group)
            current_group = []
        current_path = path
        current_group.extend(_element_units(element, profile, token_counter))
    if current_group:
        groups.append(current_group)

    children: list[StructuredChunk] = []
    for group in groups:
        children.extend(_chunk_group(group, profile, token_counter))
    parents, linked = _build_parents(children, profile, token_counter)
    return ChunkPlan(
        parents=tuple(parents),
        children=tuple(linked),
        tokenizer_name=token_counter.name,
        tokenizer_version=token_counter.version,
        profile_name=profile.name,
    )


def _element_units(element: DocumentElement, profile: ChunkProfile, counter: TokenCounter) -> list[_Unit]:
    content = element.content.strip()
    if not content:
        return []
    count = counter.count(content)
    if element.element_type in {"LIST_ITEM", "TABLE"}:
        return [_Unit(content, element.element_index, element.page_number, element.heading_path, count, count > profile.child_max_tokens)]
    if count <= profile.child_target_tokens:
        return [_Unit(content, element.element_index, element.page_number, element.heading_path, count)]
    sentences = [item.strip() for item in re.findall(r".*?(?:[。！？；.!?;]+|$)", content) if item.strip()]
    values: list[_Unit] = []
    for sentence in sentences:
        sentence_count = counter.count(sentence)
        if sentence_count <= profile.child_max_tokens:
            values.append(_Unit(sentence, element.element_index, element.page_number, element.heading_path, sentence_count))
            continue
        for value in _bounded_fallback(sentence, profile.child_max_tokens, counter):
            values.append(_Unit(value, element.element_index, element.page_number, element.heading_path, counter.count(value), True))
    return values


def _bounded_fallback(text: str, maximum: int, counter: TokenCounter) -> list[str]:
    result: list[str] = []
    remaining = text
    while remaining:
        low, high, best = 1, len(remaining), 1
        while low <= high:
            middle = (low + high) // 2
            if counter.count(remaining[:middle]) <= maximum:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        result.append(remaining[:best])
        remaining = remaining[best:]
    return result


def _chunk_group(units: list[_Unit], profile: ChunkProfile, counter: TokenCounter) -> list[StructuredChunk]:
    chunks: list[StructuredChunk] = []
    current: list[_Unit] = []
    for unit in units:
        candidate = _units_text([*current, unit])
        candidate_tokens = counter.count(candidate)
        should_flush = bool(current) and (
            candidate_tokens > profile.child_max_tokens
            or (candidate_tokens > profile.child_target_tokens and counter.count(_units_text(current)) >= profile.child_min_tokens)
        )
        if should_flush:
            chunks.append(_build_chunk(current, counter))
            current = _whole_unit_overlap(current, profile.overlap_tokens, counter)
        if current and counter.count(_units_text([*current, unit])) > profile.child_max_tokens:
            current = []
        current.append(unit)
    if current:
        chunks.append(_build_chunk(current, counter))
    return chunks


def _whole_unit_overlap(units: list[_Unit], maximum: int, counter: TokenCounter) -> list[_Unit]:
    retained: list[_Unit] = []
    for unit in reversed(units):
        candidate = [unit, *retained]
        if counter.count(_units_text(candidate)) > maximum:
            break
        retained = candidate
    return retained


def _build_chunk(units: list[_Unit], counter: TokenCounter) -> StructuredChunk:
    content = _units_text(units)
    return StructuredChunk(
        content=content,
        element_ids=tuple(dict.fromkeys(item.element_id for item in units)),
        page_numbers=tuple(dict.fromkeys(item.page_number for item in units if item.page_number is not None)),
        heading_paths=tuple(dict.fromkeys(item.heading_path for item in units)),
        token_count=counter.count(content),
        fallback=any(item.fallback for item in units),
    )


def _build_parents(children: list[StructuredChunk], profile: ChunkProfile, counter: TokenCounter):
    parents: list[StructuredChunk] = []
    linked: list[StructuredChunk] = []
    group: list[tuple[int, StructuredChunk]] = []
    for child_index, child in enumerate(children):
        if group:
            same_top = _top_heading(group[0][1]) == _top_heading(child)
            candidate = _parent_content([item for _, item in group] + [child])
            if not same_top or counter.count(candidate) > profile.parent_max_tokens:
                _finish_parent(group, parents, linked, counter)
                group = []
        group.append((child_index, child))
    if group:
        _finish_parent(group, parents, linked, counter)
    return parents, linked


def _finish_parent(group, parents, linked, counter):
    children = [item for _, item in group]
    content = _parent_content(children)
    parent_index = len(parents)
    parents.append(
        StructuredChunk(
            content=content,
            element_ids=tuple(dict.fromkeys(value for item in children for value in item.element_ids)),
            page_numbers=tuple(dict.fromkeys(value for item in children for value in item.page_numbers)),
            heading_paths=tuple(dict.fromkeys(value for item in children for value in item.heading_paths)),
            token_count=counter.count(content),
            fallback=any(item.fallback for item in children),
        )
    )
    linked.extend(replace(item, parent_index=parent_index) for item in children)


def _parent_content(children: list[StructuredChunk]) -> str:
    lines: list[str] = []
    for child in children:
        for line in child.content.splitlines():
            if line and line not in lines:
                lines.append(line)
    return "\n".join(lines)


def _top_heading(chunk: StructuredChunk) -> str:
    return next((path[0] for path in chunk.heading_paths if path), "")


def _units_text(units: list[_Unit]) -> str:
    return "\n".join(item.content for item in units if item.content)
