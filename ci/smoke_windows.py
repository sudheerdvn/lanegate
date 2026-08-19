"""End-to-end smoke test for the OS-level runtime paths the unit suite only mocks.

The pytest suite mocks every OS boundary (``subprocess.Popen``, ``os.kill``,
``_run_loop``) and skips the real-process tests on Windows, so a green unit run
proves nothing about whether ``lanegate`` actually *runs* on Windows. This script
exercises those boundaries for real, against the live OS:

  * ``lanegate init``            — a real CLI invocation end-to-end.
  * ``spawn_detached``        — really launches a detached child, on Windows via
                                ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP``.
  * ``_pid_alive``            — the ``os.kill(pid, 0)`` liveness probe, against a
                                process that is genuinely alive and then dead.
  * ``os.kill(pid, SIGTERM)`` — the ``watch --stop`` kill path (TerminateProcess
                                on Windows).
  * orchestrator lock         — real ``portalocker`` flock + live-holder refusal.

It is platform-agnostic so it runs identically on Linux/macOS/Windows; CI wires
it up as a dedicated Windows job. Any failure exits non-zero with a clear
message. The script never leaves a child process behind.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from lanegate.concurrency import (
    OrchestratorLockError,
    acquire_orchestrator_lock,
    orchestrator_lock_status,
    release_orchestrator_lock,
)
from lanegate.lifecycle import spawn_detached
from lanegate.pidutil import pid_alive as _pid_alive


class SmokeError(AssertionError):
    """A smoke-check failed."""


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise SmokeError(msg)
    print(f"  ok: {msg}")


def _wait_until(predicate, timeout: float = 10.0, interval: float = 0.1) -> bool:
    """Poll ``predicate`` until true or timeout; returns the final result."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def smoke_init(repo: Path) -> None:
    print("[smoke] lanegate init (real CLI run)")
    result = subprocess.run(
        [sys.executable, "-m", "lanegate.cli", "init"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    _check(result.returncode == 0, f"`lanegate init` exited 0 (got {result.returncode})\n{result.stderr}")
    _check((repo / ".lanegate.yml").exists(), "init scaffolded .lanegate.yml")


def smoke_spawn_and_kill(repo: Path) -> None:
    print("[smoke] spawn_detached + liveness probe + SIGTERM kill")
    log_path = repo / "logs" / "child.log"
    # A child that outlives the checks until we kill it.
    child_pid = spawn_detached(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        log_path,
    )
    try:
        _check(child_pid > 0, f"spawn_detached returned a pid ({child_pid})")
        _check(log_path.parent.exists(), "spawn_detached created the log dir")
        _check(_wait_until(lambda: _pid_alive(child_pid)), "spawned child is alive (os.kill probe)")

        # A second orchestrator acquire by a different *live* pid must be refused —
        # exercises portalocker flock + live-holder detection against a real pid.
        acquire_orchestrator_lock(repo, pid=child_pid)
        try:
            _check(
                orchestrator_lock_status(repo)["held"],
                "lock reports held while a live holder (the child) owns it",
            )
            raised = False
            try:
                acquire_orchestrator_lock(repo, pid=os.getpid())
            except OrchestratorLockError:
                raised = True
            _check(raised, "second acquire refused while a live holder owns the lock")
        finally:
            release_orchestrator_lock(repo, pid=child_pid, force=True)

        # The watch --stop path: os.kill(pid, SIGTERM) -> TerminateProcess on Windows.
        os.kill(child_pid, signal.SIGTERM)
        # On POSIX the detached child is still our direct child, so reap it to
        # clear the zombie that os.kill(pid, 0) would otherwise still see as alive
        # in containers without an init reaper. Windows has no zombies — the PID
        # is gone once TerminateProcess returns.
        if os.name == "posix":
            try:
                os.waitpid(child_pid, 0)
            except (ChildProcessError, OSError):
                pass
        _check(
            _wait_until(lambda: not _pid_alive(child_pid)),
            "child terminated after os.kill(SIGTERM) (watch --stop path)",
        )
    finally:
        # Belt-and-suspenders: never leave the child running.
        if _pid_alive(child_pid):
            try:
                os.kill(child_pid, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
                if os.name == "posix":
                    os.waitpid(child_pid, 0)
            except (ChildProcessError, OSError):
                pass


def smoke_orchestrator_lock_self(repo: Path) -> None:
    print("[smoke] orchestrator lock acquire/status/release (current process)")
    acquire_orchestrator_lock(repo, pid=os.getpid())
    status = orchestrator_lock_status(repo)
    _check(status["held"] and status["alive"], "lock held+alive for the current process")
    _check(release_orchestrator_lock(repo, pid=os.getpid()) is True, "release removed the lock")
    _check(not orchestrator_lock_status(repo)["held"], "lock free after release")


def main() -> int:
    print(f"[smoke] platform={sys.platform} python={sys.version.split()[0]}")
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=False)
        try:
            smoke_init(repo)
            smoke_spawn_and_kill(repo)
            smoke_orchestrator_lock_self(repo)
        except SmokeError as exc:
            print(f"\n[smoke] FAILED: {exc}", file=sys.stderr)
            return 1
    print("\n[smoke] all runtime smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
