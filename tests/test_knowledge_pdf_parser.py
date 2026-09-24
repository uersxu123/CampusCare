import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.knowledge_ingestion.models import ParserProfile, StoredArtifact
from app.services.knowledge_ingestion.parsers.pypdf_parser import PyPdfParser
from app.services.knowledge_ingestion.quality import enforce_quality


class _Page:
    def __init__(self, text):
        self.text = text

    def extract_text(self):
        return self.text


class _Reader:
    is_encrypted = False
    pages = [
        _Page("重复页眉\n第一页正文。\n重复页脚"),
        _Page("重复页眉\n第二页正文。\n重复页脚"),
        _Page("重复页眉\n第三页正文。\n重复页脚"),
    ]

    def __init__(self, _path):
        pass


class _EmptyReader(_Reader):
    pages = [_Page(""), _Page("")]


class PdfParserTests(unittest.TestCase):
    def _artifact(self, path):
        return StoredArtifact("test", "制度.pdf", "application/pdf", 3, "hash", path)

    def test_pdf_preserves_page_order_and_removes_statistical_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "制度.pdf"
            path.write_bytes(b"pdf")
            with patch("app.services.knowledge_ingestion.parsers.pypdf_parser.PdfReader", _Reader):
                parsed = PyPdfParser().parse(self._artifact(path), ParserProfile())
        self.assertEqual([item.page_number for item in parsed.elements], [1, 2, 3])
        self.assertEqual([item.content for item in parsed.elements], ["第一页正文。", "第二页正文。", "第三页正文。"])
        self.assertEqual(parsed.quality["empty_page_ratio"], 0.0)
        enforce_quality(parsed)

    def test_empty_pdf_fails_quality_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "扫描.pdf"
            path.write_bytes(b"pdf")
            with patch("app.services.knowledge_ingestion.parsers.pypdf_parser.PdfReader", _EmptyReader):
                parsed = PyPdfParser().parse(self._artifact(path), ParserProfile())
        with self.assertRaises(ValueError):
            enforce_quality(parsed)


if __name__ == "__main__":
    unittest.main()
