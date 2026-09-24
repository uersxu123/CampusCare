"""独立虚构探针，不读取正式题集、不写业务库、不调用 Judge。"""
from datetime import UTC, datetime
from pathlib import Path
import json

from app.core.config import get_settings
from app.services.agent_models import AgentModelRegistry
from app.services.ai import StructuredCompletionOptions
from app.services.evidence_contract import capture_evidence_diagnostics
from app.services.intent_prompts import build_intent_prompt, UNDERSTANDING_SCHEMA_NAME
from app.services.routing_v5 import PlanningResultV6, validate_planning_result_v6
from app.services.turn_metrics import TurnMetricsCollector, bind_turn_metrics
from scripts.probe_context_workitems_followup import run_case

OUT=Path('target/verification/e2e160-repairs-20260918/model-probes.json')
PLANNER=[
    ('aid', '晨星学院远航奖学金的资助标准、绩点要求和排除条件有哪些？', ['CAMPUS']),
    ('flight', '晨星学院校内航拍设备登记由哪里受理？', ['CAMPUS']),
    ('library', '借阅证遗失后图书借阅权限怎么恢复？', ['ACADEMIC']),
    ('two-goals', '帮我安排下周英语听力训练，再查一下晨星学院宿舍报修的流程。', ['ACADEMIC','CAMPUS']),
]
CASES=[
    dict(id='layout-clauses',intent='CAMPUS',question='晨星学院场地借用需经过哪些审核？',
         evidence='晨星学院场地借用规定：\n（一）所在学院审核；\n（二）学生事务中心备案。',required=['学院','学生事务中心']),
    dict(id='known-gap',intent='CAMPUS',question='晨星学院仪器押金退还需要哪些材料，多久到账？',
         evidence='晨星学院仪器押金退还须提交收据和归还确认单。本通知没有规定到账时间。',required=['收据','归还确认单'],needsGap=True),
    dict(id='dated-amount',intent='CAMPUS',question='晨星学院今年临时停车费是多少？',
         evidence='2023年度晨星学院临时停车费每小时7元。其他年度标准未公布，需查当年通知。',
         needsGap=True,forbidden=['今年临时停车费是每小时7元','今年临时停车费为每小时7元']),
]

def main():
    OUT.parent.mkdir(parents=True,exist_ok=True)
    with OUT.with_suffix('.started.json').open('x',encoding='utf-8') as f:
        json.dump({'startedAt':datetime.now(UTC).isoformat(),'planner':PLANNER,'cases':CASES},f,ensure_ascii=False,indent=2)
    settings=get_settings(); registry=AgentModelRegistry(settings)
    report={'scope':'虚构 Planner / Specialist / Response；工具使用固定合成证据，非正式 E2E','cases':[]}
    def save(row):
        report['cases'].append(row)
        OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
        print(json.dumps({'id':row['id'],'passed':row['passed']},ensure_ascii=False),flush=True)
    for key,question,intents in PLANNER:
        collector=TurnMetricsCollector('probe-'+key)
        try:
            with bind_turn_metrics(collector):
                result=registry.client_for('UnderstandingAgent').complete_structured(
                    build_intent_prompt({},question),response_model=PlanningResultV6,schema_name=UNDERSTANDING_SCHEMA_NAME,
                    options=StructuredCompletionOptions(temperature=0.0,max_tokens=registry.profile_for('UnderstandingAgent').max_tokens,repair_attempts=0),purpose='probe.route-boundaries')
            validate_planning_result_v6(result.value,{'current:0'})
            actual=[w.intent for w in result.value.workItems]
            save(dict(id=key,passed=actual==intents and all(not w.dependsOn for w in result.value.workItems),output=result.value.model_dump(mode='json'),metrics=collector.as_dict()))
        except Exception as e:
            save(dict(id=key,passed=False,error=str(e),metrics=collector.as_dict()))
    for case in CASES:
        collector=TurnMetricsCollector(case['id'])
        try:
            with bind_turn_metrics(collector),capture_evidence_diagnostics() as diagnostics:
                row=run_case(case,settings,registry)
            row.update(metrics=collector.as_dict(),evidenceDiagnostics=diagnostics)
            save(row)
        except Exception as e:
            save(dict(id=case['id'],passed=False,error=str(e),metrics=collector.as_dict()))
    report.update(completedAt=datetime.now(UTC).isoformat(),passed=all(r['passed'] for r in report['cases']))
    OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')

if __name__=='__main__': main()
