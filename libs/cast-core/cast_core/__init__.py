"""Cast Core - parsing, normalization, and digest utilities."""

from cast_core.digest import compute_digest, normalize_yaml_for_digest
from cast_core.models import (
    CastConfig,
    FileRec,
    SyncState,
    SyncStateEntry,
)
from cast_core.registry import (
    cast_home_dir,
    list_casts,
    load_registry,
    register_cast,
    registry_path,
    resolve_cast_by_id,
    resolve_cast_by_name,
    save_registry,
    unregister_cast,
)
from cast_core.yamlio import (
    ensure_cast_fields,
    extract_cast_fields,
    parse_cast_file,
    reorder_cast_fields,
    write_cast_file,
)

__all__ = [
    "compute_digest",
    "normalize_yaml_for_digest",
    # registry
    "cast_home_dir",
    "registry_path",
    "load_registry",
    "save_registry",
    "register_cast",
    "list_casts",
    "resolve_cast_by_name",
    "resolve_cast_by_id",
    "unregister_cast",
    "CastConfig",
    "FileRec",
    "SyncState",
    "SyncStateEntry",
    "parse_cast_file",
    "extract_cast_fields",
    "ensure_cast_fields",
    "reorder_cast_fields",
    "write_cast_file",
]

__version__ = "0.1.0"
