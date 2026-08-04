"""
lanegate/orchestrate/run_summary.py — structured, executor-neutral run-summary model.

One data model shared by every surface that reports on an orchestrate run
(CLI ``lanegate orchestrate`` final output, ``lanegate run-report``, and — via a
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
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RunReason(StrEnum):
    """Why an orchestrate run ended."""

    SUCCESS = "success"
    FAILURE = "failure"
    STOPPED = "stopped"


class TicketOutcomeStatus(StrEnum):
    """Outcome of one ticket dispatched during a run.

    SUCCESS/FAILURE/CHANGES_REQUESTED/SKIPPED are terminal. IN_PROGRESS and
    INTERRUPTED describe a dispatched ticket that has not reached a terminal
    outcome yet: IN_PROGRESS while the orchestrator process is still alive,
    INTERRUPTED once it isn't (crashed, killed, or otherwise gone without
    recording a ticket_outcome event) — never SKIPPED, which is reserved for
    a documented non-dispatch decision.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    CHANGES_REQUESTED = "changes_requested"
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "timestamp": self.timestamp.isoformat(),
            "reason": self.reason.value,
            "batch_tickets": [t.to_dict() for t in self.batch_tickets],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunSummary:
        return cls(
            run_id=data["run_id"],
            timestamp=datetime.datetime.fromisoformat(data["timestamp"]),
            reason=RunReason(data["reason"]),
            batch_tickets=[TicketOutcome.from_dict(t) for t in data.get("batch_tickets", [])],
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

    return build_durable_run_summary(cfg, Path(repo_root), session_ts=session_ts, tickets=tickets)


def list_run_summaries(cfg: dict, repo_root: Any) -> list[RunSummary]:
    """List all available RunSummary instances from disk, newest first."""
    from pathlib import Path
    from lanegate.orchestrate.run_report import _load_run_events, _map_ticket_outcome
    from lanegate.ticket import canonical_id, load_tickets_by_ids

    logs_dir = Path(repo_root) / ".lanegate" / "logs"
    if not logs_dir.exists():
        return []
    sessions: set[str] = set()
    for p in logs_dir.glob("orchestrate-*.events.jsonl"):
        name = p.name
        prefix = "orchestrate-"
        suffix = ".events.jsonl"
        if name.startswith(prefix) and name.endswith(suffix):
            sessions.add(name[len(prefix) : -len(suffix)])
    for p in logs_dir.glob("orchestrate-*.log"):
        name = p.stem
        prefix = "orchestrate-"
        if name.startswith(prefix):
            sessions.add(name[len(prefix) :])

    # build_run_summary only ever consults a ticket to enrich a FAILURE/
    # CHANGES_REQUESTED outcome's reason — loading every ticket on the board
    # for every historical session is wasted work (and, at scale, slow
    # enough to blow past the TUI's HTTP timeout). Prescan events cheaply
    # (no YAML parsing) for which ticket ids can actually need enrichment,
    # then parse only those ticket files instead of the whole board.
    needed_ids: set[str] = set()
    for st in sessions:
        events = _load_run_events(Path(repo_root), st)
        for e in events:
            if e.get("event") != "ticket_outcome" or not e.get("ticket_id"):
                continue
            outcome_status, _, _ = _map_ticket_outcome(e.get("outcome"), e.get("reason"))
            if outcome_status.value in ("failure", "changes_requested"):
                needed_ids.add(canonical_id(e["ticket_id"]))

    tickets_by_id = load_tickets_by_ids(
        Path(repo_root) / cfg.get("tickets_dir", ".lanegate/tickets"),
        cfg.get("ticket_prefix", "TICK"),
        needed_ids,
        cfg,
    )
    tickets = list(tickets_by_id.values())

    summaries: list[RunSummary] = []
    for st in sorted(sessions, reverse=True):
        s = build_run_summary(cfg, repo_root, session_ts=st, tickets=tickets)
        if s is not None:
            summaries.append(s)
    return summaries
