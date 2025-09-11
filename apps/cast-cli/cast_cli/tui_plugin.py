"""Cast-specific plugin for the generic cast-tui framework."""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from ruamel.yaml import YAML

from prompt_toolkit.completion import Completer, Completion, FuzzyCompleter, NestedCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings

from cast_tui import TerminalContext, Command, Plugin
from cast_sync import build_ephemeral_index, HorizontalSync
from cast_sync import CodebaseSync
from cast_core.registry import list_codebases
from cast_core.yamlio import parse_cast_file


_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.default_flow_style = False
_yaml.width = 4096


# -------------------- Cast state & helpers --------------------

def _find_cast_root() -> Path:
    cur = Path.cwd()
    if (cur / ".cast").exists():
        return cur
    for p in cur.parents:
        if (p / ".cast").exists():
            return p
    raise RuntimeError("Not in a Cast root (no .cast/ found)")


def _read_config(root: Path) -> tuple[str, Path]:
    cfg = root / ".cast" / "config.yaml"
    if not cfg.exists():
        raise RuntimeError(".cast/config.yaml missing")
    with open(cfg, encoding="utf-8") as f:
        data = _yaml.load(f) or {}
    cast_name = data.get("cast-name", "")
    vault = root / "Cast"
    if not vault.exists():
        raise RuntimeError(f"Cast folder not found at {vault}")
    return cast_name, vault


@dataclass
class FileItem:
    cast_id: str | None
    relpath: str
    title: str | None


class CastContext:
    """In-memory view of the Cast folder and ephemeral index."""
    def __init__(self, root: Path, vault: Path, cast_name: str):
        self.root = root
        self.vault = vault
        self.cast_name = cast_name
        self.items: list[FileItem] = []
        self._by_id: dict[str, FileItem] = {}
        self._by_path: dict[str, FileItem] = {}

    def reindex(self) -> None:
        idx = build_ephemeral_index(self.root, self.vault, fixup=False)
        items: list[FileItem] = []
        cast_paths = set()

        for rec in idx.by_id.values():
            p = self.vault / rec["relpath"]
            cast_paths.add(rec["relpath"])
            title = None
            try:
                fm, _body, has = parse_cast_file(p)
                if has and isinstance(fm, dict):
                    title = fm.get("title") or fm.get("name")
            except Exception:
                pass
            items.append(FileItem(cast_id=rec["cast_id"], relpath=rec["relpath"], title=title))

        # include non-cast files for convenience
        try:
            for file_path in self.vault.rglob("*"):
                if file_path.is_file():
                    relpath = str(file_path.relative_to(self.vault))
                    if relpath in cast_paths:
                        continue
                    title = None
                    if file_path.suffix.lower() in {".md", ".txt"}:
                        try:
                            with open(file_path, "r", encoding="utf-8") as f:
                                first_line = f.readline().strip()
                                if first_line.startswith("# "):
                                    title = first_line[2:].strip()
                        except Exception:
                            pass
                    items.append(FileItem(cast_id=None, relpath=relpath, title=title))
        except Exception:
            pass

        items.sort(key=lambda it: it.relpath.lower())
        self.items = items
        self._by_id = {it.cast_id: it for it in items if it.cast_id}
        self._by_path = {it.relpath: it for it in items}

    def resolve(self, token: str) -> Optional[FileItem]:
        if not token:
            return None
        it = self._by_id.get(token)
        if it:
            return it
        p = Path(token)
        if p.parts and p.parts[0] == self.vault.name:
            token = str(Path(*p.parts[1:]))
        return self._by_path.get(token)


class CastFileCompleter(Completer):
    """Dynamic file completer over CastContext; FuzzyCompleter wraps this via the framework."""

    @staticmethod
    def _needs_quoting(s: str) -> bool:
        if not s:
            return True
        specials = set(' \t\r\n"\'\\&|;<>*?()[]{}')
        return any((c in specials) or c.isspace() for c in s)

    @staticmethod
    def _dq(s: str) -> str:
        s = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{s}"'

    @staticmethod
    def _current_arg_token_and_len(document) -> tuple[str, int]:
        buf = document.text_before_cursor
        i = max(buf.rfind(" "), buf.rfind("\t"))
        start = i + 1
        token = buf[start:]
        return token, len(token)

    def __init__(self, ctx: CastContext):
        self.ctx = ctx

    def get_completions(self, document, _event):
        token, token_len = self._current_arg_token_and_len(document)
        for it in self.ctx.items:
            subtitle = f" — {it.title}" if it.title else ""
            if it.cast_id:
                disp = f"{it.relpath}{subtitle} · {it.cast_id[:8]}…"
            else:
                disp = f"{it.relpath}{subtitle}"
            insert = it.relpath
            if self._needs_quoting(insert):
                insert = self._dq(insert)
            yield Completion(insert, start_position=-token_len, display=disp)


# -------------------- commands & helpers --------------------

def _preview_file(console: Console, vault: Path, it: FileItem) -> None:
    path = vault / it.relpath
    if not path.exists():
        console.print(f"[red]Not found:[/red] {path}")
        return

    tab = Table.grid(expand=True)
    tab.add_row(Text(str(path), style="bold"))
    if it.cast_id:
        tab.add_row(Text(f"cast-id: {it.cast_id}", style="dim"))
    if it.title:
        tab.add_row(Text(f"title: {it.title}"))
    console.print(tab)

    fm, body, has_cast_yaml = parse_cast_file(path)
    if has_cast_yaml and isinstance(fm, dict):
        sub = {k: fm.get(k) for k in ("last-updated", "cast-id", "cast-version", "url", "title") if k in fm}
        if sub:
            try:
                from io import StringIO
                buf = StringIO()
                y = YAML()
                y.preserve_quotes = True
                y.default_flow_style = False
                y.width = 120
                y.dump(sub, buf)
                console.print(Panel.fit(buf.getvalue().rstrip("\n"), title="YAML (subset)", border_style="cyan"))
            except Exception:
                pass
        snippet = "\n".join((body or "").splitlines()[:60]) or "_(empty)_"
        console.print(Panel(Markdown(snippet), title="Body (first 60 lines)", expand=True))
    else:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            snippet = "\n".join(content.splitlines()[:60]) or "_(empty)_"
            if path.suffix.lower() == ".md":
                console.print(Panel(Markdown(snippet), title="Content (first 60 lines)", expand=True))
            else:
                console.print(Panel(snippet, title="Content (first 60 lines)", expand=True))
        except Exception as e:
            console.print(f"[red]Error reading file:[/red] {e}")


def _sync(console: Console, cctx: CastContext, file_token: Optional[str] = None, non_interactive: bool = True) -> int:
    hs = HorizontalSync(cctx.root)
    filt = None
    if file_token:
        it = cctx.resolve(file_token)
        if it and it.cast_id:
            filt = it.cast_id
        elif it and not it.cast_id:
            console.print(f"[yellow]Warning:[/yellow] '{file_token}' is not a Cast file (no cast-id). Syncing all files instead.")
        else:
            filt = file_token
    code = hs.sync(
        file_filter=filt,
        non_interactive=non_interactive,
        cascade=False,
        dry_run=False,
        debug=False,
    )
    if hs.summary:
        c = hs.summary.counts
        pulls = c.get("pull", 0)
        pushes = c.get("push", 0)
        created = c.get("create_peer", 0) + c.get("create_local", 0)
        deletes = c.get("delete_local", 0) + c.get("delete_peer", 0)
        renames = c.get("rename_local", 0) + c.get("rename_peer", 0)
        conflicts_open = hs.summary.conflicts_open
        conflicts_resolved = hs.summary.conflicts_resolved
        console.rule("[bold]Sync Summary[/bold]")
        console.print(
            f"⬇️ pulls: [bold]{pulls}[/bold]   ⬆️ pushes: [bold]{pushes}[/bold]   "
            f"➕ created: [bold]{created}[/bold]   ✂️ deletions: [bold]{deletes}[/bold]   "
            f"🔁 renames: [bold]{renames}[/bold]   ⚠️ open: [bold]{conflicts_open}[/bold]   ✔️ resolved: [bold]{conflicts_resolved}[/bold]"
        )
    cctx.reindex()
    return code


# -------------------- plugin implementation --------------------

class CastTUIPlugin(Plugin):
    """Registers Cast commands + completers into the generic framework."""

    def __init__(self) -> None:
        self.console = Console()
        self._cast: Optional[CastContext] = None
        self._file_completer: Optional[Completer] = None

    # ----- Plugin interface -----

    def register(self, ctx: TerminalContext) -> None:
        root = _find_cast_root()
        cast_name, vault = _read_config(root)
        self._cast = CastContext(root, vault, cast_name)
        self._cast.reindex()
        self._file_completer = CastFileCompleter(self._cast)

        # Commands
        ctx.app.register_command(Command(
            name="open",
            aliases=["view"],
            description="Preview YAML + body (default action)",
            completer=self._file_completer,
            handler=lambda c, a: self._cmd_open(c, a),
        ))
        ctx.app.register_command(Command(
            name="edit",
            description="Open file in $EDITOR",
            completer=self._file_completer,
            handler=lambda c, a: self._cmd_edit(c, a),
        ))
        ctx.app.register_command(Command(
            name="sync",
            description="Run HorizontalSync; optional file arg limits scope",
            completer=self._file_completer,
            handler=lambda c, a: self._cmd_sync(c, a),
        ))
        ctx.app.register_command(Command(
            name="report",
            description="Summarize files/peers/codebases",
            handler=lambda c, a: self._cmd_report(c, a),
        ))
        ctx.app.register_command(Command(
            name="peers",
            description="List peers referenced in cast",
            handler=lambda c, a: self._cmd_peers(c, a),
        ))
        ctx.app.register_command(Command(
            name="codebases",
            description="List codebases (referenced vs installed)",
            handler=lambda c, a: self._cmd_codebases(c, a),
        ))
        ctx.app.register_command(Command(
            name="cbsync",
            description="Sync with a codebase: cbsync <name> [<file>]",
            handler=lambda c, a: self._cmd_cbsync(c, a),
        ))

        # Default command is "open"
        ctx.app.set_default_command("open")

        # Ctrl-R: reindex
        ctx.app.add_keybinding("c-r", lambda _ev: self._reindex(ctx))

    def bottom_toolbar(self, _ctx: TerminalContext) -> HTML | str:
        n = len(self._cast.items) if self._cast else 0
        name = (self._cast.cast_name if self._cast else "Cast")
        return HTML(f"<b>{name}</b> • {n} files • <b>Ctrl-R</b> reindex • <b>help</b> for commands • <b>quit</b> to exit")

    def prompt(self, _ctx: TerminalContext) -> str:
        name = (self._cast.cast_name if self._cast else "cast")
        return f"{name.lower()}:tui> "

    def default_command(self, _ctx: TerminalContext) -> str:
        return "open"

    # ----- Commands -----

    def _cmd_open(self, ctx: TerminalContext, args: list[str]) -> None:
        if not args:
            ctx.console.print("[yellow]Provide a file (use Tab to autocomplete).[/yellow]")
            return
        item = self._cast.resolve(args[0]) if self._cast else None
        if not item:
            ctx.console.print(f"[red]No match[/red] for '{args[0]}'. Try Tab completion or paste a cast-relative path.")
            return
        _preview_file(ctx.console, self._cast.vault, item)

    def _cmd_edit(self, ctx: TerminalContext, args: list[str]) -> None:
        if not args:
            ctx.console.print("[yellow]Provide a file to edit.[/yellow]")
            return
        it = self._cast.resolve(args[0]) if self._cast else None
        if not it:
            ctx.console.print(f"[red]No match[/red] for '{args[0]}'")
            return
        path = self._cast.vault / it.relpath
        if sys.platform.startswith("win"):
            editor = os.environ.get("EDITOR") or "notepad"
        else:
            editor = os.environ.get("EDITOR") or "vi"
        try:
            ctx.console.print(f"[dim]Opening editor:[/dim] {editor} {path}")
            os.system(f'{editor} "{path}"')
        except Exception as e:
            ctx.console.print(f"[red]Failed to open editor:[/red] {e}")

    def _cmd_sync(self, ctx: TerminalContext, args: list[str]) -> None:
        tok = args[0] if args else None
        code = _sync(ctx.console, self._cast, tok, non_interactive=True)
        if code == 0:
            ctx.console.print("[green][OK][/green] Sync completed successfully")
        elif code == 1:
            ctx.console.print("[yellow][WARN][/yellow] Sync completed with warnings")
        elif code == 3:
            ctx.console.print("[yellow][WARN][/yellow] Sync completed with conflicts")
        else:
            ctx.console.print("[red][ERROR][/red] Sync failed")

    def _cmd_report(self, ctx: TerminalContext, _args: list[str]) -> None:
        idx = build_ephemeral_index(self._cast.root, self._cast.vault, fixup=False)
        table = Table(show_header=True, header_style="bold")
        table.add_column("Metric")
        table.add_column("Value", justify="right")
        table.add_row("Files", str(len(idx.by_id)))
        table.add_row("Peers", str(len(idx.all_peers())))
        table.add_row("Codebases", str(len(idx.all_codebases())))
        ctx.console.print(table)

    def _cmd_peers(self, ctx: TerminalContext, _args: list[str]) -> None:
        idx = build_ephemeral_index(self._cast.root, self._cast.vault, fixup=False)
        peers = sorted(idx.all_peers())
        if not peers:
            ctx.console.print("[dim]No peers referenced in files.[/dim]")
            return
        t = Table(show_header=True, header_style="bold")
        t.add_column("Peer")
        for p in peers:
            t.add_row(p)
        ctx.console.print(t)

    def _cmd_codebases(self, ctx: TerminalContext, _args: list[str]) -> None:
        idx = build_ephemeral_index(self._cast.root, self._cast.vault, fixup=False)
        referenced = sorted(idx.all_codebases())
        installed = {c.name: c for c in list_codebases()}
        t = Table(show_header=True, header_style="bold")
        t.add_column("Codebase")
        t.add_column("Installed", justify="center")
        t.add_column("Root")
        t.add_column("Cast")
        names = sorted(set(referenced) | set(installed.keys()))
        if not names:
            ctx.console.print("[dim]No codebases found.[/dim]")
            return
        for name in names:
            ent = installed.get(name)
            t.add_row(name, "✔" if ent else "—", str(ent.root) if ent else "", getattr(ent, "origin_cast", None) or "")
        ctx.console.print(t)

    def _cmd_cbsync(self, ctx: TerminalContext, args: list[str]) -> None:
        if not args:
            ctx.console.print("[yellow]Usage:[/yellow] cbsync <codebase> [<file>]")
            return
        cb = args[0]
        file_arg = args[1] if len(args) > 1 else None
        syncer = CodebaseSync(self._cast.root)
        code = syncer.sync(cb, file_filter=file_arg, non_interactive=True)
        if code == 0:
            ctx.console.print("[green][OK][/green] Codebase sync completed")
        elif code == 3:
            ctx.console.print("[yellow][WARN][/yellow] Codebase sync completed with conflicts (skipped)")
        else:
            ctx.console.print("[red][ERROR][/red] Codebase sync failed")

    # ----- keybinding handlers -----

    def _reindex(self, ctx: TerminalContext) -> None:
        try:
            self._cast.reindex()
            ctx.console.print("[dim]Reindexed.[/dim]")
        except Exception as e:
            ctx.console.print(f"[red]Reindex failed:[/red] {e}")