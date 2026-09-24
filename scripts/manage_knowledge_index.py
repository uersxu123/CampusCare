from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core.database import session_scope
from app.services.knowledge_index import KnowledgeIndexManager


def main() -> None:
    parser = argparse.ArgumentParser(description="管理 MindBridge 版本化知识向量索引")
    parser.add_argument("command", choices=("build", "verify", "shadow", "activate", "rollback", "status"))
    parser.add_argument("--target", choices=("ready", "active"), default="ready")
    parser.add_argument("--query", action="append", default=[], help="shadow 查询，可重复传入")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--skip-active-comparison", action="store_true", help="旧索引停用或不可访问时，仅验证新索引检索")
    args = parser.parse_args()

    db = session_scope()
    try:
        manager = KnowledgeIndexManager(db, get_settings())
        if args.command == "build":
            result = manager.build(batch_size=max(1, args.batch_size))
            payload = {
                "collection": result.collection,
                "signature": result.signature,
                "chunkCount": result.chunk_count,
                "embeddingDimension": result.embedding_dimension,
                "state": result.state,
            }
        elif args.command == "verify":
            payload = manager.verify(args.target)
        elif args.command == "shadow":
            payload = manager.shadow(args.query or None, compare_active=not args.skip_active_comparison)
        elif args.command == "activate":
            payload = manager.activate()
        elif args.command == "rollback":
            payload = manager.rollback()
        else:
            payload = manager.status()
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    finally:
        db.close()


if __name__ == "__main__":
    main()
