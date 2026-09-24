from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml
from pypdf import PdfReader
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import KnowledgeDomain
from app.models.entities import KnowledgeChunk, KnowledgeDocument
from app.services.knowledge import chunk_text
from app.services.knowledge_ingestion.pipeline import KnowledgeIngestionPipeline


@dataclass(frozen=True)
class ImportChunk:
    section_title: str | None
    page_number: int | None
    content: str


@dataclass(frozen=True)
class SectionSpec:
    title: str
    start: int
    end: int
    logical_key: str


# 书内页码映射到 PDF 物理页时固定加 9；书内 36-45 页的重复学籍办法明确排除。
HANDBOOK_SECTIONS = (
    SectionSpec("高等学校学生行为准则", 1, 1, "rights-discipline"),
    SectionSpec("中国地质大学（武汉）学生管理规定", 25, 35, "rights-discipline"),
    SectionSpec("中国地质大学（武汉）学生学费收缴管理规定", 46, 47, "academic-learning"),
    SectionSpec("中国地质大学（武汉）本科课程修读管理办法", 48, 55, "academic-learning"),
    SectionSpec("中国地质大学（武汉）学生安全管理规定", 57, 61, "rights-discipline"),
    SectionSpec("中国地质大学（武汉）学生纪律处分办法", 62, 76, "rights-discipline"),
    SectionSpec("中国地质大学（武汉）本科生课程考核纪律与学术规范", 77, 79, "academic-learning"),
    SectionSpec("中国地质大学（武汉）大学生课堂学习规范", 80, 80, "academic-learning"),
    SectionSpec("中国地质大学（武汉）本科生请销假管理办法", 81, 82, "academic-learning"),
    SectionSpec("中国地质大学（武汉）学生宿舍住宿管理办法", 83, 86, "campus-services"),
    SectionSpec("中国地质大学（武汉）学生使用计算机网络信息管理办法", 87, 90, "campus-services"),
    SectionSpec("中国地质大学（武汉）校园飞行器使用与管理办法（暂行）", 91, 92, "campus-services"),
    SectionSpec("中国地质大学（武汉）学生申诉处理办法", 93, 97, "rights-discipline"),
    SectionSpec("中国地质大学（武汉）学生就业工作流程", 98, 104, "academic-learning"),
    SectionSpec("中国地质大学（武汉）图书馆读者须知", 105, 112, "academic-learning"),
    SectionSpec("中国地质大学（武汉）《国家学生体质健康标准》实施暂行办法", 113, 114, "academic-learning"),
    SectionSpec("中国地质大学（武汉）机动车辆通行管理暂行办法", 115, 118, "campus-services"),
    SectionSpec("中国地质大学（武汉）校园电动自行车通行管理办法（修订）", 119, 123, "campus-services"),
    SectionSpec("中国地质大学（武汉）学生人事档案管理办法", 124, 132, "campus-services"),
    SectionSpec("中国地质大学（武汉）大学生基本医疗保障实施办法", 133, 150, "campus-services"),
    SectionSpec("中国地质大学（武汉）大学生节水节电行为规范", 151, 153, "campus-services"),
    SectionSpec("中国地质大学（武汉）本科生奖励资助管理办法", 155, 162, "awards-aid-organizations"),
    SectionSpec("中国地质大学（武汉）本科生奖学金评选办法", 163, 186, "awards-aid-organizations"),
    SectionSpec("中国地质大学（武汉）本科生困难认定与助学金办法", 187, 221, "awards-aid-organizations"),
    SectionSpec("中国地质大学（武汉）大学生科技与社会实践管理办法", 223, 231, "academic-learning"),
    SectionSpec("中国地质大学（武汉）学生社团活动管理办法", 232, 235, "awards-aid-organizations"),
    SectionSpec("中国地质大学（武汉）志愿服务与美育实践管理办法", 236, 244, "academic-learning"),
)


HANDBOOK_CONTACT_DIRECTORY_CHUNKS = (
    ImportChunk(
        section_title="咨询服务电话及地点",
        page_number=254,
        content=(
            "《学生手册（2025年版）》第245页咨询服务电话及地点。校园安全与校区服务："
            "南望山校区校园110，电话67883110；未来城校区校园110，电话65277110；"
            "南望山校区服务，电话67885110；未来城校区服务，电话65278110。"
            "安全保卫部户政科，电话67883117，地点西区安全保卫部。"
        ),
    ),
    ImportChunk(
        section_title="咨询服务电话及地点",
        page_number=254,
        content=(
            "《学生手册（2025年版）》第245页咨询服务电话及地点。本科生院（党委学生工作部）："
            "信息化与综合事务办公室，电话67885005，地点行政楼325；招生与学籍管理办公室，"
            "电话67883171，地点新峰公寓102；学生思想政治教育办公室（含军事教研室），"
            "电话67883243，地点行政楼329；学生奖励资助与发展中心办理国家奖学金、国家励志奖学金、"
            "国家助学金、地大英才奖学金、助学贷款、基层就业学费补偿代偿、退役士兵资助、勤工助学和日常奖励，"
            "电话67883443，地点新峰公寓105；办理英才工程、社会类奖学金、家庭经济困难认定和临时困难补助，"
            "电话67886490，地点新峰公寓106；教学运行服务办公室，电话67885010、67885007，地点行政楼315；"
            "教育改革与专业建设办公室，电话67884019、67885006，地点行政楼305；实践教学管理办公室，"
            "电话67885009，地点行政楼313；创新创业工作办公室，电话67848554，地点行政楼301；"
            "心理健康教育中心，电话67883678，地点心理中心210。"
        ),
    ),
    ImportChunk(
        section_title="咨询服务电话及地点",
        page_number=254,
        content=(
            "《学生手册（2025年版）》第245页咨询服务电话及地点。就业、团委与财务服务："
            "学生就业指导处就业资源拓展中心，电话67883307，地点新峰公寓109；就业服务管理中心，"
            "电话67883308，地点新峰公寓108；职业发展指导中心，电话67883870，地点新峰公寓306。"
            "校团委，电话67883317，地点大学生活动中心。财务与资产管理部酬金查询，电话67885058，"
            "地点行政楼一楼报账大厅。"
        ),
    ),
    ImportChunk(
        section_title="咨询服务电话及地点",
        page_number=254,
        content=(
            "《学生手册（2025年版）》第245页咨询服务电话及地点。住宿、邮政、网络、医疗与社区服务："
            "后勤保障部学生住宿服务中心，电话67886371，地点西区56栋后；邮政服务部，电话67885149，"
            "地点南望山庄邮局旁；迎宾楼，电话67883813-0，地点东区。信息化工作办公室校园网络维护，"
            "电话67885175，地点东区南望厅13/14号窗口。校医院，电话67883767，地点东区；医保咨询，"
            "电话67885891，地点东区。地大社区，电话67885825，地点东区。"
        ),
    ),
)


HOUSING_FLOWS = (
    ImportChunk("调宿办理流程", 2, "1. 下载并填写《学生宿舍调整申请表》。2. 学院学工组签字盖章。3. 学生住宿服务中心核实床位和住宿费并签字盖章。4. 原宿舍楼管员清点家具、回收钥匙并确认搬离。5. 拟入住楼管员交接床位物品并发放钥匙。6. 学生住宿服务中心办理门禁权限变更和住宿费异动。7. 完成。"),
    ImportChunk("入住办理流程", 3, "新生先完成网上报到和选房或接受学校安排，再持录取通知书到住宿楼栋报到。在校生下载并填写入住申请材料，由学院学工组签字盖章，学生住宿服务中心核实床位和住宿费。楼管员清点物品并发放钥匙，住宿服务中心录入门禁权限和住宿费异动。"),
    ImportChunk("退宿办理流程", 4, "校外居住退宿需填写申请表和安全承诺，结清水电费，经学院核实后由住宿服务中心接收材料；楼管员清查家具、回收钥匙，随后取消门禁权限并办理住宿费异动。毕业退宿按离校流程提交承诺书、完成物品和钥匙交接。"),
    ImportChunk("入住调宿退宿线上办理流程", 5, "登录学校官网信息门户，进入服务中心的后勤安保栏目，选择南望山校区宿舍调整、退宿申请或入住申请并在线办理。具体入口和可办理状态以学校当前官方页面为准。"),
)


class KnowledgeManifestImporter:
    def __init__(self, db: Session, settings: Settings, manifest_path: Path | None = None):
        self.db = db
        self.settings = settings
        self.root = settings.project_root.resolve()
        self.manifest_path = manifest_path or self.root / "app" / "knowledge" / "knowledge_manifest.yaml"
        self.pipeline = KnowledgeIngestionPipeline(db, settings)

    def load_manifest(self) -> list[dict]:
        payload = yaml.safe_load(self.manifest_path.read_text(encoding="utf-8")) or {}
        if int(payload.get("manifest_version") or 0) <= 0:
            raise ValueError("knowledge manifest_version 必填")
        documents = payload.get("documents", [])
        if not isinstance(documents, list):
            raise ValueError("knowledge manifest documents must be a list")
        required = {
            "source_key",
            "canonical_key",
            "title",
            "source_type",
            "domain",
            "status",
            "version",
            "raw_path",
            "expected_sha256",
            "cleaning_strategy",
        }
        source_keys: set[str] = set()
        canonical_keys: set[str] = set()
        valid_domains = {item.value for item in KnowledgeDomain}
        for index, spec in enumerate(documents):
            if not isinstance(spec, dict) or required - set(spec):
                missing = sorted(required - set(spec or {}))
                raise ValueError(f"knowledge manifest 第 {index + 1} 项缺少字段：{missing}")
            source_key = str(spec["source_key"]).strip()
            canonical_key = str(spec["canonical_key"]).strip()
            if not source_key or source_key in source_keys:
                raise ValueError(f"knowledge manifest source_key 重复或为空：{source_key}")
            if not canonical_key or canonical_key in canonical_keys:
                raise ValueError(f"knowledge manifest canonical_key 重复或为空：{canonical_key}")
            if str(spec["domain"]) not in valid_domains:
                raise ValueError(f"knowledge manifest domain 非法：{spec['domain']}")
            source_keys.add(source_key)
            canonical_keys.add(canonical_key)
        return documents

    def import_all(self, rebuild_index: bool = False) -> dict:
        if rebuild_index:
            raise ValueError("语料导入不再在线重建向量索引；请运行 scripts/manage_knowledge_index.py build")
        manifest = yaml.safe_load(self.manifest_path.read_text(encoding="utf-8")) or {}
        if manifest.get("corpus") == "handbook_prechunked_v2":
            from app.services.handbook_import import HandbookImporter

            return HandbookImporter(self.db, self.settings).import_all()
        specs = self.load_manifest()
        imported = []
        verified_files = {}
        try:
            for spec in specs:
                path = self._resolve_raw_path(str(spec["raw_path"]))
                actual_hash = hashlib.sha256(path.read_bytes()).hexdigest().upper()
                expected_hash = str(spec["expected_sha256"]).upper()
                if actual_hash != expected_hash:
                    raise ValueError(f"知识原始文件哈希不匹配：{spec['raw_path']}")
                verified_files[str(spec["raw_path"])] = actual_hash
                chunks = self._extract(spec, path)
                imported.append(self._upsert(spec, chunks, path, actual_hash))
            archived = self._archive_removed_manifest_documents(specs)
            self.db.flush()
            self._validate_manifest_bootstrap(specs)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return {
            "verified_files": verified_files,
            "documents": imported,
            "archived": archived,
            "manifest_count": len(specs),
        }

    def managed_raw_paths(self) -> set[str]:
        return {str(item["raw_path"]).replace("\\", "/") for item in self.load_manifest()}

    def _resolve_raw_path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError(f"Manifest 路径越界：{relative}")
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    def _extract(self, spec: dict, path: Path) -> list[ImportChunk]:
        strategy = spec["cleaning_strategy"]
        if strategy == "legacy_markdown":
            return _legacy_markdown_chunks(path.read_text(encoding="utf-8"), self.settings)
        if strategy == "markdown_headings":
            return _markdown_chunks(path.read_text(encoding="utf-8"), self.settings)
        if strategy == "handbook_sections":
            return _handbook_chunks(
                path,
                str(spec["logical_key"]),
                self.settings,
                spec.get("page_mapping"),
            )
        if strategy == "pdf_pages":
            return _pdf_page_chunks(path, spec["title"], self.settings)
        if strategy == "housing_flows":
            return list(HOUSING_FLOWS)
        raise ValueError(f"未知知识清洗策略：{strategy}")

    def _upsert(self, spec: dict, chunks: list[ImportChunk], path: Path, actual_hash: str) -> dict:
        result = self.pipeline.ingest_manifest_legacy(
            spec=spec,
            chunks=chunks,
            path=path,
            sha256=actual_hash,
        )
        return {
            "source_key": result.source,
            "canonical_key": str(spec["canonical_key"]),
            "chunks": result.chunk_count,
            "changed": result.content_changed or result.metadata_changed,
            "content_changed": result.content_changed,
            "metadata_changed": result.metadata_changed,
            "status": result.status,
        }

    def _validate_manifest_bootstrap(self, specs: list[dict]) -> None:
        for spec in specs:
            rows = (
                self.db.query(KnowledgeDocument)
                .filter(KnowledgeDocument.source_key == str(spec["source_key"]))
                .all()
            )
            if len(rows) != 1:
                raise ValueError(f"Manifest 所有权 bootstrap 匹配数量异常：{spec['source_key']}")
            row = rows[0]
            if row.canonical_key != str(spec["canonical_key"]) or row.managed_by != "MANIFEST":
                raise ValueError(f"Manifest 所有权 bootstrap 未完成：{spec['source_key']}")
        active_rows = (
            self.db.query(KnowledgeDocument)
            .filter(KnowledgeDocument.status == "ACTIVE", KnowledgeDocument.canonical_key.is_not(None))
            .all()
        )
        seen: dict[str, int] = {}
        for row in active_rows:
            key = str(row.canonical_key)
            if key in seen and seen[key] != row.id:
                raise ValueError(f"同一 canonical_key 存在多个 ACTIVE 文档：{key}")
            seen[key] = row.id

    def _archive_removed_manifest_documents(self, specs: list[dict]) -> list[dict]:
        active_source_keys = {str(spec["source_key"]) for spec in specs}
        rows = (
            self.db.query(KnowledgeDocument)
            .filter(KnowledgeDocument.managed_by == "MANIFEST")
            .filter(~KnowledgeDocument.source_key.in_(active_source_keys))
            .all()
        )
        archived = []
        for row in rows:
            if row.status == "INACTIVE":
                continue
            archived.append(
                {
                    "document_id": row.id,
                    "source_key": row.source_key,
                    "canonical_key": row.canonical_key,
                    "previous_status": row.status,
                    "new_status": "INACTIVE",
                }
            )
            row.status = "INACTIVE"
        return archived


def _markdown_chunks(text: str, settings: Settings) -> list[ImportChunk]:
    current_title = None
    buffer = []
    result = []

    def flush():
        if not buffer:
            return
        for value in stable_chunk_text(
            "\n".join(buffer), settings.knowledge_chunk_size, settings.knowledge_chunk_overlap
        ):
            result.append(ImportChunk(current_title, None, value))
        buffer.clear()

    for line in text.splitlines():
        if line.startswith("#"):
            flush()
            current_title = line.lstrip("#").strip()
        elif line.strip():
            buffer.append(line.strip())
    flush()
    return result


def _legacy_markdown_chunks(text: str, settings: Settings) -> list[ImportChunk]:
    return [
        ImportChunk(None, None, content)
        for content in chunk_text(text, settings.knowledge_chunk_size, settings.knowledge_chunk_overlap)
    ]


def _handbook_chunks(
    path: Path,
    logical_key: str,
    settings: Settings,
    page_mapping: dict | None = None,
) -> list[ImportChunk]:
    reader = PdfReader(str(path))
    mapping = page_mapping or {}
    if mapping.get("type") != "offset":
        raise ValueError("学生手册必须配置 offset page_mapping")
    offset = int(mapping.get("physical_page_offset", 0))
    expected_pages = int(mapping.get("expected_physical_pages", 0))
    if expected_pages and len(reader.pages) != expected_pages:
        raise ValueError(f"学生手册物理页数校验失败：expected={expected_pages}, actual={len(reader.pages)}")
    for anchor in mapping.get("anchors", []):
        physical_page = int(anchor["logical_page"]) + offset
        if not 1 <= physical_page <= len(reader.pages):
            raise ValueError(f"学生手册页码锚点越界：{physical_page}")
        text = reader.pages[physical_page - 1].extract_text() or ""
        if str(anchor["contains"]) not in text:
            raise ValueError(f"学生手册页码锚点校验失败：logical_page={anchor['logical_page']}")
    result = []
    for section in HANDBOOK_SECTIONS:
        if section.logical_key != logical_key:
            continue
        for internal_page in range(section.start, section.end + 1):
            physical_page = internal_page + offset
            text = _clean_pdf_text(reader.pages[physical_page - 1].extract_text() or "")
            for content in stable_chunk_text(text, settings.knowledge_chunk_size, settings.knowledge_chunk_overlap):
                if len(content) >= 30:
                    result.append(ImportChunk(section.title, physical_page, content))

    if logical_key == "campus-services":
        result.extend(HANDBOOK_CONTACT_DIRECTORY_CHUNKS)

    return result


def _pdf_page_chunks(path: Path, title: str, settings: Settings) -> list[ImportChunk]:
    result = []
    current_title = title
    for page_number, page in enumerate(PdfReader(str(path)).pages, start=1):
        text = _clean_pdf_text(page.extract_text() or "")
        chapter = re.search(r"第[一二三四五六七八九十]+章\s*[^\n]{1,20}", text)
        if chapter:
            current_title = chapter.group(0).strip()
        for content in stable_chunk_text(text, settings.knowledge_chunk_size, settings.knowledge_chunk_overlap):
            if len(content) >= 30:
                result.append(ImportChunk(current_title, page_number, content))
    return result


def _clean_pdf_text(text: str) -> str:
    lines = []
    for raw in text.replace("\x00", "").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line or re.fullmatch(r"-?\s*\d{1,3}\s*-?", line) or line == "中国地质大学学生手册":
            continue
        line = re.sub(r"^(问[：:])\s*\1", r"\1", line)
        line = re.sub(r"^(答[：:])\s*\1", r"\1", line)
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    return "\n".join(lines)


def stable_chunk_text(content: str, size: int, overlap: int) -> list[str]:
    paragraphs = [
        re.sub(r"\s+", " ", paragraph).strip()
        for paragraph in re.split(r"\n\s*\n|(?<=[。！？；])\s+", content or "")
        if paragraph.strip()
    ]
    if not paragraphs:
        return []
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > size:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(chunk_text(paragraph, size, overlap))
            continue
        candidate = f"{current}\n{paragraph}".strip() if current else paragraph
        if current and len(candidate) > size:
            chunks.append(current)
            retained = current[-max(0, overlap) :] if overlap else ""
            current = f"{retained} {paragraph}".strip()
        else:
            current = candidate
    if current:
        chunks.append(current)
    return [_normalize(item) for item in chunks if _normalize(item)]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _parse_datetime(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
