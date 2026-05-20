"""Shell 命令执行工具。

纵深防御管线：
  allow_patterns 优先 → deny_patterns 检查 → 工作区边界检查
  → sandbox 后端包装命令 → 进程管理（spawn / timeout / kill / output format）

沙箱由 sandbox.wrap_command() 提供；进程管理全在本模块。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

from mxwbot.core.tools.base import Tool, ToolResult
from mxwbot.core.tools.sandbox import wrap_command
from mxwbot.utils.security import detect_command_injection

_IS_WINDOWS = sys.platform == "win32"

# --- 内置危险命令黑名单 ----------------------------------------------------
_DEFAULT_DENY_PATTERNS: list[str] = [
    r"\brm\s+-[rf]{1,2}\b",
    r"\bdel\s+/[fq]\b",
    r"\brmdir\s+/s\b",
    r"\b(format|mkfs|diskpart)\b",
    r"\bdd\s+if=",
    r">\s*/dev/sd",
    r"\b(shutdown|reboot|poweroff|halt)\b",
    r":\(\)\s*\{",
    r">>?\s*\S*(?:history\.jsonl|\.dream_cursor)",
    r"\btee\b[^|;&<>]*(?:history\.jsonl|\.dream_cursor)",
    r"\b(?:cp|mv)\b(?:\s+[^\s|;&<>]+)+\s+\S*(?:history\.jsonl|\.dream_cursor)",
    r"\bdd\b[^|;&<>]*\bof=\S*(?:history\.jsonl|\.dream_cursor)",
    r"\bsed\s+-i[^|;&<>]*(?:history\.jsonl|\.dream_cursor)",
]

_DEVICE_WHITELIST: frozenset[str] = frozenset({
    "/dev/null", "/dev/zero", "/dev/full", "/dev/random", "/dev/urandom",
    "/dev/stdout", "/dev/stderr", "/dev/stdin", "/dev/tty",
})

_WORKSPACE_BOUNDARY_NOTE = (
    "命令试图访问工作区之外的路径。所有文件操作必须限制在工作区内。"
    "不要尝试用 cd、../ 或绝对路径绕过此限制。"
)

_MAX_OUTPUT_CHARS = 10_000
_MAX_TIMEOUT = 600

_ABS_PATH_RE = re.compile(
    r"(?:^|\s|[`\"';&|()])((?:/[^\s`\"';&|()]*)+)"
    r"|([A-Za-z]:\\[^\s`\"';&|()]*)"
    r"|(~/[^\s`\"';&|()]*)"
)


class ShellTool(Tool):
    """执行 Shell 命令。安全校验 → sandbox 包装 → 进程管理。"""

    name = "shell"
    description = "执行 Shell 命令并返回输出。命令在隔离沙箱中运行，无法访问工作区外的文件。"
    is_readonly = False
    exclusive = True
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 Shell 命令"},
            "working_dir": {"type": "string", "description": "工作目录（相对于工作区，默认 '.'）"},
            "timeout": {"type": "integer", "description": "超时秒数（默认 60，最大 600）",
                        "minimum": 1, "maximum": 600},
        },
        "required": ["command"],
    }

    def __init__(
        self,
        workspace: Path,
        *,
        timeout: int = 60,
        allow_patterns: list[str] | None = None,
        deny_patterns: list[str] | None = None,
        path_append: str = "",
    ):
        self._workspace = workspace
        self._timeout = timeout
        self._allow_patterns = allow_patterns or []
        self._deny_patterns = _DEFAULT_DENY_PATTERNS + (deny_patterns or [])
        self._path_append = path_append

    # --- 执行 --------------------------------------------------------------

    async def execute(self, command: str, working_dir: str | None = None,
                      timeout: int | None = None, **kwargs: Any) -> ToolResult:
        # 1. 白名单优先
        if self._allow_patterns:
            if not any(re.search(p, command) for p in self._allow_patterns):
                return ToolResult(
                    success=False,
                    error=f"命令不在允许列表中。允许的模式: {self._allow_patterns}",
                )

        # 2. 黑名单检查
        for pattern in self._deny_patterns:
            if re.search(pattern, command):
                return ToolResult(
                    success=False,
                    error=f"命令被拒绝（匹配黑名单: {pattern}）",
                )

        # 3. 通用注入检测
        hits = detect_command_injection(command)
        if hits:
            return ToolResult(
                success=False,
                error=f"检测到命令注入模式: {', '.join(hits)}",
            )

        # 4. 工作区边界检查
        err = self._check_workspace_boundary(command)
        if err:
            return ToolResult(success=False, error=err)

        # 5. working_dir 校验
        ws = str(self._workspace.resolve())
        cwd = ws
        if working_dir:
            resolved = (self._workspace / working_dir).resolve()
            try:
                resolved.relative_to(self._workspace)
                cwd = str(resolved)
            except ValueError:
                return ToolResult(success=False, error=_WORKSPACE_BOUNDARY_NOTE)

        # 6. 沙箱包装（bwrap 不可用时拒绝）
        try:
            wrapped = wrap_command("bwrap", command, ws, cwd)
        except ValueError as e:
            return ToolResult(success=False, error=str(e))

        # 7. 路径追加
        env = self._build_env()
        if self._path_append:
            if _IS_WINDOWS:
                env["PATH"] = env.get("PATH", "") + os.pathsep + self._path_append
            else:
                env["NANOBOT_PATH_APPEND"] = self._path_append
                wrapped = f'export PATH="$PATH{os.pathsep}$NANOBOT_PATH_APPEND"; {wrapped}'

        # 8. 进程管理
        effective_timeout = min(timeout or self._timeout, _MAX_TIMEOUT)
        try:
            proc = await self._spawn(wrapped, cwd, env)
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                await self._kill_process(proc)
                return ToolResult(success=False, error=f"命令超时 ({effective_timeout}s)")
            except asyncio.CancelledError:
                await self._kill_process(proc)
                raise
            return ToolResult(content=self._format_output(stdout, stderr))
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    # --- 安全检查 ----------------------------------------------------------

    def _check_workspace_boundary(self, command: str) -> str | None:
        ws = str(self._workspace.resolve())
        for match in _ABS_PATH_RE.finditer(command):
            path = match.group(1) or match.group(2) or match.group(3)
            if not path:
                continue
            if self._is_benign_device(path):
                continue
            try:
                resolved = Path(path).expanduser().resolve()
            except Exception:
                continue
            if self._is_benign_device(str(resolved)):
                continue
            if str(resolved).startswith(ws):
                continue
            return (
                f"工作区边界违规: 命令试图访问 '{path}'。\n"
                f"{_WORKSPACE_BOUNDARY_NOTE}"
            )
        if re.search(r"(?:^|[\s`\"';&|()])\.\.[\\/]", command):
            return _WORKSPACE_BOUNDARY_NOTE
        return None

    @staticmethod
    def _is_benign_device(path: str) -> bool:
        if path in _DEVICE_WHITELIST:
            return True
        return path.startswith("/dev/fd/")

    # --- 进程管理 ----------------------------------------------------------

    @staticmethod
    async def _spawn(command: str, cwd: str, env: dict[str, str]):
        if _IS_WINDOWS:
            return await asyncio.create_subprocess_shell(
                command, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, cwd=cwd, env=env,
            )
        bash = shutil.which("bash") or "/bin/bash"
        return await asyncio.create_subprocess_exec(
            bash, "-l", "-c", command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd, env=env,
        )

    @staticmethod
    async def _kill_process(proc: asyncio.subprocess.Process) -> None:
        try:
            proc.kill()
        except ProcessLookupError:
            return
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=5)
        if not _IS_WINDOWS and proc.pid is not None:
            try:
                os.waitpid(proc.pid, os.WNOHANG)
            except ChildProcessError:
                pass

    @staticmethod
    def _build_env() -> dict[str, str]:
        if _IS_WINDOWS:
            sr = os.environ.get("SYSTEMROOT", r"C:\Windows")
            return {
                "SYSTEMROOT": sr,
                "COMSPEC": os.environ.get("COMSPEC", f"{sr}\\system32\\cmd.exe"),
                "USERPROFILE": os.environ.get("USERPROFILE", ""),
                "TEMP": os.environ.get("TEMP", f"{sr}\\Temp"),
                "TMP": os.environ.get("TMP", f"{sr}\\Temp"),
                "PATH": os.environ.get("PATH", f"{sr}\\system32;{sr}"),
                "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
                "PYTHONUNBUFFERED": "1",
            }
        return {
            "HOME": os.environ.get("HOME", "/tmp"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "TERM": os.environ.get("TERM", "dumb"),
            "PYTHONUNBUFFERED": "1",
        }

    # --- 输出格式化 --------------------------------------------------------

    @staticmethod
    def _format_output(stdout: bytes, stderr: bytes) -> str:
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace") if stderr else ""

        if err:
            out = out + "\n[stderr]\n" + err
        out = out.strip()
        if not out:
            return "(无输出)"

        if len(out) > _MAX_OUTPUT_CHARS:
            half = _MAX_OUTPUT_CHARS // 2
            truncated = len(out) - _MAX_OUTPUT_CHARS
            out = (
                out[:half]
                + f"\n\n... ({truncated} 字符被截断) ...\n\n"
                + out[-half:]
            )
        return out
