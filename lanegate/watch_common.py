"""Shared helpers for the watch/resume_watch/notify_watch daemons."""

from __future__ import annotations

from pathlib import Path

from lanegate.pidutil import pid_alive


def write_log(log_path: Path, line: str) -> None:
    """Append one already-terminated line to a daemon log."""
    with open(log_path, "a") as f:
        f.write(line)


def read_pid(pid_path: Path) -> int | None:
    """
    Return the PID from the pid file, or None if missing, unreadable, or stale
    (i.e. the process is no longer running).
    """
    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        pid = int(raw)
    except (ValueError, TypeError):
        return None
    if pid_alive(pid):
        return pid
    return None
