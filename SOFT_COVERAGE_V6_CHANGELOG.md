# V6 Soft Coverage Changelog

- RAG pipeline version: `traditional-v6-soft-coverage`.
- Added adaptive soft facet coverage selector after facet-aware reranking.
- 0 facet: relevance-only TopK.
- 1 facet: preserve TopK if covered; otherwise reserve at most one reliable HIGH/MEDIUM direct-support candidate.
- 2-3 facets: greedy reliable set-cover, then relevance fill; preserve reranker relative order.
- LOW/NONE or indirect evidence is never promoted for coverage.
- Added top-level `evidenceCoverage` and coverage diagnostics.
- Rerank degradation returns `UNKNOWN` coverage instead of pretending facets are uncovered.
- Added unit/regression tests and frozen-200 benchmark artifacts under `benchmarks/soft_coverage_20260913/`.
