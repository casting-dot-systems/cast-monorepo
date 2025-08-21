from __future__ import annotations

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
