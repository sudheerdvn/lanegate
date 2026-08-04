"""
Tests for lanegate/orchestrate/loop.py — board-clearing loop, dry runs, milestone filter.

Split out of the former monolithic tests/test_orchestrate.py (TICK-316).
"""

from __future__ import annotations

from tests.orchestrate.conftest import *  # noqa: F401,F403


class TestConflictAwareResume:
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
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
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
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
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


# ---------------------------------------------------------------------------
# _build_executor_cmd
# ---------------------------------------------------------------------------


class TestSpawnWatchDaemon:
    def test_spawns_watch_subprocess(self, tmp_path, capsys):
        fake_proc = MagicMock()
        fake_proc.pid = 12345

        with patch("lanegate.orchestrate.subprocess.Popen") as mock_popen:
            mock_popen.return_value = fake_proc
            spawn_watch_daemon(tmp_path)

        assert mock_popen.called
        call_args = mock_popen.call_args
        cmd = call_args[0][0]
        assert "watch" in cmd

        captured = capsys.readouterr()
        assert "watch" in captured.out
        assert "12345" in captured.out


# ---------------------------------------------------------------------------
# cmd_orchestrate — dry-run
# ---------------------------------------------------------------------------


class TestCmdOrchestrateDryRun:
    def test_dry_run_prints_planned_actions_without_executing(self, tmp_path, capsys):
        cfg = _default_cfg(tmp_path)
        cfg["reviewer"] = "claude-process"
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"])

        # cmd_start/cmd_complete are imported locally inside _drain_loop, so patch
        # them at their source module.
        with (
            patch("lanegate.lifecycle.cmd_start") as mock_start,
            patch("lanegate.orchestrate.invoke_executor") as mock_exec,
            patch("lanegate.lifecycle.cmd_complete") as mock_complete,
        ):
            cmd_orchestrate(cfg, tmp_path, dry_run=True, all_milestones=True)

        # Nothing should have been called (dry-run path returns before them)
        mock_start.assert_not_called()
        mock_exec.assert_not_called()
        mock_complete.assert_not_called()

        captured = capsys.readouterr()
        assert "dry-run" in captured.out
        assert "TICK-001" in captured.out
        assert "implement_executor=claude" in captured.out
        assert "review_executor=claude" in captured.out
        assert "mode=combined" in captured.out

    def test_stored_resume_watch_args_round_trip_through_real_cli_parser(self, tmp_path, capsys):
        """F39 regression: cmd_orchestrate hands resume-watch a hand-written flag
        list to replay on retry (store_orchestrate_args). That list previously
        drifted from the real argparse flag names (`--max-parallel` was stored,
        but the actual flag is `--max`), which would make every resume-watch
        retry fail argparse with "unrecognized arguments" and exit immediately —
        silently defeating the whole point of the fix, with no test noticing
        because the two sides were never checked against each other.

        This exercises the real cmd_orchestrate call (capturing exactly what it
        passes to store_orchestrate_args) and then feeds that exact list back
        through the real CLI parser, so a future rename on either side fails
        this test instead of silently breaking resume-watch's retry."""
        cfg = _default_cfg(tmp_path)
        cfg["pools"] = {"claude-pool": {"executors": ["claude-a"], "strategy": "least-loaded"}}
        _write_ticket(tmp_path / "tickets", "TICK-001", "open", touches=["a.py"])

        captured_args: list[list[str]] = []
        with (
            patch(
                "lanegate.resume_watch.store_orchestrate_args",
                side_effect=lambda repo_root, args: captured_args.append(args),
            ),
        ):
            cmd_orchestrate(
                cfg,
                tmp_path,
                dry_run=True,
                max_parallel=3,
                human_review="per_ticket",
                milestone="v2",
                auto_analyze=False,
                recover=False,
                verbose=True,
                pool="claude-pool",
            )
        capsys.readouterr()  # discard the dry-run print output

        assert len(captured_args) == 1
        stored_args = captured_args[0]
        assert "--pool" in stored_args and "claude-pool" in stored_args

        # Feed the exact stored args back through the real CLI parser.
        from lanegate import cli

        with (
            patch("lanegate.cli.find_repo_root", return_value=tmp_path),
            patch("lanegate.cli.load_config", return_value=cfg),
            patch("lanegate.orchestrate.cmd_orchestrate") as mock_orchestrate,
            patch("sys.argv", ["lanegate", "orchestrate", *stored_args]),
        ):
            cli.main()

        mock_orchestrate.assert_called_once()
        _, kwargs = mock_orchestrate.call_args
        assert kwargs["max_parallel"] == 3
        assert kwargs["human_review"] == "per_ticket"
        assert kwargs["milestone"] == "v2"
        assert kwargs["pool"] == "claude-pool"
        assert kwargs["auto_analyze"] is False
        assert kwargs["recover"] is False
        assert kwargs["verbose"] is True

    def test_dry_run_prints_resolved_split_route(self, tmp_path, capsys):
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude"
        cfg["reviewer"] = "claude"
        cfg["executor_steps"] = {"implement": "codex", "review": "claude"}
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"])

        cmd_orchestrate(cfg, tmp_path, dry_run=True, all_milestones=True)

        captured = capsys.readouterr()
        assert "implement_executor=codex" in captured.out
        assert "review_executor=claude" in captured.out
        assert "mode=split" in captured.out

    def test_dry_run_skips_orchestrator_lock(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"])

        with (
            patch("lanegate.orchestrate.acquire_orchestrator_lock") as mock_lock,
            patch("lanegate.orchestrate.release_orchestrator_lock") as mock_release,
        ):
            cmd_orchestrate(cfg, tmp_path, dry_run=True, all_milestones=True)

        mock_lock.assert_not_called()
        mock_release.assert_not_called()


# ---------------------------------------------------------------------------
# cmd_orchestrate — board clear
# ---------------------------------------------------------------------------


class TestCmdOrchestrateEmptyBoard:
    def test_exits_cleanly_when_no_open_tickets(self, tmp_path, capsys):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        # Only merged tickets
        _write_ticket(tickets_dir, "TICK-001", "merged")

        with (
            patch("lanegate.orchestrate.acquire_orchestrator_lock") as mock_lock,
            patch("lanegate.orchestrate.release_orchestrator_lock") as mock_release,
        ):
            mock_lock.return_value = 9999
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        captured = capsys.readouterr()
        assert "board clear" in captured.out or "no more" in captured.out
        mock_lock.assert_called_once()
        mock_release.assert_called_once()


def test_orchestrate_can_run_twice_after_generated_ticket_writes_without_manual_commit(
    tmp_path, capsys
):
    if shutil.which("git") is None:
        pytest.skip("git is required for dirty-main preflight regression test")

    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)

    cfg = _default_cfg(tmp_path)
    cfg["tickets_dir"] = "tickets"
    cfg["worktrees_dir"] = "worktrees"
    cfg["commit_status_changes"] = False
    tickets_dir = tmp_path / "tickets"
    ticket_path = _write_ticket(tickets_dir, "TICK-001", "in_progress", touches=["a.py"])
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    def generated_writes(cfg_, repo_root, **kwargs):
        from lanegate.lifecycle import cmd_needs_review
        from lanegate.orchestrate import _run_acceptance_contract_audit

        ticket = parse_ticket(ticket_path)
        _run_acceptance_contract_audit(ticket, repo_root, cfg_)
        cmd_needs_review("TICK-001", cfg_, repo_root, reason="executor stopped")

    with (
        patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
        patch("lanegate.orchestrate.release_orchestrator_lock"),
        patch("lanegate.orchestrate._drain_loop", side_effect=[generated_writes, None]),
    ):
        cmd_orchestrate(cfg, tmp_path, all_milestones=True, auto_analyze=False)
        cmd_orchestrate(cfg, tmp_path, all_milestones=True, auto_analyze=False)

    captured = capsys.readouterr()
    assert "main branch has uncommitted changes" not in captured.err
    # With commit_status_changes=False, the ticket file should be left dirty (not committed)
    # and orchestrate should still work (it ignores ticket/worktree dirs in the uncommitted check)
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", "tickets"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert " M tickets/TICK-001.md" in dirty.stdout, "ticket should be dirty when commit_status_changes=False"


# ---------------------------------------------------------------------------
# cmd_orchestrate — lock acquired/released
# ---------------------------------------------------------------------------


class TestCmdOrchestrateLock:
    def test_lock_acquired_and_released_on_normal_exit(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        # No tickets — loop exits immediately

        with (
            patch("lanegate.orchestrate.acquire_orchestrator_lock") as mock_acquire,
            patch("lanegate.orchestrate.release_orchestrator_lock") as mock_release,
        ):
            mock_acquire.return_value = 9999
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_acquire.assert_called_once_with(tmp_path)
        mock_release.assert_called_once_with(tmp_path)

    def test_lock_released_on_exception(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"])

        with (
            patch("lanegate.orchestrate.acquire_orchestrator_lock") as mock_acquire,
            patch("lanegate.orchestrate.release_orchestrator_lock") as mock_release,
            patch("lanegate.orchestrate._drain_loop", side_effect=RuntimeError("boom")),
        ):
            mock_acquire.return_value = 9999
            with pytest.raises(RuntimeError, match="boom"):
                cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_release.assert_called_once()

    def test_lock_error_exits_with_error(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        from lanegate.concurrency import OrchestratorLockError

        with patch(
            "lanegate.orchestrate.acquire_orchestrator_lock",
            side_effect=OrchestratorLockError("already held"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# cmd_orchestrate — watch daemon spawned for in_review tickets
# ---------------------------------------------------------------------------


class TestCmdOrchestrateWatchDaemon:
    def test_spawns_watch_when_in_review_tickets_with_pr(self, tmp_path, capsys):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        # Board has no open tickets but there's an in_review ticket with a PR
        _write_ticket(tickets_dir, "TICK-001", "in_review", pr_number=42, branch="tick-001")

        with (
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
            patch("lanegate.orchestrate.spawn_watch_daemon") as mock_spawn,
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_spawn.assert_called_once_with(tmp_path)

    def test_no_watch_when_in_review_tickets_lack_pr(self, tmp_path, capsys):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        # in_review but no pr_number
        _write_ticket(tickets_dir, "TICK-001", "in_review")

        with (
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
            patch("lanegate.orchestrate.spawn_watch_daemon") as mock_spawn,
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_spawn.assert_not_called()
        captured = capsys.readouterr()
        assert "[orchestrate] review queue:" in captured.out
        assert "TICK-001: pending verdict" in captured.out
        assert "next: lanegate review TICK-001 --verdict approved" in captured.out

    def test_no_human_review_auto_merges_approved_local_ticket(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        cfg["autonomy"] = "full"
        tickets_dir = tmp_path / "tickets"
        (tickets_dir / "TICK-001.md").write_text(
            "---\n"
            "id: TICK-001\n"
            "title: Test TICK-001\n"
            "status: in_review\n"
            "priority: 5\n"
            "parallel_safe: true\n"
            "review_verdict: approved\n"
            "review_summary: Ready for merge\n"
            "---\n"
            "Body.\n"
        )

        def fake_merge(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            text = p.read_text().replace("status: in_review", "status: merged")
            p.write_text(text)

        with (
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
            patch("lanegate.lifecycle.cmd_merge", side_effect=fake_merge) as mock_merge,
            patch("lanegate.orchestrate.spawn_watch_daemon") as mock_spawn,
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_merge.assert_called_once_with("TICK-001", cfg, tmp_path)
        mock_spawn.assert_not_called()

    def test_merge_failure_does_not_crash_orchestrate_run(self, tmp_path, capsys):
        """A merge failure in auto_merge_approved_local_tickets must not crash the
        whole run.  The failing ticket is downgraded to needs_review and
        independent mergeable tickets continue processing."""
        from lanegate.lifecycle import MergeFailedError

        cfg = _default_cfg(tmp_path)
        cfg["autonomy"] = "full"
        tickets_dir = tmp_path / "tickets"

        # TICK-001: approved, will fail to merge
        (tickets_dir / "TICK-001.md").write_text(
            "---\n"
            "id: TICK-001\n"
            "title: Failing merge\n"
            "status: in_review\n"
            "priority: 5\n"
            "parallel_safe: true\n"
            "review_verdict: approved\n"
            "review_summary: Ready\n"
            "touches:\n"
            "  - a.py\n"
            "---\n"
            "Body.\n"
        )
        # TICK-002: approved, should succeed
        (tickets_dir / "TICK-002.md").write_text(
            "---\n"
            "id: TICK-002\n"
            "title: Good merge\n"
            "status: in_review\n"
            "priority: 5\n"
            "parallel_safe: true\n"
            "review_verdict: approved\n"
            "review_summary: Ready\n"
            "touches:\n"
            "  - b.py\n"
            "---\n"
            "Body.\n"
        )

        merged = []

        def fake_merge(tid, cfg_, repo_root_):
            if tid == "TICK-001":
                raise MergeFailedError("dirty main — merge conflict")
            # TICK-002 succeeds: advance ticket status on disk so the loop sees it merged
            p = tickets_dir / f"{tid}.md"
            text = p.read_text().replace("status: in_review", "status: merged")
            p.write_text(text)
            merged.append(tid)

        with (
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
            patch("lanegate.lifecycle.cmd_merge", side_effect=fake_merge),
            patch("lanegate.orchestrate.spawn_watch_daemon"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        captured = capsys.readouterr()

        # TICK-002 should have been merged successfully
        assert "TICK-002" in merged, "TICK-002 should have been merged despite TICK-001 failure"
        # TICK-001 must not have been merged
        assert "TICK-001" not in merged

        # TICK-001 must be downgraded to needs_review on disk
        tick1_text = (tickets_dir / "TICK-001.md").read_text()
        assert "status: needs_review" in tick1_text, "TICK-001 should be downgraded to needs_review"

        # Failure must be logged to stderr
        assert "TICK-001" in captured.err
        assert "merge failed" in captured.err

    def test_crash_after_merge_does_not_regress_merged_ticket(self, tmp_path, capsys):
        """A crash after a successful merge must not regress the merged ticket
        back to needs_review. The ticket should remain in merged status (F16).

        Scenario: During run_ticket execution, auto_merge_approved_local_tickets
        is called at the end and succeeds in merging the ticket. However, an
        exception is raised in the same run_ticket invocation after the merge.
        The worker exception handler catches this and calls pause_for_needs_review,
        but since the ticket is already merged, it should NOT be regressed.
        """
        cfg = _default_cfg(tmp_path)
        cfg["autonomy"] = "full"
        cfg["max_parallel"] = 1
        tickets_dir = tmp_path / "tickets"

        # Create two tickets: TICK-001 will crash after successful review/merge,
        # TICK-002 is independent
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"], priority=1)
        _write_ticket(tickets_dir, "TICK-002", "open", touches=["b.py"], priority=2)

        def fake_start(tid, cfg_, repo_root, **kwargs):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: open", "status: in_progress", 1))

        def fake_invoke(ticket, cfg_, wt, **kwargs):
            return (0, "", "")

        def fake_complete(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_progress", "status: code_complete", 1))

        def fake_review(tid, cfg_, repo_root, *, verdict=None, summary=None, findings=None):
            p = tickets_dir / f"{tid}.md"
            text = p.read_text().replace("status: code_complete", "status: in_review", 1)
            if "review_verdict:" not in text:
                text = text.replace(f"id: {tid}\n", f"id: {tid}\nreview_verdict: approved\n")
            p.write_text(text)

        def fake_merge(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_review", "status: merged", 1))

        merge_occurred = {"TICK-001": False}
        original_merge = fake_merge

        def fake_merge_with_crash(tid, cfg_, repo_root):
            original_merge(tid, cfg_, repo_root)
            # After successfully merging TICK-001, simulate a crash
            if tid == "TICK-001":
                merge_occurred["TICK-001"] = True
                # Crash occurs after the merge has updated the status to merged
                raise RuntimeError("simulated post-merge crash in worker thread")

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=fake_start),
            patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke),
            patch("lanegate.orchestrate._is_rate_limit", return_value=False),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._committed_files", return_value=set()),
            patch("lanegate.orchestrate._run_static_analysis", return_value=[]),
            patch("lanegate.orchestrate._run_acceptance_contract_audit", return_value=[]),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.orchestrate.run_review_agent", side_effect=lambda ticket, repo_root, **kw: (fake_review(ticket["id"], cfg, repo_root), True)[1]),
            patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete),
            patch("lanegate.lifecycle.cmd_review", side_effect=fake_review),
            patch("lanegate.lifecycle.cmd_merge", side_effect=fake_merge_with_crash),
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            # Must not raise or crash despite the post-merge exception
            cmd_orchestrate(
                cfg,
                tmp_path,
                max_parallel=1,
                human_review="none",
                all_milestones=True,
                auto_analyze=False,
            )

        captured = capsys.readouterr()

        # TICK-001 must remain in merged status, not regressed to needs_review
        # This is the key assertion: even though an exception occurred during
        # TICK-001's execution after it was merged, pause_for_needs_review
        # should NOT regress it back to needs_review.
        tick1_text = (tickets_dir / "TICK-001.md").read_text()
        assert "status: merged" in tick1_text, (
            "TICK-001 should remain in merged status (F16 fix: no regression of terminal statuses)"
        )
        assert "status: needs_review" not in tick1_text, (
            "TICK-001 should NOT be regressed to needs_review"
        )

        # The first ticket's post-merge error is contained, so the independent
        # ticket still completes normally.
        tick2_text = (tickets_dir / "TICK-002.md").read_text()
        assert "status: merged" in tick2_text

        # The post-merge error must be logged without regressing TICK-001.
        assert "TICK-001" in captured.err
        assert "post-merge crash" in captured.err
        # The fix should log "terminal status" or "skipping needs_review regression"
        # when pause_for_needs_review tries to regress TICK-001
        assert "terminal status" in captured.err or "skipping needs_review regression" in captured.err

    def test_blocked_lock_holder_reports_status_specific_next_step(self, tmp_path, capsys):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "code_complete", touches=["shared.py"])
        _write_ticket(tickets_dir, "TICK-002", "open", touches=["shared.py"])

        with (
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True, auto_analyze=False)

        captured = capsys.readouterr()
        assert "TICK-002" in captured.out
        assert "file lock held by: TICK-001 (code_complete)" in captured.out
        assert "TICK-001: code_complete - next: lanegate review TICK-001 --verdict approved" in captured.out
        assert "Run: lanegate merge TICK-001" not in captured.out

    def test_blocked_lock_holder_reports_wildcard_holder_for_concrete_ticket(self, tmp_path, capsys):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "code_complete", touches=['"*"'])
        _write_ticket(tickets_dir, "TICK-002", "open", touches=["concrete.py"])

        with (
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True, auto_analyze=False)

        captured = capsys.readouterr()
        assert "TICK-002" in captured.out
        assert "file lock held by: TICK-001 (code_complete)" in captured.out

    def test_hollow_code_complete_lock_holder_is_ignored_and_flagged(self, tmp_path, capsys):
        """A code_complete ticket whose branch has zero commits ahead of main
        (e.g. a hand-edited status, as happened to TICK-048/TICK-156 in this
        project's own board) must not lock out the rest of the board, and
        must be surfaced loudly instead of silently trusted forever."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        wt_path = tmp_path / "worktrees" / "tick-001"
        (tickets_dir / "TICK-001.md").write_text(
            "---\n"
            "id: TICK-001\n"
            "title: Test TICK-001\n"
            "status: code_complete\n"
            "priority: 1\n"
            "parallel_safe: true\n"
            f"worktree: {wt_path}\n"
            "branch: tick-001\n"
            "touches:\n  - shared.py\n"
            "---\nBody.\n"
        )
        # TICK-003 holds a *real* lock on other.py so TICK-002 stays blocked
        # for a reason unrelated to TICK-001's hollow lock on shared.py.
        _write_ticket(tickets_dir, "TICK-003", "code_complete", touches=["other.py"])
        _write_ticket(tickets_dir, "TICK-002", "open", touches=["other.py"])

        from lanegate.reviewer import ReviewError

        with (
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
            patch("lanegate.reviewer.get_worktree_diff", side_effect=ReviewError("no commits")),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True, auto_analyze=False)

        captured = capsys.readouterr()
        assert "Ignoring hollow lock holder(s)" in captured.out
        assert "TICK-001 (code_complete)" in captured.out
        assert "file lock held by: TICK-003 (code_complete)" in captured.out
        # TICK-001 must not appear as a (false) lock holder for anything.
        assert "held by: TICK-001" not in captured.out

    def test_dry_run_prints_watch_spawn_for_in_review(self, tmp_path, capsys):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "in_review", pr_number=99, branch="tick-001")

        with patch("lanegate.orchestrate.spawn_watch_daemon") as mock_spawn:
            cmd_orchestrate(cfg, tmp_path, dry_run=True, all_milestones=True)

        mock_spawn.assert_not_called()  # dry-run never actually spawns
        captured = capsys.readouterr()
        assert "watch" in captured.out


# ---------------------------------------------------------------------------
# sibling-retry-on-rate-limit (TICK-263)
# ---------------------------------------------------------------------------


def _sibling_retry_scenario(tmp_path, *, max_sibling_retries: int | None = None, pool_executors=None):
    """Shared harness: 2 (or 3, via pool_executors) instance pool, one open
    ticket. Returns (cfg, tickets_dir, calls, patches-context helpers) so
    each test only needs to supply its own fake_invoke behavior."""
    cfg = _default_cfg(tmp_path)
    cfg["max_parallel"] = 1
    executors = pool_executors or ["claude-1", "claude-2"]
    cfg["executors"] = {name: {"type": "claude-process"} for name in executors}
    cfg["pools"] = {"default": {"executors": executors, "strategy": "round-robin"}}
    if max_sibling_retries is not None:
        cfg["max_sibling_retries"] = max_sibling_retries
    tickets_dir = tmp_path / "tickets"
    _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"], priority=1)
    return cfg, tickets_dir


class TestSiblingRetryOnRateLimit:
    def test_progress_and_healthy_sibling_resumes_and_completes(self, tmp_path):
        """(a) A rate limit with clear in-worktree progress and a healthy
        sibling resumes the SAME ticket on that sibling within the run and
        carries it through to completion, instead of hibernating."""
        cfg, tickets_dir = _sibling_retry_scenario(tmp_path)

        calls: list[str | None] = []

        def fake_invoke(ticket, cfg_, wt, **kwargs):
            instance = kwargs.get("executor_override")
            calls.append(instance)
            if instance == "claude-1":
                return 1, "", "You've hit your session limit · resets 5:10pm"
            return 0, "", ""

        def fake_complete(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_progress", "status: code_complete", 1))

        def fake_review(tid, cfg_, repo_root, *, verdict=None, summary=None, findings=None):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: code_complete", "status: in_review", 1))

        from lanegate.executor import is_cooling_down

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=_fake_start_writes_in_progress),
            patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke),
            patch("lanegate.orchestrate._ticket_has_real_progress", return_value=True),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._committed_files", return_value=set()),
            patch("lanegate.orchestrate._run_static_analysis", return_value=[]),
            patch("lanegate.orchestrate._run_acceptance_contract_audit", return_value=[]),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete),
            patch("lanegate.lifecycle.cmd_review", side_effect=fake_review),
            patch("lanegate.lifecycle.cmd_hibernate") as mock_hibernate,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(
                cfg,
                tmp_path,
                max_parallel=1,
                human_review="none",
                all_milestones=True,
                auto_analyze=False,
                pool="default",
            )

        # Retried on the sibling and carried the ticket through completion +
        # review within this same run — never hibernated.
        assert calls == ["claude-1", "claude-2"]
        assert "status: in_review" in (tickets_dir / "TICK-001.md").read_text()
        mock_hibernate.assert_not_called()
        # The originally-hit instance is still marked cooling down even
        # though the ticket itself completed on the sibling.
        assert is_cooling_down(tmp_path, "claude-1") is True
        assert is_cooling_down(tmp_path, "claude-2") is False

    def test_no_progress_still_hibernates_despite_healthy_sibling(self, tmp_path):
        """(b) A rate limit with no in-worktree progress still hibernates,
        even though a healthy sibling instance is available — preserves
        today's safety behavior for a hung/looping session (TICK-258)."""
        cfg, tickets_dir = _sibling_retry_scenario(tmp_path)

        calls: list[str | None] = []

        def fake_invoke(ticket, cfg_, wt, **kwargs):
            calls.append(kwargs.get("executor_override"))
            return 1, "", "You've hit your session limit · resets 5:10pm"

        def fake_hibernate(tid, cfg_, repo_root, **kwargs):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_progress", "status: hibernated", 1))

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=_fake_start_writes_in_progress),
            patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke),
            patch("lanegate.orchestrate._ticket_has_real_progress", return_value=False),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.lifecycle.cmd_hibernate", side_effect=fake_hibernate) as mock_hibernate,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(
                cfg,
                tmp_path,
                max_parallel=1,
                human_review="none",
                all_milestones=True,
                auto_analyze=False,
                pool="default",
            )

        # No retry dispatched — only the original attempt.
        assert calls == ["claude-1"]
        assert "status: hibernated" in (tickets_dir / "TICK-001.md").read_text()
        mock_hibernate.assert_called_once()

    def test_retry_cap_of_zero_disables_sibling_retry(self, tmp_path):
        """(c) The per-ticket retry cap (max_sibling_retries) is enforced —
        with it set to 0, a ticket with progress and a healthy sibling still
        does not retry, proving the cap actually gates the retry path rather
        than something else silently limiting it to one attempt."""
        cfg, tickets_dir = _sibling_retry_scenario(
            tmp_path, max_sibling_retries=0, pool_executors=["claude-1", "claude-2", "claude-3"]
        )

        calls: list[str | None] = []

        def fake_invoke(ticket, cfg_, wt, **kwargs):
            calls.append(kwargs.get("executor_override"))
            return 1, "", "You've hit your session limit · resets 5:10pm"

        def fake_hibernate(tid, cfg_, repo_root, **kwargs):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_progress", "status: hibernated", 1))

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=_fake_start_writes_in_progress),
            patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke),
            patch("lanegate.orchestrate._ticket_has_real_progress", return_value=True),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.lifecycle.cmd_hibernate", side_effect=fake_hibernate) as mock_hibernate,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(
                cfg,
                tmp_path,
                max_parallel=1,
                human_review="none",
                all_milestones=True,
                auto_analyze=False,
                pool="default",
            )

        assert calls == ["claude-1"]
        assert "status: hibernated" in (tickets_dir / "TICK-001.md").read_text()
        mock_hibernate.assert_called_once()


# ---------------------------------------------------------------------------
# Milestone — cmd_orchestrate error, --all bypass, next_batch filtering
# ---------------------------------------------------------------------------


def _write_milestone_ticket(
    tickets_dir: Path,
    ticket_id: str,
    status: str,
    milestone: str | None = None,
    touches: list[str] | None = None,
    priority: int = 1,
) -> Path:
    touches_str = ""
    if touches:
        items = "\n".join(f"  - {t}" for t in touches)
        touches_str = f"touches:\n{items}\n"
    ms_str = f"milestone: {milestone!r}\n" if milestone else ""
    content = (
        f"---\n"
        f"id: {ticket_id}\n"
        f"title: Test {ticket_id}\n"
        f"status: {status}\n"
        f"priority: {priority}\n"
        f"parallel_safe: true\n"
        f"{touches_str}"
        f"{ms_str}"
        f"close_criteria: Tests pass.\n"
        f"---\nBody.\n"
    )
    path = tickets_dir / f"{ticket_id}.md"
    path.write_text(content)
    return path


class TestOrchestrateMilestone:
    """Milestone resolution and filtering in cmd_orchestrate and next_batch."""

    def test_no_milestone_no_default_exits_with_error(self, tmp_path, capsys):
        """cmd_orchestrate without --milestone or default_milestone exits 1 with a clear error."""
        cfg = _default_cfg(tmp_path)
        # Ensure no default_milestone in cfg
        cfg.pop("default_milestone", None)

        with pytest.raises(SystemExit) as exc_info:
            cmd_orchestrate(cfg, tmp_path)

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "milestone" in err.lower()
        assert "--all" in err

    def test_no_milestone_no_default_explicit_cfg_none_exits(self, tmp_path, capsys):
        """cfg with default_milestone=None also triggers the error."""
        cfg = _default_cfg(tmp_path)
        cfg["default_milestone"] = None

        with pytest.raises(SystemExit) as exc_info:
            cmd_orchestrate(cfg, tmp_path)

        assert exc_info.value.code == 1

    def test_all_flag_bypasses_milestone_requirement(self, tmp_path, capsys):
        """--all processes tickets across all milestones regardless of default_milestone."""
        cfg = _default_cfg(tmp_path)
        cfg.pop("default_milestone", None)
        tickets_dir = tmp_path / "tickets"
        _write_milestone_ticket(tickets_dir, "TICK-001", "open", milestone="v1", touches=["a.py"])

        with (
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", return_value=(0, "", "")),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.lifecycle.cmd_complete") as mock_complete,
            patch("lanegate.lifecycle.cmd_review"),
        ):
            # After complete, change status so loop exits
            def fake_complete(tid, cfg_, repo_root):
                p = tickets_dir / f"{tid}.md"
                text = p.read_text().replace("status: open", "status: code_complete")
                p.write_text(text)

            mock_complete.side_effect = fake_complete
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        # Should have run without error
        mock_complete.assert_called_once()

    def test_default_milestone_from_config_used(self, tmp_path, capsys):
        """default_milestone in cfg is used when no explicit --milestone flag."""
        cfg = _default_cfg(tmp_path)
        cfg["default_milestone"] = "v1"
        tickets_dir = tmp_path / "tickets"
        _write_milestone_ticket(tickets_dir, "TICK-001", "open", milestone="v1", touches=["a.py"])
        _write_milestone_ticket(tickets_dir, "TICK-002", "open", milestone="v2", touches=["b.py"])

        started_ids = []

        def fake_start(tid, cfg_, repo_root, **kwargs):
            started_ids.append(tid)

        def fake_complete(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            text = p.read_text().replace("status: open", "status: code_complete")
            p.write_text(text)

        with (
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
            patch("lanegate.lifecycle.cmd_start", side_effect=fake_start),
            patch("lanegate.orchestrate.invoke_executor", return_value=(0, "", "")),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete),
            patch("lanegate.lifecycle.cmd_review"),
        ):
            cmd_orchestrate(cfg, tmp_path)

        # Only v1 ticket should have been started
        assert "TICK-001" in started_ids
        assert "TICK-002" not in started_ids

    def test_explicit_milestone_flag_overrides_default(self, tmp_path):
        """--milestone flag takes precedence over default_milestone."""
        cfg = _default_cfg(tmp_path)
        cfg["default_milestone"] = "v1"
        tickets_dir = tmp_path / "tickets"
        _write_milestone_ticket(tickets_dir, "TICK-001", "open", milestone="v1", touches=["a.py"])
        _write_milestone_ticket(tickets_dir, "TICK-002", "open", milestone="v2", touches=["b.py"])

        started_ids = []

        def fake_start(tid, cfg_, repo_root, **kwargs):
            started_ids.append(tid)

        def fake_complete(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            text = p.read_text().replace("status: open", "status: code_complete")
            p.write_text(text)

        with (
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
            patch("lanegate.lifecycle.cmd_start", side_effect=fake_start),
            patch("lanegate.orchestrate.invoke_executor", return_value=(0, "", "")),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete),
            patch("lanegate.lifecycle.cmd_review"),
        ):
            cmd_orchestrate(cfg, tmp_path, milestone="v2")

        assert "TICK-002" in started_ids
        assert "TICK-001" not in started_ids

    def test_milestone_logged_at_startup(self, tmp_path, capsys):
        """Active milestone is logged at orchestrate startup."""
        cfg = _default_cfg(tmp_path)
        cfg["default_milestone"] = "v1"

        with (
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path)

        out = capsys.readouterr().out
        assert "v1" in out
        assert "milestone" in out.lower()

    def test_next_batch_milestone_filter(self, tmp_path):
        """next_batch with milestone only returns tickets matching that milestone."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_milestone_ticket(tickets_dir, "TICK-001", "open", milestone="v1", touches=["a.py"])
        _write_milestone_ticket(tickets_dir, "TICK-002", "open", milestone="v2", touches=["b.py"])

        result = next_batch(cfg, tmp_path, milestone="v1")
        assert len(result) == 1
        assert result[0]["id"] == "TICK-001"

    def test_next_batch_no_milestone_returns_all(self, tmp_path):
        """next_batch without milestone returns all open tickets."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_milestone_ticket(tickets_dir, "TICK-001", "open", milestone="v1", touches=["a.py"])
        _write_milestone_ticket(tickets_dir, "TICK-002", "open", milestone="v2", touches=["b.py"])

        result = next_batch(cfg, tmp_path)
        ids = {t["id"] for t in result}
        assert "TICK-001" in ids
        assert "TICK-002" in ids

    def test_milestone_filter_warns_about_near_miss_values(self, tmp_path, capsys):
        """cmd_orchestrate with milestone filter warns about near-miss milestone values.

        For example, when running with --milestone v1.5, tickets with milestone='1.5'
        (missing v prefix) should be warned about instead of silently skipped.
        """
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        # Create tickets with different milestone values
        _write_milestone_ticket(tickets_dir, "TICK-001", "open", milestone="v1.5", touches=["a.py"])
        _write_milestone_ticket(tickets_dir, "TICK-158", "open", milestone="1.5", touches=["b.py"])
        _write_milestone_ticket(tickets_dir, "TICK-159", "open", milestone="1.5", touches=["c.py"])
        _write_milestone_ticket(tickets_dir, "TICK-002", "open", milestone="v2", touches=["d.py"])

        with (
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, milestone="v1.5", dry_run=True)

        err = capsys.readouterr().err
        # Should warn about the near-miss tickets
        assert "WARNING" in err
        assert "1.5" in err
        assert "v1.5" in err
        assert "TICK-158" in err or "TICK-159" in err  # At least one of the near-miss tickets mentioned


class TestOrchestrateTicketsFlag:
    """TICK-262: --tickets restricts orchestrate to an explicit ticket list."""

    def test_next_batch_ticket_ids_restricts_candidates(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"])
        _write_ticket(tickets_dir, "TICK-002", "open", touches=["b.py"])
        _write_ticket(tickets_dir, "TICK-003", "open", touches=["c.py"])

        result = next_batch(cfg, tmp_path, ticket_ids={"TICK-002"})
        assert [t["id"] for t in result] == ["TICK-002"]

    def test_next_batch_ticket_ids_composes_with_milestone(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_milestone_ticket(tickets_dir, "TICK-001", "open", milestone="v1", touches=["a.py"])
        _write_milestone_ticket(tickets_dir, "TICK-002", "open", milestone="v2", touches=["b.py"])

        # Named but wrong milestone -> excluded; both filters must pass.
        result = next_batch(cfg, tmp_path, milestone="v1", ticket_ids={"TICK-001", "TICK-002"})
        assert [t["id"] for t in result] == ["TICK-001"]

    def test_next_batch_ticket_ids_still_respects_eligibility(self, tmp_path):
        """An explicitly-listed ticket that isn't dispatchable yet is still skipped,
        not force-dispatched (locked touches in this case)."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "in_progress", touches=["shared.py"])
        _write_ticket(tickets_dir, "TICK-002", "open", touches=["shared.py"])

        result = next_batch(cfg, tmp_path, ticket_ids={"TICK-002"})
        assert result == []

    def test_orchestrate_tickets_flag_scopes_full_run_to_board_clear(self, tmp_path):
        """Regression test (TICK-262 acceptance criteria): board with 5 open
        tickets, --tickets naming 2 of them — only those 2 are ever claimed
        across a full run to board-clear."""
        cfg = _default_cfg(tmp_path)
        cfg.pop("default_milestone", None)
        tickets_dir = tmp_path / "tickets"
        for i in range(1, 6):
            _write_ticket(tickets_dir, f"TICK-00{i}", "open", touches=[f"file{i}.py"])

        started_ids: list[str] = []

        def fake_start(tid, cfg_, repo_root, **kwargs):
            started_ids.append(tid)
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: open", "status: in_progress", 1))

        def fake_complete(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_progress", "status: code_complete", 1))

        with (
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
            patch("lanegate.lifecycle.cmd_start", side_effect=fake_start),
            patch("lanegate.orchestrate.invoke_executor", return_value=(0, "", "")),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete),
            patch("lanegate.lifecycle.cmd_review"),
        ):
            cmd_orchestrate(
                cfg, tmp_path, all_milestones=True, tickets=["TICK-002", "TICK-004"]
            )

        assert set(started_ids) == {"TICK-002", "TICK-004"}

    def test_tickets_flag_composes_with_milestone(self, tmp_path):
        """--tickets and --milestone both apply — a named ticket outside the
        active milestone is not dispatched."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_milestone_ticket(tickets_dir, "TICK-001", "open", milestone="v1", touches=["a.py"])
        _write_milestone_ticket(tickets_dir, "TICK-002", "open", milestone="v2", touches=["b.py"])

        started_ids: list[str] = []

        def fake_start(tid, cfg_, repo_root, **kwargs):
            started_ids.append(tid)
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: open", "status: code_complete", 1))

        with (
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
            patch("lanegate.lifecycle.cmd_start", side_effect=fake_start),
            patch("lanegate.orchestrate.invoke_executor", return_value=(0, "", "")),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.lifecycle.cmd_complete"),
            patch("lanegate.lifecycle.cmd_review"),
        ):
            cmd_orchestrate(
                cfg, tmp_path, milestone="v1", tickets=["TICK-001", "TICK-002"]
            )

        assert started_ids == ["TICK-001"]

    def test_ticket_scope_logged_at_startup(self, tmp_path, capsys):
        """dry-run/log output makes clear a run is ticket-scoped."""
        cfg = _default_cfg(tmp_path)
        cfg.pop("default_milestone", None)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"])

        cmd_orchestrate(
            cfg, tmp_path, all_milestones=True, tickets=["TICK-001"], dry_run=True
        )

        out = capsys.readouterr().out
        assert "ticket scope" in out.lower()
        assert "TICK-001" in out

    def test_unknown_ticket_id_warns(self, tmp_path, capsys):
        cfg = _default_cfg(tmp_path)
        cfg.pop("default_milestone", None)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"])

        cmd_orchestrate(
            cfg, tmp_path, all_milestones=True, tickets=["TICK-999"], dry_run=True
        )

        err = capsys.readouterr().err
        assert "TICK-999" in err
        assert "unknown" in err.lower()


# ---------------------------------------------------------------------------
# Auto-analyze drafts
# ---------------------------------------------------------------------------


def _write_draft_ticket(
    tickets_dir: Path,
    ticket_id: str,
    milestone: str | None = None,
) -> Path:
    ms_str = f"milestone: {milestone}\n" if milestone else ""
    content = (
        f"---\n"
        f"id: {ticket_id}\n"
        f"title: Draft {ticket_id}\n"
        f"status: draft\n"
        f"priority: 1\n"
        f"parallel_safe: true\n"
        f"{ms_str}"
        f"close_criteria: TBD.\n"
        f"---\nBody.\n"
    )
    path = tickets_dir / f"{ticket_id}.md"
    path.write_text(content)
    return path


from lanegate.orchestrate import (  # noqa: E402
    _analyze_drafts,
    _print_draft_analysis_plan,
    _scan_injection_signals,
)


class TestAnalyzeDrafts:
    """Unit tests for _analyze_drafts and _print_draft_analysis_plan."""

    def test_draft_analyzed_before_dispatch(self, tmp_path, capsys):
        """_analyze_drafts calls cmd_analyze for each draft ticket."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_draft_ticket(tickets_dir, "TICK-001")

        with patch("lanegate.analyze.cmd_analyze") as mock_analyze:
            _analyze_drafts(cfg, tmp_path, tickets_dir=tickets_dir)

        mock_analyze.assert_called_once_with("TICK-001", cfg, tmp_path)

    def test_milestone_filter_respected(self, tmp_path):
        """_analyze_drafts skips drafts that do not match the active milestone."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_draft_ticket(tickets_dir, "TICK-001", milestone="v1")
        _write_draft_ticket(tickets_dir, "TICK-002", milestone="v2")

        analyzed = []

        def fake_analyze(tid, cfg_, repo_root):
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

        def fake_analyze(tid, cfg_, repo_root):
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

        def fake_analyze(tid, cfg_, repo_root):
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
        assert "TICK-001" in captured.err

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

        def fake_analyze(tid, cfg_, repo_root):
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

        def fake_analyze(tid, cfg_, repo_root):
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


class TestBoardClearingLoopAutoAnalyze:
    """Integration-level tests: auto-analyze wired into the board-clearing loop."""

    def test_next_batch_called_before_analyze_drafts(self, tmp_path):
        """next_batch is checked before _analyze_drafts runs each loop iteration —
        ready work must never wait behind draft analysis (TICK-261)."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"])

        call_order = []

        def fake_analyze_drafts(cfg_, repo_root, milestone=None, tickets_dir=None, ticket_ids=None):
            call_order.append("analyze_drafts")

        def fake_next_batch(cfg_, repo_root, milestone=None, *, exclude_touches=None, ticket_ids=None):
            call_order.append("next_batch")
            return []  # empty → exit loop

        with (
            patch("lanegate.orchestrate._analyze_drafts", side_effect=fake_analyze_drafts),
            patch("lanegate.orchestrate.next_batch", side_effect=fake_next_batch),
        ):
            from lanegate.orchestrate import _drain_loop

            _drain_loop(
                cfg, tmp_path, max_parallel=2, dry_run=False, human_review="none", auto_analyze=True
            )

        # next_batch runs first each iteration; _analyze_drafts only runs
        # (and re-checks next_batch) when the first next_batch call came back
        # empty — here it's called twice: once to discover no work, once
        # after the (no-op) draft analysis.
        assert call_order == ["next_batch", "analyze_drafts", "next_batch"]

    def test_open_ticket_dispatched_before_draft_analyzed(self, tmp_path):
        """Regression for TICK-261: with both an open and a draft ticket in
        scope, the open ticket is claimed/dispatched before the draft is
        analyzed — not after."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"])
        _write_draft_ticket(tickets_dir, "TICK-DRAFT")

        events: list[str] = []

        def fake_analyze_drafts(cfg_, repo_root, milestone=None, tickets_dir=None, ticket_ids=None):
            events.append("analyze_drafts")

        def fake_cmd_start(tid, cfg_, repo_root, **kwargs):
            events.append(f"start:{tid}")
            p = tickets_dir / f"{tid}.md"
            text = p.read_text().replace("status: open", "status: in_progress")
            p.write_text(text)

        with (
            patch("lanegate.orchestrate._analyze_drafts", side_effect=fake_analyze_drafts),
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
            patch("lanegate.lifecycle.cmd_start", side_effect=fake_cmd_start),
            patch("lanegate.orchestrate.invoke_executor", return_value=(0, "", "")),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.lifecycle.cmd_complete"),
            patch("lanegate.lifecycle.cmd_review"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True, auto_analyze=True)

        assert events, "expected TICK-001 to be started"
        assert events[0] == "start:TICK-001"
        assert "analyze_drafts" not in events[: events.index("start:TICK-001")]

    def test_dry_run_uses_print_plan_not_analyze(self, tmp_path, capsys):
        """In dry-run mode, _print_draft_analysis_plan is called instead of _analyze_drafts,
        when there is no already-dispatchable open work (an empty batch)."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_draft_ticket(tickets_dir, "TICK-DRAFT")

        with (
            patch("lanegate.orchestrate._analyze_drafts") as mock_analyze,
            patch("lanegate.orchestrate._print_draft_analysis_plan") as mock_plan,
        ):
            cmd_orchestrate(cfg, tmp_path, dry_run=True, all_milestones=True, auto_analyze=True)

        mock_analyze.assert_not_called()
        mock_plan.assert_called()

    def test_dry_run_open_ticket_takes_priority_over_draft_plan(self, tmp_path, capsys):
        """When an open ticket is already dispatchable, dry-run processes that
        batch and does not print the draft-analysis plan in the same pass
        (TICK-261 — ready work is never gated behind draft handling)."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_draft_ticket(tickets_dir, "TICK-DRAFT")
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"])

        with (
            patch("lanegate.orchestrate._analyze_drafts") as mock_analyze,
            patch("lanegate.orchestrate._print_draft_analysis_plan") as mock_plan,
        ):
            cmd_orchestrate(cfg, tmp_path, dry_run=True, all_milestones=True, auto_analyze=True)

        mock_analyze.assert_not_called()
        mock_plan.assert_not_called()
        captured = capsys.readouterr()
        assert "would start TICK-001" in captured.out


# ---------------------------------------------------------------------------


# TICK-343: combined-mode reviews get attributed to the executor that ran them
# ---------------------------------------------------------------------------


class TestCombinedModeReviewAttribution:
    def test_combined_mode_dispatch_backfills_review_driver_and_model(self, tmp_path):
        """A combined-mode agent records its verdict with `lanegate review
        --verdict`, which cannot name the reviewer — so without this the
        ticket carries a verdict nobody is accountable for.  This asserts the
        backfill is wired into the loop, not merely unit-testable."""
        from lanegate.ticket import parse_ticket

        cfg = _default_cfg(tmp_path)
        cfg["reviewer"] = cfg["executor"]
        cfg["models"] = {"implement": "combined-mode-model"}
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"])

        def fake_stream(*_args, **_kwargs):
            p = tickets_dir / "TICK-001.md"
            p.write_text(
                p.read_text().replace(
                    "status: open", "status: in_review\nreview_verdict: approved"
                )
            )
            return (0, "", "")

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.pool._stream_subprocess", side_effect=fake_stream),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._run_static_analysis", return_value=[]) as static_analysis,
            patch("lanegate.orchestrate._run_acceptance_contract_audit", return_value=[]),
            patch("lanegate.lifecycle.cmd_merge") as mock_merge,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        static_analysis.assert_called()
        # Default supervised autonomy leaves the approved ticket for a human.
        mock_merge.assert_not_called()

        t = parse_ticket(tickets_dir / "TICK-001.md")
        assert t["review_verdict"] == "approved"
        assert t["review_driver"] == cfg["executor"]
        assert t["review_model"] == "combined-mode-model"
        bundles = sorted((tmp_path / ".lanegate" / "executor-runs" / "TICK-001").iterdir())
        assert json.loads((bundles[-1] / "status.json").read_text())["mode"] == "combined"

    def test_combined_mode_manual_autonomy_does_not_auto_merge(self, tmp_path):
        """Manual autonomy must retain the human merge gate in combined mode."""
        from lanegate.ticket import parse_ticket

        cfg = _default_cfg(tmp_path)
        cfg["autonomy"] = "manual"
        cfg["reviewer"] = cfg["executor"]
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"])

        def fake_stream(*_args, **_kwargs):
            path = tickets_dir / "TICK-001.md"
            path.write_text(
                path.read_text().replace(
                    "status: open", "status: in_review\nreview_verdict: approved"
                )
            )
            return (0, "", "")

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.pool._stream_subprocess", side_effect=fake_stream),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._run_static_analysis", return_value=[]) as static_analysis,
            patch("lanegate.orchestrate._run_acceptance_contract_audit", return_value=[]),
            patch("lanegate.lifecycle.cmd_merge") as mock_merge,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True, human_review="none")

        static_analysis.assert_called()
        mock_merge.assert_not_called()
        ticket = parse_ticket(tickets_dir / "TICK-001.md")
        assert ticket["status"] == "in_review"
        assert ticket["review_verdict"] == "approved"

    def test_dispatch_passes_repo_root_and_implementer_to_is_combined_mode(self, tmp_path):
        """cmd_orchestrate passes repo_root and implementer to _is_combined_mode during dispatch."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"])

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.loop.invoke_executor", return_value=(0, "", "")),
            patch("lanegate.orchestrate.loop.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.loop.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate.loop._committed_files", return_value=set()),
            patch("lanegate.orchestrate.loop._run_static_analysis", return_value=[]),
            patch("lanegate.lifecycle.cmd_complete"),
            patch("lanegate.lifecycle.cmd_review"),
            patch("lanegate.orchestrate.loop.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.loop.release_orchestrator_lock"),
            patch("lanegate.orchestrate.loop._is_combined_mode", return_value=False) as mock_combined,
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        assert mock_combined.called
        call_args = mock_combined.call_args
        assert call_args.args[2] == tmp_path
        assert call_args.kwargs.get("implementer") == "claude-process"

    def test_auto_none_persists_review_driver_and_model(self, tmp_path):
        """An automatic approval records that no model performed the review."""
        from lanegate.ticket import parse_ticket

        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"])

        def fake_complete(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: open", "status: code_complete"))

        def fake_review(tid, cfg_, repo_root, **kwargs):
            p = tickets_dir / f"{tid}.md"
            p.write_text(
                p.read_text().replace(
                    "status: code_complete",
                    "status: in_review\nreview_verdict: approved\n"
                    f"review_driver: {kwargs['review_driver']}\n"
                    f"review_model: {kwargs['review_model']}",
                )
            )

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", return_value=(0, "", "")),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._run_acceptance_contract_audit", return_value=[]),
            patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete),
            patch("lanegate.lifecycle.cmd_review", side_effect=fake_review),
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        ticket = parse_ticket(tickets_dir / "TICK-001.md")
        assert ticket["review_driver"] == "auto-none"
        assert ticket["review_model"] == "none"

    def test_backfill_does_not_touch_a_split_mode_review_attribution(self, tmp_path):
        """An explicitly recorded reviewer is never overwritten by the loop."""
        from lanegate.orchestrate.autofix import backfill_combined_review_metadata

        ticket = {
            "id": "TICK-001",
            "_body": "Body.",
            "review_verdict": "approved",
            "review_driver": "codex",
            "review_model": "gpt-5",
        }
        with patch("lanegate.orchestrate.autofix.write_ticket") as mock_write:
            backfill_combined_review_metadata(
                ticket,
                {"resolved_executor": "claude-a", "resolved_model": "claude-opus-4-8"},
                tmp_path,
            )

        assert ticket["review_driver"] == "codex"
        assert ticket["review_model"] == "gpt-5"
        mock_write.assert_not_called()


class TestAutoFixCycleGatesMerge:
    """TICK-348: fix -> drift-check -> re-review always runs on
    changes_requested, but `autonomy` controls whether the approved result
    auto-merges or waits for a human — the gate moved from "may the fix run"
    to "is the result approved"."""

    def _make_open_ticket(self, tmp_path: Path) -> Path:
        tickets_dir = tmp_path / "tickets"
        return _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"])

    def _fake_review_agent_approves_with_findings(self, tickets_dir: Path):
        def fake_review_agent(ticket, repo_root, worktree_path=None, cfg=None):
            p = tickets_dir / f"{ticket['id']}.md"
            text = p.read_text()
            text = text.replace("status: code_complete", "status: in_review", 1)
            text = text.replace(
                "review_verdict: changes_requested", "review_verdict: approved", 1
            )
            text += "\n## Review Findings (attempt 2)\n\nminor nit, approved anyway\n"
            p.write_text(text)
            return True

        return fake_review_agent

    def test_combined_mode_supervised_autofix_gates_merge(self, tmp_path):
        from lanegate.reviewer import DriftCheckResult
        from lanegate.ticket import parse_ticket

        cfg = _default_cfg(tmp_path)  # default autonomy: supervised
        tickets_dir = tmp_path / "tickets"
        self._make_open_ticket(tmp_path)

        def fake_invoke_combined(ticket, cfg_, wt, *, log_stream=None, terminal_stream=None,
                                  prompt_override=None, repo_root=None, executor_override=None):
            p = tickets_dir / f"{ticket['id']}.md"
            text = p.read_text().replace(
                "status: open", "status: code_complete\nreview_verdict: changes_requested"
            )
            p.write_text(text + "\n## Review Findings (attempt 1)\n\noriginal finding\n")
            return (0, "", "")

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke_combined),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._is_combined_mode", return_value=True),
            patch("lanegate.orchestrate._run_acceptance_contract_audit", return_value=[]),
            patch("lanegate.lifecycle.cmd_complete") as mock_complete,
            patch("lanegate.orchestrate.autofix.run_fix_agent", return_value=True),
            patch("lanegate.orchestrate.autofix._git_head_sha", return_value="abc123"),
            patch(
                "lanegate.orchestrate.autofix.run_drift_check",
                return_value=DriftCheckResult(ok=True, reason="in scope"),
            ),
            patch(
                "lanegate.orchestrate.autofix.run_review_agent",
                side_effect=self._fake_review_agent_approves_with_findings(tickets_dir),
            ),
            patch("lanegate.lifecycle.cmd_merge") as mock_merge,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_complete.assert_not_called()
        mock_merge.assert_not_called()

        ticket = parse_ticket(tickets_dir / "TICK-001.md")
        assert ticket["status"] == "in_review"
        assert ticket["review_verdict"] == "approved"
        assert "## Auto-Fix Attempt 1" in ticket["_body"]
        assert "## Review Findings (attempt 1)" in ticket["_body"]
        assert "## Review Findings (attempt 2)" in ticket["_body"]
        assert ticket.get("drift_check_result") == {"ok": True, "reason": "in scope"}

    def test_split_mode_supervised_autofix_gates_merge(self, tmp_path):
        from lanegate.reviewer import DriftCheckResult
        from lanegate.ticket import parse_ticket

        cfg = _default_cfg(tmp_path)  # default autonomy: supervised
        cfg["reviewer"] = "claude-process"
        tickets_dir = tmp_path / "tickets"
        self._make_open_ticket(tmp_path)

        def fake_complete(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: open", "status: code_complete", 1))

        def fake_first_review_requests_changes(ticket, repo_root, worktree_path=None, cfg=None):
            p = tickets_dir / f"{ticket['id']}.md"
            p.write_text(
                p.read_text().replace(
                    "status: code_complete", "status: code_complete\nreview_verdict: changes_requested", 1
                ) + "\n## Review Findings (attempt 1)\n\noriginal finding\n"
            )
            return False

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", return_value=(0, "", "")),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.orchestrate._run_acceptance_contract_audit", return_value=[]),
            patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete),
            patch(
                "lanegate.orchestrate.run_review_agent",
                side_effect=fake_first_review_requests_changes,
            ),
            patch("lanegate.orchestrate.autofix.run_fix_agent", return_value=True),
            patch("lanegate.orchestrate.autofix._git_head_sha", return_value="abc123"),
            patch(
                "lanegate.orchestrate.autofix.run_drift_check",
                return_value=DriftCheckResult(ok=True, reason="in scope"),
            ),
            patch(
                "lanegate.orchestrate.autofix.run_review_agent",
                side_effect=self._fake_review_agent_approves_with_findings(tickets_dir),
            ),
            patch("lanegate.lifecycle.cmd_merge") as mock_merge,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True, human_review="none")

        mock_merge.assert_not_called()

        ticket = parse_ticket(tickets_dir / "TICK-001.md")
        assert ticket["status"] == "in_review"
        assert ticket["review_verdict"] == "approved"
        assert "## Auto-Fix Attempt 1" in ticket["_body"]
        assert "## Review Findings (attempt 1)" in ticket["_body"]
        assert "## Review Findings (attempt 2)" in ticket["_body"]
        assert ticket.get("drift_check_result") == {"ok": True, "reason": "in scope"}

    def test_split_mode_full_autonomy_auto_fix_merges_unattended(self, tmp_path):
        """Companion to the supervised/manual gate above: `autonomy: full`
        still merges unattended on re-review approval, unchanged."""
        from lanegate.reviewer import DriftCheckResult
        from lanegate.ticket import parse_ticket

        cfg = _default_cfg(tmp_path)
        cfg["autonomy"] = "full"
        cfg["reviewer"] = "claude-process"
        tickets_dir = tmp_path / "tickets"
        self._make_open_ticket(tmp_path)

        def fake_complete(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: open", "status: code_complete", 1))

        def fake_first_review_requests_changes(ticket, repo_root, worktree_path=None, cfg=None):
            p = tickets_dir / f"{ticket['id']}.md"
            p.write_text(
                p.read_text().replace(
                    "status: code_complete", "status: code_complete\nreview_verdict: changes_requested", 1
                ) + "\n## Review Findings (attempt 1)\n\noriginal finding\n"
            )
            return False

        def fake_merge(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_review", "status: merged", 1))

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", return_value=(0, "", "")),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.orchestrate._run_acceptance_contract_audit", return_value=[]),
            patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete),
            patch(
                "lanegate.orchestrate.run_review_agent",
                side_effect=fake_first_review_requests_changes,
            ),
            patch("lanegate.orchestrate.autofix.run_fix_agent", return_value=True),
            patch("lanegate.orchestrate.autofix._git_head_sha", return_value="abc123"),
            patch(
                "lanegate.orchestrate.autofix.run_drift_check",
                return_value=DriftCheckResult(ok=True, reason="in scope"),
            ),
            patch(
                "lanegate.orchestrate.autofix.run_review_agent",
                side_effect=self._fake_review_agent_approves_with_findings(tickets_dir),
            ),
            patch("lanegate.lifecycle.cmd_merge", side_effect=fake_merge) as mock_merge,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True, human_review="none")

        mock_merge.assert_called_once()

        ticket = parse_ticket(tickets_dir / "TICK-001.md")
        assert ticket["status"] == "merged"
        assert ticket["review_verdict"] == "approved"
