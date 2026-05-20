"""测试 core/tools/filesystem.py"""

import pytest

from mxwbot.core.tools.filesystem import (
    EditFileTool,
    GlobTool,
    GrepTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)


class TestReadFileTool:
    @pytest.mark.asyncio
    async def test_read_existing(self, tmp_path):
        f = tmp_path / "hello.txt"
        f.write_text("hello world")
        tool = ReadFileTool(tmp_path)
        r = await tool.execute(path="hello.txt")
        assert r.success is True
        assert r.content == "hello world"

    @pytest.mark.asyncio
    async def test_read_nonexistent(self, tmp_path):
        tool = ReadFileTool(tmp_path)
        r = await tool.execute(path="nope.txt")
        assert r.success is False

    @pytest.mark.asyncio
    async def test_read_traversal_blocked(self, tmp_path):
        tool = ReadFileTool(tmp_path)
        r = await tool.execute(path="../../../etc/passwd")
        assert r.success is False


class TestWriteFileTool:
    @pytest.mark.asyncio
    async def test_write_and_read(self, tmp_path):
        tool = WriteFileTool(tmp_path)
        r = await tool.execute(path="out.txt", content="data")
        assert r.success is True
        assert (tmp_path / "out.txt").read_text() == "data"

    @pytest.mark.asyncio
    async def test_write_traversal_blocked(self, tmp_path):
        tool = WriteFileTool(tmp_path)
        r = await tool.execute(path="../../../etc/hacked", content="evil")
        assert r.success is False


class TestEditFileTool:
    @pytest.mark.asyncio
    async def test_edit_replace(self, tmp_path):
        f = tmp_path / "cfg.txt"
        f.write_text("version=1.0")
        tool = EditFileTool(tmp_path)
        r = await tool.execute(path="cfg.txt", old_string="1.0", new_string="2.0")
        assert r.success is True
        assert f.read_text() == "version=2.0"

    @pytest.mark.asyncio
    async def test_edit_not_found(self, tmp_path):
        f = tmp_path / "cfg.txt"
        f.write_text("hello")
        tool = EditFileTool(tmp_path)
        r = await tool.execute(path="cfg.txt", old_string="nope", new_string="x")
        assert r.success is False


class TestListDirTool:
    @pytest.mark.asyncio
    async def test_list(self, tmp_path):
        (tmp_path / "a.txt").write_text("")
        (tmp_path / "b").mkdir()
        tool = ListDirTool(tmp_path)
        r = await tool.execute(path=".")
        assert r.success is True
        assert "a.txt" in r.content
        assert "b/" in r.content


class TestGlobTool:
    @pytest.mark.asyncio
    async def test_glob_py(self, tmp_path):
        (tmp_path / "main.py").write_text("")
        (tmp_path / "util.py").write_text("")
        (tmp_path / "readme.md").write_text("")
        tool = GlobTool(tmp_path)
        r = await tool.execute(pattern="*.py")
        assert r.success is True
        assert "main.py" in r.content
        assert "util.py" in r.content
        assert "readme.md" not in r.content


class TestGrepTool:
    @pytest.mark.asyncio
    async def test_grep_finds_match(self, tmp_path):
        (tmp_path / "code.py").write_text("TODO: fix this\nprint('ok')\n# TODO later\n")
        tool = GrepTool(tmp_path)
        r = await tool.execute(pattern="TODO", glob="*.py")
        assert r.success is True
        assert "TODO" in r.content

    @pytest.mark.asyncio
    async def test_grep_invalid_regex(self, tmp_path):
        tool = GrepTool(tmp_path)
        r = await tool.execute(pattern="[invalid")
        assert r.success is False
