"""
Tests for lanegate/orchestrate/run_summary.py — the structured run-summary model.

Coverage:
  - RunSummary/TicketOutcome construction and field types
  - to_dict/from_dict round-trip (dict serialization for the API/TUI)
  - enum validation for `reason` and `outcome`
  - missing/None optional field handling
  - timestamp parsing through the dict round-trip
"""

from __future__ import annotations

import datetime

import pytest

from lanegate.orchestrate.run_summary import (
    RunReason,
    RunSummary,
    TicketOutcome,
    TicketOutcomeStatus,
    list_run_summaries,
)


def _make_ticket_outcome(**overrides) -> TicketOutcome:
    fields = dict(
        ticket_id="TICK-100",
        executor="claude-a",
        outcome=TicketOutcomeStatus.SUCCESS,
        duration_seconds=42.0,
    )
    fields.update(overrides)
    return TicketOutcome(**fields)


def _make_run_summary(**overrides) -> RunSummary:
    fields = dict(
        run_id="run-2026-07-30T16-00-00Z",
        timestamp=datetime.datetime(2026, 7, 30, 16, 0, 0, tzinfo=datetime.UTC),
        reason=RunReason.SUCCESS,
        batch_tickets=[_make_ticket_outcome()],
    )
    fields.update(overrides)
    return RunSummary(**fields)


def test_ticket_outcome_field_types():
    t = _make_ticket_outcome()
    assert t.ticket_id == "TICK-100"
    assert t.executor == "claude-a"
    assert isinstance(t.outcome, TicketOutcomeStatus)
    assert isinstance(t.duration_seconds, float)
    assert t.failure_reason is None
    assert t.review_reason is None
    assert t.lifecycle_summary is None


def test_ticket_outcome_round_trips_lifecycle_summary():
    ticket = _make_ticket_outcome(lifecycle_summary="merge completed on main")
    restored = TicketOutcome.from_dict(ticket.to_dict())
    assert restored.lifecycle_summary == "merge completed on main"


def test_run_summary_field_types():
    r = _make_run_summary()
    assert r.run_id == "run-2026-07-30T16-00-00Z"
    assert isinstance(r.timestamp, datetime.datetime)
    assert isinstance(r.reason, RunReason)
    assert isinstance(r.batch_tickets, list)
    assert all(isinstance(t, TicketOutcome) for t in r.batch_tickets)


def test_run_summary_defaults_to_empty_batch_tickets():
    r = RunSummary(
        run_id="run-empty",
        timestamp=datetime.datetime.now(datetime.UTC),
        reason=RunReason.STOPPED,
    )
    assert r.batch_tickets == []


def test_run_summary_round_trips_triggered_by():
    r_default = RunSummary(
        run_id="run-1",
        timestamp=datetime.datetime.now(datetime.UTC),
        reason=RunReason.SUCCESS,
    )
    assert r_default.triggered_by == "manual"
    assert r_default.trigger_reason is None

    r_custom = RunSummary(
        run_id="run-2",
        timestamp=datetime.datetime.now(datetime.UTC),
        reason=RunReason.SUCCESS,
        triggered_by="resume-watch",
        trigger_reason="rate limit on TICK-1",
    )
    d = r_custom.to_dict()
    assert d["triggered_by"] == "resume-watch"
    assert d["trigger_reason"] == "rate limit on TICK-1"

    restored = RunSummary.from_dict(d)
    assert restored.triggered_by == "resume-watch"
    assert restored.trigger_reason == "rate limit on TICK-1"



@pytest.mark.parametrize(
    "outcome",
    [
        TicketOutcomeStatus.SUCCESS,
        TicketOutcomeStatus.FAILURE,
        TicketOutcomeStatus.CHANGES_REQUESTED,
        TicketOutcomeStatus.AWAITING_MERGE,
        TicketOutcomeStatus.SKIPPED,
        TicketOutcomeStatus.IN_PROGRESS,
        TicketOutcomeStatus.INTERRUPTED,
    ],
)
def test_ticket_outcome_enum_values_round_trip(outcome):
    t = _make_ticket_outcome(outcome=outcome)
    assert TicketOutcomeStatus(t.to_dict()["outcome"]) == outcome


@pytest.mark.parametrize(
    "reason", [RunReason.RUNNING, RunReason.SUCCESS, RunReason.FAILURE, RunReason.STOPPED]
)
def test_run_summary_enum_values_round_trip(reason):
    r = _make_run_summary(reason=reason)
    assert RunReason(r.to_dict()["reason"]) == reason


def test_ticket_outcome_invalid_enum_raises():
    with pytest.raises(ValueError):
        TicketOutcomeStatus("not-a-real-outcome")


def test_run_summary_invalid_enum_raises():
    with pytest.raises(ValueError):
        RunReason("not-a-real-reason")


def test_ticket_outcome_from_dict_rejects_invalid_outcome():
    data = _make_ticket_outcome().to_dict()
    data["outcome"] = "bogus"
    with pytest.raises(ValueError):
        TicketOutcome.from_dict(data)


def test_run_summary_from_dict_rejects_invalid_reason():
    data = _make_run_summary().to_dict()
    data["reason"] = "bogus"
    with pytest.raises(ValueError):
        RunSummary.from_dict(data)


def test_ticket_outcome_round_trip():
    t = _make_ticket_outcome(failure_reason="timeout", review_reason="needs tests")
    assert TicketOutcome.from_dict(t.to_dict()) == t


def test_ticket_outcome_round_trip_missing_optional_fields():
    t = _make_ticket_outcome()
    d = t.to_dict()
    assert d["failure_reason"] is None
    assert d["review_reason"] is None
    assert TicketOutcome.from_dict(d) == t


def test_ticket_outcome_from_dict_tolerates_absent_optional_keys():
    d = _make_ticket_outcome().to_dict()
    del d["failure_reason"]
    del d["review_reason"]
    t = TicketOutcome.from_dict(d)
    assert t.failure_reason is None
    assert t.review_reason is None


def test_run_summary_round_trip():
    r = _make_run_summary(
        batch_tickets=[
            _make_ticket_outcome(),
            _make_ticket_outcome(
                ticket_id="TICK-101",
                outcome=TicketOutcomeStatus.FAILURE,
                failure_reason="pytest exit 1",
            ),
        ]
    )
    assert RunSummary.from_dict(r.to_dict()) == r


def test_run_summary_round_trip_empty_batch_tickets():
    r = _make_run_summary(batch_tickets=[])
    d = r.to_dict()
    assert d["batch_tickets"] == []
    assert RunSummary.from_dict(d) == r


def test_run_summary_timestamp_parses_through_dict_round_trip():
    ts = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)
    r = _make_run_summary(timestamp=ts)
    parsed = RunSummary.from_dict(r.to_dict())
    assert parsed.timestamp == ts


def test_run_summary_to_dict_is_json_serializable():
    import json

    r = _make_run_summary()
    json.dumps(r.to_dict())  # must not raise


def test_list_run_summaries_includes_direct_actions(tmp_path):
    from lanegate.orchestrate.run_report import begin_direct_action, record_direct_action_event

    tracking = begin_direct_action(tmp_path, "merge", ticket_id="TICK-099", executor="cli")
    record_direct_action_event(
        tmp_path, tracking["action_id"], "action_end", action_type="merge",
        ticket_id="TICK-099", status="success",
    )
    summaries = list_run_summaries({"tickets_dir": "tickets", "ticket_prefix": "TICK"}, tmp_path)
    summary = next(s for s in summaries if s.run_id == tracking["action_id"])
    assert summary.reason == RunReason.SUCCESS
    assert summary.batch_tickets[0].ticket_id == "TICK-099"
    assert summary.batch_tickets[0].executor == "direct:merge"


def test_list_run_summaries_reports_interrupted_for_ticket_without_outcome_event(tmp_path):
    import json

    tickets_dir = tmp_path / ".lanegate" / "tickets"
    tickets_dir.mkdir(parents=True)
    (tickets_dir / "TICK-101.md").write_text("---\nid: TICK-101\ntitle: Test Ticket 101\nstatus: merged\n---\n", encoding="utf-8")

    logs_dir = tmp_path / ".lanegate" / "logs"
    logs_dir.mkdir(parents=True)
    events_path = logs_dir / "orchestrate-20260811-010000.events.jsonl"
    events = [
        {"ts": "2026-08-11T01:00:00Z", "event": "run_start"},
        {"ts": "2026-08-11T01:00:01Z", "event": "ticket_dispatch", "ticket_id": "TICK-101", "executor": "claude"},
        {"ts": "2026-08-11T01:00:02Z", "event": "run_end", "status": "completed"},
    ]
    events_path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    cfg = {"tickets_dir": ".lanegate/tickets", "ticket_prefix": "TICK"}
    summaries = list_run_summaries(cfg, tmp_path)
    assert len(summaries) == 1
    s = summaries[0]
    assert len(s.batch_tickets) == 1
    assert s.batch_tickets[0].ticket_id == "TICK-101"
    assert s.batch_tickets[0].outcome == TicketOutcomeStatus.INTERRUPTED


def test_build_run_summary_preserves_recorded_failure_for_merged_ticket(tmp_path):
    import json
    from lanegate.orchestrate.run_summary import build_run_summary

    tickets_dir = tmp_path / ".lanegate" / "tickets"
    tickets_dir.mkdir(parents=True)
    (tickets_dir / "TICK-102.md").write_text("---\nid: TICK-102\ntitle: Test Ticket 102\nstatus: merged\n---\n", encoding="utf-8")

    logs_dir = tmp_path / ".lanegate" / "logs"
    logs_dir.mkdir(parents=True)
    events_path = logs_dir / "orchestrate-20260811-010000.events.jsonl"
    events = [
        {"ts": "2026-08-11T01:00:00Z", "event": "run_start"},
        {"ts": "2026-08-11T01:00:01Z", "event": "ticket_dispatch", "ticket_id": "TICK-102", "executor": "claude"},
        {"ts": "2026-08-11T01:00:02Z", "event": "ticket_outcome", "ticket_id": "TICK-102", "outcome": "failure", "reason": "pytest failed"},
        {"ts": "2026-08-11T01:00:03Z", "event": "run_end", "status": "completed"},
    ]
    events_path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    cfg = {"tickets_dir": ".lanegate/tickets", "ticket_prefix": "TICK"}
    summary = build_run_summary(cfg, tmp_path, session_ts="20260811-010000")
    assert summary is not None
    assert summary.batch_tickets[0].outcome == TicketOutcomeStatus.FAILURE
    assert "pytest failed" in (summary.batch_tickets[0].failure_reason or "")


def test_list_run_summaries_preserves_direct_merge_action_for_supervised_run(tmp_path):
    import json

    tickets_dir = tmp_path / ".lanegate" / "tickets"
    tickets_dir.mkdir(parents=True)
    (tickets_dir / "TICK-103.md").write_text("---\nid: TICK-103\ntitle: Test Ticket 103\nstatus: merged\n---\n", encoding="utf-8")

    logs_dir = tmp_path / ".lanegate" / "logs"
    logs_dir.mkdir(parents=True)
    events_path = logs_dir / "orchestrate-20260811-010000.events.jsonl"
    events = [
        {"ts": "2026-08-11T01:00:00Z", "event": "run_start"},
        {"ts": "2026-08-11T01:00:01Z", "event": "ticket_dispatch", "ticket_id": "TICK-103", "executor": "claude"},
        {"ts": "2026-08-11T01:00:02Z", "event": "ticket_outcome", "ticket_id": "TICK-103", "outcome": "awaiting_human_review"},
        {"ts": "2026-08-11T01:00:03Z", "event": "run_end", "status": "completed"},
    ]
    events_path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    action_path = logs_dir / "action-20260811-020000.events.jsonl"
    action_events = [
        {"ts": "2026-08-11T02:00:00Z", "event": "action_start", "action_id": "action-20260811-020000", "action_type": "merge", "ticket_id": "TICK-103"},
        {"ts": "2026-08-11T02:00:05Z", "event": "action_end", "action_id": "action-20260811-020000", "action_type": "merge", "ticket_id": "TICK-103", "status": "success"},
    ]
    action_path.write_text("\n".join(json.dumps(e) for e in action_events) + "\n", encoding="utf-8")

    cfg = {"tickets_dir": ".lanegate/tickets", "ticket_prefix": "TICK"}
    summaries = list_run_summaries(cfg, tmp_path)
    assert len(summaries) == 2
    action_summary = next(s for s in summaries if s.run_id == "action-20260811-020000")
    assert action_summary.batch_tickets[0].ticket_id == "TICK-103"
    assert action_summary.batch_tickets[0].executor == "direct:merge"


def test_build_direct_action_summary_review_changes_requested_is_not_failure(tmp_path):
    import json
    from lanegate.orchestrate.run_summary import _build_direct_action_summary

    logs_dir = tmp_path / ".lanegate" / "logs"
    logs_dir.mkdir(parents=True)
    action_id = "action-20260811-030000"
    action_path = logs_dir / f"{action_id}.events.jsonl"
    action_events = [
        {"ts": "2026-08-11T03:00:00Z", "event": "action_start", "action_id": action_id, "action_type": "review", "ticket_id": "TICK-104"},
        {
            "ts": "2026-08-11T03:00:05Z",
            "event": "action_end",
            "action_id": action_id,
            "action_type": "review",
            "ticket_id": "TICK-104",
            "status": "failed",
            "verdict": "changes_requested",
            "review_summary": "Needs tests",
        },
    ]
    action_path.write_text("\n".join(json.dumps(e) for e in action_events) + "\n", encoding="utf-8")

    summary = _build_direct_action_summary(tmp_path, action_id)
    assert summary is not None
    assert summary.batch_tickets[0].outcome == TicketOutcomeStatus.CHANGES_REQUESTED
    assert summary.batch_tickets[0].review_reason == "Needs tests"


def test_build_direct_action_summary_review_approved_is_success(tmp_path):
    import json
    from lanegate.orchestrate.run_summary import _build_direct_action_summary

    logs_dir = tmp_path / ".lanegate" / "logs"
    logs_dir.mkdir(parents=True)
    action_id = "action-20260811-030100"
    action_path = logs_dir / f"{action_id}.events.jsonl"
    action_events = [
        {"ts": "2026-08-11T03:01:00Z", "event": "action_start", "action_id": action_id, "action_type": "review", "ticket_id": "TICK-105"},
        {
            "ts": "2026-08-11T03:01:05Z",
            "event": "action_end",
            "action_id": action_id,
            "action_type": "review",
            "ticket_id": "TICK-105",
            "status": "success",
            "verdict": "approved",
        },
    ]
    action_path.write_text("\n".join(json.dumps(e) for e in action_events) + "\n", encoding="utf-8")

    summary = _build_direct_action_summary(tmp_path, action_id)
    assert summary is not None
    assert summary.batch_tickets[0].outcome == TicketOutcomeStatus.SUCCESS

