from dataclasses import replace
import json
import multiprocessing
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.evaluation.config import EvaluationSettings
from app.evaluation.contracts import EndToEndCase, EvaluationRuntimeOutcome
from app.evaluation.judges.business import BusinessJudgeOutput, business_judge_messages
from app.evaluation.judges.deepseek import BusinessJudgeResult, DeepSeekJudge
from app.evaluation.runner import _business_contract, _outcome_attribution
from app.evaluation.runtime.action_resolution import resolve_evaluation_action
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient, StructuredCompletionError, StructuredCompletionOptions
from app.services.knowledge import KnowledgeSearchResult
from app.services.model_completion import ModelCompletion, ModelCompletionMetadata, ModelFinishReason
from app.services.rag_pipeline import RagPipeline, RerankOutput, rerank_response_model
from app.services.retrieval_capture import (
    capture_retrieval_candidates, merge_retrieval_telemetry,
    record_retrieval_candidates, retrieval_telemetry,
)
from app.services.tool_executor import parse_call_tool_result
from app.services.turn_execution import _distinct_candidates
from app.services.turn_metrics import TurnMetricsCollector, bind_turn_metrics


def _metadata(reason=ModelFinishReason.STOP):
    return ModelCompletionMetadata("ollama", "fake", reason, True, True, "done", reason.value, 100)


def _assessment(eid="e1", facets=None):
    return {"evidenceId": eid, "relevanceLevel": "HIGH", "matchedFacetIds": facets or [], "directSupport": True}


def test_request_schema_requires_rank_and_binds_candidate_and_facet_ids():
    model = rerank_response_model(["e1"], [])
    assert set(model.model_json_schema()["required"]) == {"ranked", "assessments"}
    for invalid in ({}, {"assessments": [_assessment()]},
                    {"ranked": ["e1"], "assessments": [_assessment(facets=["invented"])]},
                    {"ranked": ["unknown"], "assessments": [_assessment()]},
                    {"ranked": ["e1"], "assessments": []}):
        with pytest.raises(ValidationError):
            model.model_validate(invalid)
    assert model.model_validate({"ranked": ["e1"], "assessments": [_assessment()]}).ranked == ["e1"]


def test_rerank_retains_semantic_rejection_reason():
    row = KnowledgeSearchResult(chunk_id=1, source="合成", content="测试证据", score=1.0)
    client = SimpleNamespace(complete_structured=lambda *args, **kwargs: SimpleNamespace(
        value=RerankOutput(ranked=["e1"], assessments=[_assessment(), _assessment()])))
    pipeline = RagPipeline(knowledge=None, client=client, settings=SimpleNamespace())
    assert pipeline._rerank("测试", [], ["e1"], {"e1": row}) is None
    assert pipeline._rerank_failure["rerankErrorCode"] == "RERANK_INCOMPLETE_ASSESSMENTS"


def test_request_schema_requires_each_assessment_id_exactly_once():
    model = rerank_response_model(["e1", "e2"], ["f1"])
    with pytest.raises(ValidationError):
        model.model_validate({"ranked": ["e1"], "assessments": [_assessment(), _assessment()]})
    assert model.model_validate({"ranked": ["e2", "e1"],
                                 "assessments": [_assessment(), _assessment("e2", ["f1"])]}).assessments[1].evidenceId == "e2"


def test_rerank_degradation_survives_tool_protocol():
    result = parse_call_tool_result({"isError": False, "structuredContent": {
        "status": "OK", "items": [{"contextId": "knowledge:1"}],
        "qualityStatus": "RERANK_DEGRADED", "diagnostics": {"rerankDegraded": True},
    }})
    assert result.ok and result.degraded
    assert result.as_payload()["data"]["qualityStatus"] == "RERANK_DEGRADED"


def _child_capture(queue):
    with capture_retrieval_candidates() as captured:
        record_retrieval_candidates([
            KnowledgeSearchResult(chunk_id=1, source="测试", content="中文证据一", score=1),
            KnowledgeSearchResult(chunk_id=2, source="测试", content="中文证据二", score=1),
        ])
    queue.put({"retrieval": retrieval_telemetry(captured)})


def test_candidate_observation_crosses_process_only_through_envelope():
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    with capture_retrieval_candidates() as parent:
        child = ctx.Process(target=_child_capture, args=(queue,))
        child.start()
        child.join(15)
        if child.is_alive():
            child.terminate()
            child.join(2)
            pytest.fail("候选遥测子进程未按时退出")
        assert child.exitcode == 0
        assert parent.status == "NOT_OBSERVED"
        merge_retrieval_telemetry(queue.get(timeout=1))
        assert parent.status == "OBSERVED"
        assert len(_distinct_candidates(parent)) == 2
        assert parent[1]["contextId"] == "knowledge:2"


def test_empty_observation_is_distinct_from_missing():
    with capture_retrieval_candidates() as capture:
        merge_retrieval_telemetry({"retrieval": {"status": "OBSERVED_EMPTY", "candidates": []}})
        assert capture.status == "OBSERVED_EMPTY"
        merge_retrieval_telemetry({})
        assert capture.status == "PARTIAL"


def _case_outcome():
    case = EndToEndCase(id="synthetic", turns=["测试问题"], expected_action="ANSWER", reference="合成事实",
                        reference_context_ids=["knowledge:1"])
    outcome = EvaluationRuntimeOutcome("synthetic", 0, "合成回答", {}, "LOW", "UNRESOLVED", True, True,
                                       [], [], ["knowledge:1"], ["合成事实"], "test", {},
                                       prompt_context_ids=["knowledge:1"], actual_tools=["rag_search"])
    return case, outcome


def test_unobserved_candidates_do_not_become_retrieval_miss():
    case, outcome = _case_outcome()
    attribution = _outcome_attribution(case, outcome, business_passed=False, safety_passed=None)
    assert attribution["layers"]["retrieval"] == "notScored"
    assert attribution["layers"]["usableEvidence"] == "correct"
    assert attribution["primary"] == "ANSWER_QUALITY_ERROR"
    attribution = _outcome_attribution(case, replace(outcome, retrieval_observation="OBSERVED_EMPTY"),
                                       business_passed=False, safety_passed=None)
    assert attribution["primary"] == "RETRIEVAL_MISS"


def test_current_retrieval_status_does_not_claim_final_answer_action():
    harness = SimpleNamespace(clarification_request=None, specialist_results=[{
        "status": "COMPLETED", "reasonCode": "RETRIEVAL_COMPLETED",
        "evidenceItems": [{"contextId": "knowledge:1"}],
    }])
    assert resolve_evaluation_action({}, harness).action == "UNRESOLVED"
    case, outcome = _case_outcome()
    payload = json.loads(business_judge_messages(case, outcome)[1].content.split("\n", 1)[1].rsplit("\n", 1)[0])
    assert "actualAction" not in payload
    assert payload["actualResponse"] == "合成回答"


def test_judge_pass_cannot_override_action_contract():
    case, outcome = _case_outcome()
    output = BusinessJudgeOutput(observed_action="ABSTAIN", relevance=1, accuracy=1, completeness=1,
                                 helpfulness=1, action_correctness=1, verdict="PASS", reasons=[], unsupported_claims=[])
    judged = BusinessJudgeResult(output, "json_schema", 0, "fake", "v3")
    contract = _business_contract(case, replace(outcome, action=output.observed_action), judged)
    assert not contract["passed"]
    assert "ACTION_SCORE_CONFLICT" in contract["judgeIssues"]


def test_failed_structured_validation_is_a_failed_metric(monkeypatch):
    client = AiClient(Settings(_env_file=None, ai_provider="ollama"))
    monkeypatch.setattr(client, "_complete_with_schema", lambda *args, **kwargs: ModelCompletion("{}", _metadata()))
    collector = TurnMetricsCollector("structured-contract")
    with bind_turn_metrics(collector), pytest.raises(StructuredCompletionError):
        client.complete_structured([AiMessage(role="user", content="合成")], response_model=RerankOutput,
                                   schema_name="contract", options=StructuredCompletionOptions(0, 100, repair_attempts=0))
    assert collector.as_dict()["calls"][0]["status"] == "FAILED"
    assert collector.as_dict()["calls"][0]["errorCode"] == "STRUCTURED_OUTPUT_INVALID"


def test_judge_truncation_has_judge_specific_error(monkeypatch):
    judge = DeepSeekJudge(Settings(_env_file=None), EvaluationSettings(_env_file=None, judge_provider="ollama"))
    def incomplete(*args, **kwargs):
        raise StructuredCompletionError("TURN_BUDGET_EXCEEDED", "截断", metadata=_metadata(ModelFinishReason.LENGTH))
    monkeypatch.setattr(judge.client, "complete_structured", incomplete)
    assert judge._judge_once([], StructuredCompletionOptions(0, 100, repair_attempts=0)).error_code == "JUDGE_OUTPUT_TRUNCATED"
