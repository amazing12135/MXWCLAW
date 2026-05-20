"""测试 core/tools/sandbox.py"""

import shutil

import pytest

from mxwbot.core.tools.sandbox import SandboxTool, wrap_command, _BACKENDS


class TestSandboxBackends:
    def test_bwrap_backend_registered(self):
        assert "bwrap" in _BACKENDS

    def test_unknown_backend(self):
        with pytest.raises(ValueError, match="未知沙箱后端"):
            wrap_command("nonexistent", "echo hi", "/tmp", ".")


class TestSandboxTool:
    @pytest.mark.asyncio
    async def test_injection_blocked(self, tmp_path):
        tool = SandboxTool(tmp_path)
        r = await tool.execute(command="ls; rm -rf /")
        assert r.success is False

    @pytest.mark.asyncio
    async def test_unknown_backend_rejected(self, tmp_path):
        tool = SandboxTool(tmp_path, backend="nonexistent")
        r = await tool.execute(command="echo hi")
        assert r.success is False
        assert "未知沙箱后端" in r.error

    @pytest.mark.asyncio
    async def test_bwrap_not_found(self, tmp_path):
        tool = SandboxTool(tmp_path, backend="bwrap")
        # bwrap 不存在于测试环境时被拒绝
        if not shutil.which("bwrap"):
            r = await tool.execute(command="echo hi")
            assert r.success is False
            assert "未安装" in r.error

    @pytest.mark.asyncio
    async def test_executes_if_bwrap_available(self, tmp_path):
        if not shutil.which("bwrap"):
            pytest.skip("bwrap 未安装")
        tool = SandboxTool(tmp_path, timeout=10)
        r = await tool.execute(command="echo hello-from-sandbox")
        assert r.success is True
        assert "hello-from-sandbox" in r.content
