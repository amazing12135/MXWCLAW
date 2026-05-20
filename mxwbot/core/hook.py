"""Agent 生命周期钩子系统。

Hook 在 AgentRunner 的关键节点被回调，用于日志、流式输出转发、指标采集等。
"""

from __future__ import annotations

from typing import Any

from mxwbot.providers.base import LLMResponse
from mxwbot.utils.text import clean_think_tags


class AgentHook:
    """Agent 生命周期钩子基类。所有方法默认为 no-op，子类按需覆写。"""

    async def on_turn_start(
        self,
        session: Any,
        state_manager: Any,
        inbound_msg: Any,
    ) -> None:
        """一轮对话开始。"""

    async def before_llm_call(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> None:
        """LLM 调用前，可修改 messages 或 tools。"""

    async def on_stream_delta(self, delta: str) -> None:
        """LLM 流式输出的单个增量片段。"""

    async def on_llm_response(self, response: LLMResponse) -> None:
        """LLM 返回完整响应后（非流式模式，或流式聚合完成后）。"""

    async def before_tool_call(self, name: str, args: dict[str, Any]) -> None:
        """某个工具执行前。"""

    async def after_tool_call(self, name: str, result: Any) -> None:
        """某个工具执行后。"""

    async def on_turn_end(
        self,
        session: Any,
        response: LLMResponse | None,
    ) -> None:
        """一轮对话结束。"""


class StreamProcessHook(AgentHook):
    """流式输出处理 Hook：think 标签清洗 + 内容累积。

    Args:
        clean_thinking: 是否清洗 ``<think>...</think>`` 标签。
    """

    def __init__(self, clean_thinking: bool = True) -> None:
        self._accumulated: list[str] = []
        self._cleaned: list[str] = []
        self._clean_thinking = clean_thinking

    @property
    def full_text(self) -> str:
        """累积的原始文本。"""
        return "".join(self._accumulated)

    @property
    def cleaned_text(self) -> str:
        """清洗后的文本。"""
        return "".join(self._cleaned)

    async def on_stream_delta(self, delta: str) -> None:
        self._accumulated.append(delta)
        if self._clean_thinking:
            # 增量清洗：对当前累积做 think 标签移除，取增量
            prev = "".join(self._cleaned)
            full = "".join(self._accumulated)
            clean = clean_think_tags(full)
            if clean != prev:
                increment = clean[len(prev):]
                self._cleaned.append(increment)
        else:
            self._cleaned.append(delta)

    def reset(self) -> None:
        """清空累积状态，准备下一轮流式输出。"""
        self._accumulated.clear()
        self._cleaned.clear()
