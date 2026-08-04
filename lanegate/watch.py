"""
watch.py — session-independent background daemon for PR approval polling.

Usage:
    lanegate watch            # run the poll loop (background it yourself)
    lanegate watch --status   # print whether a watcher is running and its pid
    lanegate watch --stop     # kill a running watcher
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from lanegate import APP_NAME
from lanegate.pidutil import pid_alive
from lanegate.ticket import load_all_tickets

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _watch_pid_file(repo_root: Path) -> Path:
    """Return the path to the watcher PID file."""
    state_dir = repo_root / f".{APP_NAME}"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "watch.pid"


def _watch_log_file(repo_root: Path) -> Path:
    """Return the path to the watcher log file."""
    state_dir = repo_root / f".{APP_NAME}"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "watch.log"


def _write_log(log_path: Path, line: str) -> None:
    """Append one already-terminated line to the watch log."""
    with open(log_path, "a") as f:
        f.write(line)


# ---------------------------------------------------------------------------
# PID helpers
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """Return True if a process with this PID is currently running.

    Delegates to the shared cross-platform probe; on Windows a plain
    ``os.kill(pid, 0)`` would *terminate* the process being checked.
    """
    return pid_alive(pid)


def _read_pid(pid_path: Path) -> int | None:
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
    if _pid_alive(pid):
        return pid
    return None


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------


def _run_loop(cfg: dict, repo_root: Path) -> None:
    """
    Internal polling loop. Called by cmd_watch when running as a daemon.
    Logs to .lanegate/watch.log.
    """
    log_path = _watch_log_file(repo_root)
    tickets_dir = Path(cfg.get("tickets_dir", "tickets"))
    if not tickets_dir.is_absolute():
        tickets_dir = repo_root / tickets_dir
    prefix = cfg.get("ticket_prefix", "TICK")

    def log(msg: str) -> None:
        line = f"{msg}\n"
        print(line, end="", flush=True)
        _write_log(log_path, line)

    log(f"[watch] started (PID {os.getpid()})")

    while True:
        tickets, _ = load_all_tickets(tickets_dir, prefix, cfg)
        in_review = [t for t in tickets if t.get("status") == "in_review" and t.get("pr_number")]

        if not in_review:
            log("[watch] no in_review tickets with pr_number — exiting")
            break

        for ticket in in_review:
            result = subprocess.run(
                ["gh", "pr", "view", str(ticket["pr_number"]), "--json", "reviewDecision"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                log(f"[watch] {ticket['id']}: gh pr view failed — {result.stderr.strip()}")
                continue

            try:
                decision = json.loads(result.stdout).get("reviewDecision")
            except (json.JSONDecodeError, AttributeError):
                log(f"[watch] {ticket['id']}: could not parse gh output")
                continue

            if decision == "APPROVED":
                log(f"[watch] {ticket['id']}: APPROVED — calling lanegate merge")
                subprocess.run([APP_NAME, "merge", ticket["id"]])
            elif decision == "CHANGES_REQUESTED":
                log(f"[watch] {ticket['id']}: changes requested — leaving at in_review")
            else:
                log(f"[watch] {ticket['id']}: reviewDecision={decision!r} — waiting")

        time.sleep(60)

    log("[watch] exiting")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def read_log_lines(repo_root: Path, tail: int | None = None) -> list[str]:
    """Return lines from the watch log file (for API log endpoint)."""
    log_path = _watch_log_file(repo_root)
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    if tail is not None:
        return lines[-tail:]
    return lines


def cmd_watch(
    cfg: dict,
    repo_root: Path,
    *,
    status: bool = False,
    stop: bool = False,
) -> None:
    """
    Main entry point for `lanegate watch`.

      lanegate watch            — run the poll loop
      lanegate watch --status   — report running state
      lanegate watch --stop     — kill the running watcher
    """
    pid_path = _watch_pid_file(repo_root)

    # ── --status ──────────────────────────────────────────────────────────────
    if status:
        pid = _read_pid(pid_path)
        if pid is None:
            print("[watch] not running")
        else:
            print(f"[watch] running (PID {pid})")
        return

    # ── --stop ────────────────────────────────────────────────────────────────
    if stop:
        pid = _read_pid(pid_path)
        if pid is None:
            print("[watch] not running — nothing to stop")
            # Clean up a stale file if present (process is dead)
            if pid_path.exists():
                pid_path.unlink(missing_ok=True)
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError) as exc:
            print(f"[watch] could not kill PID {pid}: {exc}", file=sys.stderr)
        else:
            print(f"[watch] sent SIGTERM to PID {pid}")
            pid_path.unlink(missing_ok=True)
        return

    # ── run the poll loop ─────────────────────────────────────────────────────

    # Detect and clean up a stale PID file before starting.
    if pid_path.exists():
        existing_pid = _read_pid(pid_path)
        if existing_pid is not None:
            print(
                f"[watch] already running (PID {existing_pid}). Use --stop to kill it first.",
                file=sys.stderr,
            )
            sys.exit(1)
        else:
            # Stale file — process is dead, clean it up.
            pid_path.unlink(missing_ok=True)

    # Write our own PID file.
    pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")

    # Remove PID file on clean exit. SIGTERM handler is Unix-only;
    # on Windows --stop uses TerminateProcess so the handler never fires.
    def _cleanup(*_):
        pid_path.unlink(missing_ok=True)
        sys.exit(0)

    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, _cleanup)

    try:
        _run_loop(cfg, repo_root)
    finally:
        pid_path.unlink(missing_ok=True)
