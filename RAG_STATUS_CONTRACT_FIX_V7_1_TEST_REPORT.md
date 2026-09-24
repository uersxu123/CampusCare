# MindBridge V7.1 RAG 状态契约最小修复与测试报告

## 结论

本次只修复 Specialist 与历史 RAG 状态之间的兼容问题，不改变当前 V7 正常 `OK / EMPTY` 路径。

### 修复前问题

Specialist 只识别 `OK / EMPTY`。当历史测试或旧调用方传入 `SUFFICIENT / PARTIAL / INSUFFICIENT / CONFLICT / DEGRADED` 时，会落入默认 `FAILED / TOOL_UNAVAILABLE`。此外，`DEGRADED` 状态下的候选 evidence 会被继续发布给下游。

### 修复后

| RAG status | Specialist status | reasonCode | Evidence 发布 |
|---|---|---|---|
| OK | COMPLETED | RETRIEVAL_COMPLETED | 保留 |
| EMPTY | PARTIAL | RETRIEVAL_EMPTY | 无 |
| SUFFICIENT | COMPLETED | EVIDENCE_COMPLETE | 保留 |
| PARTIAL | PARTIAL | EVIDENCE_PARTIAL | 保留 |
| INSUFFICIENT | PARTIAL | EVIDENCE_INSUFFICIENT | 保留现有有效 evidence |
| CONFLICT | PARTIAL | EVIDENCE_CONFLICT | 保留现有 evidence |
| DEGRADED | PARTIAL | GRADE_UNAVAILABLE | **清空** |

未知状态仍然映射为 `FAILED / TOOL_UNAVAILABLE`。

## 为什么对当前项目风险低

当前 V7 `RagPipeline` 正常路径仍返回：

- `OK -> COMPLETED / RETRIEVAL_COMPLETED`
- `EMPTY -> PARTIAL / RETRIEVAL_EMPTY`

本次没有改这两个映射，并新增回归测试锁定其行为。因此 Multi-Query、混合检索、RRF、Facet-Aware Reranker、Soft Coverage 以及 RAG Gate Prompt 均未改动。

## 测试结果

### Specialist 全量单测

```text
20 passed
```

原先 6 个状态契约失败已全部通过，同时新增 2 个用例验证 `OK / EMPTY` 行为没有变化。

### 相关回归

```text
66 passed
```

覆盖：

- Specialist 状态与错误处理
- Specialist RAG Gate Prompt
- ContextBuilder
- RoutePlan
- Tool Calling
- RAG Pipeline V2

### 语法编译

```text
COMPILE_OK
```

## 测试环境说明

项目默认数据库 URL 使用 MySQL + PyMySQL；当前容器未安装 `pymysql`，因此测试使用环境变量：

```text
DATABASE_URL=sqlite+pysqlite:///:memory:
```

这只影响测试数据库连接，不改变本次状态映射逻辑。
