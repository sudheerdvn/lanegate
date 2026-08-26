"""Tests for the cross-platform, non-destructive PID liveness probe.

Regression guard for the Windows bug where ``os.kill(pid, 0)`` (used as a
liveness check) actually calls ``TerminateProcess`` and kills the process it is
inspecting. Probing our own PID is the sharpest cross-platform assertion: if the
probe were still destructive, on Windows it would terminate the test process
itself.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from lanegate.pidutil import force_kill_pid, pid_alive, pid_cwd, terminate_pid


def test_pid_alive_true_for_current_process():
    assert pid_alive(os.getpid()) is True


def test_pid_alive_is_non_destructive():
    # Probe our own PID repeatedly; reaching the assertions at all proves the
    # probe did not terminate this process (it would have on Windows with the
    # old os.kill(pid, 0) idiom).
    for _ in range(5):
        assert pid_alive(os.getpid()) is True
    assert pid_alive(os.getpid()) is True


def test_pid_cwd_returns_current_process_directory_when_supported():
    cwd = pid_cwd(os.getpid())
    if sys.platform == "win32":
        assert cwd is None
    else:
        assert cwd == os.getcwd()


def test_pid_cwd_returns_none_for_invalid_pid():
    assert pid_cwd(0) is None


def test_pid_cwd_uses_lsof_on_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    fake_result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="p123\nfcwd\nn/Users/foo/project\n", stderr=""
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake_result)
    assert pid_cwd(123) == "/Users/foo/project"


def test_pid_cwd_darwin_returns_none_when_lsof_fails(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    def raise_oserror(*a, **k):
        raise OSError("lsof not found")

    monkeypatch.setattr(subprocess, "run", raise_oserror)
    assert pid_cwd(123) is None


def test_pid_alive_false_for_nonpositive_pids():
    assert pid_alive(0) is False
    assert pid_alive(-1) is False


def test_pid_alive_false_for_reaped_child():
    # Spawn a trivial process, wait for it to exit, reap it, then confirm the
    # probe reports it dead. Uses the Python executable for cross-platform reach.
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    assert pid_alive(proc.pid) is False


def test_terminate_pid_stops_a_live_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert pid_alive(proc.pid) is True
        assert terminate_pid(proc.pid) is True
        proc.wait(timeout=5)
        assert pid_alive(proc.pid) is False
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_terminate_pid_false_for_already_dead_process():
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    assert terminate_pid(proc.pid) is False


def test_force_kill_pid_stops_a_live_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert pid_alive(proc.pid) is True
        force_kill_pid(proc.pid)
        deadline = time.time() + 5
        while time.time() < deadline and pid_alive(proc.pid):
            time.sleep(0.05)
        proc.wait(timeout=5)
        assert pid_alive(proc.pid) is False
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_force_kill_pid_does_not_raise_for_already_dead_process():
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    force_kill_pid(proc.pid)  # should not raise
