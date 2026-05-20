"""Tests for providers/base.py data structures."""

import pytest

from mxwbot.providers.base import (
    LLMCallPurpose,
    LLMErrorInfo,
    LLMResponse,
    LLMStreamChunk,
    TokenUsage,
    ToolCallDelta,
    ToolCallRequest,
)


class TestTokenUsage:
    def test_defaults(self):
        u = TokenUsage()
        assert u.input_tokens == 0
        assert u.output_tokens == 0
        assert u.cache_read_tokens == 0

    def test_values(self):
        u = TokenUsage(input_tokens=100, output_tokens=50, cache_read_tokens=10)
        assert u.input_tokens == 100
        assert u.output_tokens == 50
        assert u.cache_read_tokens == 10

    def test_extra(self):
        u = TokenUsage(input_tokens=10, output_tokens=5, extra={"reasoning_tokens": 20})
        assert u.extra["reasoning_tokens"] == 20


class TestToolCallRequest:
    def test_create(self):
        tc = ToolCallRequest(id="call_1", name="read_file", arguments='{"path": "/x"}')
        assert tc.id == "call_1"
        assert tc.name == "read_file"
        assert tc.arguments == '{"path": "/x"}'


class TestLLMResponse:
    def test_defaults(self):
        r = LLMResponse()
        assert r.content is None
        assert r.tool_calls == []
        assert r.finish_reason == "stop"
        assert r.error is None
        assert r.is_ok is True

    def test_with_tool_calls(self):
        tc = ToolCallRequest(id="c1", name="search", arguments="{}")
        r = LLMResponse(content="done", tool_calls=[tc])
        assert len(r.tool_calls) == 1
        assert r.tool_calls[0].name == "search"

    def test_with_error(self):
        err = LLMErrorInfo(status_code=429, kind="api_error", type="rate_limit_exceeded",
                           should_retry=True, retry_after_s=5.0)
        r = LLMResponse(finish_reason="error", error=err)
        assert r.is_ok is False
        assert r.error.kind == "api_error"
        assert r.error.should_retry is True

    def test_reasoning_content(self):
        r = LLMResponse(content="answer", reasoning_content="Let me think...")
        assert r.reasoning_content == "Let me think..."

    def test_thinking_blocks(self):
        blocks = [{"type": "thinking", "thinking": "analysis"}]
        r = LLMResponse(content="answer", thinking_blocks=blocks)
        assert r.thinking_blocks == blocks

    def test_retry_after(self):
        r = LLMResponse(retry_after=3.5)
        assert r.retry_after == 3.5


class TestToolCallDelta:
    def test_first_chunk(self):
        d = ToolCallDelta(index=0, id="tc_1", name="read_file")
        assert d.index == 0
        assert d.id == "tc_1"
        assert d.name == "read_file"
        assert d.arguments_delta == ""

    def test_arguments_chunk(self):
        d = ToolCallDelta(index=0, arguments_delta='{"path": "/x"}')
        assert d.id is None
        assert d.name is None
        assert d.arguments_delta == '{"path": "/x"}'


class TestLLMStreamChunk:
    def test_text_delta(self):
        c = LLMStreamChunk(delta="hello")
        assert c.delta == "hello"
        assert c.tool_call_delta is None

    def test_tool_delta(self):
        td = ToolCallDelta(index=0, arguments_delta="{}")
        c = LLMStreamChunk(tool_call_delta=td)
        assert c.tool_call_delta is td
        assert c.delta is None

    def test_finish(self):
        c = LLMStreamChunk(finish_reason="stop")
        assert c.finish_reason == "stop"


class TestLLMCallPurpose:
    def test_enum_values(self):
        assert LLMCallPurpose.AGENT.value == "agent"
        assert LLMCallPurpose.SUBAGENT.value == "subagent"
        assert LLMCallPurpose.SUMMARY.value == "summary"
        assert LLMCallPurpose.SYSTEM.value == "system"


class TestIsTransientError:
    """Tests for LLMProvider._is_transient_error."""

    def test_ok_response_not_transient(self):
        from mxwbot.providers.base import LLMProvider
        resp = LLMResponse(finish_reason="stop")
        assert LLMProvider._is_transient_error(resp) is False

    def test_error_with_should_retry(self):
        from mxwbot.providers.base import LLMProvider
        resp = LLMResponse(
            finish_reason="error",
            error=LLMErrorInfo(should_retry=True),
        )
        assert LLMProvider._is_transient_error(resp) is True

    def test_error_429_rate_limit(self):
        from mxwbot.providers.base import LLMProvider
        resp = LLMResponse(
            finish_reason="error",
            error=LLMErrorInfo(status_code=429, type="rate_limit_exceeded"),
        )
        assert LLMProvider._is_transient_error(resp) is True

    def test_error_429_quota_exceeded_not_retryable(self):
        from mxwbot.providers.base import LLMProvider
        resp = LLMResponse(
            finish_reason="error",
            error=LLMErrorInfo(status_code=429, type="insufficient_quota"),
        )
        assert LLMProvider._is_transient_error(resp) is False

    def test_error_500_retryable(self):
        from mxwbot.providers.base import LLMProvider
        resp = LLMResponse(
            finish_reason="error",
            error=LLMErrorInfo(status_code=500),
        )
        assert LLMProvider._is_transient_error(resp) is True

    def test_error_401_not_retryable(self):
        from mxwbot.providers.base import LLMProvider
        resp = LLMResponse(
            finish_reason="error",
            error=LLMErrorInfo(status_code=401),
        )
        assert LLMProvider._is_transient_error(resp) is False

    def test_error_timeout_kind_retryable(self):
        from mxwbot.providers.base import LLMProvider
        resp = LLMResponse(
            finish_reason="error",
            error=LLMErrorInfo(kind="timeout"),
        )
        assert LLMProvider._is_transient_error(resp) is True


class TestChatWithRetry:
    """Tests for LLMProvider.chat_with_retry / _run_with_retry."""

    def _fake_provider(self, responses: list[LLMResponse]):
        from mxwbot.providers.base import LLMProvider

        class _FakeRetryProvider(LLMProvider):
            def __init__(self):
                super().__init__(model="test")
                self._calls = 0
                self._responses = responses

            @property
            def supports_streaming(self) -> bool:
                return True

            async def _chat_impl(self, messages, tools):
                idx = self._calls
                self._calls += 1
                if idx < len(self._responses):
                    return self._responses[idx]
                return LLMResponse(content="fallback")

            async def _chat_stream_impl(self, messages, tools):
                if False:
                    yield
                return

            def _map_exception(self, exc):
                return LLMResponse(finish_reason="error", error=LLMErrorInfo(kind="api_error"))

        return _FakeRetryProvider()

    @pytest.mark.asyncio
    async def test_success_first_attempt(self):
        provider = self._fake_provider([LLMResponse(content="ok")])
        resp = await provider.chat_with_retry([{"role": "user", "content": "hi"}])
        assert resp.content == "ok"
        assert provider._calls == 1

    @pytest.mark.asyncio
    async def test_retries_transient_then_succeeds(self):
        provider = self._fake_provider([
            LLMResponse(finish_reason="error", error=LLMErrorInfo(should_retry=True)),
            LLMResponse(content="recovered"),
        ])
        resp = await provider.chat_with_retry([{"role": "user", "content": "hi"}])
        assert resp.content == "recovered"
        assert provider._calls == 2

    @pytest.mark.asyncio
    async def test_stops_on_non_transient(self):
        provider = self._fake_provider([
            LLMResponse(finish_reason="error", error=LLMErrorInfo(status_code=401)),
        ])
        resp = await provider.chat_with_retry([{"role": "user", "content": "hi"}])
        assert resp.finish_reason == "error"
        assert provider._calls == 1

    @pytest.mark.asyncio
    async def test_exhausts_attempts(self):
        provider = self._fake_provider([
            LLMResponse(finish_reason="error", error=LLMErrorInfo(kind="timeout")),
            LLMResponse(finish_reason="error", error=LLMErrorInfo(kind="timeout")),
            LLMResponse(finish_reason="error", error=LLMErrorInfo(kind="timeout")),
            LLMResponse(finish_reason="error", error=LLMErrorInfo(kind="timeout")),
        ])
        resp = await provider.chat_with_retry([{"role": "user", "content": "hi"}])
        assert resp.finish_reason == "error"
        # Default: len(_CHAT_RETRY_DELAYS) + 1 = 4 attempts

    @pytest.mark.asyncio
    async def test_respects_retry_after_from_error(self):
        provider = self._fake_provider([
            LLMResponse(
                finish_reason="error",
                error=LLMErrorInfo(should_retry=True, retry_after_s=0.01),
            ),
            LLMResponse(content="ok"),
        ])
        resp = await provider.chat_with_retry([{"role": "user", "content": "hi"}])
        assert resp.content == "ok"
        assert provider._calls == 2
