"""Tests for providers/anthropic_provider.py — mock-based integration tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mxwbot.config.schema import ProviderConfig
from mxwbot.providers.anthropic_provider import AnthropicProvider
from mxwbot.providers.base import LLMResponse


def _make_provider(model: str = "claude-opus-4-6") -> AnthropicProvider:
    cfg = ProviderConfig(name="anthropic", api_key="sk-test", model=model)  # type: ignore[arg-type]
    return AnthropicProvider(cfg)


class TestAnthropicProvider:
    @pytest.mark.asyncio
    async def test_chat_returns_llm_response(self):
        provider = _make_provider()
        mock_resp = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="hello from claude")],
            stop_reason="end_turn",
            model="claude-opus-4-6",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )
        with patch.object(provider._client.messages, "create", AsyncMock(return_value=mock_resp)):
            resp = await provider.chat([{"role": "user", "content": "hi"}])

        assert isinstance(resp, LLMResponse)
        assert resp.content == "hello from claude"
        assert resp.finish_reason == "end_turn"
        assert resp.is_ok is True
        assert resp.usage.input_tokens == 10
        assert resp.usage.output_tokens == 5

    @pytest.mark.asyncio
    async def test_chat_with_system_message(self):
        provider = _make_provider()
        mock_resp = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            stop_reason="end_turn", model="c", usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )
        mock_create = AsyncMock(return_value=mock_resp)
        with patch.object(provider._client.messages, "create", mock_create):
            await provider.chat([
                {"role": "system", "content": "You are helpful"},
                {"role": "user", "content": "hi"},
            ])
            kwargs = mock_create.call_args.kwargs
            assert kwargs["system"] == "You are helpful"
            assert len(kwargs["messages"]) == 1

    @pytest.mark.asyncio
    async def test_chat_with_tool_use(self):
        provider = _make_provider()
        mock_resp = SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="Let me read"),
                SimpleNamespace(type="tool_use", id="toolu_1", name="read_file", input={"path": "/x"}),
            ],
            stop_reason="tool_use", model="claude-opus-4-6",
            usage=SimpleNamespace(input_tokens=10, output_tokens=10),
        )
        with patch.object(provider._client.messages, "create", AsyncMock(return_value=mock_resp)):
            resp = await provider.chat(
                [{"role": "user", "content": "read /x"}],
                tools=[{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
            )
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "read_file"
        assert resp.tool_calls[0].id == "toolu_1"

    @pytest.mark.asyncio
    async def test_chat_stream_yields_chunks(self):
        provider = _make_provider()

        # Build a plain async iterator (not async generator)
        events = [
            SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="hello"), index=0),
            SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=" world"), index=0),
            SimpleNamespace(type="message_delta", delta=SimpleNamespace(stop_reason="end_turn"), usage=SimpleNamespace(output_tokens=5)),
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

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        with patch.object(provider._client.messages, "stream", return_value=_FakeStream()):
            chunks = []
            async for chunk in provider.chat_stream([{"role": "user", "content": "hi"}]):
                chunks.append(chunk)

        texts = [c.delta for c in chunks if c.delta]
        assert "".join(texts) == "hello world"
        assert any(c.finish_reason == "end_turn" for c in chunks)

    def test_supports_streaming(self):
        assert _make_provider().supports_streaming is True


class TestAnthropicErrorMapping:
    """Verify that Anthropic SDK exceptions are converted to LLMResponse with LLMErrorInfo."""

    def _make_provider(self):
        cfg = ProviderConfig(name="anthropic", api_key="sk-test", model="claude-opus-4-6")  # type: ignore[arg-type]
        return AnthropicProvider(cfg)

    @pytest.mark.asyncio
    async def test_rate_limit_error(self):
        import anthropic
        provider = self._make_provider()
        exc = anthropic.RateLimitError(
            message="Rate limit exceeded",
            response=MagicMock(status_code=429, headers={}, json=MagicMock(return_value={"error": {"type": "rate_limit_exceeded"}})),
            body={"error": {"type": "rate_limit_exceeded"}},
        )
        with patch.object(provider._client.messages, "create", AsyncMock(side_effect=exc)):
            resp = await provider.chat([{"role": "user", "content": "hi"}])

        assert resp.finish_reason == "error"
        assert resp.error is not None
        assert resp.error.status_code == 429
        assert resp.error.should_retry is True

    @pytest.mark.asyncio
    async def test_overloaded_error(self):
        import anthropic
        provider = self._make_provider()
        exc = anthropic.APIStatusError(
            message="Overloaded",
            response=MagicMock(status_code=529, headers={}, json=MagicMock(return_value={"error": {"type": "overloaded_error"}})),
            body={"error": {"type": "overloaded_error"}},
        )
        with patch.object(provider._client.messages, "create", AsyncMock(side_effect=exc)):
            resp = await provider.chat([{"role": "user", "content": "hi"}])

        assert resp.finish_reason == "error"
        assert resp.error.status_code == 529

    @pytest.mark.asyncio
    async def test_auth_error_not_retryable(self):
        import anthropic
        provider = self._make_provider()
        exc = anthropic.AuthenticationError(
            message="Invalid API key",
            response=MagicMock(status_code=401, headers={}, json=MagicMock(return_value={})),
            body={},
        )
        with patch.object(provider._client.messages, "create", AsyncMock(side_effect=exc)):
            resp = await provider.chat([{"role": "user", "content": "hi"}])

        assert resp.finish_reason == "error"
        assert resp.error.should_retry is False
        assert resp.error.status_code == 401
