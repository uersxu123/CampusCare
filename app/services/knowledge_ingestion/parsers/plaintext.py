from __future__ import annotations

import re

from app.services.knowledge_ingestion.models import DocumentElement, ParsedDocument, ParserProfile, StoredArtifact


class PlainTextParser:
    name = "plaintext"
    version = "1.0"

    def supports(self, mime_type: str, filename: str) -> bool:
        return mime_type.split(";", 1)[0].strip().lower() == "text/plain" or filename.lower().endswith(".txt")

    def parse(self, artifact: StoredArtifact, profile: ParserProfile) -> ParsedDocument:
        text = artifact.path.read_text(encoding="utf-8")
        paragraphs = [
            re.sub(r"[ \t]+", " ", item).strip()
            for item in re.split(r"\r?\n\s*\r?\n", text)
            if item.strip()
        ]
        elements = tuple(
            DocumentElement(index, "PARAGRAPH", content)
            for index, content in enumerate(paragraphs)
        )
        visible = sum(len(re.sub(r"\s+", "", item)) for item in paragraphs)
        return ParsedDocument(
            title=artifact.original_filename.rsplit(".", 1)[0],
            elements=elements,
            parser_name=self.name,
            parser_version=self.version,
            warnings=() if elements else ("empty_document",),
            quality={"visible_characters": float(visible), "empty_page_ratio": 0.0 if elements else 1.0, "garbled_ratio": 0.0},
        )
