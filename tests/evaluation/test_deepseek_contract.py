from pydantic import SecretStr

from app.core.config import Settings
from app.evaluation.config import EvaluationSettings
from app.evaluation.judges.deepseek import DeepSeekJudge
from app.evaluation.contracts import EndToEndCase, EvaluationRuntimeOutcome
from app.evaluation.judges.business import BusinessJudgeOutput, business_judge_messages
from app.evaluation.judges.deepseek import BusinessJudgeResult
from app.services.ai import StructuredCompletionError


def _judge_output(score: float, verdict: str = "PASS") -> BusinessJudgeOutput:
    return BusinessJudgeOutput.model_validate({
        "observed_action": "ANSWER",
        "relevance": score,
        "accuracy": score,
        "completeness": score,
        "helpfulness": score,
        "action_correctness": score,
        "verdict": verdict,
        "reasons": [],
        "unsupported_claims": [],
    })


def _case_and_outcome():
    case = EndToEndCase.model_validate({
        "id": "judge-contract",
        "turns": ["问题"],
        "expected_action": "ANSWER",
        "reference": "参考答案",
    })
    outcome = EvaluationRuntimeOutcome(
        case_id=case.id,
        turn_index=0,
        response="真实回答",
        route={},
        risk_level="LOW",
        action="ANSWER",
        knowledge_requested=False,
        knowledge_used=False,
        retrieved_context_ids=[],
        retrieved_contexts=[],
        usable_context_ids=[],
        usable_contexts=[],
        trace_id="trace",
        turn_metrics={},
    )
    return case, outcome


def test_deepseek_url_and_settings_are_isolated():
    app = Settings(_env_file=None, ai_provider="mock", openai_base_url="https://example.invalid/v1")
    evaluation = EvaluationSettings(
        _env_file=None,
        judge_base_url="https://api.deepseek.com/",
        judge_api_key=SecretStr("test-only"),
    )
    judge = DeepSeekJudge(app, evaluation)
    assert judge.client.settings.openai_base_url == "https://api.deepseek.com"
    assert judge.client.settings.openai_model == "deepseek-v4-flash"
    assert app.ai_provider == "mock"
    assert app.openai_base_url == "https://example.invalid/v1"


def test_repeated_judging_uses_median_and_any_failed_verdict_fails(monkeypatch):
    app = Settings(_env_file=None, ai_provider="mock")
    evaluation = EvaluationSettings(
        _env_file=None,
        judge_api_key=SecretStr("test-only"),
        judge_repetitions=2,
    )
    judge = DeepSeekJudge(app, evaluation)
    results = [
        BusinessJudgeResult(_judge_output(0.2), "json_schema", 0, evaluation.judge_model, "business-judge-v1"),
        BusinessJudgeResult(_judge_output(0.8, "FAIL"), "json_schema", 1, evaluation.judge_model, "business-judge-v1"),
    ]
    monkeypatch.setattr(judge, "_cached_judge", lambda messages, options, repetition: results[repetition])
    case, outcome = _case_and_outcome()
    judged = judge.judge(case, outcome)
    assert judged.output.relevance == 0.5
    assert judged.output.verdict == "FAIL"
    assert judged.repair_count == 1


def test_successful_judge_result_is_cached_as_utf8_json(tmp_path, monkeypatch):
    app = Settings(_env_file=None, ai_provider="mock")
    evaluation = EvaluationSettings(
        _env_file=None,
        output_dir=tmp_path,
        judge_api_key=SecretStr("test-only"),
    )
    judge = DeepSeekJudge(app, evaluation)
    calls = 0

    def fake_judge(messages, options):
        nonlocal calls
        calls += 1
        return BusinessJudgeResult(
            _judge_output(1.0), "json_schema", 0, evaluation.judge_model, "business-judge-v1"
        )

    monkeypatch.setattr(judge, "_judge_with_retries", fake_judge)
    case, outcome = _case_and_outcome()
    messages = business_judge_messages(case, outcome)
    options = judge.client.settings
    first = judge._cached_judge(messages, options, 0)
    second = judge._cached_judge(messages, options, 0)
    assert first.output == second.output
    assert calls == 1
    assert "真实回答" not in next((tmp_path / "cache" / "judge").glob("*.json")).read_text(encoding="utf-8")


def test_unsupported_json_schema_falls_back_to_prompted_json(monkeypatch):
    app = Settings(_env_file=None, ai_provider="mock")
    evaluation = EvaluationSettings(
        _env_file=None,
        judge_api_key=SecretStr("test-only"),
    )
    judge = DeepSeekJudge(app, evaluation)
    expected = BusinessJudgeResult(
        _judge_output(1.0),
        "prompted_json",
        0,
        evaluation.judge_model,
        "business-judge-v1",
    )

    def reject_schema(*args, **kwargs):
        raise StructuredCompletionError(
            "STRUCTURED_OUTPUT_UNSUPPORTED",
            "strict JSON Schema is unsupported",
        )

    monkeypatch.setattr(judge.client, "complete_structured", reject_schema)
    monkeypatch.setattr(judge, "_prompted_json", lambda messages: expected)

    case, outcome = _case_and_outcome()
    result = judge._judge_once(business_judge_messages(case, outcome), judge.client.settings)

    assert result is expected
