"""测试 heartbeat/heartbeat_service.py."""

import json
from pathlib import Path

import pytest

from mxwbot.heartbeat.heartbeat_service import HeartbeatService
from mxwbot.providers.base import LLMProvider, LLMResponse, TokenUsage, ToolCallRequest


class _FakeProvider(LLMProvider):
    """Mock LLM provider that returns a preset response."""

    def __init__(self, response=None):
        super().__init__(model="test")
        self._response = response
        self.calls: list[dict] = []

    @property
    def supports_streaming(self) -> bool:
        return True

    async def _chat_impl(self, messages, tools):
        return self._response or LLMResponse(content="skip", usage=TokenUsage())

    async def chat_with_retry(self, messages, tools=None, **kw):
        self.calls.append({"messages": messages, "tools": tools})
        resp = await self._chat_impl(messages, tools)
        return resp

    async def _chat_stream_impl(self, messages, tools):
        if False:
            yield
        return


class TestHeartbeatService:

    @pytest.mark.asyncio
    async def test_start_stop(self, tmp_path):
        svc = HeartbeatService(workspace=tmp_path, provider=_FakeProvider(), enabled=True)
        await svc.start()
        assert svc.status()["running"] is True
        svc.stop()

    @pytest.mark.asyncio
    async def test_disabled_skips_start(self, tmp_path):
        svc = HeartbeatService(workspace=tmp_path, provider=_FakeProvider(), enabled=False)
        await svc.start()
        assert svc.status()["running"] is False

    @pytest.mark.asyncio
    async def test_trigger_no_file_returns_none(self, tmp_path):
        svc = HeartbeatService(workspace=tmp_path, provider=_FakeProvider())
        result = await svc.trigger_now()
        assert result is None

    @pytest.mark.asyncio
    async def test_trigger_with_file_skips(self, tmp_path):
        """HEARTBEAT.md 存在但 LLM 决策 skip."""
        (tmp_path / "HEARTBEAT.md").write_text("# Tasks\n- task 1")
        provider = _FakeProvider(
            LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(
                    id="c1", name="heartbeat",
                    arguments=json.dumps({"action": "skip"}),
                )],
                usage=TokenUsage(),
            )
        )
        svc = HeartbeatService(workspace=tmp_path, provider=provider)
        result = await svc.trigger_now()
        assert result is None
        assert len(provider.calls) == 1  # One LLM call for decision

    @pytest.mark.asyncio
    async def test_trigger_run_calls_execute(self, tmp_path):
        """LLM 决策 run → 回调 on_execute."""
        (tmp_path / "HEARTBEAT.md").write_text("# Tasks\n- urgent task")
        executed = []

        async def on_exec(task):
            executed.append(task)
            return "done"

        provider = _FakeProvider(
            LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(
                    id="c1", name="heartbeat",
                    arguments=json.dumps({"action": "run", "tasks": "urgent task"}),
                )],
                usage=TokenUsage(),
            )
        )
        svc = HeartbeatService(workspace=tmp_path, provider=provider, on_execute=on_exec)
        result = await svc.trigger_now()
        assert result == "done"
        assert len(executed) == 1

    @pytest.mark.asyncio
    async def test_tick_no_file(self, tmp_path):
        """_tick: HEARTBEAT.md 不存在时跳过."""
        svc = HeartbeatService(workspace=tmp_path, provider=_FakeProvider())
        await svc._tick()  # should not raise

    @pytest.mark.asyncio
    async def test_decide_skip(self):
        """_decide: LLM 返回 skip 时返回空 tasks."""
        provider = _FakeProvider(
            LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(
                    id="c1", name="heartbeat",
                    arguments=json.dumps({"action": "skip"}),
                )],
                usage=TokenUsage(),
            )
        )
        svc = HeartbeatService(workspace=Path("."), provider=provider)
        action, tasks = await svc._decide("no tasks")
        assert action == "skip"
        assert tasks == ""

    @pytest.mark.asyncio
    async def test_decide_no_tool_calls(self):
        """_decide: 无 tool_calls 时默认 skip."""
        provider = _FakeProvider(
            LLMResponse(content="ok", usage=TokenUsage())
        )
        svc = HeartbeatService(workspace=Path("."), provider=provider)
        action, tasks = await svc._decide("tasks")
        assert action == "skip"

    @pytest.mark.asyncio
    async def test_read_file_missing(self):
        svc = HeartbeatService(workspace=Path("/nonexistent"), provider=_FakeProvider())
        assert svc._read_file() is None
