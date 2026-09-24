from __future__ import annotations

from markdown_it import MarkdownIt

from app.services.knowledge_ingestion.models import DocumentElement, ParsedDocument, ParserProfile, StoredArtifact


class MarkdownParser:
    name = "markdown-it"
    version = "1.0"

    def __init__(self):
        self.markdown = MarkdownIt("commonmark").enable("table")

    def supports(self, mime_type: str, filename: str) -> bool:
        base = mime_type.split(";", 1)[0].strip().lower()
        return base in {"text/markdown", "text/x-markdown"} or filename.lower().endswith((".md", ".markdown"))

    def parse(self, artifact: StoredArtifact, profile: ParserProfile) -> ParsedDocument:
        text = artifact.path.read_text(encoding="utf-8")
        tokens = self.markdown.parse(text)
        elements: list[DocumentElement] = []
        warnings: list[str] = []
        heading_path: list[str] = []
        list_depth = 0
        index = 0
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token.type in {"bullet_list_open", "ordered_list_open"}:
                list_depth += 1
            elif token.type in {"bullet_list_close", "ordered_list_close"}:
                list_depth = max(0, list_depth - 1)
            elif token.type == "heading_open":
                level = int(token.tag[1:])
                title = tokens[i + 1].content.strip()
                heading_path = heading_path[: level - 1]
                heading_path.append(title)
                elements.append(DocumentElement(index, "HEADING", title, heading_path=tuple(heading_path)))
                index += 1
            elif token.type == "paragraph_open":
                inline = _next_token(tokens, i + 1, "inline", "paragraph_close")
                if inline is not None and inline.content.strip():
                    kind = "LIST_ITEM" if list_depth else "PARAGRAPH"
                    elements.append(DocumentElement(index, kind, inline.content.strip(), heading_path=tuple(heading_path)))
                    index += 1
            elif token.type in {"fence", "code_block"}:
                content = token.content.rstrip("\n")
                if content:
                    elements.append(
                        DocumentElement(
                            index,
                            "PARAGRAPH",
                            content,
                            heading_path=tuple(heading_path),
                            metadata={"kind": "code_block", "language": token.info.strip()},
                        )
                    )
                    index += 1
            elif token.type == "table_open":
                end, headers, rows = _read_table(tokens, i)
                lines = [" | ".join(headers), *(" | ".join(row) for row in rows)]
                elements.append(
                    DocumentElement(
                        index,
                        "TABLE",
                        "\n".join(line for line in lines if line),
                        heading_path=tuple(heading_path),
                        metadata={"headers": headers, "rows": rows},
                    )
                )
                index += 1
                i = end
            elif token.type in {"html_block"} and token.content.strip():
                elements.append(
                    DocumentElement(index, "PARAGRAPH", token.content.strip(), heading_path=tuple(heading_path), metadata={"source_type": token.type})
                )
                warnings.append(f"unknown_markdown_token:{token.type}")
                index += 1
            i += 1
        title = next((item.content for item in elements if item.element_type == "HEADING" and len(item.heading_path) == 1), artifact.original_filename.rsplit(".", 1)[0])
        visible = sum(len("".join(item.content.split())) for item in elements)
        if not elements:
            warnings.append("empty_document")
        return ParsedDocument(
            title=title,
            elements=tuple(elements),
            parser_name=self.name,
            parser_version=self.version,
            warnings=tuple(warnings),
            quality={"visible_characters": float(visible), "empty_page_ratio": 0.0 if elements else 1.0, "garbled_ratio": 0.0},
        )


def _next_token(tokens, start: int, expected: str, stop: str):
    for token in tokens[start:]:
        if token.type == expected:
            return token
        if token.type == stop:
            break
    return None


def _read_table(tokens, start: int) -> tuple[int, list[str], list[list[str]]]:
    headers: list[str] = []
    rows: list[list[str]] = []
    current: list[str] | None = None
    header_row = False
    i = start + 1
    while i < len(tokens):
        token = tokens[i]
        if token.type == "table_close":
            return i, headers, rows
        if token.type == "tr_open":
            current = []
            header_row = False
        elif token.type == "th_open":
            header_row = True
        elif token.type == "inline" and current is not None:
            current.append(token.content.strip())
        elif token.type == "tr_close" and current is not None:
            if header_row and not headers:
                headers = current
            else:
                rows.append(current)
            current = None
        i += 1
    return len(tokens) - 1, headers, rows
