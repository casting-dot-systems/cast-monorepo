"""Conflict resolution for Cast Sync."""

import shutil
import re
import difflib
from enum import Enum
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text


class ConflictResolution(Enum):
    """Conflict resolution choices."""

    KEEP_LOCAL = "local"
    KEEP_PEER = "peer"
    SKIP = "skip"


def handle_conflict(
    local_path: Path,
    peer_path: Path | None,
    cast_id: str,
    peer_name: str,
    cast_root: Path,
    interactive: bool = True,
    local_content: str | None = None,
    peer_content: str | None = None,
) -> ConflictResolution:
    """
    Handle a sync conflict by creating sidecar files and prompting user.

    Args:
        local_path: Path to local file
        peer_path: Path to peer file (if it exists)
        cast_id: Cast ID of the file
        peer_name: Name of the peer vault
        cast_root: Root of the Cast (contains .cast/)
        interactive: Whether to prompt user
        local_content: Optional local content to use instead of reading file
        peer_content: Optional peer content to use instead of reading file

    Returns:
        ConflictResolution choice
    """
    # Create conflicts directory
    conflicts_dir = cast_root / ".cast" / "conflicts"
    conflicts_dir.mkdir(parents=True, exist_ok=True)

    # Get title from filename
    title = local_path.stem

    # Write sidecar files
    local_sidecar = conflicts_dir / f"{title}~{cast_id}~LOCAL.md"
    peer_sidecar = conflicts_dir / f"{title}~{cast_id}~PEER-{peer_name}.md"

    # Write local version (write even if empty string was provided)
    if local_content is not None:
        local_sidecar.write_text(local_content, encoding="utf-8")
    elif local_path.exists():
        shutil.copy2(local_path, local_sidecar)

    # Write peer version (write even if empty string was provided)
    if peer_content is not None:
        peer_sidecar.write_text(peer_content, encoding="utf-8")
    elif peer_path and peer_path.exists():
        shutil.copy2(peer_path, peer_sidecar)

    # Preview (side-by-side) with YAML front matter awareness
    console = Console()
    try:
        local_preview: str = (
            local_content
            if local_content is not None
            else (local_path.read_text(encoding="utf-8") if local_path.exists() else "")
        )
        peer_preview: str = (
            peer_content
            if peer_content is not None
            else (peer_path.read_text(encoding="utf-8") if peer_path and peer_path.exists() else "")
        )
    except Exception:
        local_preview, peer_preview = "", ""

    def _split_front_matter(text: str) -> tuple[Optional[str], str]:
        """
        Split markdown into (yaml_text, body) if front matter exists; else (None, text).
        Recognizes common '---\\n...\\n---' front matter at file start.
        """
        m = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?", text, re.DOTALL)
        if not m:
            return None, text
        yaml_text = m.group(1)
        body = text[m.end():]
        return yaml_text, body

    def _render_side_by_side(a: str, b: str, title_left: str, title_right: str) -> Table:
        """
        Render a side-by-side, line-diffed table using Rich.
        """
        a_lines = (a or "").splitlines()
        b_lines = (b or "").splitlines()
        sm = difflib.SequenceMatcher(a=a_lines, b=b_lines)

        table = Table.grid(expand=True)
        table.add_column(f"{title_left}", ratio=1)
        table.add_column(f"{title_right}", ratio=1)

        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            # Max width approach: iterate longest span
            span = max(i2 - i1, j2 - j1)
            for k in range(span):
                left_txt = a_lines[i1 + k] if (i1 + k) < i2 else ""
                right_txt = b_lines[j1 + k] if (j1 + k) < j2 else ""

                l = Text(left_txt)
                r = Text(right_txt)
                if tag == "equal":
                    # Keep normal styling; could dim if desired:
                    # l.stylize("dim"); r.stylize("dim")
                    pass
                elif tag == "replace":
                    l.stylize("bold red")
                    r.stylize("bold green")
                elif tag == "delete":
                    l.stylize("bold red")
                elif tag == "insert":
                    r.stylize("bold green")
                table.add_row(l, r)
        return table

    if interactive:
        console.rule("[bold red]Conflict detected[/bold red]")
        # Split both sides into (yaml, body)
        local_yaml, local_body = _split_front_matter(local_preview or "")
        peer_yaml, peer_body = _split_front_matter(peer_preview or "")

        # YAML diff (empty string if missing)
        yaml_left = local_yaml if local_yaml is not None else ""
        yaml_right = peer_yaml if peer_yaml is not None else ""
        yaml_table = _render_side_by_side(
            yaml_left, yaml_right, "LOCAL (YAML)", f"PEER[{peer_name}] (YAML)"
        )
        console.print(Panel(yaml_table, title="YAML front matter (side-by-side diff)", expand=True))

        # Body diff
        body_table = _render_side_by_side(
            local_body or "", peer_body or "", "LOCAL (body)", f"PEER[{peer_name}] (body)"
        )
        console.print(Panel(body_table, title="Markdown body (side-by-side diff)", expand=True))

    if not interactive:
        # Non-interactive: keep local
        console.print(f"[yellow]Conflict in {local_path.name}: keeping LOCAL version[/yellow]")
        console.print(f"  Conflict files saved to {conflicts_dir}")
        return ConflictResolution.KEEP_LOCAL

    # Interactive prompt
    console.print("\nOptions:\n  1. Keep LOCAL\n  2. Keep PEER\n  3. Skip (resolve later)")

    while True:
        choice = input("\nYour choice [1/2/3]: ").strip()
        if choice == "1":
            return ConflictResolution.KEEP_LOCAL
        elif choice == "2":
            return ConflictResolution.KEEP_PEER
        elif choice == "3":
            return ConflictResolution.SKIP
        else:
            console.print("[red]Invalid choice. Please enter 1, 2, or 3.[/red]")
