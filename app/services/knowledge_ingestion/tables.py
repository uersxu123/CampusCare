from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from typing import Mapping

from sqlalchemy.orm import Session

from app.models.entities import KnowledgeTable
from app.services.knowledge_ingestion.models import DocumentElement


@dataclass(frozen=True)
class NormalizedTable:
    caption: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    table_html: str
    schema: Mapping[str, object]


@dataclass(frozen=True)
class TableFilter:
    column: str
    operator: str
    value: str


@dataclass(frozen=True)
class TableQuery:
    document_id: int
    table_id: int
    filters: tuple[TableFilter, ...] = ()
    sort_by: str | None = None
    descending: bool = False
    limit: int = 100
    aggregate: str | None = None
    aggregate_column: str | None = None


@dataclass(frozen=True)
class TableRowResult:
    values: Mapping[str, str]
    citation: Mapping[str, int | None]


@dataclass(frozen=True)
class TableQueryResult:
    rows: tuple[TableRowResult, ...]
    aggregate_value: float | None = None


def normalize_table(element: DocumentElement) -> NormalizedTable:
    headers = tuple(str(item).strip() for item in element.metadata.get("headers", []))
    if not headers or any(not item for item in headers) or len(set(headers)) != len(headers):
        raise ValueError("table_headers_invalid")
    rows = []
    for raw in element.metadata.get("rows", []):
        values = [str(item).strip() for item in raw]
        values = (values + [""] * len(headers))[: len(headers)]
        rows.append(tuple(values))
    caption = str(element.metadata.get("caption") or (element.heading_path[-1] if element.heading_path else "表格"))
    schema = {
        "version": 1,
        "columns": [
            {"name": header, "type": _infer_type([row[index] for row in rows])}
            for index, header in enumerate(headers)
        ],
        "mergedCells": list(element.metadata.get("merged_cells", [])),
        "units": dict(element.metadata.get("units", {})),
    }
    return NormalizedTable(caption, headers, tuple(rows), _table_html(headers, rows), schema)


def table_summary_text(table: KnowledgeTable) -> str:
    headers = json.loads(table.headers_json)
    schema = json.loads(table.schema_json or "{}")
    units = schema.get("units", {}) if isinstance(schema, dict) else {}
    unit_text = "；".join(f"{key}={value}" for key, value in units.items()) or "未标注"
    return f"表：{table.caption or '未命名表格'}\n字段：{'；'.join(headers)}\n行数：{len(json.loads(table.rows_json))}\n单位：{unit_text}"


def table_row_text(table: KnowledgeTable, row_index: int, row: list[str]) -> str:
    headers = json.loads(table.headers_json)
    pairs = "；".join(f"{header}={row[index] if index < len(row) else ''}" for index, header in enumerate(headers))
    page = f"第 {table.page_number} 页" if table.page_number is not None else "页码未知"
    return f"表：{table.caption or '未命名表格'}\n列：{pairs}\n来源：{page}，表 {table.id}，第 {row_index + 1} 行"


class KnowledgeTableQueryService:
    _operators = {"eq", "ne", "gt", "gte", "lt", "lte", "contains"}
    _aggregates = {None, "sum", "avg", "min", "max"}

    def __init__(self, db: Session):
        self.db = db

    def query(self, request: TableQuery) -> TableQueryResult:
        if not 1 <= request.limit <= 100:
            raise ValueError("table_query_limit_invalid")
        table = self.db.get(KnowledgeTable, request.table_id)
        if table is None or table.document_id != request.document_id:
            raise ValueError("table_query_scope_invalid")
        headers = [str(item) for item in json.loads(table.headers_json)]
        schema = json.loads(table.schema_json or "{}")
        types = {str(item["name"]): str(item["type"]) for item in schema.get("columns", [])}
        if request.sort_by is not None and request.sort_by not in headers:
            raise ValueError("table_query_column_invalid")
        if request.aggregate not in self._aggregates:
            raise ValueError("table_query_aggregate_invalid")
        if request.aggregate and request.aggregate_column not in headers:
            raise ValueError("table_query_column_invalid")
        for item in request.filters:
            if item.column not in headers:
                raise ValueError("table_query_column_invalid")
            if item.operator not in self._operators:
                raise ValueError("table_query_operator_invalid")
        indexed = list(enumerate(json.loads(table.rows_json)))
        filtered = [item for item in indexed if self._matches(item[1], headers, types, request.filters)]
        if request.sort_by:
            column_index = headers.index(request.sort_by)
            column_type = types.get(request.sort_by, "string")
            filtered.sort(key=lambda item: _typed(item[1][column_index], column_type), reverse=request.descending)
        aggregate_value = None
        if request.aggregate and request.aggregate_column:
            column_index = headers.index(request.aggregate_column)
            column_type = types.get(request.aggregate_column, "string")
            values = [_typed(row[column_index], column_type) for _, row in filtered]
            numbers = [float(value) for value in values if isinstance(value, (int, float))]
            if numbers:
                aggregate_value = _aggregate(numbers, request.aggregate)
        results = tuple(
            TableRowResult(
                values={header: row[index] if index < len(row) else "" for index, header in enumerate(headers)},
                citation={
                    "documentId": table.document_id,
                    "pageNumber": table.page_number,
                    "tableId": table.id,
                    "rowIndex": row_index + 1,
                },
            )
            for row_index, row in filtered[: request.limit]
        )
        return TableQueryResult(results, aggregate_value)

    @staticmethod
    def _matches(row, headers, types, filters) -> bool:
        for item in filters:
            index = headers.index(item.column)
            actual = _typed(row[index] if index < len(row) else "", types.get(item.column, "string"))
            expected = _typed(item.value, types.get(item.column, "string"))
            if item.operator == "eq" and actual != expected:
                return False
            if item.operator == "ne" and actual == expected:
                return False
            if item.operator == "contains" and str(expected) not in str(actual):
                return False
            if item.operator == "gt" and not actual > expected:
                return False
            if item.operator == "gte" and not actual >= expected:
                return False
            if item.operator == "lt" and not actual < expected:
                return False
            if item.operator == "lte" and not actual <= expected:
                return False
        return True


def _infer_type(values: list[str]) -> str:
    nonempty = [value for value in values if value]
    if nonempty and all(value.endswith("%") and _number(value) is not None for value in nonempty):
        return "percent"
    if nonempty and all(_number(value) is not None for value in nonempty):
        return "number"
    return "string"


def _typed(value: str, kind: str):
    if kind in {"number", "percent"}:
        number = _number(value)
        if number is None:
            raise ValueError("table_query_value_type_invalid")
        return number
    return str(value)


def _number(value: str) -> float | None:
    cleaned = re.sub(r"[,，\s]", "", str(value)).removesuffix("%").removesuffix("元")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _aggregate(values: list[float], operation: str) -> float:
    if operation == "sum":
        return sum(values)
    if operation == "avg":
        return sum(values) / len(values)
    if operation == "min":
        return min(values)
    if operation == "max":
        return max(values)
    raise ValueError("table_query_aggregate_invalid")


def _table_html(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    head = "".join(f"<th>{html.escape(value)}</th>" for value in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(value)}</td>" for value in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
