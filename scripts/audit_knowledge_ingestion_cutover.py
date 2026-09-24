from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.services.knowledge_ingestion.cutover import (
    build_cutover_report,
    find_unmanaged_markdown,
    unavailable_database_report,
    write_cutover_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="只读检查知识入库 V2 兼容切换条件")
    parser.add_argument("--output", default="target/knowledge-ingestion-cutover-report.json")
    parser.add_argument("--require-ready", action="store_true", help="门禁未通过时返回非零退出码")
    args = parser.parse_args()
    settings = get_settings()
    unmanaged = find_unmanaged_markdown(settings)
    db = SessionLocal()
    try:
        try:
            report = build_cutover_report(db, unmanaged_markdown=unmanaged)
        except Exception as exc:
            db.rollback()
            report = unavailable_database_report(unmanaged_markdown=unmanaged, error_type=type(exc).__name__)
    finally:
        db.close()
    output = Path(args.output)
    if not output.is_absolute():
        output = settings.project_root / output
    write_cutover_report(report, output)
    print(json.dumps({
        "output": str(output),
        "eligibleToDisableLegacyBootstrap": report["eligibleToDisableLegacyBootstrap"],
        "databaseAvailable": report["databaseAvailable"],
        "unmanagedMarkdown": report["checks"]["manifestRegistration"]["unmanagedCount"],
        "cleanupPerformed": False,
    }, ensure_ascii=False))
    return 2 if args.require_ready and not report["eligibleToDisableLegacyBootstrap"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
