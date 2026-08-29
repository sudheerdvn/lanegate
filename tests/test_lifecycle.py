"""Tests for lifecycle.py — status transitions, lock-until-merge, merge worktree cleanup."""

import inspect
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lanegate.concurrency import SafeguardLockHeld
from lanegate.lifecycle import (
    _commit_status,
    _mark_needs_review,
    _push_branch_and_open_pr,
    check_touches_compliance,
    cmd_close,
    cmd_complete,
    cmd_done,
    cmd_fail,
    cmd_hibernate,
    cmd_merge,
    cmd_needs_review,
    cmd_open,
    cmd_reopen,
    cmd_resolve_conflict,
    cmd_recover_rate_limited_reviews,
    cmd_recover_rejected,
    cmd_reset,
    cmd_review,
    cmd_stop,
    cmd_supersede,
    cmd_validate,
    resolve_reviewer,
    spawn_detached,
)
from lanegate.lifecycle.hibernate import _hibernation_note, _write_hibernation_notes
from lanegate.ticket import parse_ticket, write_ticket
from lanegate.worktree import worktree_path
from tests._helpers.lifecycle import (
    commit_all as _commit_all,
    default_cfg as _default_cfg,
    init_git_repo as _init_git_repo,
    is_iso_utc as _is_iso_utc,
    make_git_diff_mock as _make_git_diff_mock,
    start_cfg as _start_cfg,
    tracked_path_is_clean as _tracked_path_is_clean,
    write_ticket as _write_ticket,
    write_ticket_with_body as _write_ticket_with_body,
)


























































# ── cmd_review verdict tests ──────────────────────────────────────────────────
































# ---------------------------------------------------------------------------
# merge auto-log
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# _push_branch_and_open_pr unit tests
# ---------------------------------------------------------------------------


def _make_ticket(tid="TICK-001", title="Test ticket", branch="tick-001"):
    """Minimal ticket dict for helper tests."""
    return {"id": tid, "title": title, "branch": branch}


def test_push_branch_skips_when_gh_not_installed(tmp_path):
    """Returns None immediately when gh is not on PATH."""
    ticket = _make_ticket()
    with patch("lanegate.lifecycle.shutil.which", return_value=None):
        result = _push_branch_and_open_pr(tmp_path, "tick-001", ticket)
    assert result is None


def test_push_branch_skips_when_no_remote(tmp_path):
    """Returns None when git remote get-url origin fails."""
    ticket = _make_ticket()

    def mock_run(args, **kwargs):
        if "remote" in args and "get-url" in args:
            return MagicMock(returncode=1, stdout="", stderr="No remote")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.lifecycle.shutil.which", return_value="/usr/bin/gh"),
        patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run),
    ):
        result = _push_branch_and_open_pr(tmp_path, "tick-001", ticket)
    assert result is None


def test_push_branch_skips_when_push_fails(tmp_path):
    """Returns None when git push fails, prints warning."""
    ticket = _make_ticket()

    def mock_run(args, **kwargs):
        if "remote" in args and "get-url" in args:
            return MagicMock(returncode=0, stdout="git@github.com:org/repo.git", stderr="")
        if "push" in args:
            return MagicMock(returncode=1, stdout="", stderr="push rejected")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.lifecycle.shutil.which", return_value="/usr/bin/gh"),
        patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run),
    ):
        result = _push_branch_and_open_pr(tmp_path, "tick-001", ticket)
    assert result is None


def test_push_branch_creates_pr_and_returns_number_url(tmp_path):
    """Creates PR when no existing PR; returns (pr_number, pr_url)."""
    ticket = _make_ticket(tid="TICK-042", title="My feature", branch="tick-042")

    calls = []

    def mock_run(args, **kwargs):
        calls.append(list(args))
        if "remote" in args and "get-url" in args:
            return MagicMock(returncode=0, stdout="git@github.com:org/repo.git", stderr="")
        if "push" in args:
            return MagicMock(returncode=0, stdout="", stderr="")
        if "pr" in args and "view" in args:
            # No existing PR
            return MagicMock(returncode=1, stdout="", stderr="no pull requests found")
        if "pr" in args and "create" in args:
            return MagicMock(
                returncode=0, stdout="https://github.com/org/repo/pull/42\n", stderr=""
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.lifecycle.shutil.which", return_value="/usr/bin/gh"),
        patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run),
    ):
        result = _push_branch_and_open_pr(tmp_path, "tick-042", ticket)

    assert result == (42, "https://github.com/org/repo/pull/42")

    # Verify gh pr create was called with the right flags
    create_call = next((c for c in calls if "create" in c), None)
    assert create_call is not None
    assert "--base" in create_call and "main" in create_call
    assert "--head" in create_call and "tick-042" in create_call
    assert "--title" in create_call and "My feature" in create_call


def test_push_branch_reuses_existing_pr(tmp_path):
    """When gh pr view succeeds, returns existing PR without creating a new one."""
    ticket = _make_ticket(tid="TICK-007", title="Existing PR ticket", branch="tick-007")

    existing = {"number": 7, "url": "https://github.com/org/repo/pull/7"}

    calls = []

    def mock_run(args, **kwargs):
        calls.append(list(args))
        if "remote" in args and "get-url" in args:
            return MagicMock(returncode=0, stdout="git@github.com:org/repo.git", stderr="")
        if "push" in args:
            return MagicMock(returncode=0, stdout="", stderr="")
        if "pr" in args and "view" in args:
            return MagicMock(returncode=0, stdout=json.dumps(existing), stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.lifecycle.shutil.which", return_value="/usr/bin/gh"),
        patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run),
    ):
        result = _push_branch_and_open_pr(tmp_path, "tick-007", ticket)

    assert result == (7, "https://github.com/org/repo/pull/7")
    # gh pr create must NOT have been called
    assert not any("create" in c for c in calls)








# ---------------------------------------------------------------------------
# Generated metadata commit tests and optional GitHub PR behavior
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("initial_status", "transition"),
    [
        ("in_progress", "complete"),
        ("in_progress", "needs_review"),
        ("in_progress", "hibernate"),
        ("failed", "reopen"),
        ("in_review", "merge"),
    ],
)
def test_generated_status_writes_commit_even_when_commit_status_false(
    tmp_path, initial_status, transition
):
    """With commit_status_changes=False, status changes leave tracked ticket files dirty (F33 fix)."""
    if shutil.which("git") is None:
        pytest.skip("git is required for status commit integration test")

    _init_git_repo(tmp_path)
    subprocess.run(["git", "branch", "-m", "main"], cwd=tmp_path, check=True)
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    ticket_kwargs = {}
    if transition == "merge":
        ticket_kwargs["review_verdict"] = "approved"
    if transition == "complete":
        wt = worktrees_dir / "tick-001"
        wt.mkdir()
        subprocess.run(["git", "init"], cwd=wt, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=wt,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=wt,
            check=True,
            capture_output=True,
        )
        (wt / "some_file.py").write_text("# test\n")
        subprocess.run(["git", "add", "."], cwd=wt, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial commit"],
            cwd=wt,
            check=True,
            capture_output=True,
        )
        ticket_kwargs["worktree"] = str(wt)
        ticket_kwargs["branch"] = "main"
    ticket_path = _write_ticket(tickets_dir, "TICK-001", initial_status, **ticket_kwargs)
    _commit_all(tmp_path)

    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = "tickets"
    cfg["worktrees_dir"] = "worktrees"
    cfg["commit_status_changes"] = False

    if transition == "complete":
        from lanegate.lifecycle import _has_committed_changes
        with patch("lanegate.lifecycle.core_cmds._has_committed_changes", return_value=True):
            cmd_complete("TICK-001", cfg, tmp_path, allow_drift=True)
    elif transition == "needs_review":
        cmd_needs_review("TICK-001", cfg, tmp_path, reason="needs human check")
    elif transition == "hibernate":
        cmd_hibernate("TICK-001", cfg, tmp_path, reason="pause")
    elif transition == "reopen":
        cmd_reopen("TICK-001", cfg, tmp_path)
    elif transition == "merge":
        cmd_merge("TICK-001", cfg, tmp_path)

    assert not _tracked_path_is_clean(tmp_path, ticket_path), "with commit_status_changes=False, ticket should remain dirty"








# ---------------------------------------------------------------------------
# spawn_detached tests
# ---------------------------------------------------------------------------


def test_spawn_detached_unix(tmp_path):
    """On non-Windows: Popen is called with start_new_session=True."""

    log_path = tmp_path / "logs" / "watch.log"

    mock_proc = MagicMock()
    mock_proc.pid = 12345

    with (
        patch("lanegate.lifecycle.subprocess.Popen", return_value=mock_proc) as mock_popen,
        patch("lanegate.lifecycle.sys.platform", "linux"),
    ):
        pid = spawn_detached(["lanegate", "watch"], log_path)

    assert pid == 12345
    assert log_path.parent.exists(), "log_path.parent must be created"
    call_kwargs = mock_popen.call_args[1]
    assert call_kwargs.get("start_new_session") is True
    assert call_kwargs.get("close_fds") is True
    assert "creationflags" not in call_kwargs


def test_spawn_detached_windows(tmp_path):
    """On Windows: Popen is called with DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP."""
    log_path = tmp_path / "logs" / "watch.log"

    mock_proc = MagicMock()
    mock_proc.pid = 99999

    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    expected_flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

    import subprocess as _real_subprocess

    mock_popen = MagicMock(return_value=mock_proc)

    with (
        patch("lanegate.lifecycle.sys.platform", "win32"),
        patch(
            "builtins.__import__",
            side_effect=lambda name, *args, **kwargs: (
                type("mod", (), {"Popen": mock_popen, "DEVNULL": _real_subprocess.DEVNULL})()
                if name == "subprocess"
                else __import__(name, *args, **kwargs)
            ),
        ),
    ):
        # Import subprocess normally but intercept Popen on the win32 branch
        # We patch the inner import via a simpler approach:
        pass

    # Simpler approach: patch sys.platform and the subprocess import inside the win32 branch
    captured = {}

    def fake_popen(args, **kwargs):
        captured.update(kwargs)
        captured["args"] = args
        return mock_proc

    # The win32 branch does `import subprocess as _sp` then `_sp.Popen(...)`.
    # We patch subprocess.Popen globally for the duration.
    with (
        patch("subprocess.Popen", side_effect=fake_popen),
        patch("lanegate.lifecycle.sys.platform", "win32"),
    ):
        pid = spawn_detached(["lanegate", "watch"], log_path)

    assert pid == 99999
    assert log_path.parent.exists()
    assert captured.get("close_fds") is True
    assert captured.get("creationflags") == expected_flags
    assert "start_new_session" not in captured


def test_spawn_detached_creates_log_dir(tmp_path):
    """spawn_detached creates the log directory even if it doesn't exist."""
    log_path = tmp_path / "deeply" / "nested" / "dir" / "watch.log"
    assert not log_path.parent.exists()

    mock_proc = MagicMock()
    mock_proc.pid = 1

    with patch("lanegate.lifecycle.subprocess.Popen", return_value=mock_proc):
        spawn_detached(["lanegate", "watch"], log_path)

    assert log_path.parent.exists()


def test_spawn_detached_appends_to_existing_log(tmp_path):
    """spawn_detached opens the log in append mode, not truncating existing content."""
    log_path = tmp_path / "watch.log"
    log_path.write_text("existing content\n")

    mock_proc = MagicMock()
    mock_proc.pid = 2

    with patch("lanegate.lifecycle.subprocess.Popen", return_value=mock_proc):
        spawn_detached(["lanegate", "watch"], log_path)

    # File should still have the existing content (not been truncated)
    content = log_path.read_text()
    assert "existing content" in content


# ---------------------------------------------------------------------------
# resolve_reviewer tests
# ---------------------------------------------------------------------------


def test_resolve_reviewer_ticket_level_override():
    """ticket.reviewer takes precedence over everything."""
    ticket = {"reviewer": "aider"}
    cfg = {"reviewer": "openhands", "executor": "claude"}
    assert resolve_reviewer(ticket, cfg) == "aider"


def test_resolve_reviewer_cfg_level_when_no_ticket_override():
    """cfg.reviewer wins when the ticket has no reviewer field."""
    ticket = {}
    cfg = {"reviewer": "openhands", "executor": "claude"}
    assert resolve_reviewer(ticket, cfg) == "openhands"


def test_resolve_reviewer_falls_back_to_executor():
    """Falls back to cfg.executor when neither ticket.reviewer nor cfg.reviewer is set."""
    ticket = {}
    cfg = {"executor": "claude-process"}
    assert resolve_reviewer(ticket, cfg) == "claude-process"


def test_resolve_reviewer_default_when_nothing_set():
    """Returns 'claude' when no reviewer or executor is configured at all."""
    assert resolve_reviewer({}, {}) == "claude"


# ---------------------------------------------------------------------------
# TICK-043: no --no-verify in git commits
# ---------------------------------------------------------------------------


def test_commit_status_uses_dco_signoff_without_skipping_hooks(tmp_path):
    """_commit_status signs automated commits without bypassing hooks."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        result = MagicMock()
        result.returncode = 0
        return result

    ticket_path = tmp_path / "tickets" / "TICK-001.md"
    ticket_path.parent.mkdir(parents=True, exist_ok=True)
    ticket_path.write_text("---\nid: TICK-001\nstatus: open\n---\n")

    with patch("lanegate.lifecycle.subprocess.run", side_effect=fake_run):
        _commit_status(tmp_path, ticket_path, "TICK-001", "in_progress")

    for cmd in calls:
        assert "--no-verify" not in cmd, f"_commit_status used --no-verify in git call: {cmd}"
    assert "-s" in calls[0]




def test_resolve_reviewer_ticket_none_skips_to_cfg():
    """ticket.reviewer=None is treated as absent; cfg.reviewer is used."""
    ticket = {"reviewer": None}
    cfg = {"reviewer": "codex", "executor": "aider"}
    assert resolve_reviewer(ticket, cfg) == "codex"


def test_resolve_reviewer_empty_string_skips_to_cfg():
    """ticket.reviewer='' is treated as absent (falsy); cfg.reviewer is used."""
    ticket = {"reviewer": ""}
    cfg = {"reviewer": "human", "executor": "aider"}
    # Empty string is falsy, so cfg.reviewer wins
    assert resolve_reviewer(ticket, cfg) == "human"


# ---------------------------------------------------------------------------
# TICK-047: commit worktree claim AFTER setup, not before
# ---------------------------------------------------------------------------

from lanegate.lifecycle import cmd_start  # noqa: E402





































def test_hibernate_writes_notes_and_preserves_worktree(tmp_path):
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    wt = tmp_path / "worktrees" / "tick-120"
    wt.mkdir(parents=True)
    (tickets_dir / "TICK-120.md").write_text(
        "---\n"
        "id: TICK-120\n"
        "title: Test TICK-120\n"
        "status: in_progress\n"
        "touches:\n"
        "  - lanegate/lifecycle.py\n"
        f"worktree: {wt}\n"
        "branch: tick-120\n"
        "close_criteria: Notes are written.\n"
        "---\nBody text.\n"
    )

    def git_mock(args, **kwargs):
        if args[:2] == ["git", "log"]:
            return MagicMock(returncode=0, stdout="abc123 partial commit\n", stderr="")
        if args[:2] == ["git", "diff"]:
            return MagicMock(
                returncode=0,
                stdout="diff --git a/lanegate/lifecycle.py b/lanegate/lifecycle.py\n",
                stderr="",
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("lanegate.lifecycle.subprocess.run", side_effect=git_mock):
        cmd_hibernate("TICK-120", cfg, tmp_path, reason="usage limit")

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-120.md")
    assert ticket["status"] == "hibernated"
    assert ticket["worktree"] == str(wt)
    assert not (tmp_path / ".lanegate" / "notes" / "lanegate_lifecycle.py.md").exists()
    note = (tmp_path / ".lanegate" / "recovery" / "TICK-120.md").read_text()
    assert "Hibernated partial work" in note
    assert "Worktree:" in note
    assert "usage limit" in note


def test_stop_sigterms_live_executor_and_hibernates(tmp_path):
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_ticket(tickets_dir, "TICK-330", "in_progress")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    marker_base = tmp_path / ".lanegate" / "TICK-330"
    marker_base.parent.mkdir()
    marker_base.with_suffix(".pid").write_text(f"{proc.pid}\n")
    marker_base.with_suffix(".session").write_text("session\n")

    try:
        result = cmd_stop("TICK-330", cfg, tmp_path, grace_seconds=0.1)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-330.md")
    assert ticket["status"] == "hibernated"
    assert result["stopped"] is True
    assert not marker_base.with_suffix(".pid").exists()
    assert not marker_base.with_suffix(".session").exists()



def test_stop_reports_clean_result_when_pid_already_gone(tmp_path):
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_ticket(tickets_dir, "TICK-331", "in_progress")
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    marker_base = tmp_path / ".lanegate" / "TICK-331"
    marker_base.parent.mkdir()
    marker_base.with_suffix(".pid").write_text(f"{proc.pid}\n")

    result = cmd_stop("TICK-331", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    assert result["stopped"] is False
    assert result["reason"] == "already_gone"
    assert parse_ticket(tickets_dir / "TICK-331.md")["status"] == "in_progress"
    assert not marker_base.with_suffix(".pid").exists()


def test_stop_exits_nonzero_when_terminate_denied_for_live_process(tmp_path, monkeypatch):
    # terminate_pid() collapses ProcessLookupError/PermissionError/OSError into a
    # single False — cmd_stop must not treat a still-alive process it merely
    # failed to signal (e.g. permission denied) the same as one that already exited.
    import lanegate.lifecycle.hibernate as hibernate_mod

    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_ticket(tickets_dir, "TICK-334", "in_progress")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    marker_base = tmp_path / ".lanegate" / "TICK-334"
    marker_base.parent.mkdir()
    marker_base.with_suffix(".pid").write_text(f"{proc.pid}\n")

    monkeypatch.setattr(hibernate_mod, "terminate_pid", lambda pid: False)

    try:
        with pytest.raises(SystemExit) as exc_info:
            hibernate_mod.cmd_stop("TICK-334", cfg, tmp_path, grace_seconds=0.1)
        assert exc_info.value.code == 1
        assert marker_base.with_suffix(".pid").exists()
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_stop_no_pid_marker_returns_clean_result(tmp_path):
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_ticket(tickets_dir, "TICK-332", "in_progress")

    result = cmd_stop("TICK-332", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    assert result == {
        "ticket_id": "TICK-332",
        "stopped": False,
        "pid": None,
        "reason": "no_pid_marker",
    }
    assert parse_ticket(tickets_dir / "TICK-332.md")["status"] == "in_progress"


def test_stop_ignores_lock_and_watch_pid_files(tmp_path):
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_ticket(tickets_dir, "TICK-333", "in_progress")
    state_dir = tmp_path / ".lanegate"
    state_dir.mkdir()
    state_dir.joinpath("lock.pid").write_text(f"{os.getpid()}\n")
    state_dir.joinpath("watch.pid").write_text(f"{os.getpid()}\n")

    result = cmd_stop("TICK-333", cfg, tmp_path)

    assert result["reason"] == "no_pid_marker"
    assert os.getpid() == int(state_dir.joinpath("lock.pid").read_text())
    assert os.getpid() == int(state_dir.joinpath("watch.pid").read_text())














def test_touches_compliance_blocks_undeclared_files(tmp_path, capsys):
    """check_touches_compliance raises SystemExit when diff contains undeclared files."""
    ticket = {"id": "TICK-200", "touches": ["lanegate/lifecycle.py"]}
    mock_run = _make_git_diff_mock(
        committed_files=["lanegate/lifecycle.py", "lanegate/executor.py"],
    )
    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        with pytest.raises(SystemExit) as exc_info:
            check_touches_compliance("TICK-200", ticket, tmp_path)
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "lanegate/executor.py" in err
    assert "ERROR" in err


def test_touches_compliance_passes_when_only_declared_files(tmp_path):
    """check_touches_compliance does NOT raise when all changed files are declared."""
    ticket = {"id": "TICK-201", "touches": ["lanegate/lifecycle.py", "tests/test_lifecycle.py"]}
    mock_run = _make_git_diff_mock(
        committed_files=["lanegate/lifecycle.py", "tests/test_lifecycle.py"],
    )
    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        # Must not raise
        check_touches_compliance("TICK-201", ticket, tmp_path)


def test_touches_compliance_paired_test_file_not_declared_passes(tmp_path):
    """TICK-245: a committed test file paired with an already-declared module is
    not scope drift, even when the test file itself isn't in touches."""
    ticket = {"id": "TICK-201b", "touches": ["lanegate/lifecycle.py"]}
    mock_run = _make_git_diff_mock(
        committed_files=["lanegate/lifecycle.py", "tests/test_lifecycle.py"],
    )
    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        # Must not raise
        check_touches_compliance("TICK-201b", ticket, tmp_path)


def test_touches_compliance_notes_file_new_not_declared_passes(tmp_path):
    """A new file under .lanegate/notes/ is not scope drift, even though every
    implement prompt writes there without the ticket declaring it in touches."""
    ticket = {"id": "TICK-205", "touches": ["lanegate/lifecycle.py"]}
    mock_run = _make_git_diff_mock(
        committed_files=["lanegate/lifecycle.py", ".lanegate/notes/v2/lanegate_sexecutor.py.md"],
    )
    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        # Must not raise
        check_touches_compliance("TICK-205", ticket, tmp_path)


def test_touches_compliance_notes_global_not_declared_passes(tmp_path):
    """.lanegate/notes/global.md is not scope drift, same as any other notes file."""
    ticket = {"id": "TICK-206", "touches": ["lanegate/lifecycle.py"]}
    mock_run = _make_git_diff_mock(
        committed_files=["lanegate/lifecycle.py", ".lanegate/notes/global.md"],
    )
    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        # Must not raise
        check_touches_compliance("TICK-206", ticket, tmp_path)


def test_touches_compliance_notes_file_does_not_mask_other_undeclared_file(tmp_path, capsys):
    """A notes file is exempt, but a genuinely undeclared file alongside it still blocks."""
    ticket = {"id": "TICK-207", "touches": ["lanegate/lifecycle.py"]}
    mock_run = _make_git_diff_mock(
        committed_files=[
            "lanegate/lifecycle.py",
            ".lanegate/notes/global.md",
            "lanegate/executor.py",
        ],
    )
    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        with pytest.raises(SystemExit) as exc_info:
            check_touches_compliance("TICK-207", ticket, tmp_path)
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "lanegate/executor.py" in err
    assert ".lanegate/notes/global.md" not in err


def test_touches_compliance_wildcard_skips_check(tmp_path):
    """touches: ['*'] bypasses the drift check entirely — no error even with unexpected files."""
    ticket = {"id": "TICK-202", "touches": ["*"]}
    mock_run = _make_git_diff_mock(
        committed_files=["lanegate/lifecycle.py", "lanegate/executor.py", "some/random/file.py"],
    )
    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        # Must not raise
        check_touches_compliance("TICK-202", ticket, tmp_path)


def test_touches_compliance_allow_drift_warns_not_blocks(tmp_path, capsys):
    """--allow-drift emits a WARNING to stderr but does NOT raise SystemExit."""
    ticket = {"id": "TICK-203", "touches": ["lanegate/lifecycle.py"]}
    mock_run = _make_git_diff_mock(
        committed_files=["lanegate/lifecycle.py", "lanegate/executor.py"],
    )
    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        # Must not raise
        check_touches_compliance("TICK-203", ticket, tmp_path, allow_drift=True)
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "lanegate/executor.py" in err


def test_touches_compliance_no_changes_passes(tmp_path):
    """When the diff is empty, check_touches_compliance passes regardless of touches."""
    ticket = {"id": "TICK-204", "touches": ["lanegate/lifecycle.py"]}
    mock_run = _make_git_diff_mock(committed_files=[], uncommitted_files=[])
    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        check_touches_compliance("TICK-204", ticket, tmp_path)












# TICK-086: --auto-update-touches
































@pytest.mark.parametrize("command", [cmd_fail, cmd_reopen])
def test_lifecycle_rejects_path_like_ticket_ids_before_worktree_cleanup(tmp_path, command):
    """A forged ID can never escape the configured worktrees directory."""
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    victim = tmp_path / "victim"
    victim.mkdir()
    sentinel = victim / "keep.txt"
    sentinel.write_text("must survive")

    with pytest.raises(ValueError, match="invalid ticket ID"):
        command("../../victim", cfg, tmp_path)

    assert sentinel.read_text() == "must survive"





































# ---------------------------------------------------------------------------
# cmd_open: draft → open without re-running analysis
# ---------------------------------------------------------------------------


def _write_draft_ticket(tickets_dir: Path, ticket_id: str, touches: list | None = None) -> None:
    touches_yaml = ""
    if touches:
        touches_yaml = "touches:\n" + "".join(f"  - {t}\n" for t in touches)
    (tickets_dir / f"{ticket_id}.md").write_text(
        f"---\nid: {ticket_id}\ntitle: Test {ticket_id}\nstatus: draft\n{touches_yaml}---\nBody.\n"
    )


def test_cmd_open_transitions_draft_to_open(tmp_path):
    from lanegate.lifecycle import cmd_open
    from lanegate.ticket import parse_ticket

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    _write_draft_ticket(tickets_dir, "TICK-010", touches=["src/foo.py"])
    cmd_open("TICK-010", cfg, tmp_path)

    ticket = parse_ticket(tickets_dir / "TICK-010.md")
    assert ticket["status"] == "open"


def test_cmd_open_rejects_empty_touches(tmp_path):
    from lanegate.lifecycle import cmd_open

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    _write_draft_ticket(tickets_dir, "TICK-011", touches=[])
    with pytest.raises(SystemExit):
        cmd_open("TICK-011", cfg, tmp_path)


def test_cmd_open_rejects_non_draft(tmp_path):
    from lanegate.lifecycle import cmd_open

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    _write_ticket(tickets_dir, "TICK-012", "open")
    with pytest.raises(SystemExit):
        cmd_open("TICK-012", cfg, tmp_path)


def test_cmd_open_writes_status_changed_at(tmp_path):
    from lanegate.lifecycle import cmd_open
    from lanegate.ticket import parse_ticket

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    _write_draft_ticket(tickets_dir, "TICK-013", touches=["src/bar.py"])
    cmd_open("TICK-013", cfg, tmp_path)

    ticket = parse_ticket(tickets_dir / "TICK-013.md")
    assert _is_iso_utc(ticket.get("status_changed_at"))


# ---------------------------------------------------------------------------
# TICK-082: status_changed_at timestamp is written on every transition
# ---------------------------------------------------------------------------






def test_hibernate_writes_status_changed_at(tmp_path):
    """cmd_hibernate must write status_changed_at when transitioning in_progress → hibernated."""
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    wt = tmp_path / "worktrees" / "tick-201"
    wt.mkdir(parents=True)
    (tickets_dir / "TICK-201.md").write_text(
        "---\n"
        "id: TICK-201\n"
        "title: Test TICK-201\n"
        "status: in_progress\n"
        "touches:\n"
        "  - lanegate/lifecycle.py\n"
        f"worktree: {wt}\n"
        "branch: tick-201\n"
        "close_criteria: Hibernate test.\n"
        "---\nBody text.\n"
    )

    def git_mock(args, **kwargs):
        if args[:2] == ["git", "log"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        if args[:2] == ["git", "diff"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("lanegate.lifecycle.subprocess.run", side_effect=git_mock):
        cmd_hibernate("TICK-201", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-201.md")
    assert ticket["status"] == "hibernated"
    assert _is_iso_utc(ticket.get("status_changed_at")), (
        f"status_changed_at not set on hibernate: {ticket.get('status_changed_at')!r}"
    )












def test_missing_status_changed_at_shows_dash_on_board(tmp_path):
    """Tickets without status_changed_at must display '—' on the board (no crash)."""
    from lanegate.board import _time_in_status

    ticket_no_ts = {"id": "TICK-300", "status": "open", "title": "No timestamp"}
    assert _time_in_status(ticket_no_ts) == "—", "Expected '—' for ticket without status_changed_at"


def test_valid_status_changed_at_shows_age_on_board():
    """A ticket with a recent status_changed_at must return a non-dash age string."""
    import datetime as _dt

    from lanegate.board import _time_in_status

    # Set timestamp to 2 hours ago
    two_hours_ago = _dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=2)
    ticket = {
        "id": "TICK-301",
        "status": "in_progress",
        "title": "With timestamp",
        "status_changed_at": two_hours_ago.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    age = _time_in_status(ticket)
    assert age != "—", f"Expected a real age string, got {age!r}"
    assert "h" in age or "m" in age or "d" in age, f"Unexpected age format: {age!r}"


# ---------------------------------------------------------------------------
# Shared durable note preservation
# ---------------------------------------------------------------------------


def test_cleanup_stale_notes(tmp_path):
    """cmd_merge must preserve shared notes when resolving a ticket."""
    from lanegate.prompts import canonical_note_filename

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()

    notes_dir = tmp_path / ".lanegate" / "notes"
    notes_dir.mkdir(parents=True)

    ticket_note = notes_dir / "TICK-400.md"
    ticket_note.write_text("Per-ticket operational note.")
    curated_note = notes_dir / canonical_note_filename("lanegate/lifecycle.py")
    curated_note.parent.mkdir(parents=True, exist_ok=True)
    curated_note.write_text("Curated file note.")

    content = (
        "---\n"
        "id: TICK-400\n"
        "title: Test cleanup\n"
        "status: in_review\n"
        "branch: tick-400\n"
        "review_verdict: approved\n"
        "touches:\n"
        "  - lanegate/lifecycle.py\n"
        "---\nBody.\n"
    )
    (tickets_dir / "TICK-400.md").write_text(content)

    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    def mock_run(args, **kwargs):
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        cmd_merge("TICK-400", cfg, tmp_path)

    assert ticket_note.exists(), "shared ticket-id note must survive cmd_merge"
    assert curated_note.exists(), "per-file curated note must be preserved across merge"


# ---------------------------------------------------------------------------
# analyze session ID capture (TICK-188)
# ---------------------------------------------------------------------------


def test_analyze_captures_session_id(tmp_path):
    """cmd_analyze persists analyze_session_id to the ticket when model_fn returns a session tuple."""
    from lanegate.analyze import cmd_analyze
    from lanegate.ticket import parse_ticket

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    ticket_path = tickets_dir / "TICK-999.md"
    ticket_path.write_text(
        "---\n"
        "id: TICK-999\n"
        "title: Test session capture\n"
        "status: draft\n"
        "touches: []\n"
        "---\nTest ticket body.\n"
    )

    _cfg = {
        "ticket_prefix": "TICK",
        "tickets_dir": "tickets",
        "commit_status_changes": False,
    }
    session_id = "test-session-abc-123"
    model_response = '{"touches": ["lanegate/foo.py"], "close_criteria": "foo works", "depends_on": []}'

    def model_fn_with_session(prompt: str):
        return model_response, session_id

    cmd_analyze("TICK-999", _cfg, tmp_path, model_fn=model_fn_with_session)

    t = parse_ticket(ticket_path)
    assert t.get("analyze_session_id") == session_id, (
        f"expected analyze_session_id={session_id!r}, got {t.get('analyze_session_id')!r}"
    )


def test_analyze_no_session_when_model_fn_returns_str(tmp_path):
    """cmd_analyze does not set analyze_session_id when model_fn returns plain str."""
    from lanegate.analyze import cmd_analyze
    from lanegate.ticket import parse_ticket

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    ticket_path = tickets_dir / "TICK-998.md"
    ticket_path.write_text(
        "---\n"
        "id: TICK-998\n"
        "title: Test no session\n"
        "status: draft\n"
        "touches: []\n"
        "---\nTest ticket body.\n"
    )

    _cfg = {
        "ticket_prefix": "TICK",
        "tickets_dir": "tickets",
        "commit_status_changes": False,
    }
    model_response = '{"touches": ["lanegate/foo.py"], "close_criteria": "foo works", "depends_on": []}'

    cmd_analyze("TICK-998", _cfg, tmp_path, model_fn=lambda p: model_response)

    t = parse_ticket(ticket_path)
    assert "analyze_session_id" not in t or t.get("analyze_session_id") is None


def test_hibernate_reset_preserves_branch_if_diff_truncated(tmp_path):
    """When reset=True and diff is truncated (>30KB), the branch is preserved for recovery."""
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    wt = tmp_path / "worktrees" / "tick-150"
    wt.mkdir(parents=True)
    (tickets_dir / "TICK-150.md").write_text(
        "---\n"
        "id: TICK-150\n"
        "title: Test TICK-150\n"
        "status: in_progress\n"
        "touches:\n"
        "  - lanegate/lifecycle.py\n"
        f"worktree: {wt}\n"
        "branch: tick-150\n"
        "close_criteria: Diff is preserved.\n"
        "---\nBody text.\n"
    )

    # Create a large diff (40 KB) that will be truncated
    large_diff = "diff --git a/file.py b/file.py\n" + "x" * 35_000 + "\n"

    def git_mock(args, **kwargs):
        if args[:2] == ["git", "log"]:
            return MagicMock(returncode=0, stdout="abc123 partial commit\n", stderr="")
        if args[:2] == ["git", "diff"]:
            # Return a diff larger than _MAX_DIFF_BYTES (30_000)
            return MagicMock(returncode=0, stdout=large_diff, stderr="")
        if args[:2] == ["git", "branch"]:
            # Should NOT be called for branch deletion
            raise AssertionError("git branch -D should not be called when diff is truncated")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.lifecycle.subprocess.run", side_effect=git_mock),
        patch("lanegate.git.subprocess.run", side_effect=git_mock),
    ):
        cmd_hibernate("TICK-150", cfg, tmp_path, reset=True, reason="test")

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-150.md")
    assert ticket["status"] == "hibernated"
    # Branch should be preserved (not set to None)
    assert ticket["branch"] == "tick-150"
    # Worktree should be cleared
    assert ticket["worktree"] is None
    # Recovery note should show truncation
    note = (tmp_path / ".lanegate" / "recovery" / "TICK-150.md").read_text()
    assert "(diff truncated at 30 KB)" in note


def test_hibernate_no_escalation_bypass_signature_and_reset_deletes_branch(tmp_path):
    """Normal resets delete complete captures and expose no escalation bypass."""
    assert "escalation" not in inspect.signature(cmd_hibernate).parameters
    assert "escalation" not in inspect.signature(_hibernation_note).parameters
    assert "escalation" not in inspect.signature(_write_hibernation_notes).parameters

    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    wt = tmp_path / "worktrees" / "tick-151"
    wt.mkdir(parents=True)
    (tickets_dir / "TICK-151.md").write_text(
        "---\n"
        "id: TICK-151\n"
        "title: Test TICK-151\n"
        "status: in_progress\n"
        "touches:\n"
        "  - lanegate/lifecycle.py\n"
        f"worktree: {wt}\n"
        "branch: tick-151\n"
        "close_criteria: Diff is small.\n"
        "---\nBody text.\n"
    )

    # Create a small diff that will NOT be truncated
    small_diff = "diff --git a/file.py b/file.py\n+small change\n"
    branch_deleted = False

    def git_mock(args, **kwargs):
        nonlocal branch_deleted
        if args[:2] == ["git", "log"]:
            return MagicMock(returncode=0, stdout="abc123 partial commit\n", stderr="")
        if args[:2] == ["git", "diff"]:
            # Return a small diff
            return MagicMock(returncode=0, stdout=small_diff, stderr="")
        if args[:3] == ["git", "branch", "-D"]:
            # Branch deletion should be called
            branch_deleted = True
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.lifecycle.subprocess.run", side_effect=git_mock),
        patch("lanegate.git.subprocess.run", side_effect=git_mock),
    ):
        cmd_hibernate("TICK-151", cfg, tmp_path, reset=True, reason="test")

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-151.md")
    assert ticket["status"] == "hibernated"
    # Branch should be deleted
    assert ticket["branch"] is None
    # Worktree should be cleared
    assert ticket["worktree"] is None
    # Branch deletion should have been called
    assert branch_deleted, "git branch -D should have been called for small diff"
    # Recovery note should NOT show truncation
    note = (tmp_path / ".lanegate" / "recovery" / "TICK-151.md").read_text()
    assert "(diff truncated" not in note


def test_hibernate_reset_preserves_branch_when_diff_capture_fails(tmp_path):
    """A failed committed-diff capture cannot be treated as an empty diff during reset."""
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    wt = tmp_path / "worktrees" / "tick-152"
    wt.mkdir(parents=True)
    (tickets_dir / "TICK-152.md").write_text(
        "---\n"
        "id: TICK-152\n"
        "title: Test TICK-152\n"
        "status: in_progress\n"
        "touches:\n"
        "  - lanegate/lifecycle.py\n"
        f"worktree: {wt}\n"
        "branch: tick-152\n"
        "close_criteria: Capture failures preserve recovery.\n"
        "---\nBody text.\n"
    )

    def git_mock(args, **kwargs):
        if args[:2] == ["git", "log"]:
            return MagicMock(returncode=0, stdout="abc123 partial commit\n", stderr="")
        if args[:2] == ["git", "diff"]:
            return MagicMock(returncode=128, stdout="", stderr="")
        if args[:3] == ["git", "branch", "-D"]:
            raise AssertionError("git branch -D should not be called when diff capture fails")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.lifecycle.subprocess.run", side_effect=git_mock),
        patch("lanegate.git.subprocess.run", side_effect=git_mock),
    ):
        cmd_hibernate("TICK-152", cfg, tmp_path, reset=True, reason="test")

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-152.md")
    assert ticket["status"] == "hibernated"
    assert ticket["branch"] == "tick-152"
    assert ticket["worktree"] is None
    note = (tmp_path / ".lanegate" / "recovery" / "TICK-152.md").read_text()
    assert "git diff main...tick-152 failed (exit 128)" in note
    assert "Git capture warning" in note


# ---------------------------------------------------------------------------
# TICK-617: explicit destructive reset
# ---------------------------------------------------------------------------


def test_reset_terminal_status(tmp_path):
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    ticket_path = _write_ticket(tickets_dir, "TICK-617", "merged", branch="tick-617")

    with patch("lanegate.lifecycle.hibernate.subprocess.run") as run, pytest.raises(SystemExit):
        cmd_reset("TICK-617", cfg, tmp_path)

    assert parse_ticket(ticket_path)["status"] == "merged"
    run.assert_not_called()


def test_reset_mismatched_branch(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("init\n")
    _commit_all(tmp_path)
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    canonical_wt = Path(cfg["worktrees_dir"]) / "tick-618"
    subprocess.run(
        ["git", "worktree", "add", "-b", "foreign-branch", str(canonical_wt), "main"],
        cwd=tmp_path,
        check=True,
    )
    _write_ticket(
        tickets_dir,
        "TICK-618",
        "code_complete",
        worktree=str(canonical_wt),
        branch="foreign-branch",
        review_verdict="changes_requested",
    )

    with pytest.raises(RuntimeError, match="expected branch 'tick-618', found foreign-branch"):
        cmd_reset("TICK-618", cfg, tmp_path)

    assert canonical_wt.is_dir()


def test_reset_permission_error(tmp_path, capsys):
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    canonical_wt = Path(cfg["worktrees_dir"]) / "tick-619"
    canonical_wt.mkdir()
    ticket_path = _write_ticket(
        tickets_dir,
        "TICK-619",
        "code_complete",
        worktree=str(canonical_wt),
        branch="tick-619",
        review_verdict="changes_requested",
    )

    with patch(
        "lanegate.lifecycle.hibernate.remove_worktree",
        side_effect=PermissionError("protected branch"),
    ):
        cmd_reset("TICK-619", cfg, tmp_path)

    assert "WARNING: protected branch" in capsys.readouterr().err
    ticket = parse_ticket(ticket_path)
    assert ticket["status"] == "open"
    assert ticket["worktree"] is None
    assert ticket["branch"] is None
    assert "review_verdict" not in ticket


def test_reset_changes_requested_discards_canonical_worktree_and_branch(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("init\n")
    _commit_all(tmp_path)
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    canonical_wt = Path(cfg["worktrees_dir"]) / "tick-620"
    subprocess.run(
        ["git", "worktree", "add", "-b", "tick-620", str(canonical_wt), "main"],
        cwd=tmp_path,
        check=True,
    )
    ticket_path = _write_ticket(
        tickets_dir,
        "TICK-620",
        "code_complete",
        worktree=str(canonical_wt),
        branch="tick-620",
        review_verdict="changes_requested",
    )
    ticket = parse_ticket(ticket_path)
    ticket["review_summary"] = "discard this work"
    ticket["review_findings"] = ["unsafe history"]
    write_ticket(ticket)

    cmd_reset("TICK-620", cfg, tmp_path)

    ticket = parse_ticket(ticket_path)
    assert ticket["status"] == "open"
    assert ticket["worktree"] is None
    assert ticket["branch"] is None
    assert "review_verdict" not in ticket
    assert "review_summary" not in ticket
    assert "review_findings" not in ticket
    assert not canonical_wt.exists()
    assert subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", "refs/heads/tick-620"], cwd=tmp_path
    ).returncode != 0


def test_reset_no_worktree_preserves_branch(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("init\n")
    _commit_all(tmp_path)
    subprocess.run(["git", "branch", "tick-621"], cwd=tmp_path, check=True)
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    ticket_path = _write_ticket(
        tickets_dir,
        "TICK-621",
        "open",
        branch="tick-621",
    )

    cmd_reset("TICK-621", cfg, tmp_path)

    ticket = parse_ticket(ticket_path)
    assert ticket["status"] == "open"
    assert ticket["worktree"] is None
    assert ticket["branch"] is None
    # Branch tick-621 was not deleted because no canonical worktree existed to verify it
    assert subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", "refs/heads/tick-621"], cwd=tmp_path
    ).returncode == 0


# ---------------------------------------------------------------------------
# TICK-283: acceptance verification gate
# ---------------------------------------------------------------------------














# ---------------------------------------------------------------------------
# TICK-284: reconciliation -- cmd_supersede, cmd_reopen guard
# ---------------------------------------------------------------------------


def test_cmd_supersede_closes_ticket_when_branch_reachable_from_main(tmp_path):
    """A ticket whose branch is already merged into main gets closed with
    replacement_commit evidence recorded, not left to hold its touches
    lock forever."""
    _init_git_repo(tmp_path)
    subprocess.run(["git", "branch", "-m", "main"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "-b", "tick-500"], cwd=tmp_path, check=True)
    (tmp_path / "already_landed.txt").write_text("already landed\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "already landed"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "merge", "--no-ff", "tick-500", "-m", "merge tick-500"],
        cwd=tmp_path,
        check=True,
    )

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-500"
    wt.mkdir()

    _write_ticket(
        tickets_dir,
        "TICK-500",
        "in_progress",
        worktree=str(wt),
        branch="tick-500",
        touches=["already_landed.txt"],
    )
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    cmd_supersede("TICK-500", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-500.md")
    assert ticket["status"] == "closed"
    assert ticket.get("replacement_commit")
    assert ticket.get("worktree") is None


def test_cmd_close_records_completed_no_code_ticket(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-520", "open", touches=[".lanegate.yml"])
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    reason = "The recorded experiment met its close criteria; no default change is warranted."
    cmd_close("TICK-520", cfg, tmp_path, reason=reason)

    ticket = parse_ticket(tickets_dir / "TICK-520.md")
    assert ticket["status"] == "closed"
    assert reason in ticket["_body"]
    assert "closed as completed" in ticket["_body"]


def test_cmd_close_requires_reason_and_refuses_worktree(tmp_path, capsys):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-521", "open", touches=["x.py"])
    _write_ticket(
        tickets_dir, "TICK-522", "open", worktree=str(worktrees_dir / "tick-522"), touches=["x.py"]
    )
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    with pytest.raises(SystemExit):
        cmd_close("TICK-521", cfg, tmp_path)
    assert "--reason is required" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cmd_close("TICK-522", cfg, tmp_path, reason="Documented outcome")
    assert "has a worktree" in capsys.readouterr().err


def test_cmd_supersede_blocks_when_no_evidence(tmp_path, capsys):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()

    _write_ticket(tickets_dir, "TICK-501", "failed", touches=["lanegate/novel.py"])
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    with pytest.raises(SystemExit) as exc_info:
        cmd_supersede("TICK-501", cfg, tmp_path)

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "no reconciliation evidence" in err

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-501.md")
    assert ticket["status"] == "failed"


def test_cmd_supersede_closes_failed_ticket_with_manual_reason(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-504", "failed", touches=["lanegate/obsolete.py"])
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    reason = "This work was replaced by the consolidated implementation."
    cmd_supersede("TICK-504", cfg, tmp_path, reason=reason)

    ticket = parse_ticket(tickets_dir / "TICK-504.md")
    assert ticket["status"] == "closed"
    assert "failed → closed" in ticket["_body"]
    assert reason in ticket["_body"]


def test_cmd_supersede_closes_failed_ticket_with_reconciliation_evidence(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-505", "failed", touches=["lanegate/already_landed.py"])
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    with patch(
        "lanegate.reconciliation.reconcile_ticket",
        return_value={"replacement_commit": "a" * 40},
    ):
        cmd_supersede("TICK-505", cfg, tmp_path)

    ticket = parse_ticket(tickets_dir / "TICK-505.md")
    assert ticket["status"] == "closed"
    assert ticket["replacement_commit"] == "a" * 40
    assert "failed → closed" in ticket["_body"]


def test_cmd_supersede_refuses_non_failed_terminal_ticket(tmp_path, capsys):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-506", "done", touches=["lanegate/final.py"])
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    with pytest.raises(SystemExit) as exc_info:
        cmd_supersede("TICK-506", cfg, tmp_path, reason="Do not reclassify terminal work.")

    assert exc_info.value.code == 1
    assert "already 'done'" in capsys.readouterr().err
    assert parse_ticket(tickets_dir / "TICK-506.md")["status"] == "done"


def test_manual_supersede_retires_hibernated_ticket_and_cleans_up(tmp_path):
    """A human reason retires obsolete hibernated work without Git evidence."""
    _init_git_repo(tmp_path)
    subprocess.run(["git", "branch", "-m", "main"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("init\n")
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-510"
    _write_ticket(
        tickets_dir,
        "TICK-510",
        "hibernated",
        worktree=str(wt),
        branch="tick-510",
        touches=["lanegate/obsolete.py"],
    )
    _commit_all(tmp_path)
    subprocess.run(
        ["git", "worktree", "add", "-b", "tick-510", str(wt)], cwd=tmp_path, check=True
    )

    marker = tmp_path / ".lanegate" / "TICK-510.pid"
    marker.parent.mkdir()
    marker.write_text("12345\n")
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg.update(
        {
            "tickets_dir": str(tickets_dir),
            "worktrees_dir": str(worktrees_dir),
            "commit_status_changes": True,
        }
    )

    reason = "A newer architecture covers this goal without a literal duplicate."
    cmd_supersede("TICK-510", cfg, tmp_path, reason=reason)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-510.md")
    assert ticket["status"] == "closed"
    assert ticket.get("worktree") is None
    assert reason in ticket["_body"]
    assert not wt.exists()
    assert not marker.exists()
    log = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "TICK-510 status → superseded" in log


def test_cmd_reopen_dirty_diff_with_needs_review(tmp_path):
    from lanegate.lifecycle import cmd_reopen
    from lanegate.ticket import parse_ticket
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir(exist_ok=True)
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir(exist_ok=True)
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    
    _init_git_repo(tmp_path)
    subprocess.run(["git", "branch", "-m", "main"], cwd=tmp_path, check=True)
    (tmp_path / "base.txt").write_text("hello")
    _commit_all(tmp_path, "base")
    
    wt_path = tmp_path / cfg["worktrees_dir"] / "tick-001"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "add", "-b", "tick-001", str(wt_path)], cwd=tmp_path, check=True, capture_output=True)
    
    (wt_path / "foo.py").write_text("uncommitted changes")
    
    path = _write_ticket(tickets_dir, "TICK-001", "needs_review", worktree=str(wt_path), branch="tick-001")
    cmd_reopen("TICK-001", cfg, tmp_path)
    
    ticket = parse_ticket(path)
    assert ticket["status"] == "code_complete"
    assert "reopened via lanegate reopen" in ticket.get("_body", "")
    
    log = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=wt_path, capture_output=True, text=True).stdout
    assert "wip: uncommitted edits preserved" in log
    assert "Signed-off-by:" in log

def test_cmd_reopen_dirty_diff_with_failed(tmp_path):
    from lanegate.lifecycle import cmd_reopen
    from lanegate.ticket import parse_ticket
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir(exist_ok=True)
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir(exist_ok=True)
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    
    _init_git_repo(tmp_path)
    subprocess.run(["git", "branch", "-m", "main"], cwd=tmp_path, check=True)
    (tmp_path / "base.txt").write_text("hello")
    _commit_all(tmp_path, "base")
    
    wt_path = tmp_path / cfg["worktrees_dir"] / "tick-001"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "add", "-b", "tick-001", str(wt_path)], cwd=tmp_path, check=True, capture_output=True)
    
    (wt_path / "foo.py").write_text("uncommitted changes")
    
    path = _write_ticket(tickets_dir, "TICK-001", "failed", worktree=str(wt_path), branch="tick-001")
    cmd_reopen("TICK-001", cfg, tmp_path)
    
    ticket = parse_ticket(path)
    assert ticket["status"] == "code_complete"
    
    log = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=wt_path, capture_output=True, text=True).stdout
    assert "wip: uncommitted edits preserved" in log
    assert "Signed-off-by:" in log

def test_cmd_reopen_dirty_diff_with_code_complete(tmp_path, capsys):
    from lanegate.lifecycle import cmd_reopen
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir(exist_ok=True)
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir(exist_ok=True)
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    
    _init_git_repo(tmp_path)
    subprocess.run(["git", "branch", "-m", "main"], cwd=tmp_path, check=True)
    (tmp_path / "base.txt").write_text("hello")
    _commit_all(tmp_path, "base")
    
    wt_path = tmp_path / cfg["worktrees_dir"] / "tick-001"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "add", "-b", "tick-001", str(wt_path)], cwd=tmp_path, check=True, capture_output=True)
    
    (wt_path / "foo.py").write_text("uncommitted changes")
    
    _write_ticket(tickets_dir, "TICK-001", "code_complete", worktree=str(wt_path), branch="tick-001")
    
    import pytest
    with pytest.raises(SystemExit) as exc:
        cmd_reopen("TICK-001", cfg, tmp_path)
    
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "ERROR: TICK-001 is code_complete with real commits ahead of main" in err

def test_cmd_reopen_dirty_diff_with_hibernated(tmp_path, capsys):
    from lanegate.lifecycle import cmd_reopen
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir(exist_ok=True)
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir(exist_ok=True)
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    
    _init_git_repo(tmp_path)
    subprocess.run(["git", "branch", "-m", "main"], cwd=tmp_path, check=True)
    (tmp_path / "base.txt").write_text("hello")
    _commit_all(tmp_path, "base")
    
    wt_path = tmp_path / cfg["worktrees_dir"] / "tick-001"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "add", "-b", "tick-001", str(wt_path)], cwd=tmp_path, check=True, capture_output=True)
    
    (wt_path / "foo.py").write_text("uncommitted changes")
    
    _write_ticket(tickets_dir, "TICK-001", "hibernated", worktree=str(wt_path), branch="tick-001")
    
    with pytest.raises(SystemExit) as exc:
        cmd_reopen("TICK-001", cfg, tmp_path)
    
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "ERROR: TICK-001 is hibernated with real commits ahead of main" in err

def test_cmd_reopen_checkpoint_failure_exits_cleanly(tmp_path, capsys):
    from lanegate.lifecycle import cmd_reopen
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir(exist_ok=True)
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir(exist_ok=True)
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    
    _init_git_repo(tmp_path)
    subprocess.run(["git", "branch", "-m", "main"], cwd=tmp_path, check=True)
    (tmp_path / "base.txt").write_text("hello")
    _commit_all(tmp_path, "base")
    
    wt_path = tmp_path / cfg["worktrees_dir"] / "tick-001"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "add", "-b", "tick-001", str(wt_path)], cwd=tmp_path, check=True, capture_output=True)
    
    (wt_path / "foo.py").write_text("uncommitted changes")
    (tmp_path / ".git" / "worktrees" / "tick-001" / "index.lock").write_text("")
    
    _write_ticket(tickets_dir, "TICK-001", "failed", worktree=str(wt_path), branch="tick-001")
    
    with pytest.raises(SystemExit) as exc:
        cmd_reopen("TICK-001", cfg, tmp_path)
    
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "ERROR: TICK-001: failed to checkpoint worktree" in err

