"""测试 core/tools/cron.py"""

import pytest

from mxwbot.core.tools.cron import CronTool, _parse_time


class TestTimeParsing:
    def test_5m(self):
        dt = _parse_time("5m")
        assert dt is not None

    def test_1h(self):
        dt = _parse_time("1h")
        assert dt is not None

    def test_2h30m(self):
        dt = _parse_time("2h30m")
        assert dt is not None

    def test_30s(self):
        dt = _parse_time("30s")
        assert dt is not None

    def test_absolute(self):
        dt = _parse_time("2026-12-25 08:00")
        assert dt is not None
        assert dt.month == 12
        assert dt.day == 25

    def test_invalid(self):
        assert _parse_time("nonsense") is None
        assert _parse_time("") is None


class TestCronTool:
    @pytest.mark.asyncio
    async def test_add_and_list(self, tmp_path):
        tool = CronTool(tmp_path)
        r = await tool.execute(action="add", schedule="2h", message="检查日志")
        assert r.success is True
        assert "已创建" in r.content

        r = await tool.execute(action="list")
        assert "检查日志" in r.content

    @pytest.mark.asyncio
    async def test_add_expired(self, tmp_path):
        tool = CronTool(tmp_path)
        # "0s" = now, 必然 ≤ now → 已过期
        r = await tool.execute(action="add", schedule="0s", message="已过期")
        assert r.success is False
        assert "已过期" in r.error

    @pytest.mark.asyncio
    async def test_add_missing_schedule(self, tmp_path):
        tool = CronTool(tmp_path)
        r = await tool.execute(action="add", message="忘记时间")
        assert r.success is False

    @pytest.mark.asyncio
    async def test_remove(self, tmp_path):
        tool = CronTool(tmp_path)
        r = await tool.execute(action="add", schedule="2h", message="临时的")
        assert r.success is True

        # 提取 task_id
        task_id = None
        for line in r.content.split("\n"):
            if "ID:" in line:
                task_id = line.split("ID:")[1].strip()

        assert task_id is not None
        r = await tool.execute(action="remove", task_id=task_id)
        assert r.success is True

        r = await tool.execute(action="list")
        assert "(无待执行任务)" in r.content

    @pytest.mark.asyncio
    async def test_remove_nonexistent(self, tmp_path):
        tool = CronTool(tmp_path)
        r = await tool.execute(action="remove", task_id="no-such-id")
        assert r.success is False

    @pytest.mark.asyncio
    async def test_empty_list(self, tmp_path):
        tool = CronTool(tmp_path)
        r = await tool.execute(action="list")
        assert "无待执行" in r.content

    @pytest.mark.asyncio
    async def test_persistence(self, tmp_path):
        tool1 = CronTool(tmp_path)
        await tool1.execute(action="add", schedule="3h", message="持久化测试")

        # 新建实例验证持久化
        tool2 = CronTool(tmp_path)
        r = await tool2.execute(action="list")
        assert "持久化测试" in r.content
