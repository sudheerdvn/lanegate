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
import re
import sys
import time
from typing import Any
from pathlib import Path

from lanegate.concurrency import orchestrator_lock_status
from lanegate.executor import resolved_dispatch_metadata
from lanegate.ticket import load_all_tickets

from .audit import _active_status_path, _utc_now_iso, _write_json_atomic


# Deliberately lives directly under .lanegate rather than active-orchestrate:
# the latter is scanned as a directory of per-session executor statuses.
_BATCH_STATUS_FILE = "orchestrate-batch-status.json"


def _batch_status_path(repo_root: Path) -> Path:
    return repo_root / ".lanegate" / _BATCH_STATUS_FILE


def write_batch_status(
    repo_root: Path,
    batch_line: str,
    underfilled_reason: str | None,
    *,
    max_parallel: int,
    total_open: int,
) -> None:
    """Persist the latest compact orchestrate batch diagnostics for the API."""
    _write_json_atomic(
        _batch_status_path(repo_root),
        {
            "batch_line": batch_line,
            "underfilled_reason": underfilled_reason,
            "max_parallel": max_parallel,
            "total_open": total_open,
        },
    )


def read_batch_status(repo_root: Path) -> dict:
    """Read persisted batch diagnostics, tolerating absent or malformed state."""
    try:
        data = json.loads(_batch_status_path(repo_root).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("batch status is not an object")
        return data
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        return {
            "batch_line": "",
            "underfilled_reason": None,
            "max_parallel": None,
            "total_open": None,
        }


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
    base.with_suffix(".orchestrated").touch()


def _remove_executor_markers(repo_root: Path, tid: str) -> None:
    base = _executor_marker_base(repo_root, tid)
    for suffix in (".pid", ".session", ".mcp", ".orchestrated"):
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

    if getattr(ev, "turns", None) is not None:
        turns_cnt = getattr(ev, "turns")
        toks = getattr(ev, "cumulative_tokens", None)
        ctx = getattr(ev, "current_context_tokens", None)
        if toks is not None and toks > 0:
            piece = f"{turns_cnt} turns, {toks:,} cum tok"
            if ctx:
                piece += f", ctx~{ctx:,}"
            details.append(piece)
        else:
            details.append(f"{turns_cnt} turns")
    elif getattr(ev, "cumulative_tokens", None) is not None and getattr(ev, "cumulative_tokens") > 0:
        piece = f"{getattr(ev, 'cumulative_tokens'):,} cum tok"
        ctx = getattr(ev, "current_context_tokens", None)
        if ctx:
            piece += f", ctx~{ctx:,}"
        details.append(piece)

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


_LOG_PATH_SESSION_TS_RE = re.compile(r"orchestrate-(.+)\.log$")


def _log_path_session_ts(log_path: object) -> str | None:
    """Extract the run session_ts embedded in a per-session status's log_path."""
    if not isinstance(log_path, str) or not log_path:
        return None
    match = _LOG_PATH_SESSION_TS_RE.search(log_path)
    return match.group(1) if match else None


_NO_CURRENT_RUN = object()


def _current_run_session_ts(repo_root: Path) -> str | None:
    """Return the session_ts of the run the live orchestrator lock owns, if any.

    Local import avoids a circular import: run_report.py already imports from
    this module.
    """
    from lanegate.orchestrate.run_report import _resolve_active_run_session_ts

    return _resolve_active_run_session_ts(repo_root)


def _normalize_status_dict(
    status: dict,
    repo_root: Path,
    *,
    has_live: bool | None = None,
    current_run_session_ts: object = _NO_CURRENT_RUN,
) -> dict:
    from lanegate.pidutil import pid_alive as _pid_alive

    if current_run_session_ts is _NO_CURRENT_RUN:
        current_run_session_ts = _current_run_session_ts(repo_root)

    started_at = status.get("started_at")
    elapsed_seconds = None
    if isinstance(started_at, (int, float)):
        elapsed_seconds = max(0, int(time.time() - started_at))
    status["elapsed_seconds"] = elapsed_seconds
    status["elapsed"] = _format_elapsed(elapsed_seconds)

    pid = status.get("executor_pid")
    pid_live = bool(isinstance(pid, int) and _pid_alive(pid))
    status["pid_alive"] = pid_live

    lock = orchestrator_lock_status(repo_root)
    status["orchestrator_lock"] = lock
    if lock["held"]:
        status["orchestrator_lock_state"] = "live"
    elif lock["pid"] is not None:
        status["orchestrator_lock_state"] = "stale"
    else:
        status["orchestrator_lock_state"] = "none"

    state = status.get("state") or "no-active-run"
    if has_live is None:
        has_live = bool(state == "running" and pid_live)

    if has_live:
        status["active"] = True
        status["state"] = "running"
        status["reconciliation_state"] = "live"
    elif (
        lock["held"]
        and current_run_session_ts is not None
        and _log_path_session_ts(status.get("log_path")) == current_run_session_ts
    ):
        # The orchestrator is still alive but has no currently-dispatched
        # executor for *this run* (for example while analyzing, cooling down,
        # or scheduling the next ticket). Scoped to the run currently in
        # progress (matched via log_path's embedded session_ts) so a finished
        # session left over from a past run isn't resurrected as active
        # merely because some orchestrator lock happens to be held now.
        status["active"] = True
        status["state"] = "between-dispatches"
        status["reconciliation_state"] = "orchestrator_live"
        # Startup reconciliation runs after the new orchestrator has acquired
        # its lock.  Keep a stale predecessor marker discoverable there even
        # though the public state correctly reports this live orchestrator as
        # between dispatches.
        if state == "running" and not pid_live:
            status["stale_executor_marker"] = True
    elif state == "running":
        status["active"] = False
        status["reconciliation_state"] = "live" if pid_live else "stale"
    elif state in ("reconciled", "hibernated", "failed", "needs_review"):
        status["active"] = False
        status["reconciliation_state"] = status.get("reconciliation_state") or state
    elif state == "finished":
        status["active"] = False
        status["reconciliation_state"] = "finished"
    else:
        status["active"] = False
        status["reconciliation_state"] = status.get("reconciliation_state") or "none"
    return status


def _normalize_active_status(repo_root: Path, cfg: dict | None = None) -> dict:
    from lanegate.pidutil import pid_alive as _pid_alive

    # Try reading from per-session files first (used by concurrent executors)
    session_statuses = _read_all_active_statuses(repo_root)
    # Keep a representative status for the detailed fields, but aggregate
    # liveness over every session.  A recently finished sibling must not hide
    # an executor that is still running.
    raw = None
    if session_statuses:
        raw = max(session_statuses, key=lambda s: s.get("updated_at", ""))
    # Fall back to shared status file for backward compatibility
    if not raw:
        raw = _read_active_status(repo_root)

    candidates = session_statuses or ([raw] if raw else [])
    live_sessions = [
        candidate
        for candidate in candidates
        if candidate.get("state") == "running"
        and isinstance(candidate.get("executor_pid"), int)
        and _pid_alive(candidate["executor_pid"])
    ]
    if live_sessions:
        # Show the most recently updated live executor rather than a finished
        # sibling that happened to write its marker later.
        raw = max(live_sessions, key=lambda s: s.get("updated_at", ""))

    if raw:
        status = dict(raw)
    else:
        status = {
            "state": "no-active-run",
            "last_event": "no_active_run",
            "ticket_id": None,
            "executor_pid": None,
            "executor_session": None,
            "log_path": None,
            "resolved_driver": None,
            "resolved_executor": None,
            "resolved_model": None,
        }

    return _normalize_status_dict(
        status,
        repo_root,
        has_live=bool(live_sessions),
        current_run_session_ts=_current_run_session_ts(repo_root),
    )


def _normalize_session_status(
    raw: dict, repo_root: Path, current_run_session_ts: object = _NO_CURRENT_RUN
) -> dict:
    return _normalize_status_dict(
        dict(raw), repo_root, current_run_session_ts=current_run_session_ts
    )


def get_all_active_statuses(repo_root: Path) -> list[dict]:
    """Return normalized active status dicts for all live per-session executors.

    Reads all per-session files under .lanegate/active-orchestrate/*.json and
    normalizes each one. A session with a live executor pid, or
    'between-dispatches' *and part of the run currently in progress*
    (implement finished, review not dispatched yet, but the orchestrator lock
    is still held for this same run), counts as active — a sibling that is
    momentarily between dispatch calls must not silently disappear just
    because its raw per-session state isn't literally 'running'. Per-session
    files are never deleted once a ticket finishes or crashes, so filtering
    is done on the normalized `active` flag rather than the raw `state`
    string: a leftover file whose raw state is still 'running' but whose pid
    is long dead (a crashed session that was never reconciled, with no
    current orchestrator lock to attribute it to) normalizes to active=False
    while its `state` field is left untouched — filtering on `state` alone
    would wrongly keep showing it as a live worker forever. Only falls back
    to the single legacy representative status when there are no per-session
    files at all.
    """
    session_statuses = _read_all_active_statuses(repo_root)
    if not session_statuses:
        return [_normalize_active_status(repo_root)]
    current_run_session_ts = _current_run_session_ts(repo_root)
    normalized = [
        _normalize_session_status(s, repo_root, current_run_session_ts)
        for s in session_statuses
    ]
    return [s for s in normalized if s.get("active")]


def _running_session_count(repo_root: Path) -> int:
    """Return executors currently consuming a batch slot.

    ``between-dispatches`` remains active for the Workers view while the
    orchestrator owns the ticket, but its executor has finished and therefore
    must not keep the batch's *running* count inflated.
    """
    from lanegate.pidutil import pid_alive as _pid_alive

    sessions = _read_all_active_statuses(repo_root)
    if not sessions:
        return int(_normalize_active_status(repo_root).get("state") == "running")
    return sum(
        candidate.get("state") == "running"
        and isinstance(candidate.get("executor_pid"), int)
        and _pid_alive(candidate["executor_pid"])
        for candidate in sessions
    )


def get_orchestration_status(repo_root: Path) -> dict:
    """Return current orchestration status as a JSON-serializable dict (API wrapper)."""
    from lanegate.orchestrate import _last_cooldown_event

    status = _normalize_active_status(repo_root)
    batch_status = read_batch_status(repo_root)
    status["underfilled_reason"] = batch_status.get("underfilled_reason")
    status["last_cooldown"] = _last_cooldown_event(repo_root)

    max_parallel = batch_status.get("max_parallel")
    total_open = batch_status.get("total_open")
    if isinstance(max_parallel, int) and isinstance(total_open, int):
        live_count = _running_session_count(repo_root)
        status["batch_line"] = (
            f"[orchestrate] batch: {live_count} running of cap {max_parallel}, "
            f"{total_open - live_count} peers ({total_open} open tickets total)\n"
        )
    else:
        status["batch_line"] = batch_status.get("batch_line")
    return status



def _reconcile_stale_executor_markers(
    cfg: dict, repo_root: Path, *, out_stream=None, session_ts: str | None = None
) -> dict | None:
    """Hibernate an in-progress ticket if its active executor marker is stale."""
    from lanegate.orchestrate import _append_run_event

    status = _normalize_active_status(repo_root, cfg)
    stale_running_marker = (
        status.get("state") == "running"
        and status.get("reconciliation_state") == "stale"
    ) or bool(status.get("stale_executor_marker"))
    if not stale_running_marker:
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
        updated.pop("stale_executor_marker", None)
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
    updated.pop("stale_executor_marker", None)
    updated["state"] = "reconciled"
    updated["active"] = False
    updated["last_event"] = "stale_executor_reconciled_to_hibernated"
    updated["reconciliation_state"] = "hibernated"
    return _write_active_status(repo_root, updated, session_id=session_id)
