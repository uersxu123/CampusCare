import pytest

from app.evaluation.ragas_eval.factories import RagasDependencyError, require_ragas


def test_ragas_dependency_is_exact_or_fails_with_clear_error():
    try:
        version = require_ragas()
    except RagasDependencyError as exc:
        assert "requirements-evaluation.txt" in str(exc) or "版本不兼容" in str(exc)
    else:
        assert version == "0.4.3"
