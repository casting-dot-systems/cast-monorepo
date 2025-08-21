from __future__ import annotations

from tests.framework import Sandbox, mk_note, read_file, write_file


def test_framework_smoke(tmp_path):
    """Prove the framework works end-to-end across three vaults."""
    with Sandbox(tmp_path) as sb:
        A = sb.create_vault("Alpha")
        B = sb.create_vault("Beta")
        C = sb.create_vault("Gamma")

        rel = A.vault_rel("hello.md")
        cid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        write_file(
            A.root / rel,
            mk_note(cast_id=cid, title="Hello", body="Hi from A", peers=["Alpha", "Beta", "Gamma"]),
        )

        # Sync from A; B and C should receive the note, baselines should be recorded
        sb.hsync(A)
        assert (B.root / rel).exists() and (C.root / rel).exists()
        assert read_file(B.root / rel) == read_file(A.root / rel)
        assert read_file(C.root / rel) == read_file(A.root / rel)

        # Modify in B, then PULL into A
        write_file(
            B.root / rel,
            mk_note(
                cast_id=cid, title="Hello", body="Edited by B", peers=["Alpha", "Beta", "Gamma"]
            ),
        )
        sb.hsync(A)  # A should pull B's change
        assert read_file(A.root / rel) == read_file(B.root / rel)

        # Watch mode: A creates a watch-only note referring to Beta → should NOT push
        watch_rel = A.vault_rel("watch.md")
        write_file(
            A.root / watch_rel,
            mk_note(
                cast_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                title="Watch",
                body="Watch-only",
                peers=["Alpha", "Beta (watch)"],
            ),
        )
        sb.hsync(A)
        assert not (B.root / watch_rel).exists(), "watch peer should not receive pushes"

        # Limit-file: ensure other files are not treated as deleted
        keep_rel = A.vault_rel("keep.md")
        write_file(
            A.root / keep_rel,
            mk_note(
                cast_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
                title="Keep",
                body="Keep me",
                peers=["Alpha", "Beta"],
            ),
        )
        sb.hsync(A, file=str(watch_rel))  # only operate on watch.md
        assert (A.root / keep_rel).exists(), (
            "unrelated file must not be deleted during file-filtered sync"
        )

        # Report JSON sanity
        rep = sb.report_json(A)
        # Note: JSON parsing might fail due to control characters, but we test the method works
        assert isinstance(rep, dict)
        assert "file_list" in rep and "peers" in rep and "files" in rep
