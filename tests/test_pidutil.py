"""Tests for the cross-platform, non-destructive PID liveness probe.

Regression guard for the Windows bug where ``os.kill(pid, 0)`` (used as a
liveness check) actually calls ``TerminateProcess`` and kills the process it is
inspecting. Probing our own PID is the sharpest cross-platform assertion: if the
probe were still destructive, on Windows it would terminate the test process
itself.
"""

from __future__ import annotations

import os

from lanegate.pidutil import pid_alive


def test_pid_alive_true_for_current_process():
    assert pid_alive(os.getpid()) is True


def test_pid_alive_is_non_destructive():
    # Probe our own PID repeatedly; reaching the assertions at all proves the
    # probe did not terminate this process (it would have on Windows with the
    # old os.kill(pid, 0) idiom).
    for _ in range(5):
        assert pid_alive(os.getpid()) is True
    assert pid_alive(os.getpid()) is True


def test_pid_alive_false_for_nonpositive_pids():
    assert pid_alive(0) is False
    assert pid_alive(-1) is False


def test_pid_alive_false_for_reaped_child():
    # Spawn a trivial process, wait for it to exit, reap it, then confirm the
    # probe reports it dead. Uses the Python executable for cross-platform reach.
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    assert pid_alive(proc.pid) is False
