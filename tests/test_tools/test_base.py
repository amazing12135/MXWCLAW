"""测试 core/tools/base.py"""

from mxwbot.core.tools.base import Tool, ToolResult


class FakeTool(Tool):
    name = "fake"
    description = "A fake tool for testing"
    is_readonly = True
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return ToolResult(content="done")


class TestTool:
    def test_to_openai_schema(self):
        t = FakeTool()
        schema = t.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "fake"

    def test_tool_result_defaults(self):
        r = ToolResult()
        assert r.success is True
        assert r.content == ""
        assert r.error is None

    def test_tool_result_error(self):
        r = ToolResult(success=False, error="something went wrong")
        assert r.success is False
        assert r.error == "something went wrong"
