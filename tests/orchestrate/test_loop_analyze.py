"""
Tests for lanegate/orchestrate/loop_analyze.py.

Split out of the former monolithic tests/test_orchestrate.py (TICK-316).
"""

from __future__ import annotations

import datetime

from lanegate.git import GitText
from tests.orchestrate.conftest import *  # noqa: F401,F403
from lanegate.orchestrate.loop_analyze import _analyze_drafts, _print_draft_analysis_plan

from tests._helpers.orchestrate import _write_draft_ticket


class TestAnalyzeDrafts:
    """Unit tests for _analyze_drafts and _print_draft_analysis_plan."""

    def test_draft_analyzed_before_dispatch(self, tmp_path, capsys):
        """_analyze_drafts calls cmd_analyze for each draft ticket."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_draft_ticket(tickets_dir, "TICK-001")

        with patch("lanegate.analyze.cmd_analyze") as mock_analyze:
            _analyze_drafts(cfg, tmp_path, tickets_dir=tickets_dir)

        mock_analyze.assert_called_once_with("TICK-001", cfg, tmp_path, pool_name=None)

    def test_analyze_drafts_includes_open_empty_touches(self, tmp_path, capsys):
        """_analyze_drafts calls cmd_analyze for status:open tickets with empty touches."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=[])

        with patch("lanegate.analyze.cmd_analyze") as mock_analyze:
            _analyze_drafts(cfg, tmp_path, tickets_dir=tickets_dir)

        mock_analyze.assert_called_once_with("TICK-001", cfg, tmp_path, pool_name=None)

    def test_milestone_filter_respected(self, tmp_path):
        """_analyze_drafts skips drafts that do not match the active milestone."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_draft_ticket(tickets_dir, "TICK-001", milestone="v1")
        _write_draft_ticket(tickets_dir, "TICK-002", milestone="v2")

        analyzed = []

        def fake_analyze(tid, cfg_, repo_root, pool_name=None):
            analyzed.append(tid)

        with patch("lanegate.analyze.cmd_analyze", side_effect=fake_analyze):
            _analyze_drafts(cfg, tmp_path, milestone="v1", tickets_dir=tickets_dir)

        assert "TICK-001" in analyzed
        assert "TICK-002" not in analyzed

    def test_ticket_scope_respected(self, tmp_path):
        """_analyze_drafts must not analyze drafts outside an explicit
        --tickets scope (TICK-262) -- a run scoped to one ticket must not go
        analyze an unrelated draft elsewhere in the same milestone just
        because the requested ticket wasn't itself a draft ready to analyze.
        """
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_draft_ticket(tickets_dir, "TICK-001")
        _write_draft_ticket(tickets_dir, "TICK-002")

        analyzed = []

        def fake_analyze(tid, cfg_, repo_root, pool_name=None):
            analyzed.append(tid)

        with patch("lanegate.analyze.cmd_analyze", side_effect=fake_analyze):
            _analyze_drafts(cfg, tmp_path, tickets_dir=tickets_dir, ticket_ids={"TICK-002"})

        assert analyzed == ["TICK-002"]

    def test_failed_analyze_skipped_gracefully(self, tmp_path, capsys):
        """_analyze_drafts logs a warning and continues when cmd_analyze raises."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_draft_ticket(tickets_dir, "TICK-001")
        _write_draft_ticket(tickets_dir, "TICK-002")

        analyzed = []

        def fake_analyze(tid, cfg_, repo_root, pool_name=None):
            if tid == "TICK-001":
                raise RuntimeError("analyze failed")
            analyzed.append(tid)

        with patch("lanegate.analyze.cmd_analyze", side_effect=fake_analyze):
            # Should not raise
            _analyze_drafts(cfg, tmp_path, tickets_dir=tickets_dir)

        # TICK-002 should still have been analyzed despite TICK-001 failing
        assert "TICK-002" in analyzed
        captured = capsys.readouterr()
        assert "WARNING" in captured.err

    def test_failed_analyze_records_ticket_outcome_and_run_reports_failure(self, tmp_path):
        """A swallowed analyze failure must still be a durable ticket_outcome
        event so the run summary calls the run FAILURE instead of SUCCESS
        (TICK-642) — otherwise a run that aborted on a real, unresolved
        analyze error looks like a clean success everywhere but the raw log.
        """
        from lanegate.orchestrate.run_report import _append_run_event, build_run_summary
        from lanegate.orchestrate.run_summary import RunReason

        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_draft_ticket(tickets_dir, "TICK-001")
        session_ts = "2026-08-22T00-00-00"

        def fake_analyze(tid, cfg_, repo_root, pool_name=None):
            import sys as _sys

            print("ERROR: model returned empty or non-list touches; ticket left as draft", file=_sys.stderr)
            _sys.exit(1)

        _append_run_event(tmp_path, session_ts, "run_start", pid=os.getpid())
        with patch("lanegate.analyze.cmd_analyze", side_effect=fake_analyze):
            _analyze_drafts(cfg, tmp_path, tickets_dir=tickets_dir, session_ts=session_ts)
        _append_run_event(tmp_path, session_ts, "run_end", status="completed")

        summary = build_run_summary(cfg, tmp_path, session_ts=session_ts)
        assert summary.reason == RunReason.FAILURE
        assert len(summary.batch_tickets) == 1
        failed = summary.batch_tickets[0]
        assert failed.ticket_id == "TICK-001"
        assert "empty or non-list touches" in (failed.failure_reason or "")

    def test_analyze_drafts_skips_already_resolved_drafts(self, tmp_path):
        """_analyze_drafts must skip draft tickets that already have 'already resolved' in their body."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        path = _write_draft_ticket(tickets_dir, "TICK-001")
        path.write_text(path.read_text() + "\n## Needs Review Reason\nanalyze: ticket premise appears already resolved\n")

        analyzed = []
        with patch("lanegate.analyze.cmd_analyze", side_effect=lambda tid, *a, **k: analyzed.append(tid)):
            _analyze_drafts(cfg, tmp_path, tickets_dir=tickets_dir)

        assert analyzed == []

    def test_interrupt_stops_draft_analysis_without_touching_next_draft(self, tmp_path, capsys):
        """Ctrl-C is a run-level stop, not a failure to skip past."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_draft_ticket(tickets_dir, "TICK-001")
        _write_draft_ticket(tickets_dir, "TICK-002")
        analyzed = []

        def interrupted_analyze(tid, cfg_, repo_root, pool_name=None):
            analyzed.append(tid)
            raise SystemExit(130)

        with patch("lanegate.analyze.cmd_analyze", side_effect=interrupted_analyze):
            assert _analyze_drafts(cfg, tmp_path, tickets_dir=tickets_dir) is True

        assert analyzed == ["TICK-001"]
        assert parse_ticket(tickets_dir / "TICK-001.md")["status"] == "draft"
        assert parse_ticket(tickets_dir / "TICK-002.md")["status"] == "draft"
        assert "stopping further dispatch" in capsys.readouterr().err

    def test_repeated_identical_failure_stops_the_pass(self, tmp_path, capsys):
        """A systemic failure (identical stderr on consecutive drafts) must stop
        the whole draft-analysis pass instead of repeating the same doomed
        model call — and cost — across every remaining draft.
        """
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_draft_ticket(tickets_dir, "TICK-001")
        _write_draft_ticket(tickets_dir, "TICK-002")
        _write_draft_ticket(tickets_dir, "TICK-003")

        analyzed = []

        def fake_analyze(tid, cfg_, repo_root, pool_name=None):
            import sys as _sys

            analyzed.append(tid)
            print("ERROR: model returned empty or non-list touches; ticket left as draft", file=_sys.stderr)
            _sys.exit(1)

        with patch("lanegate.analyze.cmd_analyze", side_effect=fake_analyze):
            _analyze_drafts(cfg, tmp_path, tickets_dir=tickets_dir)

        # Stops after the 2nd identical failure — TICK-003 never attempted.
        assert analyzed == ["TICK-001", "TICK-002"]
        captured = capsys.readouterr()
        assert "systemic" in captured.err

    def test_failures_with_different_claude_session_metadata_stop_the_pass(self, tmp_path, capsys):
        """Volatile Claude metadata does not hide a repeated systemic failure."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_draft_ticket(tickets_dir, "TICK-001")
        _write_draft_ticket(tickets_dir, "TICK-002")
        _write_draft_ticket(tickets_dir, "TICK-003")

        metadata = {
            "TICK-001": {
                "session_id": "11111111-1111-4111-8111-111111111111",
                "message": '{"id":"22222222-2222-4222-8222-222222222222"}',
                "uuid": "33333333-3333-4333-8333-333333333333",
                "timestamp": "2026-08-04T12:00:01.123Z",
                "total_cost_usd": "0.12",
                "input_tokens": "123",
                "output_tokens": "45",
            },
            "TICK-002": {
                "session_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "message": '{"id":"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"}',
                "uuid": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                "timestamp": "2026-08-04T12:01:02.456Z",
                "total_cost_usd": "0.34",
                "input_tokens": "678",
                "output_tokens": "90",
            },
        }
        analyzed = []

        def fake_analyze(tid, cfg_, repo_root, pool_name=None):
            import sys as _sys

            analyzed.append(tid)
            print(
                "ERROR: Claude CLI unavailable "
                + " ".join(f"{key}={value}" for key, value in metadata[tid].items()),
                file=_sys.stderr,
            )
            _sys.exit(1)

        with patch("lanegate.analyze.cmd_analyze", side_effect=fake_analyze):
            _analyze_drafts(cfg, tmp_path, tickets_dir=tickets_dir)

        assert analyzed == ["TICK-001", "TICK-002"]
        assert "systemic" in capsys.readouterr().err

    def test_stops_early_once_a_draft_becomes_dispatchable(self, tmp_path):
        """Once an analyzed draft is dispatchable, the pass returns immediately
        instead of draining the rest of the draft backlog first — ready work
        must not sit idle behind unrelated drafts still waiting their turn.
        """
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_draft_ticket(tickets_dir, "TICK-001")
        _write_draft_ticket(tickets_dir, "TICK-002")

        analyzed = []

        def fake_analyze(tid, cfg_, repo_root, pool_name=None):
            analyzed.append(tid)
            # Mirror what real cmd_analyze does: flip the ticket open with
            # real (empty, unblocked) touches once analysis succeeds.
            path = tickets_dir / f"{tid}.md"
            path.write_text(
                path.read_text()
                .replace("status: draft", "status: open")
                .replace("close_criteria: TBD.\n", "close_criteria: TBD.\ntouches: [\"a.py\"]\n")
            )

        with patch("lanegate.analyze.cmd_analyze", side_effect=fake_analyze):
            _analyze_drafts(cfg, tmp_path, tickets_dir=tickets_dir)

        # TICK-002 is left for the next pass, once TICK-001 has been dispatched.
        assert analyzed == ["TICK-001"]

    def test_different_failures_do_not_stop_the_pass(self, tmp_path, capsys):
        """Distinct per-ticket failure reasons are treated as ticket-specific
        content issues, not a systemic problem — the pass keeps going.
        """
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_draft_ticket(tickets_dir, "TICK-001")
        _write_draft_ticket(tickets_dir, "TICK-002")
        _write_draft_ticket(tickets_dir, "TICK-003")

        analyzed = []

        def fake_analyze(tid, cfg_, repo_root, pool_name=None):
            import sys as _sys

            analyzed.append(tid)
            print(f"ERROR: distinct failure for {tid}", file=_sys.stderr)
            _sys.exit(1)

        with patch("lanegate.analyze.cmd_analyze", side_effect=fake_analyze):
            _analyze_drafts(cfg, tmp_path, tickets_dir=tickets_dir)

        assert analyzed == ["TICK-001", "TICK-002", "TICK-003"]
        captured = capsys.readouterr()
        assert "systemic" not in captured.err

    def test_print_draft_analysis_plan(self, tmp_path, capsys):
        """_print_draft_analysis_plan prints which drafts would be analyzed."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_draft_ticket(tickets_dir, "TICK-001")
        _write_draft_ticket(tickets_dir, "TICK-002", milestone="v2")

        _print_draft_analysis_plan(cfg, tmp_path, milestone=None, tickets_dir=tickets_dir)

        captured = capsys.readouterr()
        assert "TICK-001" in captured.out
        assert "TICK-002" in captured.out
        assert "dry-run" in captured.out

    def test_print_draft_analysis_plan_respects_ticket_ids(self, tmp_path, capsys):
        """TICK-262: --dry-run must reflect the restricted --tickets candidate
        set, not just show every draft in the milestone."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_draft_ticket(tickets_dir, "TICK-001")
        _write_draft_ticket(tickets_dir, "TICK-002")

        _print_draft_analysis_plan(
            cfg, tmp_path, milestone=None, tickets_dir=tickets_dir, ticket_ids={"TICK-002"}
        )

        captured = capsys.readouterr()
        assert "TICK-001" not in captured.out
        assert "TICK-002" in captured.out

    def test_print_draft_analysis_plan_includes_open_empty_touches(self, tmp_path, capsys):
        """_print_draft_analysis_plan includes status:open tickets with empty touches in dry-run mode."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=[])

        _print_draft_analysis_plan(cfg, tmp_path, milestone=None, tickets_dir=tickets_dir)

        captured = capsys.readouterr()
        assert "TICK-001" in captured.out
        assert "dry-run" in captured.out

    def test_no_auto_analyze_flag_bypasses_analyze(self, tmp_path, capsys):
        """--no-auto-analyze (auto_analyze=False) skips _analyze_drafts entirely."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_draft_ticket(tickets_dir, "TICK-001")
        # Also write an open ticket so the loop runs at least once
        _write_ticket(tickets_dir, "TICK-002", "open", touches=["a.py"])

        with (
            patch("lanegate.orchestrate._analyze_drafts") as mock_analyze_drafts,
            patch("lanegate.orchestrate._print_draft_analysis_plan") as mock_plan,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", return_value=(0, "", "")),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.lifecycle.cmd_complete") as mock_complete,
            patch("lanegate.lifecycle.cmd_review"),
        ):

            def fake_complete(tid, cfg_, repo_root):
                p = tickets_dir / f"{tid}.md"
                text = p.read_text().replace("status: open", "status: code_complete")
                p.write_text(text)

            mock_complete.side_effect = fake_complete
            cmd_orchestrate(cfg, tmp_path, all_milestones=True, auto_analyze=False)

        mock_analyze_drafts.assert_not_called()
        mock_plan.assert_not_called()

