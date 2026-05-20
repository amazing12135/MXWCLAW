"""Sandbox 命令包装 — 可插拔后端。

注册新后端只需实现 ``_wrap_<name>(command, workspace, cwd) -> str`` 并加入 _BACKENDS。
进程管理、超时、输出截断全部由 ShellTool 负责，本模块只做命令封装。
"""

from __future__ import annotations

import shlex
import shutil
from pathlib import Path

from mxwbot.core.tools.base import Tool, ToolResult


# ---------------------------------------------------------------------------
# Sandbox backends — 纯命令包装
# ---------------------------------------------------------------------------

def _bwrap(command: str, workspace: str, cwd: str) -> str:
    """bubblewrap 沙箱封装。

    - 工作区父目录被 tmpfs 掩藏（config 等敏感文件不可见）
    - 系统目录只读挂载（必需路径用 --ro-bind，可选路径用 --ro-bind-try）
    - /proc、/dev 重新挂载，/tmp 独立
    """
    ws = str(Path(workspace).resolve())
    sandbox_cwd = str(ws / Path(cwd).resolve().relative_to(ws)) if cwd else ws

    required = ["/usr"]
    optional = [
        "/bin", "/lib", "/lib64",
        "/etc/alternatives", "/etc/ssl/certs",
        "/etc/resolv.conf", "/etc/ld.so.cache",
    ]

    args = ["bwrap", "--new-session", "--die-with-parent"]
    for p in required:
        args += ["--ro-bind", p, p]
    for p in optional:
        args += ["--ro-bind-try", p, p]
    args += [
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--tmpfs", str(Path(ws).parent),   # 掩藏 config / sessions 等父目录文件
        "--dir", ws,                        # 重建 workspace 挂载点
        "--bind", ws, ws,                   # 挂载真实 workspace
        "--chdir", sandbox_cwd,
        "--", "sh", "-c", command,
    ]
    return shlex.join(args)


_BACKENDS: dict[str, object] = {"bwrap": _bwrap}


def wrap_command(backend: str, command: str, workspace: str, cwd: str) -> str:
    """用指定沙箱后端包装命令。"""
    fn = _BACKENDS.get(backend)
    if fn is None:
        raise ValueError(f"未知沙箱后端 {backend!r}。可用: {list(_BACKENDS)}")
    return fn(command, workspace, cwd)  # type: ignore[operator]


# ---------------------------------------------------------------------------
# SandboxTool — 显式 sandbox 工具（LLM 可直接调用 name="sandbox"）
# ---------------------------------------------------------------------------

class SandboxTool(Tool):
    """在隔离沙箱中执行 Shell 命令。

    ShellTool 内部通过 wrap_command() 自动使用沙箱；
    本工具是 LLM 可显式调用的独立 sandbox 入口。
    实际执行逻辑委托给内部的 ShellTool 实例。
    """

    name = "sandbox"
    description = (
        "在隔离的沙箱环境中执行 Shell 命令。"
        "命令在受限的文件系统中运行，无法访问工作区之外的文件。"
    )
    is_readonly = False
    exclusive = True
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 Shell 命令"},
        },
        "required": ["command"],
    }

    def __init__(self, workspace: Path, *, timeout: int = 60, backend: str = "bwrap"):
        self._workspace = workspace
        self._timeout = timeout
        self._backend = backend
        self._shell: Tool | None = None

    async def execute(self, command: str, cwd: str | None = None, **kwargs) -> ToolResult:
        # 后端验证
        if self._backend not in _BACKENDS:
            return ToolResult(
                success=False,
                error=f"未知沙箱后端: {self._backend}。可用: {list(_BACKENDS)}",
            )
        if self._backend == "bwrap" and not shutil.which("bwrap"):
            return ToolResult(
                success=False,
                error="bwrap 未安装或不在 PATH 中，无法执行沙箱命令",
            )

        if self._shell is None:
            from mxwbot.core.tools.shell import ShellTool
            self._shell = ShellTool(self._workspace, timeout=self._timeout)
        return await self._shell.execute(command=command, working_dir=cwd)
