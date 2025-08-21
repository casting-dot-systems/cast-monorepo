from __future__ import annotations

from tests.framework import Sandbox, mk_note, read_file, write_file


def test_file_filter_prevents_spurious_deletions(tmp_path):
    with Sandbox(tmp_path) as sb:
        A = sb.create_vault("A")
        B = sb.create_vault("B")
        # Create two files
        r1 = A.vault_rel("one.md")
        r2 = A.vault_rel("two.md")
        cid1 = "10101010-1010-1010-1010-101010101010"
        cid2 = "20202020-2020-2020-2020-202020202020"
        write_file(A.root / r1, mk_note(cid1, "1", "one", peers=["A", "B"]))
        write_file(A.root / r2, mk_note(cid2, "2", "two", peers=["A", "B"]))
        sb.hsync(A)
        assert (B.root / r1).exists() and (B.root / r2).exists()

        # Now sync only r1; r2 must remain untouched
        sb.hsync(A, file=str(r1))
        assert (A.root / r2).exists() and (B.root / r2).exists()


def test_cascade_sync_three_vaults(tmp_path):
    with Sandbox(tmp_path) as sb:
        A = sb.create_vault("Alpha")
        B = sb.create_vault("Beta")
        C = sb.create_vault("Gamma")
        rel = A.vault_rel("chain.md")
        cid = "30303030-3030-3030-3030-303030303030"
        # Reference all peers to enable cascade through registry
        write_file(A.root / rel, mk_note(cid, "Chain", "cascade", peers=["Alpha", "Beta", "Gamma"]))
        sb.hsync(A)  # should cascade to B and C
        assert (B.root / rel).exists() and (C.root / rel).exists()
        assert read_file(B.root / rel) == read_file(A.root / rel)
        assert read_file(C.root / rel) == read_file(A.root / rel)
