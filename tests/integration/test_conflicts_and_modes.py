from __future__ import annotations

import json
from tests.framework import Sandbox, mk_note, read_file, write_file


def test_interactive_conflict_keep_peer(tmp_path):
    with Sandbox(tmp_path) as sb:
        A = sb.create_vault("Alpha")
        B = sb.create_vault("Beta")
        cid = "11111111-2222-3333-4444-555555555555"
        rel = A.vault_rel("conflict.md")

        # Both create same path but different bodies → conflict on first contact
        write_file(A.root / rel, mk_note(cid, "C", "LOCAL", peers=["Alpha", "Beta"]))
        write_file(B.root / rel, mk_note(cid, "C", "PEER", peers=["Alpha", "Beta"]))

        # Run hsync interactively from A and choose "2" => KEEP PEER
        sb.hsync(A, non_interactive=False, input="2\n")
        assert read_file(A.root / rel).endswith("PEER\n"), (
            "A should keep PEER version after conflict"
        )


def test_safe_push_rename_when_peer_has_different_cast_id(tmp_path):
    with Sandbox(tmp_path) as sb:
        A = sb.create_vault("A")
        B = sb.create_vault("B")

        rel = A.vault_rel("samepath.md")
        cid_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        cid_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

        write_file(A.root / rel, mk_note(cid_a, "A", "A", peers=["A", "B"]))
        write_file(B.root / rel, mk_note(cid_b, "B", "B", peers=["B"]))

        sb.hsync(A)  # should not overwrite B's file; should write a renamed copy
        renamed = B.root / B.vault_rel("samepath (~from A).md")
        assert (B.root / rel).exists()
        assert renamed.exists()


def test_watch_mode_no_push(tmp_path):
    with Sandbox(tmp_path) as sb:
        A = sb.create_vault("A")
        B = sb.create_vault("B")
        rel = A.vault_rel("watch.md")
        write_file(
            A.root / rel,
            mk_note("cccccccc-cccc-cccc-cccc-cccccccccccc", "W", "X", peers=["A", "B (watch)"]),
        )
        sb.hsync(A)
        assert not (B.root / rel).exists()


def test_rename_conflict_keep_peer_adopts_peer_path(tmp_path):
    """
    When both sides rename to different paths, KEEP_PEER should also adopt the peer's name/path locally.
    """
    with Sandbox(tmp_path) as sb:
        A = sb.create_vault("Alpha")
        B = sb.create_vault("Beta")

        cid = "12121212-3434-5656-7878-909090909090"
        original_rel = A.vault_rel("O/File.md")
        a_new_rel = A.vault_rel("A-R/File.md")
        b_new_rel = A.vault_rel("B-R/File.md")

        # Same file both sides
        write_file(A.root / original_rel, mk_note(cid, "F", "X\n", peers=["Alpha", "Beta"]))
        write_file(B.root / original_rel, mk_note(cid, "F", "X\n", peers=["Alpha", "Beta"]))
        sb.hsync(A)

        # Divergent renames
        (A.root / a_new_rel).parent.mkdir(parents=True, exist_ok=True)
        (A.root / original_rel).rename(A.root / a_new_rel)
        (B.root / b_new_rel).parent.mkdir(parents=True, exist_ok=True)
        (B.root / original_rel).rename(B.root / b_new_rel)

        # Resolve conflict by keeping PEER; local should adopt peer's path
        res = sb.hsync(A, non_interactive=False, input="2\n", cascade=False)
        assert res.exit_code in (0, 3)

        assert not (A.root / a_new_rel).exists()
        assert (A.root / b_new_rel).exists()
        assert read_file(A.root / b_new_rel) == read_file(B.root / b_new_rel)


def test_watch_peer_deletion_does_not_delete_local(tmp_path):
    """
    If a WATCH peer deletes its copy, local should NOT be deleted; baseline should be cleared.
    """
    with Sandbox(tmp_path) as sb:
        A = sb.create_vault("A")
        B = sb.create_vault("B")

        rel = A.vault_rel("watch_del.md")
        cid = "12121212-aaaa-bbbb-cccc-121212121212"

        # Same file both sides; mark B as WATCH
        write_file(A.root / rel, mk_note(cid, "W", "X\n", peers=["A", "B (watch)"]))
        write_file(B.root / rel, mk_note(cid, "W", "X\n", peers=["A", "B (watch)"]))
        sb.hsync(A)  # establish baseline

        # WATCH peer deletes its copy
        (B.root / rel).unlink()

        # Sync from A. Local must NOT be deleted.
        sb.hsync(A)
        assert (A.root / rel).exists(), "Local should not be deleted by WATCH peer deletion"
        assert not (B.root / rel).exists(), "Peer deletion remains (we do not recreate for WATCH)"

        # Baseline cleared for that pair (so we don't churn)
        state = json.loads((A.root / ".cast" / "syncstate.json").read_text(encoding="utf-8"))
        assert cid not in state.get("baselines", {}), "baseline should be cleared for WATCH deletion"


def test_local_deletion_does_not_delete_watch_peer(tmp_path):
    """
    If local deletes the file, a WATCH peer must NOT be deleted.
    Baseline should be cleared so future syncs don't keep scheduling a delete.
    """
    with Sandbox(tmp_path) as sb:
        A = sb.create_vault("A")
        B = sb.create_vault("B")

        rel = A.vault_rel("keep_on_watch_peer.md")
        cid = "34343434-dddd-eeee-ffff-343434343434"

        # Create both sides; B is WATCH
        write_file(A.root / rel, mk_note(cid, "K", "Body\n", peers=["A", "B (watch)"]))
        write_file(B.root / rel, mk_note(cid, "K", "Body\n", peers=["A", "B (watch)"]))
        sb.hsync(A)  # baseline

        # Local delete
        (A.root / rel).unlink()

        # Sync from A → B must NOT be deleted
        sb.hsync(A)
        assert (B.root / rel).exists(), "WATCH peer must not be deleted when local deletes"

        # Baseline cleared (so we don't attempt delete on future runs)
        s_local = json.loads((A.root / ".cast" / "syncstate.json").read_text(encoding="utf-8"))
        assert cid not in s_local.get("baselines", {}), "baseline cleared after local delete + WATCH peer"
