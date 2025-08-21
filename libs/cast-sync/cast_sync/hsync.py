"""Horizontal sync engine with 3-way merge logic."""

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


class HorizontalSync:
    """Horizontal sync coordinator."""

    def __init__(self, root_path: Path):
        self.root_path = root_path
        self.cast_dir = root_path / ".cast"

        # Load configs
        self.config = self._load_config()
        self.syncstate = self._load_syncstate()

        # Vault path
        self.vault_path = root_path / self.config.cast_location
        self._registry = load_registry()

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
                data["baselines"][cast_id][peer_name] = {"digest": entry.digest, "ts": entry.ts}

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
                data["baselines"][cast_id][peer_name] = {"digest": entry.digest, "ts": entry.ts}
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

    def _read_cast_id(self, path: Path) -> str | None:
        try:
            fm, _, has = parse_cast_file(path)
            if has and isinstance(fm, dict):
                return fm.get("cast-id")
        except Exception:
            pass
        return None

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
        self, cast_id: str, peer_name: str, digest: str, peer_root: Path | None
    ) -> None:
        """Update baselines in local and peer syncstate (symmetrically)."""
        self._update_baseline(cast_id, peer_name, digest)
        if peer_root is None:
            return
        # In peer's syncstate, the peer name key should be *our* name.
        their_state = self._load_peer_syncstate(peer_root)
        if cast_id not in their_state.baselines:
            their_state.baselines[cast_id] = {}
        their_state.baselines[cast_id][self.config.cast_name] = SyncStateEntry(
            digest=digest, ts=datetime.now().strftime("%Y-%m-%d %H:%M")
        )
        self._save_peer_syncstate(peer_root, their_state)

    def _resolve_peer_vault_path(self, peer_name: str) -> Path | None:
        """Resolve a peer vault folder path by name.

        Resolution:
          • machine registry (resolve_cast_by_name),
          • None (unresolved).
        """
        entry = resolve_cast_by_name(peer_name)
        if entry:
            return entry.root / entry.vault_location
        return None

    def _index_peer(
        self,
        peer_name: str,
        *,
        limit_file: str | None = None,
        existing_index: EphemeralIndex | None = None,
    ) -> tuple[Path, EphemeralIndex] | None:
        """
        Resolve and index a peer vault. If `existing_index` is provided, merge
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
        Decide 3-way sync action for a file/peer pair.
        """
        cast_id = local_rec["cast_id"]
        local_digest = local_rec["digest"]

        # Get baseline
        baseline = None
        if cast_id in self.syncstate.baselines and peer_name in self.syncstate.baselines[cast_id]:
            baseline = self.syncstate.baselines[cast_id][peer_name].digest

        if peer_rec is None:
            # Peer doesn't have the file at all
            if baseline is None:
                # First contact; only push if live
                return SyncDecision.CREATE_PEER if mode == "live" else SyncDecision.NO_OP
            else:
                # Baseline exists → peer deleted OR never had it after baseline
                # If local unchanged since baseline → fast-forward accept deletion (delete local)
                # Otherwise it's a conflict (peer missing vs local modified)
                if local_digest == baseline:
                    return SyncDecision.DELETE_LOCAL
                else:
                    return SyncDecision.CONFLICT

        peer_digest = peer_rec["digest"]

        if baseline is None:
            # First contact, both exist
            if local_digest == peer_digest:
                # If same content but paths differ, prefer a rename decision.
                if local_rec["relpath"] != peer_rec["relpath"]:
                    return SyncDecision.RENAME_PEER if mode == "live" else SyncDecision.RENAME_LOCAL
                return SyncDecision.NO_OP
            else:
                return SyncDecision.CONFLICT

        # 3-way comparison
        if local_digest == baseline and peer_digest != baseline:
            # Fast-forward pull
            return SyncDecision.PULL
        elif peer_digest == baseline and local_digest != baseline:
            # Fast-forward push
            if mode == "live":
                return SyncDecision.PUSH
            else:
                return SyncDecision.NO_OP  # Watch mode, no push
        elif local_digest != baseline and peer_digest != baseline and local_digest != peer_digest:
            return SyncDecision.CONFLICT

        # At this point digests are aligned to baseline, but we may still have a path mismatch.
        if local_rec["relpath"] != peer_rec["relpath"]:
            return SyncDecision.RENAME_PEER if mode == "live" else SyncDecision.RENAME_LOCAL

        return SyncDecision.NO_OP

    def _sync_core(
        self,
        peer_filter: list[str] | None = None,
        file_filter: str | None = None,
        dry_run: bool = False,
        non_interactive: bool = False,
    ) -> int:
        """Internal core logic (single-root, no cascade)."""
        # Build local index
        logger.info(f"Indexing local vault: {self.vault_path}")
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
                )
                plans.append(plan)

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
                rec = local_index.get_by_path(file_filter)
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
                        prec = pidx.get_by_path(file_filter)
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
                    pair = self._index_peer(peer_name)  # full index to locate cast_id
                    if pair is None:
                        continue
                    peer_indices[peer_name] = pair
                peer_vault_path, peer_index = peer_indices[peer_name]
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

                if peer_digest == baseline_digest:
                    decision = SyncDecision.DELETE_PEER  # propagate local deletion
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
            return 0

        # Execute plan
        exit_code = 0
        conflicts = []

        for plan in plans:
            if plan.decision == SyncDecision.NO_OP:
                # If identical content, ensure baseline is correct.
                # Covers both "first contact identical" and "both sides converged to same digest".
                if plan.peer_digest is not None and plan.local_digest == plan.peer_digest:
                    if plan.baseline_digest != plan.local_digest:
                        self._update_baseline_both(
                            plan.cast_id, plan.peer_name, plan.local_digest, plan.peer_root
                        )
                continue

            logger.info(
                f"Executing: {plan.local_path.name} -> {plan.peer_name}: {plan.decision.value}"
            )

            try:
                if plan.decision == SyncDecision.PULL:
                    # Copy peer to local
                    if plan.peer_path:
                        self._safe_copy(plan.peer_path, plan.local_path, provenance=plan.peer_name)
                        self._update_baseline_both(
                            plan.cast_id, plan.peer_name, plan.peer_digest or "", plan.peer_root
                        )

                elif plan.decision in (SyncDecision.PUSH, SyncDecision.CREATE_PEER):
                    # Copy local to peer
                    if plan.peer_path:
                        self._safe_copy(
                            plan.local_path, plan.peer_path, provenance=self.config.cast_name
                        )
                        self._update_baseline_both(
                            plan.cast_id, plan.peer_name, plan.local_digest, plan.peer_root
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

                elif plan.decision == SyncDecision.DELETE_PEER:
                    # Propagate local deletion: remove peer and clear baselines both sides
                    if plan.peer_path:
                        plan.peer_path.unlink(missing_ok=True)
                    self._clear_baseline_both(plan.cast_id, plan.peer_name, plan.peer_root)
                    # Log peer-relative path if possible, else best-effort.
                    path_str = ""
                    if plan.peer_path:
                        try:
                            entry = resolve_cast_by_name(plan.peer_name)
                            base = (
                                (plan.peer_root / entry.vault_location)
                                if (entry and plan.peer_root)
                                else None
                            )
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

                elif plan.decision == SyncDecision.RENAME_PEER:
                    if plan.peer_path and plan.rename_to:
                        before = plan.peer_path
                        after = self._safe_move(plan.peer_path, plan.rename_to, provenance="LOCAL")
                        # Compute paths relative to the peer's vault, defensively.
                        try:
                            entry = resolve_cast_by_name(plan.peer_name)
                            base = (
                                (plan.peer_root / entry.vault_location)
                                if (entry and plan.peer_root)
                                else None
                            )
                            _from = str(before.relative_to(base)) if base else before.name
                            _to = str(after.relative_to(base)) if base else after.name
                        except Exception:
                            _from, _to = before.name, after.name
                        self._log_event(
                            "rename_peer",
                            cast_id=plan.cast_id,
                            **{"from": _from, "to": _to, "peer": plan.peer_name},
                        )
                        self._update_baseline_both(
                            plan.cast_id,
                            plan.peer_name,
                            plan.peer_digest or plan.local_digest,
                            plan.peer_root,
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
                        plan.local_path = after
                        self._update_baseline_both(
                            plan.cast_id,
                            plan.peer_name,
                            plan.peer_digest or plan.local_digest,
                            plan.peer_root,
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
                        # overwrite peer with local, then update baselines on both sides
                        if plan.local_path.exists():
                            if plan.peer_path:
                                self._safe_copy(
                                    plan.local_path,
                                    plan.peer_path,
                                    provenance=self.config.cast_name,
                                )
                            self._update_baseline_both(
                                plan.cast_id, plan.peer_name, plan.local_digest, plan.peer_root
                            )
                        else:
                            # conflict due to local deletion; KEEP_LOCAL means keep deletion → delete peer
                            if plan.peer_path:
                                plan.peer_path.unlink(missing_ok=True)
                            self._clear_baseline_both(plan.cast_id, plan.peer_name, plan.peer_root)
                    elif resolution == ConflictResolution.KEEP_PEER:
                        if plan.peer_path:
                            self._safe_copy(
                                plan.peer_path, plan.local_path, provenance=plan.peer_name
                            )
                            self._update_baseline_both(
                                plan.cast_id,
                                plan.peer_name,
                                plan.peer_digest or "",
                                plan.peer_root,
                            )
                    else:
                        # Skip - baseline not updated
                        conflicts.append(plan)

            except Exception as e:
                logger.error(f"Error syncing {plan.local_path.name}: {e}")
                exit_code = 1

        # Save updated sync state
        self._save_syncstate()

        # Set exit code
        if conflicts:
            exit_code = 3

        return exit_code

    def _update_baseline(self, cast_id: str, peer_name: str, digest: str) -> None:
        """Update baseline digest for a file/peer pair."""
        if cast_id not in self.syncstate.baselines:
            self.syncstate.baselines[cast_id] = {}

        self.syncstate.baselines[cast_id][peer_name] = SyncStateEntry(
            digest=digest, ts=datetime.now().strftime("%Y-%m-%d %H:%M")
        )

    def sync(
        self,
        peer_filter: list[str] | None = None,
        file_filter: str | None = None,
        dry_run: bool = False,
        non_interactive: bool = False,
        cascade: bool = True,
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
                    visited_roots=visited_roots,
                )
                code = max(code, code2)
            except Exception as e:
                logger.warning(f"Cascade sync failed for peer '{name}' at {peer_root}: {e}")
        return code
