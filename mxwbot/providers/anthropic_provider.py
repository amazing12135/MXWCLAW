"""Anthropic provider — adapts ``anthropic.AsyncAnthropic`` to ``LLMProvider``."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import anthropic
from anthropic import AsyncAnthropic

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


class AnthropicProvider(LLMProvider):
    """Anthropic Claude LLM backend."""

    def __init__(self, config: ProviderConfig, **global_kwargs: Any) -> None:
        super().__init__(model=config.model or "claude-sonnet-4-6", **global_kwargs)
        kw: dict[str, Any] = {"api_key": config.api_key.get_secret_value()}
        if config.base_url:
            kw["base_url"] = config.base_url
        self._client = AsyncAnthropic(**kw)
        self._max_tokens = config.max_tokens
        self._temperature = config.temperature

    @property
    def supports_streaming(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Error mapping
    # ------------------------------------------------------------------

    def _map_exception(self, exc: Exception) -> LLMResponse:
        """Map Anthropic SDK exceptions → LLMResponse with structured error."""
        if isinstance(exc, anthropic.RateLimitError):
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

        if isinstance(exc, anthropic.APITimeoutError):
            return LLMResponse(
                finish_reason="error",
                content=str(exc),
                error=LLMErrorInfo(kind="timeout", should_retry=True),
            )

        if isinstance(exc, anthropic.APIConnectionError):
            return LLMResponse(
                finish_reason="error",
                content=str(exc),
                error=LLMErrorInfo(kind="connection", should_retry=True),
            )

        if isinstance(exc, anthropic.AuthenticationError):
            return LLMResponse(
                finish_reason="error",
                content=str(exc),
                error=LLMErrorInfo(kind="api_error", type="auth_error",
                                   status_code=401, should_retry=False),
            )

        if isinstance(exc, anthropic.APIStatusError):
            info = self._extract_error_info(exc)
            info.kind = info.kind or "api_error"
            if info.status_code is None:
                info.status_code = getattr(exc, "status_code", None)
            return LLMResponse(
                finish_reason="error",
                content=str(exc),
                error=info,
            )

        return super()._map_exception(exc)

    @staticmethod
    def _extract_error_info(exc: Exception) -> LLMErrorInfo:
        """Pull structured error details from an Anthropic SDK exception."""
        info = LLMErrorInfo()
        body: dict[str, Any] | None = None

        if hasattr(exc, "response"):
            resp = exc.response  # type: ignore[attr-defined]
            if hasattr(resp, "status_code"):
                info.status_code = resp.status_code
            if hasattr(resp, "headers"):
                headers = resp.headers
                retry_after = headers.get("retry-after") or headers.get("Retry-After")
                if retry_after:
                    try:
                        info.retry_after_s = float(retry_after)
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
        system, user_messages = self._split_system(messages)
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": user_messages,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        }
        if system:
            kwargs["system"] = system
        anthropic_tools = self._openai_tools_to_anthropic(tools)
        if anthropic_tools:
            kwargs["tools"] = anthropic_tools

        resp = await self._client.messages.create(**kwargs)
        return self._convert_response(resp)

    async def _chat_stream_impl(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> AsyncIterator[LLMStreamChunk]:
        system, user_messages = self._split_system(messages)
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": user_messages,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        }
        if system:
            kwargs["system"] = system
        anthropic_tools = self._openai_tools_to_anthropic(tools)
        if anthropic_tools:
            kwargs["tools"] = anthropic_tools

        async with self._client.messages.stream(**kwargs) as stream:
            async for event in stream:
                for chunk in self._convert_stream_event(event):
                    yield chunk

    # -- internal -----------------------------------------------------------

    @staticmethod
    def _split_system(
        messages: list[dict[str, Any]],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Extract the system message (Anthropic sends it separately)."""
        system_text: str | None = None
        user_messages: list[dict[str, Any]] = []
        for m in messages:
            if m.get("role") == "system" and user_messages == []:
                system_text = str(m.get("content", ""))
            else:
                user_messages.append(m)
        return system_text, user_messages

    @staticmethod
    def _convert_response(resp: Any) -> LLMResponse:
        content_text = ""
        tool_calls: list[ToolCallRequest] = []
        for block in resp.content:
            if block.type == "text":
                content_text += block.text
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCallRequest(
                        id=block.id,
                        name=block.name,
                        arguments=json.dumps(block.input, ensure_ascii=False),
                    )
                )

        usage = TokenUsage(
            input_tokens=resp.usage.input_tokens or 0,
            output_tokens=resp.usage.output_tokens or 0,
        )

        return LLMResponse(
            content=content_text or None,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=resp.stop_reason or "stop",
        )

    @staticmethod
    def _convert_stream_event(event: Any) -> list[LLMStreamChunk]:
        """Convert an Anthropic stream event → one or more LLMStreamChunk."""
        chunks: list[LLMStreamChunk] = []

        event_type = getattr(event, "type", None)

        if event_type == "content_block_delta":
            delta = event.delta
            if delta.type == "text_delta":
                chunks.append(LLMStreamChunk(delta=delta.text))
            elif delta.type == "input_json_delta":
                # Anthropic sends tool arguments as partial_json
                chunks.append(
                    LLMStreamChunk(
                        tool_call_delta=ToolCallDelta(
                            index=getattr(event, "index", 0),
                            arguments_delta=delta.partial_json,
                        )
                    )
                )

        elif event_type == "content_block_start":
            block = event.content_block
            if block.type == "tool_use":
                chunks.append(
                    LLMStreamChunk(
                        tool_call_delta=ToolCallDelta(
                            index=getattr(event, "index", 0),
                            id=block.id,
                            name=block.name,
                        )
                    )
                )

        elif event_type == "message_delta":
            if hasattr(event.delta, "stop_reason") and event.delta.stop_reason:
                chunks.append(LLMStreamChunk(finish_reason=event.delta.stop_reason))

        return chunks
