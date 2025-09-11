# Cast TUI (now plugin-based via `cast-tui`)

`cast tui` launches an interactive shell **powered by a generic framework**: `cast-tui`.

- The **TUI core** (`cast-tui`) owns the loop, keybindings, and **fuzzy autocomplete**.
- Cast specifics (file index, preview, sync) are implemented as a **plugin** (`cast_cli.tui_plugin.CastTUIPlugin`).

## TL;DR

```bash
uv tool install --editable ./apps/cast-cli   # installs cast-cli (depends on cast-tui)
cast tui
```

## Features (unchanged UX)

* **Fuzzy autocompletion** (Prompt Toolkit) for file paths; titles/cast-id shown in the menu.
* **Preview** YAML (subset) + first 60 lines of body.
* **Edit** via `$EDITOR` (best effort).
* **Sync** whole cast or a selected file.
* **Live reindex** with `Ctrl‑R`.
* `report` and `peers` helpers.

## Commands

```
open <file>     # (default) preview YAML + body
view <file>     # alias for open
edit <file>     # open file in $EDITOR
sync [<file>]   # run HorizontalSync; file limits scope
report          # show counts of files/peers/codebases
peers           # list peers referenced in the cast
help            # help text
quit | exit     # leave TUI
```

## The plugin API (for other apps/libs)

Write a plugin that implements:

```python
from cast_tui import Plugin, TerminalContext, Command

class MyPlugin(Plugin):
    def register(self, ctx: TerminalContext) -> None:
        # register commands, completers, status bar, keybindings, etc.
        ctx.app.register_command(Command(
            name="hello",
            description="Say hello",
            handler=lambda c, a: c.console.print("hello!")
        ))

    def bottom_toolbar(self, ctx: TerminalContext):
        return "demo • press help"

    def prompt(self, ctx: TerminalContext) -> str:
        return "demo:tui> "

    def default_command(self, ctx: TerminalContext) -> str:
        return "hello"
```

Then wire it up:

```python
from cast_tui import TerminalApp
from my_module import MyPlugin

app = TerminalApp()
app.register_plugin(MyPlugin())
app.run()
```

**Autocomplete:** supply a `prompt_toolkit` `Completer` on each `Command` to plug into the TUI's global fuzzy autocomplete.

## Agent terminal (future)

This setup makes it straightforward to ship an `AgentPlugin`:

* `ask <question>` executes via your agent library (backend).
* `history`, `run`, etc., show results streamed to the Rich console.
* Works inside the same TUI—no coupling to Cast internals.