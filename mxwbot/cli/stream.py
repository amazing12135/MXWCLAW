"""Stream renderer — real-time token-by-token display on the terminal.

Adapted from nanobot's StreamRenderer for mxwbot's Rich console.
"""

from __future__ import annotations

from contextlib import contextmanager

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text


class StreamRenderer:
    """Render LLM stream tokens in real time on a Rich console.

    Args:
        render_markdown: If True, the final output is rendered as
            Rich Markdown.  Individual tokens are always printed as
            plain text to avoid flicker.
    """

    def __init__(self, render_markdown: bool = True) -> None:
        self._console = Console()
        self._accumulated: list[str] = []
        self._render_markdown = render_markdown
        self.streamed = False  # True if at least one token was received
        self._closed = False

    # ------------------------------------------------------------------
    # Public callbacks (passed to process_direct / Loop)
    # ------------------------------------------------------------------

    async def on_delta(self, token: str) -> None:
        """Called for each LLM stream token — prints immediately."""
        if self._closed:
            return
        self._accumulated.append(token)
        self._console.print(token, end="")
        self.streamed = True

    async def on_end(self, *, resuming: bool = False) -> None:
        """Called when the stream finishes (or resumes after error)."""
        if self._closed:
            return
        if resuming:
            self._console.print("\n[dim]...resuming...[/dim]")
        self._console.print()  # trailing newline

    async def close(self) -> None:
        """Finalise — render the accumulated content as Markdown if enabled."""
        if self._closed:
            return
        self._closed = True

        if self._render_markdown and self._accumulated:
            full = "".join(self._accumulated)
            self._console.print(Markdown(full))

    # ------------------------------------------------------------------
    # Sync helpers (for prompt_toolkit integration)
    # ------------------------------------------------------------------

    def on_delta_sync(self, token: str) -> None:
        """Synchronous version of on_delta."""
        if self._closed:
            return
        self._accumulated.append(token)
        self._console.print(token, end="")
        self.streamed = True

    def on_end_sync(self, resuming: bool = False) -> None:
        """Synchronous version of on_end."""
        if self._closed:
            return
        if resuming:
            self._console.print("\n[dim]...resuming...[/dim]")
        self._console.print()


class ThinkingSpinner:
    """A lightweight spinner indicating the agent is working.

    Usage::

        spinner = ThinkingSpinner()
        with spinner.pause():
            print("progress line")

    This is a simplified placeholder — a full animated spinner can
    be added in Phase 10.
    """

    def __init__(self) -> None:
        self._active = False

    @contextmanager
    def pause(self):
        """Context manager that temporarily stops the spinner."""
        prev = self._active
        self._active = False
        try:
            yield
        finally:
            self._active = prev
