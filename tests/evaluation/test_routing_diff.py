import json

import pytest

from app.evaluation.reporting.routing_diff import compare_routing_reports, main


IDENTITY = {
    "datasetSha256": "same", "promptVersion": "five-intent-context-v3",
    "schemaName": "understanding_decision_v3", "fusionConstantsVersion": "five-intent-fusion-v3",
    "understandingProvider": "ollama", "understandingModel": "qwen3:8b",
    "embeddingProvider": "ollama", "embeddingModel": "bge-m3:latest", "embeddingDigest": "digest",
}


def test_routing_diff_only_compares_matching_v3_identity():
    baseline = {"code": {"gitCommit": "before"}, "dataset": {"sha256": "same"}, "routingIdentity": IDENTITY,
                "metrics": {"routing": {"primaryIntentAccuracy": 0.9}}}
    current = {"code": {"gitCommit": "after"}, "dataset": {"sha256": "same"}, "routingIdentity": IDENTITY,
               "metrics": {"routing": {"primaryIntentAccuracy": 0.95}}}
    report = compare_routing_reports(baseline, current)
    assert report["status"] == "COMPARABLE"
    assert next(item for item in report["changes"] if item["metric"] == "primaryIntentAccuracy")["delta"] == pytest.approx(0.05)


def test_routing_diff_marks_contract_change_incomparable():
    baseline = {"dataset": {"sha256": "old"}, "routingIdentity": IDENTITY}
    current = {"dataset": {"sha256": "new"}, "routingIdentity": {**IDENTITY, "datasetSha256": "new"}}
    report = compare_routing_reports(baseline, current)
    assert report["status"] == "INCOMPARABLE_DATASET"
    assert report["changes"] == []


def test_routing_diff_writes_readable_utf8(tmp_path):
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    output = tmp_path / "diff.json"
    payload = {"dataset": {"sha256": "same"}, "routingIdentity": IDENTITY, "metrics": {"routing": {}}}
    baseline.write_text(json.dumps(payload), encoding="utf-8")
    current.write_text(json.dumps(payload), encoding="utf-8")
    assert main([str(baseline), str(current), str(output)]) == 0
    assert "COMPARABLE" in output.read_bytes().decode("utf-8")
