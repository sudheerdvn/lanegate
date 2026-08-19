"""
lanegate/orchestrate/run_report.py — durable run-event log and CLI status reporting.

Extracted from orchestrate.py (TICK-255/TICK-271..274): the durable per-run
event log (append/load/path helpers, last-run pointer, session-ts
resolution), live lanegate-spawned process enumeration, `lanegate ps`,
`build_run_report`/`lanegate run-report`, `lanegate run --status`, and the
subprocess-streaming helper used by executor dispatch.
"""

from __future__ import annotations

import datetime
import contextlib
import contextvars
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import portalocker

from lanegate.board import latest_dispatch_executors
from lanegate.concurrency import orchestrator_lock_status
from lanegate.config import resolve_executor_route
from lanegate.executor import get_executor_config
from lanegate.executor_events import ExecutorEvent, redact_transcript_text
from lanegate.logs import semantic_line_metadata
from lanegate.orchestrate.run_summary import (
    RunReason,
    RunSummary,
    TicketOutcome,
    TicketOutcomeStatus,
)
from lanegate.pidutil import pid_alive
from lanegate.ticket import attention_summary, canonical_id, load_all_tickets

from .audit import _utc_now_iso, _write_json_atomic
from .status import _format_elapsed, _normalize_active_status

# ---------------------------------------------------------------------------
# Run report (TICK-244)
#
# A durable, incrementally-written record of one orchestrate run: per-ticket
# outcomes, executor swaps/cooldowns, rate-limit hibernations, and orphaned
# processes. Answers "what happened while I was away" without cross-
# referencing git log / ps / raw logs by hand. Events are appended (one
# fsynced line per call) as they happen, not reconstructed only at query
# time, so a killed/crashed orchestrate process still leaves a readable
# partial report.
# ---------------------------------------------------------------------------

_RUN_EVENTS_SUFFIX = ".events.jsonl"
_LAST_RUN_POINTER = "last-run.json"
_SESSION_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}\Z")
_API_RUN_ID_RE = re.compile(r"run-\d{8}T\d{6}Z-[0-9a-f]{8}\Z")
_DIRECT_ACTION_SUPPRESSED = contextvars.ContextVar("direct_action_suppressed", default=False)
# Context variables do not cross process boundaries.  Executor processes may
# invoke ``lanegate review``/``fix`` themselves, so the orchestrator passes
# this marker to distinguish those nested lifecycle calls from a command an
# operator started directly in their shell.
INTERNAL_RUN_ENV = "LANEGATE_INTERNAL_RUN"


def _terminate_process_tree(proc: subprocess.Popen[str], *, grace_seconds: float = 2.0) -> None:
    """Stop *proc* and every process it started, without leaking grandchildren."""
    if sys.platform == "win32":
        # ``/T`` terminates the complete descendant tree.  The creation flag in
        # _stream_subprocess gives the executor an isolated group as well, so a
        # future graceful Windows implementation has a safe ownership boundary.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            # Keep the existing best-effort timeout behavior if taskkill is
            # unavailable (for example, in a restricted Windows environment).
            proc.kill()
        proc.wait()
        return

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass
    try:
        # The direct executor may exit promptly while a child ignores SIGTERM;
        # the process group is the ownership boundary, not the direct PID.
        os.killpg(proc.pid, 0)
    except ProcessLookupError:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait()
    except ChildProcessError:
        pass


def _run_events_path(repo_root: Path, session_ts: str) -> Path:
    logs_dir = repo_root / ".lanegate" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir / f"orchestrate-{session_ts}{_RUN_EVENTS_SUFFIX}"


def _action_events_path(repo_root: Path, action_id: str) -> Path:
    """Return the durable event stream for one direct CLI/MCP action."""
    logs_dir = repo_root / ".lanegate" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    safe_id = action_id if action_id.startswith("action-") else f"action-{action_id}"
    return logs_dir / f"{safe_id}{_RUN_EVENTS_SUFFIX}"


def begin_direct_action(
    repo_root: Path, action_type: str, *, ticket_id: str | None = None, executor: str | None = None
) -> dict:
    """Create a stable direct-action reference and persist its start event."""
    action_id = "action-" + datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
    log_path = _action_events_path(repo_root, action_id)
    record_direct_action_event(
        repo_root, action_id, "action_start", action_type=action_type, ticket_id=ticket_id,
        executor=executor, status="running",
    )
    return {"action_id": action_id, "log_path": str(log_path), "status": "running"}


def record_direct_action_event(
    repo_root: Path, action_id: str, event: str, **fields
) -> Path:
    """Append an fsynced direct-action event and return its audit-log path."""
    path = _action_events_path(repo_root, action_id)
    entry = {"ts": _utc_now_iso(), "event": event, "action_id": action_id, **fields}
    try:
        with portalocker.Lock(str(path), "a", timeout=None) as f:
            f.write(json.dumps(entry) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass
    return path


@contextlib.contextmanager
def suppress_direct_action_tracking():
    """Let a transport own one action stream instead of a nested command."""
    token = _DIRECT_ACTION_SUPPRESSED.set(True)
    try:
        yield
    finally:
        _DIRECT_ACTION_SUPPRESSED.reset(token)


def direct_action_tracking_suppressed() -> bool:
    return _DIRECT_ACTION_SUPPRESSED.get() or os.environ.get(INTERNAL_RUN_ENV) == "1"


def _append_run_event(repo_root: Path, session_ts: str | None, event: str, **fields) -> None:
    """Append one structured event to the current run's durable event log.

    A no-op (not an error) when session_ts is falsy, so call sites reachable
    from tests or --dry-run (which never establishes a run session) can call
    this unconditionally.
    """
    if not session_ts:
        return
    entry = {"ts": _utc_now_iso(), "event": event, **fields}
    path = _run_events_path(repo_root, session_ts)
    try:
        # Plain open(path, "a") relies on POSIX's atomic append-at-EOF
        # guarantee, which Windows doesn't provide across separate handles
        # -- concurrent writers can race and clobber each other's line.
        # portalocker.Lock serializes this cross-platform.
        with portalocker.Lock(str(path), "a", timeout=None) as f:
            f.write(json.dumps(entry) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass


def _write_last_run_pointer(repo_root: Path, session_ts: str, log_path: Path) -> None:
    pointer = {
        "session_ts": session_ts,
        "log_path": str(log_path),
        "events_path": str(_run_events_path(repo_root, session_ts)),
    }
    _write_json_atomic(repo_root / ".lanegate" / "logs" / _LAST_RUN_POINTER, pointer)


def _resolve_run_session_ts(repo_root: Path, session_ts: str | None) -> str | None:
    logs_dir = repo_root / ".lanegate" / "logs"
    if session_ts:
        # ``orchestrator-<pid>`` is the API's fallback identifier while a
        # CLI-started loop is live. Resolve it through the active run rather
        # than treating it as a literal log filename.
        if session_ts.startswith("orchestrator-"):
            pid_text = session_ts.removeprefix("orchestrator-")
            lock = orchestrator_lock_status(repo_root)
            if not (pid_text.isdigit() and lock.get("held") and lock.get("pid") == int(pid_text)):
                return None
            return _resolve_run_session_ts(repo_root, None)

        # The API assigns an opaque ID before the spawned loop creates its
        # durable timestamped audit files.  Accept only its currently-recorded
        # ID, and only while the loop still owns the orchestrator lock; this
        # maps the API handle to the active durable run without accepting
        # arbitrary ``run-...`` strings.
        if _API_RUN_ID_RE.fullmatch(session_ts):
            try:
                api_run = json.loads(
                    (repo_root / ".lanegate" / "api-run-current.json").read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                return None
            if api_run.get("run_id") != session_ts or not orchestrator_lock_status(repo_root).get("held"):
                return None
            return _resolve_run_session_ts(repo_root, None)

        # Never allow a supplied identifier to escape the logs directory.
        # Older durable records may use a pre-timestamp session convention, so
        # an existing artifact remains the authority for those IDs.
        if "/" in session_ts or "\\" in session_ts or session_ts in {".", ".."}:
            return None

        raw_log_name = f"orchestrate-{session_ts}.log"
        known_artifacts = (
            logs_dir / raw_log_name,
            logs_dir / "archive" / raw_log_name,
            _run_events_path(repo_root, session_ts),
        )
        if any(path.is_file() for path in known_artifacts):
            return session_ts

        # Per-ticket executor sessions (and other arbitrary strings) must not
        # be accepted merely because an orchestrator happens to be active.
        # Real current sessions use the timestamp assigned by the loop.
        if not _SESSION_TS_RE.fullmatch(session_ts):
            return None

        # A newly-started orchestrator can be queried before its first audit
        # artifact is flushed. The timestamp format above keeps this narrow
        # while allowing that live run to resolve.
        if orchestrator_lock_status(repo_root).get("held"):
            return session_ts
        return None

    pointer_path = logs_dir / _LAST_RUN_POINTER
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        if pointer.get("session_ts"):
            return pointer["session_ts"]
    except (OSError, json.JSONDecodeError):
        pass
    # Fall back to the newest orchestrate-*.log on disk — the filename's
    # timestamp segment sorts lexicographically, so the last one is newest.
    if not logs_dir.exists():
        return None
    logs = sorted(logs_dir.glob("orchestrate-*.log"))
    if not logs:
        return None
    prefix = "orchestrate-"
    name = logs[-1].stem
    return name[len(prefix):] if name.startswith(prefix) else None


def _resolve_active_run_session_ts(repo_root: Path) -> str | None:
    """Return a run session only while an orchestrate loop still owns its lock.

    Standalone ``lanegate review`` and drift checks must not append their progress
    to the last completed run merely because its pointer remains on disk.
    """
    if not orchestrator_lock_status(repo_root).get("held"):
        return None
    return _resolve_run_session_ts(repo_root, None)


def _load_current_tickets(cfg: dict, repo_root: Path) -> list[dict]:
    """Load tickets when the configured directory exists, otherwise return none.

    Run-report consumers are also used while a repository is being initialized
    or after ticket metadata was intentionally removed.  Historical run data
    remains useful in either case, so a missing live ticket directory must not
    prevent it from being displayed.
    """
    tickets_dir = repo_root / cfg.get("tickets_dir", ".lanegate/tickets")
    if not tickets_dir.is_dir():
        return []
    tickets, _ = load_all_tickets(tickets_dir, cfg.get("ticket_prefix", "TICK"), cfg)
    return tickets


def _load_run_events(repo_root: Path, session_ts: str) -> list[dict]:
    path = _run_events_path(repo_root, session_ts)
    events: list[dict] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return events
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def read_executor_events(repo_root: Path, session_ts: str | None = None) -> list[dict]:
    """Read only normalized progress records for a run.

    Event logs also contain lifecycle diagnostics intended for local recovery.
    This public helper deliberately exposes only the bounded executor event
    schema so API and UI callers can never receive raw executor output from a
    malformed or hand-edited JSONL line.
    """
    resolved = _resolve_run_session_ts(repo_root, session_ts)
    if resolved is None:
        return []
    safe_events: list[dict] = []
    for raw in _load_run_events(repo_root, resolved):
        if raw.get("event") != "executor_progress" or not raw.get("ticket_id"):
            continue
        progress = raw.get("progress")
        if not isinstance(progress, dict):
            continue
        safe_events.append(
            {
                "ts": raw.get("ts"),
                "event": "executor_progress",
                "ticket_id": str(raw["ticket_id"])[:96],
                "progress": ExecutorEvent.from_dict(progress).to_dict(),
            }
        )
    return safe_events


def summarize_executor_events(events: list[dict]) -> dict:
    """Aggregate safe progress events by execution phase, tool, and tests."""
    phases: dict[str, int] = {}
    tools: dict[str, int] = {}
    tests = {"pass": 0, "fail": 0, "running": 0}
    providers: dict[str, float] = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0}
    for entry in events:
        progress = entry.get("progress") or {}
        phase = progress.get("phase")
        if phase:
            phases[phase] = phases.get(phase, 0) + 1
        tool = progress.get("tool_category")
        if tool:
            tools[tool] = tools.get(tool, 0) + 1
        test = progress.get("test_summary") or {}
        status = test.get("status")
        if status in tests:
            tests[status] += 1
        usage = progress.get("provider_usage") or {}
        for key in providers:
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                providers[key] += value
    return {
        "phases": phases,
        "tool_categories": tools,
        "tests": {key: value for key, value in tests.items() if value},
        "provider_usage": {key: value for key, value in providers.items() if value},
    }


def read_log_page(log_path: Path, offset: int, limit: int) -> dict:
    """Read one bounded page of lines from a run's plaintext log file.

    Line numbering matches the SSE log stream's event ids (1-indexed,
    assigned by position in the file — see api._stream_log_events), so a
    page fetched here can be spliced directly against a live-streamed tail
    without a separate id scheme to reconcile. offset/limit are assumed
    non-negative and limit > 0 (validated by the caller); an offset beyond
    the end of the file yields an empty page rather than an error.
    """
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    total = len(lines)
    page = lines[offset : offset + limit]
    raw_next_offset = offset + len(page)
    next_offset: int | None = None if raw_next_offset >= total else raw_next_offset
    return {"lines": page, "total_count": total, "next_offset": next_offset}


def read_logs_paginated(
    repo_root: Path, run_id: str, offset: int = 0, limit: int = 100
) -> dict | None:
    """Read paginated raw audit history for a given run session or run_id.

    Prefer the durable human-readable orchestrator log (including its archive)
    whenever it remains available. The JSONL event file is a bounded
    structured recovery/progress record, not the raw audit transcript, and is
    used only for older runs whose text log has been purged.
    """
    if run_id.startswith("action-") and "/" not in run_id and "\\" not in run_id:
        path = _action_events_path(repo_root, run_id)
        if not path.is_file():
            return None
        events: list[dict] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                event.setdefault(
                    "message",
                    " ".join(
                        f"{key}={event[key]}"
                        for key in ("event", "action_type", "ticket_id", "status")
                        if event.get(key) is not None
                    ),
                )
                event["kind"] = "structured"
                events.append(event)
        except (OSError, json.JSONDecodeError):
            return None
        response_run_id = run_id
    else:
        session_ts = _resolve_run_session_ts(repo_root, None if run_id == "current" else run_id)
        if not session_ts:
            return None
        response_run_id = session_ts
        events = []
        logs_dir = repo_root / ".lanegate" / "logs"
        raw_log_name = f"orchestrate-{session_ts}.log"
        raw_log_paths = (logs_dir / raw_log_name, logs_dir / "archive" / raw_log_name)
        raw_log_path = next((path for path in raw_log_paths if path.is_file()), None)
        if raw_log_path is not None:
            try:
                raw = raw_log_path.read_text(encoding="utf-8")
                for line in raw.splitlines():
                    events.append({"ts": "", "event": "log", "message": line})
            except OSError:
                return None
        else:
            events_path = _run_events_path(repo_root, session_ts)
            if not events_path.is_file():
                return None
            try:
                raw = events_path.read_text(encoding="utf-8")
                for line in raw.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                        if isinstance(ev, dict) and "message" not in ev:
                            if "line" in ev:
                                ev["message"] = str(ev["line"])
                            else:
                                parts = []
                                for k in ("event", "ticket_id", "status", "reason"):
                                    if k in ev:
                                        parts.append(f"{k}={ev[k]}")
                                ev["message"] = " ".join(parts) if parts else str(ev)
                        events.append(ev)
                    except json.JSONDecodeError:
                        events.append({"ts": "", "event": "log", "message": line})
            except OSError:
                return None

    total_count = len(events)
    offset = max(0, offset)
    limit = max(1, limit)
    events_slice = events[offset : offset + limit]

    # This is the HTTP-facing projection of a durable raw audit artifact. Keep
    # line count and order intact for pagination while applying the same secret
    # redaction policy used by executor events. The local artifact itself is
    # deliberately left unchanged for operator recovery.
    for event in events_slice:
        message = event.get("message")
        if message is not None:
            event["message"] = redact_transcript_text(str(message))
            metadata = semantic_line_metadata(event["message"])
            if event.get("level") not in {"error", "warning", "success", "info"}:
                event["level"] = metadata["level"]
            event.setdefault("style", metadata["style"])
            if event.get("event") != "log":
                event["kind"] = "structured"
            elif not event.get("kind"):
                event["kind"] = metadata["kind"]

    next_offset = (
        (offset + len(events_slice)) if (offset + len(events_slice) < total_count) else None
    )

    return {
        "run_id": response_run_id,
        "events": events_slice,
        "total_count": total_count,
        "offset": offset,
        "limit": limit,
        "next_offset": next_offset,
    }


def _iso_duration_seconds(start_iso: str | None, end_iso: str | None) -> float | None:
    if not start_iso or not end_iso:
        return None
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        start = datetime.datetime.strptime(start_iso, fmt)
        end = datetime.datetime.strptime(end_iso, fmt)
    except ValueError:
        return None
    return (end - start).total_seconds()


def _collect_live_lanegate_processes(cfg: dict, repo_root: Path) -> list[dict]:
    """Enumerate every process lanegate's own state files claim is running.

    Cross-checks each recorded PID against pid_alive() regardless of whether
    anything (orchestrate's own reconciliation loop, resume-watch, etc.) is
    currently watching it. A per-ticket executor marker is only ever written
    while that ticket's executor subprocess is literally running and is
    removed right after it exits (see on_process_start / _remove_executor_
    markers above) — so a live PID here whose ticket has already moved past
    in_progress, or whose orchestrator lock is no longer held by anyone,
    means the subprocess outlived the run that spawned it (TICK-246).
    """
    procs: list[dict] = []
    state = repo_root / ".lanegate"

    lock = orchestrator_lock_status(repo_root)
    if lock.get("pid") is not None:
        procs.append(
            {
                "kind": "orchestrator-lock",
                "pid": lock["pid"],
                "alive": bool(lock["alive"]),
                "detail": "main orchestrate loop",
                "orphaned": False,
            }
        )

    for name, filename in (
        ("resume-watch", "resume-watch.pid"),
        ("watch", "watch.pid"),
        ("notify-watch", "notify-watch.pid"),
    ):
        pid_path = state / filename
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        procs.append(
            {
                "kind": name,
                "pid": pid,
                "alive": pid_alive(pid),
                "detail": f"{name} daemon",
                "orphaned": False,
            }
        )

    all_tickets = _load_current_tickets(cfg, repo_root)
    tickets_by_id = {t["id"]: t for t in all_tickets}
    ticket_status = {tid: t.get("status") for tid, t in tickets_by_id.items()}
    orchestrator_alive = bool(lock.get("alive"))
    # Same per-run dispatch record board.py's `exec:` label trusts (TICK-282)
    # — falls back to static config resolution only when no dispatch record
    # exists for the ticket (e.g. a manually-started worktree).
    dispatch_executors = latest_dispatch_executors(repo_root)

    for pid_path in sorted(state.glob("*.pid")):
        tid = pid_path.stem
        if tid not in ticket_status:
            continue  # not a per-ticket executor marker
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        alive = pid_alive(pid)
        status = ticket_status.get(tid)
        is_orchestrated = pid_path.with_suffix(".orchestrated").is_file()
        orphaned = alive and (status != "in_progress" or (is_orchestrated and not orchestrator_alive))
        executor_instance = dispatch_executors.get(tid)
        if not executor_instance:
            ticket = tickets_by_id.get(tid)
            if ticket is not None:
                route = resolve_executor_route(cfg, ticket)
                executor_cfg = get_executor_config(route["implement"], cfg)
                executor_instance = executor_cfg.get("instance") or route["implement"]
        detail = f"{tid} (ticket status: {status or 'unknown'}"
        detail += f", exec:{executor_instance})" if executor_instance else ")"
        procs.append(
            {
                "kind": "ticket-executor",
                "pid": pid,
                "alive": alive,
                "detail": detail,
                "orphaned": orphaned,
                "ticket_id": tid,
                "executor_instance": executor_instance,
            }
        )

    # Direct actions have no durable child PID.  Show their recent action
    # streams alongside process state so `lanegate ps` remains the single
    # operator entry point for both live work and its immediate history.
    from lanegate.orchestrate.run_summary import list_run_summaries

    for summary in list_run_summaries(cfg, repo_root)[:10]:
        if not summary.run_id.startswith("action-"):
            continue
        action_ticket = summary.batch_tickets[0] if summary.batch_tickets else None
        procs.append(
            {
                "kind": "direct-action",
                "pid": None,
                "alive": summary.reason == RunReason.RUNNING,
                "orphaned": False,
                "recent": True,
                "action_id": summary.run_id,
                "detail": (
                    f"{action_ticket.ticket_id if action_ticket else 'unknown'} "
                    f"({action_ticket.executor if action_ticket else 'direct'}) — {summary.reason.value}"
                ),
            }
        )
    return procs


def cmd_ps(cfg: dict, repo_root: Path, *, json_output: bool = False) -> None:
    """`lanegate ps` — list every live lanegate-spawned process, orphaned or not."""
    procs = _collect_live_lanegate_processes(cfg, repo_root)
    if json_output:
        print(json.dumps(procs, indent=2))
        return
    live = [p for p in procs if p["alive"] and p["kind"] != "direct-action"]
    actions = [p for p in procs if p["kind"] == "direct-action"]
    if not live and not actions:
        print("No live lanegate-spawned processes.")
        return
    for p in live:
        flag = "  [ORPHANED]" if p["orphaned"] else ""
        print(f"  PID {p['pid']:<8} {p['kind']:<18} {p['detail']}{flag}")
    if actions:
        print("Direct actions (recent):")
        for action in actions:
            print(f"  {action['action_id']:<34} {action['detail']}")
    orphaned = [p for p in live if p["orphaned"]]
    if orphaned:
        print(
            f"\n{len(orphaned)} orphaned process(es) — alive but no longer supervised:\n"
            + "\n".join(f"  kill {p['pid']}   # {p['detail']}" for p in orphaned)
        )


def _stale_worker_recovery_hint(ticket_id: str) -> str:
    """Actionable next step for a dispatched ticket stuck without a terminal outcome."""
    return (
        f"no terminal outcome recorded for {ticket_id} and the orchestrator process "
        f"is no longer running — run `lanegate ps` to check for a stale worker, then "
        f"`lanegate run --tickets {ticket_id}` to redispatch"
    )


def _map_ticket_outcome(
    raw_outcome: str | None, raw_reason: str | None
) -> tuple[TicketOutcomeStatus, str | None, str | None]:
    if not raw_outcome:
        return TicketOutcomeStatus.SKIPPED, None, None

    r = raw_outcome.lower()
    if r in ("success", "closed", "approved", "merged", "completed"):
        return TicketOutcomeStatus.SUCCESS, None, None
    elif r in ("changes_requested", "needs_review"):
        return TicketOutcomeStatus.CHANGES_REQUESTED, None, raw_reason or "changes requested"
    elif r == "review_pending":
        # Review never produced a verdict, so this is resumable work rather
        # than a rejection or an auto-fix recommendation.
        return TicketOutcomeStatus.SKIPPED, None, raw_reason or "review pending"
    elif r == "awaiting_human_review":
        return (
            TicketOutcomeStatus.AWAITING_MERGE,
            None,
            raw_reason or "reviewer approved — run `lanegate merge` to land it",
        )
    elif r in ("skipped", "hibernated", "paused"):
        return TicketOutcomeStatus.SKIPPED, None, None
    elif r in ("failure", "failed", "error", "crashed"):
        return TicketOutcomeStatus.FAILURE, raw_reason or "failed", None
    else:
        return TicketOutcomeStatus.SKIPPED, None, None


def _latest_review_verdict(repo_root: Path, ticket_id: str) -> dict | None:
    """Return the newest valid review audit verdict for one ticket, if any."""
    bundles_dir = repo_root / ".lanegate" / "executor-runs" / canonical_id(ticket_id)
    try:
        candidates = [path for path in bundles_dir.glob("*-review") if path.is_dir()]
    except OSError:
        return None
    if not candidates:
        return None
    try:
        newest = max(candidates, key=lambda path: path.stat().st_mtime)
        verdict = json.loads((newest / "verdict.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return verdict if isinstance(verdict, dict) else None


def _enrich_reason(
    repo_root: Path, ticket_id: str, ticket: dict | None, raw_reason: str | None
) -> str | None:
    """Prefer durable reviewer context over a generic outcome event reason.

    The ticket's own applied fields come first: they're guaranteed to
    correspond to this outcome. The newest verdict.json bundle on disk is
    only a last resort — nothing ties it to the *current* outcome, so if a
    review ran but was never applied to the ticket (e.g. the ticket had
    already moved to needs_review by the time the verdict came back),
    surfacing it here would misattribute an orphaned verdict as the reason.
    """
    if ticket and ticket.get("review_summary"):
        return str(ticket["review_summary"])
    if ticket:
        summary = attention_summary(ticket)
        if summary:
            return summary
    verdict = _latest_review_verdict(repo_root, ticket_id)
    if verdict and verdict.get("notes"):
        return str(verdict["notes"])
    return raw_reason


def build_run_summary(
    cfg: dict, repo_root: Path, *, session_ts: str | None = None, tickets: list[dict] | None = None
) -> RunSummary | None:
    """Assemble a structured RunSummary for one orchestrate run from the durable event log.

    `tickets`, when given, is used instead of re-reading every ticket file from disk —
    callers building summaries for many sessions in one request (e.g. `list_run_summaries`)
    should load tickets once and pass them through rather than pay that cost per session.
    """
    resolved = _resolve_run_session_ts(repo_root, session_ts)
    if resolved is None:
        return None
    events = _load_run_events(repo_root, resolved)

    run_start = next((e for e in events if e.get("event") == "run_start"), None)
    if run_start is None:
        return None

    run_end = next((e for e in reversed(events) if e.get("event") == "run_end"), None)
    orchestrate_pid = run_start.get("pid")

    if run_end is not None:
        end_status = run_end.get("status", "completed")
    elif isinstance(orchestrate_pid, int) and pid_alive(orchestrate_pid):
        end_status = "running"
    else:
        end_status = "crashed (no run_end recorded)"

    ts_str = run_start.get("ts")
    if ts_str:
        try:
            timestamp = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            timestamp = datetime.datetime.now(datetime.UTC)
    else:
        timestamp = datetime.datetime.now(datetime.UTC)

    all_tickets = tickets if tickets is not None else _load_current_tickets(cfg, repo_root)
    tickets_by_id: dict[str, dict] = {}
    for ticket in all_tickets:
        try:
            tickets_by_id[canonical_id(ticket["id"])] = ticket
        except ValueError:
            continue

    tickets_map: dict[str, dict] = {}
    for e in events:
        tid = e.get("ticket_id")
        if not tid:
            continue
        row = tickets_map.setdefault(
            tid,
            {"ticket_id": tid, "executors": [], "dispatched_at": None, "outcomes": []},
        )

        def record_executor(value: object) -> None:
            if isinstance(value, str) and value and value not in row["executors"]:
                row["executors"].append(value)

        if e.get("event") == "ticket_dispatch":
            record_executor(e.get("executor"))
            row["dispatched_at"] = e.get("ts")
        elif e.get("event") == "executor_metrics" and isinstance(e.get("metrics"), dict):
            record_executor(e["metrics"].get("executor"))
        elif e.get("event") == "executor_progress" and isinstance(e.get("progress"), dict):
            record_executor(e["progress"].get("executor"))
        elif e.get("event") == "executor_cooldown":
            record_executor(e.get("instance"))
        elif e.get("event") == "ticket_outcome":
            row["outcomes"].append(
                {"ts": e.get("ts"), "outcome": e.get("outcome"), "reason": e.get("reason")}
            )

    report_ts = _utc_now_iso()
    batch_tickets: list[TicketOutcome] = []
    for tid, row in sorted(tickets_map.items(), key=lambda item: item[1].get("dispatched_at") or ""):
        executor = " → ".join(row.get("executors") or []) or "unknown"
        dispatched_at = row.get("dispatched_at")
        outcomes = row.get("outcomes", [])

        try:
            matched_ticket = tickets_by_id.get(canonical_id(tid))
        except ValueError:
            matched_ticket = None
        if outcomes:
            last = outcomes[-1]
            raw_outcome = last.get("outcome")
            raw_reason = last.get("reason")
            finished_at = last.get("ts")
            outcome_status, failure_reason, review_reason = _map_ticket_outcome(raw_outcome, raw_reason)
            if outcome_status == TicketOutcomeStatus.FAILURE:
                failure_reason = _enrich_reason(repo_root, tid, matched_ticket, failure_reason)
            elif outcome_status in (
                TicketOutcomeStatus.CHANGES_REQUESTED,
                TicketOutcomeStatus.AWAITING_MERGE,
            ):
                review_reason = _enrich_reason(repo_root, tid, matched_ticket, review_reason)
        elif end_status == "running":
            # Dispatched, no terminal event yet, parent orchestrator still alive.
            finished_at = None
            outcome_status = TicketOutcomeStatus.IN_PROGRESS
            failure_reason = None
            review_reason = None
        else:
            # Dispatched, no terminal event, and the orchestrator is gone —
            # a stale/interrupted worker, never reported as skipped.
            finished_at = None
            outcome_status = TicketOutcomeStatus.INTERRUPTED
            failure_reason = _stale_worker_recovery_hint(tid)
            review_reason = None

        # Elapsed duration to the report timestamp for tickets still without
        # a terminal outcome (finished_at is None) rather than a stale 0s.
        dur = _iso_duration_seconds(dispatched_at, finished_at or report_ts)
        duration_seconds = dur if dur is not None and dur >= 0 else 0.0

        batch_tickets.append(
            TicketOutcome(
                ticket_id=tid,
                executor=executor,
                outcome=outcome_status,
                duration_seconds=duration_seconds,
                failure_reason=failure_reason,
                review_reason=review_reason,
                lifecycle_summary=row.get("lifecycle_summary"),
            )
        )

    if end_status.startswith("crashed") or end_status.startswith("failure") or end_status.startswith("error"):
        reason = RunReason.FAILURE
    elif end_status == "stopped":
        reason = RunReason.STOPPED
    elif end_status == "running":
        reason = RunReason.RUNNING
    else:
        if any(t.outcome == TicketOutcomeStatus.FAILURE for t in batch_tickets):
            reason = RunReason.FAILURE
        elif any(
            t.outcome
            in (
                TicketOutcomeStatus.CHANGES_REQUESTED,
                TicketOutcomeStatus.AWAITING_MERGE,
                TicketOutcomeStatus.SKIPPED,
                TicketOutcomeStatus.INTERRUPTED,
            )
            for t in batch_tickets
        ):
            reason = RunReason.STOPPED
        else:
            reason = RunReason.SUCCESS

    return RunSummary(
        run_id=resolved,
        timestamp=timestamp,
        reason=reason,
        batch_tickets=batch_tickets,
        triggered_by=run_start.get("triggered_by", "manual"),
        trigger_reason=run_start.get("trigger_reason"),
    )


def print_run_summary(summary: RunSummary, stream=None) -> None:
    """Print terminal run summary sourced from RunSummary."""
    if stream is None:
        stream = sys.stdout
    print(f"\n[orchestrate] run summary (terminal reason: {summary.reason.value}):", file=stream)
    if not summary.batch_tickets:
        print("[orchestrate]   (no tickets dispatched)", file=stream)
    for t in summary.batch_tickets:
        dur = _format_elapsed(t.duration_seconds)
        actionable_reason = t.failure_reason or t.review_reason or t.lifecycle_summary
        reason_str = f" — {actionable_reason}" if actionable_reason else ""
        print(
            f"[orchestrate]   {t.ticket_id:12s} {t.outcome.value:20s} "
            f"executor={t.executor:14s} duration={dur}{reason_str}",
            file=stream,
        )

    try:
        from lanegate.pending_globals import check_pending_globals, format_pending_globals_notice
        pg_info = check_pending_globals(Path.cwd())
        if pg_info["has_pending"]:
            print(f"[orchestrate] {format_pending_globals_notice(pg_info)}", file=stream)
    except Exception:
        pass



def build_run_report(cfg: dict, repo_root: Path, *, session_ts: str | None = None) -> dict | None:
    """Assemble a structured report for one orchestrate run.

    Returns None when no run has ever recorded an event log (fresh repo, or
    a run predating this feature).
    """
    resolved = _resolve_run_session_ts(repo_root, session_ts)
    if resolved is None:
        return None
    events = _load_run_events(repo_root, resolved)

    run_start = next((e for e in events if e.get("event") == "run_start"), None)
    if run_start is None:
        # No durable event log for this session (e.g. a --dry-run, which still
        # writes a plain .log file but never a run_start event) — nothing to
        # report on, distinct from a real run that's still in progress.
        return None
    run_end = next((e for e in reversed(events) if e.get("event") == "run_end"), None)

    orchestrate_pid = run_start.get("pid")
    if run_end is not None:
        status = run_end.get("status", "completed")
    elif isinstance(orchestrate_pid, int) and pid_alive(orchestrate_pid):
        status = "running"
    else:
        status = "crashed (no run_end recorded)"

    tickets: dict[str, dict] = {}
    for e in events:
        tid = e.get("ticket_id")
        if not tid:
            continue
        row = tickets.setdefault(
            tid,
            {
                "ticket_id": tid,
                "executors": [],
                "dispatched_at": None,
                "outcomes": [],
                "progress_events": [],
            },
        )

        def record_executor(value: object) -> None:
            if isinstance(value, str) and value and value not in row["executors"]:
                row["executors"].append(value)

        if e.get("event") == "ticket_dispatch":
            record_executor(e.get("executor"))
            row["dispatched_at"] = e.get("ts")
            row["was_hibernated"] = e.get("was_hibernated", False)
        elif e.get("event") == "executor_metrics" and isinstance(e.get("metrics"), dict):
            record_executor(e["metrics"].get("executor"))
        elif e.get("event") == "ticket_outcome":
            row["outcomes"].append(
                {"ts": e.get("ts"), "outcome": e.get("outcome"), "reason": e.get("reason")}
            )
        elif e.get("event") == "executor_progress" and isinstance(e.get("progress"), dict):
            record_executor(e["progress"].get("executor"))
            # Re-normalize persisted data before it reaches JSON/text reports.
            row["progress_events"].append(ExecutorEvent.from_dict(e["progress"]).to_dict())
        elif e.get("event") == "executor_cooldown":
            record_executor(e.get("instance"))

    report_ts = _utc_now_iso()
    ticket_state = _load_current_tickets(cfg, repo_root)
    by_id = {ticket["id"]: ticket for ticket in ticket_state}
    for row in tickets.values():
        row["executor"] = " → ".join(row.pop("executors", [])) or "unknown"
        if row["outcomes"]:
            last = row["outcomes"][-1]
            row["final_outcome"] = last["outcome"]
            row["final_reason"] = last.get("reason")
            row["finished_at"] = last["ts"]
        elif status == "running":
            # Dispatched, no terminal event yet, parent orchestrator still alive.
            row["final_outcome"] = "in_progress"
            row["final_reason"] = None
            row["finished_at"] = None
        else:
            # Dispatched, no terminal event, and the orchestrator is gone —
            # a stale/interrupted worker, never reported as skipped.
            row["final_outcome"] = "interrupted"
            row["final_reason"] = _stale_worker_recovery_hint(row["ticket_id"])
            row["finished_at"] = None
        # Elapsed duration to the report timestamp for tickets still without
        # a terminal outcome (finished_at is None) rather than a stale "?".
        row["duration_seconds"] = _iso_duration_seconds(
            row.get("dispatched_at"), row.get("finished_at") or report_ts
        )
        row["progress_summary"] = summarize_executor_events(
            [{"progress": progress} for progress in row["progress_events"]]
        )
        lifecycle_events = by_id.get(row["ticket_id"], {}).get("lifecycle_events") or []
        if lifecycle_events:
            row["lifecycle_summary"] = lifecycle_events[-1].get("summary")

    executor_events = [
        e for e in events if e.get("event") in ("executor_cooldown", "orphan_reconciled", "orphan_reaped")
    ]
    hibernations = [
        {"ticket_id": tid, **outcome}
        for tid, row in tickets.items()
        for outcome in row["outcomes"]
        if outcome.get("outcome") == "hibernated"
    ]

    resume_history: list[dict] = []
    if run_start is not None:
        from lanegate.resume_watch import read_history_since

        resume_history = read_history_since(repo_root, run_start.get("ts", ""))

    live_procs = _collect_live_lanegate_processes(cfg, repo_root)
    orphaned_processes = [p for p in live_procs if p["orphaned"]]

    summary = build_run_summary(cfg, repo_root, session_ts=resolved)

    return {
        "session_ts": resolved,
        "status": status,
        "summary": summary.to_dict() if summary else None,
        "started_at": run_start.get("ts") if run_start else None,
        "ended_at": run_end.get("ts") if run_end else None,
        "milestone": run_start.get("milestone") if run_start else None,
        "ticket_ids": run_start.get("ticket_ids") if run_start else None,
        "pool": run_start.get("pool") if run_start else None,
        "max_parallel": run_start.get("max_parallel") if run_start else None,
        "human_review": run_start.get("human_review") if run_start else None,
        "orchestrate_pid": orchestrate_pid,
        "tickets": sorted(tickets.values(), key=lambda r: r.get("dispatched_at") or ""),
        "executor_events": executor_events,
        "hibernations": hibernations,
        "resume_watch_history": resume_history,
        "orphaned_processes": orphaned_processes,
        "log_path": str(_run_events_path(repo_root, resolved)).replace(_RUN_EVENTS_SUFFIX, ".log"),
    }


def cmd_run_report(
    cfg: dict, repo_root: Path, *, session_ts: str | None = None, json_output: bool = False
) -> None:
    """`lanegate run-report` — structured summary of an orchestrate run or action.

    With no arguments, reports on the most recently started run (via the
    last-run pointer, falling back to the newest log on disk) — no
    run-specific argument required.  An ``action-...`` session ID reports a
    direct lifecycle action from its durable action event log.
    """
    if session_ts and session_ts.startswith("action-"):
        from lanegate.orchestrate.run_summary import _build_direct_action_summary

        summary = _build_direct_action_summary(repo_root, session_ts)
        if summary is None:
            msg = f"No LaneGate action report found for {session_ts}."
            print(json.dumps({"error": msg}) if json_output else msg)
            return
        ticket = summary.batch_tickets[0] if summary.batch_tickets else None
        log_path = _action_events_path(repo_root, session_ts)
        if json_output:
            print(json.dumps({"summary": summary.to_dict(), "log_path": str(log_path)}, indent=2))
            return
        print(f"Action: {summary.run_id}   status: {summary.reason.value}")
        if ticket:
            duration = _format_elapsed(ticket.duration_seconds)
            print(
                f"  {ticket.ticket_id:12s} {ticket.outcome.value:20s} "
                f"executor={ticket.executor:14s} {duration}"
            )
        print(f"  log: {log_path}")
        return

    report = build_run_report(cfg, repo_root, session_ts=session_ts)
    if report is None:
        msg = "No LaneGate run report found — run `lanegate run` at least once."
        print(json.dumps({"error": msg}) if json_output else msg)
        return

    if json_output:
        print(json.dumps(report, indent=2))
        return

    from lanegate.config import format_display_ts

    summary_dict = report.get("summary")
    summary = RunSummary.from_dict(summary_dict) if summary_dict else None

    terminal_reason_str = f"   terminal reason: {summary.reason.value}" if summary else ""
    print(f"Run: {report['session_ts']}   status: {report['status']}{terminal_reason_str}")
    started_disp = format_display_ts(report.get("started_at"), cfg)
    ended_disp = format_display_ts(report.get("ended_at"), cfg)
    print(f"  started: {started_disp or 'unknown'}")
    print(f"  ended:   {ended_disp or '(still running / not recorded)'}")
    if report.get("milestone"):
        print(f"  milestone: {report['milestone']}")
    if report.get("ticket_ids"):
        print(f"  ticket scope: {', '.join(report['ticket_ids'])}")
    if report.get("pool"):
        print(f"  pool: {report['pool']}")
    max_parallel = report.get("max_parallel")
    print(f"  max_parallel: {max_parallel if max_parallel is not None else 'unknown'}")
    human_review = report.get("human_review")
    print(f"  human_review: {human_review if human_review is not None else 'unknown'}")
    print(f"  log: {report.get('log_path')}")

    if summary and summary.batch_tickets:
        tickets_list = summary.batch_tickets
        print(f"\nTickets ({len(tickets_list)} dispatched):")
        for t in tickets_list:
            dur = _format_elapsed(t.duration_seconds) if t.duration_seconds is not None else "?"
            executor = t.executor
            actionable_reason = t.failure_reason or t.review_reason or t.lifecycle_summary
            reason = f"  — {actionable_reason}" if actionable_reason else ""
            print(
                f"  {t.ticket_id:12s} {t.outcome.value:20s} "
                f"executor={executor:14s} {dur}{reason}"
            )
    else:
        tickets = report["tickets"]
        print(f"\nTickets ({len(tickets)} dispatched):")
        if not tickets:
            print("  (none recorded)")
        for row in tickets:
            dur = _format_elapsed(row["duration_seconds"]) if row["duration_seconds"] is not None else "?"
            executor = row.get("executor") or "?"
            reason = f"  — {row['final_reason']}" if row.get("final_reason") else ""
            print(
                f"  {row['ticket_id']:12s} {row['final_outcome']:20s} "
                f"executor={executor:14s} {dur}{reason}"
            )

    if report["executor_events"]:
        print("\nExecutor events (swaps / cooldowns / orphan reconciliations):")
        for e in report["executor_events"]:
            ts_disp = format_display_ts(e.get("ts"), cfg)
            if e["event"] == "executor_cooldown":
                print(f"  {ts_disp}  cooldown          {e.get('instance')}: {e.get('reason')}")
            else:
                print(f"  {ts_disp}  {e['event']:16s} {e.get('ticket_id', '')}: {e.get('reason', '')}")

    flow_rows = [row for row in report["tickets"] if row.get("progress_events")]
    if flow_rows:
        print("\nExecutor progress (safe structured metadata):")
        for row in flow_rows:
            flow = row["progress_summary"]
            phases = " → ".join(flow["phases"]) or "no classified phase"
            tests = flow["tests"]
            test_text = " ".join(f"{key}:{value}" for key, value in sorted(tests.items()))
            usage = flow["provider_usage"]
            usage_text = " ".join(f"{key}={value:g}" for key, value in sorted(usage.items()))
            extras = "  ".join(part for part in (test_text, usage_text) if part)
            print(f"  {row['ticket_id']:12s} {phases}" + (f"  {extras}" if extras else ""))

    if report["resume_watch_history"]:
        print("\nResume-watch activity (rate-limit wait/retry):")
        for entry in report["resume_watch_history"]:
            ids = ", ".join(entry.get("ticket_ids") or [])
            ts_disp = format_display_ts(entry.get("ts"), cfg)
            print(f"  {ts_disp}  {entry.get('event'):10s}  {ids}")

    orphaned = report["orphaned_processes"]
    if orphaned:
        print(f"\nOrphaned processes ({len(orphaned)}):")
        for p in orphaned:
            print(f"  PID {p['pid']:<8} {p['kind']:<18} {p['detail']}")
    else:
        print("\nNo orphaned processes detected.")


def cmd_orchestrate_status(cfg: dict, repo_root: Path, *, json_output: bool = False) -> None:
    status = _normalize_active_status(repo_root, cfg)
    if json_output:
        print(json.dumps(status, indent=2, sort_keys=True))
        return

    state = status.get("state")
    recon = status.get("reconciliation_state")
    tid = status.get("ticket_id")
    if state == "no-active-run":
        print("No active orchestrate executor.")
        return

    pid = status.get("executor_pid")
    session = status.get("executor_session") or "unknown"
    print(f"Orchestrate executor: {state} ({recon})")
    print(f"  ticket: {tid or 'unknown'}")
    print(f"  executor PID/session: {pid or 'unknown'} / {session}")
    print(f"  elapsed: {status.get('elapsed')}")
    print(f"  log: {status.get('log_path') or 'unknown'}")
    print(f"  audit bundle: {status.get('audit_bundle_path') or 'unknown'}")
    print(f"  last event: {status.get('last_event') or 'unknown'}")
    lock_state = status.get("orchestrator_lock_state")
    lock_pid = (status.get("orchestrator_lock") or {}).get("pid")
    print(f"  orchestrator lock: {lock_state}" + (f" (PID {lock_pid})" if lock_pid else ""))


def _stream_subprocess(
    cmd: list[str],
    cwd: str,
    out_stream=None,
    err_stream=None,
    stdin_text: str | None = None,
    on_start=None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    on_line=None,
    idle_timeout: float | None = None,
    stall_timeout: float | None = None,
    absolute_ceiling: float | None = None,
    liveness_probe=None,
    progress_probe=None,
    budget_probe=None,
) -> tuple[int, str, str, str | None]:
    """Run a subprocess, streaming stdout/stderr to the given streams.

    Returns (exit_code, captured_stdout, captured_stderr, kill_reason), where
    ``kill_reason`` is ``"idle"``, ``"stall"``, ``"ceiling"``, or
    ``"budget_exceeded"`` when this helper kills the child and otherwise
    ``None``. ``liveness_probe`` and ``progress_probe`` return timestamps from
    independent executor monitoring: a recent verified heartbeat suppresses a
    short output-idle kill, while a long absence of semantic progress still
    reaches ``stall_timeout``.

    When out_stream/err_stream are None, falls back to sys.stdout/sys.stderr.
    Using sys.stdout/sys.stderr here (rather than the fd directly) means the
    output passes through the _LogTee installed by cmd_orchestrate, so it lands
    in the log file as well as the terminal.

    Passing explicit streams (e.g. the raw log file) routes output only to
    those streams — used in compact (non-verbose) mode to suppress executor
    output from the terminal while still capturing it in the log.

    ``env``, when provided (see :func:`lanegate.executor.resolve_executor_env`,
    TICK-088 named executor instances), overrides the subprocess environment;
    ``None`` (the default) inherits the parent process environment unchanged.

    Both streams are captured (not just stderr) — some executors, notably a
    ``claude`` CLI in non-interactive/print mode, write user-facing error/JSON
    responses (including rate-limit text) to stdout rather than stderr.
    """
    if out_stream is None:
        out_stream = sys.stdout
    if err_stream is None:
        err_stream = sys.stderr

    captured_out: list[str] = []
    captured_err: list[str] = []
    last_line_ts = time.time()
    start_ts = last_line_ts

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
        creationflags=0x00000200 if sys.platform == "win32" else 0,  # CREATE_NEW_PROCESS_GROUP
        start_new_session=sys.platform != "win32",
    )
    if on_start is not None:
        on_start(proc.pid)

    guard_violation: list[BaseException] = []

    def relay(src, dst, capture: list[str] | None = None, is_stdout: bool = True):
        nonlocal last_line_ts
        for line in src:
            if capture is not None:
                capture.append(line)
            last_line_ts = time.time()
            if on_line is not None:
                try:
                    on_line(line, is_stdout)
                except Exception as exc:
                    err_stream.write(f"{exc}\n")
                    err_stream.flush()
                    from lanegate.orchestrate.pool import WorktreeGuardViolation

                    if isinstance(exc, WorktreeGuardViolation):
                        guard_violation.append(exc)
            dst.write(line)
            dst.flush()

    t_out = threading.Thread(target=relay, args=(proc.stdout, out_stream, captured_out, True))
    t_err = threading.Thread(target=relay, args=(proc.stderr, err_stream, captured_err, False))
    t_out.start()
    t_err.start()
    if stdin_text is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            # The child exited or closed stdin before reading it (for example
            # a command that never touches stdin) — not our failure to report.
            pass

    kill_reason: str | None = None
    budget_msg: str | None = None
    if idle_timeout is None and absolute_ceiling is None and budget_probe is None:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_reason = "timeout"
    else:
        while proc.poll() is None:
            if guard_violation:
                kill_reason = "worktree_violation"
                break
            if budget_probe is not None:
                try:
                    b_res = budget_probe()
                    if b_res:
                        kill_reason = "budget_exceeded"
                        budget_msg = str(b_res)
                        break
                except Exception:
                    pass
            now = time.time()
            # ``timeout`` is the flat process limit used by completion-only
            # executors. Streaming dispatches instead use their explicit
            # idle/stall/ceiling watchdogs; do not let a short flat timeout
            # override those richer liveness signals merely because they also
            # carry a budget probe.
            if (
                timeout is not None
                and idle_timeout is None
                and absolute_ceiling is None
                and now - start_ts > timeout
            ):
                kill_reason = "timeout"
                break
            if absolute_ceiling is not None and now - start_ts > absolute_ceiling:
                kill_reason = "ceiling"
                break
            heartbeat_ts = None
            progress_ts = None
            if liveness_probe is not None:
                try:
                    heartbeat_ts = liveness_probe()
                except Exception:
                    pass
            if progress_probe is not None:
                try:
                    progress_ts = progress_probe()
                except Exception:
                    pass
            live_ts = max(
                last_line_ts,
                float(heartbeat_ts) if isinstance(heartbeat_ts, (int, float)) else 0.0,
            )
            # When a caller supplies a semantic-progress probe, do not let
            # arbitrary raw output disguise a stalled executor.  Callers
            # without that richer signal retain the legacy output-based timer.
            semantic_progress_ts = (
                float(progress_ts)
                if isinstance(progress_ts, (int, float))
                else last_line_ts
            )
            if stall_timeout is not None and now - semantic_progress_ts > stall_timeout:
                kill_reason = "stall"
                break
            if idle_timeout is not None and now - live_ts > idle_timeout:
                kill_reason = "idle"
                break
            time.sleep(0.05)
    if kill_reason is None and guard_violation:
        kill_reason = "worktree_violation"
    if kill_reason is not None:
        _terminate_process_tree(proc)
        if kill_reason == "timeout":
            message = f"timed out after {timeout}s"
        elif kill_reason == "idle":
            message = f"was idle for {idle_timeout}s"
        elif kill_reason == "stall":
            message = f"made no semantic progress for {stall_timeout}s"
        elif kill_reason == "budget_exceeded":
            message = f"budget cap exceeded: {budget_msg if budget_msg else 'budget ceiling reached'}"
        elif kill_reason == "worktree_violation":
            message = str(guard_violation[0]) if guard_violation else "wrote outside its assigned worktree"
        else:
            message = f"reached absolute ceiling of {absolute_ceiling}s"
        msg = f"\n[orchestrate] ERROR: executor process (PID {proc.pid}) {message}\n"
        if out_stream is not None:
            out_stream.write(msg)
            out_stream.flush()
        captured_err.append(msg)

    t_out.join(timeout=1)
    t_err.join(timeout=1)
    captured_stdout = "".join(captured_out)
    captured_stderr = "".join(captured_err)
    return (124 if kill_reason is not None else proc.returncode), captured_stdout, captured_stderr, (
        None if kill_reason == "timeout" else kill_reason
    )
