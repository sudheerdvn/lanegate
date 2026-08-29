"""
lanegate/orchestrate/run_summary.py — structured, executor-neutral run-summary model.

One data model shared by every surface that reports on an orchestrate run
(CLI ``lanegate run`` final output, ``lanegate run-report``, and — via a
follow-up ticket — the TUI run-history view), so no surface has to re-parse
terminal logs to answer "what happened and why" (TICK-259, following the
TICK-258 runaway-orchestration incident).

This module defines the schema only. Building a ``RunSummary`` from a run's
durable event log (see :mod:`lanegate.orchestrate.run_report`) and wiring it into
CLI/TUI output is out of scope here — see docs/internal/run-summary-design.md for the
split into follow-up tickets.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RunReason(StrEnum):
    """Overall state or terminal reason for an orchestrate run."""

    RUNNING = "running"
    SUCCESS = "success"
    FAILURE = "failure"
    STOPPED = "stopped"


class TicketOutcomeStatus(StrEnum):
    """Outcome of one ticket dispatched during a run.

    SUCCESS/FAILURE/CHANGES_REQUESTED/AWAITING_MERGE/SKIPPED are terminal.
    AWAITING_MERGE means the reviewer approved the work and a human must run
    ``lanegate merge``; unlike CHANGES_REQUESTED, it is not a review rejection.
    IN_PROGRESS and INTERRUPTED describe a dispatched ticket that has not
    reached a terminal outcome yet: IN_PROGRESS while the orchestrator process
    is still alive, INTERRUPTED once it isn't (crashed, killed, or otherwise
    gone without recording a ticket_outcome event) — never SKIPPED, which is
    reserved for a documented non-dispatch decision.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    CHANGES_REQUESTED = "changes_requested"
    AWAITING_MERGE = "awaiting_merge"
    SKIPPED = "skipped"
    IN_PROGRESS = "in_progress"
    INTERRUPTED = "interrupted"


@dataclass
class TicketOutcome:
    """One dispatched ticket's outcome within a run."""

    ticket_id: str
    executor: str
    outcome: TicketOutcomeStatus
    duration_seconds: float
    failure_reason: str | None = None
    review_reason: str | None = None
    lifecycle_summary: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "executor": self.executor,
            "outcome": self.outcome.value,
            "duration_seconds": self.duration_seconds,
            "failure_reason": self.failure_reason,
            "review_reason": self.review_reason,
            "lifecycle_summary": self.lifecycle_summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TicketOutcome:
        return cls(
            ticket_id=data["ticket_id"],
            executor=data["executor"],
            outcome=TicketOutcomeStatus(data["outcome"]),
            duration_seconds=data["duration_seconds"],
            failure_reason=data.get("failure_reason"),
            review_reason=data.get("review_reason"),
            lifecycle_summary=data.get("lifecycle_summary"),
        )


@dataclass
class RunSummary:
    """The terminal, structured summary of one orchestrate run."""

    run_id: str
    timestamp: datetime.datetime
    reason: RunReason
    batch_tickets: list[TicketOutcome] = field(default_factory=list)
    triggered_by: str = "manual"
    trigger_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "timestamp": self.timestamp.isoformat(),
            "reason": self.reason.value,
            "batch_tickets": [t.to_dict() for t in self.batch_tickets],
            "triggered_by": self.triggered_by,
            "trigger_reason": self.trigger_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunSummary:
        return cls(
            run_id=data["run_id"],
            timestamp=datetime.datetime.fromisoformat(data["timestamp"]),
            reason=RunReason(data["reason"]),
            batch_tickets=[TicketOutcome.from_dict(t) for t in data.get("batch_tickets", [])],
            triggered_by=data.get("triggered_by", "manual"),
            trigger_reason=data.get("trigger_reason"),
        )


def build_run_summary(
    cfg: dict, repo_root: Any, session_ts: str | None = None, tickets: list[dict] | None = None
) -> RunSummary | None:
    """Build a RunSummary directly from a run's durable event log.

    ``GET /api/runs`` calls this once per stored session. Routing through the
    full run-report builder made a history list with many runs take seconds
    per entry because that builder also loads tickets, process state, and
    resume-watch history. The compact durable-event builder is the shared
    summary implementation and keeps history reads responsive.

    `tickets`, when given, is passed through instead of having the builder
    re-read every ticket file from disk — see `list_run_summaries`.
    """
    from pathlib import Path
    from lanegate.orchestrate.run_report import build_run_summary as build_durable_run_summary

    if session_ts and session_ts.startswith("action-"):
        return _build_direct_action_summary(Path(repo_root), session_ts)
    return build_durable_run_summary(cfg, Path(repo_root), session_ts=session_ts, tickets=tickets)


def _build_direct_action_summary(repo_root: Any, action_id: str) -> RunSummary | None:
    """Project one direct-action JSONL stream into the shared run-history schema."""
    from pathlib import Path

    path = Path(repo_root) / ".lanegate" / "logs" / f"{action_id}.events.jsonl"
    try:
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    except (OSError, json.JSONDecodeError):
        return None
    if not events:
        return None
    first = events[0]
    last = events[-1]
    action_type = str(first.get("action_type") or "action")
    ticket_id = str(first.get("ticket_id") or "DIRECT")
    started_at = str(first.get("ts") or "")
    ended_at = str(last.get("ts") or started_at)
    try:
        timestamp = datetime.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        ended = datetime.datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
        duration = max(0.0, (ended - timestamp).total_seconds())
    except ValueError:
        timestamp = datetime.datetime.now(datetime.UTC)
        duration = 0.0
    status = str(last.get("status") or "running")
    # A `changes_requested` verdict makes `cmd_review` exit nonzero by design,
    # so `status="failed"` there reflects the decision, not a crash -- it
    # still needs the verdict-based mapping below. Any other failure (e.g. a
    # crash after an `approved` write) must stay FAILURE rather than being
    # reported as a successful approval.
    if action_type == "review" and last.get("verdict") and (
        status == "success" or last["verdict"] == "changes_requested"
    ):
        from lanegate.orchestrate.run_report import _map_ticket_outcome

        outcome, failure_reason, review_reason = _map_ticket_outcome(
            str(last["verdict"]), last.get("review_summary")
        )
        reason = RunReason.SUCCESS if outcome == TicketOutcomeStatus.SUCCESS else RunReason.STOPPED
        summary_suffix = f" — {last['review_summary']}" if last.get("review_summary") else ""
        return RunSummary(
            run_id=action_id,
            timestamp=timestamp,
            reason=reason,
            batch_tickets=[
                TicketOutcome(
                    ticket_id=ticket_id,
                    executor=f"direct:{action_type}",
                    outcome=outcome,
                    duration_seconds=duration,
                    failure_reason=failure_reason,
                    review_reason=review_reason,
                    lifecycle_summary=(
                        f"action=review; verdict={last['verdict']}{summary_suffix}; log_path={path}"
                    ),
                )
            ],
        )
    if status == "success":
        reason, outcome = RunReason.SUCCESS, TicketOutcomeStatus.SUCCESS
    elif status == "failed":
        reason, outcome = RunReason.FAILURE, TicketOutcomeStatus.FAILURE
    else:
        reason, outcome = RunReason.RUNNING, TicketOutcomeStatus.IN_PROGRESS
    return RunSummary(
        run_id=action_id,
        timestamp=timestamp,
        reason=reason,
        batch_tickets=[
            TicketOutcome(
                ticket_id=ticket_id,
                executor=f"direct:{action_type}",
                outcome=outcome,
                duration_seconds=duration,
                lifecycle_summary=f"action={action_type}; log_path={path}",
            )
        ],
    )


def list_run_summaries(cfg: dict, repo_root: Any) -> list[RunSummary]:
    """List all available RunSummary instances from disk, newest first."""
    from pathlib import Path
    from lanegate.orchestrate.run_report import _load_run_events, _map_ticket_outcome
    from lanegate.ticket import canonical_id, load_tickets_by_ids

    logs_dir = Path(repo_root) / ".lanegate" / "logs"
    if not logs_dir.exists():
        return []
    # cmd_orchestrate rotates all but the 10 most-recent orchestrate-*.log
    # files into logs_dir/archive (see loop.py's log-rotation block) well
    # before run_history_retention_days purges them -- scan both dirs so a
    # rotated-but-not-yet-purged run doesn't vanish from history early.
    search_dirs = [logs_dir, logs_dir / "archive"]
    sessions: set[str] = set()
    action_ids: set[str] = set()
    for d in search_dirs:
        for p in d.glob("orchestrate-*.events.jsonl"):
            name = p.name
            prefix = "orchestrate-"
            suffix = ".events.jsonl"
            if name.startswith(prefix) and name.endswith(suffix):
                sessions.add(name[len(prefix) : -len(suffix)])
        for p in d.glob("orchestrate-*.log"):
            name = p.stem
            prefix = "orchestrate-"
            if name.startswith(prefix):
                sessions.add(name[len(prefix) :])
        for p in d.glob("action-*.events.jsonl"):
            action_ids.add(p.name[: -len(".events.jsonl")])

    # build_run_summary only ever consults a ticket to enrich a FAILURE,
    # CHANGES_REQUESTED, or AWAITING_MERGE outcome's reason — loading every
    # ticket on the board for every historical session is wasted work (and, at
    # scale, slow enough to blow past the TUI's HTTP timeout). Prescan events
    # cheaply (no YAML parsing) for which ticket ids can actually need
    # enrichment, then parse only those ticket files instead of the whole board.
    needed_ids: set[str] = set()
    for st in sessions:
        events = _load_run_events(Path(repo_root), st)
        for e in events:
            tid = e.get("ticket_id")
            if tid:
                needed_ids.add(canonical_id(tid))

    tickets_dir = Path(repo_root) / cfg.get("tickets_dir", ".lanegate/tickets")
    if needed_ids and tickets_dir.exists():
        tickets_by_id = load_tickets_by_ids(
            tickets_dir,
            cfg.get("ticket_prefix", "TICK"),
            needed_ids,
            cfg,
        )
    else:
        tickets_by_id = {}
    tickets = list(tickets_by_id.values())

    def _ensure_utc(dt: datetime.datetime) -> datetime.datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)

    summaries: list[RunSummary] = []

    for st in sorted(sessions, reverse=True):
        s = build_run_summary(cfg, repo_root, session_ts=st, tickets=tickets)
        if s is not None:
            summaries.append(s)

    for action_id in action_ids:
        s = _build_direct_action_summary(repo_root, action_id)
        if s is not None:
            summaries.append(s)
    return sorted(summaries, key=lambda summary: summary.timestamp, reverse=True)
