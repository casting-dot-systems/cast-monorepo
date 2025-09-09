"""Rename-aware hyperlink rewriting for Obsidian-style vaults.

This module updates *intra-vault* links when files are renamed or moved.
It understands two link styles commonly found in Markdown vaults:

  1) Obsidian wiki links:   [[path/to/Note]] , [[Note#Section|Alias]]
  2) Markdown links:        [text](path/to/Note.md) (relative to the current file)

Design goals
------------
• Stateless & reusable: pass (vault_path, list[RenameSpec]) and it scans once.
• Safe rewriting: only the Markdown **body** is touched; YAML front‑matter
  is preserved byte-for-byte.
• Minimal surprises: preserves existing link style (with/without .md,
  encoded spaces, alias text, anchors, titles).

The module is intentionally independent of sync logic, so future features
(vsync, manual refactors, bulk moves) can reuse it.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import re
import posixpath
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence
from urllib.parse import unquote, quote

# ---------------------- public API ----------------------

@dataclass(frozen=True)
class RenameSpec:
    """One file rename within a vault (relative paths, POSIX separators).

    Args:
        old_rel: Vault-relative path to the old file (e.g. "Notes/Old.md")
        new_rel: Vault-relative path to the new file (e.g. "Docs/New.md")
    """
    old_rel: str
    new_rel: str

    def __post_init__(self):
        # Normalize to POSIX style and strip leading "./"
        object.__setattr__(self, "old_rel", _norm_rel(self.old_rel))
        object.__setattr__(self, "new_rel", _norm_rel(self.new_rel))
        object.__setattr__(self, "old_noext", _remove_md(self.old_rel))
        object.__setattr__(self, "new_noext", _remove_md(self.new_rel))
        object.__setattr__(self, "old_stem", PurePosixPath(self.old_noext).name)
        object.__setattr__(self, "new_stem", PurePosixPath(self.new_noext).name)


@dataclass
class FileChange:
    relpath: str
    replacements: int


@dataclass
class LinkRewriteReport:
    files_changed: int
    total_replacements: int
    changes: list[FileChange]

def _to_posix(s: str) -> str:
    s = s.replace("\\", "/")
    s = re.sub(r"/+", "/", s)
    if s.startswith("./"):
        s = s[2:]
    return s

def _remove_md(path: str) -> str:
    """Drop a trailing .md (case-insensitively) from a path string."""
    return path[:-3] if path.lower().endswith(".md") else path

def _norm_rel(s: str) -> str:
    s = _to_posix(s.strip())
    return s.strip("/")  # ensure vault-relative

def _eq(a: str, b: str, case_sensitive: bool) -> bool:
    return a == b if case_sensitive else a.lower() == b.lower()

def _exists_any(vault_path: Path, rel: str) -> bool:
    """
    Best-effort existence check that tolerates specs with or without .md.
    """
    p = (vault_path / rel)
    if p.exists():
        return True
    if not rel.lower().endswith(".md"):
        q = vault_path / f"{rel}.md"
        if q.exists():
            return True
    return False

def _prepare_specs(
    vault_path: Path,
    renames: Sequence[RenameSpec],
    case_sensitive: bool,
    *,
    flip_reversed: bool = True,
) -> list[RenameSpec]:
    """
    Defensive canonicalization of rename specs to avoid accidental "un-rename".
    - Auto-flip specs that look reversed (source exists, destination missing)
      which commonly happens if the caller passes (new, old) after the move.
    - Drop no-ops and duplicates.
    - Collapse trivial chains (A→B, B→C => A→C).
    - Prefer direction whose destination exists (disambiguates A↔B).
    - Sort by path-length (longest first) to reduce partial-overlap issues.
    """
    if not renames:
        return []

    # 1) Normalize / auto-flip / dedupe
    prelim: list[RenameSpec] = []
    seen: set[tuple[str, str]] = set()
    for spec in renames:
        s = spec
        # Skip no-ops
        if _eq(s.old_rel, s.new_rel, True):
            continue
        # Optional: auto-correct reversed specs. Only do this when explicitly enabled.
        if flip_reversed:
            try:
                old_exists = _exists_any(vault_path, s.old_rel)
                new_exists = _exists_any(vault_path, s.new_rel)
            except Exception:
                old_exists = new_exists = False
            # Heuristic: passed spec looks like (new → old) after a move
            # (i.e., "old_rel" exists and "new_rel" doesn't).
            if old_exists and not new_exists:
                s = RenameSpec(s.new_rel, s.old_rel)
        key = (s.old_rel, s.new_rel)
        if key in seen:
            continue
        seen.add(key)
        prelim.append(s)

    if not prelim:
        return []

    # 2) Collapse chains A->B->C => A->C (operate on exact rels)
    mapping: dict[str, str] = {s.old_rel: s.new_rel for s in prelim}
    def _follow(x: str) -> str:
        visited: set[str] = set()
        cur = x
        while cur in mapping and cur not in visited:
            visited.add(cur)
            cur = mapping[cur]
        return cur
    collapsed: dict[str, str] = {}
    for s in prelim:
        collapsed[s.old_rel] = _follow(s.new_rel)

    # 3) Resolve obvious inverses by preferring the direction whose dest exists
    result: list[RenameSpec] = []
    for old, new in collapsed.items():
        if any(_eq(new, o2, True) and _eq(old, n2, True) for o2, n2 in collapsed.items()):
            # inverse present; prefer the one whose destination exists
            keep = (old, new)
            inv = (new, old)
            keep_new_exists = _exists_any(vault_path, keep[1])
            inv_new_exists = _exists_any(vault_path, inv[1])
            if inv_new_exists and not keep_new_exists:
                keep = inv
            result.append(RenameSpec(*keep))
        else:
            result.append(RenameSpec(old, new))

    # 4) Stable + longest-first for safer rewrites
    result_unique: dict[tuple[str, str], RenameSpec] = {}
    for s in result:
        result_unique[(s.old_rel, s.new_rel)] = s
    ordered = list(result_unique.values())
    ordered.sort(key=lambda s: (len(s.old_rel), len(s.old_stem)), reverse=True)
    return ordered


def update_links_for_renames(
    vault_path: Path,
    renames: Sequence[RenameSpec],
    *,
    case_sensitive: bool | None = None,
    exclude_files: Iterable[Path] | None = None,
    flip_reversed: bool = True,
) -> LinkRewriteReport:
    """Rewrite links across the vault for a set of renames.

    Only Markdown files (*.md) are scanned. For each file:
      • YAML front-matter block (if present) is preserved verbatim.
      • Only the Markdown *body* is modified.

    This function defensively corrects reversed specs (e.g., caller passed
    (new, old) after the move) to avoid "un-renaming" links.

    Returns:
        LinkRewriteReport containing aggregate counts and per-file changes.
    """
    if case_sensitive is None:
        # NT filesystems are commonly case-insensitive; default accordingly.
        case_sensitive = (os.name != "nt")

    vault_path = vault_path.resolve()
    exclude_abs = {p.resolve() for p in (exclude_files or [])}

    # Canonicalize specs up front
    renames = _prepare_specs(
        vault_path,
        list(renames),
        case_sensitive,
        flip_reversed=flip_reversed,
    )
    if not renames:
        return LinkRewriteReport(files_changed=0, total_replacements=0, changes=[])

    files_changed = 0
    total_replacements = 0
    changes: list[FileChange] = []

    for md_file in vault_path.rglob("*.md"):
        if md_file.resolve() in exclude_abs:
            continue

        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception:
            continue

        header, body, _has_fm = _split_front_matter(content)
        cur_rel = _to_posix(str(md_file.relative_to(vault_path)))
        cur_dir = posixpath.dirname(cur_rel)  # '' for root

        new_body, n = _rewrite_body(body, renames, cur_dir, case_sensitive)
        if n > 0:
            # Reassemble, preserving YAML exactly as read
            new_content = f"{header}{new_body}"
            tmp = md_file.parent / f".{md_file.name}.casttmp"
            tmp.write_text(new_content, encoding="utf-8")
            tmp.replace(md_file)
            files_changed += 1
            total_replacements += n
            changes.append(FileChange(relpath=cur_rel, replacements=n))

    return LinkRewriteReport(files_changed=files_changed, total_replacements=total_replacements, changes=changes)


# ---------------------- internals ----------------------

# Simple front-matter splitter that preserves the original YAML bytes.
_FM_RE = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?", re.DOTALL)
_WIKI_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")  # [[...]]
_MDLINK_RE = re.compile(r"(?<!\!)\[(?P<text>[^\]]*?)\]\((?P<url>[^)]+?)\)")  # [text](url) but not image ![...](...)


def _split_front_matter(content: str) -> tuple[str, str, bool]:
    m = _FM_RE.match(content)
    if not m:
        return "", content, False
    return content[: m.end()], content[m.end() :], True


def _posix_join_norm(base: str, rel: str) -> str:
    # Join and normalize '..' and '.' components without touching filesystem
    return posixpath.normpath(posixpath.join("" if base == "" else base, rel))


def _should_skip_url(u: str) -> bool:
    u = u.strip()
    if u.startswith("#"):
        return True  # page-local anchor
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", u):
        return True  # absolute URL
    if u.startswith("mailto:"):
        return True
    return False


def _rewrite_body(
    body: str, renames: Sequence[RenameSpec], cur_rel_dir: str, case_sensitive: bool
) -> tuple[str, int]:
    """Apply all renames to the body; returns (new_body, replacements)."""
    total = 0
    new_body = body
    for spec in renames:
        new_body, c1 = _rewrite_wiki(new_body, spec, case_sensitive)
        new_body, c2 = _rewrite_mdlinks(new_body, spec, cur_rel_dir, case_sensitive)
        total += (c1 + c2)
    return new_body, total


def _rewrite_wiki(body: str, spec: RenameSpec, case_sensitive: bool) -> tuple[str, int]:
    """
    Rewrite Obsidian wiki links that point to the renamed file.

    Rules:
      • If the link contains a path (e.g., [[Folder/Old]]), replace path with new path (no .md).
      • If the link is bare name ([[Old]]) and the file's *name* changed, replace with [[New]].
      • Keep anchors (#Heading) and aliases (|Alias) intact.
    """
    out: list[str] = []
    last = 0
    count = 0

    for m in _WIKI_RE.finditer(body):
        inner = m.group(1)
        # Split alias
        alias = None
        target_part = inner
        if "|" in inner:
            target_part, alias = inner.split("|", 1)

        # Split anchor
        anchor = ""
        if "#" in target_part:
            path_part, anchor_part = target_part.split("#", 1)
            anchor = "#" + anchor_part
        else:
            path_part = target_part

        # Normalize
        target_norm = _to_posix(path_part.strip())
        target_noext = _remove_md(target_norm)

        should_change = False
        new_target = None

        # If the target has a path component (contains '/'), enforce full path match.
        # Otherwise (bare name), only replace when the filename itself changed.
        if "/" in target_noext:
            if _eq(target_noext, spec.old_noext, case_sensitive):
                should_change = True
                new_target = spec.new_noext
        else:
            if _eq(target_noext, spec.old_stem, case_sensitive) and not _eq(
                spec.old_stem, spec.new_stem, case_sensitive
            ):
                should_change = True
                new_target = spec.new_stem

        if should_change and new_target is not None:
            inner_new = new_target + anchor
            if alias is not None:
                inner_new = f"{inner_new}|{alias}"
            repl = f"[[{inner_new}]]"
            out.append(body[last : m.start()] + repl)
            last = m.end()
            count += 1

    out.append(body[last:])
    return "".join(out), count


def _rewrite_mdlinks(
    body: str, spec: RenameSpec, cur_rel_dir: str, case_sensitive: bool
) -> tuple[str, int]:
    """
    Rewrite regular Markdown links pointing at the renamed file.

    Resolution rules:
      • The link URL is resolved *relative to the current file's directory*.
      • If that resolves to the old path (with or without `.md`), it is rewritten
        to the new path relative to the current file.
      • Preserves: .md presence, anchors (#), query (?x=y), angle-bracketed URLs,
        and optional link titles (e.g., [text](url "title")).
    """
    out: list[str] = []
    last = 0
    count = 0

    for m in _MDLINK_RE.finditer(body):
        text = m.group("text")
        url = m.group("url")
        if _should_skip_url(url):
            continue

        u = url.strip()

        # Drop surrounding angle brackets first (before parsing components)
        had_angle = False
        if u.startswith("<") and u.endswith(">"):
            u = u[1:-1]
            had_angle = True

        # Heuristic to peel off an optional title at the end: [text](url "title")
        title = None
        pos_quote = max(u.rfind('"'), u.rfind("'"))
        if pos_quote != -1:
            pos_space = u.rfind(" ", 0, pos_quote)
            if pos_space != -1:
                title = u[pos_space + 1 :].strip()
                u = u[:pos_space].rstrip()

        # Split query and anchor (in that order)
        query = ""
        anchor = ""
        if "?" in u:
            path_part, query_part = u.split("?", 1)
            query = "?" + query_part
        else:
            path_part = u
        if "#" in path_part:
            path_inner, anchor_part = path_part.split("#", 1)
            anchor = "#" + anchor_part
        else:
            path_inner = path_part

        # Decode percent-escapes for matching; keep original encoded/angle style on output
        decoded_inner = unquote(path_inner)
        norm_path = _to_posix(decoded_inner)
        resolved = _posix_join_norm(cur_rel_dir, norm_path)  # vault-relative
        resolved_noext = _remove_md(resolved)

        orig_has_ext = decoded_inner.lower().endswith(".md")

        is_match = _eq(resolved_noext, spec.old_noext, case_sensitive) or (
            orig_has_ext and _eq(resolved, spec.old_rel, case_sensitive)
        )
        if not is_match:
            continue

        # Compute new relative path from the current file's directory
        new_rel_from_cur = _to_posix(posixpath.relpath(spec.new_rel, start=cur_rel_dir or "."))
        if new_rel_from_cur.startswith("./"):
            new_rel_from_cur = new_rel_from_cur[2:]

        # Preserve extension style
        repl_path = new_rel_from_cur
        if not orig_has_ext and repl_path.lower().endswith(".md"):
            repl_path = repl_path[:-3]

        # Preserve original encoding style (encode spaces etc. if % was present)
        if "%" in path_inner or "%20" in path_inner:
            repl_path = quote(repl_path, safe="/@:+-._~")

        new_url = repl_path + anchor + query
        if had_angle:
            new_url = f"<{new_url}>"
        if title:
            new_url = f"{new_url} {title}"
        repl = f"[{text}]({new_url})"

        out.append(body[last : m.start()] + repl)
        last = m.end()
        count += 1

    out.append(body[last:])
    return "".join(out), count