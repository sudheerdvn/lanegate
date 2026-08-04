"""Tests for lanegate/tui.py — the Python launcher for the Go TUI."""

from __future__ import annotations

import http.server
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lanegate.tui import TuiLaunchError, _wait_for_api_ready, cmd_tui


class FakeProc:
    """Stand-in for subprocess.Popen tracking terminate/kill/wait calls."""

    def __init__(self, pid: int = 4242, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.wait_calls += 1
        return self.returncode


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _OKHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def test_wait_for_api_ready_returns_once_server_answers():
    port = _free_port()
    server = http.server.HTTPServer(("127.0.0.1", port), _OKHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        proc = FakeProc()
        _wait_for_api_ready(proc, port)  # must not raise
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_wait_for_api_ready_raises_if_process_exits_early():
    proc = FakeProc(returncode=1)
    with pytest.raises(TuiLaunchError, match="exited"):
        _wait_for_api_ready(proc, _free_port())


def test_wait_for_api_ready_times_out_when_nothing_listens():
    proc = FakeProc()
    with (
        patch("lanegate.tui._API_READY_TIMEOUT_S", 0.2),
        patch("lanegate.tui._API_READY_POLL_INTERVAL_S", 0.05),
        pytest.raises(TuiLaunchError, match="timed out"),
    ):
        _wait_for_api_ready(proc, _free_port())
    assert proc.terminated


def test_default_launch_spawns_api_subprocess_on_selected_port_and_tears_it_down(tmp_path):
    fake_api_proc = FakeProc()
    with (
        patch("lanegate.tui._go_tui_command", return_value=(["lanegate-tui"], None)),
        patch("lanegate.tui._select_loopback_port", return_value=54321),
        patch("subprocess.Popen", return_value=fake_api_proc) as mock_popen,
        patch("lanegate.tui._wait_for_api_ready"),
        patch("subprocess.run") as mock_run,
    ):
        cmd_tui(tmp_path)

    popen_args = mock_popen.call_args[0][0]
    assert popen_args[-3:] == ["api", "--port", "54321"]
    mock_run.assert_called_once()
    tui_argv = mock_run.call_args[0][0]
    assert "--api-url" in tui_argv
    assert tui_argv[tui_argv.index("--api-url") + 1] == "http://127.0.0.1:54321"
    assert fake_api_proc.terminated
    assert fake_api_proc.wait_calls == 1


def test_default_launch_kills_api_subprocess_if_terminate_does_not_stop_it(tmp_path):
    fake_api_proc = FakeProc()

    def slow_wait(timeout=None):
        fake_api_proc.wait_calls += 1
        if fake_api_proc.wait_calls == 1:
            raise __import__("subprocess").TimeoutExpired(cmd="lanegate api", timeout=timeout or 0)
        return fake_api_proc.returncode

    fake_api_proc.wait = slow_wait
    with (
        patch("lanegate.tui._go_tui_command", return_value=(["lanegate-tui"], None)),
        patch("lanegate.tui._select_loopback_port", return_value=54322),
        patch("subprocess.Popen", return_value=fake_api_proc),
        patch("lanegate.tui._wait_for_api_ready"),
        patch("subprocess.run"),
    ):
        cmd_tui(tmp_path)

    assert fake_api_proc.terminated
    assert fake_api_proc.killed


def test_default_launch_reports_launch_error_when_api_never_ready(tmp_path):
    fake_api_proc = FakeProc()
    with (
        patch("lanegate.tui._go_tui_command", return_value=(["lanegate-tui"], None)),
        patch("lanegate.tui._select_loopback_port", return_value=54323),
        patch("subprocess.Popen", return_value=fake_api_proc),
        patch("lanegate.tui._wait_for_api_ready", side_effect=TuiLaunchError("nope", exit_code=1)),
        patch("subprocess.run") as mock_run,
        pytest.raises(SystemExit) as exc_info,
    ):
        cmd_tui(tmp_path)

    assert exc_info.value.code == 1
    mock_run.assert_not_called()
    # The API subprocess must still be torn down even though launch failed.
    assert fake_api_proc.terminated


def test_api_url_path_does_not_spawn_a_subprocess(tmp_path):
    with (
        patch("lanegate.tui._go_tui_command", return_value=(["lanegate-tui"], None)),
        patch("subprocess.Popen") as mock_popen,
        patch("subprocess.run") as mock_run,
    ):
        cmd_tui(tmp_path, api_url="http://127.0.0.1:9999")

    mock_popen.assert_not_called()
    tui_argv = mock_run.call_args[0][0]
    assert tui_argv[tui_argv.index("--api-url") + 1] == "http://127.0.0.1:9999"


def test_no_api_start_without_api_url_raises(tmp_path):
    with (
        patch("lanegate.tui._go_tui_command", return_value=(["lanegate-tui"], None)),
        pytest.raises(SystemExit) as exc_info,
    ):
        cmd_tui(tmp_path, no_api_start=True)
    assert exc_info.value.code == 2
