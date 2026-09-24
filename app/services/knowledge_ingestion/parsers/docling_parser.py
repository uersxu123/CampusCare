from __future__ import annotations

import importlib.util
import re
from dataclasses import replace

from app.services.knowledge_ingestion.models import DocumentElement, ParsedDocument, ParserProfile, StoredArtifact
from app.services.knowledge_ingestion.quality import enforce_quality


class DoclingParser:
    name = "docling"
    version = "optional"

    @property
    def available(self) -> bool:
        return importlib.util.find_spec("docling") is not None

    def supports(self, mime_type: str, filename: str) -> bool:
        return mime_type.split(";", 1)[0].strip().lower() == "application/pdf" or filename.lower().endswith(".pdf")

    def parse(self, artifact: StoredArtifact, profile: ParserProfile) -> ParsedDocument:
        if not self.available:
            raise ValueError("docling_dependency_unavailable")
        from docling.document_converter import DocumentConverter

        result = DocumentConverter().convert(str(artifact.path))
        markdown = result.document.export_to_markdown()
        paragraphs = [item.strip() for item in re.split(r"\n\s*\n", markdown) if item.strip()]
        elements = tuple(
            DocumentElement(index, "PARAGRAPH", content, metadata={"source_format": "docling_markdown"})
            for index, content in enumerate(paragraphs)
        )
        return ParsedDocument(
            title=artifact.original_filename.rsplit(".", 1)[0],
            elements=elements,
            parser_name=self.name,
            parser_version=self.version,
            warnings=("docling_optional_adapter",),
            quality={
                "visible_characters": float(sum(len("".join(item.content.split())) for item in elements)),
                "empty_page_ratio": 0.0 if elements else 1.0,
                "garbled_ratio": 0.0,
            },
        )


class AutoPdfParser:
    name = "pdf-auto"
    version = "1.0"

    def __init__(self, fast_parser, enhanced_parsers=()):
        self.fast_parser = fast_parser
        self.enhanced_parsers = tuple(enhanced_parsers)

    def supports(self, mime_type: str, filename: str) -> bool:
        return mime_type.split(";", 1)[0].strip().lower() == "application/pdf" or filename.lower().endswith(".pdf")

    def parse(self, artifact: StoredArtifact, profile: ParserProfile) -> ParsedDocument:
        fast = self.fast_parser.parse(artifact, profile)
        try:
            enforce_quality(fast)
            if not _needs_enhanced_layout(fast):
                return fast
        except ValueError:
            pass
        errors: list[str] = []
        for parser in self.enhanced_parsers:
            if not getattr(parser, "available", False):
                errors.append(f"{parser.name}:unavailable")
                continue
            try:
                parsed = parser.parse(artifact, profile)
                enforce_quality(parsed)
                return replace(parsed, warnings=(*parsed.warnings, f"fallback_from:{fast.parser_name}"))
            except Exception as exc:
                errors.append(f"{parser.name}:{type(exc).__name__}")
        detail = ",".join(errors) or "none_configured"
        raise ValueError(f"enhanced_pdf_parser_unavailable:{detail}")


def _needs_enhanced_layout(parsed: ParsedDocument) -> bool:
    quality = parsed.quality
    return (
        float(quality.get("reading_order_anomaly", 0.0)) > 0.5
        or float(quality.get("table_density", 0.0)) > 0.3
    )
