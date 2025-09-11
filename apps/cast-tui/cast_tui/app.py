from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol, Dict, List

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, FuzzyCompleter, NestedCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import CompleteStyle
from rich.console import Console


# ------------------------- core data structures -------------------------

@dataclass
class Command:
    """A command registered in the terminal."""
    name: str
    handler: Callable[["TerminalContext", List[str]], None]
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    completer: Optional[Completer] = None  # optional per-command completer


@dataclass
class TerminalContext:
    """Runtime context passed to command handlers."""
    console: Console
    app: "TerminalApp"
    state: dict = field(default_factory=dict)


class Plugin(Protocol):
    """Plugins register commands, keybindings, completers, toolbars, etc."""
    def register(self, ctx: TerminalContext) -> None: ...
    # Optional hooks (not required)
    def bottom_toolbar(self, ctx: TerminalContext) -> HTML | str: ...
    def prompt(self, ctx: TerminalContext) -> str: ...
    def default_command(self, ctx: TerminalContext) -> str: ...


# ------------------------- terminal app -------------------------

class TerminalApp:
    """
    A tiny, extensible terminal shell:
      • Fuzzy autocomplete via NestedCompleter + FuzzyCompleter
      • Commands with per-command completers
      • Keybindings injection
      • Bottom toolbar aggregation
      • Default command fallback (e.g., treat free text as `open <text>`)
    """

    def __init__(self):
        self.console = Console()
        self._commands: dict[str, Command] = {}
        self._name_alias: dict[str, str] = {}
        self._bottom_providers: list[Callable[[TerminalContext], HTML | str]] = []
        self._prompt_provider: Optional[Callable[[TerminalContext], str]] = None
        self._default_cmd_name: Optional[str] = None
        self._kb = KeyBindings()
        self._plugins: list[Plugin] = []
        self._session: Optional[PromptSession] = None
        self._ctx = TerminalContext(console=self.console, app=self)
        self._rebuild_needed = True
        self._completer: Optional[FuzzyCompleter] = None

        # Built-in commands
        self.register_command(Command(
            name="help",
            description="Show help for commands",
            aliases=["?"],
            handler=self._cmd_help,
        ))
        self.register_command(Command(
            name="quit",
            description="Exit the TUI",
            aliases=["exit"],
            handler=self._cmd_quit,
        ))

    # ---------- registration API ----------

    def register_plugin(self, plugin: Plugin) -> None:
        self._plugins.append(plugin)

    def register_command(self, cmd: Command) -> None:
        self._commands[cmd.name] = cmd
        for a in cmd.aliases:
            self._name_alias[a] = cmd.name
        self._rebuild_needed = True

    def add_bottom_toolbar(self, provider: Callable[[TerminalContext], HTML | str]) -> None:
        self._bottom_providers.append(provider)

    def set_prompt(self, provider: Callable[[TerminalContext], str]) -> None:
        self._prompt_provider = provider

    def set_default_command(self, name: str) -> None:
        self._default_cmd_name = name

    def add_keybinding(self, key_str: str, func: Callable) -> None:
        # key_str is prompt_toolkit key expression, e.g., "c-r"
        self._kb.add(key_str)(func)

    # ---------- internal helpers ----------

    def _resolve(self, name: str) -> Optional[Command]:
        if name in self._commands:
            return self._commands[name]
        if name in self._name_alias:
            return self._commands[self._name_alias[name]]
        return None

    def _build_completer(self) -> FuzzyCompleter:
        """
        Build a NestedCompleter mapping command names/aliases to their per-command completers.
        If a command has no completer, we map it to None (which NestedCompleter accepts).
        """
        mapping: Dict[str, Completer | None] = {}
        for name, cmd in self._commands.items():
            mapping[name] = cmd.completer
            # include aliases
            for a in cmd.aliases:
                mapping[a] = cmd.completer
        base = NestedCompleter(mapping)
        return FuzzyCompleter(base)

    def _ensure_completer(self) -> None:
        if self._rebuild_needed or self._completer is None:
            self._completer = self._build_completer()
            self._rebuild_needed = False

    # ---------- built-in commands ----------

    def _cmd_help(self, ctx: TerminalContext, _args: List[str]) -> None:
        self.console.rule("[bold]Commands[/bold]")
        rows = []
        for name in sorted(self._commands.keys()):
            cmd = self._commands[name]
            alias_str = f" (aliases: {', '.join(cmd.aliases)})" if cmd.aliases else ""
            desc = cmd.description or ""
            rows.append(f"[bold]{name}[/bold]{alias_str}  {desc}")
        self.console.print("\n".join(rows))

    def _cmd_quit(self, ctx: TerminalContext, _args: List[str]) -> None:
        raise EOFError

    # ---------- UI providers ----------

    def _bottom_toolbar_html(self) -> HTML:
        parts: list[str] = []
        for provider in self._bottom_providers:
            try:
                v = provider(self._ctx)
                if isinstance(v, str):
                    parts.append(v)
                elif isinstance(v, HTML):
                    parts.append(v.value)  # Extract the HTML string value
                else:
                    parts.append(str(v))
            except Exception:
                # ignore provider errors; keep shell usable
                continue
        joined = " • ".join(p for p in parts if p)
        if not joined:
            joined = "Type 'help' for commands • 'quit' to exit"
        return HTML(joined)

    def _prompt_text(self) -> str:
        if self._prompt_provider:
            try:
                return self._prompt_provider(self._ctx)
            except Exception:
                pass
        return "tui> "

    # ---------- event loop ----------

    def run(self) -> None:
        """Run the interactive loop once all plugins are registered."""
        # Let plugins register themselves
        for p in self._plugins:
            try:
                # Optional: allow plugins to set prompt/default/bottom toolbar
                if hasattr(p, "register"):
                    p.register(self._ctx)
                if hasattr(p, "bottom_toolbar"):
                    self.add_bottom_toolbar(lambda ctx, _p=p: _p.bottom_toolbar(ctx))  # type: ignore
                if hasattr(p, "prompt"):
                    self.set_prompt(lambda ctx, _p=p: _p.prompt(ctx))  # type: ignore
                if hasattr(p, "default_command"):
                    self.set_default_command(p.default_command(self._ctx))  # type: ignore
            except Exception as e:
                self.console.print(f"[red]Plugin init failed:[/red] {e}")

        self._ensure_completer()
        self._session = PromptSession(history=InMemoryHistory())

        while True:
            try:
                text = self._session.prompt(
                    self._prompt_text(),
                    completer=self._completer,
                    complete_style=CompleteStyle.MULTI_COLUMN,
                    key_bindings=self._kb,
                    bottom_toolbar=lambda: self._bottom_toolbar_html(),
                ).strip()
            except (EOFError, KeyboardInterrupt):
                self.console.print()
                break

            if not text:
                continue

            parts = shlex.split(text)
            cmd_name = parts[0]
            args = parts[1:]

            cmd = self._resolve(cmd_name)
            if not cmd and self._default_cmd_name:
                # Treat input as default command's args
                cmd = self._commands.get(self._default_cmd_name)
                args = [cmd_name] + args if cmd else args

            if not cmd:
                self.console.print(f"[red]Unknown command:[/red] {cmd_name}")
                continue

            try:
                cmd.handler(self._ctx, args)
            except EOFError:
                # graceful shutdown
                break
            except Exception as e:
                self.console.print(f"[red]Error:[/red] {e}")