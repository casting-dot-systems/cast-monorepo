"""Conflict resolution for Cast Sync."""

import difflib
import re
import shutil
from enum import Enum
from io import StringIO
from pathlib import Path

from cast_core.yamlio import reorder_cast_fields
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from ruamel.yaml import YAML


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

    def _split_front_matter(text: str) -> tuple[str | None, str]:
        """
        Split markdown into (yaml_text, body) if front matter exists; else (None, text).
        Recognizes common '---\\n...\\n---' front matter at file start.
        """
        m = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?", text, re.DOTALL)
        if not m:
            return None, text
        yaml_text = m.group(1)
        body = text[m.end() :]
        return yaml_text, body

    _yaml = YAML()
    _yaml.preserve_quotes = True
    _yaml.default_flow_style = False
    _yaml.width = 4096

    def _canonicalize_yaml_for_diff(yaml_text: str) -> str:
        """
        For display: parse YAML, reorder so that:
          - 'last-updated' is first,
          - cast-* fields are in canonical order,
          - others follow.
        This includes 'last-updated' (unlike digest).
        """
        try:
            data = _yaml.load(yaml_text) or {}
            if not isinstance(data, dict):
                return yaml_text
            data = reorder_cast_fields(dict(data))
            buf = StringIO()
            _yaml.dump(data, buf)
            return buf.getvalue().rstrip("\n")
        except Exception:
            # Fallback to original if parsing fails
            return yaml_text

    def _norm_lines(s: str) -> list[str]:
        return (s or "").replace("\r\n", "\n").replace("\r", "\n").splitlines()

    def _render_side_by_side(a: str, b: str, title_left: str, title_right: str) -> Table:
        """
        Render a side-by-side, line-diffed table using Rich.
        """
        a_lines = _norm_lines(a)
        b_lines = _norm_lines(b)
        sm = difflib.SequenceMatcher(None, a_lines, b_lines, autojunk=False)

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
                    l.stylize("dim")
                    r.stylize("dim")
                elif tag == "replace":
                    if left_txt == right_txt:
                        # Even inside a replace block, identical lines should be neutral.
                        l.stylize("dim")
                        r.stylize("dim")
                    else:
                        l.stylize("bold red")
                        r.stylize("bold green")
                elif tag == "delete":
                    l.stylize("bold red")
                elif tag == "insert":
                    r.stylize("bold green")
                table.add_row(l, r)
        return table

    if interactive:
        # Load local cast-name for clarity in legend
        def _local_cast_name(root: Path) -> str:
            try:
                cfg = root / ".cast" / "config.yaml"
                if not cfg.exists():
                    return "LOCAL"
                data = _yaml.load(cfg.read_text(encoding="utf-8")) or {}
                return data.get("cast-name", "LOCAL")
            except Exception:
                return "LOCAL"

        cast_name = _local_cast_name(cast_root)
        console.rule("[bold red]Conflict detected[/bold red]")

        # Legend panel
        legend = Table.grid(padding=(0, 2))
        legend.add_column(justify="left")
        legend.add_column(justify="left")
        legend.add_row(
            f"[bold]Left:[/bold] LOCAL ([cyan]{cast_name}[/cyan])",
            f"[bold]Right:[/bold] PEER [[magenta]{peer_name}[/magenta]]",
        )
        legend.add_row(
            "[red]Red[/red]: change/delete in LOCAL", "[green]Green[/green]: add/change in PEER"
        )
        console.print(Panel(legend, title="Diff legend", expand=True))

        # Split both sides into (yaml, body)
        local_yaml, local_body = _split_front_matter(local_preview or "")
        peer_yaml, peer_body = _split_front_matter(peer_preview or "")

        # YAML diff (empty string if missing)
        yaml_left = _canonicalize_yaml_for_diff(local_yaml) if local_yaml is not None else ""
        yaml_right = _canonicalize_yaml_for_diff(peer_yaml) if peer_yaml is not None else ""
        yaml_table = _render_side_by_side(
            yaml_left, yaml_right, f"LOCAL ({cast_name}) · YAML", f"PEER[{peer_name}] · YAML"
        )
        console.print(Panel(yaml_table, title="YAML front matter (side‑by‑side diff)", expand=True))

        # Body diff
        body_table = _render_side_by_side(
            (local_body or ""),
            (peer_body or ""),
            f"LOCAL ({cast_name}) · body",
            f"PEER[{peer_name}] · body",
        )
        console.print(Panel(body_table, title="Markdown body (side‑by‑side diff)", expand=True))

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
