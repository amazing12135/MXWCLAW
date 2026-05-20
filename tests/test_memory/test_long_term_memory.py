"""Tests for memory/long_term_memory.py."""

import asyncio

import pytest

from mxwbot.memory.long_term_memory import LongTermMemory


class TestLongTermMemory:
    @pytest.fixture
    async def ltm(self, tmp_path):
        db = LongTermMemory(tmp_path / "test_memory.db")
        await db.init_db()
        return db

    @pytest.mark.asyncio
    async def test_add_and_search(self, ltm):
        await ltm.add({"content": "User prefers dark mode", "category": "preference", "importance": 0.8})
        await ltm.add({"content": "Project uses pytest for testing", "category": "project_info", "importance": 0.6})

        results = await ltm.search("dark mode")
        assert len(results) >= 1
        assert "dark mode" in results[0]["content"]

    @pytest.mark.asyncio
    async def test_search_no_match(self, ltm):
        await ltm.add({"content": "User prefers dark mode", "category": "preference", "importance": 0.8})
        results = await ltm.search("nonexistent_term_xyz")
        assert results == []

    @pytest.mark.asyncio
    async def test_add_batch(self, ltm):
        ids = await ltm.add_batch([
            {"content": "Memory A", "category": "fact"},
            {"content": "Memory B", "category": "fact"},
        ])
        assert len(ids) == 2
        results = await ltm.get_recent(10)
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_get_by_category(self, ltm):
        await ltm.add({"content": "A preference", "category": "preference"})
        await ltm.add({"content": "A fact", "category": "fact"})

        prefs = await ltm.get_by_category("preference")
        assert len(prefs) == 1
        assert prefs[0]["category"] == "preference"

    @pytest.mark.asyncio
    async def test_delete(self, ltm):
        eid = await ltm.add({"content": "temporary memory"})
        assert await ltm.delete(eid) is True
        assert await ltm.delete(eid) is False  # already gone

    @pytest.mark.asyncio
    async def test_search_fallback_like(self, ltm):
        """FTS5 special chars trigger LIKE fallback."""
        await ltm.add({"content": "AND is also a word in English"})
        results = await ltm.search("AND is also")
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_get_recent(self, ltm):
        await ltm.add({"content": "old"})
        await asyncio.sleep(0.01)
        await ltm.add({"content": "new"})
        recent = await ltm.get_recent(1)
        assert len(recent) == 1
        assert recent[0]["content"] == "new"

    @pytest.mark.asyncio
    async def test_context_for_query(self, ltm):
        await ltm.add({"content": "User prefers dark mode", "category": "preference", "importance": 0.8})
        ctx = await ltm.context_for_query("dark")
        assert "dark mode" in ctx

    @pytest.mark.asyncio
    async def test_context_empty(self, ltm):
        ctx = await ltm.context_for_query("nonexistent_xyz")
        assert ctx == ""
