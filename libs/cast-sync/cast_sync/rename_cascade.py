"""Rename cascade: rewrite inbound links when a note is renamed.

Supported link forms:
  • Obsidian-style wiki links: [[Path/Name]], [[Path/Name#H2|Alias]]
  • Markdown links: [text](relative/path.md), preserving #fragment or ?query

We only rewrite if the link resolves to the exact old file path.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import unquote, quote

# [[path( no '|' or '#' ) [#anchor] [|alias] ]]
WIKI_LINK_RE = re.compile(
    r"\[\[\s*(?P<path>[^\]|#]+)"
    r"(?P<rest>(?:#[^\]|]+)?(?:\|[^\]]+)?)\s*\]\]"
)

# Markdown links/images: !?[text](url)
# We don't treat images specially but keep the optional '!' so we can leave it untouched.
MD_LINK_RE = re.compile(
    r"(?P<img>!?)\[(?P<text>[^\]]*)\]\((?P<url>[^)]+)\)"
)

_SAFE_URL_CHARS = "/-_.~()[]'@:+,;=&%!$*"


def _posix(s: str) -> str:
    return s.replace("\\", "/")


def _strip_md_ext(rel: str) -> str:
    return rel[:-3] if rel.lower().endswith(".md") else rel


def _abs_norm(p: Path) -> Path:
    # Resolve without failing if the path doesn't exist (best-effort)
    try:
        return p.resolve()
    except Exception:
        return Path(os.path.abspath(str(p)))


def _md_relpath(from_dir: Path, to_abs: Path) -> str:
    rel = os.path.relpath(str(to_abs), str(from_dir))
    return _posix(rel)


def apply_rename_cascade(vault_path: Path, old_rel: str, new_rel: str) -> int:
    """
    Rewrite inbound links across the vault.

    Args:
      vault_path: path to the vault folder
      old_rel:    old vault-relative path to the note (with .md)
      new_rel:    new vault-relative path to the note (with .md)

    Returns: number of files changed.
    """
    old_rel = _posix(old_rel)
    new_rel = _posix(new_rel)
    old_abs = _abs_norm(vault_path / old_rel)
    new_abs = _abs_norm(vault_path / new_rel)

    changed = 0
    for md in vault_path.rglob("*.md"):
        try:
            original = md.read_text(encoding="utf-8")
        except Exception:
            continue
        text = original

        # --- Wiki links ---
        def _repl_wiki(m: re.Match) -> str:
            path = m.group("path").strip()
            rest = m.group("rest") or ""
            # Treat wiki link path as vault-relative, extensionless
            cand_abs = _abs_norm(vault_path / f"{path}.md")
            if cand_abs == old_abs:
                return f"[[{_strip_md_ext(new_rel)}{rest}]]"
            return m.group(0)

        text = WIKI_LINK_RE.sub(_repl_wiki, text)

        # --- Markdown links ---
        def _repl_md(m: re.Match) -> str:
            url = m.group("url").strip()
            lower = url.lower()
            # Skip external / special schemes and anchor-only links
            if "://" in lower or lower.startswith(("mailto:", "obsidian:", "#")):
                return m.group(0)

            # Split off ?query and/or #fragment (preserve order: first '?' then '#')
            suffix = ""
            path_part = url
            for ch in ("?", "#"):
                if ch in path_part:
                    idx = path_part.find(ch)
                    suffix = path_part[idx:]
                    path_part = path_part[:idx]
                    break

            # Only consider .md targets (keep others untouched)
            if not path_part.lower().endswith(".md"):
                return m.group(0)

            # Decode percent-encoding for comparison
            path_dec = unquote(path_part)
            cand_abs = _abs_norm(md.parent / Path(path_dec))
            if cand_abs != old_abs:
                return m.group(0)

            new_rel_to_file = _md_relpath(md.parent, new_abs)
            new_url = quote(new_rel_to_file, safe=_SAFE_URL_CHARS)
            return m.group(0).replace(url, new_url + suffix)

        text = MD_LINK_RE.sub(_repl_md, text)

        if text != original:
            try:
                md.write_text(text, encoding="utf-8")
                changed += 1
            except Exception:
                # Best-effort: if write fails, skip counting it
                pass

    return changed