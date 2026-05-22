"""Heartbeat service — LLM-driven periodic task checking.

Reads ``HEARTBEAT.md`` from the workspace, asks the LLM (via a
virtual ``heartbeat`` tool call) whether there are active tasks.
If the decision is ``run``, calls ``on_execute`` to publish the
task to the Bus for normal Loop processing.

设计亮点

1. 两阶段流水线：
    - Phase 1（决策）：轻量级 LLM 调用，只看 HEARTBEAT.md + 一个 tool call
    - Phase 2（执行）：仅在 Phase 1 返回 run 时才触发重型 agent 循环
2. 结构化输出强制：通过 function calling 的 enum: ["skip", "run"] 约束 LLM 输出，杜绝了解析自由文本的歧义
3. 通知门控：evaluate_response 作为第二道关卡——任务执行完了不等于必须通知用户
4. 优雅关闭：stop() 取消 _task，_run_loop 捕获 CancelledError 干净退出
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine

from mxwbot.providers.base import LLMProvider

logger = logging.getLogger("mxwbot.heartbeat.service")

# Virtual tool that constrains the LLM to return structured skip/run
_HEARTBEAT_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "heartbeat",
            "description": "Review tasks and report decision.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["skip", "run"],
                        "description": "skip = nothing to do, run = active tasks found",
                    },
                    "tasks": {
                        "type": "string",
                        "description": "Natural-language summary of active tasks (required for run)",
                    },
                },
                "required": ["action"],
            },
        },
    }
]


class HeartbeatService:
    """LLM-driven periodic heartbeat.

    Every *interval_s* seconds the service reads ``HEARTBEAT.md``,
    asks the LLM to decide ``skip`` / ``run`` via the ``heartbeat``
    tool, and (on ``run``) fires the *on_execute* callback.

    Args:
        workspace: Workspace root (reads ``HEARTBEAT.md``).
        provider: LLM provider used for the decision.
        on_execute: Callback that receives the task summary string
            and publishes it as an ``InboundMessage`` to the Bus.
        on_notify: Optional callback for delivering the final
            response back to the user's channel.
        interval_s: Seconds between heartbeats (default 30 min).
        enabled: Whether the service is active.
        timezone: Timezone string for ``current_time_str``.
    """

    def __init__(
        self,
        workspace: Path,
        provider: LLMProvider,
        *,
        on_execute: Callable[[str], Coroutine[Any, Any, str | None]] | None = None,
        on_notify: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        interval_s: int = 30 * 60,
        enabled: bool = True,
        timezone: str | None = None,
    ) -> None:
        self._workspace = workspace
        self._provider = provider
        self._on_execute = on_execute
        self._on_notify = on_notify
        self._interval_s = interval_s
        self._enabled = enabled
        self._timezone = timezone
        self._running = False
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    @property
    def heartbeat_file(self) -> Path:
        return self._workspace / "HEARTBEAT.md"

    async def start(self) -> None:
        """Begin the heartbeat loop."""
        if not self._enabled:
            logger.info("Heartbeat: disabled")
            return
        if self._running:
            logger.warning("Heartbeat: already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())#创建了异步任务来执行心跳循环，这样它就可以在后台运行，不会阻塞主线程
        logger.info("Heartbeat: started (every %ss)", self._interval_s)

    def stop(self) -> None:
        """Cancel the heartbeat loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def trigger_now(self) -> str | None:
        """Manually execute one tick (used by CLI ``heartbeat run``)."""
        content = self._read_file()
        if not content:
            return None
        action, tasks = await self._decide(content)
        if action != "run" or not self._on_execute:
            return None
        return await self._on_execute(tasks)

    def status(self) -> dict:
        return {
            "running": self._running,
            "enabled": self._enabled,
            "interval_s": self._interval_s,
            "file_exists": self.heartbeat_file.exists(),
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _read_file(self) -> str | None:
        f = self.heartbeat_file
        if f.exists():
            try:
                return f.read_text(encoding="utf-8")
            except Exception:
                return None
        return None

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._interval_s)
                if self._running:
                    await self._tick()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Heartbeat error")

    async def _tick(self) -> None:
        content = self._read_file()
        if not content:
            logger.debug("Heartbeat: HEARTBEAT.md missing or empty")
            return

        logger.info("Heartbeat: checking for tasks...")
        try:
            action, tasks = await self._decide(content)
            if action != "run":
                logger.info("Heartbeat: OK (nothing to do)")
                return

            logger.info("Heartbeat: tasks found — %s", tasks[:100])
            if self._on_execute:
                response = await self._on_execute(tasks)
                if response and self._on_notify:
                    should_notify = await self._eval_response(response, tasks)
                    if should_notify:
                        await self._on_notify(response)
                    else:
                        logger.info("Heartbeat: silenced by post-run evaluation")
        except Exception:
            logger.exception("Heartbeat tick failed")

    async def _decide(self, content: str) -> tuple[str, str]:
        """Ask LLM to decide skip / run."""
        response = await self._provider.chat_with_retry(
            messages=[
                {
                    "role": "system",
                    "content": "You are a heartbeat agent. Call the heartbeat tool.",
                },
                {
                    "role": "user",
                    "content": (
                        f"Current Time: {self._now_str()}\n\n"
                        "Review HEARTBEAT.md and decide whether there are active tasks.\n\n"
                        f"{content}"
                    ),
                },
            ],
            tools=_HEARTBEAT_TOOL,
        )
        if not response.is_ok or not response.tool_calls:
            return "skip", ""
        args = response.tool_calls[0].arguments
        try:
            import json
            if isinstance(args, str):
                args = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            return "skip", ""
        return args.get("action", "skip"), args.get("tasks", "")

    async def _eval_response(self, response: str, tasks: str) -> bool:
        """Post-run evaluation: should we notify the user?"""
        result = await self._provider.chat_with_retry(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a notification evaluator. Reply ONLY 'yes' or 'no'.\n"
                        "Reply 'yes' if the response contains actionable results the "
                        "user should know about."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"TASK:\n{tasks}\n\n"
                        f"AGENT RESPONSE:\n{response[:2000]}\n\n"
                        "Should we notify the user? (yes/no)"
                    ),
                },
            ],
        )
        answer = (result.content or "").strip().lower()
        return answer.startswith("yes")

    def _now_str(self) -> str:
        if self._timezone:
            try:
                from zoneinfo import ZoneInfo
                return datetime.now(ZoneInfo(self._timezone)).strftime("%Y-%m-%d %H:%M:%S %Z")
            except Exception:
                pass
        return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
