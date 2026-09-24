"""仅供显式开启的独立 MCP 开发验证使用。"""
from copy import deepcopy

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .academic import get_my_courses, get_my_deadlines, get_my_timetable
from .applications import get_my_application_status
from .contracts import SCHEMAS
from .counseling import get_counseling_availability
from .official_sources import search_official_sources


EXTENSION_TOOLS = (get_my_courses, get_my_timetable, get_my_deadlines, search_official_sources, get_my_application_status, get_counseling_availability)


def register_extension_tools(mcp: FastMCP) -> None:
    for function in EXTENSION_TOOLS:
        mcp.add_tool(function, structured_output=False, annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
        # 与现有 server.py 保持一致：显式提供业务 JSON Schema。
        mcp._tool_manager._tools[function.__name__].parameters = deepcopy(SCHEMAS[function.__name__])
