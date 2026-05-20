"""测试 core/runner.py"""

import pytest

from mxwbot.core.runner import AgentRunner, AgentRunResult, AgentRunSpec
from mxwbot.core.tools.base import Tool, ToolResult
from mxwbot.core.tools.register import ToolRegistry
from mxwbot.providers.base import LLMProvider, LLMResponse, TokenUsage


class _FakeProvider(LLMProvider):
    """模拟 LLM Provider：可以预设多轮响应。"""
    def __init__(self, responses=None):
        super().__init__(model="test")
        self._responses = responses or []
        self._idx = 0

    @property
    def supports_streaming(self) -> bool:
        return True

    async def _chat_impl(self, messages, tools):
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            return resp
        return LLMResponse(content="done", usage=TokenUsage(input_tokens=1, output_tokens=1))

    async def _chat_stream_impl(self, messages, tools):
        if False:
            yield
        return


class _ReadTool(Tool):
    name = "read"
    description = "read file"
    is_readonly = True
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return ToolResult(content="file content")


class _WriteTool(Tool):
    name = "write"
    description = "write file"
    is_readonly = False
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return ToolResult(content="written")


class TestAgentRunner:
    def _make_provider(self, *responses):
        return _FakeProvider(list(responses))

    def _make_tools(self):
        reg = ToolRegistry()
        reg.register(_ReadTool())
        reg.register(_WriteTool())
        return reg

    @pytest.mark.asyncio
    async def test_simple_response_no_tools(self):
        provider = self._make_provider(
            LLMResponse(content="hello", usage=TokenUsage(input_tokens=5, output_tokens=3)),
        )
        spec = AgentRunSpec(provider=provider, tools=ToolRegistry())
        runner = AgentRunner()
        result = await runner.run(spec, [{"role": "user", "content": "hi"}])
        assert result.content == "hello"
        assert result.iterations == 1
        assert result.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_one_tool_call_cycle(self):
        """一轮工具调用后返回最终结果。"""
        from mxwbot.providers.base import ToolCallRequest
        provider = self._make_provider(
            LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="read", arguments="{}")],
                usage=TokenUsage(),
            ),
            LLMResponse(content="result is: file content", usage=TokenUsage()),
        )
        spec = AgentRunSpec(provider=provider, tools=self._make_tools())
        runner = AgentRunner()
        result = await runner.run(spec, [{"role": "user", "content": "read file"}])
        assert result.content == "result is: file content"
        assert result.iterations == 2

    @pytest.mark.asyncio
    async def test_max_iterations_exceeded(self):
        """达到最大迭代次数时停止。"""
        from mxwbot.providers.base import ToolCallRequest
        provider = self._make_provider(
            LLMResponse(
                tool_calls=[ToolCallRequest(id="c1", name="read", arguments="{}")],
                usage=TokenUsage(),
            ),
            LLMResponse(
                tool_calls=[ToolCallRequest(id="c2", name="read", arguments="{}")],
                usage=TokenUsage(),
            ),
            LLMResponse(
                tool_calls=[ToolCallRequest(id="c3", name="read", arguments="{}")],
                usage=TokenUsage(),
            ),
        )
        spec = AgentRunSpec(provider=provider, tools=self._make_tools(), max_iterations=2)
        runner = AgentRunner()
        result = await runner.run(spec, [{"role": "user", "content": "loop"}])
        assert result.finish_reason == "max_iterations"
        assert result.iterations == 2

    @pytest.mark.asyncio
    async def test_error_response_stops(self):
        """LLM 返回 error 时立即停止。"""
        from mxwbot.providers.base import LLMErrorInfo
        provider = self._make_provider(
            LLMResponse(finish_reason="error", error=LLMErrorInfo(status_code=500)),
        )
        spec = AgentRunSpec(provider=provider, tools=ToolRegistry())
        runner = AgentRunner()
        result = await runner.run(spec, [{"role": "user", "content": "hi"}])
        assert result.finish_reason == "error"
