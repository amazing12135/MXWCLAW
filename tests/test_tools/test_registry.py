"""测试 core/tools/register.py"""

import pytest

from mxwbot.core.tools.base import Tool, ToolResult
from mxwbot.core.tools.register import ToolRegistry


class _ReadTool(Tool):
    name = "read"
    description = "read"
    is_readonly = True
    parameters = {}

    async def execute(self, **kwargs):
        return ToolResult(content="read done")


class _WriteTool(Tool):
    name = "write"
    description = "write"
    is_readonly = False
    parameters = {}

    async def execute(self, **kwargs):
        return ToolResult(content="write done")


class TestToolRegistry:
    def test_register_and_get(self):
        reg = ToolRegistry()
        reg.register(_ReadTool())
        assert "read" in reg
        assert isinstance(reg.get("read"), _ReadTool)

    def test_get_unknown_raises(self):
        reg = ToolRegistry()
        with pytest.raises(KeyError):
            reg.get("nonexistent")

    def test_list_names(self):
        reg = ToolRegistry()
        reg.register(_ReadTool())
        reg.register(_WriteTool())
        assert set(reg.list_names()) == {"read", "write"}

    def test_list_readonly_names(self):
        reg = ToolRegistry()
        reg.register(_ReadTool())
        reg.register(_WriteTool())
        assert reg.list_readonly_names() == ["read"]

    def test_to_openai_schema(self):
        reg = ToolRegistry()
        reg.register(_ReadTool())
        schemas = reg.to_openai_schema()
        assert len(schemas) == 1
        assert schemas[0]["function"]["name"] == "read"

    @pytest.mark.asyncio
    async def test_execute_batch(self):
        reg = ToolRegistry()
        reg.register(_ReadTool())
        reg.register(_WriteTool())

        calls = [
            {"id": "c1", "name": "read", "arguments": {}},
            {"id": "c2", "name": "write", "arguments": {}},
        ]
        results = await reg.execute_batch(calls)
        assert len(results) == 2
        assert results[0][0] == "c1"
        assert results[0][1].content == "read done"
