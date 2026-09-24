from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.knowledge_scoring import KnowledgeTokenizer, _bm25_field


DEFAULT_SOURCE = Path("app/rag_eval/mindbridge-rag-eval.json")
DEFAULT_DATABASE = Path("target/harness/mindbridge-harness.sqlite3")
DEFAULT_OUTPUT = Path("app/evaluation/datasets/mindbridge-e2e-ragas-v1.jsonl")
DEFAULT_AUDIT = Path("app/evaluation/datasets/mindbridge-e2e-ragas-v1.audit.json")
MAX_CONTEXTS = 6
MAX_REFERENCE_SENTENCES = 6
BUILD_SCRIPT_VERSION = "ragas-dataset-v6-five-intent-route-v3"

ACTION_BY_GRADE = {
    "SUFFICIENT": "ANSWER",
    "PARTIAL": "PARTIAL_ANSWER",
    "CLARIFY": "CLARIFY",
    "NONE": "ABSTAIN",
}

FACET_LABELS = {
    "CHANNEL": "办理渠道",
    "CONTACT": "联系方式",
    "DEADLINE": "截止时间",
    "ELIGIBILITY": "申请资格",
    "MATERIALS": "所需材料",
    "POLICY_BASIS": "制度依据",
    "PROCESSING_TIME": "办理时长",
    "SCOPE": "适用范围",
    "STEPS": "办理步骤",
}

GRADE_BY_ACTION = {
    "ANSWER": "SUFFICIENT",
    "PARTIAL_ANSWER": "PARTIAL",
    "CLARIFY": "CLARIFY",
    "ABSTAIN": "NONE",
}

# 这些原始检索标签与当前已核验语料不一致。覆盖项只修正新增 RAGAS 数据，
# 原 Engineering Harness 数据集保持不变，并在输出中保留原标签供追溯。
ACTION_OVERRIDES = {
    "grant-contact-38": (
        "ABSTAIN",
        "当前助学金章节没有可核验的助学金咨询电话；语料中的电话属于应征入伍资助，不能挪作助学金咨询电话。",
    ),
    "network-account-46": (
        "PARTIAL_ANSWER",
        "当前资料可确认校园网由信息化工作办公室管理及账号使用规则，但未给出账号办理地点或入口。",
    ),
    "loan-materials-36": (
        "ABSTAIN",
        "生源地助学贷款章节只说明材料以国家开发银行当年申请指南为准，当前校内核验语料没有材料清单。",
    ),
    "loan-material-policy-151": (
        "PARTIAL_ANSWER",
        "当前语料可以确认生源地助学贷款的正式实施细则，但没有给出申请材料清单，材料应以国家开发银行当年申请指南为准。",
    ),
    "hardship-loan-materials-102": (
        "PARTIAL_ANSWER",
        "困难认定相关材料可以从校内办法核验，但生源地贷款材料在当前语料中未列明，应以当年申请指南为准。",
    ),
}

CASE_EVIDENCE_HINTS = {
    "appeal-contact-partial-20": ("学生申诉处理委员会办公室设在校团委",),
    "appeal-deadline-16": ("学生本人对处分决定有异议", "送达之日起 10 日内"),
    "discipline-types-19": ("纪律处分", "种类和期限如下"),
    "network-account-46": ("校园计算机网络（以下简称：校园网）由信息化工作办公室统一管理",),
    "sleep-support-68": ("固定起床时间",),
    "retake-eligibility-26": ("考核不合格的课程应重新选课修读",),
    "organization-steps-82": ("申请成立、学期注册",),
    "suspension-health-flow-128": ("因身心健康原因不适宜在校学习",),
    "loan-materials-36": ("生源地信用助学贷款办理具体事宜",),
    "loan-material-policy-151": ("生源地信用助学贷款办理具体事宜",),
    "hardship-loan-materials-102": ("家庭经济情况调查表",),
}

CASE_REFERENCE_FACTS = {
    "appeal-deadline-16": ("10 日内",),
    "discipline-types-19": ("警告", "严重警告", "记过", "留校察看", "开除学籍"),
    "network-account-46": ("信息化工作办公室",),
    "network-vehicle-policy-104": ("校园网", "机动车"),
    "suspension-health-flow-128": ("因身心健康原因不适宜在校学习", "书面申请"),
    "thesis-integrity-boundary-96": ("正确引用来源", "不得抄袭"),
}

CASE_MISSING_FACETS = {
    "network-account-46": ("CHANNEL",),
    "loan-material-policy-151": ("MATERIALS",),
    "hardship-loan-materials-102": ("MATERIALS",),
}

SEMANTIC_EQUIVALENTS = {
    "线上办理": ("在线办理",),
}

STRICT_FACETS = {"CHANNEL", "CONTACT", "COST", "DEADLINE", "PROCESSING_TIME"}

FACET_EVIDENCE_PATTERNS = {
    "ELIGIBILITY": (
        r"申请条件",
        r"符合.{0,24}条件",
        r"可以申请",
        r"可申请",
        r"有下列.{0,12}情形",
        r"予以(?:毕业|结业|补考|重修)",
        r"应当办理休学",
        r"因身心健康原因",
        r"应重新选课修读",
    ),
    "MATERIALS": (
        r"申请表",
        r"申请书",
        r"申诉书",
        r"决定书.{0,8}副本",
        r"书面委托",
        r"提交.{0,24}(?:材料|证明|复印件|承诺书)",
        r"提供.{0,24}(?:材料|证明|复印件)",
        r"收集.{0,12}资料",
        r"签字盖章",
    ),
    "STEPS": (
        r"(?:办理|申请|评审|报销|预约|就业).{0,12}流程",
        r"填写",
        r"提交",
        r"递交",
        r"审核",
        r"审批",
        r"领取",
        r"联系(?:导师|辅导员|学院|心理中心)",
        r"拆分",
        r"安排",
        r"建议",
        r"可以尝试",
        r"可以(?:使用|先|从|做)",
        r"申请成立",
        r"先.{0,36}(?:再|然后|开始|完成)",
        r"固定.{0,24}(?:时间|地点|安排)",
        r"逐步",
    ),
    "CHANNEL": (
        r"信息门户",
        r"(?:线上|在线)办理",
        r"(?:办理|服务|预约)(?:入口|平台|系统)",
        r"(?:服务中心|心理中心|教务部门|辅导员|办公室|服务台|一站式服务大厅)",
        r"到.{0,24}(?:办理|提交|递交|归还|申请)",
    ),
    "DEADLINE": (
        r"(?:收到|送达|公告|决定书).{0,32}\d+\s*(?:日|天|个月|工作日)(?:内|后)",
        r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日",
        r"\d{1,2}\s*月\s*\d{1,2}\s*日(?:前|后|截止)",
        r"(?:截止|期限|有效期).{0,24}\d+",
        r"每年.{0,12}(?:前|截止|办理)",
    ),
    "PROCESSING_TIME": (
        r"\d+\s*个?工作日(?:内|办结)",
        r"(?:办理|处理|审批|办结)(?:时长|时限).{0,16}\d+",
    ),
    "CONTACT": (
        r"(?<!\d)(?:0\d{2,3}[-\s]?)?\d{7,8}(?!\d)",
        r"联系(?:导师|辅导员|学院老师|心理中心|教务部门)",
    ),
    "COST": (
        r"\d+(?:\.\d+)?\s*元",
        r"(?:收费|缴费|资助|补助)(?:标准|金额)",
        r"(?:结清|缴纳).{0,12}(?:费用|水电费|住宿费|学费)",
    ),
    "SCOPE": (
        r"适用(?:于|范围)",
        r"(?:全日制|在籍|在校).{0,16}(?:本科生|研究生|学生)",
    ),
    "POLICY_BASIS": (
        r"(?:根据|依据).{0,80}(?:办法|规定|条例|法)",
        r"《[^》]{2,80}(?:办法|规定|条例|法|细则|标准)》",
    ),
}

GENERIC_QUERY_TERMS = {
    "一下",
    "什么",
    "哪些",
    "哪里",
    "怎么",
    "怎样",
    "如何",
    "是否",
    "可以",
    "需要",
    "应该",
    "学校",
    "学生",
    "相关",
    "办理",
    "申请",
}


@dataclass(frozen=True)
class Chunk:
    chunk_id: int
    document_key: str
    source_key: str
    title: str
    section_title: str
    content: str
    content_hash: str
    page_number: int | None
    source_index: int
    tags_json: str = "[]"


@dataclass(frozen=True)
class Bm25Document:
    title: str
    tags_json: str


@dataclass(frozen=True)
class Bm25Chunk:
    id: int
    source: str
    section_title: str
    content: str
    document: Bm25Document


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从现有 RAG golden set 构建端到端 RAGAS 数据集。")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--database-url", help="从运行中的 MySQL ACTIVE 语料构建")
    parser.add_argument(
        "--database-url-from-settings",
        action="store_true",
        help="从 app Settings/.env 读取数据库连接，不在命令行暴露凭据",
    )
    parser.add_argument("--chroma-host", default="127.0.0.1")
    parser.add_argument("--chroma-port", type=int, default=8001)
    parser.add_argument("--chroma-persist-dir", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cases = load_source_cases(args.source)
    database_url = args.database_url
    if args.database_url_from_settings:
        if database_url:
            raise ValueError("--database-url 与 --database-url-from-settings 不能同时使用")
        from app.core.config import get_settings

        database_url = get_settings().database_url
    if database_url:
        chunks_by_key = load_active_chunks_from_url(database_url)
        reference_source = build_reference_source_from_url(
            database_url,
            chroma_host=args.chroma_host,
            chroma_port=args.chroma_port,
            chroma_persist_dir=args.chroma_persist_dir,
        )
    else:
        chunks_by_key = load_active_chunks(args.database)
        reference_source = build_reference_source(args.database)
    bm25_rankings = build_bm25_rankings(cases, chunks_by_key)
    converted = []
    audit_rows = []
    for case in cases:
        converted_case, case_audit = convert_case(
            case,
            chunks_by_key,
            bm25_rankings.get(str(case["id"]), {}),
        )
        converted.append(converted_case)
        audit_rows.append(case_audit)

    audit = build_audit(
        args.source,
        args.database,
        converted,
        audit_rows,
        reference_source=reference_source,
    )
    if audit["blockingIssueCount"]:
        args.audit.parent.mkdir(parents=True, exist_ok=True)
        args.audit.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        raise ValueError(
            f"RAGAS 数据集存在 {audit['blockingIssueCount']} 个阻断问题，详情见内存审计结果"
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in converted),
        encoding="utf-8",
        newline="\n",
    )
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    args.audit.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "audit": str(args.audit),
                "cases": len(converted),
                "blockingIssues": audit["blockingIssueCount"],
                "reviewWarnings": audit["reviewWarningCount"],
            },
            ensure_ascii=False,
        )
    )
    return 0


def load_source_cases(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError("源 RAG 数据集必须是非空 JSON 数组")
    ids = [str(item.get("id") or "") for item in payload if isinstance(item, dict)]
    if len(ids) != len(payload) or any(not case_id for case_id in ids):
        raise ValueError("源 RAG 数据集存在无效 case")
    if len(ids) != len(set(ids)):
        raise ValueError("源 RAG 数据集 case id 必须唯一")
    return payload


def load_active_chunks(database: Path) -> dict[str, list[Chunk]]:
    if not database.exists():
        raise FileNotFoundError(f"知识数据库不存在：{database}")
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT
                c.id AS chunk_id,
                d.source_key,
                COALESCE(d.canonical_key, d.source_key) AS document_key,
                d.title,
                COALESCE(c.section_title, '') AS section_title,
                c.content,
                c.content_hash,
                c.page_number,
                c.source_index
                ,COALESCE(d.tags_json, '[]') AS tags_json
            FROM knowledge_documents d
            JOIN knowledge_chunks c ON c.document_id = d.id
            WHERE d.status = 'ACTIVE'
            ORDER BY d.id, c.source_index, c.id
            """
        ).fetchall()
    finally:
        connection.close()

    result: dict[str, list[Chunk]] = {}
    seen_by_key: dict[str, set[int]] = {}
    for row in rows:
        chunk = Chunk(
            chunk_id=int(row["chunk_id"]),
            document_key=str(row["document_key"]),
            source_key=str(row["source_key"]),
            title=str(row["title"]),
            section_title=str(row["section_title"]),
            content=str(row["content"]),
            content_hash=str(row["content_hash"] or ""),
            page_number=int(row["page_number"]) if row["page_number"] is not None else None,
            source_index=int(row["source_index"]),
            tags_json=str(row["tags_json"] or "[]"),
        )
        for key in {chunk.document_key, chunk.source_key}:
            seen = seen_by_key.setdefault(key, set())
            if chunk.chunk_id in seen:
                continue
            seen.add(chunk.chunk_id)
            result.setdefault(key, []).append(chunk)
    return result


def load_active_chunks_from_url(database_url: str) -> dict[str, list[Chunk]]:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT
                        c.id AS chunk_id,
                        d.source_key,
                        COALESCE(d.canonical_key, d.source_key) AS document_key,
                        d.title,
                        COALESCE(c.section_title, '') AS section_title,
                        c.content,
                        c.content_hash,
                        c.page_number,
                        c.source_index,
                        COALESCE(d.tags_json, '[]') AS tags_json
                    FROM knowledge_documents d
                    JOIN knowledge_chunks c ON c.document_id = d.id
                    WHERE d.status = 'ACTIVE'
                      AND (c.chunk_kind IS NULL OR c.chunk_kind <> 'TEXT_PARENT')
                    ORDER BY d.id, c.source_index, c.id
                    """
                )
            ).mappings().all()
    finally:
        engine.dispose()

    return _chunks_by_key(rows)


def _chunks_by_key(rows) -> dict[str, list[Chunk]]:
    result: dict[str, list[Chunk]] = {}
    seen_by_key: dict[str, set[int]] = {}
    for row in rows:
        chunk = Chunk(
            chunk_id=int(row["chunk_id"]),
            document_key=str(row["document_key"]),
            source_key=str(row["source_key"]),
            title=str(row["title"] or ""),
            section_title=str(row["section_title"] or ""),
            content=str(row["content"] or ""),
            content_hash=str(row["content_hash"] or ""),
            page_number=int(row["page_number"]) if row["page_number"] is not None else None,
            source_index=int(row["source_index"]),
            tags_json=str(row["tags_json"] or "[]"),
        )
        for key in {chunk.document_key, chunk.source_key}:
            seen = seen_by_key.setdefault(key, set())
            if chunk.chunk_id in seen:
                continue
            seen.add(chunk.chunk_id)
            result.setdefault(key, []).append(chunk)
    return result


def build_bm25_rankings(
    cases: list[dict],
    chunks_by_key: dict[str, list[Chunk]],
) -> dict[str, dict[int, tuple[int, float]]]:
    unique_chunks = {
        chunk.chunk_id: chunk
        for chunks in chunks_by_key.values()
        for chunk in chunks
    }
    corpus = [
        Bm25Chunk(
            id=chunk.chunk_id,
            source=chunk.source_key,
            section_title=chunk.section_title,
            content=chunk.content,
            document=Bm25Document(title=chunk.title, tags_json=chunk.tags_json),
        )
        for chunk in unique_chunks.values()
    ]
    tokenizer = KnowledgeTokenizer()
    fields = {
        "tags": (3.4, lambda item: item.document.tags_json),
        "section": (3.0, lambda item: item.section_title),
        "title": (2.4, lambda item: item.document.title),
        "content": (1.0, lambda item: item.content),
    }
    prepared_fields = {}
    for name, (_weight, getter) in fields.items():
        rows = []
        for item in corpus:
            counts = Counter(tokenizer.tokenize(getter(item)))
            rows.append((item.id, counts, sum(counts.values())))
        prepared_fields[name] = rows
    rankings: dict[str, dict[int, tuple[int, float]]] = {}
    for case in cases:
        query_counts = Counter(tokenizer.tokenize(str(case["query"])))
        scores: Counter[int] = Counter()
        for name, (weight, _getter) in fields.items():
            for chunk_id, score in _bm25_field(query_counts, prepared_fields[name]).items():
                scores[chunk_id] += score * weight
        ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        rankings[str(case["id"])] = {
            chunk_id: (rank, score)
            for rank, (chunk_id, score) in enumerate(ordered, start=1)
        }
    return rankings


def convert_case(
    case: dict,
    chunks_by_key: dict[str, list[Chunk]],
    bm25_ranking: dict[int, tuple[int, float]] | None = None,
) -> tuple[dict, dict]:
    source_grade = str(case["expectedGrade"])
    source_action = ACTION_BY_GRADE[source_grade]
    expected_keys = [str(item) for item in case.get("expectedDocumentKeys", [])]
    sections = [str(item) for item in case.get("expectedSections", [])]
    facts = reference_facts(case)
    source_facts = [str(item) for item in case.get("requiredFacts", [])]
    forbidden = [str(item) for item in case.get("forbiddenClaims", [])]
    available = {
        key: list(chunks_by_key.get(key, []))
        for key in expected_keys
    }
    selected = select_reference_chunks(case, available, bm25_ranking or {})
    covered_facets = supported_facets(case, selected)
    missing_facets = [
        facet for facet in case.get("requiredFacets", []) if str(facet) not in covered_facets
    ]
    action, adjustment_reason = resolve_expected_action(
        case,
        source_action,
        selected,
        missing_facets,
    )
    if str(case["id"]) == "network-account-46":
        covered_facets.discard("CHANNEL")
        missing_facets = ["CHANNEL"]
    missing_facets = sorted(
        set(missing_facets).union(CASE_MISSING_FACETS.get(str(case["id"]), ()))
    )
    if action == "ABSTAIN":
        selected = []
        covered_facets = set()
        missing_facets = [str(facet) for facet in case.get("requiredFacets", [])]
    grade = GRADE_BY_ACTION[action]
    reference = build_reference(case, action, selected, missing_facets)
    tags = build_tags(case, action)

    converted = {
        "id": f"ragas-{case['id']}",
        "sourceCaseId": str(case["id"]),
        "user_input": str(case["query"]),
        "turns": [str(case["query"])],
        "expected_action": action,
        "expectedTools": expected_tools(action, selected),
        "expectedRoute": expected_route(str(case["id"])),
        "expected_grade": grade,
        "source_expected_action": source_action,
        "source_expected_grade": source_grade,
        "action_adjustment_reason": adjustment_reason,
        "reference": reference,
        "reference_facts": facts,
        "source_reference_facts": source_facts,
        "reference_context_ids": [f"knowledge:{item.chunk_id}" for item in selected],
        "reference_contexts": [item.content for item in selected],
        "reference_context_metadata": [chunk_metadata(item) for item in selected],
        "expected_document_keys": expected_keys,
        "expected_sections": sections,
        "expected_scope": str(case.get("expectedScope") or "ALL"),
        "required_facets": [str(item) for item in case.get("requiredFacets", [])],
        "expected_missing_facets": [str(item) for item in case.get("expectedMissingFacets", [])],
        "forbidden_document_keys": [str(item) for item in case.get("forbiddenDocumentKeys", [])],
        "forbidden_claims": forbidden,
        "metric_applicability": metric_applicability(action),
        "tags": tags,
        "annotation": {
            "referenceAuthoringMethod": "source_sentence_extractive_v2",
            "referenceContextSelectionMethod": "bm25_expected_document_section_fact_facet_v2",
            "requiresHumanReview": False,
            "missingFacets": missing_facets,
        },
    }

    all_expected_chunks = [chunk for key in expected_keys for chunk in available.get(key, [])]
    missing_documents = [key for key in expected_keys if not available.get(key)]
    missing_sections = [
        section
        for section in sections
        if not any(section in chunk.section_title for chunk in all_expected_chunks)
    ]
    missing_facts_in_documents = [fact for fact in facts if not fact_supported(fact, all_expected_chunks)]
    missing_facts_in_selected = [fact for fact in facts if not fact_supported(fact, selected)]
    forbidden_in_reference = [claim for claim in forbidden if claim and claim in reference]
    forbidden_in_contexts = [
        claim
        for claim in forbidden
        if claim and any(claim in chunk.content for chunk in selected)
    ]
    blocking = []
    warnings = []
    if missing_documents:
        blocking.append("EXPECTED_DOCUMENT_MISSING")
    if missing_sections:
        blocking.append("EXPECTED_SECTION_MISSING")
    if missing_facts_in_selected and action in {"ANSWER", "PARTIAL_ANSWER"}:
        blocking.append("REFERENCE_FACT_NOT_SELECTED")
    if action in {"ANSWER", "PARTIAL_ANSWER"} and not selected:
        blocking.append("ANSWER_CONTEXT_EMPTY")
    if action in {"ANSWER", "PARTIAL_ANSWER"} and not reference.strip():
        blocking.append("ANSWER_REFERENCE_EMPTY")
    if forbidden_in_reference:
        blocking.append("FORBIDDEN_CLAIM_IN_REFERENCE")
    if forbidden_in_contexts:
        warnings.append("FORBIDDEN_CLAIM_PRESENT_IN_SOURCE_CONTEXT")
    if missing_facts_in_documents:
        warnings.append("REFERENCE_FACT_SEMANTIC_MATCH_ONLY")
    if any(facet in STRICT_FACETS for facet in missing_facets) and action == "ANSWER":
        blocking.append("REQUIRED_FACET_NOT_SUPPORTED")
    elif missing_facets and action == "ANSWER":
        warnings.append("REQUIRED_FACET_COVERAGE_HEURISTIC_MISS")
    if adjustment_reason:
        warnings.append("SOURCE_ACTION_ADJUSTED")
    irrelevant_contexts = [
        f"knowledge:{chunk.chunk_id}"
        for chunk in selected
        if not chunk_relevant_to_case(case, chunk) and not chunk_matches_case_hint(case, chunk)
    ]
    if irrelevant_contexts:
        warnings.append("LOW_RELEVANCE_REFERENCE_CONTEXT")

    audit = {
        "id": converted["id"],
        "sourceCaseId": converted["sourceCaseId"],
        "action": action,
        "sourceAction": source_action,
        "actionAdjustmentReason": adjustment_reason,
        "contextCount": len(selected),
        "referenceLength": len(reference),
        "coveredFacets": sorted(covered_facets),
        "missingRequiredFacets": missing_facets,
        "irrelevantContextIds": irrelevant_contexts,
        "missingDocuments": missing_documents,
        "missingSections": missing_sections,
        "missingFactsInDocuments": missing_facts_in_documents,
        "missingFactsInSelectedContexts": missing_facts_in_selected,
        "forbiddenClaimsInReference": forbidden_in_reference,
        "forbiddenClaimsInContexts": forbidden_in_contexts,
        "blockingIssues": sorted(set(blocking)),
        "reviewWarnings": sorted(set(warnings)),
    }
    return converted, audit


def select_reference_chunks(
    case: dict,
    available: dict[str, list[Chunk]],
    bm25_ranking: dict[int, tuple[int, float]],
) -> list[Chunk]:
    sections = [str(item) for item in case.get("expectedSections", [])]
    facts = reference_facts(case)
    forbidden = [str(item) for item in case.get("forbiddenClaims", [])]
    query_terms = content_terms(str(case.get("query") or ""))
    if ACTION_BY_GRADE[str(case["expectedGrade"])] in {"ABSTAIN", "CLARIFY"}:
        return []

    chunks_by_id = {
        item.chunk_id: item
        for key in [str(value) for value in case.get("expectedDocumentKeys", [])]
        for item in available.get(key, [])
    }
    chunks = list(chunks_by_id.values())

    def score_key(item: Chunk) -> tuple[int, int]:
        return (
            chunk_score(
                item,
                sections,
                facts,
                [str(value) for value in case.get("requiredFacets", [])],
                forbidden,
                query_terms,
                bm25_ranking,
            ),
            -item.source_index,
        )

    ranked_all = sorted(chunks, key=score_key, reverse=True)
    section_chunks = [
        item
        for item in ranked_all
        if sections and any(section in item.section_title for section in sections)
    ]
    ranked_seed_pool = section_chunks or ranked_all
    selected: list[Chunk] = []
    hints = CASE_EVIDENCE_HINTS.get(str(case["id"]), ())

    if hints:
        supporting = next(
            (item for item in ranked_all if all(hint_supported(hint, item.content) for hint in hints)),
            None,
        )
        if supporting is not None:
            selected.append(supporting)

    if not selected:
        seed = next((item for item in ranked_seed_pool if chunk_relevant_to_case(case, item)), None)
        if seed is not None:
            selected.append(seed)

    for fact in facts:
        if fact_supported(fact, selected):
            continue
        supporting = next(
            (
                item
                for item in ranked_all
                if item not in selected and fact_supported(fact, [item])
            ),
            None,
        )
        if supporting is not None:
            selected.append(supporting)

    for facet in [str(value) for value in case.get("requiredFacets", [])]:
        if hints or str(case["id"]) in {"loan-materials-36", "loan-material-policy-151"}:
            continue
        if any(facet_supported_text(facet, item.content, str(case["query"])) for item in selected):
            continue
        matching = next(
            (
                item
                for item in ranked_all
                if item not in selected
                and chunk_relevant_to_case(case, item)
                and facet_supported_text(facet, item.content, str(case["query"]))
            ),
            None,
        )
        if matching is not None:
            selected.append(matching)

    deduplicated = []
    seen = set()
    for item in selected:
        identity = item.content_hash or hashlib.sha256(item.content.encode("utf-8")).hexdigest()
        if identity in seen:
            continue
        seen.add(identity)
        deduplicated.append(item)
    return deduplicated[:MAX_CONTEXTS]


def chunk_score(
    chunk: Chunk,
    sections: list[str],
    facts: list[str],
    facets: list[str],
    forbidden: list[str],
    query_terms: set[str],
    bm25_ranking: dict[int, tuple[int, float]],
) -> int:
    score = 0
    score += 40 * sum(section in chunk.section_title for section in sections)
    score += 60 * sum(fact_supported(fact, [chunk]) for fact in facts)
    score += 45 * sum(facet_supported_text(facet, chunk.content, "") for facet in facets)
    score += 4 * len(query_terms.intersection(content_terms(chunk.section_title + " " + chunk.content)))
    rank, bm25_score = bm25_ranking.get(chunk.chunk_id, (10_000, 0.0))
    score += max(0, 120 - rank * 3)
    score += min(80, int(bm25_score))
    score -= 50 * sum(claim in chunk.content for claim in forbidden if claim)
    return score


def resolve_expected_action(
    case: dict,
    source_action: str,
    selected: list[Chunk],
    missing_facets: list[str],
) -> tuple[str, str | None]:
    case_id = str(case["id"])
    if case_id in ACTION_OVERRIDES:
        return ACTION_OVERRIDES[case_id]
    if source_action != "ANSWER":
        return source_action, None
    strict_missing = [facet for facet in missing_facets if facet in STRICT_FACETS]
    if not strict_missing:
        return source_action, None
    labels = "、".join(FACET_LABELS.get(facet, facet) for facet in strict_missing)
    if selected:
        return "PARTIAL_ANSWER", f"当前已核验语料未完整覆盖{labels}，只能回答已有证据支持的部分。"
    return "ABSTAIN", f"当前已核验语料没有能够支持{labels}的有效上下文。"


def supported_facets(case: dict, chunks: Iterable[Chunk]) -> set[str]:
    query = str(case.get("query") or "")
    required = [str(item) for item in case.get("requiredFacets", [])]
    explicit_missing = {str(item) for item in case.get("expectedMissingFacets", [])}
    return {
        facet
        for facet in required
        if facet not in explicit_missing
        and any(facet_supported_text(facet, chunk.content, query) for chunk in chunks)
    }


def facet_supported_text(facet: str, text: str, query: str) -> bool:
    patterns = FACET_EVIDENCE_PATTERNS.get(facet, ())
    if facet == "CONTACT" and "电话" in query:
        patterns = (FACET_EVIDENCE_PATTERNS["CONTACT"][0],)
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def chunk_relevant_to_case(case: dict, chunk: Chunk) -> bool:
    facts = reference_facts(case)
    facets = [str(item) for item in case.get("requiredFacets", [])]
    query = str(case.get("query") or "")
    query_terms = content_terms(query)
    chunk_terms = content_terms(f"{chunk.section_title} {chunk.content}")
    overlap = len(query_terms.intersection(chunk_terms))
    if any(len(fact) >= 4 and fact_supported_text(fact, chunk.content) for fact in facts):
        return True
    if overlap >= 3:
        return True
    if overlap >= 2 and (
        any(fact_supported_text(fact, chunk.content) for fact in facts)
        or any(facet_supported_text(facet, chunk.content, query) for facet in facets)
    ):
        return True
    return False


def chunk_matches_case_hint(case: dict, chunk: Chunk) -> bool:
    hints = CASE_EVIDENCE_HINTS.get(str(case.get("id")), ())
    return bool(hints) and all(hint_supported(hint, chunk.content) for hint in hints)


def build_reference(
    case: dict,
    action: str,
    selected: list[Chunk],
    missing_facets: list[str],
) -> str:
    facts = reference_facts(case)
    forbidden = [str(item) for item in case.get("forbiddenClaims", [])]
    facets = [str(item) for item in case.get("requiredFacets", [])]
    query = str(case.get("query") or "")

    if action == "CLARIFY":
        return (
            "该问题缺少适用校区等必要范围，应先确认具体校区后再查询对应规定。"
            "当前期望来源仅能作为南望山校区资料使用，在范围明确前不应直接套用其中的流程、期限或联系方式。"
        )
    if action == "ABSTAIN":
        missing = "、".join(FACET_LABELS.get(item, item) for item in facets) or "所询问的具体信息"
        suffix = "，不得给出未经来源核实的具体数值、材料、时间、地点或归口结论" if forbidden else ""
        return (
            f"当前已核验资料不足以确认{missing}，应明确说明无法核实，并建议通过学校最新官方通知或对应管理部门确认{suffix}。"
        )

    sentences = ranked_reference_sentences(
        query,
        facts,
        facets,
        forbidden,
        selected,
        CASE_EVIDENCE_HINTS.get(str(case.get("id")), ()),
        tuple(str(item) for item in case.get("requiredFacts", [])),
    )
    if not sentences and selected:
        sentences = split_sentences(selected[0].content)[:3]
    body = "".join(ensure_sentence_ending(item) for item in sentences[:MAX_REFERENCE_SENTENCES])
    if action == "PARTIAL_ANSWER":
        missing_candidates = missing_facets or [
            str(item) for item in case.get("expectedMissingFacets", [])
        ]
        missing = (
            "、".join(FACET_LABELS.get(item, item) for item in missing_candidates)
            or "问题所需的全部信息"
        )
        if body:
            return (
                f"根据当前已核验资料，可以确认：{body}"
                f"但资料尚不足以完整确认{missing}，应明确说明证据缺口，并避免补充无依据的具体信息。"
            )
        return (
            f"当前资料只能提供有限背景，尚不足以完整确认{missing}。"
            "应明确说明证据缺口，并建议通过学校最新官方渠道核实。"
        )
    return f"根据当前已核验资料：{body}" if body else "根据当前已核验资料，应按对应正式规定办理。"


def ranked_reference_sentences(
    query: str,
    facts: list[str],
    facets: list[str],
    forbidden: list[str],
    selected: list[Chunk],
    hints: tuple[str, ...] = (),
    generic_facts: tuple[str, ...] = (),
) -> list[str]:
    rows = []
    seen = set()
    query_terms = content_terms(query)
    for chunk_order, chunk in enumerate(selected):
        for sentence_order, sentence in enumerate(split_sentences(chunk.content)):
            cleaned = trim_hint_sentence(sentence.strip(), hints)
            if not cleaned or any(claim in cleaned for claim in forbidden if claim):
                continue
            identity = normalize_text(cleaned)
            if identity in seen:
                continue
            seen.add(identity)
            fact_hits = sum(fact_supported(fact, [chunk_with_content(chunk, cleaned)]) for fact in facts)
            facet_hits = sum(facet_supported_text(facet, cleaned, query) for facet in facets)
            overlap = len(query_terms.intersection(content_terms(cleaned)))
            hint_hits = sum(hint_supported(hint, cleaned) for hint in hints)
            action_signal = bool(
                "STEPS" in facets
                and re.search(r"建议|先|再|然后|可以(?:使用|尝试|先|从|做)|固定|逐步", cleaned)
            )
            generic_penalty = 25 if "MindBridge" in cleaned else 0
            score = (
                fact_hits * 100
                + facet_hits * 25
                + hint_hits * 200
                + overlap * 12
                + action_signal * 30
                - generic_penalty
                - chunk_order
                - sentence_order
            )
            rows.append((score, chunk_order, sentence_order, cleaned))

    required_rows = []
    for fact in facts:
        matches = [row for row in rows if fact_supported_text(fact, row[3])]
        match = max(
            matches,
            key=lambda row: (
                len(query_terms.intersection(content_terms(row[3]))),
                row[0],
            ),
            default=None,
        )
        if (
            match is not None
            and hints
            and fact in generic_facts
            and len(fact) <= 3
            and not any(hint_supported(hint, match[3]) for hint in hints)
        ):
            match = None
        if match is not None and match not in required_rows:
            required_rows.append(match)

    for facet in facets:
        if hints:
            continue
        matches = [row for row in rows if facet_supported_text(facet, row[3], query)]
        match = max(
            matches,
            key=lambda row: (
                len(query_terms.intersection(content_terms(row[3]))),
                row[0],
            ),
            default=None,
        )
        if match is not None and match not in required_rows:
            required_rows.append(match)

    if hints:
        match = next(
            (row for row in rows if all(hint_supported(hint, row[3]) for hint in hints)),
            None,
        )
        if match is not None and match not in required_rows:
            required_rows.append(match)

    result_rows = list(required_rows)
    target_count = max(1, len(required_rows))
    for row in sorted(rows, key=lambda item: (-item[0], item[1], item[2])):
        if row not in result_rows and (row[0] >= 10 or not result_rows):
            result_rows.append(row)
        if len(result_rows) >= min(MAX_REFERENCE_SENTENCES, target_count):
            break
    return [row[3] for row in result_rows[:MAX_REFERENCE_SENTENCES]]


def chunk_with_content(chunk: Chunk, content: str) -> Chunk:
    return Chunk(
        chunk_id=chunk.chunk_id,
        document_key=chunk.document_key,
        source_key=chunk.source_key,
        title=chunk.title,
        section_title=chunk.section_title,
        content=content,
        content_hash=chunk.content_hash,
        page_number=chunk.page_number,
        source_index=chunk.source_index,
        tags_json=chunk.tags_json,
    )


def split_sentences(content: str) -> list[str]:
    normalized = re.sub(r"#{1,6}\s+", "。", content)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return []
    sentences = [
        clean_reference_sentence(item)
        for item in re.split(r"(?<=[。！？；])", normalized)
        if item.strip()
    ]
    return [item for item in sentences if item]


def clean_reference_sentence(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip()
    cleaned = re.sub(r"^#{1,6}\s*", "", cleaned)
    cleaned = re.sub(r"^(?:[-*•]\s*)+", "", cleaned)
    cleaned = re.sub(r"^[”’'\"：:；;，,、\s]+", "", cleaned)
    cleaned = re.sub(r"^(?:然后|接着)\s*", "", cleaned)
    cleaned = re.sub(r"^(第[一二三四五六七八九十百零\d]+条)\s*\1\s*", r"\1 ", cleaned)
    return cleaned


def trim_hint_sentence(value: str, hints: tuple[str, ...]) -> str:
    if not hints or not all(hint_supported(hint, value) for hint in hints):
        return value
    markers = list(re.finditer(r"第[一二三四五六七八九十百零\d]+条", value))
    if markers and markers[-1].start() > 0:
        return value[markers[-1].start() :].lstrip()
    return value


def ensure_sentence_ending(value: str) -> str:
    return value if value.endswith(("。", "！", "？", "；")) else value + "。"


def chunk_metadata(chunk: Chunk) -> dict:
    return {
        "id": f"knowledge:{chunk.chunk_id}",
        "documentKey": chunk.document_key,
        "sourceKey": chunk.source_key,
        "title": chunk.title,
        "sectionTitle": chunk.section_title,
        "pageNumber": chunk.page_number,
        "contentHash": chunk.content_hash,
        "sourceIndex": chunk.source_index,
    }


def build_tags(case: dict, action: str) -> list[str]:
    tags = {
        action.lower().replace("_", "-"),
        str(case.get("expectedScope") or "ALL").lower().replace("_", "-"),
    }
    case_id = str(case.get("id") or "")
    for prefix in (
        "academic",
        "adjustment",
        "aid",
        "anxiety",
        "appeal",
        "award",
        "career",
        "counseling",
        "discipline",
        "dormitory",
        "exam",
        "financial",
        "housing",
        "mental",
        "negative",
        "privacy",
        "relationship",
        "research",
        "sleep",
        "thesis",
    ):
        if case_id.startswith(prefix):
            tags.add(prefix)
    return sorted(tags)


def metric_applicability(action: str) -> dict:
    answer_metrics = action in {"ANSWER", "PARTIAL_ANSWER"}
    return {
        "faithfulness": answer_metrics,
        "answerRelevancy": answer_metrics,
        "contextPrecision": answer_metrics,
        "contextRecall": answer_metrics,
        "idBasedContextPrecision": answer_metrics,
        "idBasedContextRecall": answer_metrics,
        "actionCorrectness": True,
        "safetyContract": action == "SAFETY_BYPASS",
    }


def expected_tools(action: str, selected: list[Chunk]) -> dict[str, list[str]]:
    if action == "SAFETY_BYPASS":
        return {"required": [], "forbidden": ["rag_search", "get_current_weather"]}
    if action == "ABSTAIN":
        return {"required": ["rag_search"], "forbidden": []}
    if action == "CLARIFY":
        return {"required": [], "forbidden": ["rag_search"]}
    if selected:
        return {"required": ["rag_search"], "forbidden": []}
    return {"required": [], "forbidden": ["rag_search"]}


def expected_route(case_id: str) -> dict | None:
    if case_id != "housing-change-materials-01":
        return None
    return {
        "primaryIntent": "CAMPUS",
        "intents": ["CAMPUS"],
        "riskLevel": "LOW",
        "workItemCount": 1,
        "workItemIntents": ["CAMPUS"],
        "dependencyEdges": [],
        "missingArgumentNamesByWorkItem": [[]],
    }


def reference_facts(case: dict) -> list[str]:
    facts = [str(item) for item in case.get("requiredFacts", [])]
    for fact in CASE_REFERENCE_FACTS.get(str(case.get("id")), ()):
        if fact not in facts:
            facts.append(fact)
    return facts


def fact_supported(fact: str, chunks: Iterable[Chunk]) -> bool:
    return any(fact_supported_text(fact, chunk.content) for chunk in chunks)


def fact_supported_text(fact: str, text: str) -> bool:
    if fact in text:
        return True
    return any(equivalent in text for equivalent in SEMANTIC_EQUIVALENTS.get(fact, ()))


def content_terms(value: str) -> set[str]:
    normalized = normalize_text(value)
    words = {
        item.lower()
        for item in re.findall(r"[A-Za-z0-9_-]{2,}|[一-龿]{2,8}", normalized)
        if item not in GENERIC_QUERY_TERMS
    }
    chinese = "".join(re.findall(r"[一-龿]", normalized))
    words.update(chinese[index : index + 2] for index in range(max(0, len(chinese) - 1)))
    return {item for item in words if item and item not in GENERIC_QUERY_TERMS}


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def hint_supported(hint: str, text: str) -> bool:
    return normalize_text(hint) in normalize_text(text)


def build_audit(
    source: Path,
    database: Path,
    converted: list[dict],
    rows: list[dict],
    *,
    reference_source: dict | None = None,
) -> dict:
    action_counts = Counter(item["expected_action"] for item in converted)
    source_action_counts = Counter(item["sourceAction"] for item in rows)
    blocking = [item for item in rows if item["blockingIssues"]]
    warnings = [item for item in rows if item["reviewWarnings"]]
    facet_coverage = {}
    for item in converted:
        for facet in item.get("required_facets", []):
            stats = facet_coverage.setdefault(facet, {"required": 0, "covered": 0, "missing": 0})
            stats["required"] += 1
            if facet not in item.get("annotation", {}).get("missingFacets", []):
                stats["covered"] += 1
            if facet in item.get("annotation", {}).get("missingFacets", []):
                stats["missing"] += 1
    return {
        "schemaVersion": 3,
        "sourceDataset": str(source),
        "sourceDatasetSha256": sha256_file(source),
        "referenceSource": reference_source or build_reference_source(database),
        "buildScriptVersion": BUILD_SCRIPT_VERSION,
        "outputDatasetSha256": hashlib.sha256(
            "".join(
                json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
                for item in converted
            ).encode("utf-8")
        ).hexdigest(),
        "outputCaseCount": len(converted),
        "actionDistribution": dict(sorted(action_counts.items())),
        "sourceActionDistribution": dict(sorted(source_action_counts.items())),
        "actionAdjustmentCount": sum(bool(item.get("action_adjustment_reason")) for item in converted),
        "requiredFacetCoverage": facet_coverage,
        "answerMetricCaseCount": sum(
            item["expected_action"] in {"ANSWER", "PARTIAL_ANSWER"} for item in converted
        ),
        "emptyReferenceCount": sum(not item["reference"].strip() for item in converted),
        "emptyReferenceContextsCount": sum(not item["reference_contexts"] for item in converted),
        "blockingIssueCount": len(blocking),
        "reviewWarningCount": len(warnings),
        "blockingCases": blocking,
        "reviewWarningCases": warnings,
        "cases": rows,
    }


def build_reference_source(database: Path) -> dict:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        documents = connection.execute(
            """
            SELECT id, source_key, canonical_key, content_hash, version, site,
                   verified_at, expires_at, chunking_profile
            FROM knowledge_documents
            WHERE status = 'ACTIVE'
            ORDER BY canonical_key, source_key
            """
        ).fetchall()
        document_ids = [int(item["id"]) for item in documents]
        chunks = connection.execute(
            """
            SELECT COALESCE(d.canonical_key, d.source_key) AS document_key,
                   c.source_index, c.content_hash, c.chunk_kind, c.chunking_profile
            FROM knowledge_chunks c
            JOIN knowledge_documents d ON d.id = c.document_id
            WHERE d.status = 'ACTIVE'
            ORDER BY document_key, c.source_index
            """
        ).fetchall()
        registry = connection.execute(
            """
            SELECT active_collection, active_signature
            FROM knowledge_index_registry
            ORDER BY logical_name
            LIMIT 1
            """
        ).fetchone()
    finally:
        connection.close()
    payload = {
        "documents": [
            {
                "key": item["canonical_key"] or item["source_key"],
                "sourceKey": item["source_key"],
                "contentHash": item["content_hash"],
                "version": item["version"],
                "site": item["site"],
                "verifiedAt": normalize_sqlite_datetime(item["verified_at"]),
                "expiresAt": normalize_sqlite_datetime(item["expires_at"]),
                "chunkingProfile": item["chunking_profile"],
            }
            for item in documents
        ],
        "chunks": [
            {
                "documentKey": item["document_key"],
                "sourceIndex": item["source_index"],
                "contentHash": item["content_hash"],
                "chunkKind": item["chunk_kind"],
                "chunkingProfile": item["chunking_profile"],
            }
            for item in chunks
        ],
    }
    corpus_hash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest = PROJECT_ROOT / "app" / "knowledge" / "knowledge_manifest.yaml"
    profiles = sorted({str(item["chunking_profile"]) for item in documents if item["chunking_profile"]})
    return {
        "type": "harness-snapshot",
        "relationalDatabaseType": "sqlite",
        "snapshotPath": str(database),
        "snapshotSha256": sha256_file(database),
        "corpusHash": corpus_hash,
        "manifestHash": sha256_file(manifest) if manifest.is_file() else "",
        "chunkingProfiles": profiles,
        "activeCollection": registry["active_collection"] if registry else None,
        "indexSignature": registry["active_signature"] if registry else None,
        "documentCount": len(documents),
        "chunkCount": len(chunks),
        "generatedAt": datetime.now(UTC).isoformat(),
    }


def build_reference_source_from_url(
    database_url: str,
    *,
    chroma_host: str,
    chroma_port: int,
    chroma_persist_dir: Path | None,
) -> dict:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            documents = connection.execute(
                text(
                    """
                    SELECT canonical_key, source_key, content_hash, version, site,
                           verified_at, expires_at, chunking_profile
                    FROM knowledge_documents
                    WHERE status = 'ACTIVE'
                    ORDER BY canonical_key, source_key
                    """
                )
            ).mappings().all()
            chunks = connection.execute(
                text(
                    """
                    SELECT COALESCE(d.canonical_key, d.source_key) AS document_key,
                           c.source_index, c.content_hash, c.chunk_kind, c.chunking_profile
                    FROM knowledge_chunks c
                    JOIN knowledge_documents d ON d.id = c.document_id
                    WHERE d.status = 'ACTIVE'
                    ORDER BY document_key, c.source_index, c.id
                    """
                )
            ).mappings().all()
            registry = connection.execute(
                text(
                    """
                    SELECT active_collection, active_signature
                    FROM knowledge_index_registry
                    ORDER BY logical_name
                    LIMIT 1
                    """
                )
            ).mappings().first()
    finally:
        engine.dispose()

    if not registry or not registry["active_collection"] or not registry["active_signature"]:
        raise ValueError("Docker MySQL 缺少 ACTIVE Chroma collection/signature")
    try:
        import chromadb

        client = (
            chromadb.PersistentClient(path=str(chroma_persist_dir))
            if chroma_persist_dir is not None
            else chromadb.HttpClient(host=chroma_host, port=chroma_port)
        )
        collection = client.get_collection(str(registry["active_collection"]))
        vector_count = int(collection.count())
    except Exception as exc:
        raise ValueError(f"无法读取 ACTIVE Chroma collection: {type(exc).__name__}") from exc

    payload = {
        "documents": [
            {
                "key": item["canonical_key"] or item["source_key"],
                "sourceKey": item["source_key"],
                "contentHash": item["content_hash"],
                "version": item["version"],
                "site": item["site"],
                "verifiedAt": normalize_sqlalchemy_datetime(item["verified_at"]),
                "expiresAt": normalize_sqlalchemy_datetime(item["expires_at"]),
                "chunkingProfile": item["chunking_profile"],
            }
            for item in documents
        ],
        "chunks": [
            {
                "documentKey": item["document_key"],
                "sourceIndex": item["source_index"],
                "contentHash": item["content_hash"],
                "chunkKind": item["chunk_kind"],
                "chunkingProfile": item["chunking_profile"],
            }
            for item in chunks
        ],
    }
    corpus_hash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest = PROJECT_ROOT / "app" / "knowledge" / "knowledge_manifest.yaml"
    return {
        "type": "mysql-active-corpus",
        "relationalDatabaseType": "mysql",
        "corpusHash": corpus_hash,
        "manifestHash": sha256_file(manifest) if manifest.is_file() else "",
        "chunkingProfiles": sorted(
            {str(item["chunking_profile"]) for item in chunks if item["chunking_profile"]}
            or {str(item["chunking_profile"]) for item in documents if item["chunking_profile"]}
        ),
        "activeCollection": str(registry["active_collection"]),
        "indexSignature": str(registry["active_signature"]),
        "documentCount": len(documents),
        "chunkCount": len(chunks),
        "vectorCount": vector_count,
        "generatedAt": datetime.now(UTC).isoformat(),
    }


def normalize_sqlalchemy_datetime(value) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value).replace(" ", "T")


def normalize_sqlite_datetime(value) -> str | None:
    if value is None:
        return None
    text = str(value).replace(" ", "T")
    return text[:-7] if text.endswith(".000000") else text


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
