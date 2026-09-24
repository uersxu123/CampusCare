#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from app.core.config import Settings
from app.core.enums import IntentType
from app.services.routing_v5 import PrimaryRoutingScorer, decide_fast_route, clear_prototype_vector_cache, warmup_fast_routing

INTENTS=(IntentType.CHAT,IntentType.ACADEMIC,IntentType.CAMPUS,IntentType.MENTAL)

def load(p): return [json.loads(x) for x in Path(p).open(encoding='utf-8') if x.strip()]
def frange(a,b,step):
    x=a
    while x <= b+1e-9:
        yield round(x,6); x+=step

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--dataset',default='eval_fast_route/routing_threshold_calibration_160.jsonl')
    ap.add_argument('--output-dir',default='target/fast-route-tuning')
    ap.add_argument('--ollama-url',default='http://127.0.0.1:11434')
    ap.add_argument('--embedding-model',default='bge-m3:latest')
    ap.add_argument('--min-start',type=float,default=0.92); ap.add_argument('--min-end',type=float,default=0.98); ap.add_argument('--min-step',type=float,default=0.005)
    ap.add_argument('--margin-start',type=float,default=0.04); ap.add_argument('--margin-end',type=float,default=0.14); ap.add_argument('--margin-step',type=float,default=0.01)
    ap.add_argument('--competing',type=float,default=0.90); ap.add_argument('--min-precision',type=float,default=0.98)
    args=ap.parse_args()
    rows=load(args.dataset)
    base=Settings(_env_file=None,database_url='sqlite+pysqlite:///:memory:',knowledge_vector_enabled=True,knowledge_embedding_provider='ollama',knowledge_embedding_model=args.embedding_model,knowledge_embedding_base_url=args.ollama_url,embedding_timeout_seconds=30.0,route_fast_embedding_timeout_seconds=3.0,route_fast_warmup_enabled=True,route_fast_warmup_timeout_seconds=30.0)
    clear_prototype_vector_cache()
    if not warmup_fast_routing(base):
        raise SystemExit('routing embedding warmup failed')
    scorer=PrimaryRoutingScorer(base)
    snapshots=[]
    for i,row in enumerate(rows,1):
        snap=scorer.score(row['text'])
        if not snap.embedding_available: raise SystemExit('embedding unavailable')
        snapshots.append(snap)
        if i%20==0: print(f'scored {i}/{len(rows)}',flush=True)
    sweep=[]
    for ms in frange(args.min_start,args.min_end,args.min_step):
      for margin in frange(args.margin_start,args.margin_end,args.margin_step):
        st=base.model_copy(update={'route_fast_min_score':ms,'route_fast_competing_score':args.competing,'route_fast_margin':margin,'route_fast_enabled':True})
        accepted=correct=0; single_total=single_accepted=0
        reasons={}
        for row,snap in zip(rows,snapshots):
            gold=set(row.get('labels',[])); dec=decide_fast_route(row['text'],snap,st,{})
            reasons[dec.reason]=reasons.get(dec.reason,0)+1
            if len(gold)==1: single_total+=1
            if dec.accepted:
                accepted+=1
                if len(gold)==1: single_accepted+=1
                pred={dec.intent.value}
                correct += pred==gold
        precision=correct/accepted if accepted else 1.0
        sweep.append({'min_score':ms,'competing':args.competing,'margin':margin,'accepted':accepted,'coverage':accepted/len(rows),'precision':precision,'single_coverage':single_accepted/single_total if single_total else 0.0,'reasons':reasons})
    candidates=[x for x in sweep if x['precision']>=args.min_precision]
    recommended=max(candidates,key=lambda x:(x['coverage'],x['precision'],x['min_score'],x['margin'])) if candidates else max(sweep,key=lambda x:(x['precision'],x['coverage']))
    out=Path(args.output_dir); out.mkdir(parents=True,exist_ok=True)
    (out/'fast_gate_sweep.json').write_text(json.dumps(sweep,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'fast_gate_recommended.json').write_text(json.dumps(recommended,ensure_ascii=False,indent=2),encoding='utf-8')
    fixed=min(sweep,key=lambda x:abs(x['min_score']-0.95)+abs(x['margin']-0.08))
    report=['# Fast Route Gate Tuning','',f'- dataset: {len(rows)}',f'- embedding: `{args.embedding_model}`',f'- required fast precision: {args.min_precision:.3f}','','## Current fixed config (0.95 / 0.90 / 0.08)','',f"- precision: **{fixed['precision']:.4f}**",f"- coverage: **{fixed['coverage']:.4f}**",f"- accepted: **{fixed['accepted']}**",'', '## Recommended under precision constraint','', '```json',json.dumps(recommended,ensure_ascii=False,indent=2),'```','', '> Do not change production thresholds automatically. Review false-fast samples on the 208 hold-out before adopting a new value.']
    (out/'FAST_GATE_TUNING_REPORT.md').write_text('\n'.join(report)+'\n',encoding='utf-8')
    print(json.dumps(recommended,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
