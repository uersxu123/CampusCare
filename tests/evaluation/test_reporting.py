from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
import pytest

from app.core.config import get_settings
from app.evaluation.reporting.fingerprint import (
    compare_corpus_fingerprints,
    fingerprint_active_corpus,
    fingerprint_from_audit,
)
from app.evaluation.reporting.baseline import load_baseline, update_baseline
from app.evaluation.runner import _baseline_identity


ROOT = Path(__file__).resolve().parents[2]


def test_current_smoke_fingerprint_matches_its_source_database():
    golden = fingerprint_from_audit(
        ROOT / "app/evaluation/datasets/e2e-smoke-current-v2/corpus-audit.json"
    )
    if golden.relational_database_type == "mysql":
        engine = create_engine(get_settings().database_url)
    else:
        database = ROOT / "target/harness/mindbridge-harness.sqlite3"
        if not database.is_file():
            pytest.skip("先运行 Engineering Harness 生成隔离语料快照")
        engine = create_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    try:
        with Session(engine) as db:
            runtime = fingerprint_active_corpus(
                db,
                manifest_path=ROOT / "app/knowledge/knowledge_manifest.yaml",
            )
    except SQLAlchemyError as exc:
        pytest.skip(f"Golden 声明的运行数据库不可用: {type(exc).__name__}")
    finally:
        engine.dispose()
    assert compare_corpus_fingerprints(golden, runtime)["status"] == "MATCH"


def test_historical_181_case_audit_is_preserved_separately():
    historical = fingerprint_from_audit(
        ROOT / "app/evaluation/datasets/mindbridge-e2e-ragas-v1.audit.json"
    )
    current = fingerprint_from_audit(
        ROOT / "app/evaluation/datasets/e2e-smoke-current-v2/corpus-audit.json"
    )
    assert historical.corpus_hash
    assert current.corpus_hash
    assert historical.corpus_hash != current.corpus_hash


def test_baseline_identity_ignores_paths_and_generated_timestamps():
    base = {
        "suite": "release",
        "profile": "full",
        "dataset": {"path": "first.jsonl", "sha256": "dataset-hash", "caseCount": 181},
        "corpus": {
            "runtimeCorpusFingerprint": {"corpus_hash": "corpus-hash", "generated_at": "first"},
        },
        "sut": {"models": {"default": "sut-model"}},
        "judge": {"model": "judge-model"},
    }
    changed = {
        **base,
        "dataset": {**base["dataset"], "path": "second.jsonl"},
        "corpus": {
            "runtimeCorpusFingerprint": {"corpus_hash": "corpus-hash", "generated_at": "second"},
        },
    }
    assert _baseline_identity(base) == _baseline_identity(changed)


def test_incompatible_baseline_is_stale_and_failed_results_cannot_replace_it(tmp_path):
    identity = {"suite": "release", "profile": "full", "dataset": {"sha256": "old"}}
    update_baseline(tmp_path, identity, {"passed": True, "metrics": {}})
    changed = {**identity, "dataset": {"sha256": "new"}}
    assert load_baseline(tmp_path, changed)["status"] == "STALE"
    with pytest.raises(ValueError, match="不能更新"):
        update_baseline(tmp_path, changed, {"passed": False})
