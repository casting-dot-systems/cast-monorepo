import os
from pathlib import Path
import json

import pytest

from cast_core.registry import register_cast, register_codebase, load_registry
from cast_core.yamlio import write_cast_file, ensure_cast_fields
from cast_sync.cbsync import CodebaseSync
from cast_sync.index import build_ephemeral_index


def _mk_cast(root: Path, name: str) -> Path:
    (root / ".cast").mkdir(parents=True, exist_ok=True)
    (root / "Cast").mkdir(parents=True, exist_ok=True)
    cfg = {
        "cast-version": 1,
        "cast-id": "11111111-1111-4111-8111-111111111111",
        "cast-name": name,
    }
    import ruamel.yaml
    y = ruamel.yaml.YAML()
    with open(root / ".cast" / "config.yaml", "w", encoding="utf-8") as f:
        y.dump(cfg, f)
    return root


def _mk_codebase(root: Path) -> Path:
    path = root / "docs" / "cast"
    path.mkdir(parents=True, exist_ok=True)
    return root


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_note(path: Path, fm: dict, body: str):
    fm, _ = ensure_cast_fields(fm, generate_id=True)
    write_cast_file(path, fm, body, reorder=True)


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    cast_home = tmp_path / ".cast-home"
    cast_home.mkdir()
    monkeypatch.setenv("CAST_HOME", str(cast_home))
    yield


def test_cbsync_create_and_roundtrip(tmp_path):
    # Arrange: cast + codebase
    cast_root = _mk_cast(tmp_path / "CastA", "Alpha")
    register_cast(cast_root)
    cb_root = _mk_codebase(tmp_path / "nuu-core")
    register_codebase("nuu-core", cb_root)

    # Create a note in Cast that participates in nuu-core
    rel = Path("Notes/demo.md")
    note_path = cast_root / "Cast" / rel
    note_path.parent.mkdir(parents=True, exist_ok=True)
    _write_note(
        note_path,
        {"cast-codebases": ["nuu-core"], "title": "Demo"},
        "Hello Codebase!\n",
    )

    # Act: cbsync
    code = CodebaseSync(cast_root).sync("nuu-core", non_interactive=True)
    assert code == 0

    # Assert: remote file created with same relpath
    remote_path = cb_root / "docs" / "cast" / rel
    assert remote_path.exists()
    assert "Hello Codebase!" in _read(remote_path)

    # Modify remote, pull back
    _write_note(remote_path, {"cast-codebases": ["nuu-core"], "title": "Demo R"}, "Changed Remote\n")
    code = CodebaseSync(cast_root).sync("nuu-core", non_interactive=True)
    assert code == 0
    assert "Changed Remote" in _read(note_path)

    # Modify local, push
    _write_note(note_path, {"cast-codebases": ["nuu-core"], "title": "Demo L"}, "Local Edit\n")
    code = CodebaseSync(cast_root).sync("nuu-core", non_interactive=True)
    assert code == 0
    assert "Local Edit" in _read(remote_path)


def test_cbsync_rename_and_delete(tmp_path):
    cast_root = _mk_cast(tmp_path / "CastB", "Beta")
    register_cast(cast_root)
    cb_root = _mk_codebase(tmp_path / "nuu-core")
    register_codebase("nuu-core", cb_root)

    rel = Path("Docs/plan.md")
    note = cast_root / "Cast" / rel
    note.parent.mkdir(parents=True, exist_ok=True)
    _write_note(note, {"cast-codebases": ["nuu-core"], "title": "Plan"}, "v1\n")
    CodebaseSync(cast_root).sync("nuu-core", non_interactive=True)
    remote = cb_root / "docs" / "cast" / rel
    assert remote.exists()

    # Rename locally
    new_rel = Path("Docs/plan-v2.md")
    new_note = cast_root / "Cast" / new_rel
    new_note.parent.mkdir(parents=True, exist_ok=True)
    os.rename(note, new_note)
    # No content change; cbsync should rename remote
    CodebaseSync(cast_root).sync("nuu-core", non_interactive=True)
    new_remote = cb_root / "docs" / "cast" / new_rel
    assert new_remote.exists()
    assert not remote.exists()

    # Delete remote; cbsync should delete local if unchanged
    os.remove(new_remote)
    CodebaseSync(cast_root).sync("nuu-core", non_interactive=True)
    assert not new_note.exists()