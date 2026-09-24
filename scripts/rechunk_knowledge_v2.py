from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.core.config import get_settings
from app.core.database import session_scope
from app.models.entities import KnowledgeDocument
from app.services.knowledge_ingestion.pipeline import KnowledgeIngestionPipeline


def main() -> None:
    parser = argparse.ArgumentParser(description="显式生成 structure_token_v2 Chunk；不会构建或激活索引。")
    parser.add_argument("--document-id", type=int, action="append", dest="document_ids")
    parser.add_argument("--all-active", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("target/knowledge-v2-rechunk.json"))
    args = parser.parse_args()
    if not args.document_ids and not args.all_active:
        raise SystemExit("必须提供 --document-id 或 --all-active")
    db = session_scope()
    try:
        ids = list(args.document_ids or [])
        if args.all_active:
            ids.extend(
                item.id
                for item in db.query(KnowledgeDocument)
                .filter(KnowledgeDocument.status == "ACTIVE")
                .order_by(KnowledgeDocument.id.asc())
                .all()
            )
        pipeline = KnowledgeIngestionPipeline(db, get_settings())
        results = [pipeline.rechunk_document(item) for item in dict.fromkeys(ids)]
        payload = {
            "schemaVersion": 1,
            "indexActivated": False,
            "documents": [
                {"documentId": item.document_id, "source": item.source, "children": item.chunk_count}
                for item in results
            ],
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"V2 重分割报告已写入：{args.output}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
