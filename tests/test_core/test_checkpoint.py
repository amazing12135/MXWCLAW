"""Tests for checkpoint/manager.py — CheckpointManager."""

import asyncio

import pytest

from mxwbot.checkpoint.manager import CheckpointManager, CheckpointSnapshot


class TestCheckpointManager:
    @pytest.fixture
    def cm(self, tmp_path) -> CheckpointManager:
        return CheckpointManager(tmp_path / ".mxwbot")

    def _snapshot(self, session_id="sess-1", iteration=1) -> CheckpointSnapshot:
        return CheckpointSnapshot(
            session_id=session_id,
            iteration=iteration,
            messages=[{"role": "user", "content": "hi"}],
            tool_results=[{"tool": "read_file", "result": "ok"}],
            token_usage={"input": 10, "output": 5},
            state="RUN",
        )

    @pytest.mark.asyncio
    async def test_save_and_load(self, cm):
        snap = self._snapshot()
        snap_id = await cm.save(snap)
        assert snap_id

        loaded = await cm.load(snap_id, "sess-1")
        assert loaded is not None
        assert loaded.session_id == "sess-1"
        assert loaded.iteration == 1
        assert loaded.messages == snap.messages
        assert loaded.tool_results == snap.tool_results
        assert loaded.state == "RUN"

    @pytest.mark.asyncio
    async def test_load_latest(self, cm):
        s1 = self._snapshot(iteration=1)
        s2 = self._snapshot(iteration=2)
        s3 = self._snapshot(iteration=3)

        await cm.save(s1)
        await asyncio.sleep(0.01)
        await cm.save(s2)
        await asyncio.sleep(0.01)
        await cm.save(s3)

        latest = await cm.load_latest("sess-1")
        assert latest is not None
        assert latest.iteration == 3

    @pytest.mark.asyncio
    async def test_load_nonexistent(self, cm):
        assert await cm.load_latest("nonexistent") is None
        assert await cm.load("no-id", "nonexistent") is None

    @pytest.mark.asyncio
    async def test_list_by_session(self, cm):
        await cm.save(self._snapshot(iteration=1))
        await asyncio.sleep(0.01)
        await cm.save(self._snapshot(iteration=2))

        summaries = await cm.list_by_session("sess-1")
        assert len(summaries) == 2
        assert summaries[0].iteration == 1
        assert summaries[1].iteration == 2

    @pytest.mark.asyncio
    async def test_prune(self, cm):
        for i in range(10):
            await cm.save(self._snapshot(iteration=i))
            await asyncio.sleep(0.005)

        removed = await cm.prune("sess-1", keep_last=3)
        assert removed == 7

        summaries = await cm.list_by_session("sess-1")
        assert len(summaries) == 3

    @pytest.mark.asyncio
    async def test_list_empty_session(self, cm):
        assert await cm.list_by_session("no-sess") == []
