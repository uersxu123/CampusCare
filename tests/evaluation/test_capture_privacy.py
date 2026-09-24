from app.evaluation.reporting.writer import validate_report_privacy


def test_report_payload_does_not_block_sensitive_looking_content():
    payload = {
        "api_key": "secret",
        "headers": {"Authorization": "Bearer secret"},
        "message": "Bearer abcdefghijk",
        "database": "mysql://user:secret@localhost/db",
        "case_id": "ragas-internship-risk-comparison-137",
    }

    assert validate_report_privacy(payload) == payload


def test_report_privacy_keeps_readable_chinese():
    assert validate_report_privacy({"reason": "语料指纹不一致"})["reason"] == "语料指纹不一致"
