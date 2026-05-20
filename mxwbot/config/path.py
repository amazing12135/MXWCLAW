"""Runtime path helpers — pure functions, no state.

All paths are derived from a *workspace* root.  ``mkdir(exist_ok=True)``
is cheap (a single stat when the directory already exists), so there is no
need for a memoising class.
"""

from __future__ import annotations

from pathlib import Path


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _ensure(path: Path) -> Path:
    """Create *path* (and parents) if missing; return it unchanged."""
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Directory accessors
# ---------------------------------------------------------------------------

def get_sessions_dir(workspace: Path) -> Path:
    return _ensure(workspace / "sessions")


def get_checkpoints_dir(workspace: Path) -> Path:
    return _ensure(workspace / "checkpoints")


def get_memory_dir(workspace: Path) -> Path:
    return _ensure(workspace / "memory")


def get_logs_dir(workspace: Path) -> Path:
    return _ensure(workspace / "logs")


def get_skills_dir(workspace: Path) -> Path:
    return _ensure(workspace / "skills")


def get_subagents_dir(workspace: Path) -> Path:
    return _ensure(workspace / "subagents")


def get_heartbeat_dir(workspace: Path) -> Path:
    return _ensure(workspace / "heartbeat")


def get_cache_dir(workspace: Path) -> Path:
    return _ensure(workspace / "cache")


def get_tmp_dir(workspace: Path) -> Path:
    return _ensure(workspace / "tmp")


# ---------------------------------------------------------------------------
# Typed path builders (directory + safe filename)
# ---------------------------------------------------------------------------

def _sanitize(name: str) -> str:
    """Replace characters unsafe for a filename component."""
    return name.replace("/", "_").replace("\\", "_").replace("..", "_")


def get_session_path(workspace: Path, channel: str, chat_id: str) -> Path:
    """JSONL file path for *channel* + *chat_id*."""
    return get_sessions_dir(workspace) / f"{channel}_{_sanitize(chat_id)}.jsonl"


def get_checkpoint_path(workspace: Path, session_id: str) -> Path:
    """Checkpoint sub-directory for *session_id*."""
    return get_checkpoints_dir(workspace) / _sanitize(session_id)


def get_memory_db_path(workspace: Path) -> Path:
    return get_memory_dir(workspace) / "memory.db"


def get_heartbeat_db_path(workspace: Path) -> Path:
    return get_heartbeat_dir(workspace) / "heartbeat.db"


def get_log_path(workspace: Path, name: str = "mxwbot") -> Path:
    return get_logs_dir(workspace) / f"{name}.jsonl"


def get_subagent_workspace(workspace: Path, agent_id: str) -> Path:
    return get_subagents_dir(workspace) / _sanitize(agent_id)


def get_skill_file_path(workspace: Path, skill_name: str) -> Path:
    return get_skills_dir(workspace) / f"{_sanitize(skill_name)}.md"


# ---------------------------------------------------------------------------
# Batch creation
# ---------------------------------------------------------------------------

def ensure_all_dirs(workspace: Path) -> None:
    """Pre-create all standard runtime directories."""
    for fn in (
        get_sessions_dir,
        get_checkpoints_dir,
        get_memory_dir,
        get_logs_dir,
        get_skills_dir,
        get_subagents_dir,
        get_heartbeat_dir,
        get_cache_dir,
        get_tmp_dir,
    ):
        fn(workspace)
