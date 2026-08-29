"""Merge, post-merge validation, and completion lifecycle commands."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from lanegate import APP_NAME
from lanegate.companion import (
    CompanionMergeResult,
    companion_branch_merge,
    companion_worktree_cleanup,
)
from lanegate.config import resolve_trunk_branch
from lanegate.concurrency import SafeguardLockHeld, safeguard_lock
from lanegate.promote import _auto_promote_environments
from lanegate.safeguards import effective_safeguards, run_safeguards
from lanegate.ticket import append_lifecycle_event, branch_name, canonical_id, load_all_tickets, parse_ticket, write_ticket
from lanegate.worktree import remove_worktree

from . import (
    MergeFailedError,
    _GIT_OPS_LOCK,
    _advance,
    _append_ticket_section,
    _clear_human_review_approval,
    _commit_generated_ticket_write,
    _control_repo_root,
    _get_branch_wall_time_ms,
    _get_touched_files,
    _has_uncommitted_diff,
    _mark_needs_review,
    _remove_recovery_file,
    _stamp_status_changed,
    _track_direct_action,
)

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
                elif r.stdout.strip():
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

    if branch and cfg.get("cleanup_branch_on_merge", True):
        # `branch` is always a lanegate-generated ref name (e.g. "tick-704"), never
        # attacker-controlled, and `git branch -D`/`git push --delete` take a ref
        # name, not a pathspec -- git's pathspec-magic (":(literal)"/":(glob)") only
        # applies to file pathspecs (git add/diff/checkout -- <path>), never to ref
        # names. The `--` separator already fully guards against option injection
        # (e.g. a branch name starting with "-"), so no further escaping is needed.
        branch_delete = subprocess.run(
            ["git", "branch", "-D", "--", branch],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if branch_delete.returncode != 0 and "not found" not in branch_delete.stderr.lower():
            print(
                f"WARNING: failed to delete local branch {branch!r} after merge: "
                f"{branch_delete.stderr.strip()}",
                file=sys.stderr,
            )
        pushed_by_lanegate = bool(
            cfg.get("github_pr", False) or cfg.get("push_ticket_branches", False)
        )
        if pushed_by_lanegate:
            remote_delete = subprocess.run(
                ["git", "push", "origin", "--delete", "--", branch],
                cwd=repo_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            stderr_lower = remote_delete.stderr.lower()
            already_gone = "remote ref does not exist" in stderr_lower or "not found" in stderr_lower
            if remote_delete.returncode != 0 and not already_gone:
                print(
                    f"WARNING: failed to delete remote branch {branch!r} after merge: "
                    f"{remote_delete.stderr.strip()}",
                    file=sys.stderr,
                )

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

