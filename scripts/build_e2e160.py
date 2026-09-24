"""从固定抽样与逐题审阅记录构建 E2E 包，不调用被测模型。"""
from pathlib import Path
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / 'app/evaluation/datasets/e2e-context-workitems-160-v1'
REVIEW = ROOT / 'target/verification/e2e160-package-20260918'
SMOKE = ROOT / 'app/evaluation/datasets/e2e-smoke-context-workitems-v7'

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def write(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

def main():
    from app.evaluation.dataset import load_e2e_cases
    discovery = json.loads((REVIEW / 'discovery.json').read_text(encoding='utf-8'))
    source = ROOT / discovery['sourcePath']
    assert sha(source) == discovery['sourceSha256']
    rows = {r['id']: r for r in map(json.loads, source.read_text(encoding='utf-8').splitlines())}
    selected = [rows[key] for key in discovery['selectedIds']]
    annotations = {}
    for line in (REVIEW / 'reviewed-gold.tsv').read_text(encoding='utf-8').splitlines():
        suffix, intent, action, ids, reference = line.split('|', 4)
        key = 'real-cug-' + suffix
        assert key not in annotations
        annotations[key] = (intent.split(','), action, [int(x) for x in ids.split(',')], reference)
    assert set(annotations) == set(discovery['selectedIds']) and len(annotations) == 144
    snapshot = json.loads((REVIEW / 'corpus-snapshot.json').read_text(encoding='utf-8'))
    chunks = {r['id']: r for r in snapshot['children']}
    titles = {r['key']: r['title'] for r in snapshot['documents']}
    business, changes = [], []
    for row in selected:
        intents, action, ids, reference = annotations[row['id']]
        evidence = [chunks[i] for i in ids]
        assert all(hashlib.sha256(e['content'].encode('utf-8')).hexdigest() == e['contentHash'] for e in evidence)
        route = dict(primaryIntent=intents[0], intents=list(dict.fromkeys(intents)), riskLevel='LOW', workItemCount=len(intents), workItemIntents=intents, dependencyEdges=[], missingArgumentNamesByWorkItem=[[] for _ in intents])
        case = dict(id='e2e160-' + row['id'], sourceCaseId=row['id'], user_input=row['query'], turns=[row['query']], expected_action=action,
                    expectedTools={'required':['rag_search'], 'forbidden':['get_weather']}, reference=reference, reference_facts=[reference],
                    reference_context_ids=['knowledge:' + str(i) for i in ids], reference_contexts=[e['content'] for e in evidence],
                    reference_context_metadata=[dict(id='knowledge:'+str(e['id']), documentKey=e['documentKey'], title=titles[e['documentKey']], contentHash=e['contentHash']) for e in evidence],
                    expected_document_keys=list(dict.fromkeys(e['documentKey'] for e in evidence)), expected_route=route,
                    metric_applicability=dict(faithfulness=True, answerRelevancy=True, contextPrecision=True, contextRecall=True, idBasedContextPrecision=True, idBasedContextRecall=True, actionCorrectness=True, safetyContract=False),
                    tags=['e2e160-v1', row['policy_area'], action.lower()],
                    annotation={'reviewStatus':'agent-reviewed; not independently human-reviewed', 'requiresHumanReview':True, 'source':row, 'reason':'按请求目标区分学习与校园事务；同一政策判断不按文档数拆项。独立请求无信息依赖，政策未覆盖细节仅部分回答。'})
        business.append(case)
        changes.append(dict(id=case['id'], original=row, expectedAction=action, expectedRoute=route, evidenceIds=ids, reviewedReference=reference))
    smoke_bytes = (SMOKE / 'business.jsonl').read_bytes()
    smoke = [json.loads(x) for x in smoke_bytes.decode('utf-8').splitlines()]
    safety = [json.loads(x) for x in (SMOKE / 'safety.jsonl').read_text(encoding='utf-8').splitlines()]
    all_cases = business + smoke + safety
    assert len(all_cases) == 160 and len({r['id'] for r in all_cases}) == 160
    assert len({tuple(r['turns']) for r in all_cases}) == 160
    OUT.mkdir(parents=True, exist_ok=True)
    if (OUT / 'package-freeze.json').exists():
        raise RuntimeError('评测包已冻结，拒绝覆盖')
    (OUT / 'business.jsonl').write_bytes((''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in business)).encode('utf-8') + smoke_bytes)
    (OUT / 'safety.jsonl').write_bytes((SMOKE / 'safety.jsonl').read_bytes())
    assert [len(load_e2e_cases(OUT / f)) for f in ('business.jsonl','safety.jsonl')] == [157,3]
    for case in load_e2e_cases(OUT / 'business.jsonl') + load_e2e_cases(OUT / 'safety.jsonl'):
        assert case.sut_input() == {'turns': case.turns}
    audit = json.loads((SMOKE / 'corpus-audit.json').read_text(encoding='utf-8'))
    assert audit['referenceSource']['corpusHash'] == snapshot['fingerprint']['corpus_hash']
    audit.update(scope='e2e-context-workitems-160-v1', outputCaseCount=160, businessCaseCount=157, safetyCaseCount=3, adaptationVersion='e2e-context-workitems-160-v1', datasetSha256={f:sha(OUT/f) for f in ('business.jsonl','safety.jsonl')})
    from collections import Counter
    audit['actionDistribution'] = dict(Counter(r['expected_action'] for r in business + smoke))
    write(OUT / 'corpus-audit.json', audit)
    write(OUT / 'adaptation-changes.json', {'selection':discovery, 'changes':changes, 'smokePreserved':True})
    (OUT / 'README.md').write_text('# 160 条生产链路 E2E 评测包\n\n固定等距选择当前 200 条手册检索题中的 144 条，追加原 13 条业务及 3 条安全 Smoke（原行不变）。原始 160 条文件是路由阈值校准集，不能作为 E2E。旧 181 条 E2E 引用旧语料，不直接使用。\n\n逐题参考答案来自当前有效语料，修正全 CAMPUS、全 ANSWER 标签，补充引用及工作项契约；缺失政策细节不编造答案。事实要求与边界说明共同构成 reference，非逐字匹配。多目标依请求拆分，不按命中文档数量拆分。\n\n局限：Agent 审阅，未经独立人工复核；16 条 Smoke 已用于回归，200 条题库此前用于检索测试，不是独立留出集；固定抽样保留语义相近题目。157 条业务高度偏重手册政策，仅3条安全题，高风险召回率统计不稳定，不能外推生产分布。引用块是审阅支持集合，不保证穷尽所有相关块，ID 检索指标仅供诊断。\n\n正式运行：本机 qwen3:8b、think=false；Judge 不重试、不修复、不二次生成，保留 hybrid 校验，每题隔离 MySQL、Redis DB15，后台工作器关闭。不按运行结果修改 Gold 或重跑失败题。准确性/相关性需同时报告有效 Judge 数和总题数，不能与端到端通过率混淆。\n', encoding='utf-8')
    write(OUT / 'audit-results.json', {'caseCount':160,'business':157,'safety':3,'uniqueIds':True,'uniqueExactQuestions':True,'sourceQuestionPreserved':True,'evidenceContentHashesVerified':True,'goldExcludedFromSutInput':True,'contractValidation':'passed','selectionIndependentOfResults':True,'humanReviewed':False})
    print(json.dumps({'output':str(OUT),'cases':160,'actions':audit['actionDistribution']},ensure_ascii=False))

if __name__ == '__main__':
    main()
