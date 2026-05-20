"""测试 core/context.py"""

from pathlib import Path

import pytest

from mxwbot.core.context import ContextBuilder
from mxwbot.providers.base import LLMCallPurpose


class _FakeSession:
    def __init__(self):
        self.active_task = None
        self.session_summary = None
        self._messages = []

    def get_history(self, max_messages=50):
        return self._messages

    @property
    def messages(self):
        return self._messages


class _FakeMemory:
    async def get_context(self, query=None):
        return "记忆: 用户偏好 Python"


class _FakeSkills:
    def to_context_xml(self):
        return "<skills><skill name='test'>desc</skill></skills>"

    def get_always_loaded(self):
        return []


class TestContextBuilder:
    @pytest.fixture
    def builder(self):
        return ContextBuilder(Path("/tmp/workspace"))

    @pytest.mark.asyncio
    async def test_agent_full_context(self, builder):
        session = _FakeSession()
        session._messages.append({"role": "user", "content": "old message"})
        memory = _FakeMemory()
        skills = _FakeSkills()

        msgs = await builder.build(
            LLMCallPurpose.AGENT, session, "current question",
            memory_manager=memory, skill_loader=skills,
        )
        assert len(msgs) >= 2  # system + user at minimum
        assert msgs[0]["role"] == "system"
        assert "Python" in msgs[0]["content"]
        assert "old message" in msgs[1]["content"]
        assert msgs[-1]["content"] == "current question"

    @pytest.mark.asyncio
    async def test_summary_minimal_context(self, builder):
        msgs = await builder.build(LLMCallPurpose.SUMMARY, None, "summarise this")
        assert len(msgs) >= 1
        assert msgs[0]["role"] == "system"
        assert "summarise" in msgs[-1]["content"]
        # No injected memory (from MemoryManager) for SUMMARY
        system = msgs[0]["content"]
        assert "记忆: 用户偏好" not in system

    @pytest.mark.asyncio
    async def test_subagent_minimal_context(self, builder):
        msgs = await builder.build(LLMCallPurpose.SUBAGENT, None, "find files")
        assert msgs[0]["role"] == "system"
        # No skills, no memory
        system = msgs[0]["content"]
        assert "<skill" not in system

    @pytest.mark.asyncio
    async def test_agent_with_skills(self, builder):
        skills = _FakeSkills()
        msgs = await builder.build(
            LLMCallPurpose.AGENT, _FakeSession(), "hi",
            skill_loader=skills,
        )
        system = msgs[0]["content"]
        assert "test" in system

    @pytest.mark.asyncio
    async def test_agent_with_active_task(self, builder):
        session = _FakeSession()
        session.active_task = "deploying app"
        msgs = await builder.build(LLMCallPurpose.AGENT, session, "status?")
        system = msgs[0]["content"]
        assert "deploying app" in system
