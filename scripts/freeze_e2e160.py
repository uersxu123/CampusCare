"""验证只读语料与评测测试后，拒绝覆盖地冻结新包。"""
from datetime import UTC, datetime
import json
import xml.etree.ElementTree as ET
from build_e2e160 import ROOT, OUT, REVIEW, sha

def main():
    from app.core.config import get_settings
    from app.services.vector_store import ChromaKnowledgeStore
    from app.evaluation.dataset import load_e2e_cases, DatasetError
    from app.evaluation.runner import _validate_dataset_contract
    result = ET.parse(REVIEW / 'tests.xml')
    suites = result.findall('.//testsuite')
    assert suites and all(int(s.get('failures',0)) == 0 and int(s.get('errors',0)) == 0 for s in suites)
    cases = load_e2e_cases(OUT/'business.jsonl') + load_e2e_cases(OUT/'safety.jsonl')
    assert len(cases) == 160
    _validate_dataset_contract(cases)
    try:
        load_e2e_cases(ROOT/'eval_fast_route/routing_threshold_calibration_160.jsonl')
    except DatasetError:
        pass
    else:
        raise AssertionError('应拒绝将路由校准集用作 E2E')
    snapshot = json.loads((REVIEW/'corpus-snapshot.json').read_text(encoding='utf-8'))
    store = ChromaKnowledgeStore(get_settings(), snapshot['fingerprint']['active_collection'])
    index = {int(m['db_id']):m for m in store.collection.get(include=['metadatas'])['metadatas']}
    assert len(index) == len(snapshot['children']) == 855
    for c in snapshot['children']:
        assert index[c['id']]['content_hash'] == c['contentHash']
    files = {p.name:sha(p) for p in OUT.iterdir() if p.is_file() and p.name != 'package-freeze.json'}
    inputs = [REVIEW/'reviewed-gold.tsv', REVIEW/'discovery.json', REVIEW/'corpus-snapshot.json', REVIEW/'tests.xml', ROOT/'scripts/build_e2e160.py', ROOT/'scripts/prepare_e2e160.py', ROOT/'scripts/run_frozen_e2e160.py', Path(__file__)]
    freeze = dict(frozenAt=datetime.now(UTC).isoformat(),files=files,inputs={str(p.relative_to(ROOT)):sha(p) for p in inputs}, tests=sum(int(s.get('tests',0)) for s in suites), chromaVerified=855, calibrationRejected=True, noModelRunDuringGoldReview=True)
    with (OUT/'package-freeze.json').open('x',encoding='utf-8') as f:
        json.dump(freeze,f,ensure_ascii=False,indent=2)
    print(json.dumps(freeze,ensure_ascii=False))

if __name__ == '__main__':
    from pathlib import Path
    main()
