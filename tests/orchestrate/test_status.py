"""Tests for persisted orchestration batch diagnostics."""

from __future__ import annotations

from types import SimpleNamespace

from tests.orchestrate.conftest import *  # noqa: F401,F403
from lanegate.orchestrate.status import get_all_active_statuses, read_batch_status, write_batch_status


def test_batch_status_round_trip_and_get_orchestration_status(tmp_path):
    batch_line = "[orchestrate] batch: 1 running of cap 3, 2 peers (3 open tickets total)"
    reason = "selected TICK-366 has parallel_safe=false"

    assert read_batch_status(tmp_path) == {
        "batch_line": "",
        "underfilled_reason": None,
        "max_parallel": None,
        "total_open": None,
    }
    write_batch_status(tmp_path, batch_line, reason, max_parallel=3, total_open=3)

    assert read_batch_status(tmp_path) == {
        "batch_line": batch_line,
        "underfilled_reason": reason,
        "max_parallel": 3,
        "total_open": 3,
    }
    status = get_orchestration_status(tmp_path)
    # No live sessions exist in this test, so the live recount is 0 running.
    assert status["batch_line"] == "[orchestrate] batch: 0 running of cap 3, 3 peers (3 open tickets total)\n"
    assert status["underfilled_reason"] == reason


def test_batch_status_tolerates_malformed_file(tmp_path):
    path = tmp_path / ".lanegate" / "orchestrate-batch-status.json"
    path.parent.mkdir()
    path.write_text("not json", encoding="utf-8")

    assert read_batch_status(tmp_path) == {
        "batch_line": "",
        "underfilled_reason": None,
        "max_parallel": None,
        "total_open": None,
    }


def test_orchestrator_lock_status_shows_cwd(tmp_path, monkeypatch, capsys):
    from lanegate.cli import _cmd_orchestrator_lock

    monkeypatch.setattr(
        "lanegate.concurrency.orchestrator_lock_status",
        lambda _root: {"held": True, "pid": 1234, "alive": True},
    )
    monkeypatch.setattr("lanegate.pidutil.pid_cwd", lambda _pid: "/main/checkout")

    _cmd_orchestrator_lock(SimpleNamespace(orch_cmd="status"), tmp_path)

    assert "cwd: /main/checkout" in capsys.readouterr().out


def test_normalize_session_status(tmp_path, monkeypatch):
    import os
    from lanegate.concurrency import acquire_orchestrator_lock, release_orchestrator_lock
    from lanegate.orchestrate.status import (
        _normalize_active_status,
        _normalize_session_status,
        _write_active_status,
    )

    # 1. Lock held, dead executor PID marker -> between-dispatches and stale_executor_marker=True
    monkeypatch.setattr(
        "lanegate.orchestrate.status._current_run_session_ts",
        lambda repo_root: "test-run",
    )
    lock_pid = acquire_orchestrator_lock(tmp_path)
    try:
        dead_pid = 9999999
        session = {
            "ticket_id": "TICK-100",
            "state": "running",
            "executor_pid": dead_pid,
            "started_at": 100.0,
            "log_path": ".lanegate/logs/orchestrate-test-run.log",
        }
        _write_active_status(tmp_path, session, session_id="s1")

        norm_session = _normalize_session_status(session, tmp_path)
        norm_active = _normalize_active_status(tmp_path)

        assert norm_session["orchestrator_lock_state"] == norm_active["orchestrator_lock_state"] == "live"
        assert norm_session["state"] == norm_active["state"] == "between-dispatches"
        assert norm_session["active"] == norm_active["active"] is True
        assert norm_session["reconciliation_state"] == norm_active["reconciliation_state"] == "orchestrator_live"
        assert norm_session.get("stale_executor_marker") == norm_active.get("stale_executor_marker") is True
    finally:
        release_orchestrator_lock(tmp_path, lock_pid)

    # 2. No lock held, dead executor PID -> not active, stale
    session_no_lock = {
        "ticket_id": "TICK-101",
        "state": "running",
        "executor_pid": 9999999,
        "started_at": 100.0,
    }
    _write_active_status(tmp_path, session_no_lock, session_id="s2")

    norm_session_nl = _normalize_session_status(session_no_lock, tmp_path)
    norm_active_nl = _normalize_active_status(tmp_path)

    assert norm_session_nl["orchestrator_lock_state"] == norm_active_nl["orchestrator_lock_state"] == "none"
    assert norm_session_nl["state"] == norm_active_nl["state"] == "running"
    assert norm_session_nl["active"] == norm_active_nl["active"] is False
    assert norm_session_nl["reconciliation_state"] == norm_active_nl["reconciliation_state"] == "stale"
    assert norm_session_nl.get("stale_executor_marker") == norm_active_nl.get("stale_executor_marker") is None

    # 3. Live executor PID -> running, active=True, reconciliation_state=live
    session_live = {
        "ticket_id": "TICK-102",
        "state": "running",
        "executor_pid": os.getpid(),
        "started_at": 100.0,
    }
    _write_active_status(tmp_path, session_live, session_id="s3")

    norm_session_live = _normalize_session_status(session_live, tmp_path)
    norm_active_live = _normalize_active_status(tmp_path)

    assert norm_session_live["state"] == norm_active_live["state"] == "running"
    assert norm_session_live["active"] == norm_active_live["active"] is True
    assert norm_session_live["reconciliation_state"] == norm_active_live["reconciliation_state"] == "live"


def test_get_all_active_statuses_includes_between_dispatches_session(tmp_path, monkeypatch):
    """TICK-451/TICK-432 regression: a ticket whose session file is momentarily
    'finished' (implement done, review not dispatched yet) must still show up
    as active — between-dispatches — alongside a genuinely running sibling,
    not get dropped just because it isn't literally state=='running'."""
    import os
    from lanegate.concurrency import acquire_orchestrator_lock, release_orchestrator_lock
    from lanegate.orchestrate.status import _write_active_status

    monkeypatch.setattr(
        "lanegate.orchestrate.status._current_run_session_ts",
        lambda repo_root: "test-run",
    )
    _write_active_status(
        tmp_path,
        {
            "ticket_id": "TICK-A",
            "state": "finished",
            "executor_pid": None,
            "started_at": 100.0,
            "log_path": ".lanegate/logs/orchestrate-test-run.log",
        },
        session_id="TICK-A",
    )
    _write_active_status(
        tmp_path,
        {
            "ticket_id": "TICK-B",
            "state": "running",
            "executor_pid": os.getpid(),
            "started_at": 100.0,
        },
        session_id="TICK-B",
    )

    lock_pid = acquire_orchestrator_lock(tmp_path)
    try:
        statuses = get_all_active_statuses(tmp_path)
    finally:
        release_orchestrator_lock(tmp_path, lock_pid)

    by_ticket = {s.get("ticket_id"): s for s in statuses}
    assert set(by_ticket) == {"TICK-A", "TICK-B"}
    assert by_ticket["TICK-A"]["state"] == "between-dispatches"
    assert by_ticket["TICK-A"]["active"] is True
    assert by_ticket["TICK-B"]["state"] == "running"
    assert by_ticket["TICK-B"]["active"] is True


def test_get_all_active_statuses_does_not_resurrect_sessions_from_a_past_run(tmp_path, monkeypatch):
    """Regression for the Workers-table flooding bug: per-session files are
    never deleted once a ticket finishes, so a 'finished' session left over
    from a run that ended days ago must stay inactive even while a later,
    unrelated orchestrator run holds the lock — it must not be resurrected as
    'between-dispatches' merely because *some* lock is held now."""
    from lanegate.concurrency import acquire_orchestrator_lock, release_orchestrator_lock
    from lanegate.orchestrate.status import _write_active_status

    monkeypatch.setattr(
        "lanegate.orchestrate.status._current_run_session_ts",
        lambda repo_root: "current-run",
    )
    _write_active_status(
        tmp_path,
        {
            "ticket_id": "TICK-STALE",
            "state": "finished",
            "executor_pid": None,
            "started_at": 100.0,
            "log_path": ".lanegate/logs/orchestrate-past-run.log",
        },
        session_id="TICK-STALE",
    )

    lock_pid = acquire_orchestrator_lock(tmp_path)
    try:
        statuses = get_all_active_statuses(tmp_path)
    finally:
        release_orchestrator_lock(tmp_path, lock_pid)

    assert statuses == []


def test_get_all_active_statuses_excludes_stale_running_marker_with_dead_pid(tmp_path):
    """A per-session file can be left behind with raw state=='running' by an
    executor that crashed or was killed without ever being reconciled. Once
    its pid is dead and no orchestrator lock is held to attribute it to a
    current run, it must not keep showing up as an active worker forever —
    filtering must key off the normalized `active` flag, not the raw
    (never-rewritten-back-to-inactive) `state` string."""
    from lanegate.orchestrate.status import _write_active_status

    _write_active_status(
        tmp_path,
        {
            "ticket_id": "TICK-CRASHED",
            "state": "running",
            "executor_pid": 9999999,  # not a live pid
            "started_at": 100.0,
        },
        session_id="TICK-CRASHED",
    )

    assert get_all_active_statuses(tmp_path) == []


def test_get_orchestration_status_recomputes_live_batch_count(tmp_path, monkeypatch):
    """The batch_line must reflect a live recount from current session state,
    not replay the frozen string captured at batch-formation time."""
    import os
    from lanegate.concurrency import acquire_orchestrator_lock, release_orchestrator_lock
    from lanegate.orchestrate.status import _write_active_status

    monkeypatch.setattr(
        "lanegate.orchestrate.status._current_run_session_ts",
        lambda repo_root: "test-run",
    )
    write_batch_status(
        tmp_path,
        "[orchestrate] batch: 3 running of cap 3, 2 peers (5 open tickets total)",
        None,
        max_parallel=3,
        total_open=5,
    )
    for tid in ("TICK-A", "TICK-B", "TICK-C"):
        _write_active_status(
            tmp_path,
            {"ticket_id": tid, "state": "running", "executor_pid": os.getpid(), "started_at": 100.0},
            session_id=tid,
        )

    lock_pid = acquire_orchestrator_lock(tmp_path)
    try:
        status = get_orchestration_status(tmp_path)
        assert "3 running of cap 3" in status["batch_line"]

        # A finished session stays visible to Workers as active
        # between-dispatches while the lock is held, but it has released its
        # executor slot and must not be counted as still running.
        _write_active_status(
            tmp_path,
            {
                "ticket_id": "TICK-C",
                "state": "finished",
                "executor_pid": None,
                "started_at": 100.0,
                "log_path": ".lanegate/logs/orchestrate-test-run.log",
            },
            session_id="TICK-C",
        )

        status = get_orchestration_status(tmp_path)
        assert "2 running of cap 3" in status["batch_line"]
        finished = {s["ticket_id"]: s for s in get_all_active_statuses(tmp_path)}["TICK-C"]
        assert finished["state"] == "between-dispatches"
        assert finished["active"] is True
    finally:
        release_orchestrator_lock(tmp_path, lock_pid)
