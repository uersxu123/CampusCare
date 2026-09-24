from __future__ import annotations

from app.services.knowledge_ingestion.parsers.base import DocumentParser
from app.services.knowledge_ingestion.parsers.docling_parser import AutoPdfParser, DoclingParser
from app.services.knowledge_ingestion.parsers.markdown import MarkdownParser
from app.services.knowledge_ingestion.parsers.plaintext import PlainTextParser
from app.services.knowledge_ingestion.parsers.pypdf_parser import PyPdfParser


class ParserRegistry:
    def __init__(self, parsers: tuple[DocumentParser, ...]):
        self.parsers = parsers

    @classmethod
    def default(cls) -> "ParserRegistry":
        return cls((MarkdownParser(), PlainTextParser(), AutoPdfParser(PyPdfParser(), (DoclingParser(),))))

    def resolve(self, mime_type: str, filename: str) -> DocumentParser:
        for parser in self.parsers:
            if parser.supports(mime_type, filename):
                return parser
        raise ValueError(f"unsupported_document_type:{mime_type}:{filename.rsplit('.', 1)[-1].lower()}")
