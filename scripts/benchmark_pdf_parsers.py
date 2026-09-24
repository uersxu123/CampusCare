from __future__ import annotations

import argparse
import json
import sys
import time
import tracemalloc
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.knowledge_ingestion.models import ParserProfile, StoredArtifact
from app.services.knowledge_ingestion.parsers.docling_parser import DoclingParser
from app.services.knowledge_ingestion.parsers.pypdf_parser import PyPdfParser


MINIMUM_SAMPLE_DOCUMENTS = 20


def collect_pdf_samples(root: Path) -> list[Path]:
    return sorted(path.resolve() for path in root.rglob("*.pdf") if path.is_file())


def require_sample_set(samples: list[Path]) -> None:
    if len(set(samples)) < MINIMUM_SAMPLE_DOCUMENTS:
        raise ValueError(
            f"复杂 PDF/OCR 选型至少需要 {MINIMUM_SAMPLE_DOCUMENTS} 份真实 PDF，当前只有 {len(set(samples))} 份"
        )


def benchmark(samples: list[Path]) -> dict:
    require_sample_set(samples)
    parsers = [PyPdfParser(), DoclingParser()]
    rows = []
    for path in samples:
        data = path.read_bytes()
        artifact = StoredArtifact(
            storage_key="benchmark",
            original_filename=path.name,
            mime_type="application/pdf",
            byte_size=len(data),
            sha256="benchmark",
            path=path,
        )
        for parser in parsers:
            if hasattr(parser, "available") and not parser.available:
                rows.append({"file": path.name, "parser": parser.name, "status": "UNAVAILABLE"})
                continue
            tracemalloc.start()
            started = time.perf_counter()
            try:
                parsed = parser.parse(artifact, ParserProfile())
                _current, peak = tracemalloc.get_traced_memory()
                rows.append(
                    {
                        "file": path.name,
                        "parser": parsed.parser_name,
                        "status": "SUCCEEDED",
                        "seconds": round(time.perf_counter() - started, 4),
                        "peakBytes": peak,
                        "elements": len(parsed.elements),
                        "tables": sum(item.element_type == "TABLE" for item in parsed.elements),
                        "pagesWithContent": len({item.page_number for item in parsed.elements if item.page_number}),
                        "warnings": list(parsed.warnings),
                        "quality": dict(parsed.quality),
                    }
                )
            except Exception as exc:
                _current, peak = tracemalloc.get_traced_memory()
                rows.append(
                    {
                        "file": path.name,
                        "parser": parser.name,
                        "status": "FAILED",
                        "seconds": round(time.perf_counter() - started, 4),
                        "peakBytes": peak,
                        "errorCode": f"{type(exc).__name__}:{str(exc)[:200]}",
                    }
                )
            finally:
                tracemalloc.stop()
    return {"schemaVersion": 1, "sampleCount": len(samples), "selectionMade": False, "results": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description="复杂 PDF/OCR 解析器基准；不足 20 份样本时拒绝选型。")
    parser.add_argument("sample_dir", type=Path)
    parser.add_argument("--output", type=Path, default=Path("target/pdf-parser-benchmark.json"))
    args = parser.parse_args()
    payload = benchmark(collect_pdf_samples(args.sample_dir))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"PDF 解析器基准已写入：{args.output}")


if __name__ == "__main__":
    main()
