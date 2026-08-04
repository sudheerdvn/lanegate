"""
lanegate/orchestrate/status.py — active-run status bookkeeping and reporting.

Extracted from orchestrate.py (TICK-255/TICK-271/TICK-272/TICK-273): reading
and writing the active-status file(s), executor PID markers, formatting
elapsed time, normalizing/aggregating active status across concurrent
executors, the get_orchestration_status() API wrapper, and stale-executor-
marker reconciliation.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any
from pathlib import Path

from lanegate.concurrency import orchestrator_lock_status
from lanegate.executor import resolved_dispatch_metadata
from lanegate.ticket import load_all_tickets

from .audit import _active_status_path, _utc_now_iso, _write_json_atomic


def _read_active_status(repo_root: Path, session_id: str | None = None) -> dict | None:
    path = _active_status_path(repo_root, session_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return {
            "active": False,
            "state": "unknown",
            "reconciliation_state": "unreadable",
            "last_event": "status_unreadable",
            "path": str(path),
        }


def _write_active_status(repo_root: Path, data: dict, session_id: str | None = None) -> dict:
    payload = dict(data)
    payload["updated_at"] = _utc_now_iso()
    _write_json_atomic(_active_status_path(repo_root, session_id), payload)
    return payload


def _executor_marker_base(repo_root: Path, tid: str) -> Path:
    state = repo_root / ".lanegate"
    state.mkdir(parents=True, exist_ok=True)
    return state / tid


def _write_executor_pid_marker(repo_root: Path, tid: str, pid: int, started_at: float) -> None:
    base = _executor_marker_base(repo_root, tid)
    base.with_suffix(".pid").write_text(f"{pid}\n", encoding="utf-8")
    base.with_suffix(".session").write_text(f"{started_at:.6f}\n", encoding="utf-8")


def _remove_executor_markers(repo_root: Path, tid: str) -> None:
    base = _executor_marker_base(repo_root, tid)
    for suffix in (".pid", ".session"):
        try:
            base.with_suffix(suffix).unlink()
        except FileNotFoundError:
            pass


def _format_elapsed(seconds: int | float | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def format_resolved_dispatch(dispatch: dict) -> str:
    """Format configured route and actual executor for a compact lifecycle line."""
    if not dispatch:
        return "route=unknown executor=unknown model=unknown"
    metadata = resolved_dispatch_metadata(
        driver=str(dispatch.get("resolved_driver") or "unknown"),
        executor=str(dispatch.get("resolved_executor") or "unknown"),
        model=dispatch.get("resolved_model"),
    )
    return " ".join(
        f"{('route' if key == 'resolved_driver' else key.removeprefix('resolved_'))}={value}"
        for key, value in metadata.items()
    )


def write_executing_status(tid: str, dispatch: dict, orig_out, log_f=None) -> None:
    """Write the compact executing lifecycle line with its actual route."""
    ts = time.strftime("%H:%M:%S")
    line = f"  {ts}  {tid:10s}  [executing]  {format_resolved_dispatch(dispatch)}\n"
    orig_out.write(line)
    orig_out.flush()
    if log_f is not None:
        log_f.write(line)
        log_f.flush()


def format_executor_event_status(tid: str, ev: Any) -> str:
    """Format safe executor event progress line for compact CLI output."""
    from lanegate.executor_events import ExecutorEvent
    if isinstance(ev, dict):
        ev = ExecutorEvent.from_dict(ev)

    ts = time.strftime("%H:%M:%S")
    details = []
    if ev.activity and ev.activity not in ("heartbeat", "tool_use"):
        if ev.path:
            details.append(f"{ev.activity} {ev.path}")
        else:
            details.append(ev.activity)
    elif ev.path:
        details.append(ev.path)

    if ev.test_summary:
        ts_status = ev.test_summary.get("status") or ev.test_summary.get("category")
        if ts_status:
            details.append(f"tests:{ts_status}")

    if ev.tool_category:
        details.append(ev.tool_category)

    if ev.activity_age > 0:
        details.append(f"{ev.activity_age:.1f}s")

    detail_str = f" ({', '.join(details)})" if details else ""
    model_str = f" ({ev.model})" if ev.model else ""
    return f"  {ts}  {tid:10s}  [{ev.phase}]  {ev.executor}{model_str}{detail_str}"


def _read_all_active_statuses(repo_root: Path) -> list[dict]:
    """Read all active executor statuses from per-session files.

    Returns a list of status dicts from .lanegate/active-orchestrate/*.json.
    Used when multiple executors are running in parallel to report on all of them.
    """
    status_dir = repo_root / ".lanegate" / "active-orchestrate"
    if not status_dir.exists():
        return []

    statuses = []
    try:
        for status_file in status_dir.glob("*.json"):
            try:
                data = json.loads(status_file.read_text(encoding="utf-8"))
                statuses.append(data)
            except (OSError, json.JSONDecodeError):
                pass
    except OSError:
        pass

    return statuses


def _normalize_active_status(repo_root: Path, cfg: dict | None = None) -> dict:
    from lanegate.orchestrate import _pid_alive

    # Try reading from per-session files first (used by concurrent executors)
    session_statuses = _read_all_active_statuses(repo_root)
    # If multiple concurrent executors, use the most recently updated one
    raw = None
    if session_statuses:
        raw = max(session_statuses, key=lambda s: s.get("updated_at", ""))
    # Fall back to shared status file for backward compatibility
    if not raw:
        raw = _read_active_status(repo_root)
    if not raw:
        return {
            "active": False,
            "state": "no-active-run",
            "reconciliation_state": "none",
            "last_event": "no_active_run",
            "ticket_id": None,
            "executor_pid": None,
            "executor_session": None,
            "elapsed_seconds": None,
            "elapsed": "unknown",
            "log_path": None,
            "resolved_driver": None,
            "resolved_executor": None,
            "resolved_model": None,
        }

    status = dict(raw)
    started_at = status.get("started_at")
    elapsed_seconds = None
    if isinstance(started_at, (int, float)):
        elapsed_seconds = max(0, int(time.time() - started_at))
    status["elapsed_seconds"] = elapsed_seconds
    status["elapsed"] = _format_elapsed(elapsed_seconds)

    pid = status.get("executor_pid")
    pid_live = bool(isinstance(pid, int) and _pid_alive(pid))
    status["pid_alive"] = pid_live

    state = status.get("state") or "unknown"
    if state == "running":
        status["active"] = True
        status["reconciliation_state"] = "live" if pid_live else "stale"
    elif state in ("reconciled", "hibernated", "failed", "needs_review"):
        status["active"] = False
        status["reconciliation_state"] = status.get("reconciliation_state") or state
    elif state == "finished":
        status["active"] = False
        status["reconciliation_state"] = "finished"
    else:
        status["active"] = False
        status["reconciliation_state"] = status.get("reconciliation_state") or "unknown"

    lock = orchestrator_lock_status(repo_root)
    status["orchestrator_lock"] = lock
    if lock["held"]:
        status["orchestrator_lock_state"] = "live"
    elif lock["pid"] is not None:
        status["orchestrator_lock_state"] = "stale"
    else:
        status["orchestrator_lock_state"] = "none"
    return status


def get_orchestration_status(repo_root: Path) -> dict:
    """Return current orchestration status as a JSON-serializable dict (API wrapper)."""
    from lanegate.orchestrate import _last_cooldown_event

    status = _normalize_active_status(repo_root)
    status["last_cooldown"] = _last_cooldown_event(repo_root)
    return status


def _reconcile_stale_executor_markers(
    cfg: dict, repo_root: Path, *, out_stream=None, session_ts: str | None = None
) -> dict | None:
    """Hibernate an in-progress ticket if its active executor marker is stale."""
    from lanegate.orchestrate import _append_run_event

    status = _normalize_active_status(repo_root, cfg)
    if status.get("state") != "running" or status.get("reconciliation_state") != "stale":
        return None

    tid = status.get("ticket_id")
    if not tid:
        return None

    # Write the outcome back to the same file _normalize_active_status read the
    # stale marker from (the per-session file when one exists), not the legacy
    # shared file — otherwise the per-session file's reconciliation_state stays
    # "stale" forever, and every future orchestrate run re-detects the same
    # marker and exits immediately (cmd_orchestrate's loop breaks as soon as
    # _reconcile_stale_executor_markers returns non-None), never dispatching.
    session_id = status.get("executor_session")

    tickets_dir = repo_root / cfg["tickets_dir"]
    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    ticket = next((t for t in tickets if t.get("id") == tid), None)
    if not ticket or ticket.get("status") != "in_progress":
        updated = dict(status)
        updated["state"] = "reconciled"
        updated["active"] = False
        updated["last_event"] = "stale_executor_reconciled_noop"
        updated["reconciliation_state"] = "noop"
        _write_active_status(repo_root, updated, session_id=session_id)
        return None

    from lanegate.lifecycle import cmd_hibernate

    reason = (
        "stale executor marker reconciled by orchestrate: "
        f"executor PID {status.get('executor_pid')} is not alive"
    )
    stream = out_stream if out_stream is not None else sys.stderr
    print(f"[orchestrate] {tid}: stale executor marker detected - hibernating", file=stream)
    cmd_hibernate(tid, cfg, repo_root, reason=reason)
    _remove_executor_markers(repo_root, tid)
    _append_run_event(
        repo_root, session_ts, "orphan_reconciled", ticket_id=tid, reason=reason
    )

    updated = dict(status)
    updated["state"] = "reconciled"
    updated["active"] = False
    updated["last_event"] = "stale_executor_reconciled_to_hibernated"
    updated["reconciliation_state"] = "hibernated"
    return _write_active_status(repo_root, updated, session_id=session_id)
