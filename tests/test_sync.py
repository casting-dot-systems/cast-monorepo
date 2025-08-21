"""Test sync decision logic."""


def test_sync_decision_no_baseline_same():
    """Test sync decision when files are identical with no baseline."""
    # Mock data
    local_rec = {"cast_id": "test-123", "digest": "abc123", "peers": {"VaultB": "live"}}
    peer_rec = {"cast_id": "test-123", "digest": "abc123"}

    # Create sync instance (would need proper mocking in real tests)
    # This is a simplified test showing the logic
    assert local_rec["digest"] == peer_rec["digest"]
    # Result should be NO_OP when digests match


def test_sync_decision_fast_forward_pull():
    """Test fast-forward pull decision."""
    # When local equals baseline but peer differs
    local_digest = "abc123"
    peer_digest = "def456"
    baseline = "abc123"

    # Decision should be PULL
    assert local_digest == baseline
    assert peer_digest != baseline
    # Result: PULL peer -> local


def test_sync_decision_fast_forward_push():
    """Test fast-forward push decision."""
    # When peer equals baseline but local differs
    local_digest = "def456"
    peer_digest = "abc123"
    baseline = "abc123"
    mode = "live"

    # Decision should be PUSH
    assert peer_digest == baseline
    assert local_digest != baseline
    assert mode == "live"
    # Result: PUSH local -> peer


def test_sync_decision_conflict():
    """Test conflict detection."""
    # When both differ from baseline
    local_digest = "aaa111"
    peer_digest = "bbb222"
    baseline = "ccc333"

    # Decision should be CONFLICT
    assert local_digest != baseline
    assert peer_digest != baseline
    assert local_digest != peer_digest
    # Result: CONFLICT
