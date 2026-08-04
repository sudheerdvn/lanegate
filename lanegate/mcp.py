"""
mcp.py — MCP server that exposes lanegate verbs as tools.

Start with: lanegate mcp
Uses stdio transport (the MCP default) — attach any MCP-compatible client.

Tool catalogue:
  board()                          — board state as structured JSON
  next_ticket()                    — next unblocked ticket(s)
  pipeline_status()                — commits pending at each stage
  repo_status(ticket_id?)           — bounded ticket/worktree/log/lock metadata
  recent_logs(limit?, lines?, bytes?) — bounded excerpts from recent LaneGate logs
  continuation_context(ticket_id?)  — durable resume metadata
  stats()                          — median time-in-status analytics
  flag_list(env?)                  — feature flags for an environment
  flag_set(name, value, env?)      — enable/disable a feature flag
  start(ticket_id)                 — claim ticket, create worktree
  orchestrate(...)                 — preview/run the board-clearing loop
  complete(ticket_id)              — mark code done (→ code_complete)
  review(ticket_id, verdict?, summary?, findings?) — agent review gate (→ in_review or stays code_complete)
  merge(ticket_id)                 — merge to main, remove worktree
  promote(env_name)                — promote main → stage/prod environment branch
  hibernate(ticket_id, reason?, reset?) — pause in_progress ticket with context snapshot
  needs_review(ticket_id, reason?) — escalate in_progress → needs_review
  fail(ticket_id, reason?)         — mark ticket failed, release worktree
  reopen(ticket_id)                — reopen a failed ticket → open
  validate(ticket_id)              — advance merged → validated
  done(ticket_id)                  — advance validated/merged → done
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from lanegate import APP_NAME
from lanegate.config import find_repo_root, load_config
from lanegate.lifecycle import MergeFailedError

_mcp = FastMCP(APP_NAME)

_MAX_ACTION_OUTPUT_BYTES = 8192
_MAX_LOG_LINES = 80
_MAX_LOG_BYTES = 8192
_MAX_STATUS_TICKETS = 40
_MAX_WORKTREES = 40


def _cfg_and_root() -> tuple[dict, Path]:
    repo_root = find_repo_root()
    cfg = load_config(repo_root)
    return cfg, repo_root


def _truncate_text(text: str, max_bytes: int) -> tuple[str, bool, int]:
    encoded = text.encode("utf-8", errors="replace")
    original_bytes = len(encoded)
    if original_bytes <= max_bytes:
        return text, False, original_bytes
    marker = "\n...[truncated]..."
    marker_bytes = marker.encode("utf-8")
    keep = max(0, max_bytes - len(marker_bytes))
    truncated = encoded[:keep].decode("utf-8", errors="ignore") + marker
    return truncated, True, original_bytes


def _tail_lines_bounded(path: Path, *, max_lines: int, max_bytes: int) -> dict:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return {
            "path": str(path),
            "ok": False,
            "error": str(exc),
            "text": "",
            "line_count": 0,
            "byte_count": 0,
            "truncated": False,
        }

    original_bytes = len(raw)
    tail = raw[-max_bytes:]
    byte_truncated = original_bytes > max_bytes
    text = tail.decode("utf-8", errors="replace")
    lines = text.splitlines()
    line_truncated = len(lines) > max_lines
    if line_truncated:
        lines = lines[-max_lines:]
    excerpt = "\n".join(lines)
    return {
        "path": str(path),
        "ok": True,
        "text": excerpt,
        "line_count": len(lines),
        "byte_count": len(excerpt.encode("utf-8", errors="replace")),
        "source_byte_count": original_bytes,
        "truncated": byte_truncated or line_truncated,
        "limits": {"max_lines": max_lines, "max_bytes": max_bytes},
    }


def _public_ticket(t: dict, repo_root: Path) -> dict:
    worktree = t.get("worktree")
    worktree_path = Path(worktree) if worktree else None
    if worktree_path and not worktree_path.is_absolute():
        worktree_path = repo_root / worktree_path
    return {
        "id": t.get("id"),
        "title": t.get("title", ""),
        "status": t.get("status"),
        "priority": t.get("priority"),
        "milestone": t.get("milestone"),
        "branch": t.get("branch"),
        "worktree": worktree,
        "worktree_exists": bool(worktree_path and worktree_path.exists()),
        "touches_count": len(t.get("touches") or []),
        "depends_on": list(t.get("depends_on") or []),
        "review_verdict": t.get("review_verdict"),
        "status_changed_at": t.get("status_changed_at"),
    }


def _load_ticket_summary(cfg: dict, repo_root: Path, ticket_id: str | None = None) -> dict:
    from lanegate.ticket import canonical_id, load_all_tickets

    tickets_dir = repo_root / cfg["tickets_dir"]
    tickets, quarantined = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    if ticket_id:
        wanted = canonical_id(ticket_id)
        tickets = [t for t in tickets if t.get("id") == wanted]
    counts: dict[str, int] = {}
    for ticket in tickets:
        status = ticket.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {
        "counts": counts,
        "tickets": [_public_ticket(t, repo_root) for t in tickets[:_MAX_STATUS_TICKETS]],
        "ticket_count": len(tickets),
        "quarantined_count": len(quarantined),
        "truncated": len(tickets) > _MAX_STATUS_TICKETS,
        "limits": {"max_tickets": _MAX_STATUS_TICKETS},
    }


def _worktree_summary(cfg: dict, repo_root: Path) -> dict:
    root = repo_root / cfg["worktrees_dir"]
    entries = []
    dirs = []
    if root.exists():
        dirs = sorted(p for p in root.iterdir() if p.is_dir())
        for path in dirs[:_MAX_WORKTREES]:
            entries.append({"name": path.name, "path": str(path)})
    total = len(dirs)
    return {
        "root": str(root),
        "exists": root.exists(),
        "worktrees": entries,
        "count": total,
        "truncated": total > _MAX_WORKTREES,
        "limits": {"max_worktrees": _MAX_WORKTREES},
    }


def _latest_log_paths(repo_root: Path, limit: int) -> list[Path]:
    logs_dir = repo_root / f".{APP_NAME}" / "logs"
    if not logs_dir.exists():
        return []
    return sorted(logs_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]


def _capture_action(fn, *args, **kwargs) -> dict:
    """Run a command function, capture its stdout/stderr, handle SystemExit and MergeFailedError."""
    out = io.StringIO()
    err = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            fn(*args, **kwargs)
        output, output_truncated, output_bytes = _truncate_text(
            out.getvalue().strip(), _MAX_ACTION_OUTPUT_BYTES
        )
        stderr, stderr_truncated, stderr_bytes = _truncate_text(
            err.getvalue().strip(), _MAX_ACTION_OUTPUT_BYTES
        )
        return {
            "ok": True,
            "output": output,
            "stderr": stderr,
            "output_bytes": output_bytes,
            "stderr_bytes": stderr_bytes,
            "output_truncated": output_truncated,
            "stderr_truncated": stderr_truncated,
            "limits": {"max_output_bytes": _MAX_ACTION_OUTPUT_BYTES},
        }
    except (SystemExit, MergeFailedError) as exc:
        output, output_truncated, output_bytes = _truncate_text(
            out.getvalue().strip(), _MAX_ACTION_OUTPUT_BYTES
        )
        stderr, stderr_truncated, stderr_bytes = _truncate_text(
            err.getvalue().strip(), _MAX_ACTION_OUTPUT_BYTES
        )
        result = {
            "ok": False,
            "output": output,
            "error": str(exc) if isinstance(exc, MergeFailedError) else (stderr or output),
            "stderr": stderr,
            "output_bytes": output_bytes,
            "stderr_bytes": stderr_bytes,
            "output_truncated": output_truncated,
            "stderr_truncated": stderr_truncated,
            "limits": {"max_output_bytes": _MAX_ACTION_OUTPUT_BYTES},
        }
        if isinstance(exc, SystemExit):
            result["exit_code"] = exc.code
        return result


def _capture_json(fn, *args, **kwargs) -> Any:
    """Run a command in json_output=True mode and parse the emitted JSON.

    Returns whatever shape the wrapped command emits — each MCP tool's own
    `-> dict` / `-> list` annotation is the actual contract for its caller.
    """
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args, json_output=True, **kwargs)
    return json.loads(buf.getvalue())


# ── Read-only board tools ─────────────────────────────────────────────────────


@_mcp.tool()
def board() -> dict:
    """Return the full ticket board grouped by status, plus pipeline info."""
    cfg, repo_root = _cfg_and_root()
    from lanegate.board import cmd_board

    return _capture_json(cmd_board, cfg, repo_root)


@_mcp.tool()
def next_ticket() -> dict:
    """Return the next unblocked ticket to work on plus any safe parallel companions."""
    cfg, repo_root = _cfg_and_root()
    from lanegate.board import cmd_next

    return _capture_json(cmd_next, cfg, repo_root)


@_mcp.tool()
def pipeline_status() -> list:
    """Return pending commit counts for every configured environment."""
    cfg, repo_root = _cfg_and_root()
    from lanegate.board import cmd_pipeline_status

    return _capture_json(cmd_pipeline_status, cfg, repo_root)


@_mcp.tool()
def repo_status(ticket_id: str | None = None) -> dict:
    """Return bounded ticket, worktree, latest-log, and orchestrator-lock metadata."""
    cfg, repo_root = _cfg_and_root()
    from lanegate.concurrency import orchestrator_lock_status

    latest = _latest_log_paths(repo_root, 1)
    return {
        "ok": True,
        "repo_root": str(repo_root),
        "tickets": _load_ticket_summary(cfg, repo_root, ticket_id=ticket_id),
        "worktrees": _worktree_summary(cfg, repo_root),
        "orchestrator_lock": orchestrator_lock_status(repo_root),
        "latest_log": str(latest[0]) if latest else None,
    }


@_mcp.tool()
def recent_logs(
    limit: int = 1,
    max_lines: int = _MAX_LOG_LINES,
    max_bytes: int = _MAX_LOG_BYTES,
) -> dict:
    """Return short excerpts from recent LaneGate logs with hard line and byte limits."""
    _, repo_root = _cfg_and_root()
    limit = max(0, min(limit, 5))
    max_lines = max(1, min(max_lines, _MAX_LOG_LINES))
    max_bytes = max(128, min(max_bytes, _MAX_LOG_BYTES))
    paths = _latest_log_paths(repo_root, limit)
    return {
        "ok": True,
        "logs": [
            _tail_lines_bounded(path, max_lines=max_lines, max_bytes=max_bytes) for path in paths
        ],
        "count": len(paths),
        "limits": {"max_logs": 5, "max_lines": max_lines, "max_bytes": max_bytes},
    }


@_mcp.tool()
def continuation_context(ticket_id: str | None = None) -> dict:
    """Return durable continuation metadata from tickets, worktrees, logs, and locks."""
    status = repo_status(ticket_id=ticket_id)
    logs = recent_logs(limit=1, max_lines=30, max_bytes=4096)
    return {
        "ok": True,
        "source_of_truth": ["tickets", "worktrees", ".lanegate/logs", ".lanegate/orchestrator.lock"],
        "repo_root": status["repo_root"],
        "tickets": status["tickets"],
        "worktrees": status["worktrees"],
        "orchestrator_lock": status["orchestrator_lock"],
        "recent_logs": logs["logs"],
    }


# ── Feature-flag tools ────────────────────────────────────────────────────────


@_mcp.tool()
def flag_list(env: str | None = None) -> dict:
    """Return all feature flags (and their ON/OFF state) for the given environment.

    Args:
        env: Environment name (e.g. "staging", "production"). None = global default.
    """
    cfg, _ = _cfg_and_root()
    from lanegate.flags import cmd_flag

    buf = io.StringIO()
    with redirect_stdout(buf):
        cmd_flag("list", None, cfg, env, json_output=True)
    return json.loads(buf.getvalue())


@_mcp.tool()
def flag_set(name: str, value: bool, env: str | None = None) -> dict:
    """Enable or disable a feature flag.

    Args:
        name:  Flag name (e.g. "new_checkout_flow").
        value: True to enable, False to disable.
        env:   Environment name. None = global default.
    """
    cfg, _ = _cfg_and_root()
    from lanegate.flags import set_flag

    try:
        set_flag(cfg, name, value, env)
        action = "enabled" if value else "disabled"
        env_label = f" [{env}]" if env else ""
        return {"ok": True, "output": f"Flag '{name}' {action}{env_label}."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ── Lifecycle action tools ────────────────────────────────────────────────────


@_mcp.tool()
def start(ticket_id: str) -> dict:
    """Claim a ticket: set status → in_progress and create its git worktree.

    Args:
        ticket_id: Ticket identifier, e.g. "TICK-007".
    """
    cfg, repo_root = _cfg_and_root()
    from lanegate.lifecycle import cmd_start

    return _capture_action(cmd_start, ticket_id, cfg, repo_root)


@_mcp.tool()
def complete(ticket_id: str) -> dict:
    """Mark implementation done: set status → code_complete.

    Args:
        ticket_id: Ticket identifier, e.g. "TICK-007".
    """
    cfg, repo_root = _cfg_and_root()
    from lanegate.lifecycle import cmd_complete
    from lanegate.ticket import canonical_id, load_all_tickets

    result = _capture_action(cmd_complete, ticket_id, cfg, repo_root)
    if result["ok"]:
        tickets_dir = repo_root / cfg["tickets_dir"]
        tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
        tid = canonical_id(ticket_id)
        ticket = next((t for t in tickets if t["id"] == tid), None)
        result["status"] = ticket.get("status") if ticket else "code_complete"
    return result


@_mcp.tool()
def review(
    ticket_id: str,
    verdict: str | None = None,
    summary: str | None = None,
    findings: str | None = None,
) -> dict:
    """Submit ticket for review: approved → in_review; changes_requested → stays code_complete.

    Args:
        ticket_id: Ticket identifier, e.g. "TICK-007".
        verdict:   "approved" or "changes_requested". Omit for bare status flip (backward compat).
        summary:   One-line review summary stored in review_summary frontmatter field.
        findings:  Multi-line findings appended to ticket body under ## Review Findings.
    """
    cfg, repo_root = _cfg_and_root()
    from lanegate.lifecycle import cmd_review as _cmd_review
    from lanegate.ticket import canonical_id, load_all_tickets

    result = _capture_action(
        _cmd_review, ticket_id, cfg, repo_root, verdict=verdict, summary=summary, findings=findings
    )
    tickets_dir = repo_root / cfg["tickets_dir"]
    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    tid = canonical_id(ticket_id)
    ticket = next((t for t in tickets if t["id"] == tid), None)
    if ticket:
        result["status"] = ticket.get("status")
        if ticket.get("review_verdict"):
            result["verdict"] = ticket["review_verdict"]
        if ticket.get("review_summary"):
            result["summary"] = ticket["review_summary"]
    return result


@_mcp.tool()
def merge(ticket_id: str) -> dict:
    """Merge the ticket branch to main and remove the worktree.

    Args:
        ticket_id: Ticket identifier, e.g. "TICK-007".
    """
    cfg, repo_root = _cfg_and_root()
    from lanegate.lifecycle import cmd_merge

    result = _capture_action(cmd_merge, ticket_id, cfg, repo_root)
    if result["ok"]:
        result["status"] = "merged"
    return result


# ── Promotion tool ────────────────────────────────────────────────────────────


@_mcp.tool()
def promote(env_name: str) -> dict:
    """Promote main to a configured environment branch (e.g. staging, production).

    Runs guard_script → pre_promote → git sync → post_promote.
    Auto-trigger environments are refused — those are hook-driven.

    Args:
        env_name: Environment name as defined in .lanegate.yml, e.g. "staging" or "production".
    """
    cfg, repo_root = _cfg_and_root()
    from lanegate.promote import cmd_promote

    return _capture_action(cmd_promote, env_name, cfg, repo_root)


# ── Extended lifecycle tools ──────────────────────────────────────────────────


@_mcp.tool()
def hibernate(ticket_id: str, reason: str = "", reset: bool = False) -> dict:
    """Pause an in-progress ticket: write a context snapshot then set status → hibernated.

    Args:
        ticket_id: Ticket identifier, e.g. "TICK-007".
        reason:    Optional explanation stored in the ticket body.
        reset:     When True, remove the worktree and delete the branch so the
                   ticket restarts cleanly next time (use with care).
    """
    cfg, repo_root = _cfg_and_root()
    from lanegate.lifecycle import cmd_hibernate

    result = _capture_action(cmd_hibernate, ticket_id, cfg, repo_root, reason=reason, reset=reset)
    if result["ok"]:
        result["status"] = "hibernated"
    return result


@_mcp.tool()
def needs_review(ticket_id: str, reason: str = "") -> dict:
    """Escalate an in-progress ticket to human review: status → needs_review.

    The worktree is preserved so the reviewer can inspect or continue.

    Args:
        ticket_id: Ticket identifier, e.g. "TICK-007".
        reason:    Optional explanation stored in the ticket body.
    """
    cfg, repo_root = _cfg_and_root()
    from lanegate.lifecycle import cmd_needs_review as _cmd_needs_review

    result = _capture_action(_cmd_needs_review, ticket_id, cfg, repo_root, reason=reason)
    if result["ok"]:
        result["status"] = "needs_review"
    return result


@_mcp.tool()
def fail(ticket_id: str, reason: str = "") -> dict:
    """Mark a ticket as failed: status → failed, worktree released.

    Failed tickets are terminal; use reopen() to make them eligible again.

    Args:
        ticket_id: Ticket identifier, e.g. "TICK-007".
        reason:    Optional failure description stored in the ticket body.
    """
    cfg, repo_root = _cfg_and_root()
    from lanegate.lifecycle import cmd_fail

    result = _capture_action(cmd_fail, ticket_id, cfg, repo_root, reason=reason)
    if result["ok"]:
        result["status"] = "failed"
    return result


@_mcp.tool()
def reopen(ticket_id: str) -> dict:
    """Reopen a failed ticket: status → open so it can be dispatched again.

    Args:
        ticket_id: Ticket identifier, e.g. "TICK-007".
    """
    cfg, repo_root = _cfg_and_root()
    from lanegate.lifecycle import cmd_reopen

    result = _capture_action(cmd_reopen, ticket_id, cfg, repo_root)
    if result["ok"]:
        result["status"] = "open"
    return result


@_mcp.tool()
def validate(ticket_id: str) -> dict:
    """Advance a merged ticket to validated after post-merge verification.

    Args:
        ticket_id: Ticket identifier, e.g. "TICK-007".
    """
    cfg, repo_root = _cfg_and_root()
    from lanegate.lifecycle import cmd_validate

    result = _capture_action(cmd_validate, ticket_id, cfg, repo_root)
    if result["ok"]:
        result["status"] = "validated"
    return result


@_mcp.tool()
def done(ticket_id: str) -> dict:
    """Close out a validated (or merged) ticket: status → done.

    Args:
        ticket_id: Ticket identifier, e.g. "TICK-007".
    """
    cfg, repo_root = _cfg_and_root()
    from lanegate.lifecycle import cmd_done

    result = _capture_action(cmd_done, ticket_id, cfg, repo_root)
    if result["ok"]:
        result["status"] = "done"
    return result


@_mcp.tool()
def orchestrate(
    max_parallel: int | None = None,
    dry_run: bool = True,
    human_review: str = "none",
    milestone: str | None = None,
    all_milestones: bool = False,
) -> dict:
    """Run or preview orchestration through a bounded-output wrapper.

    Defaults to dry-run so an attached agent can inspect the plan before starting
    executor work. Set dry_run=False to perform the run.
    """
    cfg, repo_root = _cfg_and_root()
    from lanegate.orchestrate import cmd_orchestrate

    result = _capture_action(
        cmd_orchestrate,
        cfg,
        repo_root,
        max_parallel=max_parallel,
        dry_run=dry_run,
        human_review=human_review,
        milestone=milestone,
        all_milestones=all_milestones,
        auto_analyze=True,
        recover=True,
        verbose=False,
    )
    result["dry_run"] = dry_run
    result["human_review"] = human_review
    return result


# ── Analytics tool ────────────────────────────────────────────────────────────


@_mcp.tool()
def stats() -> dict:
    """Return median time-in-status analytics across all tickets."""
    cfg, repo_root = _cfg_and_root()
    from lanegate.stats import cmd_stats

    return _capture_json(cmd_stats, cfg, repo_root)


@_mcp.tool()
def update_docs(status: str | None = None) -> dict:
    """Refresh README/ARCHITECTURE.md based on tickets completed since last doc update.

    Args:
        status: Optional ticket status filter (default: 'done' or as configured in .lanegate.yml).
    """
    cfg, repo_root = _cfg_and_root()
    from lanegate.update_docs import cmd_update_docs

    return _capture_action(cmd_update_docs, cfg, repo_root, status=status)


# ── Entry point ───────────────────────────────────────────────────────────────


def run_mcp_server() -> None:
    """Start the MCP server on stdio. Called by `lanegate mcp`."""
    _mcp.run()
