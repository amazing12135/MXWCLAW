"""DeepSeek provider — OpenAI-compatible API backend.

DeepSeek uses the same API shape as OpenAI, so we re-use ``OpenAIProvider``
with the default base_url set to DeepSeek's endpoint.
"""

from __future__ import annotations

from typing import Any

from mxwbot.config.schema import ProviderConfig
from mxwbot.providers.openai_provider import OpenAIProvider


class DeepSeekProvider(OpenAIProvider):
    """DeepSeek LLM backend (OpenAI-compatible API)."""

    DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
    DEFAULT_MODEL = "deepseek-chat"

    def __init__(self, config: ProviderConfig, **global_kwargs: Any) -> None:
        if not config.base_url:
            config = config.model_copy(update={"base_url": self.DEFAULT_BASE_URL})
        if not config.model:
            config = config.model_copy(update={"model": self.DEFAULT_MODEL})
        super().__init__(config, **global_kwargs)
