"""Simple test for rename functionality without imports."""


def test_rename_logic():
    """Test the rename decision logic without importing the full module."""

    # Simulate the decision logic from _decide_sync
    def decide_rename(local_relpath, peer_relpath, local_digest, peer_digest, mode, has_any_live=True):
        """Simplified rename decision logic."""
        if local_digest == peer_digest:  # Same content
            if local_relpath != peer_relpath:  # Different paths
                if mode == "live":
                    return "rename_peer"
                # Watch peers should not force local rename when any live peers exist
                return "no_op" if has_any_live else "rename_local"
        return "no_op"

    # Test case 1: Same content, different paths, live mode
    result = decide_rename("Notes/Test.md", "Projects/Test.md", "abc123", "abc123", "live")
    assert result == "rename_peer", f"Expected rename_peer, got {result}"

    # Test case 2: Same content, different paths, watch mode (with a live peer elsewhere)
    result = decide_rename("Notes/Test.md", "Projects/Test.md", "abc123", "abc123", "watch", has_any_live=True)
    assert result == "no_op", f"Expected no_op, got {result}"

    # Test case 2b: All peers are watch-only → allow local alignment to a watch peer
    result = decide_rename("Notes/Test.md", "Projects/Test.md", "abc123", "abc123", "watch", has_any_live=False)
    assert result == "rename_local", f"Expected rename_local, got {result}"

    # Test case 3: Same content, same paths
    result = decide_rename("Notes/Test.md", "Notes/Test.md", "abc123", "abc123", "live")
    assert result == "no_op", f"Expected no_op, got {result}"

    # Test case 4: Different content (should not rename)
    result = decide_rename("Notes/Test.md", "Projects/Test.md", "abc123", "def456", "live")
    assert result == "no_op", f"Expected no_op, got {result}"


def test_safe_dest_logic():
    """Test safe destination path generation logic."""

    def safe_dest_logic(base_name, suffix, existing_names):
        """Simplified safe destination logic."""
        if base_name not in existing_names:
            return base_name

        stem, ext = base_name.rsplit(".", 1) if "." in base_name else (base_name, "")
        candidate = f"{stem} {suffix}.{ext}" if ext else f"{stem} {suffix}"

        i = 2
        while candidate in existing_names:
            candidate = f"{stem} {suffix} {i}.{ext}" if ext else f"{stem} {suffix} {i}"
            i += 1

        return candidate

    # Test case 1: No collision
    result = safe_dest_logic("Test.md", "(~from Peer)", [])
    assert result == "Test.md", f"Expected Test.md, got {result}"

    # Test case 2: Collision, add suffix
    result = safe_dest_logic("Test.md", "(~from Peer)", ["Test.md"])
    assert result == "Test (~from Peer).md", f"Expected Test (~from Peer).md, got {result}"

    # Test case 3: Double collision, add counter
    existing = ["Test.md", "Test (~from Peer).md"]
    result = safe_dest_logic("Test.md", "(~from Peer)", existing)
    assert result == "Test (~from Peer) 2.md", f"Expected Test (~from Peer) 2.md, got {result}"


if __name__ == "__main__":
    test_rename_logic()
    test_safe_dest_logic()
    print("All simple rename tests passed!")
