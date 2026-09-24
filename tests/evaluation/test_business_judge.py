import pytest
from pydantic import ValidationError

from app.evaluation.judges.business import BusinessJudgeOutput


def test_business_judge_output_is_strict_and_bounded():
    valid = {
        "observed_action": "ANSWER",
        "relevance": 1, "accuracy": 1, "completeness": 0.8, "helpfulness": 0.9,
        "action_correctness": 1, "verdict": "PASS", "reasons": [], "unsupported_claims": [],
    }
    assert BusinessJudgeOutput.model_validate(valid).verdict == "PASS"
    with pytest.raises(ValidationError):
        BusinessJudgeOutput.model_validate(valid | {"accuracy": 1.1})
    with pytest.raises(ValidationError):
        BusinessJudgeOutput.model_validate(valid | {"unexpected": True})
