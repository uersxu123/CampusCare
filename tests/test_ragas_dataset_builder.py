from __future__ import annotations

import json
from pathlib import Path

from scripts.build_ragas_dataset import (
    ACTION_BY_GRADE,
    build_reference,
    facet_supported_text,
    load_source_cases,
    split_sentences,
)


ROOT = Path(__file__).resolve().parents[1]


def test_source_dataset_has_181_unique_cases() -> None:
    cases = load_source_cases(ROOT / "app/rag_eval/mindbridge-rag-eval.json")
    assert len(cases) == 181
    assert len({case["id"] for case in cases}) == 181


def test_strict_facets_require_real_evidence_patterns() -> None:
    assert facet_supported_text("DEADLINE", "学生应在处分决定书送达之日起 10 日内提出申诉。", "申诉期限")
    assert not facet_supported_text("CONTACT", "学生申诉处理委员会办公室设在校团委。", "处分申诉咨询电话是多少？")
    assert facet_supported_text("CHANNEL", "登录学校官网信息门户，进入服务中心在线办理。", "入口在哪里？")


def test_reference_builder_abstains_without_context() -> None:
    reference = build_reference(
        {"id": "test", "query": "咨询电话是多少？", "requiredFacets": ["CONTACT"], "forbiddenClaims": []},
        ACTION_BY_GRADE["NONE"],
        [],
        ["CONTACT"],
    )
    assert "无法核实" in reference
    assert "电话" not in reference or "联系方式" in reference


def test_generated_dataset_has_no_unicode_escape_literals() -> None:
    path = ROOT / "app/evaluation/datasets/mindbridge-e2e-ragas-v1.jsonl"
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        rows.append(json.loads(line))
        assert r"\u" not in line
    assert len(rows) == 181
    assert len({row["id"] for row in rows}) == 181
    for row in rows:
        assert row["reference"]
        assert len(row["reference_contexts"]) == len(row["reference_context_ids"])
        assert len(row["reference_contexts"]) == len(row["reference_context_metadata"])
        if row["expected_action"] in {"ANSWER", "PARTIAL_ANSWER"}:
            assert row["reference_contexts"]


def test_markdown_headings_do_not_delete_content() -> None:
    sentences = split_sentences("# 标题 正文内容。## 下一节 继续内容。")
    assert any("正文内容" in item for item in sentences)
    assert any("继续内容" in item for item in sentences)
