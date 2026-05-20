"""Provider registry — factory pattern.

Maps provider names to ``LLMProvider`` instances (or factories), allowing
the system to switch LLM backends by name (openai / anthropic / deepseek).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mxwbot.config.schema import ProviderConfig
from mxwbot.providers.base import LLMProvider


class ProviderRegistry:
    """Register and retrieve LLM providers by name.

    Usage::

        registry = ProviderRegistry()
        registry.register("openai", lambda: OpenAIProvider(config))
        provider = registry.get("openai")
    """

    def __init__(self) -> None:
        self._items: dict[str, LLMProvider | Callable[[], LLMProvider]] = {}

    # -- write --------------------------------------------------------------

    def register(
        self,
        name: str,
        factory_or_instance: LLMProvider | Callable[[], LLMProvider],
    ) -> None:
        """Register a provider instance or a factory callable."""
        self._items[name] = factory_or_instance

    @classmethod
    def from_configs(
        cls,
        configs: list[ProviderConfig],
        **global_kwargs: Any,
    ) -> ProviderRegistry:
        """Build a registry from a list of ``ProviderConfig`` objects.

        Each config is lazily instantiated on first ``get()``.
        """
        registry = cls()
        for cfg in configs:
            registry._register_config(cfg, global_kwargs)
        return registry

    def _register_config(
        self,
        cfg: ProviderConfig,
        global_kwargs: dict[str, Any],
    ) -> None:
        """Store a factory lambda for a ProviderConfig."""

        def _factory() -> LLMProvider:
            from mxwbot.providers.anthropic_provider import AnthropicProvider
            from mxwbot.providers.deepseek_provider import DeepSeekProvider
            from mxwbot.providers.openai_provider import OpenAIProvider

            name_lower = cfg.name.lower()
            if name_lower == "openai":
                return OpenAIProvider(cfg, **global_kwargs)
            elif name_lower == "anthropic":
                return AnthropicProvider(cfg, **global_kwargs)
            elif name_lower == "deepseek":
                return DeepSeekProvider(cfg, **global_kwargs)
            else:
                # Default: try OpenAI-compatible (many providers use this API)
                return OpenAIProvider(cfg, **global_kwargs)

        self._items[cfg.name] = _factory

    # -- read ---------------------------------------------------------------

    def get(self, name: str) -> LLMProvider:
        """Retrieve a provider by name, instantiating if necessary."""
        entry = self._items.get(name)
        if entry is None:
            available = ", ".join(self._items) or "(none)"
            raise KeyError(f"Unknown provider '{name}'. Available: {available}")
        if callable(entry):
            instance = entry()
            self._items[name] = instance
            return instance
        return entry

    def get_default(self) -> LLMProvider:
        """Return the first registered provider, or raise KeyError."""
        if not self._items:
            raise KeyError("No providers registered")
        first = next(iter(self._items))
        return self.get(first)

    def list_names(self) -> list[str]:
        """Return all registered provider names."""
        return list(self._items)

    def __contains__(self, name: str) -> bool:
        return name in self._items

    def __len__(self) -> int:
        return len(self._items)
