"""TurnState state machine + degradation policy.

Each message processed by the system flows through 8 explicit states.
The StateManager enforces legal transitions and records anchors for
observability.  DegradationPolicy defines fallback behaviour for 6
fault-injection scenarios.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# TurnState
# ---------------------------------------------------------------------------

class TurnState(Enum):
    """The 8 states a message passes through in a single turn."""

    COMMAND = "command"    # Detect slash commands (/clear, /stop, /status)
    RESTORE = "restore"    # Check for interrupted checkpoints
    COMPACT = "compact"    # Compress history if token budget exceeded
    BUILD = "build"         # Assemble context (system prompt + memory + history)
    RUN = "run"             # LLM loop with tool execution
    SAVE = "save"           # Persist session
    RESPOND = "respond"     # Deliver reply to channel
    DONE = "done"           # Terminal — turn complete


# ---------------------------------------------------------------------------
# StateManager
# ---------------------------------------------------------------------------

# Event-driven transition table: (from_state, event) → to_state
# Handlers return events; the table owns all routing decisions.
_TRANSITIONS: dict[tuple[TurnState, str], TurnState] = {
    # COMMAND: slash-command? "shortcut" → skip to SAVE; else "dispatch" → normal flow
    (TurnState.COMMAND, "shortcut"): TurnState.SAVE,
    (TurnState.COMMAND, "dispatch"): TurnState.RESTORE,
    # RESTORE: "ok" (nothing or restored)
    (TurnState.RESTORE, "ok"): TurnState.COMPACT,
    # COMPACT: "ok" (skipped or compacted)
    (TurnState.COMPACT, "ok"): TurnState.BUILD,
    # BUILD: "ok"
    (TurnState.BUILD, "ok"): TurnState.RUN,
    # RUN: "ok" → SAVE; "error" → RESTORE (retry from checkpoint)
    (TurnState.RUN, "ok"): TurnState.SAVE,
    (TurnState.RUN, "error"): TurnState.RESTORE,
    # SAVE: "ok"
    (TurnState.SAVE, "ok"): TurnState.RESPOND,
    # RESPOND: "ok"
    (TurnState.RESPOND, "ok"): TurnState.DONE,
}


_START_STATE = TurnState.COMMAND


class StateError(Exception):
    """Raised when an unknown event is dispatched from the current state."""


@dataclass
class StateAnchor:
    """A timestamped record of entering a state."""
    state: TurnState
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class StateManager:
    """Event-driven TurnState machine.

    Usage pattern::

        sm = StateManager()
        while sm.current != TurnState.DONE:
            handler = handlers[sm.current]
            event = handler(ctx)         # handler returns "ok" / "error" / etc.
            sm.dispatch(event)           # table lookup → next state

    Handlers never call ``transition()`` — they just describe what
    happened.  The transition table owns all routing.
    """

    def __init__(self) -> None:
        self._current: TurnState = _START_STATE
        self._anchors: list[StateAnchor] = []

    # -- properties ---------------------------------------------------------

    @property
    def current(self) -> TurnState:
        return self._current

    @property
    def anchors(self) -> list[StateAnchor]:
        return list(self._anchors)

    @property
    def is_terminal(self) -> bool:
        return self._current == TurnState.DONE

    # -- event dispatch -----------------------------------------------------

    def dispatch(self, event: str) -> TurnState:
        """Look up (current, event) in the table and advance state.

        Returns the new state.  Raises StateError for unknown events.
        """
        key = (self._current, event)
        target = _TRANSITIONS.get(key)
        if target is None:
            raise StateError(
                f"No transition for ({self._current.value}, {event!r})"
            )
        self._current = target
        self._anchors.append(StateAnchor(state=target))
        return target

    # -- direct transition (escape hatch) -----------------------------------

    def jump(self, target: TurnState) -> None:
        """Directly jump to *target*, bypassing the event table.

        Use this only for exceptional cases (e.g. system shutdown).
        """
        self._current = target
        self._anchors.append(StateAnchor(state=target))

    # -- inspection ---------------------------------------------------------

    def possible_events(self) -> list[str]:
        """Return all events valid from the current state."""
        return [e for (s, e) in _TRANSITIONS if s == self._current]

    # -- anchor helpers -----------------------------------------------------

    def last_anchor(self, state: TurnState | None = None) -> StateAnchor | None:
        """Return the most recent anchor, optionally filtered by state."""
        for a in reversed(self._anchors):
            if state is None or a.state == state:
                return a
        return None

    def reset(self) -> None:
        """Reset to initial state (for a fresh turn / session)."""
        self._current = _START_STATE
        self._anchors.clear()


# ---------------------------------------------------------------------------
# DegradationPolicy
# ---------------------------------------------------------------------------

class DegradationAction(Enum):
    """Action to take when a component fails."""
    IGNORE = "ignore"            # Continue without the component
    RETRY = "retry"              # Retry with backoff
    READONLY = "readonly"        # Switch to read-only mode
    ALERT = "alert"              # Notify operator
    ABORT = "abort"              # Stop processing this turn


@dataclass
class DegradationDecision:
    action: DegradationAction
    message: str = ""
    recover_after_seconds: float | None = None


class DegradationPolicy:
    """Maps fault-injection scenarios to predefined degradation strategies.

    See :ref:`design-spec:degradation-matrix`.
    """

    _MATRIX: dict[str, DegradationDecision] = {
        "mcp_disconnect": DegradationDecision(
            DegradationAction.IGNORE,
            "MCP server unreachable — marking tools unavailable",
            recover_after_seconds=30,
        ),
        "memory_db_failure": DegradationDecision(
            DegradationAction.IGNORE,
            "Memory DB unavailable — using session-only mode",
        ),
        "channel_disconnect": DegradationDecision(
            DegradationAction.RETRY,
            "Channel disconnected — queuing messages and retrying",
            recover_after_seconds=30,
        ),
        "rate_limit": DegradationDecision(
            DegradationAction.RETRY,
            "Rate limit hit — slowing down",
        ),
        "disk_full": DegradationDecision(
            DegradationAction.READONLY,
            "Disk full — switching to read-only mode",
        ),
        "runner_exception": DegradationDecision(
            DegradationAction.RETRY,
            "Runner crashed — attempting checkpoint recovery",
        ),
    }

    @classmethod
    def handle(cls, component: str, error: Exception | None = None) -> DegradationDecision:
        """Look up the degradation action for *component*."""
        return cls._MATRIX.get(
            component,
            DegradationDecision(DegradationAction.ALERT, f"Unknown degradation: {component}"),
        )
