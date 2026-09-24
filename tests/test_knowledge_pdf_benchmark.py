import tempfile
import unittest
from pathlib import Path

from app.services.knowledge_ingestion.models import DocumentElement, ParsedDocument, ParserProfile, StoredArtifact
from app.services.knowledge_ingestion.parsers.docling_parser import AutoPdfParser
from scripts.benchmark_pdf_parsers import collect_pdf_samples, require_sample_set


class _FastParser:
    name = "pypdf-fast"
    version = "test"

    def parse(self, artifact, profile):
        return ParsedDocument(
            "PDF",
            (DocumentElement(0, "PARAGRAPH", "数字 PDF 正文", page_number=1),),
            self.name,
            self.version,
            quality={"visible_characters": 8.0, "empty_page_ratio": 0.0, "garbled_ratio": 0.0},
        )


class _EmptyFastParser(_FastParser):
    def parse(self, artifact, profile):
        return ParsedDocument(
            "PDF",
            (),
            self.name,
            self.version,
            warnings=("scanned_or_empty_pdf",),
            quality={"visible_characters": 0.0, "empty_page_ratio": 1.0, "garbled_ratio": 0.0},
        )


class _EnhancedParser:
    name = "fake-enhanced"
    version = "1"
    available = True

    def parse(self, artifact, profile):
        return ParsedDocument(
            "PDF",
            (DocumentElement(0, "PARAGRAPH", "OCR 正文", page_number=1),),
            self.name,
            self.version,
            quality={"visible_characters": 5.0, "empty_page_ratio": 0.0, "garbled_ratio": 0.0},
        )


class PdfBenchmarkGateTests(unittest.TestCase):
    def test_benchmark_requires_twenty_distinct_pdf_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(3):
                (root / f"{index}.pdf").write_bytes(b"pdf")
            samples = collect_pdf_samples(root)
            self.assertEqual(len(samples), 3)
            with self.assertRaises(ValueError):
                require_sample_set(samples)

    def test_auto_parser_keeps_fast_result_when_quality_passes(self):
        parser = AutoPdfParser(_FastParser(), (_EnhancedParser(),))
        parsed = parser.parse(_artifact(), ParserProfile())
        self.assertEqual(parsed.parser_name, "pypdf-fast")

    def test_auto_parser_uses_available_enhanced_parser_or_fails_explicitly(self):
        parsed = AutoPdfParser(_EmptyFastParser(), (_EnhancedParser(),)).parse(_artifact(), ParserProfile())
        self.assertEqual(parsed.parser_name, "fake-enhanced")
        self.assertIn("fallback_from:pypdf-fast", parsed.warnings)
        with self.assertRaisesRegex(ValueError, "enhanced_pdf_parser_unavailable"):
            AutoPdfParser(_EmptyFastParser(), ()).parse(_artifact(), ParserProfile())


def _artifact():
    return StoredArtifact("test", "sample.pdf", "application/pdf", 3, "hash", Path("sample.pdf"))


if __name__ == "__main__":
    unittest.main()
