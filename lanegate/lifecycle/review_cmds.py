"""Review, needs-review, and human-approval lifecycle commands."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import (
    _append_ticket_section,
    _commit_generated_ticket_write,
    _control_repo_root,
    _enforce_verification_gate,
    _parse_findings,
    _push_branch_and_open_pr,
    _stamp_status_changed,
    _track_direct_action,
    _worktree_has_commits,
    append_lifecycle_event,
    append_status_history,
    canonical_id,
    latest_review_findings,
    load_all_tickets,
    next_review_findings_header,
    resolve_trunk_branch,
    _upsert_body_section,
    write_ticket,
)

from .hibernate import _remove_executor_markers
from .state import _clear_human_review_approval, _mark_needs_review

def _append_review_findings(ticket: dict, findings: str) -> None:
    """Record one review's findings under its own ``(attempt N)`` header.

    A changes_requested verdict is followed by a fix pass and a re-review, so
    overwriting the section — which is what this used to do — destroyed the
    findings that motivated the fix precisely when they were most worth
    keeping.  Same reasoning, and same mechanism, as the per-attempt
    ``## Auto-Fix Attempt N`` sections below.
    """
    header = next_review_findings_header(ticket.get("_body", ""))
    _append_ticket_section(ticket, header, findings)


def record_auto_fix_attempt(
    ticket_id: str,
    cfg: dict,
    repo_root: Path,
    *,
    attempt: int,
    max_attempts: int,
    note: str,
    escalate: bool = False,
    drift_ok: bool | None = None,
    drift_reason: str | None = None,
) -> None:
    """Record one auto-fix cycle attempt for audit history.

    Appends a uniquely-headed ``## Auto-Fix Attempt N`` body section per
    attempt rather than one shared header, since ``_append_ticket_section``
    replaces a named section's content on repeat calls and would clobber
    earlier attempts otherwise. Status and review_verdict are left untouched
    on escalation — they stay code_complete/changes_requested so cmd_blocked
    and cmd_merge's guard (which key off exactly that pair) keep working.

    ``drift_ok``/``drift_reason``, when given, are persisted as a
    structured ``drift_check_result`` frontmatter field so the drift verdict
    is machine-readable on the ticket, not just prose in the attempt note.
    """
    repo_root = _control_repo_root(repo_root)
    tid = canonical_id(ticket_id)
    tickets_dir = repo_root / cfg["tickets_dir"]
    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    ticket = next((t for t in tickets if t["id"] == tid), None)
    if not ticket:
        print(f"ERROR: {tid} not found", file=sys.stderr)
        sys.exit(1)

    _append_ticket_section(ticket, f"## Auto-Fix Attempt {attempt}", note)
    if escalate:
        ticket["review_summary"] = note
    if drift_ok is not None or drift_reason is not None:
        ticket["drift_check_result"] = {"ok": drift_ok, "reason": drift_reason}
    ticket["auto_fix_attempts"] = attempt
    write_ticket(ticket)
    _commit_generated_ticket_write(
        repo_root, ticket["_path"], tid, f"auto-fix-attempt-{attempt}", cfg
    )




    # close_criteria_drift_approved_* is deliberately left untouched here: it is
    # scoped to the approved close_criteria text (see _close_criteria_drift_finding),
    # not to this diff, so an unrelated needs_review bounce must not invalidate it.


def cmd_needs_review(
    ticket_id: str,
    cfg: dict,
    repo_root: Path,
    *,
    reason: str = "",
) -> None:
    """Escalate active or rejected completed work to human review.

    ``code_complete`` is accepted only after a reviewer has explicitly
    requested changes. This is the audited manual counterpart to the
    orchestrator's bounded auto-fix escalation: it preserves the worktree and
    findings while releasing the code-complete touch lock, without dispatching
    another executor.
    """
    repo_root = _control_repo_root(repo_root)
    tid = canonical_id(ticket_id)
    tickets_dir = repo_root / cfg["tickets_dir"]
    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    ticket = next((t for t in tickets if t["id"] == tid), None)
    if not ticket:
        print(f"ERROR: {tid} not found", file=sys.stderr)
        sys.exit(1)

    current = ticket.get("status")
    eligible_completed_rejection = (
        current == "code_complete" and ticket.get("review_verdict") == "changes_requested"
    )
    if current != "in_progress" and not eligible_completed_rejection:
        print(
            f"ERROR: {tid} is '{current}', expected in_progress or "
            "code_complete with review_verdict=changes_requested",
            file=sys.stderr,
        )
        sys.exit(1)

    _mark_needs_review(ticket, cfg, repo_root, reason=reason)


def _flatten_close_criteria(val: object) -> str:
    """Mirror analyze.py's local ``_flatten`` so approval snapshots compare identically."""
    return "\n".join(str(x) for x in val).strip() if isinstance(val, list) else str(val or "").strip()


def cmd_human_review_approve(
    ticket_id: str,
    cfg: dict,
    repo_root: Path,
    *,
    rationale: str,
    actor: str = "human",
) -> None:
    """Record an audited human approval for a needs_review or code_complete ticket.

    Unlike ``lanegate reopen``, which restores a needs_review ticket for
    another automatic pass, this is the dedicated path for causes that
    ``cmd_reopen`` refuses to auto-resume (hard-blocked/protected paths): it
    requires a human-authored rationale, preserves the worktree and its
    commits exactly as they are, records an approved verdict in ticket
    history, and advances to ``code_complete`` without dispatching an agent
    or touching review/merge, which stay separate decisions.

    Also supports dismissing false-positive ``changes_requested`` review
    findings on a ``code_complete`` ticket, archiving stale findings to ticket
    body history and clearing frontmatter review fields.
    """
    rationale = (rationale or "").strip()
    if not rationale:
        print("ERROR: --rationale is required for `lanegate human-review`", file=sys.stderr)
        sys.exit(1)

    repo_root = _control_repo_root(repo_root)
    tid = canonical_id(ticket_id)
    tickets_dir = repo_root / cfg["tickets_dir"]
    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    ticket = next((t for t in tickets if t["id"] == tid), None)
    if not ticket:
        print(f"ERROR: {tid} not found", file=sys.stderr)
        sys.exit(1)

    current = ticket.get("status")
    if current not in ("needs_review", "code_complete"):
        print(f"ERROR: {tid} is '{current}', expected needs_review or code_complete", file=sys.stderr)
        sys.exit(1)

    if current == "code_complete" and ticket.get("review_verdict") != "changes_requested":
        print(
            f"ERROR: {tid} is 'code_complete' but review_verdict is not 'changes_requested'",
            file=sys.stderr,
        )
        sys.exit(1)

    if not _worktree_has_commits(ticket, cfg, repo_root):
        print(
            f"ERROR: {tid} has no real commits ahead of main — use `lanegate reopen {tid}` instead",
            file=sys.stderr,
        )
        sys.exit(1)

    wt_path = Path(ticket["worktree"]) if ticket.get("worktree") else None
    if wt_path is None or not wt_path.exists():
        print(
            f"ERROR: {tid}: worktree is missing ({wt_path}) — refusing to approve for review.",
            file=sys.stderr,
        )
        sys.exit(1)

    now_utc = datetime.now(UTC)
    date_str = now_utc.strftime("%Y-%m-%d")
    now_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    if current == "code_complete":
        ticket["review_findings_dismissed_at"] = now_str
        ticket["review_findings_dismissal_rationale"] = rationale
        ticket["review_findings_dismissal_actor"] = actor
    else:
        ticket["protected_path_approved_at"] = now_str
        ticket["protected_path_approved_rationale"] = rationale
        ticket["protected_path_approved_actor"] = actor

    # Recorded unconditionally: a close-criteria-drift finding can co-occur with
    # either branch above, and human-review-approve is the only documented way
    # to clear it (see _close_criteria_drift_finding in analyze.py).
    ticket["close_criteria_drift_approved_at"] = now_str
    ticket["close_criteria_drift_approved_rationale"] = rationale
    ticket["close_criteria_drift_approved_actor"] = actor
    ticket["close_criteria_drift_approved_snapshot"] = _flatten_close_criteria(ticket.get("close_criteria"))

    # Record the exact commit this approval covers so loop.py's red-lane
    # diff re-scans can recognize an unchanged diff and skip re-escalating.
    head_result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=wt_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if head_result.returncode == 0:
        ticket["red_lane_approved_at_sha"] = head_result.stdout.strip()

    if current == "code_complete":
        summary = ticket.get("review_summary")
        raw_findings = ticket.get("review_findings")
        reviewed_at = ticket.get("reviewed_at")

        if raw_findings:
            if isinstance(raw_findings, list):
                findings_str = "\n".join(
                    f"- {item}" if not item.startswith("- ") else item for item in raw_findings
                )
            else:
                findings_str = str(raw_findings)
        else:
            findings_str = latest_review_findings(ticket)

        archive_lines = []
        if summary:
            archive_lines.append(f"**Summary**: {summary}")
        if reviewed_at:
            archive_lines.append(f"**Reviewed At**: {reviewed_at}")
        if rationale:
            archive_lines.append(f"**Dismissal Rationale**: {rationale}")
        if findings_str:
            archive_lines.append(f"\n### Findings\n{findings_str.strip()}")

        archived_text = "\n".join(archive_lines).strip()
        if archived_text:
            header = f"## Archived Review Findings ({date_str})"
            sec_text = f"{header}\n\n{archived_text}"
            ticket["_body"] = _upsert_body_section(ticket.get("_body", ""), header, sec_text)

    ticket.pop("review_verdict", None)
    ticket.pop("review_summary", None)
    ticket.pop("review_findings", None)
    ticket.pop("reviewed_at", None)
    ticket.pop("review_retry_attempt", None)
    ticket.pop("review_retry_after", None)
    # Clear the review-pending hibernation marker: leaving it set makes the
    # next `lanegate run` pass re-hibernate this ticket for the same
    # "orphaned prior session" reason it was just approved out of.
    ticket.pop("review_pending", None)
    ticket.pop("review_pending_reason", None)
    ticket["status"] = "code_complete"
    _stamp_status_changed(ticket)

    if actor == "human":
        history_note = f"human review approved: {rationale}"
        event_summary = rationale
    else:
        history_note = f"human-review approval recorded via agent tool call, rationale: {rationale}"
        event_summary = f"human-review approval recorded via agent tool call, rationale: {rationale}"

    append_status_history(ticket, current, "code_complete", history_note)
    append_lifecycle_event(
        ticket,
        event="human_review_approved",
        from_status=current,
        to_status="code_complete",
        summary=event_summary,
    )
    write_ticket(ticket)
    _commit_generated_ticket_write(repo_root, ticket["_path"], tid, "human-review-approved", cfg)
    if current == "needs_review":
        print(f"{tid}: needs_review → code_complete (human review approved, worktree preserved)")
    else:
        print(f"{tid}: code_complete (human review approved, changes_requested findings dismissed)")
    print(f"  Rationale: {rationale}")
    print(f"  Next: lanegate review {tid} --verdict approved (or lanegate run), then lanegate merge {tid}")



@_track_direct_action("review")
def cmd_review(
    ticket_id: str,
    cfg: dict,
    repo_root: Path,
    *,
    verdict: str | None = None,
    summary: str | None = None,
    findings: str | None = None,
    review_driver: str | None = None,
    review_model: str | None = None,
    review_independence: str | None = None,
) -> None:
    """Advance a ticket through the review gate.

    Without --verdict: backward-compat flip code_complete → in_review.
    verdict=approved: store fields + flip to in_review.
    verdict=changes_requested: store fields, print findings, exit non-zero,
        leave ticket at code_complete so the implementer can fix and re-run.
    """
    repo_root = _control_repo_root(repo_root)
    tid = canonical_id(ticket_id)
    tickets_dir = repo_root / cfg["tickets_dir"]
    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    ticket = next((t for t in tickets if t["id"] == tid), None)
    if not ticket:
        print(f"ERROR: {tid} not found", file=sys.stderr)
        sys.exit(1)

    wt_path = Path(ticket["worktree"]) if ticket.get("worktree") else None
    if wt_path and wt_path.exists():
        from lanegate.orchestrate.loop import is_mid_rebase

        if is_mid_rebase(wt_path):
            print(
                f"ERROR: {tid}: worktree is mid-rebase — complete or abort the rebase before running review",
                file=sys.stderr,
            )
            sys.exit(1)

    notes_root = repo_root / ".lanegate" / "notes"
    global_note = notes_root / "global.md"
    if notes_root.is_dir() and not global_note.is_file():
        per_file_notes = [
            p for p in notes_root.rglob("*.md")
            if p.name != "global.md" and p.is_file()
        ]
        if per_file_notes:
            print(
                "Consider consolidating project-wide facts into .lanegate/notes/global.md.",
                file=sys.stderr,
            )

    if verdict is not None:
        _enforce_verification_gate(ticket, cfg, repo_root, verdict=verdict, findings=findings)

    current = ticket.get("status")
    if verdict is None and current == "code_complete":
        from lanegate.orchestrate.pool import resolve_driver

        rotation_enabled = (
            bool(cfg.get("reviewer_rotation"))
            and not ((cfg.get("steps") or {}).get("review") or {}).get("driver")
            and not ticket.get("reviewer")
        )
        reviewer = "reviewer_rotation" if rotation_enabled else resolve_driver("review", ticket, cfg)
        if reviewer not in ("human", "none", "auto-none"):
            from lanegate.orchestrate.review import run_review_agent

            run_review_agent(ticket, repo_root, cfg=cfg)
            return
        print(
            f"{tid}: reviewer={reviewer!r} — no LLM reviewer configured; "
            f"flipping status only (no verdict recorded)",
            file=sys.stderr,
        )

    if current == "in_review" and verdict is not None:
        # A manual verdict may complete a review that was advanced without one.
        # A rejection must return to code_complete so its preserved worktree can
        # enter the normal fix/re-review path; leaving it in_review falsely
        # presents it as mergeable while also blocking fresh automated review.
        next_status = "code_complete" if verdict == "changes_requested" else "in_review"
        ticket["review_verdict"] = verdict
        if review_driver is not None:
            ticket["review_driver"] = review_driver
        if review_model is not None:
            ticket["review_model"] = review_model
        if review_independence is not None:
            ticket["review_independence"] = review_independence
        ticket["reviewed_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if summary is not None:
            ticket["review_summary"] = summary
        if findings:
            ticket["review_findings"] = _parse_findings(findings)
            _append_review_findings(ticket, findings)
        if next_status != current:
            ticket["status"] = next_status
            _stamp_status_changed(ticket)
        append_lifecycle_event(
            ticket,
            event="review_verdict",
            from_status="in_review",
            to_status=next_status,
            summary=f"review verdict: {verdict}" + (f" — {summary}" if summary else ""),
        )
        write_ticket(ticket)
        _commit_generated_ticket_write(
            repo_root, ticket["_path"], tid, f"review-verdict-{verdict}", cfg
        )
        print(f"{tid}: review_verdict set to {verdict} (status: {next_status})")
        if verdict == "changes_requested":
            sys.exit(1)
        return
    if current != "code_complete":
        if current == "needs_review":
            print(
                f"ERROR: {tid} is 'needs_review'. Use human-review to review this ticket:\n"
                f"  lanegate human-review {tid} --rationale \"...\"",
                file=sys.stderr,
            )
        else:
            print(
                f"ERROR: {tid} is '{current}', expected code_complete.\n"
                f"  If the ticket is already in_review and needs a verdict, pass --verdict: "
                f"lanegate review {tid} --verdict approved",
                file=sys.stderr,
            )
        sys.exit(1)

    if verdict is not None:
        # A real verdict resolves any earlier review-pending hibernation.
        # review_retry_attempt is a per-incident budget: clear it
        # here so a later, unrelated reviewer cooldown starts its own count
        # instead of inheriting this incident's exhausted attempts.
        ticket.pop("review_pending", None)
        ticket.pop("review_pending_reason", None)
        ticket.pop("review_retry_attempt", None)
        ticket.pop("review_retry_after", None)
        ticket["review_verdict"] = verdict
        ticket["reviewed_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if review_driver is not None:
        ticket["review_driver"] = review_driver
    if review_model is not None:
        ticket["review_model"] = review_model
    if review_independence is not None:
        ticket["review_independence"] = review_independence
    if summary is not None:
        ticket["review_summary"] = summary
    if findings:
        ticket["review_findings"] = _parse_findings(findings)

    if findings:
        _append_review_findings(ticket, findings)

    if verdict == "changes_requested":
        append_lifecycle_event(
            ticket,
            event="review_verdict",
            from_status="code_complete",
            to_status="code_complete",
            summary="review verdict: changes_requested" + (f" — {summary}" if summary else ""),
        )
        write_ticket(ticket)
        _commit_generated_ticket_write(
            repo_root, ticket["_path"], tid, "review-changes-requested", cfg
        )
        print(
            f"{tid}: review verdict = changes_requested (status stays code_complete)",
            file=sys.stderr,
        )
        if findings:
            print(f"\nFindings:\n{findings}", file=sys.stderr)
        sys.exit(1)

    # approved or no verdict: flip to in_review
    ticket["status"] = "in_review"
    _stamp_status_changed(ticket)
    append_lifecycle_event(
        ticket,
        event="review_completed" if verdict else "review_started",
        from_status="code_complete",
        to_status="in_review",
        summary=(f"review verdict: {verdict}" if verdict else "awaiting review verdict")
        + (f" — {summary}" if summary else ""),
    )
    write_ticket(ticket)
    _commit_generated_ticket_write(repo_root, ticket["_path"], tid, "in_review", cfg)
    if verdict == "approved":
        print(f"{tid}: review approved — status: in_review (ready to merge)")
    else:
        print(f"{tid}: code_complete → in_review")

    # Push branch and open GitHub PR when verdict is approved and github_pr is enabled.
    if verdict == "approved" and cfg.get("github_pr", False):
        branch = ticket.get("branch")
        if branch:
            result = _push_branch_and_open_pr(
                repo_root, branch, ticket, resolve_trunk_branch(cfg, repo_root)
            )
            if result is not None:
                pr_number, pr_url = result
                ticket["pr_number"] = pr_number
                ticket["pr_url"] = pr_url
                write_ticket(ticket)
                _commit_generated_ticket_write(repo_root, ticket["_path"], tid, "review-pr", cfg)
                print(f"  PR: {pr_url}")
