"""
notify_watch.py — session-independent background daemon that pushes a phone
notification when an orchestrate run looks stuck.

Executor-agnostic by design: unlike a Claude-side watchdog, this only reads
local state files (active-orchestrate.json, the orchestrator lock, ticket
statuses) and shells out to ntfy.sh, so it works the same whether the
executor behind a given ticket is Claude, Codex, aider, or a local model.

Mirrors watch.py / resume_watch.py's detached-daemon pattern (PID file, log
file, --status/--stop), but instead of driving an action (merge, resume) it
only observes and pushes a notification, deduped against the last-reported
problem so it doesn't re-alert every poll.

Usage:
    lanegate notify-watch            # run the poll loop (background it yourself)
    lanegate notify-watch --status   # print whether a watcher is running and its pid
    lanegate notify-watch --stop     # kill a running watcher
    lanegate notify-watch --test     # send one test push and exit
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

from lanegate import APP_NAME
from lanegate.concurrency import orchestrator_lock_status
from lanegate.notify import send_ntfy
from lanegate.pidutil import terminate_pid
from lanegate.resume_watch import _RATE_LIMIT_MARKER, _read_pid as _resume_watch_read_pid, _resume_watch_pid_file
from lanegate.ticket import load_all_tickets
from lanegate.watch_common import read_pid as _read_pid, write_log as _write_log

_STUCK_STATUSES = {"needs_review", "failed", "blocked"}

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _notify_watch_pid_file(repo_root: Path) -> Path:
    """Return the path to the notify-watch PID file."""
    state_dir = repo_root / f".{APP_NAME}"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "notify-watch.pid"


def _notify_watch_log_file(repo_root: Path) -> Path:
    """Return the path to the notify-watch log file."""
    state_dir = repo_root / f".{APP_NAME}"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "notify-watch.log"


def _notify_watch_state_file(repo_root: Path) -> Path:
    """Return the path to the notify-watch dedupe-state file."""
    state_dir = repo_root / f".{APP_NAME}"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "notify-watch-state.json"


def _read_last_signature(state_path: Path) -> str:
    import json

    try:
        return json.loads(state_path.read_text(encoding="utf-8")).get("last_signature", "")
    except (OSError, ValueError):
        return ""


def _write_last_signature(state_path: Path, signature: str) -> None:
    import json

    from lanegate.orchestrate import _write_json_atomic

    _write_json_atomic(state_path, {"last_signature": signature})


# ---------------------------------------------------------------------------
# Stuck-state detection
# ---------------------------------------------------------------------------


def _rate_limit_watcher_alive(repo_root: Path) -> bool:
    """True if a resume-watch daemon is currently alive for this repo.

    Used to decide whether a rate-limit hibernation is actually being
    handled right now, rather than trusting the static on_rate_limit config
    value — a crashed or gave-up resume-watch should still be flagged.
    """
    return _resume_watch_read_pid(_resume_watch_pid_file(repo_root)) is not None


def _stuck_tickets(cfg: dict, repo_root: Path) -> list[dict]:
    """
    Tickets that halted in a way needing a human, not a timer.

    A rate-limit hibernation is exempt only while a resume-watch daemon is
    actually alive for this repo — if resume-watch crashed, was never
    started (on_rate_limit: halt), or already gave up after its ceiling,
    the ticket is just as stuck as any other halt and should be flagged.
    """
    tickets_dir = Path(cfg.get("tickets_dir", "tickets"))
    if not tickets_dir.is_absolute():
        tickets_dir = repo_root / tickets_dir
    tickets, _ = load_all_tickets(tickets_dir, cfg.get("ticket_prefix", "TICK"), cfg)
    resume_watch_alive = None  # computed lazily, only if a rate-limit hibernation is seen
    stuck = []
    for t in tickets:
        status = t.get("status")
        if status in _STUCK_STATUSES:
            stuck.append(t)
        elif status == "hibernated":
            if _RATE_LIMIT_MARKER not in (t.get("_body") or ""):
                stuck.append(t)
            else:
                if resume_watch_alive is None:
                    resume_watch_alive = _rate_limit_watcher_alive(repo_root)
                if not resume_watch_alive:
                    stuck.append(t)
    return stuck


def _detect_problem(cfg: dict, repo_root: Path) -> tuple[str, str] | None:
    """
    Return (signature, message) describing the current problem, or None if
    everything looks healthy. `signature` is a stable, order-independent key
    used to dedupe repeat pushes for the same unresolved problem.
    """
    from lanegate.orchestrate import _read_active_status, _read_all_active_statuses

    notify_cfg = cfg.get("notify") or {}
    stale_after = float(notify_cfg.get("heartbeat_stale_seconds", 180))

    lock = orchestrator_lock_status(repo_root)

    # Under concurrent/pooled execution, per-session status lives under
    # .lanegate/active-orchestrate/*.json rather than the singular
    # active-orchestrate.json — scan both so this still fires with pools.
    all_active = list(_read_all_active_statuses(repo_root))
    singular = _read_active_status(repo_root)
    if singular:
        all_active.append(singular)

    active_sessions = [a for a in all_active if a and a.get("active")]

    if active_sessions:
        if not lock.get("alive"):
            session = active_sessions[0]
            ticket_id = session.get("ticket_id", "?")
            step = session.get("step", "?")
            return (
                f"process-died:{ticket_id}",
                f"orchestrate process died unexpectedly while running {ticket_id} ({step})",
            )

        for session in active_sessions:
            ticket_id = session.get("ticket_id", "?")
            step = session.get("step", "?")
            last_hb = session.get("last_heartbeat_at")
            if last_hb is not None:
                stale_for = time.time() - float(last_hb)
                if stale_for > stale_after:
                    return (
                        f"heartbeat-stale:{ticket_id}",
                        f"{ticket_id} ({step}) heartbeat stale {int(stale_for)}s — executor may be wedged",
                    )
        return None

    # No active executor right now — fine if the loop is between tickets,
    # but if the orchestrator itself isn't running AND work is waiting on a
    # human, that's the "stuck overnight" case.
    if not lock.get("alive"):
        stuck = _stuck_tickets(cfg, repo_root)
        if stuck:
            ids = sorted(f"{t['id']}({t.get('status')})" for t in stuck)
            return (
                "halted:" + ",".join(ids),
                f"orchestrate is not running — {len(stuck)} ticket(s) waiting: {', '.join(ids)}",
            )

    return None


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------


def _run_loop(cfg: dict, repo_root: Path) -> None:
    """
    Internal polling loop. Called by cmd_notify_watch when running as a
    daemon. Logs to .lanegate/notify-watch.log. Runs indefinitely (stop with
    `lanegate notify-watch --stop`) since it monitors across orchestrate runs,
    not just one.

    Shares its name with watch._run_loop and resume_watch._run_loop
    (TICK-366 duplicate-drift sweep). Each daemon polls a different
    condition with a different body — this one detects a stuck orchestrate
    run and pushes a notification — so the shared name is intentional and
    no consolidation is needed.
    """
    log_path = _notify_watch_log_file(repo_root)
    state_path = _notify_watch_state_file(repo_root)
    notify_cfg = cfg.get("notify") or {}
    topic = notify_cfg.get("ntfy_topic")
    poll_seconds = float(notify_cfg.get("poll_seconds", 60))

    def log(msg: str) -> None:
        line = f"{msg}\n"
        print(line, end="", flush=True)
        _write_log(log_path, line)

    log(f"[notify-watch] started (PID {os.getpid()})")

    if not topic:
        log("[notify-watch] no notify.ntfy_topic configured — logging only, no pushes will be sent")

    last_signature = _read_last_signature(state_path)

    while True:
        problem = _detect_problem(cfg, repo_root)
        signature = problem[0] if problem else ""

        if signature != last_signature:
            if problem:
                _, message = problem
                log(f"[notify-watch] PROBLEM: {message}")
                if topic and not send_ntfy(topic, message):
                    log("[notify-watch] ntfy push failed")
            else:
                message = "back to normal — orchestrate running cleanly"
                log(f"[notify-watch] RECOVERED: {message}")
                if topic and not send_ntfy(topic, message):
                    log("[notify-watch] ntfy push failed")
            _write_last_signature(state_path, signature)
            last_signature = signature

        time.sleep(poll_seconds)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def cmd_notify_watch(
    cfg: dict,
    repo_root: Path,
    *,
    status: bool = False,
    stop: bool = False,
    test: bool = False,
    background: bool = False,
) -> None:
    """
    Main entry point for `lanegate notify-watch`.

      lanegate notify-watch              — run the poll loop
      lanegate notify-watch --background — spawn the poll loop detached, survives
                                            this terminal closing, then exit
      lanegate notify-watch --status     — report running state
      lanegate notify-watch --stop       — kill the running watcher
      lanegate notify-watch --test       — send one test push and exit
    """
    pid_path = _notify_watch_pid_file(repo_root)

    if background and not (status or stop or test):
        from lanegate.lifecycle import spawn_detached

        existing_pid = _read_pid(pid_path)
        if existing_pid is not None:
            print(
                f"[notify-watch] already running (PID {existing_pid}). Use --stop to kill it first.",
                file=sys.stderr,
            )
            sys.exit(1)
        log_path = _notify_watch_log_file(repo_root)
        spawned_pid = spawn_detached([APP_NAME, "notify-watch"], log_path)
        print(f"[notify-watch] spawned detached (PID {spawned_pid}), survives this terminal closing")
        return

    if test:
        topic = (cfg.get("notify") or {}).get("ntfy_topic")
        if not topic:
            print("[notify-watch] no notify.ntfy_topic configured in lanegate config", file=sys.stderr)
            sys.exit(1)
        ok = send_ntfy(topic, "test push from lanegate notify-watch", title="lanegate test")
        print("[notify-watch] test push sent" if ok else "[notify-watch] test push FAILED")
        sys.exit(0 if ok else 1)

    if status:
        pid = _read_pid(pid_path)
        if pid is None:
            print("[notify-watch] not running")
        else:
            print(f"[notify-watch] running (PID {pid})")
        return

    if stop:
        pid = _read_pid(pid_path)
        if pid is None:
            print("[notify-watch] not running — nothing to stop")
            if pid_path.exists():
                pid_path.unlink(missing_ok=True)
            return
        if not terminate_pid(pid):
            print(f"[notify-watch] could not terminate PID {pid}", file=sys.stderr)
        else:
            print(f"[notify-watch] terminated PID {pid}")
            pid_path.unlink(missing_ok=True)
        return

    if pid_path.exists():
        existing_pid = _read_pid(pid_path)
        if existing_pid is not None:
            print(
                f"[notify-watch] already running (PID {existing_pid}). Use --stop to kill it first.",
                file=sys.stderr,
            )
            sys.exit(1)
        else:
            pid_path.unlink(missing_ok=True)

    pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")

    def _cleanup(*_):
        pid_path.unlink(missing_ok=True)
        sys.exit(0)

    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, _cleanup)

    try:
        _run_loop(cfg, repo_root)
    finally:
        pid_path.unlink(missing_ok=True)
