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
    cmd_reset as cmd_reset,
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


def checkpoint_dirty_worktree(repo_root: Path, wt_path: Path, msg: str = "wip: uncommitted edits preserved") -> bool:
    """Stages and commits any uncommitted edits in the worktree. Raises RuntimeError on failure."""
    status_out = subprocess.run(["git", "status", "--porcelain"], cwd=wt_path, capture_output=True, text=True)
    if status_out.returncode != 0:
        raise RuntimeError(f"git status failed: {status_out.stderr}")
    if not status_out.stdout.strip():
        return False
    add_out = subprocess.run(["git", "add", "-A"], cwd=wt_path, capture_output=True, text=True)
    if add_out.returncode != 0:
        raise RuntimeError(f"git add failed: {add_out.stderr}")
    commit_out = subprocess.run(["git", "commit", "-s", "-m", msg], cwd=wt_path, capture_output=True, text=True)
    if commit_out.returncode != 0:
        raise RuntimeError(f"git commit failed: {commit_out.stderr}")
    return True


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
    if current in TERMINAL_STATUSES and current != "failed":
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


# Shared lifecycle state transitions live in lifecycle/state.py.
from .state import (  # noqa: E402,F401
    _clear_human_review_approval,
    _mark_needs_review,
)

# Operational lifecycle commands live in lifecycle/core_cmds.py.
from .core_cmds import (  # noqa: E402,F401
    _worktree_has_commits,
    cmd_complete,
    cmd_fail,
    cmd_reopen,
    cmd_resolve_conflict,
    cmd_start,
)

# Review commands live in lifecycle/review_cmds.py. Keep package-root aliases
# for existing CLI, orchestrate, and test imports.
from .review_cmds import (  # noqa: E402,F401
    _append_review_findings,
    cmd_human_review_approve,
    cmd_needs_review,
    cmd_review,
    record_auto_fix_attempt,
)

# Merge commands live in lifecycle/merge.py. Keep package-root aliases
# for existing CLI, orchestrate, and test imports.
from .merge import (  # noqa: E402,F401
    cmd_done,
    cmd_merge,
    cmd_validate,
)


# Recovery commands live in lifecycle/recover.py. Keep these compatibility
# aliases at the package root for existing CLI, orchestrate, and test imports.
from .recover import (  # noqa: E402,F401
    cmd_recover_rate_limited_reviews,
    cmd_recover_rejected,
    mark_review_pending,
    rejected_auto_fix_recovery_reason,
    resume_review_pending,
)
