"""Tests for lanegate/api.py — loopback HTTP server (TICK-107 endpoints)."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import threading
import time
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lanegate.api import LaneGateApiServer, make_handler


# ── shared fixtures ───────────────────────────────────────────────────────────

_BASE_CFG = {
    "ticket_prefix": "TICK",
    "tickets_dir": "tickets",
    "lock_statuses": ["in_progress", "code_complete", "in_review"],
    "environments": [],
}


class FakeProc:
    def __init__(self, pid: int = 12345, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode

    def poll(self):
        return self.returncode


def _make_ticket(tickets_dir: Path, ticket_id: str, status: str, touches=None) -> None:
    frontmatter = (
        f"id: {ticket_id}\ntitle: Test {ticket_id}\nstatus: {status}\n"
        "priority: 5\nparallel_safe: true\n"
    )
    if touches:
        frontmatter += "touches:\n" + "".join(f"  - {t}\n" for t in touches)
    (tickets_dir / f"{ticket_id}.md").write_text(f"---\n{frontmatter}---\nBody.\n")


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    td = tmp_path / "tickets"
    td.mkdir()
    _make_ticket(td, "TICK-001", "open", touches=["src/foo.py"])
    _make_ticket(td, "TICK-002", "in_progress", touches=["src/bar.py"])
    return tmp_path


def _wait_for_port(port: int, timeout: float = 0.5) -> None:
    import socket
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.01):
                return
        except OSError:
            time.sleep(0.001)


@pytest.fixture
def api_server(repo_root: Path):
    """Start API server on a free port; yield (host, port); stop after test."""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    server = LaneGateApiServer(_BASE_CFG, repo_root, port=port)
    server.start()
    _wait_for_port(port)
    yield "127.0.0.1", port
    server.stop()


def _get(host: str, port: int, path: str) -> tuple[int, dict | str]:
    conn = HTTPConnection(host, port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read().decode()
    conn.close()
    try:
        return resp.status, json.loads(body)
    except json.JSONDecodeError:
        return resp.status, body


def _post(host: str, port: int, path: str, payload: dict | None = None) -> tuple[int, dict | str]:
    conn = HTTPConnection(host, port, timeout=5)
    body = json.dumps(payload or {}).encode()
    conn.request("POST", path, body=body, headers={"Content-Type": "application/json", "Content-Length": str(len(body))})
    resp = conn.getresponse()
    raw = resp.read().decode()
    conn.close()
    try:
        return resp.status, json.loads(raw)
    except json.JSONDecodeError:
        return resp.status, raw


def _put(host: str, port: int, path: str, payload: dict | None = None) -> tuple[int, dict | str]:
    conn = HTTPConnection(host, port, timeout=5)
    body = json.dumps(payload or {}).encode()
    conn.request("PUT", path, body=body, headers={"Content-Type": "application/json", "Content-Length": str(len(body))})
    resp = conn.getresponse()
    raw = resp.read().decode()
    conn.close()
    try:
        return resp.status, json.loads(raw)
    except json.JSONDecodeError:
        return resp.status, raw


# ── GET /api/board ────────────────────────────────────────────────────────────

def test_get_board_returns_200(api_server):
    host, port = api_server
    status, data = _get(host, port, "/api/board")
    assert status == 200


def test_get_board_contains_tickets_and_pipeline(api_server):
    host, port = api_server
    _, data = _get(host, port, "/api/board")
    assert "tickets" in data
    assert "pipeline" in data


def test_get_board_shows_open_ticket(api_server):
    host, port = api_server
    _, data = _get(host, port, "/api/board")
    all_ids = [t["id"] for group in data["tickets"].values() for t in group]
    assert "TICK-001" in all_ids


# ── GET /api/tickets ──────────────────────────────────────────────────────────

def test_get_tickets_returns_200(api_server):
    host, port = api_server
    status, data = _get(host, port, "/api/tickets")
    assert status == 200


def test_get_tickets_has_tickets_key(api_server):
    host, port = api_server
    _, data = _get(host, port, "/api/tickets")
    assert "tickets" in data
    assert isinstance(data["tickets"], list)


def test_get_tickets_contains_both_tickets(api_server):
    host, port = api_server
    _, data = _get(host, port, "/api/tickets")
    ids = {t["id"] for t in data["tickets"]}
    assert "TICK-001" in ids
    assert "TICK-002" in ids


# ── GET /api/diff/{ticket_id} ─────────────────────────────────────────────────

def test_get_diff_returns_json(api_server, repo_root):
    host, port = api_server
    # get_ticket_diff runs git; patch it so the test is self-contained
    fake = {
        "id": "TICK-001",
        "ticket_id": "TICK-001",
        "branch": "tick-001",
        "base": "main",
        "stat": "1 file changed",
        "files": [
            {
                "path": "src/foo.py",
                "status": "M",
                "patch": "--- a/src/foo.py\n+++ b/src/foo.py\n@@ -1 +1 @@\n-old\n+new\n",
                "truncated": False,
            }
        ],
        "diff": "--- a/src/foo.py\n+++ b/src/foo.py\n@@ -1 +1 @@\n-old\n+new\n",
        "truncated": False,
        "error": None,
    }
    with patch("lanegate.ticket.get_ticket_diff", return_value=fake):
        status, data = _get(host, port, "/api/diff/TICK-001")
    assert status == 200
    assert data["id"] == "TICK-001"
    assert data["files"][0]["path"] == "src/foo.py"


def test_get_diff_missing_id_returns_404(api_server):
    host, port = api_server
    status, _ = _get(host, port, "/api/diff/")
    assert status == 404


# ── GET /api/status ───────────────────────────────────────────────────────────

def test_get_status_returns_200(api_server, repo_root):
    host, port = api_server
    status, data = _get(host, port, "/api/status")
    assert status == 200
    assert "active" in data


def test_get_status_inactive_when_no_run(api_server, repo_root):
    host, port = api_server
    _, data = _get(host, port, "/api/status")
    # No orchestration started → active should be False
    assert data["active"] is False


# ── POST /api/orchestrate/start ───────────────────────────────────────────────

def test_orchestrate_start_returns_started(api_server, repo_root):
    host, port = api_server
    fake_proc = FakeProc(pid=12345)
    with patch("subprocess.Popen", return_value=fake_proc) as mock_popen:
        status, data = _post(host, port, "/api/orchestrate/start", {})
    assert status == 200
    assert data["run_id"].startswith("run-")
    assert data["status"] == "running"
    assert data["orchestrator_pid"] == 12345
    mock_popen.assert_called_once()
    assert mock_popen.call_args[0][0][:3] == [sys.executable, "-m", "lanegate.cli"]


def test_orchestrate_start_passes_milestone(api_server, repo_root):
    host, port = api_server
    fake_proc = FakeProc(pid=99)
    with patch("subprocess.Popen", return_value=fake_proc) as mock_popen:
        _post(host, port, "/api/orchestrate/start", {"milestone": "v2"})
    call_args = mock_popen.call_args[0][0]
    assert "--milestone" in call_args
    assert "v2" in call_args


def test_orchestrate_start_passes_max_parallel(api_server, repo_root):
    host, port = api_server
    fake_proc = FakeProc(pid=99)
    with patch("subprocess.Popen", return_value=fake_proc) as mock_popen:
        _post(host, port, "/api/orchestrate/start", {"max_parallel": 3})
    call_args = mock_popen.call_args[0][0]
    assert "--max" in call_args
    assert "3" in call_args


def test_orchestrate_start_passes_human_review(api_server, repo_root):
    host, port = api_server
    fake_proc = FakeProc(pid=99)
    with patch("subprocess.Popen", return_value=fake_proc) as mock_popen:
        _post(host, port, "/api/orchestrate/start", {"human_review": "per_ticket"})
    call_args = mock_popen.call_args[0][0]
    assert "--human-review" in call_args
    assert "per_ticket" in call_args


def test_current_run_reports_cli_started_run_not_tracked_by_api(api_server, repo_root):
    """An `orchestrate` process started outside this API instance (e.g. from
    the CLI) has no `.lanegate/api-run-current.json` for `_read_run_state` to
    find, but its on-disk executor marker still makes `get_orchestration_status`
    report active — the Run screen must reflect that instead of always
    showing idle."""
    host, port = api_server
    active_status = {
        "active": True,
        "state": "running",
        "reconciliation_state": "live",
        "executor_pid": 55555,
        "executor_session": "TICK-001-1700000000-1-implement",
        "ticket_id": "TICK-001",
        "started_at_iso": "2026-07-29T17:50:05Z",
        "heartbeat_count": 4,
        "orchestrator_lock": {"held": True, "pid": 12321, "alive": True},
    }
    with patch("lanegate.orchestrate.get_orchestration_status", return_value=active_status):
        status, data = _get(host, port, "/api/runs/current")

    assert status == 200
    assert data["run_id"] == "TICK-001-1700000000-1-implement"
    assert data["status"] == "running"
    assert data["process_alive"] is True
    assert data["tickets"] == ["TICK-001"]
    assert data["workers"][0]["ticket_id"] == "TICK-001"
    assert data["orchestrator_pid"] == 12321


def test_current_run_reports_api_started_run(api_server, repo_root):
    host, port = api_server
    fake_proc = FakeProc(pid=777)
    with patch("subprocess.Popen", return_value=fake_proc):
        _, started = _post(host, port, "/api/orchestrate/start", {"dry_run": True})

    status, data = _get(host, port, "/api/runs/current")
    assert status == 200
    assert data["run_id"] == started["run_id"]
    assert data["status"] == "running"
    assert data["orchestrator_pid"] == 777


def test_runs_list_and_detail_endpoints(api_server, repo_root):
    host, port = api_server
    mock_summary = MagicMock()
    mock_summary.to_dict.return_value = {
        "run_id": "20260728T101500Z-abc12345",
        "timestamp": "2026-07-28T10:15:00+00:00",
        "reason": "success",
        "batch_tickets": [
            {
                "ticket_id": "TICK-150",
                "executor": "codex-a",
                "outcome": "success",
                "duration_seconds": 45.0,
                "failure_reason": None,
                "review_reason": None,
            }
        ],
    }

    with (
        patch("lanegate.orchestrate.run_summary.list_run_summaries", return_value=[mock_summary]),
        patch("lanegate.orchestrate.run_summary.build_run_summary", return_value=mock_summary),
    ):
        status, data = _get(host, port, "/api/runs")
        assert status == 200
        assert "runs" in data
        assert len(data["runs"]) == 1
        assert data["runs"][0]["run_id"] == "20260728T101500Z-abc12345"

        status_det, data_det = _get(host, port, "/api/runs/20260728T101500Z-abc12345")
        assert status_det == 200
        assert data_det["run_id"] == "20260728T101500Z-abc12345"
        assert data_det["reason"] == "success"
        assert len(data_det["batch_tickets"]) == 1


def test_run_summary_reports_dispatched_ticket_without_outcome_as_interrupted(
    api_server, repo_root
):
    """A ticket dispatched by a run whose orchestrator process is gone, with
    no ticket_outcome ever recorded, must surface as "interrupted" over the
    API — never "skipped" (reserved for a documented non-dispatch decision)
    and never silently mapped to "failure" (TICK-325)."""
    from lanegate.orchestrate.run_report import _append_run_event

    host, port = api_server
    session_ts = "2026-07-31T09-30-00"
    _append_run_event(repo_root, session_ts, "run_start", pid=99999999, ts="2026-07-31T09:30:00Z")
    _append_run_event(
        repo_root,
        session_ts,
        "ticket_dispatch",
        ticket_id="TICK-700",
        executor="claude-a",
        was_hibernated=False,
        ts="2026-07-31T09:30:01Z",
    )

    with patch("lanegate.orchestrate.run_report.pid_alive", return_value=False):
        status, data = _get(host, port, f"/api/runs/{session_ts}")

    assert status == 200
    assert len(data["batch_tickets"]) == 1
    t = data["batch_tickets"][0]
    assert t["outcome"] == "interrupted"
    assert t["outcome"] != "skipped"
    assert t["outcome"] != "failure"
    assert "lanegate ps" in t["failure_reason"]


# ── POST /api/orchestrate/stop ────────────────────────────────────────────────

def test_orchestrate_stop_no_active_run(api_server, repo_root):
    host, port = api_server
    status, data = _post(host, port, "/api/orchestrate/stop", {})
    assert status == 200
    assert data["stop_requested"] is False
    assert data["status"] == "idle"
    assert "reason" in data


def test_orchestrate_stop_signals_orchestrator_not_executor(api_server, repo_root):
    host, port = api_server
    active_status = {
        "active": True,
        "state": "running",
        "reconciliation_state": "live",
        "executor_pid": 55555,
        "ticket_id": "TICK-001",
    }
    fake_proc = FakeProc(pid=44444)
    with (
        patch("subprocess.Popen", return_value=fake_proc),
        patch("lanegate.orchestrate.get_orchestration_status", return_value=active_status),
        patch("os.kill") as mock_kill,
    ):
        _, started = _post(host, port, "/api/orchestrate/start", {})
        with patch("lanegate.api.pid_alive", return_value=True):
            status, data = _post(host, port, "/api/orchestrate/stop", {})
    assert status == 200
    assert data["run_id"] == started["run_id"]
    assert data["stop_requested"] is True
    assert data["orchestrator_pid"] == 44444
    mock_kill.assert_called_once_with(44444, signal.SIGTERM)


# ── GET /api/log (SSE) ────────────────────────────────────────────────────────

def _read_sse_response(host: str, port: int) -> tuple[int, str, str]:
    """Read headers + body from an SSE endpoint without blocking forever."""
    import socket

    raw = (
        "GET /api/log HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode()

    s = socket.create_connection((host, port), timeout=5)
    s.sendall(raw)
    chunks: list[bytes] = []
    s.settimeout(2)
    try:
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    except TimeoutError:
        pass
    finally:
        s.close()

    full = b"".join(chunks).decode(errors="replace")
    # Split into header block and body
    header_end = full.find("\r\n\r\n")
    if header_end == -1:
        return 0, "", full
    header_block = full[:header_end]
    body = full[header_end + 4:]
    status_line = header_block.splitlines()[0]
    try:
        code = int(status_line.split()[1])
    except (IndexError, ValueError):
        code = 0
    return code, header_block, body


def test_log_streaming_returns_event_stream_content_type(api_server, repo_root):
    host, port = api_server
    code, headers, _ = _read_sse_response(host, port)
    assert code == 200
    assert "text/event-stream" in headers


def test_log_streaming_emits_sse_events(api_server, repo_root):
    host, port = api_server
    # Pre-seed the watch log so we have lines to stream
    log_path = repo_root / ".lanegate" / "watch.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("[watch] started (PID 1)\napi_key=sk-1234567890123456789012\n")

    code, _, body = _read_sse_response(host, port)
    assert code == 200
    assert "data:" in body
    assert "event: log" in body
    assert "[watch] started" in body
    assert "sk-" not in body
    assert "[REDACTED]" in body


def test_current_run_log_stream_alias(api_server, repo_root):
    host, port = api_server
    log_path = repo_root / ".lanegate" / "watch.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("api_key=sk-1234567890123456789012\n")

    conn = HTTPConnection(host, port, timeout=5)
    conn.request("GET", "/api/runs/current/logs/stream?follow=0")
    resp = conn.getresponse()
    body = resp.read().decode()
    conn.close()

    assert resp.status == 200
    assert "text/event-stream" in resp.getheader("Content-Type")
    assert "sk-" not in body
    assert "[REDACTED]" in body


def test_log_streaming_empty_when_no_log(api_server, repo_root):
    host, port = api_server
    # No watch.log exists → empty stream body, still 200
    code, headers, _ = _read_sse_response(host, port)
    assert code == 200


# ── GET /api/runs/current/logs (paginated) ────────────────────────────────────

def test_orchestrate_run_logs_pagination(api_server, repo_root):
    host, port = api_server
    log_path = repo_root / ".lanegate" / "watch.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("".join(f"line-{i}\n" for i in range(10)))

    status, data = _get(host, port, "/api/runs/current/logs?offset=2&limit=3")
    assert status == 200
    assert data["offset"] == 2
    assert data["limit"] == 3
    assert data["total_count"] == 10
    assert data["next_offset"] == 5
    assert [ev["message"] for ev in data["events"]] == ["line-2", "line-3", "line-4"]
    assert [ev["id"] for ev in data["events"]] == ["3", "4", "5"]


def test_current_run_log_page_redacts_secret_text(api_server, repo_root):
    host, port = api_server
    log_path = repo_root / ".lanegate" / "watch.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("ordinary line\napi_key=sk-1234567890123456789012\n")

    status, data = _get(host, port, "/api/runs/current/logs?offset=0&limit=2")

    assert status == 200
    assert data["total_count"] == 2
    messages = [event["message"] for event in data["events"]]
    assert messages[0] == "ordinary line"
    assert "sk-" not in messages[1]
    assert "[REDACTED]" in messages[1]


def test_orchestrate_run_logs_pagination_last_page_has_no_next_offset(api_server, repo_root):
    host, port = api_server
    log_path = repo_root / ".lanegate" / "watch.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("".join(f"line-{i}\n" for i in range(10)))

    status, data = _get(host, port, "/api/runs/current/logs?offset=8&limit=5")
    assert status == 200
    assert data["total_count"] == 10
    assert data["next_offset"] is None
    assert [ev["message"] for ev in data["events"]] == ["line-8", "line-9"]


def test_run_logs_1000_events(api_server, repo_root):
    host, port = api_server
    log_path = repo_root / ".lanegate" / "watch.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("".join(f"line-{i}\n" for i in range(1000)))

    status, first_page = _get(host, port, "/api/runs/current/logs?offset=0&limit=200")
    assert status == 200
    assert first_page["total_count"] == 1000
    assert first_page["next_offset"] == 200
    assert first_page["events"][0]["message"] == "line-0"

    status, last_page = _get(host, port, "/api/runs/current/logs?offset=800&limit=200")
    assert status == 200
    assert last_page["next_offset"] is None
    assert len(last_page["events"]) == 200
    assert last_page["events"][-1]["message"] == "line-999"


def test_run_logs_not_found(api_server):
    host, port = api_server
    # No watch.log and no orchestrate log written → no resolvable log file
    status, data = _get(host, port, "/api/runs/current/logs")
    assert status == 404
    assert "error" in data


def test_run_logs_error_handling(api_server, repo_root):
    host, port = api_server
    log_path = repo_root / ".lanegate" / "watch.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("line-0\n")

    status, data = _get(host, port, "/api/runs/current/logs?offset=abc&limit=10")
    assert status == 400
    assert "error" in data

    status, data = _get(host, port, "/api/runs/current/logs?offset=0&limit=0")
    assert status == 400
    assert "error" in data

    status, data = _get(host, port, "/api/runs/current/logs?offset=-1&limit=10")
    assert status == 400
    assert "error" in data


# ── GET /api/tickets/{id} - Ticket detail endpoint ────────────────────────────

def test_get_ticket_detail_returns_200(api_server, repo_root):
    host, port = api_server
    status, _ = _get(host, port, "/api/tickets/TICK-001")
    assert status == 200


def test_get_ticket_detail_missing_ticket_returns_404(api_server):
    host, port = api_server
    status, payload = _get(host, port, "/api/tickets/TICK-999")
    assert status == 404
    assert "error" in payload


def test_get_ticket_detail_json_structure(api_server, repo_root):
    host, port = api_server
    _, payload = _get(host, port, "/api/tickets/TICK-001")
    # Should include ticket fields
    assert "id" in payload
    assert "title" in payload
    assert "status" in payload
    assert "body" in payload
    assert "close_criteria" in payload


def test_get_ticket_detail_includes_review_driver_fields(api_server, repo_root):
    host, port = api_server
    td = repo_root / "tickets"
    p = td / "TICK-308.md"
    p.write_text(
        "---\n"
        "id: TICK-308\n"
        "title: Test review fields\n"
        "status: in_review\n"
        "review_driver: codex\n"
        "review_verdict: approved\n"
        "review_summary: All checks pass\n"
        "reviewed_at: 2026-07-30T22:00:00Z\n"
        "---\n"
        "Body.\n"
    )
    status, payload = _get(host, port, "/api/tickets/TICK-308")
    assert status == 200
    assert payload["id"] == "TICK-308"
    assert payload.get("review_driver") == "codex"
    assert payload.get("review_verdict") == "approved"
    assert payload.get("reviewed_at") in ("2026-07-30T22:00:00Z", "2026-07-30T22:00:00+00:00")


# ── GET /api/blocked - Blocked review queue endpoint ────────────────────────────

def test_get_blocked_returns_200(api_server):
    host, port = api_server
    status, _ = _get(host, port, "/api/blocked")
    assert status == 200


def test_get_blocked_json_structure(api_server):
    host, port = api_server
    _, payload = _get(host, port, "/api/blocked")
    # Should include blocked array (may be empty)
    assert "blocked" in payload
    assert isinstance(payload["blocked"], list)


def test_get_blocked_empty_queue(api_server):
    host, port = api_server
    _, payload = _get(host, port, "/api/blocked")
    # When no blocked tickets exist, should be empty array
    assert payload["blocked"] == []


# ── GET /api/config - Sanitized settings endpoint ───────────────────────────

def test_get_config_returns_sanitized_settings(api_server, repo_root):
    host, port = api_server
    status, data = _get(host, port, "/api/config")
    assert status == 200
    assert data["repo_root"] == str(repo_root)
    assert data["ticket_prefix"] == "TICK"
    assert data["tickets_dir"] == "tickets"
    assert data["api"]["host"] == "127.0.0.1"
    assert data["api"]["port"] == port


def test_get_config_redacts_executor_secrets(repo_root):
    import socket

    cfg = dict(_BASE_CFG)
    cfg["executors"] = {
        "claude-1": {
            "type": "claude-process",
            "api_key_env": "ANTHROPIC_API_KEY_1",
            "api_key": "sk-should-not-leak",
            "max_parallel": 2,
        }
    }

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]

    server = LaneGateApiServer(cfg, repo_root, port=free_port)
    server.start()
    _wait_for_port(free_port)
    try:
        status, data = _get("127.0.0.1", free_port, "/api/config")
    finally:
        server.stop()

    assert status == 200
    executor_entry = data["executors"]["claude-1"]
    assert executor_entry["api_key_env"] == "ANTHROPIC_API_KEY_1"
    assert executor_entry["api_key"] == "[redacted]"
    assert "sk-should-not-leak" not in json.dumps(data)


def test_get_config_settings_alias(api_server):
    host, port = api_server
    status, data = _get(host, port, "/api/settings")
    assert status == 200
    assert "repo_root" in data


# ── GET /api/pools + PUT /api/pools/{name}/executors (TICK-269) ─────────────

@pytest.fixture
def pools_repo_root(repo_root: Path) -> Path:
    """repo_root plus an on-disk .lanegate.yml with a pools: block, so
    update_pool_executor_order (which round-trips the real config file) has
    something to persist to."""
    (repo_root / ".lanegate.yml").write_text(
        """
ticket_prefix: TICK
tickets_dir: tickets
executors:
  claude-1: { type: claude-process }
  claude-2: { type: claude-process }
pools:
  default:
    executors: [claude-1, claude-2]
    strategy: least-loaded
default_pool: default
"""
    )
    return repo_root


@pytest.fixture
def pools_api_server(pools_repo_root: Path):
    import socket

    from lanegate.config import load_config

    cfg = load_config(pools_repo_root)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    server = LaneGateApiServer(cfg, pools_repo_root, port=port)
    server.start()
    _wait_for_port(port)
    yield "127.0.0.1", port
    server.stop()


def test_get_pools_returns_200(pools_api_server):
    host, port = pools_api_server
    status, data = _get(host, port, "/api/pools")
    assert status == 200


def test_get_pools_reports_current_executor_order(pools_api_server):
    host, port = pools_api_server
    status, data = _get(host, port, "/api/pools")
    assert status == 200
    pools = {p["name"]: p for p in data["pools"]}
    assert pools["default"]["executors"] == ["claude-1", "claude-2"]
    assert pools["default"]["strategy"] == "least-loaded"
    assert pools["default"]["default"] is True


def test_put_pool_executors_reorders_and_persists(pools_api_server, pools_repo_root):
    host, port = pools_api_server
    status, data = _put(
        host, port, "/api/pools/default/executors", {"executors": ["claude-2", "claude-1"]}
    )
    assert status == 200
    assert data["executors"] == ["claude-2", "claude-1"]

    # Persisted to .lanegate.yml on disk...
    from lanegate.config import load_config

    cfg = load_config(pools_repo_root)
    assert cfg["pools"]["default"]["executors"] == ["claude-2", "claude-1"]

    # ...and reflected immediately in a subsequent GET against the same
    # running server (no restart required).
    status, data = _get(host, port, "/api/pools")
    pools = {p["name"]: p for p in data["pools"]}
    assert pools["default"]["executors"] == ["claude-2", "claude-1"]


def test_put_pool_executors_unknown_pool_returns_400(pools_api_server):
    host, port = pools_api_server
    status, data = _put(
        host, port, "/api/pools/nonexistent/executors", {"executors": ["claude-1"]}
    )
    assert status == 400


def test_put_pool_executors_non_reordering_returns_400(pools_api_server):
    host, port = pools_api_server
    status, data = _put(
        host, port, "/api/pools/default/executors", {"executors": ["claude-1"]}
    )
    assert status == 400


def test_put_pool_executors_missing_body_field_returns_400(pools_api_server):
    host, port = pools_api_server
    status, data = _put(host, port, "/api/pools/default/executors", {})
    assert status == 400


# ── GET /api/runs/current — resume_watch_status field ────────────────────────

def test_current_run_resume_watch_status_absent(api_server, repo_root):
    """No daemon running → resume_watch_status is null."""
    host, port = api_server
    with patch("lanegate.api._get_resume_watch_status", return_value=None):
        status, data = _get(host, port, "/api/runs/current")
    assert status == 200
    assert data["resume_watch_status"] is None


def test_current_run_resume_watch_status_waiting(api_server, repo_root):
    """Daemon alive, initial wait → phase == 'waiting'."""
    host, port = api_server
    rws = {"phase": "waiting", "elapsed_time": 120.0, "next_retry_eta": None}
    with patch("lanegate.api._get_resume_watch_status", return_value=rws):
        status, data = _get(host, port, "/api/runs/current")
    assert status == 200
    assert data["resume_watch_status"]["phase"] == "waiting"
    assert data["resume_watch_status"]["elapsed_time"] == 120.0
    assert data["resume_watch_status"]["next_retry_eta"] is None


def test_current_run_resume_watch_status_retrying(api_server, repo_root):
    """Daemon alive, running orchestrate → phase == 'retrying'."""
    host, port = api_server
    rws = {"phase": "retrying", "elapsed_time": 305.0, "next_retry_eta": None}
    with patch("lanegate.api._get_resume_watch_status", return_value=rws):
        status, data = _get(host, port, "/api/runs/current")
    assert status == 200
    assert data["resume_watch_status"]["phase"] == "retrying"
    assert data["resume_watch_status"]["elapsed_time"] == 305.0


def test_current_run_resume_watch_status_gave_up(api_server, repo_root):
    """Daemon exited after ceiling → phase == 'gave_up'."""
    host, port = api_server
    rws = {"phase": "gave_up", "elapsed_time": 7200.0, "next_retry_eta": None}
    with patch("lanegate.api._get_resume_watch_status", return_value=rws):
        status, data = _get(host, port, "/api/runs/current")
    assert status == 200
    assert data["resume_watch_status"]["phase"] == "gave_up"
    assert data["resume_watch_status"]["elapsed_time"] == 7200.0


# ── 404 for unknown routes ────────────────────────────────────────────────────

def test_unknown_route_returns_404(api_server):
    host, port = api_server
    status, _ = _get(host, port, "/api/unknown")
    assert status == 404


def test_api_responses_do_not_advertise_wildcard_cors(api_server):
    host, port = api_server
    for method, path in (("GET", "/api/board"), ("GET", "/api/log"), ("OPTIONS", "/api/board")):
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(method, path)
        response = conn.getresponse()
        response.read()
        conn.close()
        assert response.getheader("Access-Control-Allow-Origin") is None


# ── GET /api/runs/{run_id}/logs — paginated activity history ─────────────────

def test_historical_run_logs_pagination(api_server, repo_root):
    host, port = api_server
    session_ts = "20260730T120000Z"
    events_dir = repo_root / ".lanegate" / "logs"
    events_dir.mkdir(parents=True, exist_ok=True)
    events_path = events_dir / f"orchestrate-{session_ts}.events.jsonl"
    lines = [json.dumps({"ts": "2026-07-30T12:00:00Z", "event": "test", "index": i}) for i in range(10)]
    events_path.write_text("\n".join(lines) + "\n")

    status, data = _get(host, port, f"/api/orchestrate/{session_ts}/logs?offset=0&limit=5")
    assert status == 200
    assert data["run_id"] == session_ts
    assert data["total_count"] == 10
    assert len(data["events"]) == 5
    assert data["offset"] == 0
    assert data["limit"] == 5
    assert data["next_offset"] == 5
    assert data["events"][0]["index"] == 0

    status2, data2 = _get(host, port, f"/api/orchestrate/{session_ts}/logs?offset=5&limit=5")
    assert status2 == 200
    assert len(data2["events"]) == 5
    assert data2["next_offset"] is None
    assert data2["events"][0]["index"] == 5


def test_historical_run_logs_1000_events(api_server, repo_root):
    host, port = api_server
    session_ts = "20260730T130000Z"
    events_dir = repo_root / ".lanegate" / "logs"
    events_dir.mkdir(parents=True, exist_ok=True)
    events_path = events_dir / f"orchestrate-{session_ts}.events.jsonl"
    lines = [json.dumps({"ts": "2026-07-30T13:00:00Z", "event": "log", "line": i}) for i in range(1050)]
    events_path.write_text("\n".join(lines) + "\n")

    status, data = _get(host, port, f"/api/runs/{session_ts}/logs?offset=0&limit=500")
    assert status == 200
    assert data["total_count"] == 1050
    assert len(data["events"]) == 500
    assert data["next_offset"] == 500


def test_historical_run_logs_not_found(api_server):
    host, port = api_server
    status, data = _get(host, port, "/api/runs/nonexistent_run_9999/logs")
    assert status == 404
    assert "error" in data


def test_historical_run_logs_error_handling(api_server, repo_root):
    host, port = api_server
    status, data = _get(host, port, "/api/runs/current/logs?offset=invalid")
    assert status == 400
    assert "error" in data


def test_historical_run_logs_redact_raw_transcript(api_server, repo_root):
    host, port = api_server
    session_ts = "20260730T140000Z"
    logs_dir = repo_root / ".lanegate" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / f"orchestrate-{session_ts}.log").write_text(
        "ordinary line\napi_key=sk-1234567890123456789012\n"
    )

    status, data = _get(host, port, f"/api/runs/{session_ts}/logs?offset=0&limit=2")

    assert status == 200
    assert data["total_count"] == 2
    messages = [event["message"] for event in data["events"]]
    assert messages[0] == "ordinary line"
    assert "sk-" not in messages[1]
    assert "[REDACTED]" in messages[1]
