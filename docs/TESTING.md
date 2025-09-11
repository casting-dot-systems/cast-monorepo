# Cast Testing Framework

This repo ships with a comprehensive, sandboxed **E2E testing framework** that drives the `cast` CLI across multiple local casts with a fully isolated machine registry (`CAST_HOME`). It's designed to be:

- **Deterministic & isolated** (no pollution of your real `~/.cast`)
- **Behavioral** (asserts the *intended effect* in files across casts)
- **Extensible** (simple helpers + a small scenario DSL)

## Quick start

```bash
poe itest      # run integration suite only
poe test       # run unit + integration
poe all        # format, lint, type-check, and test
```

Manual sandbox for demos:

```bash
poe sandbox        # builds ./sandbox, creates 3 casts, runs hsync, prints a report
poe sandbox:clean  # removes ./sandbox and uninstalls any registered casts within it
```

## Design

- **Sandboxed registry**: All framework tests set `CAST_HOME` to a temp directory. No global state is touched.
- **Three-cast topology**: Helpers create 3 roots (A/B/C) to cover push/pull/cascade/watch/conflicts.
- **Real CLI**: Tests call the actual Typer `app` with `CliRunner` (like a terminal), optionally with `input` to drive interactive conflicts.
- **First-class cleanup**: Every test uninstalls registered casts and removes files, even on failure.

## Writing tests

Use the helpers in `tests/framework`:

```python
from tests.framework.sandbox import Sandbox
from tests.framework.files import mk_note, write_file, read_file

def test_my_feature(tmp_path):
    with Sandbox(tmp_path) as sb:
        A = sb.create_cast("Alpha")
        B = sb.create_cast("Beta")
        C = sb.create_cast("Gamma")

        # Arrange: create a note in A with peers B,C
        rel = A.vault_rel("note.md")
        write_file(A.root / rel, mk_note(
            cast_id="deadbeef-dead-beef-dead-beefdeadbeef",
            title="Demo",
            body="Hello",
            peers=["Alpha", "Beta", "Gamma"]  # defaults to (live)
        ))

        # Act: run sync from A
        sb.hsync(A)

        # Assert: B and C received the file
        assert (B.root / rel).exists()
        assert read_file(B.root / rel) == read_file(A.root / rel)
```

### Scenario DSL (optional)

The `Scenario` helper (kept intentionally tiny) lets you chain actions:

```python
from tests.framework.sandbox import Sandbox, Scenario
from tests.framework.files import mk_note

def test_scenario(tmp_path):
    with Sandbox(tmp_path) as sb:
        A, B, C = sb.create_cast("A"), sb.create_cast("B"), sb.create_cast("C")
        rel = A.vault_rel("story.md")
        Scenario(sb)\
            .write(A, rel, mk_note("1111-...","Story","Hi", peers=["A","B","C"]))\
            .hsync(A)\
            .expect_exists(B, rel)\
            .expect_equal(A, rel, B, rel)\
            .run()
```

## Patterns you'll often need

1. **Watch mode**: Include `"CastX (watch)"` in `cast-hsync`. Pushes to watch peers are NO‑OPs.
   - **Deletions & WATCH** (now enforced):
     - If a WATCH peer deletes its copy, local is **not** deleted; baselines are cleared for that pair.
     - If local deletes the file, WATCH peers are **not** deleted; baselines are cleared for that pair.
     - LIVE peers retain previous deletion behavior (propagate/accept based on baseline digests).
2. **Interactive conflicts**: Call `sb.hsync(vault, non_interactive=False, input="2\n")` to "Keep PEER".
3. **Limit-file**: `sb.hsync(vault, file=str(relpath))` ensures no deletion pass side effects on other files.
4. **Cascade**: By default `hsync` cascades; assertions can be placed on peers' peers.
5. **Safe pushes**: When peer has a different `cast-id` at the *same path*, sync creates `(~from {cast})` file instead of overwriting.

## Extending to future features

Add small, composable helpers. Follow this approach:

- Put generic helpers in `tests/framework` (env, CLI runners, file builders).
- Add new scenario steps in `Scenario` if you find you're repeating sequences.
- For new capabilities (e.g., codebases or vsync), add focused tests under `tests/integration/`.

**Rule of thumb**: tests should read naturally "arrange → act → assert" and always clean up via the framework.

## Codebase sync tests

This repo includes basic `cbsync` tests using `pytest` and isolated registries via `CAST_HOME`.
They:

1. Register a codebase (`docs/cast`) and a cast.
2. Create a file with `cast-codebases: [nuu-core]` and run `cbsync` to create the remote copy.
3. Modify remote → pull to local, modify local → push to remote.
4. Rename & deletes on both sides.