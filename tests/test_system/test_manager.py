"""测试 system/manager.py — SystemManager 组件注册 + 生命周期."""

import pytest

from mxwbot.config.schema import (
    AgentDefaultConfig,
    ChannelConfig,
    HeartBeatConfig,
    MXWConfig,
    ProviderConfig,
)
from mxwbot.system.manager import Component, ComponentState, SystemManager


def _make_config(*, workspace=".mxwbot") -> MXWConfig:
    return MXWConfig(
        workspace=workspace,
        providers=[ProviderConfig(name="openai", api_key="sk-test", model="gpt-4")],
        channels=[],
        agent=AgentDefaultConfig(max_iterations=3),
        heartbeat=HeartBeatConfig(enabled=False),
    )


class TestSystemManager:
    @pytest.mark.asyncio
    async def test_bootstrap_creates_components(self, tmp_path):
        """bootstrap() 创建 Provider + Memory + Tools + LoopPool."""
        cfg = _make_config(workspace=str(tmp_path))
        mgr = SystemManager(cfg)
        await mgr.bootstrap()
        assert mgr.provider is not None
        assert mgr.memory is not None
        assert mgr.tools is not None
        assert mgr.loop_pool is not None
        assert mgr.cron_service is not None
        assert mgr.heartbeat is not None

    @pytest.mark.asyncio
    async def test_register_and_list_components(self, tmp_path):
        cfg = _make_config(workspace=str(tmp_path))
        mgr = SystemManager(cfg)
        await mgr.bootstrap()
        comps = mgr.list_components()
        names = {c.name for c in comps}
        assert "loop_pool" in names
        assert "cron" in names
        assert "heartbeat" in names

    @pytest.mark.asyncio
    async def test_is_running(self, tmp_path):
        cfg = _make_config(workspace=str(tmp_path))
        mgr = SystemManager(cfg)
        await mgr.bootstrap()
        assert not mgr.is_running("loop_pool")
        assert not mgr.is_running("cron")

    @pytest.mark.asyncio
    async def test_start_and_stop_component(self, tmp_path):
        cfg = _make_config(workspace=str(tmp_path))
        mgr = SystemManager(cfg)
        await mgr.bootstrap()
        ok = await mgr.start_component("cron")
        assert ok
        assert mgr.is_running("cron")
        ok = await mgr.stop_component("cron")
        assert ok
        assert not mgr.is_running("cron")

    @pytest.mark.asyncio
    async def test_start_unknown_returns_false(self, tmp_path):
        cfg = _make_config(workspace=str(tmp_path))
        mgr = SystemManager(cfg)
        await mgr.bootstrap()
        assert not await mgr.start_component("nonexistent")

    @pytest.mark.asyncio
    async def test_topological_order(self, tmp_path):
        cfg = _make_config(workspace=str(tmp_path))
        mgr = SystemManager(cfg)
        await mgr.bootstrap()
        ordered = mgr._topological_order()
        names = [c.name for c in ordered]
        # loop_pool depends on tools, so it must appear after
        lp_idx = names.index("loop_pool")
        assert lp_idx > 0  # not first

    @pytest.mark.asyncio
    async def test_create_wechat_channel(self, tmp_path):
        cfg = _make_config(workspace=str(tmp_path))
        cfg.channels = [ChannelConfig(type="wechat", enabled=True, settings={"token": "t1"})]
        mgr = SystemManager(cfg)
        await mgr.bootstrap()
        comps = mgr.list_components()
        assert any("wechat" in c.name for c in comps)

    def test_component_state_enum(self):
        assert ComponentState.STOPPED.value == "stopped"
        assert ComponentState.RUNNING.value == "running"

    def test_component_dataclass(self):
        c = Component("test", ComponentState.STOPPED, None, depends_on=["dep1"])
        assert c.name == "test"
        assert c.depends_on == ["dep1"]
