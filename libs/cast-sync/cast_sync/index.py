"""Ephemeral index building for casts."""

import logging
from pathlib import Path

from cast_core import (
    compute_digest,
    ensure_cast_fields,
    extract_cast_fields,
    parse_cast_file,
    write_cast_file,
)
from cast_core.models import FileRec
from cast_core.yamlio import parse_hsync_entries, reorder_cast_fields

logger = logging.getLogger(__name__)


class EphemeralIndex:
    """In-memory index of a cast's files."""

    def __init__(self, vault_path: Path):
        self.vault_path = vault_path
        self.by_id: dict[str, FileRec] = {}
        self.by_path: dict[str, str] = {}  # relpath -> cast_id

    def add_file(self, rec: FileRec) -> None:
        """Add a file record to the index."""
        self.by_id[rec["cast_id"]] = rec
        self.by_path[rec["relpath"]] = rec["cast_id"]

    def get_by_id(self, cast_id: str) -> FileRec | None:
        """Get file record by cast-id."""
        return self.by_id.get(cast_id)

    def get_by_path(self, relpath: str) -> FileRec | None:
        """Get file record by relative path."""
        cast_id = self.by_path.get(relpath)
        return self.by_id.get(cast_id) if cast_id else None

    def all_peers(self) -> set[str]:
        """Get all unique peer names referenced."""
        peers = set()
        for rec in self.by_id.values():
            peers.update(rec["peers"].keys())
        return peers

    def all_codebases(self) -> set[str]:
        """Get all unique codebase names referenced."""
        codebases = set()
        for rec in self.by_id.values():
            codebases.update(rec["codebases"])
        return codebases


def build_ephemeral_index(
    root_path: Path, vault_path: Path, fixup: bool = True, limit_file: str | None = None
) -> EphemeralIndex:
    """
    Build an ephemeral index of cast files in a cast folder.

    Args:
        root_path: Path to Cast root (contains .cast/)
        vault_path: Path to cast folder
        fixup: Whether to fix missing cast-id and reorder fields
        limit_file: Optional cast-id or relpath to limit indexing to one file

    Returns:
        EphemeralIndex instance
    """
    index = EphemeralIndex(vault_path)

    # Find all Markdown files
    md_files: list[Path] = []
    if limit_file:
        # Normalize limit_file to a path relative to the cast folder, supporting:
        #  - absolute paths under the cast folder
        #  - callers that include the cast folder prefix (e.g. "Cast/foo.md")
        lf = Path(limit_file)
        candidates: list[Path] = []

        if lf.is_absolute():
            try:
                candidates.append(lf.relative_to(vault_path))
            except ValueError:
                # Not under this cast folder; leave empty so we fall back to id lookup.
                pass
        else:
            if lf.parts and lf.parts[0] == vault_path.name:
                candidates.append(Path(*lf.parts[1:]))
            candidates.append(lf)

        for rel in candidates:
            cand = vault_path / rel
            if cand.exists():
                md_files = [cand]
                break

        if not md_files:
            # Maybe limit_file was a cast-id; scan all and resolve by id below.
            md_files = list(vault_path.rglob("*.md"))
    else:
        md_files = list(vault_path.rglob("*.md"))

    for md_path in md_files:
        try:
            # Parse file
            front_matter, body, has_cast_fields = parse_cast_file(md_path)

            if not has_cast_fields:
                continue

            # Ensure cast fields and canonicalize order (including last-updated first)
            if fixup and front_matter:
                modified = False
                # Legacy migration: cast-vaults -> cast-hsync
                if "cast-vaults" in front_matter and "cast-hsync" not in front_matter:
                    front_matter["cast-hsync"] = front_matter.pop("cast-vaults")
                    modified = True
                front_matter, fields_modified = ensure_cast_fields(front_matter, generate_id=True)
                modified = modified or fields_modified

                # Determine if YAML needs reordering even if no fields were added
                need_reorder = False
                try:
                    keys = list(front_matter.keys())
                    # Enforce 'last-updated' first when present
                    if "last-updated" in front_matter:
                        if not keys or keys[0] != "last-updated":
                            need_reorder = True
                    # Enforce canonical cast-* ordering
                    # Compare current key order to reorder_cast_fields result
                    reordered = reorder_cast_fields(dict(front_matter))
                    if list(reordered.keys()) != keys:
                        need_reorder = True
                except Exception:
                    # If we can't inspect order, be conservative and rewrite
                    need_reorder = True

                if modified or need_reorder:
                    write_cast_file(md_path, front_matter, body, reorder=True)
                    logger.info(f"Fixed cast fields/order in {md_path}")

            if not front_matter or "cast-id" not in front_matter:
                continue

            # Extract cast fields
            cast_fields = extract_cast_fields(front_matter)
            cast_id = cast_fields.get("cast-id", "")

            # Parse hsync entries
            hsync_entries = cast_fields.get("cast-hsync") or cast_fields.get("cast-vaults", [])
            peers = parse_hsync_entries(hsync_entries)

            # Get codebases
            codebases = cast_fields.get("cast-codebases", [])
            if not isinstance(codebases, list):
                codebases = []

            # Compute digest
            digest = compute_digest(front_matter, body)

            # Create record
            relpath = str(md_path.relative_to(vault_path))
            rec: FileRec = {
                "cast_id": cast_id,
                "relpath": relpath,
                "digest": digest,
                "peers": peers,
                "codebases": codebases,
            }

            index.add_file(rec)

            # If we were looking for a specific file by cast-id
            if limit_file and cast_id == limit_file:
                break

        except Exception as e:
            logger.warning(f"Error indexing {md_path}: {e}")
            continue

    return index
