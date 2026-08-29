"""Executor-pool dispatch and recovery helpers extracted from loop."""

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
    clear_failure_streak as _clear_pool_failure_streak,
    get_executor_config,
    is_cooling_down as _executor_is_cooling_down,
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



def _is_interrupted_exit(exit_code: int) -> bool:
    # A signal received directly by a child is reported as a negative return
    # code, while a shell/CLI that catches SIGINT commonly reports 130.
    return exit_code < 0 or exit_code == 130


def _interrupted_exit_reason(exit_code: int) -> str:
    if exit_code == 130:
        return "executor interrupted by SIGINT (exit 130)"
    signum = abs(exit_code)
    try:
        signal_name = signal.Signals(signum).name
    except ValueError:
        signal_name = f"signal {signum}"
    return f"executor interrupted by {signal_name} (exit {exit_code})"


def _watchdog_termination_reason(captured_stderr: str) -> str | None:
    import re
    match = re.search(r"dispatch terminated due to '([^']+)' after", captured_stderr)
    if match:
        return f"watchdog termination: {match.group(1)}"
    return None


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
        force_kill_pid(pid)
    return True


def _reap_orphaned_executor_processes(
    cfg: dict, repo_root: Path, *, out_stream=None, session_ts: str | None = None
) -> list[str]:
    """Kill live executor subprocesses left behind by a dead orchestrate driver.

    _collect_live_lanegate_processes already *detects* this exact
    situation — a ticket-executor PID still alive while the orchestrator
    lock that dispatched it is dead — and `lanegate ps` prints it as
    `[ORPHANED]` for a human to kill by hand. That detection is reused
    as-is here; this only adds the missing kill + durable-event + hibernate
    steps, so an orphan left running unsupervised gets bounded by
    the next `lanegate run` invocation instead of running until someone
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

            wt = ticket.get("worktree")
            if wt and Path(wt).exists() and (Path(wt) / ".git").exists():
                from lanegate.lifecycle import checkpoint_dirty_worktree
                try:
                    checkpoint_dirty_worktree(repo_root, Path(wt), msg="wip: uncommitted edits preserved before hibernation (orphan)")
                except RuntimeError as exc:
                    print(f"[orchestrate] {tid}: failed to checkpoint worktree before hibernation: {exc}", file=stream)

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


def _recent_hibernation_status(ticket: dict) -> bool:
    """Whether a hibernation timestamp is still within the cooldown window."""
    raw = ticket.get("status_changed_at")
    if not isinstance(raw, str):
        return False
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        changed_at = datetime.datetime.fromisoformat(text)
    except ValueError:
        return False
    if changed_at.tzinfo is None:
        return False
    now = datetime.datetime.now(datetime.UTC)
    return now - datetime.timedelta(seconds=DEFAULT_COOLDOWN_TTL_SECONDS) <= changed_at <= now


def _pool_instance_healthy(repo_root: Path, cfg: dict, instance_name: str) -> bool:
    """Return whether a named pool instance is available for new work.

    The executor cooldown is authoritative. A hibernated ticket marker only
    fills the brief persistence gap while that cooldown is being recorded, so
    legacy markers expire with the same fallback TTL as executor cooldowns.
    """
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
        and _active_rate_limit_hibernation(t)
        and _recent_hibernation_status(t)
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
    if (step in ("implement", "fix") and ticket.get("executor")) or (
        step == "review" and ticket.get("reviewer")
    ):
        return driver_name

    if pool_name is None:
        pool_name, _ = resolve_ticket_pool(cfg, ticket)
    pool_cfg = (cfg.get("pools") or {}).get(pool_name) if pool_name else None
    if not isinstance(pool_cfg, dict):
        # A named review/implement driver can be cooling down even without a
        # pool.  ``healthy_only`` is a hard promise to callers: never quietly
        # turn an empty healthy set into an exhausted direct driver.
        if healthy_only and not _pool_instance_healthy(repo_root, cfg, driver_name):
            return None
        return driver_name

    assert pool_name is not None  # pool_cfg is a dict only when pool_name was truthy (line above)
    excluded = excluded or set()
    candidates = [
        name for name in pool_cfg.get("executors") or [] if name not in excluded
    ]
    if not candidates:
        return None if healthy_only else driver_name
    healthy = [name for name in candidates if _pool_instance_healthy(repo_root, cfg, name)]
    if healthy_only and not healthy:
        return None
    # New dispatches must never spend an attempt on a known-cooling account.
    # The old ``healthy or candidates`` fallback is what selected Claude A
    # after its weekly quota had already been recorded.
    if not healthy:
        return None
    pick_from = healthy

    running_counts = running_counts or {}
    dispatch_counts = dispatch_counts or {}

    # Prefer instances that still have room under their own
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


def _executor_alive(ticket: dict, cfg: dict, repo_root: Path) -> bool:
    """Return whether the recorded executor/session marker is still alive."""
    tid = ticket["id"]
    state = repo_root / ".lanegate"
    pid_path = state / f"{tid}.pid"
    session_path = state / f"{tid}.session"

    if pid_path.exists():
        try:
            return pid_alive(int(pid_path.read_text(encoding="utf-8").strip()))
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
    """Reclaim work stranded by a prior session.

    Covers two cases: in-progress tickets whose executor marker is missing
    or stale, and code_complete tickets left with no review verdict. The
    latter can only be seen here because this runs once at startup, before
    this run has dispatched any worker of its own -- so any code_complete
    ticket already on the board was left behind by a session that ended
    (crashed, was killed, or errored) before it could hand the ticket to
    review. Routing it through mark_review_pending reuses the same
    hibernated/review_pending resume orchestrate already trusts for
    rate-limited reviews, so it flows back through review on this run
    instead of sitting until someone runs `lanegate review` by hand.
    """
    from lanegate.lifecycle import cmd_hibernate
    from .loop import _queue_code_complete_reviews

    tickets_dir = repo_root / cfg["tickets_dir"]
    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    orphaned = [
        t
        for t in tickets
        if t.get("status") == "in_progress" and not _executor_alive(t, cfg, repo_root)
    ]
    stranded_code_complete = [
        t
        for t in tickets
        if t.get("status") == "code_complete" and not t.get("review_verdict")
    ]

    if not orphaned and not stranded_code_complete:
        return 0

    if orphaned:
        print(
            f"[orchestrate] {len(orphaned)} orphaned in_progress ticket(s) detected from prior session"
        )
        for t in orphaned:
            wt = t.get("worktree")
            if wt and Path(wt).exists() and (Path(wt) / ".git").exists():
                from lanegate.lifecycle import checkpoint_dirty_worktree
                try:
                    checkpoint_dirty_worktree(
                        repo_root,
                        Path(wt),
                        msg="wip: uncommitted edits preserved before hibernation (orphaned session)",
                    )
                except RuntimeError as exc:
                    print(
                        f"[orchestrate] {t['id']}: failed to checkpoint worktree before hibernation: {exc}"
                    )
            branch = t.get("branch") or t["id"].lower()
            print(f"[orchestrate] hibernating {t['id']} - partial work preserved in branch {branch}")
            cmd_hibernate(t["id"], cfg, repo_root, reason="orphaned prior executor session")

    if stranded_code_complete:
        print(
            f"[orchestrate] {len(stranded_code_complete)} code_complete ticket(s) stranded from a "
            "prior session (no review verdict) — queuing for review resume"
        )
        queued = _queue_code_complete_reviews(
            cfg,
            repo_root,
            reason="orphaned prior session: code_complete with no review verdict",
        )
        for tid in queued:
            print(f"[orchestrate] queuing {tid} for review resume")

    print("[orchestrate] resuming board clearing from hibernated tickets (priority-boosted)")
    return len(orphaned) + len(stranded_code_complete)


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
    in-flight ticket) into hibernation on demand. ``worktree_path`` stays in the signature only for call-site
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
    # a literal 429 here — detection relies on structured fields or text matching below.
    texts = _gather_rate_limit_texts(
        worktree_path, captured_stdout=captured_stdout, captured_stderr=captured_stderr
    )
    text = _rate_limit_detection_text(texts)
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
    return _is_resumable_rate_limit(
        text,
        rate_limit_detected=any(re.search(pattern, text) for pattern in patterns),
    )


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


