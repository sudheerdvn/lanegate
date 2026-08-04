"""
Board-clearing loop and its supporting helpers.

Usage:
    lanegate orchestrate                        # clear the board using executor from .lanegate.yml
    lanegate orchestrate --max 3                # cap parallel tickets
    lanegate orchestrate --dry-run              # print planned actions, do nothing
    lanegate orchestrate --human-review final   # per_ticket | final | none

TICK-255: this module mixes several concerns (audit capture, active-status
bookkeeping, rate-limit detection, executor pool selection, the board-clearing
loop itself, ...) that repeatedly collide on merge. TICK-271 converted this
module into the lanegate/orchestrate/ package and extracted the safety gates
(injection scan, blocked-file check, diff parser, static analysis) into
guards.py; TICK-272 extracted tee logging and executor audit-bundle capture
into audit.py. The remaining concerns are split out by TICK-273..279 — see
docs/internal/module-split-proposal.md.
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
    resolve_acceptance_contract_mode,
    resolve_autonomy,
    resolve_max_parallel_detail,
    resolve_model,
    resolve_trunk_branch,
    resolve_ticket_pool,
)
from lanegate.executor import (
    available_instances as _available_executor_instances,
    build_executor_cmd,
    get_executor_config,
    is_cooling_down as _executor_is_cooling_down,
    resolve_executor_env,
    write_cooldown as _write_executor_cooldown,
)
from lanegate.git import git_text
from lanegate.pidutil import pid_alive
from lanegate.ticket import (
    TERMINAL_STATUSES,
    _clean_attention_reason,
    branch_name,
    is_paired_test_file,
    load_all_tickets,
    milestone_near_miss_warnings,
)

_git_text = git_text

# Safety gates (injection scan, blocked-file check, diff parser, static
# analysis) live in orchestrate/guards.py (TICK-271); re-exported here so
# `from lanegate.orchestrate import X` keeps working for every caller and test.
from .guards import (
    _is_blocked_file,
    _run_acceptance_contract_audit,
    _run_static_analysis,
    _scan_injection_signals,
)
from .guards import _BLOCKED_FILE_RULES as _BLOCKED_FILE_RULES
from .guards import _INJECTION_SIGNALS as _INJECTION_SIGNALS
from .guards import _SYSTEM_SECTION_HEADERS as _SYSTEM_SECTION_HEADERS
from .guards import _parse_diff_changed_lines as _parse_diff_changed_lines

# Tee logging and executor audit-bundle capture (transcript + task-output
# capture, manifest, gate capture) live in orchestrate/audit.py (TICK-272);
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
# enumeration, `lanegate ps`, `lanegate run-report`, `lanegate orchestrate-status`, and
# the executor subprocess-streaming helper) live in orchestrate/run_report.py
# (TICK-274); re-exported here so `from lanegate.orchestrate import X` keeps
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
from .run_report import summarize_executor_events as summarize_executor_events

# Board batch selection and review/continuation queue rendering live in
# orchestrate/batch.py (TICK-275); re-exported here so `from
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
# stale-executor-marker reconciliation) lives in orchestrate/status.py
# (TICK-273); re-exported here so `from lanegate.orchestrate import X` keeps
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
from .status import get_orchestration_status as get_orchestration_status

# Executor pool selection/invocation (driver resolution, prompt dispatch,
# worktree commit helpers) lives in orchestrate/pool.py (TICK-276);
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


def _is_interrupted_exit(exit_code: int) -> bool:
    return exit_code < 0


def _interrupted_exit_reason(exit_code: int) -> str:
    signum = abs(exit_code)
    try:
        signal_name = signal.Signals(signum).name
    except ValueError:
        signal_name = f"signal {signum}"
    return f"executor interrupted by {signal_name} (exit {exit_code})"


def _pool_state_path(repo_root: Path) -> Path:
    return repo_root / ".lanegate" / "pool_state.json"


def _load_pool_state(repo_root: Path) -> dict:
    """Load persisted pool rotation/dispatch state from .lanegate/pool_state.json."""
    path = _pool_state_path(repo_root)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_pool_state(
    repo_root: Path, pool_name: str, rr_index: int, dispatch_counts: dict[str, int]
) -> None:
    """Persist pool rotation state so the next orchestrate run continues rotation."""
    path = _pool_state_path(repo_root)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state[pool_name] = {"rr_index": rr_index, "dispatch_counts": dict(dispatch_counts)}
    _write_json_atomic(path, state)


def _last_cooldown_event(repo_root: Path) -> dict | None:
    """Return the most recent executor_cooldown event (instance/reason/ts)
    from the most recently started orchestrate run, or None.

    Resume-watch's own "waiting"/"retrying" phase is instance-agnostic — it
    just means *some* executor is hibernated for a rate limit — so without
    this, a caller (run-report's CLI text already reads this from the events
    log directly) has no way to say *which* pool instance (claude-a,
    claude-b, codex, ...) actually hit the limit.
    """
    session_ts = _resolve_run_session_ts(repo_root, None)
    if not session_ts:
        return None
    cooldowns = [e for e in _load_run_events(repo_root, session_ts) if e.get("event") == "executor_cooldown"]
    return cooldowns[-1] if cooldowns else None


def _kill_pid(pid: int, *, grace_seconds: float = 2.0) -> bool:
    """Best-effort terminate a PID this process does not own as a child.

    Since these are orphaned executor subprocesses of a dead orchestrate
    driver (not our own children), there is no Popen handle to call
    .wait()/.kill() on — only a bare PID recorded in a marker file. SIGTERM
    first, then SIGKILL if it's still alive after the grace period. Returns
    True if the PID was alive and a signal was sent, False if it was already
    gone or unsignalable (e.g. owned by another user).
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    deadline = time.time() + grace_seconds
    while time.time() < deadline and pid_alive(pid):
        time.sleep(0.05)
    if pid_alive(pid):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
    return True


def _reap_orphaned_executor_processes(
    cfg: dict, repo_root: Path, *, out_stream=None, session_ts: str | None = None
) -> list[str]:
    """Kill live executor subprocesses left behind by a dead orchestrate driver.

    TICK-246/`_collect_live_lanegate_processes` already *detects* this exact
    situation — a ticket-executor PID still alive while the orchestrator
    lock that dispatched it is dead — and `lanegate ps` prints it as
    `[ORPHANED]` for a human to kill by hand. That detection is reused
    as-is here; this only adds the missing kill + durable-event + hibernate
    steps (TICK-281), so an orphan left running unsupervised gets bounded by
    the next `lanegate orchestrate` invocation instead of running until someone
    happens to notice via `lanegate ps`.

    A driver killed via SIGKILL/OOM can't run any in-process cleanup of its
    own, so this is an external reconciliation point (the next orchestrate
    run's startup, called from the same site as
    `_reconcile_stale_executor_markers`), not a same-process try/finally.
    """
    stream = out_stream if out_stream is not None else sys.stderr
    reaped: list[str] = []
    for p in _collect_live_lanegate_processes(cfg, repo_root):
        if p["kind"] != "ticket-executor" or not p["orphaned"]:
            continue
        pid = p["pid"]
        tid = p["ticket_id"]
        print(
            f"[orchestrate] {tid}: orphaned executor PID {pid} still running with no live "
            "driver - killing",
            file=stream,
        )
        killed = _kill_pid(pid)
        _append_run_event(
            repo_root,
            session_ts,
            "orphan_reaped",
            ticket_id=tid,
            pid=pid,
            killed=killed,
            reason=p["detail"],
        )
        _remove_executor_markers(repo_root, tid)

        tickets_dir = repo_root / cfg["tickets_dir"]
        tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
        ticket = next((t for t in tickets if t.get("id") == tid), None)
        if ticket is not None and ticket.get("status") == "in_progress":
            from lanegate.lifecycle import cmd_hibernate

            cmd_hibernate(
                tid,
                cfg,
                repo_root,
                reason=(
                    f"orphaned executor PID {pid} reaped by orchestrate: its driver "
                    "process was no longer alive"
                ),
            )
        reaped.append(tid)
    return reaped


# ---------------------------------------------------------------------------
# Executor dispatch
# ---------------------------------------------------------------------------


def _pool_instance_healthy(repo_root: Path, cfg: dict, instance_name: str) -> bool:
    """Return whether a named pool instance is available for new work."""
    if _executor_is_cooling_down(repo_root, instance_name):
        return False
    tickets_dir = repo_root / cfg.get("tickets_dir", ".lanegate/tickets")
    try:
        tickets, _ = load_all_tickets(tickets_dir, cfg.get("ticket_prefix", "TICK"), cfg)
    except (OSError, ValueError):
        # A missing or malformed ticket directory must not make an otherwise
        # healthy executor unavailable. Lifecycle validation owns that error.
        return True
    marker = f"pool instance: {instance_name}"
    return not any(
        t.get("status") == "hibernated"
        and _active_rate_limit_hibernation(t.get("_body") or "")
        and marker in (t.get("_body") or "")
        for t in tickets
    )


def resolve_pool_executor(
    step: str,
    ticket: dict,
    cfg: dict,
    repo_root: Path,
    *,
    pool_name: str | None = None,
    excluded: set[str] | None = None,
    healthy_only: bool = False,
    running_counts: dict[str, int] | None = None,
    rr_index: dict[str, int] | None = None,
    dispatch_counts: dict[str, int] | None = None,
) -> str | None:
    """Resolve *step* to a healthy pool instance when a pool is available.

    This is the common pool-dispatch seam for implement, analyze, and review.
    ``healthy_only`` is used after a rate limit: it declines to select an
    exhausted instance instead of falling back to the ordinary driver.
    """
    driver_name = resolve_driver(step, ticket, cfg)
    if (step == "implement" and ticket.get("executor")) or (
        step == "review" and ticket.get("reviewer")
    ):
        return driver_name

    if pool_name is None:
        pool_name, _ = resolve_ticket_pool(cfg, ticket)
    pool_cfg = (cfg.get("pools") or {}).get(pool_name) if pool_name else None
    if not isinstance(pool_cfg, dict):
        return None if healthy_only else driver_name

    excluded = excluded or set()
    candidates = [
        name for name in pool_cfg.get("executors") or [] if name not in excluded
    ]
    if not candidates:
        return None if healthy_only else driver_name
    healthy = [name for name in candidates if _pool_instance_healthy(repo_root, cfg, name)]
    if healthy_only and not healthy:
        return None
    pick_from = healthy or candidates

    running_counts = running_counts or {}
    dispatch_counts = dispatch_counts or {}

    # TICK-286: prefer instances that still have room under their own
    # executors[name].max_parallel cap (running_counts is the caller's live
    # concurrent-dispatch count, e.g. pool_running for implement; callers
    # that don't track one, like analyze/review, pass none and this is a
    # no-op). Only fall back to a capacity-exhausted instance when literally
    # every candidate is already full — that means the global gate let more
    # work through than the pool can absorb without overloading someone, and
    # dispatching anyway (today's behavior) beats stalling the run outright.
    def _has_capacity(name: str) -> bool:
        cap = (cfg.get("executors") or {}).get(name, {}).get("max_parallel")
        return cap is None or running_counts.get(name, 0) < cap

    available = [name for name in pick_from if _has_capacity(name)]
    pick_from = available or pick_from

    if pool_cfg.get("strategy", "least-loaded") == "round-robin":
        index = (rr_index or {}).get(pool_name, 0)
        chosen = pick_from[index % len(pick_from)]
        if rr_index is not None:
            rr_index[pool_name] = index + 1
        return chosen

    return min(
        pick_from,
        key=lambda name: (running_counts.get(name, 0), dispatch_counts.get(name, 0)),
    )


# Review subagent and review-related daemon helpers live in
# orchestrate/review.py (TICK-277); re-exported here so `from
# lanegate.orchestrate import X` keeps working for every caller and test.
from .review import _git_head_sha as _git_head_sha
from .review import _invoke_cmd_review as _invoke_cmd_review
from .review import _make_error_review as _make_error_review
from .review import _minimal_cfg as _minimal_cfg
from .review import run_review_agent as run_review_agent
from .review import spawn_resume_watch_daemon as spawn_resume_watch_daemon
from .review import spawn_watch_daemon as spawn_watch_daemon


# Fix/drift-check subagents and combined-vs-split-mode helpers live in
# orchestrate/autofix.py (TICK-278); re-exported here so `from
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
_ANALYZE_SYSTEMIC_FAILURE_THRESHOLD = 2


class _Tee:
    """Write-only file-like object that mirrors writes to two streams."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, data: str) -> int:
        for stream in self._streams:
            stream.write(data)
        return len(data)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()


def _analyze_drafts(
    cfg: dict,
    repo_root: Path,
    milestone: str | None = None,
    tickets_dir=None,
    ticket_ids: set[str] | None = None,
) -> None:
    """Flip all eligible draft tickets to open via analyze.

    Skips drafts outside the active milestone filter, and outside an explicit
    ticket scope (TICK-262) when one is given — a run scoped to specific
    ticket(s) must not go analyze unrelated drafts elsewhere in the milestone
    just because none of the requested ticket(s) were ready to dispatch.
    On analyze failure, logs a warning and continues to the next draft — UNLESS
    the same failure reason repeats on consecutive drafts, which means the
    problem is systemic (bad executor config, a parsing bug, ...) rather than
    that one ticket's content. Repeating the same doomed call across every
    remaining draft just burns model cost for a guaranteed-identical failure,
    so the whole draft-analysis pass stops early in that case instead.
    """
    from lanegate.analyze import cmd_analyze
    from lanegate.ticket import load_all_tickets as _load_all_tickets

    if tickets_dir is None:
        tickets_dir = repo_root / cfg["tickets_dir"]

    tickets, _ = _load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    drafts = [
        t
        for t in tickets
        if t.get("status") == "draft"
        and (milestone is None or t.get("milestone") == milestone)
        and (ticket_ids is None or t["id"] in ticket_ids)
    ]
    last_failure_reason: str | None = None
    repeat_count = 0
    for t in drafts:
        print(f"[orchestrate] auto-analyzing draft {t['id']}")
        captured = io.StringIO()
        try:
            with contextlib.redirect_stderr(_Tee(sys.stderr, captured)):
                cmd_analyze(t["id"], cfg, repo_root)
        except (Exception, SystemExit) as exc:
            code = exc.code if isinstance(exc, SystemExit) else exc
            reason = captured.getvalue().strip() or str(exc)
            print(
                f"WARNING: analyze failed for {t['id']}: {code} — skipping",
                file=sys.stderr,
            )
            if reason and reason == last_failure_reason:
                repeat_count += 1
            else:
                last_failure_reason = reason
                repeat_count = 1
            if repeat_count >= _ANALYZE_SYSTEMIC_FAILURE_THRESHOLD:
                print(
                    f"ERROR: analyze failed with the same error on {repeat_count} consecutive "
                    f"drafts — this looks like a systemic executor/config problem, not a "
                    f"per-ticket issue. Stopping draft analysis instead of repeating the same "
                    f"failure across the rest of the queue.",
                    file=sys.stderr,
                )
                return
            continue
        last_failure_reason = None
        repeat_count = 0


def _print_draft_analysis_plan(
    cfg: dict,
    repo_root: Path,
    milestone: str | None = None,
    tickets_dir=None,
    ticket_ids: set[str] | None = None,
) -> None:
    """Print which drafts would be analyzed (dry-run mode)."""
    from lanegate.ticket import load_all_tickets as _load_all_tickets

    if tickets_dir is None:
        tickets_dir = repo_root / cfg["tickets_dir"]

    tickets, _ = _load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    drafts = [
        t
        for t in tickets
        if t.get("status") == "draft"
        and (milestone is None or t.get("milestone") == milestone)
        and (ticket_ids is None or t["id"] in ticket_ids)
    ]
    for t in drafts:
        print(f"[dry-run] would analyze draft {t['id']}")


def _pid_alive(pid: int) -> bool:
    # Delegates to the shared cross-platform probe; on Windows a plain
    # os.kill(pid, 0) would *terminate* the process being checked.
    return pid_alive(pid)


def _executor_alive(ticket: dict, cfg: dict, repo_root: Path) -> bool:
    """Return whether the recorded executor/session marker is still alive."""
    tid = ticket["id"]
    state = repo_root / ".lanegate"
    pid_path = state / f"{tid}.pid"
    session_path = state / f"{tid}.session"

    if pid_path.exists():
        try:
            return _pid_alive(int(pid_path.read_text(encoding="utf-8").strip()))
        except ValueError:
            return False

    if session_path.exists():
        try:
            started = float(session_path.read_text(encoding="utf-8").strip())
        except ValueError:
            return False
        timeout_seconds = float(cfg.get("orphan_timeout_hours", 4)) * 3600
        return (time.time() - started) < timeout_seconds

    return False


def _hibernate_orphaned(cfg: dict, repo_root: Path) -> int:
    """Hibernate in-progress tickets whose executor marker is missing or stale."""
    from lanegate.lifecycle import cmd_hibernate

    tickets_dir = repo_root / cfg["tickets_dir"]
    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    orphaned = [
        t
        for t in tickets
        if t.get("status") == "in_progress" and not _executor_alive(t, cfg, repo_root)
    ]
    if not orphaned:
        return 0

    print(
        f"[orchestrate] {len(orphaned)} orphaned in_progress ticket(s) detected from prior session"
    )
    for t in orphaned:
        branch = t.get("branch") or t["id"].lower()
        print(f"[orchestrate] hibernating {t['id']} - partial work preserved in branch {branch}")
        cmd_hibernate(t["id"], cfg, repo_root, reason="orphaned prior executor session")
    print("[orchestrate] resuming board clearing from hibernated tickets (priority-boosted)")
    return len(orphaned)


def _gather_rate_limit_texts(
    worktree_path: Path | None = None, captured_stdout: str = "", captured_stderr: str = ""
) -> list[str]:
    """Collect the raw stdout/stderr text checked for rate-limit needles.

    Shared by _is_rate_limit (boolean detection) and the hibernation reason
    builder, so the raw text that triggered detection isn't thrown away.

    stdout is checked as well as stderr: some executors (notably a ``claude``
    CLI in non-interactive/print mode) write user-facing error/JSON responses
    to stdout, not stderr, so a rate-limit message can land on either stream.

    ``captured_stdout``/``captured_stderr`` (the executor subprocess's own
    pipes, captured in-memory per call) are the only sources. This used to
    also read executor.stderr/stderr.log/.lanegate/executor.{stderr,log} out of
    the ticket's worktree, but nothing in the codebase ever wrote those files
    — they were pure agent-writable attack surface, since an executor agent
    has full write access to its own worktree and could plant rate-limit-
    shaped text there to force its own ticket (or, via rate_limit_halt, every
    in-flight ticket) into hibernation on demand (TICK-203/F14 follow-up,
    TICK-252). ``worktree_path`` stays in the signature only for call-site
    stability.
    """
    del worktree_path
    return [captured_stdout, captured_stderr]


def _is_rate_limit(
    exit_code: int,
    worktree_path: Path | None = None,
    captured_stdout: str = "",
    captured_stderr: str = "",
) -> bool:
    if _is_interrupted_exit(exit_code):
        return False
    # exit_code == 429 is NOT checked here: OS process exit codes are
    # truncated to a single byte (exit(N) -> N & 0xFF), so a subprocess that
    # tried to signal HTTP 429 would surface as 173, never 429. No executor
    # path in this codebase (subprocess-based or the ollama REST path, which
    # raises on non-2xx and is swallowed into a plain exit code) can produce
    # a literal 429 here — detection relies solely on text matching below.
    text = _rate_limit_detection_text(
        _gather_rate_limit_texts(
            worktree_path, captured_stdout=captured_stdout, captured_stderr=captured_stderr
        )
    )
    if _has_non_rate_limit_hard_error(text):
        return False
    patterns = (
        r"\byou(?:'|’)ve hit your [\w\- ]{0,24}limit\b",
        r"\b[\w\- ]{0,24}limit\b.{0,120}\b(?:try again|resets?|raise it)\b",
        r"\b(?:try again|resets?|raise it)\b.{0,120}\b[\w\- ]{0,24}limit\b",
        r"\brate[_ -]?limit[_ -]?exceeded\b",
        r"\btoo many requests\b",
        r"\bquota (?:exceeded|limit|reached)\b",
        r"\bpurchase more credits\b",
        r"\bclaude\.ai subscription\b",
        r"\bretry-after\s*:\s*\d+\b",
        r"\b429\b.{0,120}\b(?:too many requests|rate limit|quota)\b",
        r"\b(?:too many requests|rate limit|quota)\b.{0,120}\b429\b",
        r"\b(?:error|hit|reached|exceeded|retry|throttled|throttle)\b.{0,120}\brate limit\b",
        r"\brate limit\b.{0,120}\b(?:error|hit|reached|exceeded|retry|throttled|throttle)\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _rate_limit_detection_text(texts: list[str]) -> str:
    """Return executor-output text relevant for rate-limit classification.

    Codex-style failures can echo large generated diffs or source excerpts
    before ending with ``turn interrupted``.  A bare phrase like "rate limit"
    inside that code is not an executor quota error, so diff/code-looking lines
    are filtered before the stricter classifier runs.
    """
    lines: list[str] = []
    for text in texts:
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith(("+++", "---", "@@")):
                continue
            if line[:1] in {"+", "-"}:
                continue
            lines.append(line)
    return "\n".join(lines).lower()


_MAX_RATE_LIMIT_EXCERPT = 2000  # cap raw text embedded in the hibernation reason

# Must match resume_watch._RATE_LIMIT_MARKER — used there to tell "hibernated
# because of a rate limit" apart from other halt reasons, and here (TICK-089)
# to tell whether a specific pool instance is currently rate-limited.
_RATE_LIMIT_MARKER = "rate limit or quota interruption"
_NON_RATE_LIMIT_HARD_ERROR_PATTERNS = (
    r"\binvalid_request_error\b",
    r"\bstatus[\"']?\s*:\s*400\b",
    r"\brequires a newer version of codex\b",
    r"\bmodel metadata\b.{0,120}\bnot found\b",
    r"\bunknown model\b",
    r"\bmodel .* does not exist\b",
)


def _has_non_rate_limit_hard_error(text: str) -> bool:
    lowered = text.lower()
    return any(re.search(pattern, lowered) for pattern in _NON_RATE_LIMIT_HARD_ERROR_PATTERNS)


def _is_executor_setup_error(
    exit_code: int,
    worktree_path: Path | None = None,
    captured_stdout: str = "",
    captured_stderr: str = "",
) -> bool:
    """True when retrying more tickets would repeat the same executor setup failure."""
    if exit_code == 0 or _is_interrupted_exit(exit_code):
        return False
    text = _rate_limit_detection_text(
        _gather_rate_limit_texts(
            worktree_path, captured_stdout=captured_stdout, captured_stderr=captured_stderr
        )
    )
    return _has_non_rate_limit_hard_error(text)


def _executor_setup_error_reason(
    exit_code: int,
    worktree_path: Path | None = None,
    captured_stdout: str = "",
    captured_stderr: str = "",
) -> str:
    raw = "\n".join(
        t
        for t in _gather_rate_limit_texts(
            worktree_path, captured_stdout=captured_stdout, captured_stderr=captured_stderr
        )
        if t.strip()
    ).strip()
    header = f"executor setup error (executor exited {exit_code})"
    if not raw:
        return header
    if len(raw) > _MAX_RATE_LIMIT_EXCERPT:
        raw = raw[-_MAX_RATE_LIMIT_EXCERPT:]
        raw = f"...(truncated)...\n{raw}"
    return f"{header}\n\nRaw executor output:\n{raw}"


_AUTH_ERROR_PATTERNS = (
    r"\bauthentication required\b",
    r"\bauthentication failed or timed out\b",
    r"\bplease visit the url to log in\b",
)


def _is_auth_error(
    exit_code: int,
    worktree_path: Path | None = None,
    captured_stdout: str = "",
    captured_stderr: str = "",
) -> bool:
    """True when the executor exited because it needs interactive re-authentication.

    Distinguishes an expired OAuth session (e.g. agy's Google device-code
    prompt) from an ordinary implementation failure, so orchestrate can
    hibernate with an actionable reason and cool down the instance instead of
    burning retries on a prompt that non-interactive dispatch can never answer.
    """
    if exit_code == 0 or _is_interrupted_exit(exit_code):
        return False
    text = _rate_limit_detection_text(
        _gather_rate_limit_texts(
            worktree_path, captured_stdout=captured_stdout, captured_stderr=captured_stderr
        )
    )
    return any(re.search(pattern, text) for pattern in _AUTH_ERROR_PATTERNS)


def _auth_error_reason(
    exit_code: int,
    worktree_path: Path | None = None,
    captured_stdout: str = "",
    captured_stderr: str = "",
) -> str:
    raw = "\n".join(
        t
        for t in _gather_rate_limit_texts(
            worktree_path, captured_stdout=captured_stdout, captured_stderr=captured_stderr
        )
        if t.strip()
    ).strip()
    header = f"executor requires re-authentication (executor exited {exit_code})"
    if not raw:
        return header
    if len(raw) > _MAX_RATE_LIMIT_EXCERPT:
        raw = raw[-_MAX_RATE_LIMIT_EXCERPT:]
        raw = f"...(truncated)...\n{raw}"
    return f"{header}\n\nRaw executor output:\n{raw}"


def _active_rate_limit_hibernation(body: str) -> bool:
    return _RATE_LIMIT_MARKER in body and not _has_non_rate_limit_hard_error(body)


def _rate_limit_reason(
    exit_code: int,
    worktree_path: Path | None = None,
    captured_stdout: str = "",
    captured_stderr: str = "",
) -> str:
    """Build a hibernation reason that includes the raw executor error text.

    Without this, the boolean-only detection in _is_rate_limit discards
    whatever the executor actually printed (e.g. a reset-time hint), so a
    future auto-resume watcher would have nothing to parse.
    """
    raw = "\n".join(
        t
        for t in _gather_rate_limit_texts(
            worktree_path, captured_stdout=captured_stdout, captured_stderr=captured_stderr
        )
        if t.strip()
    ).strip()
    header = f"{_RATE_LIMIT_MARKER} (executor exited {exit_code})"
    if not raw:
        return header
    if len(raw) > _MAX_RATE_LIMIT_EXCERPT:
        raw = raw[-_MAX_RATE_LIMIT_EXCERPT:]
        raw = f"...(truncated)...\n{raw}"
    return f"{header}\n\nRaw executor output:\n{raw}"


def _flat_note_name(path: str) -> str:
    return f"{path.replace('/', '_')}.md"


def write_ticket_notes(repo_root: Path, tid: str, notes: str) -> Path:
    """Persist per-ticket review findings and operational notes into .lanegate/notes/<tid>.md."""
    notes_dir = repo_root / ".lanegate" / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    path = notes_dir / f"{tid}.md"
    path.write_text(notes.strip() + "\n", encoding="utf-8")
    return path


def _collect_prior_notes(ticket: dict, repo_root: Path) -> str:
    notes_dir = repo_root / ".lanegate" / "notes"
    recovery_path = repo_root / ".lanegate" / "recovery" / f"{ticket['id']}.md"
    ticket_note_path = notes_dir / f"{ticket['id']}.md"
    parts: list[str] = []
    if ticket.get("status") in ("hibernated", "needs_review") and recovery_path.exists():
        recovery_text = recovery_path.read_text(encoding="utf-8", errors="replace").strip()
        if recovery_text:
            parts.append("## Hibernation Recovery Context\n\n" + recovery_text)
    if ticket_note_path.exists():
        tn_text = ticket_note_path.read_text(encoding="utf-8", errors="replace").strip()
        if tn_text:
            parts.append("## Ticket Operational Notes\n\n" + tn_text)
    for touched in ticket.get("touches") or []:
        note_path = notes_dir / _flat_note_name(str(touched))
        if note_path.exists():
            text = note_path.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                parts.append(text)
    if not parts:
        return ""
    return "## Prior Agent Notes\n\n" + "\n\n".join(parts)


def _conflicted_files(worktree_path: Path) -> list[str]:
    """Return files with unresolved conflict markers in the active rebase."""

    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=worktree_path,
            capture_output=True,
            text=True, encoding="utf-8",
        )
    except FileNotFoundError:
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _extract_conflict_hunks(text: str) -> list[str]:
    hunks: list[str] = []
    current: list[str] = []
    in_hunk = False
    for line in text.splitlines():
        if line.startswith("<<<<<<<"):
            in_hunk = True
            current = [line]
            continue
        if in_hunk:
            current.append(line)
            if line.startswith(">>>>>>>"):
                hunks.append("\n".join(current))
                current = []
                in_hunk = False
    if current:
        hunks.append("\n".join(current))
    return hunks


def _format_conflict_detail(worktree_path: Path, conflict_files: list[str]) -> str:
    """Return only conflict hunks for executor resume context."""

    sections = [
        "## Conflict resolution required",
        "",
        "The following files have merge conflicts from rebasing onto main.",
        "Resolve ONLY the conflict markers shown. Do not rewrite unrelated code.",
        "After resolving, the implementation should still satisfy the close criteria.",
    ]
    for rel_path in conflict_files:
        path = worktree_path / rel_path
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            sections.extend(["", f"### {rel_path}", f"(could not read file: {exc})"])
            continue

        hunks = _extract_conflict_hunks(text)
        sections.extend(["", f"### {rel_path}"])
        if hunks:
            for idx, hunk in enumerate(hunks, start=1):
                sections.extend(["", f"#### Hunk {idx}", "```", hunk, "```"])
        else:
            sections.append("(no conflict marker hunks found)")
    return "\n".join(sections)


def _worktree_is_dirty(worktree_path: Path) -> bool:
    """Return True if the worktree has uncommitted tracked changes.

    `git rebase` refuses to run against these, so callers must check this
    before attempting a resume rebase rather than treating the resulting
    git error as a generic rebase failure.
    """
    if not worktree_path.exists():
        return False
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree_path,
        capture_output=True,
        text=True, encoding="utf-8",
    )
    return any(line and not line.startswith("??") for line in result.stdout.splitlines())


def _ticket_has_real_progress(worktree_path: Path) -> bool:
    """Heuristic (TICK-263) for whether a rate-limited ticket is worth
    resuming on a healthy sibling pool instance rather than hibernating.

    True when the worktree shows real work-in-progress: commits ahead of
    main, or uncommitted tracked changes. False is more consistent with a
    stuck/looping session that burned quota without producing anything —
    that case should still hibernate rather than risk depleting a second
    pool instance's quota on the same bad ticket (TICK-258).
    """
    if not worktree_path.exists():
        return False
    try:
        if check_worktree_has_commits(worktree_path):
            return True
    except FileNotFoundError:
        return False
    return _worktree_is_dirty(worktree_path)


def _run_rebase(worktree_path: Path, *, base: str | None = None) -> tuple[str, str]:
    """Return (clean|conflict|error, detail) after rebasing onto the trunk."""

    if not worktree_path.exists():
        return "error", f"missing worktree: {worktree_path}"
    base = base or resolve_trunk_branch({}, worktree_path)
    result = subprocess.run(
        ["git", "rebase", base],
        cwd=worktree_path,
        capture_output=True,
        text=True, encoding="utf-8",
    )
    if result.returncode == 0:
        return "clean", result.stdout.strip()

    conflict_files = _conflicted_files(worktree_path)
    if conflict_files:
        return "conflict", _format_conflict_detail(worktree_path, conflict_files)

    detail = result.stderr.strip() or result.stdout.strip() or f"git rebase {base} failed"
    return "error", detail


def _continue_rebase(worktree_path: Path, conflict_files: list[str]) -> tuple[bool, str]:
    if not worktree_path.exists():
        return False, f"missing worktree: {worktree_path}"
    try:
        add_cmd = ["git", "add", "--", *conflict_files] if conflict_files else ["git", "add", "-u"]
        add_result = subprocess.run(add_cmd, cwd=worktree_path, capture_output=True, text=True, encoding="utf-8")
    except FileNotFoundError:
        return False, f"missing worktree: {worktree_path}"
    if add_result.returncode != 0:
        detail = add_result.stderr.strip() or add_result.stdout.strip() or "git add failed"
        return False, detail

    env = os.environ.copy()
    env.setdefault("GIT_EDITOR", "true")
    try:
        result = subprocess.run(
            ["git", "rebase", "--continue"],
            cwd=worktree_path,
            capture_output=True,
            text=True, encoding="utf-8",
            env=env,
        )
    except FileNotFoundError:
        return False, f"missing worktree: {worktree_path}"
    if result.returncode == 0:
        return True, result.stdout.strip()
    detail = result.stderr.strip() or result.stdout.strip() or "git rebase --continue failed"
    return False, detail


def _abort_rebase(worktree_path: Path) -> None:
    try:
        subprocess.run(["git", "rebase", "--abort"], cwd=worktree_path, capture_output=True, text=True, encoding="utf-8")
    except FileNotFoundError:
        pass


def _prepend_context(ticket: dict, *sections: str) -> dict:
    parts = [section.strip() for section in sections if section and section.strip()]
    if not parts:
        return ticket
    updated = dict(ticket)
    body = updated.get("_body", "")
    updated["_body"] = "\n\n".join(parts + [body])
    return updated


_SCOPE_ONLY_NEEDS_REVIEW_REASON = re.compile(
    r"committed files outside touches list:\s*(?P<paths>.+)"
)


def _scope_only_needs_review_files(ticket: dict) -> set[str] | None:
    """Return the declared scope-drift files for a narrowly recoverable ticket."""
    if ticket.get("status") != "needs_review":
        return None
    body = ticket.get("_body") or ""
    header = "## Needs Review Reason"
    if header not in body:
        return None
    reason = body.split(header, 1)[1].lstrip("\n")
    if "\n##" in reason:
        reason = reason.split("\n##", 1)[0]
    match = _SCOPE_ONLY_NEEDS_REVIEW_REASON.fullmatch(reason.strip())
    if not match:
        return None
    paths = {path.strip() for path in match.group("paths").split(",") if path.strip()}
    return paths or None


def _record_auto_claimed_touches(ticket: dict, paths: set[str]) -> None:
    """Persist a human-readable audit trail for an automatic scope expansion."""
    if not paths:
        return
    entry = "- Auto-claimed after implementation: " + ", ".join(f"`{path}`" for path in sorted(paths))
    header = "## Scope Updates"
    body = (ticket.get("_body") or "").rstrip()
    if header not in body:
        ticket["_body"] = f"{body}\n\n{header}\n\n{entry}\n"
        return
    before, _, remainder = body.partition(header)
    section, separator, following = remainder.partition("\n##")
    updated_section = section.rstrip() + "\n" + entry
    ticket["_body"] = before.rstrip() + f"\n\n{header}\n" + updated_section
    if separator:
        ticket["_body"] += separator + following
    ticket["_body"] += "\n"


def recover_scope_only_needs_review_tickets(
    cfg: dict,
    repo_root: Path,
    *,
    milestone: str | None = None,
    ticket_ids: set[str] | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Return scope-only paused tickets to the normal review/merge pipeline.

    ``needs_review`` is always a human-safety state by default.  This opt-in
    exception recognizes only the exact stale-touches reason emitted by the
    touched-files guard, re-derives the branch diff, atomically claims every
    missing path, and repeats the intervening safety gates before review.
    """
    if cfg.get("auto_claim_touches") is not True:
        return []

    from lanegate.claim_file import claim_files
    from lanegate.concurrency import SafeguardLockHeld, safeguard_lock
    from lanegate.lifecycle import _commit_generated_ticket_write, _mark_needs_review, cmd_reopen, cmd_review
    from lanegate.safeguards import run_safeguards
    from lanegate.ticket import write_ticket

    tickets_dir = repo_root / cfg["tickets_dir"]
    all_tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    recovered: list[str] = []

    for ticket in all_tickets:
        tid = ticket["id"]
        if milestone is not None and ticket.get("milestone") != milestone:
            continue
        if ticket_ids is not None and tid not in ticket_ids:
            continue
        recorded_missing = _scope_only_needs_review_files(ticket)
        if recorded_missing is None:
            continue

        wt_value = ticket.get("worktree")
        wt = Path(wt_value) if wt_value else None
        if wt is None or not wt.exists():
            print(f"[orchestrate] {tid}: scope recovery skipped — missing worktree", file=sys.stderr)
            continue

        declared = set(ticket.get("touches") or [])
        if "*" in declared:
            continue
        committed = _committed_files(wt)
        unexpected = committed - declared
        unexpected = {path for path in unexpected if not is_paired_test_file(path, declared)}
        if not unexpected or unexpected != recorded_missing:
            print(
                f"[orchestrate] {tid}: scope recovery skipped — worktree diff no longer "
                "matches the recorded scope-drift reason",
                file=sys.stderr,
            )
            continue

        blocked = [
            f"{path} [{rule}]"
            for path in sorted(committed)
            for is_blocked, rule in [_is_blocked_file(path, cfg.get("protected_paths") or [])]
            if is_blocked
        ]
        if blocked:
            print(
                f"[orchestrate] {tid}: scope recovery skipped — hard-blocked paths: "
                + "; ".join(blocked),
                file=sys.stderr,
            )
            continue

        sensitive_patterns = cfg.get("security_sensitive_paths") or []
        sensitive = [
            path
            for path in sorted(committed)
            if any(
                fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(Path(path).name, pattern)
                for pattern in sensitive_patterns
            )
        ]
        if sensitive:
            print(
                f"[orchestrate] {tid}: scope recovery skipped — security-sensitive paths: "
                + ", ".join(sensitive),
                file=sys.stderr,
            )
            continue

        if dry_run:
            print(
                f"[orchestrate] {tid}: would auto-claim {sorted(unexpected)} and return it to review"
            )
            recovered.append(tid)
            continue

        claimed, detail = claim_files(sorted(unexpected), tid, cfg, repo_root)
        if not claimed:
            print(
                f"[orchestrate] {tid}: scope recovery skipped — could not claim files: {detail}",
                file=sys.stderr,
            )
            continue

        try:
            cmd_reopen(tid, cfg, repo_root)
        except SystemExit:
            # cmd_reopen leaves its actionable conflict/error detail on the
            # ticket and stderr; never let one paused ticket abort the board.
            continue

        refreshed, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
        restored = next((item for item in refreshed if item["id"] == tid), None)
        if not restored or restored.get("status") != "code_complete":
            continue

        # cmd_reopen clears the old Needs Review section, so write the scope
        # audit after it restores the ticket rather than letting that cleanup
        # discard the newly recorded explanation.
        _record_auto_claimed_touches(restored, unexpected)
        write_ticket(restored)
        _commit_generated_ticket_write(
            repo_root, Path(restored["_path"]), tid, "auto-claimed touches", cfg
        )

        try:
            with safeguard_lock(repo_root, tid):
                safeguards_passed, safeguard_reason = run_safeguards(
                    "pre_complete", restored, cfg, wt
                )
        except SafeguardLockHeld as exc:
            _mark_needs_review(restored, cfg, repo_root, reason=f"pre_complete safeguards unavailable: {exc}")
            continue
        if not safeguards_passed:
            _mark_needs_review(
                restored, cfg, repo_root, reason=f"pre_complete safeguards failed: {safeguard_reason}"
            )
            continue

        findings = _run_static_analysis(wt, cfg)
        threshold = int((cfg.get("static_analysis") or {}).get("threshold", 0))
        if findings and len(findings) > threshold:
            _mark_needs_review(
                restored,
                cfg,
                repo_root,
                reason=f"static analysis findings ({len(findings)}): {'; '.join(findings[:5])}",
            )
            continue

        acceptance_findings = _run_acceptance_contract_audit(restored, repo_root, cfg)
        if acceptance_findings and resolve_acceptance_contract_mode(cfg) == "blocker":
            _invoke_cmd_review(
                cmd_review,
                tid,
                cfg,
                repo_root,
                verdict="changes_requested",
                summary="acceptance-contract audit failed",
                findings="\n".join(acceptance_findings),
            )
            continue

        print(
            f"[orchestrate] {tid}: auto-claimed {sorted(unexpected)} — returning to review",
            file=sys.stderr,
        )
        try:
            cmd_review(tid, cfg, repo_root)
        except SystemExit:
            # Review failures are persisted by cmd_review; other tickets still
            # need a chance to run.
            continue
        recovered.append(tid)

    return recovered


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
        tickets: optional explicit list of ticket IDs (TICK-262) restricting
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
        pool: name of a `pools:` entry (TICK-089) to draw executor instances
            from; falls back to `cfg.default_pool` when not given, or to plain
            single-executor dispatch when neither is set.
    """
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
    store_orchestrate_args(repo_root, args_to_store)

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
            print(
                "ERROR: no milestone specified and no default_milestone in .lanegate.yml.\n"
                "Run with --milestone <m> or --all to clear tickets across all milestones.",
                file=sys.stderr,
            )
            sys.exit(1)

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

    # Resolve explicit ticket scope (TICK-262): composes with milestone above
    # rather than replacing it — an id must still pass the usual eligibility
    # filtering (status/deps/lock) in next_batch() to actually be dispatched.
    effective_ticket_ids: set[str] | None = None
    if tickets:
        effective_ticket_ids = {tid.strip() for tid in tickets if tid and tid.strip()}
        if not effective_ticket_ids:
            effective_ticket_ids = None

    if effective_ticket_ids:
        scope_tickets_dir = repo_root / cfg.get("tickets_dir", ".lanegate/tickets")
        scope_all_tickets, _ = load_all_tickets(scope_tickets_dir, cfg["ticket_prefix"])
        known_ids = {t["id"] for t in scope_all_tickets}
        unknown_ids = sorted(effective_ticket_ids - known_ids)
        if unknown_ids:
            print(
                f"WARNING: --tickets includes unknown ticket id(s): {', '.join(unknown_ids)}",
                file=sys.stderr,
            )

    max_parallel_detail = resolve_max_parallel_detail(cfg, override=max_parallel)
    effective_max = int(max_parallel_detail["value"])

    logs_dir = repo_root / ".lanegate" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    session_ts = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    log_path = logs_dir / f"orchestrate-{session_ts}.log"

    # Keep 10 most recent in logs_dir; move older to archive, purge archive after 30 days.
    archive_dir = logs_dir / "archive"
    archive_dir.mkdir(exist_ok=True)
    cutoff = datetime.datetime.now() - datetime.timedelta(days=30)
    for f in archive_dir.glob("orchestrate-*.log"):
        if datetime.datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            f.unlink(missing_ok=True)
    old_logs = sorted(logs_dir.glob("orchestrate-*.log"))
    for old in old_logs[:-9]:
        old.rename(archive_dir / old.name)

    report_session_ts = None if dry_run else session_ts
    if report_session_ts is not None:
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
        )

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
                # Detect and kill orphaned executor children (TICK-281) *before*
                # acquiring the lock below — once this process holds the lock,
                # `_collect_live_lanegate_processes` sees a live orchestrator again
                # and would no longer flag a prior driver's abandoned child as
                # orphaned, even though this run never dispatched it.
                reaped = _reap_orphaned_executor_processes(
                    cfg, repo_root, out_stream=sys.stderr, session_ts=report_session_ts
                )
                if reaped:
                    print(
                        f"[orchestrate] reaped {len(reaped)} orphaned executor "
                        f"process(es) from a dead driver: {', '.join(reaped)}"
                    )
                try:
                    pid = acquire_orchestrator_lock(repo_root)
                    print(f"[orchestrate] lock acquired (PID {pid})")
                except OrchestratorLockError as e:
                    print(f"ERROR: {e}", file=sys.stderr)
                    sys.exit(1)

            if recover and not dry_run:
                _hibernate_orphaned(cfg, repo_root)

            run_status = "completed"
            try:
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
                    _orig_out=_orig_out,
                    _log_f=_log_f,
                    session_ts=report_session_ts,
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
    verbose: bool = False,
    pool_name: str | None = None,
    ticket_ids: set[str] | None = None,
    _orig_out=None,
    _log_f=None,
    session_ts: str | None = None,
) -> None:
    """Inner board-clearing loop — separated for testability.

    Args:
        ticket_ids: optional explicit set of ticket IDs (TICK-262) restricting
            dispatch to exactly these IDs; composes with milestone.
        verbose: when True, stream full executor output to terminal; when False
            (default), print compact per-ticket status lines only and route
            executor output to the log file only.
        pool_name: name of a `pools:` entry (TICK-089) to draw executor
            instances from for tickets that don't already carry an explicit
            `ticket.executor` override. None (default) leaves dispatch
            entirely to the existing single-executor resolution.
        _orig_out: the real terminal stream (pre-tee); used for compact status
            lines and batch summaries.  Falls back to sys.stdout when None
            (e.g. during unit tests that call _drain_loop directly).
        _log_f: the open log file; used as the exclusive destination for
            executor output in compact mode.  Falls back to None (no redirect)
            when absent.
        session_ts: this run's session timestamp (TICK-244), used to append
            durable events to `.lanegate/logs/orchestrate-<ts>.events.jsonl` for
            `lanegate run-report`. None (the default, e.g. dry-run or direct
            test calls) disables event recording.
    """
    from lanegate.lifecycle import (
        MergeFailedError,
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

    # --- executor pool dispatch (TICK-089) ---
    # pool_running/pool_rr_index are only ever mutated from the main thread
    # (inside submit(), or the serial for-loop below) — worker threads never
    # touch them, so no lock is needed even under max_parallel > 1.
    pool_running: dict[str, int] = {}

    # Load persisted rotation state (TICK-268): rr_index continues from where
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
        down (TICK-090) nor hibernated-for-rate-limit (TICK-089)."""
        pool_cfg = cfg["pools"][name]
        return any(
            _pool_instance_healthy(repo_root, cfg, candidate)
            for candidate in pool_cfg["executors"]
        )

    _COOLDOWN_POLL_SECONDS = 30
    _COOLDOWN_MAX_POLLS = 4

    def _wait_for_pool_capacity(name: str) -> bool:
        """Block in bounded steps until some instance in pool *name* is no
        longer cooling down, instead of either tight-looping or halting the
        whole run the instant every instance is exhausted at once (TICK-090).
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

    def _select_pool_instance(name: str) -> str:
        # TICK-286's per-instance max_parallel capacity preference now lives
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
        assert instance is not None  # A validated pool always has executors.
        return instance

    # Per-ticket count of sibling-retry attempts (TICK-263) made so far *this
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
        within this run, instead of hibernating (TICK-263).

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

    # Ticket ID -> pool-selected instance for the current dispatch. Deliberately
    # NOT persisted onto the ticket file: run_ticket reloads a fresh copy of
    # the ticket from disk after cmd_start (to pick up cmd_start's own
    # updates), which would discard an in-memory-only assignment anyway, and
    # writing an arbitrary named-instance string into ticket.executor trips a
    # pre-existing validate_ticket gap (see TICK-247) that quarantines the
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
        if not pool_name or ticket.get("executor"):
            return None
        instance = _select_pool_instance(pool_name)
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
            # Supervised/manual autonomy permit the normal LLM review/fix
            # cycle, but an approved result still requires an explicit human
            # merge. This check also covers tickets approved during a prior
            # run (e.g. via `lanegate fix`), which reach this board-clear scan
            # without passing a worker.
            if resolve_autonomy(cfg, ticket) != "full":
                continue
            if verbose:
                print(f"[orchestrate] auto-merging {ticket['id']} (approved, no human gate)")
            try:
                cmd_merge(ticket["id"], cfg, repo_root)
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
                    merged_any = True
                    continue
                reason = f"auto-merge failed: {exc}"
                print(f"[orchestrate] {tid} merge failed — downgrading to needs_review", file=sys.stderr)
                downgrade_approved_review_to_needs_review(ticket, reason)
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
        if current in ("code_complete", "in_review", "needs_review"):
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

        batch = next_batch(cfg, repo_root, milestone=milestone, ticket_ids=ticket_ids)

        # Ready open/hibernated work always dispatches ahead of newly-created
        # drafts — only spend a loop iteration analyzing drafts when there is
        # nothing already dispatchable, so a steady trickle of new drafts can
        # never starve an existing backlog.
        if auto_analyze and not batch:
            if dry_run:
                _print_draft_analysis_plan(
                    cfg, repo_root, milestone=milestone, tickets_dir=tickets_dir, ticket_ids=ticket_ids
                )
            else:
                _analyze_drafts(
                    cfg, repo_root, milestone=milestone, tickets_dir=tickets_dir, ticket_ids=ticket_ids
                )
                batch = next_batch(cfg, repo_root, milestone=milestone, ticket_ids=ticket_ids)

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
                            print(f"  {holder_id}: inspect ticket status, then rerun: lanegate orchestrate")
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

        def run_ticket(ticket: dict) -> bool:
            nonlocal executor_setup_halt, interrupt_halt, rate_limit_halt
            tid = ticket["id"]

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

            # --- start ---
            if verbose:
                print(f"[orchestrate] starting {tid}")
            else:
                _status(tid, "starting", orig_out, _log_f)
            was_hibernated = ticket.get("status") == "hibernated"
            # cmd_start reattaches the existing worktree (instead of creating a
            # fresh one) for both hibernated and needs_review tickets — the
            # rebase-onto-main trust check below must fire for the same set,
            # not just the rate-limit hibernation case, since a reattached
            # needs_review worktree can be just as stale relative to main.
            is_resuming_worktree = ticket.get("status") in ("hibernated", "needs_review")
            prior_notes = _collect_prior_notes(ticket, repo_root)
            if verbose or _log_f is None or max_parallel > 1:
                cmd_start(tid, cfg, repo_root, interactive=False)
            else:
                with contextlib.redirect_stdout(_log_f):
                    cmd_start(tid, cfg, repo_root, interactive=False)

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
            conflict_retry = False
            rebase_conflict_files: list[str] = []
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

                    if run_rebase_fix_agent(fresh_ticket, cfg, repo_root, wt, rebase_detail):
                        print(
                            f"[orchestrate] {tid}: rebase conflict automatically resolved by fix agent",
                            file=sys.stderr,
                        )
                    else:
                        conflict_retry = True
                        rebase_conflict_files = _conflicted_files(wt)
                        resume_context = (
                            "## Conflict-aware resume\n\n"
                            "Main has changed since this ticket was hibernated. "
                            "Resolve the conflicted hunks below and complete only the missing work.\n\n"
                            f"{rebase_detail}"
                        )
                        if verbose:
                            print(f"[orchestrate] {tid}: conflict-aware resume retry")
                elif rebase_state == "error":
                    reason = f"cannot run hibernated resume check: {rebase_detail}"
                    print(f"ERROR: {reason} for {tid} - marking needs_review", file=sys.stderr)
                    if not verbose:
                        _status(tid, "needs_review", orig_out, _log_f)
                    cmd_needs_review(tid, cfg, repo_root, reason=reason)
                    _log_outcome(tid, "needs_review", reason=reason)
                    return True
            fresh_ticket = _prepend_context(fresh_ticket, prior_notes, resume_context)

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

            # --- sibling-retry-on-rate-limit (TICK-263) ---
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
                    # (TICK-090) — pool_instance when pool dispatch picked
                    # one, else whatever single executor this ticket resolved
                    # to. Structured file state under .lanegate/executors/ so
                    # `lanegate executor status`/`reset` and pool dispatch can
                    # both see it without scraping ticket bodies.
                    cooldown_instance = pool_instance or _resolve_driver_route(
                        cfg, fresh_ticket
                    )["implement"]
                    if cooldown_instance != cooldown_written_for:
                        # Not already written by the sibling-retry decision
                        # above (TICK-263) — either no retry was attempted, or
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
                    if cfg.get("on_rate_limit") == "resume":
                        print(
                            f"[orchestrate] {tid}: rate limit hit — work preserved, hibernating.\n"
                            f"  on_rate_limit=resume — spawning background watcher to resume automatically.",
                            file=sys.stderr,
                        )
                    else:
                        print(
                            f"[orchestrate] {tid}: rate limit hit — work preserved, hibernating for resume.\n"
                            f"  Check your API quota/billing, then re-run: lanegate orchestrate",
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
                if _is_interrupted_exit(exit_code):
                    reason = _interrupted_exit_reason(exit_code)
                    interrupt_halt = True
                    print(
                        f"[orchestrate] {tid}: {reason} — work preserved, hibernating.\n"
                        f"  Re-run when ready: lanegate orchestrate",
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
                    print(
                        f"[orchestrate] {tid}: executor requires re-authentication — "
                        "work preserved, hibernating.\n"
                        "  Re-authenticate the CLI (e.g. run it once interactively to "
                        "complete the login flow), then re-run: lanegate orchestrate",
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
                    print(
                        f"[orchestrate] {tid}: executor setup/configuration error — "
                        "work preserved, hibernating and halting this run.\n"
                        f"  Fix the executor/model issue, then re-run: lanegate orchestrate",
                        file=sys.stderr,
                    )
                    if not verbose:
                        _status(tid, "hibernated", orig_out, _log_f)
                    cmd_hibernate(tid, cfg, repo_root, reason=reason)
                    _log_outcome(tid, "hibernated", reason=reason)
                    return True
                if conflict_retry:
                    _abort_rebase(wt)
                    reason = f"conflict-aware retry failed (executor exited {exit_code})"
                    print(
                        f"[orchestrate] {tid}: conflict-aware retry failed (exit {exit_code}) — needs human review.\n"
                        f"  Rebase was aborted. Inspect the worktree at {wt}, then run:\n"
                        f"    lanegate needs-review {tid}   (if manual fix needed)\n"
                        f"    lanegate reopen {tid}          (to re-queue for automatic retry)",
                        file=sys.stderr,
                    )
                    if not verbose:
                        _status(tid, "needs_review", orig_out, _log_f)
                    cmd_needs_review(tid, cfg, repo_root, reason=reason)
                    _log_outcome(tid, "needs_review", reason=reason)
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

                rich_reason = "\n\n".join(reason_parts)
                print(
                    f"ERROR: {tid} executor exited {exit_code} — marked as failed, batch continues.{log_hint}\n"
                    f"  Re-run with --verbose to see executor output in the terminal.\n"
                    f"  After fixing the issue: lanegate reopen {tid} && lanegate orchestrate",
                    file=sys.stderr,
                )
                if not verbose:
                    _status(tid, "failed", orig_out, _log_f)
                cmd_fail(tid, cfg, repo_root, reason=rich_reason)
                _log_outcome(tid, "failed", reason=base_reason)
                return True

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

            if conflict_retry:
                continued, detail = _continue_rebase(wt, rebase_conflict_files)
                if not continued:
                    _abort_rebase(wt)
                    reason = f"conflict-aware rebase continue failed: {detail}"
                    print(
                        f"[orchestrate] {tid}: {reason} — needs human review.",
                        file=sys.stderr,
                    )
                    if not verbose:
                        _status(tid, "needs_review", orig_out, _log_f)
                    cmd_needs_review(tid, cfg, repo_root, reason=reason)
                    _log_outcome(tid, "needs_review", reason=reason)
                    return True

            commit_worktree_changes(wt, tid)

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
                reason = "executor exited 0 but produced no commits"
                print(
                    f"ERROR: {tid} — executor exited 0 but made no commits. Marked as failed, batch continues.\n"
                    f"  Common causes:\n"
                    f"    - Agent hit a permission prompt and could not proceed (check log)\n"
                    f"    - Agent found nothing to do (ticket may already be complete)\n"
                    f"    - Agent crashed silently (check log for tracebacks)\n"
                    + (f"  Executor log: {log_path_hint}\n" if log_path_hint else "")
                    + f"  Re-run with --verbose to see executor output in the terminal.\n"
                    f"  After investigating: lanegate reopen {tid} && lanegate orchestrate",
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
                    # TICK-245: a committed file that's the natural paired test file
                    # for an already-touched module is not scope drift.
                    unexpected = {f for f in unexpected if not is_paired_test_file(f, allowed)}
                    if unexpected:
                        if cfg.get("auto_claim_touches") is True:
                            from lanegate.claim_file import claim_files
                            from lanegate.ticket import write_ticket

                            claimed, detail = claim_files(sorted(unexpected), tid, cfg, repo_root)
                            if not claimed:
                                reason = f"could not auto-claim additional touched files: {detail}"
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
                        f"  Hibernating for retry. Re-run: lanegate orchestrate",
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
                    fixed = run_auto_fix_cycle(post_ticket, cfg, repo_root, wt)
                    if fixed:
                        if verbose:
                            print(f"[orchestrate] {tid}: auto-fix cycle reached approved")
                        all_t_fixed, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
                        fixed_ticket = next((t for t in all_t_fixed if t["id"] == tid), post_ticket)
                        if resolve_autonomy(cfg, fixed_ticket) != "full":
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
                        if not verbose:
                            _status(tid, "changes_requested", orig_out, _log_f)
                        _log_outcome(tid, "changes_requested", reason="review requested changes")
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
                        or resolve_autonomy(cfg, post_ticket) != "full"
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
                        f"  Pausing for manual review. Re-run: lanegate orchestrate",
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
                review_executor = resolve_driver("review", review_ticket, cfg)
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
                    approved = run_review_agent(review_ticket, repo_root, worktree_path=wt, cfg=cfg)
                    if not approved:
                        # The review agent persists its findings on disk —
                        # reload so the fix agent sees the newest ones rather
                        # than a copy of the ticket that predates the review.
                        all_t3, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
                        reloaded = next((t for t in all_t3 if t["id"] == tid), review_ticket)
                        fixed = run_auto_fix_cycle(reloaded, cfg, repo_root, wt)
                        if fixed:
                            if verbose:
                                print(f"[orchestrate] {tid}: auto-fix cycle reached approved")
                        else:
                            print(
                                f"[orchestrate] {tid}: review requested changes — pausing",
                                file=sys.stderr,
                            )
                            _log_outcome(tid, "changes_requested", reason="review requested changes")
                            return True

                if human_review == "per_ticket" or resolve_autonomy(cfg, review_ticket) != "full":
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
            _log_outcome(tid, final_ticket.get("status") if final_ticket else "unknown")

            return False

        def run_worker_pool(initial_items: list[dict]) -> list[str]:
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
                submitted_ids.add(tid)
                touches = set(ticket.get("touches") or [])
                instance = assign_pool_instance(ticket)
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
                            # TICK-249: the reason string alone (exception class + message)
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
                        if candidate is None and auto_analyze:
                            # Only spend time analyzing drafts once there is
                            # no already-dispatchable work to refill with —
                            # ready tickets must never wait behind drafts.
                            _analyze_drafts(
                                cfg, repo_root, milestone=milestone, tickets_dir=tickets_dir
                            )
                            candidate = next_refill_candidate()
                        if candidate is None:
                            break
                        submit(pool, candidate)

            return paused_tickets

        if max_parallel <= 1 or dry_run:
            for ticket in work_items:
                tid = ticket["id"]
                instance = assign_pool_instance(ticket) if not dry_run else None
                try:
                    paused = run_ticket(ticket)
                except (Exception, SystemExit) as exc:
                    # See matching comment in run_worker_pool above: SystemExit
                    # is a BaseException, not an Exception, so it must be
                    # caught explicitly or it unwinds out of cmd_orchestrate.
                    reason = f"ticket run crashed: {exc.__class__.__name__}: {exc}"
                    print(f"ERROR: {tid} — {reason}", file=sys.stderr)
                    # TICK-249: see matching comment in run_worker_pool above.
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
            print("\n[orchestrate] batch complete — review PRs, then run:\n    lanegate orchestrate")
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
            t = ticket_by_id.get(tid)
            status = t.get("status", "unknown") if t else "unknown"
            if status == "hibernated":
                remedy = "re-run: lanegate orchestrate"
            elif status in ("needs_review", "failed"):
                remedy = f"inspect and fix, then: lanegate reopen {tid} && lanegate orchestrate"
            elif status == "changes_requested":
                remedy = "address reviewer feedback, then: lanegate orchestrate"
            else:
                remedy = f"check ticket (status: {status})"
            summary_lines.append(f"  {tid} [{status}] -> {remedy}")
        print(
            f"[orchestrate] {len(all_paused_tickets)} ticket(s) paused during this run:\n"
            + "\n".join(summary_lines),
            file=sys.stderr,
        )
