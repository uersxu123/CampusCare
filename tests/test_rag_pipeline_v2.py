"""单查询主路径与保留的历史 coverage 纯函数兼容。"""
import json
from types import SimpleNamespace
from dataclasses import replace
import pytest
from app.services.knowledge import CandidateSearchResult, KnowledgeSearchResult
from app.services.rag_pipeline import (
    RagPipeline, RerankCandidateAssessment,
    RerankOutput, weighted_rrf, _select_with_facet_coverage,
)


def run_pipeline(*, rerank_error=False, empty=False, channels=(False, False),
                 top_k=1, facets=None, invalid=None, levels=None):
    events = []
    rows = [KnowledgeSearchResult(chunk_id=i, source="学生手册", content=f"子块{i}",
             score=1, parent_chunk_id=100) for i in (1, 2)] if not empty else []
    class Knowledge:
        def search_candidates(self, **kwargs):
            events.append(("retrieve", kwargs["query"]))
            return CandidateSearchResult(tuple(rows), tuple(reversed(rows)), tuple(rows),
                {"bm25Degraded": channels[0], "vectorDegraded": channels[1]})
        def expand_context(self, child):
            events.append(("parent", child.chunk_id))
            return replace(child, content="MySQL 中的完整父块")
    class Client:
        def complete_structured(self, messages, *, schema_name, response_model, **kwargs):
            events.append((schema_name, None))
            assert schema_name == "rag_relevance_rerank_v7"
            payload = json.loads(messages[-1].content)
            assert payload["taskText"] == "补考怎么办"
            assert "evidenceFacets" not in payload
            if rerank_error:
                raise RuntimeError("reranker unavailable")
            ids = [item["evidenceId"] for item in payload["candidates"]]
            assert len(ids) == len(set(ids))
            values = [{"evidenceId": key, "relevanceLevel":
                       (levels or {}).get(key, "HIGH" if key == "ev_chunk_2" else "MEDIUM")} for key in ids]
            if invalid == "unknown":
                values[0]["evidenceId"] = "不存在的证据"
            elif invalid == "duplicate":
                values[1]["evidenceId"] = values[0]["evidenceId"]
            elif invalid == "missing":
                values.pop()
            elif invalid == "extra":
                values[0]["matchedFacetIds"] = ["f999"]
            # 使用真实请求 schema，非法 ID/旧字段不能借助宽松替身绕过校验。
            return SimpleNamespace(value=response_model.model_validate({"assessments": values}))
    result = RagPipeline(knowledge=Knowledge(), client=Client(), settings=SimpleNamespace()).search(
        query="补考怎么办", facets=facets, top_k=top_k)
    return result, events


def test_order_and_mysql_parent():
    result, events = run_pipeline()
    assert events == [("retrieve", "补考怎么办"), ("rag_relevance_rerank_v7", None), ("parent", 2)]
    assert result["status"] == "OK"
    assert result["qualityStatus"] == "ASSESSED"
    assert result["items"][0]["content"] == "子块2"
    assert result["items"][0]["parentContent"] == "MySQL 中的完整父块"
    assert result["items"][0]["parentChunkId"] == 100
    assert result["evidenceCoverage"]["status"] == "NOT_APPLICABLE"


def test_no_query_rewrite_or_second_retrieval():
    result, events = run_pipeline()
    assert result["executedQueries"] == ["补考怎么办"]
    assert [e for e in events if e[0] != "parent"] == [
        ("retrieve", "补考怎么办"), ("rag_relevance_rerank_v7", None)]
    assert result["diagnostics"]["stages"] == ["bm25", "chroma", "rrf", "relevance_reranker"]


def test_rerank_failure_respects_top_k_and_does_not_repeat_retrieval():
    result, events = run_pipeline(rerank_error=True, top_k=2)
    assert result["qualityStatus"] == "RERANK_DEGRADED"
    assert result["diagnostics"]["rerankDegraded"]
    assert [i["evidenceId"] for i in result["items"]] == ["ev_chunk_1", "ev_chunk_2"]
    assert len([e for e in events if e[0] == "retrieve"]) == 1


def test_empty_skips_reranker():
    result, events = run_pipeline(empty=True)
    assert result["status"] == "EMPTY" and result["items"] == []
    assert events == [("retrieve", "补考怎么办")]


def test_failed_channels():
    result, events = run_pipeline(channels=(True, True))
    assert result["error"]["code"] == "RAG_UNAVAILABLE"
    assert events == [("retrieve", "补考怎么办")]


@pytest.mark.parametrize("channels", [(True, False), (False, True)])
def test_one_available_channel_still_returns_evidence(channels):
    result, events = run_pipeline(channels=channels)
    assert result["status"] == "OK"
    assert len([e for e in events if e[0] == "retrieve"]) == 1


def test_rrf_formula():
    scores = weighted_rrf([(["a", "b"], 1), (["b", "a"], 1)])
    assert scores["a"] == pytest.approx(1/61 + 1/62)
    assert scores["a"] == scores["b"]


def test_legacy_facets_are_diagnostic_only_not_multiple_queries_or_sufficiency():
    result, events = run_pipeline(facets=["材料", "截止日期"])
    assert result["executedQueries"] == ["补考怎么办"]
    assert result["diagnostics"]["queryMode"] == "validated-task-text"
    assert result["diagnostics"]["deprecatedFacetsIgnored"] is True
    assert result["evidenceCoverage"]["status"] == "NOT_APPLICABLE"
    assert len([e for e in events if e[0] == "retrieve"]) == 1


@pytest.mark.parametrize("invalid", ["unknown", "duplicate", "missing", "extra"])
def test_invalid_relevance_contract_degrades_without_repair(invalid):
    result, events = run_pipeline(invalid=invalid)
    assert result["diagnostics"]["rerankDegraded"]
    assert result["diagnostics"]["rerankErrorCode"] == "ValidationError"
    assert len([e for e in events if e[0] == "rag_relevance_rerank_v7"]) == 1
    assert len([e for e in events if e[0] == "retrieve"]) == 1


def test_low_relevance_is_not_promoted_to_supported_answer():
    result, _ = run_pipeline(levels={"ev_chunk_1": "LOW", "ev_chunk_2": "NONE"})
    assert result["status"] == "EMPTY"
    assert result["qualityStatus"] == "ASSESSED"


def _assessment(evidence_id, level="HIGH", facets=(), direct=True):
    return RerankCandidateAssessment(
        evidenceId=evidence_id,
        relevanceLevel=level,
        matchedFacetIds=list(facets),
        directSupport=direct,
    )


def test_soft_coverage_multi_facet_reserves_missing_facet_without_reordering_strong_items():
    ranked = [f"e{i}" for i in range(1, 8)]
    rerank = RerankOutput(
        ranked=ranked,
        assessments=[
            _assessment("e1", facets=("f1",)),
            _assessment("e2", facets=("f1",)),
            _assessment("e3", facets=("f1",)),
            _assessment("e4", facets=("f2",)),
            _assessment("e5", facets=("f2",)),
            _assessment("e6", facets=("f3",)),
            _assessment("e7", level="LOW", facets=("f3",)),
        ],
    )
    selected, coverage = _select_with_facet_coverage(
        rerank=rerank,
        facets=["兼得", "休学", "复学"],
        fallback_ids=ranked,
        top_k=5,
    )
    assert selected == ["e1", "e2", "e3", "e4", "e6"]
    assert coverage["status"] == "FULL"
    assert coverage["coveredFacets"] == ["f1", "f2", "f3"]
    assert coverage["selectionApplied"]
    assert coverage["coverageRatio"] == 1.0


def test_soft_coverage_single_facet_is_noop_when_topk_already_covers():
    ranked = ["e1", "e2", "e3"]
    rerank = RerankOutput(
        ranked=ranked,
        assessments=[
            _assessment("e1", facets=("f1",)),
            _assessment("e2", facets=()),
            _assessment("e3", facets=()),
        ],
    )
    selected, coverage = _select_with_facet_coverage(
        rerank=rerank,
        facets=["本科生最大借阅册数"],
        fallback_ids=ranked,
        top_k=2,
    )
    assert selected == ["e1", "e2"]
    assert coverage["selectionMode"] == "single-facet-soft-coverage"
    assert not coverage["selectionApplied"]
    assert coverage["status"] == "FULL"


def test_soft_coverage_single_facet_can_pull_one_reliable_candidate_into_topk():
    ranked = ["e1", "e2", "e3", "e4"]
    rerank = RerankOutput(
        ranked=ranked,
        assessments=[
            _assessment("e1", facets=()),
            _assessment("e2", facets=()),
            _assessment("e3", facets=()),
            _assessment("e4", level="MEDIUM", facets=("f1",)),
        ],
    )
    selected, coverage = _select_with_facet_coverage(
        rerank=rerank,
        facets=["唯一证据目标"],
        fallback_ids=ranked,
        top_k=3,
    )
    assert selected == ["e1", "e2", "e4"]
    assert coverage["status"] == "FULL"
    assert coverage["selectionApplied"]


def test_soft_coverage_does_not_promote_low_or_indirect_evidence():
    ranked = ["e1", "e2", "e3", "e4"]
    rerank = RerankOutput(
        ranked=ranked,
        assessments=[
            _assessment("e1", facets=("f1",)),
            _assessment("e2", facets=("f2",), direct=False),
            _assessment("e3", level="LOW", facets=("f2",)),
            _assessment("e4", facets=()),
        ],
    )
    selected, coverage = _select_with_facet_coverage(
        rerank=rerank,
        facets=["目标一", "目标二"],
        fallback_ids=ranked,
        top_k=3,
    )
    assert selected == ["e1", "e4"]
    assert coverage["status"] == "PARTIAL"
    assert coverage["coveredFacets"] == ["f1"]
    assert coverage["uncoveredFacets"] == ["f2"]
    assert not coverage["selectionApplied"]


def test_soft_coverage_one_candidate_can_cover_two_facets_once():
    ranked = ["e1", "e2", "e3"]
    rerank = RerankOutput(
        ranked=ranked,
        assessments=[
            _assessment("e1", facets=("f1", "f2")),
            _assessment("e2", facets=("f3",)),
            _assessment("e3", facets=()),
        ],
    )
    selected, coverage = _select_with_facet_coverage(
        rerank=rerank,
        facets=["休学", "复学", "兼得"],
        fallback_ids=ranked,
        top_k=2,
    )
    assert selected == ["e1", "e2"]
    assert coverage["status"] == "FULL"
    assert coverage["coverageRatio"] == 1.0


def test_soft_coverage_rerank_degraded_is_unknown_not_fake_uncovered():
    selected, coverage = _select_with_facet_coverage(
        rerank=None,
        facets=["目标一", "目标二"],
        fallback_ids=["e1", "e2", "e3"],
        top_k=2,
    )
    assert selected == ["e1", "e2"]
    assert coverage["status"] == "UNKNOWN"
    assert coverage["coverageRatio"] is None
    assert all(item["status"] == "UNKNOWN" for item in coverage["facets"])
