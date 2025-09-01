# Cast Sync - MVP Implementation

A local Markdown synchronization system that safely syncs participating files across multiple vaults using 3-way merge detection.

## Installation

```bash
# Install uv if you haven't already
pip install uv

# Sync dependencies
uv sync --all-packages

# Run tests
uv run pytest
```

## Quick Start

### 1. Initialize a Cast

```bash
cd your-project
uv run cast init --name "MyVault"
```

This creates:
- `.cast/` directory with configuration
- `Cast/` folder for your Markdown files

### 2. Setup Local Configuration

```bash
uv run cast setup
```

Creates `.cast/local.yaml` for machine-specific settings.

### 3. Add Peer Vaults

```bash
uv run cast add-vault "PeerVault" "/path/to/other/vault"
```

### 4. Create Participating Files

Files must have Cast front matter to participate:

```yaml
---
last-updated: 2025-08-18 14:30
cast-id: (auto-generated if missing)
cast-vaults:
  - MyVault (live)
  - PeerVault (live)
cast-codebases: []
cast-version: 1
---
```

### 5. Synchronize

```bash
# Dry run to see what would happen
uv run cast hsync --dry-run

# Actual sync
uv run cast hsync

# Sync specific file
uv run cast hsync --file "Note.md"

# Sync with specific peer only
uv run cast hsync --peer PeerVault
```

## Modes

- **live**: Full bidirectional sync (push & pull)
- **watch**: Receive-only (pull from peer, never push)

## Architecture

### Monorepo Structure

```
cast-monorepo/
├── libs/
│   ├── cast-core/     # YAML parsing, digests, models
│   ├── cast-sync/     # 3-way sync logic, conflict handling
│   └── cast-git/      # (Future: git integration)
├── apps/
│   └── cast-cli/      # CLI application
├── tests/             # Test suite
└── example/           # Example casts for testing
```

### Key Components

1. **Ephemeral Indexing**: No persistent index cache; builds fresh in-memory index each run
2. **3-Way Merge**: Tracks baselines in `syncstate.json` for safe change detection
3. **Atomic Writes**: Uses temp files + rename for safe file operations
4. **Conflict Resolution**: Interactive prompts or automatic (keep local) with sidecar files

## Commands

- `cast init` - Initialize a new Cast
- `cast setup` - Create local configuration
- `cast add-vault` - Add a peer vault
- `cast hsync` - Run horizontal sync
- `cast doctor` - Check configuration
- `cast report` - Generate JSON report of files and peers

## Development

```bash
# Format code
uv run poe fmt

# Run linter
uv run poe lint

# Type check
uv run poe check

# Run all checks
uv run poe all
```

## Example

See `example/` directory for a working two-cast setup:
- `cast1/` - CastA with initial file
- `cast2/` - CastB as peer

To test:
```bash
cd example/cast1
uv run cast hsync  # Syncs to cast2
```

## Design Principles

1. **Stateless by default** - Only `syncstate.json` persists between runs
2. **Conservative conflict detection** - Never silently overwrite changes
3. **File identity via UUID** - `cast-id` field, not filename
4. **Digest-based change detection** - SHA256 of canonical content
5. **No deletions in MVP** - Files are never deleted, only created/updated

## Limitations (MVP)

- Local sync only (no remote/network)
- Whole-file sync (no line-level merges)
- Manual conflict resolution
- No background file watching
- No git integration (vsync placeholder only)

## Future Enhancements

- Git integration for vertical sync
- Semantic YAML merging
- File deletion propagation
- Background file watchers
- Multi-device discovery
- Codebase publishing