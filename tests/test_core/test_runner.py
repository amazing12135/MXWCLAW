"""测试 core/runner.py — ReAct 循环 + 错误回灌 + 熔断机制"""

import pytest

from mxwbot.core.runner import AgentRunner, AgentRunResult, AgentRunSpec
from mxwbot.core.tools.base import Tool, ToolResult
from mxwbot.core.tools.register import ToolRegistry
from mxwbot.providers.base import (
    LLMProvider,
    LLMResponse,
    LLMStreamChunk,
    TokenUsage,
    ToolCallDelta,
    ToolCallRequest,
)


# ============================================================================
# Fake Provider — 支持 streaming
# ============================================================================

class _FakeProvider(LLMProvider):
    """模拟 LLM Provider: 可预设多轮响应 (非流式 + 流式)."""

    def __init__(self, responses=None, stream_sequences=None):
        super().__init__(model="test")
        self._responses = responses or []
        self._stream_sequences = stream_sequences or []  # 每轮一个 chunk 列表
        self._idx = 0
        self._stream_idx = 0

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
        if self._stream_idx < len(self._stream_sequences):
            seq = self._stream_sequences[self._stream_idx]
            self._stream_idx += 1
            for chunk in seq:
                yield chunk
            return
        yield LLMStreamChunk(delta="done", finish_reason="stop",
                             usage=TokenUsage(input_tokens=1, output_tokens=1))


# ============================================================================
# 测试工具 — 带真实 JSON Schema 参数
# ============================================================================

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


class _FileReadTool(Tool):
    """工具带严格 JSON Schema: path 必填 + max_lines 整数范围."""
    name = "read_file"
    description = "读取文件内容"
    is_readonly = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "max_lines": {
                "type": "integer",
                "description": "最大行数",
                "minimum": 1,
                "maximum": 1000,
            },
        },
        "required": ["path"],
    }

    async def execute(self, **kwargs):
        return ToolResult(content=f"read: {kwargs.get('path', '')}")


class _SearchTool(Tool):
    name = "search"
    description = "搜索内容"
    is_readonly = True
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
        },
        "required": ["query"],
    }

    async def execute(self, **kwargs):
        return ToolResult(content=f"results for: {kwargs.get('query', '')}")


# ============================================================================
# TestAgentRunner — 基础 + 错误回灌
# ============================================================================

class TestAgentRunner:
    def _make_provider(self, *responses):
        return _FakeProvider(responses=list(responses))

    def _make_stream_provider(self, *sequences):
        return _FakeProvider(stream_sequences=list(sequences))

    def _make_tools(self):
        reg = ToolRegistry()
        reg.register(_ReadTool())
        reg.register(_WriteTool())
        return reg

    def _make_schema_tools(self):
        """注册带真实 JSON Schema 的工具，用于校验测试."""
        reg = ToolRegistry()
        reg.register(_FileReadTool())
        reg.register(_SearchTool())
        return reg

    # -- 基础 -----------------------------------------------------------------

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
        """一轮工具调用后返回最终结果."""
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
        """达到最大迭代次数时停止."""
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
        """LLM 返回 error 时立即停止."""
        from mxwbot.providers.base import LLMErrorInfo
        provider = self._make_provider(
            LLMResponse(finish_reason="error", error=LLMErrorInfo(status_code=500)),
        )
        spec = AgentRunSpec(provider=provider, tools=ToolRegistry())
        runner = AgentRunner()
        result = await runner.run(spec, [{"role": "user", "content": "hi"}])
        assert result.finish_reason == "error"

    # -- 错误回灌: 参数校验失败 → LLM 能看到错误并重试 --------------------------

    @pytest.mark.asyncio
    async def test_validation_error_injected_into_messages(self):
        """参数校验失败 → 错误消息以 tool role 注入消息列表.

        模拟: LLM 第一轮传空参数给 read_file → 校验失败
        → Error 注入 messages → LLM 第二轮看到错误并修正 → 成功.
        """
        provider = self._make_provider(
            # 第 1 轮: LLM 传空参数 (缺 path)
            LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="read_file", arguments="{}")],
                usage=TokenUsage(),
            ),
            # 第 2 轮: LLM 看到校验错误后修正, 传正确的 path
            LLMResponse(content="读取成功: hello world", usage=TokenUsage()),
        )
        tools = self._make_schema_tools()
        messages = [{"role": "user", "content": "读文件"}]

        # 在 run() 内部, 我们可以 hook messages 来检查
        # 我们用 monkey-patch 来捕获注入的消息
        runner = AgentRunner()
        captured_messages = []

        original_run = runner.run

        async def run_with_capture(spec, msgs):
            # 用 _orig 来跑, 但捕获最终的 messages
            result = await original_run(spec, msgs)
            captured_messages.extend(msgs)
            return result

        spec = AgentRunSpec(provider=provider, tools=tools)
        result = await run_with_capture(spec, messages)

        # 第 2 轮 LLM 看到了错误并返回了最终内容
        assert result.content == "读取成功: hello world"

        # 验证 error 被注入到 messages
        tool_messages = [m for m in captured_messages if m["role"] == "tool"]
        assert len(tool_messages) == 1
        assert "Error" in tool_messages[0]["content"]
        assert "read_file" in tool_messages[0]["content"]
        assert "参数校验失败" in tool_messages[0]["content"]

    @pytest.mark.asyncio
    async def test_tool_not_found_error_injected(self):
        """LLM 幻觉出不存在的工具 → 错误回灌, 告知可用工具列表."""
        provider = self._make_provider(
            LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="delete_all", arguments="{}")],
                usage=TokenUsage(),
            ),
            LLMResponse(content="抱歉, 无法执行该操作", usage=TokenUsage()),
        )
        tools = self._make_schema_tools()
        messages = [{"role": "user", "content": "删除所有文件"}]

        spec = AgentRunSpec(provider=provider, tools=tools)
        runner = AgentRunner()
        result = await runner.run(spec, messages)

        # 工具不存在的错误被注入
        tool_messages = [m for m in messages if m["role"] == "tool"]
        assert len(tool_messages) == 1
        err = tool_messages[0]["content"]
        assert "Error" in err
        assert "未找到" in err
        assert "read_file" in err  # 可用工具列表包含已注册的工具
        assert result.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_llm_corrects_after_error_and_succeeds(self):
        """完整修正链路: 缺参数 → 错误 → LLM 补上参数 → 正常返回."""
        provider = self._make_provider(
            # 第 1 轮: 缺 query
            LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="search", arguments="{}")],
                usage=TokenUsage(),
            ),
            # 第 2 轮: 补上 query
            LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c2", name="search",
                                            arguments='{"query": "hello"}')],
                usage=TokenUsage(),
            ),
            # 第 3 轮: 得到结果, 回复用户
            LLMResponse(content="搜索 hello 的结果是...", usage=TokenUsage()),
        )
        tools = self._make_schema_tools()
        messages = [{"role": "user", "content": "搜索 hello"}]

        spec = AgentRunSpec(provider=provider, tools=tools)
        runner = AgentRunner()
        result = await runner.run(spec, messages)

        assert result.content == "搜索 hello 的结果是..."
        # 第 1 轮的 tool error + 第 2 轮的 tool success 都被注入
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        assert len(tool_msgs) == 2
        assert "Error" in tool_msgs[0]["content"]  # 第 1 次失败
        assert "results for" in tool_msgs[1]["content"]  # 第 2 次成功


# ============================================================================
# TestStreamRunner — 熔断机制 + 流式错误回灌
# ============================================================================

class TestStreamRunner:
    """测试 run_stream() 的连续失败熔断 + 错误回灌."""

    def _make_provider(self, *sequences):
        """每个 sequence 是一个 LLMStreamChunk 列表, 代表一轮 LLM 调用."""
        return _FakeProvider(stream_sequences=list(sequences))

    def _make_tools(self):
        reg = ToolRegistry()
        reg.register(_FileReadTool())
        reg.register(_SearchTool())
        return reg

    # -- helpers: 构建流式 chunk 序列 -----------------------------------------

    @staticmethod
    def _tool_call_chunks(call_id: str, name: str, args: str) -> list[LLMStreamChunk]:
        """构建一个流式 tool_call 响应 chunk 序列."""
        return [
            # 第 1 个 delta: id + name
            LLMStreamChunk(
                tool_call_delta=ToolCallDelta(index=0, id=call_id, name=name),
            ),
            # 第 2 个 delta: arguments (分片发送)
            LLMStreamChunk(
                tool_call_delta=ToolCallDelta(index=0, arguments_delta=args[: len(args)//2]),
            ),
            LLMStreamChunk(
                tool_call_delta=ToolCallDelta(index=0, arguments_delta=args[len(args)//2:]),
            ),
            # 终端 chunk
            LLMStreamChunk(
                finish_reason="tool_calls",
                usage=TokenUsage(input_tokens=10, output_tokens=5),
            ),
        ]

    @staticmethod
    def _text_chunks(text: str) -> list[LLMStreamChunk]:
        """构建一个纯文本响应的 chunk 序列."""
        return [
            LLMStreamChunk(delta=text),
            LLMStreamChunk(
                finish_reason="stop",
                usage=TokenUsage(input_tokens=5, output_tokens=3),
            ),
        ]

    # -- 测试 -----------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_error_injected_in_stream_mode(self):
        """流式模式下参数校验失败 → 错误回灌 → LLM 修正后成功."""
        tools = self._make_tools()
        provider = self._make_provider(
            # 第 1 轮: LLM 传空参数给 search (缺 query)
            self._tool_call_chunks("c1", "search", "{}"),
            # 第 2 轮: LLM 修正
            self._text_chunks("搜索失败，缺少参数"),
        )
        messages = [{"role": "user", "content": "搜索"}]

        spec = AgentRunSpec(provider=provider, tools=tools)
        runner = AgentRunner()
        result = await runner.run_stream(spec, messages)

        assert result.content == "搜索失败，缺少参数"
        # 验证错误被注入
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert "Error" in tool_msgs[0]["content"]
        assert "search" in tool_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_consecutive_failures_counter_increments(self):
        """连续 3 轮全部失败 → hint 被注入 messages."""
        tools = self._make_tools()
        provider = self._make_provider(
            # 第 1 轮: search 缺 query → 失败
            self._tool_call_chunks("c1", "search", "{}"),
            # 第 2 轮: read_file 缺 path → 失败
            self._tool_call_chunks("c2", "read_file", "{}"),
            # 第 3 轮: 两个都缺参数 → 两个都失败
            self._tool_call_chunks("c3", "search", "{}"),
            # 第 4 轮: LLM 应该看到 hint 后停止工具调用, 返回文本
            self._text_chunks("抱歉，我无法完成这个请求"),
        )
        messages = [{"role": "user", "content": "帮我查个东西再读个文件"}]

        spec = AgentRunSpec(provider=provider, tools=tools, max_iterations=5)
        runner = AgentRunner()
        result = await runner.run_stream(spec, messages)

        # hint 应该被注入了 (第 3 轮全部失败后)
        system_msgs = [m for m in messages if m["role"] == "system"]
        hint_found = any("连续多次工具调用均失败" in m["content"] for m in system_msgs)
        assert hint_found, "连续 3 轮全部失败后应注入熔断 hint"
        assert result.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_partial_success_resets_counter(self):
        """部分成功时计数器重置 → 不会误触发熔断.

        场景: 每轮有 2 个工具调用, 一个失败一个成功 × 5 轮
        → 不应该触发 hint (因为每轮都有一个成功)
        """
        tools = self._make_tools()
        # 构建流式序列: 每轮都有 search (成功) + read_file (失败)
        # 但我们用单工具调用来模拟: 奇数轮成功, 偶数轮失败
        provider = self._make_provider(
            self._tool_call_chunks("c1", "search", '{"query": "hello"}'),    # 成功
            self._tool_call_chunks("c2", "read_file", "{}"),                  # 失败
            self._tool_call_chunks("c3", "search", '{"query": "world"}'),    # 成功
            self._tool_call_chunks("c4", "read_file", "{}"),                  # 失败
            self._tool_call_chunks("c5", "search", '{"query": "test"}'),     # 成功
            self._text_chunks("done"),  # 最终回复
        )
        messages = [{"role": "user", "content": "mixed"}]

        spec = AgentRunSpec(provider=provider, tools=tools, max_iterations=7)
        runner = AgentRunner()
        result = await runner.run_stream(spec, messages)

        # 不应该有熔断 hint
        system_msgs = [m for m in messages if m["role"] == "system"]
        hint_found = any("连续多次工具调用均失败" in m["content"] for m in system_msgs)
        assert not hint_found, "单轮部分工具成功不应触发熔断"

    @pytest.mark.asyncio
    async def test_counter_resets_after_success(self):
        """2 轮全部失败 → 1 轮成功 → 2 轮全部失败 → 不触发熔断 (因为成功重置了)."""
        tools = self._make_tools()
        provider = self._make_provider(
            # 第 1-2 轮: 全部失败 (但还没到 3 轮)
            self._tool_call_chunks("c1", "search", "{}"),
            self._tool_call_chunks("c2", "read_file", "{}"),
            # 第 3 轮: 成功 (重置计数器)
            self._tool_call_chunks("c3", "search", '{"query": "hello"}'),
            # 第 4-5 轮: 全部失败
            self._tool_call_chunks("c4", "search", "{}"),
            self._tool_call_chunks("c5", "read_file", "{}"),
            self._text_chunks("done"),
        )
        messages = [{"role": "user", "content": "test reset"}]

        spec = AgentRunSpec(provider=provider, tools=tools, max_iterations=7)
        runner = AgentRunner()
        result = await runner.run_stream(spec, messages)

        # 第 4-5 轮只连续失败了 2 轮 → 不应触发
        system_msgs = [m for m in messages if m["role"] == "system"]
        hint_found = any("连续多次工具调用均失败" in m["content"] for m in system_msgs)
        assert not hint_found, "成功后应重置计数器, 之后仅 2 连败不应触发"

    @pytest.mark.asyncio
    async def test_consecutive_failures_hint_contains_guidance(self):
        """验证 hint 内容引导 LLM 停止重试并告知用户."""
        tools = self._make_tools()
        provider = self._make_provider(
            self._tool_call_chunks("c1", "search", "{}"),
            self._tool_call_chunks("c2", "read_file", "{}"),
            self._tool_call_chunks("c3", "search", "{}"),
            self._text_chunks("抱歉，我无法获取所需信息"),
        )
        messages = [{"role": "user", "content": "fail permanently"}]

        spec = AgentRunSpec(provider=provider, tools=tools, max_iterations=5)
        runner = AgentRunner()
        await runner.run_stream(spec, messages)

        system_msgs = [m for m in messages if m["role"] == "system"]
        hints = [m["content"] for m in system_msgs if "连续多次" in m["content"]]
        assert len(hints) == 1
        # hint 应包含具体指引
        assert "停止尝试更多工具" in hints[0]
        assert "替代方案" in hints[0]

    @pytest.mark.asyncio
    async def test_mixed_batch_one_failure_does_not_count(self):
        """同一轮有 2 个工具调用, 1 个成功 1 个失败 → 不算入 consecutive_failures.

        这相当于验证 failure_count < len(results) 时计数器不增加.
        通过 mock 多轮来证明: 即使 10 轮每轮都有失败, 只要不是"全部"失败, 熔断不触发.
        """
        # 这里我们依赖 execute_batch 的混合结果
        # read_file (缺 path → 失败) + search (有 query → 成功)
        # 需要使用两个 tool_call 的流来模拟
        from mxwbot.core.runner import AgentRunner as AR

        tools = self._make_tools()

        def _two_tool_round(cid1, cid2):
            """一轮返回两个 tool_call 的 chunk 序列."""
            return [
                # tool_call 0: search (会成功)
                LLMStreamChunk(
                    tool_call_delta=ToolCallDelta(index=0, id=cid1, name="search"),
                ),
                LLMStreamChunk(
                    tool_call_delta=ToolCallDelta(index=0, arguments_delta='{"query":"test"}'),
                ),
                # tool_call 1: read_file (会失败, 缺 path)
                LLMStreamChunk(
                    tool_call_delta=ToolCallDelta(index=1, id=cid2, name="read_file"),
                ),
                LLMStreamChunk(
                    tool_call_delta=ToolCallDelta(index=1, arguments_delta="{}"),
                ),
                LLMStreamChunk(
                    finish_reason="tool_calls",
                    usage=TokenUsage(input_tokens=10, output_tokens=5),
                ),
            ]

        provider = self._make_provider(
            _two_tool_round("c1", "c2"),
            _two_tool_round("c3", "c4"),
            _two_tool_round("c5", "c6"),
            _two_tool_round("c7", "c8"),
            _two_tool_round("c9", "c10"),
            self._text_chunks("processing mixed results done"),
        )
        messages = [{"role": "user", "content": "mixed batch"}]

        spec = AgentRunSpec(provider=provider, tools=tools, max_iterations=7)
        runner = AR()
        result = await runner.run_stream(spec, messages)

        # 每轮都有 search 成功 → 不应该触发熔断
        system_msgs = [m for m in messages if m["role"] == "system"]
        hint_found = any("连续多次工具调用均失败" in m["content"] for m in system_msgs)
        assert not hint_found, (
            f"每轮都有部分工具成功, 不应触发熔断. "
            f"tool msgs: {[m for m in messages if m['role'] == 'tool']}"
        )
