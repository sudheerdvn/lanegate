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


@pytest.mark.parametrize(
    "outcome",
    [
        TicketOutcomeStatus.SUCCESS,
        TicketOutcomeStatus.FAILURE,
        TicketOutcomeStatus.CHANGES_REQUESTED,
        TicketOutcomeStatus.SKIPPED,
        TicketOutcomeStatus.IN_PROGRESS,
        TicketOutcomeStatus.INTERRUPTED,
    ],
)
def test_ticket_outcome_enum_values_round_trip(outcome):
    t = _make_ticket_outcome(outcome=outcome)
    assert TicketOutcomeStatus(t.to_dict()["outcome"]) == outcome


@pytest.mark.parametrize("reason", [RunReason.SUCCESS, RunReason.FAILURE, RunReason.STOPPED])
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
