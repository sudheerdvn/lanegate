"""Operational lifecycle commands: start, complete, failure, reopen, and conflict resolution."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lanegate import APP_NAME
from lanegate.companion import companion_branch_create, companion_worktree_cleanup
from lanegate.config import resolve_trunk_branch
from lanegate.concurrency import (
    SafeguardLockHeld,
    check_local_not_behind_remote,
    claim_lock,
    locked_touches,
    metadata_commit_lock,
    safeguard_lock,
    touches_overlap,
)
from lanegate.git import has_tracking_remote
from lanegate.safeguards import run_safeguards
from lanegate.ticket import (
    append_lifecycle_event,
    append_status_history,
    branch_name,
    canonical_id,
    classify_needs_review_cause,
    load_all_tickets,
    parse_ticket,
    write_ticket,
)
from lanegate.worktree import create_worktree, remove_worktree, worktree_path

from .hibernate import cmd_hibernate
from .state import _mark_needs_review

from . import (
    _advance,
    _append_ticket_section,
    _check_touches_drift,
    _commit_generated_ticket_write,
    _commit_status,
    _control_repo_root,
    _current_reviewed_at,
    _enforce_verification_gate,
    _get_changed_files,
    _has_committed_changes,
    _is_git_worktree,
    _marker_base,
    _push_status_commit,
    _remove_executor_markers,
    _remove_recovery_file,
    _stamp_status_changed,
    _track_direct_action,
    _write_executor_marker,
    check_touches_compliance,
    resolve_human_escalation,
    resolve_reviewer,
)

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
    # Clear the review-pending hibernation marker too: otherwise the next
    # `lanegate run` pass sees it still set and immediately re-hibernates the
    # ticket for the same "orphaned prior session" reason it was just cleared
    # of, with nothing actually changed since the unblock.
    ticket.pop("review_pending", None)
    ticket.pop("review_pending_reason", None)
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


    wt_path = Path(ticket["worktree"]) if ticket.get("worktree") else None
    if wt_path and wt_path.exists() and (wt_path / ".git").exists():
        from lanegate.lifecycle import checkpoint_dirty_worktree
        try:
            if checkpoint_dirty_worktree(repo_root, wt_path, msg="wip: uncommitted edits preserved before reopen"):
                print(f"WARNING: {tid} had uncommitted edits. They have been preserved in a WIP commit.", file=sys.stderr)
        except RuntimeError as e:
            print(f"ERROR: {tid}: failed to checkpoint worktree: {e}", file=sys.stderr)
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
    ticket.pop("review_pending", None)
    ticket.pop("review_pending_reason", None)

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

