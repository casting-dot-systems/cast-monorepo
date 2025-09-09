# Cast TUI (interactive fuzzy search)

The `cast tui` command launches an interactive shell built with **prompt_toolkit**. It gives you a fast, fuzzy‑searchable view of all Cast files in the current vault with quick actions.

## Install & run

```bash
uv tool install --editable ./apps/cast-cli
cast tui
```

## What you get

* **Fuzzy autocompletion** for files by path (display shows title & cast‑id).
* **Preview** YAML (subset) + first 60 lines of body.
* **Edit** via `$EDITOR` (best effort).
* **Sync** whole vault or just a selected file.
* **Live reindex** with `Ctrl‑R`.
* Quick `report` and `peers` helpers.

## Commands

```
open <file>     # (default) preview YAML + body
view <file>     # alias for open
edit <file>     # open file in $EDITOR
sync [<file>]   # run HorizontalSync; file limits scope
report          # show counts of files/peers/codebases
peers           # list peers referenced in the vault
help            # help text
quit | exit     # leave TUI
```

**Tips**

* Use **Tab** to autocomplete (fuzzy).
* `Ctrl‑R` refreshes the index without leaving the TUI.
* You can paste a vault‑relative path (with or without the leading `Cast/`), or a `cast-id`.