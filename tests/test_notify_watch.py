"""Tests for lanegate/notify_watch.py — background stuck-orchestrate push-notification daemon."""

from __future__ import annotations

import os
import signal
from pathlib import Path
from unittest.mock import patch

import pytest

from lanegate.notify_watch import (
    _detect_problem,
    _notify_watch_pid_file,
    _read_last_signature,
    _read_pid,
    _stuck_tickets,
    _write_last_signature,
    cmd_notify_watch,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_cfg(tmp_path: Path, **overrides) -> dict:
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir(exist_ok=True)
    cfg = {
        "ticket_prefix": "TICK",
        "tickets_dir": str(tickets_dir),
        "notify": {
            "ntfy_topic": "test-topic",
            "poll_seconds": 60,
            "heartbeat_stale_seconds": 180,
        },
    }
    cfg.update(overrides)
    return cfg


def _write_ticket(tickets_dir: Path, ticket_id: str, status: str, body: str = "Body.\n") -> None:
    content = f"---\nid: {ticket_id}\ntitle: Test {ticket_id}\nstatus: {status}\n---\n{body}"
    (tickets_dir / f"{ticket_id}.md").write_text(content)


# ---------------------------------------------------------------------------
# Path helpers / _read_pid
# ---------------------------------------------------------------------------


def test_notify_watch_pid_file_returns_correct_path(tmp_path):
    pid_file = _notify_watch_pid_file(tmp_path)
    assert pid_file.name == "notify-watch.pid"
    assert pid_file.parent.name == ".lanegate"


def test_read_pid_returns_none_when_no_file(tmp_path):
    assert _read_pid(tmp_path / "notify-watch.pid") is None


def test_read_pid_returns_none_for_dead_process(tmp_path):
    pid_path = tmp_path / "notify-watch.pid"
    pid_path.write_text("999999\n")
    with patch("lanegate.watch_common.pid_alive", return_value=False):
        assert _read_pid(pid_path) is None


def test_read_pid_returns_live_pid(tmp_path):
    pid_path = tmp_path / "notify-watch.pid"
    my_pid = os.getpid()
    pid_path.write_text(f"{my_pid}\n")
    assert _read_pid(pid_path) == my_pid


# ---------------------------------------------------------------------------
# cmd_notify_watch --status / --stop
# ---------------------------------------------------------------------------


def test_status_when_no_pid_file(tmp_path, capsys):
    cmd_notify_watch(_default_cfg(tmp_path), tmp_path, status=True)
    assert "not running" in capsys.readouterr().out


def test_status_with_live_pid(tmp_path, capsys):
    pid_path = _notify_watch_pid_file(tmp_path)
    my_pid = os.getpid()
    pid_path.write_text(f"{my_pid}\n")

    cmd_notify_watch(_default_cfg(tmp_path), tmp_path, status=True)
    out = capsys.readouterr().out
    assert str(my_pid) in out
    assert "running" in out


def test_stop_with_live_pid_kills_and_removes_file(tmp_path):
    pid_path = _notify_watch_pid_file(tmp_path)
    my_pid = os.getpid()
    pid_path.write_text(f"{my_pid}\n")

    killed = []
    with patch("lanegate.notify_watch.os.kill", side_effect=lambda pid, sig: killed.append((pid, sig))):
        cmd_notify_watch(_default_cfg(tmp_path), tmp_path, stop=True)

    assert (my_pid, signal.SIGTERM) in killed
    assert not pid_path.exists()


def test_already_running_exits_nonzero(tmp_path):
    pid_path = _notify_watch_pid_file(tmp_path)
    pid_path.write_text(f"{os.getpid()}\n")

    with pytest.raises(SystemExit) as exc_info:
        cmd_notify_watch(_default_cfg(tmp_path), tmp_path)
    assert exc_info.value.code == 1
    assert pid_path.exists()


def test_background_spawns_detached_and_returns_without_running_loop(tmp_path, capsys):
    with (
        patch("lanegate.lifecycle.spawn_detached", return_value=4242) as mock_spawn,
        patch("lanegate.notify_watch._run_loop") as mock_loop,
    ):
        cmd_notify_watch(_default_cfg(tmp_path), tmp_path, background=True)

    mock_spawn.assert_called_once()
    args, _kwargs = mock_spawn.call_args
    assert args[0] == ["lanegate", "notify-watch"]
    mock_loop.assert_not_called()
    assert "4242" in capsys.readouterr().out


def test_background_exits_nonzero_when_already_running(tmp_path):
    pid_path = _notify_watch_pid_file(tmp_path)
    pid_path.write_text(f"{os.getpid()}\n")

    with pytest.raises(SystemExit) as exc_info:
        cmd_notify_watch(_default_cfg(tmp_path), tmp_path, background=True)
    assert exc_info.value.code == 1


def test_stale_pid_file_cleaned_up_on_start(tmp_path):
    pid_path = _notify_watch_pid_file(tmp_path)
    pid_path.write_text("999999\n")

    with (
        patch("lanegate.watch_common.pid_alive", return_value=False),
        patch("lanegate.notify_watch._run_loop") as mock_loop,
    ):
        cmd_notify_watch(_default_cfg(tmp_path), tmp_path)

    mock_loop.assert_called_once()
    assert not pid_path.exists()


def test_test_flag_without_topic_exits_nonzero(tmp_path):
    cfg = _default_cfg(tmp_path, notify={"ntfy_topic": None})
    with pytest.raises(SystemExit) as exc_info:
        cmd_notify_watch(cfg, tmp_path, test=True)
    assert exc_info.value.code == 1


def test_test_flag_with_topic_sends_push(tmp_path):
    cfg = _default_cfg(tmp_path)
    with patch("lanegate.notify_watch.send_ntfy", return_value=True) as mock_send:
        with pytest.raises(SystemExit) as exc_info:
            cmd_notify_watch(cfg, tmp_path, test=True)
    assert exc_info.value.code == 0
    mock_send.assert_called_once()
    assert mock_send.call_args[0][0] == "test-topic"


# ---------------------------------------------------------------------------
# _stuck_tickets
# ---------------------------------------------------------------------------


def test_stuck_tickets_includes_needs_review_blocked_failed(tmp_path):
    cfg = _default_cfg(tmp_path)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_ticket(tickets_dir, "TICK-001", "needs_review")
    _write_ticket(tickets_dir, "TICK-002", "blocked")
    _write_ticket(tickets_dir, "TICK-003", "failed")
    _write_ticket(tickets_dir, "TICK-004", "open")

    result = {t["id"] for t in _stuck_tickets(cfg, tmp_path)}
    assert result == {"TICK-001", "TICK-002", "TICK-003"}


def test_stuck_tickets_includes_hibernated_non_rate_limit(tmp_path):
    cfg = _default_cfg(tmp_path)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_ticket(
        tickets_dir,
        "TICK-005",
        "hibernated",
        body="Body.\n\n## Hibernation Reason\n\ncombined-mode executor exited 0 but status did not advance\n",
    )
    result = {t["id"] for t in _stuck_tickets(cfg, tmp_path)}
    assert result == {"TICK-005"}


def test_stuck_tickets_excludes_hibernated_rate_limit_when_resume_watch_alive(tmp_path):
    cfg = _default_cfg(tmp_path)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_ticket(
        tickets_dir,
        "TICK-006",
        "hibernated",
        body="Body.\n\n## Hibernation Reason\n\nrate limit or quota interruption (429)\n",
    )
    with patch("lanegate.notify_watch._rate_limit_watcher_alive", return_value=True):
        assert _stuck_tickets(cfg, tmp_path) == []


def test_stuck_tickets_includes_hibernated_rate_limit_when_resume_watch_not_alive(tmp_path):
    """A rate-limited hibernation with no live resume-watch (crashed, gave up,
    or never started because on_rate_limit=halt) is just as stuck as any other
    halt and must still be flagged."""
    cfg = _default_cfg(tmp_path)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_ticket(
        tickets_dir,
        "TICK-006",
        "hibernated",
        body="Body.\n\n## Hibernation Reason\n\nrate limit or quota interruption (429)\n",
    )
    with patch("lanegate.notify_watch._rate_limit_watcher_alive", return_value=False):
        result = {t["id"] for t in _stuck_tickets(cfg, tmp_path)}
    assert result == {"TICK-006"}


# ---------------------------------------------------------------------------
# _detect_problem
# ---------------------------------------------------------------------------


def test_detect_problem_none_when_active_and_heartbeat_fresh(tmp_path):
    cfg = _default_cfg(tmp_path)
    active = {
        "active": True,
        "ticket_id": "TICK-050",
        "step": "implement",
        "last_heartbeat_at": __import__("time").time(),
    }
    with (
        patch("lanegate.notify_watch.orchestrator_lock_status", return_value={"held": True, "alive": True}),
        patch("lanegate.orchestrate._read_active_status", return_value=active),
    ):
        assert _detect_problem(cfg, tmp_path) is None


def test_detect_problem_flags_stale_heartbeat(tmp_path):
    cfg = _default_cfg(tmp_path)
    active = {
        "active": True,
        "ticket_id": "TICK-050",
        "step": "implement",
        "last_heartbeat_at": __import__("time").time() - 999,
    }
    with (
        patch("lanegate.notify_watch.orchestrator_lock_status", return_value={"held": True, "alive": True}),
        patch("lanegate.orchestrate._read_active_status", return_value=active),
    ):
        problem = _detect_problem(cfg, tmp_path)
    assert problem is not None
    signature, message = problem
    assert signature == "heartbeat-stale:TICK-050"
    assert "TICK-050" in message


def test_detect_problem_flags_process_died(tmp_path):
    cfg = _default_cfg(tmp_path)
    active = {"active": True, "ticket_id": "TICK-050", "step": "implement", "last_heartbeat_at": None}
    with (
        patch("lanegate.notify_watch.orchestrator_lock_status", return_value={"held": False, "alive": False}),
        patch("lanegate.orchestrate._read_active_status", return_value=active),
    ):
        problem = _detect_problem(cfg, tmp_path)
    assert problem is not None
    assert problem[0] == "process-died:TICK-050"


def test_detect_problem_flags_pooled_executor_stale_heartbeat(tmp_path):
    cfg = _default_cfg(tmp_path)
    session = {
        "active": True,
        "ticket_id": "TICK-060",
        "step": "implement",
        "last_heartbeat_at": __import__("time").time() - 999,
    }
    with (
        patch("lanegate.notify_watch.orchestrator_lock_status", return_value={"held": True, "alive": True}),
        patch("lanegate.orchestrate._read_all_active_statuses", return_value=[session]),
        patch("lanegate.orchestrate._read_active_status", return_value=None),
    ):
        problem = _detect_problem(cfg, tmp_path)
    assert problem is not None
    signature, message = problem
    assert signature == "heartbeat-stale:TICK-060"
    assert "TICK-060" in message


def test_detect_problem_flags_pooled_executor_process_died(tmp_path):
    cfg = _default_cfg(tmp_path)
    session = {"active": True, "ticket_id": "TICK-061", "step": "implement", "last_heartbeat_at": None}
    with (
        patch("lanegate.notify_watch.orchestrator_lock_status", return_value={"held": False, "alive": False}),
        patch("lanegate.orchestrate._read_all_active_statuses", return_value=[session]),
        patch("lanegate.orchestrate._read_active_status", return_value=None),
    ):
        problem = _detect_problem(cfg, tmp_path)
    assert problem is not None
    assert problem[0] == "process-died:TICK-061"


def test_detect_problem_flags_halted_with_stuck_tickets(tmp_path):
    cfg = _default_cfg(tmp_path)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_ticket(tickets_dir, "TICK-001", "needs_review")

    with (
        patch("lanegate.notify_watch.orchestrator_lock_status", return_value={"held": False, "alive": False}),
        patch("lanegate.orchestrate._read_active_status", return_value=None),
    ):
        problem = _detect_problem(cfg, tmp_path)
    assert problem is not None
    assert "TICK-001" in problem[1]


def test_detect_problem_none_when_idle_and_board_clean(tmp_path):
    cfg = _default_cfg(tmp_path)
    with (
        patch("lanegate.notify_watch.orchestrator_lock_status", return_value={"held": False, "alive": False}),
        patch("lanegate.orchestrate._read_active_status", return_value=None),
    ):
        assert _detect_problem(cfg, tmp_path) is None


# ---------------------------------------------------------------------------
# signature state file
# ---------------------------------------------------------------------------


def test_signature_round_trips_through_state_file(tmp_path):
    state_path = tmp_path / "notify-watch-state.json"
    assert _read_last_signature(state_path) == ""
    _write_last_signature(state_path, "heartbeat-stale:TICK-050")
    assert _read_last_signature(state_path) == "heartbeat-stale:TICK-050"
