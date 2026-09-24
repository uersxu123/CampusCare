import json
from app.core.config import get_settings
from app.core.database import session_scope
from app.services.knowledge_import import KnowledgeManifestImporter


def main() -> None:
    db = session_scope()
    try:
        result = KnowledgeManifestImporter(db, get_settings()).import_all(rebuild_index=False)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        db.close()


if __name__ == "__main__":
    main()
