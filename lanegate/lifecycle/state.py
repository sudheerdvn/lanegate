"""Shared lifecycle state transitions and approval invalidation."""

from __future__ import annotations

import sys
from pathlib import Path

from lanegate.ticket import append_lifecycle_event, write_ticket

from . import (
    _append_ticket_section,
    _commit_generated_ticket_write,
    _stamp_status_changed,
)
from .hibernate import _remove_executor_markers

def _mark_needs_review(
    ticket: dict,
    cfg: dict,
    repo_root: Path,
    reason: str = "",
) -> None:
    """Mark a ticket needs_review regardless of current status. Internal use only."""
    tid = ticket["id"]
    current = ticket.get("status")
    if reason:
        _append_ticket_section(ticket, "## Needs Review Reason", reason)
    _clear_human_review_approval(ticket)
    ticket["status"] = "needs_review"
    _stamp_status_changed(ticket)
    append_lifecycle_event(
        ticket,
        event="needs_review",
        from_status=current,
        to_status="needs_review",
        summary=reason or "requires human review",
    )
    write_ticket(ticket)
    _remove_executor_markers(repo_root, tid)
    _commit_generated_ticket_write(repo_root, ticket["_path"], tid, "needs_review", cfg)
    print(f"{tid}: {current} → needs_review")
    if reason:
        print(f"  Reason: {reason}", file=sys.stderr)


def _clear_human_review_approval(ticket: dict) -> None:
    """Invalidate an approval when a ticket re-enters ``needs_review``.

    A human approval is tied to the exact state inspected.  Every path that
    sends a ticket back to needs_review must remove it before the protected
    path checks in cmd_start/cmd_reopen can be evaluated again.
    """
    ticket.pop("protected_path_approved_at", None)
    ticket.pop("protected_path_approved_rationale", None)
    ticket.pop("protected_path_approved_actor", None)
    ticket.pop("human_review_approved_at", None)
    ticket.pop("human_review_rationale", None)
    ticket.pop("human_review_actor", None)
    ticket.pop("red_lane_approved_at_sha", None)
    # close_criteria_drift_approved_* is deliberately left untouched here: it is
    # scoped to the approved close_criteria text, not to this diff, so an
    # unrelated needs_review bounce must not invalidate it.
