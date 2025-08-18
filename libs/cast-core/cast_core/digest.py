"""Content digest computation for change detection."""

import hashlib
from io import StringIO
from typing import Any

from ruamel.yaml import YAML

# Initialize YAML for canonical output
yaml_canonical = YAML()
yaml_canonical.default_flow_style = False
yaml_canonical.width = 4096


def normalize_yaml_for_digest(front_matter: dict[str, Any]) -> str:
    """
    Canonicalize YAML for digest computation.
    Removes 'last-updated' and outputs deterministic YAML.
    """
    # Copy and remove last-updated
    clean_fm = {k: v for k, v in front_matter.items() if k != "last-updated"}

    # Sort keys for deterministic output
    sorted_fm = dict(sorted(clean_fm.items()))

    # Dump to canonical YAML
    stream = StringIO()
    yaml_canonical.dump(sorted_fm, stream)
    return stream.getvalue()


def normalize_body(body: str) -> str:
    """Normalize body text (line endings to LF)."""
    return body.replace("\r\n", "\n").replace("\r", "\n")


def compute_digest(front_matter: dict[str, Any], body: str) -> str:
    """
    Compute SHA256 digest of file content.

    Args:
        front_matter: Parsed YAML front matter
        body: Markdown body text

    Returns:
        SHA256 hex digest string
    """
    # Canonicalize YAML (excluding last-updated)
    canonical_yaml = normalize_yaml_for_digest(front_matter)

    # Normalize body
    normalized_body = normalize_body(body)

    # Combine for digest
    digest_input = f"{canonical_yaml}---\n{normalized_body}"

    # Compute SHA256
    hasher = hashlib.sha256()
    hasher.update(digest_input.encode("utf-8"))

    return hasher.hexdigest()
