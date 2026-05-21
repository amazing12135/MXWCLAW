"""Cron service — schedule-driven job execution engine.

Three schedule kinds:
  * ``at`` — one-shot at a Unix-ms timestamp
  * ``every`` — repeat every N milliseconds
  * ``cron`` — standard crontab expression (requires ``croniter``)

Jobs are persisted as JSON (``{workspace}/cron/jobs.json``).  The
existing ``CronTool`` (core/tools/cron.py) writes to the same file,
and ``CronService`` detects external modifications via mtime.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine

from mxwbot.cron.types import (
    CronJob,
    CronJobState,
    CronPayload,
    CronRunRecord,
    CronSchedule,
    CronStore,
)

logger = logging.getLogger("mxwbot.cron.service")

_MAX_RUN_HISTORY = 20


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _validate_schedule(schedule: CronSchedule) -> None:
    """Raise ``ValueError`` for schemas that would never fire."""
    if schedule.tz and schedule.kind != "cron":
        raise ValueError("tz can only be used with cron schedules")
    if schedule.kind == "cron" and schedule.tz:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(schedule.tz)
        except Exception:
            raise ValueError(f"unknown timezone '{schedule.tz}'") from None


def _compute_next_run(schedule: CronSchedule, now_ms: int) -> int | None:
    """Compute the next fire time (Unix ms) for *schedule*."""
    if schedule.kind == "at":
        return schedule.at_ms if schedule.at_ms and schedule.at_ms > now_ms else None

    if schedule.kind == "every":
        if not schedule.every_ms or schedule.every_ms <= 0:
            return None
        return now_ms + schedule.every_ms

    if schedule.kind == "cron" and schedule.expr:
        try:
            from zoneinfo import ZoneInfo
            from croniter import croniter

            base_s = now_ms / 1000
            tz = ZoneInfo(schedule.tz) if schedule.tz else datetime.now().astimezone().tzinfo
            base_dt = datetime.fromtimestamp(base_s, tz=tz)
            cron = croniter(schedule.expr, base_dt)
            next_dt = cron.get_next(datetime)
            return int(next_dt.timestamp() * 1000)
        except Exception:
            return None

    return None


def _parse_every(value: str) -> int | None:
    """Parse a human-readable interval like ``"30m"`` → ms."""
    value = value.strip().lower()
    if not value:
        return None
    multipliers = {"s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}
    for suffix, mult in multipliers.items():
        if value.endswith(suffix):
            try:
                return int(float(value[:-1]) * mult)
            except (ValueError, TypeError):
                return None
    return None


def _parse_at(value: str) -> int | None:
    """Parse an ISO-ish datetime string → Unix ms."""
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(value)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# CronService
# ---------------------------------------------------------------------------

class CronService:
    """Schedule-driven job execution engine.

    Args:
        store_path: Path to ``jobs.json``.
        on_job: Async callback invoked with the ``CronJob`` when it fires.
            The callback is responsible for publishing the job payload
            to the Bus.
    """

    def __init__(
        self,
        store_path: Path,
        *,
        on_job: Callable[[CronJob], Coroutine[Any, Any, str | None]] | None = None,
    ) -> None:
        self._store_path = store_path
        self._store: CronStore | None = None
        self._on_job = on_job
        self._last_mtime: float = 0.0
        self._timer_task: asyncio.Task | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Load store, recompute next runs, arm timer."""
        self._running = True
        self._load_store()
        self._recompute_next_runs()
        self._save_store()
        self._arm_timer()
        logger.info(
            "Cron service started with %d jobs",
            len(self._store.jobs) if self._store else 0,
        )

    def stop(self) -> None:
        """Cancel timer, mark stopped."""
        self._running = False
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None

    # ------------------------------------------------------------------
    # Persistence (auto-reload on external mtime change)
    # ------------------------------------------------------------------

    def _load_store(self) -> CronStore:
        """Return the current ``CronStore``, reloading from disk if stale."""
        if self._store and self._store_path.exists():
            mtime = self._store_path.stat().st_mtime
            if mtime != self._last_mtime:
                logger.debug("Cron: jobs.json modified externally, reloading")
                self._store = None

        if self._store:
            return self._store

        if self._store_path.exists():
            try:
                data = json.loads(self._store_path.read_text(encoding="utf-8"))
                jobs = _deserialize_jobs(data.get("jobs", []))
                self._store = CronStore(version=data.get("version", 1), jobs=jobs)
            except Exception as exc:
                logger.warning("Failed to load cron store: %s", exc)
                self._store = CronStore()
        else:
            self._store = CronStore()

        self._last_mtime = self._store_path.stat().st_mtime if self._store_path.exists() else 0.0
        return self._store

    def _save_store(self) -> None:
        if not self._store:
            return
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        data = _serialize_store(self._store)
        self._store_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8",
        )
        self._last_mtime = self._store_path.stat().st_mtime

    def external_reload(self) -> None:
        """Called by ``CronTool`` after it writes to ``jobs.json``.

        Forces a cache-flush + recompute + re-arm.
        """
        self._store = None
        self._load_store()
        self._recompute_next_runs()
        self._arm_timer()

    # ------------------------------------------------------------------
    # Scheduling engine
    # ------------------------------------------------------------------

    def _recompute_next_runs(self) -> None:
        if not self._store:
            return
        now = _now_ms()
        for job in self._store.jobs:
            if job.enabled:
                job.state.next_run_at_ms = _compute_next_run(job.schedule, now)

    def _get_next_wake_ms(self) -> int | None:
        if not self._store:
            return None
        times = [
            j.state.next_run_at_ms
            for j in self._store.jobs
            if j.enabled and j.state.next_run_at_ms
        ]
        return min(times) if times else None

    def _arm_timer(self) -> None:
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None

        next_wake = self._get_next_wake_ms()
        if next_wake is None or not self._running:
            return

        delay_ms = max(0, next_wake - _now_ms())
        delay_s = delay_ms / 1000

        async def _tick():
            await asyncio.sleep(delay_s)
            if self._running:
                await self._on_timer()

        self._timer_task = asyncio.create_task(_tick())

    async def _on_timer(self) -> None:
        self._load_store()
        if not self._store:
            return

        now = _now_ms()
        due = [
            j for j in self._store.jobs
            if j.enabled and j.state.next_run_at_ms and now >= j.state.next_run_at_ms
        ]
        for job in due:
            await self._execute_job(job)

        self._save_store()
        self._arm_timer()

    async def _execute_job(self, job: CronJob) -> None:
        start_ms = _now_ms()
        logger.info("Cron: executing job '%s' (%s)", job.name, job.id)

        try:
            if self._on_job:
                await self._on_job(job)
            job.state.last_status = "ok"
            job.state.last_error = None
        except Exception as exc:
            job.state.last_status = "error"
            job.state.last_error = str(exc)
            logger.error("Cron: job '%s' failed: %s", job.name, exc)

        end_ms = _now_ms()
        job.state.last_run_at_ms = start_ms
        job.updated_at_ms = end_ms

        job.state.run_history.append(CronRunRecord(
            run_at_ms=start_ms,
            status=job.state.last_status,
            duration_ms=end_ms - start_ms,
            error=job.state.last_error,
        ))
        job.state.run_history = job.state.run_history[-_MAX_RUN_HISTORY:]

        # One-shot cleanup
        if job.schedule.kind == "at":
            if job.delete_after_run:
                self._store.jobs = [j for j in self._store.jobs if j.id != job.id]
            else:
                job.enabled = False
                job.state.next_run_at_ms = None
        else:
            job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_jobs(self, *, include_disabled: bool = False) -> list[CronJob]:
        store = self._load_store()
        jobs = store.jobs if include_disabled else [j for j in store.jobs if j.enabled]
        return sorted(jobs, key=lambda j: j.state.next_run_at_ms or float("inf"))

    def add_job(
        self,
        name: str,
        schedule: CronSchedule,
        message: str,
        *,
        deliver: bool = False,
        channel: str | None = None,
        to: str | None = None,
        delete_after_run: bool = False,
    ) -> CronJob:
        store = self._load_store()
        _validate_schedule(schedule)

        now = _now_ms()
        job = CronJob(
            id=str(uuid.uuid4())[:8],
            name=name,
            enabled=True,
            schedule=schedule,
            payload=CronPayload(
                kind="agent_turn",
                message=message,
                deliver=deliver,
                channel=channel,
                to=to,
            ),
            state=CronJobState(next_run_at_ms=_compute_next_run(schedule, now)),
            created_at_ms=now,
            updated_at_ms=now,
            delete_after_run=delete_after_run,
        )
        store.jobs.append(job)
        self._save_store()
        self._arm_timer()
        logger.info("Cron: added job '%s' (%s)", name, job.id)
        return job

    def remove_job(self, job_id: str) -> bool:
        store = self._load_store()
        before = len(store.jobs)
        store.jobs = [j for j in store.jobs if j.id != job_id]
        if len(store.jobs) < before:
            self._save_store()
            self._arm_timer()
            logger.info("Cron: removed job %s", job_id)
            return True
        return False

    def enable_job(self, job_id: str, enabled: bool = True) -> CronJob | None:
        store = self._load_store()
        for job in store.jobs:
            if job.id == job_id:
                job.enabled = enabled
                job.updated_at_ms = _now_ms()
                if enabled:
                    job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())
                else:
                    job.state.next_run_at_ms = None
                self._save_store()
                self._arm_timer()
                return job
        return None

    async def run_job(self, job_id: str, *, force: bool = False) -> bool:
        store = self._load_store()
        for job in store.jobs:
            if job.id == job_id:
                if not force and not job.enabled:
                    return False
                await self._execute_job(job)
                self._save_store()
                self._arm_timer()
                return True
        return False

    def get_job(self, job_id: str) -> CronJob | None:
        store = self._load_store()
        return next((j for j in store.jobs if j.id == job_id), None)

    def status(self) -> dict:
        store = self._load_store()
        return {
            "enabled": self._running,
            "jobs": len(store.jobs),
            "next_wake_at_ms": self._get_next_wake_ms(),
        }


# ---------------------------------------------------------------------------
# JSON serialisation helpers
# ---------------------------------------------------------------------------

def _serialize_store(store: CronStore) -> dict:
    return {
        "version": store.version,
        "jobs": [
            {
                "id": j.id,
                "name": j.name,
                "enabled": j.enabled,
                "schedule": {
                    "kind": j.schedule.kind,
                    "atMs": j.schedule.at_ms,
                    "everyMs": j.schedule.every_ms,
                    "expr": j.schedule.expr,
                    "tz": j.schedule.tz,
                },
                "payload": {
                    "kind": j.payload.kind,
                    "message": j.payload.message,
                    "deliver": j.payload.deliver,
                    "channel": j.payload.channel,
                    "to": j.payload.to,
                },
                "state": {
                    "nextRunAtMs": j.state.next_run_at_ms,
                    "lastRunAtMs": j.state.last_run_at_ms,
                    "lastStatus": j.state.last_status,
                    "lastError": j.state.last_error,
                    "runHistory": [
                        {
                            "runAtMs": r.run_at_ms,
                            "status": r.status,
                            "durationMs": r.duration_ms,
                            "error": r.error,
                        }
                        for r in j.state.run_history
                    ],
                },
                "createdAtMs": j.created_at_ms,
                "updatedAtMs": j.updated_at_ms,
                "deleteAfterRun": j.delete_after_run,
            }
            for j in store.jobs
        ],
    }


def _deserialize_jobs(raw_jobs: list[dict]) -> list[CronJob]:
    jobs: list[CronJob] = []
    for j in raw_jobs:
        sched_raw = j.get("schedule", {})
        payload_raw = j.get("payload", {})
        state_raw = j.get("state", {})
        jobs.append(CronJob(
            id=j["id"],
            name=j["name"],
            enabled=j.get("enabled", True),
            schedule=CronSchedule(
                kind=sched_raw.get("kind", "every"),
                at_ms=sched_raw.get("atMs"),
                every_ms=sched_raw.get("everyMs"),
                expr=sched_raw.get("expr"),
                tz=sched_raw.get("tz"),
            ),
            payload=CronPayload(
                kind=payload_raw.get("kind", "agent_turn"),
                message=payload_raw.get("message", ""),
                deliver=payload_raw.get("deliver", False),
                channel=payload_raw.get("channel"),
                to=payload_raw.get("to"),
            ),
            state=CronJobState(
                next_run_at_ms=state_raw.get("nextRunAtMs"),
                last_run_at_ms=state_raw.get("lastRunAtMs"),
                last_status=state_raw.get("lastStatus"),
                last_error=state_raw.get("lastError"),
                run_history=[
                    CronRunRecord(
                        run_at_ms=r["runAtMs"],
                        status=r.get("status", "ok"),
                        duration_ms=r.get("durationMs", 0),
                        error=r.get("error"),
                    )
                    for r in state_raw.get("runHistory", [])
                ],
            ),
            created_at_ms=j.get("createdAtMs", 0),
            updated_at_ms=j.get("updatedAtMs", 0),
            delete_after_run=j.get("deleteAfterRun", False),
        ))
    return jobs
