from __future__ import annotations

from app.services.knowledge_ingestion.models import ParsedDocument


def enforce_quality(parsed: ParsedDocument) -> None:
    quality = parsed.quality
    if not parsed.elements or float(quality.get("visible_characters", 0.0)) <= 0:
        raise ValueError("parse_quality_empty_document")
    if float(quality.get("empty_page_ratio", 0.0)) > 0.5:
        raise ValueError("parse_quality_too_many_empty_pages")
    if float(quality.get("garbled_ratio", 0.0)) > 0.2:
        raise ValueError("parse_quality_garbled_text")
    if float(quality.get("reading_order_anomaly", 0.0)) > 0.5:
        raise ValueError("parse_quality_reading_order")
