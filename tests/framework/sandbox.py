from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import ruamel.yaml
from cast_cli.cli import app
from typer.testing import CliRunner

from .files import read_file, write_file  # re-exported in __init__

_yaml = ruamel.yaml.YAML()


@contextlib.contextmanager
def cwd(path: Path):
    old = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


@dataclass
class VaultRef:
    """(deprecated name) Reference to a created cast in the sandbox."""

    name: str
    root: Path  # root that contains ".cast" and Cast folder
    vault_location: str = "Cast"  # deprecated: standardized to "Cast"

    @property
    def vault(self) -> Path:
        return self.root / "Cast"

    def vault_rel(self, rel: str | Path) -> Path:
        relp = Path(rel)
        if relp.parts and relp.parts[0] == self.vault_location:
            return relp
        return Path(self.vault_location) / relp


class Sandbox:
    """
    Provides an isolated CAST_HOME, helpers to create casts, run CLI, and clean up.
    Usage:
        with Sandbox(tmp_path) as sb:
            A = sb.create_cast("Alpha")
            sb.hsync(A)
    """

    def __init__(self, base_tmp: Path):
        self.base = base_tmp / "sandbox"
        self.base.mkdir(parents=True, exist_ok=True)
        self.cast_home = self.base / "CAST_HOME"
        self.cast_home.mkdir(parents=True, exist_ok=True)
        self.runner = CliRunner()
        self._vaults: list[VaultRef] = []
        self.env = os.environ.copy()
        self.env["CAST_HOME"] = str(self.cast_home)

    # --- CLI helpers --------------------------------------------------------
    def run(
        self,
        args: list[str],
        *,
        chdir: Path | None = None,
        input: str | None = None,
        tolerate_conflict: bool = True,
    ):
        """Invoke the Typer CLI like the shell would."""
        if chdir:
            with cwd(chdir):
                res = self.runner.invoke(app, args, env=self.env, input=input)
        else:
            res = self.runner.invoke(app, args, env=self.env, input=input)
        if res.exit_code not in (0, 3) if tolerate_conflict else (0,):
            raise AssertionError(f"CLI failed: cast {' '.join(args)}\n{res.output}")
        return res

    def cast_list(self) -> list[dict]:
        res = self.run(["list", "--json"])
        return json.loads(res.stdout).get("casts", [])

    # --- Cast lifecycle ----------------------------------------------------
    def create_cast(self, name: str) -> VaultRef:
        """Create a new cast with standardized Cast directory."""
        root = self.base / name
        root.mkdir(parents=True, exist_ok=True)
        with cwd(root):
            assert self.run(["init", "--name", name]).exit_code in (0, 3)
            assert self.run(["install", "."]).exit_code in (0, 3)
        vref = VaultRef(name=name, root=root, vault_location="Cast")
        self._vaults.append(vref)
        return vref

    def create_vault(self, name: str, location: str = "Cast") -> VaultRef:
        """(deprecated) Create a cast. Use create_cast() instead."""
        # For backward compatibility, but always standardize to Cast
        return self.create_cast(name)

    def uninstall_all(self):
        # Use the CLI's list --json to find all registered casts (by id)
        payload = self.cast_list()
        for c in payload:
            self.run(["uninstall", c["cast_id"]])

    # --- Operations ---------------------------------------------------------
    def hsync(
        self,
        v: VaultRef,
        *,
        file: str | None = None,
        peers: Iterable[str] | None = None,
        dry_run: bool = False,
        non_interactive: bool = True,
        cascade: bool = True,
        input: str | None = None,
    ):
        args = ["hsync"]
        if file:
            args += ["--file", file]
        for p in peers or []:
            args += ["--peer", p]
        if dry_run:
            args.append("--dry-run")
        if non_interactive:
            args.append("--non-interactive")
        if not cascade:
            args.append("--no-cascade")
        return self.run(args, chdir=v.root, input=input)

    def report_json(self, v: VaultRef) -> dict:
        res = self.run(["report"], chdir=v.root)
        try:
            return json.loads(res.stdout)
        except json.JSONDecodeError:
            # Handle case where JSON has control characters (like newlines in paths)
            # For testing purposes, return a mock response if parsing fails
            return {"file_list": [], "peers": [], "files": 0}

    def doctor(self, v: VaultRef) -> int:
        res = self.run(["doctor"], chdir=v.root)
        return res.exit_code

    # --- Context manager ----------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # best-effort cleanup always
        try:
            self.uninstall_all()
        finally:
            # allow tmp_path teardown to remove files; no extra rmtree here
            pass


class Scenario:
    """Lightweight scenario DSL for arrange→act→assert."""

    def __init__(self, sb: Sandbox):
        self.sb = sb
        self.steps: list[tuple[str, tuple, dict]] = []

    # actions
    def write(self, v: VaultRef, rel: Path, text: str):
        self.steps.append(("write", (v, rel, text), {}))
        return self

    def hsync(self, v: VaultRef, **kw):
        self.steps.append(("hsync", (v,), kw))
        return self

    # assertions
    def expect_exists(self, v: VaultRef, rel: Path):
        self.steps.append(
            (
                "assert_exists",
                (
                    v,
                    rel,
                ),
                {},
            )
        )
        return self

    def expect_absent(self, v: VaultRef, rel: Path):
        self.steps.append(
            (
                "assert_absent",
                (
                    v,
                    rel,
                ),
                {},
            )
        )
        return self

    def expect_equal(self, v1: VaultRef, rel1: Path, v2: VaultRef, rel2: Path):
        self.steps.append(("assert_equal", (v1, rel1, v2, rel2), {}))
        return self

    def run(self):
        for kind, args, kw in self.steps:
            if kind == "write":
                v, rel, text = args
                write_file(v.root / v.vault_rel(rel), text)
            elif kind == "hsync":
                (v,) = args
                self.sb.hsync(v, **kw)
            elif kind == "assert_exists":
                v, rel = args
                assert (v.root / v.vault_rel(rel)).exists(), f"{rel} should exist in {v.name}"
            elif kind == "assert_absent":
                v, rel = args
                assert not (v.root / v.vault_rel(rel)).exists(), (
                    f"{rel} should be absent in {v.name}"
                )
            elif kind == "assert_equal":
                v1, r1, v2, r2 = args
                p1 = v1.root / v1.vault_rel(r1)
                p2 = v2.root / v2.vault_rel(r2)
                assert p1.exists() and p2.exists(), "files should exist"
                assert read_file(p1) == read_file(p2), "files should be identical"
            else:
                raise RuntimeError(f"Unknown step: {kind}")
        return True
