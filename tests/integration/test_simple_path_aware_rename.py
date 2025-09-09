from __future__ import annotations

import json
from pathlib import Path
from tests.framework import Sandbox, mk_note, write_file, read_file


def test_simple_first_contact_rename_still_works(tmp_path):
    """
    Basic test: first contact rename should still work as before.
    """
    with Sandbox(tmp_path) as sb:
        A = sb.create_vault("A")
        B = sb.create_vault("B")

        cid = "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"
        a_rel = A.vault_rel("A-Path/File.md")
        b_rel = A.vault_rel("B-Path/File.md")

        # Create same file at different paths (first contact)
        write_file(A.root / a_rel, mk_note(cid, "File", "Body\n", peers=["A", "B"]))
        write_file(B.root / b_rel, mk_note(cid, "File", "Body\n", peers=["A", "B"]))

        # Sync - should rename B to match A's path
        sb.hsync(A)

        # B file should be moved to A's path
        assert not (B.root / b_rel).exists()
        assert (B.root / a_rel).exists()

        # Content should match
        assert read_file(A.root / a_rel) == read_file(B.root / a_rel)


def test_simple_baseline_establishment(tmp_path):
    """
    Test that baselines get established with path information.
    """
    with Sandbox(tmp_path) as sb:
        A = sb.create_vault("A")
        B = sb.create_vault("B")

        cid = "bbbbbbbb-2222-3333-4444-cccccccccccc"
        file_rel = A.vault_rel("Test/File.md")

        # Create same file in both vaults at same path
        write_file(A.root / file_rel, mk_note(cid, "Test", "Content\n", peers=["A", "B"]))
        write_file(B.root / file_rel, mk_note(cid, "Test", "Content\n", peers=["A", "B"]))

        # Sync to establish baseline
        sb.hsync(A)

        # Check that baseline paths were recorded
        s_local = json.loads((A.root / ".cast" / "syncstate.json").read_text())
        base = s_local["baselines"][cid]["B"]
        
        # Should have recorded the paths  
        assert "rel" in base
        assert "peer_rel" in base
        assert base["rel"] == "Test/File.md"
        assert base["peer_rel"] == "Test/File.md"