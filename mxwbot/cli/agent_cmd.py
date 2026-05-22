"""agent command — interactive REPL and single-shot agent chat.

Two modes:
  1. ``mxwbot agent -m "hi"`` → single turn, prints reply, exits
  2. ``mxwbot agent``         → interactive REPL with prompt_toolkit
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

import typer

from mxwbot.config.loader import load_config
from mxwbot.system.manager import SystemManager

logger = logging.getLogger("mxwbot.cli.agent")

_EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}


async def _run_single(
    manager: SystemManager,
    message: str,
    session_id: str,
    *,
    markdown: bool,
) -> None:
    """Single-turn: send one message and print the reply."""
    from mxwbot.cli.stream import StreamRenderer

    renderer = StreamRenderer(render_markdown=markdown)

    result = await manager.process_direct(
        message,
        session_id,
        on_stream=renderer.on_delta,
    )

    if not renderer.streamed:
        # Non-streaming response — render now
        await renderer.close()
        from rich.console import Console
        Console().print(result.content or "(no response)")

    logger.info("Agent turn completed: %s", result.finish_reason)


async def _run_interactive(
    manager: SystemManager,
    session_id: str,
    *,
    markdown: bool,
) -> None:
    """Interactive REPL loop with prompt_toolkit."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.patch_stdout import patch_stdout

    from mxwbot.cli.stream import StreamRenderer
    from mxwbot.config.path import get_session_path  # reuse path logic

    # History file
    history_path = Path(".mxwbot") / "cli_history"
    history_path.parent.mkdir(parents=True, exist_ok=True)

    session_prompt = PromptSession(
        history=FileHistory(str(history_path)),
        enable_open_in_editor=False,
        multiline=False,
    )

    from rich.console import Console
    console = Console()
    console.print("MXWbot interactive mode (type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit)\n")

    while True:
        try:
            with patch_stdout():
                line = await session_prompt.prompt_async(
                    HTML("<b fg='ansicyan'>You:</b> "),
                )
        except (EOFError, KeyboardInterrupt):
            console.print("\nGoodbye!")
            break

        line = line.strip()
        if not line:
            continue
        if line.lower() in _EXIT_COMMANDS:
            console.print("Goodbye!")
            break

        renderer = StreamRenderer(render_markdown=markdown)
        try:
            result = await manager.process_direct(
                line, session_id,
                on_stream=renderer.on_delta,
            )
        except KeyboardInterrupt:
            console.print("\n[yellow]Cancelled.[/yellow]")
            continue
        except Exception as exc:
            console.print(f"\n[red]Error: {exc}[/red]")
            continue

        if not renderer.streamed and result:
            await renderer.close()
            console.print(f"[cyan]MXWbot[/cyan]")
            console.print(result.content or "(no response)")
        console.print()  # blank line between turns


# ---------------------------------------------------------------------------
# Typer command
# ---------------------------------------------------------------------------


async def agent_cmd(
    message: str | None = None,
    session_id: str = "cli:direct",
    config_path: str | None = None,
    markdown: bool = True,
    verbose: bool = False,
) -> None:
    """Core agent logic, shared by the Typer command."""
    if verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING)

    # Load config (use defaults if no config file exists)
    try:
        cfg = load_config(Path(config_path) if config_path else None)
    except Exception:
        from mxwbot.config.schema import MXWConfig
        cfg = MXWConfig()
        logger.info("No config found, using defaults")

    manager = SystemManager(cfg)
    await manager.bootstrap()

    try:
        if message:
            await _run_single(manager, message, session_id, markdown=markdown)
        else:
            await _run_interactive(manager, session_id, markdown=markdown)
    finally:
        await manager.shutdown()
