"""Tests for lanegate/watch_common.py — shared helpers for the watch daemons."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from lanegate.watch_common import read_pid, write_log


def test_write_log_appends_line(tmp_path: Path):
    log_path = tmp_path / "daemon.log"
    write_log(log_path, "first line\n")
    write_log(log_path, "second line\n")
    assert log_path.read_text() == "first line\nsecond line\n"


def test_read_pid_returns_none_for_missing_file(tmp_path: Path):
    assert read_pid(tmp_path / "daemon.pid") is None


def test_read_pid_returns_none_for_dead_pid(tmp_path: Path):
    pid_path = tmp_path / "daemon.pid"
    pid_path.write_text("999999\n")
    with patch("lanegate.watch_common.pid_alive", return_value=False):
        assert read_pid(pid_path) is None


def test_read_pid_returns_pid_for_alive_process(tmp_path: Path):
    pid_path = tmp_path / "daemon.pid"
    my_pid = os.getpid()
    pid_path.write_text(f"{my_pid}\n")
    with patch("lanegate.watch_common.pid_alive", return_value=True):
        assert read_pid(pid_path) == my_pid
