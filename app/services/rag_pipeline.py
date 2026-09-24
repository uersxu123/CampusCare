from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from app.schemas.dtos import AiMessage
from app.services.ai import AiClient, StructuredCompletionOptions
from app.services.knowledge import CandidateSearchResult, KnowledgeSearchResult, KnowledgeService
from app.services.knowledge_scoring import reciprocal_rank_fusion


class RerankCandidateAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidenceId: str = Field(min_length=1, max_length=160)
    relevanceLevel: Literal["HIGH", "MEDIUM", "LOW", "NONE"]
    matchedFacetIds: list[str] = Field(default_factory=list, max_length=3)
    directSupport: bool


class RerankOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ranked: list[str] = Field(max_length=24)
    assessments: list[RerankCandidateAssessment] = Field(max_length=24)


class RelevanceAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidenceId: str = Field(min_length=1, max_length=160)
    relevanceLevel: Literal["HIGH", "MEDIUM", "LOW", "NONE"]


class RelevanceOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assessments: list[RelevanceAssessment] = Field(min_length=1, max_length=24)


def relevance_response_model(evidence_ids: list[str]) -> type[RelevanceOutput]:
    assessments = tuple(create_model(
        f"RequestRelevanceAssessment{index}", __base__=RelevanceAssessment,
        evidenceId=(Literal[evidence_id], ...),
    ) for index, evidence_id in enumerate(evidence_ids))
    return create_model("RequestRelevanceOutput", __base__=RelevanceOutput, assessments=(tuple[assessments], ...))


def rerank_response_model(evidence_ids: list[str], facet_ids: list[str]) -> type[RerankOutput]:
    """将本次请求的合法标识约束传到模型；仍保留下游唯一性和覆盖校验。"""
    evidence_type = Literal[tuple(evidence_ids)]
    facet_type = Literal[tuple(facet_ids)] if facet_ids else str
    assessments = tuple(create_model(
        f"RequestRerankAssessment{index}", __base__=RerankCandidateAssessment,
        evidenceId=(Literal[evidence_id], ...),
        matchedFacetIds=(list[facet_type], Field(max_length=len(facet_ids))),
    ) for index, evidence_id in enumerate(evidence_ids))
    return create_model(
        "RequestRerankOutput", __base__=RerankOutput,
        ranked=(list[evidence_type], Field(min_length=1, max_length=len(evidence_ids))),
        assessments=(tuple[assessments], ...),
    )


class RewriteOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=500)


def weighted_rrf(
    rankings: list[tuple[list[str], float]],
) -> dict[str, float]:
    return reciprocal_rank_fusion(rankings, k=60)


class RagPipeline:
    """已验证任务句的一次混合检索：BM25 + Chroma → RRF → 相关性重排。

    旧 facets 参数仅用于兼容调用，不展开查询，也不判定答案充分性。
    历史辅助函数保留供旧记录及独立兼容测试使用，不属于当前 search 主路径。
    """

    def __init__(self, *, knowledge: KnowledgeService, client: AiClient, settings):
        self.knowledge = knowledge
        self.client = client
        self.settings = settings
        self._deadline: float | None = None
        self._rerank_failure: dict[str, Any] = {}

    def search(
        self,
        *,
        query: str,
        facets: list[str] | tuple[str, ...] | None = None,
        top_k: int = 5,
        site: str | None = None,
        protected_terms: set[str] | None = None,
        remaining_seconds: float | None = None,
    ) -> dict[str, Any]:
        pipeline_budget = float(getattr(self.settings, "rag_pipeline_deadline_seconds", 60.0))
        effective_budget = pipeline_budget if remaining_seconds is None else min(pipeline_budget, remaining_seconds)
        self._deadline = time.monotonic() + max(0.0, effective_budget)
        q0 = query.strip()
        if len(q0) < 2 or len(q0) > 500 or not 1 <= top_k <= 8:
            return {"error": {"code": "INVALID_ARGUMENT", "message": "rag_search 参数无效"}}

        try:
            self._check_budget("start")
            # V7 executes the already validated taskText exactly once.  Facets
            # remain accepted for old callers but are not another query plan.
            result = self._search_single(
                q0=q0,
                top_k=top_k,
                site=site,
                protected_terms=protected_terms,
            )
            result.setdefault("diagnostics", {})["deprecatedFacetsIgnored"] = bool(facets)
            return result
        except RagBudgetExceeded as exc:
            return {"error": {"code": "DEADLINE_EXCEEDED", "message": str(exc)}}
        finally:
            self._deadline = None

    def _search_single(
        self,
        *,
        q0: str,
        top_k: int,
        site: str | None,
        protected_terms: set[str] | None,
    ) -> dict[str, Any]:
        del protected_terms
        query_text = q0

        self._check_budget("retrieval")
        first = self.knowledge.search_candidates(
            query=query_text,
            per_list_k=int(getattr(self.settings, "rag_per_list_candidate_k", 40)),
            site=site,
        )
        if _both_channels_failed(first):
            return {"error": {"code": "RAG_UNAVAILABLE", "message": "本地召回通道均不可用"}}

        candidates = _candidate_map(first)
        fused = _fused_ids(
            first,
            candidates,
            [(first.bm25_ranking, 1.0), (first.vector_ranking, 1.0)],
            int(getattr(self.settings, "rag_fused_candidate_limit", 24)),
        )
        self._check_budget("rerank")
        rerank = self._rerank(q0, [], fused, candidates)
        diagnostics = {
            **copy.deepcopy(first.diagnostics),
            "pipelineVersion": "context-workitems-v7",
            "queryMode": "validated-task-text",
            "rewriteDegraded": False,
            "facetRewriteDegradedCount": 0,
            "rerankDegraded": rerank is None,
            "stages": ["bm25", "chroma", "rrf", "relevance_reranker"],
        }
        diagnostics.update(_rerank_diagnostics(rerank))
        diagnostics.update(self._rerank_failure)
        selected_ids, evidence_coverage = _select_with_facet_coverage(
            rerank=rerank,
            facets=[],
            fallback_ids=fused,
            top_k=top_k,
        )
        diagnostics.update(_coverage_diagnostics(evidence_coverage))
        return self._build_response(
            q0=q0,
            facets=[],
            executed_queries=[query_text],
            selected_ids=selected_ids,
            candidates=candidates,
            diagnostics=diagnostics,
            rewritten_query=None,
            evidence_coverage=evidence_coverage,
        )

    def _search_with_facets(
        self,
        *,
        q0: str,
        facets: list[str],
        top_k: int,
        site: str | None,
        protected_terms: set[str] | None,
    ) -> dict[str, Any]:
        # q0 is deliberately kept unrewritten as an anchor against query drift.
        retrieval_queries: list[tuple[str, float, str]] = [(q0, 1.0, "ORIGINAL")]
        rewrite_degraded = 0
        global_protected = set(protected_terms or ())

        for index, facet in enumerate(facets, start=1):
            self._check_budget("facet_rewrite")
            rewritten = self._rewrite_facet(q0, facet, site)
            protected = global_protected | _protected_from_query(facet)
            failed = not rewritten or not rewritten.strip() or len(rewritten.strip()) > 500
            if rewritten and any(_normalize(term) not in _normalize(rewritten) for term in protected):
                failed = True
            if failed:
                rewrite_degraded += 1
                query_text = facet
            else:
                query_text = rewritten.strip()
            retrieval_queries.append((query_text, 0.85, f"FACET_{index}"))

        retrieval_queries = _dedupe_weighted_queries(retrieval_queries)
        search_results: list[tuple[CandidateSearchResult, float, str, str]] = []
        all_candidates: dict[str, KnowledgeSearchResult] = {}
        all_lists_failed = True

        # Correctness-first implementation: KnowledgeService owns a SQLAlchemy
        # Session, so we intentionally avoid sharing it across worker threads.
        # The query fan-out is logically parallel and can later be batched inside
        # KnowledgeService with one eligible-chunk snapshot / async vector calls.
        per_list_k = int(getattr(self.settings, "rag_per_list_candidate_k", 40))
        for query_text, weight, source in retrieval_queries:
            self._check_budget("retrieval")
            result = self.knowledge.search_candidates(
                query=query_text,
                per_list_k=per_list_k,
                site=site,
            )
            search_results.append((result, weight, source, query_text))
            if not _both_channels_failed(result):
                all_lists_failed = False
            for evidence_id, item in _candidate_map(result).items():
                all_candidates.setdefault(evidence_id, item)

        if all_lists_failed:
            return {"error": {"code": "RAG_UNAVAILABLE", "message": "本地召回通道均不可用"}}

        fused = _fused_ids_multi(
            search_results,
            all_candidates,
            limit=int(getattr(self.settings, "rag_fused_candidate_limit", 24)),
            reserve_per_query=int(getattr(self.settings, "rag_per_query_min_candidates", 3)),
        )
        self._check_budget("rerank")
        rerank = self._rerank(q0, facets, fused, all_candidates)
        diagnostics = _merge_multi_diagnostics(search_results)
        diagnostics.update({
            "pipelineVersion": "traditional-v6-soft-coverage",
            "queryMode": "upstream-facets",
            "rewriteDegraded": bool(rewrite_degraded),
            "facetRewriteDegradedCount": rewrite_degraded,
            "facetCount": len(facets),
            "retrievalQueryCount": len(retrieval_queries),
            "rerankDegraded": rerank is None,
            "stages": ["facet_rewrite", "multi_query", "bm25", "chroma", "weighted_rrf", "dedup", "facet_aware_reranker", "soft_coverage_selection", "coverage_check"],
        })
        diagnostics.update(_rerank_diagnostics(rerank))
        diagnostics.update(self._rerank_failure)
        selected_ids, evidence_coverage = _select_with_facet_coverage(
            rerank=rerank,
            facets=facets,
            fallback_ids=fused,
            top_k=top_k,
        )
        diagnostics.update(_coverage_diagnostics(evidence_coverage))
        return self._build_response(
            q0=q0,
            facets=facets,
            executed_queries=[item[0] for item in retrieval_queries],
            selected_ids=selected_ids,
            candidates=all_candidates,
            diagnostics=diagnostics,
            rewritten_query=None,
            evidence_coverage=evidence_coverage,
        )

    def _build_response(
        self,
        *,
        q0: str,
        facets: list[str],
        executed_queries: list[str],
        selected_ids: list[str],
        candidates: dict[str, KnowledgeSearchResult],
        diagnostics: dict[str, Any],
        rewritten_query: str | None,
        evidence_coverage: dict[str, Any],
    ) -> dict[str, Any]:
        items = []
        for key in selected_ids:
            child = candidates[key]
            item = _result_item(child)
            assessment = next((row for row in diagnostics.get("rerankCandidateFacetMatches", [])
                               if row.get("evidenceId") == key), None)
            if assessment is not None:
                item["supportAssessment"] = dict(assessment)
            if hasattr(self.knowledge, "expand_context"):
                context = self.knowledge.expand_context(child)
                item["parentContent"] = context.content if context.content != child.content else None
            items.append(item)
        response = {
            "status": "OK" if items else "EMPTY",
            "qualityStatus": "RERANK_DEGRADED" if diagnostics.get("rerankDegraded") else "ASSESSED",
            "originalQuery": q0,
            "evidenceFacets": list(facets),
            "executedQueries": executed_queries,
            "rounds": 1,
            "items": items,
            "evidenceCoverage": evidence_coverage,
            "diagnostics": diagnostics,
        }
        if rewritten_query is not None:
            response["rewrittenQuery"] = rewritten_query
        return response

    def _rerank(
        self,
        original_query: str,
        facets: list[str],
        evidence_ids: list[str],
        candidates: dict[str, KnowledgeSearchResult],
    ) -> RerankOutput | None:
        self._rerank_failure = {}
        if not evidence_ids:
            return RerankOutput(ranked=[], assessments=[])

        del facets
        payload = {
            "taskText": original_query,
            "candidates": [_candidate_prompt(candidates[item]) for item in evidence_ids],
        }
        try:
            completion = self.client.complete_structured(
                [
                    AiMessage(
                        role="system",
                        content=(
                            "你是证据相关性重排器，只判断每段 candidate 与 taskText 的相关程度，不回答问题，"
                            "也不判断整体证据充分性。每个给定 evidenceId 恰好输出一次，relevanceLevel 只能为"
                            " HIGH、MEDIUM、LOW、NONE；不得生成新 ID、解释、答案或政策结论。"
                        ),
                    ),
                    AiMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
                ],
                response_model=relevance_response_model(evidence_ids),
                schema_name="rag_relevance_rerank_v7",
                options=StructuredCompletionOptions(
                    temperature=float(getattr(self.settings, "rag_rerank_temperature", 0.0)),
                    max_tokens=int(getattr(
                        self.settings,
                        "rag_rerank_max_tokens",
                        max(2048, int(getattr(self.settings, "rag_model_max_tokens", 1024))),
                    )),
                    repair_attempts=0,
                    timeout_seconds=self._stage_timeout("rerank", "rag_rerank_timeout_seconds", 40.0),
                ),
            )
        except Exception as exc:
            self._rerank_failure = {
                "rerankErrorCode": str(getattr(exc, "code", None) or type(exc).__name__),
                "rerankErrorStage": "STRUCTURED_OUTPUT",
                "rerankValidationErrors": list(getattr(exc, "validation_errors", ()))[:5],
            }
            return None

        allowed_ids = set(evidence_ids)
        assessments = completion.value.assessments
        assessment_ids = [item.evidenceId for item in assessments]
        if len(assessment_ids) != len(set(assessment_ids)) or set(assessment_ids) != allowed_ids:
            self._rerank_failure = {"rerankErrorCode": "RERANK_INCOMPLETE_ASSESSMENTS", "rerankErrorStage": "SEMANTIC_VALIDATION",
                                    "rerankMissingIds": sorted(allowed_ids - set(assessment_ids)),
                                    "rerankDuplicateIds": sorted({item for item in assessment_ids if assessment_ids.count(item) > 1})}
            return None

        priority = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "NONE": 3}
        position = {value: index for index, value in enumerate(evidence_ids)}
        ranked = [item.evidenceId for item in sorted(assessments, key=lambda item: (priority[item.relevanceLevel], position[item.evidenceId]))]
        compatible = [RerankCandidateAssessment(
            evidenceId=item.evidenceId, relevanceLevel=item.relevanceLevel,
            matchedFacetIds=[], directSupport=item.relevanceLevel in {"HIGH", "MEDIUM"},
        ) for item in assessments]
        return RerankOutput(ranked=ranked, assessments=compatible)

    def _rewrite(self, query: str, site: str | None) -> str | None:
        try:
            completion = self.client.complete_structured(
                [
                    AiMessage(
                        role="system",
                        content=(
                            "将用户问题改写为适合学生手册检索的单个查询，补全表达、突出关键词，不输出答案或备选查询。"
                            "query 必须是正常可读文本，保留原查询中的实体、时间、数字和否定条件；问题清晰时可原样返回。"
                            "禁止 URL、联网搜索意图、候选查询列表和用户未提供的新身份或敏感事实。"
                        ),
                    ),
                    AiMessage(role="user", content=json.dumps({
                        "originalQuery": query,
                        "site": site or "ALL",
                    }, ensure_ascii=False)),
                ],
                response_model=RewriteOutput,
                schema_name="rag_rewrite_v1",
                options=StructuredCompletionOptions(
                    temperature=float(getattr(self.settings, "rag_rewrite_temperature", 0.1)),
                    max_tokens=int(getattr(self.settings, "rag_model_max_tokens", 1024)),
                    repair_attempts=0,
                    timeout_seconds=self._stage_timeout("rewrite", "rag_rewrite_timeout_seconds", 6.0),
                ),
            )
            return completion.value.query
        except Exception:
            return None

    def _rewrite_facet(self, original_query: str, facet: str, site: str | None) -> str | None:
        try:
            completion = self.client.complete_structured(
                [
                    AiMessage(
                        role="system",
                        content=(
                            "你是学生手册 Retrieval Query Rewriter。业务 WorkItem 和 evidenceFacet 已由上游确定，不得重新拆分业务问题。"
                            "只把当前 evidenceFacet 改写成一个适合 BM25 和向量检索的查询：补充规范同义表达和制度术语，保留与该 facet 相关的实体、身份、时间、数字、否定和限制条件。"
                            "不得回答问题，不得生成多个查询，不得添加用户未提供的新事实、条号、日期、身份或结论。"
                        ),
                    ),
                    AiMessage(role="user", content=json.dumps({
                        "originalQuery": original_query,
                        "evidenceFacet": facet,
                        "site": site or "ALL",
                    }, ensure_ascii=False)),
                ],
                response_model=RewriteOutput,
                schema_name="rag_facet_rewrite_v1",
                options=StructuredCompletionOptions(
                    temperature=float(getattr(self.settings, "rag_rewrite_temperature", 0.1)),
                    max_tokens=int(getattr(self.settings, "rag_model_max_tokens", 1024)),
                    repair_attempts=0,
                    timeout_seconds=self._stage_timeout("facet_rewrite", "rag_rewrite_timeout_seconds", 6.0),
                ),
            )
            return completion.value.query
        except Exception:
            return None

    def _check_budget(self, stage: str) -> None:
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise RagBudgetExceeded(f"RAG 阶段 {stage} 超过总截止时间")

    def _stage_timeout(self, stage: str, setting_name: str, default: float) -> float:
        self._check_budget(stage)
        configured = float(getattr(self.settings, setting_name, default))
        if self._deadline is None:
            return configured
        return max(0.01, min(configured, self._deadline - time.monotonic()))


class RagBudgetExceeded(RuntimeError):
    pass


def _normalize_facets(facets: list[str] | tuple[str, ...] | None) -> list[str]:
    values: list[str] = []
    for item in facets or ():
        if not isinstance(item, str):
            continue
        item = item.strip()
        if not item or len(item) > 180 or item in values:
            continue
        values.append(item)
        if len(values) >= 3:
            break
    return values


def _dedupe_weighted_queries(values: list[tuple[str, float, str]]) -> list[tuple[str, float, str]]:
    by_normalized: dict[str, tuple[str, float, str]] = {}
    order: list[str] = []
    for text, weight, source in values:
        text = text.strip()
        if not text:
            continue
        key = _normalize(text)
        if not key:
            continue
        if key not in by_normalized:
            order.append(key)
            by_normalized[key] = (text, weight, source)
        elif weight > by_normalized[key][1]:
            by_normalized[key] = (text, weight, source)
    return [by_normalized[key] for key in order]


def _candidate_map(result: CandidateSearchResult) -> dict[str, KnowledgeSearchResult]:
    return {_evidence_id(item): item for item in result.candidates}


def _fused_ids(
    first: CandidateSearchResult,
    candidates: dict[str, KnowledgeSearchResult],
    rankings: list[tuple[tuple[KnowledgeSearchResult, ...], float]],
    limit: int,
) -> list[str]:
    id_rankings = [([_evidence_id(item) for item in values], weight) for values, weight in rankings]
    scores = weighted_rrf(id_rankings)
    q0_ranks: dict[str, int] = {}
    for values in (first.bm25_ranking, first.vector_ranking):
        for rank, item in enumerate(values, 1):
            evidence_id = _evidence_id(item)
            q0_ranks[evidence_id] = min(q0_ranks.get(evidence_id, rank), rank)
    return sorted(
        (evidence_id for evidence_id in scores if evidence_id in candidates),
        key=lambda evidence_id: (-scores[evidence_id], q0_ranks.get(evidence_id, 10**9), evidence_id),
    )[:limit]


def _fused_ids_multi(
    search_results: list[tuple[CandidateSearchResult, float, str, str]],
    candidates: dict[str, KnowledgeSearchResult],
    *,
    limit: int,
    reserve_per_query: int,
) -> list[str]:
    rankings: list[tuple[list[str], float]] = []
    anchor_ranks: dict[str, int] = {}
    reserved: list[str] = []

    for result_index, (result, weight, _source, _query) in enumerate(search_results):
        bm25_ids = [_evidence_id(item) for item in result.bm25_ranking]
        vector_ids = [_evidence_id(item) for item in result.vector_ranking]
        rankings.extend(((bm25_ids, weight), (vector_ids, weight)))
        local_scores = weighted_rrf([(bm25_ids, 1.0), (vector_ids, 1.0)])
        local_order = sorted(
            (item for item in local_scores if item in candidates),
            key=lambda item: (-local_scores[item], item),
        )
        for item in local_order[:max(0, reserve_per_query)]:
            if item not in reserved:
                reserved.append(item)
        if result_index == 0:
            for values in (result.bm25_ranking, result.vector_ranking):
                for rank, item in enumerate(values, 1):
                    evidence_id = _evidence_id(item)
                    anchor_ranks[evidence_id] = min(anchor_ranks.get(evidence_id, rank), rank)

    scores = weighted_rrf(rankings)
    global_order = sorted(
        (evidence_id for evidence_id in scores if evidence_id in candidates),
        key=lambda evidence_id: (-scores[evidence_id], anchor_ranks.get(evidence_id, 10**9), evidence_id),
    )
    # Keep a small quota from every evidence facet so one dominant facet cannot
    # crowd the others out before reranking; then fill by global weighted RRF.
    merged: list[str] = []
    for evidence_id in (*reserved, *global_order):
        if evidence_id not in merged:
            merged.append(evidence_id)
        if len(merged) >= limit:
            break
    return merged


def _merge_multi_diagnostics(
    search_results: list[tuple[CandidateSearchResult, float, str, str]],
) -> dict[str, Any]:
    if not search_results:
        return {}
    first = copy.deepcopy(search_results[0][0].diagnostics)
    first["retrievalMode"] = "multi-query-hybrid"
    first["bm25Degraded"] = all(bool(item[0].diagnostics.get("bm25Degraded")) for item in search_results)
    first["vectorDegraded"] = all(bool(item[0].diagnostics.get("vectorDegraded")) for item in search_results)
    first["bm25CandidateCount"] = sum(int(item[0].diagnostics.get("bm25CandidateCount") or 0) for item in search_results)
    first["vectorCandidateCount"] = sum(int(item[0].diagnostics.get("vectorCandidateCount") or 0) for item in search_results)
    first["queryDiagnostics"] = [
        {
            "source": source,
            "query": query,
            "weight": weight,
            "bm25Degraded": bool(result.diagnostics.get("bm25Degraded")),
            "vectorDegraded": bool(result.diagnostics.get("vectorDegraded")),
            "bm25CandidateCount": int(result.diagnostics.get("bm25CandidateCount") or len(result.bm25_ranking)),
            "vectorCandidateCount": int(result.diagnostics.get("vectorCandidateCount") or len(result.vector_ranking)),
        }
        for result, weight, source, query in search_results
    ]
    return first


def _both_channels_failed(result: CandidateSearchResult) -> bool:
    return bool(result.diagnostics.get("bm25Degraded")) and bool(result.diagnostics.get("vectorDegraded"))


def _evidence_id(item: KnowledgeSearchResult) -> str:
    if item.chunk_id is not None:
        return f"ev_chunk_{item.chunk_id}"
    identity = f"{item.canonical_key}|{item.version}|{item.section_title}|{item.content}"
    return "ev_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


_RELEVANCE_PRIORITY = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "NONE": 0}


def _select_with_facet_coverage(
    *,
    rerank: RerankOutput | None,
    facets: list[str],
    fallback_ids: list[str],
    top_k: int,
) -> tuple[list[str], dict[str, Any]]:
    """Select final evidence using a soft facet-coverage constraint.

    The selector never manufactures evidence and never promotes LOW/NONE or
    indirect support merely to make coverage look complete.  With zero facets
    it is exactly relevance-only Top-K.  With one facet it preserves normal
    ranking whenever Top-K already contains a reliable supporting candidate;
    otherwise it may reserve one reliable candidate from the reranked pool.
    With multiple facets it greedily reserves reliable candidates that cover
    the largest number of still-uncovered facets, then fills the remaining
    slots by the rerank order.  Final output keeps the reranker's relative
    order so coverage does not arbitrarily reshuffle strong evidence.
    """
    if rerank is None:
        base_ranked = list(fallback_ids)
    else:
        supported_ids = {item.evidenceId for item in rerank.assessments
                         if item.directSupport and item.relevanceLevel in {"HIGH", "MEDIUM"}}
        base_ranked = [item for item in rerank.ranked if item in supported_ids]
    base_ranked = list(dict.fromkeys(base_ranked))
    top_k = max(0, int(top_k))
    facet_catalog = [
        {"facetId": f"f{index}", "text": facet}
        for index, facet in enumerate(facets, start=1)
    ]

    if not facets:
        selected = base_ranked[:top_k]
        return selected, {
            "status": "NOT_APPLICABLE",
            "selectionMode": "relevance-only",
            "selectionApplied": False,
            "coverageRatio": None,
            "coveredFacets": [],
            "uncoveredFacets": [],
            "unknownFacets": [],
            "facets": [],
        }

    if rerank is None:
        selected = base_ranked[:top_k]
        return selected, {
            "status": "UNKNOWN",
            "selectionMode": "rerank-degraded",
            "selectionApplied": False,
            "coverageRatio": None,
            "coveredFacets": [],
            "uncoveredFacets": [],
            "unknownFacets": [item["facetId"] for item in facet_catalog],
            "facets": [
                {**item, "status": "UNKNOWN", "evidenceIds": []}
                for item in facet_catalog
            ],
        }

    assessment_by_id = {item.evidenceId: item for item in rerank.assessments}
    rank_index = {evidence_id: index for index, evidence_id in enumerate(base_ranked)}
    allowed_facets = {item["facetId"] for item in facet_catalog}

    def reliable_matches(evidence_id: str) -> set[str]:
        assessment = assessment_by_id.get(evidence_id)
        if assessment is None or not assessment.directSupport:
            return set()
        if assessment.relevanceLevel not in {"HIGH", "MEDIUM"}:
            return set()
        return set(assessment.matchedFacetIds).intersection(allowed_facets)

    initial_top = base_ranked[:top_k]
    reserved: list[str] = []
    uncovered = set(allowed_facets)

    # If one facet is already represented in normal Top-K, preserve the
    # relevance-only ordering exactly.  Otherwise reserve at most one reliable
    # candidate so a single-fact query does not become a diversity problem.
    if len(facets) == 1:
        facet_id = facet_catalog[0]["facetId"]
        if not any(facet_id in reliable_matches(item) for item in initial_top):
            eligible = [item for item in base_ranked if facet_id in reliable_matches(item)]
            if eligible and top_k > 0:
                reserved.append(eligible[0])
    else:
        # Soft set-cover over reliable candidates. One evidence can satisfy
        # multiple facets and only consumes one Top-K slot.
        candidate_pool = [item for item in base_ranked if reliable_matches(item)]
        while uncovered and len(reserved) < top_k:
            eligible = [item for item in candidate_pool if item not in reserved and reliable_matches(item).intersection(uncovered)]
            if not eligible:
                break

            def reserve_key(evidence_id: str) -> tuple[int, int, int]:
                assessment = assessment_by_id[evidence_id]
                new_coverage = len(reliable_matches(evidence_id).intersection(uncovered))
                relevance = _RELEVANCE_PRIORITY.get(assessment.relevanceLevel, 0)
                return (new_coverage, relevance, -rank_index.get(evidence_id, 10**9))

            best = max(eligible, key=reserve_key)
            reserved.append(best)
            uncovered.difference_update(reliable_matches(best))

    selected_set = set(reserved)
    for evidence_id in base_ranked:
        if len(selected_set) >= top_k:
            break
        selected_set.add(evidence_id)

    selected = sorted(selected_set, key=lambda item: rank_index.get(item, 10**9))[:top_k]
    selected_set = set(selected)

    facet_rows: list[dict[str, Any]] = []
    covered_ids: list[str] = []
    uncovered_ids: list[str] = []
    for facet in facet_catalog:
        facet_id = facet["facetId"]
        evidence_ids = [
            evidence_id for evidence_id in selected
            if facet_id in reliable_matches(evidence_id)
        ]
        covered = bool(evidence_ids)
        (covered_ids if covered else uncovered_ids).append(facet_id)
        facet_rows.append({
            **facet,
            "status": "COVERED" if covered else "UNCOVERED",
            "evidenceIds": evidence_ids,
        })

    if len(covered_ids) == len(facet_catalog):
        status = "FULL"
    elif covered_ids:
        status = "PARTIAL"
    else:
        status = "NONE"

    return selected, {
        "status": status,
        "selectionMode": "single-facet-soft-coverage" if len(facets) == 1 else "multi-facet-soft-coverage",
        "selectionApplied": selected != initial_top,
        "coverageRatio": len(covered_ids) / len(facet_catalog),
        "coveredFacets": covered_ids,
        "uncoveredFacets": uncovered_ids,
        "unknownFacets": [],
        "facets": facet_rows,
    }


def _coverage_diagnostics(coverage: dict[str, Any]) -> dict[str, Any]:
    return {
        "coverageStatus": coverage.get("status"),
        "coverageSelectionMode": coverage.get("selectionMode"),
        "coverageSelectionApplied": bool(coverage.get("selectionApplied")),
        "coverageRatio": coverage.get("coverageRatio"),
        "coveredFacetCount": len(coverage.get("coveredFacets") or []),
        "uncoveredFacetCount": len(coverage.get("uncoveredFacets") or []),
        "unknownFacetCount": len(coverage.get("unknownFacets") or []),
    }


def _rerank_diagnostics(rerank: RerankOutput | None) -> dict[str, Any]:
    if rerank is None:
        return {
            "rerankAssessmentCount": 0,
            "rerankCandidateFacetMatches": [],
        }
    return {
        "rerankAssessmentCount": len(rerank.assessments),
        "rerankCandidateFacetMatches": [
            {
                "evidenceId": item.evidenceId,
                "relevanceLevel": item.relevanceLevel,
                "matchedFacetIds": list(item.matchedFacetIds),
                "directSupport": item.directSupport,
            }
            for item in rerank.assessments
        ],
    }


def _candidate_prompt(item: KnowledgeSearchResult) -> dict[str, Any]:
    return {
        "evidenceId": _evidence_id(item),
        "title": item.title,
        "section": item.section_title,
        "content": item.content[:2400],
        "source": item.source,
        "version": item.version,
        "site": item.site,
    }


def _result_item(item: KnowledgeSearchResult) -> dict[str, Any]:
    return {
        "evidenceId": _evidence_id(item),
        "contextId": _context_id(item),
        "chunkId": item.chunk_id,
        "parentChunkId": item.parent_chunk_id,
        "title": item.title,
        "content": item.content,
        "source": item.source,
        "sourceUrl": item.source_url,
        "version": item.version,
        "verifiedAt": item.verified_at.isoformat() if item.verified_at else None,
        "site": item.site,
        "pageNumber": item.page_number,
        "section": item.section_title,
    }


def _context_id(item: KnowledgeSearchResult) -> str:
    if item.chunk_id is not None:
        return f"knowledge:{item.chunk_id}"
    identity = "|".join(str(value or "") for value in (
        item.canonical_key,
        item.source_key,
        item.version,
        item.section_title,
        item.content,
    ))
    return "knowledge-hash:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _normalize(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z一-鿿]+", "", value).lower()


def _protected_from_query(query: str) -> set[str]:
    values = set(re.findall(r"20\d{2}(?:[-年/]\d{1,2})?(?:[-月/]\d{1,2})?|\d+(?:\.\d+)?|不|非|无", query))
    return {value for value in values if value}


def get_rag_pipeline() -> RagPipeline:
    from app.core.config import get_settings
    from app.core.database import SessionLocal

    settings = get_settings()
    db = SessionLocal()
    model_settings = copy.copy(settings)
    model_settings.ai_provider = settings.rag_model_provider
    model_settings.ollama_model = settings.rag_model
    model_settings.ai_think = False
    pipeline = RagPipeline(
        knowledge=KnowledgeService(db, settings),
        client=AiClient(model_settings, purpose_namespace="rag"),
        settings=settings,
    )
    pipeline._owned_db = db
    return pipeline


def close_rag_pipeline(pipeline: RagPipeline) -> None:
    db = getattr(pipeline, "_owned_db", None)
    if db is not None:
        db.close()
