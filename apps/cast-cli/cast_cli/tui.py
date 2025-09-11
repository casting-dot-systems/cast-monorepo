"""Cast TUI entrypoint: hooks the Cast plugin into the generic cast-tui framework.

Usage:
  cast tui
"""

from __future__ import annotations

import typer
from rich.console import Console

from cast_tui import TerminalApp
from cast_cli.tui_plugin import CastTUIPlugin

tui_app = typer.Typer(help="Interactive terminal for Cast (via cast-tui framework)")

console = Console()


@tui_app.callback(invoke_without_command=True)
def tui(_ctx: typer.Context) -> None:
    app = TerminalApp()
    # Register the Cast plugin (file search, preview, edit, sync, report, peers)
    app.register_plugin(CastTUIPlugin())
    app.run()