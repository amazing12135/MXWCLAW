"""Checkpoint manager — strategy-driven snapshot save / restore.

Snapshots are saved at three trigger points (see design spec 3.8):
  1. Before non-readonly tool execution (most likely failure point)
  2. Periodic fallback (every N iterations in pure-text loops)
  3. Emergency (on unhandled exception)

Writes are atomic (tmp-file + ``os.replace()``) so a crash mid-write
never corrupts existing data.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiofiles

from mxwbot.config.path import get_checkpoint_path


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class CheckpointSnapshot:
    """A full snapshot of in-flight turn state."""

    id: str = field(default_factory=_new_id)
    session_id: str = ""
    iteration: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    token_usage: dict[str, Any] = field(default_factory=dict)
    state: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class CheckpointSummary:
    """Lightweight listing entry."""

    id: str
    iteration: int
    state: str
    created_at: str


class CheckpointManager:
    """Save and restore execution state with strategy-driven triggers."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    # -- save ---------------------------------------------------------------

    def _dir_for(self, session_id: str) -> Path:
        return get_checkpoint_path(self._workspace, session_id)

    async def save(
        self,
        snapshot: CheckpointSnapshot,
    ) -> str:
        """Persist *snapshot* atomically.  Returns the snapshot id."""
        if not snapshot.id:
            snapshot.id = _new_id()
        if not snapshot.created_at:
            snapshot.created_at = datetime.now(timezone.utc).isoformat()

        ckpt_dir = self._dir_for(snapshot.session_id)
        ckpt_dir.mkdir(parents=True, exist_ok=True)

        tmp_path = ckpt_dir / f"{snapshot.id}.json.tmp"
        final_path = ckpt_dir / f"{snapshot.id}.json"

        payload = asdict(snapshot)
        async with aiofiles.open(str(tmp_path), "w", encoding="utf-8") as f:
            await f.write(json.dumps(payload, ensure_ascii=False))

        os.replace(tmp_path, final_path)  # atomic
        return snapshot.id

    # -- load ---------------------------------------------------------------

    async def load(self, checkpoint_id: str, session_id: str) -> CheckpointSnapshot | None:
        """Load a specific checkpoint by id."""
        path = self._dir_for(session_id) / f"{checkpoint_id}.json"
        return await self._load_from(path)

    async def load_latest(self, session_id: str) -> CheckpointSnapshot | None:
        """Load the most recent checkpoint for *session_id*."""
        ckpt_dir = self._dir_for(session_id)
        if not ckpt_dir.exists():
            return None

        # Find the newest snapshot file
        jsons = sorted(ckpt_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in jsons:
            snapshot = await self._load_from(path)
            if snapshot is not None:
                return snapshot
        return None

    # -- list ---------------------------------------------------------------

    async def list_by_session(self, session_id: str) -> list[CheckpointSummary]:
        """List snapshot summaries for *session_id*."""
        ckpt_dir = self._dir_for(session_id)
        if not ckpt_dir.exists():
            return []

        summaries: list[CheckpointSummary] = []
        for path in sorted(ckpt_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
            data = self._read_json_sync(path)
            if data:
                summaries.append(CheckpointSummary(
                    id=data.get("id", path.stem),
                    iteration=data.get("iteration", 0),
                    state=data.get("state", ""),
                    created_at=data.get("created_at", ""),
                ))
        return summaries

    # -- cleanup ------------------------------------------------------------

    async def prune(self, session_id: str, keep_last: int = 5) -> int:
        """Remove old checkpoints, keeping the latest *keep_last*.

        Returns the number of files removed.
        """
        ckpt_dir = self._dir_for(session_id)
        if not ckpt_dir.exists():
            return 0

        files = sorted(ckpt_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        removed = 0
        for f in files[:-keep_last] if len(files) > keep_last else []:
            f.unlink(missing_ok=True)
            removed += 1
        return removed

    # -- internal -----------------------------------------------------------

    async def _load_from(self, path: Path) -> CheckpointSnapshot | None:
        if not path.exists():
            return None
        try:
            async with aiofiles.open(str(path), "r", encoding="utf-8") as f:
                data = json.loads(await f.read())
            return CheckpointSnapshot(
                id=data.get("id", path.stem),
                session_id=data.get("session_id", ""),
                iteration=data.get("iteration", 0),
                messages=data.get("messages", []),
                tool_results=data.get("tool_results", []),
                token_usage=data.get("token_usage", {}),
                state=data.get("state", ""),
                created_at=data.get("created_at", ""),
            )
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def _read_json_sync(path: Path) -> dict[str, Any] | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
