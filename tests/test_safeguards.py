"""Tests for safeguards.py — pre_complete and pre_merge quality gates."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def _mock_run_ok(*args, **kwargs):
    """subprocess.run that always returns 0."""
    return MagicMock(returncode=0)


def _mock_run_fail(*args, **kwargs):
    """subprocess.run that always returns 1."""
    return MagicMock(returncode=1)


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
    with patch("lanegate.safeguards.subprocess.run", side_effect=_mock_run_ok):
        result = run_safeguards("pre_complete", {}, cfg, tmp_path)
    assert result == (True, None)


def test_all_guards_pass_returns_true(tmp_path):
    """All guards passing → (True, None)."""
    cfg = {"safeguards": {"pre_complete": ["pytest", "make lint"]}}
    with patch("lanegate.safeguards.subprocess.run", side_effect=_mock_run_ok):
        result = run_safeguards("pre_complete", {}, cfg, tmp_path)
    assert result == (True, None)


# ---------------------------------------------------------------------------
# run_safeguards — failing guard blocks transition
# ---------------------------------------------------------------------------


def test_failing_guard_returns_false(tmp_path, capsys):
    """A guard that exits non-zero returns (False, reason)."""
    cfg = {"safeguards": {"pre_complete": ["pytest"]}}
    with patch("lanegate.safeguards.subprocess.run", side_effect=_mock_run_fail):
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
        return MagicMock(returncode=0)

    cfg = {"safeguards": {"pre_complete": ["pytest"]}}
    with (
        patch("builtins.print", wraps=print) as mock_print,
        patch("lanegate.safeguards.subprocess.run", side_effect=mock_run),
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
    with patch("lanegate.safeguards.subprocess.run", side_effect=_mock_run_ok):
        run_safeguards("pre_complete", {}, cfg, tmp_path)
    out = capsys.readouterr().out
    assert re.search(r"PASS \[pre_complete\] pytest \(\d+\.\d+s\)", out)


def test_fail_line_includes_elapsed_time(tmp_path, capsys):
    """The FAIL line also carries the elapsed wall time."""
    cfg = {"safeguards": {"pre_complete": ["pytest"]}}
    with patch("lanegate.safeguards.subprocess.run", side_effect=_mock_run_fail):
        run_safeguards("pre_complete", {}, cfg, tmp_path)
    err = capsys.readouterr().err
    assert re.search(r"FAIL \[pre_complete\] pytest \(\d+\.\d+s\)", err)


def test_first_failing_guard_marks_all_failed(tmp_path):
    """When the first guard fails, return (False, reason) immediately without running remaining guards."""
    call_count = [0]

    def mock_run_alternating(*args, **kwargs):
        call_count[0] += 1
        # First call fails, second would succeed
        return MagicMock(returncode=1 if call_count[0] == 1 else 0)

    cfg = {"safeguards": {"pre_complete": ["pytest", "make lint"]}}
    with patch("lanegate.safeguards.subprocess.run", side_effect=mock_run_alternating):
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
        return MagicMock(returncode=0)

    with patch("lanegate.safeguards.subprocess.run", side_effect=recording_run):
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
        return MagicMock(returncode=0)

    with patch("lanegate.safeguards.subprocess.run", side_effect=recording_run):
        result = run_safeguards("pre_complete", ticket, project_cfg, tmp_path)

    # Empty list at ticket level means "add nothing", so project guards still run
    assert result == (True, None)
    assert len(calls) == 1
    assert any("pytest" in str(c) for c in calls)


def test_per_ticket_pre_merge_is_additive(tmp_path):
    """per-ticket pre_merge is added to (not replacing) project pre_merge."""
    project_cfg = {"safeguards": {"pre_merge": ["pytest"]}}
    ticket = {"safeguards": {"pre_merge": ["cargo test"]}}

    calls = []

    def recording_run(cmd, **kwargs):
        calls.append(cmd)
        return MagicMock(returncode=0)

    with patch("lanegate.safeguards.subprocess.run", side_effect=recording_run):
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
        return MagicMock(returncode=0)

    with patch("lanegate.safeguards.subprocess.run", side_effect=recording_run):
        result = run_safeguards("post_merge", {}, project_cfg, tmp_path)

    assert result == (True, None)
    assert calls == [[sys.executable, "-m", "pytest", "-q", "--tb=short"]]


def test_per_ticket_post_merge_is_additive(tmp_path):
    """per-ticket post_merge is added to (not replacing) project post_merge."""
    project_cfg = {"safeguards": {"post_merge": ["pytest"]}}
    ticket = {"safeguards": {"post_merge": ["make regression-check"]}}
    calls = []

    def recording_run(cmd, **kwargs):
        calls.append(cmd)
        return MagicMock(returncode=0)

    with patch("lanegate.safeguards.subprocess.run", side_effect=recording_run):
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


def test_resolve_npm_test(tmp_path):
    cmd = _resolve_command("npm test", tmp_path)
    assert cmd == ["npm", "run", "test"]


def test_resolve_npm_run_custom(tmp_path):
    cmd = _resolve_command("npm run ci", tmp_path)
    assert cmd == ["npm", "run", "ci"]


def test_resolve_cargo_test(tmp_path):
    cmd = _resolve_command("cargo test", tmp_path)
    assert cmd == ["cargo", "test"]


def test_resolve_go_test(tmp_path):
    cmd = _resolve_command("go test ./...", tmp_path)
    assert cmd == ["go", "test", "./..."]


def test_resolve_make(tmp_path):
    cmd = _resolve_command("make test", tmp_path)
    assert cmd == ["make", "test"]


def test_resolve_fallback_shlex(tmp_path):
    """Unrecognised strings are parsed with shlex.split (shell=False)."""
    cmd = _resolve_command("my-custom-runner --flag value", tmp_path)
    assert cmd == ["my-custom-runner", "--flag", "value"]


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


# ---------------------------------------------------------------------------
# _run_one_guard — missing executable
# ---------------------------------------------------------------------------


def test_run_one_guard_missing_executable(tmp_path, capsys):
    """If the resolved command binary doesn't exist, returns (False, reason)."""
    with patch("lanegate.safeguards.subprocess.run", side_effect=FileNotFoundError("no such file")):
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
            return MagicMock(returncode=1)
        return MagicMock(returncode=0)

    with patch("lanegate.safeguards.subprocess.run", side_effect=mock_run):
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
        return MagicMock(returncode=0)

    with (
        patch("lanegate.safeguards.subprocess.run", side_effect=mock_run),
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

    with patch("lanegate.safeguards.subprocess.run", side_effect=mock_run):
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
        return MagicMock(returncode=0)

    with safeguard_lock(tmp_path, "TICK-351"):
        # Simulates a prior `lanegate complete TICK-351` still in flight.
        with patch("lanegate.safeguards.subprocess.run", side_effect=mock_run):
            with pytest.raises(SystemExit) as exc_info:
                cmd_complete("TICK-351", cfg, tmp_path)

    assert exc_info.value.code != 0
    # No redundant pytest safeguard subprocess was spawned (only incidental git
    # bookkeeping calls made before the lock check, if any, are allowed through).
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
            return MagicMock(returncode=1)
        return MagicMock(returncode=0)

    with patch("lanegate.safeguards.subprocess.run", side_effect=mock_safeguard_run):
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
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.safeguards.subprocess.run", side_effect=mock_run),
        patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run),
    ):
        cmd_merge("TICK-401", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-401.md")
    assert ticket["status"] == "merged"


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
        return MagicMock(returncode=0, stdout="", stderr="")

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
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.safeguards.subprocess.run", side_effect=recording_run),
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
        return MagicMock(returncode=0)

    with patch("lanegate.safeguards.subprocess.run", side_effect=recording_run):
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
    """When subprocess.run times out, _run_one_guard catches TimeoutExpired and returns (False, reason)."""
    def mock_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["fake"], timeout=5)

    with patch("lanegate.safeguards.subprocess.run", side_effect=mock_timeout):
        result = _run_one_guard("pytest", tmp_path, timeout_s=5)

    assert result[0] is False
    assert "timed" in result[1].lower()
    err = capsys.readouterr().err
    assert "TIMEOUT" in err
    assert "5" in err


def test_guard_timeout_passed_to_subprocess(tmp_path):
    """The timeout_s parameter is passed to subprocess.run."""
    captured_kwargs = []

    def recording_run(*args, **kwargs):
        captured_kwargs.append(kwargs)
        return MagicMock(returncode=0)

    with patch("lanegate.safeguards.subprocess.run", side_effect=recording_run):
        _run_one_guard("pytest", tmp_path, timeout_s=120)

    assert len(captured_kwargs) == 1
    assert captured_kwargs[0].get("timeout") == 120


def test_run_safeguards_applies_timeout_from_config(tmp_path):
    """run_safeguards extracts timeout_s from config and passes it to _run_one_guard."""
    captured_kwargs = []

    def recording_run(*args, **kwargs):
        captured_kwargs.append(kwargs)
        return MagicMock(returncode=0)

    cfg = {
        "safeguards": {
            "pre_complete": ["pytest"],
            "timeout_s": 300,
        }
    }
    ticket = {}

    with patch("lanegate.safeguards.subprocess.run", side_effect=recording_run):
        result = run_safeguards("pre_complete", ticket, cfg, tmp_path)

    assert result == (True, None)
    assert len(captured_kwargs) == 1
    assert captured_kwargs[0].get("timeout") == 300


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
        return MagicMock(returncode=returncode)

    cfg = {
        "safeguards": {
            "pre_complete": ["pytest"],
            "retry_on_failure": 1,  # allow 1 retry
        }
    }
    ticket = {}

    with patch("lanegate.safeguards.subprocess.run", side_effect=mock_flaky_run):
        result = run_safeguards("pre_complete", ticket, cfg, tmp_path)

    assert result == (True, None)
    assert call_count[0] == 2  # called twice: first fail, then pass


def test_retry_exhaustion_fails(tmp_path, capsys):
    """When all retries are exhausted, the guard is still reported as failed."""
    call_count = [0]

    def mock_always_fail(*args, **kwargs):
        call_count[0] += 1
        return MagicMock(returncode=1)

    cfg = {
        "safeguards": {
            "pre_complete": ["pytest"],
            "retry_on_failure": 2,  # allow up to 2 retries (3 total attempts)
        }
    }
    ticket = {}

    with patch("lanegate.safeguards.subprocess.run", side_effect=mock_always_fail):
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
        return MagicMock(returncode=returncode)

    with patch("lanegate.safeguards.subprocess.run", side_effect=mock_flaky_run):
        result = _run_one_guard("pytest", tmp_path, retry_count=1)

    assert result == (True, None)
    assert call_count[0] == 2


def test_default_no_config_unchanged(tmp_path):
    """When timeout_s and retry_on_failure are absent, behavior is unchanged."""
    call_count = [0]

    def mock_run(*args, **kwargs):
        call_count[0] += 1
        # First call fails, second would succeed but we don't allow retries
        return MagicMock(returncode=1 if call_count[0] == 1 else 0)

    cfg = {
        "safeguards": {
            "pre_complete": ["pytest"],
            # No timeout_s, no retry_on_failure
        }
    }
    ticket = {}

    with patch("lanegate.safeguards.subprocess.run", side_effect=mock_run):
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
    with patch("lanegate.safeguards.subprocess.run", side_effect=mock_timeout):
        result, _reason = _run_one_guard("pytest", tmp_path, timeout_s=5, timed_out=timed_out)

    assert result is False
    assert timed_out == ["pytest"]


def test_run_safeguards_reports_timed_out_guards(tmp_path):
    """run_safeguards forwards which guard(s) timed out via timed_out_guards."""

    def mock_run(cmd, **kwargs):
        if "lint" in cmd:
            return MagicMock(returncode=0)
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=5)

    cfg = {"safeguards": {"pre_complete": ["pytest", "make lint"], "timeout_s": 5}}
    timed_out: list[str] = []
    with patch("lanegate.safeguards.subprocess.run", side_effect=mock_run):
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
        return MagicMock(returncode=0)

    cfg = {
        "safeguards": {
            "pre_complete": ["pytest"],
            "timeout_s": 5,
            "retry_on_failure": 1,
        }
    }
    ticket = {}

    with patch("lanegate.safeguards.subprocess.run", side_effect=mock_run):
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
