"""Tests for providers/deepseek_provider.py — mock-based integration tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mxwbot.config.schema import ProviderConfig
from mxwbot.providers.deepseek_provider import DeepSeekProvider
from mxwbot.providers.base import LLMResponse


class TestDeepSeekProvider:
    def test_defaults_applied(self):
        """When config has no model/base_url, DeepSeek defaults are used."""
        cfg = ProviderConfig(name="deepseek", api_key="sk-test")  # type: ignore[arg-type]
        provider = DeepSeekProvider(cfg)
        assert provider.model == DeepSeekProvider.DEFAULT_MODEL

    def test_explicit_overrides(self):
        cfg = ProviderConfig(
            name="deepseek", api_key="sk-test",
            model="deepseek-v3", base_url="https://custom/api",
        )  # type: ignore[arg-type]
        provider = DeepSeekProvider(cfg)
        assert provider.model == "deepseek-v3"

    @pytest.mark.asyncio
    async def test_chat_uses_openai_api(self):
        cfg = ProviderConfig(name="deepseek", api_key="sk-test", model="deepseek-chat")  # type: ignore[arg-type]
        provider = DeepSeekProvider(cfg)

        msg = MagicMock(content="hello from deepseek", tool_calls=None)
        choice = MagicMock(finish_reason="stop", message=msg)
        usage = MagicMock(prompt_tokens=5, completion_tokens=3, total_tokens=8)
        mock_resp = MagicMock(choices=[choice], model="deepseek-chat", usage=usage)

        mock_create = AsyncMock(return_value=mock_resp)
        with patch.object(provider._client.chat.completions, "create", mock_create):
            resp = await provider.chat([{"role": "user", "content": "hi"}])

        assert isinstance(resp, LLMResponse)
        assert resp.content == "hello from deepseek"
        assert resp.is_ok is True

    def test_supports_streaming(self):
        cfg = ProviderConfig(name="deepseek", api_key="sk-test", model="deepseek-chat")  # type: ignore[arg-type]
        assert DeepSeekProvider(cfg).supports_streaming is True
