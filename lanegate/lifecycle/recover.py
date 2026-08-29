"""Lifecycle recovery commands and review-pending transitions."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import (
    _append_ticket_section,
    _commit_generated_ticket_write,
    _control_repo_root,
    _remove_executor_markers,
    _stamp_status_changed,
    canonical_id,
    classify_needs_review_cause,
    load_all_tickets,
    resolve_human_escalation,
    write_ticket,
)
from lanegate import APP_NAME
from lanegate.ticket import append_lifecycle_event
from .state import _mark_needs_review

def mark_review_pending(ticket: dict, cfg: dict, repo_root: Path, *, reason: str) -> None:
    """Hibernate completed code whose automated review never ran.

    This is intentionally distinct from ``needs_review`` and from a review
    rejection: there is no verdict, no finding, and no auto-fix work to do.
    The preserved worktree is resumed by orchestrate directly at review.
    """
    tid = ticket["id"]
    current = ticket.get("status")
    if current not in {"code_complete", "in_progress", "needs_review"}:
        raise ValueError(f"{tid} cannot become review-pending from {current!r}")
    ticket.pop("review_verdict", None)
    ticket.pop("review_summary", None)
    ticket.pop("review_findings", None)
    ticket["review_pending"] = True
    ticket["review_pending_reason"] = reason
    ticket["status"] = "hibernated"
    _stamp_status_changed(ticket)
    _append_ticket_section(ticket, "## Review Pending", reason)
    append_lifecycle_event(
        ticket,
        event="review_pending",
        from_status=current,
        to_status="hibernated",
        summary=reason,
    )
    write_ticket(ticket)
    _remove_executor_markers(repo_root, tid)
    _commit_generated_ticket_write(repo_root, ticket["_path"], tid, "review-pending", cfg)
    print(f"{tid}: review pending — status: hibernated (review was not performed)", file=sys.stderr)


def resume_review_pending(ticket: dict, cfg: dict, repo_root: Path) -> None:
    """Restore a review-pending resume to code_complete before review runs.

    cmd_start reattaches a hibernated/review_pending ticket by setting its
    status to in_progress, same as any other resumed ticket -- there is no
    separate "reattach for review" status. mark_review_pending's own
    docstring is explicit that the code is already complete when a ticket
    becomes review-pending; only the review never ran. Without this call,
    the orchestrate resume path invoked the reviewer directly against an
    in_progress ticket, and cmd_review's code_complete guard rejected the
    verdict write every time -- reviewer output landed in the audit bundle
    but never reached the ticket, and the failure was invisible because
    _invoke_cmd_review's SystemExit handling (written for the normal
    changes_requested exit) silently absorbed the guard's exit too.
    """
    tid = ticket["id"]
    current = ticket.get("status")
    if current == "code_complete":
        return
    if current != "in_progress":
        raise ValueError(f"{tid} cannot resume review-pending from {current!r}")
    ticket["status"] = "code_complete"
    _stamp_status_changed(ticket)
    append_lifecycle_event(
        ticket,
        event="status_changed",
        from_status=current,
        to_status="code_complete",
        summary="resumed review-pending ticket to code_complete ahead of review",
    )
    write_ticket(ticket)
    _commit_generated_ticket_write(repo_root, ticket["_path"], tid, "resume-review-pending", cfg)


def cmd_recover_rate_limited_reviews(
    ticket_id: str | None, cfg: dict, repo_root: Path
) -> int:
    """Recover only misclassified review harness failures with durable proof.

    This deliberately reads LaneGate's own immutable-ish audit bundle, not a
    ticket body or an executor-owned worktree.  A real finding, auto-fix, or a
    non-rate-limit harness failure is never reopened by this command.
    """
    from lanegate.orchestrate.loop import _is_rate_limit  # deferred: avoids orchestrate<->lifecycle import cycle

    repo_root = _control_repo_root(repo_root)
    tickets_dir = repo_root / cfg["tickets_dir"]
    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    selected = [t for t in tickets if ticket_id is None or t["id"] == canonical_id(ticket_id)]
    recovered = 0
    for ticket in selected:
        if ticket.get("status") != "needs_review" or ticket.get("auto_fix_attempts"):
            continue
        # A stale hibernation marker can coexist with a later escalation.
        # An audit bundle is the only evidence for older tickets that have no
        # ticket-side marker (and therefore classify as unknown), but no
        # explicit non-rate-limit cause may enter unattended recovery.
        if classify_needs_review_cause(ticket) not in {"rate_limit", "unknown"}:
            continue
        bundles = repo_root / f".{APP_NAME}" / "executor-runs" / ticket["id"]
        proof = False
        safe = bundles.is_dir()
        if safe:
            for bundle in bundles.iterdir():
                if not bundle.is_dir():
                    continue
                try:
                    status = json.loads((bundle / "status.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if status.get("step") != "review":
                    continue
                try:
                    verdict = json.loads((bundle / "verdict.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    safe = False
                    break
                findings = str(verdict.get("findings") or "").strip()
                if findings or verdict.get("verdict") not in {"error", "review_pending"}:
                    safe = False
                    break
                try:
                    output = (bundle / "captured-output.txt").read_text(encoding="utf-8", errors="replace")
                except OSError:
                    output = ""
                if _is_rate_limit(1, captured_stdout=output):
                    proof = True
                else:
                    safe = False
                    break
        if not (safe and proof):
            continue
        mark_review_pending(
            ticket, cfg, repo_root,
            reason="Recovered from needs_review: audit bundle proves a rate-limited review with no findings.",
        )
        recovered += 1
    print(f"Recovered {recovered} rate-limited review ticket(s).")
    return recovered



def rejected_auto_fix_recovery_reason(ticket: dict, cfg: dict) -> str | None:
    """Return the audited recovery reason for an exhausted rejected ticket.

    A fresh ``changes_requested`` verdict remains eligible for its one normal
    auto-fix/re-review cycle. Only a failed drift check or an exhausted bounded
    auto-fix budget may be recovered out of ``code_complete`` by an operator.
    """
    if (
        ticket.get("status") != "code_complete"
        or ticket.get("review_verdict") != "changes_requested"
    ):
        return None
    drift = ticket.get("drift_check_result")
    if isinstance(drift, dict) and drift.get("ok") is False:
        detail = str(drift.get("reason") or "no diagnostic recorded")
        return f"drift check failed after bounded auto-fix: {detail}"
    try:
        attempts = int(ticket.get("auto_fix_attempts") or 0)
        max_attempts = min(
            int(cfg.get("max_auto_fix_attempts", 1)),
            int(resolve_human_escalation(cfg)["retry_limit"]),
        )
    except (TypeError, ValueError, KeyError):
        return None
    if attempts >= max_attempts:
        return f"bounded auto-fix/re-review exhausted ({attempts}/{max_attempts})"
    return None


def cmd_recover_rejected(
    ticket_id: str | None,
    cfg: dict,
    repo_root: Path,
    *,
    all_tickets: bool = False,
) -> int:
    """Release only verified exhausted rejected tickets to ``needs_review``.

    This is an agent-free migration for tickets stranded by versions that left
    a failed bounded auto-fix cycle at ``code_complete``. Every transition is
    recorded through the normal lifecycle writer, preserving the worktree and
    review findings while releasing the touch lock.
    """
    if bool(ticket_id) == all_tickets:
        raise ValueError("provide exactly one ticket ID or --all")
    repo_root = _control_repo_root(repo_root)
    tickets_dir = repo_root / cfg["tickets_dir"]
    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    target_id = canonical_id(ticket_id) if ticket_id else None
    selected = [t for t in tickets if target_id is None or t["id"] == target_id]
    recovered = 0
    for ticket in selected:
        reason = rejected_auto_fix_recovery_reason(ticket, cfg)
        if reason is None:
            continue
        _mark_needs_review(ticket, cfg, repo_root, reason=reason)
        recovered += 1
    if target_id and recovered == 0:
        print(
            f"ERROR: {target_id} is not an exhausted rejected ticket; refusing recovery",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"Recovered {recovered} exhausted rejected ticket(s) to needs_review.")
    return recovered

