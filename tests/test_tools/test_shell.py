"""测试 core/tools/shell.py"""

import shutil

import pytest

from mxwbot.core.tools.shell import ShellTool

# 所有需要 bwrap 成功执行的测试共用此标记
_needs_bwrap = pytest.mark.skipif(
    not shutil.which("bwrap"),
    reason="bwrap 未安装，Shell 必须通过沙箱执行",
)


class TestShellToolSecurity:
    """安全检测在沙箱之前，不依赖 bwrap。"""

    @pytest.mark.asyncio
    async def test_injection_blocked(self, tmp_path):
        tool = ShellTool(tmp_path)
        r = await tool.execute(command="ls; rm -rf /")
        assert r.success is False

    @pytest.mark.asyncio
    async def test_deny_rm_rf(self, tmp_path):
        tool = ShellTool(tmp_path)
        r = await tool.execute(command="rm -rf /tmp/foo")
        assert r.success is False
        assert "黑名单" in r.error

    @pytest.mark.asyncio
    async def test_deny_shutdown(self, tmp_path):
        tool = ShellTool(tmp_path)
        r = await tool.execute(command="shutdown -h now")
        assert r.success is False

    @pytest.mark.asyncio
    async def test_workspace_boundary(self, tmp_path):
        tool = ShellTool(tmp_path)
        r = await tool.execute(command="cat /etc/passwd")
        assert r.success is False
        assert "工作区" in r.error

    @pytest.mark.asyncio
    async def test_working_dir_escape_blocked(self, tmp_path):
        tool = ShellTool(tmp_path)
        r = await tool.execute(command="pwd", working_dir="../../../etc")
        assert r.success is False


class TestShellToolExecution:
    """执行测试 — 需要 bwrap。"""

    @pytest.mark.asyncio
    @_needs_bwrap
    async def test_simple_command(self, tmp_path):
        tool = ShellTool(tmp_path)
        r = await tool.execute(command="echo hello")
        assert r.success is True
        assert "hello" in r.content

    @pytest.mark.asyncio
    @_needs_bwrap
    async def test_dev_null_allowed(self, tmp_path):
        tool = ShellTool(tmp_path)
        r = await tool.execute(command="echo ok > /dev/null")
        assert r.success is True

    @pytest.mark.asyncio
    @_needs_bwrap
    async def test_working_dir_param(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        tool = ShellTool(tmp_path)
        r = await tool.execute(command="pwd", working_dir="sub")
        assert r.success is True
        assert "sub" in r.content

    @pytest.mark.asyncio
    @_needs_bwrap
    async def test_failing_command(self, tmp_path):
        tool = ShellTool(tmp_path)
        r = await tool.execute(command="python -c \"raise SystemExit(1)\"")
        assert r.error is None

    @pytest.mark.asyncio
    @_needs_bwrap
    async def test_long_output_truncated(self, tmp_path):
        tool = ShellTool(tmp_path)
        r = await tool.execute(command=f"python -c \"print('A'*{12_000})\"")
        assert r.success is True
        assert "截断" in r.content
