"""Machine-level Cast registry.

Stores installed Cast roots in a per-user registry file so that vaults
can discover peers by name across the machine (no per-vault wiring).
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
    return {"version": REGISTRY_VERSION, "updated_at": "", "casts": {}}


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
    vault_location: str

    @property
    def vault_path(self) -> Path:
        return self.root / self.vault_location


def _read_cast_config(root: Path) -> tuple[str, str, str]:
    """Return (cast_id, cast_name, cast_location) from .cast/config.yaml in root."""
    cfg = root / ".cast" / "config.yaml"
    if not cfg.exists():
        raise FileNotFoundError(f"config.yaml not found at: {cfg}")
    with open(cfg, encoding="utf-8") as f:
        data = yaml.load(f) or {}
    cast_id = data.get("cast-id")
    cast_name = data.get("cast-name")
    cast_location = data.get("cast-location", "01 Vault")
    if not cast_id or not cast_name:
        raise ValueError("config.yaml missing required fields: cast-id/cast-name")
    return cast_id, cast_name, cast_location


def register_cast(root: Path) -> CastEntry:
    """Register/update a Cast root in the machine registry."""
    root = root.expanduser().resolve()
    cast_id, name, vault_location = _read_cast_config(root)

    reg = load_registry()
    reg.setdefault("casts", {})
    reg["casts"][cast_id] = {
        "name": name,
        "root": str(root),
        "vault_location": vault_location,
    }
    save_registry(reg)
    return CastEntry(cast_id=cast_id, name=name, root=root, vault_location=vault_location)


def _entry_from_reg(cast_id: str, payload: dict[str, Any]) -> CastEntry:
    return CastEntry(
        cast_id=cast_id,
        name=payload.get("name", ""),
        root=Path(payload.get("root", "")),
        vault_location=payload.get("vault_location", "01 Vault"),
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
