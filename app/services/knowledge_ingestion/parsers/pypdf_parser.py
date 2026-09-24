from __future__ import annotations

import math
import re
from collections import Counter

from pypdf import PdfReader

from app.services.knowledge_ingestion.models import DocumentElement, ParsedDocument, ParserProfile, StoredArtifact


class PyPdfParser:
    name = "pypdf-fast"
    version = "1.0"

    def supports(self, mime_type: str, filename: str) -> bool:
        return mime_type.split(";", 1)[0].strip().lower() == "application/pdf" or filename.lower().endswith(".pdf")

    def parse(self, artifact: StoredArtifact, profile: ParserProfile) -> ParsedDocument:
        reader = PdfReader(str(artifact.path))
        if getattr(reader, "is_encrypted", False):
            raise ValueError("encrypted_pdf")
        if len(reader.pages) > profile.max_pages:
            raise ValueError(f"pdf_page_limit_exceeded:{profile.max_pages}")
        raw_pages = [(page.extract_text() or "").replace("\x00", "") for page in reader.pages]
        line_pages = [[line.strip() for line in text.splitlines() if line.strip()] for text in raw_pages]
        repeated_headers, repeated_footers = _repeated_edges(line_pages)
        elements: list[DocumentElement] = []
        warnings: list[str] = []
        visible_by_page = []
        garbled = 0
        total_visible = 0
        for page_number, lines in enumerate(line_pages, start=1):
            cleaned = list(lines)
            if cleaned and cleaned[0] in repeated_headers:
                cleaned.pop(0)
            if cleaned and cleaned[-1] in repeated_footers:
                cleaned.pop()
            content = "\n".join(cleaned).strip()
            visible = len(re.sub(r"\s+", "", content))
            visible_by_page.append(visible)
            total_visible += visible
            garbled += content.count("�") + content.count("\ufffd")
            if content:
                elements.append(
                    DocumentElement(
                        len(elements),
                        "PARAGRAPH",
                        content,
                        page_number=page_number,
                        metadata={"reading_order": len(elements)},
                    )
                )
        page_count = len(raw_pages)
        empty_pages = sum(value == 0 for value in visible_by_page)
        empty_ratio = empty_pages / page_count if page_count else 1.0
        garbled_ratio = garbled / max(1, total_visible)
        if empty_pages:
            warnings.append(f"empty_pages:{empty_pages}")
        if repeated_headers or repeated_footers:
            warnings.append("repeated_header_footer_removed")
        if not elements:
            warnings.append("scanned_or_empty_pdf")
        return ParsedDocument(
            title=artifact.original_filename.rsplit(".", 1)[0],
            elements=tuple(elements),
            parser_name=self.name,
            parser_version=self.version,
            warnings=tuple(warnings),
            quality={
                "page_count": float(page_count),
                "visible_characters": float(total_visible),
                "visible_characters_per_page": float(total_visible / page_count) if page_count else 0.0,
                "empty_page_ratio": empty_ratio,
                "garbled_ratio": garbled_ratio,
                "repeated_header_footer_ratio": float((len(repeated_headers) + len(repeated_footers)) / max(1, page_count * 2)),
                "reading_order_anomaly": 0.0,
            },
        )


def _repeated_edges(pages: list[list[str]]) -> tuple[set[str], set[str]]:
    threshold = max(2, math.ceil(len(pages) * 0.6))
    headers = Counter(lines[0] for lines in pages if lines)
    footers = Counter(lines[-1] for lines in pages if lines)
    return (
        {value for value, count in headers.items() if count >= threshold},
        {value for value, count in footers.items() if count >= threshold},
    )
