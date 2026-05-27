"""LLM Provider abstraction layer.

Defines the standard data structures and abstract base class that all
LLM providers must implement.
"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TokenUsage:
    """Token consumption for a single LLM call.

    Uses provider-neutral names.  *input_tokens* maps to OpenAI
    ``prompt_tokens`` / Anthropic ``input_tokens``; *output_tokens* maps
    to OpenAI ``completion_tokens`` / Anthropic ``output_tokens``.
    Provider-specific extra fields go into *extra*.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    extra: dict[str, int] = field(default_factory=dict)


@dataclass
class ToolCallRequest:
    """A tool call extracted from an LLM response.

    Attributes:
        id: Provider-assigned tool call id.
        name: Function/tool name.
        arguments: JSON-encoded argument string (not yet parsed).
    """

    id: str
    name: str
    arguments: str  # JSON string


@dataclass
class LLMErrorInfo:
    """Structured error metadata for retry / degradation decisions.

    When ``finish_reason == "error"`` this block carries the details;
    otherwise it is ``None``.
    """

    status_code: int | None = None
    kind: str | None = None           # "timeout", "connection", "api_error"
    type: str | None = None           # "insufficient_quota", "rate_limit_exceeded", …
    code: str | None = None           # Provider-specific error code
    retry_after_s: float | None = None
    should_retry: bool = False


@dataclass
class LLMResponse:
    """Complete (non-streaming) LLM response.

    When *finish_reason* is ``"stop"`` or ``"tool_calls"`` the response
    is considered successful.  When it is ``"error"``, consult *error*.
    """

    content: str | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: TokenUsage = field(default_factory=TokenUsage)
    retry_after: float | None = None           # Provider-supplied back-off hint
    reasoning_content: str | None = None       # DeepSeek-R1 / Kimi / MiMo
    thinking_blocks: list[dict] | None = None  # Anthropic extended thinking
    error: LLMErrorInfo | None = None

    @property
    def is_ok(self) -> bool:
        """True when the call succeeded (no error block)."""
        return self.error is None


@dataclass
class ToolCallDelta:
    """Incremental fragment of a tool call during streaming.

    The first chunk carries *id* and *name*; subsequent chunks carry only
    *arguments_delta*.  Callers accumulate these fragments to reconstruct
    the full ``ToolCallRequest``.
    """

    index: int
    id: str | None = None
    name: str | None = None
    arguments_delta: str = ""


@dataclass
class LLMStreamChunk:
    """A single chunk of a streaming LLM response.

    Exactly one of *delta* or *tool_call_delta* should be set.
    *finish_reason* is populated only on the terminal chunk.
    """

    delta: str | None = None
    tool_call_delta: ToolCallDelta | None = None
    finish_reason: str | None = None
    error: str | None = None  # Non-null when the stream terminated with an error
    reasoning_content: str | None = None  # DeepSeek-R1 thinking mode
    usage: TokenUsage | None = None  # Terminal chunk with token usage (streaming)


class LLMCallPurpose(Enum):
    """Purpose tag to prevent recursive memory injection.

    See :ref:`design-spec:memory-purpose-isolation`.
    """

    AGENT = "agent"
    SUBAGENT = "subagent"
    SUMMARY = "summary"
    SYSTEM = "system"


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class LLMProvider(ABC):
    """Abstract interface for an LLM backend.

    Subclasses must implement at least ``_chat_impl`` and
    ``_chat_stream_impl``.  The public ``chat`` / ``chat_stream`` methods
    handle pre/post processing so subclasses only deal with provider-specific
    calling conventions.
    """

    def __init__(self, model: str, **kwargs: Any) -> None:
        self._model = model
        self._kwargs = kwargs

    @property
    def model(self) -> str:
        return self._model

    # -- public API ---------------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """Non-streaming completion.  Exceptions are caught and returned
        as ``LLMResponse(finish_reason="error")`` — callers never need to
        catch provider-specific exceptions.
        """
        return await self._safe_chat(messages, tools)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        """Streaming completion.  On error the final chunk carries
        ``error`` + ``finish_reason="error"``.
        """
        async for chunk in self._safe_chat_stream(messages, tools):
            yield chunk

    # -- subclasses must implement ------------------------------------------

    @abstractmethod
    async def _chat_impl(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> LLMResponse: ...

    @abstractmethod
    async def _chat_stream_impl(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> AsyncIterator[LLMStreamChunk]: ...

    @property
    @abstractmethod
    def supports_streaming(self) -> bool:
        """Whether this provider supports streaming."""
        ...

    # -- error handling -----------------------------------------------------

    async def _safe_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> LLMResponse:
        """Call ``_chat_impl``, converting all exceptions to
        ``LLMResponse(finish_reason="error")`` so callers never deal with
        provider-specific exception types.
        """
        try:
            return await self._chat_impl(messages, tools)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._map_exception(exc)

    async def _safe_chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> AsyncIterator[LLMStreamChunk]:
        """Call ``_chat_stream_impl``, converting exceptions to an error chunk."""
        try:
            async for chunk in self._chat_stream_impl(messages, tools):
                yield chunk
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            err_resp = self._map_exception(exc)
            yield LLMStreamChunk(
                error=err_resp.content or str(exc),
                finish_reason="error",
            )

    def _map_exception(self, exc: Exception) -> LLMResponse:
        """Map a provider-specific exception to an ``LLMResponse`` error.

        The default implementation produces a generic error.  Subclasses
        should override to detect ``RateLimitError``, ``APITimeoutError``,
        ``APIError``, etc. and fill in ``LLMErrorInfo`` accordingly.
        """
        return LLMResponse(
            finish_reason="error",
            content=str(exc),
            error=LLMErrorInfo(kind="api_error", should_retry=False),
        )

    @classmethod
    def _is_transient_error(cls, response: LLMResponse) -> bool:
        """Return True if *response* represents a transient failure worth retrying."""
        if response.error is None:
            return False
        if response.error.should_retry:
            return True
        if response.error.status_code is not None:
            if response.error.status_code == 429:
                return cls._is_retryable_429(response)
            if response.error.status_code >= 500:
                return True
        if response.error.kind in ("timeout", "connection"):
            return True
        return False

    @classmethod
    def _is_retryable_429(cls, response: LLMResponse) -> bool:
        """Distinguish rate-limit 429 (retryable) from quota-exceeded 429 (fatal)."""
        if response.error is None:
            return True  # 429 with no details → assume retryable
        non_retryable = {
            "insufficient_quota", "quota_exceeded", "quota_exhausted",
            "insufficient_balance", "billing_not_active", "payment_required",
        }
        error_type = (response.error.type or "").lower()
        error_code = (response.error.code or "").lower()
        if error_type in non_retryable or error_code in non_retryable:
            return False
        retryable = {
            "rate_limit_exceeded", "too_many_requests", "request_limit_exceeded",
        }
        if error_type in retryable or error_code in retryable:
            return True
        # Unknown 429 → assume retryable
        return True

    # -- retry logic --------------------------------------------------------

    _CHAT_RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)
    _PERSISTENT_MAX_DELAY: float = 60.0

    async def chat_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_attempts: int | None = None,
    ) -> LLMResponse:
        """Non-streaming chat with automatic retry on transient failures.

        Defaults to ``len(_CHAT_RETRY_DELAYS) + 1`` attempts with
        back-off (1s → 2s → 4s).  Retries only when ``_is_transient_error``
        returns True; gives up immediately on auth / quota errors.
        """
        return await self._run_with_retry(
            lambda: self._safe_chat(messages, tools),
            max_attempts=max_attempts,
        )

    async def _run_with_retry(
        self,
        call: Callable[[], Awaitable[LLMResponse]],
        *,
        max_attempts: int | None = None,
    ) -> LLMResponse:
        """Execute *call* with retry on transient failures.

        Retry delays are taken from ``_CHAT_RETRY_DELAYS``, with per-attempt
        back-off overridden by the error's ``retry_after_s`` when available.
        """
        delays = list(self._CHAT_RETRY_DELAYS)
        if max_attempts is None:
            max_attempts = len(delays) + 1

        last_response: LLMResponse | None = None
        for attempt in range(max_attempts):
            response = await call()
            if response.finish_reason != "error":
                return response

            last_response = response
            if not self._is_transient_error(response):
                return response  # Not retryable — give up immediately

            # Determine delay
            delay: float | None = None
            if response.error and response.error.retry_after_s:
                delay = response.error.retry_after_s
            elif response.retry_after:
                delay = response.retry_after
            if delay is None and attempt < len(delays):
                delay = delays[attempt]
            if delay is None:
                return response  # No more attempts

            await asyncio.sleep(delay)

        return last_response  # type: ignore[return-value]

    # -- helpers for subclasses ---------------------------------------------

    @staticmethod
    def _tools_to_openai(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        """Convert internal tool schema → OpenAI-compatible format if needed.

        The internal format is already OpenAI-compatible, so this is a
        pass-through.  Subclasses for non-OpenAI APIs (e.g. Anthropic)
        override with their own conversion.
        """
        return tools

    @staticmethod
    def _openai_tools_to_anthropic(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        """Convert OpenAI tool schema → Anthropic tool schema."""
        if not tools:
            return None
        result: list[dict[str, Any]] = []
        for t in tools:
            func = t.get("function", {})
            result.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {}),
            })
        return result
