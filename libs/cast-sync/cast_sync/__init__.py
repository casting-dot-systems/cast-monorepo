"""Cast Sync - 3-way sync engine and conflict handling."""

from cast_sync.conflict import ConflictResolution, handle_conflict
from cast_sync.hsync import HorizontalSync, SyncDecision, SyncPlan
from cast_sync.index import build_ephemeral_index
from cast_sync.cbsync import CodebaseSync

__all__ = [
    "HorizontalSync",
    "SyncDecision",
    "SyncPlan",
    "ConflictResolution",
    "handle_conflict",
    "build_ephemeral_index",
    "CodebaseSync",
]

__version__ = "0.2.2"
