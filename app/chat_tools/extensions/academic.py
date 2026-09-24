"""学业只读工具骨架；未来在校验后接入学校接口。"""
from mcp.types import CallToolResult
from .contracts import unavailable


def get_my_courses(term_id: str | None = None) -> CallToolResult:
    """查询本人已选课程。预留工具，真实教务接口尚未接入。"""
    return unavailable("get_my_courses", locals())


def get_my_timetable(start_date: str, end_date: str, course_ids: list[str] | None = None) -> CallToolResult:
    """查询本人实际日期课表，最多31天。真实教务接口尚未接入。"""
    return unavailable("get_my_timetable", locals())


def get_my_deadlines(start_date: str, end_date: str, course_ids: list[str] | None = None, types: list[str] | None = None) -> CallToolResult:
    """查询本人考试和作业节点，类型为EXAM或ASSIGNMENT。真实接口尚未接入。"""
    return unavailable("get_my_deadlines", locals())
