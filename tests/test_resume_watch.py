"""Tests for lanegate/resume_watch.py — background rate-limit auto-resume daemon."""

from __future__ import annotations

import os
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zoneinfo import ZoneInfo

import lanegate.orchestrate.loop as loop
import lanegate.resume_watch as resume_watch
from lanegate.resume_watch import (
    _hibernated_for_rate_limit,
    _orchestrate_args_file,
    _parse_reset_time,
    _read_orchestrate_args,
    _read_pid,
    _reset_wait_seconds,
    _resume_watch_history_file,
    _resume_watch_log_file,
    _resume_watch_pid_file,
    _run_loop,
    cmd_resume_watch,
    get_daemon_status,
    read_history,
    read_history_since,
    store_orchestrate_args,
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
        "rate_limit_resume": {
            "initial_backoff_s": 300,
            "max_backoff_s": 7200,
            "ceiling_s": None,
        },
    }
    cfg.update(overrides)
    return cfg


def _write_hibernated_ticket(tickets_dir: Path, ticket_id: str, hibernation_reason: str) -> None:
    content = (
        f"---\n"
        f"id: {ticket_id}\n"
        f"title: Test {ticket_id}\n"
        f"status: hibernated\n"
        f"---\n"
        f"Body.\n\n"
        f"## Hibernation Reason\n\n"
        f"{hibernation_reason}\n"
    )
    (tickets_dir / f"{ticket_id}.md").write_text(content)


def _write_open_ticket(tickets_dir: Path, ticket_id: str) -> None:
    content = f"---\nid: {ticket_id}\ntitle: Test {ticket_id}\nstatus: open\n---\nBody.\n"
    (tickets_dir / f"{ticket_id}.md").write_text(content)


def _write_inprogress_ticket(tickets_dir: Path, ticket_id: str) -> None:
    content = f"---\nid: {ticket_id}\ntitle: Test {ticket_id}\nstatus: in_progress\n---\nBody.\n"
    (tickets_dir / f"{ticket_id}.md").write_text(content)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def test_resume_watch_pid_file_returns_correct_path(tmp_path):
    pid_file = _resume_watch_pid_file(tmp_path)
    assert pid_file.name == "resume-watch.pid"
    assert pid_file.parent.name == ".lanegate"


def test_resume_watch_log_file_returns_correct_path(tmp_path):
    log_file = _resume_watch_log_file(tmp_path)
    assert log_file.name == "resume-watch.log"
    assert log_file.parent.name == ".lanegate"


# ---------------------------------------------------------------------------
# _read_pid
# ---------------------------------------------------------------------------


def test_read_pid_returns_none_when_no_file(tmp_path):
    pid_path = tmp_path / "resume-watch.pid"
    assert _read_pid(pid_path) is None


def test_read_pid_returns_none_for_dead_process(tmp_path):
    pid_path = tmp_path / "resume-watch.pid"
    pid_path.write_text("999999\n")
    with patch("lanegate.resume_watch.pid_alive", return_value=False):
        assert _read_pid(pid_path) is None


def test_read_pid_returns_live_pid(tmp_path):
    pid_path = tmp_path / "resume-watch.pid"
    my_pid = os.getpid()
    pid_path.write_text(f"{my_pid}\n")
    assert _read_pid(pid_path) == my_pid


def test_read_pid_returns_none_for_garbage(tmp_path):
    pid_path = tmp_path / "resume-watch.pid"
    pid_path.write_text("not-a-number\n")
    assert _read_pid(pid_path) is None


# ---------------------------------------------------------------------------
# cmd_resume_watch --status / --stop
# ---------------------------------------------------------------------------


def test_status_when_no_pid_file(tmp_path, capsys):
    cfg = _default_cfg(tmp_path)
    cmd_resume_watch(cfg, tmp_path, status=True)
    out = capsys.readouterr().out
    assert "not running" in out


def test_status_with_live_pid(tmp_path, capsys):
    cfg = _default_cfg(tmp_path)
    pid_path = _resume_watch_pid_file(tmp_path)
    my_pid = os.getpid()
    pid_path.write_text(f"{my_pid}\n")

    cmd_resume_watch(cfg, tmp_path, status=True)
    out = capsys.readouterr().out
    assert str(my_pid) in out
    assert "running" in out


def test_stop_when_no_pid_file(tmp_path, capsys):
    cfg = _default_cfg(tmp_path)
    cmd_resume_watch(cfg, tmp_path, stop=True)
    out = capsys.readouterr().out
    assert "nothing to stop" in out or "not running" in out


def test_stop_with_live_pid_kills_and_removes_file(tmp_path, capsys):
    cfg = _default_cfg(tmp_path)
    pid_path = _resume_watch_pid_file(tmp_path)
    my_pid = os.getpid()
    pid_path.write_text(f"{my_pid}\n")

    killed = []

    def mock_kill(pid, sig):
        killed.append((pid, sig))

    with patch("lanegate.resume_watch.os.kill", side_effect=mock_kill):
        cmd_resume_watch(cfg, tmp_path, stop=True)

    assert (my_pid, signal.SIGTERM) in killed
    assert not pid_path.exists()


def test_already_running_exits_nonzero(tmp_path):
    cfg = _default_cfg(tmp_path)
    pid_path = _resume_watch_pid_file(tmp_path)
    my_pid = os.getpid()
    pid_path.write_text(f"{my_pid}\n")

    with pytest.raises(SystemExit) as exc_info:
        cmd_resume_watch(cfg, tmp_path)
    assert exc_info.value.code == 1
    assert pid_path.exists()


def test_stale_pid_file_cleaned_up_on_start(tmp_path):
    cfg = _default_cfg(tmp_path)
    pid_path = _resume_watch_pid_file(tmp_path)
    pid_path.write_text("999999\n")  # stale

    with (
        patch("lanegate.resume_watch.pid_alive", return_value=False),
        patch("lanegate.resume_watch._run_loop") as mock_loop,
    ):
        cmd_resume_watch(cfg, tmp_path)

    mock_loop.assert_called_once()
    assert not pid_path.exists()


# ---------------------------------------------------------------------------
# _hibernated_for_rate_limit
# ---------------------------------------------------------------------------


def test_hibernated_for_rate_limit_matches_rate_limit_reason(tmp_path):
    cfg = _default_cfg(tmp_path)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_hibernated_ticket(
        tickets_dir, "TICK-001", "rate limit or quota interruption (executor exited 429)"
    )
    result = _hibernated_for_rate_limit(cfg, tmp_path)
    assert [t["id"] for t in result] == ["TICK-001"]


def test_hibernated_for_rate_limit_excludes_other_reasons(tmp_path):
    cfg = _default_cfg(tmp_path)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_hibernated_ticket(
        tickets_dir,
        "TICK-001",
        "combined-mode executor exited 0 but ticket status did not advance",
    )
    result = _hibernated_for_rate_limit(cfg, tmp_path)
    assert result == []


def test_hibernated_for_rate_limit_excludes_non_retryable_error_false_positive(tmp_path):
    cfg = _default_cfg(tmp_path)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_hibernated_ticket(
        tickets_dir,
        "TICK-001",
        "rate limit or quota interruption (executor exited 1)\n\n"
        "Raw executor output:\n"
        "You've hit your session limit · resets 4:40pm (America/Los_Angeles)\n"
        "ERROR: {\"status\":400,\"error\":{\"type\":\"invalid_request_error\","
        "\"message\":\"requires a newer version of Codex\"}}\n\n"
        "pool instance: codex",
    )
    result = _hibernated_for_rate_limit(cfg, tmp_path)
    assert result == []


def test_hibernated_for_rate_limit_ignores_non_hibernated_tickets(tmp_path):
    cfg = _default_cfg(tmp_path)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_open_ticket(tickets_dir, "TICK-001")
    result = _hibernated_for_rate_limit(cfg, tmp_path)
    assert result == []


# ---------------------------------------------------------------------------
# _run_loop — poll logic
# ---------------------------------------------------------------------------


def test_run_loop_exits_immediately_when_nothing_rate_limited(tmp_path):
    cfg = _default_cfg(tmp_path)

    with (
        patch("lanegate.resume_watch.time.sleep") as mock_sleep,
        patch("lanegate.resume_watch._write_log"),
    ):
        _run_loop(cfg, tmp_path)

    mock_sleep.assert_not_called()


def test_run_loop_retries_and_exits_when_resolved(tmp_path):
    """Retries `lanegate orchestrate`; once no rate-limited tickets remain, exits."""
    cfg = _default_cfg(tmp_path)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_hibernated_ticket(
        tickets_dir, "TICK-001", "rate limit or quota interruption (executor exited 429)"
    )

    run_calls = []

    def mock_run(args, **kwargs):
        run_calls.append(list(args))
        # Simulate the rate limit clearing: orchestrate resolves the ticket.
        _write_open_ticket(tickets_dir, "TICK-001")
        return MagicMock(returncode=0, stdout="", stderr="")

    sleep_calls = []

    with (
        patch("lanegate.resume_watch.subprocess.run", side_effect=mock_run),
        patch("lanegate.resume_watch.time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        patch("lanegate.resume_watch._write_log"),
    ):
        _run_loop(cfg, tmp_path)

    assert len(run_calls) == 1
    assert run_calls[0] == ["lanegate", "orchestrate"]
    assert sleep_calls == [300]


def test_resume_detects_and_resumes_after_limit(tmp_path):
    """Daemon detects a rate-limit hibernation, backs off exponentially, and
    resumes orchestrate — without re-marking an in-flight ticket as failed.

    TICK-001 is hibernated with the rate-limit marker; TICK-002 was already
    in_progress when the limit hit. The rate limit clears on the third retry.
    The daemon only invokes `lanegate orchestrate`; it never writes ticket status
    itself, so the in-flight TICK-002 must retain its status across resume.
    """
    cfg = _default_cfg(
        tmp_path,
        rate_limit_resume={"initial_backoff_s": 10, "max_backoff_s": 100, "ceiling_s": None},
    )
    tickets_dir = Path(cfg["tickets_dir"])
    _write_hibernated_ticket(
        tickets_dir, "TICK-001", "rate limit or quota interruption (executor exited 429)"
    )
    _write_inprogress_ticket(tickets_dir, "TICK-002")

    attempts = [0]

    def mock_run(args, **kwargs):
        attempts[0] += 1
        # Still rate-limited for the first two retries, then it clears.
        if attempts[0] >= 3:
            _write_open_ticket(tickets_dir, "TICK-001")
        return MagicMock(returncode=0, stdout="", stderr="")

    sleep_calls = []

    with (
        patch("lanegate.resume_watch.subprocess.run", side_effect=mock_run),
        patch("lanegate.resume_watch.time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        patch("lanegate.resume_watch._write_log"),
    ):
        _run_loop(cfg, tmp_path)

    # Detected the rate-limit hibernation and retried with exponential backoff.
    assert attempts[0] == 3
    assert sleep_calls == [10, 20, 40]

    # History records the detection and the eventual resume.
    events = [e["event"] for e in read_history(tmp_path)]
    assert "hibernated" in events
    assert "resumed" in events

    # The in-flight ticket was NOT re-marked failed by the resume daemon.
    tick2 = (tickets_dir / "TICK-002.md").read_text()
    assert "status: in_progress" in tick2
    assert "status: failed" not in tick2


def test_run_loop_backs_off_exponentially_and_caps_at_max(tmp_path):
    cfg = _default_cfg(
        tmp_path,
        rate_limit_resume={"initial_backoff_s": 10, "max_backoff_s": 25, "ceiling_s": None},
    )
    tickets_dir = Path(cfg["tickets_dir"])
    _write_hibernated_ticket(
        tickets_dir, "TICK-001", "rate limit or quota interruption (executor exited 429)"
    )

    attempts = [0]

    def mock_run(args, **kwargs):
        attempts[0] += 1
        if attempts[0] >= 4:
            _write_open_ticket(tickets_dir, "TICK-001")
        return MagicMock(returncode=1, stdout="", stderr="still rate limited")

    sleep_calls = []

    with (
        patch("lanegate.resume_watch.subprocess.run", side_effect=mock_run),
        patch("lanegate.resume_watch.time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        patch("lanegate.resume_watch._write_log"),
    ):
        _run_loop(cfg, tmp_path)

    # 10 -> 20 -> 25 (capped, would be 40) -> 25 (capped again)
    assert sleep_calls == [10, 20, 25, 25]
    assert attempts[0] == 4


class _FrozenDatetime(datetime):
    """A datetime subclass whose .now() is pinned, for deterministic reset-time tests."""

    _fixed_now = datetime(2026, 7, 28, 4, 0, 0, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):
        return cls._fixed_now if tz is None else cls._fixed_now.astimezone(tz)


# ---------------------------------------------------------------------------
# _parse_reset_time / _reset_wait_seconds (TICK-257)
# ---------------------------------------------------------------------------


def test_parse_reset_time_iso8601():
    now = datetime(2026, 7, 28, 4, 0, 0, tzinfo=UTC)
    text = "You have hit your usage limit. Try again at 2026-07-28T04:29:00Z."
    parsed = _parse_reset_time(text, now=now)
    assert parsed == datetime(2026, 7, 28, 4, 29, 0, tzinfo=UTC)


def test_parse_reset_time_clock_time_rolls_to_tomorrow_if_passed():
    now = datetime(2026, 7, 28, 22, 0, 0, tzinfo=UTC)
    text = "usage limit reached, resets 9:00pm"
    parsed = _parse_reset_time(text, now=now)
    assert parsed == datetime(2026, 7, 29, 21, 0, 0, tzinfo=UTC)


def test_parse_reset_time_clock_time_same_day_if_still_ahead():
    now = datetime(2026, 7, 28, 4, 0, 0, tzinfo=UTC)
    text = "resets at 9:00pm"
    parsed = _parse_reset_time(text, now=now)
    assert parsed == datetime(2026, 7, 28, 21, 0, 0, tzinfo=UTC)


def test_parse_reset_time_real_capture_with_named_timezone():
    """Real captured-output.txt sample (TICK-157 executor-run artifact,
    persisted by TICK-256's audit-bundle change): the clock time is in the
    named IANA zone, not UTC and not the machine's local zone. 3:00am
    Pacific (PDT, UTC-7 in July) -> 11:40am Pacific must resolve to
    18:40 UTC, not 11:40 UTC."""
    now = datetime(2026, 7, 29, 10, 0, 0, tzinfo=UTC)
    text = "You've hit your session limit · resets 11:40am (America/Los_Angeles)"
    parsed = _parse_reset_time(text, now=now)
    assert parsed == datetime(2026, 7, 29, 18, 40, 0, tzinfo=UTC)


def test_parse_reset_time_unknown_timezone_name_falls_back_to_now_zone():
    now = datetime(2026, 7, 28, 4, 0, 0, tzinfo=UTC)
    text = "resets 9:00pm (Not/ARealZone)"
    parsed = _parse_reset_time(text, now=now)
    assert parsed == datetime(2026, 7, 28, 21, 0, 0, tzinfo=UTC)


def test_parse_reset_time_returns_none_for_unrecognized_format():
    now = datetime(2026, 7, 28, 4, 0, 0, tzinfo=UTC)
    assert _parse_reset_time("rate limit or quota interruption (executor exited 1)", now=now) is None
    assert _parse_reset_time("please try again later", now=now) is None
    assert _parse_reset_time("", now=now) is None


def test_reset_wait_seconds_uses_latest_deadline_across_tickets():
    now = datetime(2026, 7, 28, 4, 0, 0, tzinfo=UTC)
    hibernated = [
        {"id": "TICK-001", "_body": "try again at 2026-07-28T04:10:00Z"},
        {"id": "TICK-002", "_body": "try again at 2026-07-28T04:30:00Z"},
    ]
    wait = _reset_wait_seconds(hibernated, buffer_s=60, now=now)
    assert wait == 1800 + 60


def test_reset_wait_seconds_none_when_no_hint_present():
    hibernated = [{"id": "TICK-001", "_body": "rate limit or quota interruption (executor exited 1)"}]
    assert _reset_wait_seconds(hibernated) is None


# ---------------------------------------------------------------------------
# _run_loop reset-time scheduling (TICK-257)
# ---------------------------------------------------------------------------


def test_run_loop_uses_parsed_reset_time_instead_of_backoff(tmp_path):
    """When the hibernation reason carries a parseable reset-time hint, the
    daemon schedules the retry around it (plus a small buffer) instead of
    falling back to the default exponential-backoff wait."""
    cfg = _default_cfg(
        tmp_path,
        rate_limit_resume={
            "initial_backoff_s": 300,
            "max_backoff_s": 7200,
            "ceiling_s": None,
            "reset_buffer_s": 60,
        },
    )
    tickets_dir = Path(cfg["tickets_dir"])
    _write_hibernated_ticket(
        tickets_dir,
        "TICK-001",
        "rate limit or quota interruption (executor exited 1)\n\n"
        "Raw executor output:\nYou have hit your usage limit. "
        "Try again at 2026-07-28T04:10:00Z.",
    )

    def mock_run(args, **kwargs):
        _write_open_ticket(tickets_dir, "TICK-001")
        return MagicMock(returncode=0, stdout="", stderr="")

    sleep_calls = []

    with (
        patch("lanegate.resume_watch.subprocess.run", side_effect=mock_run),
        patch("lanegate.resume_watch.time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        patch("lanegate.resume_watch._write_log"),
        patch("lanegate.resume_watch.datetime", _FrozenDatetime),
    ):
        _run_loop(cfg, tmp_path)

    # now=04:00:00Z, reset=04:10:00Z -> 600s to reset + 60s buffer = 660s,
    # not the 300s exponential-backoff default.
    assert sleep_calls == [660]


def test_run_loop_falls_back_to_backoff_when_no_reset_time_parseable(tmp_path):
    """Regression guard: hibernation text with no reset-time hint must behave
    exactly as before this ticket — unchanged exponential backoff."""
    cfg = _default_cfg(
        tmp_path,
        rate_limit_resume={"initial_backoff_s": 10, "max_backoff_s": 100, "ceiling_s": None},
    )
    tickets_dir = Path(cfg["tickets_dir"])
    _write_hibernated_ticket(
        tickets_dir, "TICK-001", "rate limit or quota interruption (executor exited 429)"
    )

    attempts = [0]

    def mock_run(args, **kwargs):
        attempts[0] += 1
        if attempts[0] >= 3:
            _write_open_ticket(tickets_dir, "TICK-001")
        return MagicMock(returncode=1, stdout="", stderr="still rate limited")

    sleep_calls = []

    with (
        patch("lanegate.resume_watch.subprocess.run", side_effect=mock_run),
        patch("lanegate.resume_watch.time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        patch("lanegate.resume_watch._write_log"),
    ):
        _run_loop(cfg, tmp_path)

    assert sleep_calls == [10, 20, 40]
    assert attempts[0] == 3


def test_run_loop_respects_ceiling_and_gives_up(tmp_path):
    cfg = _default_cfg(
        tmp_path,
        rate_limit_resume={"initial_backoff_s": 10, "max_backoff_s": 10, "ceiling_s": 25},
    )
    tickets_dir = Path(cfg["tickets_dir"])
    _write_hibernated_ticket(
        tickets_dir, "TICK-001", "rate limit or quota interruption (executor exited 429)"
    )

    def mock_run(args, **kwargs):
        # Never resolves — still rate limited every time.
        return MagicMock(returncode=1, stdout="", stderr="still rate limited")

    sleep_calls = []

    with (
        patch("lanegate.resume_watch.subprocess.run", side_effect=mock_run),
        patch("lanegate.resume_watch.time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        patch("lanegate.resume_watch._write_log"),
    ):
        _run_loop(cfg, tmp_path)

    # waits of 10 then 10 sum to 20s elapsed (< 25 ceiling), a third wait is
    # capped to the remaining 5s (elapsed -> 25 == ceiling), then it gives up.
    assert sleep_calls == [10, 10, 5]


def test_run_loop_handles_orchestrate_invocation_failure(tmp_path):
    """If `lanegate` isn't found, log and keep polling rather than crash."""
    cfg = _default_cfg(
        tmp_path,
        rate_limit_resume={"initial_backoff_s": 5, "max_backoff_s": 5, "ceiling_s": 5},
    )
    tickets_dir = Path(cfg["tickets_dir"])
    _write_hibernated_ticket(
        tickets_dir, "TICK-001", "rate limit or quota interruption (executor exited 429)"
    )

    with (
        patch("lanegate.resume_watch.subprocess.run", side_effect=OSError("not found")),
        patch("lanegate.resume_watch.time.sleep"),
        patch("lanegate.resume_watch._write_log"),
    ):
        _run_loop(cfg, tmp_path)  # must not raise


def test_run_loop_logs_to_file(tmp_path):
    cfg = _default_cfg(tmp_path)
    # No hibernated tickets → loop exits immediately after one log line.
    _run_loop(cfg, tmp_path)

    log_file = _resume_watch_log_file(tmp_path)
    assert log_file.exists()
    content = log_file.read_text()
    assert "[resume-watch]" in content


# ---------------------------------------------------------------------------
# push notifications
# ---------------------------------------------------------------------------


def test_run_loop_pushes_on_hibernation_start_and_resolution(tmp_path):
    cfg = _default_cfg(tmp_path, notify={"ntfy_topic": "test-topic"})
    tickets_dir = Path(cfg["tickets_dir"])
    _write_hibernated_ticket(
        tickets_dir, "TICK-001", "rate limit or quota interruption (executor exited 429)"
    )

    def mock_run(args, **kwargs):
        _write_open_ticket(tickets_dir, "TICK-001")
        return MagicMock(returncode=0, stdout="", stderr="")

    pushes = []
    with (
        patch("lanegate.resume_watch.subprocess.run", side_effect=mock_run),
        patch("lanegate.resume_watch.time.sleep"),
        patch("lanegate.resume_watch._write_log"),
        patch("lanegate.resume_watch.send_ntfy", side_effect=lambda topic, msg, **kw: pushes.append(msg) or True),
    ):
        _run_loop(cfg, tmp_path)

    assert any("rate limit hit on TICK-001" in m for m in pushes)
    assert any("resumed successfully" in m for m in pushes)


def test_run_loop_pushes_on_give_up(tmp_path):
    cfg = _default_cfg(
        tmp_path,
        notify={"ntfy_topic": "test-topic"},
        rate_limit_resume={"initial_backoff_s": 10, "max_backoff_s": 10, "ceiling_s": 15},
    )
    tickets_dir = Path(cfg["tickets_dir"])
    _write_hibernated_ticket(
        tickets_dir, "TICK-001", "rate limit or quota interruption (executor exited 429)"
    )

    pushes = []
    with (
        patch("lanegate.resume_watch.subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="")),
        patch("lanegate.resume_watch.time.sleep"),
        patch("lanegate.resume_watch._write_log"),
        patch("lanegate.resume_watch.send_ntfy", side_effect=lambda topic, msg, **kw: pushes.append(msg) or True),
    ):
        _run_loop(cfg, tmp_path)

    assert any("gave up" in m for m in pushes)


def test_run_loop_no_push_when_topic_not_configured(tmp_path):
    cfg = _default_cfg(tmp_path)  # no "notify" key at all
    tickets_dir = Path(cfg["tickets_dir"])
    _write_hibernated_ticket(
        tickets_dir, "TICK-001", "rate limit or quota interruption (executor exited 429)"
    )

    def mock_run(args, **kwargs):
        _write_open_ticket(tickets_dir, "TICK-001")  # resolves so the loop terminates
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.resume_watch.subprocess.run", side_effect=mock_run),
        patch("lanegate.resume_watch.time.sleep"),
        patch("lanegate.resume_watch._write_log"),
        patch("lanegate.resume_watch.send_ntfy") as mock_send,
    ):
        _run_loop(cfg, tmp_path)

    mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# structured history
# ---------------------------------------------------------------------------


def test_run_loop_writes_history_events(tmp_path):
    cfg = _default_cfg(tmp_path)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_hibernated_ticket(
        tickets_dir, "TICK-001", "rate limit or quota interruption (executor exited 429)"
    )

    def mock_run(args, **kwargs):
        _write_open_ticket(tickets_dir, "TICK-001")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.resume_watch.subprocess.run", side_effect=mock_run),
        patch("lanegate.resume_watch.time.sleep"),
        patch("lanegate.resume_watch._write_log"),
    ):
        _run_loop(cfg, tmp_path)

    events = [e["event"] for e in read_history(tmp_path)]
    assert events == ["hibernated", "retrying", "resumed"]


def test_get_daemon_status_waiting_uses_most_recent_hibernation_not_oldest(tmp_path):
    """A history file accumulates a `hibernated` entry every time any run
    hits a rate limit. elapsed_time for the current "waiting" phase must be
    measured from the most recent one, not the first ever recorded — using
    the oldest would make elapsed_time grow across unrelated past incidents
    instead of reflecting how long the current wait has actually been."""
    pid_path = _resume_watch_pid_file(tmp_path)
    pid_path.write_text(f"{os.getpid()}\n")

    recent_ts = (datetime.now(UTC) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    history_file = _resume_watch_history_file(tmp_path)
    history_file.write_text(
        '{"ts": "2026-07-28T14:18:42Z", "event": "hibernated", "ticket_ids": ["TICK-157"]}\n'
        '{"ts": "2026-07-29T00:58:19Z", "event": "hibernated", "ticket_ids": ["TICK-255"]}\n'
        '{"ts": "2026-07-29T01:03:19Z", "event": "retrying", "ticket_ids": ["TICK-255"]}\n'
        f'{{"ts": "{recent_ts}", "event": "hibernated", "ticket_ids": ["TICK-290"]}}\n'
    )

    status = get_daemon_status(tmp_path)

    assert status["phase"] == "waiting"
    # Should be ~30s (since the most recent hibernation), not ~4 days
    # (since the oldest one in the file, 2026-07-28T14:18:42Z).
    assert status["elapsed_time"] < 120


def test_read_history_returns_empty_list_when_no_file(tmp_path):
    assert read_history(tmp_path) == []


def test_read_history_since_filters_entries_before_cutoff(tmp_path):
    history_file = _resume_watch_history_file(tmp_path)
    history_file.write_text(
        '{"ts": "2026-07-10T00:00:00Z", "event": "hibernated", "ticket_ids": ["TICK-001"]}\n'
        '{"ts": "2026-07-26T23:00:00Z", "event": "retrying", "ticket_ids": ["TICK-002"]}\n'
        '{"ts": "2026-07-27T01:00:00Z", "event": "resumed", "ticket_ids": ["TICK-002"]}\n'
    )
    since = read_history_since(tmp_path, "2026-07-26T00:00:00Z")
    assert [e["event"] for e in since] == ["retrying", "resumed"]


def test_read_history_since_empty_cutoff_returns_nothing(tmp_path):
    history_file = _resume_watch_history_file(tmp_path)
    history_file.write_text(
        '{"ts": "2026-07-10T00:00:00Z", "event": "hibernated", "ticket_ids": ["TICK-001"]}\n'
    )
    assert read_history_since(tmp_path, "") == []


def test_history_flag_prints_entries(tmp_path, capsys):
    history_file = _resume_watch_history_file(tmp_path)
    history_file.write_text(
        '{"ts": "2026-07-10T00:00:00Z", "event": "hibernated", "ticket_ids": ["TICK-001"]}\n'
    )
    cmd_resume_watch(_default_cfg(tmp_path), tmp_path, history=True)
    out = capsys.readouterr().out
    assert "hibernated" in out
    assert "TICK-001" in out


def test_history_flag_with_no_entries(tmp_path, capsys):
    cmd_resume_watch(_default_cfg(tmp_path), tmp_path, history=True)
    out = capsys.readouterr().out
    assert "no history recorded" in out


# ---------------------------------------------------------------------------
# F39: preserve orchestrate arguments on resume
# ---------------------------------------------------------------------------


def test_run_loop_preserves_orchestrate_flags(tmp_path):
    """When resume-watch re-invokes orchestrate, it should preserve the original
    command-line flags (e.g., --milestone, --all, --human-review, etc.).

    This test verifies the F39 fix: resume-watch should not drop flags when
    retrying after a rate limit, otherwise orchestrate exits 1 immediately
    (if no default_milestone is configured) and the daemon retries forever.
    """
    cfg = _default_cfg(tmp_path)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_hibernated_ticket(
        tickets_dir, "TICK-001", "rate limit or quota interruption (executor exited 429)"
    )

    # Store the original orchestrate arguments as would be done by cmd_orchestrate
    args_to_store = ["--milestone", "v2", "--human-review", "per_ticket"]
    store_orchestrate_args(tmp_path, args_to_store)

    run_calls = []

    def mock_run(args, **kwargs):
        run_calls.append(list(args))
        # Simulate rate limit clearing
        _write_open_ticket(tickets_dir, "TICK-001")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.resume_watch.subprocess.run", side_effect=mock_run),
        patch("lanegate.resume_watch.time.sleep"),
        patch("lanegate.resume_watch._write_log"),
    ):
        _run_loop(cfg, tmp_path)

    # Verify that the stored arguments were included in the retry command
    assert len(run_calls) == 1
    assert run_calls[0] == ["lanegate", "orchestrate", "--milestone", "v2", "--human-review", "per_ticket"]


def test_run_loop_uses_empty_args_when_none_stored(tmp_path):
    """When no arguments are stored (e.g., plain `lanegate orchestrate`), fall
    back to using bare orchestrate without additional flags."""
    cfg = _default_cfg(tmp_path)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_hibernated_ticket(
        tickets_dir, "TICK-001", "rate limit or quota interruption (executor exited 429)"
    )

    # Don't store any arguments — fall back to default behavior

    run_calls = []

    def mock_run(args, **kwargs):
        run_calls.append(list(args))
        _write_open_ticket(tickets_dir, "TICK-001")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.resume_watch.subprocess.run", side_effect=mock_run),
        patch("lanegate.resume_watch.time.sleep"),
        patch("lanegate.resume_watch._write_log"),
    ):
        _run_loop(cfg, tmp_path)

    # Verify that without stored arguments, we still retry (just with bare command)
    assert len(run_calls) == 1
    assert run_calls[0] == ["lanegate", "orchestrate"]


# ---------------------------------------------------------------------------
# TICK-344 / R31 — a stale reset hint must not become a ~24h sleep
# ---------------------------------------------------------------------------

# The exact string lanegate/resume_watch.py's own comment cites as confirmed real.
_REAL_RESET_BODY = "You've hit your session limit · resets 11:40am (America/Los_Angeles)"
_LA = ZoneInfo("America/Los_Angeles")


@pytest.mark.parametrize(
    "now_local,allow_rollover,expected",
    [
        # Before the reset: same answer either way — the window has not cleared.
        (datetime(2026, 8, 1, 11, 0, tzinfo=_LA), True, 2490.0),
        (datetime(2026, 8, 1, 11, 0, tzinfo=_LA), False, 2490.0),
        # Two minutes after. A *freshly emitted* hint means tomorrow...
        (datetime(2026, 8, 1, 11, 42, tzinfo=_LA), True, 86370.0),
        # ...but the same text re-read from a ticket body on a later iteration
        # means the window already cleared. This is R31: it used to return
        # 86370s here, so one unproductive retry slept for a day.
        (datetime(2026, 8, 1, 11, 42, tzinfo=_LA), False, 90.0),
    ],
)
def test_reset_wait_rollover_depends_on_hint_freshness(now_local, allow_rollover, expected):
    hibernated = [{"_body": _REAL_RESET_BODY}]
    wait = _reset_wait_seconds(hibernated, now=now_local, allow_rollover=allow_rollover)
    assert wait == pytest.approx(expected, abs=1.0)


def test_run_loop_does_not_roll_a_stale_hint_forward_after_the_first_retry(tmp_path):
    """The second iteration must not sleep ~24h on a body it already saw."""
    cfg = _default_cfg(tmp_path)
    tickets_dir = Path(cfg["tickets_dir"])
    past_reset = (datetime.now(UTC) - timedelta(minutes=2)).astimezone(_LA)
    hour = past_reset.hour % 12 or 12
    reset_hint = (
        "You've hit your session limit · resets "
        f"{hour}:{past_reset.minute:02d}{past_reset.strftime('%p').lower()} "
        "(America/Los_Angeles)"
    )
    # Hibernation reason carries both the marker and a reset hint.
    _write_hibernated_ticket(
        tickets_dir,
        "TICK-001",
        f"rate limit or quota interruption (executor exited 429) — {reset_hint}",
    )

    sleeps: list[float] = []
    calls = {"n": 0}

    def mock_run(args, **kwargs):
        calls["n"] += 1
        # First retry does NOT clear the ticket and does NOT rewrite the reason —
        # the exact condition under which R31 bit.
        if calls["n"] >= 2:
            _write_open_ticket(tickets_dir, "TICK-001")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.resume_watch.subprocess.run", side_effect=mock_run),
        patch("lanegate.resume_watch.time.sleep", side_effect=sleeps.append),
        patch("lanegate.resume_watch._write_log"),
    ):
        _run_loop(cfg, tmp_path)

    assert len(sleeps) >= 2, "expected at least two waits"
    assert sleeps[1] == pytest.approx(90.0, abs=1.0)


def test_parsed_wait_is_clamped_by_max_backoff(tmp_path):
    """R31, second half: max_backoff_s is documented as the cap on any single
    wait, but the parsed-reset path used to ignore it entirely."""
    cfg = _default_cfg(tmp_path)
    cfg["rate_limit_resume"] = {
        "initial_backoff_s": 300,
        "max_backoff_s": 600,
        "ceiling_s": None,
    }
    tickets_dir = Path(cfg["tickets_dir"])
    # A reset ~8 hours out — far beyond max_backoff_s.
    far = (datetime.now(UTC) + timedelta(hours=8)).astimezone(_LA)
    far_hour = far.hour % 12 or 12
    _write_hibernated_ticket(
        tickets_dir,
        "TICK-001",
        f"rate limit or quota interruption (executor exited 429) — "
        f"resets {far_hour}:{far.minute:02d}{far.strftime('%p').lower()} (America/Los_Angeles)",
    )

    sleeps: list[float] = []

    def mock_run(args, **kwargs):
        _write_open_ticket(tickets_dir, "TICK-001")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.resume_watch.subprocess.run", side_effect=mock_run),
        patch("lanegate.resume_watch.time.sleep", side_effect=sleeps.append),
        patch("lanegate.resume_watch._write_log"),
    ):
        _run_loop(cfg, tmp_path)

    assert sleeps, "expected at least one wait"
    assert sleeps[0] <= 600, f"parsed wait bypassed max_backoff_s: {sleeps[0]}s"


# ---------------------------------------------------------------------------
# TICK-344 / R32 — one definition of the waitable-vs-broken predicate
# ---------------------------------------------------------------------------


def test_rate_limit_classifier_is_not_duplicated():
    """resume_watch must not re-declare orchestrate's classifier. The two copies
    had already drifted in both directions; identity is the only durable check."""
    assert resume_watch._has_non_rate_limit_hard_error is loop._has_non_rate_limit_hard_error
    assert resume_watch._active_rate_limit_hibernation is loop._active_rate_limit_hibernation
    assert resume_watch._RATE_LIMIT_MARKER == loop._RATE_LIMIT_MARKER


@pytest.mark.parametrize(
    "text,is_hard_error",
    [
        # Drifted: orchestrate's regex matched, resume_watch's substring did not,
        # so a dead config was retried forever.
        ("model gpt-5-x does not exist", True),
        # Drifted the other way: resume_watch matched bare "model metadata",
        # orchestrate requires "not found" nearby.
        ("the model metadata was refreshed successfully", False),
        # Single-quoted form matched orchestrate only.
        ("{'status': 400, 'error': 'bad'}", True),
    ],
)
def test_drifted_classifier_cases_now_agree(text, is_hard_error):
    assert resume_watch._has_non_rate_limit_hard_error(text) is is_hard_error
    assert loop._has_non_rate_limit_hard_error(text) is is_hard_error


# ---------------------------------------------------------------------------
# TICK-344 / R33 — stored argv is untrusted input
# ---------------------------------------------------------------------------


def test_read_orchestrate_args_accepts_flags_we_emit(tmp_path):
    store_orchestrate_args(tmp_path, ["--max", "3", "--verbose", "--pool", "fast"])
    args, dropped = _read_orchestrate_args(tmp_path)
    assert args == ["--max", "3", "--verbose", "--pool", "fast"]
    assert dropped == []


def test_read_orchestrate_args_rejects_non_list_payload(tmp_path):
    _orchestrate_args_file(tmp_path).write_text('{"args": "--max 99"}')
    args, dropped = _read_orchestrate_args(tmp_path)
    # Previously raised TypeError on `list + str`, uncaught, killing the daemon.
    assert args == []
    assert dropped


def test_read_orchestrate_args_rejects_non_string_entries(tmp_path):
    _orchestrate_args_file(tmp_path).write_text('{"args": ["--max", 99]}')
    args, dropped = _read_orchestrate_args(tmp_path)
    # Previously raised TypeError inside ' '.join(cmd).
    assert args == []
    assert dropped


def test_read_orchestrate_args_drops_unknown_flags(tmp_path):
    _orchestrate_args_file(tmp_path).write_text(
        '{"args": ["--verbose", "--exec-something-evil", "payload"]}'
    )
    args, dropped = _read_orchestrate_args(tmp_path)
    assert args == ["--verbose"]
    assert any("--exec-something-evil" in d for d in dropped)


def test_read_orchestrate_args_strips_human_review_none(tmp_path):
    """The flag is one we can emit, but a resumed run happens when nobody is
    watching, so it may never drop the gate below the configured default."""
    store_orchestrate_args(tmp_path, ["--human-review", "none", "--verbose"])
    args, dropped = _read_orchestrate_args(tmp_path)
    assert args == ["--verbose"]
    assert any("human-review" in d for d in dropped)


def test_read_orchestrate_args_keeps_stricter_human_review(tmp_path):
    store_orchestrate_args(tmp_path, ["--human-review", "per_ticket"])
    args, dropped = _read_orchestrate_args(tmp_path)
    assert args == ["--human-review", "per_ticket"]
    assert dropped == []
