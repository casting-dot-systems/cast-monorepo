"""YAML front matter parsing and manipulation."""

import re
import re as _re
import uuid
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

# Initialize YAML parser with round-trip preservation
yaml = YAML()
yaml.preserve_quotes = True
yaml.default_flow_style = False
yaml.width = 4096  # Avoid line wrapping


CAST_FIELDS_ORDER = ["cast-id", "cast-vaults", "cast-codebases", "cast-version"]
VAULT_ENTRY_REGEX = re.compile(r"^\s*(?P<name>[^()]+?)\s*\((?P<mode>live|watch)\)\s*$")
FM_RE = _re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?", _re.DOTALL)


def parse_cast_file(filepath: Path) -> tuple[dict[str, Any] | None, str, bool]:
    """
    Parse a Markdown file with YAML front matter.

    Returns:
        (front_matter, body, has_cast_fields)
    """
    content = filepath.read_text(encoding="utf-8")

    # Find front matter (supports LF and CRLF)
    m = FM_RE.match(content)
    if not m:
        return None, content, False

    yaml_text = m.group(1)
    body = content[m.end() :]

    try:
        front_matter = yaml.load(yaml_text)
        if not isinstance(front_matter, dict):
            return None, content, False
    except YAMLError:
        return None, content, False

    # Check if it has any cast-* fields
    has_cast_fields = any(k.startswith("cast-") for k in front_matter)

    return front_matter, body, has_cast_fields


def extract_cast_fields(front_matter: dict[str, Any]) -> dict[str, Any]:
    """Extract only cast-* fields from front matter."""
    return {k: v for k, v in front_matter.items() if k.startswith("cast-")}


def parse_vault_entries(entries: list[str] | None) -> dict[str, str]:
    """
    Parse cast-vaults entries into {name: mode} dict.
    Invalid entries are ignored.
    """
    if not entries:
        return {}

    result = {}
    for entry in entries:
        if not isinstance(entry, str):
            continue
        match = VAULT_ENTRY_REGEX.match(entry)
        if match:
            result[match.group("name")] = match.group("mode")

    return result


def ensure_cast_fields(
    front_matter: dict[str, Any], generate_id: bool = True
) -> tuple[dict[str, Any], bool]:
    """
    Ensure cast-id exists and validate cast-vaults format.

    Returns:
        (updated_front_matter, was_modified)
    """
    modified = False

    if "last-updated" not in front_matter:
        front_matter["last-updated"] = ""

    # Generate cast-id if missing
    if generate_id and "cast-id" not in front_matter:
        front_matter["cast-id"] = str(uuid.uuid4())
        modified = True

    # Ensure cast-version
    if "cast-version" not in front_matter:
        front_matter["cast-version"] = 1
        modified = True

    # NOTE: Do not mutate 'cast-vaults' here. Invalid entries are handled at routing time.

    return front_matter, modified


def reorder_cast_fields(front_matter: dict[str, Any]) -> dict[str, Any]:
    """
    Reorder cast-* fields to canonical order after last-updated.
    """
    # Create new ordered dict
    result = {}

    # First, preserve any fields before cast-*
    cast_fields = {}
    other_fields = {}

    for key, value in front_matter.items():
        if key.startswith("cast-"):
            cast_fields[key] = value
        else:
            other_fields[key] = value

    # Add non-cast fields first
    if "last-updated" in other_fields:
        result["last-updated"] = other_fields.pop("last-updated")

    # Add cast fields in canonical order
    for field in CAST_FIELDS_ORDER:
        if field in cast_fields:
            result[field] = cast_fields[field]

    # Add any remaining fields
    result.update(other_fields)

    return result


def write_cast_file(
    filepath: Path, front_matter: dict[str, Any], body: str, reorder: bool = True
) -> None:
    """Write a Markdown file with YAML front matter."""
    if reorder:
        front_matter = reorder_cast_fields(front_matter)

    # Write YAML to string
    stream = StringIO()
    yaml.dump(front_matter, stream)
    yaml_text = stream.getvalue()

    # Combine with body
    content = f"---\n{yaml_text}---\n{body}"

    # Write atomically
    temp_path = filepath.parent / f".{filepath.name}.casttmp"
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(filepath)
