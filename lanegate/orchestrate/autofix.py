"""Auto-fix and drift-check subagents plus combined-mode helpers.

TICK-255/TICK-278: extracted from orchestrate/__init__.py as pure code
movement -- see docs/internal/module-split-proposal.md.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from lanegate.config import (
    _VALID_EXECUTOR_TYPES,
    resolve_human_escalation,
    resolve_model,
    resolve_trunk_branch,
    validate_model_for_executor,
)
from lanegate.budget import DispatchMeter, metering_supported_for
from lanegate.executor import (
    build_executor_cmd,
    executor_types_with,
    get_executor_config,
    parse_structured_result,
    reject_ollama_for_code_step,
    resolve_executor_env,
)
from lanegate.ticket import latest_review_findings, load_all_tickets, parse_ticket, write_ticket

from .audit import _write_review_verdict
from .pool import (
    _build_env,
    _cfg_with_driver_command_overrides,
    _get_step_budget_cap,
    _resolve_drift_driver_name,
    _resolve_driver_route,
    _ticket_for_model_resolution,
    _unpack_stream_result,
    capture_review_step_run,
    commit_worktree_changes,
    expand_driver,
    invoke_executor,
    make_event_line_handler,
    write_prompt_file_best_effort,
)
from .review import _git_head_sha, resolve_independent_review_driver, run_review_agent
from .run_report import (
    _resolve_active_run_session_ts,
    _stream_subprocess,
    begin_direct_action,
    record_direct_action_event,
)


class RateLimitedFixError(Exception):
    """Raised by run_fix_agent when the fix executor exits due to a rate limit,
    crash, or interrupt — i.e. the agent never meaningfully attempted the fix.
    The auto-fix loop should hibernate the ticket without consuming an attempt
    counter slot, matching how the implement/review phases handle rate limits.
    """


class FixFailedError(Exception):
    """Raised by run_fix_agent when the executor ran and explicitly failed
    (exited 0 but produced no commit, or any non-rate-limit non-zero exit).
    The auto-fix loop should consume an attempt slot and escalate if the cap
    is reached.
    """


def resolve_independent_fix_driver(
    ticket: dict,
    cfg: dict,
    repo_root: Path,
    *,
    pool_name: str | None = None,
) -> tuple[str | None, str]:
    """Resolve the fix driver, excluding the reviewer where possible.

    Mirrors ``resolve_independent_review_driver``'s ladder (review.py) for
    the fix step, minus its ``same_model`` fallback rung -- there is no
    config equivalent of ``review_fallback`` for fix, so genuine
    non-independence always means "refuse to dispatch", never "proceed
    anyway on the same model".

      1. ``independent``     -- a different pool instance was selected.
      2. ``different-model`` -- the reviewer is the only pool candidate, but
         a different model resolves for the fix step than the one actually
         used for review.
      3. ``needs_review``    -- independence could not be established; the
         caller must refuse to dispatch.

    When the ticket has no recorded ``review_driver`` at all (e.g. called
    outside the normal review-then-fix cycle), there is nothing to exclude
    and whatever ``resolve_pool_executor`` resolves is used as-is.
    """
    from lanegate.orchestrate.loop import resolve_pool_executor

    review_driver = ticket.get("review_driver")
    excluded = {review_driver} if review_driver else set()
    fix_executor = resolve_pool_executor(
        "fix", ticket, cfg, repo_root, excluded=excluded, pool_name=pool_name
    )
    if review_driver is None:
        return fix_executor, "independent"
    if fix_executor is not None and fix_executor not in excluded:
        return fix_executor, "independent"

    # No distinct pool instance is available -- the reviewer is the only
    # candidate left. See whether a different *model* resolves for the fix
    # step than the one actually recorded for review (e.g. a per-ticket
    # `model:` override, or per-step `models:` config on a single-executor
    # project -- the TICK-004 shape this ladder exists for).
    #
    # This must resolve fix_model exactly as invoke_executor's resolve_dispatch
    # will when it actually dispatches with executor_override=review_driver: a
    # `drivers:` entry can carry its own `model:` override that forces that
    # model for every step regardless of the base cfg's own per-step model, so
    # evaluating against plain `cfg` here would compare against a model that
    # is never actually used and let a same-model self-fix through.
    driver_cfg = expand_driver(review_driver, cfg)
    fix_driver_type = driver_cfg.get("type", review_driver)
    fix_executor_cfg = get_executor_config(fix_driver_type, cfg)
    fix_executor_type = fix_executor_cfg.get("type", fix_driver_type)
    effective_cfg = (
        dict(cfg, executor=fix_driver_type)
        if fix_driver_type != cfg.get("executor")
        else cfg
    )
    model_ticket = _ticket_for_model_resolution(ticket, fix_executor_type)
    review_model = ticket.get("review_model")
    fix_model = driver_cfg.get("model") or resolve_model(
        effective_cfg, "fix", ticket=model_ticket
    )
    if review_model and fix_model and review_model != fix_model:
        return review_driver, "different-model"

    return None, "needs_review"


def run_fix_agent(
    ticket: dict,
    cfg: dict,
    repo_root: Path,
    worktree_path: Path,
    findings: str,
    pre_fix_sha: str,
    pool_name: str | None = None,
) -> bool:
    """
    Run a fix subagent that addresses review findings on top of the ticket's
    existing diff, then commit the result.

    Args:
        pre_fix_sha: The worktree's HEAD commit sha before this call, used to
            detect whether the fix pass actually produced a new commit —
            ``check_worktree_has_commits`` (main-relative) would trivially
            return True here since the branch already has the original
            implementation's commits, so it can't be reused for this check.

    Returns True if the executor exited 0 and produced at least one new commit
    on top of pre_fix_sha.

    Raises:
        RateLimitedFixError: The executor exited non-zero due to a rate limit
            or interrupt (SIGINT/Ctrl-C). The agent never meaningfully attempted
            the fix; the attempt counter should NOT be incremented and the ticket
            should be hibernated for later retry.
        FixFailedError: The executor ran but failed (exited non-zero for a
            non-rate-limit reason, or exited 0 but produced no commit). The
            attempt counter should be incremented and the ticket escalated if
            the cap is reached.
    """
    from lanegate.reviewer import ReviewError, build_fix_prompt, get_worktree_diff
    from lanegate.ticket import branch_name

    tid = ticket["id"]
    branch = branch_name(tid)

    try:
        diff = get_worktree_diff(
            worktree_path, branch, base=resolve_trunk_branch(cfg, repo_root)
        )
    except ReviewError as exc:
        print(f"WARNING: fix agent could not read diff for {tid}: {exc}", file=sys.stderr)
        raise FixFailedError(f"fix agent could not read diff for {tid}: {exc}") from exc

    fix_prompt = build_fix_prompt(
        ticket,
        diff=diff,
        findings=findings,
        project_root=repo_root,
        worktree_path=worktree_path,
        cfg=cfg,
    )

    # The reviewer that recorded the findings being fixed must never also be
    # the one fixing them with no independent check one step earlier in the
    # cycle -- unless a different *model* resolves for the fix step than the
    # one actually used for review, which is independence too (mirrors
    # resolve_independent_review_driver's "different-model" rung).
    fix_executor, fix_independence = resolve_independent_fix_driver(
        ticket, cfg, repo_root, pool_name=pool_name
    )
    if fix_independence == "needs_review":
        print(
            f"WARNING: no independent fix executor is available for {tid}; "
            "refusing to dispatch the reviewer to fix its own findings",
            file=sys.stderr,
        )
        raise FixFailedError(f"no independent fix executor available for {tid}")
    if fix_independence == "different-model":
        print(
            f"[autofix] {tid}: {fix_executor!r} implemented the review this ticket is "
            "fixing findings from, but a different model resolves for the fix step -- "
            "proceeding on that model instead of refusing dispatch.",
            file=sys.stderr,
        )

    exit_code, captured_stdout, captured_stderr = invoke_executor(
        ticket,
        cfg,
        worktree_path,
        prompt_override=fix_prompt,
        step="fix",
        repo_root=repo_root,
        executor_override=fix_executor,
    )
    if exit_code != 0:
        # Distinguish rate-limit / interrupt exits (agent never tried) from
        # genuine failures (agent tried and produced an error).  Deferred
        # import avoids the circular-import issue (loop.py already imports
        # run_auto_fix_cycle from this module at module level).
        from lanegate.orchestrate.loop import _is_interrupted_exit, _is_rate_limit

        if _is_rate_limit(
            exit_code,
            worktree_path,
            captured_stdout=captured_stdout,
            captured_stderr=captured_stderr,
        ) or _is_interrupted_exit(exit_code):
            raise RateLimitedFixError(
                f"fix agent exited {exit_code} for {tid} (rate limit or interrupt)"
            )
        print(f"WARNING: fix agent exited {exit_code} for {tid}", file=sys.stderr)
        raise FixFailedError(f"fix agent exited {exit_code} for {tid}")

    _, _ = commit_worktree_changes(
        worktree_path, tid, message=f"fix: address review findings for {tid}", ticket=ticket
    )

    head_after = _git_head_sha(worktree_path)
    if head_after is None or head_after == pre_fix_sha:
        print(f"WARNING: fix agent for {tid} exited 0 but made no new commit", file=sys.stderr)
        raise FixFailedError(f"fix agent for {tid} exited 0 but made no new commit")
    return True


def run_rebase_fix_agent(
    ticket: dict,
    cfg: dict,
    repo_root: Path,
    worktree_path: Path,
    rebase_detail: str,
    pool_name: str | None = None,
) -> bool:
    """Run an autofix agent in worktree_path to resolve git rebase content conflicts,
    handling metadata-only conflicts deterministically without LLM, invoking fix agents
    for code conflicts, and continuing until complete or failing closed.

    Returns True if conflict resolution succeeded and rebase was continued cleanly,
    False otherwise.
    """
    from lanegate.orchestrate.loop import (
        _abort_rebase,
        _conflicted_files,
        _continue_rebase,
        _format_conflict_detail,
        resolve_pool_executor,
    )
    from lanegate.reconciliation import is_metadata_only_conflict, resolve_metadata_conflict
    from lanegate.orchestrate.run_report import begin_direct_action, record_direct_action_event

    tid = ticket["id"]
    tickets_dir = cfg.get("tickets_dir", ".lanegate/tickets")
    max_steps = cfg.get("max_rebase_steps", 10)
    seen_snapshots: set[tuple[tuple[str, str], ...]] = set()
    agent_resolved_conflict_files: set[str] = set()
    recovery_action = begin_direct_action(repo_root, "rebase-recovery", ticket_id=tid)
    recovery_action_id = recovery_action["action_id"]

    def finish_recovery(success: bool) -> bool:
        """Close the recovery stream so it is never rendered as perpetually live."""
        record_direct_action_event(
            repo_root,
            recovery_action_id,
            "action_end",
            ticket_id=tid,
            action_type="rebase-recovery",
            status="success" if success else "failed",
        )
        return success

    def record_human_merge_hold() -> None:
        """Persist the extra merge gate required after agent conflict recovery.

        Git can verify that a rebase completed and the normal review can verify
        the resulting diff, but an agent had to make a semantic choice for at
        least one conflicting hunk.  That is sufficient reason to prevent an
        unattended merge even when the ticket otherwise uses ``full``
        autonomy.  Keep the compact evidence on the ticket so the hold survives
        a later orchestrator run.
        """
        if not agent_resolved_conflict_files:
            return

        ticket["requires_human_merge"] = True
        ticket["human_merge_reason"] = "automated rebase conflict recovery"
        ticket["rebase_conflict_files"] = sorted(agent_resolved_conflict_files)
        if not ticket.get("_path"):
            return

        write_ticket(ticket)
        # Import only at the point of persistence: lifecycle commands import
        # this module for conflict recovery, so a module-level import would
        # create a cycle.
        from lanegate.lifecycle import _commit_generated_ticket_write

        _commit_generated_ticket_write(
            repo_root,
            Path(ticket["_path"]),
            tid,
            "rebase-conflict-recovery-hold",
            cfg,
            required=False,
        )

    def _get_staged_marker_info(rel_path: str) -> tuple[dict[str, int], list[dict]]:
        """Collect max occurrences and surrounding non-marker contexts for lines starting
        with conflict marker prefixes from git index stages 1, 2, 3."""
        from collections import Counter
        import subprocess

        staged_counts: dict[str, int] = {}
        staged_occurrences: list[dict] = []

        def _is_marker(line: str) -> bool:
            s = line.lstrip()
            return s.startswith(("<<<<<<<", ">>>>>>>")) or s.rstrip() == "======="

        for stage in (1, 2, 3):
            try:
                res = subprocess.run(
                    ["git", "show", f":{stage}:{rel_path}"],
                    cwd=worktree_path,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                if res.returncode == 0:
                    lines = res.stdout.splitlines()
                    stage_counts = Counter(
                        line.lstrip()
                        for line in lines
                        if _is_marker(line)
                    )
                    for marker, count in stage_counts.items():
                        staged_counts[marker] = max(staged_counts.get(marker, 0), count)

                    for idx, line in enumerate(lines):
                        marker = line.lstrip()
                        if not _is_marker(line):
                            continue

                        prev_line = lines[idx - 1].rstrip() if idx > 0 else None
                        next_line = lines[idx + 1].rstrip() if idx < len(lines) - 1 else None

                        p = idx - 1
                        prev_nm = None
                        while p >= 0:
                            if not _is_marker(lines[p]):
                                prev_nm = lines[p].rstrip()
                                break
                            p -= 1

                        n = idx + 1
                        next_nm = None
                        while n < len(lines):
                            if not _is_marker(lines[n]):
                                next_nm = lines[n].rstrip()
                                break
                            n += 1

                        is_top = (idx == 0) or all(
                            _is_marker(lines[k])
                            for k in range(idx)
                        )
                        is_bottom = (idx == len(lines) - 1) or all(
                            _is_marker(lines[k])
                            for k in range(idx + 1, len(lines))
                        )

                        staged_occurrences.append({
                            "stage": stage,
                            "marker": marker,
                            "prev_line": prev_line,
                            "next_line": next_line,
                            "prev_nm": prev_nm,
                            "next_nm": next_nm,
                            "is_top": is_top,
                            "is_bottom": is_bottom,
                        })
            except Exception:
                pass
        return staged_counts, staged_occurrences

    def abort_if_markers_remain(paths: list[str]) -> bool:
        """Fail closed before staging text that still contains a conflict hunk.

        Git considers a file resolved once it is staged; it does not reject
        literal ``<<<<<<<`` / ``=======`` / ``>>>>>>>`` text.  The executor is
        deliberately asked not to stage files, so this is the last trustworthy
        point to reject an incomplete agent resolution. Pre-existing source
        lines beginning with ``<<<<<<<``, ``=======``, or ``>>>>>>>`` (e.g.,
        RST underlines or test fixtures) present in stage 1, 2, or 3 of the git
        index are valid source text. Residual markers introduced during
        conflict resolution that were not present in any staged version (or
        present in excess of staged multiplicity, or whose position does not
        preserve a staged line's surrounding context) are hunk markers and
        must never be staged from an agent-resolved source file.
        """
        from collections import Counter

        def _is_marker(line: str) -> bool:
            s = line.lstrip()
            return s.startswith(("<<<<<<<", ">>>>>>>")) or s.rstrip() == "======="

        marker_files: list[str] = []
        for rel_path in paths:
            path = worktree_path / rel_path
            if not path.exists():
                continue
            staged_counts, staged_occurrences = _get_staged_marker_info(rel_path)
            text = path.read_text(encoding="utf-8", errors="replace")
            wt_lines = text.splitlines()

            worktree_counts = Counter(
                line.lstrip()
                for line in wt_lines
                if _is_marker(line)
            )

            has_residual = False
            for marker, count in worktree_counts.items():
                if count > staged_counts.get(marker, 0):
                    has_residual = True
                    break

            if has_residual:
                marker_files.append(rel_path)
                continue

            for st in staged_occurrences:
                m = st["marker"]
                st2_prevs = {
                    occ["prev_line"]
                    for occ in staged_occurrences
                    if occ["stage"] == 2 and occ["marker"] == m and occ["prev_line"] is not None
                } | {
                    occ["prev_nm"]
                    for occ in staged_occurrences
                    if occ["stage"] == 2 and occ["marker"] == m and occ["prev_nm"] is not None
                }
                st3_prevs = {
                    occ["prev_line"]
                    for occ in staged_occurrences
                    if occ["stage"] == 3 and occ["marker"] == m and occ["prev_line"] is not None
                } | {
                    occ["prev_nm"]
                    for occ in staged_occurrences
                    if occ["stage"] == 3 and occ["marker"] == m and occ["prev_nm"] is not None
                }

                st2_nexts = {
                    occ["next_line"]
                    for occ in staged_occurrences
                    if occ["stage"] == 2 and occ["marker"] == m and occ["next_line"] is not None
                } | {
                    occ["next_nm"]
                    for occ in staged_occurrences
                    if occ["stage"] == 2 and occ["marker"] == m and occ["next_nm"] is not None
                }
                st3_nexts = {
                    occ["next_line"]
                    for occ in staged_occurrences
                    if occ["stage"] == 3 and occ["marker"] == m and occ["next_line"] is not None
                } | {
                    occ["next_nm"]
                    for occ in staged_occurrences
                    if occ["stage"] == 3 and occ["marker"] == m and occ["next_nm"] is not None
                }

                if st2_prevs and st3_prevs:
                    st["prev_stable"] = bool(st2_prevs & st3_prevs)
                else:
                    st["prev_stable"] = True

                if st2_nexts and st3_nexts:
                    st["next_stable"] = bool(st2_nexts & st3_nexts)
                else:
                    st["next_stable"] = True

            used_staged_indices: set[int] = set()
            for idx, line in enumerate(wt_lines):
                marker = line.lstrip()
                if not _is_marker(line):
                    continue

                prev_line = wt_lines[idx - 1].rstrip() if idx > 0 else None
                next_line = wt_lines[idx + 1].rstrip() if idx < len(wt_lines) - 1 else None

                p = idx - 1
                prev_nm = None
                while p >= 0:
                    if not _is_marker(wt_lines[p]):
                        prev_nm = wt_lines[p].rstrip()
                        break
                    p -= 1

                n = idx + 1
                next_nm = None
                while n < len(wt_lines):
                    if not _is_marker(wt_lines[n]):
                        next_nm = wt_lines[n].rstrip()
                        break
                    n += 1

                is_top = (idx == 0) or all(
                    _is_marker(wt_lines[k])
                    for k in range(idx)
                )
                is_bottom = (idx == len(wt_lines) - 1) or all(
                    _is_marker(wt_lines[k])
                    for k in range(idx + 1, len(wt_lines))
                )

                wt_info = {
                    "marker": marker,
                    "prev_line": prev_line,
                    "next_line": next_line,
                    "prev_nm": prev_nm,
                    "next_nm": next_nm,
                    "is_top": is_top,
                    "is_bottom": is_bottom,
                }

                def _contexts_match(wt: dict, st: dict) -> bool:
                    if wt["marker"] != st["marker"]:
                        return False
                    if (
                        st["prev_nm"] is None
                        and st["next_nm"] is None
                        and wt["prev_nm"] is None
                        and wt["next_nm"] is None
                    ):
                        return True

                    if st["is_top"]:
                        prev_matched = wt["is_top"]
                    else:
                        prev_matched = (not wt["is_top"]) and (
                            (
                                wt["prev_line"] is not None
                                and wt["prev_line"] == st["prev_line"]
                                and st["prev_line"].strip() != ""
                            )
                            or (
                                wt["prev_nm"] is not None
                                and wt["prev_nm"] == st["prev_nm"]
                                and st["prev_nm"].strip() != ""
                            )
                        )

                    if st["is_bottom"]:
                        next_matched = wt["is_bottom"]
                    else:
                        next_matched = (not wt["is_bottom"]) and (
                            (
                                wt["next_line"] is not None
                                and wt["next_line"] == st["next_line"]
                                and st["next_line"].strip() != ""
                            )
                            or (
                                wt["next_nm"] is not None
                                and wt["next_nm"] == st["next_nm"]
                                and st["next_nm"].strip() != ""
                            )
                        )

                    prev_must_match = st.get("prev_stable", True)
                    next_must_match = st.get("next_stable", True)

                    if st["is_top"] and st["is_bottom"]:
                        return wt["is_top"] and wt["is_bottom"]
                    elif st["is_top"]:
                        return wt["is_top"] and (next_matched if next_must_match else True)
                    elif st["is_bottom"]:
                        return wt["is_bottom"] and (prev_matched if prev_must_match else True)
                    else:
                        if not prev_must_match and not next_must_match:
                            # Both neighboring lines are unstable across stages —
                            # no context anchor exists to confirm identity.  Fail
                            # closed: an unanchored marker cannot be matched.
                            return False
                        req_prev = prev_matched if prev_must_match else True
                        req_next = next_matched if next_must_match else True
                        return req_prev and req_next

                matched = False
                for st_idx, st_occ in enumerate(staged_occurrences):
                    if st_idx in used_staged_indices:
                        continue
                    if _contexts_match(wt_info, st_occ):
                        used_staged_indices.add(st_idx)
                        matched = True
                        break

                if not matched:
                    has_residual = True
                    break

            if has_residual:
                marker_files.append(rel_path)

        if not marker_files:
            return False

        print(
            f"WARNING: rebase conflict markers remain for {tid}: {', '.join(marker_files)}",
            file=sys.stderr,
        )
        record_direct_action_event(
            repo_root,
            recovery_action_id,
            "rebase_markers_remaining",
            ticket_id=tid,
            reason_code="conflict_markers_remaining",
            conflict_files=marker_files,
        )
        _abort_rebase(worktree_path)
        return True

    try:
        step = 0
        resolved_conflict_files: set[str] = set()
        while step < max_steps:
            step += 1
            conflict_files = _conflicted_files(worktree_path)
            if not conflict_files:
                continued, continue_detail = _continue_rebase(worktree_path, [])
                if continued:
                    record_human_merge_hold()
                    _, _ = commit_worktree_changes(
                        worktree_path,
                        tid,
                        message=f"fix: resolve rebase conflict markers for {tid}",
                        ticket=ticket,
                        paths=sorted(resolved_conflict_files),
                    )
                    return finish_recovery(True)
                _abort_rebase(worktree_path)
                return finish_recovery(False)

            snapshot_list = []
            for f in sorted(conflict_files):
                fp = worktree_path / f
                content = fp.read_text(encoding="utf-8", errors="replace") if fp.exists() else ""
                snapshot_list.append((f, content))
            snapshot = tuple(snapshot_list)

            if snapshot in seen_snapshots:
                print(f"WARNING: rebase conflict state unchanged for {tid}", file=sys.stderr)
                record_direct_action_event(
                    repo_root,
                    recovery_action_id,
                    "rebase_stuck",
                    ticket_id=tid,
                    reason_code="stuck_rebase",
                    conflict_files=conflict_files,
                )
                _abort_rebase(worktree_path)
                return finish_recovery(False)
            seen_snapshots.add(snapshot)
            resolved_conflict_files.update(conflict_files)

            metadata_conflict_files = [
                path for path in conflict_files
                if is_metadata_only_conflict([path], tickets_dir)
            ]
            source_conflict_files = [
                path for path in conflict_files
                if path not in metadata_conflict_files
            ]

            # Resolve ticket metadata before any agent sees a mixed batch. This
            # preserves the deterministic reconciliation policy for lifecycle
            # state and ensures the source-only marker scan cannot conceal a
            # staged ticket conflict marker.
            if metadata_conflict_files:
                try:
                    for p in metadata_conflict_files:
                        resolve_metadata_conflict(worktree_path, p)
                except Exception as exc:
                    print(
                        f"WARNING: failed to resolve metadata conflict for {tid}: {exc}",
                        file=sys.stderr,
                    )
                    record_direct_action_event(
                        repo_root,
                        recovery_action_id,
                        "metadata_conflict_error",
                        ticket_id=tid,
                        reason_code="metadata_conflict_error",
                        error=str(exc),
                        conflict_files=metadata_conflict_files,
                    )
                    _abort_rebase(worktree_path)
                    return finish_recovery(False)

                record_direct_action_event(
                    repo_root,
                    recovery_action_id,
                    "resolve_metadata_conflict",
                    ticket_id=tid,
                    reason_code="metadata_conflict_resolved",
                    conflict_files=metadata_conflict_files,
                )

            if not source_conflict_files:
                continued, continue_detail = _continue_rebase(worktree_path, conflict_files)
                if continued:
                    record_human_merge_hold()
                    _, _ = commit_worktree_changes(
                        worktree_path,
                        tid,
                        message=f"fix: resolve rebase conflict markers for {tid}",
                        ticket=ticket,
                        paths=sorted(resolved_conflict_files),
                    )
                    return finish_recovery(True)
                continue

            current_detail = _format_conflict_detail(worktree_path, source_conflict_files)

            rebase_fix_prompt = (
                f"You are resolving git rebase content conflicts for ticket {tid}.\n\n"
                f"{current_detail}\n\n"
                "Instructions:\n"
                "1. Inspect the conflict markers (`<<<<<<< HEAD`, `=======`, `>>>>>>>`) in the conflicted files.\n"
                "2. Edit the files to resolve the conflict markers cleanly, combining the changes correctly.\n"
                "3. You MUST completely remove all conflict markers (`<<<<<<< HEAD`, `=======`, `>>>>>>>`). They are not valid syntax.\n"
                "4. Run project tests to confirm the resolution passes tests and is syntactically valid.\n"
                "5. Do NOT run `git rebase --continue` or `git add` yourself; save the resolved files in place.\n\n"
                "Example resolution:\n"
                "Before:\n"
                "```python\n"
                "def greet():\n"
                "<<<<<<< HEAD\n"
                "    print('hello from main')\n"
                "=======\n"
                "    print('hello from branch')\n"
                ">>>>>>> branch-name\n"
                "```\n"
                "After (conflict markers completely removed):\n"
                "```python\n"
                "def greet():\n"
                "    print('hello from main')\n"
                "    print('hello from branch')\n"
                "```"
            )

            record_direct_action_event(
                repo_root,
                recovery_action_id,
                "rebase_code_conflict",
                ticket_id=tid,
                reason_code="code_conflict_detected",
                conflict_files=source_conflict_files,
                conflict_hunks=current_detail,
            )
            # Metadata conflicts are reconciled deterministically. Only source
            # conflicts resolved by an agent require the extra human merge gate.
            agent_resolved_conflict_files.update(source_conflict_files)

            fix_executor = resolve_pool_executor(
                "fix", ticket, cfg, repo_root, pool_name=pool_name
            )
            exit_code, *_ = invoke_executor(
                ticket,
                cfg,
                worktree_path,
                prompt_override=rebase_fix_prompt,
                step="fix",
                repo_root=repo_root,
                executor_override=fix_executor,
            )
            if exit_code != 0:
                print(f"WARNING: rebase fix agent exited {exit_code} for {tid}", file=sys.stderr)
                _abort_rebase(worktree_path)
                return finish_recovery(False)

            if abort_if_markers_remain(source_conflict_files):
                return finish_recovery(False)

            continued, continue_detail = _continue_rebase(worktree_path, source_conflict_files)
            if continued:
                record_human_merge_hold()
                _, _ = commit_worktree_changes(
                    worktree_path,
                    tid,
                    message=f"fix: resolve rebase conflict markers for {tid}",
                    ticket=ticket,
                    paths=sorted(resolved_conflict_files),
                )
                return finish_recovery(True)

        print(f"WARNING: rebase recovery exceeded max iterations for {tid}", file=sys.stderr)
        _abort_rebase(worktree_path)
        return finish_recovery(False)
    except Exception as exc:
        print(f"WARNING: rebase recovery exception for {tid}: {exc}", file=sys.stderr)
        _abort_rebase(worktree_path)
        return finish_recovery(False)


def run_drift_check(
    ticket: dict,
    cfg: dict,
    repo_root: Path,
    worktree_path: Path,
    findings: str,
    pre_fix_sha: str,
    pool_name: str | None = None,
):
    """
    Run a drift-check subagent that verifies a fix pass still matches ticket
    intent, independent of the fix agent itself.

    Args:
        pre_fix_sha: The worktree's HEAD commit sha before the fix pass ran —
            used as the base for isolating exactly what the fix changed.

    Returns a ``reviewer.DriftCheckResult``. Fail-closed: any exception,
    timeout, or parse failure returns ``ok=False`` rather than letting an
    unverifiable fix through.
    """
    from lanegate.reviewer import (
        ReviewError,
        build_drift_check_prompt,
        get_worktree_diff,
        parse_drift_check_result,
    )
    from lanegate.ticket import branch_name

    tid = ticket["id"]
    branch = branch_name(tid)

    try:
        original_diff = get_worktree_diff(
            worktree_path, branch, base=resolve_trunk_branch(cfg, repo_root)
        )
        fix_diff = get_worktree_diff(worktree_path, branch, base=pre_fix_sha)
    except ReviewError as exc:
        from lanegate.reviewer import DriftCheckResult

        return DriftCheckResult(ok=False, reason=f"drift check could not read diff: {exc}")

    prompt = build_drift_check_prompt(
        ticket,
        original_diff=original_diff,
        fix_diff=fix_diff,
        findings=findings,
        project_root=repo_root,
        worktree_path=worktree_path,
        cfg=cfg,
    )

    from lanegate.reviewer import DriftCheckResult

    prompt_path = write_prompt_file_best_effort(worktree_path, tid, "drift_check", prompt)
    session_ts = _resolve_active_run_session_ts(repo_root)
    bundle_path: Path | None = None

    def _record(result: DriftCheckResult) -> DriftCheckResult:
        """Persist the drift outcome to this run's bundle before returning it."""
        _write_review_verdict(
            bundle_path,
            {"verdict": "approved" if result.ok else "changes_requested",
             "drift_ok": result.ok,
             "notes": result.reason},
        )
        return result

    try:
        # Resolved inside the try so a bad named-executor config (api_key_env
        # pointing at an unset var, or a type with no known key-injection
        # target — TICK-088), or a malformed driver env overlay, is caught by
        # the same fail-closed handler below as any other drift-check error.
        drift_driver_name = _resolve_drift_driver_name(
            ticket, cfg, repo_root, pool_name=pool_name
        )
        drift_driver_cfg = expand_driver(drift_driver_name, cfg)
        drift_executor = drift_driver_cfg.get("type", drift_driver_name)
        # expand_driver() only expands `drivers:` entries, so a drift executor
        # configured as a named instance (`executors: {local-ollama: {type:
        # ollama}}`, TICK-088) is still an instance name here. Guard on the
        # resolved type or that config dispatches a raw ollama drift check.
        resolved_drift_executor_cfg = get_executor_config(drift_executor, cfg)
        resolved_drift_type = resolved_drift_executor_cfg.get(
            "type", drift_executor
        )
        reject_ollama_for_code_step("drift_check", resolved_drift_type)
        drift_effective_cfg = (
            dict(cfg, executor=drift_executor) if drift_executor != cfg.get("executor") else cfg
        )
        # A ticket's model pin applies to its implementation/fix lifecycle.
        # Drift is an independent review route, so it must resolve from that
        # route's configuration rather than inherit an implementation-only
        # ticket model (which might not even be accepted by this executor).
        drift_model = drift_driver_cfg.get("model") or resolve_model(
            drift_effective_cfg, "drift_check"
        )
        if drift_model is not None:
            validate_model_for_executor(
                drift_model,
                resolved_drift_type,
                "models.drift_check",
                provider=(
                    resolved_drift_executor_cfg.get("provider")
                    or drift_driver_cfg.get("provider")
                ),
            )
        drift_command_cfg = _cfg_with_driver_command_overrides(
            cfg, drift_executor, drift_driver_cfg
        )
        # TICK-310: drift_check may resume the fix pass it audits, but only
        # when both dispatches use the exact same executor and model. A
        # provider/model change must start cold: CLI session IDs are not
        # portable across providers or model variants.
        resume_candidate = ticket.get("fix_session_id")
        resume_session_id = None
        if resume_candidate:
            origin_executor = ticket.get("fix_session_executor")
            origin_model = ticket.get("fix_session_model")
            if origin_executor != drift_executor or origin_model != drift_model:
                reason = "session origin does not match selected executor/model"
            else:
                from lanegate.context_log import (
                    _get_default_db_path,
                    _get_project_id,
                    resume_session_gate,
                )

                allowed, reason = resume_session_gate(
                    cfg, _get_default_db_path(), _get_project_id(repo_root), resume_candidate
                )
                if allowed:
                    resume_session_id = resume_candidate
            if resume_session_id is None:
                print(
                    f"[orchestrate] {tid}: not resuming session for drift_check — {reason}",
                    file=sys.stderr,
                )
        stdin_capable = resolved_drift_type in executor_types_with("stdin_capable")
        # Agy's JSON mode is completion-only, so it needs a flat timeout
        # rather than output-idle detection.
        # Drift checks have no worker heartbeat monitor. Codex's JSON output
        # can be quiet while it works, so use the hard ceiling instead of an
        # output-idle kill for that executor type.
        streaming_capable = resolved_drift_type in executor_types_with("streaming_capable_without_heartbeat")
        step_max_turns = _get_step_budget_cap(cfg, "drift_check", "max_turns")
        step_max_tokens = _get_step_budget_cap(cfg, "drift_check", "max_cumulative_tokens")
        meter = (
            DispatchMeter(step="drift_check")
            if metering_supported_for(resolved_drift_type)
            else None
        )

        def check_budget() -> str | None:
            if meter is None:
                return None
            if step_max_turns is not None and meter.turns >= step_max_turns:
                return f"max_turns cap reached ({meter.turns}/{step_max_turns} turns)"
            if step_max_tokens is not None and meter.tokens >= step_max_tokens:
                return (
                    "max_cumulative_tokens cap reached "
                    f"({meter.tokens}/{step_max_tokens} tokens)"
                )
            return None

        drift_cmd = build_executor_cmd(
            drift_executor, prompt, drift_command_cfg, model=drift_model,
            analyze_session_id=resume_session_id,
            use_stdin=stdin_capable,
            max_turns=step_max_turns,
            step="drift_check",
        )
        drift_executor_env = resolve_executor_env(get_executor_config(drift_executor, cfg))
        drift_executor_env = _build_env(drift_driver_cfg, base_env=drift_executor_env)
        stream_kwargs = {
            "idle_timeout": cfg.get("executor_idle_timeout_seconds", 75),
            "absolute_ceiling": cfg.get("executor_absolute_ceiling_seconds", 1500),
            "budget_probe": check_budget,
        } if streaming_capable else {
            "timeout": cfg.get("executor_absolute_ceiling_seconds", 1500),
            "budget_probe": check_budget,
        }
        start_time = time.time()
        session_id = f"{tid}-{time.time_ns()}-{os.getpid()}-drift_check"
        print(
            f"[drift-check] {tid}: {drift_driver_name} ({drift_model}) verifying fix…",
            file=sys.stderr,
        )
        handle_line = make_event_line_handler(
            repo_root,
            session_ts,
            tid,
            executor=drift_driver_name,
            model=drift_model,
            step="drift_check",
            terminal_stream=sys.stderr,
            meter=meter,
            worktree_path=worktree_path,
        )
        rc, captured_stdout, captured_stderr, kill_reason = _unpack_stream_result(_stream_subprocess(
            drift_cmd,
            cwd=str(worktree_path),
            out_stream=io.StringIO(),
            env=drift_executor_env,
            stdin_text=prompt if stdin_capable else None,
            on_line=handle_line,
            **stream_kwargs,
        ))
        bundle_path = capture_review_step_run(
            repo_root,
            worktree_path,
            ticket,
            cfg,
            step="drift_check",
            executor=drift_executor,
            driver_name=drift_driver_name,
            model=drift_model,
            session_id=session_id,
            prompt_path=prompt_path,
            start_time=start_time,
            exit_code=rc,
            captured_stdout=captured_stdout,
            captured_stderr=captured_stderr,
        )
        if rc != 0:
            partial = captured_stdout.strip()
            suffix = f": {partial}" if kill_reason == "ceiling" and partial else ""
            print(
                f"WARNING: drift check exited {rc} for {tid} — treating as drift",
                file=sys.stderr,
            )
            return _record(
                DriftCheckResult(ok=False, reason=f"drift check subprocess exited {rc}{suffix}")
            )
        # drift_executor may be a named pool instance (e.g. "claude-a") rather
        # than a bare type -- expand_driver() does not resolve that down to
        # "claude" the way get_executor_config() does, so parse_structured_result
        # must be keyed on the resolved type or it never matches the registry
        # and drift-check silently falls back to parsing the raw JSON envelope
        # as plain text, which the fail-closed path below then reads as drift.
        parsed = parse_structured_result(resolved_drift_type, captured_stdout)
        output = parsed["result_text"].strip() if parsed is not None else captured_stdout.strip()
        if parsed is not None:
            from lanegate.context_log import record_step_cost

            record_step_cost(
                repo_root, tid, "drift_check", drift_executor, drift_model, parsed,
                dispatch_start_time=start_time,
            )
        matches = re.findall(r'\{[^{}]*"drift_ok"[^{}]*\}', output, re.DOTALL)
        raw_for_parse = matches[-1] if matches else output
        return _record(parse_drift_check_result(raw_for_parse))
    except Exception as exc:
        print(f"WARNING: drift check failed for {tid}: {exc} — treating as drift", file=sys.stderr)
        return _record(DriftCheckResult(ok=False, reason=f"drift check error: {exc}"))


def _extract_review_findings(ticket: dict) -> str:
    """Return the most recent review's findings from the ticket body.

    cmd_review appends findings there (see ``_append_review_findings``) — not
    to ticket.get("review_findings"), which nothing in lifecycle.py ever
    writes.  Since TICK-343 each review gets its own ``(attempt N)`` section,
    so the fix agent must be handed the newest one rather than whichever
    header happens to match first.
    """
    return latest_review_findings(ticket)


def backfill_combined_review_metadata(ticket: dict, dispatch: dict, repo_root: Path) -> None:
    """Attribute a combined-mode self-review to the executor that performed it.

    In combined mode the agent records its own verdict by shelling out to
    ``lanegate review --verdict``, which has no way to say who it was — there are
    no ``--review-driver``/``--review-model`` flags.  Fills driver, model,
    and sets review_independence="self".  Only fills gaps: an explicitly
    recorded driver (a real split-mode review) is never overwritten.
    """
    del repo_root  # ticket["_path"] already locates the file; kept for call-site symmetry
    if not ticket.get("review_verdict") or ticket.get("review_driver"):
        return
    ticket["review_driver"] = dispatch.get("resolved_executor")
    ticket["review_model"] = dispatch.get("resolved_model")
    ticket["review_independence"] = "self"
    write_ticket(ticket)


def run_auto_fix_cycle(
    ticket: dict,
    cfg: dict,
    repo_root: Path,
    worktree_path: Path,
    pool_name: str | None = None,
) -> bool | None:
    """
    Run up to the lower of ``max_auto_fix_attempts`` and
    ``human_escalation.retry_limit`` fix -> drift-check -> re-review cycles
    for a ticket whose review came back changes_requested.
    Always attempted regardless of ``autonomy`` — the caller decides what to
    do with a True result (merge unattended vs. wait for a human verdict);
    this function only runs the mechanical cycle.

    Returns True if a re-review within budget comes back approved (ticket
    ends at status=in_review, review_verdict=approved, exactly like a
    human-approved ticket — written by run_review_agent's own cmd_review
    call, no special-case write needed here). Returns False if the fix pass
    fails for a genuine reason (agent ran but failed to commit, drift-check
    fails, or attempt cap exceeded). Returns None if the fix agent was
    interrupted by a rate limit or signal before it could meaningfully attempt
    a fix — in this case the ticket is hibernated (status=hibernated) and the
    attempt counter is NOT incremented, so the next ``lanegate run`` will
    retry the fix from where it left off.
    In every False case the ticket is left at status=code_complete,
    review_verdict=changes_requested (unchanged, so cmd_blocked/cmd_merge's
    guard keep working) with one "## Auto-Fix Attempt N" body section per
    attempt and an updated review_summary describing the escalation reason.
    """
    from lanegate.lifecycle import record_auto_fix_attempt
    from lanegate.ticket import parse_ticket

    tid = ticket["id"]
    if ticket.get("status") != "code_complete":
        # Defensive backstop, not the primary guard: every current caller
        # already checks this before dispatching here (a ticket that's
        # already needs_review/hibernated/in_review has no review findings
        # to act on, and a generic failure here would overwrite whatever
        # specific reason moved it there). This exists so a future call
        # site can't reintroduce that overwrite bug by forgetting the check.
        return False
    tickets_dir = repo_root / cfg["tickets_dir"]
    mechanical_limit = int(cfg.get("max_auto_fix_attempts", 1))
    escalation_limit = int(resolve_human_escalation(cfg)["retry_limit"])
    # max_auto_fix_attempts is the ordinary mechanical budget.  The human
    # escalation retry_limit is a safety ceiling, so neither setting can
    # increase the number of unattended retry attempts beyond the other.
    max_attempts = min(mechanical_limit, escalation_limit)

    initial = parse_ticket(ticket["_path"]) if ticket.get("_path") else ticket
    if initial is None:
        initial = ticket
    verification_summary = str(initial.get("review_summary") or "").casefold()
    if (
        initial.get("verification_not_possible")
        or "verification was not actually possible" in verification_summary
    ):
        record_auto_fix_attempt(
            tid,
            cfg,
            repo_root,
            attempt=0,
            max_attempts=max_attempts,
            note=(
                "auto-fix declined: review verification was not actually possible — "
                "requires a human decision"
            ),
            escalate=True,
        )
        return False
    if not _extract_review_findings(initial).strip():
        record_auto_fix_attempt(
            tid,
            cfg,
            repo_root,
            attempt=0,
            max_attempts=max_attempts,
            note=(
                "auto-fix declined: review produced no findings to act on "
                "(likely a harness error, not a rejection) — not attempted"
            ),
            escalate=True,
        )
        return False

    for attempt in range(1, max_attempts + 1):
        current = parse_ticket(ticket["_path"]) if ticket.get("_path") else ticket
        if current is None:
            current = ticket
        findings = _extract_review_findings(current)

        pre_fix_sha = _git_head_sha(worktree_path)
        if pre_fix_sha is None:
            record_auto_fix_attempt(
                tid,
                cfg,
                repo_root,
                attempt=attempt,
                max_attempts=max_attempts,
                note=(
                    f"auto-fix escalated: could not read worktree HEAD "
                    f"(attempt {attempt}/{max_attempts})"
                ),
                escalate=True,
            )
            return False

        try:
            run_fix_agent(
                current, cfg, repo_root, worktree_path, findings, pre_fix_sha, pool_name=pool_name
            )
        except RateLimitedFixError as exc:
            # Rate limit / interrupt — the fix agent never meaningfully ran.
            # Hibernate the ticket WITHOUT incrementing the attempt counter so
            # the next ``lanegate run`` can retry from where it left off,
            # matching the behaviour of the implement/review hibernation path.
            from lanegate.lifecycle import cmd_hibernate

            reason = (
                f"rate limit or quota interruption during fix agent "
                f"(attempt {attempt}/{max_attempts}): {exc}"
            )
            print(
                f"[autofix] {tid}: rate limit hit during fix pass — hibernating. "
                f"Re-run: lanegate run",
                file=sys.stderr,
            )
            cmd_hibernate(tid, cfg, repo_root, reason=reason)
            return None
        except FixFailedError:
            record_auto_fix_attempt(
                tid,
                cfg,
                repo_root,
                attempt=attempt,
                max_attempts=max_attempts,
                note=f"auto-fix escalated: fix pass failed (attempt {attempt}/{max_attempts})",
                escalate=True,
            )
            return False

        if current.get("_path"):
            reloaded = parse_ticket(current["_path"])
            if reloaded:
                for k in ("fix_session_executor", "fix_session_model", "acceptance_contract_audit", "acceptance_contract_audit_summary", "close_criteria", "change_notes"):
                    if k in reloaded:
                        current[k] = reloaded[k]

        drift = run_drift_check(
            current, cfg, repo_root, worktree_path, findings, pre_fix_sha, pool_name=pool_name
        )
        if not drift.ok:
            record_auto_fix_attempt(
                tid,
                cfg,
                repo_root,
                attempt=attempt,
                max_attempts=max_attempts,
                note=(
                    f"auto-fix escalated: drift-check failed "
                    f"(attempt {attempt}/{max_attempts}): {drift.reason}"
                ),
                escalate=True,
                drift_ok=drift.ok,
                drift_reason=drift.reason,
            )
            return False

        # Enforce model independence: the review agent should evaluate the code
        # using a different model from the one that just wrote the fix.
        review_ticket = dict(current)
        if review_ticket.get("fix_session_executor"):
            review_ticket["implement_session_executor"] = review_ticket["fix_session_executor"]
        if review_ticket.get("fix_session_model"):
            review_ticket["implement_session_model"] = review_ticket["fix_session_model"]

        approved = run_review_agent(
            review_ticket, repo_root, worktree_path=worktree_path, cfg=cfg, pool_name=pool_name
        )

        if approved:
            record_auto_fix_attempt(
                tid,
                cfg,
                repo_root,
                attempt=attempt,
                max_attempts=max_attempts,
                note=(
                    f"fix pass + drift-check ok; re-review approved "
                    f"(attempt {attempt}/{max_attempts})"
                ),
                drift_ok=drift.ok,
                drift_reason=drift.reason,
            )
            return True

        if attempt == max_attempts:
            record_auto_fix_attempt(
                tid,
                cfg,
                repo_root,
                attempt=attempt,
                max_attempts=max_attempts,
                note=(
                    f"auto-fix attempts exhausted ({max_attempts}/{max_attempts}) — "
                    f"escalated for human review"
                ),
                escalate=True,
            )
            return False

        all_t, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
        post = next((t for t in all_t if t["id"] == tid), current)
        record_auto_fix_attempt(
            tid,
            cfg,
            repo_root,
            attempt=attempt,
            max_attempts=max_attempts,
            note=(
                f"fix pass + drift-check ok; re-review verdict: "
                f"{post.get('review_verdict')} (attempt {attempt}/{max_attempts}) — retrying"
            ),
        )

    return False


def cmd_fix(ticket_id: str, cfg: dict, repo_root: Path) -> None:
    """Run the fix -> drift-check -> re-review cycle for a ticket out of band.

    The only entry point besides the in-loop callers in orchestrate/loop.py —
    an out-of-band review (a human running ``lanegate review`` directly, or a
    review from a separate `lanegate run` process) has no other way to
    reach the auto-fix machinery, since it is otherwise reachable only from
    inside ``_drain_loop`` immediately after a dispatch in the same process.
    """
    from lanegate.ticket import canonical_id

    tid = canonical_id(ticket_id)
    tracking = begin_direct_action(repo_root, "fix", ticket_id=tid, executor="cli")
    print(f"Action {tracking['action_id']}: fix running (log: {tracking['log_path']})")
    try:
        fixed = _cmd_fix_tracked(tid, cfg, repo_root)
    except BaseException:
        record_direct_action_event(
            repo_root, tracking["action_id"], "action_end", action_type="fix", ticket_id=tid,
            status="failed",
        )
        raise
    status = "success" if fixed is True else ("rate_limited" if fixed is None else "escalated")
    record_direct_action_event(
        repo_root, tracking["action_id"], "action_end", action_type="fix", ticket_id=tid,
        status=status,
    )
    if fixed is True:
        print(f"Action {tracking['action_id']}: fix success")
    elif fixed is None:
        print(f"Action {tracking['action_id']}: fix rate_limited")
    else:
        print(f"Action {tracking['action_id']}: fix escalated")


def _cmd_fix_tracked(tid: str, cfg: dict, repo_root: Path) -> bool | None:
    """Existing fix flow, separated so its action envelope also records failures."""
    tickets_dir = repo_root / cfg["tickets_dir"]
    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    ticket = next((t for t in tickets if t["id"] == tid), None)
    if not ticket:
        print(f"ERROR: {tid} not found", file=sys.stderr)
        sys.exit(1)

    if ticket.get("status") != "code_complete" or ticket.get("review_verdict") != "changes_requested":
        print(
            f"ERROR: {tid} is not awaiting a fix — status={ticket.get('status')!r}, "
            f"review_verdict={ticket.get('review_verdict')!r} "
            "(expected status=code_complete, review_verdict=changes_requested)",
            file=sys.stderr,
        )
        sys.exit(1)

    wt = ticket.get("worktree")
    if not wt or not Path(wt).exists():
        print(f"ERROR: {tid} has no worktree on disk (worktree={wt!r})", file=sys.stderr)
        sys.exit(1)

    fixed = run_auto_fix_cycle(ticket, cfg, repo_root, Path(wt))
    if fixed is True:
        print(f"[fix] {tid}: auto-fix cycle reached approved — awaiting human merge approval")
    elif fixed is None:
        print(f"[fix] {tid}: auto-fix cycle rate-limited / hibernated — retryable via lanegate run", file=sys.stderr)
    else:
        print(f"[fix] {tid}: auto-fix cycle escalated — see ticket for details", file=sys.stderr)
    return fixed


def _has_explicit_review_route(cfg: dict, ticket: dict) -> bool:
    """Return True if a review driver/reviewer was explicitly configured.

    Checks per-ticket reviewer override, steps.review.driver, top-level reviewer,
    or legacy executor_steps.review.
    """
    if ticket and ticket.get("reviewer"):
        return True
    if ((cfg.get("steps") or {}).get("review") or {}).get("driver"):
        return True
    if cfg.get("reviewer"):
        return True
    if (cfg.get("executor_steps") or {}).get("review"):
        return True
    return False


# Executor types with their own shell/command-execution capability, able to
# self-drive `lanegate complete && lanegate review --verdict ...` from
# inside their own session the way combined mode's appended prompt
# instructions require (see _build_combined_prompt below). A pure
# code-editing tool like aider has no such capability: it edits and commits
# files, then exits -- it cannot act on instructions to shell out and run
# CLI commands. Dispatching combined mode to one produces a ticket that
# commits real, correct code and then permanently fails ("executor exited 0
# but ticket status did not advance"), identically on every retry, since
# nothing about a retry changes the executor's inability to comply.
# Confirmed live in a fresh-install smoke test (aider + explicit reviewer:
# aider pin). Conservative allowlist: an executor type not listed here falls
# through to the independence ladder / split-mode dispatch instead, which is
# always safe, rather than assuming an unverified type can self-drive.
_COMBINED_MODE_CAPABLE_TYPES = {"claude", "claude-subagent", "claude-process", "codex", "agy"}
assert _COMBINED_MODE_CAPABLE_TYPES <= _VALID_EXECUTOR_TYPES, (
    "_COMBINED_MODE_CAPABLE_TYPES must stay a subset of _VALID_EXECUTOR_TYPES -- "
    "a stale/renamed entry here would silently drop out of the allowlist instead "
    "of raising at import time."
)


def combined_mode_capable(driver_name: str, cfg: dict) -> bool:
    driver_cfg = expand_driver(driver_name, cfg)
    resolved_type = driver_cfg.get("type", driver_name)
    executor_type = get_executor_config(resolved_type, cfg).get("type", resolved_type)
    return executor_type in _COMBINED_MODE_CAPABLE_TYPES


def _is_combined_mode(
    cfg: dict,
    ticket: dict,
    repo_root: Path | None = None,
    *,
    implementer: str | None = None,
) -> bool:
    """Return True when review dispatch degrades to self-review (Rung 3 of independence ladder).

    When implement and review steps resolve to the same executor name and no explicit
    review route was configured, attempt the review independence ladder (TICK-345) first:
      1. 'independent': a different pool instance is available -> split mode (return False)
      2. 'different-model': a different model is available on the same instance -> split mode (return False)
      3. 'self': same instance and same model, no alternative -> combined mode (return True)

    An explicit same-executor pin (via reviewer:, steps.review.driver, or executor_steps.review)
    or callers without worktree context (repo_root is None) bypass the ladder and return True --
    but only when the implement executor is actually capable of combined mode at all
    (combined_mode_capable); otherwise this always returns False regardless of an explicit
    pin, so dispatch falls through to split-mode review instead of a route that can never
    complete.
    """
    route = _resolve_driver_route(cfg, ticket)
    if route["mode"] == "split":
        return False

    impl = implementer or route["implement"]
    if not combined_mode_capable(impl, cfg):
        return False

    if _has_explicit_review_route(cfg, ticket) or repo_root is None:
        return True

    _, independence = resolve_independent_review_driver(
        ticket, cfg, repo_root, implementer=impl
    )
    return independence == "self"


def _build_combined_prompt(ticket: dict, implement_prompt: str, trunk_branch: str) -> str:
    """Append the combined-mode review+completion instructions to the implement prompt.

    The agent is responsible for running ``lanegate complete`` and
    ``lanegate review --verdict`` after finishing its implementation, so the
    orchestrator can skip those as separate subprocess calls.
    """
    appendix = (
        "\n\nAfter your implementation is complete and committed:\n\n"
        f"1. Review your own diff (`git diff {trunk_branch}..HEAD`).\n"
        "2. If the implementation meets the close criteria, run:\n"
        f'       lanegate complete {ticket["id"]} && lanegate review {ticket["id"]} --verdict approved --summary "<one line>"\n'
        "3. If changes are needed, run:\n"
        f'       lanegate complete {ticket["id"]} && lanegate review {ticket["id"]} --verdict changes_requested --summary "<reason>"\n\n'
        "Do not exit until you have run one of the above commands."
    )
    return implement_prompt + appendix
