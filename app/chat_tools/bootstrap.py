from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass


logger = logging.getLogger("mindbridge.chat_tools.bootstrap")


@dataclass(frozen=True)
class BootstrapReport:
    ready: bool
    stage: str
    duration_ms: int
    active_collection: str | None = None
    index_signature: str | None = None
    vector_count: int | None = None
    error_code: str | None = None


_report: BootstrapReport | None = None


def bootstrap_readonly_dependencies(*, strict: bool | None = None) -> BootstrapReport:
    """Preload native dependencies and verify the ACTIVE vector index before stdio starts."""
    global _report
    if _report is not None:
        return _report
    started = time.perf_counter()
    stage = "DEPENDENCY_IMPORT"
    strict = _env_bool("CHAT_TOOLS_STRICT_STARTUP", False) if strict is None else strict
    try:
        # These imports deliberately happen on the process main thread before
        # FastMCP starts its event loop.  Importing them lazily in rag_search
        # reproduced the Windows native-extension deadlock described by PR-1.
        import numpy  # noqa: F401
        import chromadb

        stage = "ACTIVE_INDEX_CHECK"
        from app.core.config import get_settings
        from app.core.database import SessionLocal
        from app.models.entities import KnowledgeIndexRegistry

        settings = get_settings()
        db = SessionLocal()
        try:
            registry = db.query(KnowledgeIndexRegistry).filter(
                KnowledgeIndexRegistry.logical_name == settings.knowledge_vector_collection_base
            ).one_or_none()
            if registry is None or not registry.active_collection or not registry.active_signature:
                raise RuntimeError("ACTIVE collection/signature 不可用")
            active_collection = registry.active_collection
            index_signature = registry.active_signature
        finally:
            db.close()

        client = chromadb.PersistentClient(path=settings.chroma_persist_dir)
        vector_count = int(client.get_collection(active_collection).count())
        if vector_count <= 0:
            raise RuntimeError("ACTIVE collection 为空")
        _report = BootstrapReport(
            ready=True,
            stage="READY",
            duration_ms=_elapsed_ms(started),
            active_collection=active_collection,
            index_signature=index_signature,
            vector_count=vector_count,
        )
        logger.warning(
            "CHAT_TOOLS_READY duration_ms=%s collection=%s vector_count=%s",
            _report.duration_ms,
            active_collection,
            vector_count,
        )
        return _report
    except Exception as exc:
        code = "DEPENDENCY_INIT_FAILED" if stage == "DEPENDENCY_IMPORT" else "ACTIVE_INDEX_UNAVAILABLE"
        _report = BootstrapReport(
            ready=False,
            stage=stage,
            duration_ms=_elapsed_ms(started),
            error_code=code,
        )
        logger.exception("CHAT_TOOLS_NOT_READY stage=%s code=%s", stage, code)
        if strict:
            raise RuntimeError(f"{code}: {type(exc).__name__}: {exc}") from exc
        return _report


def bootstrap_report() -> BootstrapReport | None:
    return _report


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
