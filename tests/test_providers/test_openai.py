"""Tests for providers/openai_provider.py — mock-based integration tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mxwbot.config.schema import ProviderConfig
from mxwbot.providers.base import LLMResponse
from mxwbot.providers.openai_provider import OpenAIProvider


def _make_provider(model: str = "gpt-4") -> OpenAIProvider:
    cfg = ProviderConfig(name="openai", api_key="sk-test", model=model)  # type: ignore[arg-type]
    return OpenAIProvider(cfg)


def _mock_choice(content=None, tool_calls=None, finish_reason="stop"):
    """Build a SimpleNamespace that mimics an OpenAI stream/response choice."""
    msg = SimpleNamespace(content=content, tool_calls=tool_calls, role="assistant")
    return SimpleNamespace(message=msg, finish_reason=finish_reason, index=0)


class TestOpenAIProvider:
    @pytest.mark.asyncio
    async def test_chat_returns_llm_response(self):
        provider = _make_provider()
        choice = _mock_choice(content="hello world")
        mock_resp = SimpleNamespace(
            id="chatcmpl-123", choices=[choice], model="gpt-4",
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )
        with patch.object(provider._client.chat.completions, "create", AsyncMock(return_value=mock_resp)):
            resp = await provider.chat([{"role": "user", "content": "hi"}])

        assert isinstance(resp, LLMResponse)
        assert resp.content == "hello world"
        assert resp.finish_reason == "stop"
        assert resp.is_ok is True
        assert resp.usage.input_tokens == 10
        assert resp.usage.output_tokens == 5

    @pytest.mark.asyncio
    async def test_chat_stream_yields_chunks(self):
        provider = _make_provider()

        # Build a sync iterable that mimics OpenAI's async stream
        events = [
            SimpleNamespace(choices=[
                SimpleNamespace(delta=SimpleNamespace(content="hello", tool_calls=None), finish_reason=None, index=0)
            ]),
            SimpleNamespace(choices=[
                SimpleNamespace(delta=SimpleNamespace(content=" world", tool_calls=None), finish_reason="stop", index=0)
            ]),
        ]

        class _FakeStream:
            def __aiter__(self):
                self._it = iter(events)
                return self

            async def __anext__(self):
                try:
                    return next(self._it)
                except StopIteration:
                    raise StopAsyncIteration

        with patch.object(provider._client.chat.completions, "create", AsyncMock(return_value=_FakeStream())):
            chunks = []
            async for chunk in provider.chat_stream([{"role": "user", "content": "hi"}]):
                chunks.append(chunk)

        texts = [c.delta for c in chunks if c.delta]
        assert "".join(texts) == "hello world"
        assert any(c.finish_reason == "stop" for c in chunks)

    @pytest.mark.asyncio
    async def test_chat_with_tool_calls(self):
        provider = _make_provider()

        func = SimpleNamespace(name="read_file", arguments='{"path":"/x"}')
        tc = SimpleNamespace(id="call_1", function=func)
        choice = _mock_choice(content=None, tool_calls=[tc], finish_reason="tool_calls")
        mock_resp = SimpleNamespace(
            choices=[choice], model="gpt-4",
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )
        with patch.object(provider._client.chat.completions, "create", AsyncMock(return_value=mock_resp)):
            resp = await provider.chat([{"role": "user", "content": "read /x"}])

        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "read_file"
        assert resp.tool_calls[0].arguments == '{"path":"/x"}'

    def test_supports_streaming(self):
        assert _make_provider().supports_streaming is True


class TestOpenAIErrorMapping:
    """Verify that SDK exceptions are converted to LLMResponse with LLMErrorInfo."""

    def _make_provider(self):
        cfg = ProviderConfig(name="openai", api_key="sk-test", model="gpt-4")  # type: ignore[arg-type]
        return OpenAIProvider(cfg)

    @pytest.mark.asyncio
    async def test_rate_limit_error(self):
        import openai
        provider = self._make_provider()
        exc = openai.RateLimitError(
            message="Rate limit exceeded",
            response=MagicMock(status_code=429, headers={}, json=MagicMock(return_value={"error": {"type": "rate_limit_exceeded"}})),
            body={"error": {"type": "rate_limit_exceeded"}},
        )
        with patch.object(provider._client.chat.completions, "create", AsyncMock(side_effect=exc)):
            resp = await provider.chat([{"role": "user", "content": "hi"}])

        assert resp.finish_reason == "error"
        assert resp.error is not None
        assert resp.error.status_code == 429
        assert resp.error.should_retry is True

    @pytest.mark.asyncio
    async def test_timeout_error(self):
        import openai
        provider = self._make_provider()
        exc = openai.APITimeoutError(request=MagicMock())
        with patch.object(provider._client.chat.completions, "create", AsyncMock(side_effect=exc)):
            resp = await provider.chat([{"role": "user", "content": "hi"}])

        assert resp.finish_reason == "error"
        assert resp.error.kind == "timeout"
        assert resp.error.should_retry is True

    @pytest.mark.asyncio
    async def test_auth_error_not_retryable(self):
        import openai
        provider = self._make_provider()
        exc = openai.AuthenticationError(
            message="Invalid API key",
            response=MagicMock(status_code=401, headers={}, json=MagicMock(return_value={})),
            body={},
        )
        with patch.object(provider._client.chat.completions, "create", AsyncMock(side_effect=exc)):
            resp = await provider.chat([{"role": "user", "content": "hi"}])

        assert resp.finish_reason == "error"
        assert resp.error.should_retry is False
        assert resp.error.status_code == 401

    @pytest.mark.asyncio
    async def test_server_error_retryable(self):
        provider = self._make_provider()
        # Simulate a 500 via RateLimitError with 500 status (tests the generic path)
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.headers = {}
        mock_resp.json.return_value = {}

        import openai
        exc = openai.RateLimitError(
            message="Internal server error",
            response=mock_resp,
            body={},
        )
        with patch.object(provider._client.chat.completions, "create", AsyncMock(side_effect=exc)):
            resp = await provider.chat([{"role": "user", "content": "hi"}])

        assert resp.finish_reason == "error"
        # 500 → transient by _is_transient_error
        from mxwbot.providers.base import LLMProvider
        assert LLMProvider._is_transient_error(resp) is True
