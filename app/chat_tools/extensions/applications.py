"""办事查询骨架，不提交或撤回申请。"""
from mcp.types import CallToolResult
from .contracts import unavailable


def get_my_application_status(application_id: str | None = None, service_type: str | None = None, term_id: str | None = None) -> CallToolResult:
    """查询本人申请进度；无编号时可查询近期申请。真实办事接口尚未接入。"""
    return unavailable("get_my_application_status", locals())
