"""Core data models for Cast Sync."""

from typing import Literal, TypedDict

from pydantic import BaseModel, Field, ConfigDict


class CastConfig(BaseModel):
    """Configuration for a Cast (in .cast/config.yaml)."""

    # Accept both alias keys (e.g., "cast-name") and field names ("cast_name")
    model_config = ConfigDict(populate_by_name=True)

    cast_version: int = Field(default=1, alias="cast-version")
    cast_id: str = Field(description="UUID4 for this Cast/Root", alias="cast-id")
    cast_name: str = Field(description="Name of this cast", alias="cast-name")
    # DEPRECATED: location is standardized to "Cast" and omitted from new configs.
    cast_location: str = Field(
        default="Cast", description="(deprecated) relative path; always 'Cast'", alias="cast-location"
    )


class InstalledVault(BaseModel):
    """(deprecated name) A peer Cast installed locally."""

    name: str = Field(description="Peer name as referenced in cast-hsync")
    filepath: str = Field(description="Absolute path to peer Cast folder")


class InstalledCodebase(BaseModel):
    """A codebase installed locally."""

    name: str = Field(description="Codebase name")
    filepath: str = Field(description="Absolute path to codebase")


class LocalConfig(BaseModel):
    """Machine-specific config (in .cast/local.yaml)."""

    # Same behavior: allow population by either alias or field name.
    model_config = ConfigDict(populate_by_name=True)

    path_to_root: str = Field(description="Absolute path to Root", alias="path-to-root")
    installed_vaults: list[InstalledVault] = Field(default_factory=list, alias="installed-vaults")
    installed_codebases: list[InstalledCodebase] = Field(
        default_factory=list, alias="installed-codebases"
    )


class VaultMode(TypedDict):
    """Vault participation mode."""

    name: str
    mode: Literal["live", "watch"]


class FileRec(TypedDict):
    """In-memory file record during hsync."""

    cast_id: str
    relpath: str
    digest: str
    peers: dict[str, Literal["live", "watch"]]  # name -> mode
    codebases: list[str]


class SyncStateEntry(BaseModel):
    """Baseline entry for a file/peer pair."""

    digest: str = Field(description="SHA256 hex digest")
    ts: str = Field(description="Timestamp YYYY-MM-DD HH:mm")
    # NEW: optional cast-relative paths captured at baseline time
    rel: str | None = Field(default=None, description="Local cast-relative path at baseline")
    peer_rel: str | None = Field(default=None, description="Peer cast-relative path at baseline")


class SyncState(BaseModel):
    """Persistent sync state (in .cast/syncstate.json)."""

    version: int = Field(default=1)
    updated_at: str = Field(description="Last update timestamp")
    baselines: dict[str, dict[str, SyncStateEntry]] = Field(
        default_factory=dict, description="cast-id -> peer -> baseline"
    )


class CastFrontMatter(TypedDict, total=False):
    """Parsed front matter fields (cast-* only)."""

    cast_id: str | None
    cast_hsync: list[str] | None
    cast_vaults: list[str] | None  # Legacy field for migration
    cast_codebases: list[str] | None
    cast_version: int | None
