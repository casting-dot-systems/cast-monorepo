"""cast-tui: a small, extensible terminal shell with fuzzy autocomplete."""

from .app import (
    TerminalApp,
    TerminalContext,
    Command,
    Plugin,
)

__all__ = [
    "TerminalApp",
    "TerminalContext",
    "Command",
    "Plugin",
]

__version__ = "0.1.0"