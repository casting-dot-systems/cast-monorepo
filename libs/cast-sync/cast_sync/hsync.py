"""Horizontal sync engine with 3-way merge logic (standardized to Root/Cast)."""

import json
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from cast_core import CastConfig, SyncState, SyncStateEntry
from cast_core.registry import load_registry, resolve_cast_by_name
from cast_core.yamlio import parse_cast_file

from cast_sync.conflict import ConflictResolution, handle_conflict
from cast_sync.index import EphemeralIndex, build_ephemeral_index
from cast_sync.rename_cascade import apply_rename_cascade

logger = logging.getLogger(__name__)


class SyncDecision(Enum):
    """Sync decision for a file/peer pair."""

    NO_OP = "no_op"
    PULL = "pull"
    PUSH = "push"
    CONFLICT = "conflict"
    DELETE_LOCAL = "delete_local"  # accept deletion from peer
    DELETE_PEER = "delete_peer"  # propagate deletion to peer
    CREATE_PEER = "create_peer"
    CREATE_LOCAL = "create_local"
    RENAME_PEER = "rename_peer"  # rename peer to local path (live)
    RENAME_LOCAL = "rename_local"  # rename local to peer path (watch)


@dataclass
class SyncPlan:
    """Plan for syncing a single file with a peer."""

    cast_id: str
    local_path: Path
    peer_name: str
    peer_path: Path | None
    peer_root: Path | None
    decision: SyncDecision
    local_digest: str
    peer_digest: str | None
    baseline_digest: str | None
    # Optional rename destination
    rename_to: Path | None = None
    peer_mode: str | None = None  # 'live' | 'watch' (as seen from the decision context)


@dataclass
class SummaryItem:
    """One executed (or planned) change suitable for user summaries."""
    action: str                  # e.g. 'pull', 'push', 'create_peer', 'delete_local', 'rename_peer', 'conflict', ...
    cast_id: str
    peer: str
    local_rel: str               # path relative to local cast (best-effort for deletions)
    peer_rel: str | None = None  # path relative to peer cast (if known)
    detail: str | None = None    # human text like "peer → local", or "Projects/TODO.md → Notes/TODO.md"


@dataclass
class SyncSummary:
    """Aggregated summary of a sync run."""
    started: str
    finished: str
    counts: dict[str, int]
    items: list[SummaryItem]
    conflicts_open: int          # unresolved conflicts (skipped)
    conflicts_resolved: int      # resolved by KEEP_LOCAL/KEEP_PEER


class HorizontalSync:
    """Horizontal sync coordinator."""

    def __init__(self, root_path: Path):
        self.root_path = root_path
        self.cast_dir = root_path / ".cast"

        # Load configs
        self.config = self._load_config()
        self.syncstate = self._load_syncstate()

        # Standardized Cast content directory
        self.vault_path = root_path / "Cast"
        self._registry = load_registry()
        # Exposed artifacts for the CLI
        self.summary: SyncSummary | None = None
        self.last_plans: list[SyncPlan] = []

    def _load_config(self) -> CastConfig:
        """Load cast config."""
        config_path = self.cast_dir / "config.yaml"
        if not config_path.exists():
            raise FileNotFoundError(f"Cast not initialized: {config_path} not found")

        import ruamel.yaml

        yaml = ruamel.yaml.YAML()
        with open(config_path, encoding="utf-8") as f:
            data = yaml.load(f)
        return CastConfig(**data)

    def _load_syncstate(self) -> SyncState:
        """Load sync state."""
        syncstate_path = self.cast_dir / "syncstate.json"
        if not syncstate_path.exists():
            # Create empty state
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            return SyncState(version=1, updated_at=now, baselines={})

        with open(syncstate_path, encoding="utf-8") as f:
            data = json.load(f)

        # Convert nested dicts to SyncStateEntry objects
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
        """Save sync state to disk."""
        syncstate_path = self.cast_dir / "syncstate.json"

        # Convert to dict for JSON
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

        # Write atomically
        temp_path = syncstate_path.parent / f".{syncstate_path.name}.casttmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        temp_path.replace(syncstate_path)

    def _load_peer_syncstate(self, peer_root: Path) -> SyncState:
        """Load (or create empty) syncstate for a peer root."""
        path = peer_root / ".cast" / "syncstate.json"
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

    def _save_peer_syncstate(self, peer_root: Path, state: SyncState) -> None:
        """Persist peer syncstate atomically."""
        path = peer_root / ".cast" / "syncstate.json"
        data = {
            "version": state.version,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "baselines": {},
        }
        for cast_id, peers in state.baselines.items():
            data["baselines"][cast_id] = {}
            for peer_name, entry in peers.items():
                row = {"digest": entry.digest, "ts": entry.ts}
                if getattr(entry, "rel", None):
                    row["rel"] = entry.rel
                if getattr(entry, "peer_rel", None):
                    row["peer_rel"] = entry.peer_rel
                data["baselines"][cast_id][peer_name] = row
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".{path.name}.casttmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(path)

    # ---- Logging / helpers -------------------------------------------------
    def _log_event(self, event: str, **payload) -> None:
        """Append a structured sync event to .cast/sync.log as JSONL."""
        try:
            log_dir = self.cast_dir
            log_path = log_dir / "sync.log"
            log_dir.mkdir(parents=True, exist_ok=True)
            payload = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M"), "event": event, **payload}
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + os.linesep)
        except Exception as e:
            logger.debug(f"Failed to write sync event log: {e}")

    def _rename_cascade(self, vault_path: Path, old_rel: str, new_rel: str, scope: str) -> None:
        """
        Best-effort link rewrite after a rename. Logs an event with changed file count.
        """
        try:
            n = apply_rename_cascade(vault_path, old_rel, new_rel)
            if n:
                self._log_event("rename_cascade", old=old_rel, new=new_rel, scope=scope, files=n)
                logger.info(f"Rename cascade ({scope}): updated {n} file(s)")
        except Exception as e:
            logger.warning(f"Rename cascade failed ({scope}): {e}")

    def _read_cast_id(self, path: Path) -> str | None:
        try:
            fm, _, has = parse_cast_file(path)
            if has and isinstance(fm, dict):
                return fm.get("cast-id")
        except Exception:
            pass
        return None

    def _normalize_rel_for_lookup(self, path_or_id: str) -> str:
        """
        Convert a user-provided --file value into a cast-relative path
        suitable for EphemeralIndex.get_by_path(), if it looks like a path.
        Otherwise return the string unchanged (cast-id case).
        """
        p = Path(path_or_id)
        if p.is_absolute():
            try:
                return str(p.relative_to(self.vault_path))
            except ValueError:
                return str(p)
        if p.parts and p.parts[0] == self.vault_path.name:
            return str(Path(*p.parts[1:]))
        return str(p)

    def _local_rel(self, p: Path) -> str:
        """Get cast-relative path for a local file."""
        try:
            return str(p.relative_to(self.vault_path))
        except Exception:
            return p.name

    def _peer_rel_str(self, peer_name: str, peer_root: Path | None, p: Path | None) -> str | None:
        """Get cast-relative path for a peer file."""
        if p is None or peer_root is None:
            return None
        base = peer_root / "Cast"
        try:
            return str(p.relative_to(base))
        except Exception:
            return p.name

    def _safe_dest(self, base: Path, suffix: str) -> Path:
        """Return a non-existing path by appending a suffix (and counter if needed)."""
        if not base.exists():
            return base
        stem = base.stem
        ext = base.suffix
        candidate = base.with_name(f"{stem} {suffix}{ext}")
        i = 2
        while candidate.exists():
            candidate = base.with_name(f"{stem} {suffix} {i}{ext}")
            i += 1
        return candidate

    def _safe_move(self, src: Path, dest: Path, *, provenance: str) -> Path:
        """Move src→dest safely (avoid overwriting different cast-ids). Returns final dest."""
        if src.resolve() == dest.resolve():
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            existing_id = self._read_cast_id(dest)
            src_id = self._read_cast_id(src)
            if existing_id and src_id and existing_id == src_id:
                # Same logical file already at dest → remove src, keep dest
                try:
                    src.unlink(missing_ok=True)
                except Exception:
                    pass
                return dest
            # Different or unreadable → allocate suffixed destination
            dest = self._safe_dest(dest, f"(~from {provenance})")
        shutil.move(str(src), str(dest))
        return dest

    def _safe_copy(self, src: Path, dest: Path, *, provenance: str) -> Path:
        """Copy src→dest safely (avoid overwriting different cast-ids). Returns final dest."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            existing_id = self._read_cast_id(dest)
            src_id = self._read_cast_id(src)
            if existing_id and src_id and existing_id != src_id:
                dest = self._safe_dest(dest, f"(~from {provenance})")
        shutil.copy2(src, dest)
        return dest

    def _update_baseline_both(
        self, cast_id: str, peer_name: str, digest: str,
        peer_root: Path | None,
        local_rel: str | None = None, peer_rel: str | None = None
    ) -> None:
        """Update baselines in local and peer syncstate (symmetrically)."""
        # local (our perspective)
        self._update_baseline(cast_id, peer_name, digest, local_rel=local_rel, peer_rel=peer_rel)
        if peer_root is None:
            return
        # peer (their perspective) — flip roles of rel/peer_rel
        their_state = self._load_peer_syncstate(peer_root)
        if cast_id not in their_state.baselines:
            their_state.baselines[cast_id] = {}
        entry = their_state.baselines[cast_id].get(self.config.cast_name)
        if entry is None:
            entry = SyncStateEntry(digest=digest, ts=datetime.now().strftime("%Y-%m-%d %H:%M"))
            their_state.baselines[cast_id][self.config.cast_name] = entry
        entry.digest = digest
        entry.ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        if peer_rel is not None:
            entry.rel = peer_rel
        if local_rel is not None:
            entry.peer_rel = local_rel
        self._save_peer_syncstate(peer_root, their_state)

    def _resolve_peer_vault_path(self, peer_name: str) -> Path | None:
        """Resolve a peer cast folder path by name.

        Resolution:
          • machine registry (resolve_cast_by_name),
          • None (unresolved).
        """
        entry = resolve_cast_by_name(peer_name)
        if entry:
            return entry.root / "Cast"
        return None

    def _index_peer(
        self,
        peer_name: str,
        *,
        limit_file: str | None = None,
        existing_index: EphemeralIndex | None = None,
    ) -> tuple[Path, EphemeralIndex] | None:
        """
        Resolve and index a peer cast. If `existing_index` is provided, merge
        newly discovered records into it (used for incremental, per-file indexing).
        """
        peer_vault_path = self._resolve_peer_vault_path(peer_name)
        if not peer_vault_path:
            logger.warning(f"Peer '{peer_name}' not found (not in machine registry).")
            return None
        if not peer_vault_path.exists():
            logger.warning(f"Peer vault path does not exist: {peer_vault_path}")
            return None

        peer_root = peer_vault_path.parent
        peer_cast_dir = peer_root / ".cast"
        if not peer_cast_dir.exists():
            logger.warning(
                f"Peer '{peer_name}' is missing .cast/ at {peer_root}; skip. Install the peer with 'cast install'."
            )
            return None

        scope = f" (limited to {limit_file})" if limit_file else ""
        logger.info(f"Indexing peer {peer_name}{scope}: {peer_vault_path}")
        tmp_index = build_ephemeral_index(
            peer_root,
            peer_vault_path,
            fixup=False,
            limit_file=limit_file,
        )

        if existing_index is None:
            return peer_vault_path, tmp_index

        # Merge tmp_index into existing_index
        for rec in tmp_index.by_id.values():
            existing_index.add_file(rec)
        return peer_vault_path, existing_index

    def _clear_baseline_both(self, cast_id: str, peer_name: str, peer_root: Path | None) -> None:
        """Remove baselines for (cast_id, peer_name) in both local and peer syncstate."""
        # local
        if cast_id in self.syncstate.baselines:
            self.syncstate.baselines[cast_id].pop(peer_name, None)
            if not self.syncstate.baselines[cast_id]:
                self.syncstate.baselines.pop(cast_id, None)
        # peer
        if peer_root is not None:
            their_state = self._load_peer_syncstate(peer_root)
            if cast_id in their_state.baselines:
                their_state.baselines[cast_id].pop(self.config.cast_name, None)
                if not their_state.baselines[cast_id]:
                    their_state.baselines.pop(cast_id, None)
            self._save_peer_syncstate(peer_root, their_state)

    def _decide_sync(self, local_rec, peer_rec, peer_name: str, mode: str) -> SyncDecision:
        """
        Decide 3-way sync action for a file/peer pair with path-aware logic.
        """
        cast_id = local_rec["cast_id"]
        local_digest = local_rec["digest"]
        
        # fetch full baseline (paths + digest)
        b_entry = None
        if cast_id in self.syncstate.baselines and peer_name in self.syncstate.baselines[cast_id]:
            b_entry = self.syncstate.baselines[cast_id][peer_name]
        baseline = (b_entry.digest if b_entry else None)

        # Peer entirely missing
        if peer_rec is None:
            if baseline is None:
                return SyncDecision.CREATE_PEER if mode == "live" else SyncDecision.NO_OP
            # With baseline: distinguish deletion vs. "local-only rename"
            if local_digest == baseline:
                local_moved = bool(b_entry and b_entry.rel and (local_rec["relpath"] != b_entry.rel))
                if local_moved:
                    # Peer missing but only local renamed since baseline → create on peer at new path
                    return SyncDecision.CREATE_PEER if mode == "live" else SyncDecision.NO_OP
                # Otherwise: treat as peer deletion if the peer is LIVE.
                # WATCH peers must not trigger destructive local deletes.
                return SyncDecision.DELETE_LOCAL if mode == "live" else SyncDecision.NO_OP
            # content diverged but peer missing → conflict
            return SyncDecision.CONFLICT

        peer_digest = peer_rec["digest"]

        # First contact (no baseline)
        if baseline is None:
            if local_digest == peer_digest:
                if local_rec["relpath"] != peer_rec["relpath"]:
                    if mode == "live":
                        return SyncDecision.RENAME_PEER
                    else:
                        # WATCH peers must not force local renames if we have any LIVE peers.
                        has_any_live = any(
                            (m == "live" and n != self.config.cast_name)
                            for n, m in local_rec.get("peers", {}).items()
                        )
                        if has_any_live:
                            return SyncDecision.NO_OP
                        return SyncDecision.RENAME_LOCAL
                return SyncDecision.NO_OP
            return SyncDecision.CONFLICT

        # With baseline: see who moved
        local_moved = bool(b_entry and b_entry.rel and (local_rec["relpath"] != b_entry.rel))
        peer_moved = bool(b_entry and b_entry.peer_rel and (peer_rec["relpath"] != b_entry.peer_rel))

        # Pure rename(s) (content unchanged on both sides)
        if local_digest == baseline and peer_digest == baseline:
            if local_moved and not peer_moved:
                return SyncDecision.RENAME_PEER if mode == "live" else SyncDecision.NO_OP
            if peer_moved and not local_moved:
                # watch policy: only rename local if all peers are watch-only
                if mode == "live":
                    return SyncDecision.RENAME_LOCAL
                has_any_live = any(
                    (m == "live" and n != self.config.cast_name)
                    for n, m in local_rec.get("peers", {}).items()
                )
                return SyncDecision.NO_OP if has_any_live else SyncDecision.RENAME_LOCAL
            if local_moved and peer_moved:
                # both moved, possibly to different places → structural conflict
                if local_rec["relpath"] != peer_rec["relpath"]:
                    return SyncDecision.CONFLICT
            # neither moved or both moved to same place
            return SyncDecision.NO_OP

        # Fast-forward cases with possible path drift:
        if local_digest == baseline and peer_digest != baseline:
            # pull to local; pull writes to local path, baseline will capture paths later
            return SyncDecision.PULL
        if peer_digest == baseline and local_digest != baseline:
            # push to peer's path chosen as local_rel (includes rename+content in one step)
            return SyncDecision.PUSH if mode == "live" else SyncDecision.NO_OP

        # both content changed and differ → conflict
        if local_digest != baseline and peer_digest != baseline and local_digest != peer_digest:
            return SyncDecision.CONFLICT

        # Otherwise, if digests equal but path mismatch (e.g., converged content):
        if local_digest == peer_digest and local_rec["relpath"] != peer_rec["relpath"]:
            if mode == "live":
                return SyncDecision.RENAME_PEER
            else:
                has_any_live = any(
                    (m == "live" and n != self.config.cast_name)
                    for n, m in local_rec.get("peers", {}).items()
                )
                return SyncDecision.NO_OP if has_any_live else SyncDecision.RENAME_LOCAL

        return SyncDecision.NO_OP

    def _sync_core(
        self,
        peer_filter: list[str] | None = None,
        file_filter: str | None = None,
        dry_run: bool = False,
        non_interactive: bool = False,
    ) -> int:
        """Internal core logic (single-root, no cascade)."""
        # Initialize summary
        self.summary = SyncSummary(
            started=datetime.now().strftime("%Y-%m-%d %H:%M"),
            finished="",
            counts={},
            items=[],
            conflicts_open=0,
            conflicts_resolved=0,
        )
        # Build local index
        logger.info(f"Indexing local cast: {self.vault_path}")
        local_index = build_ephemeral_index(
            self.root_path, self.vault_path, fixup=True, limit_file=file_filter
        )

        # Discover peers from local records, skipping self
        discovered = local_index.all_peers()
        if self.config.cast_name in discovered:
            discovered.discard(self.config.cast_name)
            logger.info(f"Skipping self in peer set: {self.config.cast_name}")
        if peer_filter:
            discovered = discovered.intersection(set(peer_filter))
        logger.info(f"Found peers: {discovered}")

        # We'll index peers lazily per file (and cache)
        peer_indices: dict[str, tuple[Path, EphemeralIndex]] = {}

        # Build sync plan
        plans: list[SyncPlan] = []

        for local_rec in local_index.by_id.values():
            for peer_name, mode in local_rec["peers"].items():
                # respect peer filter and skip self
                if peer_filter and peer_name not in peer_filter:
                    continue
                if peer_name == self.config.cast_name:
                    continue
                # Ensure this peer is indexed (limited to this file's relpath for speed)
                pair = peer_indices.get(peer_name)
                if pair is None:
                    pair = self._index_peer(peer_name, limit_file=local_rec["relpath"])
                    if pair is None:
                        continue
                    peer_indices[peer_name] = pair
                else:
                    # augment index with this specific relpath, if it wasn't scanned yet
                    peer_indices[peer_name] = (
                        self._index_peer(
                            peer_name, limit_file=local_rec["relpath"], existing_index=pair[1]
                        )
                        or pair
                    )

                peer_vault_path, peer_index = peer_indices[peer_name]
                peer_rec = peer_index.get_by_id(local_rec["cast_id"])

                decision = self._decide_sync(local_rec, peer_rec, peer_name, mode)

                local_path = self.vault_path / local_rec["relpath"]
                peer_path = None
                peer_digest = None
                peer_root: Path | None = None
                rename_to: Path | None = None

                if peer_rec:
                    peer_path = peer_vault_path / peer_rec["relpath"]
                    peer_digest = peer_rec["digest"]
                elif decision in (SyncDecision.CREATE_PEER, SyncDecision.PUSH):
                    # Determine peer path for new file
                    peer_path = peer_vault_path / local_rec["relpath"]

                baseline_digest = None
                peer_root = peer_vault_path.parent
                if (
                    local_rec["cast_id"] in self.syncstate.baselines
                    and peer_name in self.syncstate.baselines[local_rec["cast_id"]]
                ):
                    baseline_digest = self.syncstate.baselines[local_rec["cast_id"]][
                        peer_name
                    ].digest

                # For rename decisions, compute destination path
                if decision == SyncDecision.RENAME_PEER and peer_path is not None:
                    rename_to = peer_vault_path / local_rec["relpath"]
                elif decision == SyncDecision.RENAME_LOCAL:
                    if peer_rec:
                        rename_to = self.vault_path / peer_rec["relpath"]
                peer_mode = mode

                plan = SyncPlan(
                    cast_id=local_rec["cast_id"],
                    local_path=local_path,
                    peer_name=peer_name,
                    peer_path=peer_path,
                    peer_root=peer_root,
                    decision=decision,
                    local_digest=local_rec["digest"],
                    peer_digest=peer_digest,
                    baseline_digest=baseline_digest,
                    rename_to=rename_to,
                    peer_mode=peer_mode,
                )
                plans.append(plan)

        # Stash the plan for debug rendering by the CLI
        self.last_plans = plans[:]

        # Deletion pass: local file missing but baseline exists → decide per peer
        # IMPORTANT: when file_filter is set, we must NOT treat non-scanned files as deleted.
        # We therefore restrict the deletion pass to the filtered cast-id (if resolvable), or skip it.
        allowed_ids: set[str] | None = None
        if file_filter:
            allowed_ids = set()
            # If filter matches a known cast-id in baselines, restrict to that
            if file_filter in self.syncstate.baselines:
                allowed_ids.add(file_filter)
            else:
                # If filter was a relpath we scanned (and exists), map to its cast-id
                normalized_rel = self._normalize_rel_for_lookup(file_filter)
                rec = local_index.get_by_path(normalized_rel)
                if rec:
                    allowed_ids.add(rec["cast_id"])
                else:
                    # Try to resolve by peeking at peers referenced anywhere in syncstate
                    peers_in_state = {
                        p
                        for peers_map in self.syncstate.baselines.values()
                        for p in peers_map.keys()
                    }
                    for pname in peers_in_state:
                        pair = peer_indices.get(pname) or self._index_peer(
                            pname, limit_file=file_filter
                        )
                        if not pair:
                            continue
                        peer_indices[pname] = pair
                        _, pidx = pair
                        prec = pidx.get_by_path(normalized_rel)
                        if prec:
                            allowed_ids.add(prec["cast_id"])

        for cast_id, peers_map in list(self.syncstate.baselines.items()):
            if allowed_ids is not None and cast_id not in allowed_ids:
                continue
            if cast_id in local_index.by_id:
                continue  # still present locally; handled above
            for peer_name in list(peers_map.keys()):
                if peer_filter and peer_name not in (peer_filter or []):
                    continue
                # Make sure the peer is indexed (full scan: we need to find cast-id anywhere)
                pair = peer_indices.get(peer_name)
                if pair is None:
                    # No cache yet → build a FULL index so we can locate the cast-id anywhere.
                    pair = self._index_peer(peer_name)
                    if pair is None:
                        continue
                else:
                    # We may have a LIMITED index from the main planning pass.
                    # Upgrade it to FULL by merging a full scan into the existing index.
                    # (Calling _index_peer without limit_file triggers a full scan.)
                    upgraded = self._index_peer(peer_name, existing_index=pair[1])
                    pair = upgraded or pair
                peer_indices[peer_name] = pair
                peer_vault_path, peer_index = pair
                peer_rec = peer_index.get_by_id(cast_id)
                baseline_digest = self.syncstate.baselines[cast_id][peer_name].digest

                # Synthesize paths/digests for planning
                # If we need a local path for conflicts/sidecars, default to peer path name or cast_id
                local_rel = peer_rec["relpath"] if peer_rec else f"{cast_id}.md"
                local_path = self.vault_path / local_rel
                peer_path = (peer_vault_path / peer_rec["relpath"]) if peer_rec else None
                peer_digest = peer_rec["digest"] if peer_rec else None

                if peer_rec is None:
                    # Both sides missing → just clear baselines
                    self._clear_baseline_both(cast_id, peer_name, peer_vault_path.parent)
                    self._log_event("baseline_cleared_orphan", cast_id=cast_id, peer=peer_name)
                    continue

                # Determine peer mode from its own copy (local is missing).
                # On the peer's copy, the relationship is keyed by *our* cast name.
                peer_mode = "live"
                try:
                    peers_map = peer_rec.get("peers") or {}
                    if isinstance(peers_map, dict):
                        peer_mode = peers_map.get(self.config.cast_name, "live")
                except Exception:
                    peer_mode = "live"

                if peer_digest == baseline_digest:
                    if peer_mode == "live":
                        # Local deleted, peer unchanged since baseline → propagate deletion to LIVE peer
                        decision = SyncDecision.DELETE_PEER
                    else:
                        # WATCH peer: do NOT delete; clear baseline both sides and skip planning an action
                        self._clear_baseline_both(cast_id, peer_name, peer_vault_path.parent)
                        self._log_event(
                            "baseline_cleared_watch_skip",
                            cast_id=cast_id,
                            peer=peer_name,
                        )
                        continue
                else:
                    decision = SyncDecision.CONFLICT  # local missing vs peer changed

                plan = SyncPlan(
                    cast_id=cast_id,
                    local_path=local_path,
                    peer_name=peer_name,
                    peer_path=peer_path,
                    peer_root=peer_vault_path.parent,
                    decision=decision,
                    local_digest="",  # local missing
                    peer_digest=peer_digest,
                    baseline_digest=baseline_digest,
                    rename_to=None,
                    peer_mode=peer_mode,
                )
                plans.append(plan)

        # Print plan if dry run
        if dry_run:
            print("\nDry run - planned actions:")
            for plan in plans:
                if plan.decision != SyncDecision.NO_OP:
                    line = f"  {plan.local_path.name} -> {plan.peer_name}: {plan.decision.value}"
                    if (
                        plan.decision in (SyncDecision.RENAME_PEER, SyncDecision.RENAME_LOCAL)
                        and plan.rename_to
                    ):
                        src = (
                            plan.peer_path
                            if plan.decision == SyncDecision.RENAME_PEER
                            else plan.local_path
                        )
                        line += f"  {src.name} → {plan.rename_to.name}"
                    print(line)
            # Populate summary counts/items for the CLI even in dry-run
            for plan in plans:
                if plan.decision == SyncDecision.NO_OP:
                    continue
                action = plan.decision.value
                self.summary.counts[action] = self.summary.counts.get(action, 0) + 1
                # best-effort relpaths
                local_rel = ""
                try:
                    local_rel = str(plan.local_path.relative_to(self.vault_path))
                except Exception:
                    local_rel = plan.local_path.name
                peer_rel = None
                if plan.peer_path:
                    try:
                        base = (plan.peer_root / "Cast") if plan.peer_root else None
                        peer_rel = str(plan.peer_path.relative_to(base)) if base else plan.peer_path.name
                    except Exception:
                        peer_rel = plan.peer_path.name
                detail = None
                if plan.decision in (SyncDecision.PULL, SyncDecision.CREATE_LOCAL):
                    detail = "peer → local"
                elif plan.decision in (SyncDecision.PUSH, SyncDecision.CREATE_PEER):
                    detail = "local → peer"
                elif plan.decision in (SyncDecision.DELETE_LOCAL,):
                    detail = "deleted locally (accept peer deletion)"
                elif plan.decision in (SyncDecision.DELETE_PEER,):
                    detail = "deleted on peer (propagate local deletion)"
                elif plan.decision == SyncDecision.RENAME_PEER and plan.rename_to and plan.peer_path:
                    try:
                        base = (plan.peer_root / "Cast") if plan.peer_root else None
                        _from = str(plan.peer_path.relative_to(base)) if base else plan.peer_path.name
                        _to = str(plan.rename_to.relative_to(base)) if base else plan.rename_to.name
                        detail = f"peer: {_from} → {_to}"
                    except Exception:
                        detail = f"peer: {plan.peer_path.name} → {plan.rename_to.name}"
                elif plan.decision == SyncDecision.RENAME_LOCAL and plan.rename_to:
                    try:
                        _from = str(plan.local_path.relative_to(self.vault_path))
                        _to = str(plan.rename_to.relative_to(self.vault_path))
                        detail = f"local: {_from} → {_to}"
                    except Exception:
                        detail = f"local: {plan.local_path.name} → {plan.rename_to.name}"
                elif plan.decision == SyncDecision.CONFLICT:
                    detail = "conflict (resolution pending)"
                    # Count as open conflict in dry-run
                    self.summary.conflicts_open += 1
                self.summary.items.append(
                    SummaryItem(
                        action=action,
                        cast_id=plan.cast_id,
                        peer=plan.peer_name,
                        local_rel=local_rel,
                        peer_rel=peer_rel,
                        detail=detail,
                    )
                )
            self.summary.finished = datetime.now().strftime("%Y-%m-%d %H:%M")
            return 0

        # Execute plan
        exit_code = 0
        conflicts = []

        for plan in plans:
            if plan.decision == SyncDecision.NO_OP:
                # Special NO_OP: peer missing + baseline exists + WATCH → clear baselines quietly.
                # This covers the case where a WATCH peer deleted its copy; we keep local and
                # drop the relationship so future syncs don't churn.
                if (
                    plan.peer_digest is None
                    and plan.baseline_digest is not None
                    and (plan.peer_mode or "") == "watch"
                ):
                    try:
                        self._clear_baseline_both(plan.cast_id, plan.peer_name, plan.peer_root)
                        self._log_event("baseline_cleared_watch_skip", cast_id=plan.cast_id, peer=plan.peer_name)
                    except Exception:
                        pass
                    continue
                # If identical content, ensure baseline is correct.
                # Covers both "first contact identical" and "both sides converged to same digest".
                if plan.peer_digest is not None and plan.local_digest == plan.peer_digest:
                    # ALWAYS refresh paths (even if digest unchanged)
                    self._update_baseline_both(
                        plan.cast_id,
                        plan.peer_name,
                        plan.peer_digest or plan.local_digest,
                        plan.peer_root,
                        local_rel=self._local_rel(plan.local_path),
                        peer_rel=self._peer_rel_str(plan.peer_name, plan.peer_root, plan.peer_path),
                    )
                continue

            logger.info(
                f"Executing: {plan.local_path.name} -> {plan.peer_name}: {plan.decision.value}"
            )

            try:
                if plan.decision == SyncDecision.PULL:
                    # Copy peer to local
                    # If peer moved since baseline, adopt PEER name/path before pulling
                    if plan.peer_path:
                        peer_rel_now = self._peer_rel_str(plan.peer_name, plan.peer_root, plan.peer_path)
                        local_rel_now = self._local_rel(plan.local_path)
                        if peer_rel_now and local_rel_now != peer_rel_now:
                            target = self.vault_path / peer_rel_now
                            before = plan.local_path
                            after = self._safe_move(plan.local_path, target, provenance=plan.peer_name)
                            plan.local_path = after
                            try:
                                _from = str(before.relative_to(self.vault_path))
                                _to = str(after.relative_to(self.vault_path))
                            except Exception:
                                _from, _to = before.name, after.name
                            self._log_event("rename_local", cast_id=plan.cast_id, **{"from": _from, "to": _to, "peer": plan.peer_name})
                            # Cascade rename in LOCAL cast
                            try:
                                self._rename_cascade(self.vault_path, _from, _to, f"local adopt({plan.peer_name})")
                            except Exception:
                                pass
                            self.summary.counts["rename_local"] = self.summary.counts.get("rename_local", 0) + 1
                            self.summary.items.append(
                                SummaryItem("rename_local", plan.cast_id, plan.peer_name, _to, None, f"local: {_from} → {_to}")
                            )
                    if plan.peer_path:
                        self._safe_copy(plan.peer_path, plan.local_path, provenance=plan.peer_name)
                        self._update_baseline_both(
                            plan.cast_id, plan.peer_name, plan.peer_digest or "", plan.peer_root,
                            local_rel=self._local_rel(plan.local_path),
                            peer_rel=self._peer_rel_str(plan.peer_name, plan.peer_root, plan.peer_path),
                        )
                    # summary
                    try:
                        local_rel = str(plan.local_path.relative_to(self.vault_path))
                    except Exception:
                        local_rel = plan.local_path.name
                    peer_rel = None
                    if plan.peer_path:
                        try:
                            base = (plan.peer_root / "Cast") if plan.peer_root else None
                            peer_rel = str(plan.peer_path.relative_to(base)) if base else plan.peer_path.name
                        except Exception:
                            peer_rel = plan.peer_path.name
                    self.summary.counts["pull"] = self.summary.counts.get("pull", 0) + 1
                    self.summary.items.append(
                        SummaryItem("pull", plan.cast_id, plan.peer_name, local_rel, peer_rel, "peer → local")
                    )

                elif plan.decision in (SyncDecision.PUSH, SyncDecision.CREATE_PEER):
                    # Copy local to peer
                    if plan.peer_path:
                        # If peer exists but path differs from local (rename+edit), rename peer first
                        local_rel_now = self._local_rel(plan.local_path)
                        peer_rel_now = self._peer_rel_str(plan.peer_name, plan.peer_root, plan.peer_path)
                        if peer_rel_now and local_rel_now != peer_rel_now:
                            # Compute peer cast base
                            try:
                                peer_vault_base = (plan.peer_root / "Cast") if plan.peer_root else None
                            except Exception:
                                peer_vault_base = None
                            if peer_vault_base:
                                desired = peer_vault_base / local_rel_now
                            else:
                                # best-effort
                                desired = plan.peer_path.parent / Path(local_rel_now).name
                            before = plan.peer_path
                            after = self._safe_move(plan.peer_path, desired, provenance="LOCAL")
                            plan.peer_path = after
                            try:
                                base = (plan.peer_root / "Cast") if plan.peer_root else None
                                _from = str(before.relative_to(base)) if base else before.name
                                _to = str(after.relative_to(base)) if base else after.name
                            except Exception:
                                _from, _to = before.name, after.name
                            self._log_event("rename_peer", cast_id=plan.cast_id, **{"from": _from, "to": _to, "peer": plan.peer_name})
                            # Cascade rename in PEER vault
                            if peer_vault_base:
                                try:
                                    self._rename_cascade(peer_vault_base, _from, _to, f"peer {plan.peer_name} adopt(LOCAL)")
                                except Exception:
                                    pass
                            self.summary.counts["rename_peer"] = self.summary.counts.get("rename_peer", 0) + 1
                            self.summary.items.append(SummaryItem("rename_peer", plan.cast_id, plan.peer_name, local_rel_now, _to, f"peer: {_from} → {_to}"))
                        self._safe_copy(
                            plan.local_path, plan.peer_path, provenance=self.config.cast_name
                        )
                        self._update_baseline_both(
                            plan.cast_id, plan.peer_name, plan.local_digest, plan.peer_root,
                            local_rel=self._local_rel(plan.local_path),
                            peer_rel=self._peer_rel_str(plan.peer_name, plan.peer_root, plan.peer_path),
                        )
                    try:
                        local_rel = str(plan.local_path.relative_to(self.vault_path))
                    except Exception:
                        local_rel = plan.local_path.name
                    peer_rel = None
                    if plan.peer_path:
                        try:
                            base = (plan.peer_root / "Cast") if plan.peer_root else None
                            peer_rel = str(plan.peer_path.relative_to(base)) if base else plan.peer_path.name
                        except Exception:
                            peer_rel = plan.peer_path.name
                    key = "create_peer" if plan.decision == SyncDecision.CREATE_PEER else "push"
                    self.summary.counts[key] = self.summary.counts.get(key, 0) + 1
                    self.summary.items.append(
                        SummaryItem(key, plan.cast_id, plan.peer_name, local_rel, peer_rel, "local → peer")
                    )

                elif plan.decision == SyncDecision.DELETE_LOCAL:
                    # Accept peer deletion: remove local and clear baselines both sides
                    plan.local_path.unlink(missing_ok=True)
                    self._clear_baseline_both(plan.cast_id, plan.peer_name, plan.peer_root)
                    self._log_event(
                        "delete_local",
                        cast_id=plan.cast_id,
                        path=str(plan.local_path.relative_to(self.vault_path)),
                        peer=plan.peer_name,
                    )
                    try:
                        local_rel = str(plan.local_path.relative_to(self.vault_path))
                    except Exception:
                        local_rel = plan.local_path.name
                    self.summary.counts["delete_local"] = self.summary.counts.get("delete_local", 0) + 1
                    self.summary.items.append(
                        SummaryItem("delete_local", plan.cast_id, plan.peer_name, local_rel, None,
                                    "deleted locally (accept peer deletion)")
                    )

                elif plan.decision == SyncDecision.DELETE_PEER:
                    # Propagate local deletion: remove peer and clear baselines both sides
                    if plan.peer_path:
                        plan.peer_path.unlink(missing_ok=True)
                    self._clear_baseline_both(plan.cast_id, plan.peer_name, plan.peer_root)
                    # Log peer-relative path if possible, else best-effort.
                    path_str = ""
                    if plan.peer_path:
                        try:
                            base = (plan.peer_root / "Cast") if plan.peer_root else None
                            path_str = (
                                str(plan.peer_path.relative_to(base))
                                if base
                                else plan.peer_path.name
                            )
                        except Exception:
                            path_str = plan.peer_path.name
                    self._log_event(
                        "delete_peer", cast_id=plan.cast_id, path=path_str, peer=plan.peer_name
                    )
                    try:
                        local_rel = str(plan.local_path.relative_to(self.vault_path))
                    except Exception:
                        local_rel = plan.local_path.name
                    self.summary.counts["delete_peer"] = self.summary.counts.get("delete_peer", 0) + 1
                    self.summary.items.append(
                        SummaryItem("delete_peer", plan.cast_id, plan.peer_name, local_rel, path_str or "", 
                                    "deleted on peer (propagate local deletion)")
                    )

                elif plan.decision == SyncDecision.RENAME_PEER:
                    if plan.peer_path and plan.rename_to:
                        before = plan.peer_path
                        after = self._safe_move(plan.peer_path, plan.rename_to, provenance="LOCAL")
                        # Compute paths relative to the peer's vault, defensively.
                        try:
                            base = (plan.peer_root / "Cast") if plan.peer_root else None
                            _from = str(before.relative_to(base)) if base else before.name
                            _to = str(after.relative_to(base)) if base else after.name
                        except Exception:
                            _from, _to = before.name, after.name
                        self._log_event(
                            "rename_peer",
                            cast_id=plan.cast_id,
                            **{"from": _from, "to": _to, "peer": plan.peer_name},
                        )
                        # Cascade rename in PEER vault
                        try:
                            base = (plan.peer_root / "Cast") if plan.peer_root else None
                            if base: self._rename_cascade(base, _from, _to, f"peer {plan.peer_name} (rename_peer)")
                        except Exception: pass
                        self._update_baseline_both(
                            plan.cast_id,
                            plan.peer_name,
                            plan.peer_digest or plan.local_digest,
                            plan.peer_root,
                            local_rel=self._local_rel(plan.local_path),
                            peer_rel=self._peer_rel_str(plan.peer_name, plan.peer_root, after),
                        )
                        # summary
                        try:
                            local_rel = str(plan.local_path.relative_to(self.vault_path))
                        except Exception:
                            local_rel = plan.local_path.name
                        self.summary.counts["rename_peer"] = self.summary.counts.get("rename_peer", 0) + 1
                        self.summary.items.append(
                            SummaryItem("rename_peer", plan.cast_id, plan.peer_name, local_rel, _to, f"peer: {_from} → {_to}")
                        )

                elif plan.decision == SyncDecision.RENAME_LOCAL:
                    if plan.rename_to:
                        before = plan.local_path
                        after = self._safe_move(
                            plan.local_path, plan.rename_to, provenance=plan.peer_name
                        )
                        try:
                            _from = str(before.relative_to(self.vault_path))
                            _to = str(after.relative_to(self.vault_path))
                        except Exception:
                            _from, _to = before.name, after.name
                        self._log_event(
                            "rename_local",
                            cast_id=plan.cast_id,
                            **{"from": _from, "to": _to, "peer": plan.peer_name},
                        )
                        # Cascade rename in LOCAL vault
                        try:
                            self._rename_cascade(self.vault_path, _from, _to, "local (rename_local)")
                        except Exception: pass
                        plan.local_path = after
                        self._update_baseline_both(
                            plan.cast_id,
                            plan.peer_name,
                            plan.peer_digest or plan.local_digest,
                            plan.peer_root,
                            local_rel=self._local_rel(after),
                            peer_rel=self._peer_rel_str(plan.peer_name, plan.peer_root, plan.peer_path),
                        )
                        self.summary.counts["rename_local"] = self.summary.counts.get("rename_local", 0) + 1
                        self.summary.items.append(
                            SummaryItem("rename_local", plan.cast_id, plan.peer_name, _to, None, f"local: {_from} → {_to}")
                        )

                elif plan.decision == SyncDecision.CONFLICT:
                    # Handle conflict
                    resolution = handle_conflict(
                        plan.local_path,
                        plan.peer_path,
                        plan.cast_id,
                        plan.peer_name,
                        self.root_path,
                        interactive=not non_interactive,
                        # If local is missing (deletion), show empty local content in preview
                        local_content=("" if not plan.local_path.exists() else None),
                    )

                    if resolution == ConflictResolution.KEEP_LOCAL:
                        # Adopt LOCAL path/name on peer if mismatch, then overwrite peer with local
                        if plan.peer_path and plan.peer_path.exists():
                            local_rel_now = self._local_rel(plan.local_path)
                            peer_rel_now = self._peer_rel_str(plan.peer_name, plan.peer_root, plan.peer_path)
                            if peer_rel_now and local_rel_now != peer_rel_now:
                                # compute desired path in peer vault
                                try:
                                    base = (plan.peer_root / "Cast") if plan.peer_root else None
                                except Exception:
                                    base = None
                                desired = (base / local_rel_now) if base else plan.peer_path.parent / Path(local_rel_now).name
                                before = plan.peer_path
                                after = self._safe_move(plan.peer_path, desired, provenance="LOCAL")
                                plan.peer_path = after
                                try:
                                    base = (plan.peer_root / "Cast") if plan.peer_root else None
                                    _from = str(before.relative_to(base)) if base else before.name
                                    _to = str(after.relative_to(base)) if base else after.name
                                except Exception:
                                    _from, _to = before.name, after.name
                                self._log_event("rename_peer", cast_id=plan.cast_id, **{"from": _from, "to": _to, "peer": plan.peer_name})
                                # Cascade rename in PEER cast
                                try:
                                    base = (plan.peer_root / "Cast") if plan.peer_root else None
                                    if base: self._rename_cascade(base, _from, _to, f"peer {plan.peer_name} (conflict KEEP_LOCAL)")
                                except Exception: pass
                                self.summary.counts["rename_peer"] = self.summary.counts.get("rename_peer", 0) + 1
                                self.summary.items.append(SummaryItem("rename_peer", plan.cast_id, plan.peer_name, local_rel_now, _to, f"peer: {_from} → {_to}"))
                        if plan.local_path.exists():
                            if plan.peer_path:
                                self._safe_copy(
                                    plan.local_path,
                                    plan.peer_path,
                                    provenance=self.config.cast_name,
                                )
                            self._update_baseline_both(
                                plan.cast_id, plan.peer_name, plan.local_digest, plan.peer_root,
                                local_rel=self._local_rel(plan.local_path),
                                peer_rel=self._peer_rel_str(plan.peer_name, plan.peer_root, plan.peer_path),
                            )
                        else:
                            # conflict due to local deletion; KEEP_LOCAL means keep deletion → delete peer
                            if plan.peer_path:
                                plan.peer_path.unlink(missing_ok=True)
                            self._clear_baseline_both(plan.cast_id, plan.peer_name, plan.peer_root)
                        try:
                            local_rel = str(plan.local_path.relative_to(self.vault_path))
                        except Exception:
                            local_rel = plan.local_path.name
                        self.summary.counts["conflict_keep_local"] = self.summary.counts.get("conflict_keep_local", 0) + 1
                        self.summary.conflicts_resolved += 1
                        self.summary.items.append(
                            SummaryItem("conflict", plan.cast_id, plan.peer_name, local_rel, None, "resolved: KEEP_LOCAL")
                        )
                    elif resolution == ConflictResolution.KEEP_PEER:
                        # Adopt PEER path/name locally if mismatch, then pull peer content
                        if plan.peer_path and plan.local_path.exists():
                            peer_rel_now = self._peer_rel_str(plan.peer_name, plan.peer_root, plan.peer_path)
                            local_rel_now = self._local_rel(plan.local_path)
                            if peer_rel_now and local_rel_now != peer_rel_now:
                                target = self.vault_path / peer_rel_now
                                before = plan.local_path
                                after = self._safe_move(plan.local_path, target, provenance=plan.peer_name)
                                plan.local_path = after
                                try:
                                    _from = str(before.relative_to(self.vault_path))
                                    _to = str(after.relative_to(self.vault_path))
                                except Exception:
                                    _from, _to = before.name, after.name
                                self._log_event("rename_local", cast_id=plan.cast_id, **{"from": _from, "to": _to, "peer": plan.peer_name})
                                # Cascade rename in LOCAL cast
                                try:
                                    self._rename_cascade(self.vault_path, _from, _to, "local (conflict KEEP_PEER)")
                                except Exception: pass
                                self.summary.counts["rename_local"] = self.summary.counts.get("rename_local", 0) + 1
                                self.summary.items.append(SummaryItem("rename_local", plan.cast_id, plan.peer_name, _to, None, f"local: {_from} → {_to}"))
                        if plan.peer_path:
                            self._safe_copy(
                                plan.peer_path, plan.local_path, provenance=plan.peer_name
                            )
                            self._update_baseline_both(
                                plan.cast_id,
                                plan.peer_name,
                                plan.peer_digest or "",
                                plan.peer_root,
                                local_rel=self._local_rel(plan.local_path),
                                peer_rel=self._peer_rel_str(plan.peer_name, plan.peer_root, plan.peer_path),
                            )
                        try:
                            local_rel = str(plan.local_path.relative_to(self.vault_path))
                        except Exception:
                            local_rel = plan.local_path.name
                        self.summary.counts["conflict_keep_peer"] = self.summary.counts.get("conflict_keep_peer", 0) + 1
                        self.summary.conflicts_resolved += 1
                        self.summary.items.append(
                            SummaryItem("conflict", plan.cast_id, plan.peer_name, local_rel, None, "resolved: KEEP_PEER")
                        )
                    else:
                        # Skip - baseline not updated
                        conflicts.append(plan)
                        try:
                            local_rel = str(plan.local_path.relative_to(self.vault_path))
                        except Exception:
                            local_rel = plan.local_path.name
                        self.summary.conflicts_open += 1
                        self.summary.items.append(
                            SummaryItem("conflict", plan.cast_id, plan.peer_name, local_rel, None, "skipped (unresolved)")
                        )

            except Exception as e:
                logger.error(f"Error syncing {plan.local_path.name}: {e}")
                exit_code = 1

        # Save updated sync state
        self._save_syncstate()

        # Set exit code
        if conflicts:
            exit_code = 3
        if self.summary:
            self.summary.finished = datetime.now().strftime("%Y-%m-%d %H:%M")

        return exit_code

    def _update_baseline(self, cast_id: str, peer_name: str, digest: str,
                         local_rel: str | None = None, peer_rel: str | None = None) -> None:
        """Update baseline digest for a file/peer pair."""
        if cast_id not in self.syncstate.baselines:
            self.syncstate.baselines[cast_id] = {}
        existing = self.syncstate.baselines[cast_id].get(peer_name)
        if existing is None:
            existing = SyncStateEntry(digest=digest, ts=datetime.now().strftime("%Y-%m-%d %H:%M"))
            self.syncstate.baselines[cast_id][peer_name] = existing
        # always refresh digest/ts; update paths when provided
        existing.digest = digest
        existing.ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        if local_rel is not None:
            existing.rel = local_rel
        if peer_rel is not None:
            existing.peer_rel = peer_rel

    def sync(
        self,
        peer_filter: list[str] | None = None,
        file_filter: str | None = None,
        dry_run: bool = False,
        non_interactive: bool = False,
        cascade: bool = True,
        debug: bool = False,
        visited_roots: set[Path] | None = None,
    ) -> int:
        """Run horizontal sync (optionally cascading to peers-of-peers)."""
        # core run for this root
        code = self._sync_core(peer_filter, file_filter, dry_run, non_interactive)
        if not cascade:
            return code

        # discover direct peers and recurse
        visited_roots = visited_roots or set()
        visited_roots.add(self.root_path.resolve())

        # Build local index (again) to get peers; cheap enough
        local_index = build_ephemeral_index(
            self.root_path, self.vault_path, fixup=True, limit_file=file_filter
        )
        peers = local_index.all_peers()
        # Skip self on cascade too
        if self.config.cast_name in peers:
            peers.discard(self.config.cast_name)
        for name in peers:
            vpath = self._resolve_peer_vault_path(name)
            if not vpath:
                continue
            peer_root = vpath.parent.resolve()
            if peer_root in visited_roots:
                continue
            try:
                code2 = HorizontalSync(peer_root).sync(
                    None,
                    file_filter,
                    dry_run,
                    non_interactive,
                    cascade=True,
                    debug=debug,
                    visited_roots=visited_roots,
                )
                code = max(code, code2)
            except Exception as e:
                logger.warning(f"Cascade sync failed for peer '{name}' at {peer_root}: {e}")
        return code
