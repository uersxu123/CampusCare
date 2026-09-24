from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.evaluation.contracts import EndToEndCase, EvaluationRuntimeOutcome
from app.evaluation.runtime.adapter import EvaluationRuntimeAdapter
from app.services.process_supervisor import ProcessSupervisor, SupervisedProcessResult


class EvaluationCaseSupervisor:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        cancel_grace_seconds: float,
        cleanup_timeout_seconds: float,
    ):
        self.process = ProcessSupervisor(
            timeout_seconds=timeout_seconds,
            cancel_grace_seconds=cancel_grace_seconds,
            cleanup_timeout_seconds=cleanup_timeout_seconds,
        )

    def run_case(self, case: EndToEndCase, adapter: EvaluationRuntimeAdapter) -> tuple[list[EvaluationRuntimeOutcome], SupervisedProcessResult]:
        spec = adapter.worker_spec()
        payload = {"case": case.model_dump(mode="json"), "adapter": spec}
        result = self.process.run(_evaluation_case_worker, payload, generation_id=f"case:{case.id}")
        if result.status == "COMPLETED":
            return [EvaluationRuntimeOutcome(**item) for item in result.value], result
        return [_failed_outcome(case, result)], result


def _evaluation_case_worker(payload: dict[str, Any], cancel_event) -> list[dict[str, Any]]:
    if cancel_event.is_set():
        raise RuntimeError("CASE_CANCELLED_BEFORE_START")
    spec = payload["adapter"]
    settings = Settings.model_validate(spec["settings"])
    source_db = None
    engine = None
    if spec.get("useSourceDatabase"):
        engine = create_engine(settings.database_url)
        source_db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        adapter = EvaluationRuntimeAdapter(
            settings,
            source_db,
            require_hybrid_retrieval=bool(spec.get("requireHybridRetrieval")),
        )
        case = EndToEndCase.model_validate(payload["case"])
        outcomes = asyncio.run(adapter.run_case(case))
        return [item.__dict__ for item in outcomes]
    finally:
        if source_db is not None:
            source_db.close()
        if engine is not None:
            engine.dispose()


def _failed_outcome(case: EndToEndCase, result: SupervisedProcessResult) -> EvaluationRuntimeOutcome:
    return EvaluationRuntimeOutcome(
        case_id=case.id,
        turn_index=0,
        response="",
        route={},
        risk_level="LOW",
        action="UNKNOWN",
        knowledge_requested=False,
        knowledge_used=False,
        retrieved_context_ids=[],
        retrieved_contexts=[],
        usable_context_ids=[],
        usable_contexts=[],
        trace_id="",
        turn_metrics={
            "supervision": {
                "status": result.status,
                "elapsedSeconds": result.elapsed_seconds,
                "workerPid": result.worker_pid,
                "resourceCleanup": result.resource_cleanup,
                "jobAssigned": result.job_assigned,
                "providerCancellation": result.provider_cancellation,
            }
        },
        warnings=["STACK_CAPTURE_UNAVAILABLE"],
        error_code=result.error_code or result.status,
        infra_error_codes=[result.error_code or result.status],
        business_status="FAILED",
        upstream_error_codes=[result.error_code or result.status],
    )
