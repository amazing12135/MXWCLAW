"""AgentRunner — 核心 ReAct 循环引擎。

LLM 推理 → 工具调用 → 结果注入 → 再推理，最多 max_iterations 轮。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

from mxwbot.providers.base import LLMProvider, LLMResponse, TokenUsage
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
            messages.append({
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
            })
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
