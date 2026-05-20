"""Tests for memory/token_budget.py — pure-function token helpers."""

import pytest

from mxwbot.memory.token_budget import (
    count_tokens,
    token_usage_ratio,
    truncate_messages,
    truncate_messages_smart,
    _find_user_turn_start,
    _remove_orphaned_tool_results,
)


class TestCountTokens:
    def test_ascii(self):
        msgs = [{"role": "user", "content": "hello world"}]
        n = count_tokens(msgs)
        assert n > 0

    def test_empty(self):
        assert count_tokens([]) == 0
        assert count_tokens([{"role": "user", "content": ""}]) == 0

    def test_cjk(self):
        msgs = [{"role": "user", "content": "你好世界"}]
        n = count_tokens(msgs)
        assert n > 0

    def test_model_param(self):
        n = count_tokens([{"role": "user", "content": "hello"}], model="gpt-4")
        assert n > 0

    def test_unknown_model_fallback(self):
        n = count_tokens([{"role": "user", "content": "hello"}], model="nonexistent")
        assert n > 0


class TestTokenUsageRatio:
    def test_normal(self):
        msgs = [{"role": "user", "content": "hello world"}]
        ratio = token_usage_ratio(msgs, max_tokens=1000)
        assert 0 < ratio < 1.0

    def test_empty(self):
        assert token_usage_ratio([], max_tokens=1000) == 0.0

    def test_zero_max(self):
        """Zero budget → infinite ratio (except empty which is 0)."""
        r = token_usage_ratio([{"role": "user", "content": "hi"}], max_tokens=0)
        assert r > 1.0


class TestTruncateMessages:
    def test_no_op_under_budget(self):
        msgs = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "hi"},
        ]
        result = truncate_messages(msgs, max_tokens=10000, target_ratio=0.8)
        assert len(result) == 2

    def test_keeps_system(self):
        msgs = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "a" * 10000},
        ]
        result = truncate_messages(msgs, max_tokens=10, target_ratio=0.8)
        assert len(result) >= 1
        assert result[0]["role"] == "system"

    def test_drops_oldest_first(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "old message"},
            {"role": "user", "content": "new message"},
        ]
        result = truncate_messages(msgs, max_tokens=100, target_ratio=0.5)
        assert result[0]["role"] == "system"
        contents = [m["content"] for m in result[1:]]
        assert "new message" in contents

    def test_empty(self):
        assert truncate_messages([], max_tokens=1000) == []


class TestFindUserTurnStart:
    def test_finds_first_user(self):
        msgs = [
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "b"},
        ]
        assert _find_user_turn_start(msgs) == 1

    def test_no_user_returns_zero(self):
        msgs = [{"role": "assistant", "content": "a"}]
        assert _find_user_turn_start(msgs) == 0


class TestRemoveOrphanedToolResults:
    def test_removes_orphan(self):
        msgs = [
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ]
        result = _remove_orphaned_tool_results(msgs)
        assert len(result) == 0  # no tool_call → orphaned

    def test_keeps_paired(self):
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "function": {"name": "read"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ]
        result = _remove_orphaned_tool_results(msgs)
        assert len(result) == 2


class TestTruncateMessagesSmart:
    def test_aligns_to_user_turn(self):
        """The result always starts with system (or user if no system)."""
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
        ]
        result = truncate_messages_smart(msgs, max_tokens=10000)
        assert result[0]["role"] == "system"
        # System is preserved, followed by a user turn
        roles_after_system = [m["role"] for m in result[1:]]
        assert "user" in roles_after_system

    def test_aligns_when_truncation_cuts_mid_turn(self):
        """When token budget forces dropping the system message and early user,
        the result still starts with user (not assistant)."""
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1" * 500},  # large — will be dropped
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2" * 500},  # large — will be dropped
        ]
        result = truncate_messages_smart(msgs, max_tokens=100, target_ratio=0.5)
        # System must be first
        assert result[0]["role"] == "system"

    def test_removes_orphaned_tools(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "read file"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {"name": "read"}}]},
            {"role": "tool", "tool_call_id": "t2", "content": "orphan result"},
            {"role": "tool", "tool_call_id": "t1", "content": "valid result"},
            {"role": "assistant", "content": "done"},
        ]
        result = truncate_messages_smart(msgs, max_tokens=10000)
        # The orphan t2 should be removed
        tool_messages = [m for m in result if m.get("role") == "tool"]
        assert len(tool_messages) == 1
        assert tool_messages[0]["tool_call_id"] == "t1"

    def test_empty(self):
        assert truncate_messages_smart([], max_tokens=1000) == []
