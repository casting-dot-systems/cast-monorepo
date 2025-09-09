"""Cast Sync - 3-way sync engine and conflict handling."""

from cast_sync.conflict import ConflictResolution, handle_conflict
from cast_sync.hsync import HorizontalSync, SyncDecision, SyncPlan
from cast_sync.index import build_ephemeral_index
from cast_sync.rename import RenameSpec, LinkRewriteReport, update_links_for_renames

__all__ = [
    "HorizontalSync",
    "SyncDecision",
    "SyncPlan",
    "ConflictResolution",
    "handle_conflict",
    "build_ephemeral_index",
    # rename utils
    "RenameSpec",
    "LinkRewriteReport",
    "update_links_for_renames",
]

__version__ = "0.1.3"
