from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any


def write_file(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def read_file(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _normalize_peers(peers: Iterable[Any]) -> list[str]:
    """
    Accepts:
      - ["CastA", "CastB (watch)"]
      - [("CastA","live"), ("CastB","watch")]
    Returns a list of strings in "Name (mode)" format.
    """
    out: list[str] = []
    for it in peers or []:
        if isinstance(it, str):
            if "(" in it:
                out.append(it)
            else:
                out.append(f"{it} (live)")
        else:
            name, mode = it
            out.append(f"{name} ({mode})")
    return out


def mk_note(
    cast_id: str,
    title: str,
    body: str,
    *,
    peers: Iterable[Any] | None = None,
    extra_fm: dict[str, Any] | None = None,
) -> str:
    """Create a markdown note with YAML front matter including cast-* fields."""
    lines = [
        "---",
        f"cast-id: {cast_id}",
        "cast-hsync:",
    ]
    for e in _normalize_peers(peers or []):
        lines.append(f"- {e}")
    lines.extend(
        [
            "cast-version: 1",
            f"title: {title}",
        ]
    )
    for k, v in (extra_fm or {}).items():
        lines.append(f"{k}: {v}")
    lines.extend(["---", body, ""])
    return "\n".join(lines)
