"""AgentRunner — 核心 ReAct 循环引擎。

LLM 推理 → 工具调用 → 结果注入 → 再推理，最多 max_iterations 轮。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

from mxwbot.providers.base import (
    LLMProvider,
    LLMResponse,
    LLMStreamChunk,
    TokenUsage,
    ToolCallRequest,
)
from mxwbot.core.tools.base import ToolResult
from mxwbot.core.tools.register import ToolRegistry


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class AgentRunSpec:
    """单次 Agent 执行的配置。"""

    provider: LLMProvider
    tools: ToolRegistry
    max_iterations: int = 3
    checkpoint_interval: int = 2
    bus: Any = None   # MessageBus（用于确认弹窗）

    # Optional hooks
    hook: Any = None

    # Checkpoint callback (runner 不直接操作 CheckpointManager)
    on_checkpoint: Any = None


@dataclass
class AgentRunResult:
    """Agent 执行结果。"""

    content: str = ""
    tool_calls_made: int = 0
    iterations: int = 0
    usage: TokenUsage = field(default_factory=TokenUsage)
    finish_reason: str = "stop"


class AgentRunner:
    """纯 ReAct 循环引擎。"""

    async def run(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> AgentRunResult:
        """执行 ReAct 循环。

        Args:
            spec: 执行配置（provider/tools/hook/max_iterations）。
            messages: 初始消息列表（由 ContextBuilder 组装）。

        Returns:
            AgentRunResult。
        """
        tools_schema = spec.tools.to_openai_schema() if spec.tools else None
        total_usage = TokenUsage()
        total_tool_calls = 0
        iteration = 0

        while iteration < spec.max_iterations:
            iteration += 1

            # Hook: before LLM call
            if spec.hook:
                await spec.hook.before_llm_call(messages, tools_schema)

            # LLM 调用
            response = await spec.provider.chat(messages, tools=tools_schema)

            # 累计 token
            total_usage.input_tokens += response.usage.input_tokens
            total_usage.output_tokens += response.usage.output_tokens

            # Hook: after LLM response
            if spec.hook:
                await spec.hook.on_llm_response(response)

            # 错误检查
            if not response.is_ok:
                return AgentRunResult(
                    content=response.content or "",
                    tool_calls_made=total_tool_calls,
                    iterations=iteration,
                    usage=total_usage,
                    finish_reason="error",
                )

            # 无工具调用 → 返回最终内容
            if not response.tool_calls:
                return AgentRunResult(
                    content=response.content or "",
                    tool_calls_made=total_tool_calls,
                    iterations=iteration,
                    usage=total_usage,
                    finish_reason="stop",
                )

            # 有工具调用
            tool_calls = response.tool_calls
            total_tool_calls += len(tool_calls)

            # 检查是否有写操作（需要确认）
            writable_tools = [
                tc for tc in tool_calls
                if tc.name in spec.tools and not spec.tools.get(tc.name).is_readonly
            ]

            if writable_tools and spec.bus:
                # 批量确认：一次 iteration 只弹一次
                req_id = _new_id()
                from mxwbot.bus.messages import OutboundMessage
                approved = await spec.bus.request_confirmation(OutboundMessage(
                    channel="", chat_id="",
                    msg_type="confirmation_request",
                    request_id=req_id,
                    content=f"即将执行 {len(writable_tools)} 个写操作",
                    timeout_seconds=60,
                ))
                if not approved:
                    return AgentRunResult(
                        content="用户拒绝了写操作",
                        tool_calls_made=total_tool_calls,
                        iterations=iteration,
                        usage=total_usage,
                        finish_reason="denied",
                    )

            # Checkpoint: 工具调用前保存
            if spec.on_checkpoint:
                await spec.on_checkpoint(messages, iteration)

            # 执行工具
            tool_call_dicts = [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in tool_calls
            ]
            results = await spec.tools.execute_batch(tool_call_dicts)

            # 注入结果到消息列表
            asst_msg: dict[str, Any] = {
                "role": "assistant",
                "content": response.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.arguments},
                    }
                    for tc in tool_calls
                ],
            }
            if response.reasoning_content:
                asst_msg["reasoning_content"] = response.reasoning_content
            messages.append(asst_msg)
            for cid, result in results:
                # Find the tool name for this call
                tool_name = next(
                    (tc.name for tc in tool_calls if tc.id == cid), "unknown"
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": cid,
                    "name": tool_name,
                    "content": result.content if result.success else f"Error: {result.error}",
                })

            # Hook: after tool calls
            if spec.hook:
                for cid, result in results:
                    tool_name = next(
                        (tc.name for tc in tool_calls if tc.id == cid), "unknown"
                    )
                    await spec.hook.after_tool_call(tool_name, result)

            # 周期 Checkpoint
            if spec.on_checkpoint and iteration % spec.checkpoint_interval == 0:
                await spec.on_checkpoint(messages, iteration)

        # 达到最大迭代次数（或无迭代）
        return AgentRunResult(
            content="",
            tool_calls_made=total_tool_calls,
            iterations=iteration,
            usage=total_usage,
            finish_reason="max_iterations",
        )

    # ------------------------------------------------------------------
    # Streaming variant — same ReAct loop but LLM calls are streamed
    # ------------------------------------------------------------------

    async def run_stream(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> AgentRunResult:
        """Execute ReAct loop with streaming LLM calls.

        Text deltas are forwarded to ``spec.hook.on_stream_delta()`` so
        the caller can publish them to the Bus / Channel in real time.
        Tool-call accumulation happens silently; once the full response
        is assembled, tool execution follows the same path as ``run()``.
        """
        tools_schema = spec.tools.to_openai_schema() if spec.tools else None
        total_usage = TokenUsage()
        total_tool_calls = 0
        iteration = 0

        while iteration < spec.max_iterations:
            iteration += 1

            # Hook: before LLM call
            if spec.hook:
                await spec.hook.before_llm_call(messages, tools_schema)

            # ★ Streaming LLM call — accumulate deltas into LLMResponse
            response = await self._stream_one(
                spec.provider, messages, tools_schema, spec.hook,
            )

            # 累计 token
            total_usage.input_tokens += response.usage.input_tokens
            total_usage.output_tokens += response.usage.output_tokens

            # Hook: after LLM response
            if spec.hook:
                await spec.hook.on_llm_response(response)

            # 错误检查
            if not response.is_ok:
                return AgentRunResult(
                    content=response.content or "",
                    tool_calls_made=total_tool_calls,
                    iterations=iteration,
                    usage=total_usage,
                    finish_reason="error",
                )

            # 无工具调用 → 返回最终内容
            if not response.tool_calls:
                return AgentRunResult(
                    content=response.content or "",
                    tool_calls_made=total_tool_calls,
                    iterations=iteration,
                    usage=total_usage,
                    finish_reason="stop",
                )

            # 有工具调用 — same flow as run()
            tool_calls = response.tool_calls
            total_tool_calls += len(tool_calls)

            # 检查是否有写操作（需要确认）
            writable_tools = [
                tc for tc in tool_calls
                if tc.name in spec.tools and not spec.tools.get(tc.name).is_readonly
            ]
            if writable_tools and spec.bus:
                req_id = _new_id()
                from mxwbot.bus.messages import OutboundMessage
                approved = await spec.bus.request_confirmation(OutboundMessage(
                    channel="", chat_id="",
                    msg_type="confirmation_request",
                    request_id=req_id,
                    content=f"即将执行 {len(writable_tools)} 个写操作",
                    timeout_seconds=60,
                ))
                if not approved:
                    return AgentRunResult(
                        content="用户拒绝了写操作",
                        tool_calls_made=total_tool_calls,
                        iterations=iteration,
                        usage=total_usage,
                        finish_reason="denied",
                    )

            # Checkpoint: 工具调用前保存
            if spec.on_checkpoint:
                await spec.on_checkpoint(messages, iteration)

            # 执行工具
            tool_call_dicts = [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in tool_calls
            ]
            results = await spec.tools.execute_batch(tool_call_dicts)

            # 注入结果到消息列表
            asst_msg: dict[str, Any] = {
                "role": "assistant",
                "content": response.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.arguments},
                    }
                    for tc in tool_calls
                ],
            }
            if response.reasoning_content:
                asst_msg["reasoning_content"] = response.reasoning_content
            messages.append(asst_msg)
            for cid, result in results:
                tool_name = next(
                    (tc.name for tc in tool_calls if tc.id == cid), "unknown"
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": cid,
                    "name": tool_name,
                    "content": result.content if result.success else f"Error: {result.error}",
                })

            # Hook: after tool calls
            if spec.hook:
                for cid, result in results:
                    tool_name = next(
                        (tc.name for tc in tool_calls if tc.id == cid), "unknown"
                    )
                    await spec.hook.after_tool_call(tool_name, result)

            # 周期 Checkpoint
            if spec.on_checkpoint and iteration % spec.checkpoint_interval == 0:
                await spec.on_checkpoint(messages, iteration)

        return AgentRunResult(
            content="",
            tool_calls_made=total_tool_calls,
            iterations=iteration,
            usage=total_usage,
            finish_reason="max_iterations",
        )

    # ------------------------------------------------------------------
    # Internal: stream accumulation
    # ------------------------------------------------------------------

    async def _stream_one(
        self,
        provider: LLMProvider,
        messages: list[dict[str, Any]],
        tools_schema: list[dict[str, Any]] | None,
        hook: Any,
    ) -> LLMResponse:
        """Consume ``chat_stream()`` and accumulate into an ``LLMResponse``.

        Text deltas are forwarded to *hook.on_stream_delta()* in real time.
        Tool-call deltas are silently accumulated into a full
        ``ToolCallRequest`` per index.
        """
        content_parts: list[str] = []
        pending_calls: dict[int, dict[str, Any]] = {}  # index → {id, name, args}
        finish_reason = "stop"
        usage = TokenUsage()
        reasoning_content: str | None = None

        async for chunk in provider.chat_stream(messages, tools_schema):
            if chunk.error:
                finish_reason = "error"
                if hook:
                    await hook.on_stream_delta(f"\n[响应中断: {chunk.error}]")
                content_parts.append(chunk.error)
                break

            if chunk.delta:
                content_parts.append(chunk.delta)
                if hook:
                    await hook.on_stream_delta(chunk.delta)

            if chunk.reasoning_content:
                reasoning_content = chunk.reasoning_content

            if chunk.tool_call_delta:
                d = chunk.tool_call_delta
                if d.index not in pending_calls:
                    pending_calls[d.index] = {"id": d.id or "", "name": d.name or "", "args": ""}
                else:
                    if d.id:
                        pending_calls[d.index]["id"] = d.id
                    if d.name:
                        pending_calls[d.index]["name"] = d.name
                pending_calls[d.index]["args"] += d.arguments_delta

            if chunk.finish_reason:
                finish_reason = chunk.finish_reason

        # Build tool_calls from accumulated deltas
        tool_calls: list[ToolCallRequest] = []
        for idx in sorted(pending_calls):
            tc = pending_calls[idx]
            if tc["id"] and tc["name"]:
                tool_calls.append(ToolCallRequest(
                    id=tc["id"],
                    name=tc["name"],
                    arguments=tc["args"],
                ))

        return LLMResponse(
            content="".join(content_parts) or None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            reasoning_content=reasoning_content,
        )
