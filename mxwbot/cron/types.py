"""Cron job data types.

Mirrors the nanobot cron types but adapted for mxwbot's dataclass
convention and JSON-file persistence model.

- CronSchedule: defines when the job should run (at/every/cron)
- CronPayload: defines what the job does (currently only "agent_turn")
- CronRunRecord: records the outcome of each run (timestamp, status, error)
- CronJobState: tracks runtime state (next run time, last status, history)
- CronJob: the main job definition (id, name, schedule, payload, state)
- CronStore: the top-level structure for storing all jobs in a JSON file

"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class CronSchedule:
    """Schedule definition for a cron job.

    Three schedule kinds:
      - ``at``: one-shot at a specific Unix-ms timestamp
      - ``every``: repeat every *every_ms* milliseconds
      - ``cron``: standard crontab expression (requires ``croniter``)
    """

    kind: Literal["at", "every", "cron"]
    at_ms: int | None = None
    every_ms: int | None = None
    expr: str | None = None
    tz: str | None = None  # timezone for cron kind only


@dataclass
class CronPayload:
    """What to do when the job fires."""

    kind: Literal["agent_turn"] = "agent_turn"
    message: str = ""                 # Content sent to the agent
    deliver: bool = False             # Whether to deliver the response
    channel: str | None = None        # Target channel for delivery
    to: str | None = None             # Target chat_id for delivery


@dataclass
class CronRunRecord:
    """A single execution record."""

    run_at_ms: int
    status: Literal["ok", "error", "skipped"]
    duration_ms: int = 0
    error: str | None = None


@dataclass
class CronJobState:
    """Runtime state of a job (updated after each run)."""

    next_run_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_status: Literal["ok", "error", "skipped"] | None = None
    last_error: str | None = None
    run_history: list[CronRunRecord] = field(default_factory=list)


@dataclass
class CronJob:
    """A scheduled job."""

    id: str
    name: str
    enabled: bool = True
    schedule: CronSchedule = field(default_factory=lambda: CronSchedule(kind="every"))
    payload: CronPayload = field(default_factory=CronPayload)
    state: CronJobState = field(default_factory=CronJobState)
    created_at_ms: int = 0
    updated_at_ms: int = 0
    delete_after_run: bool = False


@dataclass
class CronStore:
    """Persistent store for cron jobs."""

    version: int = 1
    jobs: list[CronJob] = field(default_factory=list)
