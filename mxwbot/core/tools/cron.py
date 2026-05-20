"""Cron 定时任务工具 — 创建/查看/取消定时提醒。

时间表达式支持:
  - 相对时间: "5m", "30s", "1h", "2h30m", "1d"
  - 绝对时间: "2026-05-14 09:00"

存储: 内存 dict + JSONL 持久化（Phase 9 Heartbeat 消费）。
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiofiles

from mxwbot.core.tools.base import Tool, ToolResult


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 时间解析
# ---------------------------------------------------------------------------

_TIME_UNITS: dict[str, int] = {
    "s": 1, "sec": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}

_RELATIVE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(s|sec|second|seconds|m|min|minute|minutes|h|hr|hour|hours|d|day|days|w|week|weeks)\b", re.IGNORECASE)

_ABSOLUTE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})")


def _parse_time(expr: str) -> datetime | None:
    """解析时间表达式为 UTC datetime。"""
    expr = expr.strip()

    # 绝对时间
    m = _ABSOLUTE_RE.match(expr)
    if m:
        dt = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=timezone.utc)

    # 相对时间
    seconds = 0
    for m in _RELATIVE_RE.finditer(expr):
        val = float(m.group(1))
        unit = m.group(2).lower()
        seconds += val * _TIME_UNITS.get(unit, 0)

    if seconds >= 0 and _RELATIVE_RE.search(expr):
        return _utc_now() + timedelta(seconds=seconds)

    return None


# ---------------------------------------------------------------------------
# CronTool
# ---------------------------------------------------------------------------

class CronTool(Tool):
    """定时任务管理。"""

    name = "cron"
    description = (
        "管理定时提醒和延迟任务。\n"
        "时间格式:\n"
        "  - 相对: '5m' (5分钟), '1h' (1小时), '30s' (30秒), '2h30m'\n"
        "  - 绝对: '2026-05-14 09:00'\n"
        "操作: add (添加), list (列出), remove (移除)"
    )
    is_readonly = False
    exclusive = True
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "list", "remove"],
                "description": "操作: add=添加任务, list=列出任务, remove=取消任务",
            },
            "schedule": {
                "type": "string",
                "description": "执行时间 (action=add 时必填)。例如 '5m', '1h', '2026-05-14 09:00'",
            },
            "message": {
                "type": "string",
                "description": "任务描述/提醒内容 (action=add 时必填)",
            },
            "task_id": {
                "type": "string",
                "description": "要取消的任务 ID (action=remove 时必填)",
            },
        },
        "required": ["action"],
    }

    def __init__(self, workspace: Path):
        self._storage_path = workspace / "cron" / "tasks.jsonl"
        self._tasks: dict[str, dict[str, Any]] = {}

    async def _load(self) -> None:
        """从 JSONL 加载任务到内存。"""
        self._tasks.clear()
        if not self._storage_path.exists():
            return
        async with aiofiles.open(str(self._storage_path), "r", encoding="utf-8") as f:
            async for line in f:
                line = line.strip()
                if line:
                    try:
                        task = json.loads(line)
                        self._tasks[task["id"]] = task
                    except json.JSONDecodeError:
                        pass

    async def _save(self) -> None:
        """原子写入全部任务到 JSONL。"""
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._storage_path.with_suffix(".tmp")
        async with aiofiles.open(str(tmp), "w", encoding="utf-8") as f:
            for task in sorted(self._tasks.values(), key=lambda t: t.get("run_at", "")):
                await f.write(json.dumps(task, ensure_ascii=False) + "\n")
        import os
        os.replace(tmp, self._storage_path)

    async def execute(self, action: str, schedule: str | None = None,
                      message: str | None = None, task_id: str | None = None,
                      **kwargs: Any) -> ToolResult:
        await self._load()

        if action == "add":
            return await self._do_add(schedule, message)
        elif action == "list":
            return self._do_list()
        elif action == "remove":
            return await self._do_remove(task_id)
        else:
            return ToolResult(success=False, error=f"未知操作: {action}")

    async def _do_add(self, schedule: str | None, message: str | None) -> ToolResult:
        if not schedule:
            return ToolResult(success=False, error="请提供 schedule 参数，例如 '5m' 或 '2026-05-14 09:00'")
        if not message:
            return ToolResult(success=False, error="请提供 message 参数（任务描述）")

        run_at = _parse_time(schedule)
        if run_at is None:
            return ToolResult(
                success=False,
                error=f"无法解析时间表达式: {schedule!r}。使用相对格式 '5m'/'1h' 或绝对格式 '2026-05-14 09:00'",
            )

        now = _utc_now()
        if run_at <= now:
            return ToolResult(success=False, error=f"任务时间 {schedule!r} 已过期")

        task = {
            "id": _new_id(),
            "run_at": run_at.isoformat(),
            "message": message,
            "created_at": _utc_now().isoformat(),
        }
        self._tasks[task["id"]] = task
        await self._save()

        return ToolResult(content=(
            f"✅ 任务已创建\n"
            f"  ID: {task['id']}\n"
            f"  时间: {run_at.strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"  内容: {message}"
        ))

    def _do_list(self) -> ToolResult:
        if not self._tasks:
            return ToolResult(content="(无待执行任务)")

        now = _utc_now()
        lines = []
        for task in sorted(self._tasks.values(), key=lambda t: t.get("run_at", "")):
            run_at = datetime.fromisoformat(task["run_at"])
            status = "⏳ 等待" if run_at > now else "✅ 已过期"
            lines.append(
                f"  [{task['id']}] {status} | "
                f"{run_at.strftime('%m-%d %H:%M')} | {task['message']}"
            )
        return ToolResult(content="\n".join(lines))

    async def _do_remove(self, task_id: str | None) -> ToolResult:
        if not task_id:
            return ToolResult(success=False, error="请提供 task_id 参数")
        if task_id not in self._tasks:
            available = ", ".join(self._tasks) or "(无)"
            return ToolResult(success=False, error=f"任务 {task_id!r} 不存在。可用 ID: {available}")

        task = self._tasks.pop(task_id)
        await self._save()
        return ToolResult(content=f"✅ 任务已取消: {task['message']}")
