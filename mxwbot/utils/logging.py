"""Structured JSONL logging for MXWbot.

Produces one JSON object per line to stderr or a file, making logs easy to
filter with tools like jq.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO


class JsonlFormatter(logging.Formatter):
    """Emit log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "module": record.name,
        }
        if record.msg:
            payload["msg"] = record.getMessage()
        if record.exc_info and record.exc_info[1]:
            payload["error"] = str(record.exc_info[1])

        # Optional: merge extra context passed via `extra=`
        extra: dict[str, object] = {}
        for key in ("session_id", "channel", "chat_id", "latency_ms", "tool"):
            val = getattr(record, key, None)
            if val is not None:
                extra[key] = val
        if extra:
            payload["ctx"] = extra

        return json.dumps(payload, ensure_ascii=False, default=str)


class JsonlLogger:
    """Structured JSONL logger wrapper.

    Usage::

        log = JsonlLogger("mxwbot.core")
        log.info("Loop started", extra={"session_id": "abc"})
        log.error("Runner crashed", exc_info=True)
    """

    def __init__(
        self,
        name: str,
        level: int = logging.INFO,
        output: TextIO | Path | None = None,
    ) -> None:
        self._logger = logging.getLogger(name)
        self._logger.setLevel(level)
        self._logger.propagate = False

        handler: logging.Handler
        if output is None:
            handler = logging.StreamHandler(sys.stderr)
        elif isinstance(output, Path):
            handler = logging.FileHandler(str(output), encoding="utf-8")
        else:
            handler = logging.StreamHandler(output)

        handler.setFormatter(JsonlFormatter())
        handler.setLevel(level)
        self._logger.handlers.clear()
        self._logger.addHandler(handler)

    def debug(self, msg: str, **extra: object) -> None:
        self._logger.debug(msg, extra=extra if extra else None)

    def info(self, msg: str, **extra: object) -> None:
        self._logger.info(msg, extra=extra if extra else None)

    def warning(self, msg: str, **extra: object) -> None:
        self._logger.warning(msg, extra=extra if extra else None)

    def error(self, msg: str, **extra: object) -> None:
        self._logger.error(msg, extra=extra if extra else None)


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    """Configure the root logger to use JSONL output.

    Args:
        level: One of DEBUG, INFO, WARNING, ERROR.
        log_file: Optional file path for log output (default: stderr).
    """
    handler: logging.Handler
    if log_file:
        handler = logging.FileHandler(str(log_file), encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stderr)

    handler.setFormatter(JsonlFormatter())
    handler.setLevel(getattr(logging, level.upper(), logging.INFO))

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(handler.level)
    root.addHandler(handler)
