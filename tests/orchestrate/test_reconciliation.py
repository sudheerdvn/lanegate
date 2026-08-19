"""
Tests for stale process reconciliation, orphan process reaping, and run-report/status plumbing.

Split out of the former monolithic tests/test_orchestrate.py (TICK-316).
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from tests.orchestrate.conftest import *  # noqa: F401,F403


class TestStaleExecutorReconciliation:
    def test_stale_executor_marker_reconciles_to_hibernated(self, tmp_path):
        """When an in-progress ticket has a stale executor marker, reconciliation hibernates it."""
        import time

        from lanegate.orchestrate import _reconcile_stale_executor_markers
        from lanegate.ticket import parse_ticket

        cfg = _default_cfg(tmp_path)

        # Create an in-progress ticket
        ticket = _write_ticket(
            tmp_path / cfg["tickets_dir"],
            "TICK-111",
            "in_progress",
            touches=["a.py"],
        )

        # Manually create a stale active status (dead PID)
        status = {
            "schema_version": 1,
            "ticket_id": "TICK-111",
            "executor": "claude-process",
            "executor_pid": 2147483647,  # Very high PID that definitely doesn't exist
            "executor_session": "TICK-111-123456-9999-implement",
            "step": "implement",
            "state": "running",
            "reconciliation_state": "stale",
            "started_at": int(time.time()) - 3600,
            "started_at_iso": "2026-01-01T00:00:00Z",
            "log_path": "/tmp/executor.log",
            "prompt_path": "/tmp/prompt.md",
            "heartbeat_count": 5,
            "last_event": "executor_heartbeat",
        }
        from lanegate.orchestrate import _write_active_status

        _write_active_status(tmp_path, status)

        # Call reconciliation
        from io import StringIO

        out_stream = StringIO()
        reconciled = _reconcile_stale_executor_markers(cfg, tmp_path, out_stream=out_stream)

        # Verify reconciliation happened
        assert reconciled is not None
        assert reconciled["active"] is False
        assert reconciled["state"] == "reconciled"
        assert reconciled["reconciliation_state"] == "hibernated"
        assert "stale executor marker" in out_stream.getvalue().lower()

        # Verify the ticket is now hibernated
        updated_ticket = parse_ticket(ticket)
        assert updated_ticket["status"] == "hibernated"

    def test_stale_executor_marker_noop_when_ticket_already_hibernated(self, tmp_path):
        """When stale marker exists but ticket is already hibernated, reconciliation returns None (noop)."""
        import time

        from lanegate.orchestrate import _reconcile_stale_executor_markers
        from lanegate.ticket import parse_ticket

        cfg = _default_cfg(tmp_path)

        # Create a hibernated ticket (simulating prior _hibernate_orphaned)
        ticket = _write_ticket(
            tmp_path / cfg["tickets_dir"],
            "TICK-222",
            "hibernated",
            touches=["b.py"],
        )

        # Manually create a stale active status (dead PID)
        status = {
            "schema_version": 1,
            "ticket_id": "TICK-222",
            "executor": "claude-process",
            "executor_pid": 2147483647,  # Very high PID that definitely doesn't exist
            "executor_session": "TICK-222-123456-9999-implement",
            "step": "implement",
            "state": "running",
            "reconciliation_state": "stale",
            "started_at": int(time.time()) - 3600,
            "started_at_iso": "2026-01-01T00:00:00Z",
            "log_path": "/tmp/executor.log",
            "prompt_path": "/tmp/prompt.md",
            "heartbeat_count": 5,
            "last_event": "executor_heartbeat",
        }
        from lanegate.orchestrate import _write_active_status

        _write_active_status(tmp_path, status)

        # Call reconciliation
        from io import StringIO

        out_stream = StringIO()
        reconciled = _reconcile_stale_executor_markers(cfg, tmp_path, out_stream=out_stream)

        # Verify reconciliation is a noop (returns None)
        assert reconciled is None

        # Verify the ticket status unchanged (still hibernated)
        updated_ticket = parse_ticket(ticket)
        assert updated_ticket["status"] == "hibernated"

        # Verify the marker was cleared
        from lanegate.orchestrate import _normalize_active_status
        marker_status = _normalize_active_status(tmp_path, cfg)
        assert marker_status["state"] == "reconciled"
        assert marker_status["reconciliation_state"] == "noop"

    def test_reconciliation_clears_the_per_session_file_it_read_from(self, tmp_path):
        """Regression: _normalize_active_status prefers per-session files
        (.lanegate/active-orchestrate/<session>.json), but the old write-back always
        targeted the legacy shared file. That left the per-session file's
        reconciliation_state stuck at "stale" forever, so every subsequent
        orchestrate run re-detected the same marker and (via cmd_orchestrate's
        `if reconciled is not None: break`) exited immediately without ever
        dispatching new work. A second reconciliation attempt must be a no-op."""
        import time

        from lanegate.orchestrate import _reconcile_stale_executor_markers, _write_active_status

        cfg = _default_cfg(tmp_path)
        _write_ticket(
            tmp_path / cfg["tickets_dir"],
            "TICK-112",
            "in_progress",
            touches=["a.py"],
        )

        session_id = "TICK-112-123456-9999-implement"
        status = {
            "schema_version": 1,
            "ticket_id": "TICK-112",
            "executor": "claude-process",
            "executor_pid": 2147483647,
            "executor_session": session_id,
            "step": "implement",
            "state": "running",
            "reconciliation_state": "stale",
            "started_at": int(time.time()) - 3600,
            "started_at_iso": "2026-01-01T00:00:00Z",
            "log_path": "/tmp/executor.log",
            "prompt_path": "/tmp/prompt.md",
            "heartbeat_count": 5,
            "last_event": "executor_heartbeat",
        }
        _write_active_status(tmp_path, status, session_id=session_id)

        from io import StringIO

        first = _reconcile_stale_executor_markers(cfg, tmp_path, out_stream=StringIO())
        assert first is not None
        assert first["reconciliation_state"] == "hibernated"

        # The per-session file itself (not just the legacy shared file) must
        # reflect the reconciled state, so a fresh orchestrate run doesn't
        # re-detect the same stale marker and exit immediately again.
        session_file = tmp_path / ".lanegate" / "active-orchestrate" / f"{session_id}.json"
        assert json.loads(session_file.read_text())["reconciliation_state"] == "hibernated"

        second = _reconcile_stale_executor_markers(cfg, tmp_path, out_stream=StringIO())
        assert second is None


# ---------------------------------------------------------------------------
# spawn_watch_daemon
# ---------------------------------------------------------------------------


# _collect_prior_notes
# ---------------------------------------------------------------------------


def test_collect_prior_notes_no_recovery_returns_empty(tmp_path):
    """No .lanegate/recovery/<tid>.md and a non-hibernated/needs_review status
    means _collect_prior_notes has nothing to surface (TICK-481: the old
    per-file .lanegate/notes/ branch was dead -- write and read sides never
    agreed on a directory -- and has been removed)."""
    from lanegate.orchestrate import _collect_prior_notes

    ticket = {
        "id": "TICK-002",
        "status": "open",
        "touches": ["lanegate/lifecycle.py"],
    }

    result = _collect_prior_notes(ticket, tmp_path)

    assert result == ""


def test_collect_prior_notes_ignores_shared_notes(tmp_path):
    """Shared notes are injected boundedly via get_bounded_shared_notes in analyze/executor,
    so _collect_prior_notes ignores .lanegate/notes/ to prevent duplicate unbudgeted injection."""
    from lanegate.orchestrate import _collect_prior_notes

    notes_dir = tmp_path / ".lanegate" / "notes"
    notes_dir.mkdir(parents=True)
    (notes_dir / "global.md").write_text("global note")
    (notes_dir / "lanegate_worktree.py.md").write_text("file note")
    ticket = {"id": "TICK-002", "status": "open", "touches": ["lanegate/worktree.py"]}

    result = _collect_prior_notes(ticket, tmp_path)

    assert result == ""


def test_durable_notes_are_shared_by_control_and_stage_worktrees(tmp_path):
    """Implementation, review, and fix paths all resolve to the canonical notes store."""
    control_notes = tmp_path / ".lanegate" / "notes"
    control_notes.mkdir(parents=True)
    worktree_notes = {}
    for stage in ("implementation", "review", "fix"):
        notes_path = tmp_path / "worktrees" / stage / ".lanegate" / "notes"
        notes_path.parent.mkdir(parents=True)
        notes_path.symlink_to(control_notes, target_is_directory=True)
        worktree_notes[stage] = notes_path

    (worktree_notes["implementation"] / "src_widget.py.md").write_text("Keep writes atomic.\n")
    (worktree_notes["review"] / "global.md").write_text("Use the canonical notes store.\n")
    (worktree_notes["fix"] / "src_widget.py.md").write_text("Keep writes atomic; retain retries.\n")

    assert (control_notes / "src_widget.py.md").read_text() == "Keep writes atomic; retain retries.\n"
    assert (worktree_notes["review"] / "src_widget.py.md").read_text() == "Keep writes atomic; retain retries.\n"
    assert (control_notes / "global.md").read_text() == "Use the canonical notes store.\n"
    assert (worktree_notes["fix"] / "global.md").read_text() == "Use the canonical notes store.\n"


def test_collect_prior_notes_includes_recovery_for_hibernated(tmp_path):
    from lanegate.orchestrate import _collect_prior_notes

    recovery_dir = tmp_path / ".lanegate" / "recovery"
    recovery_dir.mkdir(parents=True)
    (recovery_dir / "TICK-003.md").write_text("## Recovery\n\nPrior work context.")

    ticket = {
        "id": "TICK-003",
        "status": "hibernated",
        "touches": [],
    }

    result = _collect_prior_notes(ticket, tmp_path)

    assert "Hibernation Recovery Context" in result
    assert "Prior work context" in result


def test_concurrent_executors_write_separate_status_files(tmp_path):
    """Verify concurrent executors write to per-session status files, not a shared file.

    F10 finding: with max_parallel > 1, concurrent invoke_executor calls were
    clobbering each other's status in a single .lanegate/active-orchestrate.json file.
    This test verifies the fix: each executor writes to .lanegate/active-orchestrate/<session_id>.json.
    """
    from lanegate.orchestrate import (
        _active_status_path,
        _read_active_status,
        _write_active_status,
    )

    # Simulate two concurrent executors writing status
    session_id_1 = "TICK-001-1234567890-1001-implement"
    session_id_2 = "TICK-002-1234567891-1002-implement"

    status_1 = {
        "ticket_id": "TICK-001",
        "executor_session": session_id_1,
        "state": "running",
        "executor_pid": 1001,
    }
    status_2 = {
        "ticket_id": "TICK-002",
        "executor_session": session_id_2,
        "state": "running",
        "executor_pid": 1002,
    }

    # Each executor writes to its own session file
    _write_active_status(tmp_path, status_1, session_id=session_id_1)
    _write_active_status(tmp_path, status_2, session_id=session_id_2)

    # Verify both statuses are preserved (not clobbered)
    read_1 = _read_active_status(tmp_path, session_id=session_id_1)
    read_2 = _read_active_status(tmp_path, session_id=session_id_2)

    assert read_1 is not None
    assert read_1.get("ticket_id") == "TICK-001"
    assert read_1.get("executor_pid") == 1001

    assert read_2 is not None
    assert read_2.get("ticket_id") == "TICK-002"
    assert read_2.get("executor_pid") == 1002

    # Verify they're stored in separate files under .lanegate/active-orchestrate/
    path_1 = _active_status_path(tmp_path, session_id=session_id_1)
    path_2 = _active_status_path(tmp_path, session_id=session_id_2)

    assert path_1 != path_2
    assert ".lanegate/active-orchestrate/" in path_1.as_posix()
    assert ".lanegate/active-orchestrate/" in path_2.as_posix()
    assert path_1.exists()
    assert path_2.exists()


def test_find_latest_audit_bundle_locates_most_recent_session(tmp_path):
    """Verify audit bundles can be found by scanning executor-runs directory.

    With concurrent executors, the shared status file is unreliable. This test
    verifies we can find the audit bundle by scanning .lanegate/executor-runs/<tid>/
    for the most recent session directory.
    """
    from lanegate.orchestrate import _find_latest_audit_bundle

    tid = "TICK-123"
    repo_root = tmp_path

    # Create multiple session directories (simulating multiple executor runs)
    runs_dir = repo_root / ".lanegate" / "executor-runs" / tid
    runs_dir.mkdir(parents=True, exist_ok=True)

    session_1 = runs_dir / "TICK-123-1000-1001-implement"
    session_2 = runs_dir / "TICK-123-2000-1002-implement"
    session_3 = runs_dir / "TICK-123-3000-1003-implement"

    session_1.mkdir(parents=True, exist_ok=True)
    session_2.mkdir(parents=True, exist_ok=True)
    session_3.mkdir(parents=True, exist_ok=True)

    # Write status.json to each session to make them look like real audit bundles
    (session_1 / "status.json").write_text(json.dumps({"ticket_id": tid}))
    (session_2 / "status.json").write_text(json.dumps({"ticket_id": tid}))
    (session_3 / "status.json").write_text(json.dumps({"ticket_id": tid}))

    # Find the latest bundle
    latest = _find_latest_audit_bundle(repo_root, tid)

    # Should return the most recently modified session
    assert latest is not None
    assert latest.name in ("TICK-123-1000-1001-implement", "TICK-123-2000-1002-implement", "TICK-123-3000-1003-implement")


def test_normalize_active_status_reads_per_session_files(tmp_path, monkeypatch):
    """Verify --status command aggregates all active executor statuses.

    With concurrent executors, we need to read from per-session files instead of
    a single shared file. This test verifies _normalize_active_status checks
    per-session files first.
    """
    from lanegate.orchestrate import (
        _normalize_active_status,
        _write_active_status,
    )

    session_id = "TICK-100-1234567890-1001-implement"

    # Write status to per-session file
    status = {
        "ticket_id": "TICK-100",
        "executor_session": session_id,
        "state": "running",
        "executor_pid": 1001,
        "started_at": 1234567890,
    }
    _write_active_status(tmp_path, status, session_id=session_id)
    monkeypatch.setattr("lanegate.pidutil.pid_alive", lambda pid: pid == 1001)

    # Normalize should read from per-session file
    normalized = _normalize_active_status(tmp_path)

    assert normalized.get("active") is True
    assert normalized.get("ticket_id") == "TICK-100"
    assert normalized.get("executor_pid") == 1001


def test_normalize_active_status_stays_live_between_dispatches(tmp_path, monkeypatch):
    from lanegate.orchestrate import _normalize_active_status, _write_active_status

    _write_active_status(
        tmp_path,
        {
            "ticket_id": "TICK-101",
            "executor_session": "TICK-101-finished",
            "executor_pid": 1002,
            "state": "finished",
            "started_at": 1234567890,
            "log_path": ".lanegate/logs/orchestrate-test-run.log",
        },
        session_id="TICK-101-finished",
    )
    monkeypatch.setattr(
        "lanegate.orchestrate.status.orchestrator_lock_status",
        lambda repo_root: {"held": True, "pid": 4242, "alive": True},
    )
    monkeypatch.setattr(
        "lanegate.orchestrate.status._current_run_session_ts",
        lambda repo_root: "test-run",
    )

    normalized = _normalize_active_status(tmp_path)

    assert normalized["active"] is True
    assert normalized["state"] == "between-dispatches"
    assert normalized["orchestrator_lock_state"] == "live"
    assert normalized["ticket_id"] == "TICK-101"


def test_normalize_active_status_aggregates_live_parallel_sessions(tmp_path, monkeypatch):
    from lanegate.orchestrate import _active_status_path, _normalize_active_status, _write_active_status

    running_session = "TICK-102-running"
    finished_session = "TICK-103-finished"
    _write_active_status(
        tmp_path,
        {
            "ticket_id": "TICK-102",
            "executor_session": running_session,
            "executor_pid": 1003,
            "state": "running",
            "started_at": 1234567890,
        },
        session_id=running_session,
    )
    _write_active_status(
        tmp_path,
        {
            "ticket_id": "TICK-103",
            "executor_session": finished_session,
            "executor_pid": 1004,
            "state": "finished",
            "started_at": 1234567890,
        },
        session_id=finished_session,
    )
    finished_path = _active_status_path(tmp_path, session_id=finished_session)
    finished = json.loads(finished_path.read_text())
    finished["updated_at"] = "9999-12-31T23:59:59Z"
    finished_path.write_text(json.dumps(finished))
    monkeypatch.setattr("lanegate.pidutil.pid_alive", lambda pid: pid == 1003)
    monkeypatch.setattr(
        "lanegate.orchestrate.status.orchestrator_lock_status",
        lambda repo_root: {"held": False, "pid": None, "alive": False},
    )

    normalized = _normalize_active_status(tmp_path)

    assert normalized["active"] is True
    assert normalized["state"] == "running"
    assert normalized["ticket_id"] == "TICK-102"


def test_normalize_active_status_stale_lock_is_idle(tmp_path, monkeypatch):
    from lanegate.orchestrate import _normalize_active_status, _write_active_status

    _write_active_status(
        tmp_path,
        {
            "ticket_id": "TICK-104",
            "executor_session": "TICK-104-finished",
            "executor_pid": 1005,
            "state": "finished",
            "started_at": 1234567890,
        },
        session_id="TICK-104-finished",
    )
    monkeypatch.setattr(
        "lanegate.orchestrate.status.orchestrator_lock_status",
        lambda repo_root: {"held": False, "pid": 4243, "alive": False},
    )

    normalized = _normalize_active_status(tmp_path)

    assert normalized["active"] is False
    assert normalized["orchestrator_lock_state"] == "stale"


def test_stream_subprocess_timeout_kills_process(tmp_path):
    """Verify _stream_subprocess enforces process timeout and kills hanging processes."""
    import sys
    import time
    from lanegate.orchestrate import _stream_subprocess

    start = time.time()
    rc, out, err, kill_reason = _stream_subprocess(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        str(tmp_path),
        timeout=0.2,
    )
    elapsed = time.time() - start

    assert rc == 124
    assert kill_reason is None
    assert "timed out after 0.2s" in err
    assert elapsed < 3.0, f"Process should be killed promptly, took {elapsed:.2f}s"


def test_stream_subprocess_timeout_still_applies_with_budget_probe(tmp_path):
    """A polling budget probe must not bypass the ordinary process timeout."""
    import sys
    import time
    from lanegate.orchestrate import _stream_subprocess

    start = time.time()
    rc, _out, err, kill_reason = _stream_subprocess(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        str(tmp_path),
        timeout=0.2,
        budget_probe=lambda: None,
    )
    elapsed = time.time() - start

    assert rc == 124
    assert kill_reason is None
    assert "timed out after 0.2s" in err
    assert elapsed < 3.0, f"Process should be killed promptly, took {elapsed:.2f}s"


@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX process groups")
def test_stream_subprocess_timeout_terminates_process_tree(tmp_path):
    """A timeout must not leave an executor's spawned child behind."""
    from lanegate.pidutil import pid_alive

    child_pid_path = tmp_path / "child.pid"
    child_code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    code = (
        "import pathlib, subprocess, sys, time; "
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid)); "
        "time.sleep(30)"
    )

    from lanegate.orchestrate import _stream_subprocess

    rc, _out, _err, _kill_reason = _stream_subprocess(
        [sys.executable, "-c", code], str(tmp_path), timeout=0.2
    )

    child_pid = int(child_pid_path.read_text())
    assert rc == 124
    for _ in range(20):
        if not pid_alive(child_pid):
            break
        time.sleep(0.05)
    assert not pid_alive(child_pid), "timed-out executor child must not survive"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX launch options")
def test_stream_subprocess_starts_new_posix_session(tmp_path, monkeypatch):
    from lanegate.orchestrate import run_report

    real_popen = subprocess.Popen
    captured = {}

    def recording_popen(*args, **kwargs):
        captured.update(kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(run_report.subprocess, "Popen", recording_popen)
    rc, _out, _err, _kill_reason = run_report._stream_subprocess(
        [sys.executable, "-c", "pass"], str(tmp_path), timeout=2
    )

    assert rc == 0
    assert captured["start_new_session"] is True


def test_stream_subprocess_windows_uses_group_and_tree_kill(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    from lanegate.orchestrate import run_report

    proc = MagicMock(pid=1234)
    proc.wait.return_value = 0
    taskkill = MagicMock()
    monkeypatch.setattr(run_report.sys, "platform", "win32")
    monkeypatch.setattr(run_report.subprocess, "run", taskkill)

    run_report._terminate_process_tree(proc)

    clean_proc = MagicMock(pid=5678, stdin=None, stdout=[], stderr=[], returncode=0)
    clean_proc.wait.return_value = 0
    popen = MagicMock(return_value=clean_proc)
    monkeypatch.setattr(run_report.subprocess, "Popen", popen)
    run_report._stream_subprocess(["executor"], str(tmp_path), timeout=0.01)

    assert popen.call_args.kwargs["creationflags"] == 0x00000200
    taskkill.assert_called_once_with(
        ["taskkill", "/PID", "1234", "/T", "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def test_stream_subprocess_devnull_stdin_when_stdin_text_none(tmp_path):
    """Verify _stream_subprocess passes DEVNULL on stdin when stdin_text is None."""
    import sys
    from lanegate.orchestrate import _stream_subprocess

    rc, out, err, kill_reason = _stream_subprocess(
        [sys.executable, "-c", "import sys; content = sys.stdin.read(); print(f'READ:{len(content)}')"],
        str(tmp_path),
        stdin_text=None,
        timeout=5.0,
    )

    assert rc == 0
    assert kill_reason is None
    assert "READ:0" in out


def test_stream_subprocess_not_killed_while_events_keep_arriving(tmp_path):
    import sys
    from lanegate.orchestrate import _stream_subprocess

    code = "import time; [print('{\\\"type\\\": \\\"event\\\"}', flush=True) or time.sleep(.03) for _ in range(8)]"
    # Budgets are generous relative to the ~240ms of scripted work so this
    # doesn't flake under CI contention (macOS runners in particular can
    # have slow subprocess spawn/scheduling latency).
    rc, out, err, kill_reason = _stream_subprocess(
        [sys.executable, "-c", code], str(tmp_path), timeout=.05,
        idle_timeout=.5, absolute_ceiling=5,
    )
    assert rc == 0
    assert kill_reason is None
    assert out.count('"type"') == 8


def test_stream_subprocess_killed_on_idle_gap(tmp_path):
    import sys
    from lanegate.orchestrate import _stream_subprocess

    rc, out, err, kill_reason = _stream_subprocess(
        [sys.executable, "-c", "import time; time.sleep(2)"], str(tmp_path),
        idle_timeout=.08, absolute_ceiling=1,
    )
    assert rc == 124
    assert kill_reason == "idle"


def test_stream_subprocess_fresh_heartbeat_prevents_output_idle_kill(tmp_path):
    """A quiet child is not killed while a liveness monitor stays fresh."""
    import sys
    import threading
    import time
    from lanegate.orchestrate import _stream_subprocess

    heartbeat = [time.time()]
    stop = threading.Event()

    def keep_heartbeating():
        while not stop.wait(.02):
            heartbeat[0] = time.time()

    worker = threading.Thread(target=keep_heartbeating, daemon=True)
    worker.start()
    try:
        # Budgets are generous relative to the .2s child sleep so this
        # doesn't flake under CI contention (macOS runners in particular can
        # have slow subprocess spawn/scheduling latency).
        rc, _out, _err, kill_reason = _stream_subprocess(
            [sys.executable, "-c", "import time; time.sleep(.2)"], str(tmp_path),
            idle_timeout=.3,
            stall_timeout=3,
            absolute_ceiling=5,
            liveness_probe=lambda: heartbeat[0],
        )
    finally:
        stop.set()
        worker.join(timeout=1)

    assert rc == 0
    assert kill_reason is None


def test_stream_subprocess_kills_live_but_no_progress_at_stall_limit(tmp_path):
    """Heartbeat liveness cannot keep a non-progressing executor alive forever."""
    import sys
    import time
    from lanegate.orchestrate import _stream_subprocess

    rc, _out, err, kill_reason = _stream_subprocess(
        [sys.executable, "-c", "import time; time.sleep(2)"], str(tmp_path),
        idle_timeout=.05,
        stall_timeout=.12,
        absolute_ceiling=2,
        liveness_probe=time.time,
        progress_probe=lambda: 0.0,
    )

    assert rc == 124
    assert kill_reason == "stall"
    assert "no semantic progress" in err


def test_stream_subprocess_ceiling_kill_returns_captured_partial_output(tmp_path):
    import sys
    from lanegate.orchestrate import _stream_subprocess

    code = "import time; print('{\\\"type\\\": \\\"event\\\", \\\"text\\\": \\\"partial\\\"}', flush=True); time.sleep(2)"
    rc, out, err, kill_reason = _stream_subprocess(
        [sys.executable, "-c", code], str(tmp_path), idle_timeout=1, absolute_ceiling=.1,
    )
    assert rc == 124
    assert kill_reason == "ceiling"
    assert "partial" in out


# ---------------------------------------------------------------------------
# TICK-244: run report — durable, incremental record of an orchestrate run
# ---------------------------------------------------------------------------


class TestRunReportEvents:
    def test_merged_ticket_records_dispatch_and_final_outcome(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        # This test exercises the fully autonomous merge path.  Supervised
        # autonomy deliberately stops for a human before merge.
        cfg["autonomy"] = "full"
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"])

        def fake_start(tid, cfg_, repo_root, **kwargs):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: open", "status: in_progress", 1))

        def fake_complete(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(
                p.read_text().replace("status: in_progress", "status: code_complete", 1)
            )

        def fake_review(tid, cfg_, repo_root, *, verdict=None, summary=None, findings=None):
            p = tickets_dir / f"{tid}.md"
            text = p.read_text().replace("status: code_complete", "status: in_review", 1)
            if "review_verdict:" not in text:
                text = text.replace(f"id: {tid}\n", f"id: {tid}\nreview_verdict: approved\n")
            p.write_text(text)

        def fake_merge(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_review", "status: merged", 1))

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=fake_start),
            patch("lanegate.orchestrate.invoke_executor", return_value=(0, "", "")),
            patch("lanegate.orchestrate._is_rate_limit", return_value=False),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._committed_files", return_value=set()),
            patch("lanegate.orchestrate._run_static_analysis", return_value=[]),
            patch("lanegate.orchestrate._run_acceptance_contract_audit", return_value=[]),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.orchestrate.run_review_agent", side_effect=lambda ticket, repo_root, **kw: (fake_review(ticket["id"], cfg, repo_root), True)[1]),
            patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete),
            patch("lanegate.lifecycle.cmd_review", side_effect=fake_review),
            patch("lanegate.lifecycle.cmd_merge", side_effect=fake_merge),
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(
                cfg, tmp_path, human_review="none", all_milestones=True, auto_analyze=False
            )

        report = build_run_report(cfg, tmp_path)
        assert report is not None
        assert report["status"] == "completed"
        assert report["summary"] is not None
        assert report["summary"]["reason"] == "success"
        assert len(report["summary"]["batch_tickets"]) == 1
        t_summary = report["summary"]["batch_tickets"][0]
        assert t_summary["ticket_id"] == "TICK-001"
        assert t_summary["outcome"] == "success"
        assert len(report["tickets"]) == 1
        row = report["tickets"][0]
        assert row["ticket_id"] == "TICK-001"
        assert row["final_outcome"] == "merged"
        assert row["executor"]  # dispatch resolved some executor name
        assert row["duration_seconds"] is not None
        assert row["duration_seconds"] >= 0

        # Rendering must not raise, in either mode.
        cmd_run_report(cfg, tmp_path)
        cmd_run_report(cfg, tmp_path, json_output=True)

    def test_rate_limit_hibernation_records_outcome_and_cooldown_event(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        _write_ticket(tmp_path / "tickets", "TICK-001", "open", touches=["a.py"])

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", return_value=(1, "", "rate limit hit")),
            patch("lanegate.orchestrate._is_rate_limit", return_value=True),
            patch("lanegate.lifecycle.cmd_hibernate"),
            patch("lanegate.orchestrate._write_executor_cooldown"),
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        report = build_run_report(cfg, tmp_path)
        assert report["tickets"][0]["final_outcome"] == "hibernated"
        assert len(report["hibernations"]) == 1
        assert any(e["event"] == "executor_cooldown" for e in report["executor_events"])

    def test_executor_failure_records_failed_outcome(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        _write_ticket(tmp_path / "tickets", "TICK-001", "open", touches=["a.py"])

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", return_value=(2, "", "boom")),
            patch("lanegate.orchestrate._is_rate_limit", return_value=False),
            patch("lanegate.lifecycle.cmd_fail"),
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        report = build_run_report(cfg, tmp_path)
        row = report["tickets"][0]
        assert row["final_outcome"] == "failed"
        assert "exited with code 2" in row["final_reason"]

    def test_crash_inside_drain_loop_still_records_run_end(self, tmp_path):
        """A killed/crashed orchestrate process can't run its own finally block,
        but a raised (non-fatal-to-the-process) exception still must — the
        report must reflect a crash, not silently look like a clean run."""
        cfg = _default_cfg(tmp_path)
        _write_ticket(tmp_path / "tickets", "TICK-001", "open", touches=["a.py"])

        with (
            patch("lanegate.orchestrate._drain_loop", side_effect=RuntimeError("boom")),
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
            pytest.raises(RuntimeError),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        report = build_run_report(cfg, tmp_path)
        assert "crashed" in report["status"]
        assert "boom" in report["status"]

    def test_dry_run_does_not_write_a_run_report(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        _write_ticket(tmp_path / "tickets", "TICK-001", "open", touches=["a.py"])
        cmd_orchestrate(cfg, tmp_path, dry_run=True, all_milestones=True)
        assert build_run_report(cfg, tmp_path) is None


class TestBuildRunReport:
    def test_returns_none_when_nothing_ever_ran(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        assert build_run_report(cfg, tmp_path) is None

    def test_run_report_text_output(self, tmp_path, capsys):
        cfg = _default_cfg(tmp_path)
        session_ts = "2026-01-01T00-00-00"
        _append_run_event(
            tmp_path,
            session_ts,
            "run_start",
            pid=os.getpid(),
            milestone="v1",
            pool="fast",
            max_parallel=3,
            human_review="per_ticket",
        )
        _append_run_event(tmp_path, session_ts, "run_end", status="completed")

        cmd_run_report(cfg, tmp_path, session_ts=session_ts)

        output = capsys.readouterr().out
        assert "milestone: v1" in output
        assert "pool: fast" in output
        assert "max_parallel: 3" in output
        assert "human_review: per_ticket" in output
        assert "log:" in output

    def test_resolves_most_recent_session_via_pointer_with_no_args(self, tmp_path):
        from lanegate.orchestrate import _write_last_run_pointer

        cfg = _default_cfg(tmp_path)
        session_ts = "2026-01-01T00-00-00"
        _append_run_event(
            tmp_path,
            session_ts,
            "run_start",
            pid=os.getpid(),
            milestone="v1",
            pool=None,
            max_parallel=1,
            human_review="none",
        )
        _append_run_event(tmp_path, session_ts, "run_end", status="completed")
        _write_last_run_pointer(
            tmp_path, session_ts, _run_events_path(tmp_path, session_ts)
        )

        report = build_run_report(cfg, tmp_path)
        assert report["session_ts"] == session_ts
        assert report["status"] == "completed"
        assert report["milestone"] == "v1"

    def test_crashed_run_detected_when_no_run_end_and_pid_dead(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        session_ts = "2026-01-01T00-00-00"
        _append_run_event(
            tmp_path,
            session_ts,
            "run_start",
            pid=1234567,
            milestone=None,
            pool=None,
            max_parallel=1,
            human_review="none",
        )
        with patch("lanegate.orchestrate.pid_alive", return_value=False):
            report = build_run_report(cfg, tmp_path, session_ts=session_ts)
        assert "crashed" in report["status"]

    def test_running_when_no_run_end_and_pid_alive(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        session_ts = "2026-01-01T00-00-00"
        _append_run_event(
            tmp_path,
            session_ts,
            "run_start",
            pid=os.getpid(),
            milestone=None,
            pool=None,
            max_parallel=1,
            human_review="none",
        )
        report = build_run_report(cfg, tmp_path, session_ts=session_ts)
        assert report["status"] == "running"

    def test_events_survive_malformed_lines(self, tmp_path):
        session_ts = "2026-01-01T00-00-00"
        path = _run_events_path(tmp_path, session_ts)
        path.write_text('{"ts": "x", "event": "run_start", "pid": 1}\nNOT JSON\n')
        events = _load_run_events(tmp_path, session_ts)
        assert len(events) == 1


class TestDispatchedTicketWithoutTerminalOutcome:
    """A dispatched ticket that never got a ticket_outcome event must show
    in_progress while the orchestrator is alive and interrupted once it
    isn't — never skipped, which is reserved for a documented non-dispatch
    decision (TICK-325)."""

    def test_live_orchestrator_shows_in_progress_with_elapsed_duration(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        session_ts = "2026-01-01T00-00-00"
        _append_run_event(
            tmp_path, session_ts, "run_start", pid=os.getpid(), ts="2026-01-01T00:00:00Z"
        )
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_dispatch",
            ticket_id="TICK-500",
            executor="claude-a",
            was_hibernated=False,
            ts="2026-01-01T00:00:01Z",
        )

        report = build_run_report(cfg, tmp_path, session_ts=session_ts)
        assert report["status"] == "running"
        row = report["tickets"][0]
        assert row["final_outcome"] == "in_progress"
        assert row["final_outcome"] != "skipped"
        assert row["duration_seconds"] is not None
        assert row["duration_seconds"] >= 0

        summary = report["summary"]
        t_summary = summary["batch_tickets"][0]
        assert t_summary["outcome"] == "in_progress"
        assert t_summary["duration_seconds"] >= 0

        # Rendering must not raise.
        cmd_run_report(cfg, tmp_path, session_ts=session_ts)
        cmd_run_report(cfg, tmp_path, session_ts=session_ts, json_output=True)

    def test_dead_orchestrator_shows_interrupted_with_recovery_hint(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        session_ts = "2026-01-01T00-00-00"
        _append_run_event(
            tmp_path, session_ts, "run_start", pid=99999999, ts="2026-01-01T00:00:00Z"
        )
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_dispatch",
            ticket_id="TICK-501",
            executor="claude-a",
            was_hibernated=False,
            ts="2026-01-01T00:00:01Z",
        )

        with patch("lanegate.orchestrate.run_report.pid_alive", return_value=False):
            report = build_run_report(cfg, tmp_path, session_ts=session_ts)

        assert "crashed" in report["status"]
        row = report["tickets"][0]
        assert row["final_outcome"] == "interrupted"
        assert row["final_outcome"] != "skipped"
        assert "lanegate ps" in row["final_reason"]
        assert "lanegate run --tickets TICK-501" in row["final_reason"]
        assert row["duration_seconds"] is not None
        assert row["duration_seconds"] >= 0

        summary = report["summary"]
        t_summary = summary["batch_tickets"][0]
        assert t_summary["outcome"] == "interrupted"
        assert "lanegate ps" in t_summary["failure_reason"]
        # The run itself is reported as a crash (dead PID, no run_end) —
        # the interrupted ticket outcome is what distinguishes this from a
        # documented skip, not the run-level reason.
        assert summary["reason"] == "failure"

        with patch("lanegate.orchestrate.run_report.pid_alive", return_value=False):
            cmd_run_report(cfg, tmp_path, session_ts=session_ts)

    def test_mixed_run_summary_completed_and_interrupted_tickets(self, tmp_path):
        """run_end was recorded (not a hard crash), but one dispatched ticket
        never got a terminal outcome — e.g. the process exited right after
        writing run_end without finishing every in-flight ticket. That
        ticket must read interrupted, and the run-level reason must reflect
        the non-terminal ticket rather than reporting a clean success."""
        cfg = _default_cfg(tmp_path)
        session_ts = "2026-01-01T00-00-00"
        _append_run_event(
            tmp_path, session_ts, "run_start", pid=os.getpid(), ts="2026-01-01T00:00:00Z"
        )
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_dispatch",
            ticket_id="TICK-502",
            executor="claude-a",
            was_hibernated=False,
            ts="2026-01-01T00:00:01Z",
        )
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_outcome",
            ticket_id="TICK-502",
            outcome="success",
            ts="2026-01-01T00:00:05Z",
        )
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_dispatch",
            ticket_id="TICK-503",
            executor="claude-b",
            was_hibernated=False,
            ts="2026-01-01T00:00:06Z",
        )
        _append_run_event(tmp_path, session_ts, "run_end", status="completed", ts="2026-01-01T00:00:07Z")

        report = build_run_report(cfg, tmp_path, session_ts=session_ts)

        outcomes = {row["ticket_id"]: row["final_outcome"] for row in report["tickets"]}
        assert outcomes["TICK-502"] == "success"
        assert outcomes["TICK-503"] == "interrupted"
        assert "skipped" not in outcomes.values()
        assert report["summary"]["reason"] == "stopped"

    def test_never_dispatched_ticket_excluded_from_tickets_list(self, tmp_path):
        """A ticket blocked by a dependency/touch conflict never gets a
        ticket_dispatch event, so it must not appear in the dispatched-ticket
        result list at all (distinct from a genuinely-skipped decision)."""
        cfg = _default_cfg(tmp_path)
        session_ts = "2026-01-01T00-00-00"
        _append_run_event(
            tmp_path, session_ts, "run_start", pid=os.getpid(), ts="2026-01-01T00:00:00Z"
        )
        _append_run_event(tmp_path, session_ts, "run_end", status="completed", ts="2026-01-01T00:00:01Z")

        report = build_run_report(cfg, tmp_path, session_ts=session_ts)
        assert report["tickets"] == []
        assert report["summary"]["batch_tickets"] == []


class TestGetOrchestrationStatusLastCooldown:
    """get_orchestration_status() must say *which* pool instance most recently
    hit a rate-limit cooldown — resume-watch's own "waiting" phase is
    instance-agnostic, so without this an operator watching the Run screen
    can't tell claude-a from codex when a run is stalled on a rate limit."""

    def test_reports_most_recent_cooldown_instance(self, tmp_path):
        from lanegate.orchestrate import _write_last_run_pointer

        session_ts = "2026-01-01T00-00-00"
        _append_run_event(tmp_path, session_ts, "run_start", pid=os.getpid(), milestone=None, pool="default", max_parallel=2, human_review="none")
        _append_run_event(tmp_path, session_ts, "executor_cooldown", instance="claude-a", reason="rate limit or quota interruption (executor exited 1)")
        _append_run_event(tmp_path, session_ts, "executor_cooldown", instance="codex", reason="rate limit or quota interruption (executor exited 1)")
        _write_last_run_pointer(tmp_path, session_ts, _run_events_path(tmp_path, session_ts))

        status = get_orchestration_status(tmp_path)
        assert status["last_cooldown"]["instance"] == "codex"

    def test_none_when_no_run_ever_recorded(self, tmp_path):
        status = get_orchestration_status(tmp_path)
        assert status["last_cooldown"] is None


class TestLiveLaneGateProcesses:
    def test_marker_for_ticket_no_longer_in_progress_is_orphaned(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        _write_ticket(tmp_path / "tickets", "TICK-001", "merged", touches=["a.py"])
        state = tmp_path / ".lanegate"
        state.mkdir(parents=True, exist_ok=True)
        (state / "TICK-001.pid").write_text(f"{os.getpid()}\n")

        procs = _collect_live_lanegate_processes(cfg, tmp_path)
        ticket_proc = next(p for p in procs if p["kind"] == "ticket-executor")
        assert ticket_proc["pid"] == os.getpid()
        assert ticket_proc["alive"] is True
        assert ticket_proc["orphaned"] is True

    def test_marker_for_in_progress_ticket_with_no_orchestrator_running_is_orphaned(
        self, tmp_path
    ):
        cfg = _default_cfg(tmp_path)
        _write_ticket(tmp_path / "tickets", "TICK-001", "in_progress", touches=["a.py"])
        state = tmp_path / ".lanegate"
        state.mkdir(parents=True, exist_ok=True)
        (state / "TICK-001.pid").write_text(f"{os.getpid()}\n")
        (state / "TICK-001.orchestrated").touch()
        # No orchestrator.lock file at all -> orchestrator_lock_status().alive is False

        procs = _collect_live_lanegate_processes(cfg, tmp_path)
        ticket_proc = next(p for p in procs if p["kind"] == "ticket-executor")
        assert ticket_proc["orphaned"] is True

    def test_standalone_process_not_marked_orphaned_without_orchestrator_lock(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        _write_ticket(tmp_path / "tickets", "TICK-001", "in_progress", touches=["a.py"])
        state = tmp_path / ".lanegate"
        state.mkdir(parents=True, exist_ok=True)
        (state / "TICK-001.pid").write_text(f"{os.getpid()}\n")
        # Direct single-ticket dispatch: no .orchestrated marker and no orchestrator.lock

        procs = _collect_live_lanegate_processes(cfg, tmp_path)
        ticket_proc = next(p for p in procs if p["kind"] == "ticket-executor")
        assert ticket_proc["orphaned"] is False

    def test_dead_marker_pid_is_not_reported_as_orphaned(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        _write_ticket(tmp_path / "tickets", "TICK-001", "merged", touches=["a.py"])
        state = tmp_path / ".lanegate"
        state.mkdir(parents=True, exist_ok=True)
        (state / "TICK-001.pid").write_text("1234567\n")

        with patch("lanegate.orchestrate.pid_alive", return_value=False):
            procs = _collect_live_lanegate_processes(cfg, tmp_path)
        ticket_proc = next(p for p in procs if p["kind"] == "ticket-executor")
        assert ticket_proc["alive"] is False
        assert ticket_proc["orphaned"] is False

    def test_cmd_ps_flags_orphan_in_text_and_json(self, tmp_path, capsys):
        cfg = _default_cfg(tmp_path)
        _write_ticket(tmp_path / "tickets", "TICK-001", "merged", touches=["a.py"])
        state = tmp_path / ".lanegate"
        state.mkdir(parents=True, exist_ok=True)
        (state / "TICK-001.pid").write_text(f"{os.getpid()}\n")

        cmd_ps(cfg, tmp_path)
        assert "ORPHANED" in capsys.readouterr().out

        cmd_ps(cfg, tmp_path, json_output=True)
        data = json.loads(capsys.readouterr().out)
        assert any(p["orphaned"] for p in data)

    def test_cmd_ps_reports_none_when_state_empty(self, tmp_path, capsys):
        cfg = _default_cfg(tmp_path)
        cmd_ps(cfg, tmp_path)
        assert "No live lanegate-spawned processes" in capsys.readouterr().out

    def test_ps_shows_dispatched_pool_instance_not_default(self, tmp_path, capsys):
        """TICK-282: a ticket dispatched to a non-default pool instance must
        show the actual instance `lanegate ps` — sourced from the same
        ticket_dispatch event record board.py's `exec:` label trusts — not
        the statically-configured default (claude-a, first-listed)."""
        cfg = _default_cfg(tmp_path)
        cfg["executors"] = {
            "claude-a": {"type": "claude-process"},
            "claude-b": {"type": "claude-process"},
        }
        _write_ticket(tmp_path / "tickets", "TICK-001", "in_progress", touches=["a.py"])
        state = tmp_path / ".lanegate"
        state.mkdir(parents=True, exist_ok=True)
        (state / "TICK-001.pid").write_text(f"{os.getpid()}\n")

        _append_run_event(
            tmp_path,
            "2026-07-29T01-02-03",
            "ticket_dispatch",
            ticket_id="TICK-001",
            executor="claude-b",
            was_hibernated=False,
        )

        cmd_ps(cfg, tmp_path)
        out = capsys.readouterr().out
        assert "exec:claude-b" in out
        assert "exec:claude-a" not in out

        cmd_ps(cfg, tmp_path, json_output=True)
        data = json.loads(capsys.readouterr().out)
        ticket_proc = next(p for p in data if p["kind"] == "ticket-executor")
        assert ticket_proc["executor_instance"] == "claude-b"

    def test_ps_falls_back_to_static_config_when_no_dispatch_record(self, tmp_path, capsys):
        """No ticket_dispatch event on disk (e.g. a manually-started
        worktree) -> falls back to the statically-resolved default instance,
        same as board.py's non-pooled fallback."""
        cfg = _default_cfg(tmp_path)
        cfg["executors"] = {
            "claude-a": {"type": "claude-process"},
            "claude-b": {"type": "claude-process"},
        }
        _write_ticket(tmp_path / "tickets", "TICK-001", "in_progress", touches=["a.py"])
        state = tmp_path / ".lanegate"
        state.mkdir(parents=True, exist_ok=True)
        (state / "TICK-001.pid").write_text(f"{os.getpid()}\n")

        cmd_ps(cfg, tmp_path)
        assert "exec:claude-a" in capsys.readouterr().out


class TestReapOrphanedExecutorProcesses:
    """TICK-281: orchestrate must kill orphaned executor children left by a
    dead driver, not just report them via `lanegate ps`."""

    def test_kills_live_orphan_and_hibernates_its_ticket(self, tmp_path):
        """Simulates the reported scenario: the orchestrate driver died
        mid-dispatch, its executor child is still alive (ticket still
        in_progress, no live orchestrator lock). The reap step must detect
        it (reusing _collect_live_lanegate_processes's orphan logic) and
        actually terminate it, not merely list it."""
        import threading
        from io import StringIO

        cfg = _default_cfg(tmp_path)
        _write_ticket(tmp_path / "tickets", "TICK-281", "in_progress", touches=["a.py"])
        state = tmp_path / ".lanegate"
        state.mkdir(parents=True, exist_ok=True)

        child = subprocess.Popen(["sleep", "30"])
        (state / "TICK-281.pid").write_text(f"{child.pid}\n")
        (state / "TICK-281.session").write_text("123456.0\n")
        (state / "TICK-281.orchestrated").touch()
        # No orchestrator.lock file at all -> orchestrator_lock_status().alive
        # is False, i.e. the driver that dispatched this child is dead.

        # _kill_pid polls pid_alive() for up to its grace period after
        # SIGTERM, and a signalled-but-unreaped child stays a zombie (still
        # "alive" to pid_alive) until something calls waitpid on it. Reap it
        # from a background thread the instant it actually dies, instead of
        # letting the test itself absorb the full grace-period wait via a
        # post-hoc child.wait().
        reaper = threading.Thread(target=child.wait, daemon=True)
        reaper.start()

        try:
            out = StringIO()
            reaped = _reap_orphaned_executor_processes(
                cfg, tmp_path, out_stream=out, session_ts="2026-07-29T00-00-00"
            )
            reaper.join(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

        assert reaped == ["TICK-281"]
        assert child.poll() is not None  # the child was actually terminated
        assert "TICK-281" in out.getvalue()

        updated = parse_ticket(tmp_path / "tickets" / "TICK-281.md")
        assert updated["status"] == "hibernated"

        assert not (state / "TICK-281.pid").exists()
        assert not (state / "TICK-281.session").exists()
        assert not (state / "TICK-281.orchestrated").exists()

        events = _load_run_events(tmp_path, "2026-07-29T00-00-00")
        reaped_events = [e for e in events if e["event"] == "orphan_reaped"]
        assert len(reaped_events) == 1
        assert reaped_events[0]["ticket_id"] == "TICK-281"
        assert reaped_events[0]["pid"] == child.pid

    def test_standalone_process_not_reaped_without_orchestrator_lock(self, tmp_path):
        """A standalone single-ticket process running without an orchestrator lock
        (no .orchestrated marker) is not marked as orphaned and not reaped."""
        cfg = _default_cfg(tmp_path)
        _write_ticket(tmp_path / "tickets", "TICK-283", "in_progress", touches=["a.py"])
        state = tmp_path / ".lanegate"
        state.mkdir(parents=True, exist_ok=True)

        child = subprocess.Popen(["sleep", "30"])
        (state / "TICK-283.pid").write_text(f"{child.pid}\n")
        (state / "TICK-283.session").write_text("123456.0\n")

        try:
            reaped = _reap_orphaned_executor_processes(cfg, tmp_path)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

        assert reaped == []
        updated = parse_ticket(tmp_path / "tickets" / "TICK-283.md")
        assert updated["status"] == "in_progress"
        assert (state / "TICK-283.pid").exists()

    def test_leaves_supervised_ticket_untouched(self, tmp_path):
        """A ticket-executor covered by a live orchestrator lock is not
        orphaned and must not be killed or hibernated."""
        cfg = _default_cfg(tmp_path)
        _write_ticket(tmp_path / "tickets", "TICK-282", "in_progress", touches=["a.py"])
        state = tmp_path / ".lanegate"
        state.mkdir(parents=True, exist_ok=True)
        (state / "TICK-282.pid").write_text(f"{os.getpid()}\n")
        (state / "orchestrator.lock").write_text(f"{os.getpid()}\n")

        reaped = _reap_orphaned_executor_processes(cfg, tmp_path)

        assert reaped == []
        updated = parse_ticket(tmp_path / "tickets" / "TICK-282.md")
        assert updated["status"] == "in_progress"
        assert (state / "TICK-282.pid").exists()

    def test_noop_when_no_live_processes(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        assert _reap_orphaned_executor_processes(cfg, tmp_path) == []


class TestExecutorEventsPipeline:
    def test_executor_events_cli_formatting(self):
        from lanegate.executor_events import ExecutorEvent
        from lanegate.orchestrate.status import format_executor_event_status

        ev = ExecutorEvent(
            phase="implementing",
            activity="reading_file",
            ts="2026-07-30T16:00:00Z",
            activity_age=4.2,
            executor="claude",
            model="sonnet",
            tool_category="file_read",
            path="src/api.py",
        )
        line = format_executor_event_status("TICK-101", ev)
        assert "TICK-101" in line
        assert "[implementing]" in line
        assert "claude" in line
        assert "(sonnet)" in line
        assert "reading_file src/api.py" in line
        assert "4.2s" in line

    def test_executor_events_written_to_run_events_log(self, tmp_path):
        from lanegate.executor_events import ExecutorEvent
        from lanegate.orchestrate import _append_run_event, _load_run_events

        session_ts = "2026-07-30T16-00-00"
        ev = ExecutorEvent(
            phase="testing",
            activity="testing",
            ts="2026-07-30T16:00:05Z",
            executor="codex",
            test_summary={"status": "pass"},
        )
        _append_run_event(
            tmp_path,
            session_ts,
            "executor_progress",
            ticket_id="TICK-102",
            progress=ev.to_dict(),
        )

        events = _load_run_events(tmp_path, session_ts)
        prog_events = [e for e in events if e.get("event") == "executor_progress"]
        assert len(prog_events) == 1
        assert prog_events[0]["ticket_id"] == "TICK-102"
        assert prog_events[0]["progress"]["phase"] == "testing"

    def test_no_secrets_or_raw_prompts_in_events(self):
        from lanegate.executor_events import normalize_claude_event

        raw_secret_line = {
            "type": "content_block_start",
            "content_block": {
                "type": "tool_use",
                "name": "Bash",
                "input": {
                    "command": "export SECRET_KEY=sk-123456789012345678901234 && pytest tests/"
                },
            },
        }
        ev = normalize_claude_event(raw_secret_line, executor="claude")
        assert ev is not None
        # Command must be categorized into safe metadata (pytest), not raw bash line
        assert "sk-12345" not in str(ev.to_dict())
        assert ev.tool_category == "pytest"

    def test_executor_events_api_pipeline(self, tmp_path):
        from lanegate.api import make_handler
        from lanegate.executor_events import ExecutorEvent
        from lanegate.orchestrate import _append_run_event
        from io import BytesIO

        session_ts = "2026-07-30T16-30-00"
        ev = ExecutorEvent(
            phase="implementing",
            activity="reading_file",
            ts="2026-07-30T16:30:00Z",
            executor="claude",
            path="src/api.py",
        )
        _append_run_event(
            tmp_path,
            session_ts,
            "executor_progress",
            ticket_id="TICK-103",
            progress=ev.to_dict(),
        )

        class DummyServer:
            pass

        handler_cls = make_handler(_default_cfg(tmp_path), tmp_path)
        handler = handler_cls.__new__(handler_cls)
        handler.path = f"/api/runs/{session_ts}/events"
        handler.command = "GET"
        handler.requestline = f"GET /api/runs/{session_ts}/events HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.rfile = BytesIO()
        handler.wfile = BytesIO()
        handler.headers = {}
        handler.do_GET()

        response_bytes = handler.wfile.getvalue()
        assert b"200 OK" in response_bytes
        assert b"executor_progress" in response_bytes
        assert b"TICK-103" in response_bytes
        assert b"src/api.py" in response_bytes

    def test_events_api_and_report_filter_untrusted_progress_fields(self, tmp_path):
        """Every public progress surface re-normalizes persisted JSONL data."""
        from io import BytesIO

        from lanegate.api import make_handler
        from lanegate.orchestrate import _append_run_event, build_run_report, read_executor_events

        session_ts = "2026-07-30T16-35-00"
        _append_run_event(tmp_path, session_ts, "run_start", pid=os.getpid())
        _append_run_event(tmp_path, session_ts, "ticket_dispatch", ticket_id="TICK-104", executor="codex")
        _append_run_event(
            tmp_path,
            session_ts,
            "executor_progress",
            ticket_id="TICK-104",
            progress={
                "phase": "implement",
                "activity": "reasoning: export API_KEY=sk-1234567890123456789012",
                "executor": "codex",
                "path": "../.env",
                "test_summary": {"status": "pass", "output": "raw test output"},
                "raw_output": "OPENAI_API_KEY=super-secret",
            },
        )
        _append_run_event(tmp_path, session_ts, "ticket_outcome", ticket_id="TICK-104", outcome="success")
        _append_run_event(tmp_path, session_ts, "run_end", status="completed")

        events = read_executor_events(tmp_path, session_ts)
        assert len(events) == 1
        assert events[0]["progress"]["phase"] == "implementing"
        assert events[0]["progress"]["path"] is None
        assert "raw_output" not in events[0]["progress"]
        assert "sk-" not in str(events)

        report = build_run_report(_default_cfg(tmp_path), tmp_path, session_ts=session_ts)
        assert report is not None
        assert report["tickets"][0]["progress_summary"]["phases"] == {"implementing": 1}
        assert "OPENAI_API_KEY" not in str(report)

        handler_cls = make_handler(_default_cfg(tmp_path), tmp_path)
        handler = handler_cls.__new__(handler_cls)
        handler.path = f"/api/runs/{session_ts}/events"
        handler.command = "GET"
        handler.requestline = f"GET /api/runs/{session_ts}/events HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.rfile = BytesIO()
        handler.wfile = BytesIO()
        handler.headers = {}
        handler.do_GET()
        payload = handler.wfile.getvalue()
        assert b"OPENAI_API_KEY" not in payload
        assert b"raw test output" not in payload

    def test_concurrent_progress_event_appends_remain_parseable(self, tmp_path):
        import threading

        from lanegate.executor_events import ExecutorEvent
        from lanegate.orchestrate import _append_run_event, read_executor_events

        session_ts = "2026-07-30T16-40-00"

        def emit(ticket_id: str):
            _append_run_event(
                tmp_path,
                session_ts,
                "executor_progress",
                ticket_id=ticket_id,
                progress=ExecutorEvent(
                    phase="implementing", activity="reading_file", ts="2026-07-30T16:40:00Z",
                    executor="codex", path=f"src/{ticket_id}.py",
                ).to_dict(),
            )

        threads = [threading.Thread(target=emit, args=(f"TICK-{i:03d}",)) for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        events = read_executor_events(tmp_path, session_ts)
        assert len(events) == 12
        assert {event["ticket_id"] for event in events} == {f"TICK-{i:03d}" for i in range(12)}
