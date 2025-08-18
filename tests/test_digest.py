"""Test digest computation."""

from cast_core import compute_digest, normalize_yaml_for_digest


def test_digest_ignores_last_updated():
    """Test that digest ignores last-updated field."""
    front_matter1 = {
        "last-updated": "2025-08-18 10:00",
        "cast-id": "test-123",
        "cast-vaults": ["VaultA (live)"],
        "title": "Test Note",
    }

    front_matter2 = {
        "last-updated": "2025-08-19 15:30",  # Different timestamp
        "cast-id": "test-123",
        "cast-vaults": ["VaultA (live)"],
        "title": "Test Note",
    }

    body = "This is the body content."

    digest1 = compute_digest(front_matter1, body)
    digest2 = compute_digest(front_matter2, body)

    assert digest1 == digest2, "Digests should be equal when only last-updated differs"


def test_digest_changes_with_content():
    """Test that digest changes when content changes."""
    front_matter = {
        "cast-id": "test-123",
        "cast-vaults": ["VaultA (live)"],
    }

    body1 = "Original content"
    body2 = "Modified content"

    digest1 = compute_digest(front_matter, body1)
    digest2 = compute_digest(front_matter, body2)

    assert digest1 != digest2, "Digests should differ when body changes"


def test_digest_changes_with_yaml():
    """Test that digest changes when YAML fields change."""
    front_matter1 = {
        "cast-id": "test-123",
        "cast-vaults": ["VaultA (live)"],
        "custom-field": "value1",
    }

    front_matter2 = {
        "cast-id": "test-123",
        "cast-vaults": ["VaultA (live)"],
        "custom-field": "value2",  # Different value
    }

    body = "Same body content"

    digest1 = compute_digest(front_matter1, body)
    digest2 = compute_digest(front_matter2, body)

    assert digest1 != digest2, "Digests should differ when YAML fields change"


def test_normalize_yaml_deterministic():
    """Test that YAML normalization is deterministic."""
    front_matter = {
        "field-z": "last",
        "field-a": "first",
        "cast-id": "test",
        "last-updated": "2025-08-18 10:00",
    }

    yaml1 = normalize_yaml_for_digest(front_matter)
    yaml2 = normalize_yaml_for_digest(front_matter)

    assert yaml1 == yaml2, "Normalized YAML should be deterministic"
    assert "last-updated" not in yaml1, "last-updated should be removed"
