"""
Shared imports, fixtures, and helpers for tests/orchestrate/*.

Split out of the former monolithic tests/test_orchestrate.py (TICK-316).
Every module in this package does `from tests.orchestrate.conftest import *`
to pull in the same imports/helpers the monolith had at module scope.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch as _patch

import pytest

from lanegate.config import ConfigError
from lanegate.api import _run_payload
from lanegate.executor import build_executor_cmd as _build_executor_cmd
from lanegate.orchestrate import (
    _append_run_event,
    _auth_error_reason,
    _build_combined_prompt,
    _build_env,
    _claude_encoded_cwd,
    _collect_live_lanegate_processes,
    _committed_files,
    _conflicted_files,
    _find_claude_transcript,
    _format_conflict_detail,
    _gather_rate_limit_texts,
    _hibernate_orphaned,
    _is_auth_error,
    _is_blocked_file,
    _is_combined_mode,
    _is_rate_limit,
    _load_run_events,
    _rate_limit_reason,
    _reap_orphaned_executor_processes,
    _resolve_run_session_ts,
    _run_events_path,
    _run_rebase,
    _scan_injection_signals,
    _worktree_is_dirty,
    build_run_report,
    check_worktree_has_commits,
    cmd_orchestrate,
    cmd_ps,
    cmd_run_report,
    commit_worktree_changes,
    expand_driver,
    get_orchestration_status,
    invoke_executor,
    next_batch,
    resolve_driver,
    run_auto_fix_cycle,
    run_review_agent,
    spawn_watch_daemon,
)
from lanegate.ticket import parse_ticket
from lanegate.orchestrate.pool import resolve_dispatch
from lanegate.orchestrate.status import format_resolved_dispatch, write_executing_status


@pytest.fixture(autouse=True)
def _compat_stream_subprocess(monkeypatch):
    """Let legacy orchestration tests keep mocking ``subprocess.run``.

    Production review and drift checks use the streaming runner so they can
    retain partial output.  Most pre-existing loop tests deliberately mock
    the older subprocess seam; this adapter preserves that test seam while
    individual streaming tests replace it with their own explicit double.
    """
    def fake_stream(cmd, **kwargs):
        result = subprocess.run(
            cmd,
            cwd=kwargs.get("cwd"),
            capture_output=True,
            text=True,
            env=kwargs.get("env"),
            input=kwargs.get("stdin_text"),
        )
        return result.returncode, result.stdout, getattr(result, "stderr", ""), None

    monkeypatch.setattr("lanegate.orchestrate.review._stream_subprocess", fake_stream)
    monkeypatch.setattr("lanegate.orchestrate.autofix._stream_subprocess", fake_stream)


_LOOP_PATCH_NAMES = {
    "Path",
    "_abort_rebase",
    "_analyze_drafts",
    "_auth_error_reason",
    "_committed_files",
    "_conflicted_files",
    "_continue_rebase",
    "_drain_loop",
    "_is_auth_error",
    "_is_combined_mode",
    "_is_rate_limit",
    "_print_draft_analysis_plan",
    "_rate_limit_reason",
    "_run_acceptance_contract_audit",
    "_run_rebase",
    "_run_static_analysis",
    "_ticket_has_real_progress",
    "_worktree_is_dirty",
    "_write_executor_cooldown",
    "acquire_orchestrator_lock",
    "check_worktree_has_commits",
    "commit_worktree_changes",
    "invoke_executor",
    "next_batch",
    "pid_alive",
    "release_orchestrator_lock",
    "run_auto_fix_cycle",
    "run_review_agent",
    "spawn_resume_watch_daemon",
    "spawn_watch_daemon",
    "subprocess",
    "time",
}


def patch(target, *args, **kwargs):
    """Patch moved loop globals at their implementation module."""
    if isinstance(target, str) and target.startswith("lanegate.orchestrate."):
        remainder = target.removeprefix("lanegate.orchestrate.")
        if remainder.split(".", 1)[0] in _LOOP_PATCH_NAMES:
            target = f"lanegate.orchestrate.loop.{remainder}"
    return _patch(target, *args, **kwargs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dispatch_call(mock_run):
    """Return the executor dispatch call recorded by a patched ``subprocess.run``.

    Review-class steps now write a run directory, which snapshots the worktree
    with `git status`/`git diff` after the executor returns.  Tests that assert
    on the dispatched command want the executor call, not whichever call
    happened to be last.
    """
    for call in reversed(mock_run.call_args_list):
        cmd = call.args[0] if call.args else call.kwargs.get("cmd")
        if cmd and cmd[0] != "git":
            return call
    raise AssertionError(f"no executor dispatch call in {mock_run.call_args_list}")


def _default_cfg(tmp_path: Path) -> dict:
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir(exist_ok=True)
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir(exist_ok=True)
    return {
        "ticket_prefix": "TICK",
        "tickets_dir": str(tickets_dir),
        "worktrees_dir": str(worktrees_dir),
        "lock_statuses": ["in_progress", "code_complete", "in_review"],
        "commit_status_changes": False,
        "environments": [],
        "executor": "claude-process",
        # Board-clearing unit tests exercise lifecycle transitions with mocked
        # implementers; they are not integration tests for a real external
        # reviewer.  Keep that default explicit so they do not spawn one.
        "reviewer": "auto-none",
        "max_parallel": 2,
        "executors": {},
        "trunk_branch": "main",
    }


@pytest.fixture(autouse=True)
def _resolve_executor_bins_to_their_names(monkeypatch):
    monkeypatch.setattr("lanegate.executor.shutil.which", lambda bin_name: bin_name)


@pytest.fixture(autouse=True)
def _virtual_worktree_risk_diff_is_clean(monkeypatch):
    """Keep mocked-worktree tests explicit about their successful risk scan.

    Board-clearing unit tests generally mock ``cmd_start`` and model lifecycle
    state in ticket files without creating an actual Git worktree.  A missing
    worktree is now correctly fail-closed in production, so these tests need a
    successful diff result unless a test intentionally overrides it to cover
    the failure path.
    """
    from lanegate.git import GitText

    monkeypatch.setattr(
        "lanegate.orchestrate.loop._git_text",
        lambda *_args, **_kwargs: GitText(""),
    )


def _write_ticket(
    tickets_dir: Path,
    ticket_id: str,
    status: str,
    *,
    parallel_safe: bool = True,
    priority: int = 1,
    touches: list[str] | None = None,
    pr_number: int | None = None,
    branch: str | None = None,
    complexity: int | None = None,
    findings: str | None = None,
    depends_on: list[str] | None = None,
) -> Path:
    touches_str = ""
    if touches:
        items = "\n".join(f"  - {t}" for t in touches)
        touches_str = f"touches:\n{items}\n"
    pr_str = f"pr_number: {pr_number}\n" if pr_number else ""
    branch_str = f"branch: {branch}\n" if branch else ""
    complexity_str = f"complexity: {complexity}\n" if complexity is not None else ""
    depends_on_str = ""
    if depends_on:
        depends_on_str = "depends_on:\n" + "\n".join(f"  - {dep}" for dep in depends_on) + "\n"
    findings_body = f"## Review Findings\n{findings}\n" if findings else ""
    content = (
        f"---\n"
        f"id: {ticket_id}\n"
        f"title: Test {ticket_id}\n"
        f"status: {status}\n"
        f"priority: {priority}\n"
        f"parallel_safe: {str(parallel_safe).lower()}\n"
        f"{touches_str}"
        f"{pr_str}"
        f"{branch_str}"
        f"{complexity_str}"
        f"{depends_on_str}"
        f"close_criteria: All tests pass.\n"
        f"---\nBody.\n"
        f"{findings_body}"
    )
    path = tickets_dir / f"{ticket_id}.md"
    path.write_text(content)
    return path


def _fake_start_writes_in_progress(tid, cfg_, repo_root, **kwargs) -> None:
    """Reusable cmd_start side_effect: flips open/hibernated -> in_progress
    on disk, like the real cmd_start does. A bare no-op mock leaves the
    ticket at 'open', which makes pause_for_needs_review's status branch
    (in_progress vs. everything else) untestable in a way that matches
    production behavior.
    """
    tickets_dir = Path(repo_root) / cfg_["tickets_dir"]
    p = tickets_dir / f"{tid}.md"
    text = p.read_text()
    for from_status in ("open", "hibernated"):
        updated = text.replace(f"status: {from_status}", "status: in_progress", 1)
        if updated != text:
            p.write_text(updated)
            return


def _fake_complete_writes_code_complete(tid, cfg_, repo_root) -> None:
    """Reusable cmd_complete side_effect: flips open/in_progress -> code_complete
    on disk, like the real cmd_complete does. Matching against a single
    hardcoded source status (e.g. only 'open') silently no-ops when an earlier
    mocked cmd_start already advanced the ticket to 'in_progress' -- the
    orchestrate loop then finds the ticket still short of code_complete and
    (correctly) declines to dispatch review, which reads as review being
    skipped rather than as the fixture's status match failing.
    """
    tickets_dir = Path(repo_root) / cfg_["tickets_dir"]
    p = tickets_dir / f"{tid}.md"
    text = p.read_text()
    for from_status in ("open", "in_progress"):
        updated = text.replace(f"status: {from_status}", "status: code_complete", 1)
        if updated != text:
            p.write_text(updated)
            return


__all__ = [
    "json",
    "os",
    "shutil",
    "subprocess",
    "Path",
    "MagicMock",
    "pytest",
    "ConfigError",
    "_run_payload",
    "_build_executor_cmd",
    "_append_run_event",
    "_auth_error_reason",
    "_build_combined_prompt",
    "_build_env",
    "_claude_encoded_cwd",
    "_collect_live_lanegate_processes",
    "_committed_files",
    "_conflicted_files",
    "_find_claude_transcript",
    "_format_conflict_detail",
    "_gather_rate_limit_texts",
    "_hibernate_orphaned",
    "_is_auth_error",
    "_is_blocked_file",
    "_is_combined_mode",
    "_is_rate_limit",
    "_load_run_events",
    "_rate_limit_reason",
    "_reap_orphaned_executor_processes",
    "_resolve_run_session_ts",
    "_run_events_path",
    "_run_rebase",
    "_scan_injection_signals",
    "_worktree_is_dirty",
    "build_run_report",
    "check_worktree_has_commits",
    "cmd_orchestrate",
    "cmd_ps",
    "cmd_run_report",
    "commit_worktree_changes",
    "expand_driver",
    "get_orchestration_status",
    "invoke_executor",
    "next_batch",
    "resolve_driver",
    "run_auto_fix_cycle",
    "run_review_agent",
    "spawn_watch_daemon",
    "parse_ticket",
    "resolve_dispatch",
    "format_resolved_dispatch",
    "write_executing_status",
    "_LOOP_PATCH_NAMES",
    "patch",
    "_default_cfg",
    "_dispatch_call",
    "_write_ticket",
    "_fake_start_writes_in_progress",
    "_fake_complete_writes_code_complete",
]
