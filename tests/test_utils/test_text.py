"""Tests for utils/text.py — think tag cleaning, token estimation, truncation, replay sanitization."""

import pytest

from mxwbot.utils.text import (
    clean_assistant_replay_text,
    clean_think_tags,
    estimate_tokens,
    truncate_messages,
)


class TestCleanThinkTags:
    def test_basic(self):
        assert clean_think_tags("<think>reasoning</think>answer") == "answer"

    def test_multiline(self):
        text = "<think>\nLet me think\nabout this\n</think>\n\nHere is the result"
        expected = "Here is the result"
        assert clean_think_tags(text) == expected

    def test_no_tags(self):
        assert clean_think_tags("hello world") == "hello world"

    def test_only_tags(self):
        assert clean_think_tags("<think>nothing</think>") == ""

    def test_case_insensitive(self):
        assert clean_think_tags("<THINK>reason</THINK>done") == "done"


class TestEstimateTokens:
    def test_empty(self):
        assert estimate_tokens("") == 0

    def test_ascii(self):
        n = estimate_tokens("hello world")
        assert n > 0

    def test_chinese(self):
        n = estimate_tokens("你好世界")
        assert n > 0

    def test_model_param(self):
        # Should not raise for known model
        n = estimate_tokens("hello", model="gpt-4")
        assert n > 0

    def test_unknown_model_fallback(self):
        n = estimate_tokens("hello", model="nonexistent-model-xyz")
        assert n > 0


class TestTruncateMessages:
    def test_no_truncation_when_under_budget(self):
        msgs = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "hi"},
        ]
        result = truncate_messages(msgs, max_tokens=999999)
        assert len(result) == 2

    def test_keeps_system_message(self):
        msgs = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "a" * 10000},
        ]
        result = truncate_messages(msgs, max_tokens=100)
        assert len(result) >= 1
        assert result[0]["role"] == "system"

    def test_empty_list(self):
        assert truncate_messages([], 100) == []


class TestCleanAssistantReplayText:
    def test_strips_message_time_prefix(self):
        text = "[Message Time: 2026-05-13 10:30:00]\nActual reply content"
        result = clean_assistant_replay_text(text)
        assert result == "Actual reply content"

    def test_strips_image_breadcrumb(self):
        text = "first line\n[image: /tmp/photo.jpg]\nlast line"
        result = clean_assistant_replay_text(text)
        assert "image" not in result
        assert "first line" in result
        assert "last line" in result

    def test_strips_tool_echo(self):
        text = "generate_image(sunset over mountains)"
        result = clean_assistant_replay_text(text)
        assert result == ""

    def test_preserves_normal_text(self):
        text = "I found 3 files in the directory"
        result = clean_assistant_replay_text(text)
        assert result == "I found 3 files in the directory"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
