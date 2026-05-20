"""MCP 协议工具 — v1 存根。"""

from typing import Any

from mxwbot.core.tools.base import Tool, ToolResult


class McpTool(Tool):
    """MCP 协议工具（Phase 5 存根）。"""

    name = "mcp"
    description = "通过 MCP 协议调用外部工具（v1 未实现）"
    is_readonly = True
    parameters = {
        "type": "object",
        "properties": {
            "server": {"type": "string", "description": "MCP Server 名称"},
            "tool": {"type": "string", "description": "工具名称"},
            "arguments": {"type": "object", "description": "调用参数"},
        },
        "required": ["server", "tool"],
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(content="MCP 工具将在后续版本实现")
