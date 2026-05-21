"""Management HTTP API — embedded server for CLI admin commands.

Listens only on 127.0.0.1 so it is not reachable from the network.
All state mutations are dispatched via ``run_coroutine_threadsafe``
to keep the asyncio event loop single-threaded.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("mxwbot.system.api")


class ManagementAPI:
    """Embedded HTTP server for administrative operations.

    Args:
        manager: SystemManager instance (must be fully bootstrapped).
        host: Bind address (default 127.0.0.1).
        port: Bind port (default 9090).
    """

    def __init__(self, manager: Any, host: str = "127.0.0.1", port: int = 9090) -> None:
        self._manager = manager
        self._loop = asyncio.get_running_loop()

        # Bind the handler class to the manager instance
        mgr = manager
        loop = self._loop

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                logger.debug("API: %s", format % args)

            def _json(self, data: dict, status: int = 200) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

            def _read_body(self) -> dict:
                length = int(self.headers.get("Content-Length", 0))
                if not length:
                    return {}
                return json.loads(self.rfile.read(length))

            def _run_async(self, coro) -> Any:
                return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=30)

            # -- routing -------------------------------------------------
            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/api/channel/list":
                    comps = [c for c in mgr.list_components() if "channel" in c.name]
                    self._json({"channels": [
                        {"name": c.name, "state": c.state.value} for c in comps
                    ]})
                elif path == "/api/heartbeat/status":
                    self._json(mgr.heartbeat.status() if mgr.heartbeat else {})
                elif path == "/api/cron/list":
                    jobs = mgr.cron_service.list_jobs(include_disabled=True) if mgr.cron_service else []
                    self._json({"jobs": [
                        {"id": j.id, "name": j.name, "enabled": j.enabled,
                         "next_run_at_ms": j.state.next_run_at_ms,
                         "last_status": j.state.last_status}
                        for j in jobs
                    ]})
                elif path == "/api/cron/status":
                    self._json(mgr.cron_service.status() if mgr.cron_service else {})
                elif path.startswith("/api/cron/") and not path.endswith("/list") and not path.endswith("/status"):
                    job_id = path.rsplit("/", 1)[-1]
                    job = mgr.cron_service.get_job(job_id) if mgr.cron_service else None
                    if job:
                        self._json({"id": job.id, "name": job.name, "enabled": job.enabled,
                                     "schedule": job.schedule.kind, "next_run_at_ms": job.state.next_run_at_ms,
                                     "payload": job.payload.message})
                    else:
                        self._json({"error": "not found"}, 404)
                elif path == "/api/status":
                    self._json({
                        "loop_pool": mgr.is_running("loop_pool"),
                        "components": {c.name: c.state.value for c in mgr.list_components()},
                    })
                else:
                    self._json({"error": "not found"}, 404)

            def do_POST(self):
                path = urlparse(self.path).path
                qs = parse_qs(urlparse(self.path).query)

                if path == "/api/channel/start":
                    name = qs.get("name", [""])[0]
                    ok = self._run_async(mgr.start_component(f"{name}_channel"))
                    self._json({"status": "ok" if ok else "error"})
                elif path == "/api/channel/stop":
                    name = qs.get("name", [""])[0]
                    self._run_async(mgr.stop_component(f"{name}_channel"))
                    self._json({"status": "ok"})
                elif path == "/api/heartbeat/start":
                    self._run_async(mgr.start_component("heartbeat"))
                    self._json({"status": "ok"})
                elif path == "/api/heartbeat/stop":
                    self._run_async(mgr.stop_component("heartbeat"))
                    self._json({"status": "ok"})
                elif path == "/api/heartbeat/run":
                    result = self._run_async(mgr.heartbeat.trigger_now() if mgr.heartbeat else None)
                    self._json({"status": "ok", "tasks": result})
                elif path == "/api/cron/add":
                    body = self._read_body()
                    sched = body.get("schedule", {})
                    from mxwbot.cron.types import CronSchedule
                    job = mgr.cron_service.add_job(
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
                    ) if mgr.cron_service else None
                    self._json({"id": job.id, "next_run": job.state.next_run_at_ms}) if job else self._json({"error": "no cron service"}, 500)
                elif path.startswith("/api/cron/") and path.endswith("/enable"):
                    job_id = path.rsplit("/", 2)[-2]
                    body = self._read_body()
                    ok = mgr.cron_service.enable_job(job_id, body.get("enabled", True)) if mgr.cron_service else None
                    self._json({"status": "ok" if ok else "not found"})
                else:
                    self._json({"error": "not found"}, 404)

            def do_DELETE(self):
                path = urlparse(self.path).path
                if path.startswith("/api/cron/"):
                    job_id = path.rsplit("/", 1)[-1]
                    ok = mgr.cron_service.remove_job(job_id) if mgr.cron_service else False
                    self._json({"status": "ok" if ok else "not found"})

        self._server = HTTPServer((host, port), _Handler)

    def start(self) -> None:
        """Start the HTTP server in a daemon thread."""
        t = threading.Thread(target=self._server.serve_forever, daemon=True)
        t.start()
        logger.info("Management API listening on %s:%d", *self._server.server_address)

    def stop(self) -> None:
        self._server.shutdown()
