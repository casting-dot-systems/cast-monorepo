"""Google Docs integration for Cast CLI (create, add & pull).

Commands:
  cast gdoc new "<Title>" [--dir RELPATH] [--folder-id FOLDER] [--share-with EMAIL ...] [--auto-pull]
  cast gdoc add <doc_url> [--title TITLE] [--dir RELPATH] [--overwrite/--no-overwrite] [--auto-pull]
  cast gdoc pull [<file.md> | --all]   # Pull one file or every GDoc note (prefixed with '(GDoc) ')

Auth precedence:
  1) Service account via GOOGLE_APPLICATION_CREDENTIALS
  2) OAuth client in .cast/google/client_secret.json (token cached as .cast/google/token.json)
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Tuple
import dotenv

import typer
from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn
from ruamel.yaml import YAML

from cast_core.yamlio import write_cast_file, parse_cast_file, ensure_cast_fields

dotenv.load_dotenv()

gdoc_app = typer.Typer(help="Google Docs integration (create, add & pull)")
console = Console()
yaml_rt = YAML()
yaml_rt.preserve_quotes = True
yaml_rt.default_flow_style = False
yaml_rt.width = 4096

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
DOCS_SCOPES = ["https://www.googleapis.com/auth/documents.readonly"]
SCOPES = DRIVE_SCOPES + DOCS_SCOPES

# Extract a Google Doc ID from its URL
DOC_URL_ID_RE = re.compile(r"/document/d/([a-zA-Z0-9_-]+)")


# -------------------- small utils --------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="minutes")


def _sanitize_filename(name: str) -> str:
    """Mildly sanitize a name for a Markdown filename."""
    s = (name or "").strip()
    # Keep spaces and common punctuation; remove path separators and control chars.
    s = s.replace("/", "-").replace("\\", "-")
    s = re.sub(r"[\x00-\x1f\x7f]", "", s)
    return s


def _get_root_and_vault() -> tuple[Path, Path]:
    """Locate Cast root (contains .cast/) and standardized Cast folder."""
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
    vault = root / "Cast"
    if not vault.exists():
        console.print(f"[red]Cast folder not found at {vault}[/red]")
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
    from google.auth.transport.requests import Request

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
            # Refresh cached OAuth token if needed
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                token.write_text(creds.to_json(), encoding="utf-8")
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
    data = drive.files().export(fileId=doc_id, mimeType="text/markdown").execute()
    return data.decode("utf-8")


def _doc_id_from_url_field(fm: dict) -> Optional[str]:
    """Extract Google Doc ID solely from the 'url' field in front matter."""
    url = (fm or {}).get("url")
    if isinstance(url, str):
        m = DOC_URL_ID_RE.search(url)
        if m:
            return m.group(1)
    return None


def _iter_gdoc_notes(vault: Path) -> Iterable[Path]:
    """Yield Markdown notes that look like GDoc files based on '(GDoc)' prefix."""
    for p in vault.rglob("*.md"):
        try:
            if p.name.lower().startswith("(gdoc) "):
                yield p
        except Exception:
            continue


def _pull_one_note(drive, docs, file: Path) -> Tuple[bool, Optional[str]]:
    """
    Pull Markdown from the linked Google Doc and refresh the local note body.
    Returns (ok, revision_id).
    """
    fm, _, _ = parse_cast_file(file)
    if fm is None:
        console.print(f"[red]File lacks YAML front matter:[/red] {file}")
        return False, None
    # Primary path: derive doc_id from URL
    doc_id = _doc_id_from_url_field(fm)
    # Legacy migration: if URL missing but legacy 'document_id' exists, synthesize URL and migrate
    if not doc_id and isinstance(fm, dict) and fm.get("document_id"):
        legacy_id = str(fm["document_id"])
        # Best-effort canonical URL (fall back to edit URL if needed)
        try:
            url = _canonical_doc_url(drive, legacy_id)
        except Exception:
            url = f"https://docs.google.com/document/d/{legacy_id}/edit"
        fm["url"] = url
        doc_id = legacy_id
    if not doc_id:
        console.print(f"[red]Missing or invalid 'url' in front matter (cannot derive Google Doc ID):[/red] {file}")
        return False, None

    # Export Markdown
    try:
        md = _export_markdown(drive, doc_id)
    except Exception as e:
        console.print(f"[red]Export failed for {file} (doc {doc_id}):[/red] {e}")
        return False, None

    # Get revisionId for provenance (best-effort)
    try:
        doc = docs.documents().get(documentId=doc_id).execute()
        rev = doc.get("revisionId")
    except Exception:
        rev = None

    # Update FM
    fm["last-updated"] = _now_iso()
    # Drop legacy image-related field if present
    fm.pop("media_dir", None)
    # Remove legacy document_id if present (we no longer store or rely on it)
    fm.pop("document_id", None)

    write_cast_file(file, fm, md, reorder=True)
    return True, rev


def _canonical_doc_url(drive, doc_id: str) -> str:
    """Return a stable webViewLink for the Doc; fallback to a standard edit URL."""
    try:
        file = drive.files().get(
            fileId=doc_id,
            fields="webViewLink",
            supportsAllDrives=True
        ).execute()
        url = file.get("webViewLink")
        if url:
            return url
    except Exception:
        pass
    return f"https://docs.google.com/document/d/{doc_id}/edit"


def _fetch_doc_title(docs, doc_id: str) -> Optional[str]:
    """Fetch the Google Doc title (best effort)."""
    try:
        meta = docs.documents().get(documentId=doc_id).execute()
        return meta.get("title")
    except Exception:
        return None


# -------------------- commands --------------------
@gdoc_app.command("add")
def gdoc_add(
    doc_url: str = typer.Argument(..., help="URL to an existing Google Doc"),
    title: Optional[str] = typer.Option(
        None, "--title", "-t", help="Override note title; otherwise use the Doc title"
    ),
    dir: Path = typer.Option(
        Path("."), "--dir", help="Cast-relative directory to place the note"
    ),
    overwrite: bool = typer.Option(
        True, "--overwrite/--no-overwrite", help="Overwrite the note if it already exists"
    ),
    auto_pull: bool = typer.Option(
        True, "--auto-pull/--no-auto-pull", help="Automatically pull content after creating the note"
    ),
):
    """
    Attach an existing Google Doc by URL and create a '(GDoc) <Title>.md' note
    with YAML front-matter (url, last-updated, cast-*) only. The Doc ID is derived from the URL at runtime.
    Optionally auto-pulls the content immediately.
    """
    root, vault = _get_root_and_vault()

    m = DOC_URL_ID_RE.search(doc_url)
    if not m:
        console.print("[red]Could not extract document ID from URL.[/red]")
        raise typer.Exit(2)
    doc_id = m.group(1)

    drive, docs = _build_services(root)

    # Get title if not provided
    if not title:
        title = _fetch_doc_title(docs, doc_id) or doc_id
    safe_title = _sanitize_filename(title)

    note_dir = (vault / dir).resolve()
    note_dir.mkdir(parents=True, exist_ok=True)
    note_path = note_dir / f"(GDoc) {safe_title}.md"
    if note_path.exists() and not overwrite:
        console.print(f"[red]Note already exists:[/red] {note_path} (pass --overwrite to replace)")
        raise typer.Exit(2)

    url = _canonical_doc_url(drive, doc_id)

    # Initialize front-matter
    front = {
        "url": url,
        "last-updated": _now_iso(),
    }
    front, _ = ensure_cast_fields(front, generate_id=True)

    body = (
        "_This file is generated from Google Docs. "
        "Edit the Google Doc via the link in YAML and run `cast gdoc pull` to refresh._\n"
    )
    write_cast_file(note_path, front, body, reorder=True)

    console.print(f"[green]✔ Linked existing Google Doc[/green]: {url}")
    console.print(f"[green]✔ Wrote note[/green]: {note_path}")
    
    # Auto-pull content if requested
    if auto_pull:
        console.print("[blue]Auto-pulling content...[/blue]")
        success, rev = _pull_one_note(drive, docs, note_path)
        if success:
            console.print(f"[green]✔ Content pulled successfully[/green]")
            if rev:
                console.print(f"  revision_id: {rev}")
        else:
            console.print(f"[yellow]⚠ Auto-pull failed - you can manually run:[/yellow] cast gdoc pull {note_path}")


@gdoc_app.command("new")
def gdoc_new(
    title: str = typer.Argument(..., help="Title for the new note & Google Doc"),
    dir: Path = typer.Option(Path("."), "--dir", help="Cast-relative directory for the note"),
    folder_id: Optional[str] = typer.Option(None, "--folder-id", help="Drive folderId for the Doc"),
    share_with: List[str] = typer.Option(
        [], "--share-with", help="Email(s) to grant writer access to the Doc"
    ),
    auto_pull: bool = typer.Option(
        False, "--auto-pull/--no-auto-pull", help="Automatically pull content after creating the Doc"
    ),
):
    """
    Create an empty Google Doc with the same title as the note and link it with a 'url' in YAML.
    Optionally auto-pulls the content immediately (useful if the Doc has initial content).
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
    note_path = (vault / dir).resolve() / f"(GDoc) {safe_title}.md"
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
        # Ensure Cast fields exist (cast-id, cast-version).
        # cast-hsync/codebases left to the user or hsync to manage.
    }
    front, _ = ensure_cast_fields(front, generate_id=True)

    body = (
        "_This file is generated from Google Docs. "
        "Edit the Google Doc via the link in YAML and run `cast gdoc pull` to refresh._\n"
    )
    write_cast_file(note_path, front, body, reorder=True)

    console.print(f"[green]✔ Created Google Doc[/green]: {url}")
    console.print(f"[green]✔ Wrote note[/green]: {note_path}")
    
    # Auto-pull content if requested
    if auto_pull:
        console.print("[blue]Auto-pulling content...[/blue]")
        success, rev = _pull_one_note(drive, docs, note_path)
        if success:
            console.print(f"[green]✔ Content pulled successfully[/green]")
            if rev:
                console.print(f"  revision_id: {rev}")
        else:
            console.print(f"[yellow]⚠ Auto-pull failed - you can manually run:[/yellow] cast gdoc pull {note_path}")


@gdoc_app.command("pull")
def gdoc_pull(
    file: Optional[Path] = typer.Argument(
        None,
        help="Path to local Cast note (Markdown). Omit and use --all to pull every GDoc note."
    ),
    all_: bool = typer.Option(
        False, "--all", "-a", "--a",
        help="Pull all GDoc notes in the cast (files prefixed with '(GDoc) ')."
    ),
):
    """
    Pull Markdown from the linked Google Doc(s) and refresh the local note body/bodies.
    Use either a single <file.md> argument or --all / -a / --a.
    """
    root, vault = _get_root_and_vault()
    drive, docs = _build_services(root)

    # Pull everything
    if all_:
        files = list(_iter_gdoc_notes(vault))
        if not files:
            console.print("[yellow]No '(GDoc) ' notes found in the cast.[/yellow]")
            raise typer.Exit(0)

        console.print(f"[bold]Pulling {len(files)} GDoc note(s)...[/bold]")
        ok = 0
        failed = 0
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
        ) as progress:
            t = progress.add_task("Pulling", total=len(files))
            for p in files:
                success, _rev = _pull_one_note(drive, docs, p)
                ok += 1 if success else 0
                failed += 0 if success else 1
                progress.advance(t, 1)

        console.print(f"[green]✔ Completed[/green] — {ok} succeeded, {failed} failed.")
        raise typer.Exit(0 if failed == 0 else 1)

    # Pull a single file
    if not file:
        console.print("[red]Provide a <file.md> or use --all/-a.[/red]")
        raise typer.Exit(2)
    if not file.exists():
        console.print(f"[red]Not found:[/red] {file}")
        raise typer.Exit(2)

    success, rev = _pull_one_note(drive, docs, file)
    if not success:
        raise typer.Exit(2)
    console.print(f"[green]✔ Updated[/green] {file}")