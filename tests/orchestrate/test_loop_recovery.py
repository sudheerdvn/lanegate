"""
Tests for lanegate/orchestrate/loop_recovery.py.

Split out of the former monolithic tests/test_orchestrate.py (TICK-316).
"""

from __future__ import annotations

import datetime

from lanegate.git import GitText
from tests.orchestrate.conftest import *  # noqa: F401,F403
from tests._helpers.orchestrate import _write_draft_ticket




from lanegate.orchestrate.loop_recovery import recover_scope_only_needs_review_tickets

class TestConflictAwareResume:
    def test_review_pending_resume_skips_implementation(self, tmp_path):
        """A hibernated completed ticket resumes at review, not invoke_executor."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        path = _write_ticket(tickets_dir, "TICK-001", "hibernated", touches=["a.py"])
        path.write_text(path.read_text().replace("status: hibernated", "status: hibernated\nreview_pending: true"))
        (tmp_path / "worktrees" / "tick-001").mkdir()

        def start_review_pending(tid, cfg_, root, **kwargs):
            path.write_text(path.read_text().replace("status: hibernated", "status: in_progress"))

        def approve(tid, cfg_, root, **kwargs):
            # resume_review_pending has already restored code_complete by the
            # time cmd_review runs -- see the regression test below for that
            # transition in isolation.
            text = path.read_text().replace("status: code_complete", "status: in_review")
            path.write_text(text.replace("review_pending: true\n", "review_verdict: approved\n"))

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=start_review_pending),
            patch("lanegate.orchestrate._run_rebase", return_value=("clean", "")),
            patch("lanegate.lifecycle.cmd_review", side_effect=approve),
            patch("lanegate.orchestrate.invoke_executor") as invoke,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        invoke.assert_not_called()
        assert parse_ticket(path)["status"] == "in_review"

    def test_review_pending_conflict_recovery_reverifies_before_review(self, tmp_path):
        """Agent-resolved rebase conflicts invalidate pre-hibernation checks."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        path = _write_ticket(tickets_dir, "TICK-001", "hibernated", touches=["a.py"])
        path.write_text(path.read_text().replace("status: hibernated", "status: hibernated\nreview_pending: true"))
        (tmp_path / "worktrees" / "tick-001").mkdir()

        def start_review_pending(tid, cfg_, root, **kwargs):
            path.write_text(path.read_text().replace("status: hibernated", "status: in_progress"))

        def approve(tid, cfg_, root, **kwargs):
            text = path.read_text().replace("status: code_complete", "status: in_review")
            path.write_text(text.replace("review_pending: true\n", "review_verdict: approved\n"))

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=start_review_pending),
            patch("lanegate.orchestrate._run_rebase", return_value=("conflict", "### a.py")),
            patch("lanegate.orchestrate.autofix.run_rebase_fix_agent", return_value=True),
            patch("lanegate.orchestrate.loop._git_head_sha", side_effect=["before", "after"]),
            patch("lanegate.safeguards.run_safeguards", return_value=(True, "")) as safeguards,
            patch("lanegate.lifecycle.cmd_review", side_effect=approve) as review,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        safeguards.assert_called_once()
        assert safeguards.call_args.args[0] == "pre_complete"
        assert safeguards.call_args.args[2:] == (cfg, tmp_path / "worktrees" / "tick-001")
        review.assert_called_once()
        assert parse_ticket(path)["pre_complete_verified_sha"] == "after"

    def test_review_pending_conflict_recovery_blocks_review_when_verification_fails(self, tmp_path):
        """A failed post-rebase safeguard must not dispatch a reviewer."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        path = _write_ticket(tickets_dir, "TICK-001", "hibernated", touches=["a.py"])
        path.write_text(path.read_text().replace("status: hibernated", "status: hibernated\nreview_pending: true"))
        (tmp_path / "worktrees" / "tick-001").mkdir()

        def start_review_pending(tid, cfg_, root, **kwargs):
            path.write_text(path.read_text().replace("status: hibernated", "status: in_progress"))

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=start_review_pending),
            patch("lanegate.orchestrate._run_rebase", return_value=("conflict", "### a.py")),
            patch("lanegate.orchestrate.autofix.run_rebase_fix_agent", return_value=True),
            patch("lanegate.orchestrate.loop._git_head_sha", return_value="after"),
            patch("lanegate.safeguards.run_safeguards", return_value=(False, "tests failed")) as safeguards,
            patch("lanegate.lifecycle.cmd_review") as review,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        safeguards.assert_called_once()
        review.assert_not_called()
        assert parse_ticket(path)["status"] == "needs_review"

    def test_review_pending_resume_full_autonomy_auto_merges_without_crash(self, tmp_path, capsys):
        """Regression test: resuming a hibernated review-pending ticket under
        full autonomy must reach auto_merge_approved_local_tickets and log the
        merged outcome without an UnboundLocalError on final_ticket."""
        cfg = _default_cfg(tmp_path)
        cfg["autonomy"] = "full"
        tickets_dir = tmp_path / "tickets"
        path = _write_ticket(tickets_dir, "TICK-001", "hibernated", touches=["a.py"])
        path.write_text(path.read_text().replace("status: hibernated", "status: hibernated\nreview_pending: true"))
        (tmp_path / "worktrees" / "tick-001").mkdir()

        def start_review_pending(tid, cfg_, root, **kwargs):
            path.write_text(path.read_text().replace("status: hibernated", "status: in_progress"))

        def approve(tid, cfg_, root, **kwargs):
            text = path.read_text().replace("status: code_complete", "status: in_review")
            path.write_text(text.replace("review_pending: true\n", "review_verdict: approved\n"))

        def fake_merge(tid, cfg_, root):
            path.write_text(path.read_text().replace("status: in_review", "status: merged"))

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=start_review_pending),
            patch("lanegate.orchestrate._run_rebase", return_value=("clean", "")),
            patch("lanegate.lifecycle.cmd_review", side_effect=approve),
            patch("lanegate.lifecycle.cmd_merge", side_effect=fake_merge),
            patch("lanegate.orchestrate.invoke_executor") as invoke,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        captured = capsys.readouterr()
        assert "worker thread crashed" not in captured.err
        assert "UnboundLocalError" not in captured.err
        invoke.assert_not_called()
        assert parse_ticket(path)["status"] == "merged"

    def test_review_pending_resume_red_lane_does_not_auto_merge(self, tmp_path):
        """A resumed review must re-scan its diff before the shared merge path."""
        from types import SimpleNamespace

        cfg = _default_cfg(tmp_path)
        cfg["autonomy"] = "full"
        tickets_dir = tmp_path / "tickets"
        path = _write_ticket(tickets_dir, "TICK-001", "hibernated", touches=["a.py"])
        path.write_text(path.read_text().replace("status: hibernated", "status: hibernated\nreview_pending: true"))
        (tmp_path / "worktrees" / "tick-001").mkdir()

        def start_review_pending(tid, cfg_, root, **kwargs):
            path.write_text(path.read_text().replace("status: hibernated", "status: in_progress"))

        def approve(tid, cfg_, root, **kwargs):
            text = path.read_text().replace("status: code_complete", "status: in_review")
            path.write_text(text.replace("review_pending: true\n", "review_verdict: approved\n"))

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=start_review_pending),
            patch("lanegate.orchestrate._run_rebase", return_value=("clean", "")),
            patch("lanegate.lifecycle.cmd_review", side_effect=approve),
            patch(
                "lanegate.orchestrate.loop._git_text",
                return_value=SimpleNamespace(ok=True, text="+ token = 'sk-red-lane'"),
            ),
            patch("lanegate.orchestrate.loop.scan_risk_lane", return_value="red") as mock_scan,
            patch("lanegate.lifecycle.cmd_merge") as mock_merge,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_scan.assert_called_once()
        mock_merge.assert_not_called()
        ticket = parse_ticket(path)
        assert ticket["status"] == "needs_review"
        assert ticket["review_verdict"] == "changes_requested"

    def test_ready_to_merge_ticket_drains_before_dispatching_new_open_work(self, tmp_path):
        """Approved in_review work must merge before the loop dispatches a new
        open ticket -- otherwise WIP (and the touch-locks it holds) keeps
        growing while already-finished tickets sit waiting, which is exactly
        what produces avoidable lock contention on the rest of the board."""
        cfg = _default_cfg(tmp_path)
        cfg["autonomy"] = "full"
        cfg["max_parallel"] = 1
        tickets_dir = tmp_path / "tickets"
        p1 = _write_ticket(tickets_dir, "TICK-001", "in_review", touches=["a.py"])
        p1.write_text(p1.read_text().replace("close_criteria:", "review_verdict: approved\nclose_criteria:"))
        _write_ticket(tickets_dir, "TICK-002", "open", touches=["b.py"])

        call_order = []

        def fake_merge(tid, cfg_, root):
            call_order.append(f"merge:{tid}")
            p1.write_text(p1.read_text().replace("status: in_review", "status: merged"))

        def fake_start(tid, cfg_, root, **kwargs):
            call_order.append(f"start:{tid}")
            # Stop right here -- this test only cares about dispatch order,
            # not a full implement/review/merge pipeline for TICK-002. The
            # worker exception handler downgrades this one ticket to
            # needs_review without crashing the rest of the run.
            raise RuntimeError("stop-here: ordering test only cares about start order")

        with (
            patch("lanegate.lifecycle.cmd_merge", side_effect=fake_merge),
            patch("lanegate.lifecycle.cmd_start", side_effect=fake_start),
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
            patch("lanegate.orchestrate.spawn_watch_daemon"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        assert call_order == ["merge:TICK-001", "start:TICK-002"]
        assert parse_ticket(p1)["status"] == "merged"

    def test_full_autonomy_holds_agent_resolved_rebase_for_human_merge(self, tmp_path, capsys):
        """A passed review cannot auto-merge a branch whose rebase needed an agent."""
        cfg = _default_cfg(tmp_path)
        cfg["autonomy"] = "full"
        tickets_dir = tmp_path / "tickets"
        path = _write_ticket(tickets_dir, "TICK-001", "in_review", touches=["a.py"])
        path.write_text(
            path.read_text().replace(
                "close_criteria:",
                "review_verdict: approved\nrequires_human_merge: true\n"
                "rebase_conflict_files: [a.py]\nclose_criteria:",
            )
        )

        with (
            patch("lanegate.lifecycle.cmd_merge") as mock_merge,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
            patch("lanegate.orchestrate.spawn_watch_daemon"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_merge.assert_not_called()
        assert parse_ticket(path)["status"] == "in_review"
        assert "requires human merge approval (a.py)" in capsys.readouterr().err

    def test_run_rebase_uses_real_git_rebase_main(self, tmp_path):
        with patch(
            "lanegate.orchestrate.subprocess.run",
            return_value=subprocess.CompletedProcess(["git"], 0, stdout="", stderr=""),
        ) as mock_run:
            state, detail = _run_rebase(tmp_path, base="main")

        assert state == "clean"
        assert detail == ""
        assert mock_run.call_args.args[0] == ["git", "rebase", "main"]

    def test_format_conflict_detail_includes_only_conflict_hunks(self, tmp_path):
        conflicted = tmp_path / "app.py"
        conflicted.write_text(
            "before\n"
            "<<<<<<< HEAD\n"
            "ours\n"
            "=======\n"
            "theirs\n"
            ">>>>>>> main\n"
            "after\n"
        )

        detail = _format_conflict_detail(tmp_path, ["app.py"])

        assert "## Conflict resolution required" in detail
        assert "### app.py" in detail
        assert "<<<<<<< HEAD" in detail
        assert "=======" in detail
        assert ">>>>>>> main" in detail
        assert "before" not in detail
        assert "after" not in detail

    def test_run_rebase_conflict_reports_hunks_not_full_diff(self, tmp_path):
        (tmp_path / "app.py").write_text(
            "before\n"
            "<<<<<<< HEAD\n"
            "ours\n"
            "=======\n"
            "theirs\n"
            ">>>>>>> main\n"
            "after\n"
        )

        def fake_run(args, **kwargs):
            if args == ["git", "rebase", "main"]:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="conflict")
            if args == ["git", "diff", "--name-only", "--diff-filter=U"]:
                return subprocess.CompletedProcess(args, 0, stdout="app.py\n", stderr="")
            raise AssertionError(args)

        with patch("lanegate.orchestrate.subprocess.run", side_effect=fake_run):
            state, detail = _run_rebase(tmp_path, base="main")

        assert state == "conflict"
        assert "app.py" in detail
        assert "<<<<<<< HEAD" in detail
        assert "before" not in detail
        assert "after" not in detail

    def test_conflicted_files_returns_unmerged_paths(self, tmp_path):
        with patch(
            "lanegate.orchestrate.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["git"], 0, stdout="app.py\npkg/mod.py\n", stderr=""
            ),
        ):
            assert _conflicted_files(tmp_path) == ["app.py", "pkg/mod.py"]

    def test_hibernated_conflict_success_continues_rebase(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "hibernated", touches=["a.py"])
        def fake_complete(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: hibernated", "status: code_complete"))

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch(
                "lanegate.orchestrate._run_rebase",
                return_value=("conflict", "## Conflict resolution required\n\n### a.py"),
            ),
            patch("lanegate.orchestrate._conflicted_files", return_value=["a.py"]),
            patch("lanegate.orchestrate.invoke_executor", return_value=(0, "", "")),
            patch(
                "lanegate.orchestrate.autofix.run_rebase_fix_agent", return_value=True
            ) as mock_rebase_fix,
            patch("lanegate.orchestrate._continue_rebase", return_value=(True, "")) as mock_continue,
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=(False, None)),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._committed_files", return_value={"a.py"}),
            patch("lanegate.orchestrate._run_static_analysis", return_value=[]),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete),
            patch("lanegate.lifecycle.cmd_review"),
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_rebase_fix.assert_called_once()
        assert mock_rebase_fix.call_args.args[4] == "## Conflict resolution required\n\n### a.py"
        assert mock_rebase_fix.call_args.kwargs["pool_name"] is None
        mock_continue.assert_not_called()

    def test_hibernated_conflict_executor_failure_aborts_rebase(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "hibernated", touches=["a.py"])

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch(
                "lanegate.orchestrate._run_rebase",
                return_value=("conflict", "## Conflict resolution required\n\n### a.py"),
            ),
            patch("lanegate.orchestrate._conflicted_files", return_value=["a.py"]),
            patch("lanegate.orchestrate.invoke_executor", return_value=(7, "", "")),
            patch("lanegate.orchestrate.autofix.run_rebase_fix_agent", return_value=False),
            patch("lanegate.orchestrate._abort_rebase") as mock_abort,
            patch("lanegate.lifecycle.cmd_needs_review") as mock_needs_review,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_abort.assert_called_once()
        mock_needs_review.assert_called_once()

    def test_worktree_is_dirty_true_for_tracked_changes(self, tmp_path):
        with patch(
            "lanegate.orchestrate.subprocess.run",
            return_value=subprocess.CompletedProcess(["git"], 0, stdout=" M foo.py\n", stderr=""),
        ):
            assert _worktree_is_dirty(tmp_path) is True

    def test_worktree_is_dirty_false_for_untracked_only(self, tmp_path):
        with patch(
            "lanegate.orchestrate.subprocess.run",
            return_value=subprocess.CompletedProcess(["git"], 0, stdout="?? new.py\n", stderr=""),
        ):
            assert _worktree_is_dirty(tmp_path) is False

    def test_worktree_is_dirty_false_when_clean(self, tmp_path):
        with patch(
            "lanegate.orchestrate.subprocess.run",
            return_value=subprocess.CompletedProcess(["git"], 0, stdout="", stderr=""),
        ):
            assert _worktree_is_dirty(tmp_path) is False

    def test_orchestrate_hibernated_dirty_worktree(self, tmp_path):
        """A dirty worktree from an interrupted prior run must skip the rebase
        check and resume the executor, not force-fail into needs_review."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "hibernated", touches=["a.py"])
        captured_bodies = []

        def fake_invoke(ticket, cfg_, worktree_path, **kwargs):
            captured_bodies.append(ticket["_body"])
            return (0, "", "")

        def fake_complete(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: hibernated", "status: code_complete"))

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate._worktree_is_dirty", return_value=True) as mock_dirty,
            patch("lanegate.orchestrate._run_rebase") as mock_rebase,
            patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=(False, None)),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._committed_files", return_value={"a.py"}),
            patch("lanegate.orchestrate._run_static_analysis", return_value=[]),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete),
            patch("lanegate.lifecycle.cmd_review"),
            patch("lanegate.lifecycle.cmd_needs_review") as mock_needs_review,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_dirty.assert_called_once()
        mock_rebase.assert_not_called()
        mock_needs_review.assert_not_called()
        assert captured_bodies
        assert "Resuming with pending uncommitted changes" in captured_bodies[0]

    def test_ready_to_merge_red_lane_ticket_with_matching_approval_sha_skips_escalation(self, tmp_path):
        """A red-lane ticket already approved by a human at the current HEAD sha
        must not be re-escalated by the pre-merge risk-lane re-scan."""
        from types import SimpleNamespace

        cfg = _default_cfg(tmp_path)
        cfg["autonomy"] = "full"
        tickets_dir = tmp_path / "tickets"
        path = _write_ticket(tickets_dir, "TICK-001", "in_review", touches=["a.py"])
        path.write_text(
            path.read_text().replace(
                "close_criteria:",
                "review_verdict: approved\nred_lane_approved_at_sha: deadbeef1234\nclose_criteria:",
            )
        )
        (tmp_path / "worktrees" / "tick-001").mkdir()

        def fake_git_text(argv, *_args, **_kwargs):
            if argv[:2] == ["git", "diff"]:
                return SimpleNamespace(ok=True, text="+ token = 'sk-red-lane'")
            if argv[:3] == ["git", "rev-parse", "HEAD"]:
                return SimpleNamespace(ok=True, text="deadbeef1234")
            return SimpleNamespace(ok=True, text="")

        def fake_merge(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_review", "status: merged"))

        with (
            patch(
                "lanegate.orchestrate.loop._git_text",
                side_effect=fake_git_text,
            ),
            patch("lanegate.orchestrate.loop.scan_risk_lane", return_value="red"),
            patch("lanegate.lifecycle.cmd_merge", side_effect=fake_merge) as mock_merge,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
            patch("lanegate.orchestrate.spawn_watch_daemon"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_merge.assert_called_once()
        ticket = parse_ticket(path)
        assert ticket["status"] == "merged"


class TestRecoverScopeOnlyNeedsReviewNotesExemption:
    """TICK-651: .lanegate/notes/ writes are exempt from scope-drift, including
    in the auto_claim_touches recovery path (recover_scope_only_needs_review_tickets)."""

    def test_notes_only_drift_is_not_auto_claimed(self, tmp_path, capsys):
        """A ticket hibernated only over an (exempt) notes file is skipped for
        recovery rather than having the notes file auto-claimed into touches —
        the live diff no longer matches the recorded scope-drift reason once
        the notes file is filtered out, so auto-claim correctly defers instead
        of re-declaring a file the exemption says shouldn't need declaring."""
        cfg = _default_cfg(tmp_path)
        cfg["auto_claim_touches"] = True
        tickets_dir = Path(cfg["tickets_dir"])
        wt = tmp_path / "worktrees" / "tick-900"
        wt.mkdir(parents=True)
        (tickets_dir / "TICK-900.md").write_text(
            "---\n"
            "id: TICK-900\n"
            "title: Test TICK-900\n"
            "status: needs_review\n"
            "priority: 1\n"
            "parallel_safe: true\n"
            "touches:\n  - a.py\n"
            f"worktree: {wt}\n"
            "close_criteria: All tests pass.\n"
            "---\n"
            "Body.\n\n"
            "## Needs Review Reason\n"
            "committed files outside touches list: .lanegate/notes/global.md\n"
        )

        with patch(
            "lanegate.orchestrate.loop._committed_files",
            return_value={"a.py", ".lanegate/notes/global.md"},
        ):
            recovered = recover_scope_only_needs_review_tickets(cfg, tmp_path)

        assert recovered == []
        err = capsys.readouterr().err
        assert "scope recovery skipped" in err


