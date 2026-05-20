"""Tests for config/path.py — pure-function path helpers."""

from pathlib import Path

import pytest

from mxwbot.config.path import (
    ensure_all_dirs,
    get_checkpoint_path,
    get_checkpoints_dir,
    get_heartbeat_dir,
    get_logs_dir,
    get_memory_db_path,
    get_memory_dir,
    get_session_path,
    get_sessions_dir,
    get_skills_dir,
    get_subagents_dir,
    get_subagent_workspace,
)


class TestPathFunctions:
    @pytest.fixture
    def ws(self, tmp_path) -> Path:
        return tmp_path / ".mxwbot"

    def test_sessions_dir_created(self, ws):
        d = get_sessions_dir(ws)
        assert d.exists()
        assert d.name == "sessions"

    def test_ensure_all(self, ws):
        ensure_all_dirs(ws)
        assert get_sessions_dir(ws).exists()
        assert get_checkpoints_dir(ws).exists()
        assert get_memory_dir(ws).exists()
        assert get_logs_dir(ws).exists()
        assert get_skills_dir(ws).exists()
        assert get_subagents_dir(ws).exists()
        assert get_heartbeat_dir(ws).exists()

    def test_session_path_sanitizes_chat_id(self, ws):
        p = get_session_path(ws, "wechat", "user@wx_12345/evil")
        assert "/" not in p.name
        assert "\\" not in p.name
        assert p.name.startswith("wechat_")

    def test_checkpoint_path_sanitization(self, ws):
        p = get_checkpoint_path(ws, "sess/../../evil")
        assert "/" not in p.name
        assert ".." not in p.name

    def test_dirs_created_in_workspace(self, ws):
        d = get_sessions_dir(ws)
        assert str(ws.resolve()) in str(d.resolve())

    def test_memory_db_path(self, ws):
        assert get_memory_db_path(ws).name == "memory.db"

    def test_subagent_workspace_sanitized(self, ws):
        p = get_subagent_workspace(ws, "agent/../evil")
        assert ".." not in p.name
        assert "/" not in p.name


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
