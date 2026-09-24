import tempfile
import unittest
from pathlib import Path

from app.services.knowledge_ingestion.models import ParserProfile, StoredArtifact
from app.services.knowledge_ingestion.parsers.markdown import MarkdownParser
from app.services.knowledge_ingestion.parsers.plaintext import PlainTextParser


def artifact(path: Path, filename: str) -> StoredArtifact:
    return StoredArtifact("test", filename, "text/markdown", path.stat().st_size, "hash", path)


class MarkdownParserTests(unittest.TestCase):
    def test_ast_preserves_heading_path_lists_table_and_code_block(self):
        source = """# 一级
导语。

## 二级
- 列表甲
- 列表乙

| 项目 | 金额 |
|---|---:|
| 一等 | 3000 |

```python
print('ok')
```
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guide.md"
            path.write_text(source, encoding="utf-8")
            parsed = MarkdownParser().parse(artifact(path, path.name), ParserProfile())

        headings = [item for item in parsed.elements if item.element_type == "HEADING"]
        self.assertEqual([item.heading_path for item in headings], [("一级",), ("一级", "二级")])
        lists = [item for item in parsed.elements if item.element_type == "LIST_ITEM"]
        self.assertEqual([item.content for item in lists], ["列表甲", "列表乙"])
        table = next(item for item in parsed.elements if item.element_type == "TABLE")
        self.assertEqual(table.metadata["headers"], ["项目", "金额"])
        self.assertEqual(table.metadata["rows"], [["一等", "3000"]])
        code = next(item for item in parsed.elements if item.metadata.get("kind") == "code_block")
        self.assertIn("print('ok')", code.content)
        self.assertEqual(code.heading_path, ("一级", "二级"))

    def test_plaintext_keeps_paragraph_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.txt"
            path.write_text("第一段。\n\n第二段。\n\n第三段。", encoding="utf-8")
            parsed = PlainTextParser().parse(artifact(path, path.name), ParserProfile())
        self.assertEqual([item.content for item in parsed.elements], ["第一段。", "第二段。", "第三段。"])


if __name__ == "__main__":
    unittest.main()
