"""Tests for providers/registry.py."""

import pytest

from mxwbot.config.schema import ProviderConfig
from mxwbot.providers.base import LLMProvider, LLMResponse
from mxwbot.providers.registry import ProviderRegistry


class FakeProvider(LLMProvider):
    """A fake provider for testing the registry."""

    @property
    def supports_streaming(self) -> bool:
        return True

    async def _chat_impl(self, messages, tools):
        return LLMResponse(content="fake response")

    async def _chat_stream_impl(self, messages, tools):
        # Async generator — no need to yield anything for tests
        if False:
            yield
        return


class TestProviderRegistry:
    def test_register_and_get(self):
        reg = ProviderRegistry()
        reg.register("test", FakeProvider(model="test-model"))
        p = reg.get("test")
        assert isinstance(p, FakeProvider)
        assert p.model == "test-model"

    def test_register_factory_lazy(self):
        reg = ProviderRegistry()
        called = False

        def factory():
            nonlocal called
            called = True
            return FakeProvider(model="lazy")

        reg.register("lazy", factory)
        assert not called
        p = reg.get("lazy")
        assert called
        assert p.model == "lazy"

    def test_get_unknown_raises(self):
        reg = ProviderRegistry()
        with pytest.raises(KeyError, match="Unknown provider"):
            reg.get("nonexistent")

    def test_get_default(self):
        reg = ProviderRegistry()
        reg.register("first", FakeProvider(model="m1"))
        reg.register("second", FakeProvider(model="m2"))
        p = reg.get_default()
        assert p.model == "m1"

    def test_get_default_empty_raises(self):
        reg = ProviderRegistry()
        with pytest.raises(KeyError, match="No providers"):
            reg.get_default()

    def test_list_names(self):
        reg = ProviderRegistry()
        reg.register("a", FakeProvider(model="x"))
        reg.register("b", FakeProvider(model="y"))
        names = reg.list_names()
        assert set(names) == {"a", "b"}

    def test_contains(self):
        reg = ProviderRegistry()
        reg.register("x", FakeProvider(model="m"))
        assert "x" in reg
        assert "y" not in reg

    def test_from_configs(self):
        configs = [
            ProviderConfig(name="fake1", api_key="sk-1", model="gpt-4"),
            ProviderConfig(name="fake2", api_key="sk-2", model="claude"),
        ]
        # Since "fake1" doesn't match openai/anthropic/deepseek, it falls back to OpenAIProvider
        reg = ProviderRegistry.from_configs(configs)
        assert len(reg) == 2
        assert "fake1" in reg
        assert "fake2" in reg
