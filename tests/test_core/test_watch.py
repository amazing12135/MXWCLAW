"""测试 watch/ — MetricsCollector + WatchPanel."""

import pytest

from mxwbot.bus.queue import MessageBus
from mxwbot.config.schema import (
    AgentDefaultConfig,
    HeartBeatConfig,
    MXWConfig,
    ProviderConfig,
)
from mxwbot.system.manager import SystemManager
from mxwbot.watch.metrics import MetricsCollector, SystemSnapshot
from mxwbot.watch.panel import WatchPanel


class TestMetricsCollector:
    @pytest.mark.asyncio
    async def test_collect_returns_snapshot(self, tmp_path):
        cfg = MXWConfig(workspace=str(tmp_path))
        mgr = SystemManager(cfg)
        collector = MetricsCollector(mgr)

        snap = collector.collect()
        assert isinstance(snap, SystemSnapshot)
        assert snap.uptime_seconds >= 0
        assert snap.components is not None

    @pytest.mark.asyncio
    async def test_collect_after_bootstrap(self, tmp_path):
        cfg = MXWConfig(
            workspace=str(tmp_path),
            providers=[ProviderConfig(name="openai", api_key="sk-test", model="gpt-4")],
            agent=AgentDefaultConfig(max_iterations=3),
            heartbeat=HeartBeatConfig(enabled=False),
        )
        mgr = SystemManager(cfg)
        collector = MetricsCollector(mgr)
        await mgr.bootstrap()

        snap = collector.collect()
        assert snap.cron_jobs >= 0


class TestSystemSnapshot:
    def test_defaults(self):
        snap = SystemSnapshot()
        assert snap.uptime_seconds == 0
        assert snap.active_sessions == 0
        assert snap.queue_depth_input == 0


class TestWatchPanel:
    def test_watch_panel_creation(self, tmp_path):
        cfg = MXWConfig(workspace=str(tmp_path))
        mgr = SystemManager(cfg)
        collector = MetricsCollector(mgr)
        panel = WatchPanel(collector)
        assert panel is not None

    @pytest.mark.asyncio
    async def test_watch_panel_stop(self, tmp_path):
        cfg = MXWConfig(workspace=str(tmp_path))
        mgr = SystemManager(cfg)
        collector = MetricsCollector(mgr)
        panel = WatchPanel(collector)
        panel.stop()  # should not raise
