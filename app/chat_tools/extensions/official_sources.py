"""官方来源工具骨架，不执行真实搜索或抓取。"""
from mcp.types import CallToolResult
from .contracts import unavailable


def search_official_sources(query: str, source_scope: str, date_from: str | None = None, date_to: str | None = None, max_results: int = 3) -> CallToolResult:
    """搜索官方来源并提取正文。预留工具，搜索提供方尚未接入。"""
    # 后续在参数校验后接入：搜索 → 按学校配置筛选官方来源 → 正文提取 → 契约化返回。
    # 当前不把搜索摘要或演示内容当作已核验正文。
    return unavailable("search_official_sources", locals())
