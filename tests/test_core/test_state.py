"""Tests for core/state.py — event-driven state machine and degradation policy."""

import pytest

from mxwbot.core.state import (
    DegradationAction,
    DegradationPolicy,
    StateError,
    StateManager,
    TurnState,
)


class TestTurnState:
    def test_all_states_present(self):
        assert len(TurnState) == 8
        assert TurnState.COMMAND.value == "command"
        assert TurnState.DONE.value == "done"


class TestStateManager:
    # -- normal flow --------------------------------------------------------

    def test_full_normal_flow(self):
        """Normal message: COMMAND → RESTORE → COMPACT → BUILD → RUN → SAVE → RESPOND → DONE."""
        sm = StateManager()
        steps = [
            ("dispatch", TurnState.RESTORE),
            ("ok", TurnState.COMPACT),
            ("ok", TurnState.BUILD),
            ("ok", TurnState.RUN),
            ("ok", TurnState.SAVE),
            ("ok", TurnState.RESPOND),
            ("ok", TurnState.DONE),
        ]
        for event, expected in steps:
            new = sm.dispatch(event)
            assert new == sm.current == expected

    def test_command_shortcut(self):
        """Slash command: COMMAND → SAVE → RESPOND → DONE (skips RESTORE..RUN)."""
        sm = StateManager()
        sm.dispatch("shortcut")       # COMMAND → SAVE
        assert sm.current == TurnState.SAVE
        sm.dispatch("ok")             # SAVE → RESPOND
        sm.dispatch("ok")             # RESPOND → DONE
        assert sm.current == TurnState.DONE

    # -- branches -----------------------------------------------------------

    def test_run_error_fallback(self):
        """RUN → "error" → RESTORE (checkpoint retry)."""
        sm = StateManager()
        # Reach RUN
        sm.dispatch("dispatch")
        sm.dispatch("ok")   # RESTORE → COMPACT
        sm.dispatch("ok")   # COMPACT → BUILD
        sm.dispatch("ok")   # BUILD → RUN
        assert sm.current == TurnState.RUN

        new = sm.dispatch("error")
        assert new == TurnState.RESTORE

    def test_unknown_event_raises(self):
        sm = StateManager()
        with pytest.raises(StateError, match="No transition"):
            sm.dispatch("bogus_event")

    def test_event_from_wrong_state_raises(self):
        """('ok') is valid from SAVE, but not from COMMAND."""
        sm = StateManager()
        with pytest.raises(StateError):
            sm.dispatch("ok")  # No (COMMAND, "ok")

    # -- terminal -----------------------------------------------------------

    def test_done_is_terminal(self):
        sm = StateManager()
        sm.dispatch("shortcut")   # → SAVE
        sm.dispatch("ok")         # → RESPOND
        sm.dispatch("ok")         # → DONE
        assert sm.is_terminal is True
        assert sm.possible_events() == []

    # -- inspection ---------------------------------------------------------

    def test_possible_events(self):
        sm = StateManager()
        # COMMAND has two events
        events = sm.possible_events()
        assert set(events) == {"shortcut", "dispatch"}

    # -- anchors ------------------------------------------------------------

    def test_anchors_recorded(self):
        sm = StateManager()
        sm.dispatch("dispatch")
        sm.dispatch("ok")
        assert len(sm.anchors) == 2
        assert sm.anchors[0].state == TurnState.RESTORE
        assert sm.anchors[1].state == TurnState.COMPACT

    def test_last_anchor_filtered(self):
        sm = StateManager()
        sm.dispatch("dispatch")
        sm.dispatch("ok")
        a = sm.last_anchor(TurnState.RESTORE)
        assert a is not None
        assert a.state == TurnState.RESTORE

    # -- direct jump (escape hatch) ----------------------------------------

    def test_jump_bypasses_table(self):
        sm = StateManager()
        sm.jump(TurnState.DONE)
        assert sm.is_terminal is True

    # -- reset -------------------------------------------------------------

    def test_reset(self):
        sm = StateManager()
        sm.dispatch("dispatch")
        sm.reset()
        assert sm.current == TurnState.COMMAND
        assert sm.anchors == []


class TestDegradationPolicy:
    def test_known_component(self):
        d = DegradationPolicy.handle("mcp_disconnect")
        assert d.action == DegradationAction.IGNORE

    def test_rate_limit(self):
        d = DegradationPolicy.handle("rate_limit")
        assert d.action == DegradationAction.RETRY

    def test_disk_full(self):
        d = DegradationPolicy.handle("disk_full")
        assert d.action == DegradationAction.READONLY

    def test_unknown_component(self):
        d = DegradationPolicy.handle("unknown_bug")
        assert d.action == DegradationAction.ALERT
