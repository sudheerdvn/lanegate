"""
Tests for lanegate/orchestrate/run_report.py — build_run_summary, build_run_report,
and cmd_run_report CLI output.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from lanegate.orchestrate.run_report import (
    _append_run_event,
    build_run_report,
    build_run_summary,
    cmd_run_report,
    print_run_summary,
    read_logs_paginated,
)
from lanegate.orchestrate.run_summary import (
    RunReason,
    RunSummary,
    TicketOutcomeStatus,
    list_run_summaries,
)


def _default_cfg(tmp_path: Path) -> dict:
    (tmp_path / ".lanegate/tickets").mkdir(parents=True, exist_ok=True)
    return {
        "tickets_dir": ".lanegate/tickets",
        "ticket_prefix": "TICK",
        "worktrees_dir": ".lanegate/worktrees",
    }


class TestBuildRunSummary:
    def test_returns_none_when_no_run_events(self, tmp_path: Path):
        cfg = _default_cfg(tmp_path)
        assert build_run_summary(cfg, tmp_path) is None

    def test_history_listing_uses_compact_durable_summary(self, tmp_path: Path):
        cfg = _default_cfg(tmp_path)
        session_ts = "2026-07-30T15-00-00"
        _append_run_event(tmp_path, session_ts, "run_start", pid=os.getpid(), ts="2026-07-30T15:00:00Z")
        _append_run_event(tmp_path, session_ts, "run_end", status="completed", ts="2026-07-30T15:01:00Z")

        # The full report loader performs unrelated ticket/process/recovery
        # work. A history list must not invoke it once for every stored run.
        with patch("lanegate.orchestrate.run_report.build_run_report", side_effect=AssertionError):
            summaries = list_run_summaries(cfg, tmp_path)

        assert [summary.run_id for summary in summaries] == [session_ts]

    def test_successful_run(self, tmp_path: Path):
        cfg = _default_cfg(tmp_path)
        session_ts = "2026-07-30T16-00-00"
        _append_run_event(tmp_path, session_ts, "run_start", pid=os.getpid(), ts="2026-07-30T16:00:00Z")
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_dispatch",
            ticket_id="TICK-001",
            executor="claude-a",
            was_hibernated=False,
            ts="2026-07-30T16:00:01Z",
        )
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_outcome",
            ticket_id="TICK-001",
            outcome="success",
            ts="2026-07-30T16:00:10Z",
        )
        _append_run_event(tmp_path, session_ts, "run_end", status="completed", ts="2026-07-30T16:00:11Z")

        summary = build_run_summary(cfg, tmp_path, session_ts=session_ts)
        assert summary is not None
        assert isinstance(summary, RunSummary)
        assert summary.run_id == session_ts
        assert summary.reason == RunReason.SUCCESS
        assert len(summary.batch_tickets) == 1

        ticket = summary.batch_tickets[0]
        assert ticket.ticket_id == "TICK-001"
        assert ticket.executor == "claude-a"
        assert ticket.outcome == TicketOutcomeStatus.SUCCESS
        assert ticket.duration_seconds == 9.0
        assert ticket.failure_reason is None
        assert ticket.review_reason is None

    def test_successful_run_without_live_tickets_directory(self, tmp_path: Path):
        """Historical summaries remain available after live tickets are removed."""
        cfg = {
            "tickets_dir": ".lanegate/tickets",
            "ticket_prefix": "TICK",
            "worktrees_dir": ".lanegate/worktrees",
        }
        session_ts = "2026-07-30T16-05-00"
        _append_run_event(tmp_path, session_ts, "run_start", pid=os.getpid(), ts="2026-07-30T16:05:00Z")
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_dispatch",
            ticket_id="TICK-001",
            executor="agy",
            ts="2026-07-30T16:05:01Z",
        )
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_outcome",
            ticket_id="TICK-001",
            outcome="success",
            ts="2026-07-30T16:05:10Z",
        )
        _append_run_event(tmp_path, session_ts, "run_end", status="completed", ts="2026-07-30T16:05:11Z")

        summary = build_run_summary(cfg, tmp_path, session_ts=session_ts)

        assert summary is not None
        assert summary.batch_tickets[0].ticket_id == "TICK-001"

        report = build_run_report(cfg, tmp_path, session_ts=session_ts)
        assert report is not None
        assert report["tickets"][0]["ticket_id"] == "TICK-001"

    def test_failed_ticket_run(self, tmp_path: Path):
        cfg = _default_cfg(tmp_path)
        session_ts = "2026-07-30T16-10-00"
        _append_run_event(tmp_path, session_ts, "run_start", pid=os.getpid(), ts="2026-07-30T16:10:00Z")
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_dispatch",
            ticket_id="TICK-002",
            executor="gpt-4o",
            was_hibernated=False,
            ts="2026-07-30T16:10:01Z",
        )
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_outcome",
            ticket_id="TICK-002",
            outcome="failed",
            reason="pytest exited with code 1",
            ts="2026-07-30T16:10:05Z",
        )
        _append_run_event(tmp_path, session_ts, "run_end", status="completed", ts="2026-07-30T16:10:06Z")

        summary = build_run_summary(cfg, tmp_path, session_ts=session_ts)
        assert summary is not None
        assert summary.reason == RunReason.FAILURE
        assert len(summary.batch_tickets) == 1

        t = summary.batch_tickets[0]
        assert t.ticket_id == "TICK-002"
        assert t.executor == "gpt-4o"
        assert t.outcome == TicketOutcomeStatus.FAILURE
        assert t.duration_seconds == 4.0
        assert t.failure_reason == "pytest exited with code 1"
        assert t.review_reason is None

    def test_changes_requested_run(self, tmp_path: Path):
        cfg = _default_cfg(tmp_path)
        session_ts = "2026-07-30T16-20-00"
        _append_run_event(tmp_path, session_ts, "run_start", pid=os.getpid(), ts="2026-07-30T16:20:00Z")
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_dispatch",
            ticket_id="TICK-003",
            executor="claude-b",
            was_hibernated=False,
            ts="2026-07-30T16:20:01Z",
        )
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_outcome",
            ticket_id="TICK-003",
            outcome="changes_requested",
            reason="review requested changes",
            ts="2026-07-30T16:20:08Z",
        )
        _append_run_event(tmp_path, session_ts, "run_end", status="completed", ts="2026-07-30T16:20:09Z")

        summary = build_run_summary(cfg, tmp_path, session_ts=session_ts)
        assert summary is not None
        assert summary.reason == RunReason.STOPPED
        assert len(summary.batch_tickets) == 1

        t = summary.batch_tickets[0]
        assert t.ticket_id == "TICK-003"
        assert t.executor == "claude-b"
        assert t.outcome == TicketOutcomeStatus.CHANGES_REQUESTED
        assert t.duration_seconds == 7.0
        assert t.failure_reason is None
        assert t.review_reason == "review requested changes"

    def test_changes_requested_run_prefers_verdict_json_reason(self, tmp_path: Path):
        cfg = _default_cfg(tmp_path)
        session_ts = "2026-07-30T16-21-00"
        verdict_path = (
            tmp_path
            / ".lanegate/executor-runs/TICK-003/TICK-003-2026-07-30T16-20-00-1-review/verdict.json"
        )
        verdict_path.parent.mkdir(parents=True)
        verdict_path.write_text(
            json.dumps(
                {
                    "verdict": "changes_requested",
                    "notes": "missing operational docs",
                    "findings": "",
                    "driver": "claude-a",
                    "model": "m",
                }
            )
        )
        _append_run_event(tmp_path, session_ts, "run_start", pid=os.getpid(), ts="2026-07-30T16:20:00Z")
        _append_run_event(
            tmp_path, session_ts, "ticket_dispatch", ticket_id="TICK-003", executor="claude-b", ts="2026-07-30T16:20:01Z"
        )
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_outcome",
            ticket_id="TICK-003",
            outcome="changes_requested",
            reason="review requested changes",
            ts="2026-07-30T16:20:08Z",
        )
        _append_run_event(tmp_path, session_ts, "run_end", status="completed", ts="2026-07-30T16:20:09Z")

        summary = build_run_summary(cfg, tmp_path, session_ts=session_ts)
        assert summary is not None
        assert summary.batch_tickets[0].review_reason == "missing operational docs"

    def test_changes_requested_run_prefers_ticket_review_summary_over_orphaned_verdict_json(
        self, tmp_path: Path
    ):
        """A verdict.json on disk is not guaranteed to belong to this outcome --
        e.g. a reviewer ran and wrote an approved verdict for a ticket that had
        already been routed to needs_review by a failed pre_complete gate, so
        cmd_review rejected applying it. The ticket's own review_summary is
        authoritative and must win over whatever verdict bundle happens to be
        newest on disk.
        """
        cfg = _default_cfg(tmp_path)
        session_ts = "2026-07-30T16-23-00"
        verdict_path = (
            tmp_path
            / ".lanegate/executor-runs/TICK-006/TICK-006-2026-07-30T16-23-00-1-review/verdict.json"
        )
        verdict_path.parent.mkdir(parents=True)
        verdict_path.write_text(
            json.dumps(
                {
                    "verdict": "approved",
                    "notes": "orphaned approval never applied to the ticket",
                    "findings": "",
                    "driver": "claude-a",
                    "model": "m",
                }
            )
        )
        ticket_path = tmp_path / ".lanegate/tickets/TICK-006.md"
        ticket_path.write_text(
            "---\n"
            "id: TICK-006\n"
            "title: Orphaned verdict\n"
            "status: needs_review\n"
            'review_summary: "pre_complete safeguards failed: pytest: nonzero exit"\n'
            "touches: []\n"
            "---\n"
            "Ticket body.\n"
        )
        _append_run_event(tmp_path, session_ts, "run_start", pid=os.getpid(), ts="2026-07-30T16:23:00Z")
        _append_run_event(
            tmp_path, session_ts, "ticket_dispatch", ticket_id="TICK-006", executor="aider-7b", ts="2026-07-30T16:23:01Z"
        )
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_outcome",
            ticket_id="TICK-006",
            outcome="needs_review",
            reason="pre_complete safeguards failed: pytest: nonzero exit",
            ts="2026-07-30T16:23:08Z",
        )
        _append_run_event(tmp_path, session_ts, "run_end", status="completed", ts="2026-07-30T16:23:09Z")

        summary = build_run_summary(cfg, tmp_path, session_ts=session_ts)
        assert summary is not None
        assert (
            summary.batch_tickets[0].review_reason
            == "pre_complete safeguards failed: pytest: nonzero exit"
        )

    def test_changes_requested_run_falls_back_to_review_summary_then_attention_summary(self, tmp_path: Path):
        cfg = _default_cfg(tmp_path)
        session_ts = "2026-07-30T16-22-00"
        ticket_path = tmp_path / ".lanegate/tickets/TICK-005.md"
        ticket_path.write_text(
            "---\n"
            "id: TICK-005\n"
            "title: Review fallback\n"
            "status: needs_review\n"
            "review_verdict: changes_requested\n"
            "review_summary: add deployment notes\n"
            "touches: []\n"
            "---\n"
            "Ticket body.\n"
        )
        _append_run_event(tmp_path, session_ts, "run_start", pid=os.getpid(), ts="2026-07-30T16:22:00Z")
        _append_run_event(
            tmp_path, session_ts, "ticket_dispatch", ticket_id="TICK-005", executor="claude-b", ts="2026-07-30T16:22:01Z"
        )
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_outcome",
            ticket_id="TICK-005",
            outcome="changes_requested",
            reason="review requested changes",
            ts="2026-07-30T16:22:08Z",
        )
        _append_run_event(tmp_path, session_ts, "run_end", status="completed", ts="2026-07-30T16:22:09Z")

        summary = build_run_summary(cfg, tmp_path, session_ts=session_ts)
        assert summary is not None
        assert summary.batch_tickets[0].review_reason == "add deployment notes"

        ticket_path.write_text(
            "---\n"
            "id: TICK-005\n"
            "title: Review fallback\n"
            "status: needs_review\n"
            "review_verdict: changes_requested\n"
            "touches: []\n"
            "---\n"
            "## Needs Review Reason\n\n"
            "restore the rollback procedure\n"
        )
        summary = build_run_summary(cfg, tmp_path, session_ts=session_ts)
        assert summary is not None
        assert summary.batch_tickets[0].review_reason == "restore the rollback procedure"

    def test_skipped_ticket_run(self, tmp_path: Path):
        cfg = _default_cfg(tmp_path)
        session_ts = "2026-07-30T16-30-00"
        _append_run_event(tmp_path, session_ts, "run_start", pid=os.getpid(), ts="2026-07-30T16:30:00Z")
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_dispatch",
            ticket_id="TICK-004",
            executor="claude-a",
            was_hibernated=False,
            ts="2026-07-30T16:30:01Z",
        )
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_outcome",
            ticket_id="TICK-004",
            outcome="skipped",
            ts="2026-07-30T16:30:02Z",
        )
        _append_run_event(tmp_path, session_ts, "run_end", status="completed", ts="2026-07-30T16:30:03Z")

        summary = build_run_summary(cfg, tmp_path, session_ts=session_ts)
        assert summary is not None
        assert summary.reason == RunReason.STOPPED
        assert len(summary.batch_tickets) == 1

        t = summary.batch_tickets[0]
        assert t.ticket_id == "TICK-004"
        assert t.outcome == TicketOutcomeStatus.SKIPPED


def test_read_logs_paginated_prefers_raw_log_over_structured_events(tmp_path: Path):
    session_ts = "2026-07-30T15-30-00"
    _append_run_event(tmp_path, session_ts, "run_start", pid=os.getpid())
    logs_dir = tmp_path / ".lanegate" / "logs"
    (logs_dir / f"orchestrate-{session_ts}.log").write_text("raw first line\nraw second line\n")

    payload = read_logs_paginated(tmp_path, session_ts, offset=0, limit=100)

    assert payload is not None
    assert [event["message"] for event in payload["events"]] == ["raw first line", "raw second line"]
    assert payload["total_count"] == 2


def test_read_logs_paginated_redacts_raw_log_messages_without_changing_pagination(tmp_path: Path):
    session_ts = "2026-07-30T15-31-00"
    _append_run_event(tmp_path, session_ts, "run_start", pid=os.getpid())
    logs_dir = tmp_path / ".lanegate" / "logs"
    (logs_dir / f"orchestrate-{session_ts}.log").write_text(
        "ordinary line\napi_key=sk-1234567890123456789012\n"
    )

    payload = read_logs_paginated(tmp_path, session_ts, offset=1, limit=1)

    assert payload is not None
    assert payload["total_count"] == 2
    assert payload["next_offset"] is None
    assert "sk-" not in payload["events"][0]["message"]
    assert "[REDACTED]" in payload["events"][0]["message"]


class TestUnfinishedDispatchedTicket:
    """A dispatched ticket without a terminal ticket_outcome must read
    in_progress while the orchestrator is alive, and interrupted (with an
    actionable recovery hint) once it isn't — never skipped (TICK-325)."""

    def test_in_progress_while_orchestrator_alive(self, tmp_path: Path):
        cfg = _default_cfg(tmp_path)
        session_ts = "2026-07-31T09-00-00"
        _append_run_event(tmp_path, session_ts, "run_start", pid=os.getpid(), ts="2026-07-31T09:00:00Z")
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_dispatch",
            ticket_id="TICK-600",
            executor="claude-a",
            was_hibernated=False,
            ts="2026-07-31T09:00:01Z",
        )

        summary = build_run_summary(cfg, tmp_path, session_ts=session_ts)
        assert summary is not None
        t = summary.batch_tickets[0]
        assert t.outcome == TicketOutcomeStatus.IN_PROGRESS
        assert t.outcome != TicketOutcomeStatus.SKIPPED
        assert t.duration_seconds >= 0
        assert t.failure_reason is None

    def test_interrupted_once_orchestrator_is_gone(self, tmp_path: Path):
        cfg = _default_cfg(tmp_path)
        session_ts = "2026-07-31T09-10-00"
        _append_run_event(tmp_path, session_ts, "run_start", pid=99999999, ts="2026-07-31T09:10:00Z")
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_dispatch",
            ticket_id="TICK-601",
            executor="claude-a",
            was_hibernated=False,
            ts="2026-07-31T09:10:01Z",
        )

        with patch("lanegate.orchestrate.run_report.pid_alive", return_value=False):
            summary = build_run_summary(cfg, tmp_path, session_ts=session_ts)

        assert summary is not None
        t = summary.batch_tickets[0]
        assert t.outcome == TicketOutcomeStatus.INTERRUPTED
        assert t.outcome != TicketOutcomeStatus.SKIPPED
        assert t.duration_seconds >= 0
        assert t.failure_reason is not None
        assert "lanegate ps" in t.failure_reason
        assert "lanegate orchestrate --tickets TICK-601" in t.failure_reason


class TestCmdRunReport:
    def test_run_report_text_output_includes_terminal_reason_and_summary(self, tmp_path: Path, capsys):
        cfg = _default_cfg(tmp_path)
        session_ts = "2026-07-30T17-00-00"
        _append_run_event(tmp_path, session_ts, "run_start", pid=os.getpid(), ts="2026-07-30T17:00:00Z")
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_dispatch",
            ticket_id="TICK-010",
            executor="claude-3-5",
            was_hibernated=False,
            ts="2026-07-30T17:00:01Z",
        )
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_outcome",
            ticket_id="TICK-010",
            outcome="failed",
            reason="syntax error in build",
            ts="2026-07-30T17:00:05Z",
        )
        _append_run_event(tmp_path, session_ts, "run_end", status="completed", ts="2026-07-30T17:00:06Z")

        cmd_run_report(cfg, tmp_path, session_ts=session_ts)

        out = capsys.readouterr().out
        assert "terminal reason: failure" in out
        assert "TICK-010" in out
        assert "failure" in out
        assert "executor=claude-3-5" in out
        assert "syntax error in build" in out

    def test_run_report_json_output_contains_run_summary(self, tmp_path: Path, capsys):
        cfg = _default_cfg(tmp_path)
        session_ts = "2026-07-30T18-00-00"
        _append_run_event(tmp_path, session_ts, "run_start", pid=os.getpid(), ts="2026-07-30T18:00:00Z")
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_dispatch",
            ticket_id="TICK-020",
            executor="claude-3-5",
            was_hibernated=False,
            ts="2026-07-30T18:00:01Z",
        )
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_outcome",
            ticket_id="TICK-020",
            outcome="success",
            ts="2026-07-30T18:00:10Z",
        )
        _append_run_event(tmp_path, session_ts, "run_end", status="completed", ts="2026-07-30T18:00:11Z")

        cmd_run_report(cfg, tmp_path, session_ts=session_ts, json_output=True)

        out = capsys.readouterr().out
        data = json.loads(out)
        assert "summary" in data
        summary_data = data["summary"]
        assert summary_data["run_id"] == session_ts
        assert summary_data["reason"] == "success"
        assert len(summary_data["batch_tickets"]) == 1
        t_data = summary_data["batch_tickets"][0]
        assert t_data["ticket_id"] == "TICK-020"
        assert t_data["executor"] == "claude-3-5"
        assert t_data["outcome"] == "success"
        assert t_data["duration_seconds"] == 9.0
        assert t_data["failure_reason"] is None
        assert t_data["review_reason"] is None
