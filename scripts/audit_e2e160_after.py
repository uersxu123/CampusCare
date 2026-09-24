"""正式运行结束后的只读隔离/冻结复核。"""
from pathlib import Path
import argparse
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def main():
    from app.core.config import get_settings
    from app.core.database import SessionLocal
    from app.evaluation.reporting.fingerprint import fingerprint_active_corpus
    from app.evaluation.runtime.isolation import _redis_database_url
    from app.services.vector_store import ChromaKnowledgeStore
    from sqlalchemy import text
    import redis
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    run = ROOT/'target/evaluation/e2e-context-workitems-160-v1'/args.run_id
    assert (run/'postflight.json').exists(), '运行未结束'
    dataset = ROOT/'app/evaluation/datasets/e2e-context-workitems-160-v1'
    package = json.loads((dataset/'package-freeze.json').read_text(encoding='utf-8'))
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    app = get_settings()
    with SessionLocal() as db:
        fingerprint = fingerprint_active_corpus(db,manifest_path=ROOT/'app/knowledge/knowledge_manifest.yaml').as_dict()
        temporary = list(db.execute(text("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME LIKE 'mindbridge_eval_%'")).scalars())
    client = redis.Redis.from_url(_redis_database_url(app.redis_url,15))
    size = client.dbsize()
    client.close()
    snapshot = json.loads((ROOT/'target/verification/e2e160-package-20260918/corpus-snapshot.json').read_text(encoding='utf-8'))
    store = ChromaKnowledgeStore(app,fingerprint['active_collection'])
    index = {int(m['db_id']):m for m in store.collection.get(include=['metadatas'])['metadatas']}
    life = [json.loads(s) for s in (run/'lifecycle.jsonl').read_text(encoding='utf-8').splitlines()]
    ends = [e for e in life if e.get('finished') == 1 or e.get('aborted') == 1]
    audit = {'redisDb15Size':size,'visibleEvaluationSchemas':temporary,'cleanupConfirmed':sum(e.get('resourceCleanup')=='CONFIRMED' for e in ends),'terminalLifecycleCount':len(ends),
             'corpusUnchanged':all(fingerprint[k]==snapshot['fingerprint'][k] for k in ('corpus_hash','manifest_hash','active_collection','index_signature','document_count','chunk_count')),
             'chromaUnchanged':len(index)==855 and all(index.get(c['id'],{}).get('content_hash')==c['contentHash'] for c in snapshot['children']),
             'packageUnchanged':all(sha(dataset/p)==h for p,h in package['files'].items()),
             'buildInputsUnchanged':all(sha(ROOT/p)==h for p,h in package['inputs'].items()),
             'schemaVisibilityLimitation':'应用账号仅能列出具有权限的 schema，逐题清理以 supervisor 的 CONFIRMED 为主证据；不删除未知数据库或 Redis 数据。'}
    (run/'isolation-audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(audit,ensure_ascii=False))

if __name__ == '__main__':
    main()
