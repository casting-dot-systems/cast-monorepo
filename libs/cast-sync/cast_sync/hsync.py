"""Horizontal sync engine with 3-way merge logic."""

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, Set

from cast_core import CastConfig, SyncState, SyncStateEntry
from cast_core.registry import load_registry, resolve_cast_by_name

from cast_sync.conflict import ConflictResolution, handle_conflict
from cast_sync.index import EphemeralIndex, build_ephemeral_index
from cast_core.yamlio import parse_cast_file

logger = logging.getLogger(__name__)


class SyncDecision(Enum):
    """Sync decision for a file/peer pair."""

    NO_OP = "no_op"
    PULL = "pull"
    PUSH = "push"
    CONFLICT = "conflict"
    CREATE_PEER = "create_peer"
    CREATE_LOCAL = "create_local"


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
        with open(config_path) as f:
            data = yaml.load(f)
        return CastConfig(**data)

    def _load_syncstate(self) -> SyncState:
        """Load sync state."""
        syncstate_path = self.cast_dir / "syncstate.json"
        if not syncstate_path.exists():
            # Create empty state
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            return SyncState(version=1, updated_at=now, baselines={})

        with open(syncstate_path) as f:
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
        with open(temp_path, "w") as f:
            json.dump(data, f, indent=2)
        temp_path.replace(syncstate_path)

    def _load_peer_syncstate(self, peer_root: Path) -> SyncState:
        """Load (or create empty) syncstate for a peer root."""
        path = peer_root / ".cast" / "syncstate.json"
        if not path.exists():
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            return SyncState(version=1, updated_at=now, baselines={})
        with open(path, "r", encoding="utf-8") as f:
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

    def _update_baseline_both(self, cast_id: str, peer_name: str, digest: str, peer_root: Path | None) -> None:
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
            # Peer doesn't have file
            if baseline is None:
                # First contact
                if mode == "live":
                    return SyncDecision.CREATE_PEER
                else:
                    return SyncDecision.NO_OP
            else:
                # File existed before, recreate
                if mode == "live":
                    return SyncDecision.CREATE_PEER
                else:
                    return SyncDecision.NO_OP

        peer_digest = peer_rec["digest"]

        if baseline is None:
            # First contact, both exist
            if local_digest == peer_digest:
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

        # Discover peers
        all_peers = local_index.all_peers()
        if peer_filter:
            all_peers = all_peers.intersection(set(peer_filter))

        logger.info(f"Found peers: {all_peers}")

        # Index each peer
        peer_indices: dict[str, tuple[Path, EphemeralIndex]] = {}
        for peer_name in all_peers:
            # Resolve peer path from local.yaml or registry
            peer_vault_path = self._resolve_peer_vault_path(peer_name)
            if not peer_vault_path:
                logger.warning(
                    f"Peer '{peer_name}' not found (neither in local.yaml nor in machine registry)."
                )
                continue

            if not peer_vault_path.exists():
                logger.warning(f"Peer vault path does not exist: {peer_vault_path}")
                continue

            # Peer root is parent of vault path
            peer_root = peer_vault_path.parent
            peer_cast_dir = peer_root / ".cast"
            if not peer_cast_dir.exists():
                logger.warning(
                    f"Peer '{peer_name}' is missing .cast/ at {peer_root}; skip. Install the peer with 'cast install'."
                )
                continue

            # Index peer
            logger.info(f"Indexing peer {peer_name}: {peer_vault_path}")
            peer_index = build_ephemeral_index(
                peer_root,
                peer_vault_path,
                fixup=False,  # Don't modify peer files during index
            )
            peer_indices[peer_name] = (peer_vault_path, peer_index)

        # Build sync plan
        plans: list[SyncPlan] = []

        for local_rec in local_index.by_id.values():
            for peer_name, mode in local_rec["peers"].items():
                if peer_name not in peer_indices:
                    continue

                peer_vault_path, peer_index = peer_indices[peer_name]
                peer_rec = peer_index.get_by_id(local_rec["cast_id"])

                decision = self._decide_sync(local_rec, peer_rec, peer_name, mode)

                local_path = self.vault_path / local_rec["relpath"]
                peer_path = None
                peer_digest = None
                peer_root: Path | None = None

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
                )
                plans.append(plan)

        # Print plan if dry run
        if dry_run:
            print("\nDry run - planned actions:")
            for plan in plans:
                if plan.decision != SyncDecision.NO_OP:
                    print(f"  {plan.local_path.name} -> {plan.peer_name}: {plan.decision.value}")
            return 0

        # Execute plan
        exit_code = 0
        conflicts = []

        for plan in plans:
            if plan.decision == SyncDecision.NO_OP:
                # First contact & identical: set baseline even if there's nothing to copy
                if (
                    plan.baseline_digest is None
                    and plan.peer_digest is not None
                    and plan.local_digest == plan.peer_digest
                ):
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
                        shutil.copy2(plan.peer_path, plan.local_path)
                        self._update_baseline_both(
                            plan.cast_id, plan.peer_name, plan.peer_digest or "", plan.peer_root
                        )

                elif plan.decision in (SyncDecision.PUSH, SyncDecision.CREATE_PEER):
                    # Copy local to peer
                    if plan.peer_path:
                        plan.peer_path.parent.mkdir(parents=True, exist_ok=True)
                        dest_path = plan.peer_path
                        # Avoid overwriting a different cast-id at same path
                        if dest_path.exists():
                            existing_id = None
                            try:
                                fm, _, has = parse_cast_file(dest_path)
                                if has and isinstance(fm, dict):
                                    existing_id = fm.get("cast-id")
                            except Exception:
                                existing_id = None
                            if existing_id and existing_id != plan.cast_id:
                                stem = dest_path.stem
                                suffix = f" (~from {self.config.cast_name})"
                                candidate = dest_path.with_name(f"{stem}{suffix}{dest_path.suffix}")
                                i = 2
                                while candidate.exists():
                                    candidate = dest_path.with_name(
                                        f"{stem}{suffix} {i}{dest_path.suffix}"
                                    )
                                    i += 1
                                dest_path = candidate

                        shutil.copy2(plan.local_path, dest_path)
                        self._update_baseline_both(
                            plan.cast_id, plan.peer_name, plan.local_digest, plan.peer_root
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
                    )

                    if resolution == ConflictResolution.KEEP_LOCAL:
                        # overwrite peer with local, then update baselines on both sides
                        if plan.peer_path:
                            plan.peer_path.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(plan.local_path, plan.peer_path)
                        self._update_baseline_both(
                            plan.cast_id, plan.peer_name, plan.local_digest, plan.peer_root
                        )
                    elif resolution == ConflictResolution.KEEP_PEER:
                        if plan.peer_path:
                            shutil.copy2(plan.peer_path, plan.local_path)
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
        visited_roots: Set[Path] | None = None,
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
        local_index = build_ephemeral_index(self.root_path, self.vault_path, fixup=True, limit_file=file_filter)
        peers = local_index.all_peers()
        for name in peers:
            vpath = self._resolve_peer_vault_path(name)
            if not vpath:
                continue
            peer_root = vpath.parent.resolve()
            if peer_root in visited_roots:
                continue
            try:
                code2 = HorizontalSync(peer_root).sync(None, file_filter, dry_run, non_interactive, cascade=True, visited_roots=visited_roots)
                code = max(code, code2)
            except Exception as e:
                logger.warning(f"Cascade sync failed for peer '{name}' at {peer_root}: {e}")
        return code
