"""Metrics collector — reads live state from SystemManager & Bus."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class SystemSnapshot:
    """Point-in-time system metrics."""
    uptime_seconds: float = 0.0
    active_sessions: int = 0
    queue_depth_input: int = 0
    components: dict[str, str] = field(default_factory=dict)  # name → state
    cron_jobs: int = 0
    cron_next_wake_s: float | None = None


class MetricsCollector:
    """Pull metrics from the live ``SystemManager``."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager
        self._start = datetime.now(timezone.utc)

    def collect(self) -> SystemSnapshot:
        mgr = self._manager
        uptime = (datetime.now(timezone.utc) - self._start).total_seconds()

        return SystemSnapshot(
            uptime_seconds=uptime,
            active_sessions=len(getattr(mgr.sessions, "_sessions", {})),
            queue_depth_input=mgr.bus.queue_depth_input,
            components={
                c.name: c.state.value
                for c in mgr.list_components()
            },
            cron_jobs=mgr.cron_service.status()["jobs"] if mgr.cron_service else 0,
            cron_next_wake_s=(
                (mgr.cron_service.status().get("next_wake_at_ms") or 0) / 1000
                if mgr.cron_service else None
            ),
        )
