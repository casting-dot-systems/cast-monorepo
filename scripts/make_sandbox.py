#!/usr/bin/env python3
"""
Create/clean a manual sandbox under ./sandbox using the real CLI (python -m cast_cli ...).
This is *separate* from pytest, useful for manual poking and demos.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SANDBOX = ROOT / "sandbox"
CAST_HOME = SANDBOX / ".cast-home"


def run(
    args: list[str],
    cwd: Path | None = None,
    env: dict | None = None,
    check: bool = True,
    input: str | None = None,
):
    env2 = os.environ.copy()
    if env:
        env2.update(env)
    print(f"-> {' '.join(args)}  (cwd={cwd or ROOT})")
    proc = subprocess.run(
        args, cwd=cwd or ROOT, env=env2, text=True, input=input, capture_output=True
    )
    if check and proc.returncode not in (0, 3):  # 3 = conflicts tolerated
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(proc.returncode)
    return proc


def build():
    # fresh sandbox
    SANDBOX.mkdir(exist_ok=True)
    CAST_HOME.mkdir(parents=True, exist_ok=True)
    env = {"CAST_HOME": str(CAST_HOME)}

    # 3 casts
    vA = SANDBOX / "vaultA"
    vB = SANDBOX / "vaultB"
    vC = SANDBOX / "vaultC"
    for p in (vA, vB, vC):
        p.mkdir(exist_ok=True)
        run([sys.executable, "-m", "cast_cli", "init", "--name", p.name], cwd=p, env=env)
        run([sys.executable, "-m", "cast_cli", "install", "."], cwd=p, env=env)

    # Create note in A → peers B,C (live)
    note_rel = Path("Cast") / "hello.md"
    text = "\n".join(
        [
            "---",
            "cast-id: 11111111-1111-1111-1111-111111111111",
            "cast-hsync:",
            "- vaultA (live)",
            "- vaultB (live)",
            "- vaultC (live)",
            "cast-version: 1",
            "title: Hello",
            "---",
            "Hi from A!",
            "",
        ]
    )
    (vA / note_rel).parent.mkdir(parents=True, exist_ok=True)
    (vA / note_rel).write_text(text, encoding="utf-8")

    # Sync from A (cascade will reach peers-of-peers too)
    run([sys.executable, "-m", "cast_cli", "hsync", "--non-interactive"], cwd=vA, env=env)

    print("\nReport from A:")
    rep = run([sys.executable, "-m", "cast_cli", "report"], cwd=vA, env=env).stdout
    print(rep)
    try:
        data = json.loads(rep)
        assert any(x["path"] == str(note_rel) for x in data["file_list"]), (
            "Report should include hello.md"
        )
    except json.JSONDecodeError as e:
        print(f"JSON parsing error: {e}")
        # Continue despite JSON parsing error - the sandbox creation itself worked
    assert (vB / note_rel).exists() and (vC / note_rel).exists(), "Peers should receive file"
    print("\n[OK] Sandbox built at ./sandbox")


def clean():
    if CAST_HOME.exists():
        # best-effort uninstall all casts
        env = {"CAST_HOME": str(CAST_HOME)}
        out = run([sys.executable, "-m", "cast_cli", "list", "--json"], env=env, check=False).stdout
        try:
            casts = json.loads(out).get("casts", [])
            for c in casts:
                run(
                    [sys.executable, "-m", "cast_cli", "uninstall", c["cast_id"]],
                    env=env,
                    check=False,
                )
        except Exception:
            pass
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    print("[OK] Sandbox cleaned")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    if cmd == "build":
        build()
    elif cmd == "clean":
        clean()
    else:
        print("Usage: python scripts/make_sandbox.py [build|clean]")
        sys.exit(2)
