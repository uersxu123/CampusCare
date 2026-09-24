# MindBridge Routing V5.4 Implementation Notes

Implemented against `MIND_BRIDGE_PRIMARY_INTENT_ROUTING_DEGRADED_BROADCAST_REFACTOR_GUIDE(1).md`.

## Main changes

- Added minimal V5 planner contract: `PlanningResultV5 -> workItems[{sourceText,intent,dependsOn}]`.
- Planner prompt now uses `contextView + currentInput`; history is understanding-only context.
- Normal path builds RoutePlan V5 directly in Python and does not run Rule/Embedding/IntentFusion a second time.
- Added minimal planner validation and one retry (two planner attempts total).
- Added Global Degraded router with four primary intent score maps, Rule + Embedding fusion, deterministic 0/1/N selection, and Rule-only fallback.
- Added `NORMAL / DEGRADED_DIRECT / DEGRADED_BROADCAST`, `degraded`, and `degradedReason` to RoutePlan V5.
- Kept RoutePlan V3/V4 payload reading compatibility.
- V5 Router no longer creates RISK work items; Safety remains independent and HIGH risk can trigger response flow without waiting for RoutePlan.
- V5 planner no longer creates `evidenceFacets`; runtime WorkItem keeps the compatibility field as an empty array/tuple.
- Specialist tasks carry an exact `routePlanArtifactId`; specialists read that exact RoutePlan.
- Degraded broadcast specialists perform a domain scope filter and can emit `OUT_OF_SCOPE_SKIPPED`; ResponseAgent filters skipped results.
- 0-intent degraded routing and work-item-limit control are program-level fixed responses, not fake specialist work items.
- Added V5 routing and runtime tests.

## Intentionally not changed

- Existing RAG ownership and `KnowledgeQueryPlanner`.
- `rag_pipeline` multi-query/fusion/rerank/soft-coverage behavior.
- Physical knowledge-base layout.
- A new clarification/coreference agent or second-level intent system.
- Global removal of legacy V3 classes; they remain for compatibility while V5 is the new main path.

## Test notes

- `tests/test_routing_v5.py` + `tests/test_routing_v5_runtime.py`: 12 passed.
- Selected RAG/Knowledge/Safety/Response regressions: 69 passed; 3 legacy Specialist tests still construct tasks without the now-required `routePlanArtifactId` and therefore fail by design under the V5 contract.
- Full-suite collection in this execution environment is blocked by the missing optional `mcp` Python package; no source change was made to work around that environment dependency.
