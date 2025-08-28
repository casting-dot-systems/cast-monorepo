"""Cast Sync - 3-way sync engine and conflict handling."""

from cast_sync.conflict import ConflictResolution, handle_conflict
from cast_sync.hsync import HorizontalSync, SyncDecision, SyncPlan
from cast_sync.index import build_ephemeral_index

__all__ = [
    "HorizontalSync",
    "SyncDecision",
    "SyncPlan",
    "ConflictResolution",
    "handle_conflict",
    "build_ephemeral_index",
]

__version__ = "0.1.2"
