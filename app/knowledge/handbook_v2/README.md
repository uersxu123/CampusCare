# 学生手册 V2 知识库

本目录取自学生手册_RAG_结构化父子分块_V2_项目交付包.zip。磁盘交付文件保持原样；导入时在同一文档、章节和已有父块边界内合并连续短条款、普通段落和列表项。

- 49 份文档，272 个父块，1487 个子块。
- 导入合并目标为 240 token，预算上限 400 token；完整问答、表格行和其他结构块独立保留。合并正文仅以空行连接原始子块，不删改原文。
- 当前规则导入后为 855 个子块，中位数 177，条款与普通正文中位数 244。长度使用原始计数加分隔符预算，并非重新精确分词；原有超过预算的独立块不截断。
- MySQL knowledge_chunks 保存父块和子块。子块 parent_chunk_id 外键指向父块数据库 ID。
- 交付包有 297 个独立子块的 parent_id 为 null；保持原样，不伪造父块。
- knowledge_elements.metadata_json 保存交付包的 chunk_id / parent_id、来源和页码等元数据；knowledge_chunks.element_ids_json 关联对应元素。
- ChromaDB 只索引子块，返回其 MySQL chunk ID。最终精排后通过 parent_chunk_id 回查 MySQL，结果包含子块 content、父块 parentContent、parentChunkId 和 pageNumber。
- references_v2.json 保留交付引用关系；当前传统 RAG 不执行多跳引用检索。
- 旧知识文档归档为 ARCHIVED，旧向量 collection 保留作历史备份，但不用于当前检索。旧 Markdown 和 PDF 不再自动导入。

检索顺序：问题改写 → BM25 与 ChromaDB 双路召回 → RRF（k=60，双路等权）→ 现有 Qwen 模型 Reranker → MySQL 父块上下文。
只返回 OK / EMPTY 检索状态，不生成证据充分性、缺口、冲突判断，不再触发二次检索。改写或精排不可用时分别回退原问题或 RRF 排序，并在 diagnostics 标记降级。

初始化/更新（项目根目录执行）：

```sh
python -m scripts.import_knowledge
python -m scripts.manage_knowledge_index build
python -m scripts.manage_knowledge_index shadow
python -m scripts.manage_knowledge_index activate
```

应用启动会幂等导入新版 MySQL 语料；向量索引由上述命令显式构建和激活。
