"""测试 system/api.py — ManagementAPI HTTP 路由."""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from mxwbot.config.schema import (
    AgentDefaultConfig,
    HeartBeatConfig,
    MXWConfig,
    ProviderConfig,
)
from mxwbot.system.api import ManagementAPI
from mxwbot.system.manager import SystemManager


def _make_config(*, workspace=".mxwbot") -> MXWConfig:
    return MXWConfig(
        workspace=workspace,
        providers=[ProviderConfig(name="openai", api_key="sk-test", model="gpt-4")],
        channels=[],
        agent=AgentDefaultConfig(max_iterations=3),
        heartbeat=HeartBeatConfig(enabled=False),
    )


async def _make_client(mgr) -> TestClient:
    api = ManagementAPI(mgr)
    server = TestServer(api._app)
    await server.start_server()
    return TestClient(server)


class TestManagementAPI:
    """ManagementAPI integration tests using aiohttp test client."""

    @pytest.mark.asyncio
    async def test_status_endpoint(self, tmp_path):
        cfg = _make_config(workspace=str(tmp_path))
        mgr = SystemManager(cfg)
        await mgr.bootstrap()

        client = await _make_client(mgr)
        resp = await client.get("/api/status")
        assert resp.status == 200
        data = await resp.json()
        assert "loop_pool" in data

    @pytest.mark.asyncio
    async def test_channel_list_empty(self, tmp_path):
        cfg = _make_config(workspace=str(tmp_path))
        mgr = SystemManager(cfg)
        await mgr.bootstrap()

        client = await _make_client(mgr)
        resp = await client.get("/api/channel/list")
        assert resp.status == 200
        assert "channels" in await resp.json()

    @pytest.mark.asyncio
    async def test_heartbeat_status(self, tmp_path):
        cfg = _make_config(workspace=str(tmp_path))
        mgr = SystemManager(cfg)
        await mgr.bootstrap()

        client = await _make_client(mgr)
        resp = await client.get("/api/heartbeat/status")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_cron_list(self, tmp_path):
        cfg = _make_config(workspace=str(tmp_path))
        mgr = SystemManager(cfg)
        await mgr.bootstrap()

        client = await _make_client(mgr)
        resp = await client.get("/api/cron/list")
        assert resp.status == 200
        assert "jobs" in await resp.json()

    @pytest.mark.asyncio
    async def test_cron_add_missing_fields(self, tmp_path):
        cfg = _make_config(workspace=str(tmp_path))
        mgr = SystemManager(cfg)
        await mgr.bootstrap()

        client = await _make_client(mgr)
        resp = await client.post("/api/cron/add", json={})
        assert resp.status == 400
        assert "Missing" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_channel_start_requires_name(self, tmp_path):
        cfg = _make_config(workspace=str(tmp_path))
        mgr = SystemManager(cfg)
        await mgr.bootstrap()

        client = await _make_client(mgr)
        resp = await client.post("/api/channel/start")
        assert resp.status == 400
        assert "name" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_cron_get_nonexistent(self, tmp_path):
        cfg = _make_config(workspace=str(tmp_path))
        mgr = SystemManager(cfg)
        await mgr.bootstrap()

        client = await _make_client(mgr)
        resp = await client.get("/api/cron/doesnotexist")
        assert resp.status == 404
