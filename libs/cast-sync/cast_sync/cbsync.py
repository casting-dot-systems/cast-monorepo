"""CodebaseSync - Sync selected Cast files with a Codebase's docs/cast folder.

Behavior:
  • Files participate when their YAML 'cast-codebases' contains <codebase>.
  • Baselines are stored under peer key 'cb:<codebase>'.
  • Decisions mirror HorizontalSync (mode='live' only).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from cast_core import CastConfig, SyncState, SyncStateEntry
from cast_core.registry import resolve_codebase_by_name
from cast_core.yamlio import (
    ensure_cast_fields,
    ensure_codebase_membership,
    parse_cast_file,
    write_cast_file,
)
from cast_sync.index import EphemeralIndex, build_ephemeral_index
from cast_sync.rename_cascade import apply_rename_cascade
from cast_sync.conflict import ConflictResolution, handle_conflict

logger = logging.getLogger(__name__)


class CBDecision(Enum):
    NO_OP = "no_op"
    PULL = "pull"
    PUSH = "push"
    CONFLICT = "conflict"
    DELETE_LOCAL = "delete_local"
    DELETE_REMOTE = "delete_peer"
    CREATE_LOCAL = "create_local"
    CREATE_REMOTE = "create_peer"
    RENAME_LOCAL = "rename_local"
    RENAME_REMOTE = "rename_peer"


@dataclass
class CBPlan:
    cast_id: str
    local_path: Path
    remote_path: Optional[Path]
    remote_root: Optional[Path]
    decision: CBDecision
    local_digest: str | None
    remote_digest: str | None
    baseline_digest: str | None
    rename_to: Optional[Path] = None


@dataclass
class CBSummaryItem:
    action: str
    cast_id: str
    local_rel: str
    remote_rel: Optional[str] = None
    detail: Optional[str] = None


@dataclass
class CBSummary:
    started: str
    finished: str
    counts: dict[str, int]
    items: list[CBSummaryItem]
    conflicts_open: int
    conflicts_resolved: int


class CodebaseSync:
    """Single-codebase sync coordinator (no cascade)."""

    def __init__(self, root_path: Path):
        self.root_path = root_path
        self.cast_dir = root_path / ".cast"
        self.vault_path = root_path / "Cast"
        self.config = self._load_config()
        self.syncstate = self._load_syncstate()
        self.summary: Optional[CBSummary] = None
        self.last_plans: list[CBPlan] = []

    def _rename_cascade(self, base: Path, old_rel: str, new_rel: str, scope: str) -> None:
        """Best-effort inbound link rewrite after renames."""
        try:
            n = apply_rename_cascade(base, old_rel, new_rel)
            if n:
                logger.info(f"Rename cascade ({scope}): updated {n} file(s)")
        except Exception as e:
            logger.warning(f"Rename cascade failed ({scope}): {e}")

    def _normalize_remote_membership(self, remote_vault: Path, codebase_name: str) -> int:
        """
        Ensure every Markdown file in the codebase (docs/cast) participates in Cast:
          - add YAML front-matter if missing,
          - ensure cast-id/version present,
          - ensure 'cast-codebases: [codebase_name]' and 'cast-hsync: ["<this-cast> (live)"]'
        Returns number of files updated.
        """
        fixed = 0
        for p in remote_vault.rglob("*.md"):
            try:
                fm, body, has_cast_fields = parse_cast_file(p)
                if fm is None:
                    # No YAML front-matter at all - create from scratch
                    fm = {}
                    fm, _ = ensure_cast_fields(fm, generate_id=True)
                    fm, _ = ensure_codebase_membership(
                        fm, codebase=codebase_name, origin_cast=self.config.cast_name
                    )
                    write_cast_file(p, fm, body, reorder=True)
                    fixed += 1
                    continue
                # Has YAML front-matter (cast or non-cast) — preserve existing and ensure cast fields
                if isinstance(fm, dict):
                    # First ensure cast-id and cast-version are present
                    fm_with_fields, fields_changed = ensure_cast_fields(fm, generate_id=True)
                    # Then ensure codebase membership
                    fm2, membership_changed = ensure_codebase_membership(
                        fm_with_fields, codebase=codebase_name, origin_cast=self.config.cast_name
                    )
                    if fields_changed or membership_changed:
                        write_cast_file(p, fm2, body, reorder=True)
                        fixed += 1
            except Exception:
                # Best-effort; skip problematic files
                continue
        return fixed

    # ---- IO helpers (mirrors HorizontalSync) -------------------
    def _load_config(self) -> CastConfig:
        path = self.cast_dir / "config.yaml"
        if not path.exists():
            raise FileNotFoundError(f"Cast not initialized: {path} not found")
        import ruamel.yaml
        y = ruamel.yaml.YAML()
        with open(path, encoding="utf-8") as f:
            data = y.load(f)
        return CastConfig(**data)

    def _load_syncstate(self) -> SyncState:
        path = self.cast_dir / "syncstate.json"
        if not path.exists():
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            return SyncState(version=1, updated_at=now, baselines={})
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        baselines = {}
        for cast_id, peers in data.get("baselines", {}).items():
            baselines[cast_id] = {}
            for peer_name, entry in peers.items():
                baselines[cast_id][peer_name] = SyncStateEntry(**entry)
        return SyncState(
            version=data.get("version", 1),
            updated_at=data.get("updated_at", ""),
            baselines=baselines,
        )

    def _save_syncstate(self) -> None:
        path = self.cast_dir / "syncstate.json"
        data = {
            "version": self.syncstate.version,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "baselines": {},
        }
        for cast_id, peers in self.syncstate.baselines.items():
            data["baselines"][cast_id] = {}
            for peer_name, entry in peers.items():
                row = {"digest": entry.digest, "ts": entry.ts}
                if getattr(entry, "rel", None):
                    row["rel"] = entry.rel
                if getattr(entry, "peer_rel", None):
                    row["peer_rel"] = entry.peer_rel
                data["baselines"][cast_id][peer_name] = row
        tmp = path.parent / f".{path.name}.casttmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(path)

    # ---- utility ------------------------------------------------
    @staticmethod
    def _rel(base: Path, p: Path) -> str:
        try:
            return str(p.relative_to(base))
        except Exception:
            return p.name

    def _update_baseline(
        self, cast_id: str, peer_key: str, digest: str,
        *, local_rel: Optional[str] = None, remote_rel: Optional[str] = None
    ) -> None:
        if cast_id not in self.syncstate.baselines:
            self.syncstate.baselines[cast_id] = {}
        entry = self.syncstate.baselines[cast_id].get(peer_key)
        if entry is None:
            entry = SyncStateEntry(digest=digest, ts=datetime.now().strftime("%Y-%m-%d %H:%M"))
            self.syncstate.baselines[cast_id][peer_key] = entry
        entry.digest = digest
        entry.ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        if local_rel is not None:
            entry.rel = local_rel
        if remote_rel is not None:
            entry.peer_rel = remote_rel

    # ---- decisions (subset of HorizontalSync; mode='live') -----
    def _decide(
        self,
        cast_id: str,
        local_rec: Optional[dict],
        remote_rec: Optional[dict],
        baseline: Optional[SyncStateEntry],
    ) -> CBDecision:
        if local_rec is None and remote_rec is None:
            return CBDecision.NO_OP
        # first contact
        if baseline is None:
            if local_rec and remote_rec:
                if local_rec["digest"] == remote_rec["digest"]:
                    # resolve path mismatches by renaming remote to local
                    if local_rec["relpath"] != remote_rec["relpath"]:
                        return CBDecision.RENAME_REMOTE
                    return CBDecision.NO_OP
                return CBDecision.CONFLICT
            if local_rec and not remote_rec:
                return CBDecision.CREATE_REMOTE
            if remote_rec and not local_rec:
                return CBDecision.CREATE_LOCAL

        # with baseline
        b = baseline.digest if baseline else None
        if local_rec is None and remote_rec is not None:
            # deleted locally
            if remote_rec["digest"] == b:
                return CBDecision.DELETE_REMOTE  # propagate deletion
            return CBDecision.CONFLICT
        if remote_rec is None and local_rec is not None:
            # deleted remotely
            if local_rec["digest"] == b:
                return CBDecision.DELETE_LOCAL
            return CBDecision.CONFLICT

        # both present
        ld = local_rec["digest"]
        rd = remote_rec["digest"]
        local_moved = bool(baseline and baseline.rel and (local_rec["relpath"] != baseline.rel))
        remote_moved = bool(baseline and baseline.peer_rel and (remote_rec["relpath"] != baseline.peer_rel))

        if ld == b and rd == b:
            if local_moved and not remote_moved:
                return CBDecision.RENAME_REMOTE
            if remote_moved and not local_moved:
                return CBDecision.RENAME_LOCAL
            if local_moved and remote_moved and local_rec["relpath"] != remote_rec["relpath"]:
                return CBDecision.CONFLICT
            return CBDecision.NO_OP

        if ld == b and rd != b:
            return CBDecision.PULL
        if rd == b and ld != b:
            return CBDecision.PUSH

        if ld != rd:
            return CBDecision.CONFLICT
        # digests equal but paths differ
        if local_rec["relpath"] != remote_rec["relpath"]:
            return CBDecision.RENAME_REMOTE
        return CBDecision.NO_OP

    # ---- main ---------------------------------------------------
    def sync(
        self,
        codebase_name: str,
        *,
        file_filter: Optional[str] = None,
        dry_run: bool = False,
        non_interactive: bool = True,
        debug: bool = False,
    ) -> int:
        entry = resolve_codebase_by_name(codebase_name)
        if not entry:
            raise FileNotFoundError(f"Codebase '{codebase_name}' not found in registry. Run 'cast codebase install'.")
        remote_vault = entry.docs_cast_path
        if not remote_vault.exists():
            raise FileNotFoundError(f"Codebase path missing: {remote_vault}")

        self.summary = CBSummary(
            started=datetime.now().strftime("%Y-%m-%d %H:%M"),
            finished="",
            counts={},
            items=[],
            conflicts_open=0,
            conflicts_resolved=0,
        )
        peer_key = f"cb:{codebase_name}"

        # build index (limit_file same semantics as hsync)
        local_index = build_ephemeral_index(self.root_path, self.vault_path, fixup=True, limit_file=file_filter)
        # Normalize codebase files so they all participate (agents may leave out YAML)
        _n = self._normalize_remote_membership(remote_vault, codebase_name)
        if _n:
            logger.info(f"cbsync: normalized {_n} file(s) in codebase '{codebase_name}'")
        remote_index = build_ephemeral_index(entry.root, remote_vault, fixup=True, limit_file=file_filter)

        # working set = union of cast_ids present where local has codebase membership
        local_ids = {rec["cast_id"] for rec in local_index.by_id.values() if codebase_name in (rec.get("codebases") or [])}
        remote_ids = {rec["cast_id"] for rec in remote_index.by_id.values() if codebase_name in (rec.get("codebases") or [])}

        # --- Pre‑normalize LOCAL files so 'origin' shows up in the cast too ---
        # We only touch files that already participate in this codebase on the local side.
        # If we modify any YAML, rebuild the local index so planning uses correct digests.
        _fixed = 0
        for cid in list(local_ids):
            lrec = local_index.get_by_id(cid)
            if not lrec:
                continue
            lpath = self.vault_path / lrec["relpath"]
            try:
                fm, body, has = parse_cast_file(lpath)
                if not has or not isinstance(fm, dict):
                    continue
                fm2, changed = ensure_codebase_membership(fm, codebase=codebase_name, origin_cast=self.config.cast_name)
                if changed:
                    write_cast_file(lpath, fm2, body, reorder=True)
                    _fixed += 1
            except Exception:
                continue
        if _fixed:
            logger.info(f"cbsync: normalized {_fixed} local file(s) for codebase '{codebase_name}'")
            local_index = build_ephemeral_index(self.root_path, self.vault_path, fixup=True, limit_file=file_filter)
            # recompute local_ids since codebase membership may have been added
            local_ids = {rec["cast_id"] for rec in local_index.by_id.values() if codebase_name in (rec.get("codebases") or [])}

        # if --file provided and points to an id/path that exists only on remote, include it
        if file_filter:
            # detect by path on remote
            rec = remote_index.get_by_path(file_filter) or local_index.get_by_path(file_filter)
            if rec:
                local_ids.add(rec["cast_id"])
                remote_ids.add(rec["cast_id"])

        ids = local_ids.union(remote_ids)

        plans: list[CBPlan] = []
        for cid in sorted(ids):
            lrec = local_index.get_by_id(cid)
            rrec = remote_index.get_by_id(cid)
            baseline = self.syncstate.baselines.get(cid, {}).get(peer_key)
            decision = self._decide(cid, lrec, rrec, baseline)
            # For CREATE_LOCAL operations, prefer the remote filename over cast-id
            if lrec:
                local_path = self.vault_path / lrec["relpath"]
            elif baseline and baseline.rel:
                local_path = self.vault_path / baseline.rel
            elif rrec and decision == CBDecision.CREATE_LOCAL:
                # Use remote filename when creating local file
                local_path = self.vault_path / rrec["relpath"]
            else:
                # Fall back to cast-id filename
                local_path = self.vault_path / f"{cid}.md"
            remote_path = remote_vault / (rrec["relpath"] if rrec else (baseline.peer_rel if baseline and baseline.peer_rel else (lrec["relpath"] if lrec else f"{cid}.md")))
            rename_to = None
            if decision == CBDecision.RENAME_REMOTE and lrec:
                rename_to = remote_vault / lrec["relpath"]
            if decision == CBDecision.RENAME_LOCAL and rrec:
                rename_to = self.vault_path / rrec["relpath"]
            plans.append(CBPlan(
                cast_id=cid,
                local_path=local_path,
                remote_path=remote_path if (rrec or decision in (CBDecision.CREATE_REMOTE, CBDecision.PUSH, CBDecision.DELETE_REMOTE, CBDecision.RENAME_REMOTE)) else None,
                remote_root=entry.root,
                decision=decision,
                local_digest=lrec["digest"] if lrec else None,
                remote_digest=rrec["digest"] if rrec else None,
                baseline_digest=(baseline.digest if baseline else None),
                rename_to=rename_to,
            ))
        self.last_plans = plans[:]

        if dry_run:
            for p in plans:
                if p.decision == CBDecision.NO_OP:
                    continue
                print(f"{p.cast_id[:8]}… → {codebase_name}: {p.decision.value}")
                self.summary.counts[p.decision.value] = self.summary.counts.get(p.decision.value, 0) + 1
            self.summary.finished = datetime.now().strftime("%Y-%m-%d %H:%M")
            return 0

        exit_code = 0
        for p in plans:
            try:
                # helper to refresh YAML invariants when we write to either side
                def _fix_yaml(dest: Path, side: str) -> None:
                    try:
                        fm, body, has = parse_cast_file(dest)
                        if not has:
                            return
                        fm2, changed = ensure_codebase_membership(fm, codebase=codebase_name, origin_cast=self.config.cast_name)
                        if changed:
                            write_cast_file(dest, fm2, body, reorder=True)
                    except Exception:
                        pass

                if p.decision == CBDecision.NO_OP:
                    # if digests equal, refresh baseline paths
                    if p.local_digest and p.remote_digest and p.local_digest == p.remote_digest:
                        self._update_baseline(
                            p.cast_id, peer_key, p.local_digest,
                            local_rel=self._rel(self.vault_path, p.local_path),
                            remote_rel=self._rel(entry.docs_cast_path, p.remote_path) if p.remote_path else None,
                        )
                    continue

                if p.decision == CBDecision.PULL:
                    # adopt remote rename first if needed
                    if p.remote_path and p.local_path.exists():
                        rrel = self._rel(entry.docs_cast_path, p.remote_path)
                        lrel = self._rel(self.vault_path, p.local_path)
                        if rrel and rrel != lrel:
                            target = self.vault_path / rrel
                            target.parent.mkdir(parents=True, exist_ok=True)
                            before = p.local_path
                            shutil.move(str(p.local_path), str(target))
                            p.local_path = target
                            # rename-cascade on LOCAL
                            try:
                                self._rename_cascade(self.vault_path, lrel, rrel, f"codebase pull adopt({codebase_name})")
                            except Exception:
                                pass
                    shutil.copy2(p.remote_path, p.local_path)
                    _fix_yaml(p.local_path, "local")
                    self._update_baseline(
                        p.cast_id, peer_key, p.remote_digest or "",
                        local_rel=self._rel(self.vault_path, p.local_path),
                        remote_rel=self._rel(entry.docs_cast_path, p.remote_path),
                    )
                    self.summary.counts["pull"] = self.summary.counts.get("pull", 0) + 1
                    self.summary.items.append(CBSummaryItem("pull", p.cast_id, self._rel(self.vault_path, p.local_path), self._rel(entry.docs_cast_path, p.remote_path), "remote → local"))

                elif p.decision in (CBDecision.PUSH, CBDecision.CREATE_REMOTE):
                    # adopt local rename on remote before copy
                    if p.remote_path:
                        lrel = self._rel(self.vault_path, p.local_path)
                        rrel = self._rel(entry.docs_cast_path, p.remote_path)
                        if lrel and rrel != lrel:
                            desired = entry.docs_cast_path / lrel
                            desired.parent.mkdir(parents=True, exist_ok=True)
                            if p.remote_path.exists():
                                shutil.move(str(p.remote_path), str(desired))
                            p.remote_path = desired
                            # rename-cascade on REMOTE codebase
                            try:
                                self._rename_cascade(entry.docs_cast_path, rrel, lrel, "codebase push adopt(LOCAL)")
                            except Exception:
                                pass
                    else:
                        # compute expected remote path
                        lrel = self._rel(self.vault_path, p.local_path)
                        p.remote_path = entry.docs_cast_path / lrel
                    p.remote_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p.local_path, p.remote_path)
                    _fix_yaml(p.remote_path, "remote")
                    self._update_baseline(
                        p.cast_id, peer_key, p.local_digest or "",
                        local_rel=self._rel(self.vault_path, p.local_path),
                        remote_rel=self._rel(entry.docs_cast_path, p.remote_path),
                    )
                    key = "create_peer" if p.decision == CBDecision.CREATE_REMOTE else "push"
                    self.summary.counts[key] = self.summary.counts.get(key, 0) + 1
                    self.summary.items.append(CBSummaryItem(key, p.cast_id, self._rel(self.vault_path, p.local_path), self._rel(entry.docs_cast_path, p.remote_path), "local → remote"))

                elif p.decision == CBDecision.CREATE_LOCAL:
                    p.local_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p.remote_path, p.local_path)
                    _fix_yaml(p.local_path, "local")
                    self._update_baseline(
                        p.cast_id, peer_key, p.remote_digest or "",
                        local_rel=self._rel(self.vault_path, p.local_path),
                        remote_rel=self._rel(entry.docs_cast_path, p.remote_path),
                    )
                    self.summary.counts["create_local"] = self.summary.counts.get("create_local", 0) + 1
                    self.summary.items.append(CBSummaryItem("create_local", p.cast_id, self._rel(self.vault_path, p.local_path), self._rel(entry.docs_cast_path, p.remote_path), "remote → local"))

                elif p.decision == CBDecision.DELETE_LOCAL:
                    p.local_path.unlink(missing_ok=True)
                    # clear baseline
                    if p.cast_id in self.syncstate.baselines:
                        self.syncstate.baselines[p.cast_id].pop(peer_key, None)
                        if not self.syncstate.baselines[p.cast_id]:
                            self.syncstate.baselines.pop(p.cast_id)
                    self.summary.counts["delete_local"] = self.summary.counts.get("delete_local", 0) + 1
                    self.summary.items.append(CBSummaryItem("delete_local", p.cast_id, self._rel(self.vault_path, p.local_path), None, "deleted locally (accept remote deletion)"))

                elif p.decision == CBDecision.DELETE_REMOTE:
                    if p.remote_path:
                        p.remote_path.unlink(missing_ok=True)
                    if p.cast_id in self.syncstate.baselines:
                        self.syncstate.baselines[p.cast_id].pop(peer_key, None)
                        if not self.syncstate.baselines[p.cast_id]:
                            self.syncstate.baselines.pop(p.cast_id)
                    self.summary.counts["delete_peer"] = self.summary.counts.get("delete_peer", 0) + 1
                    self.summary.items.append(CBSummaryItem("delete_peer", p.cast_id, self._rel(self.vault_path, p.local_path), self._rel(entry.docs_cast_path, p.remote_path) if p.remote_path else "", "deleted on remote (propagate)"))

                elif p.decision == CBDecision.RENAME_REMOTE and p.remote_path and p.rename_to:
                    p.rename_to.parent.mkdir(parents=True, exist_ok=True)
                    if p.remote_path.exists():
                        before_rel = self._rel(entry.docs_cast_path, p.remote_path)
                        after_rel = self._rel(entry.docs_cast_path, p.rename_to)
                        shutil.move(str(p.remote_path), str(p.rename_to))
                        # cascade on REMOTE
                        try:
                            self._rename_cascade(entry.docs_cast_path, before_rel, after_rel, "codebase rename_peer")
                        except Exception:
                            pass
                    # refresh baseline with new paths
                    self._update_baseline(
                        p.cast_id, peer_key, p.remote_digest or p.local_digest or "",
                        local_rel=self._rel(self.vault_path, p.local_path),
                        remote_rel=self._rel(entry.docs_cast_path, p.rename_to),
                    )
                    self.summary.counts["rename_peer"] = self.summary.counts.get("rename_peer", 0) + 1
                    self.summary.items.append(CBSummaryItem("rename_peer", p.cast_id, self._rel(self.vault_path, p.local_path), self._rel(entry.docs_cast_path, p.rename_to), "remote: rename to match local"))

                elif p.decision == CBDecision.RENAME_LOCAL and p.rename_to:
                    p.rename_to.parent.mkdir(parents=True, exist_ok=True)
                    if p.local_path.exists():
                        before_rel = self._rel(self.vault_path, p.local_path)
                        after_rel = self._rel(self.vault_path, p.rename_to)
                        shutil.move(str(p.local_path), str(p.rename_to))
                        try:
                            self._rename_cascade(self.vault_path, before_rel, after_rel, "codebase rename_local")
                        except Exception:
                            pass
                    p.local_path = p.rename_to
                    self._update_baseline(
                        p.cast_id, peer_key, p.remote_digest or p.local_digest or "",
                        local_rel=self._rel(self.vault_path, p.local_path),
                        remote_rel=self._rel(entry.docs_cast_path, p.remote_path) if p.remote_path else None,
                    )
                    self.summary.counts["rename_local"] = self.summary.counts.get("rename_local", 0) + 1
                    self.summary.items.append(CBSummaryItem("rename_local", p.cast_id, self._rel(self.vault_path, p.local_path), self._rel(entry.docs_cast_path, p.remote_path) if p.remote_path else None, "local: rename to match remote"))

                elif p.decision == CBDecision.CONFLICT:
                    res = handle_conflict(
                        p.local_path,
                        p.remote_path,
                        p.cast_id,
                        f"cb:{codebase_name}",
                        self.root_path,
                        interactive=not non_interactive,
                        local_content=("" if not p.local_path.exists() else None),
                    )
                    if res == ConflictResolution.KEEP_LOCAL:
                        # push local over remote (adopt local name on remote)
                        lrel = self._rel(self.vault_path, p.local_path)
                        desired = entry.docs_cast_path / lrel
                        desired.parent.mkdir(parents=True, exist_ok=True)
                        if p.remote_path and p.remote_path.exists() and desired != p.remote_path:
                            before_rel = self._rel(entry.docs_cast_path, p.remote_path)
                            shutil.move(str(p.remote_path), str(desired))
                            # cascade on REMOTE
                            try:
                                self._rename_cascade(entry.docs_cast_path, before_rel, lrel, "codebase conflict KEEP_LOCAL")
                            except Exception:
                                pass
                        shutil.copy2(p.local_path, desired)
                        _fix_yaml(desired, "remote")
                        self._update_baseline(
                            p.cast_id, peer_key, p.local_digest or "",
                            local_rel=lrel,
                            remote_rel=self._rel(entry.docs_cast_path, desired),
                        )
                        self.summary.counts["conflict_keep_local"] = self.summary.counts.get("conflict_keep_local", 0) + 1
                        self.summary.conflicts_resolved += 1
                        self.summary.items.append(CBSummaryItem("conflict", p.cast_id, lrel, self._rel(entry.docs_cast_path, desired), "resolved: KEEP_LOCAL"))
                    elif res == ConflictResolution.KEEP_PEER:
                        # adopt remote name locally then pull
                        if p.remote_path:
                            rrel = self._rel(entry.docs_cast_path, p.remote_path)
                            target = self.vault_path / rrel
                            target.parent.mkdir(parents=True, exist_ok=True)
                            if p.local_path.exists() and target != p.local_path:
                                before_rel = self._rel(self.vault_path, p.local_path)
                                shutil.move(str(p.local_path), str(target))
                                # cascade on LOCAL
                                try:
                                    self._rename_cascade(self.vault_path, before_rel, rrel, "codebase conflict KEEP_PEER")
                                except Exception:
                                    pass
                            shutil.copy2(p.remote_path, target)
                            _fix_yaml(target, "local")
                            self._update_baseline(
                                p.cast_id, peer_key, p.remote_digest or "",
                                local_rel=rrel, remote_rel=rrel,
                            )
                        self.summary.counts["conflict_keep_peer"] = self.summary.counts.get("conflict_keep_peer", 0) + 1
                        self.summary.conflicts_resolved += 1
                        self.summary.items.append(CBSummaryItem("conflict", p.cast_id, self._rel(self.vault_path, p.local_path), self._rel(entry.docs_cast_path, p.remote_path) if p.remote_path else None, "resolved: KEEP_PEER"))
                    else:
                        self.summary.conflicts_open += 1
                        self.summary.items.append(CBSummaryItem("conflict", p.cast_id, self._rel(self.vault_path, p.local_path), self._rel(entry.docs_cast_path, p.remote_path) if p.remote_path else None, "skipped"))
                        exit_code = max(exit_code, 3)
                else:
                    # no-op default
                    pass
            except Exception as e:
                logger.error(f"Error in cbsync for {p.cast_id[:8]}…: {e}")
                exit_code = max(exit_code, 1)

        self._save_syncstate()
        if self.summary:
            self.summary.finished = datetime.now().strftime("%Y-%m-%d %H:%M")
        return exit_code