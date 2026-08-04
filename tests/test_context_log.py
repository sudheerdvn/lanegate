"""
tests/test_context_log.py — Unit tests for lanegate.context_log
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lanegate.context_log import (
    _import_legacy,
    _is_legacy_imported,
    _load_entries_from_db,
    _load_step_costs_from_db,
    _upsert_row,
    cmd_context_stats,
    cmd_log_backfill,
    compute_stats,
    compute_step_cost_stats,
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

    # avg work tok for claude-subagent: (28656+21554)//2 = 25105
    assert "25,105" in out

    # pass rate for aider: 0/1 = 0%
    assert "0%" in out

    # pass rate for claude-subagent: 2/2 = 100%
    assert "100%" in out


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
    assert parsed["input_tokens"] == 13142
    assert parsed["output_tokens"] == 5  # 5 output + 0 reasoning
    assert parsed["cache_read_tokens"] == 0
    assert parsed["cache_creation_tokens"] == 0
    assert parsed["cost_usd"] is None  # codex reports no dollar figure


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
