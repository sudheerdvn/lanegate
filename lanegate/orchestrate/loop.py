"""
Board-clearing loop and its supporting helpers.

Usage:
    lanegate run                                # clear the board using executor from .lanegate.yml
    lanegate run --max 3                        # cap parallel tickets
    lanegate run --dry-run                      # print planned actions, do nothing
    lanegate run --human-review final           # per_ticket | final | none
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import datetime
import fnmatch
import io
import json
import os
import re
import signal
import subprocess
import sys
import traceback
import threading
import time
from pathlib import Path

from lanegate.concurrency import (
    OrchestratorLockError,
    acquire_orchestrator_lock,
    hollow_lock_holders,
    release_orchestrator_lock,
    touches_overlap,
)
from lanegate.config import (
    is_auto_fix_lane,
    is_red_lane,
    resolve_acceptance_contract_mode,
    resolve_autonomy,
    resolve_human_escalation,
    resolve_max_parallel_detail,
    resolve_model,
    resolve_trunk_branch,
    resolve_ticket_pool,
)
from lanegate.executor import (
    DEFAULT_COOLDOWN_TTL_SECONDS,
    available_instances as _available_executor_instances,
    build_executor_cmd,
    clear_cooldown as _clear_executor_cooldown,
    clear_failure_streak as _clear_pool_failure_streak,
    get_executor_config,
    is_cooling_down as _executor_is_cooling_down,
    read_cooldown as _read_executor_cooldown,
    record_failure_signature as _record_pool_failure_signature,
    resolve_executor_env,
    write_cooldown as _write_executor_cooldown,
)
from lanegate.git import git_text
from lanegate.pidutil import force_kill_pid, pid_alive
from lanegate.ticket import (
    TERMINAL_STATUSES,
    _RATE_LIMIT_MARKER,
    _active_rate_limit_hibernation,
    _clean_attention_reason,
    _has_non_rate_limit_hard_error,
    _is_resumable_rate_limit,
    branch_name,
    is_lanegate_notes_file,
    is_paired_test_file,
    load_all_tickets,
    milestone_near_miss_warnings,
    needs_review_recovery_advice,
)

_git_text = git_text

# Kept at module scope so tests and operators can shorten the bounded
# cooldown-poll interval without reaching into _drain_loop's closure.
_COOLDOWN_POLL_SECONDS = 30


def _configured_executor_names(cfg: dict) -> list[str]:
    """Return configured executor instances, in stable first-seen order."""
    names: list[str] = []

    def add(name: object) -> None:
        if isinstance(name, str) and name and name not in names:
            names.append(name)

    executors = cfg.get("executors") or {}
    if isinstance(executors, dict):
        for name, executor_cfg in executors.items():
            if isinstance(executor_cfg, dict) and executor_cfg.get("type"):
                add(name)
    pools = cfg.get("pools") or {}
    if isinstance(pools, dict):
        for pool_cfg in pools.values():
            if isinstance(pool_cfg, dict):
                for name in pool_cfg.get("executors") or []:
                    add(name)
    return names


def _auto_reset_elapsed_executor_cooldowns(cfg: dict, repo_root: Path) -> list[str]:
    """Clear only configured cooldowns with an elapsed, parseable time window."""
    now = datetime.datetime.now(datetime.UTC)
    reset: list[str] = []
    for name in _configured_executor_names(cfg):
        if not _executor_is_cooling_down(repo_root, name):
            continue
        cooldown = _read_executor_cooldown(repo_root, name)
        until = cooldown.get("until") if cooldown else None
        if not isinstance(until, str) or not until:
            continue
        try:
            deadline = datetime.datetime.fromisoformat(until.replace("Z", "+00:00"))
        except ValueError:
            continue
        if deadline.tzinfo is None:
            continue
        if deadline.astimezone(datetime.UTC) <= now and _clear_executor_cooldown(repo_root, name):
            reset.append(name)
    return reset

# Safety gates (injection scan, blocked-file check, diff parser, static
# analysis) live in orchestrate/guards.py; re-exported here so
# `from lanegate.orchestrate import X` keeps working for every caller and test.
from .guards import (
    _is_blocked_file,
    _run_acceptance_contract_audit,
    _run_static_analysis,
    _scan_injection_signals,
    check_control_plane_compliance,
    risk_lane_requires_human_review,
    scan_risk_lane,
)
from .guards import _BLOCKED_FILE_RULES as _BLOCKED_FILE_RULES
from .guards import _INJECTION_SIGNALS as _INJECTION_SIGNALS
from .guards import _SYSTEM_SECTION_HEADERS as _SYSTEM_SECTION_HEADERS
from .guards import _parse_diff_changed_lines as _parse_diff_changed_lines

# Tee logging and executor audit-bundle capture (transcript + task-output
# capture, manifest, gate capture) live in orchestrate/audit.py;
# re-exported here so `from lanegate.orchestrate import X` keeps working for
# every caller and test.
from .audit import _LogTee as _LogTee
from .audit import _active_status_path as _active_status_path
from .audit import _artifact_safe_name as _artifact_safe_name
from .audit import _audit_bundle_path as _audit_bundle_path
from .audit import _capture_executor_audit_bundle as _capture_executor_audit_bundle
from .audit import _claude_encoded_cwd as _claude_encoded_cwd
from .audit import _codex_session_dirs as _codex_session_dirs
from .audit import _copy_bounded_file as _copy_bounded_file
from .audit import _copy_claude_task_outputs as _copy_claude_task_outputs
from .audit import _find_claude_transcript as _find_claude_transcript
from .audit import _find_codex_transcript as _find_codex_transcript
from .audit import _find_latest_audit_bundle as _find_latest_audit_bundle
from .audit import _finish_gate_capture as _finish_gate_capture
from .audit import _load_audit_manifest as _load_audit_manifest
from .audit import _manifest_capture as _manifest_capture
from .audit import _manifest_missing as _manifest_missing
from .audit import _mtime_in_window as _mtime_in_window
from .audit import _new_manifest as _new_manifest
from .audit import _record_gate as _record_gate
from .audit import _record_static_analysis_decision as _record_static_analysis_decision
from .audit import _run_gate_command as _run_gate_command
from .audit import _run_git_snapshot as _run_git_snapshot
from .audit import _safe_rel as _safe_rel
from .audit import _save_audit_manifest as _save_audit_manifest
from .audit import _start_gate_capture as _start_gate_capture
from .audit import _status as _status
from .audit import _utc_now_iso as _utc_now_iso
from .audit import _write_bounded_text as _write_bounded_text
from .audit import _write_json_atomic as _write_json_atomic

# Durable run-event log and CLI status-reporting commands (event log
# append/load/path helpers, last-run pointer, live lanegate-spawned process
# enumeration, `lanegate ps`, `lanegate run-report`, `lanegate run --status`, and
# the executor subprocess-streaming helper) live in orchestrate/run_report.py;
# re-exported here so `from lanegate.orchestrate import X` keeps
# working for every caller and test.
from .run_report import _LAST_RUN_POINTER as _LAST_RUN_POINTER
from .run_report import _RUN_EVENTS_SUFFIX as _RUN_EVENTS_SUFFIX
from .run_report import _append_run_event as _append_run_event
from .run_report import _collect_live_lanegate_processes as _collect_live_lanegate_processes
from .run_report import _iso_duration_seconds as _iso_duration_seconds
from .run_report import _load_run_events as _load_run_events
from .run_report import _resolve_run_session_ts as _resolve_run_session_ts
from .run_report import _run_events_path as _run_events_path
from .run_report import _stream_subprocess as _stream_subprocess
from .run_report import _write_last_run_pointer as _write_last_run_pointer
from .run_report import build_run_report as build_run_report
from .run_report import build_run_summary as build_run_summary
from .run_report import cmd_orchestrate_status as cmd_orchestrate_status
from .run_report import cmd_ps as cmd_ps
from .run_report import cmd_run_report as cmd_run_report
from .run_report import print_run_summary as print_run_summary
from .run_report import read_executor_events as read_executor_events
from .run_report import suppress_direct_action_tracking as suppress_direct_action_tracking
from .run_report import summarize_executor_events as summarize_executor_events

# Board batch selection and review/continuation queue rendering live in
# orchestrate/batch.py; re-exported here so `from
# lanegate.orchestrate import X` keeps working for every caller and test.
from .batch import _continuation_step_lines as _continuation_step_lines
from .batch import _format_max_parallel_detail as _format_max_parallel_detail
from .batch import _print_continuation_steps as _print_continuation_steps
from .batch import _print_review_queue as _print_review_queue
from .batch import _review_queue_lines as _review_queue_lines
from .batch import _ticket_next_step_line as _ticket_next_step_line
from .batch import _underfilled_batch_reason as _underfilled_batch_reason
from .batch import next_batch as next_batch

# Active-run status bookkeeping (read/write active-status file(s), executor
# PID markers, elapsed-time formatting, normalization/aggregation across
# concurrent executors, the get_orchestration_status() API wrapper, and
# stale-executor-marker reconciliation) lives in orchestrate/status.py;
# re-exported here so `from lanegate.orchestrate import X` keeps
# working for every caller and test.
from .status import _executor_marker_base as _executor_marker_base
from .status import _format_elapsed as _format_elapsed
from .status import _normalize_active_status as _normalize_active_status
from .status import _read_active_status as _read_active_status
from .status import _read_all_active_statuses as _read_all_active_statuses
from .status import _reconcile_stale_executor_markers as _reconcile_stale_executor_markers
from .status import _remove_executor_markers as _remove_executor_markers
from .status import _write_active_status as _write_active_status
from .status import _write_executor_pid_marker as _write_executor_pid_marker
from .status import get_all_active_statuses as get_all_active_statuses
from .status import get_orchestration_status as get_orchestration_status

from .status import write_batch_status as write_batch_status

# Executor pool selection/invocation (driver resolution, prompt dispatch,
# worktree commit helpers) lives in orchestrate/pool.py;
# re-exported here so `from lanegate.orchestrate import X` keeps working for
# every caller and test.
from .pool import _CONFIG_ERROR_EXIT_CODE as _CONFIG_ERROR_EXIT_CODE
from .pool import _DEFAULT_HEARTBEAT_SECONDS as _DEFAULT_HEARTBEAT_SECONDS
from .pool import _build_env as _build_env
from .pool import _cfg_with_driver_command_overrides as _cfg_with_driver_command_overrides
from .pool import _committed_files as _committed_files
from .pool import _expand_env_refs as _expand_env_refs
from .pool import _invoke_ollama as _invoke_ollama
from .pool import _resolve_driver_route as _resolve_driver_route
from .pool import _resolve_drift_driver_name as _resolve_drift_driver_name
from .pool import _ticket_for_model_resolution as _ticket_for_model_resolution
from .pool import _write_prompt_file as _write_prompt_file
from .pool import check_worktree_has_commits as check_worktree_has_commits
from .pool import commit_worktree_changes as commit_worktree_changes
from .pool import expand_driver as expand_driver
from .pool import invoke_executor as invoke_executor
from .pool import resolve_dispatch as resolve_dispatch
from .pool import resolve_driver as resolve_driver
from .status import write_executing_status as write_executing_status


















# ---------------------------------------------------------------------------
# Executor dispatch
# ---------------------------------------------------------------------------








# Review subagent and review-related daemon helpers live in
# orchestrate/review.py; re-exported here so `from
# lanegate.orchestrate import X` keeps working for every caller and test.
from .review import _git_head_sha as _git_head_sha
from .review import _invoke_cmd_review as _invoke_cmd_review
from .review import _make_error_review as _make_error_review
from .review import _minimal_cfg as _minimal_cfg
from .review import run_review_agent as run_review_agent
from .review import spawn_resume_watch_daemon as spawn_resume_watch_daemon
from .review import spawn_watch_daemon as spawn_watch_daemon


# Fix/drift-check subagents and combined-vs-split-mode helpers live in
# orchestrate/autofix.py; re-exported here so `from
# lanegate.orchestrate import X` keeps working for every caller and test.
from .autofix import _build_combined_prompt as _build_combined_prompt
from .autofix import _extract_review_findings as _extract_review_findings
from .autofix import _is_combined_mode as _is_combined_mode
from .autofix import backfill_combined_review_metadata as backfill_combined_review_metadata
from .autofix import run_auto_fix_cycle as run_auto_fix_cycle
from .autofix import run_drift_check as run_drift_check
from .autofix import run_fix_agent as run_fix_agent


# ---------------------------------------------------------------------------
# Main board-clearing loop
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Draft auto-analyze helpers
# ---------------------------------------------------------------------------

# Consecutive drafts that must fail with the identical stderr text before
# _analyze_drafts treats it as a systemic problem and stops the whole pass
# rather than a per-ticket content issue.

# Consecutive same-signature executor failures on one pool instance, within
# _POOL_FAILURE_STREAK_WINDOW_S of each other, treated as equivalent to a
# recognized rate-limit hibernation even when the text matches no known
# rate-limit pattern (e.g. agy's opaque "timeout waiting for response").
_POOL_FAILURE_STREAK_THRESHOLD = 5
_POOL_FAILURE_STREAK_WINDOW_S = 900

























# The shared ticket.py classifier distinguishes resumable rate-limit
# hibernations from human-actionable hibernations; this loop also uses its
# marker to identify pool instances that remain in cooldown.













































# Dispatch helpers live separately; re-export the historical loop surface.
from .loop_dispatch import (_auth_error_reason,_executor_alive,_executor_setup_error_reason,_gather_rate_limit_texts,_hibernate_orphaned,_interrupted_exit_reason,_is_auth_error,_is_executor_setup_error,_is_interrupted_exit,_is_rate_limit,_kill_pid,_last_cooldown_event,_load_pool_state,_pool_instance_healthy,_pool_state_path,_rate_limit_detection_text,_rate_limit_reason,_watchdog_termination_reason,_reap_orphaned_executor_processes,_recent_hibernation_status,_save_pool_state,resolve_pool_executor)

from .loop_analyze import _Tee, _analyze_drafts, _normalize_analyze_failure_reason, _print_draft_analysis_plan, _queue_code_complete_reviews

from .loop_recovery import _abort_rebase, _collect_prior_notes, _conflicted_files, _continue_rebase, _extract_conflict_hunks, _format_conflict_detail, _prepend_context, _record_auto_claimed_touches, _run_rebase, _scope_only_needs_review_files, _ticket_has_real_progress, _worktree_is_dirty, is_mid_rebase, recover_scope_only_needs_review_tickets


def _is_suspend_gap(captured_stdout: str = "", captured_stderr: str = "") -> bool:
    """True when LaneGate's own watchdog classified the kill as an orchestrator
    suspend gap (wall-clock elapsed far exceeds timeout while watchdog CPU time
    barely moved).

    Matches the diagnostic line ``invoke_executor`` appends *after* the kill
    (``dispatch terminated due to 'suspend_gap'``) — a LaneGate-generated string,
    not executor-controlled output. Used only to phrase the hibernation reason:
    the hibernate-vs-halt decision is made by ``_watchdog_termination_reason``.
    """
    return "dispatch terminated due to 'suspend_gap'" in (captured_stdout + "\n" + captured_stderr)


def _checkpoint_before_hibernate(repo_root: Path, wt: str | Path | None, tid: str, kind: str) -> None:
    """WIP-commit any uncommitted edits in the ticket worktree before a
    hibernation path discards the run context, so ``lanegate run`` can resume
    the work later. No-op when there is no usable worktree; a checkpoint
    failure is logged, not raised (hibernation must still proceed).
    """
    if not (wt and Path(wt).exists() and (Path(wt) / ".git").exists()):
        return
    from lanegate.lifecycle import checkpoint_dirty_worktree
    try:
        checkpoint_dirty_worktree(
            repo_root,
            Path(wt),
            msg=f"wip: uncommitted edits preserved before hibernation ({kind})",
        )
    except RuntimeError as exc:
        print(
            f"[orchestrate] {tid}: failed to checkpoint worktree before hibernation: {exc}",
            file=sys.stderr,
        )


def cmd_orchestrate(
    cfg: dict,
    repo_root: Path,
    *,
    max_parallel: int | None = None,
    dry_run: bool = False,
    human_review: str | None = None,
    milestone: str | None = None,
    all_milestones: bool = False,
    tickets: list[str] | None = None,
    auto_analyze: bool = True,
    recover: bool = True,
    verbose: bool = False,
    pool: str | None = None,
    executors: list[str] | None = None,
) -> None:
    """Clear the ticket board using the configured executor.

    Thin wrapper around `_cmd_orchestrate_body` that suppresses direct-action
    tracking for the whole run: cmd_start/cmd_review/cmd_merge/etc. run
    dozens of times per ticket inside a run as ordinary lifecycle steps, and
    each already appears in this run's own orchestrate-*.events.jsonl, so
    tracking them again as standalone `action-*` entries would just duplicate
    the run in the run list. Direct-action tracking exists to give a
    human running `lanegate start`/`review`/etc. by hand (outside any run) a
    durable run id; suppress it here so it stays out of process.
    """
    with suppress_direct_action_tracking():
        _cmd_orchestrate_body(
            cfg,
            repo_root,
            max_parallel=max_parallel,
            dry_run=dry_run,
            human_review=human_review,
            milestone=milestone,
            all_milestones=all_milestones,
            tickets=tickets,
            auto_analyze=auto_analyze,
            recover=recover,
            verbose=verbose,
            pool=pool,
            executors=executors,
        )


def _cmd_orchestrate_body(
    cfg: dict,
    repo_root: Path,
    *,
    max_parallel: int | None = None,
    dry_run: bool = False,
    human_review: str | None = None,
    milestone: str | None = None,
    all_milestones: bool = False,
    tickets: list[str] | None = None,
    auto_analyze: bool = True,
    recover: bool = True,
    verbose: bool = False,
    pool: str | None = None,
    executors: list[str] | None = None,
) -> None:
    """
    Clear the ticket board using the configured executor.

    Args:
        cfg: loaded config dict
        repo_root: repository root path
        max_parallel: cap on parallel workers (overrides cfg.max_parallel)
        dry_run: print planned actions without executing
        human_review: "none" | "per_ticket" | "final" | None. When None (not
            passed explicitly on the CLI), falls back to default_human_review
            in .lanegate.yml, or "none" if that is also unset.
        milestone: restrict the run to tickets with this milestone tag
        all_milestones: clear tickets across all milestones (overrides milestone check)
        tickets: optional explicit list of ticket IDs restricting
            dispatch to exactly these IDs, on top of (not instead of) the
            usual status/milestone/deps/lock eligibility filtering. Composes
            with milestone rather than replacing it.
        auto_analyze: when True (default), analyze draft tickets at the top of
            each loop iteration before calling next_batch; set to False via
            --no-auto-analyze to skip this step.
        recover: when True, hibernate orphaned in_progress tickets before
            acquiring the orchestrator lock.
        verbose: when True, stream full executor output to terminal; when False
            (default), print compact per-ticket progress lines only and route
            executor output to the log file only.
        pool: name of a `pools:` entry to draw executor instances
            from; falls back to `cfg.default_pool` when not given, or to plain
            single-executor dispatch when neither is set.
        executors: optional ad-hoc list of executor instance names to draw from
            for this run without declaring a named pools: entry.
    """
    triggered_by = os.environ.get("LANEGATE_RUN_TRIGGER", "manual")
    trigger_reason = os.environ.get("LANEGATE_RUN_TRIGGER_REASON")

    # Store the original orchestrate arguments for resume-watch to use on retry
    from lanegate.resume_watch import store_orchestrate_args
    args_to_store: list[str] = []
    if max_parallel is not None:
        args_to_store.extend(["--max", str(max_parallel)])
    if dry_run:
        args_to_store.append("--dry-run")
    if human_review is not None:
        args_to_store.extend(["--human-review", human_review])
    if milestone is not None:
        args_to_store.extend(["--milestone", milestone])
    if all_milestones:
        args_to_store.append("--all")
    if tickets:
        args_to_store.extend(["--tickets", ",".join(tickets)])
    if not auto_analyze:
        args_to_store.append("--no-auto-analyze")
    if not recover:
        args_to_store.append("--no-recover")
    if verbose:
        args_to_store.append("--verbose")
    if pool is not None:
        args_to_store.extend(["--pool", pool])
    if executors is not None:
        args_to_store.extend(["--executors", ",".join(executors)])
    store_orchestrate_args(repo_root, args_to_store)

    if pool is not None and executors is not None:
        print(
            "ERROR: --pool and --executors are mutually exclusive selectors",
            file=sys.stderr,
        )
        sys.exit(1)

    effective_pool: str | None
    if executors is not None:
        unknown = [e for e in executors if e not in (cfg.get("executors") or {})]
        if unknown:
            print(
                f"ERROR: unknown executor(s) in --executors: {', '.join(unknown)}",
                file=sys.stderr,
            )
            sys.exit(1)
        cfg = {
            **cfg,
            "pools": {
                **(cfg.get("pools") or {}),
                "__adhoc__": {"executors": executors, "strategy": "least-loaded"},
            },
        }
        effective_pool = "__adhoc__"
    else:
        effective_pool = pool or cfg.get("default_pool")

    if effective_pool is not None and effective_pool not in (cfg.get("pools") or {}):
        print(
            f"ERROR: --pool {effective_pool!r} is not defined in pools: in .lanegate.yml",
            file=sys.stderr,
        )
        sys.exit(1)
    effective_human_review = (
        human_review if human_review is not None else cfg.get("default_human_review", "none")
    )
    if effective_human_review not in ("none", "per_ticket", "final"):
        source = "--human-review" if human_review is not None else "default_human_review in .lanegate.yml"
        print(
            f"ERROR: {source} must be one of none, per_ticket, final; got {effective_human_review!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Guard: uncommitted changes on main block any eventual merge step.
    # Catch this early so users don't hit a cryptic git error mid-run.
    # Exclude gitignored directories (tickets_dir, worktrees_dir) since they don't affect merges
    # and may be left dirty when commit_status_changes=False.
    if not dry_run:
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_root),
            capture_output=True,
            text=True, encoding="utf-8",
        )
        tickets_dir = cfg.get("tickets_dir", ".lanegate/tickets")
        worktrees_dir = cfg.get("worktrees_dir", ".lanegate/worktrees")
        dirty_files = [
            line[3:]
            for line in dirty.stdout.splitlines()
            if line and not line.startswith("??")  # ignore untracked
            and not line[3:].startswith(f"{tickets_dir}/")  # ignore tracked ticket files
            and not line[3:].startswith(f"{worktrees_dir}/")  # ignore tracked worktree files
        ]
        if dirty_files:
            print(
                "ERROR: main branch has uncommitted changes — these will block ticket merges later.\n"
                "  Uncommitted files:\n"
                + "".join(f"    {f}\n" for f in dirty_files)
                + "  Commit or stash them before running orchestrate:\n"
                "    git add -p && git commit\n"
                "    (or: git stash)",
                file=sys.stderr,
            )
            sys.exit(1)

    # Resolve effective milestone (unless --all overrides everything)
    effective_milestone: str | None = None
    if not all_milestones:
        if milestone:
            effective_milestone = milestone
        elif cfg.get("default_milestone"):
            effective_milestone = cfg["default_milestone"]
        else:
            from lanegate.create import _discover_milestones

            probe_dir = repo_root / cfg.get("tickets_dir", ".lanegate/tickets")
            if _discover_milestones(probe_dir, cfg["ticket_prefix"], cfg, raise_on_error=True):
                print(
                    "ERROR: no milestone specified and no default_milestone in .lanegate.yml.\n"
                    "Run with --milestone <m> or --all to clear tickets across all milestones.",
                    file=sys.stderr,
                )
                sys.exit(1)
            # No ticket uses the milestone field at all -- nothing to scope by,
            # so a bare `lanegate run` clears everything instead of hard-erroring
            # on a fresh project's first, untagged ticket.

    # Detect and warn about milestone near-miss values (e.g., '1.5' vs 'v1.5')
    if effective_milestone:
        tickets_dir = repo_root / cfg.get("tickets_dir", ".lanegate/tickets")
        all_tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
        near_misses = milestone_near_miss_warnings(all_tickets, effective_milestone)
        if near_misses:
            ticket_names = ", ".join(w["ticket_id"] for w in near_misses)
            print(
                f"WARNING: Skipping {len(near_misses)} ticket(s) with near-miss milestone values: {ticket_names}\n"
                f"  These tickets have milestone values like {near_misses[0]['ticket_milestone']!r}\n"
                f"  instead of the active milestone {effective_milestone!r}.\n"
                f"  If intended for this run, update their milestone field.",
                file=sys.stderr,
            )

    # Resolve explicit ticket scope: composes with milestone above
    # rather than replacing it — an id must still pass the usual eligibility
    # filtering (status/deps/lock) in next_batch() to actually be dispatched.
    effective_ticket_ids: set[str] | None = None
    milestone_excluded_ticket_scope = False
    if tickets:
        effective_ticket_ids = {tid.strip() for tid in tickets if tid and tid.strip()}
        if not effective_ticket_ids:
            effective_ticket_ids = None

    if effective_ticket_ids:
        scope_tickets_dir = repo_root / cfg.get("tickets_dir", ".lanegate/tickets")
        scope_all_tickets, _ = load_all_tickets(scope_tickets_dir, cfg["ticket_prefix"], cfg)
        known_ids = {t["id"] for t in scope_all_tickets}
        unknown_ids = sorted(effective_ticket_ids - known_ids)
        if unknown_ids:
            print(
                f"WARNING: --tickets includes unknown ticket id(s): {', '.join(unknown_ids)}",
                file=sys.stderr,
            )
        if effective_milestone:
            milestone_excluded = [
                ticket
                for ticket in scope_all_tickets
                if ticket["id"] in effective_ticket_ids
                and ticket.get("milestone") != effective_milestone
            ]
            if milestone_excluded:
                milestone_excluded_ticket_scope = True
                excluded_details = ", ".join(
                    f"{ticket['id']} (milestone {ticket.get('milestone')!r})"
                    for ticket in milestone_excluded
                )
                milestones = {ticket.get("milestone") for ticket in milestone_excluded}
                if len(milestones) == 1 and None not in milestones:
                    fix = f"--milestone {next(iter(milestones))}"
                else:
                    fix = "--milestone <actual milestone>"
                print(
                    "WARNING: --tickets scope excludes ticket(s) due to the active "
                    f"milestone filter {effective_milestone!r}: {excluded_details}\n"
                    f"  Re-run with {fix} or --all to include them.",
                    file=sys.stderr,
                )

    max_parallel_detail = resolve_max_parallel_detail(cfg, override=max_parallel)
    effective_max = int(max_parallel_detail["value"])

    logs_dir = repo_root / ".lanegate" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    session_ts = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    log_path = logs_dir / f"orchestrate-{session_ts}.log"

    # Keep 10 most recent in logs_dir; move older to archive. Archive purge is
    # opt-in (run_history_purge_enabled, default False) and only then bounded
    # by run_history_retention_days (default 60) -- see
    # run_summary.list_run_summaries, which reads both logs_dir and archive_dir
    # so archived runs stay visible in run history unless a project explicitly
    # enables the purge.
    archive_dir = logs_dir / "archive"
    archive_dir.mkdir(exist_ok=True)
    if cfg.get("run_history_purge_enabled", False):
        retention_days = cfg.get("run_history_retention_days", 60)
        cutoff = datetime.datetime.now() - datetime.timedelta(days=retention_days)
        for f in archive_dir.glob("orchestrate-*.log"):
            if datetime.datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                f.unlink(missing_ok=True)
    old_logs = sorted(logs_dir.glob("orchestrate-*.log"))
    for old in old_logs[:-9]:
        old.rename(archive_dir / old.name)

    report_session_ts = None if dry_run else session_ts

    with open(log_path, "w") as _log_f:
        _log_f.write(
            f"=== orchestrate {datetime.datetime.now().isoformat(timespec='seconds')} ===\n"
        )
        _log_f.flush()

        _orig_out, _orig_err = sys.stdout, sys.stderr
        sys.stdout = _LogTee(_orig_out, _log_f)
        sys.stderr = _LogTee(_orig_err, _log_f)
        try:
            print(f"[orchestrate] logging to {log_path}")
            if effective_milestone:
                print(f"[orchestrate] milestone filter: {effective_milestone}")
            if effective_ticket_ids:
                print(
                    f"[orchestrate] ticket scope: {', '.join(sorted(effective_ticket_ids))}"
                )
            print(
                f"[orchestrate] max_parallel: "
                f"{_format_max_parallel_detail(max_parallel_detail)}"
            )

            if not dry_run:
                assert report_session_ts is not None  # report_session_ts is session_ts whenever not dry_run
                reaped: list[str] = []

                def reap_before_claim() -> None:
                    # The acquisition guard has rejected any live holder, while
                    # the stale holder remains visible to orphan detection.
                    reaped.extend(
                        _reap_orphaned_executor_processes(
                            cfg,
                            repo_root,
                            out_stream=sys.stderr,
                            session_ts=report_session_ts,
                        )
                    )

                try:
                    pid = acquire_orchestrator_lock(repo_root, before_claim=reap_before_claim)
                    print(f"[orchestrate] lock acquired (PID {pid})")
                except OrchestratorLockError as e:
                    print(f"ERROR: {e}", file=sys.stderr)
                    sys.exit(1)
                if reaped:
                    print(
                        f"[orchestrate] reaped {len(reaped)} orphaned executor "
                        f"process(es) from a dead driver: {', '.join(reaped)}"
                    )

                _write_last_run_pointer(repo_root, report_session_ts, log_path)
                _append_run_event(
                    repo_root,
                    report_session_ts,
                    "run_start",
                    pid=os.getpid(),
                    milestone=effective_milestone,
                    ticket_ids=sorted(effective_ticket_ids) if effective_ticket_ids else None,
                    pool=effective_pool,
                    max_parallel=effective_max,
                    human_review=effective_human_review,
                    triggered_by=triggered_by,
                    trigger_reason=trigger_reason,
                )

            if recover and not dry_run:
                _hibernate_orphaned(cfg, repo_root)
                from lanegate.lifecycle import cmd_recover_rate_limited_reviews

                rec_tickets_dir = repo_root / cfg.get("tickets_dir", ".lanegate/tickets")
                rec_all_tickets, _ = load_all_tickets(
                    rec_tickets_dir, cfg.get("ticket_prefix", "TICK-"), cfg
                )
                rec_candidates = [
                    t["id"]
                    for t in rec_all_tickets
                    if t.get("status") == "needs_review"
                    and not t.get("auto_fix_attempts")
                    and (
                        effective_milestone is None
                        or t.get("milestone") == effective_milestone
                    )
                    and (
                        effective_ticket_ids is None
                        or t["id"] in effective_ticket_ids
                    )
                ]
                recovered_count = 0
                for tid in rec_candidates:
                    buf = io.StringIO()
                    old_stdout = sys.stdout
                    try:
                        sys.stdout = buf
                        r = cmd_recover_rate_limited_reviews(tid, cfg, repo_root)
                        recovered_count += r
                    finally:
                        sys.stdout = old_stdout
                if recovered_count > 0:
                    print(f"Recovered {recovered_count} rate-limited review ticket(s).")

            run_status = "completed"
            try:
                drain_recovery_kwargs = {"recover": False} if not recover else {}
                _drain_loop(
                    cfg,
                    repo_root,
                    effective_max,
                    dry_run,
                    effective_human_review,
                    effective_milestone,
                    auto_analyze=auto_analyze,
                    verbose=verbose,
                    pool_name=effective_pool,
                    ticket_ids=effective_ticket_ids,
                    milestone_excluded_ticket_scope=milestone_excluded_ticket_scope,
                    _orig_out=_orig_out,
                    _log_f=_log_f,
                    session_ts=report_session_ts,
                    **drain_recovery_kwargs,
                )
            except BaseException as exc:
                run_status = f"crashed: {exc.__class__.__name__}: {exc}"
                raise
            finally:
                _append_run_event(repo_root, report_session_ts, "run_end", status=run_status)
                if report_session_ts and not dry_run:
                    summary = build_run_summary(cfg, repo_root, session_ts=report_session_ts)
                    if summary:
                        print_run_summary(summary, stream=_orig_out)
                if not dry_run:
                    release_orchestrator_lock(repo_root)
                    print("[orchestrate] lock released")
        finally:
            sys.stdout = _orig_out
            sys.stderr = _orig_err


def _drain_loop(
    cfg: dict,
    repo_root: Path,
    max_parallel: int,
    dry_run: bool,
    human_review: str,
    milestone: str | None = None,
    *,
    auto_analyze: bool = True,
    recover: bool = True,
    verbose: bool = False,
    pool_name: str | None = None,
    ticket_ids: set[str] | None = None,
    milestone_excluded_ticket_scope: bool = False,
    _orig_out=None,
    _log_f=None,
    session_ts: str | None = None,
) -> None:
    """Inner board-clearing loop — separated for testability.

    Args:
        ticket_ids: optional explicit set of ticket IDs restricting
            dispatch to exactly these IDs; composes with milestone.
        verbose: when True, stream full executor output to terminal; when False
            (default), print compact per-ticket status lines only and route
            executor output to the log file only.
        pool_name: name of a `pools:` entry to draw executor
            instances from for tickets that don't already carry an explicit
            `ticket.executor` override. None (default) leaves dispatch
            entirely to the existing single-executor resolution.
        _orig_out: the real terminal stream (pre-tee); used for compact status
            lines and batch summaries.  Falls back to sys.stdout when None
            (e.g. during unit tests that call _drain_loop directly).
        _log_f: the open log file; used as the exclusive destination for
            executor output in compact mode.  Falls back to None (no redirect)
            when absent.
        session_ts: this run's session timestamp, used to append
            durable events to `.lanegate/logs/orchestrate-<ts>.events.jsonl` for
            `lanegate run-report`. None (the default, e.g. dry-run or direct
            test calls) disables event recording.
    """
    from lanegate.lifecycle import (
        MergeFailedError,
        _clear_human_review_approval,
        _commit_generated_ticket_write,
        cmd_complete,
        cmd_fail,
        cmd_hibernate,
        cmd_merge,
        cmd_needs_review,
        cmd_start,
    )
    from lanegate.lifecycle import (
        cmd_review as _cmd_review,
    )
    from lanegate.concurrency import SafeguardLockHeld, safeguard_lock
    from lanegate.safeguards import run_safeguards
    from lanegate.ticket import load_all_tickets, write_ticket
    from lanegate.worktree import worktree_path

    # When called from tests (without the tee context), fall back to sys.stdout.
    orig_out = _orig_out if _orig_out is not None else sys.stdout

    tickets_dir = repo_root / cfg["tickets_dir"]
    worktrees_dir = repo_root / cfg["worktrees_dir"]

    def _log_dispatch(tid: str, executor: str | None, was_hibernated: bool) -> None:
        _append_run_event(
            repo_root,
            session_ts,
            "ticket_dispatch",
            ticket_id=tid,
            executor=executor,
            was_hibernated=was_hibernated,
        )

    def _log_outcome(tid: str, outcome: str, *, reason: str | None = None) -> None:
        _append_run_event(
            repo_root, session_ts, "ticket_outcome", ticket_id=tid, outcome=outcome, reason=reason
        )

    # --- executor pool dispatch ---
    # pool_running/pool_rr_index are only ever mutated from the main thread
    # (inside submit(), or the serial for-loop below) — worker threads never
    # touch them, so no lock is needed even under max_parallel > 1.
    pool_running: dict[str, int] = {}

    # Load persisted rotation state: rr_index continues from where
    # the last run left off; dispatch_counts breaks least-loaded ties across runs.
    _persisted_pool_state: dict = (
        _load_pool_state(repo_root) if pool_name and not dry_run else {}
    )
    _persisted_pool = _persisted_pool_state.get(pool_name, {}) if pool_name else {}
    pool_rr_index: dict[str, int] = (
        {pool_name: _persisted_pool.get("rr_index", 0)} if pool_name else {}
    )
    # Cumulative dispatch totals per instance; never decremented. Used as a
    # tiebreaker in least-loaded selection so historical usage is respected
    # even when pool_running is all-zeros at the start of a fresh run.
    _pool_dispatch_counts: dict[str, int] = dict(
        _persisted_pool.get("dispatch_counts", {})
    )

    def _pool_has_available_instance(name: str) -> bool:
        """True if at least one instance in pool *name* is neither cooling
        down nor hibernated-for-rate-limit."""
        pool_cfg = cfg["pools"][name]
        return any(
            _pool_instance_healthy(repo_root, cfg, candidate)
            for candidate in pool_cfg["executors"]
        )

    _COOLDOWN_MAX_POLLS = 4

    def _wait_for_pool_capacity(name: str) -> bool:
        """Block in bounded steps until some instance in pool *name* is no
        longer cooling down, instead of either tight-looping or halting the
        whole run the instant every instance is exhausted at once.
        Gives up (returns False) after a bounded number of polls so a
        genuinely stuck run still surfaces to the human gate / resume-watch
        rather than hanging forever."""
        for _ in range(_COOLDOWN_MAX_POLLS):
            if _pool_has_available_instance(name):
                return True
            print(
                f"[orchestrate] all instances in pool {name!r} are cooling down — "
                f"waiting {_COOLDOWN_POLL_SECONDS}s before checking again",
                file=sys.stderr,
            )
            time.sleep(_COOLDOWN_POLL_SECONDS)
        return _pool_has_available_instance(name)

    def _select_pool_instance(name: str) -> str | None:
        # Per-instance max_parallel capacity preference now lives
        # inside resolve_pool_executor() itself (the shared seam implement,
        # analyze, and review all route through) rather than duplicated here.
        instance = resolve_pool_executor(
            "implement",
            {},
            cfg,
            repo_root,
            pool_name=name,
            running_counts=pool_running,
            rr_index=pool_rr_index,
            dispatch_counts=_pool_dispatch_counts,
        )
        if instance is None and _wait_for_pool_capacity(name):
            instance = resolve_pool_executor(
                "implement",
                {},
                cfg,
                repo_root,
                pool_name=name,
                running_counts=pool_running,
                rr_index=pool_rr_index,
                dispatch_counts=_pool_dispatch_counts,
            )
        return instance

    # Per-ticket count of sibling-retry attempts made so far *this
    # run* — bounds how many times a single ticket can be bounced to another
    # pool instance after a rate limit, so a misbehaving ticket can't cycle
    # through the whole pool in one run even if each individual attempt
    # superficially looks like progress.
    sibling_retry_counts: dict[str, int] = {}
    _MAX_SIBLING_RETRIES = int(cfg.get("max_sibling_retries", 1))

    def _decide_sibling_retry(
        tid: str, cooldown_instance: str, worktree_path: Path
    ) -> tuple[str | None, str]:
        """Decide whether a ticket that just hit a rate limit on
        *cooldown_instance* should be retried on a healthy sibling instance
        within this run, instead of hibernating.

        Returns (instance, reason) where instance is the sibling to retry on,
        or (None, reason) to fall back to today's hibernate-and-wait
        behavior. Must be called AFTER cooldown_instance's own cooldown file
        has been written, so `_pool_has_available_instance` /
        `_select_pool_instance` correctly exclude it from the candidate pool.
        """
        if not pool_name:
            return None, "no pool configured — nothing to retry on"
        attempts_used = sibling_retry_counts.get(tid, 0)
        if attempts_used >= _MAX_SIBLING_RETRIES:
            return None, (
                f"sibling-retry cap ({_MAX_SIBLING_RETRIES}) already used for "
                f"{tid} this run"
            )
        if not _ticket_has_real_progress(worktree_path):
            return None, (
                "no in-worktree progress (no commits ahead of main, no "
                "uncommitted changes) — more consistent with a stuck/looping "
                "session than real work, hibernating instead of risking a "
                "second pool instance's quota"
            )
        if not _pool_has_available_instance(pool_name):
            return None, "no healthy sibling instance available in this pool"
        sibling = _select_pool_instance(pool_name)
        if sibling == cooldown_instance:
            return None, "pool selection did not yield a distinct healthy sibling"
        return sibling, (
            "in-worktree progress detected (commits and/or uncommitted diff) "
            f"and sibling instance {sibling!r} is healthy"
        )

    # Set once any ticket's executor hits a rate/quota limit. A rate limit is a
    # global (account/quota-level) condition, so every *new* executor
    # invocation would hit the same wall and churn otherwise-fine tickets into
    # failed/hibernated. Once set, we stop pulling new work via next_batch()
    # (both the worker-pool refill and the outer loop) and let already-dispatched
    # tickets finish, then exit so resume-watch can wait out the limit.
    rate_limit_halt = False
    # Set when an executor exits because it received a signal (for example
    # SIGINT from Ctrl+C). Preserve active work and stop dispatching new
    # tickets; the next orchestrate run can resume from hibernated state.
    interrupt_halt = False
    # Set when an executor reports a hard setup/configuration error (for
    # example an unsupported model or stale Codex CLI). Retrying the rest of
    # the board would only churn unrelated tickets into failed.
    executor_setup_halt = False

    # Ticket ID -> pool-selected instance for the current dispatch. Deliberately
    # NOT persisted onto the ticket file: run_ticket reloads a fresh copy of
    # the ticket from disk after cmd_start (to pick up cmd_start's own
    # updates), which would discard an in-memory-only assignment anyway, and
    # writing an arbitrary named-instance string into ticket.executor trips a
    # pre-existing validate_ticket gap that quarantines the
    # ticket everywhere load_all_tickets is used. Passing the selection
    # through invoke_executor's executor_override instead avoids that
    # landmine entirely — it's a pure dispatch-time concern, never written to
    # the ticket's own frontmatter.
    pool_assignment: dict[str, str] = {}

    def assign_pool_instance(ticket: dict) -> str | None:
        """Pick a pool instance for `ticket`'s dispatch. No-op (returns None)
        when no pool is active or the ticket already carries an explicit
        `executor` override — pools only stand in for the same choice a user
        could make by hand, never overriding one."""
        nonlocal rate_limit_halt
        if not pool_name or ticket.get("executor"):
            return None
        instance = _select_pool_instance(pool_name)
        if instance is None:
            rate_limit_halt = True
            return None
        pool_assignment[ticket["id"]] = instance
        pool_running[instance] = pool_running.get(instance, 0) + 1
        if not dry_run:
            _pool_dispatch_counts[instance] = _pool_dispatch_counts.get(instance, 0) + 1
            _save_pool_state(
                repo_root,
                pool_name,
                pool_rr_index.get(pool_name, 0),
                _pool_dispatch_counts,
            )
        return instance

    def release_pool_instance(instance_name: str | None) -> None:
        if instance_name is None:
            return
        pool_running[instance_name] = max(0, pool_running.get(instance_name, 0) - 1)

    # Initialize git operations lock for worker pool mode (F3 fix).
    # When max_parallel > 1, serialize all git operations to prevent concurrent
    # merge/commit interference on the shared primary checkout.
    if max_parallel > 1:
        import lanegate.lifecycle as lifecycle_module
        lifecycle_module._GIT_OPS_LOCK = threading.Lock()
    else:
        # Single-threaded mode: no lock needed.
        import lanegate.lifecycle as lifecycle_module
        lifecycle_module._GIT_OPS_LOCK = None

    def auto_merge_approved_local_tickets(tickets: list[dict]) -> bool:
        """Merge local approved tickets when there is no configured human gate."""
        if dry_run or human_review != "none":
            return False
        merged_any = False
        for ticket in tickets:
            if milestone is not None and ticket.get("milestone") != milestone:
                continue
            if ticket.get("status") != "in_review":
                continue
            if ticket.get("review_verdict") != "approved":
                continue
            if ticket.get("pr_number"):
                continue
            # A completed rebase only proves Git accepted the result.  If an
            # agent resolved any conflict hunk, preserve the normal automated
            # review but require a human to make the final merge decision.
            if ticket.get("requires_human_merge"):
                files = ", ".join(ticket.get("rebase_conflict_files") or [])
                detail = f" ({files})" if files else ""
                print(
                    f"[orchestrate] {ticket['id']}: automated rebase conflict recovery"
                    f" requires human merge approval{detail}",
                    file=sys.stderr,
                )
                continue
            # Supervised/manual autonomy permit the normal LLM review/fix
            # cycle, but an approved result still requires an explicit human
            # merge. This check also covers tickets approved during a prior
            # run (e.g. via `lanegate fix`), which reach this board-clear scan
            # without passing a worker.
            if not is_auto_fix_lane(resolve_autonomy(cfg, ticket)):
                continue

            # A ticket may reach this shared merge path after a prior run or a
            # review-pending resume, neither of which necessarily traversed
            # run_ticket's post-implementation risk-lane gate.  Re-scan the
            # current branch diff here so an approved verdict never bypasses
            # the human escalation required for red-lane changes.
            merge_worktree = (
                Path(ticket["worktree"])
                if ticket.get("worktree")
                else worktree_path(worktrees_dir, ticket["id"])
            )
            merge_trunk = resolve_trunk_branch(cfg, repo_root)
            diff_capture = _git_text(
                ["git", "diff", f"{merge_trunk}...HEAD"], merge_worktree
            )
            risk_lane = scan_risk_lane(
                diff_capture.text if diff_capture.ok else "", ticket
            )
            red_signal_triggered = risk_lane_requires_human_review(
                risk_lane, resolve_human_escalation(cfg)
            )
            if red_signal_triggered:
                approved_sha = ticket.get("red_lane_approved_at_sha")
                if approved_sha:
                    head_capture = _git_text(
                        ["git", "rev-parse", "HEAD"], merge_worktree
                    )
                    if head_capture.ok and head_capture.text.strip() == approved_sha:
                        red_signal_triggered = False
            if red_signal_triggered:
                reason = (
                    "red-lane escalation: diff scan classified this change as "
                    "'red' (external credentials, security-sensitive, or irreversible "
                    "operation detected) — human review required"
                )
                print(f"[orchestrate] {ticket['id']}: WARNING — {reason}", file=sys.stderr)
                pause_for_needs_review(ticket["id"], reason)
                continue
            if verbose:
                print(f"[orchestrate] auto-merging {ticket['id']} (approved, no human gate)")
            try:
                with suppress_direct_action_tracking():
                    cmd_merge(ticket["id"], cfg, repo_root)
                _log_outcome(ticket["id"], "merged")
                merged_any = True
            except (Exception, SystemExit) as exc:
                tid = ticket["id"]
                # A merge helper can raise after it has already recorded the
                # durable merged status (for example, a later push/cleanup
                # failure).  Never regress that completed ticket merely
                # because its caller did not return normally.
                latest, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
                latest_ticket = next((item for item in latest if item["id"] == tid), None)
                if latest_ticket and latest_ticket.get("status") == "merged":
                    print(
                        f"[orchestrate] {tid}: merged before post-merge error: {exc}; "
                        "skipping needs_review regression for terminal status",
                        file=sys.stderr,
                    )
                    _log_outcome(tid, "merged")
                    merged_any = True
                    continue
                reason = f"auto-merge failed: {exc}"
                print(f"[orchestrate] {tid} merge failed — downgrading to needs_review", file=sys.stderr)
                downgrade_approved_review_to_needs_review(ticket, reason)
                _log_outcome(tid, "needs_review", reason=reason)
        return merged_any

    def append_or_replace_section(ticket: dict, header: str, text: str) -> None:
        body = ticket.get("_body", "")
        if header in body:
            pre, _, rest = body.partition(header)
            after = rest.lstrip("\n")
            next_heading = after.find("\n##")
            replacement = pre.rstrip() + f"\n\n{header}\n\n{text.strip()}\n"
            ticket["_body"] = replacement if next_heading == -1 else replacement + after[next_heading:]
            return
        ticket["_body"] = body.rstrip() + f"\n\n{header}\n\n{text.strip()}\n"

    def downgrade_approved_review_to_needs_review(ticket: dict, reason: str) -> None:
        _clear_human_review_approval(ticket)
        ticket["status"] = "needs_review"
        ticket["review_verdict"] = "changes_requested"
        ticket["review_summary"] = "blocked by orchestrate gate"
        ticket["status_changed_at"] = _utc_now_iso()
        append_or_replace_section(ticket, "## Needs Review Reason", reason)
        write_ticket(ticket)
        _commit_generated_ticket_write(
            repo_root,
            Path(ticket["_path"]),
            ticket["id"],
            "needs_review",
            cfg,
        )
        _remove_executor_markers(repo_root, ticket["id"])

    def _force_needs_review_write(ticket: dict, reason: str) -> None:
        """Directly write status=needs_review, bypassing cmd_needs_review's
        in_progress-only guard (which sys.exit(1)s on any other status —
        an escaping SystemExit would abort the rest of the batch). Used for
        tickets whose status doesn't match any of the CLI-guarded
        transitions, e.g. a crash before cmd_start's own status write landed.
        """
        _clear_human_review_approval(ticket)
        ticket["status"] = "needs_review"
        ticket["status_changed_at"] = _utc_now_iso()
        append_or_replace_section(ticket, "## Needs Review Reason", reason)
        write_ticket(ticket)
        _commit_generated_ticket_write(
            repo_root,
            Path(ticket["_path"]),
            ticket["id"],
            "needs_review",
            cfg,
        )
        _remove_executor_markers(repo_root, ticket["id"])

    def pause_for_needs_review(tid: str, reason: str) -> None:
        all_tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
        current_ticket = next((t for t in all_tickets if t["id"] == tid), None)
        current = current_ticket.get("status") if current_ticket else None
        if current == "in_progress":
            if not verbose:
                _status(tid, "needs_review", orig_out, _log_f)
            cmd_needs_review(tid, cfg, repo_root, reason=reason)
            _log_outcome(tid, "needs_review", reason=reason)
            return
        if current == "code_complete":
            # A failed gate must release the code_complete lock.  That status
            # can mean either a combined executor exited before recording a
            # review verdict or an auto-fix/re-review exhausted its bounded
            # retry budget.  Leaving it unchanged would requeue or retry it
            # indefinitely and block every overlapping open ticket.
            print(
                f"[orchestrate] {tid}: forcing needs_review from code_complete "
                f"after a failed gate: {reason}",
                file=sys.stderr,
            )
            if not verbose:
                _status(tid, "needs_review", orig_out, _log_f)
            assert current_ticket is not None  # current == "code_complete" implies current_ticket was found above
            _force_needs_review_write(current_ticket, reason)
            _log_outcome(tid, "needs_review", reason=reason)
            return
        if current in ("in_review", "needs_review"):
            if (
                current == "in_review"
                and current_ticket is not None
                and current_ticket.get("review_verdict") == "approved"
            ):
                downgrade_approved_review_to_needs_review(current_ticket, reason)
                current = "needs_review"
                print(
                    f"[orchestrate] {tid}: approved review invalidated by gate; "
                    f"moved to needs_review: {reason}",
                    file=sys.stderr,
                )
                if not verbose:
                    _status(tid, "needs_review", orig_out, _log_f)
                _log_outcome(tid, "needs_review", reason=reason)
                return
            if current_ticket is not None:
                append_or_replace_section(current_ticket, "## Needs Review Reason", reason)
                write_ticket(current_ticket)
                _commit_generated_ticket_write(
                    repo_root,
                    Path(current_ticket["_path"]),
                    tid,
                    "needs_review",
                    cfg,
                )
            print(
                f"[orchestrate] {tid}: review gate already active "
                f"(status={current}); warning preserved in log: {reason}",
                file=sys.stderr,
            )
            if not verbose:
                _status(tid, "review_pending", orig_out, _log_f)
            _log_outcome(tid, "review_pending", reason=reason)
            return
        if current_ticket is None:
            print(
                f"[orchestrate] {tid}: cannot mark needs_review — ticket not found: {reason}",
                file=sys.stderr,
            )
            _log_outcome(tid, "error", reason=f"ticket not found: {reason}")
            return
        # Any other status (open, hibernated, failed, merged, ...) —
        # cmd_needs_review only accepts in_progress and would sys.exit(1)
        # here, crashing the whole run over a single ticket's edge case.
        # However, do not regress terminal statuses (merged, validated, done,
        # failed) — a crash after successful merge should not undo the merge.
        if current in TERMINAL_STATUSES:
            print(
                f"[orchestrate] {tid}: skipping needs_review regression — already in terminal status "
                f"'{current}'; error context: {reason}",
                file=sys.stderr,
            )
            return
        if not verbose:
            _status(tid, "needs_review", orig_out, _log_f)
        print(
            f"[orchestrate] {tid}: forcing needs_review from unexpected status "
            f"'{current}': {reason}",
            file=sys.stderr,
        )
        _force_needs_review_write(current_ticket, reason)
        _log_outcome(tid, "needs_review", reason=reason)

    all_paused_tickets: list[str] = []
    # A code_complete result produced by this run is not an orphaned review.
    # The worker that just handled it owns its completion/review outcome; do
    # not requeue it as a fresh review on a later scheduler iteration.
    handled_ticket_ids: set[str] = set()

    # Scope drift is normally handled in the same worker pass.  If an older
    # run paused solely for that reason, reclaim it before selecting new work
    # so it can proceed through the same review/auto-merge flow instead of
    # requiring a manual claim-file + reopen + review sequence.
    recovered_scope_tickets = recover_scope_only_needs_review_tickets(
        cfg,
        repo_root,
        milestone=milestone,
        ticket_ids=ticket_ids,
        dry_run=dry_run,
    )
    if recovered_scope_tickets:
        print(
            "[orchestrate] scope-recovery candidates: "
            + ", ".join(recovered_scope_tickets),
            file=sys.stderr,
        )

    while True:
        if not dry_run:
            reconciled = _reconcile_stale_executor_markers(
                cfg, repo_root, out_stream=sys.stderr, session_ts=session_ts
            )
            if reconciled is not None:
                break

            reset_cooldowns = _auto_reset_elapsed_executor_cooldowns(cfg, repo_root)
            if reset_cooldowns:
                print(
                    "[orchestrate] reset elapsed executor cooldown(s): "
                    + ", ".join(reset_cooldowns),
                    file=sys.stderr,
                )

        # Approved, ready-to-merge tickets always drain ahead of dispatching
        # new open work -- otherwise WIP (and the touch-locks it holds) keeps
        # growing while already-finished tickets sit waiting, which is
        # exactly the situation that produces avoidable lock contention.
        if not dry_run:
            all_tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
            if auto_merge_approved_local_tickets(all_tickets):
                continue

        batch = next_batch(cfg, repo_root, milestone=milestone, ticket_ids=ticket_ids)

        # Reviews are terminal-path work: do not spend fresh model capacity
        # analyzing drafts while completed changes are waiting for their first
        # independent verdict. Startup recovery normally queues these, but the
        # live loop must make the same guarantee after any state transition.
        if not batch and not dry_run and recover:
            queued_reviews = _queue_code_complete_reviews(
                cfg,
                repo_root,
                milestone=milestone,
                ticket_ids=ticket_ids,
                exclude_ticket_ids=handled_ticket_ids,
            )
            if queued_reviews:
                print(
                    "[orchestrate] queued code-complete ticket(s) for review before "
                    f"draft analysis: {', '.join(queued_reviews)}",
                    file=sys.stderr,
                )
                batch = next_batch(cfg, repo_root, milestone=milestone, ticket_ids=ticket_ids)

        # Ready open/hibernated work always dispatches ahead of newly-created
        # drafts. Analyze only after selecting that work, and only when it
        # leaves spare batch capacity, so drafts cannot displace the backlog.
        if auto_analyze and len(batch) < max_parallel:
            if dry_run:
                _print_draft_analysis_plan(
                    cfg, repo_root, milestone=milestone, tickets_dir=tickets_dir, ticket_ids=ticket_ids
                )
            else:
                if _analyze_drafts(
                    cfg, repo_root, milestone=milestone, tickets_dir=tickets_dir,
                    ticket_ids=ticket_ids, pool_name=pool_name, session_ts=session_ts,
                ) is True:
                    interrupt_halt = True
                    break
                # Re-select only to pick up newly-analyzed work. Preserve the
                # selection that existed before analysis: a draft can become
                # the greedy, non-parallel-safe head of the refreshed query,
                # but it must not displace ready work already selected for
                # this dispatch. Only add safe, non-overlapping candidates.
                refreshed = next_batch(cfg, repo_root, milestone=milestone, ticket_ids=ticket_ids)
                if not batch:
                    batch = refreshed
                elif all(ticket.get("parallel_safe") for ticket in batch):
                    selected_ids = {ticket["id"] for ticket in batch}
                    selected_touches = {
                        touch for ticket in batch for touch in ticket.get("touches") or []
                    }
                    for ticket in refreshed:
                        ticket_touches = set(ticket.get("touches") or [])
                        if (
                            len(batch) >= max_parallel
                            or ticket["id"] in selected_ids
                            or not ticket.get("parallel_safe")
                            or touches_overlap(ticket_touches, selected_touches)
                        ):
                            continue
                        batch.append(ticket)
                        selected_ids.add(ticket["id"])
                        selected_touches.update(ticket_touches)

        if not batch:
            all_tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
            if auto_merge_approved_local_tickets(all_tickets):
                continue

            in_review_with_pr = [
                t for t in all_tickets if t.get("status") == "in_review" and t.get("pr_number")
            ]

            # Diagnose why next_batch returned empty.
            open_in_milestone = [
                t
                for t in all_tickets
                if t.get("status") in ("open", "hibernated")
                and (milestone is None or t.get("milestone") == milestone)
                and (ticket_ids is None or t["id"] in ticket_ids)
            ]
            if open_in_milestone:
                # There are open tickets but none passed next_batch — explain why.
                lock_statuses = cfg.get("lock_statuses", [])
                hollow_ids = {h["id"] for h in hollow_lock_holders(all_tickets, lock_statuses, repo_root)}
                holder_status: dict[str, str] = {}
                holder_by_id: dict[str, dict] = {}
                trusted_holders: list[dict] = []
                for holder in all_tickets:
                    if holder.get("status") not in lock_statuses:
                        continue
                    holder_status[holder["id"]] = holder.get("status", "")
                    holder_by_id[holder["id"]] = holder
                    if holder["id"] in hollow_ids:
                        # Claims a lock-holding status but its branch has no
                        # real commits ahead of main — do not let it block
                        # the rest of the board; surfaced separately below.
                        continue
                    trusted_holders.append(holder)

                status_map = {t["id"]: t.get("status") for t in all_tickets}
                terminal = TERMINAL_STATUSES
                blocked_by_lock: list[tuple[str, list[str]]] = []
                blocked_by_deps: list[tuple[str, list[str]]] = []
                for t in open_in_milestone:
                    deps_unmet = [
                        dep
                        for dep in (t.get("depends_on") or [])
                        if status_map.get(dep) not in terminal
                    ]
                    lock_holders = [
                        f"{holder['id']} ({holder_status.get(holder['id'], '?')})"
                        for holder in trusted_holders
                        if touches_overlap(t.get("touches") or [], holder.get("touches") or [])
                    ]
                    if lock_holders:
                        blocked_by_lock.append((t["id"], lock_holders))
                    elif deps_unmet:
                        blocked_by_deps.append((t["id"], deps_unmet))

                print(
                    f"[orchestrate] {len(open_in_milestone)} open ticket(s) blocked — none eligible this pass:"
                )
                for tid_b, holders in blocked_by_lock:
                    print(f"  [lock]  {tid_b} — file lock held by: {', '.join(holders)}")
                for tid_b, deps in blocked_by_deps:
                    dep_detail = ", ".join(f"{d} ({status_map.get(d, 'unknown')})" for d in deps)
                    print(f"  [deps]  {tid_b} — waiting on: {dep_detail}")
                if not blocked_by_lock and not blocked_by_deps:
                    print("  (unknown reason — check touches and depends_on in your ticket files)")

                if hollow_ids:
                    print("\n  Ignoring hollow lock holder(s) (status claims completion, but the")
                    print("  branch has no commits ahead of main — likely a hand-edited status):")
                    for holder_id in sorted(hollow_ids):
                        holder = holder_by_id.get(holder_id, {})
                        print(
                            f"    {holder_id} ({holder.get('status', '?')}): inspect "
                            f"{holder.get('worktree') or '(no worktree recorded)'} — lanegate reopen "
                            "does not yet accept this status, so correct its status field by hand "
                            "(e.g. back to open) once you've confirmed no real work exists"
                        )

                if blocked_by_lock:
                    holders_all = sorted({h.split()[0] for _, hs in blocked_by_lock for h in hs})
                    print("\n  To unblock lock-holding ticket(s):")
                    for holder_id in holders_all:
                        holder_step = _ticket_next_step_line(holder_by_id.get(holder_id, {}))
                        if holder_step:
                            print(f"  {holder_step}")
                        else:
                            print(f"  {holder_id}: inspect ticket status, then rerun: lanegate run")
                if blocked_by_deps:
                    dep_holders = sorted({d for _, ds in blocked_by_deps for d in ds})
                    statuses_needed = [
                        f"{d} is currently {status_map.get(d, 'unknown')}" for d in dep_holders
                    ]
                    print(
                        f"\n  To unblock: complete the dependency ticket(s) first.\n"
                        f"  {'; '.join(statuses_needed)}"
                    )
            else:
                if milestone_excluded_ticket_scope:
                    print(
                        "[orchestrate] no scoped tickets match the active milestone filter "
                        "— see warning above"
                    )
                else:
                    scope_desc = "in this ticket scope" if ticket_ids else "in this milestone"
                    print(f"[orchestrate] board clear — no more open tickets {scope_desc}")

            _print_review_queue(all_tickets, milestone=milestone, stream=orig_out)
            _print_continuation_steps(all_tickets, milestone=milestone, stream=orig_out)

            if in_review_with_pr:
                for t in in_review_with_pr:
                    verdict = t.get("review_verdict") or "pending"
                    print(
                        f"[orchestrate] {t['id']} is in_review (PR #{t['pr_number']}, verdict: {verdict})"
                        f" — merge the PR or run: lanegate merge {t['id']}"
                    )
                if dry_run:
                    print("[dry-run] would spawn: lanegate watch (polls for PR merge)")
                else:
                    spawn_watch_daemon(repo_root)
            break

        if all_paused_tickets:
            paused_ids = set(all_paused_tickets)
            batch = [t for t in batch if t["id"] not in paused_ids]
            if not batch:
                # next_batch() does not remember tickets that paused during
                # this invocation.  A high-priority hibernated ticket can
                # therefore keep winning its greedy selection and exclude
                # touch-conflicting peers, even though we subsequently drop
                # it from the batch above.  Query again with a ticket scope
                # that excludes this run's paused IDs so those peers get a
                # chance to form their own parallel-safe batch.
                if ticket_ids is None:
                    all_tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
                    candidate_ticket_ids = {t["id"] for t in all_tickets} - paused_ids
                else:
                    candidate_ticket_ids = ticket_ids - paused_ids
                batch = next_batch(
                    cfg,
                    repo_root,
                    milestone=milestone,
                    ticket_ids=candidate_ticket_ids,
                )
                if not batch:
                    print(
                        f"[orchestrate] {len(paused_ids)} ticket(s) paused during this run; "
                        "no dispatchable candidates remain after excluding tickets that "
                        "already failed or paused this session — stopping"
                    )
                    break

        work_items = batch[:max_parallel]

        # --- batch summary line (compact or verbose, always shown) ---
        all_tickets_for_count, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
        total_open = sum(
            1
            for t in all_tickets_for_count
            if t.get("status") in ("open", "hibernated")
            and (milestone is None or t.get("milestone") == milestone)
        )
        n_running = len(work_items)
        n_peers = total_open - n_running
        batch_line = (
            f"[orchestrate] batch: {n_running} running of cap {max_parallel}, {n_peers} peers"
            f" ({total_open} open tickets total)\n"
        )
        orig_out.write(batch_line)
        orig_out.flush()
        if _log_f is not None:
            _log_f.write(batch_line)
            _log_f.flush()
        underfilled_reason = _underfilled_batch_reason(
            cfg, repo_root, work_items, max_parallel, milestone=milestone
        )
        if underfilled_reason:
            underfilled_line = f"[orchestrate] batch under-filled: {underfilled_reason}\n"
            orig_out.write(underfilled_line)
            orig_out.flush()
            if _log_f is not None:
                _log_f.write(underfilled_line)
                _log_f.flush()
        write_batch_status(
            repo_root,
            batch_line.strip(),
            underfilled_reason,
            max_parallel=max_parallel,
            total_open=total_open,
        )

        def run_ticket(ticket: dict) -> bool:
            # Worker-pool mode runs this in a ThreadPoolExecutor thread, which
            # does not inherit contextvars set by the submitting thread (unlike
            # asyncio). cmd_orchestrate's own suppress_direct_action_tracking()
            # wrap is therefore invisible here under max_parallel > 1, so this
            # nested call re-applies it in whichever thread actually executes.
            with suppress_direct_action_tracking():
                return _run_ticket_body(ticket)

        def _run_ticket_body(ticket: dict) -> bool:
            nonlocal executor_setup_halt, interrupt_halt, rate_limit_halt
            from lanegate.lifecycle import _commit_generated_ticket_write

            tid = ticket["id"]
            handled_ticket_ids.add(tid)

            if dry_run:
                _route = _resolve_driver_route(cfg, ticket)
                _branch = ticket.get("branch") or branch_name(tid)
                print(
                    f"[dry-run] would start {tid}  "
                    f"implement_executor={_route['implement']}  "
                    f"review_executor={_route['review']}  "
                    f"mode={_route['mode']}  branch={_branch}"
                )
                print(f"[dry-run] would invoke executor for {tid}")
                print(f"[dry-run] would complete {tid}")
                if human_review != "none":
                    print(f"[dry-run] would run review for {tid}")
                return False

            # A rejected review has a committed worktree and actionable
            # findings.  It must be resumable by a later `lanegate run`;
            # otherwise it keeps its file lock forever and can deadlock every
            # overlapping open ticket.  Do not use cmd_start here: that
            # command correctly refuses code_complete tickets because it is
            # an implementation claim, whereas this is a fix continuation.
            if (
                ticket.get("status") == "code_complete"
                and ticket.get("review_verdict") == "changes_requested"
            ):
                wt_value = ticket.get("worktree")
                wt = Path(wt_value) if wt_value else worktree_path(worktrees_dir, tid)
                if not wt.exists():
                    reason = (
                        "review requested changes but the preserved worktree is missing "
                        f"({wt})"
                    )
                    pause_for_needs_review(tid, reason)
                    return True

                if verbose:
                    print(f"[orchestrate] resuming rejected review for {tid}")
                else:
                    _status(tid, "auto-fixing", orig_out, _log_f)
                _log_dispatch(tid, "auto-fix", was_hibernated=False)

                fixed = run_auto_fix_cycle(ticket, cfg, repo_root, wt, pool_name=pool_name)
                if fixed is None:
                    # Rate limit during fix pass — ticket hibernated, no attempt consumed.
                    _log_outcome(tid, "hibernated", reason="rate limit during fix agent")
                    return True
                if not fixed:
                    _log_outcome(tid, "changes_requested", reason="review requested changes")
                    return True

                latest, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
                fixed_ticket = next((item for item in latest if item["id"] == tid), ticket)
                if human_review != "none" or not is_auto_fix_lane(resolve_autonomy(cfg, fixed_ticket)):
                    _log_outcome(tid, "awaiting_human_review")
                    return True
                auto_merge_approved_local_tickets([fixed_ticket])
                final_all, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
                final_ticket = next((item for item in final_all if item["id"] == tid), None)
                _log_outcome(tid, str(final_ticket.get("status")) if final_ticket else "unknown")
                return False

            # --- start ---
            if verbose:
                print(f"[orchestrate] starting {tid}")
            else:
                _status(tid, "starting", orig_out, _log_f)
            was_hibernated = ticket.get("status") == "hibernated"
            was_review_pending = was_hibernated and bool(ticket.get("review_pending"))
            # cmd_start reattaches the existing worktree (instead of creating a
            # fresh one) for both hibernated and needs_review tickets — the
            # rebase-onto-main trust check below must fire for the same set,
            # not just the rate-limit hibernation case, since a reattached
            # needs_review worktree can be just as stale relative to main.
            is_resuming_worktree = ticket.get("status") in ("hibernated", "needs_review")
            prior_notes = _collect_prior_notes(ticket, repo_root)
            try:
                if verbose or _log_f is None or max_parallel > 1:
                    cmd_start(tid, cfg, repo_root, interactive=False)
                else:
                    with contextlib.redirect_stdout(_log_f):
                        cmd_start(tid, cfg, repo_root, interactive=False)
            except (SystemExit, Exception) as exc:
                code_str = f"exit code {exc.code}" if isinstance(exc, SystemExit) else str(exc)
                err_msg = f"cmd_start failed: {code_str}"
                print(f"ERROR: [{tid}] {err_msg}", file=sys.stderr)
                pause_for_needs_review(tid, err_msg)
                return True

            # Re-load ticket to get fresh data (cmd_start updates it), then
            # resolve the worktree. Hibernated tickets may reattach to a
            # non-conventional worktree path stored in the ticket.
            all_t, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
            fresh_ticket = next((t for t in all_t if t["id"] == tid), ticket)

            dispatch_executor = pool_assignment.get(tid)
            if dispatch_executor is None:
                try:
                    dispatch_executor = _resolve_driver_route(cfg, fresh_ticket)["implement"]
                except Exception:
                    dispatch_executor = None
            _log_dispatch(tid, dispatch_executor, was_hibernated)

            # --- trust check + injection scan (before worktree setup) ---
            trust_block = fresh_ticket.get("trusted") is False
            injection_findings = _scan_injection_signals(fresh_ticket)

            if trust_block or injection_findings:
                parts = []
                if trust_block:
                    parts.append(f"untrusted source ({fresh_ticket.get('source', 'unknown')})")
                if injection_findings:
                    detail = "; ".join(injection_findings[:2])
                    if len(injection_findings) > 2:
                        detail += f" (+ {len(injection_findings) - 2} more)"
                    parts.append("injection signals: " + detail)
                reason = "pre-execution checks failed: " + " | ".join(parts)
                print(f"[orchestrate] {tid}: WARNING — {reason}", file=sys.stderr)
                if not verbose:
                    _status(tid, "needs_review", orig_out, _log_f)
                cmd_needs_review(tid, cfg, repo_root, reason=reason)
                _log_outcome(tid, "needs_review", reason=reason)
                return True

            acceptance_findings = _run_acceptance_contract_audit(fresh_ticket, repo_root, cfg)
            if acceptance_findings and resolve_acceptance_contract_mode(cfg) == "blocker":
                detail = "; ".join(acceptance_findings[:2])
                if len(acceptance_findings) > 2:
                    detail += f" (+ {len(acceptance_findings) - 2} more)"
                reason = "acceptance-contract audit failed: " + detail
                print(f"[orchestrate] {tid}: WARNING — {reason}", file=sys.stderr)
                if not verbose:
                    _status(tid, "needs_review", orig_out, _log_f)
                cmd_needs_review(tid, cfg, repo_root, reason=reason)
                _log_outcome(tid, "needs_review", reason=reason)
                return True
            elif acceptance_findings and verbose:
                # Advisory mode (default): findings are persisted on the ticket
                # (acceptance_contract_audit metadata) for the reviewer to weigh —
                # they do not by themselves block implementation.
                print(
                    f"[orchestrate] {tid}: acceptance-contract audit found "
                    f"{len(acceptance_findings)} finding(s) (advisory — continuing)"
                )

            wt = (
                Path(fresh_ticket["worktree"])
                if fresh_ticket.get("worktree")
                else worktree_path(worktrees_dir, tid)
            )

            resume_context = ""
            # A review-pending ticket was already verified before it was
            # hibernated.  Rebase can nevertheless change the commit that
            # review will inspect, so record whether this resume needs a new
            # pre-complete verification before dispatching that reviewer.
            pre_rebase_head = _git_head_sha(wt) if was_review_pending else None
            review_pending_rebase_changed = False
            if is_resuming_worktree and _worktree_is_dirty(wt):
                # A prior run (e.g. one interrupted by a rate limit or quota
                # error) left uncommitted changes. `git rebase` cannot run
                # against a dirty worktree, and that is not itself a resume
                # failure — skip the rebase and let the executor pick up and
                # commit its own pending work instead of force-failing the
                # ticket into needs_review.
                if verbose:
                    print(
                        f"[orchestrate] {tid}: worktree has uncommitted changes from a "
                        "prior run — skipping rebase-onto-main check"
                    )
                resume_context = (
                    "## Resuming with pending uncommitted changes\n\n"
                    "A previous run of this ticket left uncommitted changes in the "
                    "worktree (for example, it was interrupted by a rate limit or "
                    "quota error before it could commit). The rebase-onto-main "
                    "check was skipped because it cannot run against a dirty "
                    "worktree. Review the existing working-tree diff, finish the "
                    "remaining work, and commit it."
                )
            elif is_resuming_worktree:
                rebase_state, rebase_detail = _run_rebase(
                    wt, base=resolve_trunk_branch(cfg, repo_root)
                )
                if rebase_state == "conflict":
                    from lanegate.orchestrate.autofix import run_rebase_fix_agent

                    if run_rebase_fix_agent(
                        fresh_ticket,
                        cfg,
                        repo_root,
                        wt,
                        rebase_detail,
                        pool_name=pool_name,
                    ):
                        # A conflicted rebase necessarily incorporated an
                        # agent-selected resolution.  Do not trust the
                        # verification from before hibernation.
                        review_pending_rebase_changed = was_review_pending
                        print(
                            f"[orchestrate] {tid}: rebase conflict automatically resolved by fix agent",
                            file=sys.stderr,
                        )
                    else:
                        _abort_rebase(wt)
                        reason = (
                            f"rebase conflict resolution failed for {tid} "
                            "(reason_code: rebase_conflict_failed)"
                        )
                        print(f"ERROR: {reason} — marking needs_review", file=sys.stderr)
                        if not verbose:
                            _status(tid, "needs_review", orig_out, _log_f)
                        cmd_needs_review(tid, cfg, repo_root, reason=reason)
                        _log_outcome(tid, "needs_review", reason=reason)
                        return True
                elif (
                    rebase_state == "clean"
                    and was_review_pending
                    and pre_rebase_head is not None
                    and _git_head_sha(wt) != pre_rebase_head
                ):
                    # A clean rebase can also replay commits onto a newer
                    # base.  Only repeat verification when it actually
                    # changed the reviewed commit; an already up-to-date
                    # review-pending ticket keeps its existing evidence.
                    review_pending_rebase_changed = True
                elif rebase_state == "error":
                    reason = f"cannot run hibernated resume check: {rebase_detail}"
                    print(f"ERROR: {reason} for {tid} - marking needs_review", file=sys.stderr)
                    if not verbose:
                        _status(tid, "needs_review", orig_out, _log_f)
                    cmd_needs_review(tid, cfg, repo_root, reason=reason)
                    _log_outcome(tid, "needs_review", reason=reason)
                    return True
            fresh_ticket = _prepend_context(fresh_ticket, prior_notes, resume_context)

            # A rate-limited review has already passed implementation and every
            # pre-complete guard.  Reattach/rebase it like any hibernated
            # worktree, then resume at review -- never invoke an implementer
            # merely because cmd_start temporarily restored the lock state.
            if was_review_pending:
                from lanegate.lifecycle import resume_review_pending

                if review_pending_rebase_changed:
                    timed_out_guards: list[str] = []
                    try:
                        with safeguard_lock(repo_root, tid):
                            safeguards_passed, safeguard_reason = run_safeguards(
                                "pre_complete",
                                fresh_ticket,
                                cfg,
                                wt,
                                timed_out_guards=timed_out_guards,
                            )
                    except SafeguardLockHeld as exc:
                        safeguards_passed = False
                        safeguard_reason = f"pre_complete safeguards unavailable: {exc}"

                    if not safeguards_passed:
                        reason = (
                            "review-pending post-rebase pre_complete safeguards failed: "
                            f"{safeguard_reason}"
                        )
                        print(f"ERROR: {reason} — marking needs_review", file=sys.stderr)
                        if not verbose:
                            _status(tid, "needs_review", orig_out, _log_f)
                        cmd_needs_review(tid, cfg, repo_root, reason=reason)
                        _log_outcome(tid, "needs_review", reason=reason)
                        return True

                    verified_sha = _git_head_sha(wt)
                    if verified_sha:
                        # Update pre_complete_verified_sha on disk without
                        # persisting fresh_ticket's transient prepended context body.
                        from lanegate.ticket import parse_ticket, write_ticket

                        tpath = Path(fresh_ticket["_path"])
                        disk_ticket = parse_ticket(tpath) or fresh_ticket
                        disk_ticket["pre_complete_verified_sha"] = verified_sha
                        write_ticket(disk_ticket)
                        fresh_ticket["pre_complete_verified_sha"] = verified_sha
                        _commit_generated_ticket_write(
                            repo_root, tpath, tid, "update pre_complete_verified_sha after rebase", cfg
                        )

                # cmd_start above set status to in_progress like any other
                # resume -- restore code_complete before the reviewer runs, or
                # cmd_review's guard silently rejects the verdict write below
                # (see resume_review_pending's docstring for the full chain).
                resume_review_pending(fresh_ticket, cfg, repo_root)
                rotation_enabled = (
                    bool(cfg.get("reviewer_rotation"))
                    and not ((cfg.get("steps") or {}).get("review") or {}).get("driver")
                    and not fresh_ticket.get("reviewer")
                )
                review_executor = "reviewer_rotation" if rotation_enabled else resolve_driver("review", fresh_ticket, cfg)
                if review_executor in ("none", "auto-none"):
                    _invoke_cmd_review(
                        _cmd_review, tid, cfg, repo_root, verdict="approved",
                        summary="auto-approved after review-pending resume",
                        review_driver="auto-none", review_model="none",
                    )
                elif review_executor == "human":
                    from lanegate.lifecycle import mark_review_pending

                    latest, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
                    pending = next((item for item in latest if item["id"] == tid), fresh_ticket)
                    mark_review_pending(
                        pending, cfg, repo_root,
                        reason="Review pending: configured reviewer is human.",
                    )
                    _log_outcome(tid, "review_pending", reason="awaiting human review")
                    return True
                else:
                    approved = run_review_agent(
                        fresh_ticket, repo_root, worktree_path=wt, cfg=cfg, pool_name=pool_name
                    )
                    latest, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
                    resumed = next((item for item in latest if item["id"] == tid), fresh_ticket)
                    if resumed.get("status") == "hibernated" and resumed.get("review_pending"):
                        _log_outcome(tid, "review_pending", reason=resumed.get("review_pending_reason"))
                        return True
                    if not approved:
                        if resumed.get("status") == "in_review":
                            # run_review_agent fell back to a pool-resolved
                            # human reviewer (cmd_review(verdict=None)) —
                            # correctly parked awaiting a human verdict, not
                            # an escalation.
                            _log_outcome(tid, "awaiting_human_review")
                            return True
                        if resumed.get("status") != "code_complete":
                            # run_review_agent already moved the ticket to a
                            # terminal state on its own (e.g. needs_review via
                            # no independent reviewer being available) — see
                            # the identical guard in the split-mode dispatch
                            # branch above for the full rationale.
                            _log_outcome(tid, "needs_review")
                            return True
                        # A substantive rejection still takes the normal fix
                        # path; only the harness/rate-limit path above skips it.
                        _fix_result = run_auto_fix_cycle(resumed, cfg, repo_root, wt, pool_name=pool_name)
                        if _fix_result is None:
                            # Rate limit during fix pass — ticket hibernated, no attempt consumed.
                            _log_outcome(tid, "hibernated", reason="rate limit during fix agent")
                            return True
                        if not _fix_result:
                            pause_for_needs_review(
                                tid,
                                "auto-fix/re-review did not reach approval; "
                                "human intervention is required",
                            )
                            return True

                latest, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
                resumed = next((item for item in latest if item["id"] == tid), fresh_ticket)
                if human_review != "none" or not is_auto_fix_lane(resolve_autonomy(cfg, resumed)):
                    _log_outcome(tid, "awaiting_human_review")
                    return True
                auto_merge_approved_local_tickets([resumed])
                final_all, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
                final_ticket = next((t for t in final_all if t["id"] == tid), None)
                final_status = str(final_ticket.get("status")) if final_ticket and final_ticket.get("status") else "unknown"
                _log_outcome(tid, final_status)
                return False

            # --- invoke executor (combined or split mode) ---
            # Combined mode: implement and review resolve to the same executor.
            # A single subprocess receives the merged implement+review prompt and
            # is responsible for calling `lanegate complete` and `lanegate review --verdict`
            # internally.  The orchestrator skips those steps afterwards.
            # Split mode: unchanged — a separate review subprocess is spawned.
            combined = _is_combined_mode(cfg, fresh_ticket, repo_root, implementer=dispatch_executor)
            if combined and human_review == "per_ticket":
                print(
                    f"[orchestrate] WARNING: {tid} is in combined mode (implement and review "
                    f"use the same executor). The --human-review per_ticket flag has no effect — "
                    f"the executor self-reviews as part of its implementation prompt. "
                    f"Use --human-review final or final batch review instead.",
                    file=sys.stderr,
                )
            try:
                dispatch = resolve_dispatch(
                    fresh_ticket,
                    cfg,
                    executor_override=pool_assignment.get(tid),
                )
            except Exception:
                # Dispatch preserves its existing configuration-error handling
                # below; lifecycle reporting must not alter it.
                dispatch = {}
            write_executing_status(tid, dispatch, orig_out, _log_f)
            if verbose:
                mode_label = "combined" if combined else "split"
                print(f"[orchestrate] invoking executor for {tid} ({mode_label} mode)")
            # In compact mode, route executor output only to the log file (not terminal).
            exec_log_stream = _log_f if (not verbose and _log_f is not None) else None
            # Mirror heartbeat lines to the real terminal in compact mode so the user
            # can see that a long-running ticket is still making progress.
            exec_terminal_stream = orig_out if (not verbose and _log_f is not None) else None
            pool_instance = pool_assignment.get(tid)
            combined_prompt: str | None = None
            if combined:
                from lanegate.executor import build_implement_prompt

                impl_prompt = build_implement_prompt(fresh_ticket, project_root=wt, cfg=cfg)
                combined_prompt = _build_combined_prompt(
                    fresh_ticket,
                    impl_prompt,
                    resolve_trunk_branch(cfg, repo_root),
                )

            def _dispatch(instance: str | None) -> tuple[int, str, str]:
                return invoke_executor(
                    fresh_ticket,
                    cfg,
                    wt,
                    log_stream=exec_log_stream,
                    terminal_stream=exec_terminal_stream,
                    prompt_override=combined_prompt,
                    repo_root=repo_root,
                    executor_override=instance,
                )

            exit_code, captured_stdout, captured_stderr = _dispatch(pool_instance)
            cooldown_written_for: str | None = None

            # --- sibling-retry-on-rate-limit ---
            # A single-instance rate limit doesn't have to hibernate the
            # ticket outright: if the worktree shows real progress and a
            # healthy sibling pool instance exists, retry once on that
            # sibling within this run instead of paying the full hibernate/
            # wait cost. cooldown_instance is written here (before the
            # existing exit_code!=0 handling below) so the sibling selection
            # correctly excludes the instance that just hit the wall.
            if exit_code != 0 and _is_rate_limit(
                exit_code, wt, captured_stdout=captured_stdout, captured_stderr=captured_stderr
            ):
                cooldown_instance = pool_instance or _resolve_driver_route(
                    cfg, fresh_ticket
                )["implement"]
                cooldown_reason = _rate_limit_reason(
                    exit_code, wt, captured_stdout=captured_stdout, captured_stderr=captured_stderr
                )
                _write_executor_cooldown(
                    repo_root, cooldown_instance, cooldown_reason, retry_after=cooldown_reason
                )
                _append_run_event(
                    repo_root,
                    session_ts,
                    "executor_cooldown",
                    instance=cooldown_instance,
                    reason=cooldown_reason,
                    ticket_id=tid,
                )
                cooldown_written_for = cooldown_instance
                sibling_instance, decision_reason = _decide_sibling_retry(tid, cooldown_instance, wt)
                _append_run_event(
                    repo_root,
                    session_ts,
                    "sibling_retry_decision",
                    ticket_id=tid,
                    from_instance=cooldown_instance,
                    to_instance=sibling_instance,
                    retried=bool(sibling_instance),
                    reason=decision_reason,
                )
                if sibling_instance:
                    sibling_retry_counts[tid] = sibling_retry_counts.get(tid, 0) + 1
                    print(
                        f"[orchestrate] {tid}: {cooldown_instance} hit a rate limit — "
                        f"retrying on sibling {sibling_instance!r} instead of hibernating "
                        f"({decision_reason})",
                        file=sys.stderr,
                    )
                    pool_assignment[tid] = sibling_instance
                    pool_instance = sibling_instance
                    exit_code, captured_stdout, captured_stderr = _dispatch(sibling_instance)
                else:
                    print(
                        f"[orchestrate] {tid}: {cooldown_instance} hit a rate limit — "
                        f"not retrying on a sibling ({decision_reason})",
                        file=sys.stderr,
                    )
            if exit_code != 0:
                if _is_rate_limit(
                    exit_code, wt, captured_stdout=captured_stdout, captured_stderr=captured_stderr
                ):
                    reason = _rate_limit_reason(
                        exit_code, wt, captured_stdout=captured_stdout, captured_stderr=captured_stderr
                    )
                    # Mark the actual instance that hit the wall cooling down
                    # — pool_instance when pool dispatch picked
                    # one, else whatever single executor this ticket resolved
                    # to. Structured file state under .lanegate/executors/ so
                    # `lanegate executor status`/`reset` and pool dispatch can
                    # both see it without scraping ticket bodies.
                    cooldown_instance = pool_instance or _resolve_driver_route(
                        cfg, fresh_ticket
                    )["implement"]
                    if cooldown_instance != cooldown_written_for:
                        # Not already written by the sibling-retry decision
                        # above — either no retry was attempted, or
                        # the sibling itself also just hit its own limit.
                        _write_executor_cooldown(
                            repo_root, cooldown_instance, reason, retry_after=reason
                        )
                        _append_run_event(
                            repo_root,
                            session_ts,
                            "executor_cooldown",
                            instance=cooldown_instance,
                            reason=reason,
                            ticket_id=tid,
                        )
                    if pool_instance:
                        # Recorded in the hibernation body (not ticket
                        # frontmatter — see the note on pool_assignment above)
                        # so a later run's pool selection can tell which
                        # instance is currently exhausted and route around it.
                        reason = f"{reason}\n\npool instance: {pool_instance}"
                    _checkpoint_before_hibernate(repo_root, wt, tid, "rate_limit")
                    if cfg.get("on_rate_limit") == "resume":
                        print(
                            f"[orchestrate] {tid}: rate limit hit — work preserved, hibernating.\n"
                            f"  on_rate_limit=resume — spawning background watcher to resume automatically.",
                            file=sys.stderr,
                        )
                    else:
                        print(
                            f"[orchestrate] {tid}: rate limit hit — work preserved, hibernating for resume.\n"
                            f"  Check your API quota/billing, then re-run: lanegate run",
                            file=sys.stderr,
                        )
                    if not verbose:
                        _status(tid, "hibernated", orig_out, _log_f)
                    cmd_hibernate(tid, cfg, repo_root, reason=reason)
                    _log_outcome(tid, "hibernated", reason=reason)
                    if cfg.get("on_rate_limit") == "resume":
                        spawn_resume_watch_daemon(repo_root)
                    if pool_name and _pool_has_available_instance(pool_name):
                        # Another pool instance still has capacity — this is a
                        # single-instance quota hit, not a global one. Don't
                        # halt the run; the refill loop routes the next
                        # ticket to whichever instance is still available.
                        print(
                            f"[orchestrate] {tid}: {cooldown_instance} cooling down — "
                            "other pool instance(s) still available, continuing",
                            file=sys.stderr,
                        )
                    elif cfg.get("on_rate_limit") == "resume":
                        # The ticket is already hibernated and resume-watch is
                        # responsible for waiting out the limit. Do not keep
                        # the foreground orchestrator parked in the cooldown
                        # poll loop, where Ctrl+C creates another interruption
                        # path for no useful work.
                        rate_limit_halt = True
                    elif pool_name and _wait_for_pool_capacity(pool_name):
                        # Every instance was cooling down; waited (bounded)
                        # until at least one became available again rather
                        # than halting the run outright.
                        pass
                    else:
                        # Signal the loop to stop dispatching new tickets. Other
                        # in-flight tickets are left to finish (they will each
                        # hibernate on the same limit), but no fresh work is pulled.
                        rate_limit_halt = True
                    return True
                watchdog_reason = _watchdog_termination_reason(captured_stderr)
                if watchdog_reason:
                    # A watchdog kill (idle/stall/ceiling/timeout, or an orchestrator
                    # suspend gap) is a per-ticket event: preserve any uncommitted work
                    # and hibernate this ticket for retry, but do NOT halt the run —
                    # the other in-flight and queued tickets are independent.
                    if _is_suspend_gap(captured_stdout, captured_stderr):
                        watchdog_reason = (
                            "executor watchdog resumed after an apparent orchestrator "
                            "suspend gap; preserving work for retry rather than treating "
                            "this as a genuine executor timeout"
                        )
                    _checkpoint_before_hibernate(repo_root, wt, tid, "watchdog")
                    print(
                        f"[orchestrate] {tid}: {watchdog_reason} — work preserved, hibernating.\n"
                        f"  Re-run when ready: lanegate run",
                        file=sys.stderr,
                    )
                    if not verbose:
                        _status(tid, "hibernated", orig_out, _log_f)
                    cmd_hibernate(tid, cfg, repo_root, reason=watchdog_reason)
                    _log_outcome(tid, "hibernated", reason=watchdog_reason)
                    return True

                if _is_interrupted_exit(exit_code):
                    reason = _interrupted_exit_reason(exit_code)
                    interrupt_halt = True
                    _checkpoint_before_hibernate(repo_root, wt, tid, "interrupt")
                    print(
                        f"[orchestrate] {tid}: {reason} — work preserved, hibernating.\n"
                        f"  Re-run when ready: lanegate run",
                        file=sys.stderr,
                    )
                    if not verbose:
                        _status(tid, "hibernated", orig_out, _log_f)
                    cmd_hibernate(tid, cfg, repo_root, reason=reason)
                    _log_outcome(tid, "hibernated", reason=reason)
                    return True
                if _is_auth_error(
                    exit_code, wt, captured_stdout=captured_stdout, captured_stderr=captured_stderr
                ):
                    reason = _auth_error_reason(
                        exit_code,
                        wt,
                        captured_stdout=captured_stdout,
                        captured_stderr=captured_stderr,
                    )
                    cooldown_instance = pool_instance or _resolve_driver_route(
                        cfg, fresh_ticket
                    )["implement"]
                    _write_executor_cooldown(repo_root, cooldown_instance, reason)
                    if pool_instance:
                        reason = f"{reason}\n\npool instance: {pool_instance}"
                    _checkpoint_before_hibernate(repo_root, wt, tid, "auth_error")
                    print(
                        f"[orchestrate] {tid}: executor requires re-authentication — "
                        "work preserved, hibernating.\n"
                        "  Re-authenticate the CLI (e.g. run it once interactively to "
                        "complete the login flow), then re-run: lanegate run",
                        file=sys.stderr,
                    )
                    if not verbose:
                        _status(tid, "hibernated", orig_out, _log_f)
                    cmd_hibernate(tid, cfg, repo_root, reason=reason)
                    _log_outcome(tid, "hibernated", reason=reason)
                    if not (pool_name and _pool_has_available_instance(pool_name)):
                        executor_setup_halt = True
                    return True
                if _is_executor_setup_error(
                    exit_code, wt, captured_stdout=captured_stdout, captured_stderr=captured_stderr
                ):
                    reason = _executor_setup_error_reason(
                        exit_code,
                        wt,
                        captured_stdout=captured_stdout,
                        captured_stderr=captured_stderr,
                    )
                    if pool_instance:
                        reason = f"{reason}\n\npool instance: {pool_instance}"
                    executor_setup_halt = True
                    _checkpoint_before_hibernate(repo_root, wt, tid, "executor_setup_error")
                    print(
                        f"[orchestrate] {tid}: executor setup/configuration error — "
                        "work preserved, hibernating and halting this run.\n"
                        f"  Fix the executor/model issue, then re-run: lanegate run",
                        file=sys.stderr,
                    )
                    if not verbose:
                        _status(tid, "hibernated", orig_out, _log_f)
                    cmd_hibernate(tid, cfg, repo_root, reason=reason)
                    _log_outcome(tid, "hibernated", reason=reason)
                    return True
                log_path_str = getattr(_log_f, "name", "") if _log_f is not None else ""
                log_hint = (
                    f"\n  Executor log: {log_path_str}"
                    if log_path_str
                    else ""
                )
                base_reason = f"executor exited with code {exit_code}"
                reason_parts = [_clean_attention_reason(base_reason)]
                if log_path_str:
                    try:
                        rel = Path(log_path_str).relative_to(repo_root)
                        reason_parts.append(f"**Log File:** `{rel}`")
                    except ValueError:
                        reason_parts.append(f"**Log File:** `{log_path_str}`")

                output_text = (captured_stderr or captured_stdout or "").strip()
                if output_text:
                    lines = [l for l in output_text.splitlines() if l.strip()]
                    tail = lines[-12:] if len(lines) > 12 else lines
                    snippet = "\n".join(tail).strip()
                    if len(snippet) > 1500:
                        snippet = snippet[-1500:]
                    reason_parts.append(f"**Error Output Snippet:**\n```\n{snippet}\n```")

                if pool_instance:
                    signature = _normalize_analyze_failure_reason(output_text or base_reason)
                    is_streak = _record_pool_failure_signature(
                        repo_root,
                        pool_instance,
                        signature,
                        window_s=_POOL_FAILURE_STREAK_WINDOW_S,
                        threshold=_POOL_FAILURE_STREAK_THRESHOLD,
                    )
                    if is_streak:
                        reason = (
                            f"{_RATE_LIMIT_MARKER} ({_POOL_FAILURE_STREAK_THRESHOLD} consecutive "
                            f"failures on {pool_instance} with an identical error signature "
                            f"within {_POOL_FAILURE_STREAK_WINDOW_S}s, executor exited "
                            f"{exit_code}) — text matched no known rate-limit pattern, but "
                            "treated as equivalent since it has the same shape (same instance, "
                            "same error, repeating fast) so resume-watch can retry it with "
                            "backoff instead of requiring manual reopen."
                        )
                        _write_executor_cooldown(repo_root, pool_instance, reason)
                        _append_run_event(
                            repo_root,
                            session_ts,
                            "executor_cooldown",
                            instance=pool_instance,
                            reason=reason,
                            ticket_id=tid,
                        )
                        reason = f"{reason}\n\npool instance: {pool_instance}"
                        print(
                            f"[orchestrate] {tid}: {pool_instance} hit "
                            f"{_POOL_FAILURE_STREAK_THRESHOLD} consecutive same-signature "
                            "failures — treating as an unrecognized rate limit and hibernating.\n"
                            "  Check the executor/provider status, then re-run: lanegate run",
                            file=sys.stderr,
                        )
                        if not verbose:
                            _status(tid, "hibernated", orig_out, _log_f)
                        cmd_hibernate(tid, cfg, repo_root, reason=reason)
                        _log_outcome(tid, "hibernated", reason=reason)
                        if cfg.get("on_rate_limit") == "resume":
                            spawn_resume_watch_daemon(repo_root)
                        if pool_name and _pool_has_available_instance(pool_name):
                            print(
                                f"[orchestrate] {tid}: {pool_instance} cooling down — "
                                "other pool instance(s) still available, continuing",
                                file=sys.stderr,
                            )
                        elif cfg.get("on_rate_limit") == "resume":
                            rate_limit_halt = True
                        elif pool_name and _wait_for_pool_capacity(pool_name):
                            pass
                        else:
                            rate_limit_halt = True
                        return True

                rich_reason = "\n\n".join(reason_parts)
                print(
                    f"ERROR: {tid} executor exited {exit_code} — marked as failed, batch continues.{log_hint}\n"
                    f"  Re-run with --verbose to see executor output in the terminal.\n"
                    f"  After fixing the issue: lanegate reopen {tid} && lanegate run",
                    file=sys.stderr,
                )
                if not verbose:
                    _status(tid, "failed", orig_out, _log_f)
                cmd_fail(tid, cfg, repo_root, reason=rich_reason)
                _log_outcome(tid, "failed", reason=base_reason)
                return True

            if pool_instance:
                _clear_pool_failure_streak(repo_root, pool_instance)

            audit_bundle_path: Path | None = None
            # Find the audit bundle by looking for the most recent session directory.
            # The audit bundle is created and contains its own status.json, making it
            # the reliable source of truth for concurrent executors (the shared
            # active-orchestrate.json gets overwritten by concurrent writers).
            latest_bundle = _find_latest_audit_bundle(repo_root, tid)
            if latest_bundle and latest_bundle.exists():
                audit_bundle_path = latest_bundle
                # Verify this is for the right ticket by checking status.json
                status_file = audit_bundle_path / "status.json"
                if status_file.exists():
                    try:
                        bundle_status = json.loads(status_file.read_text(encoding="utf-8"))
                        if bundle_status.get("ticket_id") != tid:
                            audit_bundle_path = None
                    except (OSError, json.JSONDecodeError):
                        audit_bundle_path = None

            committed_ok, commit_err = commit_worktree_changes(wt, tid)

            # --- validate that at least one file was committed ---
            try:
                has_commits = check_worktree_has_commits(wt)
            except FileNotFoundError as exc:
                reason = f"worktree directory disappeared mid-run: {exc}"
                print(
                    f"[orchestrate] {tid}: {reason} — needs human review.",
                    file=sys.stderr,
                )
                if not verbose:
                    _status(tid, "needs_review", orig_out, _log_f)
                cmd_needs_review(tid, cfg, repo_root, reason=reason)
                _log_outcome(tid, "needs_review", reason=reason)
                return True

            if not has_commits:
                log_path_hint = (
                    _log_f.name if (_log_f is not None and hasattr(_log_f, "name")) else None
                )
                if commit_err:
                    reason = f"auto-commit rejected: {commit_err}"
                else:
                    reason = "executor exited 0 but produced no commits"
                print(
                    f"ERROR: {tid} — executor exited 0 but made no commits. Marked as failed, batch continues.\n"
                    f"  Common causes:\n"
                    f"    - Agent hit a permission prompt and could not proceed (check log)\n"
                    f"    - Agent found nothing to do (ticket may already be complete)\n"
                    f"    - Agent crashed silently (check log for tracebacks)\n"
                    + (f"  Executor log: {log_path_hint}\n" if log_path_hint else "")
                    + f"  Re-run with --verbose to see executor output in the terminal.\n"
                    f"  After investigating: lanegate reopen {tid} && lanegate run",
                    file=sys.stderr,
                )
                if not verbose:
                    _status(tid, "failed", orig_out, _log_f)
                cmd_fail(tid, cfg, repo_root, reason=reason)
                _log_outcome(tid, "failed", reason=reason)
                return True

            # --- touched-files guard ---
            # Only enforce when the ticket declares a non-empty touches list.
            # Tickets without touches skip the check (legacy or intentionally broad).
            touches_list = fresh_ticket.get("touches") or []
            if touches_list:
                # Wildcard escape hatch: touches: ["*"] means "anything goes"
                if "*" not in touches_list:
                    committed = _committed_files(wt)
                    allowed = set(touches_list)
                    unexpected = committed - allowed
                    # A committed file that's the natural paired test file
                    # for an already-touched module is not scope drift.
                    unexpected = {f for f in unexpected if not is_paired_test_file(f, allowed)}
                    unexpected = {f for f in unexpected if not is_lanegate_notes_file(f)}
                    if unexpected:
                        if cfg.get("auto_claim_touches") is True:
                            from lanegate.claim_file import claim_files
                            from lanegate.ticket import write_ticket

                            claimed, claim_detail = claim_files(sorted(unexpected), tid, cfg, repo_root)
                            if not claimed:
                                reason = f"could not auto-claim additional touched files: {claim_detail}"
                                print(f"[orchestrate] {tid}: WARNING — {reason}", file=sys.stderr)
                                pause_for_needs_review(tid, reason)
                                return True
                            updated_tickets, _ = load_all_tickets(
                                tickets_dir, cfg["ticket_prefix"], cfg
                            )
                            updated_ticket = next(
                                (item for item in updated_tickets if item["id"] == tid), None
                            )
                            if updated_ticket is not None:
                                _record_auto_claimed_touches(updated_ticket, unexpected)
                                write_ticket(updated_ticket)
                                _commit_generated_ticket_write(
                                    repo_root,
                                    Path(updated_ticket["_path"]),
                                    tid,
                                    "auto-claimed touches",
                                    cfg,
                                )
                            print(
                                f"[orchestrate] {tid}: auto-claimed additional touched files: {sorted(unexpected)}",
                                file=sys.stderr,
                            )
                        else:
                            unexpected_sorted = sorted(unexpected)
                            reason = "committed files outside touches list: " + ", ".join(unexpected_sorted)
                            print(
                                f"[orchestrate] {tid}: WARNING — {reason}",
                                file=sys.stderr,
                            )
                            pause_for_needs_review(tid, reason)
                            return True

            # --- risk-lane classification ---
            # A red-lane signal (external credentials, security-sensitive or
            # irreversible operations) always escalates to a human, even when
            # the ticket's configured autonomy is "full"/"green"/"yellow" —
            # the risk lane is a safety override on top of autonomy, not a
            # substitute for it. An explicit autonomy of "red" escalates
            # unconditionally. Green/yellow lanes with no red signal fall
            # through and stay on the automatic fix/re-review/merge path,
            # same as "full" (see the is_auto_fix_lane checks below).
            #
            # Always route through pause_for_needs_review: an
            # in_progress ticket used to go through cmd_hibernate(escalation=True)
            # instead, but hibernated isn't recognized by human-gate
            # checks, so a hibernated escalation could get auto-redispatched
            # before a human ever saw it. needs_review is gated everywhere.
            resolved_autonomy = resolve_autonomy(cfg, fresh_ticket)
            escalation_triggers = resolve_human_escalation(cfg)
            trunk_branch_for_scan = resolve_trunk_branch(cfg, repo_root)
            diff_capture = _git_text(["git", "diff", f"{trunk_branch_for_scan}...HEAD"], wt)
            if not diff_capture.ok:
                red_signal_triggered = False
                reason = (
                    "red-lane escalation: risk scan unavailable because "
                    f"`git diff {trunk_branch_for_scan}...HEAD` failed: "
                    f"{diff_capture.error or 'no diagnostic available'} — human review required"
                )
            else:
                risk_lane = scan_risk_lane(diff_capture.text, fresh_ticket)
                red_signal_triggered = risk_lane_requires_human_review(
                    risk_lane, escalation_triggers
                )
                if red_signal_triggered:
                    approved_sha = fresh_ticket.get("red_lane_approved_at_sha")
                    if approved_sha:
                        head_capture = _git_text(["git", "rev-parse", "HEAD"], wt)
                        if head_capture.ok and head_capture.text.strip() == approved_sha:
                            red_signal_triggered = False
                reason = (
                    "red-lane escalation: diff scan classified this change as "
                    "'red' (external credentials, security-sensitive, or irreversible "
                    "operation detected) — human review required"
                    if red_signal_triggered
                    else "red-lane escalation: ticket autonomy is 'red' — human review required"
                )
            if not diff_capture.ok or red_signal_triggered or is_red_lane(resolved_autonomy):
                print(f"[orchestrate] {tid}: WARNING — {reason}", file=sys.stderr)
                escalation_ticket = next(
                    (t for t in load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)[0] if t["id"] == tid),
                    fresh_ticket,
                )
                branch = escalation_ticket.get("branch") or branch_name(tid)
                append_or_replace_section(
                    escalation_ticket,
                    "## Human Escalation",
                    f"{reason}\n\n"
                    f"Branch `{branch}` and worktree `{wt}` are preserved — not reset.\n\n"
                    "To resume after review:\n"
                    f"- Inspect the change: `git log {trunk_branch_for_scan}..{branch} --oneline` "
                    f"and `git diff {trunk_branch_for_scan}...{branch}`\n"
                    f"- Resume orchestration: `lanegate human-review {tid} --rationale \"...\"` (or `lanegate reopen {tid}`)",
                )
                from lanegate.ticket import write_ticket

                write_ticket(escalation_ticket)
                _commit_generated_ticket_write(
                    repo_root, Path(escalation_ticket["_path"]), tid, "human escalation", cfg
                )
                pause_for_needs_review(tid, reason)
                return True

            # --- blocked-file check ---
            # Fires for ALL committed files regardless of touches list.
            # Certain file categories are always hard-blocked and route to
            # needs_review even when the file is listed in touches.
            protected_paths = cfg.get("protected_paths") or []
            all_committed = _committed_files(wt)
            blocked_matches: list[tuple[str, str]] = []  # (file, rule)
            for committed_file in sorted(all_committed):
                blocked, rule = _is_blocked_file(committed_file, extra_patterns=protected_paths)
                if blocked:
                    blocked_matches.append((committed_file, rule))
            if blocked_matches:
                detail_lines = [f"{f} [{rule}]" for f, rule in blocked_matches]
                reason = "committed files match hard-blocked categories: " + "; ".join(detail_lines)
                print(
                    f"[orchestrate] {tid}: WARNING — {reason}",
                    file=sys.stderr,
                )
                pause_for_needs_review(tid, reason)
                return True

            # --- security-sensitive paths check ---
            # User-defined list of project-specific sensitive paths. Any commit
            # touching these is escalated to needs_review regardless of ticket
            # autonomy setting. Runs after blocked-file check, before static
            # analysis.
            sensitive_patterns = cfg.get("security_sensitive_paths") or []
            if sensitive_patterns:
                sensitive_hits = [
                    f
                    for f in sorted(all_committed)
                    if any(
                        fnmatch.fnmatch(f, p) or fnmatch.fnmatch(Path(f).name, p)
                        for p in sensitive_patterns
                    )
                ]
                if sensitive_hits:
                    detail = ", ".join(sorted(sensitive_hits))
                    reason = (
                        f"committed files match security_sensitive_paths — "
                        f"human review required: {detail}"
                    )
                    print(f"[orchestrate] {tid}: WARNING — {reason}", file=sys.stderr)
                    pause_for_needs_review(tid, reason)
                    return True

            ok_cp, cp_err = check_control_plane_compliance(ticket, repo_root=repo_root, cfg=cfg, worktree_path=wt, check_review_independence=False)
            if not ok_cp:
                reason = f"control plane compliance failed: {cp_err}"
                print(f"[orchestrate] {tid}: WARNING — {reason}", file=sys.stderr)
                pause_for_needs_review(tid, reason)
                return True

            # --- static analysis gate ---
            # Runs after the blocked-file check, before LLM review dispatch.
            sa_findings = _run_static_analysis(wt, cfg, audit_bundle_path=audit_bundle_path)
            sa_cfg = cfg.get("static_analysis") or {}
            threshold = int(sa_cfg.get("threshold", 0))
            _record_static_analysis_decision(
                audit_bundle_path,
                findings=sa_findings,
                threshold=threshold,
                blocked=bool(sa_findings and len(sa_findings) > threshold),
            )
            if sa_findings:
                if len(sa_findings) > threshold:
                    joined = "; ".join(sa_findings[:5])
                    if len(sa_findings) > 5:
                        joined += f" (+ {len(sa_findings) - 5} more)"
                    reason = f"static analysis findings ({len(sa_findings)}): {joined}"
                    print(
                        f"[orchestrate] {tid}: WARNING — {reason}",
                        file=sys.stderr,
                    )
                    pause_for_needs_review(tid, reason)
                    return True

            if combined:
                # Combined mode: the executor subprocess already ran `lanegate complete`
                # and `lanegate review --verdict` as part of its combined prompt.
                # The orchestrator skips both cmd_complete and the review step here.
                # We still emit the "reviewing" status line for log consistency.
                if not verbose:
                    _status(tid, "reviewing", orig_out, _log_f)
                if verbose:
                    print(
                        f"[orchestrate] {tid}: combined mode — complete+review handled by executor"
                    )
                # Guard: verify the executor actually advanced the ticket status.
                # If still open/in_progress, the executor ignored the combined prompt.
                all_t_post, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
                post_ticket = next((t for t in all_t_post if t["id"] == tid), None)
                post_status = post_ticket.get("status") if post_ticket else None
                if post_ticket:
                    # The executor recorded its own verdict via the CLI, which
                    # cannot name the reviewer. Attribute it here or the ticket
                    # keeps a verdict nobody is accountable for.
                    backfill_combined_review_metadata(post_ticket, dispatch, repo_root)
                if post_ticket and post_ticket.get("review_verdict") == "approved":
                    acceptance_findings = _run_acceptance_contract_audit(post_ticket, repo_root, cfg)
                    if acceptance_findings and resolve_acceptance_contract_mode(cfg) == "blocker":
                        findings = "\n".join(acceptance_findings)
                        _invoke_cmd_review(
                            _cmd_review,
                            tid,
                            cfg,
                            repo_root,
                            verdict="changes_requested",
                            summary="acceptance-contract audit failed",
                            findings=findings,
                        )
                        print(
                            f"[orchestrate] {tid}: acceptance-contract audit blocked approval — pausing",
                            file=sys.stderr,
                        )
                        if not verbose:
                            _status(tid, "changes_requested", orig_out, _log_f)
                        _log_outcome(tid, "changes_requested", reason="acceptance-contract audit failed")
                        return True
                    elif acceptance_findings and verbose:
                        print(
                            f"[orchestrate] {tid}: acceptance-contract audit found "
                            f"{len(acceptance_findings)} finding(s) (advisory — approval stands)"
                        )
                if post_status in ("open", "in_progress"):
                    reason = (
                        "combined-mode executor exited 0 but ticket status did not advance "
                        "(executor must call 'lanegate complete && lanegate review --verdict')"
                    )
                    print(
                        f"ERROR: {tid} — {reason}.\n"
                        f"  Hibernating for retry. Re-run: lanegate run",
                        file=sys.stderr,
                    )
                    # cmd_hibernate only accepts in_progress and sys.exit(1)s
                    # otherwise — that SystemExit would unwind out of
                    # cmd_orchestrate and abort the rest of the batch. Fall
                    # back to pause_for_needs_review for any other observed
                    # status (e.g. "open", if cmd_start's transition never
                    # took effect) instead of assuming in_progress.
                    if post_status == "in_progress":
                        if not verbose:
                            _status(tid, "hibernated", orig_out, _log_f)
                        cmd_hibernate(tid, cfg, repo_root, reason=reason)
                        _log_outcome(tid, "hibernated", reason=reason)
                    else:
                        pause_for_needs_review(tid, reason)
                    return True
                elif (
                    post_status == "code_complete"
                    and post_ticket
                    and post_ticket.get("review_verdict") == "changes_requested"
                ):
                    fixed = run_auto_fix_cycle(post_ticket, cfg, repo_root, wt, pool_name=pool_name)
                    if fixed is None:
                        # Rate limit during fix pass — ticket hibernated, no attempt consumed.
                        _log_outcome(tid, "hibernated", reason="rate limit during fix agent")
                        return True
                    if fixed:
                        if verbose:
                            print(f"[orchestrate] {tid}: auto-fix cycle reached approved")
                        all_t_fixed, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
                        fixed_ticket = next((t for t in all_t_fixed if t["id"] == tid), post_ticket)
                        if not is_auto_fix_lane(resolve_autonomy(cfg, fixed_ticket)):
                            print(
                                f"[orchestrate] {tid}: awaiting human merge approval "
                                "(auto-fix cycle approved)",
                                file=sys.stderr,
                            )
                            _log_outcome(tid, "awaiting_human_review")
                            return True
                    else:
                        print(
                            f"[orchestrate] {tid}: combined-mode review requested changes — pausing",
                            file=sys.stderr,
                        )
                        pause_for_needs_review(
                            tid,
                            "auto-fix/re-review did not reach approval; "
                            "human intervention is required",
                        )
                        return True
                elif (
                    post_status == "in_review"
                    and post_ticket
                    and post_ticket.get("review_verdict") == "approved"
                ):
                    # Success: executor correctly ran `lanegate complete && lanegate
                    # review --verdict approved`. The acceptance-contract audit
                    # already ran above (line 5029) against this same verdict —
                    # don't re-run it. Fall through to the shared merge / next-
                    # batch handling below instead of downgrading to
                    # needs_review.
                    if verbose:
                        print(f"[orchestrate] {tid}: combined mode — approved")
                    if (
                        human_review == "per_ticket"
                        or not is_auto_fix_lane(resolve_autonomy(cfg, post_ticket))
                    ):
                        print(
                            f"[orchestrate] {tid}: awaiting human merge approval "
                            "(supervised autonomy)",
                            file=sys.stderr,
                        )
                        _log_outcome(tid, "awaiting_human_review")
                        return True
                else:
                    # Unhandled combined-mode state: executor exited 0 but left
                    # the ticket in an unexpected state (e.g. code_complete with
                    # no verdict, needs_review, failed). This means the combined
                    # prompt was not fully executed. Pause for manual review.
                    reason = (
                        "combined-mode executor exited 0 but left ticket in unhandled state "
                        f"(status={post_status}, verdict={post_ticket.get('review_verdict') if post_ticket else None}) "
                        "— executor must call 'lanegate complete && lanegate review --verdict'"
                    )
                    print(
                        f"ERROR: {tid} — {reason}.\n"
                        f"  Pausing for manual review. Re-run: lanegate run",
                        file=sys.stderr,
                    )
                    pause_for_needs_review(tid, reason)
                    return True
            else:
                # --- complete (split mode) ---
                if verbose:
                    print(f"[orchestrate] completing {tid}")
                else:
                    _status(tid, "reviewing", orig_out, _log_f)
                cmd_complete(tid, cfg, repo_root)

                # --- review (split mode) ---
                all_t2, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
                t2 = next((t for t in all_t2 if t["id"] == tid), None)
                if t2 is None or t2.get("status") != "code_complete":
                    # cmd_complete routes a failed pre_complete gate straight to
                    # needs_review and returns without raising -- nothing else
                    # signals that here. Without this check, execution fell
                    # through into review dispatch against a ticket that never
                    # reached code_complete: an LLM reviewer ran and wrote an
                    # approved verdict.json that no ticket ever applied (cmd_review
                    # rejected it since status != code_complete), leaving an
                    # orphaned approved verdict on disk for a ticket stuck in
                    # needs_review.
                    events = (t2.get("lifecycle_events") or []) if t2 else []
                    reason = events[-1].get("summary") if events else "did not reach code_complete"
                    pause_for_needs_review(tid, reason)
                    return True
                review_ticket = t2 or fresh_ticket
                acceptance_findings = _run_acceptance_contract_audit(review_ticket, repo_root, cfg)
                if acceptance_findings and resolve_acceptance_contract_mode(cfg) == "blocker":
                    findings = "\n".join(acceptance_findings)
                    _invoke_cmd_review(
                        _cmd_review,
                        tid,
                        cfg,
                        repo_root,
                        verdict="changes_requested",
                        summary="acceptance-contract audit failed",
                        findings=findings,
                    )
                    print(
                        f"[orchestrate] {tid}: acceptance-contract audit blocked approval — pausing",
                        file=sys.stderr,
                    )
                    if not verbose:
                        _status(tid, "changes_requested", orig_out, _log_f)
                    return True
                elif acceptance_findings and verbose:
                    # Advisory mode (default): fall through to the real reviewer
                    # (review_executor below) instead of short-circuiting it —
                    # findings are persisted on the ticket for it to weigh.
                    print(
                        f"[orchestrate] {tid}: acceptance-contract audit found "
                        f"{len(acceptance_findings)} finding(s) (advisory — deferring to reviewer)"
                    )
                rotation_enabled = (
                    bool(cfg.get("reviewer_rotation"))
                    and not ((cfg.get("steps") or {}).get("review") or {}).get("driver")
                    and not review_ticket.get("reviewer")
                )
                review_executor = "reviewer_rotation" if rotation_enabled else resolve_driver("review", review_ticket, cfg)
                if review_executor in ("none", "auto-none"):
                    # Explicitly configured no LLM review: record auto-approved verdict
                    _invoke_cmd_review(
                        _cmd_review,
                        tid, cfg, repo_root,
                        verdict="approved", summary="auto-approved",
                        # Both fields, so "no model reviewed this" is recorded
                        # rather than left indistinguishable from "we forgot to
                        # record which model reviewed this".
                        review_driver="auto-none", review_model="none",
                    )
                elif review_executor == "human":
                    # reviewer: human — pause for a human to record a verdict.
                    _invoke_cmd_review(_cmd_review, tid, cfg, repo_root, review_driver="human")
                    print(
                        f"[orchestrate] {tid}: awaiting human review — run "
                        f"`lanegate review {tid} --verdict approved` or request changes",
                        file=sys.stderr,
                    )
                    _log_outcome(tid, "awaiting_human_review")
                    return True
                else:
                    # Non-human reviewer (codex, claude, agy, etc.): ALWAYS RUN REVIEWER!
                    approved = run_review_agent(
                        review_ticket, repo_root, worktree_path=wt, cfg=cfg, pool_name=pool_name
                    )
                    if not approved:
                        # The review agent persists its findings on disk —
                        # reload so the fix agent sees the newest ones rather
                        # than a copy of the ticket that predates the review.
                        all_t3, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
                        reloaded = next((t for t in all_t3 if t["id"] == tid), review_ticket)
                        if reloaded.get("status") == "hibernated" and reloaded.get("review_pending"):
                            # Reviewer temporarily unavailable (cooldown/rate
                            # limit) — self-resolves on the next `lanegate
                            # run`, not a human-intervention escalation.
                            _log_outcome(tid, "review_pending", reason=reloaded.get("review_pending_reason"))
                            return True
                        if reloaded.get("status") == "in_review":
                            # run_review_agent fell back to a pool-resolved
                            # human reviewer — correctly parked awaiting a
                            # human verdict, not an escalation.
                            _log_outcome(tid, "awaiting_human_review")
                            return True
                        if reloaded.get("status") != "code_complete":
                            # run_review_agent already moved the ticket to a
                            # terminal state on its own (e.g. needs_review via
                            # _escalate_no_reviewer when no independent
                            # reviewer is available, each with its own
                            # specific reason) instead of leaving it at
                            # code_complete the way a genuine changes_requested
                            # verdict does. run_auto_fix_cycle assumes that
                            # code_complete precondition; calling it anyway
                            # finds no review findings to act on and fails
                            # generically, overwriting the ticket's real, more
                            # specific reason with a misleading one.
                            _log_outcome(tid, "needs_review")
                            return True
                        fixed = run_auto_fix_cycle(reloaded, cfg, repo_root, wt, pool_name=pool_name)
                        if fixed is None:
                            # Rate limit during fix pass — ticket hibernated, no attempt consumed.
                            _log_outcome(tid, "hibernated", reason="rate limit during fix agent")
                            return True
                        if fixed:
                            if verbose:
                                print(f"[orchestrate] {tid}: auto-fix cycle reached approved")
                        else:
                            print(
                                f"[orchestrate] {tid}: review requested changes — pausing",
                                file=sys.stderr,
                            )
                            pause_for_needs_review(
                                tid,
                                "auto-fix/re-review did not reach approval; "
                                "human intervention is required",
                            )
                            return True

                if human_review == "per_ticket" or not is_auto_fix_lane(resolve_autonomy(cfg, review_ticket)):
                    print(
                        f"[orchestrate] {tid}: awaiting human merge approval "
                        "(supervised autonomy)",
                        file=sys.stderr,
                    )
                    _log_outcome(tid, "awaiting_human_review")
                    return True
                elif human_review == "final":
                    # human_review == "final": hold for final batch approval before merge
                    pass

            all_t_merge, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
            auto_merge_approved_local_tickets(
                [t for t in all_t_merge if t.get("id") == tid]
            )

            if not verbose:
                _status(tid, "done", orig_out, _log_f)

            final_all, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
            final_ticket = next((t for t in final_all if t["id"] == tid), None)
            _log_outcome(tid, str(final_ticket.get("status")) if final_ticket else "unknown")

            return False

        def run_worker_pool(initial_items: list[dict]) -> list[str]:
            nonlocal interrupt_halt
            paused_tickets: list[str] = []
            submitted_ids: set[str] = set()
            in_flight: dict[concurrent.futures.Future, tuple[str, set[str], str | None]] = {}

            def current_excluded_touches() -> set[str]:
                excluded: set[str] = set()
                for _, touches, _ in in_flight.values():
                    excluded.update(touches)
                return excluded

            def submit(pool: concurrent.futures.ThreadPoolExecutor, ticket: dict) -> None:
                tid = ticket["id"]
                touches = set(ticket.get("touches") or [])
                instance = assign_pool_instance(ticket)
                # An explicit ticket.executor deliberately bypasses pool
                # selection, so its assignment is None while it remains
                # dispatchable.  Reserve/reject a ticket only when selection
                # actually exhausted the active pool.
                if instance is None and pool_name and rate_limit_halt:
                    return
                submitted_ids.add(tid)
                future = pool.submit(run_ticket, ticket)
                in_flight[future] = (tid, touches, instance)

            def next_refill_candidate() -> dict | None:
                batch = next_batch(
                    cfg,
                    repo_root,
                    milestone=milestone,
                    exclude_touches=current_excluded_touches(),
                    ticket_ids=ticket_ids,
                )
                for candidate in batch:
                    if candidate["id"] not in submitted_ids:
                        return candidate
                return None

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel) as pool:
                for ticket in initial_items:
                    submit(pool, ticket)

                while in_flight:
                    done, _ = concurrent.futures.wait(
                        in_flight, return_when=concurrent.futures.FIRST_COMPLETED
                    )
                    for future in done:
                        tid_done, _, instance_done = in_flight.pop(future)
                        release_pool_instance(instance_done)
                        try:
                            if future.result():
                                paused_tickets.append(tid_done)
                        except (Exception, SystemExit) as exc:
                            # SystemExit (raised by guard-exiting lifecycle
                            # calls like cmd_start/cmd_needs_review/cmd_fail on
                            # a precondition failure) is a BaseException, not
                            # an Exception — `except Exception` alone lets it
                            # unwind straight out of cmd_orchestrate. Catch it
                            # explicitly here so any lifecycle guard tripping
                            # inside run_ticket downgrades one ticket instead
                            # of killing the batch.
                            reason = f"worker thread crashed: {exc.__class__.__name__}: {exc}"
                            print(f"ERROR: {tid_done} — {reason}", file=sys.stderr)
                            # The reason string alone (exception class + message)
                            # was often not enough to find the actual failing line without
                            # cross-referencing timestamped logs by hand — print the full
                            # traceback right here instead.
                            traceback.print_exc(file=sys.stderr)
                            # The ticket may already have moved past in_progress
                            # (e.g. the crash happened during merge, after review
                            # approved it) — cmd_needs_review only accepts
                            # in_progress and exits the whole process otherwise.
                            # Route through pause_for_needs_review so a crash on
                            # one ticket downgrades that ticket only, instead of
                            # taking down the rest of the batch.
                            pause_for_needs_review(tid_done, reason)
                            paused_tickets.append(tid_done)

                    while len(in_flight) < max_parallel:
                        if rate_limit_halt or interrupt_halt or executor_setup_halt:
                            # A rate limit, user interrupt, or executor setup
                            # error was hit — do not pull new tickets.
                            break
                        candidate = next_refill_candidate()
                        if candidate is None and recover:
                            queued_reviews = _queue_code_complete_reviews(
                                cfg,
                                repo_root,
                                milestone=milestone,
                                ticket_ids=ticket_ids,
                                exclude_ticket_ids=handled_ticket_ids,
                            )
                            if queued_reviews:
                                print(
                                    "[orchestrate] queued code-complete ticket(s) for review before "
                                    f"draft analysis: {', '.join(queued_reviews)}",
                                    file=sys.stderr,
                                )
                                candidate = next_refill_candidate()
                        if candidate is None and auto_analyze:
                            # Only spend time analyzing drafts once there is
                            # no already-dispatchable work to refill with —
                            # ready tickets must never wait behind drafts.
                            if _analyze_drafts(
                                cfg, repo_root, milestone=milestone, tickets_dir=tickets_dir,
                                ticket_ids=ticket_ids, pool_name=pool_name, session_ts=session_ts,
                            ):
                                interrupt_halt = True
                                break
                            candidate = next_refill_candidate()
                        if candidate is None:
                            break
                        submit(pool, candidate)

            return paused_tickets

        if max_parallel <= 1 or dry_run:
            for ticket in work_items:
                tid = ticket["id"]
                instance = assign_pool_instance(ticket) if not dry_run else None
                # An explicit ticket.executor deliberately bypasses pool
                # selection, so assign_pool_instance() returns None even
                # though this ticket is ready to dispatch.  Only stop when
                # selection actually exhausted the pool and set its halt flag.
                if instance is None and pool_name and not dry_run and rate_limit_halt:
                    break
                try:
                    paused = run_ticket(ticket)
                except (Exception, SystemExit) as exc:
                    # See matching comment in run_worker_pool above: SystemExit
                    # is a BaseException, not an Exception, so it must be
                    # caught explicitly or it unwinds out of cmd_orchestrate.
                    reason = f"ticket run crashed: {exc.__class__.__name__}: {exc}"
                    print(f"ERROR: {tid} — {reason}", file=sys.stderr)
                    # See matching comment in run_worker_pool above.
                    traceback.print_exc(file=sys.stderr)
                    pause_for_needs_review(tid, reason)
                    paused = True
                finally:
                    release_pool_instance(instance)
                if paused:
                    all_paused_tickets.append(tid)
                if rate_limit_halt or interrupt_halt or executor_setup_halt:
                    break
        else:
            all_paused_tickets.extend(run_worker_pool(work_items))

        if dry_run:
            # In dry-run mode, only process one batch then stop
            break

        if rate_limit_halt or interrupt_halt or executor_setup_halt:
            # A rate limit, user interrupt, or executor setup error halted this
            # run. Every affected in-flight ticket has already hibernated; do
            # NOT loop back to next_batch() for new work. When
            # on_rate_limit=resume, a resume-watch daemon was already spawned
            # for rate limits.
            break

        if human_review == "final":
            # Print instructions and exit, spawning watcher
            print("\n[orchestrate] batch complete — review PRs, then run:\n    lanegate run")
            all_tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
            _print_review_queue(all_tickets, milestone=milestone, stream=orig_out)
            _print_continuation_steps(all_tickets, milestone=milestone, stream=orig_out)
            in_review_with_pr = [
                t for t in all_tickets if t.get("status") == "in_review" and t.get("pr_number")
            ]
            if in_review_with_pr:
                spawn_watch_daemon(repo_root)
            break

    if all_paused_tickets:
        all_tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
        ticket_by_id = {t["id"]: t for t in all_tickets}
        _print_review_queue(all_tickets, milestone=milestone, stream=orig_out)
        _print_continuation_steps(all_tickets, milestone=milestone, stream=orig_out)
        summary_lines = []
        for tid in all_paused_tickets:
            paused_ticket = ticket_by_id.get(tid)
            status = paused_ticket.get("status", "unknown") if paused_ticket else "unknown"
            if status == "hibernated":
                remedy = "re-run: lanegate run"
            elif status == "needs_review":
                remedy = needs_review_recovery_advice(paused_ticket) if paused_ticket else f"inspect and fix, then: lanegate reopen {tid} && lanegate run"
            elif status == "failed":
                remedy = f"inspect and fix, then: lanegate reopen {tid} && lanegate run"
            elif status == "changes_requested":
                remedy = "address reviewer feedback, then: lanegate run"
            else:
                remedy = f"check ticket (status: {status})"
            summary_lines.append(f"  {tid} [{status}] -> {remedy}")
        print(
            f"[orchestrate] {len(all_paused_tickets)} ticket(s) paused during this run:\n"
            + "\n".join(summary_lines),
            file=sys.stderr,
        )
