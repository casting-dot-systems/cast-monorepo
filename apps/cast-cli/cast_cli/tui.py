"""Interactive terminal (TUI) for Cast with fuzzy file search.

Usage:
  cast tui

Features:
  • Fuzzy autocompletion for files (prompt_toolkit FuzzyCompleter).
  • Preview YAML + body snippet for quick inspection.
  • One-shot edit via $EDITOR (best effort; returns to TUI).
  • Run sync (optionally file-scoped via --file equivalent).
  • Live reindex (Ctrl-R) without leaving the TUI.
  • Report & peers quick views.

Design notes:
  - Avoid circular imports with cli.py (we re-implement a tiny root-finder).
  - Read-only by default (except 'sync' which delegates to HorizontalSync).
  - No persistent state; index is ephemeral and refreshable.

"""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from ruamel.yaml import YAML

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion, FuzzyCompleter, NestedCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import CompleteStyle

from cast_sync import build_ephemeral_index, HorizontalSync
from cast_core.yamlio import parse_cast_file

tui_app = typer.Typer(help="Interactive terminal for Cast (fuzzy file search & quick actions)")

console = Console()
_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.default_flow_style = False
_yaml.width = 4096


# ---------- small utils ----------
def _find_cast_root() -> Path:
    """Find the Cast root by looking for .cast/ in cwd or its parents."""
    cur = Path.cwd()
    if (cur / ".cast").exists():
        return cur
    for p in cur.parents:
        if (p / ".cast").exists():
            return p
    console.print("[red]Error: Not in a Cast root (no .cast/ found)[/red]")
    raise typer.Exit(2)


def _read_config(root: Path) -> tuple[str, Path]:
    """Return (cast_name, vault_path) from .cast/config.yaml."""
    cfg = root / ".cast" / "config.yaml"
    if not cfg.exists():
        console.print("[red].cast/config.yaml missing[/red]")
        raise typer.Exit(2)
    with open(cfg, encoding="utf-8") as f:
        data = _yaml.load(f) or {}
    cast_name = data.get("cast-name", "")
    vault_rel = data.get("cast-location", "Cast")
    vault = root / vault_rel
    if not vault.exists():
        console.print(f"[red]Vault not found at {vault}[/red]")
        raise typer.Exit(2)
    return cast_name, vault


@dataclass
class FileItem:
    cast_id: str | None  # None for regular files without cast-id
    relpath: str
    title: str | None


class CastContext:
    """In-memory view of the current Cast (root, vault, index, file metadata)."""

    def __init__(self, root: Path, vault: Path, cast_name: str):
        self.root = root
        self.vault = vault
        self.cast_name = cast_name
        self.items: list[FileItem] = []
        self._by_id: dict[str, FileItem] = {}
        self._by_path: dict[str, FileItem] = {}

    def reindex(self) -> None:
        """Refresh ephemeral index and derived metadata, including all vault files."""
        # First, get Cast files with cast-ids from the ephemeral index
        idx = build_ephemeral_index(self.root, self.vault, fixup=False)
        items: list[FileItem] = []
        cast_files = set()

        # Add Cast files from the index
        for rec in idx.by_id.values():
            p = self.vault / rec["relpath"]
            cast_files.add(rec["relpath"])
            title = None
            try:
                fm, _body, has = parse_cast_file(p)
                if has and isinstance(fm, dict):
                    title = fm.get("title") or fm.get("name")
            except Exception:
                title = None
            items.append(FileItem(cast_id=rec["cast_id"], relpath=rec["relpath"], title=title))

        # Add all other files in the vault
        try:
            for file_path in self.vault.rglob("*"):
                if file_path.is_file():
                    try:
                        relpath = str(file_path.relative_to(self.vault))
                        # Skip if already processed as Cast file
                        if relpath in cast_files:
                            continue
                        
                        # Try to extract title for any file type (best effort)
                        title = None
                        if file_path.suffix.lower() in {'.md', '.txt'}:
                            try:
                                with open(file_path, 'r', encoding='utf-8') as f:
                                    first_line = f.readline().strip()
                                    # Extract title from markdown header
                                    if first_line.startswith('# '):
                                        title = first_line[2:].strip()
                            except Exception:
                                pass
                        
                        items.append(FileItem(cast_id=None, relpath=relpath, title=title))
                    except (ValueError, OSError):
                        continue  # Skip files we can't process
        except Exception:
            pass  # Continue with just Cast files if directory traversal fails

        items.sort(key=lambda it: it.relpath.lower())
        self.items = items
        self._by_id = {it.cast_id: it for it in items if it.cast_id}
        self._by_path = {it.relpath: it for it in items}

    def resolve(self, token: str) -> Optional[FileItem]:
        """Resolve user token to a file by cast-id exact match or relpath (with or without leading 'Cast/')."""
        if not token:
            return None
        # cast-id exact
        it = self._by_id.get(token)
        if it:
            return it
        # relpath (with or without leading vault folder)
        p = Path(token)
        if p.parts and p.parts[0] == self.vault.name:
            token = str(Path(*p.parts[1:]))
        return self._by_path.get(token)


class CastFileCompleter(Completer):
    """Dynamic file completer over CastContext items (to be wrapped by FuzzyCompleter)."""

    def __init__(self, ctx: CastContext):
        self.ctx = ctx

    @staticmethod
    def _needs_quoting(s: str) -> bool:
        """Return True if token should be double-quoted for shlex safety."""
        if not s:
            return True
        specials = set(' \t\r\n"\'\\&|;<>*?()[]{}')
        return any((c in specials) or c.isspace() for c in s)

    @staticmethod
    def _dq(s: str) -> str:
        """Double-quote s, escaping backslashes and quotes for POSIX shlex."""
        s = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{s}"'

    @staticmethod
    def _current_arg_token_and_len(document) -> tuple[str, int]:
        """
        Return (token, length) for the current argument from the last whitespace to cursor.
        This includes a half-typed opening quote if present so we can replace it cleanly.
        """
        buf = document.text_before_cursor
        # Find start of current arg (after last whitespace)
        i = max(buf.rfind(" "), buf.rfind("\t"))
        start = i + 1
        token = buf[start:]
        return token, len(token)

    def get_completions(self, document, complete_event):
        # Replace the *entire* current argument (not just "word" chars),
        # so we handle partial quotes and paths with punctuation.
        token, token_len = self._current_arg_token_and_len(document)
        # Yield all items; FuzzyCompleter will filter/rank.
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


def _bottom_toolbar(ctx: CastContext) -> HTML:
    return HTML(
        f"<b>{ctx.cast_name or 'Cast'}</b> • {len(ctx.items)} files"
        " • <b>Ctrl-R</b> reindex • <b>help</b> for commands • <b>quit</b> to exit"
    )


def _preview_file(vault: Path, it: FileItem) -> None:
    path = vault / it.relpath
    if not path.exists():
        console.print(f"[red]Not found:[/red] {path}")
        return
    
    # Header table
    tab = Table.grid(expand=True)
    tab.add_row(Text(str(path), style="bold"))
    if it.cast_id:
        tab.add_row(Text(f"cast-id: {it.cast_id}", style="dim"))
    if it.title:
        tab.add_row(Text(f"title: {it.title}"))
    console.print(tab)

    # Try to parse as Cast file first
    fm, body, has_cast_yaml = parse_cast_file(path)
    
    if has_cast_yaml and isinstance(fm, dict):
        # Show Cast file YAML subset
        sub = {k: fm.get(k) for k in ("last-updated", "cast-id", "cast-version", "url", "title") if k in fm}
        if sub:
            yaml_text = ""
            try:
                from io import StringIO

                buf = StringIO()
                y = YAML()
                y.preserve_quotes = True
                y.default_flow_style = False
                y.width = 120
                y.dump(sub, buf)
                yaml_text = buf.getvalue()
            except Exception:
                pass
            if yaml_text:
                console.print(Panel.fit(yaml_text.rstrip("\n"), title="YAML (subset)", border_style="cyan"))
        
        # Show Cast file body
        snippet = "\n".join((body or "").splitlines()[:60]) or "_(empty)_"
        console.print(Panel(Markdown(snippet), title="Body (first 60 lines)", expand=True))
    else:
        # Handle regular files - show raw content
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            snippet = "\n".join(content.splitlines()[:60]) or "_(empty)_"
            
            # Use Markdown for .md files, plain text for others
            if path.suffix.lower() == '.md':
                console.print(Panel(Markdown(snippet), title=f"Content (first 60 lines)", expand=True))
            else:
                console.print(Panel(snippet, title=f"Content (first 60 lines)", expand=True))
        except Exception as e:
            console.print(f"[red]Error reading file:[/red] {e}")


def _sync(ctx: CastContext, file_token: Optional[str] = None, non_interactive: bool = True) -> int:
    hs = HorizontalSync(ctx.root)
    filt = None
    if file_token:
        it = ctx.resolve(file_token)
        # Only sync if the file has a cast-id (is a Cast file)
        if it and it.cast_id:
            filt = it.cast_id
        elif it and not it.cast_id:
            console.print(f"[yellow]Warning:[/yellow] '{file_token}' is not a Cast file (no cast-id). Syncing all files instead.")
        else:
            # Fallback to the token as-is (might be a cast-id directly)
            filt = file_token
    code = hs.sync(
        file_filter=filt,
        non_interactive=non_interactive,
        cascade=True,
        dry_run=False,
        debug=False,
    )
    # Friendly summary
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
    ctx.reindex()
    return code


def _report(ctx: CastContext) -> None:
    idx = build_ephemeral_index(ctx.root, ctx.vault, fixup=False)
    table = Table(show_header=True, header_style="bold")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Files", str(len(idx.by_id)))
    table.add_row("Peers", str(len(idx.all_peers())))
    table.add_row("Codebases", str(len(idx.all_codebases())))
    console.print(table)


def _peers(ctx: CastContext) -> None:
    idx = build_ephemeral_index(ctx.root, ctx.vault, fixup=False)
    peers = sorted(idx.all_peers())
    if not peers:
        console.print("[dim]No peers referenced in files.[/dim]")
        return
    t = Table(show_header=True, header_style="bold")
    t.add_column("Peer")
    for p in peers:
        t.add_row(p)
    console.print(t)


def _edit(vault: Path, it: FileItem) -> None:
    path = vault / it.relpath
    if sys.platform.startswith("win"):
        editor = os.environ.get("EDITOR") or "notepad"
    else:
        editor = os.environ.get("EDITOR") or "vi"
    try:
        console.print(f"[dim]Opening editor:[/dim] {editor} {path}")
        os.system(f'{editor} "{path}"')
    except Exception as e:
        console.print(f"[red]Failed to open editor:[/red] {e}")


def _help() -> None:
    console.rule("[bold]Commands[/bold]")
    console.print(
        """\
[bold]open[/bold] <file>     Preview YAML + body (default if you only type a file)
[bold]view[/bold] <file>     Alias for open
[bold]edit[/bold] <file>     Open in $EDITOR (best effort)
[bold]sync[/bold] [file]     Run HorizontalSync (limits to file if provided)
[bold]report[/bold]          Summarize files/peers/codebases
[bold]peers[/bold]           List all peers found in index
[bold]help[/bold]            Show this help
[bold]quit[/bold] | exit     Leave TUI

Tips:
  • Use [bold]Tab[/bold] for fuzzy completion.
  • Press [bold]Ctrl‑R[/bold] any time to reindex.
"""
    )


# ---------- interactive entrypoint ----------
@tui_app.callback(invoke_without_command=True)
def tui(_ctx: typer.Context) -> None:
    """
    Launch the interactive terminal. Just run `cast tui`.
    """
    root = _find_cast_root()
    cast_name, vault = _read_config(root)
    ctx = CastContext(root, vault, cast_name)
    ctx.reindex()

    file_completer = CastFileCompleter(ctx)

    # Hierarchical command completer with dynamic file completion.
    base = NestedCompleter(
        {
            "open": file_completer,
            "view": file_completer,
            "edit": file_completer,
            "sync": file_completer,  # optional file arg
            "report": None,
            "peers": None,
            "help": None,
            "quit": None,
            "exit": None,
        }
    )
    completer = FuzzyCompleter(base)

    kb = KeyBindings()

    @kb.add("c-r")
    def _reindex(_event):
        ctx.reindex()
        console.print("[dim]Reindexed.[/dim]")

    history = InMemoryHistory()
    session = PromptSession(history=history)

    while True:
        try:
            text = session.prompt(
                "cast:tui> ",
                completer=completer,
                complete_style=CompleteStyle.MULTI_COLUMN,
                key_bindings=kb,
                bottom_toolbar=lambda: _bottom_toolbar(ctx),
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break

        if not text:
            continue

        parts = shlex.split(text)
        cmd = parts[0]
        args = parts[1:]

        # Default action: interpret a single token as open <file>
        if cmd not in {"open", "view", "edit", "sync", "report", "peers", "help", "quit", "exit"}:
            args = [cmd] + args
            cmd = "open"

        if cmd in ("quit", "exit"):
            break

        if cmd in ("help", "?"):
            _help()
            continue

        if cmd == "report":
            _report(ctx)
            continue

        if cmd == "peers":
            _peers(ctx)
            continue

        if cmd == "sync":
            tok = args[0] if args else None
            code = _sync(ctx, tok, non_interactive=True)
            if code == 0:
                console.print("[green][OK][/green] Sync completed successfully")
            elif code == 1:
                console.print("[yellow][WARN][/yellow] Sync completed with warnings")
            elif code == 3:
                console.print("[yellow][WARN][/yellow] Sync completed with conflicts")
            else:
                console.print("[red][ERROR][/red] Sync failed")
            continue

        # open/view/edit require a file token (path or cast-id)
        if not args:
            console.print("[yellow]Provide a file (use Tab to autocomplete).[/yellow]")
            continue

        tok = args[0]
        item = ctx.resolve(tok)
        if not item:
            console.print(
                f"[red]No match[/red] for '{tok}'. Try fuzzy completion with Tab or paste a vault-relative path."
            )
            continue

        if cmd in ("open", "view"):
            _preview_file(ctx.vault, item)
        elif cmd == "edit":
            _edit(ctx.vault, item)
        else:
            console.print(f"[red]Unknown command:[/red] {cmd}")