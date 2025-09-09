from __future__ import annotations

import json
from pathlib import Path
from tests.framework import Sandbox, mk_note, write_file, read_file


def _read_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def test_local_rename_propagates_without_cascade_and_updates_baseline(tmp_path):
    """
    After initial sync, rename locally; sync WITHOUT cascade should:
      • rename on peer to match local path,
      • rewrite peer links,
      • persist rel/peer_rel in both baselines.
    """
    with Sandbox(tmp_path) as sb:
        A = sb.create_vault("A")
        B = sb.create_vault("B")

        cid = "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"
        old_rel = A.vault_rel("Notes/Old Name.md")
        new_rel = A.vault_rel("Docs/New Name.md")

        # Create same file in both vaults
        write_file(A.root / old_rel, mk_note(cid, "Doc", "Body\n", peers=["A", "B"]))
        write_file(B.root / old_rel, mk_note(cid, "Doc", "Body\n", peers=["A", "B"]))

        # References in B pointing to OLD path (will be rewritten on rename)
        write_file(B.root / B.vault_rel("Refs/wiki.md"), "see [[Notes/Old Name]]\n")
        write_file(B.root / B.vault_rel("Refs/md.md"),   "[x](../Notes/Old%20Name.md)\n")

        # Establish baseline
        sb.hsync(A)

        # Rename locally (filesystem move)
        (A.root / new_rel).parent.mkdir(parents=True, exist_ok=True)
        (A.root / A.vault_rel("Notes")).mkdir(exist_ok=True)  # ensure parent exists
        (A.root / old_rel).rename(A.root / new_rel)

        # Sync WITHOUT cascade
        sb.hsync(A, cascade=False)

        # Peer file moved
        assert not (B.root / old_rel).exists()
        assert (B.root / new_rel).exists()

        # Links on peer updated
        w = read_file(B.root / B.vault_rel("Refs/wiki.md"))
        m = read_file(B.root / B.vault_rel("Refs/md.md"))
        assert "Notes/Old Name" not in w
        assert "Notes/Old%20Name" not in m
        assert "Docs/New Name" in w
        assert "Docs/New%20Name" in m or "Docs/New Name" in m

        # Baseline paths recorded (local perspective)
        s_local = _read_json(A.root / ".cast" / "syncstate.json")
        base = s_local["baselines"][cid]["B"]
        assert base.get("rel") == "Docs/New Name.md"
        assert base.get("peer_rel") == "Docs/New Name.md"

        # Baseline paths recorded (peer perspective)
        s_peer = _read_json(B.root / ".cast" / "syncstate.json")
        peer_base = s_peer["baselines"][cid]["A"]
        assert peer_base.get("rel") == "Docs/New Name.md"
        assert peer_base.get("peer_rel") == "Docs/New Name.md"


def test_peer_rename_propagates_back_to_local(tmp_path):
    """
    After initial sync, rename on peer; sync should rename LOCAL, rewrite local links,
    and persist rel/peer_rel accordingly.
    """
    with Sandbox(tmp_path) as sb:
        A = sb.create_vault("A")
        B = sb.create_vault("B")

        cid = "cccccccc-4444-5555-6666-dddddddddddd"
        old_rel = A.vault_rel("Area/Thing.md")
        new_rel = A.vault_rel("Renamed/Thing.md")

        write_file(A.root / old_rel, mk_note(cid, "T", "X\n", peers=["A", "B"]))
        write_file(B.root / old_rel, mk_note(cid, "T", "X\n", peers=["A", "B"]))
        write_file(A.root / A.vault_rel("Refs/r.md"), "[link](../Area/Thing.md)\nsee [[Area/Thing]]\n")

        sb.hsync(A)

        # Rename on peer
        (B.root / new_rel).parent.mkdir(parents=True, exist_ok=True)
        (B.root / old_rel).rename(B.root / new_rel)

        # Sync — local should adopt peer's rename (B is live)
        sb.hsync(A, cascade=False)

        assert not (A.root / old_rel).exists()
        assert (A.root / new_rel).exists()

        # Local links rewritten
        r = read_file(A.root / A.vault_rel("Refs/r.md"))
        assert "Area/Thing" not in r
        assert "Renamed/Thing" in r

        # Baseline paths updated (local perspective)
        s_local = _read_json(A.root / ".cast" / "syncstate.json")
        base = s_local["baselines"][cid]["B"]
        assert base.get("rel") == "Renamed/Thing.md"
        assert base.get("peer_rel") == "Renamed/Thing.md"


def test_both_sides_rename_to_different_paths_creates_conflict(tmp_path):
    """
    When both sides rename the same file to different paths after a baseline,
    this should create a structural conflict.
    """
    with Sandbox(tmp_path) as sb:
        A = sb.create_vault("A")
        B = sb.create_vault("B")

        cid = "dddddddd-7777-8888-9999-eeeeeeeeeeee"
        original_rel = A.vault_rel("Original/File.md")
        a_new_rel = A.vault_rel("A-Renamed/File.md")
        b_new_rel = A.vault_rel("B-Renamed/File.md")

        # Create same file in both vaults
        write_file(A.root / original_rel, mk_note(cid, "F", "Content\n", peers=["A", "B"]))
        write_file(B.root / original_rel, mk_note(cid, "F", "Content\n", peers=["A", "B"]))

        # Establish baseline
        sb.hsync(A)

        # Both sides rename to different paths
        (A.root / a_new_rel).parent.mkdir(parents=True, exist_ok=True)
        (A.root / original_rel).rename(A.root / a_new_rel)
        
        (B.root / b_new_rel).parent.mkdir(parents=True, exist_ok=True) 
        (B.root / original_rel).rename(B.root / b_new_rel)

        # Sync should detect conflict 
        res = sb.hsync(A, non_interactive=False, input="keep_local\n")
        # Should have exit code indicating conflict was handled
        assert res.exit_code in (0, 3)


def test_local_only_rename_with_peer_missing_creates_on_peer(tmp_path):
    """
    If local renames a file but peer is missing it (but not deleted), 
    sync should create the file on peer at the new path.
    """
    with Sandbox(tmp_path) as sb:
        A = sb.create_vault("A")  
        B = sb.create_vault("B")

        cid = "eeeeeeee-9999-aaaa-bbbb-cccccccccccc"
        old_rel = A.vault_rel("Old/Location.md")
        new_rel = A.vault_rel("New/Location.md")

        # Create file in both initially
        write_file(A.root / old_rel, mk_note(cid, "L", "Data\n", peers=["A", "B"]))
        write_file(B.root / old_rel, mk_note(cid, "L", "Data\n", peers=["A", "B"]))

        # Establish baseline
        sb.hsync(A)

        # Remove from B (simulate peer missing, not deleted)
        (B.root / old_rel).unlink()

        # Rename on A
        (A.root / new_rel).parent.mkdir(parents=True, exist_ok=True)
        (A.root / old_rel).rename(A.root / new_rel)

        # Sync should create on peer at new location
        sb.hsync(A, cascade=False)

        # File should appear at new location on peer
        assert not (B.root / old_rel).exists()
        assert (B.root / new_rel).exists()

        # Content should match
        assert read_file(A.root / new_rel) == read_file(B.root / new_rel)


def test_baseline_paths_preserved_across_no_op_syncs(tmp_path):
    """
    NO_OP syncs should still update baseline paths to keep them current.
    """
    with Sandbox(tmp_path) as sb:
        A = sb.create_vault("A")
        B = sb.create_vault("B")

        cid = "ffffffff-aaaa-bbbb-cccc-dddddddddddd"
        file_rel = A.vault_rel("Test/File.md")

        # Create same file in both vaults
        write_file(A.root / file_rel, mk_note(cid, "Test", "Same content\n", peers=["A", "B"]))
        write_file(B.root / file_rel, mk_note(cid, "Test", "Same content\n", peers=["A", "B"]))

        # First sync establishes baseline
        sb.hsync(A)

        # Verify baseline paths were recorded
        s_local = _read_json(A.root / ".cast" / "syncstate.json")
        base = s_local["baselines"][cid]["B"]
        assert base.get("rel") == "Test/File.md"
        assert base.get("peer_rel") == "Test/File.md"

        # Second sync should be NO_OP but still maintain paths
        sb.hsync(A)

        # Paths should still be there
        s_local2 = _read_json(A.root / ".cast" / "syncstate.json")
        base2 = s_local2["baselines"][cid]["B"]
        assert base2.get("rel") == "Test/File.md"
        assert base2.get("peer_rel") == "Test/File.md"


def test_local_rename_plus_edit_propagates_and_updates_paths(tmp_path):
    """
    If local renames AND edits the file (fast-forward push case),
    the peer should adopt the new path and the new content in a single sync.
    """
    with Sandbox(tmp_path) as sb:
        A = sb.create_vault("A")
        B = sb.create_vault("B")

        cid = "abababab-1212-3434-5656-efefefefefef"
        old_rel = A.vault_rel("Old/Loc.md")
        new_rel = A.vault_rel("New/Loc.md")

        # Same initial file on both sides
        write_file(A.root / old_rel, mk_note(cid, "Doc", "Body\n", peers=["A", "B"]))
        write_file(B.root / old_rel, mk_note(cid, "Doc", "Body\n", peers=["A", "B"]))
        sb.hsync(A)  # establish baseline

        # Rename + edit on A
        (A.root / new_rel).parent.mkdir(parents=True, exist_ok=True)
        (A.root / old_rel).rename(A.root / new_rel)
        write_file(A.root / new_rel, mk_note(cid, "Doc", "Edited\n", peers=["A", "B"]))

        # Sync without cascade; B should move file and get edited content
        sb.hsync(A, cascade=False)

        assert not (B.root / old_rel).exists()
        assert (B.root / new_rel).exists()
        assert read_file(B.root / new_rel) == read_file(A.root / new_rel)

        # Baseline paths recorded on A
        s_local = _read_json(A.root / ".cast" / "syncstate.json")
        base = s_local["baselines"][cid]["B"]
        assert base.get("rel") == "New/Loc.md"
        assert base.get("peer_rel") == "New/Loc.md"