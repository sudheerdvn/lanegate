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

import shutil
import subprocess
import sys
import threading
import time as time
from datetime import UTC, datetime
from pathlib import Path

from lanegate import APP_NAME
from lanegate.companion import (
    CompanionMergeResult,
    companion_branch_create,
    companion_branch_merge,
    companion_worktree_cleanup,
)
from lanegate.config import resolve_trunk_branch
from lanegate.concurrency import (
    SafeguardLockHeld,
    check_local_not_behind_remote,
    claim_lock,
    locked_touches,
    safeguard_lock,
    touches_overlap,
)
from lanegate.git import git_text
from lanegate.promote import _auto_promote_environments
from lanegate.safeguards import effective_safeguards, run_safeguards
from lanegate.ticket import (
    TERMINAL_STATUSES,
    append_lifecycle_event,
    append_status_history,
    branch_name,
    canonical_id,
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

    if _GIT_OPS_LOCK is not None:
        with _GIT_OPS_LOCK:
            return _run_commit()
    return _run_commit()


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


def cmd_start(
    ticket_id: str,
    cfg: dict,
    repo_root: Path,
    *,
    interactive: bool = True,
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

        existing_wt = Path(ticket["worktree"]) if ticket.get("worktree") else None
        reattaching = (
            previous_status in ("hibernated", "needs_review")
            and existing_wt is not None
            and existing_wt.exists()
        )
        if reattaching:
            assert existing_wt is not None  # reattaching implies existing_wt is set and exists()
            wt_path = existing_wt
            branch = ticket.get("branch") or branch
        else:
            # Create the worktree BEFORE writing or committing the status change.
            # If this raises RuntimeError the ticket file is untouched and still open.
            try:
                wt_path = create_worktree(repo_root, worktrees_dir, tid, branch, trunk_branch)
            except RuntimeError as e:
                print(f"ERROR: {e}", file=sys.stderr)
                sys.exit(1)

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
        executor = ticket.get("executor") or cfg.get("executor", "claude")
        _write_executor_marker(repo_root, tid, executor)

        if not _commit_generated_ticket_write(
            repo_root, ticket["_path"], tid, "in_progress", cfg, required=False
        ):
            # Roll back in-memory and on-disk; worktree is already created but
            # the status commit failed — leave the ticket open so a retry can
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
            print(f"ERROR: failed to commit status lock for {tid}", file=sys.stderr)
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
    print(f"Invariants: {', '.join(str(i) for i in (ticket.get('invariants') or [])) or 'none'}")
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
    """Record one auto-fix cycle attempt (TICK-120) for audit history.

    Appends a uniquely-headed ``## Auto-Fix Attempt N`` body section per
    attempt rather than one shared header, since ``_append_ticket_section``
    replaces a named section's content on repeat calls and would clobber
    earlier attempts otherwise. Status and review_verdict are left untouched
    on escalation — they stay code_complete/changes_requested so cmd_blocked
    and cmd_merge's guard (which key off exactly that pair) keep working.

    ``drift_ok``/``drift_reason`` (TICK-348), when given, are persisted as a
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


def cmd_needs_review(
    ticket_id: str,
    cfg: dict,
    repo_root: Path,
    *,
    reason: str = "",
) -> None:
    """Transition in_progress -> needs_review while preserving the worktree."""
    repo_root = _control_repo_root(repo_root)
    tid = canonical_id(ticket_id)
    tickets_dir = repo_root / cfg["tickets_dir"]
    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    ticket = next((t for t in tickets if t["id"] == tid), None)
    if not ticket:
        print(f"ERROR: {tid} not found", file=sys.stderr)
        sys.exit(1)

    current = ticket.get("status")
    if current != "in_progress":
        print(f"ERROR: {tid} is '{current}', expected in_progress", file=sys.stderr)
        sys.exit(1)

    _mark_needs_review(ticket, cfg, repo_root, reason=reason)


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
    """Recompute per-criterion verification records (TICK-283) and, when an
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
            f"advance to code_complete. Run `lanegate reopen {tid}` to reset it to "
            f"open for a fresh dispatch.",
            file=sys.stderr,
        )
        sys.exit(1)

    _enforce_verification_gate(ticket, cfg, repo_root, verdict=None, findings=None)
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

    wt = ticket.get("worktree")
    ticket["status"] = "failed"
    _remove_recovery_file(repo_root, tid)

    # Preserve worktree if ticket has review_verdict=changes_requested
    if ticket.get("review_verdict") != "changes_requested":
        ticket["worktree"] = None

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

    # Remove the worktree so the touches lock is freed (unless changes_requested)
    if wt and ticket.get("review_verdict") != "changes_requested":
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
    elif wt and ticket.get("review_verdict") == "changes_requested":
        print(
            f"[lifecycle] skipping worktree cleanup for {tid} (review_verdict=changes_requested) — preserved for inspection",
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


def cmd_reopen(
    ticket_id: str,
    cfg: dict,
    repo_root: Path,
) -> None:
    """Reset a ticket back into the pipeline for re-dispatch or re-review.

    From ``failed``: resets to ``open``. The worktree is already ``None``
    after ``cmd_fail``; nothing extra to release.

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
        touches-scope or static-analysis finding): before trusting the
        worktree, run the same rebase-onto-main check orchestrate.py runs
        for hibernated auto-resume (reuses ``_run_rebase``/``_abort_rebase``).
        A clean rebase updates the worktree in place and resets to
        ``code_complete`` so ``lanegate review`` can pick it back up.
        review_verdict/review_summary are cleared since a human is
        overriding the gate's prior block. A conflicting rebase is aborted
        and the ticket stays ``needs_review`` with the conflict recorded,
        instead of being handed back for review stale.

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

    # TICK-284: before spending effort reopening (and re-dispatching, or
    # rebasing a stale worktree), check whether this ticket's work already
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
        if current in ("hibernated", "needs_review", "code_complete")
        else False
    )

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
        from lanegate.orchestrate import _abort_rebase, _run_rebase, _worktree_is_dirty

        wt_path = Path(ticket["worktree"]) if ticket.get("worktree") else None

        # Reuse the same rebase-onto-main trust check the hibernation
        # auto-resume path runs in orchestrate.py before handing a
        # reattached worktree back into the pipeline — a needs_review
        # ticket can sit just as long (and drift just as far from main) as
        # a hibernated one, and blindly restoring it for review risks a
        # stale diff clobbering everything main has gained since.
        if wt_path is None or not wt_path.exists():
            rebase_state, rebase_detail = "error", f"missing worktree: {wt_path}"
        elif _worktree_is_dirty(wt_path):
            # Mirrors orchestrate's hibernated-resume handling: `git rebase`
            # cannot run against a dirty tree, and that alone isn't a
            # staleness failure — skip the check and fall through to the
            # existing restore-for-review behavior.
            rebase_state, rebase_detail = "skipped", "worktree has uncommitted changes"
        else:
            rebase_state, rebase_detail = _run_rebase(
                wt_path, base=resolve_trunk_branch(cfg, repo_root)
            )

        if rebase_state == "conflict":
            from lanegate.orchestrate.autofix import run_rebase_fix_agent

            if run_rebase_fix_agent(ticket, cfg, repo_root, wt_path, rebase_detail):
                print(
                    f"{tid}: rebase-onto-main conflict automatically resolved by fix agent",
                    file=sys.stderr,
                )
                rebase_state = "clean"
            else:
                _abort_rebase(wt_path)
                reason = (
                    "reopen attempted to rebase this branch onto main before restoring it "
                    "for review, but main has diverged in a way that conflicts with this "
                    "branch's changes. Resolve the conflict (e.g. via `lanegate orchestrate`, "
                    "which will retry the rebase and let an executor resolve it) before "
                    f"reopening again.\n\n{rebase_detail}"
                )
                _mark_needs_review(ticket, cfg, repo_root, reason=reason)
                print(
                    f"{tid}: rebase-onto-main conflict — left in needs_review, not restored "
                    "for review. See the ticket body for conflicted files.",
                    file=sys.stderr,
                )
                return

        if rebase_state == "error":
            print(
                f"ERROR: {tid}: could not verify the branch is current with main "
                f"({rebase_detail}) — refusing to restore for review.",
                file=sys.stderr,
            )
            sys.exit(1)

        body = ticket.get("_body", "")
        header = "## Needs Review Reason"
        if header in body:
            ticket["_body"] = body[: body.index(header)].rstrip()
        ticket["status"] = "code_complete"
        ticket.pop("review_verdict", None)
        ticket.pop("review_summary", None)
        history_note = (
            "reopened via lanegate reopen — worktree has real commits ahead of main, "
            + ("rebased onto main, " if rebase_state == "clean" else "")
            + "restored for review"
        )
        append_status_history(ticket, "needs_review", "code_complete", history_note)
        _stamp_status_changed(ticket)
        write_ticket(ticket)
        if cfg.get("commit_status_changes", True):
            _commit_status(repo_root, ticket["_path"], tid, "code_complete")
        print(f"{tid}: needs_review → code_complete (worktree preserved, ready for `lanegate review`)")
        return

    body = ticket.get("_body", "")
    for header in ("## Failure Reason", "## Hibernation Reason", "## Needs Review Reason"):
        if header in body:
            body = body[: body.index(header)].rstrip()
    ticket["_body"] = body

    if current in ("hibernated", "needs_review", "code_complete"):
        wt = ticket.get("worktree")
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
        ticket["worktree"] = None
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


def cmd_supersede(ticket_id: str, cfg: dict, repo_root: Path, *, reason: str = "") -> None:
    """Close a ticket once reconciliation (TICK-284) finds evidence its work
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
    append_status_history(ticket, current, "closed", history_note)

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
        # Ticket already in_review but missing a verdict (e.g. manually advanced
        # without --verdict). Allow updating the verdict without a status change.
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
        append_lifecycle_event(
            ticket,
            event="review_verdict",
            from_status="in_review",
            to_status="in_review",
            summary=f"review verdict: {verdict}" + (f" — {summary}" if summary else ""),
        )
        write_ticket(ticket)
        _commit_generated_ticket_write(
            repo_root, ticket["_path"], tid, f"review-verdict-{verdict}", cfg
        )
        print(f"{tid}: review_verdict set to {verdict} (status stays in_review)")
        if verdict == "changes_requested":
            sys.exit(1)
        return
    if current != "code_complete":
        print(
            f"ERROR: {tid} is '{current}', expected code_complete.\n"
            f"  If the ticket is already in_review and needs a verdict, pass --verdict: "
            f"lanegate review {tid} --verdict approved",
            file=sys.stderr,
        )
        sys.exit(1)

    if verdict is not None:
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


def cmd_merge(ticket_id: str, cfg: dict, repo_root: Path) -> None:
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

    # Defense in depth (TICK-283): re-check the verification records already
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

    # Run pre_merge safeguards before the git merge.
    # Resolve the worktree for the guard commands; fall back to repo_root when absent.
    wt_for_guards = Path(ticket["worktree"]) if ticket.get("worktree") else repo_root
    merge_timed_out_guards: list[str] = []
    try:
        with safeguard_lock(repo_root, tid):
            safeguards_passed, safeguard_reason = run_safeguards(
                "pre_merge", ticket, cfg, wt_for_guards, timed_out_guards=merge_timed_out_guards
            )
    except SafeguardLockHeld as exc:
        sys.exit(f"ERROR: {exc}")

    if not safeguards_passed:
        reason = f"pre_merge safeguards failed: {safeguard_reason}"
        print(
            f"WARNING: {tid} pre_merge safeguards failed — routing to needs_review.",
            file=sys.stderr,
        )
        _mark_needs_review(ticket, cfg, repo_root, reason=reason)
        return

    def _do_git_merge():
        # Serialize all git operations on repo_root to prevent concurrent merge/commit
        # interference when max_parallel > 1 (F3 fix: worker-pool unserialized git ops).
        # Reconcile any uncommitted local write to the ticket's own file left on
        # main by an earlier lifecycle transition before attempting the merge. Otherwise
        # git's own safety check refuses the merge outright the moment the
        # incoming branch touches this file too, even when the two changes
        # wouldn't actually conflict textually (TICK-122).
        if _has_uncommitted_diff(repo_root, ticket["_path"]):
            subprocess.run(
                [
                    "git",
                    "commit",
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
        if branch:
            _pre_merge_head = subprocess.run(
                ["git", "rev-parse", "HEAD"], capture_output=True, text=True, encoding="utf-8", cwd=repo_root
            ).stdout.strip()

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
                # silently empty on a real conflict (TICK-123 symptom A).
                detail = "\n".join(s for s in (r.stdout.strip(), r.stderr.strip()) if s)
                msg = f"ERROR merging {branch} → main:\n{detail}"
                print(msg, file=sys.stderr)
                raise MergeFailedError(msg)
            if r.stdout.strip():
                print(r.stdout.strip())

        return _merged_into_branch, _pre_merge_head

    if _GIT_OPS_LOCK is not None:
        with _GIT_OPS_LOCK:
            _merged_into_branch, _pre_merge_head = _do_git_merge()
    else:
        _merged_into_branch, _pre_merge_head = _do_git_merge()

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
    # it back as-is would silently clobber whatever the merge just wrote
    # (TICK-123 symptom B).
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
        from lanegate.context_log import _get_default_db_path, log_agent_run

        log_agent_run(
            log_path=None,
            ticket_id=tid,
            subagent_tokens=None,
            tool_uses=0,
            duration_ms=0,
            touched_files=_touched,
            repo_root=repo_root,
            executor=ticket.get("executor") or cfg.get("executor", "claude"),
            wall_time_ms=_wall_ms,
            tests_passed=None,
            db_path=_get_default_db_path(),
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
