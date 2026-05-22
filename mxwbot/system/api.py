"""Management HTTP API — aiohttp-based admin server.

Listens only on 127.0.0.1.  All handlers are plain async functions
running directly in the main event loop — no threads, no blocking.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

logger = logging.getLogger("mxwbot.system.api")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _require_fields(data: dict, *required: str) -> str | None:
    """Return error message if any required key is missing, else None."""
    missing = [k for k in required if k not in data]
    return f"Missing required fields: {', '.join(missing)}" if missing else None


def _check_fields(data: dict, path: str, *required: str) -> web.Response | None:
    """Return 400 JSON response if fields are missing, else None."""
    err = _require_fields(data, *required)
    if err:
        return web.json_response({"error": f"{path}: {err}"}, status=400)
    return None


# ---------------------------------------------------------------------------
# ManagementAPI
# ---------------------------------------------------------------------------


class ManagementAPI:
    """Async HTTP admin server built on aiohttp.

    Args:
        manager: ``SystemManager`` instance (must be fully bootstrapped).
        host: Bind address (default 127.0.0.1).
        port: Bind port (default 9090).
    """

    def __init__(self, manager: Any, host: str = "127.0.0.1", port: int = 9090) -> None:
        self._manager = manager
        self._host = host
        self._port = port
        self._app = web.Application()
        self._runner: web.AppRunner | None = None
        self._setup_routes()

    # ------------------------------------------------------------------
    # Route registration
    # ------------------------------------------------------------------

    def _setup_routes(self) -> None:
        self._app.add_routes([
            # -- channel --
            web.get("/api/channel/list", self._handle_channel_list),
            web.post("/api/channel/start", self._handle_channel_start),
            web.post("/api/channel/stop", self._handle_channel_stop),
            # -- heartbeat --
            web.post("/api/heartbeat/start", self._handle_heartbeat_start),
            web.post("/api/heartbeat/stop", self._handle_heartbeat_stop),
            web.post("/api/heartbeat/run", self._handle_heartbeat_run),
            web.get("/api/heartbeat/status", self._handle_heartbeat_status),
            # -- cron --
            web.get("/api/cron/list", self._handle_cron_list),
            web.get("/api/cron/status", self._handle_cron_status),
            web.post("/api/cron/add", self._handle_cron_add),
            web.delete("/api/cron/{job_id}", self._handle_cron_remove),
            web.post("/api/cron/{job_id}/enable", self._handle_cron_enable),
            web.get("/api/cron/{job_id}", self._handle_cron_get),
            # -- system --
            web.get("/api/status", self._handle_status),
        ])

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the aiohttp server."""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.info("Management API listening on %s:%d", self._host, self._port)

    async def stop(self) -> None:
        """Gracefully shutdown."""
        if self._runner:
            await self._runner.cleanup()

    # ------------------------------------------------------------------
    # Handlers: channel
    # ------------------------------------------------------------------

    async def _handle_channel_list(self, _request: web.Request) -> web.Response:
        mgr = self._manager
        comps = [c for c in mgr.list_components() if "channel" in c.name]
        return web.json_response({
            "channels": [{"name": c.name, "state": c.state.value} for c in comps],
        })

    async def _handle_channel_start(self, request: web.Request) -> web.Response:
        name = request.query.get("name", "")
        if not name:
            return web.json_response({"error": "query param 'name' is required"}, status=400)
        ok = await self._manager.start_component(f"{name}_channel")
        return web.json_response({"status": "ok" if ok else "error"})

    async def _handle_channel_stop(self, request: web.Request) -> web.Response:
        name = request.query.get("name", "")
        if not name:
            return web.json_response({"error": "query param 'name' is required"}, status=400)
        await self._manager.stop_component(f"{name}_channel")
        return web.json_response({"status": "ok"})

    # ------------------------------------------------------------------
    # Handlers: heartbeat
    # ------------------------------------------------------------------

    async def _handle_heartbeat_start(self, _request: web.Request) -> web.Response:
        await self._manager.start_component("heartbeat")
        return web.json_response({"status": "ok"})

    async def _handle_heartbeat_stop(self, _request: web.Request) -> web.Response:
        await self._manager.stop_component("heartbeat")
        return web.json_response({"status": "ok"})

    async def _handle_heartbeat_run(self, _request: web.Request) -> web.Response:
        hb = getattr(self._manager, "heartbeat", None)
        result = await hb.trigger_now() if hb else None
        return web.json_response({"status": "ok", "tasks": result})

    async def _handle_heartbeat_status(self, _request: web.Request) -> web.Response:
        hb = getattr(self._manager, "heartbeat", None)
        return web.json_response(hb.status() if hb else {})

    # ------------------------------------------------------------------
    # Handlers: cron
    # ------------------------------------------------------------------

    async def _handle_cron_list(self, _request: web.Request) -> web.Response:
        cs = self._manager.cron_service
        jobs = cs.list_jobs(include_disabled=True) if cs else []
        return web.json_response({
            "jobs": [
                {
                    "id": j.id, "name": j.name, "enabled": j.enabled,
                    "next_run_at_ms": j.state.next_run_at_ms,
                    "last_status": j.state.last_status,
                }
                for j in jobs
            ],
        })

    async def _handle_cron_status(self, _request: web.Request) -> web.Response:
        cs = self._manager.cron_service
        return web.json_response(cs.status() if cs else {})

    async def _handle_cron_add(self, request: web.Request) -> web.Response:
        body = await request.json()
        resp = _check_fields(body, "body", "name", "message", "schedule")
        if resp: return resp

        sched = body["schedule"]
        resp = _check_fields(sched, "body.schedule", "kind")
        if resp: return resp

        from mxwbot.cron.types import CronSchedule
        cs = self._manager.cron_service
        if not cs:
            return web.json_response({"error": "no cron service"}, status=500)

        job = cs.add_job(
            name=body["name"],
            schedule=CronSchedule(
                kind=sched["kind"],
                at_ms=sched.get("at_ms"),
                every_ms=sched.get("every_ms"),
                expr=sched.get("expr"),
                tz=sched.get("tz"),
            ),
            message=body.get("message", ""),
            deliver=body.get("deliver", False),
            channel=body.get("channel"),
            delete_after_run=body.get("delete_after_run", False),
        )
        return web.json_response({"id": job.id, "next_run": job.state.next_run_at_ms})

    async def _handle_cron_remove(self, request: web.Request) -> web.Response:
        job_id = request.match_info["job_id"]
        cs = self._manager.cron_service
        ok = cs.remove_job(job_id) if cs else False
        return web.json_response({"status": "ok" if ok else "not found"})

    async def _handle_cron_enable(self, request: web.Request) -> web.Response:
        job_id = request.match_info["job_id"]
        body = await request.json()
        resp = _check_fields(body, "body", "enabled")
        if resp: return resp

        cs = self._manager.cron_service
        ok = cs.enable_job(job_id, body["enabled"]) if cs else None
        return web.json_response({"status": "ok" if ok else "not found"})

    async def _handle_cron_get(self, request: web.Request) -> web.Response:
        job_id = request.match_info["job_id"]
        cs = self._manager.cron_service
        job = cs.get_job(job_id) if cs else None
        if job:
            return web.json_response({
                "id": job.id, "name": job.name, "enabled": job.enabled,
                "schedule": job.schedule.kind,
                "next_run_at_ms": job.state.next_run_at_ms,
                "payload": job.payload.message,
            })
        return web.json_response({"error": "not found"}, status=404)

    # ------------------------------------------------------------------
    # Handlers: system
    # ------------------------------------------------------------------

    async def _handle_status(self, _request: web.Request) -> web.Response:
        mgr = self._manager
        return web.json_response({
            "loop_pool": mgr.is_running("loop_pool"),
            "components": {c.name: c.state.value for c in mgr.list_components()},
        })
