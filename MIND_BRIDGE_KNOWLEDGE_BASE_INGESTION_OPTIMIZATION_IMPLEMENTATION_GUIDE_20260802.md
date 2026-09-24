# MindBridge 知识库解析、分割与管理优化实施指南

> 文档状态：可直接交给 AI 编码代理分阶段实施
>
> 编制日期：2026-08-02
>
> 适用项目：`mindbridge-py`
>
> 目标版本：Knowledge Ingestion V2
>
> 本文范围：文档治理、文件解析、Document IR、结构化分割、表格处理、管理员导入、索引发布与评测

## 1. 使用说明

本文是代码改造规范，不是概念性建议。执行改造的 AI 必须先阅读全文，再按照阶段顺序实施。不得一次性重写 `KnowledgeService`，不得只替换 PDF 库后宣称知识库改造完成，也不得绕过现有索引注册表直接覆盖线上 Chroma collection。

开始编码前必须：

1. 阅读项目根目录 `AGENTS.md`（如存在）、`README.md`、本文件和所有将修改的源文件。
2. 运行 `git status --short`。当前工作区可能包含用户未提交修改，必须保留并在其基础上工作，不得回滚或覆盖无关改动。
3. 记录相关测试基线，优先补充失败测试，再修改生产代码。
4. 每次只实施本文的一个阶段；当前阶段测试通过后才能进入下一阶段。
5. 所有源文件保存为 UTF-8，优先 UTF-8 无 BOM；中文必须直接可读，禁止改成 `\uXXXX`。
6. 数据库变更只允许通过新的 Alembic migration 完成，不得修改历史 migration。
7. 不在日志、指标和异常 trace 中记录完整上传文档正文、表格内容或用户隐私数据。
8. 引入 Docling、MinerU、PaddleOCR 等重型依赖前必须先做样本文档基准测试，不得直接加入默认生产依赖。

## 2. 实施结论

本项目不需要替换成 RAGFlow 或 Dify。应保留现有业务编排、BM25F、可选 BGE-M3/Chroma、RRF、重排、证据评分和索引注册表，只重构当前薄弱的入库链路。

目标链路为：

```text
Git Manifest 文档 ─┐
                    ├─> 统一文档目录 -> 原始文件存储 -> 解析器路由 -> Document IR
管理员上传文档 ────┘                                             |
                                                                  v
                                                        质量门禁与人工预览
                                                                  |
                                                                  v
                                                        结构化父子分割
                                                                  |
                              ┌───────────────────────────────────┴─────────────────────┐
                              v                                                         v
                    BM25/向量检索索引                                         表格结构化索引
                              |                                                         |
                              └──────────────────────> 统一证据与引用 <─────────────────┘
```

“统一纳入 Manifest”不得实现为让管理员编辑 Git YAML。正确方案是双来源注册、同一治理契约：

- Git Manifest 管理固定、可版本控制的内置知识。
- 数据库 Catalog 管理管理员上传的动态知识。
- 两者统一使用 `source_key`、`canonical_key`、版本、哈希、解析配置、分割配置、审核状态和索引发布流程。
- 管理员继续可以上传、查看解析结果、调整分割配置、重新处理和发布文档。

## 3. 当前实现基线

### 3.1 当前入口

| 来源 | 入口 | 实际处理 |
|---|---|---|
| 管理员粘贴文本 | `POST /api/admin/knowledge` | 直接调用 `KnowledgeService.ingest()` |
| 管理员上传文件 | `POST /api/admin/knowledge/file` | PDF 用 `pypdf` 展平；其他文件按 UTF-8 解码 |
| Manifest | `KnowledgeManifestImporter.import_all()` | 根据 `cleaning_strategy` 选择专用逻辑 |
| 启动兼容导入 | `app/core/bootstrap.py` | 扫描未被 Manifest 管理的 Markdown，走旧滑窗分割 |

当前管理员上传记录使用：

```text
managed_by = UPLOAD
source_type = UPLOAD
status = DRAFT
domain = MENTAL_HEALTH
site = ALL
version = 1
```

这意味着“上传成功”只表示数据库已经生成 Chunk，不等于文档已经审核并进入线上 ACTIVE 检索。后续 API 和界面必须明确显示这两个状态。

### 3.2 当前普通滑窗算法

配置位于 `app/core/config.py`：

```text
knowledge_chunk_size = 512
knowledge_chunk_overlap = 64
```

单位是 Python 字符数，不是 Token。`app/services/knowledge.py::chunk_text()` 的行为是：

1. 使用 `re.sub(r"\s+", " ", text)` 把换行和连续空白压成一个空格。
2. 每块截取 512 个字符。
3. 步长为 `512 - 64 = 448`，相邻块固定重叠 64 个字符。
4. 不识别句子、段落、标题、条款、列表、页面或表格边界。
5. 最后一块允许很短。

管理员上传、管理员粘贴文本和启动时兼容导入的旧 Markdown 都走该算法。

### 3.3 当前 Manifest 分割算法

`app/services/knowledge_import.py::stable_chunk_text()` 的行为是：

1. 先按空行，或 `。！？；` 后紧跟的空白切成段落。
2. 段落内部空白归一化。
3. 贪心合并段落，目标约 512 字符。
4. 加入下一段会超长时，输出当前块，并把当前块末尾 64 个字符拼入下一块。
5. 单段超过 512 字符时，退回普通字符滑窗。

该算法存在以下边界：

- 重叠保留的是任意 64 个字符，可能从句子或条款中间开始。
- “64 字符尾部 + 下一完整段落”可能生成超过 512 字符的块，理论上接近 577 字符。
- 英文句号、冒号、列表项和编号条款不是可靠边界。
- 分割长度与 Embedding 模型的 Token 长度没有直接关系。

Manifest 的具体预处理如下：

- Markdown：行首 `#` 触发章节切换，只保留当前标题，不保留完整标题路径。
- 学生手册 PDF：使用人工 `HANDBOOK_SECTIONS`，书内页码固定加 9，逐页提取。
- 普通 PDF：逐页提取，只识别简单的 `第X章` 模式。
- 住宿流程：绕过 PDF 自动解析，直接使用人工整理的 `HOUSING_FLOWS`。
- PDF Chunk 小于 30 字符时直接丢弃。

### 3.4 当前数据模型能力

`KnowledgeDocument` 已有来源、领域、标签、站点、状态、版本、验证时间、失效时间和内容哈希，具备基础治理能力。

`KnowledgeChunk` 当前只有：

```text
document_id
source
source_index
section_title
page_number
content
content_hash
```

当前无法表达：

- 标题层级路径；
- 元素类型；
- 页面坐标与阅读顺序；
- 表格、表头、单元格和合并关系；
- 图片和公式引用；
- 解析器名称、版本、置信度；
- Chunk 使用的分割配置；
- 父块与子块关系；
- 原始文件与处理产物的位置；
- 解析失败和质量门禁结果。

### 3.5 必须保留的现有能力

改造不得破坏：

1. `KnowledgeService.search()` 的对外调用方式和 `SearchResult` 证据语义。
2. BM25F、可选向量召回、RRF、重排和 Evidence Grader。
3. `KnowledgeIndexRegistry` 的 READY、ACTIVE、PREVIOUS 构建、验证、激活和回滚流程。
4. Manifest 文件哈希校验和幂等导入。
5. `status=DRAFT/ACTIVE/INACTIVE` 的检索过滤语义。
6. 文档来源、页码和章节标题在最终引用中的可追溯性。
7. 当前管理员上传 API 的兼容期，不允许第一阶段直接删除旧接口。

## 4. 目标领域模型

### 4.1 状态必须拆成两个维度

不得继续使用一个 `status` 同时表达处理进度和发布状态。

发布状态沿用：

```text
DRAFT -> ACTIVE -> INACTIVE
```

新增处理状态 `ingestion_status`：

```text
PENDING -> PARSING -> PARSED -> CHUNKED -> READY
                    \-> FAILED
```

规则：

- `READY` 只说明解析、质量检查和分割成功。
- 只有 `status=ACTIVE` 且索引版本已激活的文档可以作为线上证据。
- 重新解析 ACTIVE 文档时，旧版本继续服务；新版本进入 READY 后再原子切换。
- 失败不得删除当前 ACTIVE 版本。

### 4.2 统一 Document IR

新增 `app/services/knowledge_ingestion/models.py`，定义与具体解析器无关的中间结构。建议使用冻结 dataclass 或 Pydantic model，禁止在解析器之间传递无约束 `dict`。

```python
@dataclass(frozen=True)
class BoundingBox:
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True)
class DocumentElement:
    element_index: int
    element_type: str
    content: str
    page_number: int | None
    bbox: BoundingBox | None
    heading_path: tuple[str, ...]
    parent_index: int | None
    metadata: Mapping[str, object]


@dataclass(frozen=True)
class ParsedDocument:
    title: str
    elements: tuple[DocumentElement, ...]
    parser_name: str
    parser_version: str
    warnings: tuple[str, ...]
    quality: Mapping[str, float]
```

首批合法 `element_type`：

```text
TITLE
HEADING
PARAGRAPH
LIST_ITEM
TABLE
FIGURE
FORMULA
PAGE_HEADER
PAGE_FOOTER
```

未知类型允许映射为 `PARAGRAPH`，但必须在 warning 中记录，不得直接丢弃正文。

### 4.3 数据库建议

实施前先确认 Alembic 当前唯一 head。预计新增 `0012_knowledge_ingestion_v2.py`，但如果实施时已有 0012，则顺延编号，不得制造并行 head。

新增表：

#### `knowledge_artifacts`

```text
id
document_id
storage_key
original_filename
mime_type
byte_size
sha256
created_at
```

用途：保存上传原件或 Manifest 原始文件的可追踪引用。数据库不直接存大文件正文。

#### `knowledge_elements`

```text
id
document_id
artifact_id
element_index
element_type
page_number
bbox_json
heading_path_json
parent_element_id
content
content_hash
metadata_json
parser_name
parser_version
confidence
```

唯一约束：`(document_id, element_index)`。

#### `knowledge_tables`

```text
id
document_id
element_id
page_number
caption
headers_json
rows_json
table_html
schema_json
content_hash
```

不得只保存模型生成的表格摘要；原始结构化行列必须保留。

扩展 `knowledge_documents`：

```text
ingestion_status
parser_profile
chunking_profile
parser_version
last_ingestion_error
active_revision
```

扩展 `knowledge_chunks`：

```text
chunk_kind
parent_chunk_id
heading_path_json
element_ids_json
token_count
chunking_profile
```

建议 `chunk_kind`：

```text
TEXT_CHILD
TEXT_PARENT
TABLE_SUMMARY
TABLE_ROW
```

迁移兼容规则：

- 旧文档回填 `ingestion_status=READY`、`parser_profile=legacy`、`chunking_profile=legacy_char_v1`。
- 旧 Chunk 回填 `chunk_kind=TEXT_CHILD`，其他新增字段允许为空。
- 迁移不得重新分割现有文档；重新解析必须作为独立运维动作。
- migration 的 downgrade 只删除新增结构，不得删除原有知识文档和 Chunk。

## 5. 模块边界

新增包：

```text
app/services/knowledge_ingestion/
  __init__.py
  models.py              # IR、配置、结果和错误类型
  catalog.py             # 双来源文档目录和状态转换
  artifact_store.py      # 原始文件持久化、哈希和安全文件名
  parser_registry.py     # MIME/配置到解析器的路由
  quality.py             # 解析质量门禁
  pipeline.py            # 事务边界和全流程编排
  chunker.py             # structure_token_v2
  tables.py              # 表格规范化和派生 Chunk
  parsers/
    base.py
    plaintext.py
    markdown.py
    pypdf_parser.py
    docling_parser.py     # 可选适配器，依赖不可用时明确降级
```

现有文件职责调整：

| 文件 | 改造要求 |
|---|---|
| `app/services/knowledge.py` | 保留检索；旧 `ingest*` 变成兼容包装，不再包含解析细节 |
| `app/services/knowledge_import.py` | 保留 Manifest 验证和专用配置，调用统一 pipeline |
| `app/services/knowledge_index.py` | 接收新 Chunk 字段，保持 READY/ACTIVE 发布语义 |
| `app/models/entities.py` | 增加实体和关系，不重命名现有表 |
| `app/schemas/dtos.py` | 增加导入任务、预览、发布和重处理 DTO |
| `app/api/routes.py` | 增加管理 API，保留旧 API 兼容包装 |
| `app/static/admin.html` | 增加元数据、状态、预览和发布操作 |
| `app/static/admin.js` | 展示解析警告、Chunk 预览和任务状态 |
| `app/core/bootstrap.py` | 最终移除未登记 Markdown 的隐式导入，先加兼容开关和告警 |

禁止创建第二套检索服务。入库和检索可以拆模块，但数据库中的 ACTIVE `KnowledgeChunk` 仍由现有 `KnowledgeService.search()` 统一查询。

## 6. 解析器策略

### 6.1 统一接口

`parsers/base.py` 定义：

```python
class DocumentParser(Protocol):
    name: str
    version: str

    def supports(self, mime_type: str, filename: str) -> bool: ...

    def parse(self, artifact: StoredArtifact, profile: ParserProfile) -> ParsedDocument: ...
```

解析器不得直接写数据库、生成向量或改变文档发布状态。Pipeline 负责事务和状态机。

### 6.2 Markdown 和纯文本

Markdown 不再逐行判断 `line.startswith("#")`，应使用 Markdown AST 或可靠解析库，至少保留：

- H1-H6 完整路径；
- 段落；
- 有序和无序列表；
- 表格；
- 代码块边界。

纯文本解析器可以按空行和句子生成段落元素，但必须保留原始段落顺序。

### 6.3 PDF 路由

默认 `parser_profile=auto`：

1. 先使用 `pypdf` 快速提取数字 PDF。
2. 计算每页可见字符数、空页比例、乱码比例、重复页眉页脚比例和阅读顺序异常。
3. 简单数字 PDF 达到质量门禁时使用结果。
4. 表格密集、双栏、复杂布局或质量门禁失败时，尝试 Docling。
5. 扫描 PDF 进入 OCR 适配器；中文样本先比较 MinerU 与 PaddleOCR，再决定默认实现。
6. 可选依赖不可用时，文档进入 `FAILED` 或 `PARSED_WITH_WARNINGS`，不得静默产生空知识。

保留 `pypdf`，因为它对简单数字 PDF 速度快、依赖轻。Docling/MinerU/PaddleOCR 是增强路径，不是无条件替换。

### 6.4 页眉页脚与阅读顺序

重复页眉页脚必须基于多页统计识别，不能继续只硬编码“中国地质大学学生手册”。清洗结果保存在 IR，但原始解析产物必须可追踪。

学生手册固定 `+9` 偏移应迁移为 Manifest 中显式、可验证的 `page_mapping` 配置。若目录或锚点校验失败，导入失败并给出具体错误，不能继续使用错误页码。

## 7. 结构化分割 V2

### 7.1 配置

保留旧配置用于 `legacy_char_v1` 回放，新增配置建议：

```text
KNOWLEDGE_CHUNKING_PROFILE=structure_token_v2
KNOWLEDGE_CHILD_TARGET_TOKENS=320
KNOWLEDGE_CHILD_MAX_TOKENS=480
KNOWLEDGE_CHILD_MIN_TOKENS=80
KNOWLEDGE_PARENT_MAX_TOKENS=1200
KNOWLEDGE_OVERLAP_TOKENS=48
```

数值是初始基线，不是永久常量。最终值必须通过本项目评测集确定。

TokenCounter 必须是可注入接口，并记录 tokenizer 名称和版本。优先使用与 Embedding 模型兼容的 tokenizer；测试中使用确定性 fake counter。不得把 Python 字符数标记为 Token 数。

### 7.2 分割优先级

边界优先级从高到低：

```text
文档
  -> 标题路径
    -> 页面或条款组
      -> 表格 / 列表 / 段落
        -> 完整句子
          -> 最后的安全长度回退
```

必须遵守：

1. 不跨一级制度章节合并。
2. 尽量不跨标题路径合并。
3. 编号条款、列表项和表格行不能从中间截断。
4. 重叠使用完整句子或完整元素，不再复制任意尾部字符。
5. 超长段落先按句子切；超长句再按标点；最后才允许安全长度回退。
6. 每个 Chunk 保存对应 `element_ids`、标题路径、页码集合和分割配置版本。
7. `content` 保存干净正文；用于索引的 `search_text` 可以拼接文档标题、标题路径和标签，但不得污染最终引用正文。

### 7.3 父子块

- 子块约 220～480 Token，用于 BM25 和向量召回。
- 父块最多约 1200 Token，用于命中后的上下文扩展。
- 子块保存 `parent_chunk_id`。
- 检索排序基于子块；提交给 Evidence Grader 前按父块合并和去重。
- 同一父块多个子块命中时只能计算一次上下文预算，避免 Prompt 重复。

现有相邻 `source_index ± 1` 扩展可以在兼容期保留，但 V2 文档优先使用明确的父块关系。

### 7.4 伪代码

```python
def chunk_document(parsed, profile, token_counter):
    groups = group_by_heading_and_semantic_boundary(parsed.elements)
    children = []

    for group in groups:
        units = to_atomic_units(group)  # 段落、条款、列表项、表格行、完整句子
        current = []

        for unit in units:
            if fits(current, unit, profile.child_max_tokens):
                current.append(unit)
                continue

            if current:
                children.append(build_child(current))
            current = whole_unit_overlap(current, profile.overlap_tokens)

            for safe_unit in split_oversized_unit(unit, profile, token_counter):
                if not fits(current, safe_unit, profile.child_max_tokens):
                    children.append(build_child(current))
                    current = whole_unit_overlap(current, profile.overlap_tokens)
                current.append(safe_unit)

        if current:
            children.append(build_child(current))

    parents = build_parent_chunks(children, profile.parent_max_tokens)
    return link_children_to_parents(children, parents)
```

## 8. PDF 表格处理

表格不能只转换成一段 Markdown 后与普通段落一起向量化。每张表生成三种数据：

1. 原始结构：HTML/JSON，包含表头、行、单元格、合并关系、单位、页码和 bbox。
2. `TABLE_SUMMARY`：表名、主题、字段、时间范围和单位，用于发现相关表。
3. `TABLE_ROW`：每行重复表名和表头，用于语义召回。

示例：

```text
表：2025 年奖学金评定标准
列：奖项=一等奖学金；比例=5%；金额=3000 元；适用对象=本科生
来源：第 18 页，表 2，第 3 行
```

精确问题必须走结构化路径：

- “一等奖和二等奖相差多少钱”需要读取类型化单元格后计算。
- “比例高于 10% 的项目有哪些”需要受限过滤。
- “这张表主要讲什么”可以使用表格摘要和普通 RAG。

结构化查询必须：

- 只访问知识表白名单；
- 只允许 SELECT、过滤、排序和受限聚合；
- 强制 `document_id/table_id` 范围；
- 限制返回行数和执行时间；
- 返回文档、页码、表格、行/单元格级引用；
- 禁止让模型直接执行任意 SQL。

第一版可以只完成表格存储、摘要块和行块，结构化计算作为后续阶段，但数据模型必须提前兼容。

## 9. 管理员端与 API

### 9.1 兼容原则

第一阶段继续保留：

```text
POST /api/admin/knowledge
POST /api/admin/knowledge/file
GET  /api/admin/knowledge/status
```

旧上传接口内部改为调用统一 Pipeline，并保持 `{source, chunks}` 响应，避免立即破坏现有 `admin.js`。响应可加字段，但不能删除旧字段。

### 9.2 新 API

建议新增：

```text
POST /api/admin/knowledge/documents
GET  /api/admin/knowledge/documents
GET  /api/admin/knowledge/documents/{id}
GET  /api/admin/knowledge/documents/{id}/preview
POST /api/admin/knowledge/documents/{id}/reprocess
POST /api/admin/knowledge/documents/{id}/publish
POST /api/admin/knowledge/documents/{id}/deactivate
GET  /api/admin/knowledge/jobs/{id}
```

上传参数至少包含：

```text
file
title
canonical_key（可自动生成，但必须允许管理员确认）
domain
tags
site
version
parser_profile
chunking_profile
verified_at
expires_at
```

预览响应必须包含：

- 文档元数据和处理状态；
- 解析器与版本；
- 质量分和 warnings；
- 元素数量、页数、表格数量；
- Chunk 数量和长度分布；
- 每个 Chunk 的正文、标题路径、页码、类型和父块；
- 失败时的安全错误码和可操作说明。

### 9.3 文件安全

上传实现必须：

- 限制大小、扩展名和 MIME 类型；
- 使用服务端生成的 storage key，禁止把原始文件名直接作为磁盘路径；
- 防止 `../`、绝对路径和同名覆盖；
- 使用 SHA-256 去重，并保留同一文件的业务版本关系；
- PDF 解析设置页数、时间和内存上限；
- 拒绝加密且无密码的 PDF，并给出明确状态；
- 非 PDF 文件不再统一 `decode(errors="ignore")`，不支持的格式必须明确拒绝。

### 9.4 任务执行

MVP 可以同步处理小文本，但 PDF/OCR 不应长期占用 API 请求。正式版本使用可恢复的数据库任务记录和 worker：

```text
PENDING -> RUNNING -> SUCCEEDED
                   \-> FAILED
```

不得只依赖 FastAPI 内存 BackgroundTask 作为最终方案，因为进程重启后任务无法恢复。项目已有 Redis 时可以复用，但不要为了第一阶段引入完整分布式队列框架。

## 10. 索引与发布

现有 `app/services/knowledge_index.py` 已具备构建 READY、验证、激活和回滚能力，必须复用。

V2 索引文本建议：

```text
文档标题
标题路径
领域、校区、标签
Chunk 类型
正文或表格行
```

规则：

1. BM25 和向量索引使用同一个 ACTIVE 文档快照签名。
2. 索引签名加入 `parser_version`、`chunking_profile` 和 Chunk 内容哈希。
3. 新解析结果先构建 READY collection。
4. 运行 smoke query 和评测门禁。
5. 门禁通过后激活，旧 collection 进入 PREVIOUS。
6. 激活失败立即回滚，不修改文档原始文件和解析结果。
7. 表格摘要块与表格行块必须标记类型，重排时避免同一张表的多行淹没其他证据。

## 11. 分阶段实施计划

### Phase 0：基线、治理和兼容测试

目标：在不改变生产行为的前提下冻结现状。

实施：

1. 为 `chunk_text()`、`stable_chunk_text()`、Markdown、逐页 PDF 和管理员 PDF 上传补精确行为测试。
2. 添加脚本报告 ACTIVE 文档、`canonical_key`、内容哈希和重复情况，不自动删除。
3. 为启动时隐式 Markdown 导入增加结构化告警和兼容开关，默认先保持当前行为。
4. 记录当前评测集的 Chunk 数、长度分布、Recall@K 和引用页码准确率。

验收：

- 当前功能无行为变化；
- 测试能明确证明管理员上传 PDF 当前会丢失页码；
- 重复文档报告可审计；
- 没有数据迁移。

预计：2～3 个工作日。

### Phase 1：统一 Catalog、Artifact 和 Pipeline

目标：Manifest 和管理员上传进入同一编排链路，仍使用旧解析和旧分割结果。

实施：

1. 新增 V2 migration、实体和状态转换。
2. 实现 `artifact_store.py`、`catalog.py` 和 `pipeline.py`。
3. 把 `KnowledgeService.ingest*()` 改成兼容包装。
4. Manifest Importer 改为调用 Pipeline，但输出必须与基线一致。
5. 添加幂等、同名文件、哈希去重、失败回滚和路径安全测试。

停止条件：如果同一输入的旧 Chunk 内容或顺序发生变化，先修复兼容问题，不进入 Phase 2。

预计：3～5 个工作日。

### Phase 2：Document IR 与基础解析器

目标：Markdown、纯文本和简单 PDF 产生统一元素。

实施：

1. 新增 IR 和 Parser Protocol。
2. 实现 Markdown AST、纯文本和 `pypdf` 解析器。
3. 保存标题路径、页码、元素顺序、parser 版本和 warning。
4. 实现解析质量门禁。
5. 将学生手册页码偏移迁移为显式配置并校验。

停止条件：页码、元素顺序或标题路径不能稳定复现时，不实现新分割。

预计：4～7 个工作日。

### Phase 3：结构化父子分割

目标：上线 `structure_token_v2`，保留 `legacy_char_v1` 回滚能力。

实施：

1. 实现 TokenCounter 和可版本化 ChunkProfile。
2. 实现元素、句子、条款和完整单位重叠。
3. 生成父块、子块及引用映射。
4. 检索层增加父块扩展，对旧 Chunk 保留相邻块兼容逻辑。
5. 索引签名加入解析和分割版本。

停止条件：V2 在固定评测集上的 Recall@10、引用正确率或延迟明显差于基线时，不激活索引。

预计：4～6 个工作日。

### Phase 4：复杂 PDF 和 OCR 选型

目标：解决双栏、扫描页、复杂布局和表格识别。

实施前建立至少 20 份真实样本：

- 简单数字 PDF；
- 双栏制度文件；
- 扫描版中文通知；
- 跨页表格；
- 合并单元格；
- 带页眉页脚和目录的手册。

比较 `pypdf`、Docling、MinerU、PaddleOCR 的正文顺序、表格结构、页码、耗时、内存和安装体积。根据结果实现可选适配器，不凭工具热度选型。

预计：5～10 个工作日，取决于 OCR 部署方式。

### Phase 5：表格双通道

目标：同时支持语义问答和精确表格查询。

实施：

1. 保存 HTML/JSON 原始表格。
2. 生成摘要块和带表头的行块。
3. 增加表格级去重与检索限额。
4. 增加受限结构化查询服务。
5. 引用精确到文档、页、表格和行/单元格。

预计：5～10 个工作日。

### Phase 6：管理员预览、发布和重处理

目标：让知识治理可操作、可审核。

实施：

1. 文档列表、状态和失败原因。
2. 解析元素与 Chunk 预览。
3. 元数据编辑、重新解析和重新分割。
4. READY 发布、ACTIVE 下线和索引状态展示。
5. 长任务轮询和重试。

预计：5～8 个工作日。

### Phase 7：删除兼容债务

只有在所有旧 Markdown 已登记、管理员上传已迁移且回滚演练通过后：

1. 默认关闭启动时隐式扫描 Markdown。
2. 标记旧 `chunk_text()` 只用于历史回放。
3. 清理重复 ACTIVE 文档，但先导出报告和备份。
4. 更新 README、运维手册和故障恢复流程。

## 12. 测试要求

新增建议测试文件：

```text
tests/test_knowledge_document_ir.py
tests/test_knowledge_parser_registry.py
tests/test_knowledge_markdown_parser.py
tests/test_knowledge_pdf_parser.py
tests/test_knowledge_structure_chunker.py
tests/test_knowledge_table_ingestion.py
tests/test_knowledge_ingestion_pipeline.py
tests/test_knowledge_admin_lifecycle.py
tests/test_knowledge_v2_retrieval_regressions.py
tests/test_knowledge_ingestion_migration.py
```

必须覆盖：

- 中文标点、英文标点、无空格长文本；
- 多级标题、列表、编号条款和代码块；
- 超长段落和超长句子；
- 64 字符边界问题的回归；
- 空 PDF、加密 PDF、扫描 PDF和乱码 PDF；
- PDF 页码、跨页段落和重复页眉页脚；
- 简单表格、跨页表格、合并单元格和空单元格；
- 同一文件重复上传、同名不同内容和同内容不同文件名；
- Pipeline 任一步失败后的事务回滚；
- ACTIVE 文档重处理失败时旧版本继续可检索；
- READY 索引验证、激活和回滚；
- 旧 API 返回字段兼容；
- 文件名路径穿越、超限文件和不支持 MIME；
- 中文源码中无意外 `\uXXXX`。

测试中不得依赖真实外部 OCR 服务。所有 Parser、TokenCounter、ArtifactStore 和 EmbeddingBackend 必须可以注入 fake。

## 13. 评测与验收标准

先保存 V1 基线，再比较 V2。没有基线数据不得宣称“检索效果提升”。

### 13.1 解析指标

| 指标 | 最低验收 |
|---|---:|
| 数字 PDF 页码保留率 | 100% |
| Markdown 标题路径准确率 | 100%（固定测试集） |
| 正文阅读顺序准确率 | 不低于基线，复杂 PDF 人工抽检通过 |
| 表格结构 F1 | 样本集目标不低于 0.90 |
| 空文档静默入库 | 0 |

### 13.2 分割与检索指标

| 指标 | 最低验收 |
|---|---:|
| 子块超过 max token | 0，除非带明确 fallback 标志 |
| 从条款/表格行中间截断 | 0（固定测试集） |
| 引用文档与页码准确率 | 不低于 95% |
| Recall@10 | 不低于 V1，目标提升 10 个百分点 |
| 重复 ACTIVE canonical version | 0 |
| 同输入重复处理的内容哈希 | 100% 一致 |

### 13.3 发布与可靠性指标

- READY 门禁失败不得影响 ACTIVE。
- ACTIVE/PREVIOUS 回滚演练必须通过。
- 所有 Chunk 可追溯到文档、Artifact 和至少一个 Element。
- 管理员能看见解析器、分割配置、warning 和失败原因。
- 上传原件、解析产物、数据库和向量索引版本能够相互核对。

## 14. AI 编码执行顺序

执行 AI 每一阶段必须按以下顺序工作：

1. 复述当前阶段目标、非目标和预计修改文件。
2. 检查 `git status`，识别与用户现有改动重叠的文件。
3. 阅读相关实现和测试，不根据本文假设代码仍未变化。
4. 运行最小测试基线并记录结果。
5. 先添加能够描述新契约的测试。
6. 使用最小代码修改使测试通过。
7. 运行阶段测试、知识库回归和完整测试。
8. 检查 migration heads、工作区 diff 和 `\u[0-9a-fA-F]{4}`。
9. 更新 README 和本文的实施状态，但不伪造性能数据。
10. 向用户报告修改文件、测试结果、遗留风险和下一阶段，不自动进入下一阶段。

建议给执行 AI 的单阶段指令：

```text
阅读 MIND_BRIDGE_KNOWLEDGE_BASE_INGESTION_OPTIMIZATION_IMPLEMENTATION_GUIDE_20260802.md 全文，
只实施 Phase N。先检查当前代码和未提交改动，运行并记录相关测试基线；先写测试，再做最小实现。
不得实施后续阶段，不得替换现有检索架构，不得覆盖用户改动。完成后运行阶段测试和完整回归，
检查 Alembic head、UTF-8 编码以及源码中的意外 Unicode 转义，并报告验收项逐条结果。
```

## 15. 禁止事项

执行过程中禁止：

- 把所有管理员上传内容写入 Git Manifest；
- 让管理员直接编辑 YAML；
- 删除现有上传 API 后再开发新界面；
- 直接修改或删除旧 migration；
- 在应用启动时自动重建向量索引；
- 将 DRAFT 文档默认作为线上证据；
- 将 PDF 全文展平后声称已支持表格；
- 只保存 LLM 生成的表格摘要而丢弃原始单元格；
- 使用任意 SQL 或让模型直接连接业务数据库；
- 在没有基准测试时无条件引入重型 OCR 依赖；
- 用字符数冒充 Token 数；
- 使用任意字符尾部作为 V2 重叠单位；
- 解析失败后创建空 Chunk 或静默忽略错误；
- 一次提交同时改 ingestion、retrieval、Agent、Prompt 和前端全部链路；
- 为了通过新测试而删除现有知识可靠性、安全和引用校验。

## 16. 回滚策略

每个 V2 文档必须记录 `parser_profile` 和 `chunking_profile`。回滚按以下顺序：

1. 索引异常：使用现有知识索引 rollback，恢复 PREVIOUS collection。
2. V2 Chunk 异常：将文档切回 `legacy_char_v1`，重新生成 READY 索引并验证。
3. 新解析器异常：切回 `pypdf_fast` 或旧 Manifest 专用解析器。
4. 管理端异常：旧上传 API 在兼容期继续可用。
5. migration 异常：先回滚应用，再按 migration downgrade；不得删除原始 Artifact。

任何回滚都不得覆盖上传原件、历史解析产物和审计记录。

## 17. 工作量与推荐优先级

单人实施估算：

| 范围 | 时间 |
|---|---:|
| Phase 0～1：治理与统一入口 | 1 周左右 |
| Phase 2～3：IR 与结构化分割 | 1～2 周 |
| Phase 4～5：复杂 PDF、OCR 与表格 | 2～3 周 |
| Phase 6～7：管理端与收尾 | 1～2 周 |

推荐先完成 Phase 0～3，形成可上线、可回滚的知识入库 V2 MVP。Phase 4～5 必须由真实 PDF 样本和业务问题驱动，不应为了展示技术栈而提前增加复杂度。

## 18. 最终完成定义

只有同时满足以下条件，才能宣布本次知识库优化完成：

1. Manifest 和管理员上传使用统一 Pipeline，但保持不同的来源注册方式。
2. 管理员可以上传、预览、重新分割、发布和下线文档。
3. Markdown、普通 PDF、复杂 PDF 和扫描 PDF 都有明确解析策略与失败状态。
4. Chunk 使用结构边界和可验证 Token 预算，保留父子关系和来源映射。
5. PDF 表格保留原始行列结构，并能提供语义与精确查询两条路径。
6. 所有 ACTIVE 证据都能追溯到文档、版本、页码、元素和原始文件。
7. 新索引经过 READY 验证后才能激活，并且可以回滚。
8. 固定评测集证明解析、召回和引用质量不低于 V1 基线。
9. 旧接口兼容策略、数据迁移和运维文档完整。
10. 完整测试通过，且未破坏现有 Agent、澄清、记忆、安全和知识可靠性链路。

## 19. 实施状态

### Phase 0：已完成（2026-08-02）

已完成：

1. 冻结 `chunk_text()`、`stable_chunk_text()`、Manifest Markdown、逐页 PDF 和管理员 PDF 上传的精确兼容行为。
2. 测试明确证明管理员 PDF 上传会展平多页正文，生成的旧 Chunk 不保留 `page_number`。
3. 语料核对报告新增 ACTIVE 文档、`canonical_key`、内容哈希、重复 canonical 和重复内容快照；默认仍为只读，不自动删除数据。
4. 启动时隐式 Markdown 导入新增 `KNOWLEDGE_LEGACY_MARKDOWN_BOOTSTRAP_ENABLED` 兼容开关，默认值为 `true`，并输出不含正文的结构化告警。
5. 固定评测报告新增 ACTIVE Chunk 数量、字符长度分布和引用页码映射核对指标。
6. 保存精简基线到 `app/rag_eval/knowledge-ingestion-v1-baseline-summary.json`；完整离线报告保存到 `target/knowledge-ingestion-v1-baseline.json`，语料审计保存到 `target/knowledge-corpus-active-audit.json`。

V1 BM25 基线：

| 指标 | 结果 |
|---|---:|
| 固定评测 case | 181 |
| ACTIVE 文档 | 22 |
| ACTIVE Chunk | 530 |
| Chunk 字符长度 min / p50 / p95 / max | 21 / 430 / 512 / 512 |
| Chunk 平均字符数 | 386.098 |
| Recall@5 | 0.963294 |
| MRR | 0.872024 |
| NDCG@5 | 0.885916 |
| Grade Macro F1 | 0.963542 |
| 页码映射核对 | 482 / 482 |
| 重复 ACTIVE canonical | 0 |
| 重复 ACTIVE 内容哈希 | 0 |

说明：长度仍是 Python 字符数，不得解释为 Token；页码指标仅核对检索结果与数据库 Chunk 页码的一致性，不代表 PDF 印刷页码已经过人工标注验证。

验证结果：阶段回归 `35 passed`；完整回归 `248 passed`，另有 20 条既有 FastAPI `on_event` 弃用告警；Alembic 未新增迁移。Phase 1 尚未开始。

### Phase 1：已完成（2026-08-03）

已完成：

1. 新增 `0012_knowledge_ingestion_v2`，建立 Artifact、Element、Table、父子 Chunk 和数据库入库任务结构；Alembic 仅有一个 head。
2. 实现 Catalog、文件 ArtifactStore 和统一 Pipeline，Manifest 与管理员上传保留各自注册方式但进入同一编排入口。
3. `KnowledgeService.ingest*()` 保留为兼容包装；旧 `{source, chunks}` API 未删除。
4. Manifest 兼容路径仍使用 `legacy_char_v1`，回归测试冻结原 Chunk 内容和顺序。
5. 覆盖幂等、哈希复用、同名不同内容、路径穿越、超限、失败回滚及敏感错误信息清理。

### Phase 2：已完成（2026-08-03）

已完成：

1. 建立统一 Document IR、Parser Protocol 和 Parser Registry。
2. 实现 Markdown AST、纯文本和 `pypdf_fast` 解析器，保存标题路径、页码、元素顺序、解析器版本和 warning。
3. 解析质量门禁会拒绝空文档、加密 PDF、扫描 PDF 和不可接受乱码，不创建空 Chunk。
4. PDF 页码偏移使用显式配置并接受校验，解析结果可稳定复现。

### Phase 3：已完成并正式激活新索引（2026-08-03）

已完成：

1. 实现可版本化 TokenCounter、`structure_token_v2` 和 `legacy_char_v1` 回滚路径。
2. 分割保留结构、句子、条款和完整单位重叠，生成父块、子块与 Element 引用映射。
3. 检索支持父块扩展，旧 Chunk 继续使用相邻块兼容逻辑；索引签名包含 parser/chunking profile。
4. 固定测试覆盖 Token 上限、64 字符边界、超长中英文文本、标题、列表、代码块及可重复哈希。

固定 181 条评测集已分别保存公平 V1 基线和 V2 正式报告。V1/V2 的 Recall@10 分别为 `0.987103` 和 `0.995040`，引用页码准确率均为 `1.0`；V2 BM25 P95 为 `0.243939` 秒，低于自身 `4.0` 秒绝对门禁。经用户明确决定，V1 P95 × 1.20 的相对延迟条件只保留为观察项，不再阻断激活；质量门禁保持不变。

已按顺序执行 V2 `build -> verify -> shadow -> activate`，随后执行 `rollback` 确认 ACTIVE 指针回到 V1 并写入 `rolledBackAt`，再重新执行 V2 `build -> verify -> shadow -> activate`。最终 ACTIVE collection 为 `mindbridge_knowledge_v3__ee700a94bab6`，包含 698 个可检索子块；V1 collection 保留为 PREVIOUS。最终 `verify --target active` 通过，MySQL V2 语料 hash、索引 signature、embedding 模型 digest 和向量数量一致。

### Phase 4：已完成可执行基准框架，OCR 选型条件未满足（2026-08-03）

已完成 `pypdf` 解析质量统计、可注入候选适配器和 `scripts/benchmark_pdf_parsers.py`，覆盖空 PDF、加密 PDF、扫描 PDF、乱码、页码及页眉页脚处理测试。

当前仓库只有 3 份真实 PDF，未达到本阶段要求的至少 20 份真实样本。因此没有引入 Docling、MinerU、PaddleOCR 等重型依赖，也没有宣称完成 OCR 工具选型；收集足够样本后才能执行正文顺序、表格结构、页码、耗时、内存和安装体积的正式比较。

### Phase 5：已完成（2026-08-03）

已完成：

1. 表格原始 HTML、JSON 和 schema 均持久化，不丢弃原始单元格。
2. 生成 `TABLE_SUMMARY` 和带表头的 `TABLE_ROW`，支持语义召回与精确行查询。
3. 实现表格级内容哈希去重、每表检索限额和受限类型化查询，不接受任意 SQL。
4. 引用可追踪到文档、页、表格、行和单元格。

### Phase 6：已完成（2026-08-03）

已完成：

1. 数据库入库任务、可恢复后台 worker、轮询和失败重试。
2. 文档列表、详情、元素与 Chunk 预览、元数据编辑、重新解析和重新分割 API。
3. READY 发布、ACTIVE 下线和索引状态展示；管理员页面提供对应生命周期操作和响应式布局。
4. ACTIVE 文档重处理失败时保留旧 revision 和旧 Chunk 继续提供检索。

### Phase 7：切换门禁已通过，旧 Markdown 启动扫描已关闭（2026-08-03）

已完成只读切换审计和 `scripts/audit_knowledge_ingestion_cutover.py`，并将 `chunk_text()` 标记为仅用于兼容回放和回滚。README 已补充 V2 运维、API、切换门禁和恢复顺序。

16 个静态 Markdown 已全部登记到 Manifest；数据库中 22 个 ACTIVE 文档全部由 Manifest 管理，无重复 ACTIVE canonical version。V2 激活和回滚演练后，正式审计结果为 `eligibleToDisableLegacyBootstrap=true`、`databaseAvailable=true`、`unmanagedMarkdown=0`、`cleanupPerformed=false`。因此：

1. `KNOWLEDGE_LEGACY_MARKDOWN_BOOTSTRAP_ENABLED` 默认值、Docker 环境和示例配置均已改为 `false`。
2. 没有需要清理的重复 ACTIVE canonical version；未删除原件、Artifact、历史解析产物、V1 collection 或审计记录。
3. Docker 镜像已重建并启动，容器内环境变量为 `false`；启动日志仅执行 Manifest 校验和导入，没有兼容 Markdown 扫描告警。管理状态接口显示 V2 ACTIVE、698 个向量 Chunk、向量后端可用。
4. 宿主机最终完整回归为 `288 passed`，另有 20 条既有 FastAPI `on_event` 弃用告警；Alembic 仍为唯一 head `0012_knowledge_ingestion_v2`，源码 Unicode 转义与 UTF-8 BOM 检查通过。

当前结论：Phase 1、2、3、5、6、7 已完成。Phase 4 的 OCR 正式选型仍因真实 PDF 只有 3 份、未达到至少 20 份样本而保持未完成；不得伪造样本或引入未经基准验证的重型 OCR 默认依赖，因此仍不能宣布完全满足第 18 节涉及复杂 PDF 和扫描 PDF 的最终完成定义。
