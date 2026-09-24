from pydantic import SecretStr

from app.evaluation.config import EvaluationSettings
from app.evaluation.ragas_eval.factories import create_factories


def test_ragas_judge_budget_is_independent_from_business_judge_budget(tmp_path):
    settings = EvaluationSettings(
        _env_file=None,
        judge_api_key=SecretStr("test-only"),
        judge_max_tokens=1200,
        ragas_judge_max_tokens=8192,
        ragas_cache_dir=tmp_path,
    )

    factories = create_factories(settings)

    assert settings.judge_max_tokens == 1200
    assert settings.ragas_judge_max_tokens == 8192
    assert factories.llm.model_args["max_tokens"] == 8192
