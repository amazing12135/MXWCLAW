"""OpenAI provider — adapts ``openai.AsyncOpenAI`` to ``LLMProvider``.

Also serves as the base for any OpenAI-compatible API (e.g. DeepSeek,
local vLLM / Ollama).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import openai
from openai import AsyncOpenAI

from mxwbot.config.schema import ProviderConfig
from mxwbot.providers.base import (
    LLMErrorInfo,
    LLMProvider,
    LLMResponse,
    LLMStreamChunk,
    ToolCallDelta,
    ToolCallRequest,
    TokenUsage,
)


class OpenAIProvider(LLMProvider):
    """OpenAI (and OpenAI-compatible) LLM backend."""

    def __init__(self, config: ProviderConfig, **global_kwargs: Any) -> None:
        super().__init__(model=config.model or "gpt-4", **global_kwargs)
        kw: dict[str, Any] = {"api_key": config.api_key.get_secret_value()}
        if config.base_url:
            kw["base_url"] = config.base_url
        self._client = AsyncOpenAI(**kw)
        self._max_tokens = config.max_tokens
        self._temperature = config.temperature

    @property
    def supports_streaming(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Error mapping
    # ------------------------------------------------------------------

    def _map_exception(self, exc: Exception) -> LLMResponse:
        """Map OpenAI SDK exceptions → LLMResponse with structured error."""
        # Rate limit
        if isinstance(exc, openai.RateLimitError):
            info = self._extract_error_info(exc)
            info.kind = info.kind or "api_error"
            info.type = info.type or "rate_limit_exceeded"
            info.should_retry = True
            if info.status_code is None:
                info.status_code = 429
            return LLMResponse(
                finish_reason="error",
                content=str(exc),
                retry_after=info.retry_after_s,
                error=info,
            )

        # Timeout
        if isinstance(exc, openai.APITimeoutError):
            return LLMResponse(
                finish_reason="error",
                content=str(exc),
                error=LLMErrorInfo(kind="timeout", should_retry=True),
            )

        # Connection
        if isinstance(exc, openai.APIConnectionError):
            return LLMResponse(
                finish_reason="error",
                content=str(exc),
                error=LLMErrorInfo(kind="connection", should_retry=True),
            )

        # Authentication
        if isinstance(exc, openai.AuthenticationError):
            return LLMResponse(
                finish_reason="error",
                content=str(exc),
                error=LLMErrorInfo(kind="api_error", type="auth_error",
                                   status_code=401, should_retry=False),
            )

        # Generic API error
        if isinstance(exc, openai.APIError):
            info = self._extract_error_info(exc)
            info.kind = info.kind or "api_error"
            if info.status_code is None and getattr(exc, "status_code", None):
                info.status_code = exc.status_code
            return LLMResponse(
                finish_reason="error",
                content=str(exc),
                error=info,
            )

        # Unknown exception — fall through to base
        return super()._map_exception(exc)

    @staticmethod
    def _extract_error_info(exc: Exception) -> LLMErrorInfo:
        """Pull structured error details from an OpenAI SDK exception."""
        info = LLMErrorInfo()
        body: dict[str, Any] | None = None

        if hasattr(exc, "response"):
            resp = exc.response  # type: ignore[attr-defined]
            if hasattr(resp, "status_code"):
                info.status_code = resp.status_code
            if hasattr(resp, "headers"):
                headers = resp.headers
                retry_after = headers.get("retry-after-ms") or headers.get("Retry-After")
                if retry_after:
                    try:
                        ms = float(retry_after)
                        info.retry_after_s = (ms / 1000.0) if ms > 100 else ms
                    except (ValueError, TypeError):
                        pass
            if hasattr(resp, "json"):
                try:
                    body = resp.json()
                except Exception:
                    pass

        if body is None and hasattr(exc, "body") and exc.body:  # type: ignore[attr-defined]
            body = exc.body  # type: ignore[attr-defined]

        if isinstance(body, dict):
            error_obj = body.get("error") or {}
            if isinstance(error_obj, dict):
                info.type = error_obj.get("type")
                info.code = error_obj.get("code")

        return info

    # ------------------------------------------------------------------

    async def _chat_impl(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> LLMResponse:
        kwargs = self._build_request_kwargs(messages, tools, stream=False)
        resp = await self._client.chat.completions.create(**kwargs)
        return self._convert_response(resp)

    async def _chat_stream_impl(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> AsyncIterator[LLMStreamChunk]:
        kwargs = self._build_request_kwargs(messages, tools, stream=True)
        stream = await self._client.chat.completions.create(**kwargs)
        async for event in stream:
            for chunk in self._convert_stream_event(event):
                yield chunk

    # -- internal -----------------------------------------------------------

    def _build_request_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        stream: bool,
    ) -> dict[str, Any]:
        kw: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": stream,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        }
        if tools:
            kw["tools"] = self._tools_to_openai(tools)
        return kw

    @staticmethod
    def _convert_response(resp: Any) -> LLMResponse:
        choice = resp.choices[0]
        msg = choice.message
        tool_calls: list[ToolCallRequest] = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append(
                    ToolCallRequest(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=tc.function.arguments,
                    )
                )
        usage = TokenUsage()
        if resp.usage:
            usage = TokenUsage(
                input_tokens=resp.usage.prompt_tokens or 0,
                output_tokens=resp.usage.completion_tokens or 0,
            )
        return LLMResponse(
            content=msg.content or None,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=choice.finish_reason or "stop",
        )

    @staticmethod
    def _convert_stream_event(event: Any) -> list[LLMStreamChunk]:
        """Convert a single OpenAI stream chunk → one or more LLMStreamChunk."""
        chunks: list[LLMStreamChunk] = []
        if not event.choices:
            return chunks

        choice = event.choices[0]
        delta = choice.delta

        # Text delta
        if delta and delta.content:
            chunks.append(LLMStreamChunk(delta=delta.content))

        # Tool call deltas
        if delta and delta.tool_calls:
            for tc_delta in delta.tool_calls:
                chunks.append(
                    LLMStreamChunk(
                        tool_call_delta=ToolCallDelta(
                            index=tc_delta.index,
                            id=tc_delta.id,
                            name=tc_delta.function.name if tc_delta.function else None,
                            arguments_delta=(
                                tc_delta.function.arguments if tc_delta.function else ""
                            ),
                        )
                    )
                )

        # Finish reason (terminal chunk)
        if choice.finish_reason:
            chunks.append(LLMStreamChunk(finish_reason=choice.finish_reason))

        return chunks
