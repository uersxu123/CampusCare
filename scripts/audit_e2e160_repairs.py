"""修复交付清单与原始引文离线回放，不改正式评测结果。"""
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
import hashlib
import json
import xml.etree.ElementTree as ET

from app.services.evidence_contract import locate_quote

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'target/verification/e2e160-repairs-20260918'
RUN=ROOT/'target/evaluation/e2e-context-workitems-160-v1/20260918T1211Z-e2e160-v1'

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def read(path): return json.loads(path.read_text(encoding='utf-8'))

def main():
    freeze=read(RUN/'freeze-manifest.json')
    changes=[]
    for name,before in freeze['runtimeFiles'].items():
        after=sha(ROOT/name)
        if before!=after: changes.append({'path':name,'before':before,'after':after})
    diagnostics=read(RUN/'failure-analysis-evidence.json')
    by_case={}
    quote_counts=Counter()
    for q in diagnostics['quotes']:
        if q['category']=='valid': continue
        matched=any(locate_quote(q['quote'],s) is not None for s in q['originals'])
        quote_counts['recovered' if matched else 'stillRejected']+=1
        by_case.setdefault(q['id'],[]).append(matched)
    recovered=sorted(k for k,v in by_case.items() if all(v))
    snapshots=read(ROOT/'target/verification/e2e160-package-20260918/corpus-snapshot.json')
    from app.core.database import SessionLocal
    from app.core.config import get_settings
    from app.evaluation.reporting.fingerprint import fingerprint_active_corpus
    from app.services.vector_store import ChromaKnowledgeStore
    from sqlalchemy import text
    with SessionLocal() as db:
        corpus=fingerprint_active_corpus(db,manifest_path=ROOT/'app/knowledge/knowledge_manifest.yaml').as_dict()
        counts={t:db.execute(text(f'SELECT COUNT(*) FROM `{t}`')).scalar_one() for t in ('user_accounts','chat_sessions','chat_messages','psychological_reports','agent_run_traces')}
    store=ChromaKnowledgeStore(get_settings(),corpus['active_collection'])
    index={int(m['db_id']):m for m in store.collection.get(include=['metadatas'])['metadatas']}
    suite=ET.parse(OUT/'release-final.xml').find('.//testsuite')
    package=ROOT/'app/evaluation/datasets/e2e-context-workitems-160-v1'
    result={'createdAt':datetime.now(UTC).isoformat(),'changedRuntimeFiles':changes,
        'protectedFilesUnchanged':{name:sha(ROOT/name)==freeze['runtimeFiles'][name] for name in ('app/core/config.py','app/services/assessment.py','app/services/routing_v5.py','app/agents/routing.py')},
        'datasetUnchanged':all(sha(package/name)==digest for name,digest in read(package/'package-freeze.json')['files'].items()),
        'businessCounts':counts,'businessCountsUnchanged':counts==read(RUN/'postflight.json')['businessCounts'],
        'corpusUnchanged':corpus['corpus_hash']==snapshots['fingerprint']['corpus_hash'],
        'chromaUnchanged':len(index)==855 and all(index.get(c['id'],{}).get('content_hash')==c['contentHash'] for c in snapshots['children']),
        'quoteReplay':{'scope':'原失败引用的离线机械回放；不是E2E重跑或质量提分','caseCount':len(by_case),'allQuotesNowMatchCases':recovered,'quoteCounts':dict(quote_counts)},
        'tests':suite.attrib,'modelProbes':{'passed':sum(c['passed'] for c in read(OUT/'model-probes.json')['cases']),'total':7,'finalPolicyGateAddedAfterProbe':True},
        'historicalReports':{name:sha(RUN/name) for name in ('summary.json','e2e-report.json','cases.jsonl','freeze-manifest.json')}}
    result['analysisSourceReportUnchanged']=sha(RUN/'e2e-report.json')==read(RUN/'diagnostic-summary.json')['sourceReportSha256']
    result['implementationFiles']={str(p.relative_to(ROOT)):sha(p) for p in [ROOT/'tests/test_e2e160_repairs.py',ROOT/'scripts/probe_e2e160_repairs.py',Path(__file__)]}
    (OUT/'delivery-manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    total=int(suite.get('tests')); failed=int(suite.get('failures')); skipped=int(suite.get('skipped')); errors=int(suite.get('errors'))
    lines=['# E2E 160 失败修复交付', '',
        '本轮修改生产链路与诊断代码，保留原有工作区内容，未重跑正式160条，未修改冻结Gold、历史评分、模型配置、Safety逻辑、路由门/阈值或知识库索引。', '',
        '## 已实现', '',
        '- 引用：允许中文标点及汉字相邻的排版换行，保留原文偏移；不合并数字之间、英文单词之间的空白，不改标点、单位或否定。空引文拒绝。',
        '- Specialist：每个工作项单次RAG；额度用完后隐藏检索工具，仍允许授权原文回读；强制重复调用仍被拒绝。通用AgentLoop保留可配置配额以兼容独立调用方。',
        '- 提示词：补全既定领域的就业手续、借阅、交通无人机等边界；鼓励短原文引用，不改变路由规则、结构门或阈值。',
        '- Response：完整嵌套状态schema前置；有工作项和工具运行时的正常路径统一结构化交付，无状态变化使用空更新。禁止政策任务转成NOT_REQUIRED、部分回答清空缺口、无回读依据升级FULL。',
        '- Response更新失败：记录具体校验异常及受控诊断，仅交付已有验证片段和缺口，保留错误标志，不复用被拒绝正文，不隐藏失败。',
        '- 时间：Specialist从共享整轮预算留出20秒给后续合成，不增加模型或整轮预算；若不足则及时返回未完成状态。',
        '- 政策识别：补充认定/审批等词；学业和校园Agent实际使用RAG后必须履行证据契约，避免关键词漏判。',
        '- 评测归因：路由比较主意图及意图列表；提示证据未采集时不推断为传递失败。原始历史评分不改写。', '',
        '## 验证', '',
        f'- 最终完整回归：{total-failed-skipped-errors}通过、{failed}失败、{skipped}跳过、{errors}异常。完整记录 release-final.xml。',
        '- 已知失败仍为模型不可用时Safety把“Python程序崩溃”判中风险；Safety保持不变。天气外部集成测试跳过。',
        '- 7条虚构模型探针只运行一次，4条通过，3条失败：借阅证路由错误，两条引文被模型改写。最终正文保留了未知/年度边界，但契约失败仍如实记录。',
        '- 探针后新增的“实际RAG触发政策契约”通过最终单元/回归验证；没有重跑失败模型探针，不宣称模型探针全通过。',
        f'- 历史97条引用错误离线机械回放：{len(recovered)}条样本的原不匹配引文现可全部定位；恢复{quote_counts["recovered"]}条引文，仍拒绝{quote_counts["stillRejected"]}条。此数据仅衡量定位器，不代表E2E提升。',
        f'- 业务记录数量未变：{result["businessCountsUnchanged"]}；语料指纹未变：{result["corpusUnchanged"]}；Chroma内容未变：{result["chromaUnchanged"]}；冻结题集未变：{result["datasetUnchanged"]}。', '',
        '## 剩余限制', '',
        '本地qwen3:8b仍可能错分领域、改写引文或遗漏范围。严格引用检查继续保留。未提高输出预算；长输出是否仍截断需新的授权评测验证。单次RAG减少重复调用，也可能减少多子问题证据覆盖，此时应明确部分回答而非制造完整答案。', '',
        'Judge误判、Gold独立人工复核及Safety降级另需处理，本次没有根据输出修改题集或评分提示。不得把历史58.13%改称本轮通过率。', '',
        '## 文件', '',
        '- delivery-manifest.json：改动及受保护文件哈希、语料/业务记录校验、引用回放、测试摘要。',
        '- release-final.xml：完整回归结果。',
        '- model-probes.json、model-probes.started.json：固定虚构输入、原始模型输出、失败证据及调用指标。',
        '- before/：本轮修改前的生产文件副本。',
    ]
    (OUT/'IMPLEMENTATION_REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in ('historicalReports','changedRuntimeFiles','implementationFiles')},ensure_ascii=False))

if __name__=='__main__': main()
