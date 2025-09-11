"""Machine-level Cast registry.

Stores installed Cast roots in a per-user registry file so that casts
can discover peers by name across the machine (no per-cast wiring).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

yaml = YAML()

REGISTRY_VERSION = 1


def cast_home_dir() -> Path:
    """Return per-user Cast home (override with CAST_HOME)."""
    env = os.environ.get("CAST_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".cast"


def registry_path() -> Path:
    """Path to registry JSON."""
    return cast_home_dir() / "registry.json"


def _now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _empty_registry() -> dict[str, Any]:
    # Add top-level 'codebases' map (parallel to 'casts').
    return {"version": REGISTRY_VERSION, "updated_at": "", "casts": {}, "codebases": {}}


def load_registry() -> dict[str, Any]:
    path = registry_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        reg = _empty_registry()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(reg, f, indent=2)
        return reg
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_registry(reg: dict[str, Any]) -> None:
    path = registry_path()
    reg["version"] = REGISTRY_VERSION
    reg["updated_at"] = _now_ts()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.casttmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reg, f, indent=2)
    tmp.replace(path)


@dataclass
class CastEntry:
    cast_id: str
    name: str
    root: Path
    # Standardized: content folder is always root / "Cast"
    @property
    def cast_path(self) -> Path:
        return self.root / "Cast"

@dataclass
class CodebaseEntry:
    """Registered codebase root; docs/cast is the sync mount."""
    name: str
    root: Path
    origin_cast: str | None = None  # single home cast for this codebase
    @property
    def docs_cast_path(self) -> Path:
        return self.root / "docs" / "cast"


def _read_cast_config(root: Path) -> tuple[str, str]:
    """Return (cast_id, cast_name) from .cast/config.yaml in root."""
    cfg = root / ".cast" / "config.yaml"
    if not cfg.exists():
        raise FileNotFoundError(f"config.yaml not found at: {cfg}")
    with open(cfg, encoding="utf-8") as f:
        data = yaml.load(f) or {}
    cast_id = data.get("cast-id")
    cast_name = data.get("cast-name")
    if not cast_id or not cast_name:
        raise ValueError("config.yaml missing required fields: cast-id/cast-name")
    return cast_id, cast_name


def register_cast(root: Path) -> CastEntry:
    """
    Register/update a Cast root in the machine registry.

    Invariants enforced:
      • Exactly one entry per cast-id (keyed as before).
      • Exactly one entry per name (new): any other entry that uses the same name is removed.
      • Exactly one entry per root path (new): any other entry that uses the same root is removed.
    """
    root = root.expanduser().resolve()
    cast_id, name = _read_cast_config(root)

    reg = load_registry()
    reg.setdefault("casts", {})

    # Enforce uniqueness by NAME and by ROOT across the registry.
    root_str = str(root)
    to_remove: list[str] = []
    for cid, data in list(reg.get("casts", {}).items()):
        if cid == cast_id:
            # We'll overwrite our own key below; skip here.
            continue
        same_name = data.get("name") == name
        same_root = data.get("root") == root_str
        if same_name or same_root:
            to_remove.append(cid)

    if to_remove:
        for cid in to_remove:
            reg["casts"].pop(cid, None)

    # Upsert our entry by id (canonical)
    reg["casts"][cast_id] = {"name": name, "root": str(root)}
    save_registry(reg)
    return CastEntry(cast_id=cast_id, name=name, root=root)


def _entry_from_reg(cast_id: str, payload: dict[str, Any]) -> CastEntry:
    # Ignore legacy 'vault_location' if present; standardized to "Cast"
    return CastEntry(
        cast_id=cast_id,
        name=payload.get("name", ""),
        root=Path(payload.get("root", "")),
    )


def list_casts() -> list[CastEntry]:
    reg = load_registry()
    out: list[CastEntry] = []
    for cid, data in reg.get("casts", {}).items():
        out.append(_entry_from_reg(cid, data))
    return out


def resolve_cast_by_id(cast_id: str) -> CastEntry | None:
    reg = load_registry()
    data = reg.get("casts", {}).get(cast_id)
    if not data:
        return None
    return _entry_from_reg(cast_id, data)


def resolve_cast_by_name(name: str) -> CastEntry | None:
    reg = load_registry()
    for cid, data in reg.get("casts", {}).items():
        if data.get("name") == name:
            return _entry_from_reg(cid, data)
    return None


def unregister_cast(
    *, cast_id: str | None = None, name: str | None = None, root: Path | None = None
) -> CastEntry | None:
    """
    Remove a Cast from the machine registry.
    You may specify by cast_id, name, or root path.
    Returns the removed CastEntry if found, else None.
    """
    reg = load_registry()
    casts = reg.get("casts", {})
    target_id: str | None = None

    if cast_id and cast_id in casts:
        target_id = cast_id
    elif name:
        for cid, data in casts.items():
            if data.get("name") == name:
                target_id = cid
                break
    elif root:
        root_str = str(root.expanduser().resolve())
        for cid, data in casts.items():
            if data.get("root") == root_str:
                target_id = cid
                break

    if not target_id:
        return None

    payload = casts.pop(target_id)
    reg["casts"] = casts
    save_registry(reg)
    return _entry_from_reg(target_id, payload)


# ---------------------- CODEBASE REGISTRY ----------------------

def register_codebase(name: str, root: Path, origin_cast: str | None = None) -> CodebaseEntry:
    """
    Register/update a Codebase root in the machine registry.
      • Unique by name and root path (last write wins).
      • Validates the expected layout (docs/cast).
    """
    name = (name or "").strip()
    if not name or " " in name:
        raise ValueError("Codebase name must be a non-space string (e.g., 'nuu-core').")
    root = root.expanduser().resolve()
    doc_path = root / "docs" / "cast"
    cast_config_path = root / ".cast"
    if not doc_path.exists():
        # Be strict here; caller can create dirs first for clarity.
        raise FileNotFoundError(f"Expected path not found: {doc_path} (create it and retry)")
    if not cast_config_path.exists():
        # Also require .cast directory in root for new structure
        raise FileNotFoundError(f"Expected .cast directory not found: {cast_config_path} (run 'cast codebase init' first)")

    # If a cast is specified, make sure it's installed (best-effort validation).
    if origin_cast:
        ent = resolve_cast_by_name(origin_cast)
        if not ent:
            raise FileNotFoundError(
                f"Cast '{origin_cast}' not found in registry. Install it first: 'cast install <cast_root>'."
            )

    reg = load_registry()
    reg.setdefault("codebases", {})

    # Remove any other entries that share the same name or root
    to_remove: list[str] = []
    for cb_name, data in list(reg["codebases"].items()):
        same_name = (cb_name == name)
        same_root = (Path(data.get("root", "")).resolve() == root)
        if same_name or same_root:
            to_remove.append(cb_name)
    for cb in to_remove:
        reg["codebases"].pop(cb, None)

    payload = {"root": str(root)}
    if origin_cast:
        payload["origin_cast"] = origin_cast
    reg["codebases"][name] = payload
    save_registry(reg)
    return CodebaseEntry(name=name, root=root, origin_cast=origin_cast)

def list_codebases() -> list[CodebaseEntry]:
    reg = load_registry()
    out: list[CodebaseEntry] = []
    for name, data in reg.get("codebases", {}).items():
        out.append(CodebaseEntry(
            name=name,
            root=Path(data.get("root", "")),
            origin_cast=data.get("origin_cast")
        ))
    return out

def resolve_codebase_by_name(name: str) -> CodebaseEntry | None:
    reg = load_registry()
    data = reg.get("codebases", {}).get(name)
    if not data:
        return None
    return CodebaseEntry(
        name=name,
        root=Path(data.get("root", "")),
        origin_cast=data.get("origin_cast")
    )

def unregister_codebase(*, name: str | None = None, root: Path | None = None) -> CodebaseEntry | None:
    """
    Remove a Codebase from the machine registry by name or root.
    """
    reg = load_registry()
    codebases = reg.get("codebases", {})
    target_name: str | None = None

    if name and name in codebases:
        target_name = name
    elif root:
        root_str = str(root.expanduser().resolve())
        for cb_name, data in codebases.items():
            if data.get("root") == root_str:
                target_name = cb_name
                break
    if not target_name:
        return None

    payload = codebases.pop(target_name)
    reg["codebases"] = codebases
    save_registry(reg)
    return CodebaseEntry(
        name=target_name,
        root=Path(payload.get("root", "")),
        origin_cast=payload.get("origin_cast")
    )
