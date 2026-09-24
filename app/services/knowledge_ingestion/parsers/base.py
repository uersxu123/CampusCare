from __future__ import annotations

from typing import Protocol

from app.services.knowledge_ingestion.models import ParsedDocument, ParserProfile, StoredArtifact


class DocumentParser(Protocol):
    name: str
    version: str

    def supports(self, mime_type: str, filename: str) -> bool: ...

    def parse(self, artifact: StoredArtifact, profile: ParserProfile) -> ParsedDocument: ...
