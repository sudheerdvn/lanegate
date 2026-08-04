"""Auto-fix and drift-check subagents plus combined-mode helpers.

TICK-255/TICK-278: extracted from orchestrate/__init__.py as pure code
movement -- see docs/internal/module-split-proposal.md.
"""

from __future__ import annotations

import io
import os
import re
import sys
import time
from pathlib import Path

from lanegate.config import resolve_model, resolve_trunk_branch
from lanegate.executor import (
    _CLAUDE_SUBPROCESS_TYPES,
    build_executor_cmd,
    get_executor_config,
    parse_structured_result,
    resolve_executor_env,
)
from lanegate.ticket import latest_review_findings, load_all_tickets, write_ticket

from .audit import _write_review_verdict
from .pool import (
    _build_env,
    _cfg_with_driver_command_overrides,
    _resolve_drift_driver_name,
    _resolve_driver_route,
    _unpack_stream_result,
    capture_review_step_run,
    commit_worktree_changes,
    expand_driver,
    invoke_executor,
    make_event_line_handler,
    write_prompt_file_best_effort,
)
from .review import _git_head_sha, resolve_independent_review_driver, run_review_agent
from .run_report import _resolve_active_run_session_ts, _stream_subprocess


def run_fix_agent(
    ticket: dict,
    cfg: dict,
    repo_root: Path,
    worktree_path: Path,
    findings: str,
    pre_fix_sha: str,
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

    Returns True only if the executor exited 0 and produced at least one new
    commit on top of pre_fix_sha; False otherwise (a mechanical failure — the
    caller should escalate without running a drift check, since there is
    nothing new to check).
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
        return False

    fix_prompt = build_fix_prompt(
        ticket, diff=diff, findings=findings, project_root=worktree_path, cfg=cfg
    )

    # The reviewer that recorded the findings being fixed must never also be
    # the one fixing them — a reviewer fixing its own findings has no
    # independent check (TICK-345, one step earlier in the cycle).
    from lanegate.orchestrate.loop import resolve_pool_executor

    excluded = {ticket["review_driver"]} if ticket.get("review_driver") else set()
    fix_executor = resolve_pool_executor("fix", ticket, cfg, repo_root, excluded=excluded)
    if excluded and (fix_executor is None or fix_executor in excluded):
        print(
            f"WARNING: no independent fix executor is available for {tid}; "
            "refusing to dispatch the reviewer to fix its own findings",
            file=sys.stderr,
        )
        return False

    exit_code, *_ = invoke_executor(
        ticket,
        cfg,
        worktree_path,
        prompt_override=fix_prompt,
        step="fix",
        repo_root=repo_root,
        executor_override=fix_executor,
    )
    if exit_code != 0:
        print(f"WARNING: fix agent exited {exit_code} for {tid}", file=sys.stderr)
        return False

    commit_worktree_changes(
        worktree_path, tid, message=f"fix: address review findings for {tid}"
    )

    head_after = _git_head_sha(worktree_path)
    if head_after is None or head_after == pre_fix_sha:
        print(f"WARNING: fix agent for {tid} exited 0 but made no new commit", file=sys.stderr)
        return False
    return True


def run_rebase_fix_agent(
    ticket: dict,
    cfg: dict,
    repo_root: Path,
    worktree_path: Path,
    rebase_detail: str,
) -> bool:
    """Run an autofix agent in worktree_path to resolve git rebase content conflicts,
    run tests, and continue the rebase.

    Returns True if conflict resolution succeeded and rebase was continued cleanly,
    False otherwise.
    """
    from lanegate.orchestrate.loop import _abort_rebase, _conflicted_files, _continue_rebase

    tid = ticket["id"]
    conflict_files = _conflicted_files(worktree_path)

    rebase_fix_prompt = (
        f"You are resolving git rebase content conflicts for ticket {tid}.\n\n"
        f"{rebase_detail}\n\n"
        "Instructions:\n"
        "1. Inspect the conflict markers (`<<<<<<< HEAD`, `=======`, `>>>>>>>`) in the conflicted files.\n"
        "2. Edit the files to resolve the conflict markers cleanly, combining the changes correctly.\n"
        "3. Run project tests to confirm the resolution passes tests and is syntactically valid.\n"
        "4. Do NOT run `git rebase --continue` or `git add` yourself; save the resolved files in place."
    )

    exit_code, *_ = invoke_executor(
        ticket, cfg, worktree_path, prompt_override=rebase_fix_prompt, step="fix", repo_root=repo_root
    )
    if exit_code != 0:
        print(f"WARNING: rebase fix agent exited {exit_code} for {tid}", file=sys.stderr)
        return False

    continued, continue_detail = _continue_rebase(worktree_path, conflict_files)
    if not continued:
        print(f"WARNING: continue rebase failed for {tid}: {continue_detail}", file=sys.stderr)
        return False

    commit_worktree_changes(
        worktree_path, tid, message=f"fix: resolve rebase conflict markers for {tid}"
    )
    return True


def run_drift_check(
    ticket: dict,
    cfg: dict,
    repo_root: Path,
    worktree_path: Path,
    findings: str,
    pre_fix_sha: str,
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
        project_root=worktree_path,
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
        drift_driver_name = _resolve_drift_driver_name(ticket, cfg)
        drift_driver_cfg = expand_driver(drift_driver_name, cfg)
        drift_executor = drift_driver_cfg.get("type", drift_driver_name)
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
        resolved_drift_type = get_executor_config(drift_executor, cfg).get(
            "type", drift_executor
        )
        stdin_capable = resolved_drift_type in (_CLAUDE_SUBPROCESS_TYPES | {"codex", "ollama"})
        # Agy's JSON mode is completion-only, so it needs a flat timeout
        # rather than output-idle detection.
        # Drift checks have no worker heartbeat monitor. Codex's JSON output
        # can be quiet while it works, so use the hard ceiling instead of an
        # output-idle kill for that executor type.
        streaming_capable = resolved_drift_type in _CLAUDE_SUBPROCESS_TYPES
        drift_cmd = build_executor_cmd(
            drift_executor, prompt, drift_command_cfg, model=drift_model,
            analyze_session_id=resume_session_id,
            use_stdin=stdin_capable,
        )
        drift_executor_env = resolve_executor_env(get_executor_config(drift_executor, cfg))
        drift_executor_env = _build_env(drift_driver_cfg, base_env=drift_executor_env)
        stream_kwargs = {
            "idle_timeout": cfg.get("executor_idle_timeout_seconds", 75),
            "absolute_ceiling": cfg.get("executor_absolute_ceiling_seconds", 1500),
        } if streaming_capable else {"timeout": cfg.get("executor_absolute_ceiling_seconds", 1500)}
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

            record_step_cost(repo_root, tid, "drift_check", drift_executor, drift_model, parsed)
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
) -> bool:
    """
    Run up to ``cfg.get("max_auto_fix_attempts", 1)`` fix -> drift-check ->
    re-review cycles for a ticket whose review came back changes_requested.
    Always attempted regardless of ``autonomy`` — the caller decides what to
    do with a True result (merge unattended vs. wait for a human verdict);
    this function only runs the mechanical cycle.

    Returns True if a re-review within budget comes back approved (ticket
    ends at status=in_review, review_verdict=approved, exactly like a
    human-approved ticket — written by run_review_agent's own cmd_review
    call, no special-case write needed here). Returns False if the fix pass
    fails, the drift-check fails (fail-closed, regardless of remaining
    attempt budget — this gate is never bypassed, in any autonomy mode), or
    the attempt cap is exceeded. In every False case the ticket is left at
    status=code_complete, review_verdict=changes_requested (unchanged, so
    cmd_blocked/cmd_merge's guard keep working) with one "## Auto-Fix Attempt
    N" body section per attempt and an updated review_summary describing the
    escalation reason.
    """
    from lanegate.lifecycle import record_auto_fix_attempt
    from lanegate.ticket import parse_ticket

    tid = ticket["id"]
    tickets_dir = repo_root / cfg["tickets_dir"]
    max_attempts = cfg.get("max_auto_fix_attempts", 1)

    initial = parse_ticket(ticket["_path"]) if ticket.get("_path") else ticket
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

        if not run_fix_agent(current, cfg, repo_root, worktree_path, findings, pre_fix_sha):
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

        drift = run_drift_check(current, cfg, repo_root, worktree_path, findings, pre_fix_sha)
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

        approved = run_review_agent(current, repo_root, worktree_path=worktree_path, cfg=cfg)

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
    review from a separate `lanegate orchestrate` process) has no other way to
    reach the auto-fix machinery, since it is otherwise reachable only from
    inside ``_drain_loop`` immediately after a dispatch in the same process.
    """
    from lanegate.ticket import canonical_id

    tid = canonical_id(ticket_id)
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
    if fixed:
        print(f"[fix] {tid}: auto-fix cycle reached approved — awaiting human merge approval")
    else:
        print(f"[fix] {tid}: auto-fix cycle escalated — see ticket for details", file=sys.stderr)


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
    or callers without worktree context (repo_root is None) bypass the ladder and return True.
    """
    route = _resolve_driver_route(cfg, ticket)
    if route["mode"] == "split":
        return False

    if _has_explicit_review_route(cfg, ticket) or repo_root is None:
        return True

    impl = implementer or route["implement"]
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
