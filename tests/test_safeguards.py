"""Tests for safeguards.py — pre_complete and pre_merge quality gates."""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import portalocker
import pytest

from lanegate.safeguards import (
    _check_script_guard_conflicts,
    _find_prompt_artifact_violations,
    _is_safe_guard_for_ticket,
    _resolve_command,
    _run_one_guard,
    effective_safeguards,
    run_safeguards,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_process(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    """A completed ``Popen`` test double with deterministic captured output."""
    process = MagicMock()
    process.returncode = returncode
    process.stdout = stdout
    process.stderr = stderr
    process.communicate.return_value = (stdout, stderr)
    return process


def _mock_run_ok(*args, **kwargs):
    """``Popen`` test double that exits successfully."""
    return _mock_process(returncode=0)


def _mock_run_fail(*args, **kwargs):
    """``Popen`` test double that exits unsuccessfully."""
    return _mock_process(returncode=1)


# ---------------------------------------------------------------------------
# run_safeguards — no guards configured → no regression
# ---------------------------------------------------------------------------


def test_no_safeguards_returns_true(tmp_path):
    """When no safeguards are configured, run_safeguards returns (True, None) immediately."""
    ticket = {}
    cfg = {}
    result = run_safeguards("pre_complete", ticket, cfg, tmp_path)
    assert result == (True, None)


def test_empty_safeguards_dict_returns_true(tmp_path):
    """safeguards: {} (the default) must not block anything."""
    ticket = {}
    cfg = {"safeguards": {}}
    assert run_safeguards("pre_complete", ticket, cfg, tmp_path) == (True, None)
    assert run_safeguards("pre_merge", ticket, cfg, tmp_path) == (True, None)


def test_stage_not_present_returns_true(tmp_path):
    """When safeguards is set but the specific stage is absent, return (True, None)."""
    cfg = {"safeguards": {"pre_merge": ["pytest"]}}
    assert run_safeguards("pre_complete", {}, cfg, tmp_path) == (True, None)


# ---------------------------------------------------------------------------
# run_safeguards — passing guard allows transition
# ---------------------------------------------------------------------------


def test_passing_guard_returns_true(tmp_path):
    """A guard that exits 0 returns (True, None)."""
    cfg = {"safeguards": {"pre_complete": ["pytest"]}}
    with patch("lanegate.safeguards._Popen", side_effect=_mock_run_ok):
        result = run_safeguards("pre_complete", {}, cfg, tmp_path)
    assert result == (True, None)


def test_all_guards_pass_returns_true(tmp_path):
    """All guards passing → (True, None)."""
    cfg = {"safeguards": {"pre_complete": ["pytest", "make lint"]}}
    with patch("lanegate.safeguards._Popen", side_effect=_mock_run_ok):
        result = run_safeguards("pre_complete", {}, cfg, tmp_path)
    assert result == (True, None)


# ---------------------------------------------------------------------------
# run_safeguards — failing guard blocks transition
# ---------------------------------------------------------------------------


def test_failing_guard_returns_false(tmp_path, capsys):
    """A guard that exits non-zero returns (False, reason)."""
    cfg = {"safeguards": {"pre_complete": ["pytest"]}}
    with patch("lanegate.safeguards._Popen", side_effect=_mock_run_fail):
        result = run_safeguards("pre_complete", {}, cfg, tmp_path)
    assert result[0] is False
    assert result[1] is not None  # reason provided
    err = capsys.readouterr().err
    assert "FAIL" in err
    assert "pre_complete" in err


def test_run_line_printed_before_guard_executes(tmp_path, capsys):
    """The RUN line is emitted before the guard subprocess is invoked (ordering,
    not just presence) — otherwise a slow guard is indistinguishable from a hang."""
    seen_before_subprocess = []

    def mock_run(*args, **kwargs):
        seen_before_subprocess.append(capsys.readouterr().out)
        return _mock_process(returncode=0)

    cfg = {"safeguards": {"pre_complete": ["pytest"]}}
    with (
        patch("builtins.print", wraps=print) as mock_print,
        patch("lanegate.safeguards._Popen", side_effect=mock_run),
    ):
        run_safeguards("pre_complete", {}, cfg, tmp_path)

    assert len(seen_before_subprocess) == 1
    assert "RUN" in seen_before_subprocess[0]
    assert "pre_complete" in seen_before_subprocess[0]
    assert "pytest" in seen_before_subprocess[0]
    # PASS hasn't been printed yet at the point the subprocess is invoked
    assert "PASS" not in seen_before_subprocess[0]
    assert any(
        call.args == ("  RUN  [pre_complete] pytest",) and call.kwargs == {"flush": True}
        for call in mock_print.call_args_list
    )


def test_pass_line_includes_elapsed_time(tmp_path, capsys):
    """The completion line carries the elapsed wall time, e.g. '(18.3s)'."""
    cfg = {"safeguards": {"pre_complete": ["pytest"]}}
    with patch("lanegate.safeguards._Popen", side_effect=_mock_run_ok):
        run_safeguards("pre_complete", {}, cfg, tmp_path)
    out = capsys.readouterr().out
    assert re.search(r"PASS \[pre_complete\] pytest \(\d+\.\d+s\)", out)
    assert "WAIT [safeguard-lock]" not in out


def test_fail_line_includes_elapsed_time(tmp_path, capsys):
    """The FAIL line also carries the elapsed wall time."""
    cfg = {"safeguards": {"pre_complete": ["pytest"]}}
    with patch("lanegate.safeguards._Popen", side_effect=_mock_run_fail):
        run_safeguards("pre_complete", {}, cfg, tmp_path)
    err = capsys.readouterr().err
    assert re.search(r"FAIL \[pre_complete\] pytest \(\d+\.\d+s\)", err)


def test_first_failing_guard_marks_all_failed(tmp_path):
    """When the first guard fails, return (False, reason) immediately without running remaining guards."""
    call_count = [0]

    def mock_run_alternating(*args, **kwargs):
        call_count[0] += 1
        # First call fails, second would succeed
        return _mock_process(returncode=1 if call_count[0] == 1 else 0)

    cfg = {"safeguards": {"pre_complete": ["pytest", "make lint"]}}
    with patch("lanegate.safeguards._Popen", side_effect=mock_run_alternating):
        result = run_safeguards("pre_complete", {}, cfg, tmp_path)
    assert result[0] is False
    assert result[1] is not None
    assert call_count[0] == 1  # only first guard ran before failure


# ---------------------------------------------------------------------------
# per-ticket override
# ---------------------------------------------------------------------------


def test_per_ticket_safeguards_are_additive_to_project(tmp_path):
    """Per-ticket safeguards are added to project-level ones (additive, not override)."""
    project_cfg = {"safeguards": {"pre_complete": ["pytest"]}}
    ticket = {"safeguards": {"pre_complete": ["make custom-test"]}}

    calls = []

    def recording_run(cmd, **kwargs):
        calls.append(cmd)
        return _mock_process(returncode=0)

    with patch("lanegate.safeguards._Popen", side_effect=recording_run):
        result = run_safeguards("pre_complete", ticket, project_cfg, tmp_path)

    assert result == (True, None)
    # F41: per-ticket safeguards are additive, not an override — a compromised
    # ticket must not be able to disable project-level checks.
    assert len(calls) == 2
    assert any("pytest" in str(c) for c in calls)
    assert any("custom-test" in str(c) for c in calls)


def test_per_ticket_empty_list_still_runs_project_guards(tmp_path):
    """ticket safeguards: {pre_complete: []} — empty list adds nothing, so project guards run."""
    project_cfg = {"safeguards": {"pre_complete": ["pytest"]}}
    ticket = {"safeguards": {"pre_complete": []}}

    calls = []

    def recording_run(cmd, **kwargs):
        calls.append(cmd)
        return _mock_process(returncode=0)

    with patch("lanegate.safeguards._Popen", side_effect=recording_run):
        result = run_safeguards("pre_complete", ticket, project_cfg, tmp_path)

    # Empty list at ticket level means "add nothing", so project guards still run
    assert result == (True, None)
    assert len(calls) == 1
    assert any("pytest" in str(c) for c in calls)


def test_per_ticket_pre_merge_is_additive(tmp_path, monkeypatch):
    """per-ticket pre_merge is added to (not replacing) project pre_merge."""
    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    project_cfg = {"safeguards": {"pre_merge": ["pytest"]}}
    ticket = {"safeguards": {"pre_merge": ["cargo test"]}}

    calls = []

    def recording_run(cmd, **kwargs):
        calls.append(cmd)
        return _mock_process(returncode=0)

    with patch("lanegate.safeguards._Popen", side_effect=recording_run):
        result = run_safeguards("pre_merge", ticket, project_cfg, tmp_path)

    assert result == (True, None)
    assert len(calls) == 2
    assert any("cargo" in str(c) for c in calls)
    assert any("pytest" in str(c) for c in calls)


def test_project_post_merge_guard_runs(tmp_path):
    """Project-level post_merge safeguards are accepted like other stages."""
    project_cfg = {"safeguards": {"post_merge": ["pytest"]}}
    calls = []

    def recording_run(cmd, **kwargs):
        calls.append(cmd)
        return _mock_process(returncode=0)

    with patch("lanegate.safeguards._Popen", side_effect=recording_run):
        result = run_safeguards("post_merge", {}, project_cfg, tmp_path)

    assert result == (True, None)
    assert calls == [[sys.executable, "-m", "pytest", "-q", "--tb=short"]]


def test_per_ticket_post_merge_is_additive(tmp_path, monkeypatch):
    """per-ticket post_merge is added to (not replacing) project post_merge."""
    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    project_cfg = {"safeguards": {"post_merge": ["pytest"]}}
    ticket = {"safeguards": {"post_merge": ["make regression-check"]}}
    calls = []

    def recording_run(cmd, **kwargs):
        calls.append(cmd)
        return _mock_process(returncode=0)

    with patch("lanegate.safeguards._Popen", side_effect=recording_run):
        result = run_safeguards("post_merge", ticket, project_cfg, tmp_path)

    assert result == (True, None)
    assert len(calls) == 2
    assert any("make" in str(c) for c in calls)
    assert any("pytest" in str(c) for c in calls)


# ---------------------------------------------------------------------------
# _resolve_command — built-in runner recognition
# ---------------------------------------------------------------------------


def test_resolve_pytest_bare(tmp_path):
    cmd = _resolve_command("pytest", tmp_path)
    assert cmd == [sys.executable, "-m", "pytest", "-q", "--tb=short"]


def test_resolve_pytest_with_args(tmp_path):
    cmd = _resolve_command("pytest tests/ -x -q", tmp_path)
    assert cmd == [sys.executable, "-m", "pytest", "-q", "--tb=short", "tests/", "-x", "-q"]


def test_resolve_npm_test(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    cmd = _resolve_command("npm test", tmp_path)
    assert cmd == ["npm", "run", "test"]


def test_resolve_npm_run_custom(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    cmd = _resolve_command("npm run ci", tmp_path)
    assert cmd == ["npm", "run", "ci"]


def test_resolve_cargo_test(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    cmd = _resolve_command("cargo test", tmp_path)
    assert cmd == ["cargo", "test"]


def test_resolve_go_test(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    cmd = _resolve_command("go test ./...", tmp_path)
    assert cmd == ["go", "test", "./..."]


def test_resolve_make(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    cmd = _resolve_command("make test", tmp_path)
    assert cmd == ["make", "test"]


def test_resolve_fallback_shlex(tmp_path, monkeypatch):
    """Unrecognised strings are parsed with shlex.split (shell=False)."""
    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    cmd = _resolve_command("my-custom-runner --flag value", tmp_path)
    assert cmd == ["my-custom-runner", "--flag", "value"]


def test_pytest_missing_falls_back_to_python_module(tmp_path, monkeypatch):
    """PATH has no pytest binary, but resolving pytest command uses sys.executable (-m pytest)."""
    monkeypatch.setattr("shutil.which", lambda cmd: None if cmd == "pytest" else f"/usr/bin/{cmd}")
    cmd = _resolve_command("pytest", tmp_path)
    assert cmd == [sys.executable, "-m", "pytest", "-q", "--tb=short"]


# ---------------------------------------------------------------------------
# _resolve_command — shell script (.sh) handling
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason=".sh scripts not supported on Windows")
def test_resolve_sh_script_found(tmp_path):
    """A .sh script that exists is resolved to its absolute path."""
    script = tmp_path / "scripts" / "run-tests.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\nexit 0\n")

    cmd = _resolve_command("scripts/run-tests.sh", tmp_path)
    assert cmd == [str(script)]


@pytest.mark.skipif(sys.platform == "win32", reason=".sh scripts not supported on Windows")
def test_resolve_sh_script_missing_returns_none(tmp_path, capsys):
    """A .sh script that does not exist returns None and prints an error."""
    cmd = _resolve_command("scripts/does-not-exist.sh", tmp_path)
    assert cmd is None
    err = capsys.readouterr().err
    assert "not found" in err


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only behaviour")
def test_resolve_sh_on_windows_returns_none(tmp_path, capsys):
    """On Windows, .sh scripts produce a clear error and return None."""
    cmd = _resolve_command("scripts/run-tests.sh", tmp_path)
    assert cmd is None
    err = capsys.readouterr().err
    assert "Windows" in err


def test_resolve_sh_on_non_windows_does_not_error(tmp_path, capsys):
    """On non-Windows, .sh resolution proceeds normally (path check only)."""
    if sys.platform == "win32":
        pytest.skip("Non-Windows test")
    # Script doesn't exist → None + error, but NOT the Windows error
    cmd = _resolve_command("scripts/run-tests.sh", tmp_path)
    assert cmd is None
    err = capsys.readouterr().err
    assert "Windows" not in err
    assert "not found" in err


def test_resolve_command_relative_path_with_separator_against_worktree(tmp_path):
    """A command with path separator resolves against worktree."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    runner = bin_dir / "runner"
    runner.write_text("#!/bin/sh\nexit 0\n")
    runner.chmod(0o755)

    cmd = _resolve_command("bin/runner --arg", tmp_path)
    assert cmd == ["bin/runner", "--arg"]

    unresolved = _resolve_command("bin/missing --arg", tmp_path)
    assert unresolved == (None, "unresolved: bin/missing")


# ---------------------------------------------------------------------------
# _run_one_guard — missing executable
# ---------------------------------------------------------------------------


def test_run_one_guard_unresolved_command_preflight(tmp_path):
    """An unresolved configured command fails during preflight with distinct reason."""
    passed, reason = _run_one_guard("nonexistent-tool-xyz --foo", tmp_path)
    assert passed is False
    assert reason == "unresolved command: nonexistent-tool-xyz not found on PATH"


def test_run_one_guard_missing_executable(tmp_path, capsys, monkeypatch):
    """If the resolved command binary passes preflight but Popen raises FileNotFoundError."""
    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    with patch("lanegate.safeguards._Popen", side_effect=FileNotFoundError("no such file")):
        result = _run_one_guard("nonexistent-tool --foo", tmp_path)
    assert result[0] is False
    assert result[1] is not None
    err = capsys.readouterr().err
    assert "ERROR" in err


def test_run_one_guard_empty_string_is_noop(tmp_path):
    """Empty guard string is treated as a no-op and returns (True, None)."""
    result = _run_one_guard("", tmp_path)
    assert result == (True, None)


# ---------------------------------------------------------------------------
# Integration with lifecycle — cmd_complete blocks on failing safeguard
# ---------------------------------------------------------------------------


def test_lifecycle_complete_routes_to_needs_review_on_failing_safeguard(tmp_path, capsys):
    """cmd_complete transitions to needs_review when pre_complete safeguards fail."""
    from lanegate.lifecycle import cmd_complete

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-300"
    wt.mkdir()

    content = (
        "---\n"
        "id: TICK-300\n"
        "title: Test safeguard block\n"
        "status: in_progress\n"
        f"worktree: {wt}\n"
        "touches:\n"
        '  - "*"\n'
        "---\nBody.\n"
    )
    (tickets_dir / "TICK-300.md").write_text(content)

    cfg = {
        "ticket_prefix": "TICK",
        "tickets_dir": str(tickets_dir),
        "worktrees_dir": str(worktrees_dir),
        "lock_statuses": ["in_progress", "code_complete", "in_review"],
        "commit_status_changes": False,
        "environments": [],
        "safeguards": {"pre_complete": ["pytest"]},
    }

    def mock_run(args, **kwargs):
        # pytest fails
        if "-m" in args and "pytest" in args:
            return _mock_process(returncode=1)
        return _mock_process(returncode=0)

    with patch("lanegate.safeguards._Popen", side_effect=mock_run):
        with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
            cmd_complete("TICK-300", cfg, tmp_path)

    err = capsys.readouterr().err
    assert "pre_complete" in err or "safeguard" in err.lower()
    assert "needs_review" in err.lower()

    # Status should have transitioned to needs_review
    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-300.md")
    assert ticket["status"] == "needs_review"


def test_lifecycle_complete_passes_on_passing_safeguard(tmp_path):
    """cmd_complete advances status when pre_complete safeguards all pass."""
    from lanegate.lifecycle import cmd_complete

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-301"
    wt.mkdir()

    content = (
        "---\n"
        "id: TICK-301\n"
        "title: Test safeguard pass\n"
        "status: in_progress\n"
        f"worktree: {wt}\n"
        "touches:\n"
        '  - "*"\n'
        "---\nBody.\n"
    )
    (tickets_dir / "TICK-301.md").write_text(content)

    cfg = {
        "ticket_prefix": "TICK",
        "tickets_dir": str(tickets_dir),
        "worktrees_dir": str(worktrees_dir),
        "lock_statuses": ["in_progress", "code_complete", "in_review"],
        "commit_status_changes": False,
        "environments": [],
        "safeguards": {"pre_complete": ["pytest"]},
    }

    def mock_run(args, **kwargs):
        if args[:2] == ["git", "rev-parse"]:
            return MagicMock(returncode=0, stdout="deadbeef\n")
        return MagicMock(returncode=0)

    with (
        patch("lanegate.safeguards._Popen", side_effect=_mock_run_ok),
        patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run),
    ):
        cmd_complete("TICK-301", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-301.md")
    assert ticket["status"] == "code_complete"


def test_lifecycle_complete_routes_timeout_to_needs_review(tmp_path, capsys):
    """TICK-246: a pre_complete safeguard that times out routes the ticket to
    needs_review with a clear reason, instead of leaving it stuck in_progress."""
    from lanegate.lifecycle import cmd_complete
    from lanegate.ticket import parse_ticket

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-350"
    wt.mkdir()

    content = (
        "---\n"
        "id: TICK-350\n"
        "title: Test safeguard timeout routing\n"
        "status: in_progress\n"
        f"worktree: {wt}\n"
        "touches:\n"
        '  - "*"\n'
        "---\nBody.\n"
    )
    (tickets_dir / "TICK-350.md").write_text(content)

    cfg = {
        "ticket_prefix": "TICK",
        "tickets_dir": str(tickets_dir),
        "worktrees_dir": str(worktrees_dir),
        "lock_statuses": ["in_progress", "code_complete", "in_review"],
        "commit_status_changes": False,
        "environments": [],
        "safeguards": {"pre_complete": ["pytest"], "timeout_s": 5},
    }

    real_run = subprocess.run

    def mock_run(args, **kwargs):
        if "pytest" in args:
            raise subprocess.TimeoutExpired(cmd=args, timeout=5)
        return real_run(args, **kwargs)

    with patch("lanegate.safeguards._Popen", side_effect=mock_run):
        cmd_complete("TICK-350", cfg, tmp_path)

    ticket = parse_ticket(tickets_dir / "TICK-350.md")
    assert ticket["status"] == "needs_review"
    assert "timed out" in ticket["_body"].lower()


def test_lifecycle_complete_refuses_when_safeguard_lock_held(tmp_path):
    """TICK-246: a concurrent cmd_complete for the same ticket refuses instead of
    spawning a second (redundant) safeguard subprocess."""
    from lanegate.concurrency import safeguard_lock
    from lanegate.lifecycle import cmd_complete
    from lanegate.ticket import parse_ticket

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-351"
    wt.mkdir()

    content = (
        "---\n"
        "id: TICK-351\n"
        "title: Test safeguard dedupe lock\n"
        "status: in_progress\n"
        f"worktree: {wt}\n"
        "touches:\n"
        '  - "*"\n'
        "---\nBody.\n"
    )
    (tickets_dir / "TICK-351.md").write_text(content)

    cfg = {
        "ticket_prefix": "TICK",
        "tickets_dir": str(tickets_dir),
        "worktrees_dir": str(worktrees_dir),
        "lock_statuses": ["in_progress", "code_complete", "in_review"],
        "commit_status_changes": False,
        "environments": [],
        "safeguards": {"pre_complete": ["pytest"]},
    }

    calls = []

    def mock_run(args, **kwargs):
        calls.append(args)
        return _mock_process(returncode=0)

    with safeguard_lock(tmp_path, "TICK-351"):
        # Simulates a prior `lanegate complete TICK-351` still in flight.
        with patch("lanegate.safeguards._Popen", side_effect=mock_run):
            with pytest.raises(SystemExit) as exc_info:
                cmd_complete("TICK-351", cfg, tmp_path)

    assert exc_info.value.code != 0
    assert not any("pytest" in c for c in calls)
    # Status must NOT have advanced, and the held lock must not be released twice.
    ticket = parse_ticket(tickets_dir / "TICK-351.md")
    assert ticket["status"] == "in_progress"


# ---------------------------------------------------------------------------
# Integration with lifecycle — cmd_merge blocks on failing safeguard
# ---------------------------------------------------------------------------


def _make_merge_ticket(
    tickets_dir: Path, worktrees_dir: Path, ticket_id: str, safeguards: dict | None = None
):
    """Create an in_review+approved ticket with an optional safeguards block."""
    wt = worktrees_dir / ticket_id.lower().replace("-", "-")
    wt.mkdir(exist_ok=True)

    safeguards_yaml = ""
    if safeguards:

        safeguards_yaml = "safeguards:\n"
        for stage, guards in safeguards.items():
            safeguards_yaml += f"  {stage}:\n"
            for g in guards:
                safeguards_yaml += f"    - {g}\n"

    content = (
        f"---\n"
        f"id: {ticket_id}\n"
        f"title: Test merge safeguard\n"
        f"status: in_review\n"
        f"review_verdict: approved\n"
        f"branch: {ticket_id.lower()}\n"
        f"worktree: {wt}\n"
        f"{safeguards_yaml}"
        f"---\nBody.\n"
    )
    (tickets_dir / f"{ticket_id}.md").write_text(content)
    return wt


def test_lifecycle_merge_routes_to_needs_review_on_failing_pre_merge(tmp_path, capsys):
    """cmd_merge transitions to needs_review when pre_merge safeguards fail."""
    from lanegate.lifecycle import cmd_merge

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()

    _make_merge_ticket(tickets_dir, worktrees_dir, "TICK-400")

    cfg = {
        "ticket_prefix": "TICK",
        "tickets_dir": str(tickets_dir),
        "worktrees_dir": str(worktrees_dir),
        "lock_statuses": ["in_progress", "code_complete", "in_review"],
        "commit_status_changes": False,
        "environments": [],
        "safeguards": {"pre_merge": ["pytest"]},
    }

    def mock_safeguard_run(args, **kwargs):
        if "-m" in args and "pytest" in args:
            return _mock_process(returncode=1)
        return _mock_process(returncode=0)

    with patch("lanegate.safeguards._Popen", side_effect=mock_safeguard_run):
        with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_safeguard_run):
            cmd_merge("TICK-400", cfg, tmp_path)

    err = capsys.readouterr().err
    assert "pre_merge" in err or "safeguard" in err.lower()
    assert "needs_review" in err.lower()

    # Ticket should have transitioned to needs_review
    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-400.md")
    assert ticket["status"] == "needs_review"


def test_lifecycle_merge_passes_on_passing_pre_merge(tmp_path):
    """cmd_merge proceeds when pre_merge safeguards all pass."""
    from lanegate.lifecycle import cmd_merge

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()

    _make_merge_ticket(tickets_dir, worktrees_dir, "TICK-401")

    cfg = {
        "ticket_prefix": "TICK",
        "tickets_dir": str(tickets_dir),
        "worktrees_dir": str(worktrees_dir),
        "lock_statuses": ["in_progress", "code_complete", "in_review"],
        "commit_status_changes": False,
        "environments": [],
        "safeguards": {"pre_merge": ["pytest"]},
    }

    def mock_run(args, **kwargs):
        return _mock_process(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.safeguards._Popen", side_effect=mock_run),
        patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run),
    ):
        cmd_merge("TICK-401", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-401.md")
    assert ticket["status"] == "merged"


def test_lifecycle_merge_can_skip_worktree_pre_merge_but_verifies_main(tmp_path):
    """pre_merge_worktree: false skips only the duplicate worktree run."""
    from lanegate.lifecycle import cmd_merge

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    worktree = _make_merge_ticket(tickets_dir, worktrees_dir, "TICK-405")

    cfg = {
        "ticket_prefix": "TICK",
        "tickets_dir": str(tickets_dir),
        "worktrees_dir": str(worktrees_dir),
        "lock_statuses": ["in_progress", "code_complete", "in_review"],
        "commit_status_changes": False,
        "environments": [],
        "safeguards": {"pre_merge": ["pytest"], "pre_merge_worktree": False},
    }

    def mock_run(args, **kwargs):
        return _mock_process(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run),
        patch("lanegate.lifecycle.merge.run_safeguards", return_value=(True, None)) as safeguards,
    ):
        cmd_merge("TICK-405", cfg, tmp_path)

    assert safeguards.call_count == 1
    stage, ticket, passed_cfg, passed_worktree = safeguards.call_args.args
    assert stage == "pre_merge"
    assert ticket["id"] == "TICK-405"
    assert passed_cfg is cfg
    assert passed_worktree == tmp_path
    assert safeguards.call_args.kwargs["label"] == "post_merge_verify"
    assert passed_worktree != worktree


def test_lifecycle_merge_runs_worktree_pre_merge_before_main_verification(tmp_path):
    """Enabled worktree verification runs before main is modified."""
    from lanegate.lifecycle import cmd_merge

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    worktree = _make_merge_ticket(tickets_dir, worktrees_dir, "TICK-406")

    cfg = {
        "ticket_prefix": "TICK",
        "tickets_dir": str(tickets_dir),
        "worktrees_dir": str(worktrees_dir),
        "lock_statuses": ["in_progress", "code_complete", "in_review"],
        "commit_status_changes": False,
        "environments": [],
        "safeguards": {"pre_merge": ["pytest"], "pre_merge_worktree": True},
    }

    def mock_run(args, **kwargs):
        return _mock_process(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run),
        patch("lanegate.lifecycle.merge.run_safeguards", return_value=(True, None)) as safeguards,
    ):
        cmd_merge("TICK-406", cfg, tmp_path)

    assert safeguards.call_count == 2
    pre_merge_call, post_merge_call = safeguards.call_args_list
    assert pre_merge_call.args[0] == "pre_merge"
    assert pre_merge_call.args[3] == worktree
    assert "label" not in pre_merge_call.kwargs
    assert post_merge_call.args[0] == "pre_merge"
    assert post_merge_call.args[3] == tmp_path
    assert post_merge_call.kwargs["label"] == "post_merge_verify"


def test_lifecycle_merge_no_safeguards_behaves_as_before(tmp_path):
    """When no safeguards configured, cmd_merge behaves exactly as before (no regression)."""
    from lanegate.lifecycle import cmd_merge

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()

    _make_merge_ticket(tickets_dir, worktrees_dir, "TICK-402")

    cfg = {
        "ticket_prefix": "TICK",
        "tickets_dir": str(tickets_dir),
        "worktrees_dir": str(worktrees_dir),
        "lock_statuses": ["in_progress", "code_complete", "in_review"],
        "commit_status_changes": False,
        "environments": [],
        # No "safeguards" key at all
    }

    def mock_run(args, **kwargs):
        return _mock_process(returncode=0, stdout="", stderr="")

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        cmd_merge("TICK-402", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-402.md")
    assert ticket["status"] == "merged"


# ---------------------------------------------------------------------------
# Per-ticket safeguards in lifecycle
# ---------------------------------------------------------------------------


def test_lifecycle_merge_per_ticket_safeguard_is_additive(tmp_path):
    """Per-ticket pre_merge safeguard is added to (not replacing) project-level pre_merge."""
    from lanegate.lifecycle import cmd_merge

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()

    _make_merge_ticket(
        tickets_dir,
        worktrees_dir,
        "TICK-403",
        safeguards={"pre_merge": ["make custom-check"]},
    )

    cfg = {
        "ticket_prefix": "TICK",
        "tickets_dir": str(tickets_dir),
        "worktrees_dir": str(worktrees_dir),
        "lock_statuses": ["in_progress", "code_complete", "in_review"],
        "commit_status_changes": False,
        "environments": [],
        "safeguards": {"pre_merge": ["pytest"]},  # project-level — should also run
    }

    calls = []

    def recording_run(args, **kwargs):
        calls.append(list(args))
        return _mock_process(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.safeguards._Popen", side_effect=recording_run),
        patch("lanegate.lifecycle.subprocess.run", side_effect=recording_run),
    ):
        cmd_merge("TICK-403", cfg, tmp_path)

    # Both "make custom-check" and "pytest" should have run
    assert any("make" in str(c) for c in calls)
    assert any("-m" in c and "pytest" in c for c in calls if isinstance(c, list))


# ---------------------------------------------------------------------------
# Security: Per-ticket shell scripts filtering (F41)
# ---------------------------------------------------------------------------


def test_is_safe_guard_for_ticket_allows_built_in_types():
    """Built-in guard types (pytest, npm, cargo, go, make) are safe for per-ticket."""
    assert _is_safe_guard_for_ticket("pytest") is True
    assert _is_safe_guard_for_ticket("pytest tests/ -x") is True
    assert _is_safe_guard_for_ticket("npm test") is True
    assert _is_safe_guard_for_ticket("npm run ci") is True
    assert _is_safe_guard_for_ticket("cargo test") is True
    assert _is_safe_guard_for_ticket("go test ./...") is True
    assert _is_safe_guard_for_ticket("make test") is True


def test_is_safe_guard_for_ticket_forbids_shell_scripts():
    """Shell scripts (.sh files) are NOT safe for per-ticket config."""
    assert _is_safe_guard_for_ticket("scripts/run-tests.sh") is False
    assert _is_safe_guard_for_ticket("./scripts/test.sh") is False
    assert _is_safe_guard_for_ticket("test.sh") is False


def test_per_ticket_shell_scripts_are_filtered_out(tmp_path):
    """Per-ticket shell scripts are filtered; project guards still run."""
    project_cfg = {"safeguards": {"pre_complete": ["pytest"]}}
    ticket = {"safeguards": {"pre_complete": ["scripts/custom-test.sh", "make verify"]}}

    calls = []

    def recording_run(cmd, **kwargs):
        calls.append(cmd)
        return _mock_process(returncode=0)

    with patch("lanegate.safeguards._Popen", side_effect=recording_run):
        result = run_safeguards("pre_complete", ticket, project_cfg, tmp_path)

    assert result == (True, None)
    # Should have run: pytest (project) and make verify (safe ticket guard)
    # Should NOT have run: scripts/custom-test.sh (unsafe shell script)
    assert len(calls) == 2
    assert any("pytest" in str(c) for c in calls)
    assert any("make" in str(c) and "verify" in str(c) for c in calls)


def test_effective_safeguards_filters_shell_scripts():
    """effective_safeguards filters shell scripts from per-ticket config."""
    project_cfg = {"safeguards": {"pre_complete": ["pytest"]}}
    ticket = {"safeguards": {"pre_complete": ["scripts/test.sh", "npm test", "cargo test"]}}

    guards = effective_safeguards("pre_complete", ticket, project_cfg)

    # Should contain: pytest (project) + npm test (safe) + cargo test (safe)
    # Should NOT contain: scripts/test.sh (filtered)
    assert len(guards) == 3
    assert "pytest" in guards
    assert "npm test" in guards
    assert "cargo test" in guards
    assert not any(".sh" in g for g in guards)


def test_trusted_ticket_can_use_shell_scripts(tmp_path):
    """Tickets marked trusted: true can use shell scripts in per-ticket safeguards."""
    script_path = tmp_path / "scripts" / "custom-test.sh"
    script_path.parent.mkdir(parents=True)
    script_path.write_text("#!/bin/sh\nexit 0\n")

    project_cfg = {"safeguards": {"pre_complete": ["pytest"]}}
    ticket = {
        "trusted": True,
        "safeguards": {"pre_complete": ["scripts/custom-test.sh"]},
    }

    guards = effective_safeguards("pre_complete", ticket, project_cfg)

    # Trusted ticket can use shell scripts
    assert len(guards) == 2
    assert "pytest" in guards
    assert any(".sh" in g for g in guards)


# ---------------------------------------------------------------------------
# Security: Script/touches conflicts (F41)
# ---------------------------------------------------------------------------


def test_check_script_guard_conflicts_no_conflict(tmp_path):
    """No error when guard script is not in touches."""
    ticket = {
        "touches": ["src/main.py", "tests/test.py"],
        "safeguards": {"pre_complete": ["scripts/test.sh"]},
    }

    errors = _check_script_guard_conflicts(ticket)
    assert errors == []


def test_check_script_guard_conflicts_detects_conflict(tmp_path, capsys):
    """Error when guard script appears in touches (agent could modify it)."""
    ticket = {
        "touches": ["src/main.py", "scripts/test.sh"],
        "safeguards": {"pre_complete": ["scripts/test.sh"]},
    }

    errors = _check_script_guard_conflicts(ticket)
    assert len(errors) == 1
    assert "scripts/test.sh" in errors[0]
    assert "cannot be in touches" in errors[0]


def test_run_safeguards_blocks_on_script_guard_conflict(tmp_path, capsys):
    """run_safeguards fails when a guard script is in touches."""
    ticket = {
        "touches": ["scripts/test.sh"],
        "safeguards": {"pre_complete": ["scripts/test.sh"]},
    }
    cfg = {}

    result = run_safeguards("pre_complete", ticket, cfg, tmp_path)

    assert result[0] is False
    assert "cannot be in touches" in result[1]
    err = capsys.readouterr().err
    assert "ERROR" in err
    assert "cannot be in touches" in err


# ---------------------------------------------------------------------------
# Security: Trusted tickets (F41)
# ---------------------------------------------------------------------------


def test_untrusted_ticket_safeguards_filtered():
    """Untrusted tickets have per-ticket safeguards filtered (shell scripts removed)."""
    project_cfg = {"safeguards": {"pre_complete": ["pytest"]}}
    ticket = {
        "trusted": False,
        "safeguards": {"pre_complete": ["scripts/test.sh", "npm test"]},
    }

    guards = effective_safeguards("pre_complete", ticket, project_cfg)

    # Should have pytest (project) + npm test (safe)
    # Should NOT have scripts/test.sh (filtered)
    assert "pytest" in guards
    assert "npm test" in guards
    assert not any(".sh" in g for g in guards)


def test_trusted_true_ticket_safeguards_not_filtered():
    """Trusted tickets can use any guard type in per-ticket safeguards."""
    project_cfg = {"safeguards": {"pre_complete": ["pytest"]}}
    ticket = {
        "trusted": True,
        "safeguards": {"pre_complete": ["scripts/test.sh", "make custom"]},
    }

    guards = effective_safeguards("pre_complete", ticket, project_cfg)

    # Trusted ticket gets everything: project + all per-ticket guards
    assert "pytest" in guards
    assert "scripts/test.sh" in guards
    assert "make custom" in guards


# ---------------------------------------------------------------------------
# Timeout support (TICK-168)
# ---------------------------------------------------------------------------


def test_guard_timeout_kills_hanging(tmp_path, capsys):
    """A Popen construction timeout is reported as a failed guard."""
    def mock_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["fake"], timeout=5)

    with patch("lanegate.safeguards._Popen", side_effect=mock_timeout):
        result = _run_one_guard("pytest", tmp_path, timeout_s=5)

    assert result[0] is False
    assert "timed" in result[1].lower()
    err = capsys.readouterr().err
    assert "TIMEOUT" in err
    assert "5" in err


def test_guard_timeout_passed_to_subprocess(tmp_path):
    """The timeout is passed to the Popen process's communicate call."""
    processes = []

    def recording_run(*args, **kwargs):
        process = _mock_process(returncode=0)
        processes.append(process)
        return process

    with patch("lanegate.safeguards._Popen", side_effect=recording_run):
        _run_one_guard("pytest", tmp_path, timeout_s=120)

    assert len(processes) == 1
    processes[0].communicate.assert_called_once_with(timeout=120)


def test_run_safeguards_applies_timeout_from_config(tmp_path):
    """run_safeguards extracts timeout_s and passes it to communicate."""
    processes = []

    def recording_run(*args, **kwargs):
        process = _mock_process(returncode=0)
        processes.append(process)
        return process

    cfg = {
        "safeguards": {
            "pre_complete": ["pytest"],
            "timeout_s": 300,
        }
    }
    ticket = {}

    with patch("lanegate.safeguards._Popen", side_effect=recording_run):
        result = run_safeguards("pre_complete", ticket, cfg, tmp_path)

    assert result == (True, None)
    assert len(processes) == 1
    processes[0].communicate.assert_called_once_with(timeout=300)


# ---------------------------------------------------------------------------
# Retry support (TICK-168)
# ---------------------------------------------------------------------------


def test_retry_flaky_guard_passes(tmp_path, capsys):
    """A flaky guard that fails once then passes is treated as passing after retry."""
    call_count = [0]

    def mock_flaky_run(*args, **kwargs):
        call_count[0] += 1
        # First call fails, second succeeds
        returncode = 1 if call_count[0] == 1 else 0
        return _mock_process(returncode=returncode)

    cfg = {
        "safeguards": {
            "pre_complete": ["pytest"],
            "retry_on_failure": 1,  # allow 1 retry
        }
    }
    ticket = {}

    with patch("lanegate.safeguards._Popen", side_effect=mock_flaky_run):
        result = run_safeguards("pre_complete", ticket, cfg, tmp_path)

    assert result == (True, None)
    assert call_count[0] == 2  # called twice: first fail, then pass


def test_retry_exhaustion_fails(tmp_path, capsys):
    """When all retries are exhausted, the guard is still reported as failed."""
    call_count = [0]

    def mock_always_fail(*args, **kwargs):
        call_count[0] += 1
        return _mock_process(returncode=1)

    cfg = {
        "safeguards": {
            "pre_complete": ["pytest"],
            "retry_on_failure": 2,  # allow up to 2 retries (3 total attempts)
        }
    }
    ticket = {}

    with patch("lanegate.safeguards._Popen", side_effect=mock_always_fail):
        result = run_safeguards("pre_complete", ticket, cfg, tmp_path)

    assert result[0] is False
    assert "attempts" in result[1].lower()
    assert call_count[0] == 3  # attempted 3 times (1 + 2 retries)
    err = capsys.readouterr().err
    assert "FAIL" in err


def test_retry_on_failure_parameter_passed_to_guard(tmp_path):
    """_run_one_guard accepts and uses the retry_count parameter."""
    call_count = [0]

    def mock_flaky_run(*args, **kwargs):
        call_count[0] += 1
        returncode = 1 if call_count[0] == 1 else 0
        return _mock_process(returncode=returncode)

    with patch("lanegate.safeguards._Popen", side_effect=mock_flaky_run):
        result = _run_one_guard("pytest", tmp_path, retry_count=1)

    assert result == (True, None)
    assert call_count[0] == 2


def test_default_no_config_unchanged(tmp_path):
    """When timeout_s and retry_on_failure are absent, behavior is unchanged."""
    call_count = [0]

    def mock_run(*args, **kwargs):
        call_count[0] += 1
        # First call fails, second would succeed but we don't allow retries
        return _mock_process(returncode=1 if call_count[0] == 1 else 0)

    cfg = {
        "safeguards": {
            "pre_complete": ["pytest"],
            # No timeout_s, no retry_on_failure
        }
    }
    ticket = {}

    with patch("lanegate.safeguards._Popen", side_effect=mock_run):
        result = run_safeguards("pre_complete", ticket, cfg, tmp_path)

    assert result[0] is False  # failed on first attempt, no retry
    assert call_count[0] == 1  # only one attempt


def test_guard_that_never_exits_is_killed_at_timeout(tmp_path):
    """A genuinely hanging command (TICK-246) is killed once it exceeds timeout_s,
    instead of hanging lanegate complete/merge indefinitely."""
    cfg = {"safeguards": {"pre_complete": ["pytest"], "timeout_s": 1}}
    ticket = {}

    # sleep 30 stands in for a hung pytest run; timeout_s=1 must cut it short.
    with patch("lanegate.safeguards._resolve_command", return_value=["sleep", "30"]):
        start = time.monotonic()
        result, _reason = run_safeguards("pre_complete", ticket, cfg, tmp_path)
        elapsed = time.monotonic() - start

    assert result is False
    assert elapsed < 10  # killed well before the full 30s sleep would complete


def test_run_one_guard_reports_timed_out_guard(tmp_path):
    """The optional timed_out list distinguishes a timeout from a plain failure."""

    def mock_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["fake"], timeout=5)

    timed_out: list[str] = []
    with patch("lanegate.safeguards._Popen", side_effect=mock_timeout):
        result, _reason = _run_one_guard("pytest", tmp_path, timeout_s=5, timed_out=timed_out)

    assert result is False
    assert timed_out == ["pytest"]


def test_run_safeguards_reports_timed_out_guards(tmp_path):
    """run_safeguards forwards which guard(s) timed out via timed_out_guards."""

    def mock_run(cmd, **kwargs):
        if "lint" in cmd:
            return _mock_process(returncode=0)
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=5)

    cfg = {"safeguards": {"pre_complete": ["pytest", "make lint"], "timeout_s": 5}}
    timed_out: list[str] = []
    with patch("lanegate.safeguards._Popen", side_effect=mock_run):
        result, _reason = run_safeguards("pre_complete", {}, cfg, tmp_path, timed_out_guards=timed_out)

    assert result is False
    assert timed_out == ["pytest"]


def test_timeout_and_retry_work_together(tmp_path):
    """Timeout and retry can be used together in config."""
    call_count = [0]

    def mock_run(*args, **kwargs):
        call_count[0] += 1
        # First call times out, second succeeds
        if call_count[0] == 1:
            raise subprocess.TimeoutExpired(cmd=["fake"], timeout=5)
        return _mock_process(returncode=0)

    cfg = {
        "safeguards": {
            "pre_complete": ["pytest"],
            "timeout_s": 5,
            "retry_on_failure": 1,
        }
    }
    ticket = {}

    with patch("lanegate.safeguards._Popen", side_effect=mock_run):
        result = run_safeguards("pre_complete", ticket, cfg, tmp_path)

    # TimeoutExpired is not retried — it returns immediately
    assert result[0] is False
    assert "timed" in result[1].lower()
    assert call_count[0] == 1  # timeout returns immediately, no retry


# ---------------------------------------------------------------------------
# TICK-306: no full prompt/transcript persistence under tickets_dir
# ---------------------------------------------------------------------------


def test_no_prompt_persistence_missing_tickets_dir_is_noop(tmp_path):
    violations = _find_prompt_artifact_violations(tmp_path / "does-not-exist")
    assert violations == []


def test_no_prompt_persistence_normal_ticket_and_sidecar_pass(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    (tickets_dir / "TICK-001.md").write_text("---\nid: TICK-001\n---\nNormal ticket body.\n")
    context_dir = tmp_path / ".lanegate" / "context" / "TICK-001"
    context_dir.mkdir(parents=True)
    (context_dir / "file_skeletons.json").write_text('{"a.py": "def a(): ..."}')

    violations = _find_prompt_artifact_violations(tickets_dir)

    assert violations == []


@pytest.mark.parametrize(
    "filename",
    [
        "TICK-001.session",
        "TICK-001.jsonl",
        "TICK-001-prompt.txt",
        "executor-transcript.log",
        "TICK-001.chat",
        "TICK-001.history",
    ],
)
def test_no_transcript_in_tickets_rejects_artifact_filenames(tmp_path, filename):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    (tickets_dir / filename).write_text("some persisted content")

    violations = _find_prompt_artifact_violations(tickets_dir)

    assert len(violations) == 1
    assert filename in violations[0]


def test_no_transcript_in_tickets_rejects_oversized_ticket_markdown(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    huge_prompt_like_body = "x" * 50000
    (tickets_dir / "TICK-001.md").write_text(huge_prompt_like_body)

    violations = _find_prompt_artifact_violations(tickets_dir)

    assert len(violations) == 1
    assert "TICK-001.md" in violations[0]


def test_no_transcript_in_tickets_allows_normal_sized_markdown(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    (tickets_dir / "TICK-001.md").write_text("---\nid: TICK-001\n---\nNormal body.\n" * 10)

    violations = _find_prompt_artifact_violations(tickets_dir)

    assert violations == []


def test_no_transcript_in_tickets_allows_accumulated_operational_history(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    authored_content = "---\nid: TICK-001\n---\n# Title\n\nAuthored body text.\n"
    operational_history = (
        "\n## Review Findings\n" + ("Review finding detail line.\n" * 1000) +
        "\n## Auto-Fix Attempt 1\n" + ("Auto-fix attempt log line.\n" * 1000) +
        "\n## Status History\n" + ("Status history entry line.\n" * 1000)
    )
    full_content = authored_content + operational_history
    assert len(full_content.encode("utf-8")) > 40000
    (tickets_dir / "TICK-001.md").write_text(full_content)

    violations = _find_prompt_artifact_violations(tickets_dir)

    assert violations == []


def test_no_transcript_in_tickets_allows_large_dismissal_rationale(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    full_content = (
        "## Dismissal Rationale\n"
        + ("Dismissed review finding detail.\n" * 1600)
        + "\n## Background\nAuthored body text.\n"
    )
    assert len(full_content.encode("utf-8")) > 40000
    (tickets_dir / "TICK-001.md").write_text(full_content)

    violations = _find_prompt_artifact_violations(
        tickets_dir, own_ticket_id="TICK-001"
    )

    assert violations == []


def test_no_transcript_in_tickets_rejects_pasted_transcript_under_operational_section(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    authored_content = "---\nid: TICK-001\n---\n# Title\n\nAuthored body text.\n"
    transcript_under_review_findings = (
        "\n## Review Findings\n" +
        ('{"step_index": 1, "type": "USER_INPUT", "content": "pasted transcript line"}\n' * 1000)
    )
    full_content = authored_content + transcript_under_review_findings
    assert len(full_content.encode("utf-8")) > 40000
    (tickets_dir / "TICK-001.md").write_text(full_content)

    violations = _find_prompt_artifact_violations(tickets_dir)

    assert len(violations) == 1
    assert "TICK-001.md" in violations[0]


def test_no_transcript_in_tickets_rejects_oversized_single_authored_section(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    authored_content = "---\nid: TICK-001\n---\n# Title\n\nAuthored body text.\n"
    huge_authored_section = (
        "\n## Background\n" + ("x" * 50000)
    )
    full_content = authored_content + huge_authored_section
    assert len(full_content.encode("utf-8")) > 40000
    (tickets_dir / "TICK-001.md").write_text(full_content)

    violations = _find_prompt_artifact_violations(tickets_dir)

    assert len(violations) == 1
    assert "TICK-001.md" in violations[0]



def test_no_transcript_in_tickets_oversized_other_ticket_is_not_flagged_when_scoped(tmp_path):
    """A pre-merge/post-merge run for TICK-001 must not fail because some
    unrelated, already-merged ticket (e.g. a legacy TICK-117-style file with
    an old embedded file_skeletons blob) happens to sit oversized in the same
    tickets_dir -- own_ticket_id scopes the size check to the ticket actually
    in flight."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    (tickets_dir / "TICK-001.md").write_text("---\nid: TICK-001\n---\nNormal body.\n")
    (tickets_dir / "TICK-002.md").write_text("x" * 50000)

    violations = _find_prompt_artifact_violations(tickets_dir, own_ticket_id="TICK-001")

    assert violations == []


def test_no_transcript_in_tickets_own_oversized_ticket_still_flagged_when_scoped(tmp_path):
    """The scoping in the test above must not blind the check to the ticket's
    own oversized body -- only unrelated tickets are exempt."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    (tickets_dir / "TICK-001.md").write_text("x" * 50000)
    (tickets_dir / "TICK-002.md").write_text("---\nid: TICK-002\n---\nNormal body.\n")

    violations = _find_prompt_artifact_violations(tickets_dir, own_ticket_id="TICK-001")

    assert len(violations) == 1
    assert "TICK-001.md" in violations[0]


def test_run_safeguards_pre_merge_does_not_block_on_unrelated_oversized_ticket(tmp_path):
    """Integration-level regression: a merge for TICK-001 must not be rolled
    back by an unrelated, already-merged legacy ticket's oversized file
    elsewhere in tickets_dir -- this is the exact failure that blocked
    TICK-259's merge (TICK-117/143/144, pre-existing and unrelated)."""
    tickets_dir = tmp_path / ".lanegate" / "tickets"
    tickets_dir.mkdir(parents=True)
    (tickets_dir / "TICK-001.md").write_text("---\nid: TICK-001\n---\nNormal body.\n")
    (tickets_dir / "TICK-117.md").write_text("x" * 50000)

    result, reason = run_safeguards("pre_merge", {"id": "TICK-001"}, {}, tmp_path)

    assert result is True
    assert reason is None


def test_run_safeguards_pre_complete_blocks_on_persisted_transcript(tmp_path):
    tickets_dir = tmp_path / ".lanegate" / "tickets"
    tickets_dir.mkdir(parents=True)
    (tickets_dir / "session.jsonl").write_text('{"role": "user", "content": "..."}')

    result, reason = run_safeguards("pre_complete", {}, {}, tmp_path)

    assert result is False
    assert "session.jsonl" in reason


def test_run_safeguards_pre_merge_blocks_on_persisted_transcript(tmp_path):
    tickets_dir = tmp_path / ".lanegate" / "tickets"
    tickets_dir.mkdir(parents=True)
    (tickets_dir / "session.jsonl").write_text('{"role": "user", "content": "..."}')

    result, reason = run_safeguards("pre_merge", {}, {}, tmp_path)

    assert result is False
    assert "session.jsonl" in reason


def test_run_safeguards_post_merge_does_not_check_transcript_persistence(tmp_path):
    """post_merge runs against the merged main tree after promotion, not a
    ticket worktree -- the persistence guard only applies to pre_complete and
    pre_merge, where an executor's own worktree is still in play."""
    tickets_dir = tmp_path / ".lanegate" / "tickets"
    tickets_dir.mkdir(parents=True)
    (tickets_dir / "session.jsonl").write_text('{"role": "user", "content": "..."}')

    result, _reason = run_safeguards("post_merge", {}, {}, tmp_path)

    assert result is True


def test_run_safeguards_respects_configured_tickets_dir(tmp_path):
    custom_dir = tmp_path / "custom-tickets"
    custom_dir.mkdir()
    (custom_dir / "TICK-001.session").write_text("session marker")
    cfg = {"tickets_dir": "custom-tickets"}

    result, reason = run_safeguards("pre_complete", {}, cfg, tmp_path)

    assert result is False
    assert "TICK-001.session" in reason


def test_run_safeguards_pre_complete_passes_when_no_artifacts(tmp_path):
    tickets_dir = tmp_path / ".lanegate" / "tickets"
    tickets_dir.mkdir(parents=True)
    (tickets_dir / "TICK-001.md").write_text("---\nid: TICK-001\n---\nNormal body.\n")

    result, _reason = run_safeguards("pre_complete", {}, {}, tmp_path)

    assert result is True


def test_safeguard_process_group_reaping_verifies_child_killed(tmp_path):
    from lanegate.safeguards import _run_one_guard

    if sys.platform == "win32":
        pytest.skip("Process group SIGKILL is POSIX-specific")

    pid_file = tmp_path / "child.pid"
    script = tmp_path / "spawn_child.py"
    script.write_text(
        f"import subprocess, sys, time\n"
        f"proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open('{pid_file}', 'w').write(str(proc.pid))\n"
        f"time.sleep(60)\n"
    )

    ok, reason = _run_one_guard(f"{sys.executable} {script}", tmp_path, timeout_s=1)

    assert ok is False
    assert "timed out" in reason
    assert pid_file.exists()

    child_pid = int(pid_file.read_text().strip())
    # Verify the spawned child process no longer exists in OS kernel
    time.sleep(0.2)
    with pytest.raises(OSError):
        os.kill(child_pid, 0)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def test_safeguard_file_lock_serializes_separate_worktree_processes(tmp_path):
    from lanegate.safeguards import _safeguard_file_lock

    control = tmp_path / "control"
    control.mkdir()
    _git(control, "init", "-b", "main")
    _git(control, "config", "user.email", "tests@example.invalid")
    _git(control, "config", "user.name", "LaneGate tests")
    (control / "README.md").write_text("fixture\n")
    _git(control, "add", "README.md")
    _git(control, "commit", "-m", "fixture")

    worktrees = control / "worktrees"
    worktree_a = worktrees / "a"
    worktree_b = worktrees / "b"
    _git(control, "worktree", "add", "-b", "test-lock-a", str(worktree_a), "main")
    _git(control, "worktree", "add", "-b", "test-lock-b", str(worktree_b), "main")

    acquired = tmp_path / "acquired"
    child_code = """
from pathlib import Path
import sys

from lanegate.safeguards import _safeguard_file_lock

with _safeguard_file_lock(Path(sys.argv[1])):
    Path(sys.argv[2]).touch()
"""

    with _safeguard_file_lock(worktree_a):
        child_env = os.environ | {
            "PYTHONPATH": str(Path(__file__).resolve().parents[1])
            + os.pathsep
            + os.environ.get("PYTHONPATH", ""),
        }
        child = subprocess.Popen(
            [sys.executable, "-c", child_code, str(worktree_b), str(acquired)],
            cwd=worktree_b,
            env=child_env,
            stdout=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.25)
        assert not acquired.exists(), "second worktree acquired the shared safeguard lock"

    child_stdout, _ = child.communicate(timeout=5)
    assert child.returncode == 0
    assert acquired.exists()
    assert (control / ".lanegate" / "safeguard.lock").exists()
    assert "WAIT [safeguard-lock] another worktree is running safeguards" in child_stdout


def test_safeguard_file_lock_uses_an_unbounded_portalocker_wait_after_contention(tmp_path):
    """Regression for portalocker treating ``None`` as its five-second default."""
    from lanegate import safeguards

    class BusyThenAcquired:
        timeouts: list[float] = []

        def __init__(self, _path, _mode, *, timeout, **_kwargs):
            self.timeout = timeout
            self.timeouts.append(timeout)

        def acquire(self):
            if self.timeout == 0:
                raise portalocker.exceptions.LockException("busy")
            return self

        def release(self):
            pass

    with patch("lanegate.safeguards.portalocker.Lock", BusyThenAcquired):
        with safeguards._safeguard_file_lock(tmp_path):
            pass

    assert BusyThenAcquired.timeouts == [0, math.inf]


def test_safeguards_wait_for_local_contention_without_failing(tmp_path, capsys):
    """A parallel worker queues behind an in-process guard and then runs."""
    from lanegate.safeguards import _SAFEGUARD_LOCK

    holder_ready = threading.Event()
    release_holder = threading.Event()
    worker_done = threading.Event()
    result: list[tuple[bool, str | None]] = []

    def hold_lock() -> None:
        with _SAFEGUARD_LOCK:
            holder_ready.set()
            release_holder.wait(timeout=5)

    def run_waiting_guard() -> None:
        with patch("lanegate.safeguards._Popen", side_effect=_mock_run_ok):
            result.append(
                run_safeguards(
                    "pre_complete", {}, {"safeguards": {"pre_complete": ["pytest"]}}, tmp_path
                )
            )
        worker_done.set()

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert holder_ready.wait(timeout=1)
    worker = threading.Thread(target=run_waiting_guard)
    worker.start()
    assert not worker_done.wait(timeout=0.1)

    release_holder.set()
    holder.join(timeout=2)
    worker.join(timeout=2)

    assert worker_done.is_set()
    assert result == [(True, None)]
    assert "WAIT [safeguard-lock] another local guard is running" in capsys.readouterr().out


def test_control_plane_file_requires_ticket_branch(tmp_path):
    """Control-plane files (safeguards.py, analyze.py, review.py) must be modified within a ticket worktree.

    Control-plane files come only from control_plane_files in .lanegate.yml
    (never hardcoded), so cfg declares them explicitly here.
    """
    # Attempting to modify safeguards.py directly on main without ticket worktree fails
    ticket_on_main = {"touches": ["lanegate/safeguards.py"]}
    cfg = {"control_plane_files": ["lanegate/safeguards.py"]}
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="main")
        passed, reason = run_safeguards("pre_complete", ticket_on_main, cfg, tmp_path)
        assert passed is False
        assert "must be modified within a ticket worktree" in reason

    # Modifying in a ticket worktree passes
    ticket_in_worktree = {"id": "TICK-610", "touches": ["lanegate/safeguards.py"]}
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="feature-branch")
        passed_wt, reason_wt = run_safeguards("pre_complete", ticket_in_worktree, cfg, tmp_path)
        assert passed_wt is True

    # Non-control-plane file on main works fine
    ticket_regular = {"touches": ["src/app.py"]}
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="main")
        passed_reg, _ = run_safeguards("pre_complete", ticket_regular, cfg, tmp_path)
        assert passed_reg is True


def test_control_plane_multi_commit_direct_to_main_is_not_bypassed(tmp_path):
    """A control-plane edit committed directly to main isn't missed just because
    a later, unrelated commit is what HEAD happens to point at.

    Regression for a real bypass: the old check only ever compared
    HEAD~1..HEAD (or a self-diff of trunk...trunk, which is always empty),
    so an earlier direct-to-main commit touching a control-plane file was
    invisible once any later commit landed on top of it.
    """
    if shutil.which("git") is None:
        pytest.skip("git is required for this integration test")

    remote_dir = tmp_path / "remote.git"
    remote_dir.mkdir()
    subprocess.run(["git", "init", "--bare", "-b", "main"], cwd=remote_dir, check=True, capture_output=True)

    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", str(remote_dir), str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "a@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "A"], cwd=repo, check=True)

    (repo / "lanegate").mkdir()
    (repo / "lanegate" / "safeguards.py").write_text("# initial\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo, check=True, capture_output=True)

    # Commit 1 (direct to main): touches the control-plane file.
    (repo / "lanegate" / "safeguards.py").write_text("# modified control-plane logic\n")
    subprocess.run(["git", "commit", "-am", "sneak in a control-plane change"], cwd=repo, check=True, capture_output=True)

    # Commit 2 (direct to main): unrelated file, lands on top as the new HEAD.
    (repo / "README.md").write_text("unrelated change\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "unrelated"], cwd=repo, check=True, capture_output=True)

    cfg = {"control_plane_files": ["lanegate/safeguards.py"]}
    ticket = {"id": "TICK-610", "touches": ["README.md"]}
    passed, reason = run_safeguards("pre_complete", ticket, cfg, repo)
    assert passed is False
    assert "must be modified within a ticket worktree" in reason


def test_post_merge_verify_skips_control_plane_branch_isolation(tmp_path):
    cfg = {"control_plane_files": ["lanegate/safeguards.py"]}
    ticket = {"id": "TICK-610", "touches": ["lanegate/safeguards.py"], "review_independence": "independent"}
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="main")
        passed, reason = run_safeguards("pre_merge", ticket, cfg, tmp_path, label="post_merge_verify")
        assert passed is True
        assert reason is None


def test_is_control_plane_file_does_not_match_vendored_copy():
    from lanegate.safeguards import is_control_plane_file
    cfg = {"control_plane_files": ["lanegate/analyze.py"]}
    assert is_control_plane_file("lanegate/analyze.py", cfg) is True
    assert is_control_plane_file("third_party/lanegate/analyze.py", cfg) is False


def test_collect_control_plane_touches_handles_staged_rename(tmp_path):
    from lanegate.safeguards import collect_control_plane_touches
    cfg = {"control_plane_files": ["lanegate/safeguards.py"]}
    ticket = {"id": "TICK-610", "touches": []}
    (tmp_path / ".git").mkdir()
    porcelain_out = "R  lanegate/safeguards.py -> renamed_safeguards.py\n"

    def fake_run(cmd, cwd=None, capture_output=False, text=False, check=False):
        if "status" in cmd:
            return MagicMock(returncode=0, stdout=porcelain_out)
        return MagicMock(returncode=0, stdout="ticket-branch")

    with patch("subprocess.run", side_effect=fake_run):
        found, branch = collect_control_plane_touches(ticket, tmp_path, cfg)
        assert "lanegate/safeguards.py" in found

