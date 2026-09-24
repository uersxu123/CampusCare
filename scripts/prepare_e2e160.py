"""建立独立评测候选，只读取语料；不运行被测模型、不改索引。"""
from collections import Counter
from datetime import datetime, UTC
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.core.database import SessionLocal
from app.models.entities import KnowledgeDocument, KnowledgeChunk
from app.services.knowledge_scoring import KnowledgeTokenizer, bm25f_scores
from app.evaluation.reporting.fingerprint import fingerprint_active_corpus

SOURCE = ROOT / "benchmarks/evidence_facets_multiquery_20260913/realistic_student_queries_200.jsonl"
OUT = ROOT / "target/verification/e2e160-package-20260918"


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    source = [json.loads(line) for line in SOURCE.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(source) == 200
    # 固定等距抽样覆盖整个题集；不用旧模型结果筛题。
    selected = [source[((2 * index + 1) * 200) // (2 * 144)] for index in range(144)]
    tokenizer = KnowledgeTokenizer()
    with SessionLocal() as db:
        documents = db.query(KnowledgeDocument).filter(KnowledgeDocument.status == "ACTIVE").all()
        chunks = db.query(KnowledgeChunk).filter(KnowledgeChunk.document_id.in_([doc.id for doc in documents]), KnowledgeChunk.chunk_kind != "SECTION_PARENT").all()
        chunks = [chunk for chunk in chunks if "PARENT" not in chunk.chunk_kind]
        candidates = []
        for row in selected:
            pool = [chunk for chunk in chunks if chunk.document.source_key.removeprefix("handbook-v2:") in row["expected_document_ids"]]
            scores = bm25f_scores(row["query"], pool, tokenizer)
            ranked = sorted(pool, key=lambda chunk: (-scores.get(chunk.id, 0), chunk.id))
            # 每篇来源先取最相关的两块，供后续逐题审阅，不当作自动可信 Gold。
            chosen = []
            for doc_id in row["expected_document_ids"]:
                chosen.extend([chunk for chunk in ranked if chunk.document.source_key == "handbook-v2:" + doc_id][:2])
            candidates.append({**row, "candidateEvidence": [{"id": chunk.id, "documentKey": chunk.document.source_key, "title": chunk.document.title, "content": chunk.content, "contentHash": chunk.content_hash} for chunk in chosen]})
        fingerprint = fingerprint_active_corpus(db, manifest_path=ROOT / "app/knowledge/knowledge_manifest.yaml").as_dict()
        write(OUT / "corpus-snapshot.json", {"fingerprint": fingerprint, "documents": [{"key": doc.source_key, "title": doc.title} for doc in documents], "children": [{"id": chunk.id, "documentKey": chunk.document.source_key, "content": chunk.content, "contentHash": chunk.content_hash} for chunk in chunks]})
    write(OUT / "candidates.json", candidates)
    write(OUT / "discovery.json", {"createdAt": datetime.now(UTC).isoformat(), "sourcePath": str(SOURCE.relative_to(ROOT)), "sourceSha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(), "sourceCount": 200, "selectionRule": "index=floor((2*i+1)*200/(2*144)), i=0..143; source order; no model-result selection", "selectedIds": [row["id"] for row in selected], "plannedComposition": "144 current-corpus retrieval questions + existing 13 business / 3 safety Smoke = 160", "issues": ["160 条命名文件实际是阈值校准集，缺少 E2E Gold", "181 条旧 E2E 引用旧语料；不能替换指纹后强跑", "当前 200 条是文档级检索集，全部 ANSWER、全部 CAMPUS 标签不能直接作为当前 E2E Gold", "必须补充事实引用、当前路由和工具契约，并逐题处理证据不足"]})
    print(json.dumps({"selected": len(candidates), "children": len(chunks), "fingerprint": fingerprint}, ensure_ascii=False))


if __name__ == "__main__":
    main()
