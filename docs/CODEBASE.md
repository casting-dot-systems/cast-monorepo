# Cast Codebases & `cbsync` (single‑home cast)

Cast Codebase lets you sync selected notes between a Cast and a code repo under:

```
<codebase>/docs/cast/
```

> **YAML invariants**
>
> - `cast-hsync` entries are **alphabetically sorted** (`Name (mode)`), de‑duplicated (preferring `live`).
> - `cast-codebases` is de‑duplicated and **alphabetically sorted**.
> - Files missing `cast-id`/`cast-version` are fixed during index/build.

It's separate from peer‑to‑peer `hsync`.

## Install & register a codebase (link to one Cast)

```bash
mkdir -p /path/to/nuu-core/docs/cast
cast codebase install /path/to/nuu-core -n nuu-core --to-cast cast
cast codebase list
```

## Zero‑config agent flow (codebase side)

Agents can drop plain Markdown in `docs/cast/` with **no YAML**. On the next
`cast cbsync` run **inside the codebase**, the CLI will add front‑matter:

```yaml
cast-id: "<generated>"
cast-version: 1
cast-hsync:
  - "<origin-cast> (live)"
cast-codebases:
  - "<this-codebase>"
```

The file is then synced to the linked Cast.

## Mark a note as belonging to a codebase (cast side)

Add to the note's YAML:

```yaml
cast-codebases:
  - nuu-core
```

You don't need to add `cast-hsync`; `cbsync` will ensure there is an origin entry
(`"<this-cast-name> (live)"`) on first sync to the codebase.

## Sync

```bash
# from a Cast: sync everything that participates in nuu-core
cast cbsync nuu-core

# just one file (by relpath or cast-id)
cast cbsync nuu-core --file Cast/Notes/plan.md

# from the codebase root (uses the linked cast)
cd /path/to/nuu-core
cast cbsync
```

### What it does

- Copies changes both ways (3‑way, rename‑aware).
- Stores baselines under peer key `cb:nuu-core` inside `.cast/syncstate.json`.
- Conflicts show a side‑by‑side diff; non‑interactive default keeps LOCAL.

### What it doesn't do

- It does not cascade to other casts (that is `hsync`'s job).

## Agent protocol (codebase side) — summary

- Write Markdown in `docs/cast/`. YAML is optional—`cast cbsync` will add it.
- `cast-id` and `cast-version` are assigned if missing.
- `cast-codebases: [<this-codebase>]` and `cast-hsync: ["<origin> (live)"]` are ensured.