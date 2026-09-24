from types import SimpleNamespace

import pytest

from app.agents.autonomous import ResponseAgent, _validated_response_updates, _verified_partial_response, _requires_policy_evidence
from app.core.enums import IntentType
from app.services.evidence_contract import locate_quote, normalize_quote
from app.services.execution_control import ExecutionBudget, bind_execution_budget, bounded_agent_execution, current_execution_budget
from app.services.agent_loop import AgentLoop
from app.services.tool_models import AiToolCall, AiToolDefinition, ToolResult


@pytest.mark.parametrize('original,quote', [
    ('学校进行审核，\n符合要求后登记。', '学校进行审核，符合要求后登记。'),
    ('（一）\n申请须经学院审核；\n（二）提交证明。', '（一）申请须经学院审核；（二）提交证明。'),
    ('每年限额\n500元。', '每年限额500元。'),
    ('学时为32\n学时。', '学时为32学时。'),
])
def test_layout_quote_returns_original_offsets(original, quote):
    assert locate_quote(quote, original) == original
    assert normalize_quote(original) == normalize_quote(quote)


@pytest.mark.parametrize('original,quote', [
    ('费用为1\n500元', '费用为1500元'), ('费用1.5元', '费用15元'),
    ('不得申请', '可以申请'), ('class room', 'classroom'),
    ('适用2025年', '适用2026年'), ('申请，审核后办理', '申请审核后办理'),
])
def test_layout_does_not_authorize_changed_facts(original, quote):
    assert locate_quote(quote, original) is None


def test_response_schema_exposes_nested_update_constraints():
    schema = ResponseAgent.FinalContract.model_json_schema()
    fields = schema['$defs']['ResponseWorkItemUpdate']['properties']
    assert fields['answerStatus']['enum'] == ['FULL', 'PARTIAL', 'NONE', 'NOT_REQUIRED']
    assert schema['$defs']['ResponseWorkItemUpdate']['additionalProperties'] is False
    with pytest.raises(ValueError):
        ResponseAgent.FinalContract(answerText='回答', workItemUpdates=[{'workItemId':'W1','answerStatus':'UNKNOWN'}])


def test_response_empty_updates_and_typed_updates():
    states = [{'workItemId':'W1','answerStatus':'PARTIAL','missingInfo':['截止时间'],'evidenceRefs':['e1']}]
    assert _validated_response_updates([], states, []) == []
    value = ResponseAgent.FinalContract(answerText='尚未确认截止时间', workItemUpdates=[
        {'workItemId':'W1','answerStatus':'PARTIAL','missingInfo':['截止时间'],'evidenceRefs':['e1']}])
    assert _validated_response_updates(value.workItemUpdates, states, [])[0]['answerStatus'] == 'PARTIAL'
    for update in [dict(workItemId='W1',answerStatus='NOT_REQUIRED'),dict(workItemId='W1',answerStatus='PARTIAL'),
                   dict(workItemId='W1',answerStatus='FULL',evidenceRefs=['e1'])]:
        with pytest.raises(ValueError):
            _validated_response_updates([update],states,[])


def test_rejected_update_fallback_never_reuses_unverified_answer():
    result = _verified_partial_response([dict(objective='补办申请',status='PARTIAL',answerStatus='PARTIAL',
        answerBrief='任何人都能当天免费办理',missingInfo=['费用和办理时间'],
        evidenceNotes=[{'evidenceId':'e1','quote':'申请须由学院审核。'}])])
    assert '任何人' not in result and '当天免费' not in result
    assert '学院审核' in result and '费用和办理时间' in result


def test_specialist_budget_leaves_time_for_synthesis():
    class Specialist:
        execution_reserve_seconds = 20
        services = SimpleNamespace(settings=SimpleNamespace(agent_loop_deadline_seconds=100))
        @bounded_agent_execution
        def run(self): return current_execution_budget().remaining()
    with bind_execution_budget(ExecutionBudget.start(35)):
        assert 14 < Specialist().run() <= 15
        assert current_execution_budget().remaining() > 34
    with bind_execution_budget(ExecutionBudget.start(10)):
        assert Specialist().run() == 0


def test_policy_recognition_includes_recognition_and_corrections():
    assert _requires_policy_evidence(IntentType.ACADEMIC,{'taskText':'如何认定科技成果？'})
    assert _requires_policy_evidence(IntentType.ACADEMIC,{'taskText':'志愿时长更正怎么操作？'})
    assert not _requires_policy_evidence(IntentType.CHAT,{'taskText':'翻译这段认定办法'})
    assert _requires_policy_evidence(IntentType.CAMPUS,{'taskText':'今年停车费多少？'},['chat_readonly__rag_search'])


def test_single_search_disappears_while_authorized_read_remains():
    rag = AiToolDefinition('rag_search','检索',{'type':'object'})
    read = AiToolDefinition('read_tool_evidence','回读',{'type':'object'})
    snapshots = []
    class Client:
        def complete_with_tools(self, messages, tools, **kwargs):
            snapshots.append([t.name for t in tools])
            calls = (AiToolCall('c1','rag_search',{'query':'办理条件'}),) if len(snapshots)==1 else ()
            return SimpleNamespace(content='尚缺截止日期',tool_calls=calls,verified_complete=True)
    class Executor:
        def execute(self, **kwargs):
            return ToolResult(True,'OK',data={'items':[{'evidenceId':'e1','content':'学院审核'}]},
                              execution_id='stored1',persisted=True,dispatched=True)
    result = AgentLoop(client=Client(),executor=Executor(),max_rag_calls=1,max_model_rounds=4).run(
        agent_name='Campus',messages=[],tools=[rag,read])
    assert snapshots == [['rag_search'], ['read_tool_evidence']]
    assert result.stop_reason == 'COMPLETED'


def test_attribution_uses_intent_list_and_does_not_guess_missing_prompt_capture():
    from app.evaluation.contracts import EndToEndCase
    from app.evaluation.runner import _outcome_attribution
    case = EndToEndCase.model_validate(dict(id='synthetic',turns=['查询社团审批'],expected_action='ANSWER',reference='办理审批',
        reference_context_ids=['knowledge:1'],expected_route=dict(primaryIntent='CAMPUS',intents=['CAMPUS'],riskLevel='LOW',
        workItemCount=1,workItemIntents=['CAMPUS'],dependencyEdges=[],missingArgumentNamesByWorkItem=[[]])))
    actual = SimpleNamespace(route={'primaryIntent':'CAMPUS','intents':['CAMPUS','ACADEMIC']},actual_tools=[],action='ANSWER',
        retrieval_observation='OBSERVED',retrieved_context_ids=['knowledge:1'],usable_context_ids=['knowledge:1'],
        prompt_context_ids=[],prompt_contexts_observed=False,knowledge_requested=True,tool_diagnostics={},
        infra_error_codes=[],error_code='AGENT_FINAL_RESPONSE_MISSING')
    result = _outcome_attribution(case,actual,business_passed=None,safety_passed=None)
    assert result['primary'] == 'ROUTING_ERROR'
    assert 'CONTEXT_TRANSFER_ERROR' not in result['secondary']
