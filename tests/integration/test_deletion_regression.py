from __future__ import annotations

from tests.framework import Sandbox, mk_note, write_file, read_file


def test_local_delete_with_other_file_does_not_undelete(tmp_path):
    """
    Regression: if a peer was cached with a limited index (from other files),
    deleting a different file locally must NOT be undone by the peer.
    """
    with Sandbox(tmp_path) as sb:
        A = sb.create_vault("A")
        B = sb.create_vault("B")

        # Two files referencing the same peer B
        rel_keep = A.vault_rel("keep.md")
        rel_gone = A.vault_rel("gone.md")
        cid_keep = "11111111-aaaa-bbbb-cccc-111111111111"
        cid_gone = "22222222-aaaa-bbbb-cccc-222222222222"

        # Create both in A and B so baseline can be established.
        write_file(A.root / rel_keep, mk_note(cid_keep, "Keep", "keep", peers=["A", "B"]))
        write_file(B.root / rel_keep, mk_note(cid_keep, "Keep", "keep", peers=["A", "B"]))
        write_file(A.root / rel_gone, mk_note(cid_gone, "Gone", "gone", peers=["A", "B"]))
        write_file(B.root / rel_gone, mk_note(cid_gone, "Gone", "gone", peers=["A", "B"]))

        # Establish baselines
        sb.hsync(A)

        # Delete the target file locally in A
        (A.root / rel_gone).unlink()

        # Run hsync; previously, a limited peer index could cause the baseline to be
        # cleared and the peer would push the file back. Now, it must stay deleted.
        sb.hsync(A)

        assert not (A.root / rel_gone).exists(), "Local deleted file must remain deleted"
        assert not (B.root / rel_gone).exists(), "Peer file must be deleted (propagate deletion)"

        # The other file remains present on both sides
        assert (A.root / rel_keep).exists() and (B.root / rel_keep).exists()
        assert read_file(A.root / rel_keep) == read_file(B.root / rel_keep)