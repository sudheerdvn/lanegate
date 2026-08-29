"""Recovery and rebase helpers extracted from loop."""

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













def _collect_prior_notes(ticket: dict, repo_root: Path) -> str:
    """Return hibernation recovery context for *ticket*, if any."""
    recovery_path = repo_root / ".lanegate" / "recovery" / f"{ticket['id']}.md"
    if ticket.get("status") not in ("hibernated", "needs_review") or not recovery_path.exists():
        return ""
    recovery_text = recovery_path.read_text(encoding="utf-8", errors="replace").strip()
    if not recovery_text:
        return ""
    return "## Hibernation Recovery Context\n\n" + recovery_text


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
    """Heuristic for whether a rate-limited ticket is worth
    resuming on a healthy sibling pool instance rather than hibernating.

    True when the worktree shows real work-in-progress: commits ahead of
    main, or uncommitted tracked changes. False is more consistent with a
    stuck/looping session that burned quota without producing anything —
    that case should still hibernate rather than risk depleting a second
    pool instance's quota on the same bad ticket.
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


def is_mid_rebase(worktree_path: Path) -> bool:
    """Return True if git rebase is currently in progress in worktree_path."""
    if not worktree_path or not worktree_path.exists():
        return False
    try:
        for folder in ("rebase-merge", "rebase-apply"):
            res = subprocess.run(
                ["git", "rev-parse", "--git-path", folder],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            if res.returncode == 0:
                p_str = res.stdout.strip()
                if p_str:
                    p = Path(p_str)
                    full_p = p if p.is_absolute() else (worktree_path / p)
                    if full_p.exists():
                        return True
    except FileNotFoundError:
        pass
    return False


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
        unexpected = {path for path in unexpected if not is_lanegate_notes_file(path)}
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
        ok_cp, cp_err = check_control_plane_compliance(restored, repo_root=repo_root, cfg=cfg, worktree_path=wt, check_review_independence=False)
        if not ok_cp:
            _mark_needs_review(
                restored,
                cfg,
                repo_root,
                reason=f"control plane compliance failed: {cp_err}",
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


