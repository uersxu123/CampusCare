from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class StoredArtifact:
    storage_key: str
    original_filename: str
    mime_type: str
    byte_size: int
    sha256: str
    path: Path
    created: bool = False


@dataclass(frozen=True)
class ParserProfile:
    name: str = "auto"
    page_mapping: Mapping[str, object] = field(default_factory=dict)
    max_pages: int = 500


@dataclass(frozen=True)
class BoundingBox:
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True)
class DocumentElement:
    element_index: int
    element_type: str
    content: str
    page_number: int | None = None
    bbox: BoundingBox | None = None
    heading_path: tuple[str, ...] = ()
    parent_index: int | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedDocument:
    title: str
    elements: tuple[DocumentElement, ...]
    parser_name: str
    parser_version: str
    warnings: tuple[str, ...] = ()
    quality: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class LegacyChunk:
    content: str
    section_title: str | None = None
    page_number: int | None = None


@dataclass(frozen=True)
class IngestionResult:
    source: str
    document_id: int
    chunk_count: int
    status: str
    ingestion_status: str
    content_changed: bool
    metadata_changed: bool
    warnings: tuple[str, ...] = ()
