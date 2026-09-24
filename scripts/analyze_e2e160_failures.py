"""离线分析冻结评测原始记录，不调用模型或改变 Gold。"""
from pathlib import Path
from collections import Counter
import json
import re
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.services.evidence_contract import locate_quote
RUN=ROOT/'target/evaluation/e2e-context-workitems-160-v1/20260918T1211Z-e2e160-v1'
DATA=ROOT/'app/evaluation/datasets/e2e-context-workitems-160-v1'
def rows(p): return [json.loads(x) for x in p.read_text(encoding='utf-8').splitlines()]
def main():
    cases={c['id']:c for c in rows(DATA/'business.jsonl')+rows(DATA/'safety.jsonl')}
    actual={c['case_id']:c for c in rows(RUN/'cases.jsonl')}
    report=json.loads((RUN/'e2e-report.json').read_text(encoding='utf-8'))
    results={c['caseId']:c for c in report['results']}
    quotes=[]; routes=[]; terminals=[]; judges=[]; bad=[]; rag=[]
    for key,c in cases.items():
        a=actual[key]; r=results[key]; e=c['expected_route']; ar=a['route']
        if ar.get('primaryIntent')!=e['primaryIntent'] or ar.get('intents')!=e['intents']:
            routes.append(dict(id=key,question=c['turns'],expected=e,actual=ar,tools=a['actual_tools'],judgeVerdict=(r.get('output') or {}).get('verdict')))
        if a.get('error_code'):
            terminals.append(dict(id=key,error=a['error_code'],question=c['turns'],metrics=a['turn_metrics'],specialists=a['specialist_results'],workItems=a['work_item_outcomes'],response=a['response']))
        if r.get('contract',{}).get('judgeIssues'):
            judges.append(dict(id=key,question=c['turns'],expected=c['expected_action'],reference=c['reference'],response=a['response'],runtime=a['action'],judge=r['output'],issues=r['contract']['judgeIssues']))
        if not r['passed']:
            bad.append(dict(id=key,question=c['turns'],expected=c['expected_action'],reference=c['reference'],response=a['response'],judge=r.get('output'),contract=r.get('contract'),attribution=r['attribution']))
        for d in a.get('evidence_diagnostics',[]):
            if d.get('errorCode')!='EVIDENCE_QUOTE_INVALID': continue
            visible={}
            for v in d.get('visibleEvidence',[]):
                visible.setdefault(v.get('evidenceId') or v.get('contextId'),[]).extend(str(v.get(k) or '') for k in ('content','text','snippet','parentContent'))
            for n in (d.get('rawContract') or {}).get('evidenceNotes',[]):
                q=n.get('quote',''); texts=visible.get(n.get('evidenceId'),[])
                category='valid' if any(locate_quote(q,t) is not None for t in texts) else 'whitespace_only' if q and any(re.sub(r'\s+','',q) in re.sub(r'\s+','',t) for t in texts) else 'unknown_id' if not texts else 'text_diff'
                quotes.append(dict(id=key,workItem=d.get('workItemId'),category=category,quote=q,evidenceId=n.get('evidenceId'),originals=texts if category!='valid' else []))
        def calls(v):
            if isinstance(v,dict):
                for k,w in v.items():
                    if k=='calls' and isinstance(w,list): yield from w
                    else: yield from calls(w)
            elif isinstance(v,list):
                for w in v: yield from calls(w)
        rs=[v for v in calls(a.get('tool_diagnostics',{})) if str(v.get('toolName','')).endswith('rag_search')]
        rag.append(dict(id=key,calls=rs,workItems=len(ar.get('workItems',[]))))
    counts=Counter(q['category'] for q in quotes)
    qcases={q['id'] for q in quotes if q['category']!='valid'}
    onlyspace=[key for key in qcases if {q['category'] for q in quotes if q['id']==key and q['category']!='valid'}=={'whitespace_only'}]
    out=dict(quoteCounts=dict(counts),quoteCaseCount=len(qcases),whitespaceOnlyCases=onlyspace,routeConfusion=dict(Counter(x['expected']['primaryIntent']+' -> '+x['actual'].get('primaryIntent','') for x in routes)),routes=routes,terminalErrors=terminals,judgeConflicts=judges,failed=bad,quotes=quotes,rag=rag)
    (RUN/'failure-analysis-evidence.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in out.items() if k in ('quoteCounts','quoteCaseCount','whitespaceOnlyCases','routeConfusion')},ensure_ascii=False))
    print('routes',json.dumps([{k:v for k,v in x.items() if k in ('id','question','judgeVerdict')}|{'expected':x['expected']['primaryIntent'],'actual':x['actual'].get('primaryIntent'),'planSource':x['actual'].get('planSource')} for x in routes],ensure_ascii=False))
    print('terminal summaries',json.dumps([{'id':x['id'],'error':x['error'],'metrics':x['metrics']} for x in terminals],ensure_ascii=False))
if __name__=='__main__': main()
