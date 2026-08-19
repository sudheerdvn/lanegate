"""Tests for dispatch metering and cost attribution (TICK-411)."""

import json

import pytest

from lanegate.budget import (
    ADVISORY_TURN_THRESHOLDS,
    DispatchMeter,
    advisory_turn_threshold,
    metering_supported_for,
)
from lanegate.executor_events import ExecutorEvent


def claude_turn(**message) -> str:
    return json.dumps({"type": "assistant", "message": message})


def event(activity: str, path: str | None = None) -> ExecutorEvent:
    return ExecutorEvent(
        phase="implementing", activity=activity, ts="2026-08-06T00:00:00Z", path=path
    )


class TestTurnAndUsageCounting:
    def test_counts_one_turn_per_claude_assistant_envelope(self):
        meter = DispatchMeter()
        for _ in range(3):
            meter.observe(claude_turn(content=[]))
        assert meter.turns == 3

    def test_counts_codex_internal_turns(self):
        meter = DispatchMeter()
        for _ in range(3):
            meter.observe(json.dumps({"type": "item.completed"}))
        meter.observe(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5}}))
        assert meter.turns == 3

    def test_cache_reads_count_as_spend(self):
        """Cache reads dominate a long run; a figure ignoring them is useless."""
        meter = DispatchMeter()
        meter.observe(
            claude_turn(usage={"input_tokens": 100, "cache_read_input_tokens": 90_000})
        )
        assert meter.tokens == 90_100

    def test_cumulative_cost_is_maxed_not_summed(self):
        """Claude reports cost-to-date; summing would multiply a long run's spend."""
        meter = DispatchMeter()
        for cost in (0.5, 1.25, 2.0):
            meter.observe(json.dumps({"type": "result", "total_cost_usd": cost}))
        assert meter.cost_usd == pytest.approx(2.0)

    def test_malformed_lines_never_raise(self):
        meter = DispatchMeter()
        for line in ("", "   ", "not json", "[1,2,3]", '{"type": null}', "null"):
            meter.observe(line)
        assert meter.turns == 0

    def test_session_id_captured_from_stream_not_final_envelope(self):
        """A killed dispatch emits no final envelope, so the id must come early."""
        meter = DispatchMeter()
        meter.observe(json.dumps({"type": "system", "subtype": "init", "session_id": "abc-123"}))
        meter.observe(claude_turn(content=[]))
        assert meter.session_id == "abc-123"

    def test_first_session_id_wins(self):
        meter = DispatchMeter()
        meter.observe(json.dumps({"type": "system", "session_id": "first"}))
        meter.observe(json.dumps({"type": "result", "session_id": "second"}))
        assert meter.session_id == "first"


class TestTurnMix:
    def test_repeat_reads_are_counted(self):
        meter = DispatchMeter()
        for _ in range(3):
            meter.observe("", event("reading_file", "src/a.py"))
        meter.observe("", event("reading_file", "src/b.py"))
        assert meter.reread_count == 2  # a.py read 3x = 2 rereads; b.py = 0

    def test_distinct_written_files_tracked(self):
        meter = DispatchMeter()
        meter.observe("", event("writing_file", "src/a.py"))
        meter.observe("", event("writing_file", "src/a.py"))
        meter.observe("", event("writing_file", "src/b.py"))
        assert len(meter.files_written) == 2


class TestDiagnosis:
    """The point of the feature: separate a LaneGate problem from a ticket problem."""

    def test_cheap_run_is_not_diagnosed(self):
        meter = DispatchMeter(step="implement", turns=10)
        assert meter.diagnose() is None

    def test_exploration_heavy_run_blames_missing_context(self):
        meter = DispatchMeter(step="implement")
        meter.turns = ADVISORY_TURN_THRESHOLDS["implement"] + 1
        for i in range(60):
            meter.observe("", event("reading_file", f"src/f{i}.py"))
        for i in range(10):
            meter.observe("", event("writing_file", f"src/f{i}.py"))
        result = meter.diagnose()
        assert result is not None
        assert result["verdict"] == "context-starved"

    def test_rereads_alone_blame_missing_context(self):
        """Re-paging the same file in is the signature of context not sticking."""
        meter = DispatchMeter(step="implement")
        meter.turns = ADVISORY_TURN_THRESHOLDS["implement"] + 1
        for _ in range(5):
            meter.observe("", event("reading_file", "src/same.py"))
        for i in range(40):
            meter.observe("", event("running_command"))
        assert meter.diagnose()["verdict"] == "context-starved"

    def test_wide_write_spread_blames_the_ticket(self):
        meter = DispatchMeter(step="implement")
        meter.turns = ADVISORY_TURN_THRESHOLDS["implement"] + 1
        for i in range(8):
            meter.observe("", event("writing_file", f"src/f{i}.py"))
        for _ in range(30):
            meter.observe("", event("running_command"))
        result = meter.diagnose()
        assert result["verdict"] == "oversized-ticket"
        assert "8 distinct files" in result["detail"]

    def test_repeated_test_runs_are_called_out(self):
        meter = DispatchMeter(step="implement")
        meter.turns = ADVISORY_TURN_THRESHOLDS["implement"] + 1
        for _ in range(25):
            meter.observe("", event("testing"))
        for _ in range(30):
            meter.observe("", event("running_command"))
        assert meter.diagnose()["verdict"] == "test-churn"

    def test_expensive_but_unclear_run_is_not_forced_into_a_cause(self):
        meter = DispatchMeter(step="implement")
        meter.turns = ADVISORY_TURN_THRESHOLDS["implement"] + 1
        for _ in range(40):
            meter.observe("", event("running_command"))
        assert meter.diagnose()["verdict"] == "unattributed"


class TestConfiguration:
    def test_project_can_tune_the_threshold(self):
        assert advisory_turn_threshold("implement", {"turn_advisory": {"implement": 5}}) == 5

    def test_zero_disables_the_advisory(self):
        assert advisory_turn_threshold("implement", {"turn_advisory": {"implement": 0}}) is None

    def test_disabled_step_is_never_diagnosed(self):
        meter = DispatchMeter(step="implement", turns=500)
        assert meter.diagnose({"turn_advisory": {"implement": 0}}) is None

    def test_unconfigured_project_uses_measured_defaults(self):
        assert advisory_turn_threshold("implement") == ADVISORY_TURN_THRESHOLDS["implement"]

    @pytest.mark.parametrize("executor_type", ["claude", "claude-process", "codex"])
    def test_streaming_executors_are_meterable(self, executor_type):
        assert metering_supported_for(executor_type)

    @pytest.mark.parametrize("executor_type", ["aider", "agy", "ollama", "openhands"])
    def test_non_streaming_executors_report_unmeterable(self, executor_type):
        """Better to say a run cannot be metered than to report a fake turn count."""
        assert not metering_supported_for(executor_type)


class TestSymbolLookupIsBuiltIn:
    """TICK-411: structural lookup must not require a third-party tool."""

    def test_python_symbols_come_from_stdlib_ast(self, tmp_path):
        from lanegate.analyze import file_symbols

        src = tmp_path / "m.py"
        src.write_text("class A:\n    def go(self): pass\n\ndef top(x): pass\n")
        found = " ".join(file_symbols(src, tmp_path))
        assert "class A" in found
        assert "def top" in found

    def test_unreadable_file_degrades_to_empty_not_error(self, tmp_path):
        from lanegate.analyze import file_symbols

        assert file_symbols(tmp_path / "gone.py", tmp_path) == []

    def test_unknown_language_degrades_to_empty(self, tmp_path):
        """A missing grammar means fall back to searching, not crash."""
        from lanegate.analyze import file_symbols

        odd = tmp_path / "a.zzz"
        odd.write_text("whatever")
        assert file_symbols(odd, tmp_path) == []


class TestSkeletonsCoverAllLanguages:
    """TICK-412: non-Python files used to reach the agent with no structure."""

    def test_go_file_yields_signatures_not_just_a_line_count(self, tmp_path):
        pytest.importorskip("tree_sitter_go")
        from lanegate.analyze import _build_file_skeleton

        src = tmp_path / "srv.go"
        src.write_text(
            "package main\n\n"
            "type Server struct {\n\tAddr string\n}\n\n"
            "func (s *Server) Start() error {\n\treturn nil\n}\n"
        )
        skeleton = _build_file_skeleton(src, tmp_path)
        assert "func (s *Server) Start() error" in skeleton
        assert "type Server struct" in skeleton

    def test_language_without_a_grammar_still_gets_a_header(self, tmp_path):
        from lanegate.analyze import _build_file_skeleton

        src = tmp_path / "a.zzz"
        src.write_text("one\ntwo\n")
        skeleton = _build_file_skeleton(src, tmp_path)
        assert "a.zzz" in skeleton
        assert "(2 lines)" in skeleton


class TestSkeletonsAreRegeneratedNotReplayed:
    """TICK-412: a stale skeleton is worse than none — the agent believes it."""

    def test_regeneration_reflects_current_file_not_the_snapshot(self, tmp_path):
        from lanegate.ticket import load_file_skeletons

        src = tmp_path / "m.py"
        src.write_text("def current_name(): pass\n")
        ticket = {
            "touches": ["m.py"],
            "file_skeletons": {"m.py": "m.py  (1 lines)\n  line   1: def stale_name()"},
        }
        fresh = load_file_skeletons(ticket, tmp_path, regenerate=True)
        assert "current_name" in fresh["m.py"]
        assert "stale_name" not in fresh["m.py"]

    def test_stored_snapshot_is_used_when_regeneration_finds_nothing(self, tmp_path):
        """Deleted or unparseable touches must not blank out existing context."""
        from lanegate.ticket import load_file_skeletons

        ticket = {
            "touches": ["gone.py"],
            "file_skeletons": {"gone.py": "gone.py  (1 lines)"},
        }
        assert load_file_skeletons(ticket, tmp_path, regenerate=True) == {
            "gone.py": "gone.py  (1 lines)"
        }

    def test_default_still_replays_the_snapshot(self, tmp_path):
        from lanegate.ticket import load_file_skeletons

        src = tmp_path / "m.py"
        src.write_text("def current_name(): pass\n")
        ticket = {"touches": ["m.py"], "file_skeletons": {"m.py": "stored"}}
        assert load_file_skeletons(ticket, tmp_path) == {"m.py": "stored"}
