"""文件系统工具：读/写/编辑/列表/Glob/Grep。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mxwbot.core.tools.base import Tool, ToolResult
from mxwbot.utils.security import is_path_safe


def _resolve_in_workspace(path_str: str, workspace: Path, *, unrestricted: bool = False) -> Path | None:
    """将路径解析到 workspace 内，穿越检测失败返回 None。

    当 *unrestricted* 为 True 时，绝对路径直接使用，相对路径仍相对于 workspace 解析。
    """
    if unrestricted:
        p = Path(path_str)
        try:
            if p.is_absolute():
                return p.resolve()
            return (workspace / p).resolve()
        except (OSError, ValueError):
            return None
    if not is_path_safe(path_str, workspace):
        return None
    resolved = (workspace / path_str).resolve()
    return resolved


class ReadFileTool(Tool):
    """读取文件内容。"""

    name = "read_file"
    description = "读取指定文件的内容"
    is_readonly = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（相对于工作区，或绝对路径）"},
        },
        "required": ["path"],
    }

    def __init__(self, workspace: Path, *, unrestricted: bool = False):
        self._workspace = workspace
        self._unrestricted = unrestricted

    async def execute(self, path: str, **kwargs: Any) -> ToolResult:
        resolved = _resolve_in_workspace(path, self._workspace, unrestricted=self._unrestricted)
        if resolved is None:
            return ToolResult(success=False, error=f"路径不安全或不存在: {path}")
        if not resolved.exists():
            return ToolResult(success=False, error=f"文件不存在: {path}")
        try:
            content = resolved.read_text(encoding="utf-8")
            return ToolResult(content=content)
        except Exception as e:
            return ToolResult(success=False, error=str(e))


class WriteFileTool(Tool):
    """创建/覆盖文件。"""

    name = "write_file"
    description = "创建或覆盖一个文件"
    is_readonly = False
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（相对于工作区，或绝对路径）"},
            "content": {"type": "string", "description": "文件内容"},
        },
        "required": ["path", "content"],
    }

    def __init__(self, workspace: Path, *, unrestricted: bool = False):
        self._workspace = workspace
        self._unrestricted = unrestricted

    async def execute(self, path: str, content: str, **kwargs: Any) -> ToolResult:
        resolved = _resolve_in_workspace(path, self._workspace, unrestricted=self._unrestricted)
        if resolved is None:
            return ToolResult(success=False, error=f"路径不安全: {path}")
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
            return ToolResult(content=f"已写入: {path}")
        except Exception as e:
            return ToolResult(success=False, error=str(e))


class EditFileTool(Tool):
    """精确字符串替换编辑。"""

    name = "edit_file"
    description = "在文件中执行精确字符串替换"
    is_readonly = False
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（相对于工作区，或绝对路径）"},
            "old_string": {"type": "string", "description": "要替换的原字符串"},
            "new_string": {"type": "string", "description": "替换后的新字符串"},
        },
        "required": ["path", "old_string", "new_string"],
    }

    def __init__(self, workspace: Path, *, unrestricted: bool = False):
        self._workspace = workspace
        self._unrestricted = unrestricted

    async def execute(self, path: str, old_string: str, new_string: str, **kwargs: Any) -> ToolResult:
        resolved = _resolve_in_workspace(path, self._workspace, unrestricted=self._unrestricted)
        if resolved is None:
            return ToolResult(success=False, error=f"路径不安全: {path}")
        if not resolved.exists():
            return ToolResult(success=False, error=f"文件不存在: {path}")
        try:
            content = resolved.read_text(encoding="utf-8")
            if old_string not in content:
                return ToolResult(success=False, error="未找到匹配的字符串")
            content = content.replace(old_string, new_string, 1)
            resolved.write_text(content, encoding="utf-8")
            return ToolResult(content=f"已编辑: {path}")
        except Exception as e:
            return ToolResult(success=False, error=str(e))


class ListDirTool(Tool):
    """列出目录内容。"""

    name = "list_dir"
    description = "列出目录中的文件和子目录"
    is_readonly = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目录路径（相对于工作区，默认 '.'，或绝对路径）"},
        },
    }

    def __init__(self, workspace: Path, *, unrestricted: bool = False):
        self._workspace = workspace
        self._unrestricted = unrestricted

    async def execute(self, path: str = ".", **kwargs: Any) -> ToolResult:
        resolved = _resolve_in_workspace(path, self._workspace, unrestricted=self._unrestricted)
        if resolved is None or not resolved.exists():
            return ToolResult(success=False, error=f"路径不安全或不存在: {path}")
        try:
            items = sorted(resolved.iterdir(), key=lambda p: (not p.is_dir(), p.name))
            lines = []
            for item in items:
                suffix = "/" if item.is_dir() else ""
                lines.append(f"  {item.name}{suffix}")
            return ToolResult(content="\n".join(lines))
        except Exception as e:
            return ToolResult(success=False, error=str(e))


class GlobTool(Tool):
    """Glob 文件匹配。"""

    name = "glob"
    description = "使用 glob 模式查找文件"
    is_readonly = True
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob 模式，如 **/*.py（可以是绝对路径模式）"},
        },
        "required": ["pattern"],
    }

    def __init__(self, workspace: Path, *, unrestricted: bool = False):
        self._workspace = workspace
        self._unrestricted = unrestricted

    async def execute(self, pattern: str, **kwargs: Any) -> ToolResult:
        if self._unrestricted:
            try:
                p = Path(pattern)
                if p.is_absolute():
                    import glob as glob_mod
                    matches = sorted(p.parent.glob(p.name))
                    lines = [str(m) for m in matches]
                    return ToolResult(content="\n".join(lines) if lines else "(无匹配)")
                matches = sorted(self._workspace.glob(pattern))
                lines = [str(m.relative_to(self._workspace)) for m in matches]
                return ToolResult(content="\n".join(lines) if lines else "(无匹配)")
            except Exception as e:
                return ToolResult(success=False, error=str(e))
        if not is_path_safe(pattern, self._workspace):
            return ToolResult(success=False, error=f"Glob 模式不安全: {pattern}")
        try:
            matches = sorted(self._workspace.glob(pattern))
            lines = [str(m.relative_to(self._workspace)) for m in matches]
            return ToolResult(content="\n".join(lines) if lines else "(无匹配)")
        except Exception as e:
            return ToolResult(success=False, error=str(e))


class GrepTool(Tool):
    """内容搜索。"""

    name = "grep"
    description = "在文件中搜索匹配的文本模式（正则表达式）"
    is_readonly = True
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式搜索模式"},
            "path": {"type": "string", "description": "搜索目录（相对路径默认 '.'，或绝对路径）"},
            "glob": {"type": "string", "description": "文件过滤 glob，如 *.py"},
        },
        "required": ["pattern"],
    }

    def __init__(self, workspace: Path, *, unrestricted: bool = False):
        self._workspace = workspace
        self._unrestricted = unrestricted

    async def execute(self, pattern: str, path: str = ".", glob: str = "*", **kwargs: Any) -> ToolResult:
        import re

        try:
            regex = re.compile(pattern)
        except re.error as e:
            return ToolResult(success=False, error=f"正则无效: {e}")

        search_dir = _resolve_in_workspace(path, self._workspace, unrestricted=self._unrestricted)
        if search_dir is None or not search_dir.exists():
            return ToolResult(success=False, error=f"路径不安全或不存在: {path}")

        lines: list[str] = []
        base = search_dir if self._unrestricted else self._workspace
        try:
            for filepath in sorted(search_dir.rglob(glob)):
                if not filepath.is_file():
                    continue
                try:
                    content = filepath.read_text(encoding="utf-8")
                except Exception:
                    continue
                for lineno, line in enumerate(content.splitlines(), 1):
                    if regex.search(line):
                        try:
                            rel = filepath.relative_to(base)
                        except ValueError:
                            rel = filepath
                        lines.append(f"{rel}:{lineno}: {line}")
                        if len(lines) >= 50:
                            break
                if len(lines) >= 50:
                    lines.append("... (截断，最多 50 条)")
                    break
            return ToolResult(content="\n".join(lines) if lines else "(无匹配)")
        except Exception as e:
            return ToolResult(success=False, error=str(e))
