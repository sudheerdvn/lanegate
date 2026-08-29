"""Tests for shared lifecycle state transitions."""

from __future__ import annotations

def test_clear_human_review_approval_removes_both_fields():
    """Every direct needs_review transition shares this invalidation helper."""
    from lanegate.lifecycle.state import _clear_human_review_approval

    ticket = {
        "protected_path_approved_at": "2026-08-10T21:00:00Z",
        "protected_path_approved_rationale": "Previously inspected state.",
        "protected_path_approved_actor": "human",
        "human_review_approved_at": "2026-08-10T21:00:00Z",
        "human_review_rationale": "Previously inspected state.",
        "human_review_actor": "human",
        "close_criteria_drift_approved_at": "2026-08-10T21:00:00Z",
        "red_lane_approved_at_sha": "deadbeef",
        "title": "keep unrelated metadata",
    }

    _clear_human_review_approval(ticket)

    assert "protected_path_approved_at" not in ticket
    assert "protected_path_approved_rationale" not in ticket
    assert "protected_path_approved_actor" not in ticket
    assert "human_review_approved_at" not in ticket
    assert "human_review_rationale" not in ticket
    assert "human_review_actor" not in ticket
    assert "red_lane_approved_at_sha" not in ticket
    assert ticket.get("close_criteria_drift_approved_at") == "2026-08-10T21:00:00Z"
    assert ticket["title"] == "keep unrelated metadata"

