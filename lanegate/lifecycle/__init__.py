"""
lifecycle.py — ticket status machine: start, complete, review, merge, validate, done.

Design notes:
1. Merge worktree-leak guard: captures worktree path BEFORE nulling it.
2. touches lock held until merge (lock_statuses config), not just in_progress.
3. TOCTOU-safe start via inlined re-read inside claim_lock window before status write.
4. Protected-branch guard on worktree removal via worktree.remove_worktree.
5. Generated ticket metadata writes are committed immediately in Git checkouts.
"""

from __future__ import annotations

import json
import functools
import shutil
import subprocess
import sys
import threading
import time as time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lanegate import APP_NAME
from lanegate.companion import (
    CompanionMergeResult,
    companion_branch_create,
    companion_branch_merge,
    companion_worktree_cleanup,
)
from lanegate.config import resolve_human_escalation, resolve_trunk_branch
from lanegate.concurrency import (
    SafeguardLockHeld,
    check_local_not_behind_remote,
    claim_lock,
    locked_touches,
    metadata_commit_lock,
    safeguard_lock,
    touches_overlap,
)
from lanegate.git import git_text, has_tracking_remote
from lanegate.promote import _auto_promote_environments
from lanegate.safeguards import effective_safeguards, run_safeguards
from lanegate.ticket import (
    TERMINAL_STATUSES,
    _upsert_body_section,
    append_lifecycle_event,
    append_status_history,
    branch_name,
    canonical_id,
    classify_needs_review_cause,
    latest_review_findings,
    load_all_tickets,
    next_review_findings_header,
    parse_ticket,
    write_ticket,
)
from lanegate.worktree import create_worktree, remove_worktree, worktree_path

from .hibernate import (
    _append_ticket_section,
    _cleanup_ticket_notes,
    _control_repo_root,
    _push_branch_and_open_pr,
    _remove_executor_markers,
    _remove_recovery_file,
    _stamp_status_changed,
    _write_executor_marker,
)
from .hibernate import (
    _hibernation_note as _hibernation_note,
)
from .hibernate import (
    _marker_base as _marker_base,
)
from .hibernate import (
    _recovery_path as _recovery_path,
)
from .hibernate import (
    _write_hibernation_notes as _write_hibernation_notes,
)
from .hibernate import (
    cmd_hibernate as cmd_hibernate,
)
from .hibernate import (
    cmd_stop as cmd_stop,
)
from .touches import (
    _check_touches_drift as _check_touches_drift,
)
from .touches import (
    _get_branch_wall_time_ms,
    _get_changed_files,
    _get_touched_files,
    _has_committed_changes,
    check_touches_compliance,
)

_git_text = git_text


class MergeFailedError(Exception):
    """Raised by cmd_merge when the git merge operation fails.

    Allows in-process callers (e.g. orchestrate's auto_merge loop) to catch
    the failure, downgrade the ticket, and keep processing independent tickets
    instead of crashing via sys.exit(1).
    """


# Module-level lock for serializing git operations on repo_root when running
# with max_parallel > 1 in orchestrate mode (F3 fix). Set by orchestrate._drain_loop,
# None otherwise. Protects against concurrent git operations (merge, commit, etc.)
# on the shared primary checkout.
_GIT_OPS_LOCK: threading.Lock | None = None


def _parse_findings(findings_str: str) -> list[str]:
    """Parse a findings string into a list of individual findings.

    Splits by newlines, strips whitespace, and filters out empty items.
    """
    if not findings_str:
        return []
    items = [line.strip() for line in findings_str.split("\n")]
    return [item for item in items if item]


def resolve_reviewer(ticket: dict, cfg: dict) -> str:
    """Resolve the reviewer executor for a ticket.

    Resolution order (first non-empty value wins):
      1. ticket-level ``reviewer`` field
      2. ``steps.review.driver`` in .lanegate.yml
      3. project-level ``reviewer`` in .lanegate.yml
      4. project-level ``executor`` in .lanegate.yml
      5. built-in default: ``"claude"``
    """
    return (
        ticket.get("reviewer")
        or ((cfg.get("steps") or {}).get("review") or {}).get("driver")
        or cfg.get("reviewer")
        or cfg.get("executor", "claude")
    )


def spawn_detached(args: list[str], log_path: Path) -> int:
    """Spawn a detached background process, platform-agnostic.

    On Unix: uses start_new_session=True so the child is in a new session and
    is not killed when the parent process group exits.
    On Windows: uses DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP via creationflags.

    stdout and stderr are both redirected to log_path (opened in append mode).
    log_path.parent is created if it does not exist.

    Returns the PID of the spawned process.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as log_fh:
        if sys.platform == "win32":
            import subprocess as _sp

            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            p = _sp.Popen(
                args,
                stdout=log_fh,
                stderr=log_fh,
                close_fds=True,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            )
        else:
            p = subprocess.Popen(
                args,
                stdout=log_fh,
                stderr=log_fh,
                close_fds=True,
                start_new_session=True,
            )
    return p.pid


def _has_uncommitted_diff(repo_root: Path, path: Path) -> bool:
    r = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", str(path)],
        cwd=repo_root,
        capture_output=True,
    )
    return r.returncode != 0


def _is_git_worktree(repo_root: Path) -> bool:
    if not (Path(repo_root) / ".git").exists():
        return False
    r = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repo_root,
        capture_output=True,
        text=True, encoding="utf-8",
    )
    return r.returncode == 0 and r.stdout.strip() == "true"


def _commit_status(repo_root: Path, ticket_path: Path, ticket_id: str, to_status: str) -> bool:
    def _run_commit():
        r = subprocess.run(
            [
                "git",
                "commit",
                "-s",
                "--only",
                str(ticket_path),
                "-m",
                f"chore: {ticket_id} status → {to_status}",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True, encoding="utf-8",
        )
        return r.returncode == 0

    with metadata_commit_lock(repo_root):
        if _GIT_OPS_LOCK is not None:
            with _GIT_OPS_LOCK:
                return _run_commit()
        return _run_commit()


def _push_status_commit(repo_root: Path) -> bool:
    """Push HEAD (the just-made status commit) to its tracking remote.

    A rejection means another clone pushed a conflicting commit first —
    the caller is responsible for rolling back the local commit.
    """
    r = subprocess.run(
        ["git", "push"],
        cwd=repo_root,
        capture_output=True,
        text=True, encoding="utf-8",
    )
    return r.returncode == 0


def _commit_generated_ticket_write(
    repo_root: Path,
    ticket_path: Path,
    ticket_id: str,
    to_status: str,
    cfg: dict,
    *,
    required: bool = True,
) -> bool:
    """Commit LaneGate-generated ticket metadata writes when inside Git.

    Respects the commit_status_changes config flag (default true).
    """
    if not cfg.get("commit_status_changes", True):
        return True
    if not _is_git_worktree(repo_root):
        return True
    if not _has_uncommitted_diff(repo_root, ticket_path):
        return True
    ok = _commit_status(repo_root, ticket_path, ticket_id, to_status)
    if not ok and required:
        print(
            f"ERROR: failed to commit generated ticket write for {ticket_id} ({to_status})",
            file=sys.stderr,
        )
        sys.exit(1)
    return ok


def _track_direct_action(action_type: str):
    """Give manual lifecycle invocations a durable, operator-visible run id."""
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(ticket_id: str, cfg: dict, repo_root: Path, *args, **kwargs):
            # Import lazily: run_report depends on board, which imports this
            # module for review routing during package initialization.
            from lanegate.orchestrate.run_report import (
                begin_direct_action,
                direct_action_tracking_suppressed,
                record_direct_action_event,
            )

            if direct_action_tracking_suppressed():
                return fn(ticket_id, cfg, repo_root, *args, **kwargs)
            tracking_root = _control_repo_root(repo_root)
            baseline_reviewed_at = _current_reviewed_at(action_type, ticket_id, cfg, tracking_root)
            tracking = begin_direct_action(
                tracking_root, action_type, ticket_id=canonical_id(ticket_id), executor="cli"
            )
            print(
                f"Action {tracking['action_id']}: {action_type} running "
                f"(log: {tracking['log_path']})"
            )
            try:
                result = fn(ticket_id, cfg, repo_root, *args, **kwargs)
            except BaseException:
                record_direct_action_event(
                    tracking_root, tracking["action_id"], "action_end", action_type=action_type,
                    ticket_id=canonical_id(ticket_id), status="failed",
                    **_review_verdict_fields(action_type, ticket_id, cfg, tracking_root, baseline_reviewed_at),
                )
                raise
            record_direct_action_event(
                tracking_root, tracking["action_id"], "action_end", action_type=action_type,
                ticket_id=canonical_id(ticket_id), status="success",
                **_review_verdict_fields(action_type, ticket_id, cfg, tracking_root, baseline_reviewed_at),
            )
            print(f"Action {tracking['action_id']}: {action_type} success")
            return result
        return wrapped
    return decorate


def _current_reviewed_at(action_type: str, ticket_id: str, cfg: dict, tracking_root: Path) -> str | None:
    """Snapshot ``reviewed_at`` before a review action dispatches."""
    if action_type != "review":
        return None
    try:
        tickets_dir = Path(cfg.get("tickets_dir", ".lanegate/tickets"))
        if not tickets_dir.is_absolute():
            tickets_dir = tracking_root / tickets_dir
        prefix = cfg.get("ticket_prefix", "TICK-")
        tickets, _ = load_all_tickets(tickets_dir, prefix, cfg)
    except Exception:
        # Action tracking is diagnostic only.  A malformed or unreadable ticket
        # must not prevent the review command from recording its own failure.
        return None
    ticket = next((t for t in tickets if t.get("id") == canonical_id(ticket_id)), None)
    return ticket.get("reviewed_at") if ticket else None


def _review_verdict_fields(
    action_type: str, ticket_id: str, cfg: dict, tracking_root: Path, baseline_reviewed_at: str | None
) -> dict:
    """Reload the ticket for review actions so action_end can carry the
    actual verdict, not just process exit status.

    ``review_verdict``/``review_summary`` persist in frontmatter across a
    ``cmd_review`` call that exits before writing a new verdict (e.g. the
    verification gate or a stale-status guard) -- only trust them here when
    ``reviewed_at`` actually changed during this call, otherwise a prior
    invocation's verdict would be misattributed to this one. Comparing
    against a before-call snapshot (rather than a timestamp threshold)
    avoids false negatives when two review actions land within the same
    whole-second timestamp.
    """
    if action_type != "review":
        return {}
    try:
        tickets_dir = Path(cfg.get("tickets_dir", ".lanegate/tickets"))
        if not tickets_dir.is_absolute():
            tickets_dir = tracking_root / tickets_dir
        prefix = cfg.get("ticket_prefix", "TICK-")
        tickets, _ = load_all_tickets(tickets_dir, prefix, cfg)
    except Exception:
        # Never replace the underlying review error or suppress action_end when
        # enrichment cannot read the ticket directory.
        return {}
    ticket = next((t for t in tickets if t.get("id") == canonical_id(ticket_id)), None)
    if ticket is None:
        return {}
    reviewed_at = ticket.get("reviewed_at")
    if not reviewed_at or reviewed_at == baseline_reviewed_at:
        return {}
    extra = {
        "verdict": ticket.get("review_verdict"),
        "review_summary": ticket.get("review_summary"),
    }
    return {k: v for k, v in extra.items() if v is not None}


@_track_direct_action("start")
def cmd_start(
    ticket_id: str,
    cfg: dict,
    repo_root: Path,
    *,
    interactive: bool = True,
    executor: str | None = None,
) -> None:
    repo_root = _control_repo_root(repo_root)
    tid = canonical_id(ticket_id)
    tickets_dir = repo_root / cfg["tickets_dir"]
    worktrees_dir = repo_root / cfg["worktrees_dir"]
    lock_statuses = cfg["lock_statuses"]
    trunk_branch = resolve_trunk_branch(cfg, repo_root)

    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    ticket = next((t for t in tickets if t["id"] == tid), None)
    if not ticket:
        print(f"ERROR: {tid} not found", file=sys.stderr)
        sys.exit(1)
    previous_status = ticket.get("status")
    if previous_status not in ("open", "hibernated", "needs_review"):
        print(
            f"ERROR: {tid} is '{previous_status}', expected open, hibernated, or needs_review",
            file=sys.stderr,
        )
        sys.exit(1)

    if previous_status == "needs_review":
        # cmd_start reattaches a needs_review ticket directly (manual `lanegate
        # start` and the orchestrate dispatch loop both go through this path,
        # bypassing cmd_reopen entirely) -- a hard-blocked path must not be
        # silently resumed for another automatic implementation pass.
        if (
            classify_needs_review_cause(ticket) == "protected_path"
            and not (ticket.get("protected_path_approved_at") or ticket.get("human_review_approved_at"))
        ):
            print(
                f"ERROR: {tid} is needs_review for a hard-blocked path — this requires an "
                f"explicit human decision before it can be resumed. Inspect the diff, then run: "
                f"lanegate human-review {tid} --rationale \"...\"",
                file=sys.stderr,
            )
            sys.exit(1)

    if not ticket.get("touches"):
        print(f"ERROR: {tid} has no touches — analysis not done yet.", file=sys.stderr)
        print(
            f"  Add touches to tickets/{tid}.md or run `lanegate analyze {tid}` (coming soon).",
            file=sys.stderr,
        )
        sys.exit(1)

    branch = branch_name(tid)
    wt_path = worktree_path(worktrees_dir, tid)

    # Fetch/divergence check (cross-clone protection)
    try:
        check_local_not_behind_remote(repo_root, branch)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    # Serialize the read→write→commit claim window: under this flock the TOCTOU re-read,
    # the worktree creation, the status write, and the commit are atomic against any other
    # `lanegate start`.  The commit is deferred until AFTER the worktree succeeds so that a
    # worktree creation failure never leaves a committed in_progress status with no worktree.
    with claim_lock(repo_root):
        # TOCTOU re-read: another session may have claimed the same ticket since our first read.
        fresh = parse_ticket(ticket["_path"])
        if fresh is None:
            print(f"ERROR: ticket file disappeared: {ticket['_path']}", file=sys.stderr)
            sys.exit(1)
        if fresh.get("status") != previous_status:
            print(
                f"ERROR: ticket was grabbed by another session (now '{fresh.get('status')}')",
                file=sys.stderr,
            )
            sys.exit(1)
        ticket = fresh

        # TOCTOU conflict check: re-read all tickets and verify no conflicts while holding
        # the lock. This is atomic with the claim that follows, preventing two concurrent
        # `lanegate start` calls from both reaching in_progress with overlapping touches.
        fresh_tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
        locked = locked_touches(fresh_tickets, lock_statuses)
        ticket_touches = ticket.get("touches") or []
        if touches_overlap(ticket_touches, locked):
            conflicts = sorted(set(ticket_touches) & set(locked)) or ["*"]
            blockers = [
                t["id"]
                for t in fresh_tickets
                if t.get("status") in lock_statuses
                and touches_overlap(ticket_touches, t.get("touches") or [])
            ]
            print(f"ERROR: {tid} conflicts with {blockers} on: {conflicts}", file=sys.stderr)
            sys.exit(1)

        # Ticket metadata is executor-writable.  Always run the canonical
        # worktree through create_worktree's branch and ancestry validation;
        # merely matching a metadata path must never bypass those checks.
        # Recovery statuses may explicitly reattach their canonical branch,
        # but only after that validation removes any stale checkout.
        from lanegate.config import protected_branches

        reuse_existing_branch = (
            previous_status in ("hibernated", "needs_review") and ticket.get("branch") == branch
        )
        protected = protected_branches(cfg) or None

        # Create the worktree BEFORE writing or committing the status change.
        # If this raises RuntimeError the ticket file is untouched and still open.
        create_kwargs: dict[str, Any] = {"reuse_existing_branch": reuse_existing_branch}
        if protected:
            create_kwargs["protected"] = protected
        try:
            wt_path = create_worktree(
                repo_root,
                worktrees_dir,
                tid,
                branch,
                trunk_branch,
                **create_kwargs,
            )
        except (RuntimeError, PermissionError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(str(e))

        # Worktree is up — now claim the ticket.
        previous_status_changed_at = ticket.get("status_changed_at")
        ticket["status"] = "in_progress"
        ticket["worktree"] = str(wt_path)
        ticket["branch"] = branch
        _stamp_status_changed(ticket)
        append_lifecycle_event(
            ticket,
            event="implementation_started",
            from_status=previous_status,
            to_status="in_progress",
            summary="worktree claimed for implementation",
        )
        write_ticket(ticket)
        marker_executor = executor or ticket.get("executor") or cfg.get("executor", "claude")
        _write_executor_marker(repo_root, tid, marker_executor)

        def _revert_claim():
            # Roll back in-memory and on-disk; worktree is already created but
            # the status commit/push failed — leave the ticket open so a retry can
            # reclaim it. Preserve the original status_changed_at when possible
            # so the rollback itself does not leave the ticket dirty.
            ticket["status"] = previous_status
            if previous_status == "open":
                ticket["worktree"] = None
                ticket["branch"] = None
            if previous_status_changed_at is None:
                ticket.pop("status_changed_at", None)
            else:
                ticket["status_changed_at"] = previous_status_changed_at
            write_ticket(ticket)
            _remove_executor_markers(repo_root, tid)

        commit_ok = _commit_generated_ticket_write(
            repo_root, ticket["_path"], tid, "in_progress", cfg, required=False
        )
        if not commit_ok:
            _revert_claim()
            print(f"ERROR: failed to commit status lock for {tid}", file=sys.stderr)
            sys.exit(1)

        # Push the claim commit immediately so a racing clone's own claim on this
        # ticket is visible to us (and ours to it) before either starts work — the
        # in_progress status only lives on the shared branch once it's pushed, so
        # without this a second clone can never see that the ticket was taken.
        if (
            cfg.get("commit_status_changes", True)
            and _is_git_worktree(repo_root)
            and has_tracking_remote(repo_root)
            and not _push_status_commit(repo_root)
        ):
            # Another clone's commit reached the remote first — undo ours so
            # the retry starts from a clean, up-to-date state.
            subprocess.run(
                ["git", "reset", "--mixed", "HEAD~1"],
                cwd=repo_root,
                capture_output=True,
            )
            _revert_claim()
            print(
                f"ERROR: {tid} claim commit was pushed and rejected by origin — "
                "another clone may have claimed this ticket. Fetch and retry.",
                file=sys.stderr,
            )
            sys.exit(1)

        # Commit succeeded — now safe to delete recovery notes.
        if previous_status in ("hibernated", "needs_review"):
            _remove_recovery_file(repo_root, tid)

    for companion in ticket.get("companion_repos") or []:
        companion_branch_create(repo_root, companion, branch, tid, trunk_branch)

    print(f"Started {tid}")
    print(f"  worktree: {wt_path}")
    print(f"  branch:   {branch}")
    print()
    print("=== Context Prompt ===")
    print(f"Ticket : {ticket['id']} — {ticket['title']}")
    print(f"Model: {ticket.get('model') or 'unspecified'}")
    _touches = ticket.get("touches") or []
    print(f"Touches: {', '.join(_touches) or 'none'}")
    _acceptance_matrix = ticket.get("acceptance_matrix")
    _invariants = (
        _acceptance_matrix.get("invariants") if isinstance(_acceptance_matrix, dict) else None
    ) or []
    print(f"Invariants: {', '.join(str(i) for i in _invariants) or 'none'}")
    print(f"Close criteria: {ticket.get('close_criteria', '')}")
    if interactive:
        # Only meaningful when a human or an interactive coding session is reading
        # stdout to decide whether to self-implement or delegate — cmd_orchestrate's
        # automated pipeline (any executor) calls cmd_start too, but nothing there
        # parses or acts on this advisory text, so it passes interactive=False.
        # TODO: this on/off split is a stopgap (suppress vs. show the same fixed
        # text). A more effective version for non-interactive/automated executors
        # would say something an automated pipeline could actually act on (or
        # route it to a structured field instead of stdout prose) rather than
        # just being silenced. Revisit once there's a concrete automated-executor
        # use case to design against.
        _touches_label = (
            "unpredictable scope" if _touches == ["*"] else f"{len(_touches)} touched file(s)"
        )
        if _touches == ["*"] or len(_touches) >= 3:
            print(
                f"Dispatch tip: {_touches_label} — a signal to double-check, not a verdict. "
                "Delegating to a separate agent buys isolation and an independent review pass, "
                "not token savings (a fresh agent re-pays cold-start context cost and shares no "
                "cache with this one). Inline is still fine if the change is small/mechanical "
                "per file even across many files (e.g. the same one-line fix repeated); delegate "
                "when the scope needs real per-file judgment, touches unfamiliar code, or the "
                "change is consequential enough to want a second opinion before merge."
            )
        else:
            print(
                f"Dispatch tip: {_touches_label} — likely small enough to implement inline in "
                "this session if it already has the needed context loaded. Delegate anyway if "
                "the change needs real design judgment or an independent review pass before merge."
            )
    print()
    print(ticket.get("_body", ""))


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
    # scoped to the approved close_criteria text (see _close_criteria_drift_finding),
    # not to this diff, so an unrelated needs_review bounce must not invalidate it.


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


def _advance(
    ticket_id: str,
    to_status: str,
    allow_from: list[str],
    cfg: dict,
    repo_root: Path,
    unlock: bool = False,
) -> None:
    repo_root = _control_repo_root(repo_root)
    tid = canonical_id(ticket_id)
    tickets_dir = repo_root / cfg["tickets_dir"]

    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    ticket = next((t for t in tickets if t["id"] == tid), None)
    if not ticket:
        print(f"ERROR: {tid} not found", file=sys.stderr)
        sys.exit(1)
    current = ticket.get("status")
    if current not in allow_from:
        print(f"ERROR: {tid} is '{current}', expected one of {allow_from}", file=sys.stderr)
        sys.exit(1)

    if unlock:
        # Capture worktree path BEFORE nulling (fixes the merge worktree-leak bug)
        wt = ticket.get("worktree")
        ticket["worktree"] = None
        ticket["status"] = to_status
        _stamp_status_changed(ticket)
        append_lifecycle_event(
            ticket,
            event="status_changed",
            from_status=current,
            to_status=to_status,
            summary="lifecycle transition",
        )
        write_ticket(ticket)
        if to_status != "hibernated":
            _remove_recovery_file(repo_root, tid)
        _remove_executor_markers(repo_root, tid)
        _commit_generated_ticket_write(repo_root, ticket["_path"], tid, to_status, cfg)
        # Remove worktree after committing (so it's captured correctly)
        if wt:
            wt_path = Path(wt)
            if wt_path.exists():
                from lanegate.config import protected_branches

                protected = protected_branches(cfg)
                try:
                    remove_worktree(repo_root, wt_path, protected)
                except PermissionError as e:
                    print(f"WARNING: {e}", file=sys.stderr)
            for companion in ticket.get("companion_repos") or []:
                companion_worktree_cleanup(repo_root, companion, tid)
    else:
        ticket["status"] = to_status
        _stamp_status_changed(ticket)
        append_lifecycle_event(
            ticket,
            event="status_changed",
            from_status=current,
            to_status=to_status,
            summary="lifecycle transition",
        )
        write_ticket(ticket)
        if to_status != "hibernated":
            _remove_recovery_file(repo_root, tid)
        _remove_executor_markers(repo_root, tid)
        _commit_generated_ticket_write(repo_root, ticket["_path"], tid, to_status, cfg)

    print(f"{tid}: {current} → {to_status}")


def _enforce_verification_gate(
    ticket: dict,
    cfg: dict,
    repo_root: Path,
    *,
    verdict: str | None,
    findings: str | None,
) -> None:
    """Recompute per-criterion verification records and, when an
    ``approved`` verdict is being recorded, block it if required criteria
    still lack evidence.

    Always persists the recomputed ``ticket["verification"]`` field (even
    when it goes on to block), so a human inspecting the ticket after a
    block sees exactly which criteria are unresolved without having to
    re-run anything. ``findings`` text on an approved verdict is treated as
    the reviewer's human judgment covering any criterion that couldn't be
    automatically verified -- it flips those to "manual" rather than
    leaving them unverified, preserving human judgment for non-automatable
    criteria per this gate's own design.
    """
    from lanegate.analyze import verify_acceptance_criteria

    wt = ticket.get("worktree")
    wt_path = Path(wt) if wt and Path(wt).exists() else repo_root
    records = verify_acceptance_criteria(
        ticket,
        wt_path,
        prior=ticket.get("verification"),
        trunk_branch=resolve_trunk_branch(cfg, repo_root),
    )

    if verdict == "approved" and findings:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        for r in records:
            if r.status == "unverified":
                r.status = "manual"
                r.evidence = f"human judgment via review findings: {findings.strip()[:200]}"
                r.checked_at = now

    ticket["verification"] = [r.as_metadata() for r in records]
    write_ticket(ticket)
    _commit_generated_ticket_write(repo_root, ticket["_path"], ticket["id"], "verification", cfg)

    if verdict != "approved":
        return

    unresolved = [r for r in records if r.status == "unverified"]
    if unresolved:
        print(
            f"ERROR: {ticket['id']} has {len(unresolved)} acceptance criterion/criteria "
            "with no automated evidence and no review --findings to cover them by human "
            "judgment:",
            file=sys.stderr,
        )
        for r in unresolved:
            print(f"  - {r.criterion}", file=sys.stderr)
        print(
            "Fix the gap, or re-run with --findings documenting the human verification "
            "performed for these criteria.",
            file=sys.stderr,
        )
        sys.exit(1)


@_track_direct_action("complete")
def cmd_complete(
    ticket_id: str,
    cfg: dict,
    repo_root: Path,
    *,
    allow_drift: bool = False,
    auto_update_touches: bool = False,
) -> None:
    repo_root = _control_repo_root(repo_root)
    tid = canonical_id(ticket_id)
    tickets_dir = repo_root / cfg["tickets_dir"]
    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    ticket = next((t for t in tickets if t["id"] == tid), None)

    if not ticket:
        print(f"ERROR: {tid} not found", file=sys.stderr)
        sys.exit(1)

    wt = ticket.get("worktree")
    if not wt:
        print(
            f"ERROR: {tid} has no worktree set — refusing to advance to code_complete.",
            file=sys.stderr,
        )
        sys.exit(1)

    wt_path = Path(wt)
    if not wt_path.exists():
        print(
            f"ERROR: {tid} worktree {wt} does not exist — refusing to advance to "
            f"code_complete. Restore the worktree or run `lanegate reopen {tid}` to reset "
            f"it to open for a fresh dispatch.",
            file=sys.stderr,
        )
        sys.exit(1)

    if auto_update_touches:
        declared = set(ticket.get("touches") or [])
        if "*" not in declared:
            changed = _get_changed_files(wt_path)
            undeclared = changed - declared
            if undeclared:
                new_touches = sorted(declared | undeclared)
                print(
                    f"[complete] {tid}: auto-updating touches with {sorted(undeclared)!r}",
                    file=sys.stderr,
                )
                ticket["touches"] = new_touches
                write_ticket(ticket)
    else:
        check_touches_compliance(tid, ticket, wt_path, allow_drift=allow_drift)

    timed_out_guards: list[str] = []
    try:
        with safeguard_lock(repo_root, tid):
            safeguards_passed, safeguard_reason = run_safeguards(
                "pre_complete", ticket, cfg, wt_path, timed_out_guards=timed_out_guards
            )
    except SafeguardLockHeld as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if not safeguards_passed:
        if safeguard_reason and "unresolved command:" in safeguard_reason:
            print(
                f"ERROR: {tid} pre_complete safeguard cannot resolve: {safeguard_reason}",
                file=sys.stderr,
            )
            print(
                "Leaving ticket status unchanged. Fix PATH or safeguard configuration before retrying.",
                file=sys.stderr,
            )
            sys.exit(1)
        reason = f"pre_complete safeguards failed: {safeguard_reason}"
        print(
            f"WARNING: {tid} pre_complete safeguards failed — routing to needs_review.",
            file=sys.stderr,
        )
        _mark_needs_review(ticket, cfg, repo_root, reason=reason)
        return

    if not _has_committed_changes(wt_path):
        print(
            f"ERROR: {tid} worktree has no commits ahead of main — refusing to "
            f"advance to code_complete. If you edited files manually, commit them "
            f"in the worktree first (git -C {wt_path} add -A && git -C {wt_path} "
            f"commit), then re-run `lanegate complete {tid}`. Only use `lanegate "
            f"reopen {tid}` if you actually want to discard this worktree and "
            f"redispatch from scratch.",
            file=sys.stderr,
        )
        sys.exit(1)

    from lanegate.orchestrate.audit import has_step_bundle

    if not has_step_bundle(repo_root, tid, "implement"):
        from lanegate.orchestrate.pool import capture_manual_implement_step_run

        capture_manual_implement_step_run(
            repo_root,
            wt_path,
            ticket,
            cfg,
            safeguards_passed=safeguards_passed,
            safeguard_reason=safeguard_reason,
        )
        # _advance() reloads the ticket, so persist the provenance before it
        # writes the code_complete transition.
        ticket["implement_mode"] = "manual"
        write_ticket(ticket)

    _enforce_verification_gate(ticket, cfg, repo_root, verdict=None, findings=None)

    from lanegate.orchestrate.review import _git_head_sha

    sha = _git_head_sha(wt_path)
    if sha:
        # records the commit pre_complete safeguards actually ran
        # against, so build_review_prompt can detect a stale "tests already
        # ran" claim after a later fix commit. _advance() reloads the ticket
        # from disk, so this must be persisted before it runs.
        ticket["pre_complete_verified_sha"] = sha
        write_ticket(ticket)

    _advance(ticket_id, "code_complete", ["in_progress"], cfg, repo_root)


def cmd_fail(
    ticket_id: str,
    cfg: dict,
    repo_root: Path,
    *,
    reason: str = "",
) -> None:
    """Transition a ticket from in_progress → failed.

    Releases the worktree lock (sets worktree=None so the touches are freed)
    and records an optional failure reason in the ticket body.
    Tickets in ``failed`` status are terminal — they are never eligible for
    merge, complete, or review.

    Preserves the worktree if the ticket has review_verdict=changes_requested
    to allow human inspection.
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
    if current not in ("in_progress", "open"):
        print(
            f"ERROR: {tid} is '{current}', cannot mark as failed from this state",
            file=sys.stderr,
        )
        sys.exit(1)

    if reason:
        body = ticket.get("_body", "")
        failure_header = "## Failure Reason"
        if failure_header not in body:
            body = body.rstrip() + f"\n\n{failure_header}\n\n{reason}\n"
        ticket["_body"] = body

    # The ticket body is executor-writable; cleanup may only delete the
    # branch derived from the canonical ticket ID.
    branch = branch_name(tid)
    
    preserve = ticket.get("review_verdict") == "changes_requested" or _worktree_has_commits(ticket, cfg, repo_root)

    # Validate ownership before mutating lifecycle state. A canonical pathname
    # alone is not authority to force-remove a different or detached worktree.
    if not preserve:
        from lanegate.config import protected_branches
        from lanegate.worktree import worktree_path

        protected = protected_branches(cfg)
        worktrees_dir = repo_root / cfg.get("worktrees_dir", ".lanegate/worktrees")
        canonical_wt = worktree_path(worktrees_dir, tid)
        if canonical_wt.exists():
            remove_worktree(repo_root, canonical_wt, protected, expected_branch=branch)
            if canonical_wt.exists():
                import shutil

                shutil.rmtree(canonical_wt)
            if canonical_wt.exists():
                raise RuntimeError(f"ERROR: Failed to remove worktree directory at {canonical_wt}")

    ticket["status"] = "failed"
    _remove_recovery_file(repo_root, tid)

    # Preserve worktree if ticket has review_verdict=changes_requested or has commits
    if not preserve:
        ticket["worktree"] = None
        ticket["branch"] = None

    _stamp_status_changed(ticket)
    append_lifecycle_event(
        ticket,
        event="failed",
        from_status=current,
        to_status="failed",
        summary=reason or "implementation failed",
    )
    write_ticket(ticket)
    _remove_executor_markers(repo_root, tid)

    _commit_generated_ticket_write(repo_root, ticket["_path"], tid, "failed", cfg)

    # Remove the worktree so the touches lock is freed (unless preserved)
    if not preserve:
        for companion in ticket.get("companion_repos") or []:
            companion_worktree_cleanup(repo_root, companion, tid)

        if branch:
            r_br = subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=repo_root,
                capture_output=True,
                text=True,
            )
            # Verify against the fully-qualified ref: a bare name is ambiguous
            # and `rev-parse --verify` prefers a same-named tag, which would
            # make this falsely report deletion failure for an already-deleted
            # branch that happens to share a name with an unrelated tag.
            r_verify = subprocess.run(
                ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
                cwd=repo_root,
                capture_output=True,
            )
            if r_verify.returncode == 0:
                raise RuntimeError(f"ERROR: Failed to delete git branch '{branch}': {r_br.stderr}")
    else:
        trigger = "review_verdict=changes_requested" if ticket.get("review_verdict") == "changes_requested" else "has_commits"
        print(
            f"[lifecycle] skipping worktree cleanup for {tid} ({trigger}) — preserved for inspection",
            file=sys.stderr,
        )

    print(f"{tid}: {current} → failed")
    if reason:
        print(f"  Reason: {reason}", file=sys.stderr)


def _worktree_has_commits(ticket: dict, cfg: dict, repo_root: Path) -> bool:
    """True if the ticket's worktree branch has real commits ahead of the trunk.

    Distinguishes "an executor actually did work here" from "this ticket
    never got past a pre-flight gate" (e.g. the acceptance-contract audit),
    where the worktree directory exists but is empty of ticket-specific
    commits. Shared with concurrency.locked_touches()/hollow_lock_holders(),
    which apply the same check to code_complete/in_review lock holders.
    """
    from lanegate.reviewer import worktree_has_commits

    return worktree_has_commits(ticket, repo_root, resolve_trunk_branch(cfg, repo_root))


def _restore_needs_review_for_review(
    ticket: dict,
    cfg: dict,
    repo_root: Path,
    *,
    history_note: str,
    from_status: str = "needs_review",
    reason_header: str = "## Needs Review Reason",
) -> None:
    """Restore a real-commit ticket (needs_review or failed) to code_complete."""
    tid = ticket["id"]
    body = ticket.get("_body", "")
    if reason_header in body:
        ticket["_body"] = body[: body.index(reason_header)].rstrip()
    ticket["status"] = "code_complete"
    ticket.pop("review_verdict", None)
    ticket.pop("review_summary", None)
    # Also clear the reviewer-cooldown retry budget: otherwise a
    # ticket escalated after repeated cooldowns re-enters review already at
    # its retry ceiling and escalates again on the very next cooldown, even
    # after a human has fixed the underlying pool/config issue.
    ticket.pop("review_retry_attempt", None)
    ticket.pop("review_retry_after", None)
    append_status_history(ticket, from_status, "code_complete", history_note)
    _stamp_status_changed(ticket)
    write_ticket(ticket)
    if cfg.get("commit_status_changes", True):
        _commit_status(repo_root, ticket["_path"], tid, "code_complete")
    print(
        f"{tid}: {from_status} → code_complete (worktree preserved, ready for `lanegate review`)"
    )


def cmd_reopen(
    ticket_id: str,
    cfg: dict,
    repo_root: Path,
) -> None:
    """Reset a ticket back into the pipeline for re-dispatch or re-review.

    From ``failed``, branches on whether the worktree has real commits:
      - No commits (the ordinary case: ``cmd_fail`` already nulled out
        ``worktree``/``branch`` and removed them): resets to ``open`` for a
        fresh dispatch; nothing extra to release.
      - Real commits exist (``cmd_fail`` preserves the worktree/branch
        instead of deleting them when ``review_verdict=changes_requested``,
        specifically so a human can inspect the rejected work): preserve the
        worktree and reset to ``code_complete`` so ``lanegate review`` can
        pick it back up, same as the needs_review-with-commits case below.
        ``failed`` has no ``lanegate start``-based recovery (unlike
        hibernated), so refusing here with no path forward would strand the
        ticket; restoring it is the only way back.

    From ``hibernated``: a zero-commit worktree is cleaned up and reset to
    ``open`` for a fresh dispatch. Hibernated worktrees with real commits are
    preserved for ``lanegate start`` to resume rather than discarded.

    From ``needs_review``, branches on whether the worktree has real commits:
      - No commits (the ticket never got past a pre-flight gate, e.g. the
        acceptance-contract audit blocked dispatch before any executor ran):
        clean up the stale empty worktree/branch and reset to ``open`` for a
        fresh dispatch, same as the failed case.
      - Real commits exist (the ticket was implemented and got downgraded
        from code_complete/in_review by a post-implementation gate, e.g. a
        touches-scope or static-analysis finding): preserve the worktree and
        reset to ``code_complete`` so ``lanegate review`` can pick it back
        up. ``reopen`` does not rebase the branch or dispatch an agent;
        review_verdict/review_summary are cleared since a human is overriding
        the gate's prior block.

    From ``code_complete``, same commits check but the meaning is inverted:
      - No commits (``cmd_complete`` advanced the ticket without any real
        work behind it — the exact bug this recovers from): clean up the
        stale empty worktree/branch and reset to ``open`` for a fresh
        dispatch, same as the needs_review no-commits case.
      - Real commits exist: this is a healthy, legitimately-progressed
        ticket, not a wedged one — refuse rather than discard real work.

    Strips the operational failure, hibernation, or needs-review reason from
    the ticket body either way, so the next pass starts clean.
    """
    repo_root = _control_repo_root(repo_root)
    tid = canonical_id(ticket_id)
    tickets_dir = repo_root / cfg["tickets_dir"]

    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    ticket = next((t for t in tickets if t["id"] == tid), None)
    if not ticket:
        print(f"ERROR: {tid} not found", file=sys.stderr)
        sys.exit(1)

    # Before spending effort reopening (and re-dispatching), check
    # whether this ticket's work already
    # exists somewhere else -- reopening a superseded ticket just re-does
    # work that's already done, and can create pointless rebase conflicts.
    from lanegate.reconciliation import reconcile_ticket

    evidence = reconcile_ticket(
        ticket, tickets, repo_root, trunk_branch=resolve_trunk_branch(cfg, repo_root)
    )
    if evidence is not None:
        evidence_desc = (
            f"already reachable from main (commit {evidence['replacement_commit'][:12]})"
            if "replacement_commit" in evidence
            else f"equivalent to already-merged {evidence['equivalent_ticket_id']}"
        )
        print(
            f"ERROR: {tid} appears superseded -- {evidence_desc}. Reopening it would just "
            f"re-do work that already exists. Run `lanegate supersede {tid}` to close it with "
            "that evidence recorded, instead of reopening.",
            file=sys.stderr,
        )
        sys.exit(1)

    current = ticket.get("status")
    if current not in ("failed", "hibernated", "needs_review", "code_complete"):
        print(
            f"ERROR: {tid} is '{current}', expected 'failed', 'hibernated', 'needs_review', "
            "or 'code_complete'",
            file=sys.stderr,
        )
        sys.exit(1)

    has_commits = (
        _worktree_has_commits(ticket, cfg, repo_root)
        if current in ("failed", "hibernated", "needs_review", "code_complete")
        else False
    )

    if current == "failed" and has_commits:
        wt_path = Path(ticket["worktree"]) if ticket.get("worktree") else None
        if wt_path is None or not wt_path.exists():
            print(
                f"ERROR: {tid}: worktree is missing ({wt_path}) — refusing to restore for review.",
                file=sys.stderr,
            )
            sys.exit(1)

        _restore_needs_review_for_review(
            ticket,
            cfg,
            repo_root,
            from_status="failed",
            reason_header="## Failure Reason",
            history_note=(
                "reopened via lanegate reopen — worktree with real commits (preserved by "
                "cmd_fail for changes_requested) restored for review, not discarded"
            ),
        )
        return

    if current == "hibernated" and has_commits:
        print(
            f"ERROR: {tid} is hibernated with real commits ahead of main — preserve that work "
            f"with `lanegate start {tid}` instead of reopening it for a fresh dispatch.",
            file=sys.stderr,
        )
        sys.exit(1)

    if current == "code_complete" and has_commits:
        print(
            f"ERROR: {tid} is code_complete with real commits ahead of main — nothing to "
            f"recover. `lanegate reopen` only resets wedged zero-commit tickets; use "
            f"`lanegate review {tid}` to move it forward.",
            file=sys.stderr,
        )
        sys.exit(1)

    if current == "needs_review" and has_commits:
        cause = classify_needs_review_cause(ticket)
        if cause == "protected_path" and not (ticket.get("protected_path_approved_at") or ticket.get("human_review_approved_at")):
            print(
                f"ERROR: {tid} is needs_review for a hard-blocked path — this requires an "
                f"explicit human decision, not an automatic reopen. Inspect the diff, then run: "
                f"lanegate human-review {tid} --rationale \"...\"",
                file=sys.stderr,
            )
            sys.exit(1)

        wt_path = Path(ticket["worktree"]) if ticket.get("worktree") else None
        if wt_path is None or not wt_path.exists():
            print(
                f"ERROR: {tid}: worktree is missing ({wt_path}) — refusing to restore for review.",
                file=sys.stderr,
            )
            sys.exit(1)

        _restore_needs_review_for_review(
            ticket,
            cfg,
            repo_root,
            history_note=(
                "reopened via lanegate reopen — worktree with real commits preserved and "
                "restored for review (no rebase or agent dispatch)"
            ),
        )
        return

    body = ticket.get("_body", "")
    for header in ("## Failure Reason", "## Hibernation Reason", "## Needs Review Reason"):
        if header in body:
            body = body[: body.index(header)].rstrip()
    ticket["_body"] = body

    # The ticket body is executor-writable; cleanup may only delete the
    # branch derived from the canonical ticket ID.
    branch = branch_name(tid)

    from lanegate.config import protected_branches

    protected = protected_branches(cfg)
    worktrees_dir = repo_root / cfg.get("worktrees_dir", ".lanegate/worktrees")
    canonical_wt = worktree_path(worktrees_dir, tid)

    # Ticket metadata is agent-writable.  Only remove the path derived from
    # trusted configuration and the canonical ticket ID; never recurse into a
    # path supplied by ticket["worktree"].
    if canonical_wt.exists():
        remove_worktree(repo_root, canonical_wt, protected, expected_branch=branch)
        if canonical_wt.exists():
            import shutil

            shutil.rmtree(canonical_wt)
        if canonical_wt.exists():
            raise RuntimeError(f"ERROR: Failed to remove worktree directory at {canonical_wt}")

    for companion in ticket.get("companion_repos") or []:
        companion_worktree_cleanup(repo_root, companion, tid)

    if branch:
        r_br = subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        # Verify against the fully-qualified ref: a bare name is ambiguous and
        # `rev-parse --verify` prefers a same-named tag, which would make this
        # falsely report deletion failure for an already-deleted branch that
        # happens to share a name with an unrelated tag.
        r_verify = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
            cwd=repo_root,
            capture_output=True,
        )
        if r_verify.returncode == 0:
            raise RuntimeError(f"ERROR: Failed to delete git branch '{branch}': {r_br.stderr}")

    ticket["worktree"] = None
    ticket["branch"] = None
    ticket.pop("review_verdict", None)
    ticket.pop("review_summary", None)

    if current == "hibernated":
        reopen_reason = (
            "reopened via lanegate reopen — hibernated worktree had no real commits ahead of "
            "main, cleaned up for fresh dispatch"
        )
    elif current == "needs_review":
        reopen_reason = (
            "reopened via lanegate reopen — worktree had no real commits ahead of main "
            "(never got past a pre-flight gate), cleaned up for fresh dispatch"
        )
    elif current == "code_complete":
        reopen_reason = (
            "reopened via lanegate reopen — worktree had no real commits ahead of main "
            "despite reaching code_complete, cleaned up for fresh dispatch"
        )
    else:
        reopen_reason = "reopened via lanegate reopen — failed ticket reset for fresh dispatch"
    append_status_history(ticket, current, "open", reopen_reason)
    ticket["status"] = "open"
    _stamp_status_changed(ticket)
    write_ticket(ticket)
    _remove_recovery_file(repo_root, tid)

    _commit_generated_ticket_write(repo_root, ticket["_path"], tid, "open", cfg)

    print(f"{tid}: {current} → open")


def cmd_resolve_conflict(
    ticket_id: str,
    cfg: dict,
    repo_root: Path,
    *,
    pool_name: str | None = None,
) -> None:
    """Explicitly rebase a needs-review worktree and resolve a conflict with an agent.

    Unlike ``cmd_reopen``, this is a work-execution command: it may mutate the
    branch with a rebase and invoke an executor when the rebase conflicts.
    """
    repo_root = _control_repo_root(repo_root)
    tid = canonical_id(ticket_id)
    if pool_name is not None and pool_name not in (cfg.get("pools") or {}):
        print(
            f"ERROR: --pool {pool_name!r} is not defined in pools: in .lanegate.yml",
            file=sys.stderr,
        )
        sys.exit(1)

    tickets_dir = repo_root / cfg["tickets_dir"]
    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    ticket = next((t for t in tickets if t["id"] == tid), None)
    if ticket is None:
        print(f"ERROR: {tid} not found", file=sys.stderr)
        sys.exit(1)
    if ticket.get("status") != "needs_review":
        print(
            f"ERROR: {tid} is '{ticket.get('status')}', expected 'needs_review' before resolving a conflict",
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
        print(f"ERROR: {tid}: worktree is missing ({wt_path})", file=sys.stderr)
        sys.exit(1)

    def run_post_rebase_safeguards() -> tuple[bool, str | None]:
        """Run the post-rebase guard under the same per-ticket lock as complete."""
        timed_out_guards: list[str] = []
        try:
            with safeguard_lock(repo_root, tid):
                result = run_safeguards(
                    "pre_complete", ticket, cfg, wt_path, timed_out_guards=timed_out_guards
                )
        except SafeguardLockHeld as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        if result[0]:
            from lanegate.orchestrate.review import _git_head_sha

            sha = _git_head_sha(wt_path)
            if sha:
                ticket["pre_complete_verified_sha"] = sha
                write_ticket(ticket)
        return result

    from lanegate.orchestrate import _abort_rebase, _run_rebase, _worktree_is_dirty

    if _worktree_is_dirty(wt_path):
        print(
            f"ERROR: {tid}: worktree has uncommitted changes; commit or discard them before resolving a rebase conflict",
            file=sys.stderr,
        )
        sys.exit(1)

    rebase_state, rebase_detail = _run_rebase(
        wt_path, base=resolve_trunk_branch(cfg, repo_root)
    )
    if rebase_state == "clean":
        safeguards_passed, safeguard_reason = run_post_rebase_safeguards()
        if not safeguards_passed:
            reason = f"resolve-conflict pre_complete safeguards failed: {safeguard_reason}"
            _mark_needs_review(ticket, cfg, repo_root, reason=reason)
            print(f"{tid}: post-rebase verification failed — left in needs_review", file=sys.stderr)
            return
        _restore_needs_review_for_review(
            ticket,
            cfg,
            repo_root,
            history_note="resolved via lanegate resolve-conflict — rebased cleanly onto main",
        )
        return
    if rebase_state == "error":
        print(f"ERROR: {tid}: could not rebase onto main ({rebase_detail})", file=sys.stderr)
        sys.exit(1)

    from lanegate.orchestrate.autofix import run_rebase_fix_agent

    if run_rebase_fix_agent(
        ticket, cfg, repo_root, wt_path, rebase_detail, pool_name=pool_name
    ):
        safeguards_passed, safeguard_reason = run_post_rebase_safeguards()
        if not safeguards_passed:
            reason = f"resolve-conflict pre_complete safeguards failed: {safeguard_reason}"
            _mark_needs_review(ticket, cfg, repo_root, reason=reason)
            print(f"{tid}: post-rebase verification failed — left in needs_review", file=sys.stderr)
            return
        _restore_needs_review_for_review(
            ticket,
            cfg,
            repo_root,
            history_note=(
                "resolved via lanegate resolve-conflict — rebase conflict fixed by "
                f"agent from pool {pool_name or cfg.get('default_pool') or 'default'}"
            ),
        )
        return

    _abort_rebase(wt_path)
    _mark_needs_review(
        ticket,
        cfg,
        repo_root,
        reason=(
            "lanegate resolve-conflict attempted to rebase this branch onto main, but the "
            "selected fix agent could not resolve the conflict. The rebase was aborted. "
            "(reason_code: rebase_conflict_failed)"
        ),
    )
    print(f"{tid}: rebase conflict unresolved — left in needs_review", file=sys.stderr)
    sys.exit(1)


def cmd_supersede(ticket_id: str, cfg: dict, repo_root: Path, *, reason: str = "") -> None:
    """Close a ticket once reconciliation finds evidence its work
    already exists elsewhere: either its own branch is already an ancestor
    of main, or an already-merged ticket with identical touches and a
    similar title covers the same intent. Records that evidence as durable
    ``replacement_commit``/``equivalent_ticket_id`` metadata. A non-empty
    human ``reason`` permits retirement without automated evidence and is
    recorded in status history instead.

    Unlike ``cmd_fail``, this is not a failure -- the ticket doesn't need
    re-dispatch, it needs to stop holding its touches lock and cluttering
    the active board. Refuses (rather than silently no-op) when
    reconciliation finds no evidence, so this can't be used to hand-wave a
    ticket closed without a real reason on record. The explicit ``reason``
    path supplies that durable human judgment for obsolete work.
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
    if current in TERMINAL_STATUSES:
        print(f"ERROR: {tid} is already '{current}'", file=sys.stderr)
        sys.exit(1)

    manual_reason = reason.strip()
    if manual_reason:
        evidence_desc = "manual reason recorded"
        history_note = f"superseded (manual reason: {manual_reason})"
    else:
        from lanegate.reconciliation import reconcile_ticket

        evidence = reconcile_ticket(
            ticket, tickets, repo_root, trunk_branch=resolve_trunk_branch(cfg, repo_root)
        )
        if evidence is None:
            print(
                f"ERROR: no reconciliation evidence found for {tid} -- its branch (if any) "
                "isn't already reachable from main, and no already-merged ticket with "
                "identical touches and a similar title was found. Not marking superseded.",
                file=sys.stderr,
            )
            sys.exit(1)

        ticket.update(evidence)
        evidence_desc = (
            f"replacement_commit={evidence['replacement_commit'][:12]}"
            if "replacement_commit" in evidence
            else f"equivalent_ticket_id={evidence['equivalent_ticket_id']}"
        )
        history_note = f"superseded ({evidence_desc})"
    append_status_history(ticket, str(current), "closed", history_note)

    wt = ticket.get("worktree")
    ticket["status"] = "closed"
    ticket["worktree"] = None
    _stamp_status_changed(ticket)
    write_ticket(ticket)
    _remove_recovery_file(repo_root, tid)
    _remove_executor_markers(repo_root, tid)
    _commit_generated_ticket_write(repo_root, ticket["_path"], tid, "superseded", cfg)

    if wt:
        wt_path = Path(wt)
        if wt_path.exists():
            from lanegate.config import protected_branches

            protected = protected_branches(cfg)
            try:
                remove_worktree(repo_root, wt_path, protected)
            except PermissionError as e:
                print(f"WARNING: {e}", file=sys.stderr)

    print(f"{tid}: {current} → closed ({evidence_desc})")


def cmd_close(ticket_id: str, cfg: dict, repo_root: Path, *, reason: str = "") -> None:
    """Close a completed no-code ticket with a durable human reason.

    This is intentionally distinct from ``cmd_supersede``: it records that
    the ticket's own close criteria were completed, rather than claiming the
    work was replaced elsewhere.  It only accepts tickets without a worktree,
    so it cannot discard active or unmerged work.
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
    if current in TERMINAL_STATUSES:
        print(f"ERROR: {tid} is already '{current}'", file=sys.stderr)
        sys.exit(1)
    completed_reason = reason.strip()
    if not completed_reason:
        print(
            "ERROR: --reason is required to close a ticket as completed; record the evidence or outcome.",
            file=sys.stderr,
        )
        sys.exit(1)
    if ticket.get("worktree"):
        print(
            f"ERROR: {tid} has a worktree; use review/merge/done for code work, or supersede it with evidence.",
            file=sys.stderr,
        )
        sys.exit(1)

    append_status_history(ticket, str(current), "closed", f"closed as completed: {completed_reason}")
    ticket["status"] = "closed"
    _stamp_status_changed(ticket)
    write_ticket(ticket)
    _remove_recovery_file(repo_root, tid)
    _remove_executor_markers(repo_root, tid)
    _commit_generated_ticket_write(repo_root, ticket["_path"], tid, "closed", cfg)
    print(f"{tid}: {current} → closed (completed: {completed_reason})")


def cmd_open(
    ticket_id: str,
    cfg: dict,
    repo_root: Path,
) -> None:
    """Transition a draft ticket to open without re-running analysis.

    Requires ``touches`` to be non-empty — if it is empty, the user must run
    ``lanegate analyze`` first to populate it (``lanegate start`` would refuse an
    empty-touches ticket anyway).
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
    if current != "draft":
        print(
            f"ERROR: {tid} is '{current}', expected 'draft'",
            file=sys.stderr,
        )
        sys.exit(1)

    if not ticket.get("touches"):
        print(
            f"ERROR: {tid} has no touches — run `lanegate analyze {tid}` to populate them first",
            file=sys.stderr,
        )
        sys.exit(1)

    ticket["status"] = "open"
    _stamp_status_changed(ticket)
    write_ticket(ticket)

    _commit_generated_ticket_write(repo_root, ticket["_path"], tid, "open", cfg)

    print(f"{tid}: draft → open")


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

    if verdict is not None:
        _enforce_verification_gate(ticket, cfg, repo_root, verdict=verdict, findings=findings)

    current = ticket.get("status")
    if verdict is None and current == "code_complete":
        from lanegate.orchestrate.pool import resolve_driver

        reviewer = resolve_driver("review", ticket, cfg)
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


@_track_direct_action("merge")
def cmd_merge(ticket_id: str, cfg: dict, repo_root: Path, *, reconcile: bool = False) -> None:
    repo_root = _control_repo_root(repo_root)
    tid = canonical_id(ticket_id)
    tickets_dir = repo_root / cfg["tickets_dir"]

    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    ticket = next((t for t in tickets if t["id"] == tid), None)
    if not ticket:
        print(f"ERROR: {tid} not found", file=sys.stderr)
        sys.exit(1)

    current = ticket.get("status")
    if current != "in_review":
        if current == "code_complete":
            print(
                f"ERROR: {tid} is 'code_complete' — run 'lanegate review {tid} --verdict approved' first.",
                file=sys.stderr,
            )
        else:
            print(
                f"ERROR: {tid} is '{current}', expected in_review with an approved verdict.",
                file=sys.stderr,
            )
        sys.exit(1)

    verdict = ticket.get("review_verdict")
    if verdict != "approved":
        if verdict is None:
            print(
                f"ERROR: {tid} is in_review but has no review_verdict — "
                f"run 'lanegate review {tid} --verdict approved' to record approval before merging.",
                file=sys.stderr,
            )
        else:
            print(
                f"ERROR: {tid} review_verdict is '{verdict}', not 'approved' — "
                f"re-run 'lanegate review {tid} --verdict approved' after addressing feedback.",
                file=sys.stderr,
            )
        sys.exit(1)

    # Defense in depth: re-check the verification records already
    # persisted by `lanegate review` rather than recomputing -- if the ticket
    # frontmatter was hand-edited (or review_verdict flipped without going
    # back through the gate) between review and merge, don't let a stale
    # approval slip an unresolved criterion through.
    unresolved = [
        r
        for r in (ticket.get("verification") or [])
        if isinstance(r, dict) and r.get("status") == "unverified"
    ]
    if unresolved:
        print(
            f"ERROR: {tid} has unresolved verification records — re-run "
            f"'lanegate review {tid} --verdict approved' to re-gate before merging:",
            file=sys.stderr,
        )
        for r in unresolved:
            print(f"  - {r.get('criterion')}", file=sys.stderr)
        sys.exit(1)

    branch = ticket.get("branch")
    wt_val = ticket.get("worktree")

    if wt_val:
        from lanegate.orchestrate.guards import check_control_plane_compliance
        ok_cp, cp_err = check_control_plane_compliance(
            ticket, repo_root=repo_root, cfg=cfg, worktree_path=Path(wt_val), check_review_independence=True
        )
        if not ok_cp:
            reason = f"control plane compliance failed: {cp_err}"
            print(f"WARNING: {tid} {reason} — routing to needs_review.", file=sys.stderr)
            _mark_needs_review(ticket, cfg, repo_root, reason=reason)
            return

    # Run pre_merge safeguards before the git merge when the project has not
    # explicitly disabled the redundant worktree re-check. The same effective
    # guard list is always verified against merged main below.
    safeguards_cfg = cfg.get("safeguards") or {}
    run_pre_merge_worktree = safeguards_cfg.get("pre_merge_worktree", True)
    if run_pre_merge_worktree and wt_val:
        wt_for_guards = Path(wt_val)
        merge_timed_out_guards: list[str] = []
        try:
            with safeguard_lock(repo_root, tid):
                safeguards_passed, safeguard_reason = run_safeguards(
                    "pre_merge", ticket, cfg, wt_for_guards, timed_out_guards=merge_timed_out_guards
                )
        except SafeguardLockHeld as exc:
            sys.exit(f"ERROR: {exc}")

        if not safeguards_passed:
            if safeguard_reason and "unresolved command:" in safeguard_reason:
                print(
                    f"ERROR: {tid} pre_merge safeguard cannot resolve: {safeguard_reason}",
                    file=sys.stderr,
                )
                print(
                    "Leaving ticket status unchanged. Fix PATH or safeguard configuration before retrying.",
                    file=sys.stderr,
                )
                sys.exit(1)
            reason = f"pre_merge safeguards failed: {safeguard_reason}"
            print(
                f"WARNING: {tid} pre_merge safeguards failed — routing to needs_review.",
                file=sys.stderr,
            )
            _mark_needs_review(ticket, cfg, repo_root, reason=reason)
            return
    elif not wt_val:
        print(
            f"  SKIP [pre_merge] worktree verification skipped for manual/no-worktree ticket."
        )
    else:
        print(
            f"  SKIP [pre_merge] worktree verification disabled by "
            "safeguards.pre_merge_worktree: false"
        )

    def _do_git_merge():
        from lanegate.reconciliation import (
            branch_reachable_from_main,
            conflicted_paths,
            is_metadata_only_conflict,
            resolve_metadata_conflict,
        )

        # Serialize all git operations on repo_root to prevent concurrent merge/commit
        # interference when max_parallel > 1 (F3 fix: worker-pool unserialized git ops).
        # Reconcile any uncommitted local write to the ticket's own file left on
        # main by an earlier lifecycle transition before attempting the merge. Otherwise
        # git's own safety check refuses the merge outright the moment the
        # incoming branch touches this file too, even when the two changes
        # wouldn't actually conflict textually.
        if _has_uncommitted_diff(repo_root, ticket["_path"]):
            subprocess.run(
                [
                    "git",
                    "commit",
                    "-s",
                    "--only",
                    str(ticket["_path"]),
                    "-m",
                    f"chore: {tid} pending ticket-file write before merge",
                ],
                cwd=repo_root,
                capture_output=True,
                text=True, encoding="utf-8",
            )

        _merged_into_branch = (
            subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True, encoding="utf-8",
                cwd=repo_root,
            ).stdout.strip()
            or resolve_trunk_branch(cfg, repo_root)
        )

        _pre_merge_head = None
        already_integrated_tip = None
        if branch:
            _pre_merge_head = subprocess.run(
                ["git", "rev-parse", "HEAD"], capture_output=True, text=True, encoding="utf-8", cwd=repo_root
            ).stdout.strip()

            already_integrated_tip = branch_reachable_from_main(
                repo_root, branch, resolve_trunk_branch(cfg, repo_root)
            )
            if already_integrated_tip:
                print(
                    f"{tid}: branch already integrated into main — skipping git merge, "
                    f"finalizing ticket"
                )
            else:
                r = subprocess.run(
                    [
                        "git",
                        "merge",
                        "--no-ff",
                        branch,
                        "-m",
                        f"Merge {tid}: {ticket.get('title', '')}",
                    ],
                    capture_output=True,
                    text=True, encoding="utf-8",
                    cwd=repo_root,
                )
                if r.returncode != 0:
                    conflicts = conflicted_paths(repo_root)
                    metadata_only = is_metadata_only_conflict(conflicts, cfg["tickets_dir"])

                    if metadata_only:
                        for p in conflicts:
                            resolve_metadata_conflict(repo_root, p)
                        commit = subprocess.run(
                            ["git", "commit", "-s", "--no-edit"],
                            cwd=repo_root,
                            capture_output=True,
                            text=True, encoding="utf-8",
                        )
                        if commit.returncode == 0:
                            print(f"{tid}: merge integrated; ticket status finalized")
                        else:
                            merge_head = repo_root / ".git" / "MERGE_HEAD"
                            if merge_head.exists():
                                subprocess.run(
                                    ["git", "merge", "--abort"],
                                    cwd=repo_root,
                                    capture_output=True,
                                )
                            detail = "\n".join(
                                s for s in (commit.stdout.strip(), commit.stderr.strip()) if s
                            )
                            msg = (
                                f"ERROR reconciling metadata conflict for {branch} → main "
                                f"(commit failed):\n{detail}"
                            )
                            print(msg, file=sys.stderr)
                            raise MergeFailedError(msg)
                    else:
                        # If a merge conflict left the repo mid-merge, abort it to restore clean state.
                        merge_head = repo_root / ".git" / "MERGE_HEAD"
                        if merge_head.exists():
                            subprocess.run(
                                ["git", "merge", "--abort"],
                                cwd=repo_root,
                                capture_output=True,
                            )
                        # git merge's real conflict detail ("CONFLICT (content): ...")
                        # goes to stdout, not stderr -- surface both or the message is
                        # silently empty on a real conflict.
                        detail = "\n".join(s for s in (r.stdout.strip(), r.stderr.strip()) if s)
                        msg = f"ERROR merging {branch} → main:\n{detail}"
                        print(msg, file=sys.stderr)
                        raise MergeFailedError(msg)
                if r.stdout.strip():
                    print(r.stdout.strip())

        return _merged_into_branch, _pre_merge_head, already_integrated_tip

    if _GIT_OPS_LOCK is not None:
        with _GIT_OPS_LOCK:
            _merged_into_branch, _pre_merge_head, _already_integrated = _do_git_merge()
    else:
        _merged_into_branch, _pre_merge_head, _already_integrated = _do_git_merge()

    if branch and _pre_merge_head is not None:
        # The pre_merge safeguard above only ever validated this ticket's own
        # isolated worktree against whatever main looked like when the branch
        # was created -- it never proves the *actual* merge commit (this
        # ticket's changes combined with everything else already on main,
        # including other tickets merged since) still passes. Re-run the same
        # guard list here, against repo_root itself, before the merge is
        # allowed to stand.
        post_merge_verify_timed_out: list[str] = []
        try:
            with safeguard_lock(repo_root, tid):
                post_merge_verify_passed, _post_merge_verify_reason = run_safeguards(
                    "pre_merge",
                    ticket,
                    cfg,
                    repo_root,
                    timed_out_guards=post_merge_verify_timed_out,
                    label="post_merge_verify",
                )
        except SafeguardLockHeld as exc:
            sys.exit(f"ERROR: {exc}")

        if not post_merge_verify_passed:
            if _already_integrated:
                if post_merge_verify_timed_out:
                    reason = (
                        f"post-merge verification safeguard timed out on main after merging: "
                        f"{', '.join(post_merge_verify_timed_out)}."
                    )
                else:
                    reason = (
                        f"post-merge verification safeguard failed on main: "
                        f"{_post_merge_verify_reason}"
                    )
                print(
                    f"WARNING: {tid} post-merge verification failed on already-integrated branch — keeping status 'merged' with diagnostic.",
                    file=sys.stderr,
                )
                _append_ticket_section(ticket, "## Post-Merge Verification Diagnostic", reason)
                ticket["status"] = "merged"
                ticket["post_merge_diagnostic"] = reason
                _stamp_status_changed(ticket)
                write_ticket(ticket)
                _commit_generated_ticket_write(repo_root, ticket["_path"], tid, "merged-diagnostic", cfg)

                msg = f"ERROR: {tid} — {reason}"
                print(msg, file=sys.stderr)
                raise MergeFailedError(msg)

            reset = subprocess.run(
                ["git", "reset", "--hard", _pre_merge_head],
                cwd=repo_root,
                capture_output=True,
                text=True, encoding="utf-8",
            )
            if reset.returncode != 0:
                detail = "\n".join(s for s in (reset.stdout.strip(), reset.stderr.strip()) if s)
                sys.exit(
                    f"ERROR: {tid} merge broke main's test suite AND the automatic rollback "
                    f"to {_pre_merge_head[:12]} failed:\n{detail}\n"
                    f"main is left mid-merge — resolve manually with `git status` in the "
                    f"control repo."
                )

            if post_merge_verify_timed_out:
                reason = (
                    f"merge of {tid} succeeded, but the post-merge verification safeguard "
                    f"timed out on main after merging: {', '.join(post_merge_verify_timed_out)}. "
                    f"main was reset back to its pre-merge commit ({_pre_merge_head[:12]})."
                )
            else:
                reason = (
                    f"merge of {tid} succeeded, but broke main's test suite once combined "
                    f"with everything already on main (this ticket's own worktree tests passed "
                    f"in isolation). main was reset back to its pre-merge commit "
                    f"({_pre_merge_head[:12]}); this ticket needs another look before "
                    f"re-merging."
                )
            _append_ticket_section(ticket, "## Needs Review Reason", reason)
            _clear_human_review_approval(ticket)
            ticket["status"] = "needs_review"
            ticket["review_verdict"] = "changes_requested"
            ticket["review_summary"] = (
                "merge succeeded but broke main post-merge — see Needs Review Reason"
            )
            _stamp_status_changed(ticket)
            write_ticket(ticket)
            _commit_generated_ticket_write(repo_root, ticket["_path"], tid, "needs_review", cfg)

            msg = f"ERROR: {tid} — {reason}"
            print(msg, file=sys.stderr)
            raise MergeFailedError(msg)

    if branch:
        _companion_failures = []
        for companion in ticket.get("companion_repos") or []:
            result = companion_branch_merge(
                repo_root,
                companion,
                branch,
                tid,
                ticket.get("title", ""),
                resolve_trunk_branch(cfg, repo_root),
            )
            if result in (CompanionMergeResult.FAILED_CHECKOUT, CompanionMergeResult.FAILED_MERGE):
                _companion_failures.append((companion, result))

        if _companion_failures:
            # Main repo merge above already succeeded — that can't be safely
            # unwound here. What we can and must do is refuse to advance the
            # ticket to "merged" so board/status stays truthful; the human
            # (or a retried `lanegate merge`) resolves the companion repo state.
            detail = ", ".join(f"{c} ({r.value})" for c, r in _companion_failures)
            msg = (
                f"ERROR: {tid} — main repo merged, but companion merge failed for: {detail}\n"
                f"Ticket status NOT advanced to 'merged' — resolve the companion repo "
                f"state and re-run 'lanegate merge {tid}'."
            )
            print(msg, file=sys.stderr)
            raise MergeFailedError(msg)

    # BUG FIX: capture worktree path BEFORE nulling (original code read it after = None)
    wt = ticket.get("worktree")

    # Collect analytics data BEFORE removing the worktree so git diff still works.
    # The worktree is still valid at this point (merge just completed on repo_root).
    _merge_branch = branch or branch_name(tid)
    _wt_for_analytics = Path(wt) if wt and Path(wt).exists() else repo_root
    try:
        _touched = _get_touched_files(
            _wt_for_analytics, _merge_branch, resolve_trunk_branch(cfg, repo_root)
        )
        _wall_ms = _get_branch_wall_time_ms(
            _wt_for_analytics, resolve_trunk_branch(cfg, repo_root)
        )
    except Exception:
        _touched = []
        _wall_ms = 0

    # Re-read the ticket from disk: `git merge` above may have just changed
    # this ticket's own on-disk body (new sections, DoD checkbox state
    # brought in from the branch). The in-memory `ticket` still reflects
    # main's pre-merge copy loaded at the top of this function, and writing
    # it back as-is would silently clobber whatever the merge just wrote.
    fresh = parse_ticket(ticket["_path"])
    if fresh is not None:
        ticket = fresh

    ticket["status"] = "merged"
    ticket["worktree"] = None
    _stamp_status_changed(ticket)
    append_lifecycle_event(
        ticket,
        event="merged",
        from_status=current,
        to_status="merged",
        summary="merge completed on main",
    )
    write_ticket(ticket)
    _remove_recovery_file(repo_root, tid)
    _cleanup_ticket_notes(ticket, repo_root)
    _commit_generated_ticket_write(repo_root, ticket["_path"], tid, "merged", cfg)

    # Now remove the worktree with the captured path
    if wt:
        wt_path = Path(wt)
        if wt_path.exists():
            from lanegate.config import protected_branches

            protected = protected_branches(cfg)
            try:
                remove_worktree(repo_root, wt_path, protected)
            except PermissionError as e:
                print(f"WARNING: {e}", file=sys.stderr)
        for companion in ticket.get("companion_repos") or []:
            companion_worktree_cleanup(repo_root, companion, tid)

    # Auto-log every merge so analytics is never empty after ticket completion
    try:
        from lanegate.context_log import (
            _get_default_db_path,
            _get_project_id,
            get_ticket_executor,
            log_agent_run,
        )

        _db_path = _get_default_db_path()
        # step_costs already knows which executor/pool instance really ran
        # this ticket's steps -- ticket.get("executor")/cfg.get("executor")
        # is only a manual per-ticket pin or the project's static default
        # driver, neither of which reflects actual per-step routing.
        _real_executor = get_ticket_executor(_db_path, _get_project_id(repo_root), tid)
        log_agent_run(
            log_path=None,
            ticket_id=tid,
            subagent_tokens=None,
            tool_uses=0,
            duration_ms=0,
            touched_files=_touched,
            repo_root=repo_root,
            executor=_real_executor or ticket.get("executor") or cfg.get("executor", "claude"),
            wall_time_ms=_wall_ms,
            tests_passed=None,
            db_path=_db_path,
        )
    except Exception:
        pass  # analytics failure must never break the merge path

    print(f"{tid}: {current} → merged")
    if effective_safeguards("post_merge", ticket, cfg):
        print(f"  Next: lanegate validate {tid}")

    # Close the GitHub PR if one was opened during review (fail-silent — gh may be absent).
    pr_number = ticket.get("pr_number")
    if pr_number and shutil.which("gh"):
        subprocess.run(
            ["gh", "pr", "close", str(pr_number), "--comment", "Merged locally via lanegate merge."],
            cwd=repo_root,
            capture_output=True,
        )

    flag = ticket.get("feature_flag")
    if flag:
        print(f"\n  Feature gated behind flag '{flag}' (default OFF).")
        print(f"  Enable when validated: {APP_NAME} flag enable {flag}")

    if branch:
        _auto_promote_environments(cfg, repo_root, _merged_into_branch)

    from lanegate.pending_globals import check_pending_globals, format_pending_globals_notice
    pg_info = check_pending_globals(repo_root)
    if pg_info["has_pending"]:
        print(f"\n  {format_pending_globals_notice(pg_info)}")



def cmd_validate(ticket_id: str, cfg: dict, repo_root: Path) -> None:
    repo_root = _control_repo_root(repo_root)
    tid = canonical_id(ticket_id)
    tickets_dir = repo_root / cfg["tickets_dir"]

    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    ticket = next((t for t in tickets if t["id"] == tid), None)
    if not ticket:
        print(f"ERROR: {tid} not found", file=sys.stderr)
        sys.exit(1)

    current = ticket.get("status")
    if current != "merged":
        print(f"ERROR: {tid} is '{current}', expected one of ['merged']", file=sys.stderr)
        sys.exit(1)

    safeguards_passed, safeguard_reason = run_safeguards("post_merge", ticket, cfg, repo_root)
    if not safeguards_passed:
        if safeguard_reason and "unresolved command:" in safeguard_reason:
            print(
                f"ERROR: {tid} post_merge safeguard cannot resolve: {safeguard_reason}",
                file=sys.stderr,
            )
            print(
                "Leaving ticket status unchanged. Fix PATH or safeguard configuration before retrying.",
                file=sys.stderr,
            )
            sys.exit(1)
        reason = f"post_merge safeguards failed: {safeguard_reason}"
        print(
            f"WARNING: {tid} post_merge safeguards failed — routing to needs_review.",
            file=sys.stderr,
        )
        _mark_needs_review(ticket, cfg, repo_root, reason=reason)
        return

    _advance(ticket_id, "validated", ["merged"], cfg, repo_root)


def cmd_done(ticket_id: str, cfg: dict, repo_root: Path) -> None:
    repo_root = _control_repo_root(repo_root)
    tid = canonical_id(ticket_id)
    tickets_dir = repo_root / cfg["tickets_dir"]

    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    ticket = next((t for t in tickets if t["id"] == tid), None)
    if not ticket:
        print(f"ERROR: {tid} not found", file=sys.stderr)
        sys.exit(1)

    guards = effective_safeguards("post_merge", ticket, cfg)
    if guards and ticket.get("status") == "merged":
        print(
            f"ERROR: {tid} has post_merge safeguards configured — run 'lanegate validate {tid}' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    allow_from = ["validated"] if guards else ["validated", "merged"]
    _advance(ticket_id, "done", allow_from, cfg, repo_root)
