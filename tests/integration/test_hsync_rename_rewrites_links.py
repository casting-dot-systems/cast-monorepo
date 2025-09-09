import os
from pathlib import Path

from cast_core.registry import register_cast
from cast_core.yamlio import write_cast_file, ensure_cast_fields
from cast_sync import HorizontalSync


def _mk_note(cast_id: str, peers: list[str], title: str, body: str = "Body\n") -> tuple[dict, str]:
    fm = {
        "cast-id": cast_id,
        "cast-vaults": [f"{p} (live)" for p in peers],
        "cast-version": 1,
        "title": title,
    }
    fm, _ = ensure_cast_fields(fm, generate_id=False)
    return fm, body


def test_hsync_rename_updates_peer_links_end_to_end(tmp_path: Path, monkeypatch):
    """
    First-contact rename: A has file at Docs/New Name.md, B has same cast-id at Notes/Old Name.md.
    Sync from A should rename on B and rewrite links across B that pointed to the OLD path.
    """
    cast_home = tmp_path / ".cast-home"
    cast_home.mkdir()
    monkeypatch.setenv("CAST_HOME", str(cast_home))

    # Roots & vaults
    A_root = tmp_path / "A"
    B_root = tmp_path / "B"
    (A_root / ".cast").mkdir(parents=True)
    (B_root / ".cast").mkdir(parents=True)
    (A_root / "Cast").mkdir()
    (B_root / "Cast").mkdir()

    # config.yaml
    (A_root / ".cast" / "config.yaml").write_text(
        "\n".join(
            [
                f'cast-id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"',
                'cast-name: "A"',
                'cast-location: "Cast"',
            ]
        ),
        encoding="utf-8",
    )
    (B_root / ".cast" / "config.yaml").write_text(
        "\n".join(
            [
                f'cast-id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"',
                'cast-name: "B"',
                'cast-location: "Cast"',
            ]
        ),
        encoding="utf-8",
    )

    # register in isolated registry
    register_cast(A_root)
    register_cast(B_root)

    # The same logical file (same cast-id) at different paths
    cast_id = "11111111-1111-1111-1111-111111111111"
    fm, body = _mk_note(cast_id, peers=["A", "B"], title="Doc")
    # In A at NEW path
    new_rel = Path("Docs") / "New Name.md"
    (A_root / "Cast" / new_rel.parent).mkdir(parents=True, exist_ok=True)
    write_cast_file(A_root / "Cast" / new_rel, dict(fm), "Hello from A\n", reorder=True)
    # In B at OLD path
    old_rel = Path("Notes") / "Old Name.md"
    (B_root / "Cast" / old_rel.parent).mkdir(parents=True, exist_ok=True)
    write_cast_file(B_root / "Cast" / old_rel, dict(fm), "Hello from A\n", reorder=True)

    # Files in B that reference the OLD path
    (B_root / "Cast" / "Refs").mkdir(parents=True, exist_ok=True)
    (B_root / "Cast" / "Refs" / "wiki.md").write_text(
        "link: [[Notes/Old Name]] / [[Notes/Old Name|alias]]\n", encoding="utf-8"
    )
    (B_root / "Cast" / "Refs" / "md.md").write_text(
        '[x](../Notes/Old%20Name.md#h1 "t") and [y](../Notes/Old%20Name?x=y)\n', encoding="utf-8"
    )

    # Run sync from A (non-interactive)
    hs = HorizontalSync(A_root)
    rc = hs.sync(non_interactive=True, cascade=True, debug=False)
    assert rc in (0, 1)  # no conflicts expected

    # On B: file moved to NEW and links rewritten
    assert not (B_root / "Cast" / old_rel).exists()
    assert (B_root / "Cast" / new_rel).exists()

    w = (B_root / "Cast" / "Refs" / "wiki.md").read_text(encoding="utf-8")
    assert "[[Notes/Old Name]]" not in w
    assert "[[Notes/Old Name|alias]]" not in w
    assert "[[Docs/New Name]]" in w

    m = (B_root / "Cast" / "Refs" / "md.md").read_text(encoding="utf-8")
    assert "Notes/Old%20Name" not in m
    assert "../Docs/New%20Name.md#h1" in m or "../Docs/New Name.md#h1" in m
    assert "../Docs/New%20Name?x=y" in m or "../Docs/New Name?x=y" in m

    # Second sync should be a no-op regarding rename and rewrites
    hs2 = HorizontalSync(A_root)
    rc2 = hs2.sync(non_interactive=True, cascade=True, debug=False)
    assert rc2 in (0, 1)
    # files remain
    assert (B_root / "Cast" / new_rel).exists()
    assert not (B_root / "Cast" / old_rel).exists()