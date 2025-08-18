"""End-to-end CLI tests using a sandbox CAST_HOME and Typer CliRunner."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict

import pytest
from typer.testing import CliRunner

from cast_cli.cli import app


runner = CliRunner()


def _write_file(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _mk_note(cast_id: str, peers: list[str], title: str, body: str = "Body") -> str:
    lines = [
        "---",
        f"cast-id: {cast_id}",
        "cast-vaults:",
    ]
    for n in peers:
        lines.append(f"- {n} (live)")
    lines.extend(
        [
            "cast-version: 1",
            f"title: {title}",
            "---",
            body,
        ]
    )
    return "\n".join(lines) + "\n"


@pytest.fixture()
def env(tmp_path: Path) -> Dict[str, str]:
    """Sandbox environment with isolated CAST_HOME."""
    cast_home = tmp_path / "CAST_HOME"
    cast_home.mkdir()
    env = os.environ.copy()
    env["CAST_HOME"] = str(cast_home)
    return env


def test_full_flow_install_list_sync(env, tmp_path: Path):
    # Create two cast roots
    root1 = tmp_path / "vault1"
    root2 = tmp_path / "vault2"
    root1.mkdir()
    root2.mkdir()

    # init both
    import os
    old_cwd = os.getcwd()
    try:
        os.chdir(root1)
        res = runner.invoke(app, ["init", "--name", "vault1"], env=env)
        assert res.exit_code == 0, res.output
        res = runner.invoke(app, ["install", "."], env=env)
        assert res.exit_code == 0, res.output

        os.chdir(root2)
        res = runner.invoke(app, ["init", "--name", "vault2"], env=env)
        assert res.exit_code == 0, res.output
        res = runner.invoke(app, ["install", "."], env=env)
        assert res.exit_code == 0, res.output
    finally:
        os.chdir(old_cwd)

    # list casts
    res = runner.invoke(app, ["list", "--json"], env=env)
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    names = {c["name"] for c in payload["casts"]}
    assert {"vault1", "vault2"} <= names

    # create a note in vault1
    # pick a stable cast-id for determinism
    cid = "11111111-1111-1111-1111-111111111111"
    note_rel = Path("01 Vault") / "note.md"
    note1 = root1 / note_rel
    text = _mk_note(cast_id=cid, peers=["vault1", "vault2"], title="Note A", body="Hello")
    _write_file(note1, text)

    # hsync from vault1 → should push to vault2
    old_cwd = os.getcwd()
    try:
        os.chdir(root1)
        res = runner.invoke(app, ["hsync", "--non-interactive"], env=env)
        assert res.exit_code in (0, 3), res.output  # success or conflicts tolerance
    finally:
        os.chdir(old_cwd)

    note2 = root2 / note_rel
    assert note2.exists(), "note should be created in peer vault2"
    assert _read(note2) == _read(note1)

    # validate baseline exists in root1/.cast/syncstate.json
    syncstate = json.loads((root1 / ".cast" / "syncstate.json").read_text(encoding="utf-8"))
    assert cid in syncstate.get("baselines", {}), "baseline should be recorded for cast-id"

    # modify peer, then hsync from vault1 → should PULL
    _write_file(note2, _mk_note(cast_id=cid, peers=["vault1", "vault2"], title="Note A", body="PeerEdit"))
    old_cwd = os.getcwd()
    try:
        os.chdir(root1)
        res = runner.invoke(app, ["hsync", "--non-interactive"], env=env)
        assert res.exit_code in (0, 3), res.output
    finally:
        os.chdir(old_cwd)
    assert _read(note1) == _read(note2), "local note should have peer's change after PULL"


def test_first_contact_identical_sets_baseline(env, tmp_path: Path):
    root1 = tmp_path / "r1"
    root2 = tmp_path / "r2"
    root1.mkdir()
    root2.mkdir()

    import os
    old_cwd = os.getcwd()
    try:
        os.chdir(root1)
        assert runner.invoke(app, ["init", "--name", "vault1"], env=env).exit_code == 0
        assert runner.invoke(app, ["install", "."], env=env).exit_code == 0
        
        os.chdir(root2)
        assert runner.invoke(app, ["init", "--name", "vault2"], env=env).exit_code == 0
        assert runner.invoke(app, ["install", "."], env=env).exit_code == 0
    finally:
        os.chdir(old_cwd)

    cid = "22222222-2222-2222-2222-222222222222"
    rel = Path("01 Vault") / "same.md"
    body = "Same content"
    text = _mk_note(cast_id=cid, peers=["vault1", "vault2"], title="Same", body=body)
    _write_file(root1 / rel, text)
    _write_file(root2 / rel, text)

    # first contact, identical → baseline should be set (NO_OP)
    old_cwd = os.getcwd()
    try:
        os.chdir(root1)
        res = runner.invoke(app, ["hsync", "--non-interactive"], env=env)
        assert res.exit_code in (0, 3), res.output
    finally:
        os.chdir(old_cwd)

    syncstate = json.loads((root1 / ".cast" / "syncstate.json").read_text(encoding="utf-8"))
    assert cid in syncstate.get("baselines", {}), "baseline should be recorded even with NO_OP"


def test_safe_push_rename_when_peer_has_different_cast_id(env, tmp_path: Path):
    root1 = tmp_path / "r1"
    root2 = tmp_path / "r2"
    root1.mkdir()
    root2.mkdir()

    import os
    old_cwd = os.getcwd()
    try:
        os.chdir(root1)
        assert runner.invoke(app, ["init", "--name", "vault1"], env=env).exit_code == 0
        assert runner.invoke(app, ["install", "."], env=env).exit_code == 0
        
        os.chdir(root2)
        assert runner.invoke(app, ["init", "--name", "vault2"], env=env).exit_code == 0
        assert runner.invoke(app, ["install", "."], env=env).exit_code == 0
    finally:
        os.chdir(old_cwd)

    rel = Path("01 Vault") / "conflict.md"

    # local cast-id A
    cid_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    _write_file(root1 / rel, _mk_note(cast_id=cid_a, peers=["vault1", "vault2"], title="A", body="A"))

    # peer already has a different cast-id B at the same path
    cid_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    _write_file(root2 / rel, _mk_note(cast_id=cid_b, peers=["vault2"], title="B", body="B"))

    # hsync should NOT overwrite root2/conflict.md; it should create "conflict (~from vault1).md"
    old_cwd = os.getcwd()
    try:
        os.chdir(root1)
        res = runner.invoke(app, ["hsync", "--non-interactive"], env=env)
        assert res.exit_code in (0, 3), res.output
    finally:
        os.chdir(old_cwd)

    # original peer file intact
    assert (root2 / rel).exists()
    # renamed copy exists
    renamed = root2 / "01 Vault" / "conflict (~from vault1).md"
    assert renamed.exists(), "renamed file should exist to avoid destructive overwrite"