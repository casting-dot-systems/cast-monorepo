"""Cast CLI commands."""

import json
import logging
import uuid
from pathlib import Path

import typer
from cast_core import (
    list_casts,
    register_cast,
    resolve_cast_by_name,
    unregister_cast,
)
from cast_core.filelock import cast_lock
from cast_sync import HorizontalSync, build_ephemeral_index
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table
from ruamel.yaml import YAML

# Initialize
app = typer.Typer(help="Cast Sync - Synchronize Markdown files across local vaults")
console = Console()
yaml = YAML()
yaml.preserve_quotes = True
yaml.default_flow_style = False

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M",
)
logger = logging.getLogger(__name__)


def _sanitize_name(name: str) -> str:
    """
    Lightly sanitize a cast name for file-system friendliness and consistency:
      - trim whitespace
      - replace path separators with hyphens
    """
    name = (name or "").strip()
    return name.replace("/", "-").replace("\\", "-")


def get_current_root() -> Path:
    """Find the Cast root by looking for .cast/ directory."""
    current = Path.cwd()

    # Check current directory first
    if (current / ".cast").exists():
        return current

    # Walk up to find .cast/
    for parent in current.parents:
        if (parent / ".cast").exists():
            return parent

    console.print("[red]Error: Not in a Cast root directory (no .cast/ found)[/red]")
    raise typer.Exit(2)


@app.command()
def install(
    path: str = typer.Argument(".", help="Path to an existing Cast root"),
    name: str | None = typer.Option(
        None,
        "--name",
        "-n",
        help="Override the cast name before registering (updates .cast/config.yaml).",
    ),
):
    """
    Install/register a Cast in the machine registry (under ~/.cast/registry.json).

    Notes:
      • Enforces unique names and roots in the registry (replaces any duplicates).
      • If --name is provided, .cast/config.yaml is updated prior to registration.
    """
    root = Path(path).expanduser().resolve()
    try:
        # Optionally rename the cast prior to registration
        if name:
            config_path = root / ".cast" / "config.yaml"
            if not config_path.exists():
                console.print(
                    "[red]Install failed:[/red] .cast/config.yaml not found in the target root"
                )
                raise typer.Exit(2)
            with open(config_path, encoding="utf-8") as f:
                cfg = yaml.load(f) or {}
            cfg["cast-name"] = _sanitize_name(name)
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(cfg, f)

        entry = register_cast(root)
        console.print(
            f"[green][OK][/green] Installed cast: [bold]{entry.name}[/bold]\n"
            f"  root: {entry.root}\n"
            f"  vault: {entry.vault_path}"
        )
    except Exception as e:
        console.print(f"[red]Install failed:[/red] {e}")
        raise typer.Exit(2) from e


@app.command("list")
def list_cmd(
    json_out: bool = typer.Option(False, "--json", help="Output as JSON"),
    show_ids: bool = typer.Option(False, "--ids", help="Include cast IDs in table output"),
):
    """List casts installed in the machine registry."""
    try:
        entries = list_casts()
        if json_out:
            payload = {
                "casts": [
                    {
                        "cast_id": e.cast_id,
                        "name": e.name,
                        "root": str(e.root),
                        "vault": str(e.vault_path),
                    }
                    for e in entries
                ]
            }
            print(json.dumps(payload, indent=2))
        else:
            console.rule("[bold cyan]Installed Casts[/bold cyan]")
            if not entries:
                console.print("[yellow]No casts installed[/yellow]")
            else:
                table = Table(show_header=True, header_style="bold")
                table.add_column("Name")
                if show_ids:
                    table.add_column("ID")
                table.add_column("Root")
                table.add_column("Vault")
                for e in entries:
                    row = [e.name]
                    if show_ids:
                        row.append(e.cast_id)
                    row.extend([str(e.root), str(e.vault_path)])
                    table.add_row(*row)
                console.print(table)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(2) from e


@app.command()
def init(
    name: str | None = typer.Option(None, "--name", help="Name for this Cast"),
    location: str = typer.Option("01 Vault", "--location", help="Vault location relative to root"),
    install_after: bool = typer.Option(
        True, "--install/--no-install", help="Also register in machine registry (default: install)"
    ),
):
    """Initialize a new Cast in the current directory."""
    root = Path.cwd()
    cast_dir = root / ".cast"

    if cast_dir.exists():
        console.print("[yellow]Cast already initialized in this directory[/yellow]")
        raise typer.Exit(1)

    # Prompt for name if not provided
    if not name:
        name = Prompt.ask("Enter a name for this Cast")

    name = _sanitize_name(name)
    # Create directories
    cast_dir.mkdir(parents=True)
    vault_dir = root / location
    vault_dir.mkdir(parents=True, exist_ok=True)

    # Create config
    config = {
        "cast-version": 1,
        "cast-id": str(uuid.uuid4()),
        "cast-name": name,
        "cast-location": location,
    }

    with open(cast_dir / "config.yaml", "w", encoding="utf-8") as f:
        yaml.dump(config, f)

    # Create empty syncstate
    syncstate = {"version": 1, "updated_at": "", "baselines": {}}
    with open(cast_dir / "syncstate.json", "w", encoding="utf-8") as f:
        json.dump(syncstate, f, indent=2)

    console.print(f"[green][OK] Cast initialized: {name}[/green]")
    console.print(f"  Root: {root}")
    console.print(f"  Vault: {vault_dir}")

    # Optional: auto-install/register to machine registry
    if install_after:
        try:
            entry = register_cast(root)
            console.print(
                f"[green][OK][/green] Installed cast: [bold]{entry.name}[/bold]\n  root: {entry.root}\n  vault: {entry.vault_path}"
            )
        except Exception as e:
            console.print(f"[red]Note:[/red] init succeeded, but auto-install failed: {e}")


# NOTE: 'setup' and 'add_vault' were removed. Peer discovery is registry-only.


@app.command()
def uninstall(
    identifier: str = typer.Argument(
        ...,
        help="Cast identifier: id, name, or path to root",
    ),
):
    """Uninstall (unregister) a Cast from the machine registry."""
    try:
        # Try by id
        removed = unregister_cast(cast_id=identifier)
        if not removed:
            # Try by name
            removed = unregister_cast(name=identifier)
        if not removed:
            # Try by root path
            p = Path(identifier).expanduser()
            if p.exists():
                removed = unregister_cast(root=p.resolve())

        if not removed:
            console.print(f"[red]Uninstall failed:[/red] No installed cast matched '{identifier}'")
            raise typer.Exit(2)

        console.print(
            f"[green][OK][/green] Uninstalled cast: [bold]{removed.name}[/bold] (id={removed.cast_id})\n  root: {removed.root}"
        )
    except Exception as e:
        console.print(f"[red]Uninstall failed:[/red] {e}")
        raise typer.Exit(2) from e


@app.command()
def hsync(
    file: str | None = typer.Option(None, "--file", help="Sync only this file (cast-id or path)"),
    peer: list[str] | None = typer.Option(None, "--peer", help="Sync only with these peers"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be done without doing it"
    ),
    non_interactive: bool = typer.Option(
        False, "--non-interactive", help="Don't prompt for conflicts"
    ),
    cascade: bool = typer.Option(
        True, "--cascade/--no-cascade", help="Also run hsync for peers (and peers of peers)"
    ),
):
    """Run horizontal sync across local vaults."""
    try:
        root = get_current_root()
        # (Note) registry-backed discovery happens inside HorizontalSync

        # Check if vault exists
        config_path = root / ".cast" / "config.yaml"
        with open(config_path, encoding="utf-8") as f:
            config_data = yaml.load(f)

        vault_path = root / config_data.get("cast-location", "01 Vault")
        if not vault_path.exists():
            console.print(f"[red]Error: Vault not found at {vault_path}[/red]")
            raise typer.Exit(2)

        # Run sync
        console.print(f"[cyan]Syncing vault: {vault_path}[/cyan]")

        try:
            with cast_lock(root):
                syncer = HorizontalSync(root)
                exit_code = syncer.sync(
                    peer_filter=list(peer) if peer else None,
                    file_filter=file,
                    dry_run=dry_run,
                    non_interactive=non_interactive,
                    cascade=cascade,
                )
        except RuntimeError as e:
            console.print(f"[red]Unable to start sync:[/red] {e}")
            raise typer.Exit(2)

        if exit_code == 0:
            console.print("[green][OK] Sync completed successfully[/green]")
        elif exit_code == 1:
            console.print("[yellow][WARN] Sync completed with warnings[/yellow]")
        elif exit_code == 3:
            console.print("[yellow][WARN] Sync completed with conflicts[/yellow]")
        else:
            console.print("[red][ERROR] Sync failed[/red]")

        if exit_code != 0:
            raise typer.Exit(exit_code)

    except FileNotFoundError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(2) from e
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        logger.exception("Sync failed")
        raise typer.Exit(2) from e


@app.command()
def doctor():
    """Check Cast configuration and report issues."""
    try:
        root = get_current_root()
        cast_dir = root / ".cast"

        issues = []
        warnings = []

        # Check config.yaml
        config_path = cast_dir / "config.yaml"
        if not config_path.exists():
            issues.append("config.yaml not found")
        else:
            with open(config_path, encoding="utf-8") as f:
                config = yaml.load(f)

            if not config.get("cast-id"):
                issues.append("cast-id missing in config.yaml")
            if not config.get("cast-name"):
                issues.append("cast-name missing in config.yaml")

            vault_location = config.get("cast-location", "01 Vault")
            vault_path = root / vault_location
            if not vault_path.exists():
                issues.append(f"Vault not found at {vault_location}")

            # Check registry installation state
            try:
                entries = list_casts()
                installed = any(
                    e.cast_id == config.get("cast-id") and e.root == root for e in entries
                )
                if not installed:
                    warnings.append(
                        "This Cast is not installed in the machine registry. Run 'cast install .'"
                    )
            except Exception as e:
                warnings.append(f"Could not read machine registry: {e}")

        # Check syncstate.json
        syncstate_path = cast_dir / "syncstate.json"
        if not syncstate_path.exists():
            warnings.append("syncstate.json not found (will be created on first sync)")

        # Validate that referenced peers are resolvable via the machine registry
        try:
            if config_path.exists() and not issues:
                vault_location = config.get("cast-location", "01 Vault")
                vault_path = root / vault_location
                if vault_path.exists():
                    idx = build_ephemeral_index(root, vault_path, fixup=False)
                    for peer in sorted(idx.all_peers()):
                        if not resolve_cast_by_name(peer):
                            warnings.append(
                                f"Peer '{peer}' not found in machine registry. "
                                "Install that peer with 'cast install .' in its root."
                            )
        except Exception as e:
            warnings.append(f"Peer check skipped due to error: {e}")

        # Report
        if issues:
            console.print("[red]Issues found:[/red]")
            for issue in issues:
                console.print(f"  [X] {issue}")

        if warnings:
            console.print("[yellow]Warnings:[/yellow]")
            for warning in warnings:
                console.print(f"  [!] {warning}")

        if not issues and not warnings:
            console.print("[green][OK] Cast configuration looks good![/green]")

        return 0 if not issues else 1

    except Exception as e:
        console.print(f"[red]Error during check: {e}[/red]")
        return 2


@app.command()
def report():
    """Generate a report of Cast files and peers."""
    try:
        root = get_current_root()

        # Build index
        from cast_sync import build_ephemeral_index

        config_path = root / ".cast" / "config.yaml"
        with open(config_path, encoding="utf-8") as f:
            config = yaml.load(f)

        vault_path = root / config.get("cast-location", "01 Vault")

        if not vault_path.exists():
            console.print(f"[red]Error: Vault not found at {vault_path}[/red]")
            raise typer.Exit(2)

        index = build_ephemeral_index(root, vault_path, fixup=False)

        # Generate report
        report = {
            "vault": str(vault_path),
            "files": len(index.by_id),
            "peers": list(index.all_peers()),
            "codebases": list(index.all_codebases()),
            "file_list": [],
        }

        for cast_id, rec in index.by_id.items():
            report["file_list"].append(
                {
                    "cast_id": cast_id,
                    "path": rec["relpath"],
                    "peers": rec["peers"],
                    "codebases": rec["codebases"],
                }
            )

        # Output as JSON
        console.print(json.dumps(report, indent=2))

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(2) from e


if __name__ == "__main__":
    app()
