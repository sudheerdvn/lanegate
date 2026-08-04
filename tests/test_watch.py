"""Tests for lanegate/watch.py — background PR-approval polling daemon."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lanegate.watch import (
    _read_pid,
    _run_loop,
    _watch_log_file,
    _watch_pid_file,
    cmd_watch,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_cfg(tmp_path: Path) -> dict:
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir(exist_ok=True)
    return {
        "ticket_prefix": "TICK",
        "tickets_dir": str(tickets_dir),
        "worktrees_dir": str(tmp_path / "worktrees"),
    }


def _write_ticket(
    tickets_dir: Path,
    ticket_id: str,
    status: str,
    pr_number: int | None = None,
) -> None:
    content = f"---\nid: {ticket_id}\ntitle: Test {ticket_id}\nstatus: {status}\n"
    if pr_number is not None:
        content += f"pr_number: {pr_number}\n"
    content += "---\nBody.\n"
    (tickets_dir / f"{ticket_id}.md").write_text(content)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def test_watch_pid_file_returns_correct_path(tmp_path):
    pid_file = _watch_pid_file(tmp_path)
    assert pid_file.name == "watch.pid"
    assert pid_file.parent.name == ".lanegate"


def test_watch_log_file_returns_correct_path(tmp_path):
    log_file = _watch_log_file(tmp_path)
    assert log_file.name == "watch.log"
    assert log_file.parent.name == ".lanegate"


# ---------------------------------------------------------------------------
# _read_pid
# ---------------------------------------------------------------------------


def test_read_pid_returns_none_when_no_file(tmp_path):
    pid_path = tmp_path / "watch.pid"
    assert _read_pid(pid_path) is None


def test_read_pid_returns_none_for_dead_process(tmp_path):
    pid_path = tmp_path / "watch.pid"
    pid_path.write_text("999999\n")
    with patch("lanegate.watch.os.kill", side_effect=ProcessLookupError):
        result = _read_pid(pid_path)
    assert result is None


def test_read_pid_returns_live_pid(tmp_path):
    pid_path = tmp_path / "watch.pid"
    my_pid = os.getpid()
    pid_path.write_text(f"{my_pid}\n")
    assert _read_pid(pid_path) == my_pid


def test_read_pid_returns_none_for_garbage(tmp_path):
    pid_path = tmp_path / "watch.pid"
    pid_path.write_text("not-a-number\n")
    assert _read_pid(pid_path) is None


# ---------------------------------------------------------------------------
# cmd_watch --status
# ---------------------------------------------------------------------------


def test_status_when_no_pid_file(tmp_path, capsys):
    cfg = _default_cfg(tmp_path)
    cmd_watch(cfg, tmp_path, status=True)
    out = capsys.readouterr().out
    assert "not running" in out


def test_status_with_live_pid(tmp_path, capsys):
    cfg = _default_cfg(tmp_path)
    pid_path = _watch_pid_file(tmp_path)
    my_pid = os.getpid()
    pid_path.write_text(f"{my_pid}\n")

    cmd_watch(cfg, tmp_path, status=True)
    out = capsys.readouterr().out
    assert str(my_pid) in out
    assert "running" in out


def test_status_with_stale_pid_file(tmp_path, capsys):
    """A stale PID file (dead process) reads as not running."""
    cfg = _default_cfg(tmp_path)
    pid_path = _watch_pid_file(tmp_path)
    pid_path.write_text("999999\n")

    with patch("lanegate.watch.os.kill", side_effect=ProcessLookupError):
        cmd_watch(cfg, tmp_path, status=True)
    out = capsys.readouterr().out
    assert "not running" in out


# ---------------------------------------------------------------------------
# cmd_watch --stop
# ---------------------------------------------------------------------------


def test_stop_when_no_pid_file(tmp_path, capsys):
    cfg = _default_cfg(tmp_path)
    # Should be a no-op, not raise
    cmd_watch(cfg, tmp_path, stop=True)
    out = capsys.readouterr().out
    assert "nothing to stop" in out or "not running" in out


def test_stop_with_live_pid_kills_and_removes_file(tmp_path, capsys):
    cfg = _default_cfg(tmp_path)
    pid_path = _watch_pid_file(tmp_path)
    my_pid = os.getpid()
    pid_path.write_text(f"{my_pid}\n")

    killed = []

    def mock_kill(pid, sig):
        killed.append((pid, sig))

    with patch("lanegate.watch.os.kill", side_effect=mock_kill):
        cmd_watch(cfg, tmp_path, stop=True)

    assert (my_pid, signal.SIGTERM) in killed
    assert not pid_path.exists()

    out = capsys.readouterr().out
    assert "SIGTERM" in out or str(my_pid) in out


def test_stop_cleans_stale_pid_file(tmp_path, capsys):
    """--stop with a stale PID file removes it without error."""
    cfg = _default_cfg(tmp_path)
    pid_path = _watch_pid_file(tmp_path)
    pid_path.write_text("999999\n")

    with patch("lanegate.watch.os.kill", side_effect=ProcessLookupError):
        cmd_watch(cfg, tmp_path, stop=True)
    assert not pid_path.exists()


# ---------------------------------------------------------------------------
# Stale PID file cleanup on start
# ---------------------------------------------------------------------------


def test_stale_pid_file_cleaned_up_on_start(tmp_path):
    """
    If a stale PID file exists when we try to start, it is silently cleaned up
    and the loop proceeds (exits immediately since no in_review tickets).
    """
    cfg = _default_cfg(tmp_path)
    pid_path = _watch_pid_file(tmp_path)
    pid_path.write_text("999999\n")  # stale

    # _run_loop will exit immediately (no in_review tickets), and the pid file
    # is removed in the finally block.
    with patch("lanegate.watch.os.kill", side_effect=ProcessLookupError), \
         patch("lanegate.watch._run_loop") as mock_loop:
        cmd_watch(cfg, tmp_path)

    mock_loop.assert_called_once()
    assert not pid_path.exists()


def test_already_running_exits_nonzero(tmp_path):
    """If a live PID file exists when we try to start, exit with code 1."""
    cfg = _default_cfg(tmp_path)
    pid_path = _watch_pid_file(tmp_path)
    my_pid = os.getpid()
    pid_path.write_text(f"{my_pid}\n")

    with pytest.raises(SystemExit) as exc_info:
        cmd_watch(cfg, tmp_path)
    assert exc_info.value.code == 1
    # The pid file must NOT be deleted (it belongs to the "existing" watcher)
    assert pid_path.exists()


# ---------------------------------------------------------------------------
# _run_loop — poll logic
# ---------------------------------------------------------------------------


def test_run_loop_exits_when_no_in_review_tickets(tmp_path):
    """Loop exits immediately when there are no in_review tickets with pr_number."""
    cfg = _default_cfg(tmp_path)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_ticket(tickets_dir, "TICK-001", "open")

    called = []

    def no_sleep(seconds):
        called.append(seconds)

    with (
        patch("lanegate.watch.time.sleep", side_effect=no_sleep),
        patch("lanegate.watch._write_log"),
    ):
        _run_loop(cfg, tmp_path)

    # sleep should never be called — loop exits after first check
    assert called == []


def test_run_loop_exits_when_in_review_ticket_has_no_pr_number(tmp_path):
    """in_review tickets without pr_number are ignored → loop exits."""
    cfg = _default_cfg(tmp_path)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_ticket(tickets_dir, "TICK-001", "in_review")  # no pr_number

    with (
        patch("lanegate.watch.time.sleep") as mock_sleep,
        patch("lanegate.watch._write_log"),
    ):
        _run_loop(cfg, tmp_path)

    mock_sleep.assert_not_called()


def _mock_gh_decision(decision: str | None):
    """Return a mock subprocess.run result for gh pr view."""
    payload = json.dumps({"reviewDecision": decision})
    return MagicMock(returncode=0, stdout=payload, stderr="")


def test_run_loop_approved_calls_lanegate_merge(tmp_path):
    """When PR is APPROVED, lanegate merge <ticket_id> is called."""
    cfg = _default_cfg(tmp_path)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_ticket(tickets_dir, "TICK-001", "in_review", pr_number=42)

    merge_calls = []

    def mock_run(args, **kwargs):
        if "gh" in args:
            return _mock_gh_decision("APPROVED")
        if "lanegate" in args and "merge" in args:
            merge_calls.append(list(args))
            # Simulate the ticket being moved to merged so the loop exits
            _write_ticket(tickets_dir, "TICK-001", "merged")
            return MagicMock(returncode=0)
        return MagicMock(returncode=0)

    # After the merge the loop sleeps once, then reloads tickets and sees no
    # eligible in_review tickets → exits. So one sleep call is expected.
    sleep_count = [0]

    def mock_sleep(seconds):
        sleep_count[0] += 1

    with (
        patch("lanegate.watch.subprocess.run", side_effect=mock_run),
        patch("lanegate.watch.time.sleep", side_effect=mock_sleep),
        patch("lanegate.watch._write_log"),
    ):
        _run_loop(cfg, tmp_path)

    assert any("TICK-001" in " ".join(c) for c in merge_calls), (
        f"lanegate merge TICK-001 was not called; calls: {merge_calls}"
    )
    # Exactly one sleep between the first and second iteration
    assert sleep_count[0] == 1


def test_run_loop_changes_requested_does_not_merge(tmp_path):
    """When CHANGES_REQUESTED, lanegate merge is NOT called; loop exits when no remaining eligible tickets."""
    cfg = _default_cfg(tmp_path)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_ticket(tickets_dir, "TICK-001", "in_review", pr_number=42)

    merge_calls = []
    sleep_count = [0]

    def mock_run(args, **kwargs):
        if "gh" in args:
            return _mock_gh_decision("CHANGES_REQUESTED")
        if "lanegate" in args and "merge" in args:
            merge_calls.append(list(args))
        return MagicMock(returncode=0)

    def mock_sleep(seconds):
        sleep_count[0] += 1
        if sleep_count[0] >= 2:
            # Force exit by removing the ticket
            _write_ticket(tickets_dir, "TICK-001", "code_complete")

    with (
        patch("lanegate.watch.subprocess.run", side_effect=mock_run),
        patch("lanegate.watch.time.sleep", side_effect=mock_sleep),
        patch("lanegate.watch._write_log"),
    ):
        _run_loop(cfg, tmp_path)

    assert merge_calls == [], f"merge was unexpectedly called: {merge_calls}"


def test_run_loop_logs_to_file(tmp_path):
    """The loop appends messages to .lanegate/watch.log."""
    cfg = _default_cfg(tmp_path)
    # No tickets → loop exits immediately after one log line.
    _run_loop(cfg, tmp_path)

    log_file = _watch_log_file(tmp_path)
    assert log_file.exists()
    content = log_file.read_text()
    assert "[watch]" in content


def test_run_loop_handles_gh_failure_gracefully(tmp_path):
    """If gh pr view fails, log and continue — do not crash."""
    cfg = _default_cfg(tmp_path)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_ticket(tickets_dir, "TICK-001", "in_review", pr_number=42)

    sleep_count = [0]

    def mock_run(args, **kwargs):
        if "gh" in args:
            return MagicMock(returncode=1, stdout="", stderr="gh: command failed")
        return MagicMock(returncode=0)

    def mock_sleep(seconds):
        sleep_count[0] += 1
        # After one sleep, remove the ticket so the loop exits
        _write_ticket(tickets_dir, "TICK-001", "merged")

    with (
        patch("lanegate.watch.subprocess.run", side_effect=mock_run),
        patch("lanegate.watch.time.sleep", side_effect=mock_sleep),
        patch("lanegate.watch._write_log"),
    ):
        _run_loop(cfg, tmp_path)  # must not raise

    assert sleep_count[0] == 1
