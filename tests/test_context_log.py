"""
tests/test_context_log.py — Unit tests for lanegate.context_log
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lanegate.context_log import (
    _import_legacy,
    _is_legacy_imported,
    _load_entries_from_db,
    _load_step_costs_from_db,
    _local_day,
    _print_basic_table,
    _print_by_day,
    _print_compare,
    _real_executor_by_ticket,
    _upsert_row,
    cmd_context_stats,
    cmd_log_backfill,
    compute_stats,
    compute_step_cost_stats,
    get_ticket_executor,
    load_entries_for_analytics,
    log_agent_run,
    log_step_cost,
    record_step_cost,
    resume_session_gate,
    stats_json,
)
from lanegate.executor import (
    parse_agy_json_result,
    parse_claude_json_result,
    parse_codex_json_result,
    parse_structured_result,
)
from lanegate.lifecycle import _get_branch_wall_time_ms, _get_touched_files


@pytest.fixture(autouse=True)
def fixture_project_root(tmp_path, monkeypatch):
    """Prevent payload analytics from discovering real tickets via Path.cwd()."""
    monkeypatch.chdir(tmp_path)


def test_default_analytics_root_is_fixture_root(tmp_path):
    """Default analytics discovery reads the fixture tickets directory."""
    from lanegate.context_log import compute_payload_composition_stats

    tickets_dir = tmp_path / ".lanegate" / "tickets"
    tickets_dir.mkdir(parents=True)
    with patch("lanegate.ticket.load_all_tickets", return_value=[]) as load_tickets:
        compute_payload_composition_stats()

    assert load_tickets.call_args.args[0] == tickets_dir


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_file(directory: Path, name: str, lines: int) -> Path:
    """Create a file with exactly `lines` non-empty lines."""
    p = directory / name
    p.write_text("\n".join(f"line{i}" for i in range(lines)) + "\n")
    return p


def _write_records(log_file: Path, records: list[dict]) -> None:
    log_file.write_text("\n".join(json.dumps(r) for r in records) + "\n")


# ---------------------------------------------------------------------------
# log_agent_run tests
# ---------------------------------------------------------------------------


def test_log_agent_run_appends_valid_json_line(tmp_path: Path) -> None:
    """log_agent_run writes exactly one valid JSON line with new schema fields."""
    log_file = tmp_path / "context.jsonl"
    _make_file(tmp_path, "foo.py", 10)

    log_agent_run(
        log_path=log_file,
        ticket_id="TICK-001",
        subagent_tokens=1000,
        tool_uses=5,
        duration_ms=3000,
        touched_files=["foo.py"],
        repo_root=tmp_path,
        summary_tokens=180,
    )

    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1

    record = json.loads(lines[0])
    assert record["ticket_id"] == "TICK-001"
    assert record["subagent_tokens"] == 1000
    assert record["tool_uses"] == 5
    assert record["duration_ms"] == 3000
    assert "timestamp" in record
    assert record["timestamp"].endswith("Z")
    # New schema: no estimated_inline_tokens; has summary_tokens instead
    assert "estimated_inline_tokens" not in record
    assert record["summary_tokens"] == 180


def test_log_agent_run_writes_new_fields(tmp_path: Path) -> None:
    """log_agent_run writes all new schema fields to JSONL."""
    log_file = tmp_path / "context.jsonl"

    log_agent_run(
        log_path=log_file,
        ticket_id="TICK-NEW",
        subagent_tokens=5000,
        tool_uses=10,
        duration_ms=60000,
        touched_files=[],
        repo_root=tmp_path,
        summary_tokens=150,
        executor="claude-subagent",
        model="claude-sonnet-4-6",
        wall_time_ms=72000,
        parallel_peers=["TICK-002", "TICK-003"],
        batch_id="batch-001",
        tests_passed=True,
        drift_warnings=0,
    )

    record = json.loads(log_file.read_text().strip())
    assert record["executor"] == "claude-subagent"
    assert record["model"] == "claude-sonnet-4-6"
    assert record["wall_time_ms"] == 72000
    assert record["parallel_peers"] == ["TICK-002", "TICK-003"]
    assert record["batch_id"] == "batch-001"
    assert record["tests_passed"] is True
    assert record["drift_warnings"] == 0
    assert record["summary_tokens"] == 150


def test_log_agent_run_null_subagent_tokens(tmp_path: Path) -> None:
    """subagent_tokens=None writes null in JSON."""
    log_file = tmp_path / "context.jsonl"

    log_agent_run(
        log_path=log_file,
        ticket_id="TICK-NULL",
        subagent_tokens=None,
        tool_uses=3,
        duration_ms=1000,
        touched_files=[],
        repo_root=tmp_path,
    )

    record = json.loads(log_file.read_text().strip())
    assert record["subagent_tokens"] is None


def test_log_agent_run_creates_log_file_if_missing(tmp_path: Path) -> None:
    """log_agent_run creates the log file when it doesn't exist yet."""
    log_file = tmp_path / "new_dir" / "context.jsonl"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    assert not log_file.exists()

    log_agent_run(
        log_path=log_file,
        ticket_id="TICK-004",
        subagent_tokens=100,
        tool_uses=1,
        duration_ms=200,
        touched_files=[],
        repo_root=tmp_path,
    )

    assert log_file.exists()
    record = json.loads(log_file.read_text().strip())
    assert record["ticket_id"] == "TICK-004"


def test_log_agent_run_uses_default_log_path(tmp_path: Path) -> None:
    """When log_path=None the file lands at repo_root/lanegate-context-log.jsonl."""
    log_agent_run(
        log_path=None,
        ticket_id="TICK-005",
        subagent_tokens=50,
        tool_uses=1,
        duration_ms=100,
        touched_files=[],
        repo_root=tmp_path,
    )

    default = tmp_path / "lanegate-context-log.jsonl"
    assert default.exists()
    record = json.loads(default.read_text().strip())
    assert record["ticket_id"] == "TICK-005"


def test_log_agent_run_appends_multiple_lines(tmp_path: Path) -> None:
    """Multiple calls append multiple lines rather than overwriting."""
    log_file = tmp_path / "context.jsonl"

    for i in range(3):
        log_agent_run(
            log_path=log_file,
            ticket_id=f"TICK-{i:03d}",
            subagent_tokens=100 * (i + 1),
            tool_uses=i + 1,
            duration_ms=500 * (i + 1),
            touched_files=[],
            repo_root=tmp_path,
        )

    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 3

    ids = [json.loads(line)["ticket_id"] for line in lines]
    assert ids == ["TICK-000", "TICK-001", "TICK-002"]


# ---------------------------------------------------------------------------
# cmd_context_stats — basic (no flags)
# ---------------------------------------------------------------------------


def test_cmd_context_stats_no_file(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Missing log file prints the sentinel message."""
    log_file = tmp_path / "missing.jsonl"

    cmd_context_stats(log_file)

    out = capsys.readouterr().out
    assert "No context log entries yet." in out


def test_cmd_context_stats_empty_file(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Empty log file prints the sentinel message."""
    log_file = tmp_path / "empty.jsonl"
    log_file.write_text("")

    cmd_context_stats(log_file)

    out = capsys.readouterr().out
    assert "No context log entries yet." in out


def test_cmd_context_stats_corrected_compression_ratio(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Default view shows corrected compression ratio: work_tok / main_tok."""
    log_file = tmp_path / "context.jsonl"

    records = [
        {
            "ticket_id": "TICK-006",
            "subagent_tokens": 28656,
            "summary_tokens": 180,
            "tool_uses": 15,
            "duration_ms": 84930,
            "timestamp": "2026-06-20T14:23:00Z",
            "executor": "claude-subagent",
            "model": "claude-sonnet-4-6",
        },
        {
            "ticket_id": "TICK-007",
            "subagent_tokens": 21554,
            "summary_tokens": 150,
            "tool_uses": 10,
            "duration_ms": 60000,
            "timestamp": "2026-06-20T15:00:00Z",
            "executor": "claude-subagent",
            "model": "claude-sonnet-4-6",
        },
        {
            "ticket_id": "TICK-008",
            "subagent_tokens": 22609,
            "summary_tokens": 160,
            "tool_uses": 12,
            "duration_ms": 70000,
            "timestamp": "2026-06-20T16:00:00Z",
            "executor": "claude-subagent",
            "model": "claude-sonnet-4-6",
        },
    ]
    _write_records(log_file, records)

    cmd_context_stats(log_file)

    out = capsys.readouterr().out

    assert "TICK-006" in out
    assert "TICK-007" in out
    assert "TICK-008" in out
    assert "TOTAL" in out

    # Work tokens column
    assert "28,656" in out
    assert "21,554" in out
    assert "22,609" in out

    # Main-session tok column
    assert "180" in out
    assert "150" in out

    # Compression ratio for TICK-006: 28656/180 ≈ 159x
    assert "159x" in out

    # Total work tokens: 28656+21554+22609=72819
    assert "72,819" in out
    # Total main: 180+150+160=490
    assert "490" in out
    # Total compression: 72819/490 ≈ 149x
    assert "149x" in out

    # Old metric should NOT appear
    assert "estimated_inline_tokens" not in out
    assert "Saved" not in out


def test_cmd_context_stats_table_and_totals(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Two entries produce a table with correct per-row and TOTAL values."""
    log_file = tmp_path / "context.jsonl"

    records = [
        {
            "ticket_id": "TICK-006",
            "subagent_tokens": 28656,
            "summary_tokens": 180,
            "tool_uses": 15,
            "duration_ms": 84930,
            "timestamp": "2026-06-20T14:23:00Z",
        },
        {
            "ticket_id": "TICK-007",
            "subagent_tokens": 21554,
            "summary_tokens": 150,
            "tool_uses": 10,
            "duration_ms": 60000,
            "timestamp": "2026-06-20T15:00:00Z",
        },
    ]
    _write_records(log_file, records)

    cmd_context_stats(log_file)

    out = capsys.readouterr().out

    assert "TICK-006" in out
    assert "TICK-007" in out
    assert "TOTAL" in out

    assert "28,656" in out
    assert "21,554" in out
    # total work: 50210
    assert "50,210" in out


def test_print_basic_table_with_step_costs_shows_real_numbers_not_dashes(
    capsys: pytest.CaptureFixture,
) -> None:
    """The bug this fixes: with step_costs data available, per-ticket rows
    show real tokens/cost instead of the '—'/0 the dead subagent_tokens
    channel produced for every ticket (TICK-488)."""
    entries = [{"ticket_id": "TICK-480"}, {"ticket_id": "TICK-481"}]
    step_costs = [
        {"ticket_id": "TICK-480", "step": "implement", "input_tokens": 1000, "output_tokens": 200, "cost_usd": 0.05},
        {"ticket_id": "TICK-481", "step": "implement", "input_tokens": 500, "output_tokens": 100, "cost_usd": 0.03},
    ]

    _print_basic_table(entries, step_costs=step_costs)
    out = capsys.readouterr().out

    assert "TICK-480" in out
    assert "TICK-481" in out
    assert "1,200" in out  # TICK-480 total tokens
    assert "$0.05" in out
    assert "TOTAL" in out
    assert "$0.08" in out  # combined total cost
    # the old broken table's columns must not appear
    assert "Work tokens" not in out
    assert "Compression" not in out


# ---------------------------------------------------------------------------
# cmd_context_stats(full=True) — extended panels
# ---------------------------------------------------------------------------


def _three_records(with_batch: bool = False) -> list[dict]:
    batch_id = "batch-001" if with_batch else ""
    return [
        {
            "ticket_id": "TICK-006",
            "subagent_tokens": 28656,
            "summary_tokens": 180,
            "tool_uses": 15,
            "duration_ms": 84930,
            "wall_time_ms": 90000,
            "batch_id": batch_id,
            "executor": "claude-subagent",
            "model": "claude-sonnet-4-6",
            "tests_passed": True,
            "drift_warnings": 0,
            "timestamp": "2026-06-20T14:23:00Z",
        },
        {
            "ticket_id": "TICK-007",
            "subagent_tokens": 21554,
            "summary_tokens": 150,
            "tool_uses": 10,
            "duration_ms": 60000,
            "wall_time_ms": 65000,
            "batch_id": batch_id,
            "executor": "claude-subagent",
            "model": "claude-sonnet-4-6",
            "tests_passed": True,
            "drift_warnings": 0,
            "timestamp": "2026-06-20T15:00:00Z",
        },
        {
            "ticket_id": "TICK-008",
            "subagent_tokens": 22609,
            "summary_tokens": 160,
            "tool_uses": 12,
            "duration_ms": 70000,
            "wall_time_ms": 72000,
            "batch_id": batch_id,
            "executor": "claude-subagent",
            "model": "claude-sonnet-4-6",
            "tests_passed": True,
            "drift_warnings": 0,
            "timestamp": "2026-06-20T16:00:00Z",
        },
    ]


def test_cmd_context_stats_full_all_panels(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """--full includes compression, cost trend, and quality panels."""
    log_file = tmp_path / "context.jsonl"
    _write_records(log_file, _three_records())

    cmd_context_stats(log_file, full=True)

    out = capsys.readouterr().out

    # Panel 1: Compression
    assert "--- Compression ---" in out
    assert "72,819" in out  # total work tokens
    assert "490" in out  # total main-session tokens
    assert "72,329" in out  # kept out (72819-490)

    # Panel 3: Cost Trend
    assert "--- Cost Trend" in out
    assert "TICK-006" in out
    assert "█" in out  # at least one bar character

    # Trend verdict — 3 records is below the 5-ticket minimum, so no RISING/FLAT verdict
    assert "not enough data yet" in out

    # Panel 4: Quality
    assert "--- Quality ---" in out
    assert "3/3" in out
    assert "100%" in out

    # Overall verdict
    assert "Verdict: Delegation is" in out


def test_cmd_context_stats_full_with_batch(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """--full with batch_id data shows Parallelism panel."""
    log_file = tmp_path / "context.jsonl"
    _write_records(log_file, _three_records(with_batch=True))

    cmd_context_stats(log_file, full=True)

    out = capsys.readouterr().out

    assert "--- Parallelism ---" in out
    assert "batch-001" in out
    assert "3 tickets" in out


def test_cmd_context_stats_full_no_token_data_skips_compression(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """--full with all subagent_tokens null skips compression panel gracefully."""
    log_file = tmp_path / "context.jsonl"
    records = [
        {
            "ticket_id": "TICK-A",
            "subagent_tokens": None,
            "summary_tokens": 0,
            "tool_uses": 5,
            "duration_ms": 30000,
            "timestamp": "2026-06-20T12:00:00Z",
        },
    ]
    _write_records(log_file, records)

    cmd_context_stats(log_file, full=True)

    out = capsys.readouterr().out

    # Should NOT crash and should NOT show compression panel
    assert "--- Compression ---" not in out
    # Basic table still renders
    assert "TICK-A" in out


# ---------------------------------------------------------------------------
# cmd_context_stats(compare=True) — executor comparison
# ---------------------------------------------------------------------------


def test_cmd_context_stats_compare_groups_by_executor_model(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """--compare groups entries by executor+model and shows side-by-side."""
    log_file = tmp_path / "context.jsonl"

    records = [
        {
            "ticket_id": "TICK-006",
            "subagent_tokens": 28656,
            "summary_tokens": 180,
            "wall_time_ms": 90000,
            "tool_uses": 15,
            "duration_ms": 84930,
            "executor": "claude-subagent",
            "model": "claude-sonnet-4-6",
            "tests_passed": True,
            "timestamp": "2026-06-20T14:23:00Z",
        },
        {
            "ticket_id": "TICK-007",
            "subagent_tokens": 21554,
            "summary_tokens": 150,
            "wall_time_ms": 65000,
            "tool_uses": 10,
            "duration_ms": 60000,
            "executor": "claude-subagent",
            "model": "claude-sonnet-4-6",
            "tests_passed": True,
            "timestamp": "2026-06-20T15:00:00Z",
        },
        {
            "ticket_id": "TICK-008",
            "subagent_tokens": 19000,
            "summary_tokens": 160,
            "wall_time_ms": 50000,
            "tool_uses": 8,
            "duration_ms": 48000,
            "executor": "aider",
            "model": "gpt-4o",
            "tests_passed": False,
            "timestamp": "2026-06-20T16:00:00Z",
        },
    ]
    _write_records(log_file, records)

    cmd_context_stats(log_file, compare=True)

    out = capsys.readouterr().out

    assert "=== Executor Comparison ===" in out
    assert "claude-subagent" in out
    assert "claude-sonnet-4-6" in out
    assert "aider" in out
    assert "gpt-4o" in out

    # header columns present
    assert "cost" in out
    assert "tokens" in out

    # avg work tok for claude-subagent: (28656+21554)//2 = 25105
    assert "25,105" in out

    # pass rate for aider: 0/1 = 0%
    assert "0%" in out

    # pass rate for claude-subagent: 2/2 = 100%
    assert "100%" in out


def test_cmd_context_stats_compare_with_step_costs(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--compare uses step_costs to show real aggregated cost and token usage."""
    db_path = tmp_path / "analytics.db"
    monkeypatch.setenv("LANEGATE_CONTEXT_LOG_DB", str(db_path))

    log_file = tmp_path / "context.jsonl"
    records = [
        {
            "project": "proj",
            "ticket_id": "TICK-100",
            "subagent_tokens": 10000,
            "summary_tokens": 100,
            "wall_time_ms": 1000,
            "tool_uses": 5,
            "duration_ms": 900,
            "executor": "claude-b",
            "model": "claude-3-5-sonnet",
            "tests_passed": True,
            "timestamp": "2026-06-20T10:00:00Z",
        },
        {
            "project": "proj",
            "ticket_id": "TICK-101",
            "subagent_tokens": 8000,
            "summary_tokens": 100,
            "wall_time_ms": 900,
            "tool_uses": 4,
            "duration_ms": 800,
            "executor": "codex",
            "model": "gpt-5-codex",
            "tests_passed": True,
            "timestamp": "2026-06-20T11:00:00Z",
        },
    ]
    _write_records(log_file, records)

    log_step_cost(
        db_path,
        "proj",
        "TICK-100",
        "implement",
        executor="claude-b",
        model="claude-3-5-sonnet",
        input_tokens=1500,
        output_tokens=300,
        cost_usd=0.05,
    )

    step_costs = _load_step_costs_from_db(db_path)
    cmd_context_stats(log_file, compare=True, step_costs=step_costs)
    out = capsys.readouterr().out

    assert "claude-b" in out
    assert "$0.0500" in out
    assert "1,800" in out


def test_cmd_context_stats_compare_explicit_log_does_not_cross_contaminate_with_db(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit JSONL log comparisons do not query DB or cross-contaminate ticket executors."""
    db_path = tmp_path / "analytics.db"
    monkeypatch.setenv("LANEGATE_CONTEXT_LOG_DB", str(db_path))

    log_file = tmp_path / "context.jsonl"
    records = [
        {
            "project": "proj",
            "ticket_id": "TICK-100",
            "subagent_tokens": 10000,
            "summary_tokens": 100,
            "wall_time_ms": 1000,
            "tool_uses": 5,
            "duration_ms": 900,
            "executor": "claude-b",
            "model": "claude-3-5-sonnet",
            "tests_passed": True,
            "timestamp": "2026-06-20T10:00:00Z",
        },
        {
            "project": "proj",
            "ticket_id": "TICK-101",
            "subagent_tokens": 8000,
            "summary_tokens": 100,
            "wall_time_ms": 900,
            "tool_uses": 4,
            "duration_ms": 800,
            "executor": "codex",
            "model": "gpt-5-codex",
            "tests_passed": True,
            "timestamp": "2026-06-20T11:00:00Z",
        },
    ]
    _write_records(log_file, records)

    # DB has contradictory data for TICK-100
    log_step_cost(
        db_path,
        "proj",
        "TICK-100",
        "implement",
        executor="different-executor",
        model="different-model",
        input_tokens=99999,
        output_tokens=99999,
        cost_usd=99.0,
    )

    # Calling cmd_context_stats with explicit log file and compare=True does NOT load DB
    cmd_context_stats(log_file, compare=True)
    out = capsys.readouterr().out

    assert "claude-b" in out
    assert "codex" in out
    assert "different-executor" not in out


def test_cmd_context_stats_compare_multi_attempt_step_costs_isolated_by_executor(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """When a ticket has multiple step_cost attempts by different executors, costs are strictly partitioned."""
    db_path = tmp_path / "analytics.db"

    log_file = tmp_path / "context.jsonl"
    records = [
        {
            "project": "proj",
            "ticket_id": "TICK-100",
            "subagent_tokens": 10000,
            "summary_tokens": 100,
            "wall_time_ms": 1000,
            "tool_uses": 5,
            "duration_ms": 900,
            "executor": "claude-b",
            "model": "claude-3-5-sonnet",
            "tests_passed": True,
            "timestamp": "2026-06-20T10:00:00Z",
        },
        {
            "project": "proj",
            "ticket_id": "TICK-101",
            "subagent_tokens": 8000,
            "summary_tokens": 100,
            "wall_time_ms": 900,
            "tool_uses": 4,
            "duration_ms": 800,
            "executor": "codex",
            "model": "gpt-5-codex",
            "tests_passed": True,
            "timestamp": "2026-06-20T11:00:00Z",
        },
    ]
    _write_records(log_file, records)

    # TICK-100 has a first attempt by a different executor that failed
    log_step_cost(
        db_path,
        "proj",
        "TICK-100",
        "implement",
        executor="failed-executor",
        model="failed-model",
        input_tokens=5000,
        output_tokens=1000,
        cost_usd=0.10,
    )
    # TICK-100 final attempt by claude-b
    log_step_cost(
        db_path,
        "proj",
        "TICK-100",
        "implement",
        executor="claude-b",
        model="claude-3-5-sonnet",
        input_tokens=1000,
        output_tokens=200,
        cost_usd=0.03,
    )
    # TICK-101 by codex
    log_step_cost(
        db_path,
        "proj",
        "TICK-101",
        "implement",
        executor="codex",
        model="gpt-5-codex",
        input_tokens=2000,
        output_tokens=400,
        cost_usd=0.07,
    )

    step_costs = _load_step_costs_from_db(db_path)
    cmd_context_stats(log_file, compare=True, step_costs=step_costs)
    out = capsys.readouterr().out

    assert "claude-b" in out
    assert "$0.0300" in out
    assert "codex" in out
    assert "$0.0700" in out
    assert "failed-executor" not in out


def test_real_executor_by_ticket_prefers_implement_step() -> None:
    step_costs = [
        {"project": "org/repo", "ticket_id": "TICK-1", "step": "analyze", "executor": "claude-a", "timestamp": "t1"},
        {"project": "org/repo", "ticket_id": "TICK-1", "step": "implement", "executor": "claude-b", "timestamp": "t2"},
        {"project": "org/repo", "ticket_id": "TICK-2", "step": "fix", "executor": "codex", "timestamp": "t1"},
    ]
    result = _real_executor_by_ticket(step_costs)
    assert result == {("org/repo", "TICK-1"): "claude-b", ("org/repo", "TICK-2"): "codex"}


def test_real_executor_by_ticket_ignores_review_and_drift_check_rows() -> None:
    """review/drift_check are dispatched to a deliberately *different*,
    independent executor instance -- a ticket implemented by aider but
    reviewed by claude-a must not be attributed to claude-a (TICK-549
    review round 3 finding)."""
    step_costs = [
        {"project": "org/repo", "ticket_id": "TICK-1", "step": "review", "executor": "claude-a", "timestamp": "t1"},
        {"project": "org/repo", "ticket_id": "TICK-1", "step": "drift_check", "executor": "codex", "timestamp": "t2"},
    ]
    result = _real_executor_by_ticket(step_costs)
    assert result == {}


def test_real_executor_by_ticket_does_not_collide_across_projects() -> None:
    """Ticket ids are per-project sequential and collide across projects --
    keying on ticket_id alone would let one project's real executor bleed
    into another's identically-numbered ticket (TICK-549 review finding)."""
    step_costs = [
        {"project": "org/alpha", "ticket_id": "TICK-100", "step": "implement", "executor": "claude-b", "timestamp": "2026-06-20T10:00:00Z"},
        {"project": "org/beta", "ticket_id": "TICK-100", "step": "implement", "executor": "codex", "timestamp": "2026-07-01T10:00:00Z"},
    ]
    result = _real_executor_by_ticket(step_costs)
    assert result == {("org/alpha", "TICK-100"): "claude-b", ("org/beta", "TICK-100"): "codex"}


def test_print_compare_resolves_executor_from_step_costs(
    capsys: pytest.CaptureFixture,
) -> None:
    """--compare must not trust the analytics table's own (often wrong)
    per-ticket executor guess: TICK-549 found production tickets actually
    driven by claude-a/claude-b logged in the analytics table under the
    project's static default executor (here 'codex') instead. When
    step_costs is available it is authoritative."""
    entries = [
        {
            "ticket_id": "TICK-100",
            "executor": "codex",  # analytics table's wrong per-ticket guess
            "model": "",
            "subagent_tokens": 10000,
            "wall_time_ms": 1000,
            "tests_passed": True,
        },
        {
            "ticket_id": "TICK-101",
            "executor": "codex",
            "model": "gpt-5-codex",
            "subagent_tokens": 8000,
            "wall_time_ms": 900,
            "tests_passed": True,
        },
    ]
    step_costs = [
        {
            "ticket_id": "TICK-100",
            "step": "implement",
            "executor": "claude-b",
            "timestamp": "2026-06-20T10:00:00Z",
        },
    ]

    _print_compare(entries, step_costs=step_costs)
    out = capsys.readouterr().out

    assert "claude-b" in out
    assert "codex" in out
    # TICK-100 must be bucketed under claude-b, not codex: only TICK-101
    # remains in the codex/gpt-5-codex group.
    codex_line = next(line for line in out.splitlines() if line.startswith("codex"))
    assert codex_line.split()[2] == "1"


def test_print_compare_blank_executor_buckets_as_claude(
    capsys: pytest.CaptureFixture,
) -> None:
    """A row whose stored executor is '' or absent (e.g. an _import_legacy'd
    entry, or a partial cmd_log_backfill call) buckets under 'claude', not a
    blank-labeled row -- and with no step_costs data to resolve it from,
    _print_compare's own e.get("executor") or "claude" fallback is what
    catches it (TICK-549)."""
    entries = [
        {"ticket_id": "TICK-1", "executor": "", "model": "", "tests_passed": True},
        {"ticket_id": "TICK-2", "model": "", "tests_passed": True},  # key omitted entirely
        {"ticket_id": "TICK-3", "executor": "codex", "model": "", "tests_passed": True},
    ]

    _print_compare(entries)
    out = capsys.readouterr().out

    lines = out.splitlines()
    sep_idx = next(i for i, line in enumerate(lines) if line.startswith("---"))
    data_lines = [line for line in lines[sep_idx + 1 :] if line.strip()]
    # Exactly two groups: no separate blank-labeled bucket for TICK-1/TICK-2.
    assert {line.split()[0] for line in data_lines} == {"claude", "codex"}
    claude_line = next(line for line in data_lines if line.startswith("claude "))
    assert claude_line.split()[1] == "2"


def test_cmd_context_stats_compare_single_executor(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """--compare with single executor prints 'Only one executor' message."""
    log_file = tmp_path / "context.jsonl"

    records = [
        {
            "ticket_id": "TICK-006",
            "subagent_tokens": 28656,
            "summary_tokens": 180,
            "tool_uses": 15,
            "duration_ms": 84930,
            "executor": "claude-subagent",
            "model": "claude-sonnet-4-6",
            "timestamp": "2026-06-20T14:23:00Z",
        },
        {
            "ticket_id": "TICK-007",
            "subagent_tokens": 21554,
            "summary_tokens": 150,
            "tool_uses": 10,
            "duration_ms": 60000,
            "executor": "claude-subagent",
            "model": "claude-sonnet-4-6",
            "timestamp": "2026-06-20T15:00:00Z",
        },
    ]
    _write_records(log_file, records)

    cmd_context_stats(log_file, compare=True)

    out = capsys.readouterr().out
    assert "Only one executor in log" in out


def test_cmd_context_stats_compare_null_tokens_shows_dash(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """--compare shows '—' for avg work tok when all subagent_tokens are null for an executor."""
    log_file = tmp_path / "context.jsonl"

    records = [
        {
            "ticket_id": "TICK-A",
            "subagent_tokens": None,
            "summary_tokens": 100,
            "wall_time_ms": 30000,
            "tool_uses": 5,
            "duration_ms": 28000,
            "executor": "human",
            "model": "",
            "timestamp": "2026-06-20T12:00:00Z",
        },
        {
            "ticket_id": "TICK-B",
            "subagent_tokens": 15000,
            "summary_tokens": 100,
            "wall_time_ms": 40000,
            "tool_uses": 8,
            "duration_ms": 38000,
            "executor": "claude-subagent",
            "model": "claude-sonnet-4-6",
            "timestamp": "2026-06-20T13:00:00Z",
        },
    ]
    _write_records(log_file, records)

    cmd_context_stats(log_file, compare=True)

    out = capsys.readouterr().out

    # human row should show — for avg work tok
    assert "human" in out
    assert "—" in out


# ---------------------------------------------------------------------------
# _local_day / _print_by_day — real cost grouped by operator-local day
# ---------------------------------------------------------------------------


def test_local_day_converts_utc_to_pacific_crossing_midnight() -> None:
    """A UTC timestamp shortly after UTC midnight is still the *previous*
    Pacific day -- grouping by raw UTC date would misattribute it."""
    # 2026-08-13T05:30:00Z is 2026-08-12T22:30:00 in America/Los_Angeles (PDT, UTC-7).
    assert _local_day("2026-08-13T05:30:00Z") == "2026-08-12"
    # 2026-08-12T20:00:00Z is 2026-08-12T13:00:00 local -- same UTC and local day.
    assert _local_day("2026-08-12T20:00:00Z") == "2026-08-12"


def test_local_day_unparseable_returns_placeholder() -> None:
    assert _local_day("not-a-timestamp") == "?"
    assert _local_day("") == "?"


def test_print_by_day_groups_across_utc_date_boundary(capsys: pytest.CaptureFixture) -> None:
    """Two dispatches on different UTC calendar dates that are the same
    Pacific day must land in one row, not two (the bug this feature fixes)."""
    step_costs = [
        {"ticket_id": "TICK-A", "cost_usd": 1.0, "timestamp": "2026-08-12T20:00:00Z"},
        {"ticket_id": "TICK-B", "cost_usd": 2.0, "timestamp": "2026-08-13T05:30:00Z"},
        {"ticket_id": "TICK-C", "cost_usd": 5.0, "timestamp": "2026-08-13T20:00:00Z"},
    ]

    _print_by_day(step_costs)

    out = capsys.readouterr().out
    assert "=== Real Cost by Day (America/Los_Angeles) ===" in out

    lines = {line.split()[0]: line for line in out.splitlines() if line.split()[:1] and line.split()[0].startswith("2026-")}
    assert "2026-08-12" in lines
    assert "2026-08-13" in lines
    # 08-12 local day got both TICK-A and TICK-B (2 dispatches, $3.00)
    assert lines["2026-08-12"].split()[1] == "2"
    assert "3.00" in lines["2026-08-12"]
    # 08-13 local day got only TICK-C (1 dispatch, $5.00)
    assert lines["2026-08-13"].split()[1] == "1"
    assert "5.00" in lines["2026-08-13"]
    assert "8.00" in out.rsplit("\n", 2)[-2]  # TOTAL row has the grand total


def test_print_by_day_empty_shows_message(capsys: pytest.CaptureFixture) -> None:
    _print_by_day([])
    out = capsys.readouterr().out
    assert "No step-cost data logged yet." in out


# ---------------------------------------------------------------------------
# compute_stats — pure function contract
# ---------------------------------------------------------------------------

_FIXTURE = [
    {
        "ticket_id": "TICK-006",
        "subagent_tokens": 28656,
        "summary_tokens": 180,
        "tool_uses": 15,
        "duration_ms": 84930,
        "wall_time_ms": 90000,
        "batch_id": "batch-001",
        "executor": "claude-subagent",
        "model": "claude-sonnet-4-6",
        "tests_passed": True,
        "drift_warnings": 0,
        "timestamp": "2026-06-20T14:23:00Z",
    },
    {
        "ticket_id": "TICK-007",
        "subagent_tokens": 21554,
        "summary_tokens": 150,
        "tool_uses": 10,
        "duration_ms": 60000,
        "wall_time_ms": 65000,
        "batch_id": "batch-001",
        "executor": "claude-subagent",
        "model": "claude-sonnet-4-6",
        "tests_passed": True,
        "drift_warnings": 0,
        "timestamp": "2026-06-20T15:00:00Z",
    },
]


def test_compute_stats_top_level_keys() -> None:
    """compute_stats returns all required top-level keys."""
    result = compute_stats(_FIXTURE)
    for key in (
        "has_entries",
        "totals",
        "tickets",
        "parallelism",
        "cost_trend",
        "quality",
        "verdict",
    ):
        assert key in result, f"missing key: {key}"


def test_compute_stats_has_entries_true() -> None:
    result = compute_stats(_FIXTURE)
    assert result["has_entries"] is True


def test_compute_stats_totals() -> None:
    result = compute_stats(_FIXTURE)
    t = result["totals"]
    assert t["work_tokens"] == 50210  # 28656 + 21554
    assert t["main_tokens"] == 330  # 180 + 150
    assert t["compression_ratio"] == round(50210 / 330)  # 152
    assert t["kept_out"] == 50210 - 330


def test_compute_stats_per_ticket_rows() -> None:
    result = compute_stats(_FIXTURE)
    tickets = result["tickets"]
    assert len(tickets) == 2
    assert tickets[0]["ticket_id"] == "TICK-006"
    assert tickets[0]["work_tokens"] == 28656
    assert tickets[0]["main_tokens"] == 180
    assert tickets[0]["compression"] == round(28656 / 180)  # 159


def test_compute_stats_parallelism() -> None:
    result = compute_stats(_FIXTURE)
    par = result["parallelism"]
    assert len(par) == 1
    assert par[0]["batch_id"] == "batch-001"
    assert par[0]["tickets"] == 2
    assert par[0]["wall_s"] == 90  # max(90000, 65000) // 1000
    assert par[0]["sum_durations_s"] == 144  # (84930 + 60000) // 1000
    assert par[0]["gain"] is not None


def test_compute_stats_quality() -> None:
    result = compute_stats(_FIXTURE)
    q = result["quality"]
    assert q["tests_passed"] == 2
    assert q["tests_total"] == 2
    assert q["pass_rate"] == 1.0
    assert q["drift_warnings"] == 0


def test_compute_stats_verdict_paying_off() -> None:
    result = compute_stats(_FIXTURE)
    assert result["verdict"]["label"] == "PAYING OFF"
    assert "compression" in result["verdict"]["detail"]


def test_compute_stats_no_work_tokens() -> None:
    """All null subagent_tokens → work_tokens None, compression_ratio None."""
    entries = [{"ticket_id": "TICK-A", "subagent_tokens": None, "summary_tokens": 100}]
    result = compute_stats(entries)
    assert result["totals"]["work_tokens"] is None
    assert result["totals"]["compression_ratio"] is None
    assert result["totals"]["kept_out"] is None


def test_compute_stats_cost_trend_flat() -> None:
    entries = [
        {"ticket_id": f"TICK-{i:03}", "subagent_tokens": 20000, "summary_tokens": 100}
        for i in range(1, 6)
    ]
    result = compute_stats(entries)
    assert result["cost_trend"]["verdict"] == "FLAT"
    assert len(result["cost_trend"]["points"]) == 5


def test_compute_stats_cost_trend_rising() -> None:
    """Rising cost trend detected via positive OLS slope > 10% of mean per step."""
    # Monotonically increasing: slope/mean ≈ 0.33 per step → well above 10% threshold
    entries = [
        {"ticket_id": "TICK-001", "subagent_tokens": 1000, "summary_tokens": 10},
        {"ticket_id": "TICK-002", "subagent_tokens": 2000, "summary_tokens": 10},
        {"ticket_id": "TICK-003", "subagent_tokens": 3000, "summary_tokens": 10},
        {"ticket_id": "TICK-004", "subagent_tokens": 4000, "summary_tokens": 10},
        {"ticket_id": "TICK-005", "subagent_tokens": 5000, "summary_tokens": 10},
    ]
    result = compute_stats(entries)
    assert result["cost_trend"]["verdict"] == "RISING"


def test_compute_stats_cost_trend_falling() -> None:
    """Falling cost trend detected via negative OLS slope < -10% of mean per step."""
    entries = [
        {"ticket_id": "TICK-001", "subagent_tokens": 5000, "summary_tokens": 10},
        {"ticket_id": "TICK-002", "subagent_tokens": 4000, "summary_tokens": 10},
        {"ticket_id": "TICK-003", "subagent_tokens": 3000, "summary_tokens": 10},
        {"ticket_id": "TICK-004", "subagent_tokens": 2000, "summary_tokens": 10},
        {"ticket_id": "TICK-005", "subagent_tokens": 1000, "summary_tokens": 10},
    ]
    result = compute_stats(entries)
    assert result["cost_trend"]["verdict"] == "FALLING"


def test_compute_stats_cost_trend_none_below_minimum() -> None:
    """Trend verdict is None when fewer than 5 tickets have token data."""
    result = compute_stats(_FIXTURE)  # _FIXTURE has 2 entries
    assert result["cost_trend"]["verdict"] is None


# ---------------------------------------------------------------------------
# compute_stats(step_cost_entries=...) — real step_costs grounding (TICK-488)
# ---------------------------------------------------------------------------

_STEP_COST_FIXTURE = [
    {"ticket_id": "TICK-100", "step": "implement", "input_tokens": 1000, "output_tokens": 200, "cost_usd": 0.05},
    {"ticket_id": "TICK-100", "step": "review", "input_tokens": 300, "output_tokens": 50, "cost_usd": 0.02},
    {"ticket_id": "TICK-101", "step": "implement", "input_tokens": 500, "output_tokens": 100, "cost_usd": 0.03},
]


def test_compute_stats_ignores_step_costs_when_not_passed() -> None:
    """Without step_cost_entries, compute_stats is 100% unchanged (legacy path)."""
    result = compute_stats(_FIXTURE)
    assert result["verdict"]["grounded"] is False
    assert result["verdict"]["label"] == "PAYING OFF"
    assert result["tickets"][0]["work_tokens"] == 28656


def test_compute_stats_per_ticket_rollup_from_step_costs() -> None:
    """tickets rolls up real per-ticket token/cost totals from step_costs,
    even though the fixture's own subagent_tokens/summary_tokens are empty --
    the real numbers don't depend on that dead channel at all."""
    entries = [{"ticket_id": "TICK-100"}, {"ticket_id": "TICK-101"}]
    result = compute_stats(entries, step_cost_entries=_STEP_COST_FIXTURE)
    tickets = {t["ticket_id"]: t for t in result["tickets"]}
    assert tickets["TICK-100"]["total_tokens"] == 1000 + 200 + 300 + 50
    assert tickets["TICK-100"]["total_cost_usd"] == 0.07
    assert tickets["TICK-100"]["dispatches"] == 2
    assert tickets["TICK-101"]["total_tokens"] == 500 + 100
    assert tickets["TICK-101"]["total_cost_usd"] == 0.03
    assert tickets["TICK-101"]["dispatches"] == 1


def test_compute_stats_verdict_grounded_in_step_costs_not_fabricated() -> None:
    """When step_cost_entries is given, the verdict is a factual real-cost
    summary -- no NOT WORTH IT/BREAK-EVEN/PAYING OFF label fabricated from a
    comparison baseline that doesn't exist in the data."""
    entries = [{"ticket_id": "TICK-100", "subagent_tokens": None}, {"ticket_id": "TICK-101", "subagent_tokens": None}]
    result = compute_stats(entries, step_cost_entries=_STEP_COST_FIXTURE)
    verdict = result["verdict"]
    assert verdict["grounded"] is True
    assert "label" not in verdict
    assert verdict["total_cost_usd"] == 0.10
    assert verdict["avg_cost_per_ticket_usd"] == 0.05
    assert "$0.10 real cost across 2 tickets" in verdict["detail"]


def test_compute_stats_verdict_not_grounded_when_step_costs_empty() -> None:
    """An empty (not None) step_cost_entries list falls back to the legacy
    path rather than claiming to be grounded in zero real data."""
    result = compute_stats(_FIXTURE, step_cost_entries=[])
    assert result["verdict"]["grounded"] is False


# ---------------------------------------------------------------------------
# stats_json — JSON contract
# ---------------------------------------------------------------------------


def test_stats_json_valid_json() -> None:
    """stats_json output is valid JSON."""
    import json

    out = stats_json(_FIXTURE)
    parsed = json.loads(out)
    assert isinstance(parsed, dict)


def test_stats_json_required_keys() -> None:
    """stats_json includes all top-level keys from the plan contract."""
    import json

    parsed = json.loads(stats_json(_FIXTURE))
    for key in (
        "has_entries",
        "totals",
        "tickets",
        "parallelism",
        "cost_trend",
        "quality",
        "verdict",
    ):
        assert key in parsed


def test_stats_json_totals_values() -> None:
    import json

    parsed = json.loads(stats_json(_FIXTURE))
    assert parsed["totals"]["work_tokens"] == 50210
    assert parsed["totals"]["compression_ratio"] == round(50210 / 330)


# ---------------------------------------------------------------------------
# CLI: analytics --json, alias, --log repeated
# ---------------------------------------------------------------------------


def test_cli_analytics_json_emits_valid_json(tmp_path: Path) -> None:
    """analytics --json prints valid JSON with has_entries: true."""
    import json
    from unittest.mock import patch

    from lanegate import cli

    log_file = tmp_path / "ctx.jsonl"
    _write_records(log_file, _FIXTURE)

    captured = []
    with (
        patch("sys.argv", ["lanegate", "analytics", "--json", "--log", str(log_file)]),
        patch("builtins.print", side_effect=lambda *a, **k: captured.append(a[0] if a else "")),
    ):
        cli.main()

    assert captured, "nothing printed"
    parsed = json.loads(captured[0])
    assert parsed["has_entries"] is True
    assert "totals" in parsed
    assert "verdict" in parsed


def test_cli_analytics_json_empty_log(tmp_path: Path) -> None:
    """analytics --json with missing log prints has_entries: false."""
    import json
    from unittest.mock import patch

    from lanegate import cli

    missing = tmp_path / "nope.jsonl"

    captured = []
    with (
        patch("sys.argv", ["lanegate", "analytics", "--json", "--log", str(missing)]),
        patch("builtins.print", side_effect=lambda *a, **k: captured.append(a[0] if a else "")),
    ):
        cli.main()

    parsed = json.loads(captured[0])
    assert parsed == {"has_entries": False}


def test_cli_analytics_shows_prompt_payload_composition_table(tmp_path: Path) -> None:
    """lgt analytics output includes the per-step Prompt Payload Composition table."""
    from unittest.mock import patch
    from lanegate import cli

    log_file = tmp_path / "ctx.jsonl"
    _write_records(log_file, _FIXTURE)

    captured = []
    with (
        patch("sys.argv", ["lanegate", "analytics", "--log", str(log_file)]),
        patch("builtins.print", side_effect=lambda *a, **k: captured.append(" ".join(str(x) for x in a))),
    ):
        cli.main()

    output = "\n".join(captured)
    assert "Prompt Payload Composition" in output
    assert "% Prompt" in output or "Mean B" in output
    assert "instruction-template" in output


def test_compute_payload_composition_stats_aggregates_metrics(tmp_path: Path) -> None:
    """compute_payload_composition_stats aggregates per-step component metrics."""
    from lanegate.context_log import compute_payload_composition_stats

    ticket = {
        "id": "TICK-101",
        "title": "Test payload composition",
        "touches": ["lanegate/cli.py"],
        "close_criteria": "Done.",
        "_body": "Implement prompt composition.",
    }

    stats = compute_payload_composition_stats(tickets=[ticket], repo_root=tmp_path)
    assert "steps" in stats
    steps = stats["steps"]
    assert "implement" in steps
    assert "analyze" in steps
    assert "review" in steps
    assert "fix" in steps

    impl_step = steps["implement"]
    assert "total_bytes_mean" in impl_step
    assert impl_step["total_bytes_mean"] > 0
    comps = impl_step["components"]
    assert len(comps) > 0
    for c in comps:
        assert "label" in c
        assert "mean_bytes" in c
        assert "median_bytes" in c
        assert "max_bytes" in c
        assert "tokens_est" in c
        assert "pct_of_prompt" in c
        assert "reason" in c


def test_compute_payload_composition_stats_resolves_reviewer_type_and_diff(
    tmp_path: Path,
) -> None:
    """The review step's describe fn must resolve reviewer_type/diff for the
    ticket the same way run_review_agent() does and forward them into
    describe_review_payload() -- otherwise a project configured with
    reviewer: aider is always audited against the tool-capable prompt shape
    (is_non_tool_reviewer(None) is always False) and undercounts the
    inlined GIT DIFF section for exactly the configs TICK-644 targets."""
    from lanegate.context_log import compute_payload_composition_stats

    ticket = {
        "id": "TICK-101",
        "title": "Test payload composition",
        "touches": ["lanegate/cli.py"],
        "close_criteria": "Done.",
        "_body": "Implement prompt composition.",
        "branch": "tick-101",
    }
    cfg = {
        "reviewer": "aider-review",
        "executors": {"aider-review": {"type": "aider"}},
    }
    captured_kwargs = {}
    real_describe = None

    def fake_describe(*args, **kwargs):
        captured_kwargs.update(kwargs)
        from lanegate.reviewer import describe_review_payload

        return describe_review_payload(*args, **kwargs)

    with (
        patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/x.py b/x.py\n+x\n"),
        patch("lanegate.reviewer.describe_review_payload", side_effect=fake_describe),
    ):
        compute_payload_composition_stats(tickets=[ticket], repo_root=tmp_path, cfg=cfg)

    assert captured_kwargs.get("reviewer_type") == "aider"
    assert captured_kwargs.get("diff") == "diff --git a/x.py b/x.py\n+x\n"


def test_compute_payload_composition_stats_resolves_reviewer_via_steps_block(
    tmp_path: Path,
) -> None:
    """resolve_executor() (used by an earlier version of this fix) never
    looks at cfg["steps"][step]["driver"] -- the newer routing block
    run_review_agent() actually resolves through, via resolve_pool_executor
    -> resolve_driver. A project routed with `steps: {review: {driver:
    aider-local-review}}` (exactly the style .lanegate.yml.example
    documents for a fully-local VRAM-tiered split) must be audited against
    that driver's real type, not silently fall back to the global
    executor default."""
    from lanegate.context_log import compute_payload_composition_stats

    ticket = {
        "id": "TICK-102",
        "title": "Test steps-routed reviewer resolution",
        "touches": ["lanegate/cli.py"],
        "close_criteria": "Done.",
        "_body": "Implement.",
        "branch": "tick-102",
    }
    cfg = {
        "executor": "claude",
        "drivers": {"aider-local-review": {"type": "aider"}},
        "steps": {"review": {"driver": "aider-local-review"}},
    }
    captured_kwargs = {}

    def fake_describe(*args, **kwargs):
        captured_kwargs.update(kwargs)
        from lanegate.reviewer import describe_review_payload

        return describe_review_payload(*args, **kwargs)

    with (
        patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/x.py b/x.py\n+x\n"),
        patch("lanegate.reviewer.describe_review_payload", side_effect=fake_describe),
    ):
        compute_payload_composition_stats(tickets=[ticket], repo_root=tmp_path, cfg=cfg)

    assert captured_kwargs.get("reviewer_type") == "aider"


def test_compute_payload_composition_stats_resolves_reviewer_via_pools_block(
    tmp_path: Path,
) -> None:
    """resolve_driver() alone (used by an earlier version of this fix) never
    does pool selection -- it only resolves the *configured* driver name
    (ticket/cfg.reviewer/steps/executor_steps/global executor), falling
    straight to the global `executor:` default and skipping `pools:`
    entirely. A project actually routed through a review pool (this repo's
    own .lanegate.yml has one) must be audited against the pool-selected
    instance's type, not the unrelated global-executor fallback."""
    from lanegate.context_log import compute_payload_composition_stats

    ticket = {
        "id": "TICK-103",
        "title": "Test pool-routed reviewer resolution",
        "touches": ["lanegate/cli.py"],
        "close_criteria": "Done.",
        "_body": "Implement.",
        "branch": "tick-103",
    }
    cfg = {
        "executor": "claude",
        "pools": {"default": {"executors": ["aider-pool-review"]}},
        "default_pool": "default",
        "executors": {"aider-pool-review": {"type": "aider"}},
    }
    captured_kwargs = {}

    def fake_describe(*args, **kwargs):
        captured_kwargs.update(kwargs)
        from lanegate.reviewer import describe_review_payload

        return describe_review_payload(*args, **kwargs)

    with (
        patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/x.py b/x.py\n+x\n"),
        patch("lanegate.reviewer.describe_review_payload", side_effect=fake_describe),
    ):
        compute_payload_composition_stats(tickets=[ticket], repo_root=tmp_path, cfg=cfg)

    assert captured_kwargs.get("reviewer_type") == "aider"


def test_compute_payload_composition_stats_uses_ticket_worktree_field(tmp_path: Path) -> None:
    """_describe_review must check ticket["worktree"] first, matching
    run_review_agent()'s own resolution order (orchestrate/review.py) --
    falling straight to the conventional <worktrees_dir>/<id> layout would
    silently audit the wrong (or a nonexistent) path for any ticket whose
    recorded worktree doesn't match that convention (a custom location, or
    non-default naming)."""
    from lanegate.context_log import compute_payload_composition_stats

    custom_worktree = tmp_path / "somewhere-else" / "custom-dir"
    ticket = {
        "id": "TICK-104",
        "title": "Test ticket.worktree field is honored",
        "touches": ["lanegate/cli.py"],
        "close_criteria": "Done.",
        "_body": "Implement.",
        "branch": "tick-104",
        "worktree": str(custom_worktree),
    }
    captured_paths = []

    def fake_get_worktree_diff(worktree_path, branch, base=None):
        captured_paths.append(Path(worktree_path))
        return "diff --git a/x.py b/x.py\n+x\n"

    with patch("lanegate.reviewer.get_worktree_diff", side_effect=fake_get_worktree_diff):
        compute_payload_composition_stats(tickets=[ticket], repo_root=tmp_path, cfg={})

    assert captured_paths == [custom_worktree]


def test_compute_payload_composition_stats_resolves_trunk_branch_once(tmp_path: Path) -> None:
    """resolve_trunk_branch() is a git subprocess call and resolves to the
    same value for every ticket in a single compute_payload_composition_stats
    call (same repo, same cfg) -- it must be resolved once outside the
    per-ticket loop, not once per ticket, or `lanegate stats`/`summary` on a
    project with hundreds of tickets performs hundreds of redundant git
    invocations for a value that never changes."""
    from lanegate.context_log import compute_payload_composition_stats

    tickets = [
        {
            "id": f"TICK-{i}",
            "title": "t",
            "touches": [],
            "close_criteria": "",
            "_body": "",
            "branch": f"tick-{i}",
        }
        for i in range(5)
    ]
    calls = []

    def fake_resolve_trunk_branch(cfg, repo_root):
        calls.append(1)
        return "main"

    with (
        patch("lanegate.config.resolve_trunk_branch", side_effect=fake_resolve_trunk_branch),
        patch("lanegate.reviewer.get_worktree_diff", side_effect=Exception("no worktree")),
    ):
        compute_payload_composition_stats(tickets=tickets, repo_root=tmp_path, cfg={})

    assert len(calls) == 1


def test_compute_payload_composition_stats_degrades_on_reviewer_resolution_error(
    tmp_path: Path,
) -> None:
    """resolve_reviewer_driver_and_type() raising for one ticket (a
    malformed routing rule, an unresolvable pool/driver reference) must not
    drop that ticket from the "review" step's stats entirely -- it should
    degrade to the tool-capable default (reviewer_type=None), the same
    best-effort contract the diff-fetch path already honors, rather than
    letting the exception propagate up to the outer per-ticket try/except
    that silently discards the whole ticket."""
    from lanegate.context_log import compute_payload_composition_stats

    ticket = {
        "id": "TICK-105",
        "title": "t",
        "touches": [],
        "close_criteria": "",
        "_body": "",
        "branch": "tick-105",
    }

    with (
        patch(
            "lanegate.orchestrate.review.resolve_reviewer_driver_and_type",
            side_effect=RuntimeError("boom"),
        ),
        patch("lanegate.reviewer.get_worktree_diff", side_effect=Exception("no worktree")),
    ):
        stats = compute_payload_composition_stats(tickets=[ticket], repo_root=tmp_path, cfg={})

    assert "review" in stats["steps"]


def test_cli_analytics_json_includes_payload_composition(tmp_path: Path) -> None:
    """lgt analytics --json output includes payload_composition field."""
    import json
    from unittest.mock import patch
    from lanegate import cli

    log_file = tmp_path / "ctx.jsonl"
    _write_records(log_file, _FIXTURE)

    captured = []
    with (
        patch("sys.argv", ["lanegate", "analytics", "--json", "--log", str(log_file)]),
        patch("builtins.print", side_effect=lambda *a, **k: captured.append(a[0] if a else "")),
    ):
        cli.main()

    parsed = json.loads(captured[0])
    assert "payload_composition" in parsed
    assert "steps" in parsed["payload_composition"]


def test_cli_context_stats_alias_still_works(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """context-stats alias produces same text output as before."""
    from unittest.mock import patch

    from lanegate import cli

    log_file = tmp_path / "ctx.jsonl"
    _write_records(log_file, _FIXTURE)

    with patch("sys.argv", ["lanegate", "context-stats", "--log", str(log_file)]):
        cli.main()

    out = capsys.readouterr().out
    assert "TICK-006" in out
    assert "TOTAL" in out


def test_cli_analytics_repeated_log_merges_entries(tmp_path: Path) -> None:
    """--log repeated merges entries from both files into one result."""
    import json
    from unittest.mock import patch

    from lanegate import cli

    log_a = tmp_path / "a.jsonl"
    log_b = tmp_path / "b.jsonl"
    _write_records(log_a, [_FIXTURE[0]])
    _write_records(log_b, [_FIXTURE[1]])

    captured = []
    with (
        patch(
            "sys.argv", ["lanegate", "analytics", "--json", "--log", str(log_a), "--log", str(log_b)]
        ),
        patch("builtins.print", side_effect=lambda *a, **k: captured.append(a[0] if a else "")),
    ):
        cli.main()

    parsed = json.loads(captured[0])
    assert parsed["totals"]["work_tokens"] == 50210  # 28656 + 21554 from both files
    assert len(parsed["tickets"]) == 2


# ---------------------------------------------------------------------------
# SQLite: _upsert_row / _load_entries_from_db
# ---------------------------------------------------------------------------


def test_sqlite_upsert_and_load(tmp_path: Path) -> None:
    """_upsert_row writes a row; _load_entries_from_db reads it back."""
    db = tmp_path / "analytics.db"
    row = {
        "ticket_id": "TICK-001",
        "executor": "claude-subagent",
        "model": "claude-sonnet-4-6",
        "subagent_tokens": 12000,
        "summary_tokens": 200,
        "tool_uses": 8,
        "duration_ms": 45000,
        "wall_time_ms": 50000,
        "tests_passed": True,
        "drift_warnings": 0,
        "parallel_peers": None,
        "batch_id": "",
        "timestamp": "2026-06-20T12:00:00Z",
    }
    _upsert_row(db, "org/repo", row)
    entries = _load_entries_from_db(db, project="org/repo")

    assert len(entries) == 1
    e = entries[0]
    assert e["ticket_id"] == "TICK-001"
    assert e["subagent_tokens"] == 12000
    assert e["tests_passed"] is True
    assert e["project"] == "org/repo"


def test_sqlite_upsert_missing_executor_defaults_to_claude(tmp_path: Path) -> None:
    """A row with no 'executor' key (or an explicitly blank one) is stored as
    'claude', matching log_agent_run's own default and _print_compare's
    grouping default, instead of a bare empty string (TICK-549)."""
    db = tmp_path / "analytics.db"
    _upsert_row(db, "org/repo", {"ticket_id": "TICK-001", "timestamp": "2026-06-20T10:00:00Z"})
    _upsert_row(
        db,
        "org/repo",
        {"ticket_id": "TICK-002", "executor": "", "timestamp": "2026-06-20T10:00:00Z"},
    )

    entries = {e["ticket_id"]: e for e in _load_entries_from_db(db, project="org/repo")}
    assert entries["TICK-001"]["executor"] == "claude"
    assert entries["TICK-002"]["executor"] == "claude"


def test_sqlite_upsert_replaces_on_same_pk(tmp_path: Path) -> None:
    """Second upsert with same (project, ticket_id) replaces the first row."""
    db = tmp_path / "analytics.db"
    base = {
        "ticket_id": "TICK-001",
        "executor": "human",
        "subagent_tokens": None,
        "summary_tokens": 0,
        "tool_uses": 0,
        "duration_ms": 0,
        "wall_time_ms": 0,
        "tests_passed": None,
        "drift_warnings": 0,
        "timestamp": "2026-06-20T10:00:00Z",
    }
    _upsert_row(db, "org/repo", base)

    updated = {
        **base,
        "executor": "claude-subagent",
        "subagent_tokens": 30000,
        "tests_passed": True,
    }
    _upsert_row(db, "org/repo", updated)

    entries = _load_entries_from_db(db, project="org/repo")
    assert len(entries) == 1
    assert entries[0]["executor"] == "claude-subagent"
    assert entries[0]["subagent_tokens"] == 30000


def test_sqlite_project_filter(tmp_path: Path) -> None:
    """_load_entries_from_db(project=X) returns only rows for that project."""
    db = tmp_path / "analytics.db"
    _upsert_row(db, "org/alpha", {"ticket_id": "TICK-001", "timestamp": "2026-06-20T10:00:00Z"})
    _upsert_row(db, "org/beta", {"ticket_id": "TICK-002", "timestamp": "2026-06-20T10:00:00Z"})

    alpha = _load_entries_from_db(db, project="org/alpha")
    assert len(alpha) == 1
    assert alpha[0]["ticket_id"] == "TICK-001"

    all_rows = _load_entries_from_db(db, project=None)
    assert len(all_rows) == 2


def test_sqlite_tests_passed_roundtrip(tmp_path: Path) -> None:
    """True/False/None tests_passed survive the int→bool SQLite roundtrip."""
    db = tmp_path / "analytics.db"
    for tid, tp in [("TICK-T", True), ("TICK-F", False), ("TICK-N", None)]:
        _upsert_row(
            db, "p", {"ticket_id": tid, "tests_passed": tp, "timestamp": "2026-06-20T10:00:00Z"}
        )
    rows = {e["ticket_id"]: e for e in _load_entries_from_db(db)}
    assert rows["TICK-T"]["tests_passed"] is True
    assert rows["TICK-F"]["tests_passed"] is False
    assert rows["TICK-N"]["tests_passed"] is None


def test_sqlite_parallel_peers_roundtrip(tmp_path: Path) -> None:
    """parallel_peers list is serialised/deserialised correctly."""
    db = tmp_path / "analytics.db"
    _upsert_row(
        db,
        "p",
        {
            "ticket_id": "TICK-001",
            "parallel_peers": ["TICK-002", "TICK-003"],
            "timestamp": "2026-06-20T10:00:00Z",
        },
    )
    e = _load_entries_from_db(db)[0]
    assert e["parallel_peers"] == ["TICK-002", "TICK-003"]


# ---------------------------------------------------------------------------
# SQLite: legacy import
# ---------------------------------------------------------------------------


def test_legacy_import_imports_jsonl(tmp_path: Path) -> None:
    """_import_legacy reads JSONL and writes rows into the DB."""
    db = tmp_path / "analytics.db"
    jsonl = tmp_path / "lanegate-context-log.jsonl"
    _write_records(jsonl, _FIXTURE)

    n = _import_legacy(db, jsonl, "org/repo")

    assert n == 2
    entries = _load_entries_from_db(db, project="org/repo")
    assert len(entries) == 2
    assert {e["ticket_id"] for e in entries} == {"TICK-006", "TICK-007"}


def test_legacy_import_marks_done(tmp_path: Path) -> None:
    """_import_legacy sets the sentinel so the import is not repeated."""
    db = tmp_path / "analytics.db"
    jsonl = tmp_path / "log.jsonl"
    _write_records(jsonl, [_FIXTURE[0]])

    assert not _is_legacy_imported(db, "org/repo")
    _import_legacy(db, jsonl, "org/repo")
    assert _is_legacy_imported(db, "org/repo")


def test_legacy_import_missing_jsonl_returns_zero(tmp_path: Path) -> None:
    """_import_legacy on a missing file returns 0 and does not crash."""
    db = tmp_path / "analytics.db"
    n = _import_legacy(db, tmp_path / "missing.jsonl", "org/repo")
    assert n == 0


# ---------------------------------------------------------------------------
# SQLite: log_agent_run with db_path
# ---------------------------------------------------------------------------


def test_log_agent_run_writes_sqlite_when_db_path_given(tmp_path: Path) -> None:
    """log_agent_run writes to SQLite when db_path is provided."""
    db = tmp_path / "analytics.db"
    log_file = tmp_path / "ctx.jsonl"

    log_agent_run(
        log_path=log_file,
        ticket_id="TICK-001",
        subagent_tokens=5000,
        tool_uses=10,
        duration_ms=30000,
        touched_files=[],
        repo_root=tmp_path,
        executor="claude-subagent",
        model="claude-sonnet-4-6",
        tests_passed=True,
        db_path=db,
    )

    entries = _load_entries_from_db(db)
    assert len(entries) == 1
    assert entries[0]["ticket_id"] == "TICK-001"
    assert entries[0]["subagent_tokens"] == 5000
    assert entries[0]["tests_passed"] is True
    # JSONL still written too
    assert log_file.exists()


def test_log_agent_run_does_not_clobber_backfilled_tokens(tmp_path: Path) -> None:
    """A later log_agent_run call with subagent_tokens=None (e.g. the merge-time
    auto-log fallback) must not erase real numbers a `lanegate log` backfill
    already wrote for this ticket."""
    db = tmp_path / "analytics.db"
    log_file = tmp_path / "ctx.jsonl"

    cmd_log_backfill(
        "TICK-001",
        tmp_path,
        db_path=db,
        subagent_tokens=28000,
        summary_tokens=400,
        tests_passed=True,
    )

    log_agent_run(
        log_path=log_file,
        ticket_id="TICK-001",
        subagent_tokens=None,
        tool_uses=0,
        duration_ms=0,
        touched_files=[],
        repo_root=tmp_path,
        executor="claude",
        tests_passed=None,
        db_path=db,
    )

    entries = _load_entries_from_db(db)
    assert len(entries) == 1
    assert entries[0]["subagent_tokens"] == 28000
    assert entries[0]["summary_tokens"] == 400
    assert entries[0]["tests_passed"] is True


def test_log_agent_run_no_sqlite_without_db_path(tmp_path: Path) -> None:
    """log_agent_run without db_path does NOT create a DB file."""
    db = tmp_path / "should_not_exist.db"
    log_file = tmp_path / "ctx.jsonl"

    log_agent_run(
        log_path=log_file,
        ticket_id="TICK-002",
        subagent_tokens=1000,
        tool_uses=3,
        duration_ms=5000,
        touched_files=[],
        repo_root=tmp_path,
    )

    assert not db.exists()
    assert log_file.exists()


# ---------------------------------------------------------------------------
# SQLite: cmd_log_backfill
# ---------------------------------------------------------------------------


def test_cmd_log_backfill_creates_row(tmp_path: Path) -> None:
    """cmd_log_backfill inserts a new row when none exists."""
    db = tmp_path / "analytics.db"

    cmd_log_backfill(
        "TICK-001",
        tmp_path,
        db_path=db,
        subagent_tokens=34000,
        summary_tokens=210,
        executor="claude-subagent",
        model="claude-sonnet-4-6",
        tests_passed=True,
    )

    entries = _load_entries_from_db(db)
    assert len(entries) == 1
    e = entries[0]
    assert e["ticket_id"] == "TICK-001"
    assert e["subagent_tokens"] == 34000
    assert e["summary_tokens"] == 210
    assert e["executor"] == "claude-subagent"
    assert e["tests_passed"] is True


def test_cmd_log_backfill_merges_into_existing_row(tmp_path: Path) -> None:
    """cmd_log_backfill updates only the provided fields, preserving others."""
    db = tmp_path / "analytics.db"

    # Seed a row from merge (human, no tokens)
    _upsert_row(
        db,
        tmp_path.name,
        {
            "ticket_id": "TICK-001",
            "executor": "human",
            "subagent_tokens": None,
            "summary_tokens": 0,
            "timestamp": "2026-06-20T10:00:00Z",
        },
    )

    # Backfill token data
    cmd_log_backfill(
        "TICK-001",
        tmp_path,
        db_path=db,
        subagent_tokens=28000,
        executor="claude-subagent",
    )

    entries = _load_entries_from_db(db)
    assert len(entries) == 1
    assert entries[0]["subagent_tokens"] == 28000
    assert entries[0]["executor"] == "claude-subagent"


# ---------------------------------------------------------------------------
# load_entries_for_analytics: legacy import integration
# ---------------------------------------------------------------------------


def test_load_entries_for_analytics_imports_legacy_on_first_run(tmp_path: Path) -> None:
    """load_entries_for_analytics triggers legacy import when DB is empty."""
    db = tmp_path / "analytics.db"
    jsonl = tmp_path / "lanegate-context-log.jsonl"
    _write_records(jsonl, _FIXTURE)

    entries, show_project = load_entries_for_analytics(
        tmp_path,
        db_path=db,
    )

    assert not show_project
    assert len(entries) == 2
    assert _is_legacy_imported(db, tmp_path.name)


def test_load_entries_for_analytics_all_projects(tmp_path: Path) -> None:
    """--all-projects returns entries across projects and sets show_project=True."""
    db = tmp_path / "analytics.db"
    _upsert_row(db, "org/alpha", {"ticket_id": "TICK-001", "timestamp": "2026-06-20T10:00:00Z"})
    _upsert_row(db, "org/beta", {"ticket_id": "TICK-002", "timestamp": "2026-06-20T10:00:00Z"})

    entries, show_project = load_entries_for_analytics(
        tmp_path,
        all_projects=True,
        db_path=db,
    )

    assert show_project is True
    assert len(entries) == 2


def test_load_entries_for_analytics_respects_log_flag(tmp_path: Path) -> None:
    """When jsonl_paths is provided, reads JSONL directly (backward compat)."""
    jsonl = tmp_path / "log.jsonl"
    _write_records(jsonl, [_FIXTURE[0]])

    entries, show_project = load_entries_for_analytics(
        tmp_path,
        jsonl_paths=[jsonl],
    )

    assert not show_project
    assert len(entries) == 1
    assert entries[0]["ticket_id"] == "TICK-006"


# ---------------------------------------------------------------------------
# _get_touched_files helper
# ---------------------------------------------------------------------------


def test_get_touched_files_returns_list_from_git(tmp_path: Path) -> None:
    """_get_touched_files parses git diff --name-only output into a list."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "lanegate/lifecycle.py\nlanegate/context_log.py\ntests/test_context_log.py\n"

    with patch("subprocess.run", return_value=mock_result) as mock_run:
        files = _get_touched_files(tmp_path, "tick-083", trunk_branch="main")

    mock_run.assert_called_once_with(
        ["git", "diff", "--name-only", "main...tick-083"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
    )
    assert files == ["lanegate/lifecycle.py", "lanegate/context_log.py", "tests/test_context_log.py"]


def test_get_touched_files_returns_empty_on_git_failure(tmp_path: Path) -> None:
    """_get_touched_files returns [] when git exits non-zero."""
    mock_result = MagicMock()
    mock_result.returncode = 128
    mock_result.stdout = ""
    mock_result.stderr = "fatal: not a git repo"

    with patch("subprocess.run", return_value=mock_result):
        files = _get_touched_files(tmp_path, "tick-083")

    assert files == []


def test_get_touched_files_returns_empty_when_no_changes(tmp_path: Path) -> None:
    """_get_touched_files returns [] when git produces empty output (no diff)."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "\n"

    with patch("subprocess.run", return_value=mock_result):
        files = _get_touched_files(tmp_path, "tick-083")

    assert files == []


def test_get_touched_files_filters_blank_lines(tmp_path: Path) -> None:
    """_get_touched_files strips blank lines from git output."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "foo.py\n\nbar.py\n"

    with patch("subprocess.run", return_value=mock_result):
        files = _get_touched_files(tmp_path, "main")

    assert files == ["foo.py", "bar.py"]


# ---------------------------------------------------------------------------
# _get_branch_wall_time_ms helper
# ---------------------------------------------------------------------------


def test_get_branch_wall_time_ms_returns_ms_from_oldest_commit(tmp_path: Path) -> None:
    """_get_branch_wall_time_ms computes ms from oldest commit timestamp to now."""
    import time

    # Simulate two commits: newest first (git log order), oldest last
    now = time.time()
    oldest_ts = now - 3600  # 1 hour ago
    newest_ts = now - 1800  # 30 minutes ago

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = f"{int(newest_ts)}\n{int(oldest_ts)}\n"

    with (
        patch("subprocess.run", return_value=mock_result),
        patch("lanegate.lifecycle.time") as mock_time,
    ):
        mock_time.time.return_value = now
        ms = _get_branch_wall_time_ms(tmp_path)

    # Should be approx 3600000 ms (within 1000 ms for integer rounding)
    assert abs(ms - 3600000) < 1000


def test_get_branch_wall_time_ms_returns_zero_on_git_failure(tmp_path: Path) -> None:
    """_get_branch_wall_time_ms returns 0 when git exits non-zero."""
    mock_result = MagicMock()
    mock_result.returncode = 128
    mock_result.stdout = ""
    mock_result.stderr = "fatal: not a git repo"

    with patch("subprocess.run", return_value=mock_result):
        ms = _get_branch_wall_time_ms(tmp_path)

    assert ms == 0


def test_get_branch_wall_time_ms_returns_zero_when_no_commits(tmp_path: Path) -> None:
    """_get_branch_wall_time_ms returns 0 when git log output is empty (branch has no commits)."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""

    with patch("subprocess.run", return_value=mock_result):
        ms = _get_branch_wall_time_ms(tmp_path)

    assert ms == 0


def test_get_branch_wall_time_ms_returns_zero_on_invalid_timestamp(tmp_path: Path) -> None:
    """_get_branch_wall_time_ms returns 0 when git outputs non-integer data."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "not-a-timestamp\n"

    with patch("subprocess.run", return_value=mock_result):
        ms = _get_branch_wall_time_ms(tmp_path)

    assert ms == 0


def test_get_branch_wall_time_ms_single_commit(tmp_path: Path) -> None:
    """_get_branch_wall_time_ms handles a branch with exactly one commit."""
    import time

    now = time.time()
    commit_ts = now - 7200  # 2 hours ago

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = f"{int(commit_ts)}\n"

    with (
        patch("subprocess.run", return_value=mock_result),
        patch("lanegate.lifecycle.time") as mock_time,
    ):
        mock_time.time.return_value = now
        ms = _get_branch_wall_time_ms(tmp_path)

    assert abs(ms - 7200000) < 1000


# ---------------------------------------------------------------------------
# parse_claude_json_result (executor.py) -- lives here since these tests are
# about what context_log/step_costs consume, not executor dispatch mechanics.
# ---------------------------------------------------------------------------


def test_parse_claude_json_result_valid_envelope() -> None:
    stdout = json.dumps(
        {
            "type": "result",
            "result": "pong",
            "total_cost_usd": 0.0841,
            "duration_ms": 2720,
            "num_turns": 1,
            "usage": {
                "input_tokens": 2,
                "output_tokens": 4,
                "cache_creation_input_tokens": 13227,
                "cache_read_input_tokens": 15806,
            },
            "is_error": False,
            "session_id": "abc-123",
        }
    )
    parsed = parse_claude_json_result(stdout)
    assert parsed == {
        "result_text": "pong",
        "cost_usd": 0.0841,
        "duration_ms": 2720,
        "num_turns": 1,
        "input_tokens": 2,
        "output_tokens": 4,
        "cache_creation_tokens": 13227,
        "cache_read_tokens": 15806,
        "is_error": False,
        "session_id": "abc-123",
    }


def test_parse_claude_json_result_plain_text_returns_none() -> None:
    """A non-Claude executor (or an older CLI without --output-format json) just
    returns plain text -- must not raise, must signal 'no cost data' via None."""
    assert parse_claude_json_result("APPROVE\n\nLooks good.") is None


def test_parse_claude_json_result_json_without_result_key_returns_none() -> None:
    """Some other JSON blob (e.g. an embedded verdict object) isn't the outer
    Claude envelope and must not be mistaken for one."""
    assert parse_claude_json_result(json.dumps({"verdict": "approve"})) is None


def test_parse_claude_json_result_missing_usage_defaults_to_none_fields() -> None:
    parsed = parse_claude_json_result(json.dumps({"type": "result", "result": "ok"}))
    assert parsed["result_text"] == "ok"
    assert parsed["input_tokens"] is None
    assert parsed["cost_usd"] is None


# ---------------------------------------------------------------------------
# parse_codex_json_result -- Codex's `exec --json` is JSONL, not a single
# envelope, and reports no dollar cost (token counts only).
# ---------------------------------------------------------------------------

# Real sample from `codex exec --json "Reply with only the single word: pong"`.
_CODEX_PONG_JSONL = "\n".join(
    [
        json.dumps({"type": "thread.started", "thread_id": "019fb199-3ce6-75b0-8217-d6963f4c6318"}),
        json.dumps({"type": "turn.started"}),
        json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "item_0", "type": "agent_message", "text": "pong"},
            }
        ),
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 13142,
                    "cached_input_tokens": 0,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 5,
                    "reasoning_output_tokens": 0,
                },
            }
        ),
    ]
)


def test_parse_codex_json_result_real_sample() -> None:
    parsed = parse_codex_json_result(_CODEX_PONG_JSONL)
    assert parsed["result_text"] == "pong"
    assert parsed["input_tokens"] == 13142  # already uncached: cached_input_tokens is 0 here
    assert parsed["output_tokens"] == 5  # 5 output + 0 reasoning
    assert parsed["cache_read_tokens"] == 0
    assert parsed["cache_creation_tokens"] == 0
    assert parsed["num_turns"] == 1
    # Codex reports no dollar figure itself; lanegate estimates one from the
    # normalized token counts so the step is not silently free in analytics.
    assert parsed["cost_usd"] is not None
    assert parsed["cost_usd"] > 0


def test_parse_codex_json_result_sums_reasoning_into_output_tokens() -> None:
    jsonl = "\n".join(
        [
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}}),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 10, "output_tokens": 5, "reasoning_output_tokens": 20},
                }
            ),
        ]
    )
    parsed = parse_codex_json_result(jsonl)
    assert parsed["output_tokens"] == 25


def test_parse_codex_json_result_concatenates_multiple_agent_messages() -> None:
    jsonl = "\n".join(
        [
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "first"}}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "second"}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}),
        ]
    )
    parsed = parse_codex_json_result(jsonl)
    assert parsed["result_text"] == "first\nsecond"


def test_parse_codex_json_result_no_turn_completed_returns_none() -> None:
    """Missing the terminal event (truncated capture, non-Codex stdout) => None."""
    jsonl = json.dumps({"type": "thread.started", "thread_id": "abc"})
    assert parse_codex_json_result(jsonl) is None


def test_parse_codex_json_result_plain_text_returns_none() -> None:
    assert parse_codex_json_result("not json at all") is None


def test_parse_codex_json_result_ignores_malformed_lines() -> None:
    """One corrupt line in the stream shouldn't blow up parsing of the rest."""
    jsonl = "\n".join(
        [
            "{not valid json",
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}),
        ]
    )
    parsed = parse_codex_json_result(jsonl)
    assert parsed["result_text"] == "ok"


# ---------------------------------------------------------------------------
# parse_agy_json_result -- Antigravity CLI (agy), Google's successor to the
# deprecated Gemini CLI. Single JSON envelope like Claude, but no dollar cost.
# ---------------------------------------------------------------------------


def test_parse_agy_json_result_valid_envelope() -> None:
    stdout = json.dumps(
        {
            "conversation_id": "conv-123",
            "status": "SUCCESS",
            "response": "pong",
            "duration_seconds": 2.72,
            "num_turns": 1,
            "usage": {
                "input_tokens": 24939,
                "output_tokens": 20,
                "thinking_tokens": 154,
                "cache_read_tokens": 21263,
                "total_tokens": 25113,
            },
        }
    )
    parsed = parse_agy_json_result(stdout)
    assert parsed == {
        "result_text": "pong",
        "cost_usd": None,
        "duration_ms": 2720,
        "num_turns": 1,
        "input_tokens": 24939,
        "output_tokens": 174,  # 20 output + 154 thinking
        "cache_creation_tokens": None,
        "cache_read_tokens": 21263,
        "is_error": False,
        "session_id": "conv-123",
    }


def test_parse_agy_json_result_error_status_sets_is_error() -> None:
    stdout = json.dumps(
        {
            "conversation_id": "conv-456",
            "status": "ERROR",
            "response": "",
            "error": "RESOURCE_EXHAUSTED: quota exceeded",
        }
    )
    parsed = parse_agy_json_result(stdout)
    assert parsed["is_error"] is True


def test_parse_agy_json_result_plain_text_returns_none() -> None:
    """A non-agy executor (or an agy version without --output-format json)
    just returns plain text -- must not raise, must signal 'no cost data'."""
    assert parse_agy_json_result("pong") is None


def test_parse_agy_json_result_json_without_status_key_returns_none() -> None:
    """Some other JSON blob isn't the agy envelope and must not be mistaken for one."""
    assert parse_agy_json_result(json.dumps({"verdict": "approve"})) is None


def test_parse_agy_json_result_missing_usage_defaults_to_none_fields() -> None:
    parsed = parse_agy_json_result(json.dumps({"status": "SUCCESS", "response": "ok"}))
    assert parsed["result_text"] == "ok"
    assert parsed["input_tokens"] is None
    assert parsed["output_tokens"] is None
    assert parsed["cost_usd"] is None


# ---------------------------------------------------------------------------
# parse_structured_result -- the executor-type-keyed dispatch every call site
# (invoke_executor, review.py, autofix.py) uses instead of hardcoding
# per-executor branches.
# ---------------------------------------------------------------------------


def test_parse_structured_result_dispatches_to_claude_parser() -> None:
    stdout = json.dumps({"type": "result", "result": "pong", "usage": {}})
    parsed = parse_structured_result("claude-process", stdout)
    assert parsed["result_text"] == "pong"


def test_parse_structured_result_dispatches_to_codex_parser() -> None:
    parsed = parse_structured_result("codex", _CODEX_PONG_JSONL)
    assert parsed["result_text"] == "pong"


def test_parse_structured_result_unknown_executor_type_returns_none() -> None:
    """Types with no registered parser (aider, ollama, openhands, or anything
    not yet wired up) must degrade to 'no cost data', never raise."""
    assert parse_structured_result("aider", "some aider output") is None
    assert parse_structured_result("ollama", "some ollama output") is None
    assert parse_structured_result("gemini", "not registered yet") is None


# ---------------------------------------------------------------------------
# step_costs table: log_step_cost / _load_step_costs_from_db / compute_step_cost_stats
# ---------------------------------------------------------------------------


def test_log_step_cost_writes_and_loads(tmp_path: Path) -> None:
    db = tmp_path / "analytics.db"
    log_step_cost(
        db,
        "sudheerdvn/lanegate-dev",
        "TICK-500",
        "implement",
        executor="claude-a",
        model="claude-sonnet-5",
        input_tokens=2,
        output_tokens=4,
        cache_creation_tokens=13227,
        cache_read_tokens=15806,
        cost_usd=0.0841,
        duration_ms=2720,
        num_turns=1,
    )
    rows = _load_step_costs_from_db(db, project="sudheerdvn/lanegate-dev")
    assert len(rows) == 1
    assert rows[0]["ticket_id"] == "TICK-500"
    assert rows[0]["step"] == "implement"
    assert rows[0]["cost_usd"] == 0.0841
    assert rows[0]["cache_creation_tokens"] == 13227


def test_log_step_cost_appends_does_not_overwrite(tmp_path: Path) -> None:
    """Unlike the analytics table's upsert-by-ticket, step_costs keeps every
    dispatch (analyze + implement + a retried review all land as separate
    rows for the same ticket) instead of the last one clobbering the rest."""
    db = tmp_path / "analytics.db"
    log_step_cost(db, "proj", "TICK-1", "analyze", cost_usd=0.01)
    log_step_cost(db, "proj", "TICK-1", "implement", cost_usd=0.05)
    log_step_cost(db, "proj", "TICK-1", "review", cost_usd=0.02)

    rows = _load_step_costs_from_db(db, project="proj")
    assert len(rows) == 3
    assert {r["step"] for r in rows} == {"analyze", "implement", "review"}


def test_get_ticket_executor_prefers_implement_step(tmp_path: Path) -> None:
    """get_ticket_executor() picks the ticket's real implement-step executor
    over an earlier analyze-step dispatch that used a different one."""
    db = tmp_path / "analytics.db"
    log_step_cost(db, "proj", "TICK-1", "analyze", executor="claude-a", cost_usd=0.01)
    log_step_cost(db, "proj", "TICK-1", "implement", executor="claude-b", cost_usd=0.05)

    assert get_ticket_executor(db, "proj", "TICK-1") == "claude-b"


def test_get_ticket_executor_falls_back_to_latest_fix_row(tmp_path: Path) -> None:
    """With no implement-step row, the most recent *fix* row wins -- fix is
    implementer-owned work (resolve_driver routes it the same way as
    implement), unlike review/drift_check."""
    db = tmp_path / "analytics.db"
    log_step_cost(
        db, "proj", "TICK-1", "fix", executor="agy", timestamp="2026-06-20T10:00:00Z"
    )
    log_step_cost(
        db, "proj", "TICK-1", "fix", executor="claude-b", timestamp="2026-06-20T11:00:00Z"
    )

    assert get_ticket_executor(db, "proj", "TICK-1") == "claude-b"


def test_get_ticket_executor_ignores_review_and_drift_check_rows(tmp_path: Path) -> None:
    """review and drift_check are dispatched to a deliberately *different*,
    independent executor instance (resolve_driver) -- a ticket implemented
    by aider but reviewed by claude-a must not be attributed to claude-a."""
    db = tmp_path / "analytics.db"
    log_step_cost(
        db, "proj", "TICK-1", "review", executor="claude-a", timestamp="2026-06-20T10:00:00Z"
    )
    log_step_cost(
        db, "proj", "TICK-1", "drift_check", executor="codex", timestamp="2026-06-20T11:00:00Z"
    )

    assert get_ticket_executor(db, "proj", "TICK-1") is None


def test_get_ticket_executor_none_when_no_rows(tmp_path: Path) -> None:
    db = tmp_path / "analytics.db"
    log_step_cost(db, "proj", "TICK-1", "implement", executor="claude", cost_usd=0.01)

    assert get_ticket_executor(db, "proj", "TICK-2") is None
    assert get_ticket_executor(tmp_path / "nope.db", "proj", "TICK-1") is None


def test_load_step_costs_from_db_missing_file_returns_empty(tmp_path: Path) -> None:
    assert _load_step_costs_from_db(tmp_path / "nope.db") == []


def test_load_step_costs_from_db_migrates_older_schema(tmp_path: Path) -> None:
    """A DB created before step_costs existed (only analytics/sessions tables)
    must not raise 'no such table' -- _load_step_costs_from_db has to migrate
    it in first, the same way `touched_files` upgrades older analytics rows."""
    import sqlite3

    db = tmp_path / "analytics.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE analytics (project TEXT, ticket_id TEXT)")
    conn.commit()
    conn.close()

    assert _load_step_costs_from_db(db, project="proj") == []


def test_compute_step_cost_stats_aggregates_per_step() -> None:
    entries = [
        {"step": "implement", "cost_usd": 0.05, "input_tokens": 10, "output_tokens": 20},
        {"step": "implement", "cost_usd": 0.07, "input_tokens": 12, "output_tokens": 24},
        {"step": "review", "cost_usd": 0.02, "input_tokens": 5, "output_tokens": 8},
    ]
    stats = compute_step_cost_stats(entries)
    by_step = {s["step"]: s for s in stats["steps"]}
    assert by_step["implement"]["count"] == 2
    assert by_step["implement"]["total_cost_usd"] == 0.12
    assert by_step["implement"]["avg_input_tokens"] == 11
    assert by_step["review"]["count"] == 1
    assert stats["total_cost_usd"] == 0.14
    assert stats["total_dispatches"] == 3


def test_compute_step_cost_stats_empty() -> None:
    stats = compute_step_cost_stats([])
    assert stats["steps"] == []
    assert stats["total_cost_usd"] is None
    assert stats["total_dispatches"] == 0


def test_compute_step_cost_stats_balanced_across_claude_and_codex_rows() -> None:
    """A Claude row (input_tokens already uncached) and a normalized Codex row
    (parse_codex_json_result output -- input_tokens is uncached, cost_usd
    estimated) must average together to a sane figure instead of the old bug
    where Codex's raw cumulative-including-cache input_tokens dwarfed
    Claude's uncached-only figure by orders of magnitude, and Codex's
    cost_usd=0 made "total cost" claude-only while "avg tokens" was
    codex-dominated."""
    claude_row = {
        "step": "implement", "executor": "claude-a",
        "cost_usd": 0.08, "input_tokens": 2, "output_tokens": 4,
    }
    codex_parsed = parse_codex_json_result(_CODEX_PONG_JSONL)
    codex_row = {
        "step": "implement", "executor": "codex",
        "cost_usd": codex_parsed["cost_usd"],
        "input_tokens": codex_parsed["input_tokens"],
        "output_tokens": codex_parsed["output_tokens"],
    }
    stats = compute_step_cost_stats([claude_row, codex_row])
    implement = stats["steps"][0]
    # Both rows contribute real, non-None cost -- no more "claude-only total".
    assert implement["total_cost_usd"] == round(0.08 + codex_parsed["cost_usd"], 4)
    # Neither uncached-input figure is orders of magnitude off the other --
    # both are single-dispatch token counts, not one cumulative-across-turns.
    assert implement["avg_input_tokens"] == round((2 + codex_parsed["input_tokens"]) / 2)
    assert implement["avg_input_tokens"] < 100_000


def test_compute_step_cost_stats_null_costs_excluded_from_average() -> None:
    """A row with no cost_usd (e.g. a non-Claude executor step) must not turn
    the average into 0 or crash -- it's just excluded from the cost math."""
    entries = [
        {"step": "implement", "cost_usd": None, "input_tokens": None},
        {"step": "implement", "cost_usd": 0.10, "input_tokens": 30},
    ]
    stats = compute_step_cost_stats(entries)
    step = stats["steps"][0]
    assert step["count"] == 2
    assert step["avg_cost_usd"] == 0.10
    assert step["avg_input_tokens"] == 30


# ---------------------------------------------------------------------------
# record_step_cost -- the tolerant wrapper dispatch call sites use
# ---------------------------------------------------------------------------


def test_record_step_cost_noop_when_parsed_is_none(tmp_path: Path) -> None:
    db = tmp_path / "analytics.db"
    record_step_cost(tmp_path, "TICK-1", "implement", "claude-a", "claude-sonnet-5", None, db_path=db)
    assert not db.exists()


def test_record_step_cost_writes_row(tmp_path: Path) -> None:
    db = tmp_path / "analytics.db"
    parsed = {
        "cost_usd": 0.05,
        "input_tokens": 3,
        "output_tokens": 6,
        "cache_creation_tokens": 100,
        "cache_read_tokens": 200,
        "duration_ms": 1500,
        "num_turns": 1,
    }
    with patch("lanegate.context_log._get_project_id", return_value="proj"):
        record_step_cost(
            tmp_path, "TICK-1", "implement", "claude-a", "claude-sonnet-5", parsed, db_path=db
        )
    rows = _load_step_costs_from_db(db, project="proj")
    assert len(rows) == 1
    assert rows[0]["cost_usd"] == 0.05
    assert rows[0]["executor"] == "claude-a"


def test_record_step_cost_clamps_duration_to_wall_clock(tmp_path: Path) -> None:
    """A self-reported duration_ms larger than the dispatch's own measured
    wall-clock elapsed time is capped to that measured value -- confirmed
    live in a fresh-install agy smoke test: agy's duration_seconds reflects
    the whole resumed --conversation session (prior turns included), not
    just this invocation's turn, so it reported ~42s for a subprocess call
    LaneGate's own started_at/finished_at measured at ~22s."""
    db = tmp_path / "analytics.db"
    parsed = {"cost_usd": 0.0, "duration_ms": 300_000, "num_turns": 2}
    with patch("lanegate.context_log._get_project_id", return_value="proj"):
        record_step_cost(
            tmp_path, "TICK-1", "implement", "agy", "default", parsed,
            db_path=db, dispatch_start_time=time.time(),
        )
    rows = _load_step_costs_from_db(db, project="proj")
    assert len(rows) == 1
    assert rows[0]["duration_ms"] < 5000


def test_record_step_cost_no_clamp_without_dispatch_start_time(tmp_path: Path) -> None:
    """Existing callers that don't pass dispatch_start_time keep writing the
    self-reported duration_ms unchanged -- no behavior change for them."""
    db = tmp_path / "analytics.db"
    parsed = {"cost_usd": 0.0, "duration_ms": 300_000, "num_turns": 2}
    with patch("lanegate.context_log._get_project_id", return_value="proj"):
        record_step_cost(tmp_path, "TICK-1", "implement", "agy", "default", parsed, db_path=db)
    rows = _load_step_costs_from_db(db, project="proj")
    assert rows[0]["duration_ms"] == 300_000


def test_record_step_cost_swallows_db_errors(tmp_path: Path) -> None:
    """Cost logging must never break the calling dispatch path."""
    parsed = {"cost_usd": 0.05}
    with patch("lanegate.context_log.log_step_cost", side_effect=RuntimeError("disk full")):
        record_step_cost(
            tmp_path, "TICK-1", "implement", "claude-a", "claude-sonnet-5", parsed,
            db_path=tmp_path / "analytics.db",
        )
    # no exception raised => pass


# ---------------------------------------------------------------------------
# resume_session_gate (TICK-310) -- age/size ceilings for --resume chaining
# ---------------------------------------------------------------------------


def test_resume_session_gate_fails_open_with_no_history(tmp_path: Path) -> None:
    """A brand new session (nothing logged for it yet) is always safe to
    resume -- the ceilings only have something to bite on once it has
    accumulated real usage."""
    db = tmp_path / "analytics.db"
    allowed, reason = resume_session_gate({}, db, "proj", "sess-new")
    assert allowed is True
    assert "no prior step_costs history" in reason


def test_resume_session_gate_disabled_returns_false(tmp_path: Path) -> None:
    db = tmp_path / "analytics.db"
    cfg = {"session_chaining": {"enabled": False}}
    allowed, reason = resume_session_gate(cfg, db, "proj", "sess-1")
    assert allowed is False
    assert "enabled is false" in reason


def test_resume_session_gate_blocks_stale_session(tmp_path: Path) -> None:
    db = tmp_path / "analytics.db"
    log_step_cost(
        db, "proj", "TICK-1", "implement",
        session_id="sess-1", timestamp="2020-01-01T00:00:00Z",
    )
    allowed, reason = resume_session_gate({}, db, "proj", "sess-1")
    assert allowed is False
    assert "max_session_age_s" in reason


def test_resume_session_gate_allows_fresh_session(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    db = tmp_path / "analytics.db"
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    log_step_cost(
        db, "proj", "TICK-1", "implement",
        session_id="sess-1", timestamp=now, input_tokens=2, cache_creation_tokens=100,
    )
    allowed, reason = resume_session_gate({}, db, "proj", "sess-1")
    assert allowed is True
    assert "within age/size ceilings" in reason


def test_resume_session_gate_blocks_oversized_session(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    db = tmp_path / "analytics.db"
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    log_step_cost(
        db, "proj", "TICK-1", "implement",
        session_id="sess-1", timestamp=now,
        input_tokens=1000, cache_creation_tokens=100000, cache_read_tokens=100000,
    )
    allowed, reason = resume_session_gate({}, db, "proj", "sess-1")
    assert allowed is False
    assert "max_session_tokens" in reason


def test_resume_session_gate_respects_configured_ceilings(tmp_path: Path) -> None:
    """Custom max_session_tokens is honored, not just the 150000 default."""
    from datetime import UTC, datetime

    db = tmp_path / "analytics.db"
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    log_step_cost(
        db, "proj", "TICK-1", "implement",
        session_id="sess-1", timestamp=now, input_tokens=500,
    )
    cfg = {"session_chaining": {"max_session_tokens": 100}}
    allowed, reason = resume_session_gate(cfg, db, "proj", "sess-1")
    assert allowed is False
    assert "max_session_tokens=100" in reason


def test_resume_session_gate_scopes_by_session_id(tmp_path: Path) -> None:
    """A stale/oversized row for a DIFFERENT session must not block resuming
    this one -- ceilings are per-session, not per-ticket."""
    db = tmp_path / "analytics.db"
    log_step_cost(
        db, "proj", "TICK-1", "implement",
        session_id="sess-other", timestamp="2020-01-01T00:00:00Z",
    )
    allowed, reason = resume_session_gate({}, db, "proj", "sess-mine")
    assert allowed is True
    assert "no prior step_costs history" in reason


def test_get_default_db_path_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Both analytics DB overrides work and remain documented for test isolation."""
    from lanegate.context_log import _get_default_db_path

    custom_db = tmp_path / "custom.db"
    monkeypatch.setenv("LANEGATE_CONTEXT_LOG_DB", str(custom_db))
    assert _get_default_db_path() == custom_db

    monkeypatch.delenv("LANEGATE_CONTEXT_LOG_DB")
    analytics_db = tmp_path / "analytics_env.db"
    monkeypatch.setenv("LANEGATE_ANALYTICS_DB", str(analytics_db))
    assert _get_default_db_path() == analytics_db

    config_reference = (
        Path(__file__).resolve().parents[1] / "docs" / "config-reference.md"
    ).read_text()
    assert "LANEGATE_CONTEXT_LOG_DB" in config_reference
    assert "LANEGATE_ANALYTICS_DB" in config_reference
    assert "~/.local/share/lanegate/analytics.db" in config_reference
    assert "test isolation" in config_reference


def test_cleanup_test_pollution(tmp_path: Path) -> None:
    """cleanup_test_pollution removes TICK-997/TICK-998 rows by ticket_id only,
    preserves genuine zero-value production rows, and scopes deletes to project
    when one is given."""
    import sqlite3
    from lanegate.context_log import _init_db, _upsert_row, cleanup_test_pollution, log_step_cost

    db = tmp_path / "test_polluted.db"
    _init_db(db)

    # Test fixture rows (the only intended targets)
    _upsert_row(db, "proj", {"ticket_id": "TICK-997", "subagent_tokens": 0, "summary_tokens": 0, "duration_ms": 0, "wall_time_ms": 0})
    _upsert_row(db, "proj", {"ticket_id": "TICK-998", "subagent_tokens": 0, "summary_tokens": 0, "duration_ms": 0, "wall_time_ms": 0})
    # Genuine production rows: normal, and zero-value for legitimate reasons
    # (e.g. wall_time_ms not yet backfilled, or an agy dispatch with no usage
    # in its envelope) -- must survive regardless of column values.
    _upsert_row(db, "proj", {"ticket_id": "TICK-100", "subagent_tokens": 5000, "summary_tokens": 100, "duration_ms": 1000, "wall_time_ms": 1200})
    _upsert_row(db, "proj", {"ticket_id": "TICK-101", "subagent_tokens": 0, "summary_tokens": 0, "tool_uses": 0, "duration_ms": 0, "wall_time_ms": 0})
    # A TICK-997 in a different project must survive when cleanup is scoped.
    _upsert_row(db, "other-proj", {"ticket_id": "TICK-997", "subagent_tokens": 42, "summary_tokens": 10, "duration_ms": 500, "wall_time_ms": 600})

    log_step_cost(db, "proj", "TICK-997", "review", input_tokens=0, output_tokens=0, duration_ms=0, cost_usd=0.0)
    log_step_cost(db, "proj", "TICK-998", "drift_check", input_tokens=0, output_tokens=0, duration_ms=0, cost_usd=0.0)
    log_step_cost(db, "proj", "TICK-100", "implement", input_tokens=1000, output_tokens=200, duration_ms=5000, cost_usd=0.05)
    # Genuine zero-cost step (e.g. agy envelope lacking usage/duration_seconds).
    log_step_cost(db, "proj", "TICK-101", "implement", input_tokens=0, output_tokens=0, duration_ms=0, cost_usd=0.0)
    log_step_cost(db, "other-proj", "TICK-997", "implement", input_tokens=300, output_tokens=50, duration_ms=2000, cost_usd=0.02)

    cleanup_test_pollution(db, project="proj")

    conn = sqlite3.connect(str(db))
    analytics_rows = conn.execute("SELECT project, ticket_id FROM analytics").fetchall()
    step_costs_rows = conn.execute("SELECT project, ticket_id FROM step_costs").fetchall()
    conn.close()

    assert ("proj", "TICK-997") not in analytics_rows
    assert ("proj", "TICK-998") not in analytics_rows
    assert ("proj", "TICK-100") in analytics_rows
    assert ("proj", "TICK-101") in analytics_rows  # zero-value production row preserved
    assert ("other-proj", "TICK-997") in analytics_rows  # other project untouched

    assert ("proj", "TICK-997") not in step_costs_rows
    assert ("proj", "TICK-998") not in step_costs_rows
    assert ("proj", "TICK-100") in step_costs_rows
    assert ("proj", "TICK-101") in step_costs_rows  # zero-value production row preserved
    assert ("other-proj", "TICK-997") in step_costs_rows  # other project untouched
