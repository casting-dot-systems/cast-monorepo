from pathlib import Path

from cast_sync.rename import RenameSpec, update_links_for_renames


def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_update_links_handles_various_styles_and_reversed_spec(tmp_path: Path):
    """Regression: do not 'un-rename' when a reversed spec is passed; update all link styles correctly."""
    vault = tmp_path / "Cast"
    vault.mkdir(parents=True, exist_ok=True)

    # Destination (already moved)
    write(vault / "Docs/New Name.md", "# New\n")

    # References that point to OLD location (to be rewritten)
    write(
        vault / "Refs/wiki.md",
        "\n".join(
            [
                "See [[Notes/Old Name]] and [[Notes/Old Name|alias]].",
                "Bare should also change when name changed: [[Old Name]].",
            ]
        )
        + "\n",
    )
    write(
        vault / "Refs/markdown.md",
        "\n".join(
            [
                # with .md + anchor + title
                '[md](../Notes/Old%20Name.md#intro "t")',
                # extensionless + query (percent-encoded path)
                "[md2](../Notes/Old%20Name?x=y)",
                # angle-bracketed
                "[md3](<../Notes/Old Name.md#h1>)",
            ]
        )
        + "\n",
    )

    # Reference that already points to NEW (must remain unchanged)
    write(
        vault / "Refs/already_new.md",
        "[ok](../Docs/New%20Name.md#intro)\nSee [[Docs/New Name]].\n",
    )

    # INTENTIONALLY PASS REVERSED SPEC (new, old) — the function should auto-flip it.
    rep = update_links_for_renames(
        vault,
        [RenameSpec("Docs/New Name.md", "Notes/Old Name.md")],
        case_sensitive=None,
    )

    # Sanity on aggregation
    assert rep.files_changed == 2
    assert rep.total_replacements >= 4  # several distinct styles

    # Verify rewrites
    w = read(vault / "Refs/wiki.md")
    assert "[[Notes/Old Name]]" not in w
    assert "[[Notes/Old Name|alias]]" not in w
    # Full path wiki→new path
    assert "[[Docs/New Name]]" in w
    # Bare wiki should flip to new name (name changed)
    assert "[[New Name]]" in w

    m = read(vault / "Refs/markdown.md")
    assert "Notes/Old%20Name" not in m
    assert "Notes/Old Name" not in m
    # Expect correctly relativized replacements (from Refs/ to Docs/)
    assert "../Docs/New%20Name.md#intro" in m or "../Docs/New Name.md#intro" in m
    assert "../Docs/New%20Name?x=y" in m or "../Docs/New Name?x=y" in m
    assert "<../Docs/New Name.md#h1>" in m or "<../Docs/New%20Name.md#h1>" in m

    # Ensure links that already pointed at NEW stayed intact (idempotent)
    already = read(vault / "Refs/already_new.md")
    assert "../Docs/New%20Name.md#intro" in already or "../Docs/New Name.md#intro" in already
    assert "[[Docs/New Name]]" in already

    # Idempotence: run again → no changes
    rep2 = update_links_for_renames(
        vault,
        [RenameSpec("Docs/New Name.md", "Notes/Old Name.md")],
        case_sensitive=None,
    )
    assert rep2.files_changed == 0
    assert rep2.total_replacements == 0