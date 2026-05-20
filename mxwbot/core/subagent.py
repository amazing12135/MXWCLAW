"""SubAgentManager — 子代理生命周期管理。

子代理在主 Agent 的工具调用中被创建，独立执行任务后返回结果。
设计约束：子代理不嵌套创建孙代理（ToolRegistry 不含 spawn 工具）。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mxwbot.core.runner import AgentRunner, AgentRunResult, AgentRunSpec
from mxwbot.core.tools.register import ToolRegistry
from mxwbot.providers.base import LLMProvider


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class SubAgentConfig:
    """子代理配置。"""

    task: str
    provider: LLMProvider
    tools: ToolRegistry | None = None
    max_iterations: int = 5
    timeout_seconds: int = 120

    # 工作区隔离
    workspace: Path | None = None


@dataclass
class SubAgentResult:
    """子代理执行结果。"""

    agent_id: str
    status: str       # "completed" | "cancelled" | "timeout" | "error"
    content: str = ""
    iterations: int = 0
    error: str | None = None


class SubAgentManager:
    """子代理生命周期管理器。

    最多同时运行 ``max_concurrent`` 个子代理。
    子代理禁止递归创建孙代理（ToolRegistry 不含 spawn 工具）。
    """

    def __init__(self, max_concurrent: int = 5) -> None:
        self._max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active: dict[str, asyncio.Task[SubAgentResult]] = {}
        self._runner = AgentRunner()

    @property
    def active_count(self) -> int:
        return len(self._active)

    # -- 生命周期 -----------------------------------------------------------

    async def spawn(self, config: SubAgentConfig) -> str:
        """启动一个子代理，返回 agent_id。

        Args:
            config: 子代理配置。

        Returns:
            agent_id，可用于 cancel() 或 wait()。
        """
        agent_id = _new_id()
        task = asyncio.create_task(self._run_subagent(agent_id, config))
        self._active[agent_id] = task
        return agent_id

    async def cancel(self, agent_id: str) -> bool:
        """取消一个正在运行的子代理。

        Returns:
            True 表示成功取消。
        """
        task = self._active.get(agent_id)
        if task is None:
            return False
        task.cancel()
        return True

    async def wait(self, agent_id: str, timeout: float | None = None) -> SubAgentResult:
        """等待子代理完成。

        Args:
            agent_id: 子代理 ID。
            timeout: 超时秒数，None 表示使用子代理自身配置的超时。

        Returns:
            SubAgentResult。
        """
        task = self._active.get(agent_id)
        if task is None:
            return SubAgentResult(
                agent_id=agent_id, status="error",
                error=f"子代理 {agent_id} 不存在",
            )
        try:
            result = await asyncio.wait_for(task, timeout=timeout)
        except asyncio.TimeoutError:
            task.cancel()
            return SubAgentResult(
                agent_id=agent_id, status="timeout",
                error=f"子代理超时 ({timeout}s)",
            )
        return result

    async def wait_all(self) -> list[SubAgentResult]:
        """等待所有活跃子代理完成。"""
        if not self._active:
            return []
        results = await asyncio.gather(*self._active.values(), return_exceptions=True)
        out: list[SubAgentResult] = []
        for r in results:
            if isinstance(r, SubAgentResult):
                out.append(r)
            elif isinstance(r, Exception):
                out.append(SubAgentResult(agent_id="", status="error", error=str(r)))
        self._active.clear()
        return out

    # -- 内部 ---------------------------------------------------------------

    async def _run_subagent(self, agent_id: str, config: SubAgentConfig) -> SubAgentResult:
        async with self._semaphore:
            try:
                tools = config.tools or ToolRegistry()
                spec = AgentRunSpec(
                    provider=config.provider,
                    tools=tools,
                    max_iterations=config.max_iterations,
                )
                messages = [
                    {"role": "system", "content": f"你是子代理，完成任务后报告结果。\n\n任务: {config.task}"},
                    {"role": "user", "content": config.task},
                ]
                result = await asyncio.wait_for(
                    self._runner.run(spec, messages),
                    timeout=config.timeout_seconds,
                )
                self._active.pop(agent_id, None)
                return SubAgentResult(
                    agent_id=agent_id,
                    status="completed",
                    content=result.content,
                    iterations=result.iterations,
                )
            except asyncio.TimeoutError:
                self._active.pop(agent_id, None)
                return SubAgentResult(
                    agent_id=agent_id, status="timeout",
                    error=f"子代理超时 ({config.timeout_seconds}s)",
                )
            except asyncio.CancelledError:
                self._active.pop(agent_id, None)
                return SubAgentResult(agent_id=agent_id, status="cancelled")
            except Exception as e:
                self._active.pop(agent_id, None)
                return SubAgentResult(agent_id=agent_id, status="error", error=str(e))
