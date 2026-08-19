"""
api.py — loopback-only HTTP server implementing the lanegate API.

Routes:
  GET  /api/board              → board state (tickets grouped by status + pipeline)
  GET  /api/tickets            → flat list of all tickets
  GET  /api/tickets/{id}       → full ticket detail (frontmatter, body, review, findings)
  GET  /api/blocked            → blocked/changes-requested review queue
  GET  /api/diff/{ticket_id}   → structured diff for ticket branch vs main
  POST /api/runs/start          → start an addressable LaneGate run
  POST /api/runs/stop           → request graceful run shutdown
  POST /api/orchestrate/*       → backward-compatible aliases for /api/runs/*
  GET  /api/status             → active run status
  GET  /api/v1/analyze/status  → active standalone-analysis status
  GET  /api/runs/current       → current API-started run state
  GET  /api/runs/current/logs/stream → SSE stream of run log lines
  GET  /api/runs/current/logs  → paginated (offset/limit) run log lines
  GET  /api/log                → legacy alias for the current log stream
  GET  /api/config             → sanitized resolved config, repo paths, API metadata
  GET  /api/pools               → pools.<name>.executors lists + live dispatch state
  PUT  /api/pools/{name}/executors → persist a reordered executors list for one pool
"""

from __future__ import annotations

import datetime
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from lanegate.executor_events import redact_transcript_text
from lanegate.logs import semantic_line_metadata
from lanegate.orchestrate.loop import _pool_state_path
from lanegate.pidutil import pid_alive
from lanegate.timeutil import utc_now_iso as _utc_now_iso

_DEFAULT_PORT = 8000
_BIND_HOST = "127.0.0.1"
_RUN_STATE_FILE = "api-run-current.json"
_STREAM_POLL_SECONDS = 0.5
_API_TOKEN_FILE_PREFIX = "api-token-"
_API_TOKEN_HEADER = "X-LaneGate-Token"


def _json_response(handler: BaseHTTPRequestHandler, data: object, status: int = 200) -> None:
    body = json.dumps(data, default=str).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _error_response(handler: BaseHTTPRequestHandler, message: str, status: int = 400) -> None:
    _json_response(handler, {"error": message}, status)


def _api_token_path(repo_root: Path, port: int) -> Path:
    """Return the gitignored local token file for one API listener."""
    return repo_root / ".lanegate" / f"{_API_TOKEN_FILE_PREFIX}{port}"


def _write_api_token(repo_root: Path, port: int, token: str) -> Path:
    """Persist a server token for local native clients with owner-only access."""
    path = _api_token_path(repo_root, port)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as token_file:
        token_file.write(f"{token}\n")
    path.chmod(0o600)
    return path


# Config only ever stores *references* to secrets (e.g. api_key_env holds an
# environment variable name, never a key value), but /api/config redacts
# anything shaped like a credential as defense-in-depth against a future
# field accidentally carrying a real secret. The "_env" exemption keeps
# legitimate env-var-name fields like api_key_env readable.
_SECRET_KEY_RE = re.compile(r"(secret|token|password|credential|key)", re.IGNORECASE)
_SAFE_KEY_SUFFIX_RE = re.compile(r"_env$", re.IGNORECASE)


def _redact_secrets(value: object) -> object:
    if isinstance(value, dict):
        redacted: dict[object, object] = {}
        for k, v in value.items():
            if (
                isinstance(k, str)
                and _SECRET_KEY_RE.search(k)
                and not _SAFE_KEY_SUFFIX_RE.search(k)
            ):
                redacted[k] = None if v is None else "[redacted]"
            else:
                redacted[k] = _redact_secrets(v)
        return redacted
    if isinstance(value, list):
        return [_redact_secrets(v) for v in value]
    return value


def _sanitized_config_payload(cfg: dict, repo_root: Path, *, api_host: str, api_port: int | None) -> dict:
    """Build the read-only /api/config payload: resolved settings, repo paths,
    and API metadata, with credential-shaped fields redacted (see
    _redact_secrets). Deliberately field-by-field (not a raw cfg dump) so an
    unrelated future config key can't leak through unreviewed.
    """
    environments = [
        {
            "name": env.get("name"),
            "branch": env.get("branch"),
            "from": env.get("from"),
            "trigger": env.get("trigger"),
            "sync": env.get("sync"),
        }
        for env in (cfg.get("environments") or [])
        if isinstance(env, dict)
    ]

    return {
        "repo_root": str(repo_root),
        "ticket_prefix": cfg.get("ticket_prefix"),
        "tickets_dir": cfg.get("tickets_dir"),
        "worktrees_dir": cfg.get("worktrees_dir"),
        "executor": cfg.get("executor"),
        "executor_steps": _redact_secrets(cfg.get("executor_steps") or {}),
        "executors": _redact_secrets(cfg.get("executors") or {}),
        "models": cfg.get("models") or {},
        "max_parallel": cfg.get("max_parallel"),
        "default_milestone": cfg.get("default_milestone"),
        "on_rate_limit": cfg.get("on_rate_limit"),
        "github_pr": cfg.get("github_pr"),
        "commit_status_changes": cfg.get("commit_status_changes"),
        "environments": environments,
        "api": {
            "host": api_host,
            "port": api_port,
        },
    }


def _read_pool_state(repo_root: Path) -> dict:
    """Read the rotation/dispatch state the run engine persists per pool
    so /api/pools can show live load alongside static config."""
    try:
        return json.loads(_pool_state_path(repo_root).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _pools_payload(cfg: dict, repo_root: Path) -> dict:
    """Build the /api/pools payload: each pool's executors in their
    configured (preference) order, plus persisted rotation/dispatch state.
    """
    pools_cfg = cfg.get("pools") or {}
    state = _read_pool_state(repo_root)
    default_pool = cfg.get("default_pool")
    pools = []
    for name in sorted(pools_cfg):
        pool = pools_cfg[name]
        executors = list(pool.get("executors") or [])
        pool_state = state.get(name) or {}
        dispatch_counts = pool_state.get("dispatch_counts") or {}
        pools.append(
            {
                "name": name,
                "strategy": pool.get("strategy", "least-loaded"),
                "executors": executors,
                "dispatch_counts": {ex: dispatch_counts.get(ex, 0) for ex in executors},
                "rr_index": pool_state.get("rr_index", 0),
                "default": name == default_pool,
            }
        )
    return {"pools": pools}


def _state_path(repo_root: Path) -> Path:
    state_dir = repo_root / ".lanegate"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / _RUN_STATE_FILE


def _write_run_state(repo_root: Path, data: dict) -> dict:
    payload = dict(data)
    payload["updated_at"] = _utc_now_iso()
    path = _state_path(repo_root)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return payload


def _read_run_state(repo_root: Path) -> dict | None:
    try:
        return json.loads(_state_path(repo_root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "run_id": None,
            "status": "unknown",
            "error": f"could not read API run state: {exc}",
        }


def _proc_returncode(proc: subprocess.Popen | None) -> int | None:
    if proc is None or not hasattr(proc, "poll"):
        return None
    return proc.poll()


def _get_resume_watch_status(repo_root: Path) -> dict | None:
    """Read resume-watch daemon state from PID file and history JSONL."""
    try:
        from lanegate.resume_watch import get_daemon_status
        return get_daemon_status(repo_root)
    except Exception:
        return None


def _run_payload(repo_root: Path, processes: dict[str, subprocess.Popen], run: dict | None) -> dict:
    from lanegate.analyze import get_active_analysis_status
    from lanegate.orchestrate import get_all_active_statuses, get_orchestration_status

    orchestration = get_orchestration_status(repo_root)
    active_statuses = get_all_active_statuses(repo_root)
    analysis = get_active_analysis_status(repo_root)
    resume_watch_status = _get_resume_watch_status(repo_root)

    worker_statuses = [
        s for s in active_statuses
        if s.get("ticket_id") or s.get("executor_pid")
    ]
    if not worker_statuses and (orchestration.get("ticket_id") or orchestration.get("executor_pid")):
        worker_statuses = [orchestration]

    tickets = list(dict.fromkeys(s["ticket_id"] for s in worker_statuses if s.get("ticket_id")))
    workers = [
        {
            "ticket_id": s.get("ticket_id"),
            "executor_pid": s.get("executor_pid"),
            "state": s.get("state"),
            "reconciliation_state": s.get("reconciliation_state"),
            "resolved_driver": s.get("resolved_driver"),
            "resolved_executor": s.get("resolved_executor"),
            "resolved_model": s.get("resolved_model"),
            "last_executor_event": s.get("last_executor_event"),
        }
        for s in worker_statuses
    ]


    if not run:
        # No run was started through this API instance's own /api/runs/start
        # endpoint, but a `lanegate run` process started independently (e.g.
        # from the CLI) still shows up in `orchestration` via the on-disk
        # executor marker — reflect that instead of always reporting idle,
        # or the Run screen falsely says "no active run" while one is live.
        lock = orchestration.get("orchestrator_lock") or {}
        active = bool(orchestration.get("active") or lock.get("held"))
        state = orchestration.get("state")
        from lanegate.orchestrate.run_report import _resolve_active_run_session_ts

        # ``executor_session`` identifies a single ticket dispatch, not the
        # board-clearing run. The Run screen uses this value to request the raw
        # audit log, so it must be the durable run session instead.
        run_id = _resolve_active_run_session_ts(repo_root) if active else None
        if not run_id and active and isinstance(lock.get("pid"), int):
            run_id = f"orchestrator-{lock['pid']}"
        return {
            "run_id": run_id if active else None,
            "status": state if active and state in ("running", "between-dispatches") else ("running" if active else "idle"),
            "started_at_iso": orchestration.get("started_at_iso") if active else None,
            "orchestrator_pid": lock.get("pid") if active else None,
            "process_alive": active,
            "tickets": tickets if active else [],
            "workers": workers if active else [],
            "last_event_id": orchestration.get("heartbeat_count") if active else None,
            "orchestration": orchestration,
            "analysis": analysis,
            "batch_line": orchestration.get("batch_line") or "",
            "underfilled_reason": orchestration.get("underfilled_reason"),
            "resume_watch_status": resume_watch_status,
        }

    payload = dict(run)
    run_id = payload.get("run_id")
    proc = processes.get(run_id) if isinstance(run_id, str) else None
    returncode = _proc_returncode(proc)
    if returncode is not None:
        payload["process_exit_code"] = returncode

    pid = payload.get("orchestrator_pid")
    if proc is not None and returncode is None:
        process_alive = True
    else:
        process_alive = bool(isinstance(pid, int) and pid_alive(pid))

    if payload.get("stop_requested"):
        status = "stopping" if process_alive else "stopped"
    elif process_alive:
        status = (
            "between-dispatches"
            if orchestration.get("state") == "between-dispatches"
            else "running"
        )
    elif returncode is not None or payload.get("process_exit_code") is not None:
        status = "finished"
    else:
        status = payload.get("status") or "unknown"

    payload.update(
        {
            "status": status,
            "process_alive": process_alive,
            "tickets": tickets,
            "workers": workers,
            "last_event_id": orchestration.get("heartbeat_count"),
            "orchestration": orchestration,
            "analysis": analysis,
            "batch_line": orchestration.get("batch_line") or "",
            "underfilled_reason": orchestration.get("underfilled_reason"),
            "resume_watch_status": resume_watch_status,
        }
    )
    return payload


def _with_active_dispatch(ticket: dict, orchestration: dict) -> dict:
    """Attach active dispatch fields without re-resolving static config."""
    result = dict(ticket)
    fields = ("resolved_driver", "resolved_executor", "resolved_model")
    if ticket.get("id") == orchestration.get("ticket_id"):
        result.update({field: orchestration.get(field) for field in fields})
    else:
        result.update({field: None for field in fields})
    return result


def _build_run_cmd(params: dict) -> list[str]:
    """Build the canonical CLI invocation for an API-started LaneGate run."""
    cmd = [sys.executable, "-m", "lanegate.cli", "run"]

    max_parallel = params.get("max_parallel")
    if max_parallel is not None:
        cmd.extend(["--max", str(max_parallel)])

    milestone = params.get("milestone")
    if milestone:
        cmd.extend(["--milestone", str(milestone)])

    human_review = params.get("human_review")
    if human_review:
        cmd.extend(["--human-review", str(human_review)])

    if params.get("all_milestones") or params.get("all"):
        cmd.append("--all")
    if params.get("dry_run"):
        cmd.append("--dry-run")
    if params.get("no_auto_analyze"):
        cmd.append("--no-auto-analyze")
    if params.get("no_recover"):
        cmd.append("--no-recover")
    if params.get("verbose"):
        cmd.append("--verbose")

    return cmd


def _latest_orchestrate_log(repo_root: Path) -> Path | None:
    logs_dir = repo_root / ".lanegate" / "logs"
    try:
        logs = [p for p in logs_dir.glob("orchestrate-*.log") if p.is_file()]
    except OSError:
        return None
    return max(logs, key=lambda p: p.stat().st_mtime) if logs else None


def _resolve_log_path(repo_root: Path, run: dict | None, status: dict | None = None) -> Path | None:
    candidates: list[Path] = []
    if status and status.get("log_path"):
        candidates.append(Path(str(status["log_path"])))
    if run and run.get("log_path"):
        candidates.append(Path(str(run["log_path"])))
    latest = _latest_orchestrate_log(repo_root)
    if latest is not None:
        candidates.append(latest)
    candidates.append(repo_root / ".lanegate" / "watch.log")

    for path in candidates:
        if not path.is_absolute():
            path = repo_root / path
        if path.is_file():
            return path
    return None


def _sse_event(event: dict) -> bytes:
    event_type = event.get("type") or "message"
    event_id = event.get("id")
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_type}")
    lines.append(f"data: {json.dumps(event, default=str)}")
    lines.append("")
    lines.append("")
    return "\n".join(lines).encode()


def _stream_log_events(
    handler: BaseHTTPRequestHandler,
    repo_root: Path,
    processes: dict[str, subprocess.Popen],
    *,
    follow: bool,
) -> None:
    run = _read_run_state(repo_root)
    payload = _run_payload(repo_root, processes, run)
    log_path = _resolve_log_path(repo_root, run, payload.get("orchestration"))

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive" if follow else "close")
    handler.end_headers()
    if not follow:
        handler.close_connection = True

    def write_event(event: dict) -> bool:
        try:
            handler.wfile.write(_sse_event(event))
            handler.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    if log_path is None:
        write_event(
            {
                "id": "0",
                "type": "log_status",
                "timestamp": _utc_now_iso(),
                "run_id": payload.get("run_id"),
                "message": "no log file available",
                "data": {"path": None},
            }
        )
        return

    line_no = 0

    def emit_existing() -> bool:
        nonlocal line_no
        try:
            lines = log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines[line_no:]:
            line_no += 1
            message = redact_transcript_text(line)
            if not write_event(
                {
                    "id": str(line_no),
                    "type": "log",
                    "timestamp": _utc_now_iso(),
                    "run_id": payload.get("run_id"),
                    "ticket_id": (payload.get("orchestration") or {}).get("ticket_id"),
                    "message": message,
                    **semantic_line_metadata(message),
                    "data": {"path": str(log_path)},
                }
            ):
                return False
        return True

    if not emit_existing() or not follow:
        return

    while True:
        current = _run_payload(repo_root, processes, _read_run_state(repo_root))
        if current.get("status") not in {"running", "stopping"}:
            emit_existing()
            return
        time.sleep(_STREAM_POLL_SECONDS)
        if not emit_existing():
            return


def make_handler(
    cfg: dict,
    repo_root: Path,
    processes_param: dict[str, subprocess.Popen] | None = None,
    api_token_param: str | None = None,
):
    """Return a BaseHTTPRequestHandler subclass bound to cfg and repo_root."""
    processes: dict[str, subprocess.Popen] = {} if processes_param is None else processes_param
    api_token: str = api_token_param or secrets.token_urlsafe(32)

    class _ApiHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # suppress default Apache-style access log

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")
            query = parse_qs(parsed.query)

            if path == "/api/board":
                self._handle_board()
            elif path == "/api/tickets":
                self._handle_tickets()
            elif path.startswith("/api/tickets/"):
                ticket_id = path[len("/api/tickets/"):]
                self._handle_ticket_detail(ticket_id)
            elif path.startswith("/api/diff/"):
                ticket_id = path[len("/api/diff/"):]
                self._handle_diff(ticket_id)
            elif path == "/api/blocked":
                self._handle_blocked()
            elif path == "/api/status":
                self._handle_status()
            elif path == "/api/runs":
                self._handle_runs()
            elif path in ("/api/v1/analyze/status", "/api/analyze/status"):
                self._handle_analyze_status()
            elif path == "/api/runs/current":
                self._handle_current_run()
            elif path == "/api/runs/current/logs/stream":
                self._handle_log(follow=query.get("follow", ["1"])[0] != "0")
            elif path == "/api/runs/current/logs":
                self._handle_log_page(query)
            elif match_logs := re.match(r"^/api/(?:orchestrate|runs)/([^/]+)/logs$", path):
                self._handle_run_logs(match_logs.group(1), query)
            elif match_events := re.match(r"^/api/(?:orchestrate|runs)/([^/]+)/events$", path):
                self._handle_run_events(match_events.group(1), query)
            elif path.startswith("/api/runs/"):
                run_id = path[len("/api/runs/"):]
                self._handle_run_summary(run_id)
            elif path == "/api/log":
                self._handle_log(follow=False)
            elif path in ("/api/config", "/api/settings"):
                self._handle_config()
            elif path == "/api/pools":
                self._handle_pools()
            else:
                _error_response(self, "not found", 404)

        def do_PUT(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")
            match = re.match(r"^/api/pools/([^/]+)/executors$", path)
            if match:
                if self._authorize_mutation():
                    self._handle_pool_executors_update(match.group(1))
            else:
                _error_response(self, "not found", 404)

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")

            if path in ("/api/runs/start", "/api/orchestrate/start"):
                if self._authorize_mutation():
                    self._handle_run_start()
            elif path in ("/api/runs/stop", "/api/orchestrate/stop"):
                if self._authorize_mutation():
                    self._handle_run_stop()
            else:
                _error_response(self, "not found", 404)

        # ── GET handlers ──────────────────────────────────────────────────────

        def _handle_board(self):
            from lanegate.board import get_board_state

            try:
                data = get_board_state(cfg, repo_root)
                _json_response(self, data)
            except Exception as exc:
                _error_response(self, str(exc), 500)

        def _handle_tickets(self):
            from lanegate.board import get_tickets

            try:
                tickets = get_tickets(cfg, repo_root)
                from lanegate.orchestrate import get_orchestration_status

                orchestration = get_orchestration_status(repo_root)
                _json_response(
                    self,
                    {"tickets": [_with_active_dispatch(ticket, orchestration) for ticket in tickets]},
                )
            except Exception as exc:
                _error_response(self, str(exc), 500)

        def _handle_diff(self, ticket_id: str):
            from lanegate.ticket import get_ticket_diff

            if not ticket_id:
                _error_response(self, "ticket_id is required", 400)
                return
            try:
                from lanegate.config import resolve_trunk_branch

                data = get_ticket_diff(
                    ticket_id,
                    repo_root,
                    base=resolve_trunk_branch(cfg, repo_root),
                )
                _json_response(self, data)
            except Exception as exc:
                _error_response(self, str(exc), 500)

        def _handle_ticket_detail(self, ticket_id: str):
            from lanegate.ticket import get_ticket_detail

            if not ticket_id:
                _error_response(self, "ticket_id is required", 400)
                return
            try:
                data = get_ticket_detail(ticket_id, cfg, repo_root)
                if data.get("error"):
                    _error_response(self, data["error"], data.get("status", 400))
                else:
                    from lanegate.orchestrate import get_orchestration_status

                    _json_response(self, _with_active_dispatch(data, get_orchestration_status(repo_root)))
            except Exception as exc:
                _error_response(self, str(exc), 500)

        def _handle_blocked(self):
            from lanegate.board import get_blocked_queue

            try:
                data = get_blocked_queue(cfg, repo_root)
                _json_response(self, data)
            except Exception as exc:
                _error_response(self, str(exc), 500)

        def _handle_status(self):
            from lanegate.orchestrate import get_orchestration_status

            try:
                data = get_orchestration_status(repo_root)
                _json_response(self, data)
            except Exception as exc:
                _error_response(self, str(exc), 500)

        def _handle_analyze_status(self):
            from lanegate.analyze import get_active_analysis_status

            data = get_active_analysis_status(repo_root)
            if data is None:
                _error_response(self, "no active analysis", 404)
                return
            _json_response(self, data)

        def _handle_current_run(self):
            try:
                data = _run_payload(repo_root, processes, _read_run_state(repo_root))
                _json_response(self, data)
            except Exception as exc:
                _error_response(self, str(exc), 500)

        def _handle_runs(self):
            from lanegate.orchestrate.run_summary import list_run_summaries

            try:
                summaries = list_run_summaries(cfg, repo_root)
                _json_response(self, {"runs": [s.to_dict() for s in summaries]})
            except Exception as exc:
                _error_response(self, str(exc), 500)

        def _handle_run_summary(self, run_id: str):
            from lanegate.orchestrate.run_summary import build_run_summary

            try:
                summary = build_run_summary(cfg, repo_root, session_ts=run_id)
                if summary is None:
                    _error_response(self, f"run summary not found: {run_id}", 404)
                else:
                    _json_response(self, summary.to_dict())
            except Exception as exc:
                _error_response(self, str(exc), 500)

        def _handle_run_logs(self, run_id: str, query: dict[str, list[str]]):
            from lanegate.orchestrate.run_report import read_logs_paginated

            try:
                raw_offset = query.get("offset", ["0"])[0]
                raw_limit = query.get("limit", ["100"])[0]
                offset = int(raw_offset)
                limit = int(raw_limit)
            except ValueError:
                _error_response(self, "invalid offset or limit parameter", 400)
                return

            try:
                res = read_logs_paginated(repo_root, run_id, offset=offset, limit=limit)
                if res is None:
                    _error_response(self, f"run log not found: {run_id}", 404)
                else:
                    _json_response(self, res)
            except Exception as exc:
                _error_response(self, str(exc), 500)

        def _handle_run_events(self, run_id: str, query: dict[str, list[str]]):
            from lanegate.orchestrate.run_report import read_executor_events, _resolve_run_session_ts

            try:
                if run_id.startswith("action-"):
                    # Direct-action logs are already structured JSONL. Expose
                    # their bounded event projection through the same history
                    # endpoint used by TUI run detail views.
                    from lanegate.orchestrate.run_report import read_logs_paginated

                    page = read_logs_paginated(repo_root, run_id, offset=0, limit=1000)
                    if page is None:
                        _error_response(self, f"run events not found: {run_id}", 404)
                    else:
                        _json_response(self, {"run_id": run_id, "events": page["events"]})
                    return
                session_ts = _resolve_run_session_ts(repo_root, None if run_id == "current" else run_id)
                if not session_ts:
                    _error_response(self, f"run events not found: {run_id}", 404)
                    return
                # This endpoint is intentionally narrower than the local
                # recovery log: it is the stable API/TUI contract for safe
                # progress metadata only, never raw executor output.
                _json_response(self, {"run_id": session_ts, "events": read_executor_events(repo_root, session_ts)})
            except Exception as exc:
                _error_response(self, str(exc), 500)

        def _handle_log(self, *, follow: bool):
            try:
                _stream_log_events(self, repo_root, processes, follow=follow)
            except Exception as exc:
                if not self.wfile.closed:
                    _error_response(self, str(exc), 500)

        def _handle_log_page(self, query: dict[str, list[str]]):
            try:
                offset = int(query.get("offset", ["0"])[0])
                limit = int(query.get("limit", ["200"])[0])
            except ValueError:
                _error_response(self, "offset and limit must be integers", 400)
                return
            if offset < 0 or limit <= 0:
                _error_response(self, "offset must be >= 0 and limit must be > 0", 400)
                return
            try:
                from lanegate.orchestrate.run_report import read_log_page

                run = _read_run_state(repo_root)
                payload = _run_payload(repo_root, processes, run)
                log_path = _resolve_log_path(repo_root, run, payload.get("orchestration"))
                if log_path is None:
                    _error_response(self, "no log file available", 404)
                    return
                page = read_log_page(log_path, offset, limit)
                events = []
                for i, line in enumerate(page["lines"]):
                    message = redact_transcript_text(line)
                    events.append(
                        {
                            "id": str(offset + i + 1),
                            "type": "log",
                            "timestamp": _utc_now_iso(),
                            "run_id": payload.get("run_id"),
                            "ticket_id": (payload.get("orchestration") or {}).get("ticket_id"),
                            "message": message,
                            **semantic_line_metadata(message),
                            "data": {"path": str(log_path)},
                        }
                    )
                _json_response(
                    self,
                    {
                        "run_id": payload.get("run_id"),
                        "offset": offset,
                        "limit": limit,
                        "total_count": page["total_count"],
                        "next_offset": page["next_offset"],
                        "events": events,
                    },
                )
            except Exception as exc:
                _error_response(self, str(exc), 500)

        def _handle_config(self):
            try:
                server_address = getattr(self.server, "server_address", None)
                port = server_address[1] if server_address else None
                data = _sanitized_config_payload(cfg, repo_root, api_host=_BIND_HOST, api_port=port)
                _json_response(self, data)
            except Exception as exc:
                _error_response(self, str(exc), 500)

        def _handle_pools(self):
            try:
                data = _pools_payload(cfg, repo_root)
                _json_response(self, data)
            except Exception as exc:
                _error_response(self, str(exc), 500)

        # ── PUT handlers ──────────────────────────────────────────────────────

        def _handle_pool_executors_update(self, pool_name: str):
            from lanegate.config import ConfigError, update_pool_executor_order

            body = self._read_body()
            try:
                params = json.loads(body) if body else {}
            except json.JSONDecodeError:
                _error_response(self, "invalid JSON request body", 400)
                return
            if not isinstance(params, dict) or not isinstance(params.get("executors"), list):
                _error_response(
                    self, "request body must be a JSON object with an 'executors' list", 400
                )
                return
            executors = params["executors"]
            if not all(isinstance(ex, str) for ex in executors):
                _error_response(self, "'executors' must be a list of strings", 400)
                return

            try:
                updated = update_pool_executor_order(repo_root, pool_name, executors)
            except ConfigError as exc:
                _error_response(self, str(exc), 400)
                return
            except Exception as exc:
                _error_response(self, str(exc), 500)
                return

            if isinstance(cfg.get("pools"), dict) and pool_name in cfg["pools"]:
                cfg["pools"][pool_name]["executors"] = list(executors)

            _json_response(self, updated)

        # ── POST handlers ─────────────────────────────────────────────────────

        def _handle_run_start(self):
            body = self._read_body()
            try:
                params = json.loads(body) if body else {}
            except json.JSONDecodeError:
                _error_response(self, "invalid JSON request body", 400)
                return
            if not isinstance(params, dict):
                _error_response(self, "request body must be a JSON object", 400)
                return

            cmd = _build_run_cmd(params)
            run_id = f"run-{datetime.datetime.now(datetime.UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"

            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(repo_root),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as exc:
                _error_response(self, str(exc), 500)
                return

            processes[run_id] = proc
            run = _write_run_state(
                repo_root,
                {
                    "run_id": run_id,
                    "status": "running",
                    "started_at": time.time(),
                    "started_at_iso": _utc_now_iso(),
                    "orchestrator_pid": proc.pid,
                    "command": cmd,
                    "cwd": str(repo_root),
                    "stop_requested": False,
                    "last_event": "orchestrator_started",
                },
            )
            data = _run_payload(repo_root, processes, run)
            _json_response(self, data)

        def _handle_run_stop(self):
            from lanegate.concurrency import orchestrator_lock_status

            try:
                body = self._read_body()
                params = json.loads(body) if body else {}
                if not isinstance(params, dict):
                    _error_response(self, "request body must be a JSON object", 400)
                    return
                requested_run_id = params.get("run_id")
                run = _read_run_state(repo_root)
                if requested_run_id and (not run or run.get("run_id") != requested_run_id):
                    _error_response(self, f"run not found: {requested_run_id}", 404)
                    return

                payload = _run_payload(repo_root, processes, run)
                lock = orchestrator_lock_status(repo_root)
                pid = payload.get("orchestrator_pid") if payload.get("process_alive") else None
                if not isinstance(pid, int) and lock.get("held"):
                    pid = lock.get("pid")

                if isinstance(pid, int):
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError) as exc:
                        _error_response(self, f"could not signal orchestrator PID {pid}: {exc}", 500)
                        return

                    run_id = payload.get("run_id")
                    if run is not None:
                        run = _write_run_state(
                            repo_root,
                            {
                                **run,
                                "stop_requested": True,
                                "stop_requested_at": _utc_now_iso(),
                                "last_event": "stop_requested",
                            },
                        )
                        payload = _run_payload(repo_root, processes, run)
                    _json_response(
                        self,
                        {
                            "run_id": run_id,
                            "status": payload.get("status"),
                            "stop_requested": True,
                            "orchestrator_pid": pid,
                        },
                    )
                    return

                active = payload.get("orchestration") or {}
                if active.get("executor_pid"):
                    _error_response(
                        self,
                        "active executor found but no live orchestrator process is available for graceful stop",
                        409,
                    )
                    return

                _json_response(
                    self,
                    {
                        "run_id": payload.get("run_id"),
                        "status": payload.get("status"),
                        "stop_requested": False,
                        "reason": "no active orchestration",
                    },
                )
            except json.JSONDecodeError:
                _error_response(self, "invalid JSON request body", 400)
            except Exception as exc:
                _error_response(self, str(exc), 500)

        # ── helpers ───────────────────────────────────────────────────────────

        def _authorize_mutation(self) -> bool:
            """Reject unauthenticated writes before handlers inspect their bodies."""
            supplied = self.headers.get(_API_TOKEN_HEADER)
            if not supplied or not secrets.compare_digest(supplied, api_token):
                _error_response(self, "missing or invalid API token", 401)
                return False
            return True

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length", 0))
            return self.rfile.read(length) if length else b""

    return _ApiHandler


class LaneGateApiServer:
    """Loopback-only HTTP server for the lanegate API."""

    def __init__(self, cfg: dict, repo_root: Path, port: int = _DEFAULT_PORT) -> None:
        self.cfg = cfg
        self.repo_root = repo_root
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._processes: dict[str, subprocess.Popen] = {}
        self._api_token = secrets.token_urlsafe(32)
        self.token_path: Path | None = None

    def start(self) -> None:
        handler = make_handler(self.cfg, self.repo_root, self._processes, self._api_token)
        self._server = ThreadingHTTPServer((_BIND_HOST, self.port), handler)
        self.token_path = _write_api_token(
            self.repo_root, self._server.server_address[1], self._api_token
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None

    def serve_forever(self) -> None:
        """Block the calling thread, serving requests until KeyboardInterrupt."""
        handler = make_handler(self.cfg, self.repo_root, self._processes, self._api_token)
        self._server = ThreadingHTTPServer((_BIND_HOST, self.port), handler)
        self.token_path = _write_api_token(
            self.repo_root, self._server.server_address[1], self._api_token
        )
        print(
            f"lanegate api: listening on {_BIND_HOST}:{self.port} (loopback only)",
            file=sys.stderr,
        )
        try:
            self._server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self._server.server_close()


def cmd_api(cfg: dict, repo_root: Path, port: int = _DEFAULT_PORT) -> None:
    """Entry point for `lanegate api` CLI command."""
    server = LaneGateApiServer(cfg, repo_root, port=port)
    server.serve_forever()
