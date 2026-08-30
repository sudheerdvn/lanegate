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
    INTERNAL_RUN_ENV,
    _append_run_event,
    begin_direct_action,
    direct_action_tracking_suppressed,
    _resolve_run_session_ts,
    build_run_report,
    build_run_summary,
    cmd_run_report,
    print_run_summary,
    read_logs_paginated,
    record_direct_action_event,
    cmd_ps,
)
from lanegate.logs import _line_style, semantic_line_metadata
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


def test_internal_run_environment_suppresses_direct_action_tracking(monkeypatch):
    """A child executor's CLI process inherits suppression across exec()."""
    monkeypatch.setenv(INTERNAL_RUN_ENV, "1")
    assert direct_action_tracking_suppressed()


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

    def test_build_run_summary_surfaces_triggered_by(self, tmp_path: Path):
        cfg = _default_cfg(tmp_path)
        session_ts = "2026-07-30T17-00-00"
        _append_run_event(
            tmp_path,
            session_ts,
            "run_start",
            pid=os.getpid(),
            ts="2026-07-30T17:00:00Z",
            triggered_by="resume-watch",
            trigger_reason="rate limit on TICK-1",
        )
        _append_run_event(tmp_path, session_ts, "run_end", status="completed", ts="2026-07-30T17:01:00Z")

        summary = build_run_summary(cfg, tmp_path, session_ts=session_ts)
        assert summary is not None
        assert summary.triggered_by == "resume-watch"
        assert summary.trigger_reason == "rate limit on TICK-1"

        # Companion case: older-style run_start missing triggered_by defaults to 'manual'
        session_ts_old = "2026-07-30T18-00-00"
        _append_run_event(tmp_path, session_ts_old, "run_start", pid=os.getpid(), ts="2026-07-30T18:00:00Z")
        _append_run_event(tmp_path, session_ts_old, "run_end", status="completed", ts="2026-07-30T18:01:00Z")

        summary_old = build_run_summary(cfg, tmp_path, session_ts=session_ts_old)
        assert summary_old is not None
        assert summary_old.triggered_by == "manual"
        assert summary_old.trigger_reason is None

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

    def test_auto_merged_ticket_reconciled_in_run_summary(self, tmp_path: Path):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / cfg["tickets_dir"]
        tickets_dir.mkdir(parents=True, exist_ok=True)
        (tickets_dir / "TICK-001.md").write_text(
            "---\n"
            "id: TICK-001\n"
            "title: Merged Ticket\n"
            "status: merged\n"
            "priority: 5\n"
            "---\n"
            "Body.\n"
        )
        session_ts = "2026-07-30T16-30-00"
        _append_run_event(tmp_path, session_ts, "run_start", pid=os.getpid(), ts="2026-07-30T16:30:00Z")
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_dispatch",
            ticket_id="TICK-001",
            executor="claude",
            ts="2026-07-30T16:30:01Z",
        )
        _append_run_event(tmp_path, session_ts, "run_end", status="completed", ts="2026-07-30T16:30:10Z")

        summary = build_run_summary(cfg, tmp_path, session_ts=session_ts)
        assert summary is not None
        assert len(summary.batch_tickets) == 1
        t = summary.batch_tickets[0]
        assert t.ticket_id == "TICK-001"
        assert t.outcome == TicketOutcomeStatus.INTERRUPTED

    def test_build_run_summary_skips_malformed_ticket_id(self, tmp_path: Path):
        """A trailing-space id on an unrelated ticket must not crash the summary build."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / cfg["tickets_dir"]
        tickets_dir.mkdir(parents=True, exist_ok=True)
        (tickets_dir / "TICK-900.md").write_text(
            "---\nid: 'TICK-900 '\ntitle: Malformed\nstatus: open\npriority: 5\n---\nBody.\n"
        )
        session_ts = "2026-07-30T16-14-00"
        _append_run_event(tmp_path, session_ts, "run_start", pid=os.getpid(), ts="2026-07-30T16:14:00Z")
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_dispatch",
            ticket_id="TICK-001",
            executor="claude-a",
            ts="2026-07-30T16:14:01Z",
        )
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_outcome",
            ticket_id="TICK-001",
            outcome="success",
            ts="2026-07-30T16:14:10Z",
        )
        _append_run_event(tmp_path, session_ts, "run_end", status="completed", ts="2026-07-30T16:14:11Z")

        summary = build_run_summary(cfg, tmp_path, session_ts=session_ts)
        assert summary is not None
        assert len(summary.batch_tickets) == 1
        assert summary.batch_tickets[0].ticket_id == "TICK-001"
        assert summary.batch_tickets[0].outcome == TicketOutcomeStatus.SUCCESS

    def test_running_run_returns_running_reason(self, tmp_path: Path):
        cfg = _default_cfg(tmp_path)
        session_ts = "2026-07-30T16-15-00"
        _append_run_event(tmp_path, session_ts, "run_start", pid=os.getpid(), ts="2026-07-30T16:15:00Z")
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_dispatch",
            ticket_id="TICK-003",
            executor="gpt-4o",
            ts="2026-07-30T16:15:01Z",
        )
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_outcome",
            ticket_id="TICK-003",
            outcome="failed",
            reason="pytest exited with code 1",
            ts="2026-07-30T16:15:05Z",
        )

        summary = build_run_summary(cfg, tmp_path, session_ts=session_ts)

        assert summary is not None
        assert summary.reason == RunReason.RUNNING

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

    def test_awaiting_human_review_run(self, tmp_path: Path):
        cfg = _default_cfg(tmp_path)
        session_ts = "2026-07-30T16-20-30"
        _append_run_event(tmp_path, session_ts, "run_start", pid=os.getpid(), ts="2026-07-30T16:20:30Z")
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_dispatch",
            ticket_id="TICK-003",
            executor="claude-b",
            was_hibernated=False,
            ts="2026-07-30T16:20:31Z",
        )
        _append_run_event(
            tmp_path,
            session_ts,
            "ticket_outcome",
            ticket_id="TICK-003",
            outcome="awaiting_human_review",
            reason=None,
            ts="2026-07-30T16:20:38Z",
        )
        _append_run_event(tmp_path, session_ts, "run_end", status="completed", ts="2026-07-30T16:20:39Z")

        summary = build_run_summary(cfg, tmp_path, session_ts=session_ts)
        assert summary is not None
        assert summary.reason == RunReason.STOPPED
        assert len(summary.batch_tickets) == 1

        t = summary.batch_tickets[0]
        assert t.outcome == TicketOutcomeStatus.AWAITING_MERGE
        assert t.review_reason == "reviewer approved — run `lanegate merge` to land it"

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


def test_read_logs_paginated_includes_presentation_metadata(tmp_path: Path):
    session_ts = "2026-07-30T15-30-01"
    logs_dir = tmp_path / ".lanegate" / "logs"
    logs_dir.mkdir(parents=True)
    (logs_dir / f"orchestrate-{session_ts}.log").write_text(
        "[orchestrate] TICK-1: executor finished (exit 0)\n"
        "[orchestrate] TICK-2: WARNING — rate limited\n"
        "executor failed (exit 1)\n"
        '{"type":"item.started"}\n'
        "[orchestrate] review verdict CHANGES_REQUESTED\n"
        "+added line\n"
    )

    payload = read_logs_paginated(tmp_path, session_ts)

    assert payload is not None
    assert [event["message"] for event in payload["events"]] == [
        "[orchestrate] TICK-1: executor finished (exit 0)",
        "[orchestrate] TICK-2: WARNING — rate limited",
        "executor failed (exit 1)",
        '{"type":"item.started"}',
        "[orchestrate] review verdict CHANGES_REQUESTED",
        "+added line",
    ]
    assert [
        {key: event[key] for key in ("style", "level", "kind")}
        for event in payload["events"]
    ] == [
        semantic_line_metadata(line)
        for line in [
            "[orchestrate] TICK-1: executor finished (exit 0)",
            "[orchestrate] TICK-2: WARNING — rate limited",
            "executor failed (exit 1)",
            '{"type":"item.started"}',
            "[orchestrate] review verdict CHANGES_REQUESTED",
            "+added line",
        ]
    ]
    assert [(event["level"], event["kind"]) for event in payload["events"]] == [
        ("info", "orchestrator"),
        ("warning", "orchestrator"),
        ("error", "executor"),
        ("info", "protocol"),
        ("warning", "orchestrator"),
        ("success", "executor"),
    ]


def test_line_style_anchored_error():
    for line in (
        "ERROR: executor could not start",
        "[orchestrate] ERROR worker exited unexpectedly",
        "Traceback (most recent call last):",
        "  ERROR: executor could not start",
        "  Traceback (most recent call last):",
        "[orchestrate] TICK-500: executor configuration failed for 'codex': no such binary",
        "[orchestrate] TICK-500 merge failed — downgrading to needs_review",
        "FAILED tests/test_run_report.py::test_x - AssertionError",
        "[orchestrate] TICK-500 executor finished (exit 1, 87s elapsed)",
        "executor failed (exit code 1: general error)",
    ):
        assert _line_style(line) == "bold red"
        assert semantic_line_metadata(line)["level"] == "error"

    for line in (
        "The ticket body mentions ERROR: handling.",
        "The previous run FAILED after a retry.",
        "See traceback details in the review notes.",
        "executor completed (exit 0)",
    ):
        assert _line_style(line) != "bold red"
        assert semantic_line_metadata(line)["level"] != "error"


def test_read_logs_paginated_action_events_include_presentation_metadata(tmp_path: Path):
    action_id = "action-2026-07-30T15-30-02Z"
    record_direct_action_event(
        tmp_path,
        action_id,
        "action_end",
        status="failed",
        message="ERROR: action could not complete",
    )

    payload = read_logs_paginated(tmp_path, action_id)

    assert payload is not None
    assert payload["run_id"] == action_id
    event = payload["events"][0]
    assert event["message"] == "ERROR: action could not complete"
    assert {key: event[key] for key in ("level", "style", "kind")} == {
        "level": "error",
        "style": "bold red",
        "kind": "structured",
    }


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


def test_read_log_page_post_slice_enrichment(tmp_path: Path):
    session_ts = "2026-07-30T15-31-01"
    logs_dir = tmp_path / ".lanegate" / "logs"
    logs_dir.mkdir(parents=True)
    selected_line = "api_key=sk-1234567890123456789012"
    (logs_dir / f"orchestrate-{session_ts}.log").write_text(
        f"before page\n{selected_line}\nafter page\n"
    )

    with patch(
        "lanegate.orchestrate.run_report.redact_transcript_text",
        side_effect=lambda message: f"redacted:{message}",
    ) as redact, patch(
        "lanegate.orchestrate.run_report.semantic_line_metadata",
        return_value={"style": "", "level": "info", "kind": "executor"},
    ) as metadata:
        payload = read_logs_paginated(tmp_path, session_ts, offset=1, limit=1)

    assert payload is not None
    assert payload["total_count"] == 3
    assert payload["offset"] == 1
    assert payload["next_offset"] == 2
    assert payload["events"] == [
        {
            "ts": "",
            "event": "log",
            "message": f"redacted:{selected_line}",
            "level": "info",
            "style": "",
            "kind": "executor",
        }
    ]
    redact.assert_called_once_with(selected_line)
    metadata.assert_called_once_with(f"redacted:{selected_line}")


def test_resolve_run_session_ts_validates_session_id(tmp_path: Path):
    invalid_run_ids = (
        "TICK-001-1700000000-1-implement",
        "2026-08-10T18-32-45",
    )

    for run_id in invalid_run_ids:
        assert _resolve_run_session_ts(tmp_path, run_id) is None
        assert read_logs_paginated(tmp_path, run_id) is None


def test_resolve_run_session_ts_maps_current_api_run_to_active_session(tmp_path: Path):
    api_run_id = "run-20260810T050000Z-a1b2c3d4"
    session_ts = "2026-08-10T18-32-45"
    logs_dir = tmp_path / ".lanegate" / "logs"
    logs_dir.mkdir(parents=True)
    (tmp_path / ".lanegate" / "api-run-current.json").write_text(json.dumps({"run_id": api_run_id}))
    (logs_dir / f"orchestrate-{session_ts}.log").write_text("API audit line\n")

    with patch("lanegate.orchestrate.run_report.orchestrator_lock_status", return_value={"held": True}):
        assert _resolve_run_session_ts(tmp_path, api_run_id) == session_ts
        payload = read_logs_paginated(tmp_path, api_run_id)

    assert payload is not None
    assert payload["run_id"] == session_ts
    assert [event["message"] for event in payload["events"]] == ["API audit line"]


def test_run_summary_preserves_all_executors_that_handled_a_ticket(tmp_path: Path):
    """A fix/review failover must not be reported as only its first worker."""
    cfg = _default_cfg(tmp_path)
    session_ts = "2026-08-11T16-35-23"
    _append_run_event(tmp_path, session_ts, "run_start", pid=os.getpid(), ts="2026-08-11T23:35:23Z")
    _append_run_event(
        tmp_path, session_ts, "ticket_dispatch", ticket_id="TICK-500", executor="claude-a",
        ts="2026-08-11T23:35:41Z",
    )
    _append_run_event(
        tmp_path, session_ts, "executor_metrics", ticket_id="TICK-500",
        metrics={"step": "fix", "executor": "codex"}, ts="2026-08-11T23:45:42Z",
    )
    _append_run_event(
        tmp_path, session_ts, "executor_progress", ticket_id="TICK-500",
        progress={"executor": "claude-b"}, ts="2026-08-11T23:46:39Z",
    )
    _append_run_event(
        tmp_path, session_ts, "ticket_outcome", ticket_id="TICK-500", outcome="merged",
        ts="2026-08-12T00:01:17Z",
    )
    _append_run_event(tmp_path, session_ts, "run_end", status="completed", ts="2026-08-12T00:01:32Z")

    summary = build_run_summary(cfg, tmp_path, session_ts=session_ts)
    report = build_run_report(cfg, tmp_path, session_ts=session_ts)

    assert summary is not None
    assert summary.batch_tickets[0].executor == "claude-a → codex → claude-b"
    assert report is not None
    assert report["tickets"][0]["executor"] == "claude-a → codex → claude-b"


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
        assert "lanegate run --tickets TICK-601" in t.failure_reason


class TestCmdRunReport:
    def test_run_report_accepts_direct_action_id(self, tmp_path: Path, capsys):
        cfg = _default_cfg(tmp_path)
        tracking = begin_direct_action(tmp_path, "merge", ticket_id="TICK-123", executor="cli")
        record_direct_action_event(
            tmp_path, tracking["action_id"], "action_end", action_type="merge",
            ticket_id="TICK-123", status="success",
        )

        cmd_run_report(cfg, tmp_path, session_ts=tracking["action_id"])

        out = capsys.readouterr().out
        assert f"Action: {tracking['action_id']}" in out
        assert "status: success" in out
        assert "TICK-123" in out

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


def test_ps_includes_recent_completed_direct_actions(tmp_path: Path, capsys):
    cfg = _default_cfg(tmp_path)
    tracking = begin_direct_action(tmp_path, "review", ticket_id="TICK-123", executor="cli")
    record_direct_action_event(
        tmp_path, tracking["action_id"], "action_end", action_type="review",
        ticket_id="TICK-123", status="success",
    )

    assert Path(tracking["log_path"]).is_file()
    assert '"action_type": "review"' in Path(tracking["log_path"]).read_text()
    cmd_ps(cfg, tmp_path)
    out = capsys.readouterr().out
    assert tracking["action_id"] in out
    assert "Direct actions (recent):" in out


def test_ps_includes_running_direct_actions(tmp_path: Path, capsys):
    cfg = _default_cfg(tmp_path)
    tracking = begin_direct_action(tmp_path, "review", ticket_id="TICK-123", executor="cli")

    cmd_ps(cfg, tmp_path)
    out = capsys.readouterr().out
    assert tracking["action_id"] in out
    assert "Direct actions (recent):" in out


def test_stream_subprocess_kills_process_on_worktree_guard_violation(tmp_path: Path):
    import sys
    import time
    from lanegate.orchestrate.pool import WorktreeGuardViolation
    from lanegate.orchestrate.run_report import _stream_subprocess

    cmd = [
        sys.executable,
        "-c",
        "import time; [print(i, flush=True) or time.sleep(0.05) for i in range(200)]",
    ]

    def on_line_guard(line: str, is_stdout: bool = True):
        raise WorktreeGuardViolation("[worktree-guard] test violation")

    start_ts = time.time()
    rc, stdout, stderr, kill_reason = _stream_subprocess(
        cmd,
        str(tmp_path),
        on_line=on_line_guard,
        idle_timeout=5,
        absolute_ceiling=5,
    )
    elapsed = time.time() - start_ts

    assert rc != 0
    assert kill_reason == "worktree_violation"
    assert elapsed < 3.0


def _sleepy_cmd(seconds: float = 3.0) -> list[str]:
    import sys
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def test_stream_subprocess_classifies_suspend_gap_when_wall_time_dwarfs_cpu(tmp_path: Path):
    """elapsed > 3x timeout with near-zero watchdog CPU => kill_reason 'suspend_gap'.

    Simulated by burning wall-clock time in on_start (which runs after start_ts
    is taken) while the watchdog process itself only sleeps — exactly the shape
    of the orchestrator being SIGSTOPped mid-run.
    """
    import time
    from lanegate.orchestrate.run_report import _stream_subprocess

    rc, _stdout, stderr, kill_reason = _stream_subprocess(
        _sleepy_cmd(3.0),
        str(tmp_path),
        timeout=0.1,
        budget_probe=lambda: None,  # force the watchdog while-loop path
        on_start=lambda _pid: time.sleep(0.6),  # wall time passes, CPU does not
    )

    assert rc != 0
    assert kill_reason == "suspend_gap"
    assert "suspended" in stderr


def test_stream_subprocess_classifies_ordinary_timeout_when_cpu_tracks_wall(tmp_path: Path):
    """A normal timeout (elapsed ~= timeout) is NOT a suspend gap.

    ``_stream_subprocess`` collapses a plain ``timeout`` kill_reason to ``None``
    in its return tuple (legacy contract); the point here is that it does not
    escalate to ``suspend_gap``.
    """
    import time
    from lanegate.orchestrate.run_report import _stream_subprocess

    # Generous absolute numbers so a loaded CI runner's scheduling jitter can't
    # push `elapsed` past the suspend-gap threshold (3 * timeout). With
    # timeout=1.0s the wall clock has to reach 3.0s to misclassify; on_start
    # sleeps only ~1.2s (just over timeout, far under 3x), leaving ~1.8s of
    # headroom. A tiny timeout (0.1s / 0.3s threshold) flaked on macOS runners.
    rc, _stdout, stderr, kill_reason = _stream_subprocess(
        _sleepy_cmd(5.0),
        str(tmp_path),
        timeout=1.0,
        budget_probe=lambda: None,
        on_start=lambda _pid: time.sleep(1.2),
    )

    assert rc != 0
    assert kill_reason != "suspend_gap"
    assert "timed out after 1.0s" in stderr

