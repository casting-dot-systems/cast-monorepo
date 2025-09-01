"""Google Docs integration for Cast CLI (one-way pull).

Commands:
  cast gdoc new "<Title>" [--dir RELPATH] [--folder-id FOLDER] [--share-with EMAIL ...]
  cast gdoc pull <file.md> [--no-extract-images]

Auth precedence:
  1) Service account via GOOGLE_APPLICATION_CREDENTIALS
  2) OAuth client in .cast/google/client_secret.json (token cached as .cast/google/token.json)
"""
from __future__ import annotations

import base64
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
import dotenv

import typer
from rich.console import Console
from ruamel.yaml import YAML

from cast_core.yamlio import write_cast_file, parse_cast_file, ensure_cast_fields

dotenv.load_dotenv()

gdoc_app = typer.Typer(help="Google Docs integration (create & pull)")
console = Console()
yaml_rt = YAML()
yaml_rt.preserve_quotes = True
yaml_rt.default_flow_style = False
yaml_rt.width = 4096

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
DOCS_SCOPES = ["https://www.googleapis.com/auth/documents.readonly"]
SCOPES = DRIVE_SCOPES + DOCS_SCOPES


# -------------------- small utils --------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sanitize_filename(name: str) -> str:
    """Mildly sanitize a name for a Markdown filename."""
    s = (name or "").strip()
    # Keep spaces and common punctuation; remove path separators and control chars.
    s = s.replace("/", "-").replace("\\", "-")
    s = re.sub(r"[\x00-\x1f\x7f]", "", s)
    return s


def _get_root_and_vault() -> tuple[Path, Path]:
    """Locate Cast root (contains .cast/) and vault path from config."""
    cur = Path.cwd()
    root = None
    if (cur / ".cast").exists():
        root = cur
    else:
        for p in cur.parents:
            if (p / ".cast").exists():
                root = p
                break
    if root is None:
        console.print("[red]Not in a Cast root (no .cast/ found)[/red]")
        raise typer.Exit(2)
    cfg_path = root / ".cast" / "config.yaml"
    if not cfg_path.exists():
        console.print("[red].cast/config.yaml missing[/red]")
        raise typer.Exit(2)
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml_rt.load(f) or {}
    vault_rel = cfg.get("cast-location", "Cast")
    vault = root / vault_rel
    if not vault.exists():
        console.print(f"[red]Vault not found at {vault}[/red]")
        raise typer.Exit(2)
    return root, vault


def _ensure_google_deps():
    """Fail fast with a guidance message if google deps are not installed."""
    try:
        import googleapiclient  # noqa: F401
        import google_auth_oauthlib  # noqa: F401
        import google.oauth2  # noqa: F401
    except Exception:
        console.print(
            "[red]Missing Google client libraries.[/red]\n"
            "This should not happen as they are required dependencies.\n"
            "Try reinstalling: [bold]uv tool install --editable ./apps/cast-cli[/bold]"
        )
        raise typer.Exit(2)


def _get_creds(root: Path):
    """Return Google credentials (service account preferred; else OAuth)."""
    _ensure_google_deps()
    from google.oauth2.service_account import Credentials as SA
    from google.oauth2.credentials import Credentials as UserCreds
    from google_auth_oauthlib.flow import InstalledAppFlow

    # 1) Service account
    sa_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if sa_path and Path(sa_path).exists():
        return SA.from_service_account_file(sa_path, scopes=SCOPES)

    # 2) OAuth (stored under .cast/google)
    gdir = root / ".cast" / "google"
    gdir.mkdir(parents=True, exist_ok=True)
    token = gdir / "token.json"
    secret = gdir / "client_secret.json"

    creds = None
    if token.exists():
        try:
            creds = UserCreds.from_authorized_user_file(str(token), SCOPES)
        except Exception:
            creds = None
    if not creds:
        if not secret.exists():
            console.print(
                "[red]No service account found and OAuth client missing.[/red]\n"
                f"Place your OAuth client at: [bold]{secret}[/bold]\n"
                "Then rerun the command to complete auth."
            )
            raise typer.Exit(2)
        flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
        creds = flow.run_local_server(port=0)
        token.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _build_services(root: Path):
    _ensure_google_deps()
    from googleapiclient.discovery import build

    creds = _get_creds(root)
    drive = build("drive", "v3", credentials=creds)
    docs = build("docs", "v1", credentials=creds)
    return drive, docs


def _resolve_folder_id(drive, folder_id: str) -> str:
    """Resolve shortcuts and validate Shared Drive folders."""
    try:
        meta = drive.files().get(
            fileId=folder_id,
            fields="id, mimeType, driveId, shortcutDetails",
            supportsAllDrives=True
        ).execute()
    except Exception as e:
        console.print(f"[red]Error accessing folder {folder_id}:[/red] {e}")
        raise typer.Exit(2)

    # If it's a shortcut, hop to the real target
    if meta.get("mimeType") == "application/vnd.google-apps.shortcut":
        target_id = meta["shortcutDetails"]["targetId"]
        try:
            meta = drive.files().get(
                fileId=target_id,
                fields="id, mimeType, driveId",
                supportsAllDrives=True
            ).execute()
        except Exception as e:
            console.print(f"[red]Error accessing shortcut target {target_id}:[/red] {e}")
            raise typer.Exit(2)

    # Must be a folder in a Shared Drive (driveId present)
    if meta.get("mimeType") != "application/vnd.google-apps.folder":
        console.print("[red]The provided --folder-id is not a folder.[/red]")
        raise typer.Exit(2)
    if not meta.get("driveId"):
        console.print(
            "[red]The provided folder is not in a Shared Drive.[/red]\n"
            "Use a Shared Drive folder to avoid service-account quota issues."
        )
        raise typer.Exit(2)
    
    return meta["id"]


def _create_google_doc(drive, title: str, parent_folder_id: Optional[str]) -> tuple[str, str]:
    """Create a Google Doc with proper Shared Drive support."""
    from googleapiclient.errors import HttpError
    
    body = {"name": title, "mimeType": "application/vnd.google-apps.document"}
    if parent_folder_id:
        body["parents"] = [parent_folder_id]
    
    try:
        file = drive.files().create(
            body=body, 
            fields="id,webViewLink", 
            supportsAllDrives=True
        ).execute()
    except HttpError as e:
        if e.resp.status == 403 and "storageQuotaExceeded" in str(e):
            console.print(
                "[red]Drive reports: storage quota exceeded.[/red]\n"
                "Likely causes:\n"
                "  • Folder is not a Shared Drive folder or is a shortcut → resolve it.\n"
                "  • Service account lacks Content manager on the Shared Drive.\n"
                "Fix: pass --folder-id for a real Shared Drive folder the SA can write to."
            )
            raise typer.Exit(2)
        raise
    
    doc_id = file["id"]
    url = file.get("webViewLink", f"https://docs.google.com/document/d/{doc_id}/edit")
    return doc_id, url


def _export_markdown(drive, doc_id: str) -> str:
    data = drive.files().export(fileId=doc_id, mimeType="text/markdown", supportsAllDrives=True).execute()
    return data.decode("utf-8")


DATA_URI_RE = re.compile(r"!\[[^\]]*]\((data:image/[a-zA-Z]+;base64,[^)]+)\)")


def _extract_data_uris(md: str, media_dir_abs: Path, rel_prefix_from_file: Path, base_name: str) -> str:
    """
    Write embedded data-URI images to media_dir_abs and rewrite links to relative paths
    from the Markdown file's parent.
    """
    media_dir_abs.mkdir(parents=True, exist_ok=True)
    # Compute the relative prefix from file parent to media_dir_abs (POSIX-ish for Markdown)
    rel_prefix = Path(os.path.relpath(media_dir_abs, start=rel_prefix_from_file)).as_posix()
    idx = 1

    def repl(m):
        nonlocal idx
        uri = m.group(1)
        header, b64 = uri.split(",", 1)
        ext = header.split("/")[1].split(";")[0]
        fname = f"{base_name}-img-{idx}.{ext}"
        (media_dir_abs / fname).write_bytes(base64.b64decode(b64))
        idx += 1
        return m.group(0).replace(uri, f"{rel_prefix}/{fname}")

    return DATA_URI_RE.sub(repl, md)


# -------------------- commands --------------------
@gdoc_app.command("new")
def gdoc_new(
    title: str = typer.Argument(..., help="Title for the new note & Google Doc"),
    dir: Path = typer.Option(Path("."), "--dir", help="Vault-relative directory for the note"),
    folder_id: Optional[str] = typer.Option(None, "--folder-id", help="Drive folderId for the Doc"),
    share_with: List[str] = typer.Option(
        [], "--share-with", help="Email(s) to grant writer access to the Doc"
    ),
):
    """
    Create an empty Google Doc with the same title as the note and link it in YAML.
    """
    root, vault = _get_root_and_vault()
    
    # Fail fast if using a service account without a Shared Drive folder
    is_sa = bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
    if is_sa and not folder_id:
        console.print(
            "[red]Using a service account requires --folder-id for a Shared Drive folder[/red]\n"
            "Add the SA as Content manager on that Shared Drive and pass its folder ID."
        )
        raise typer.Exit(2)
    
    drive, docs = _build_services(root)

    # Resolve and validate folder ID if provided
    if folder_id:
        folder_id = _resolve_folder_id(drive, folder_id)

    # File path
    safe_title = _sanitize_filename(title)
    note_path = (vault / dir).resolve() / f"{safe_title}.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)

    # Create Doc
    doc_id, url = _create_google_doc(drive, title=safe_title, parent_folder_id=folder_id)

    # Optional sharing
    if share_with:
        try:
            for email in share_with:
                drive.permissions().create(
                    fileId=doc_id,
                    body={"type": "user", "role": "writer", "emailAddress": email},
                    sendNotificationEmail=False,
                    supportsAllDrives=True,
                ).execute()
        except Exception as e:
            console.print(f"[yellow]Warning:[/yellow] failed to add some permissions: {e}")

    # Initialize front-matter
    front = {
        "url": url,
        "media_dir": f"Media/GDoc/{doc_id}",
        "pulled_at": None,
        # Ensure Cast fields exist (cast-id, cast-version).
        # cast-vaults/codebases left to the user or hsync to manage.
    }
    front, _ = ensure_cast_fields(front, generate_id=True)

    body = (
        "_This file is generated from Google Docs. "
        "Edit the Google Doc via the link in YAML and run `cast gdoc pull` to refresh._\n"
    )
    write_cast_file(note_path, front, body, reorder=True)

    console.print(f"[green]✔ Created Google Doc[/green]: {url}")
    console.print(f"[green]✔ Wrote note[/green]: {note_path}")


@gdoc_app.command("pull")
def gdoc_pull(
    file: Path = typer.Argument(..., help="Path to local Cast note (Markdown)"),
    extract_images: bool = typer.Option(True, "--extract-images/--no-extract-images"),
):
    """
    Pull Markdown from the linked Google Doc and refresh the local note body.
    """
    root, vault = _get_root_and_vault()
    if not file.exists():
        console.print(f"[red]Not found:[/red] {file}")
        raise typer.Exit(2)

    # Parse current front-matter
    fm, _, has_cast = parse_cast_file(file)
    if fm is None:
        console.print("[red]File lacks YAML front matter.[/red]")
        raise typer.Exit(2)
    source = fm.get("source") or {}
    doc_id = source.get("document_id")
    if not doc_id:
        console.print("[red]source.document_id missing in front matter[/red]")
        raise typer.Exit(2)

    drive, docs = _build_services(root)

    # Export Markdown
    try:
        md = _export_markdown(drive, doc_id)
    except Exception as e:
        console.print(f"[red]Export failed:[/red] {e}")
        raise typer.Exit(2)

    # Extract images
    if extract_images:
        media_rel = source.get("media_dir") or f"media/gdoc/{doc_id}"
        media_abs = (vault / media_rel).resolve()
        md = _extract_data_uris(
            md,
            media_dir_abs=media_abs,
            rel_prefix_from_file=file.parent.resolve(),
            base_name=file.stem,
        )

    # Get revisionId for provenance
    try:
        doc = docs.documents().get(documentId=doc_id).execute()
        rev = doc.get("revisionId")
    except Exception:
        rev = None

    # Update FM
    fm.setdefault("source", {})
    fm["source"]["revision_id"] = rev
    fm["source"]["pulled_at"] = _now_iso()

    write_cast_file(file, fm, md, reorder=True)
    console.print(f"[green]✔ Pulled Doc[/green] {doc_id} → updated {file}")
    if rev:
        console.print(f"  revision_id: {rev}")