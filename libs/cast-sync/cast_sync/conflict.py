"""Conflict resolution for Cast Sync."""

import shutil
from enum import Enum
from pathlib import Path


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

    if not interactive:
        # Non-interactive: keep local
        print(f"Conflict in {local_path.name}: keeping LOCAL version")
        print(f"  Conflict files saved to {conflicts_dir}")
        return ConflictResolution.KEEP_LOCAL

    # Interactive prompt
    print(f"\nConflict detected in: {local_path.name}")
    print(f"  Local:  {local_sidecar}")
    print(f"  Peer:   {peer_sidecar}")
    print("\nOptions:")
    print("  1. Keep LOCAL")
    print("  2. Keep PEER")
    print("  3. Skip (resolve manually later)")

    while True:
        choice = input("\nYour choice [1/2/3]: ").strip()
        if choice == "1":
            return ConflictResolution.KEEP_LOCAL
        elif choice == "2":
            return ConflictResolution.KEEP_PEER
        elif choice == "3":
            return ConflictResolution.SKIP
        else:
            print("Invalid choice. Please enter 1, 2, or 3.")
