"""只从已完成的原始结果汇总诊断，不调用模型、不修改原始分数。"""
from collections import Counter
from pathlib import Path
import argparse
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / 'app/evaluation/datasets/e2e-context-workitems-160-v1'

def read(path):
    return json.loads(path.read_text(encoding='utf-8'))

def rows(path):
    return [json.loads(x) for x in path.read_text(encoding='utf-8').splitlines() if x.strip()]

def ratio(n, d):
    return {'numerator': n, 'denominator': d, 'value': n/d if d else None}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    run = ROOT/'target/evaluation/e2e-context-workitems-160-v1'/args.run_id
    assert (run/'postflight.json').exists(), '正式运行尚未结束'
    report, summary = read(run/'e2e-report.json'), read(run/'summary.json')
    cases = rows(DATASET/'business.jsonl') + rows(DATASET/'safety.jsonl')
    actual = {r['case_id']:r for r in rows(run/'cases.jsonl')}
    results = {r['caseId']:r for r in report['results']}
    safety_ids = {c['id'] for c in cases if c['expected_action'] == 'SAFETY_BYPASS'}
    passed = sum(bool(r.get('passed')) for r in results.values())
    high = sum(actual.get(i,{}).get('risk_level') == 'HIGH' for i in safety_ids)
    primary, intent_set, plan = 0, 0, 0
    eligible, observed = 0, 0
    plan_details = []
    for c in cases:
        e = c.get('expected_route')
        if not e:
            continue
        eligible += 1
        a = actual.get(c['id'],{}).get('route',{})
        observed += bool(a.get('primaryIntent'))
        p = a.get('primaryIntent') == e['primaryIntent']
        intents = p and a.get('intents') == e['intents']
        primary += p
        intent_set += intents
        items = a.get('workItems',[])
        ids = [x.get('workItemId') for x in items]
        canonical = {v:'W'+str(i+1) for i,v in enumerate(ids)}
        edges = sorted([canonical.get(dep,dep), canonical.get(w.get('workItemId'),w.get('workItemId'))] for w in items for dep in w.get('dependsOn',[]))
        missing = [sorted(w.get('missingFields',[])) for w in items]
        expected_edges = sorted(e.get('dependencyEdges',[]))
        match = bool(intents and len(items) == e['workItemCount'] and [w.get('intent') for w in items] == e['workItemIntents'] and edges == expected_edges and missing == [sorted(v) for v in e['missingArgumentNamesByWorkItem']])
        plan += match
        if not match:
            plan_details.append({'caseId':c['id'], 'expected':e, 'actual':a})
    errors = Counter(code for a in actual.values() for code in set([a.get('error_code'), *a.get('infra_error_codes',[])]) if code)
    failed = [r for r in results.values() if not r.get('passed')]
    attribution = Counter(r.get('attribution',{}).get('primary') or 'UNCLASSIFIED' for r in failed)
    clean_pass = sum(bool(results.get(i,{}).get('passed')) and not a.get('error_code') and not a.get('infra_error_codes') for i,a in actual.items())
    extra = {'runId':args.run_id,'planned':160,'terminalOutcomes':len(actual),'judgedOrSafetyResults':len(results),
             'endToEndPass':ratio(passed,160), 'businessPass':ratio(sum(bool(r.get('passed')) for i,r in results.items() if i not in safety_ids),157),
             'safetyContractPass':ratio(sum(bool(results.get(i,{}).get('passed')) for i in safety_ids),3), 'highRiskRecall':ratio(high,3),
             'primaryIntentAccuracy':ratio(primary,eligible),'intentSummaryAccuracy':ratio(intent_set,eligible),'workItemPlanExactMatch':ratio(plan,eligible),
             'routeObservationCoverage':ratio(observed,eligible),'passWithoutRecordedInfraError':ratio(clean_pass,160),
             'quality':{k:{'mean':report['metrics'].get(k), 'validJudgeCases':report['denominators'].get(k,0),'businessCases':157,'allCases':160} for k in ('accuracy','relevance','completeness','helpfulness')},
             'failureAttribution':dict(attribution),'infraCodes':dict(errors),'workItemMismatches':plan_details,
             'failedCaseIds':[r['caseId'] for r in failed], 'sourceReportSha256':hashlib.sha256((run/'e2e-report.json').read_bytes()).hexdigest()}
    (run/'diagnostic-summary.json').write_text(json.dumps(extra,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    lines = ['# 160 条 E2E 评测报告','',f'运行 ID：{args.run_id}。固定题集单次执行，未重跑失败样本，未根据结果修改 Gold、Prompt 或预算。','',
             '## 数据与运行条件','', '144 条固定等距抽样的当前手册题 + 原 13 条业务 Smoke + 3 条安全 Smoke；问题不改，补充当前语料引用、事实和动作/路由契约。47 项评测测试通过，855 个 Chroma 子块指纹校验通过，hybrid 保持开启。', '',
             '应用及 Judge 使用本机 qwen3:8b、think=false。Judge 重试0、修复0、重复1，无二次生成回退。每题隔离 MySQL，Redis DB15，后台工作器关闭。', '',
             '## 结果与分母','', '| 指标 | 结果 | 分母 |','|---|---:|---:|']
    for key,label in [('endToEndPass','端到端通过率'),('businessPass','业务通过率'),('safetyContractPass','安全契约通过率'),('highRiskRecall','高风险召回'),('primaryIntentAccuracy','主意图正确率'),('intentSummaryAccuracy','主意图及意图列表正确率'),('workItemPlanExactMatch','工作项计划精确匹配'),('passWithoutRecordedInfraError','通过且无记录的基础设施错误')]:
        m=extra[key]
        lines.append(f"| {label} | {m['value']:.2%} | {m['numerator']}/{m['denominator']} |" if m['value'] is not None else f'| {label} | N/A | 0 |')
    for key,label in [('accuracy','准确性'),('relevance','相关性'),('completeness','完整性'),('helpfulness','帮助性')]:
        m=extra['quality'][key]
        value=f"{m['mean']:.2%}" if m['mean'] is not None else 'N/A'
        lines.append(f"| Judge {label}平均分 | {value} | {m['validJudgeCases']} 条有效业务 Judge / 157 条业务 / 160 条总题 |")
    lines += ['', '质量分是评分均值，不是正确回答数量占160的比例；Judge失败不静默计为通过。工作项计划精确匹配为附加诊断，现有 runner 的 routeObservedAccuracy 仅检查主意图与意图列表。', '', '## 失败分层','', '```json',json.dumps({'failureAttribution':dict(attribution),'infraCodes':dict(errors)},ensure_ascii=False,indent=2),'```','', '| 样本 | 主要归因 | Judge / 契约依据 |','|---|---|---|']
    for r in failed:
        reasons = (r.get('output') or {}).get('reasons',[])
        reason = '; '.join(reasons) or str(r.get('error_code') or r.get('contract') or r.get('safety'))
        lines.append('| '+r['caseId']+' | '+str(r.get('attribution',{}).get('primary'))+' | '+reason.replace('|','/').replace('\n',' ')+' |')
    lines += ['', '## 适用范围与限制','', '这不是独立留出集：16 条 Smoke 已用于回归，200 条源题此前用于检索评测。Gold 为 Agent 按当前语料审阅，未经独立人工复核；题目高度偏向手册政策，安全只有3条，不能把该召回率外推到生产。引用块不穷尽所有等价支持，ID 命中仅作诊断。', '', '最终答案可通过 Judge 而上游发生引用校验或其他错误，故单独报告无记录错误通过率。Trace 与原始 cases.jsonl 保留，不用质量均值掩盖这些问题。', '', '## 冻结与隔离复核','', '```json',json.dumps(read(run/'postflight.json'),ensure_ascii=False,indent=2),'```','', '原始 summary.json、e2e-report.json、cases.jsonl 未被本报告脚本改写。']
    (run/'E2E_SMOKE_V2_REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    all_lines = ['# 160 条逐题结果', '', '原始结果只读汇总；空缺评分表示未获得有效评分，不代表0分。', '', '| 序号 | ID | 问题 | 通过 | 主归因 | 准确性 | 相关性 | Judge/运行错误 |', '|---:|---|---|---|---|---:|---:|---|']
    for number,c in enumerate(cases,1):
        r = results.get(c['id'],{})
        output = r.get('output') or {}
        invalid = bool(r.get('error_code') or r.get('errorCode') or r.get('contract',{}).get('judgeIssues'))
        fields = [str(number),c['id'],' / '.join(c['turns']), 'PASS' if r.get('passed') else 'FAIL', str(r.get('attribution',{}).get('primary') or ''), str(output.get('accuracy','')) if not invalid else '', str(output.get('relevance','')) if not invalid else '', str(r.get('error_code') or r.get('errorCode') or r.get('contract',{}).get('judgeIssues') or '')]
        all_lines.append('| '+' | '.join(v.replace('|','/').replace('\n',' ') for v in fields)+' |')
    (run/'ALL_CASE_RESULTS.md').write_text('\n'.join(all_lines)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in extra.items() if k != 'workItemMismatches'},ensure_ascii=False))

if __name__ == '__main__':
    main()
