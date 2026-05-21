"""测试 cron/cron_service.py — CronService 增删查改 + 调度计算."""

from pathlib import Path

import pytest

from mxwbot.cron.types import CronJob, CronSchedule, CronStore
from mxwbot.cron.cron_service import (
    _now_ms,
    _compute_next_run,
    _parse_every,
    _parse_at,
    _validate_schedule,
    CronService,
)


class TestCronSchedule:
    def test_compute_next_run_every(self):
        """every 模式: 下次 = now + every_ms."""
        now = 1_000_000_000_000  # fixed timestamp
        sched = CronSchedule(kind="every", every_ms=60_000)  # 1 min
        nxt = _compute_next_run(sched, now)
        assert nxt == now + 60_000

    def test_compute_next_run_at_past(self):
        """at 模式: 已过期返回 None."""
        now = 1_000_000_000_000
        sched = CronSchedule(kind="at", at_ms=now - 1000)
        assert _compute_next_run(sched, now) is None

    def test_compute_next_run_at_future(self):
        """at 模式: 未到期返回 at_ms."""
        now = 1_000_000_000_000
        future = now + 3_600_000
        sched = CronSchedule(kind="at", at_ms=future)
        assert _compute_next_run(sched, now) == future

    def test_parse_every_valid(self):
        assert _parse_every("30m") == 1_800_000
        assert _parse_every("1h") == 3_600_000
        assert _parse_every("1d") == 86_400_000

    def test_parse_every_invalid(self):
        assert _parse_every("bad") is None
        assert _parse_every("") is None

    def test_parse_at_valid(self):
        ms = _parse_at("2026-06-01T09:00:00")
        assert ms is not None
        assert ms > _now_ms()

    def test_validate_tz_only_for_cron(self):
        with pytest.raises(ValueError, match="tz"):
            _validate_schedule(CronSchedule(kind="every", every_ms=1000, tz="UTC"))


class TestCronService:
    @pytest.mark.asyncio
    async def test_add_and_list(self, tmp_path):
        """添加任务后可列出."""
        store = tmp_path / "jobs.json"
        svc = CronService(store_path=store)
        await svc.start()

        job = svc.add_job("test-job", CronSchedule(kind="every", every_ms=60_000), "hello")
        assert job.id
        assert job.name == "test-job"

        jobs = svc.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].id == job.id

        svc.stop()

    @pytest.mark.asyncio
    async def test_remove_job(self, tmp_path):
        """删除不存在的任务返回 False."""
        store = tmp_path / "jobs.json"
        svc = CronService(store_path=store)
        await svc.start()
        assert svc.remove_job("nonexistent") is False
        svc.stop()

    @pytest.mark.asyncio
    async def test_enable_disable(self, tmp_path):
        """启用/禁用任务."""
        store = tmp_path / "jobs.json"
        svc = CronService(store_path=store)
        await svc.start()

        job = svc.add_job("tog", CronSchedule(kind="every", every_ms=60_000), "msg")
        assert job.enabled is True

        svc.enable_job(job.id, enabled=False)
        j = svc.get_job(job.id)
        assert j.enabled is False
        svc.stop()

    @pytest.mark.asyncio
    async def test_persist_and_reload(self, tmp_path):
        """任务持久化后重启可恢复."""
        store = tmp_path / "jobs.json"
        svc1 = CronService(store_path=store)
        await svc1.start()
        svc1.add_job("persist", CronSchedule(kind="every", every_ms=60_000), "survive")
        svc1.stop()

        # New instance loads same store
        svc2 = CronService(store_path=store)
        await svc2.start()
        jobs = svc2.list_jobs(include_disabled=True)
        assert len(jobs) == 1
        assert jobs[0].name == "persist"
        svc2.stop()

    @pytest.mark.asyncio
    async def test_status(self, tmp_path):
        store = tmp_path / "jobs.json"
        svc = CronService(store_path=store)
        await svc.start()
        s = svc.status()
        assert s["jobs"] == 0
        svc.stop()

    @pytest.mark.asyncio
    async def test_run_job_executes_callback(self, tmp_path):
        """手动触发 run_job 执行 callback."""
        store = tmp_path / "jobs.json"
        executed = []

        async def on_job(job: CronJob):
            executed.append(job.name)

        svc = CronService(store_path=store, on_job=on_job)
        await svc.start()
        job = svc.add_job("cb-test", CronSchedule(kind="every", every_ms=60_000), "run")
        await svc.run_job(job.id, force=True)
        assert "cb-test" in executed
        svc.stop()
