"""Conflict resolution for Cast Sync."""

import shutil
import re
from enum import Enum
from pathlib import Path
from typing import Optional

from rich.columns import Columns
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.console import Group


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

    # Write local version
    if local_content:
        local_sidecar.write_text(local_content, encoding="utf-8")
    elif local_path.exists():
        shutil.copy2(local_path, local_sidecar)

    # Write peer version
    if peer_content:
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

    def _render_preview(content: str) -> Group:
        yaml_text, body = _split_front_matter(content or "")
        blocks = []
        if yaml_text is not None:
            blocks.append(
                Panel(
                    Syntax(yaml_text, "yaml", word_wrap=True),
                    title="YAML front matter",
                    expand=True,
                )
            )
        else:
            blocks.append(
                Panel(
                    "[dim]No front matter[/dim]",
                    title="YAML front matter",
                    expand=True,
                )
            )
        blocks.append(Panel(Markdown(body or ""), title="Markdown body", expand=True))
        return Group(*blocks)

    if interactive:
        console.rule("[bold red]Conflict detected[/bold red]")
        console.print(
            Columns(
                [
                    Panel(
                        _render_preview(local_preview),
                        title=f"LOCAL · {local_path.name}",
                        expand=True,
                    ),
                    Panel(
                        _render_preview(peer_preview),
                        title=f"PEER[{peer_name}] · {peer_path.name if peer_path else '(missing)'}",
                        expand=True,
                    ),
                ],
                equal=True,
                expand=True,
            )
        )

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
