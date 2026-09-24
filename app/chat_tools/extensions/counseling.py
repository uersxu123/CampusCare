"""咨询时段查询骨架，不创建预约。"""
from mcp.types import CallToolResult
from .contracts import unavailable


def get_counseling_availability(start_date: str, end_date: str, campus: str | None = None, mode: str | None = None) -> CallToolResult:
    """查询咨询可预约时段，方式为ONLINE或OFFLINE；仅查询，真实接口尚未接入。"""
    return unavailable("get_counseling_availability", locals())
