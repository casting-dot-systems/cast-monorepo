"""Cast CLI commands."""

import json
import logging
import uuid
from pathlib import Path

import typer
from cast_sync import HorizontalSync
from rich.console import Console
from rich.prompt import Prompt
from ruamel.yaml import YAML
from typing import Optional

from cast_core import (
    register_cast,
    list_casts,
)

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
def install(path: str = typer.Argument(".", help="Path to an existing Cast root")):
    """Install/register a Cast in the machine registry (under ~/.cast/registry.json)."""
    root = Path(path).expanduser().resolve()
    try:
        entry = register_cast(root)
        console.print(
            f"[green][OK][/green] Installed cast: [bold]{entry.name}[/bold] "
            f"(id={entry.cast_id})\n  root: {entry.root}\n  vault: {entry.vault_path}"
        )
    except Exception as e:
        console.print(f"[red]Install failed:[/red] {e}")
        raise typer.Exit(2) from e


@app.command("list")
def list_cmd(json_out: bool = typer.Option(False, "--json", help="Output as JSON")):
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
            for e in entries:
                console.print(f"[bold]{e.name}[/bold]  (id={e.cast_id})\n  root: {e.root}\n  vault: {e.vault_path}\n")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(2) from e


@app.command()
def init(
    name: str | None = typer.Option(None, "--name", help="Name for this Cast"),
    location: str = typer.Option("01 Vault", "--location", help="Vault location relative to root"),
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

    with open(cast_dir / "config.yaml", "w") as f:
        yaml.dump(config, f)

    # Create empty syncstate
    syncstate = {"version": 1, "updated_at": "", "baselines": {}}
    with open(cast_dir / "syncstate.json", "w") as f:
        json.dump(syncstate, f, indent=2)

    # Add local.yaml to .gitignore
    gitignore = root / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text()
        if "/.cast/local.yaml" not in content:
            with open(gitignore, "a") as f:
                f.write("\n/.cast/local.yaml\n")
    else:
        gitignore.write_text("/.cast/local.yaml\n")

    console.print(f"[green][OK] Cast initialized: {name}[/green]")
    console.print(f"  Root: {root}")
    console.print(f"  Vault: {vault_dir}")


@app.command()
def setup():
    """Set up local configuration for this Cast."""
    root = get_current_root()
    cast_dir = root / ".cast"
    local_path = cast_dir / "local.yaml"

    if local_path.exists():
        console.print("[yellow]Local configuration already exists[/yellow]")
        if not typer.confirm("Overwrite?", default=False):
            raise typer.Exit(0)

    # Create local config
    config = {
        "path-to-root": str(root.absolute()),
        "installed-vaults": [],
        "installed-codebases": [],
    }

    with open(local_path, "w") as f:
        yaml.dump(config, f)

    console.print("[green][OK] Local configuration created[/green]")
    console.print(f"  Edit {local_path} to add peer vaults")


@app.command()
def add_vault(
    name: str = typer.Argument(..., help="Name of the peer vault"),
    path: str = typer.Argument(..., help="Path to the peer vault folder"),
):
    """Add a peer vault to local configuration."""
    root = get_current_root()
    local_path = root / ".cast" / "local.yaml"

    if not local_path.exists():
        console.print("[red]Error: Run 'cast setup' first[/red]")
        raise typer.Exit(2)

    # Load existing config
    with open(local_path) as f:
        config = yaml.load(f)

    if not config:
        config = {"installed-vaults": []}
    if "installed-vaults" not in config:
        config["installed-vaults"] = []

    # Check if already exists
    for vault in config["installed-vaults"]:
        if vault["name"] == name:
            console.print(f"[yellow]Vault '{name}' already exists[/yellow]")
            if not Prompt.ask("Update path?", default=False):
                raise typer.Exit(0)
            vault["filepath"] = str(Path(path).absolute())
            break
    else:
        # Add new vault
        config["installed-vaults"].append({"name": name, "filepath": str(Path(path).absolute())})

    # Save
    with open(local_path, "w") as f:
        yaml.dump(config, f)

    console.print(f"[green][OK] Added vault: {name} -> {path}[/green]")


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
):
    """Run horizontal sync across local vaults."""
    try:
        root = get_current_root()
        # (Note) registry-backed discovery happens inside HorizontalSync

        # Check if vault exists
        config_path = root / ".cast" / "config.yaml"
        with open(config_path) as f:
            config_data = yaml.load(f)

        vault_path = root / config_data.get("cast-location", "01 Vault")
        if not vault_path.exists():
            console.print(f"[red]Error: Vault not found at {vault_path}[/red]")
            raise typer.Exit(2)

        # Run sync
        console.print(f"[cyan]Syncing vault: {vault_path}[/cyan]")

        syncer = HorizontalSync(root)
        exit_code = syncer.sync(
            peer_filter=list(peer) if peer else None,
            file_filter=file,
            dry_run=dry_run,
            non_interactive=non_interactive,
        )

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
            with open(config_path) as f:
                config = yaml.load(f)

            if not config.get("cast-id"):
                issues.append("cast-id missing in config.yaml")
            if not config.get("cast-name"):
                issues.append("cast-name missing in config.yaml")

            vault_location = config.get("cast-location", "01 Vault")
            vault_path = root / vault_location
            if not vault_path.exists():
                issues.append(f"Vault not found at {vault_location}")

        # Check local.yaml
        local_path = cast_dir / "local.yaml"
        if not local_path.exists():
            warnings.append("local.yaml not found (run 'cast setup')")
        else:
            with open(local_path) as f:
                local_config = yaml.load(f)

            if local_config:
                # Check installed vaults
                for vault in local_config.get("installed-vaults", []):
                    vault_path = Path(vault["filepath"])
                    if not vault_path.exists():
                        warnings.append(
                            f"Vault '{vault['name']}' path not found: {vault['filepath']}"
                        )

        # Check syncstate.json
        syncstate_path = cast_dir / "syncstate.json"
        if not syncstate_path.exists():
            warnings.append("syncstate.json not found (will be created on first sync)")

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
        with open(config_path) as f:
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
