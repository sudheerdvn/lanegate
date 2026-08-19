"""Cross-platform process management helpers.

Why this module exists
----------------------
``os.kill(pid, 0)`` is the usual POSIX idiom for "is this PID alive?" — signal 0
performs the permission/existence check without delivering a signal. On
**Windows it is destructive**: ``os.kill`` does not send POSIX signals; for any
signal other than CTRL_C/CTRL_BREAK it calls ``TerminateProcess(handle, sig)``.
So ``os.kill(pid, 0)`` opens the target and terminates it with exit code 0 — a
"status check" that kills the process it is inspecting.

lanegate probes liveness in several hot paths (``watch --status``, the
orchestrator-lock status read, stale-lock reclaim). Routing them all through
``pid_alive`` keeps the POSIX behaviour and uses a non-destructive
``OpenProcess`` + ``WaitForSingleObject`` query on Windows.
"""

from __future__ import annotations

import os
import sys


def _pid_alive_windows(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    SYNCHRONIZE = 0x00100000
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_ACCESS_DENIED = 5
    ERROR_INVALID_PARAMETER = 87
    WAIT_TIMEOUT = 0x102  # object still non-signalled → process still running

    OpenProcess = kernel32.OpenProcess
    OpenProcess.restype = wintypes.HANDLE
    OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)

    handle = OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # No handle: distinguish "no such process" from "exists but not queryable".
        err = ctypes.get_last_error()  # type: ignore[attr-defined]  # Windows-only ctypes API
        if err == ERROR_ACCESS_DENIED:
            return True  # process exists, owned by a more privileged account
        if err == ERROR_INVALID_PARAMETER:
            return False  # no process with this PID
        # Be conservative for unexpected errors: treat as not alive rather than
        # risk a false "alive" wedging a lock forever.
        return False
    try:
        # WAIT_TIMEOUT means the process object is not yet signalled, i.e. still
        # running. WAIT_OBJECT_0 (0) means it has exited.
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    """Return True if a process with this PID is currently running.

    Non-destructive on every platform. On POSIX uses ``os.kill(pid, 0)``; on
    Windows uses ``OpenProcess`` + ``WaitForSingleObject`` (never
    ``TerminateProcess``).
    """
    if pid <= 0:
        return False

    if sys.platform == "win32":
        try:
            return _pid_alive_windows(pid)
        except OSError:
            return False

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user — still alive.
        return True
    except OSError:
        return False
    return True


def terminate_pid(pid: int) -> bool:
    """Send a graceful termination request to a process.

    On POSIX sends SIGTERM; on Windows calls ``os.kill(pid, signal.SIGTERM)``
    which maps to ``TerminateProcess`` (no graceful-shutdown path exists for
    arbitrary PIDs on Windows, but the effect — the process stops — is the
    same). Returns True if the request was sent, False if the process was
    already gone or not accessible.
    """
    import signal as _signal

    try:
        os.kill(pid, _signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def force_kill_pid(pid: int) -> None:
    """Unconditionally kill a process, suppressing common benign errors.

    On POSIX sends SIGKILL. On Windows ``signal.SIGKILL`` does not exist;
    uses ``taskkill /PID <pid> /F`` instead, which is the same pattern the
    rest of the codebase uses for forced process-tree teardown.
    """
    if sys.platform == "win32":
        import subprocess as _sp

        try:
            _sp.run(
                ["taskkill", "/PID", str(pid), "/F"],
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
                check=False,
            )
        except OSError:
            pass
    else:
        import signal as _signal

        try:
            os.kill(pid, _signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
