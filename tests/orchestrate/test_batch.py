"""
Tests for lanegate/orchestrate/batch.py — next batch calculation, dependency conflict checks.

Split out of the former monolithic tests/test_orchestrate.py (TICK-316).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.orchestrate.conftest import *  # noqa: F401,F403
from lanegate.orchestrate.batch import _underfilled_batch_reason


class TestNextBatch:
    def test_loop_helpers_are_reexported(self):
        import lanegate.orchestrate as orchestrate
        from lanegate.orchestrate import loop

        helper_names = (
            "_analyze_drafts",
            "_is_rate_limit",
            "_run_rebase",
            "_drain_loop",
            "cmd_orchestrate",
        )
        for name in helper_names:
            assert getattr(orchestrate, name) is getattr(loop, name)

    def test_batch_helpers_are_reexported(self):
        import lanegate.orchestrate as orchestrate
        from lanegate.orchestrate import batch

        helper_names = (
            "next_batch",
            "_format_max_parallel_detail",
            "_underfilled_batch_reason",
            "_review_queue_lines",
            "_ticket_next_step_line",
            "_continuation_step_lines",
            "_print_continuation_steps",
            "_print_review_queue",
        )
        for name in helper_names:
            assert getattr(orchestrate, name) is getattr(batch, name)

    def test_returns_empty_when_no_open_tickets(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "in_progress")
        result = next_batch(cfg, tmp_path)
        assert result == []

    def test_returns_open_ticket(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["foo.py"])
        result = next_batch(cfg, tmp_path)
        assert len(result) == 1
        assert result[0]["id"] == "TICK-001"

    def test_returns_parallel_batch(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"], priority=1)
        _write_ticket(tickets_dir, "TICK-002", "open", touches=["b.py"], priority=2)
        result = next_batch(cfg, tmp_path)
        assert len(result) == 2

    def test_excludes_conflicting_touches(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        # TICK-001 is in_progress with shared.py
        _write_ticket(tickets_dir, "TICK-001", "in_progress", touches=["shared.py"])
        # TICK-002 wants shared.py too — should be blocked
        _write_ticket(tickets_dir, "TICK-002", "open", touches=["shared.py"])
        result = next_batch(cfg, tmp_path)
        assert result == []

    def test_wildcard_lock_excludes_concrete_ticket(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "in_progress", touches=['"*"'])
        _write_ticket(tickets_dir, "TICK-002", "open", touches=["concrete.py"])
        assert next_batch(cfg, tmp_path) == []

    def test_wildcard_ticket_is_not_batched_with_concrete_peer(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=['"*"'], priority=1)
        _write_ticket(tickets_dir, "TICK-002", "open", touches=["concrete.py"], priority=2)
        assert [ticket["id"] for ticket in next_batch(cfg, tmp_path)] == ["TICK-001"]

    def test_priority_ordering(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["z.py"], priority=5)
        _write_ticket(tickets_dir, "TICK-002", "open", touches=["a.py"], priority=1)
        result = next_batch(cfg, tmp_path)
        # TICK-002 (priority 1) should come first
        assert result[0]["id"] == "TICK-002"

    def test_hibernated_priority_boost_over_open_same_priority(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["open.py"], priority=1)
        _write_ticket(tickets_dir, "TICK-002", "hibernated", touches=["hib.py"], priority=1)
        result = next_batch(cfg, tmp_path)
        assert result[0]["id"] == "TICK-002"

    def test_needs_review_is_not_dispatched(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "needs_review", touches=["a.py"], priority=1)
        assert next_batch(cfg, tmp_path) == []

    def test_rejected_ticket_is_selected_for_a_later_auto_fix_pass(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        rejected = _write_ticket(tickets_dir, "TICK-001", "code_complete", touches=["a.py"])
        rejected.write_text(
            rejected.read_text().replace("close_criteria:", "review_verdict: changes_requested\nclose_criteria:")
        )

        assert [ticket["id"] for ticket in next_batch(cfg, tmp_path)] == ["TICK-001"]

    def test_rejected_ticket_stays_blocked_by_a_different_lock_holder(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        rejected = _write_ticket(tickets_dir, "TICK-001", "code_complete", touches=["a.py"])
        rejected.write_text(
            rejected.read_text().replace("close_criteria:", "review_verdict: changes_requested\nclose_criteria:")
        )
        _write_ticket(tickets_dir, "TICK-002", "in_progress", touches=["a.py"])

        assert next_batch(cfg, tmp_path) == []

    def test_rejected_ticket_with_failed_drift_check_stays_human_gated(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        rejected = _write_ticket(tickets_dir, "TICK-001", "code_complete", touches=["a.py"])
        rejected.write_text(
            rejected.read_text().replace(
                "close_criteria:",
                "review_verdict: changes_requested\n"
                "drift_check_result: {ok: false, reason: out of scope}\n"
                "close_criteria:",
            )
        )

        assert next_batch(cfg, tmp_path) == []

    @pytest.mark.parametrize("status", ["merged", "validated", "done"])
    def test_delivered_dependency_unblocks_canonical_id(self, tmp_path, status):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", status, touches=["dependency.py"])
        _write_ticket(
            tickets_dir,
            "TICK-002",
            "open",
            touches=["dependent.py"],
            depends_on=["tick-1"],
        )
        assert [ticket["id"] for ticket in next_batch(cfg, tmp_path)] == ["TICK-002"]

    @pytest.mark.parametrize("status", ["failed", "closed"])
    def test_undelivered_dependency_blocks_selection_and_diagnostic(self, tmp_path, status):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", status, touches=["dependency.py"])
        _write_ticket(
            tickets_dir,
            "TICK-002",
            "open",
            touches=["dependent.py"],
            depends_on=["TICK-001"],
            priority=2,
        )
        _write_ticket(
            tickets_dir,
            "TICK-003",
            "open",
            touches=["selected.py"],
            parallel_safe=False,
            priority=1,
        )
        _write_ticket(
            tickets_dir,
            "TICK-004",
            "open",
            touches=["peer.py"],
            priority=3,
        )
        assert [ticket["id"] for ticket in next_batch(cfg, tmp_path)] == ["TICK-003"]
        batch = [{"id": "TICK-003", "touches": ["selected.py"], "parallel_safe": False}]
        assert "TICK-002 blocked by dependency TICK-001" in _underfilled_batch_reason(
            cfg, tmp_path, batch, max_parallel=2
        )

    def _routing_cfg(self, tmp_path) -> dict:
        cfg = _default_cfg(tmp_path)
        cfg["executors"] = {"claude-1": {"type": "claude-process"}, "ollama-1": {"type": "ollama"}}
        cfg["pools"] = {
            "local": {"executors": ["ollama-1"]},
            "default": {"executors": ["claude-1"]},
        }
        cfg["default_pool"] = "default"
        cfg["routing"] = [
            {"when": {"complexity_max": 2, "touches_max": 3}, "executor_pool": "local"},
            {"when": {"complexity_min": 3}, "executor_pool": "default"},
        ]
        return cfg

    def test_low_complexity_ticket_routes_to_local_pool(self, tmp_path):
        cfg = self._routing_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"], complexity=1)
        result = next_batch(cfg, tmp_path)
        assert len(result) == 1
        assert result[0]["_routed_pool"] == "local"

    def test_high_complexity_ticket_routes_to_default_pool(self, tmp_path):
        cfg = self._routing_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"], complexity=5)
        result = next_batch(cfg, tmp_path)
        assert len(result) == 1
        assert result[0]["_routed_pool"] == "default"

    def test_low_complexity_but_many_touches_falls_through_to_default_pool(self, tmp_path):
        cfg = self._routing_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(
            tickets_dir,
            "TICK-001",
            "open",
            touches=["a.py", "b.py", "c.py", "d.py"],
            complexity=1,
        )
        result = next_batch(cfg, tmp_path)
        assert len(result) == 1
        assert result[0]["_routed_pool"] == "default"

    def test_unanalyzed_ticket_falls_through_to_default_pool_without_error(self, tmp_path):
        """A ticket with no `complexity` (analyze not yet run) must not crash
        rule evaluation -- it matches no complexity-gated rule and falls
        back to `default_pool`."""
        cfg = self._routing_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"])
        result = next_batch(cfg, tmp_path)
        assert len(result) == 1
        assert result[0]["_routed_pool"] == "default"

    def test_no_routing_configured_leaves_routed_pool_none(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"])
        result = next_batch(cfg, tmp_path)
        assert result[0]["_routed_pool"] is None


# TICK-517: reviewer-cooldown retry window gates next_batch dispatch
# ---------------------------------------------------------------------------


class TestNextBatchReviewerCooldownRetry:
    def _write_review_pending_ticket(
        self,
        tickets_dir: Path,
        ticket_id: str,
        *,
        retry_after: str | None,
        cooling_down: bool = True,
    ) -> None:
        retry_line = f"review_retry_after: {retry_after!r}\n" if retry_after else ""
        reason = (
            "Independent reviewer temporarily unavailable (cooldown); "
            f"retry after {retry_after}. No healthy independent reviewer is available."
            if cooling_down
            else "rate limit or quota interruption"
        )
        (tickets_dir / f"{ticket_id}.md").write_text(
            "---\n"
            f"id: {ticket_id}\n"
            f"title: Test {ticket_id}\n"
            "status: hibernated\n"
            "priority: 1\n"
            "parallel_safe: true\n"
            "touches:\n  - foo.py\n"
            "review_pending: true\n"
            f"review_pending_reason: {reason!r}\n"
            f"{retry_line}"
            "---\nBody.\n"
        )

    def test_next_batch_skips_review_pending_before_retry_window(self, tmp_path):
        """A hibernated review_pending ticket whose reviewers are still
        cooling down must not be re-dispatched before its recorded retry
        time -- next_batch must not pick it (or anything blocked by its
        touch lock, since nothing else is open here)."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        self._write_review_pending_ticket(tickets_dir, "TICK-001", retry_after=future)

        result = next_batch(cfg, tmp_path)

        assert result == []

    def test_next_batch_resumes_review_pending_after_retry_window(self, tmp_path):
        """The same ticket, once its recorded retry time has elapsed, is
        selected exactly like any other hibernated ticket -- and a
        pre-migration review_pending ticket with no review_retry_after field
        at all is treated the same, immediately retry-eligible."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        self._write_review_pending_ticket(tickets_dir, "TICK-001", retry_after=past)

        result = next_batch(cfg, tmp_path)

        assert [t["id"] for t in result] == ["TICK-001"]

    def test_next_batch_resumes_pre_migration_review_pending_with_no_retry_field(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        self._write_review_pending_ticket(tickets_dir, "TICK-001", retry_after=None)

        result = next_batch(cfg, tmp_path)

        assert [t["id"] for t in result] == ["TICK-001"]

    def test_next_batch_treats_malformed_retry_after_as_eligible(self, tmp_path):
        """A malformed/unparsable review_retry_after must fail closed to
        retry-eligible-now rather than raise or skip forever."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        self._write_review_pending_ticket(tickets_dir, "TICK-001", retry_after="not-a-timestamp")

        result = next_batch(cfg, tmp_path)

        assert [t["id"] for t in result] == ["TICK-001"]

    def test_next_batch_still_skips_plain_rate_limited_hibernation_normally(self, tmp_path):
        """The true rate-limit hibernation marker is untouched by this gate --
        a rate-limited review_pending ticket is unconditionally eligible
        (matching pre-existing behavior), not treated as reviewer-cooldown."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        self._write_review_pending_ticket(tickets_dir, "TICK-001", retry_after=None, cooling_down=False)

        result = next_batch(cfg, tmp_path)

        assert [t["id"] for t in result] == ["TICK-001"]


# --max flag respected
# ---------------------------------------------------------------------------


class TestMaxParallel:
    def test_max_caps_batch_size(self, tmp_path, capsys):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        # 3 parallel-safe open tickets with disjoint touches
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"], priority=1)
        _write_ticket(tickets_dir, "TICK-002", "open", touches=["b.py"], priority=2)
        _write_ticket(tickets_dir, "TICK-003", "open", touches=["c.py"], priority=3)

        started = []

        def fake_run(
            cfg,
            repo_root,
            max_parallel,
            dry_run,
            human_review,
            milestone=None,
            *,
            auto_analyze=True,
            verbose=False,
            pool_name=None,
            ticket_ids=None,
            _orig_out=None,
            _log_f=None,
            session_ts=None,
        ):
            # Check that the max_parallel cap was passed correctly
            started.append(max_parallel)

        with (
            patch("lanegate.orchestrate._drain_loop", side_effect=fake_run),
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, max_parallel=1, all_milestones=True)

        assert started[0] == 1


def test_slot_refill_and_concurrency_cap(tmp_path):
    import threading
    import time

    import lanegate.orchestrate as orch

    def run_scenario(root: Path, max_parallel: int, *, require_refill_before_slow_done: bool):
        root.mkdir()
        cfg = _default_cfg(root)
        tickets_dir = root / "tickets"
        for index, touch in enumerate(("a.py", "b.py", "c.py"), start=1):
            _write_ticket(
                tickets_dir,
                f"TICK-00{index}",
                "open",
                touches=[touch],
                priority=index,
            )

        events: list[tuple[str, str]] = []
        active = 0
        max_seen = 0
        state_lock = threading.Lock()
        tick2_started = threading.Event()
        tick3_started = threading.Event()
        real_next_batch = orch.next_batch
        next_queries: list[set[str]] = []

        def update_status(tid: str, from_status: str, to_status: str) -> None:
            with state_lock:
                path = tickets_dir / f"{tid}.md"
                text = path.read_text()
                text = text.replace(f"status: {from_status}", f"status: {to_status}", 1)
                path.write_text(text)

        def fake_start(tid, cfg_, repo_root, **kwargs):
            update_status(tid, "open", "in_progress")

        def fake_complete(tid, cfg_, repo_root):
            update_status(tid, "in_progress", "code_complete")

        def fake_review(tid, cfg_, repo_root, *, verdict=None, summary=None, findings=None):
            update_status(tid, "code_complete", "in_review")

        def fake_merge(tid, cfg_, repo_root):
            update_status(tid, "in_review", "merged")

        def recording_next_batch(
            cfg_, repo_root, milestone=None, *, exclude_touches=None, ticket_ids=None
        ):
            next_queries.append(set(exclude_touches or set()))
            with state_lock:
                return real_next_batch(
                    cfg_,
                    repo_root,
                    milestone=milestone,
                    exclude_touches=exclude_touches,
                    ticket_ids=ticket_ids,
                )

        def fake_invoke(ticket, cfg_, wt, **kwargs):
            nonlocal active, max_seen
            tid = ticket["id"]
            with state_lock:
                active += 1
                max_seen = max(max_seen, active)
                events.append(("start", tid))
            if tid == "TICK-002":
                tick2_started.set()
                if require_refill_before_slow_done:
                    assert tick3_started.wait(2), "slot was not refilled before TICK-002 finished"
            elif tid == "TICK-001" and require_refill_before_slow_done:
                assert tick2_started.wait(2), "initial second worker did not start"
            elif tid == "TICK-003":
                tick3_started.set()
            time.sleep(0.01)
            with state_lock:
                events.append(("end", tid))
                active -= 1
            return (0, "", "")

        with (
            patch("lanegate.orchestrate.next_batch", side_effect=recording_next_batch),
            patch("lanegate.lifecycle.cmd_start", side_effect=fake_start),
            patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=(False, None)),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._committed_files", return_value=set()),
            patch("lanegate.orchestrate._run_static_analysis", return_value=[]),
            patch("lanegate.orchestrate._run_acceptance_contract_audit", return_value=[]),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete),
            patch("lanegate.lifecycle.cmd_review", side_effect=fake_review),
            patch("lanegate.lifecycle.cmd_merge", side_effect=fake_merge),
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(
                cfg,
                root,
                max_parallel=max_parallel,
                human_review="none",
                all_milestones=True,
                auto_analyze=False,
            )

        return events, max_seen, next_queries

    parallel_events, parallel_max, parallel_queries = run_scenario(
        tmp_path / "parallel",
        2,
        require_refill_before_slow_done=True,
    )
    assert parallel_max == 2
    assert parallel_events.index(("start", "TICK-003")) < parallel_events.index(
        ("end", "TICK-002")
    )
    assert {"b.py"} in parallel_queries

    sequential_events, sequential_max, sequential_queries = run_scenario(
        tmp_path / "sequential",
        1,
        require_refill_before_slow_done=False,
    )
    assert sequential_max == 1
    assert sequential_events == [
        ("start", "TICK-001"),
        ("end", "TICK-001"),
        ("start", "TICK-002"),
        ("end", "TICK-002"),
        ("start", "TICK-003"),
        ("end", "TICK-003"),
    ]
    assert sequential_queries
    assert all(not excluded for excluded in sequential_queries)


def test_continue_after_pause(tmp_path, capsys):
    """A *non-rate-limit* pause does not halt touch-independent work.

    Parallel mode: when one ticket fails, independent tickets fill the
    freed slot and continue.  Sequential mode: the outer loop loops via
    next_batch() (paused IDs filtered) so TICK-002 runs after TICK-001 pauses.
    Final summary enumerates all paused tickets with remediation commands.

    (A *rate-limit* pause is deliberately different — it halts new dispatch;
    see test_rate_limit_blocks_next_ticket.)
    """

    def run_scenario(root: Path, max_parallel: int) -> list[str]:
        root.mkdir()
        cfg = _default_cfg(root)
        cfg["max_parallel"] = max_parallel
        tickets_dir = root / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"], priority=1)
        _write_ticket(tickets_dir, "TICK-002", "open", touches=["b.py"], priority=2)
        if max_parallel > 1:
            _write_ticket(tickets_dir, "TICK-003", "open", touches=["c.py"], priority=3)

        processed: list[str] = []

        def fake_invoke(ticket, cfg_, wt, **kwargs):
            if ticket["id"] == "TICK-001":
                return (1, "", "")  # non-rate-limit executor failure → cmd_fail
            processed.append(ticket["id"])
            return (0, "", "")

        def fake_start(tid, cfg_, repo_root, **kwargs):
            p = tickets_dir / f"{tid}.md"
            text = p.read_text()
            for from_status in ("open", "hibernated"):
                updated = text.replace(f"status: {from_status}", "status: in_progress", 1)
                if updated != text:
                    p.write_text(updated)
                    return

        def fake_fail(tid, cfg_, repo_root, *, reason=""):
            p = tickets_dir / f"{tid}.md"
            p.write_text(
                p.read_text().replace("status: in_progress", "status: failed", 1)
            )

        def fake_complete(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(
                p.read_text().replace("status: in_progress", "status: code_complete", 1)
            )

        def fake_review(tid, cfg_, repo_root, *, verdict=None, summary=None, findings=None):
            p = tickets_dir / f"{tid}.md"
            text = p.read_text()
            text = text.replace("status: code_complete", "status: in_review", 1)
            if "review_verdict:" not in text:
                text = text.replace(
                    f"id: {tid}\n",
                    f"id: {tid}\nreview_verdict: approved\n",
                )
            p.write_text(text)

        def fake_merge(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_review", "status: merged", 1))

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=fake_start),
            patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke),
            patch("lanegate.orchestrate._is_rate_limit", return_value=False),
            patch("lanegate.lifecycle.cmd_fail", side_effect=fake_fail),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=(False, None)),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._committed_files", return_value=set()),
            patch("lanegate.orchestrate._run_static_analysis", return_value=[]),
            patch("lanegate.orchestrate._run_acceptance_contract_audit", return_value=[]),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete),
            patch("lanegate.lifecycle.cmd_review", side_effect=fake_review),
            patch("lanegate.lifecycle.cmd_merge", side_effect=fake_merge),
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(
                cfg,
                root,
                max_parallel=max_parallel,
                human_review="none",
                all_milestones=True,
                auto_analyze=False,
            )

        _, err = capsys.readouterr()
        return processed, err

    # Parallel mode: TICK-001 and TICK-002 in initial batch; TICK-001 pauses →
    # freed slot is refilled with TICK-003, which must complete.
    par_processed, par_err = run_scenario(tmp_path / "parallel", max_parallel=2)
    assert "TICK-002" in par_processed, "parallel: TICK-002 should complete despite TICK-001 pause"
    assert "TICK-003" in par_processed, "parallel: freed slot should be filled with TICK-003"
    assert "TICK-001" not in par_processed
    # Final summary lists TICK-001 with failed remediation.
    assert "TICK-001" in par_err
    assert "failed" in par_err
    assert "lanegate run" in par_err

    # Sequential mode: TICK-001 pauses on first outer-loop iteration; the loop
    # continues via next_batch() and TICK-002 runs on the next iteration.
    seq_processed, seq_err = run_scenario(tmp_path / "sequential", max_parallel=1)
    assert "TICK-002" in seq_processed, "sequential: TICK-002 should continue after TICK-001 pause"
    assert "TICK-001" not in seq_processed
    # Final summary also applies in sequential mode.
    assert "TICK-001" in seq_err
    assert "failed" in seq_err


def test_paused_top_ticket_does_not_starve_touch_conflicting_peers(tmp_path, capsys):
    """A reselected paused top ticket must not end the outer drain loop.

    TICK-001 pauses but stays hibernated, so it remains eligible to
    ``next_batch()``. Its touch blocks TICK-002 and TICK-003 from the greedy
    batch, while unrelated TICK-004 is also eligible. The outer loop must
    re-query without TICK-001 and dispatch both blocked peers and TICK-004.
    """
    cfg = _default_cfg(tmp_path)
    # This scenario requires successful peers to auto-merge; the project
    # default is supervised and now intentionally preserves approved work for
    # a human merge.
    cfg["autonomy"] = "full"
    cfg["max_parallel"] = 1
    tickets_dir = tmp_path / "tickets"
    _write_ticket(tickets_dir, "TICK-001", "open", touches=["shared.py"], priority=1)
    _write_ticket(tickets_dir, "TICK-002", "open", touches=["shared.py"], priority=2)
    _write_ticket(tickets_dir, "TICK-003", "open", touches=["shared.py"], priority=3)
    _write_ticket(tickets_dir, "TICK-004", "open", touches=["unrelated.py"], priority=4)

    processed: list[str] = []

    def fake_invoke(ticket, cfg_, wt, **kwargs):
        if ticket["id"] == "TICK-001":
            return (1, "", "")
        processed.append(ticket["id"])
        return (0, "", "")

    def replace_status(tid, old, new):
        path = tickets_dir / f"{tid}.md"
        path.write_text(path.read_text().replace(f"status: {old}", f"status: {new}", 1))

    def fake_start(tid, cfg_, repo_root, **kwargs):
        replace_status(tid, "open", "in_progress")

    def fake_fail_as_hibernated(tid, cfg_, repo_root, *, reason=""):
        # Model an already-paused ticket that remains eligible for next_batch.
        replace_status(tid, "in_progress", "hibernated")

    def fake_complete(tid, cfg_, repo_root):
        replace_status(tid, "in_progress", "code_complete")

    def fake_review(tid, cfg_, repo_root, *, verdict=None, summary=None, findings=None):
        replace_status(tid, "code_complete", "in_review")
        path = tickets_dir / f"{tid}.md"
        path.write_text(path.read_text().replace(f"id: {tid}\n", f"id: {tid}\nreview_verdict: approved\n", 1))

    def fake_merge(tid, cfg_, repo_root):
        replace_status(tid, "in_review", "merged")

    with (
        patch("lanegate.lifecycle.cmd_start", side_effect=fake_start),
        patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke),
        patch("lanegate.orchestrate._is_rate_limit", return_value=False),
        patch("lanegate.lifecycle.cmd_fail", side_effect=fake_fail_as_hibernated),
        patch("lanegate.orchestrate.commit_worktree_changes", return_value=(False, None)),
        patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
        patch("lanegate.orchestrate._committed_files", return_value=set()),
        patch("lanegate.orchestrate._run_static_analysis", return_value=[]),
        patch("lanegate.orchestrate._run_acceptance_contract_audit", return_value=[]),
        patch("lanegate.orchestrate._is_combined_mode", return_value=False),
        patch("lanegate.orchestrate.run_review_agent", side_effect=lambda ticket, repo_root, **kw: (fake_review(ticket["id"], cfg, repo_root), True)[1]),
        patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete),
        patch("lanegate.lifecycle.cmd_review", side_effect=fake_review),
        patch("lanegate.lifecycle.cmd_merge", side_effect=fake_merge),
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
        )

    assert set(processed) == {"TICK-002", "TICK-003", "TICK-004"}
    assert "TICK-001" not in processed
    assert "no dispatchable candidates remain after excluding tickets" in capsys.readouterr().out


def test_needs_review_gate_continues_drain_loop(tmp_path, capsys):
    """A crash after a ticket leaves in_progress must not halt the whole run.

    cmd_needs_review only accepts tickets in in_progress and calls sys.exit(1)
    otherwise. If a worker thread crashes after its ticket has already reached
    code_complete/in_review (e.g. during merge), a handler that calls
    cmd_needs_review directly re-raises that SystemExit, which is not caught by
    `except Exception` and unwinds out of cmd_orchestrate entirely — silently
    halting the rest of the batch instead of downgrading only the crashed
    ticket. Two touch-disjoint tickets: TICK-001's merge crashes after review
    approval; TICK-002 is unrelated and must still complete in the same
    cmd_orchestrate invocation, and cmd_orchestrate must return normally
    (no SystemExit).
    """
    cfg = _default_cfg(tmp_path)
    # The healthy peer is expected to merge during this run.
    cfg["autonomy"] = "full"
    cfg["max_parallel"] = 2
    tickets_dir = tmp_path / "tickets"
    _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"], priority=1)
    _write_ticket(tickets_dir, "TICK-002", "open", touches=["b.py"], priority=2)

    processed: list[str] = []

    def fake_invoke(ticket, cfg_, wt, **kwargs):
        processed.append(ticket["id"])
        return (0, "", "")

    def fake_start(tid, cfg_, repo_root, **kwargs):
        p = tickets_dir / f"{tid}.md"
        text = p.read_text()
        p.write_text(text.replace("status: open", "status: in_progress", 1))

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
        if tid == "TICK-001":
            raise RuntimeError("simulated git push failure during merge")
        p = tickets_dir / f"{tid}.md"
        p.write_text(p.read_text().replace("status: in_review", "status: merged", 1))

    with (
        patch("lanegate.lifecycle.cmd_start", side_effect=fake_start),
        patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke),
        patch("lanegate.orchestrate._is_rate_limit", return_value=False),
        patch("lanegate.orchestrate.commit_worktree_changes", return_value=(False, None)),
        patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
        patch("lanegate.orchestrate._committed_files", return_value=set()),
        patch("lanegate.orchestrate._run_static_analysis", return_value=[]),
        patch("lanegate.orchestrate._run_acceptance_contract_audit", return_value=[]),
        patch("lanegate.orchestrate._is_combined_mode", return_value=False),
        patch("lanegate.orchestrate.run_review_agent", side_effect=lambda ticket, repo_root, **kw: (fake_review(ticket["id"], cfg, repo_root), True)[1]),
        patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete),
        patch("lanegate.lifecycle.cmd_review", side_effect=fake_review),
        patch("lanegate.lifecycle.cmd_merge", side_effect=fake_merge),
        patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
        patch("lanegate.orchestrate.release_orchestrator_lock"),
    ):
        # Must not raise SystemExit — that's the bug: a crash on one ticket
        # must never propagate out and abort tickets still in flight.
        cmd_orchestrate(
            cfg,
            tmp_path,
            max_parallel=2,
            human_review="none",
            all_milestones=True,
            auto_analyze=False,
        )

    assert "TICK-002" in processed, "independent ticket must still run in the same invocation"

    t1_status = [
        l for l in (tickets_dir / "TICK-001.md").read_text().splitlines() if "status:" in l
    ][0]
    t2_status = [
        l for l in (tickets_dir / "TICK-002.md").read_text().splitlines() if "status:" in l
    ][0]
    assert "needs_review" in t1_status, "crashed ticket is downgraded, not left in an invalid state"
    assert "merged" in t2_status, "independent ticket completes normally"


@pytest.mark.parametrize("crash_phase", ["start", "execute", "commit", "review"])
def test_chaos_crash_at_each_phase_pauses_one_ticket(tmp_path, capsys, crash_phase):
    """A crash at any drain-loop phase must degrade to one paused ticket,
    never crash cmd_orchestrate or block an independent, touch-disjoint
    ticket from completing in the same invocation.

    TICK-001 crashes at `crash_phase`; TICK-002 is unrelated (disjoint
    touches) and must run the full happy path to `merged`. This is the
    general-purpose version of test_needs_review_gate_continues_drain_loop
    (which covers a post-review/merge crash specifically) and
    test_merge_failure_does_not_crash_orchestrate_run (which covers
    auto_merge_approved_local_tickets specifically) — together they exercise
    every phase: start, execute, commit, review here; merge in those two.
    """
    cfg = _default_cfg(tmp_path)
    # The healthy peer is expected to merge during this run.
    cfg["autonomy"] = "full"
    cfg["max_parallel"] = 1
    tickets_dir = tmp_path / "tickets"
    _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"], priority=1)
    _write_ticket(tickets_dir, "TICK-002", "open", touches=["b.py"], priority=2)

    processed: list[str] = []

    def fake_start(tid, cfg_, repo_root, **kwargs):
        if tid == "TICK-001" and crash_phase == "start":
            raise RuntimeError("simulated start-phase crash")
        p = tickets_dir / f"{tid}.md"
        p.write_text(p.read_text().replace("status: open", "status: in_progress", 1))

    def fake_invoke(ticket, cfg_, wt, **kwargs):
        if ticket["id"] == "TICK-001" and crash_phase == "execute":
            raise RuntimeError("simulated execute-phase crash")
        processed.append(ticket["id"])
        return (0, "", "")

    def fake_commit(wt, tid):
        if tid == "TICK-001" and crash_phase == "commit":
            raise RuntimeError("simulated commit-phase crash")
        return False, None

    def fake_complete(tid, cfg_, repo_root):
        p = tickets_dir / f"{tid}.md"
        p.write_text(p.read_text().replace("status: in_progress", "status: code_complete", 1))

    def fake_review(tid, cfg_, repo_root, *, verdict=None, summary=None, findings=None):
        if tid == "TICK-001" and crash_phase == "review":
            raise RuntimeError("simulated review-phase crash")
        p = tickets_dir / f"{tid}.md"
        text = p.read_text().replace("status: code_complete", "status: in_review", 1)
        if "review_verdict:" not in text:
            text = text.replace(f"id: {tid}\n", f"id: {tid}\nreview_verdict: approved\n")
        p.write_text(text)

    def fake_merge(tid, cfg_, repo_root):
        p = tickets_dir / f"{tid}.md"
        p.write_text(p.read_text().replace("status: in_review", "status: merged", 1))

    with (
        patch("lanegate.lifecycle.cmd_start", side_effect=fake_start),
        patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke),
        patch("lanegate.orchestrate._is_rate_limit", return_value=False),
        patch("lanegate.orchestrate.commit_worktree_changes", side_effect=fake_commit),
        patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
        patch("lanegate.orchestrate._committed_files", return_value=set()),
        patch("lanegate.orchestrate._run_static_analysis", return_value=[]),
        patch("lanegate.orchestrate._run_acceptance_contract_audit", return_value=[]),
        patch("lanegate.orchestrate._is_combined_mode", return_value=False),
        patch("lanegate.orchestrate.run_review_agent", side_effect=lambda ticket, repo_root, **kw: (fake_review(ticket["id"], cfg, repo_root), True)[1]),
        patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete),
        patch("lanegate.lifecycle.cmd_review", side_effect=fake_review),
        patch("lanegate.lifecycle.cmd_merge", side_effect=fake_merge),
        patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
        patch("lanegate.orchestrate.release_orchestrator_lock"),
    ):
        # The core assertion: this must never raise SystemExit or any other
        # exception, regardless of which phase crashed.
        cmd_orchestrate(
            cfg,
            tmp_path,
            max_parallel=1,
            human_review="none",
            all_milestones=True,
            auto_analyze=False,
        )

    t2_status = [
        l for l in (tickets_dir / "TICK-002.md").read_text().splitlines() if "status:" in l
    ][0]
    assert "merged" in t2_status, (
        f"independent ticket must still complete when TICK-001 crashes at {crash_phase!r}"
    )

    t1_status = [
        l for l in (tickets_dir / "TICK-001.md").read_text().splitlines() if "status:" in l
    ][0]
    # A crash must never let the ticket silently reach "merged" — but landing
    # at code_complete (its last known-good state, with a clear "next:"
    # remedy) is a legitimate paused outcome, not just needs_review.
    assert "merged" not in t1_status, (
        f"crashed ticket must never silently complete when it crashed at {crash_phase!r}"
    )


@pytest.mark.parametrize("max_parallel", [1, 2])
def test_real_system_exit_from_guard_call_pauses_one_ticket(tmp_path, capsys, max_parallel):
    """A genuine SystemExit from a guard-exiting lifecycle call (cmd_start,
    cmd_hibernate, cmd_needs_review, cmd_fail, and cmd_complete all raise it
    via sys.exit(1) on precondition failure) must be caught at the run_ticket
    crash boundary same as any other exception.

    SystemExit is a BaseException, not an Exception subclass, so `except
    Exception` alone does not catch it. Both the serial-loop boundary
    (max_parallel<=1) and the worker-pool future.result() boundary
    (max_parallel>1) must explicitly catch SystemExit too, or a real guard
    failure — as opposed to the RuntimeError used elsewhere in this file's
    chaos tests — unwinds straight out of cmd_orchestrate and kills tickets
    still in flight.
    """
    cfg = _default_cfg(tmp_path)
    # The healthy peer is expected to merge during this run.
    cfg["autonomy"] = "full"
    cfg["max_parallel"] = max_parallel
    tickets_dir = tmp_path / "tickets"
    _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"], priority=1)
    _write_ticket(tickets_dir, "TICK-002", "open", touches=["b.py"], priority=2)

    processed: list[str] = []

    def fake_start(tid, cfg_, repo_root, **kwargs):
        if tid == "TICK-001":
            # Mirrors cmd_start's real guard-failure path (sys.exit(1)) —
            # raised directly as SystemExit, not a plain Exception. (Note:
            # this module does not import `sys` at module scope, so
            # `sys.exit(...)` here would raise NameError instead — a
            # different, already-caught exception that would make this test
            # pass regardless of the fix. `raise SystemExit(1)` avoids that
            # trap.)
            raise SystemExit(1)
        p = tickets_dir / f"{tid}.md"
        p.write_text(p.read_text().replace("status: open", "status: in_progress", 1))

    def fake_invoke(ticket, cfg_, wt, **kwargs):
        processed.append(ticket["id"])
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

    with (
        patch("lanegate.lifecycle.cmd_start", side_effect=fake_start),
        patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke),
        patch("lanegate.orchestrate._is_rate_limit", return_value=False),
        patch("lanegate.orchestrate.commit_worktree_changes", return_value=(False, None)),
        patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
        patch("lanegate.orchestrate._committed_files", return_value=set()),
        patch("lanegate.orchestrate._run_static_analysis", return_value=[]),
        patch("lanegate.orchestrate._run_acceptance_contract_audit", return_value=[]),
        patch("lanegate.orchestrate._is_combined_mode", return_value=False),
        patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete),
        patch("lanegate.lifecycle.cmd_review", side_effect=fake_review),
        patch("lanegate.lifecycle.cmd_merge", side_effect=fake_merge),
        patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
        patch("lanegate.orchestrate.release_orchestrator_lock"),
    ):
        # Must not raise SystemExit — a real guard failure (not a mocked
        # RuntimeError) must never propagate out and abort tickets still in
        # flight.
        cmd_orchestrate(
            cfg,
            tmp_path,
            max_parallel=max_parallel,
            human_review="none",
            all_milestones=True,
            auto_analyze=False,
        )

    assert "TICK-002" in processed, "independent ticket must still run despite TICK-001's SystemExit"

    t1_status = [
        l for l in (tickets_dir / "TICK-001.md").read_text().splitlines() if "status:" in l
    ][0]
    t2_status = [
        l for l in (tickets_dir / "TICK-002.md").read_text().splitlines() if "status:" in l
    ][0]
    assert "needs_review" in t1_status, (
        "ticket whose SystemExit fired before reaching in_progress must be "
        "force-downgraded to needs_review, not left crashed or silently merged"
    )
    assert "merged" in t2_status, "independent ticket completes normally"

    # TICK-249: the crash reason string alone (exception class + message)
    # wasn't enough to find the actual failing line without cross-referencing
    # timestamped logs by hand -- both the serial-loop and worker-pool crash
    # boundaries now also print the full traceback of the caught exception.
    err = capsys.readouterr().err
    assert ("Traceback (most recent call last)" in err) or ("cmd_start failed" in err)
    assert ("SystemExit" in err) or ("exit code" in err)


def test_orchestrate_handles_missing_worktree_mid_run(tmp_path, capsys):
    """Orchestrate continues when a ticket's worktree disappears mid-run.

    Simulates a worktree directory being deleted during execution (e.g., by a
    concurrent merge or cleanup process). The affected ticket should be marked
    needs_review, and other tickets should continue without crashing the worker
    pool.
    """
    cfg = _default_cfg(tmp_path)
    cfg["max_parallel"] = 2
    tickets_dir = tmp_path / "tickets"
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir(exist_ok=True)

    # Create 3 parallel-safe tickets with disjoint touches
    _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"], priority=1)
    _write_ticket(tickets_dir, "TICK-002", "open", touches=["b.py"], priority=2)
    _write_ticket(tickets_dir, "TICK-003", "open", touches=["c.py"], priority=3)

    processed = []
    deleted_worktrees = set()

    def fake_start(tid, cfg_, repo_root, **kwargs):
        p = tickets_dir / f"{tid}.md"
        text = p.read_text()
        text = text.replace("status: open", "status: in_progress", 1)
        p.write_text(text)
        # Create a real worktree directory for this ticket
        wt = worktrees_dir / tid.lower()
        wt.mkdir(exist_ok=True)

    def fake_invoke(ticket, cfg_, wt, **kwargs):
        tid = ticket["id"]
        if tid == "TICK-001":
            # Simulate worktree disappearing during executor run (race condition)
            # Delete the worktree before returning
            import shutil
            if wt.exists():
                shutil.rmtree(wt)
            deleted_worktrees.add(tid)
        processed.append(tid)
        return (0, "", "")  # Executor returns success

    def fake_complete(tid, cfg_, repo_root):
        p = tickets_dir / f"{tid}.md"
        text = p.read_text()
        text = text.replace("status: in_progress", "status: code_complete", 1)
        p.write_text(text)

    def fake_review(tid, cfg_, repo_root, *, verdict=None, summary=None, findings=None):
        p = tickets_dir / f"{tid}.md"
        text = p.read_text()
        text = text.replace("status: code_complete", "status: in_review", 1)
        if "review_verdict:" not in text:
            text = text.replace(
                f"id: {tid}\n",
                f"id: {tid}\nreview_verdict: approved\n",
            )
        p.write_text(text)

    def fake_merge(tid, cfg_, repo_root):
        p = tickets_dir / f"{tid}.md"
        p.write_text(p.read_text().replace("status: in_review", "status: merged", 1))

    def fake_needs_review(tid, cfg_, repo_root, *, reason=''):
        p = tickets_dir / f"{tid}.md"
        text = p.read_text()
        text = text.replace("status: in_progress", "status: needs_review", 1)
        p.write_text(text)

    with (
        patch("lanegate.lifecycle.cmd_start", side_effect=fake_start),
        patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke),
        patch("lanegate.orchestrate.commit_worktree_changes", return_value=(True, None)),
        # check_worktree_has_commits will receive a missing worktree path and must not crash
        patch("lanegate.orchestrate._committed_files", return_value=set()),
        patch("lanegate.orchestrate._run_static_analysis", return_value=[]),
        patch("lanegate.orchestrate._run_acceptance_contract_audit", return_value=[]),
        patch("lanegate.orchestrate._is_combined_mode", return_value=False),
        patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete),
        patch("lanegate.lifecycle.cmd_review", side_effect=fake_review),
        patch("lanegate.lifecycle.cmd_needs_review", side_effect=fake_needs_review),
        patch("lanegate.lifecycle.cmd_merge", side_effect=fake_merge),
        patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
        patch("lanegate.orchestrate.release_orchestrator_lock"),
    ):
        cmd_orchestrate(
            cfg,
            tmp_path,
            max_parallel=2,
            human_review="none",
            all_milestones=True,
            auto_analyze=False,
        )

    captured = capsys.readouterr()

    # Verify that the affected ticket was processed (executor returned)
    assert "TICK-001" in processed

    # Verify that other tickets continued to process
    assert "TICK-002" in processed, "TICK-002 should complete despite TICK-001's missing worktree"
    assert "TICK-003" in processed, "TICK-003 should complete despite TICK-001's missing worktree"

    # Verify the orchestrate loop didn't crash
    # (capsys.readouterr() completing means the loop finished cleanly)
    # Check that output exists (basic sign the process didn't crash)
    assert len(captured.out) + len(captured.err) > 0

    # Verify that TICK-001's status was marked needs_review due to missing worktree
    ticket_text = (tickets_dir / "TICK-001.md").read_text()
    assert "status: needs_review" in ticket_text, "TICK-001 should be marked needs_review"


def test_orchestrate_audits_effective_max_parallel_and_underfilled_batch(tmp_path, capsys):
    cfg = _default_cfg(tmp_path)
    cfg["max_parallel"] = 4
    cfg["executors"] = {"claude-process": {"max_parallel": 3}}
    tickets_dir = tmp_path / "tickets"
    _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"], priority=1)
    _write_ticket(tickets_dir, "TICK-002", "open", touches=["a.py"], priority=2)
    _write_ticket(
        tickets_dir,
        "TICK-003",
        "open",
        touches=["b.py"],
        priority=3,
        parallel_safe=False,
    )
    _write_ticket(tickets_dir, "TICK-004", "open", touches=["c.py"], priority=4)

    cmd_orchestrate(
        cfg,
        tmp_path,
        dry_run=True,
        all_milestones=True,
        auto_analyze=False,
    )

    captured = capsys.readouterr()
    assert "max_parallel: 3" in captured.out
    assert "source: default executor override: executors['claude-process'].max_parallel" in captured.out
    assert "global default executor" in captured.out
    assert "default executor override takes precedence over max_parallel=4" in captured.out
    assert "batch: 2 running of cap 3" in captured.out
    assert "batch under-filled: only 2 compatible ticket(s) available for cap 3" in captured.out
    assert "TICK-002 conflicts on a.py" in captured.out
    assert "with selected ticket(s) TICK-001" in captured.out


def test_underfilled_batch_reports_wildcard_selected_holder(tmp_path):
    cfg = _default_cfg(tmp_path)
    tickets_dir = tmp_path / "tickets"
    _write_ticket(tickets_dir, "TICK-001", "open", touches=['"*"'], priority=1)
    _write_ticket(tickets_dir, "TICK-002", "open", touches=["concrete.py"], priority=2)
    reason = _underfilled_batch_reason(
        cfg,
        tmp_path,
        [{"id": "TICK-001", "touches": ["*"], "parallel_safe": True}],
        max_parallel=2,
    )
    assert reason is not None
    assert "TICK-002 conflicts on *" in reason
    assert "selected ticket(s) TICK-001" in reason


def test_orchestrate_underfilled_batch_explains_serial_ticket_and_skipped_peers(
    tmp_path, capsys
):
    cfg = _default_cfg(tmp_path)
    cfg["max_parallel"] = 3
    tickets_dir = tmp_path / "tickets"
    _write_ticket(
        tickets_dir,
        "TICK-001",
        "open",
        touches=["lanegate/executor.py"],
        priority=1,
        parallel_safe=False,
    )
    _write_ticket(
        tickets_dir,
        "TICK-002",
        "open",
        touches=["lanegate/executor.py", "lanegate/ticket.py"],
        priority=2,
        parallel_safe=True,
    )
    (tickets_dir / "TICK-003.md").write_text(
        "---\n"
        "id: TICK-003\n"
        "title: Test TICK-003\n"
        "status: open\n"
        "priority: 3\n"
        "parallel_safe: false\n"
        "depends_on: [TICK-002]\n"
        "touches: [lanegate/orchestrate.py]\n"
        "close_criteria: All tests pass.\n"
        "---\n"
        "Body.\n"
    )

    cmd_orchestrate(
        cfg,
        tmp_path,
        dry_run=True,
        all_milestones=True,
        auto_analyze=False,
    )

    captured = capsys.readouterr()
    assert "batch: 1 running of cap 3" in captured.out
    assert "selected TICK-001 has parallel_safe=false" in captured.out
    assert "TICK-002 conflicts on lanegate/executor.py" in captured.out
    assert "TICK-003 blocked by dependency TICK-002" in captured.out


def test_batch_diagnostics_persisted_to_log_file(tmp_path):
    """Verify that '[orchestrate] batch:' and 'batch under-filled:' diagnostics
    are written to the persisted log file, not just the terminal."""
    cfg = _default_cfg(tmp_path)
    cfg["max_parallel"] = 3
    tickets_dir = tmp_path / "tickets"
    _write_ticket(
        tickets_dir,
        "TICK-001",
        "open",
        touches=["a.py"],
        priority=1,
        parallel_safe=False,
    )
    _write_ticket(tickets_dir, "TICK-002", "open", touches=["b.py"], priority=2)
    _write_ticket(tickets_dir, "TICK-003", "open", touches=["c.py"], priority=3)

    # Mock the executor and lifecycle commands to complete quickly without
    # actually running code
    def fake_start(tid, cfg_, repo_root, **kwargs):
        pass

    def fake_invoke(ticket, cfg_, wt, **kwargs):
        pass

    def fake_complete(tid, cfg_, repo_root):
        pass

    def fake_review(tid, cfg_, repo_root, *, verdict=None, summary=None, findings=None):
        pass

    def fake_merge(tid, cfg_, repo_root):
        p = tickets_dir / f"{tid}.md"
        text = p.read_text().replace("status: in_progress", "status: merged")
        p.write_text(text)

    with (
        patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
        patch("lanegate.orchestrate.release_orchestrator_lock"),
        patch("lanegate.lifecycle.cmd_start", side_effect=fake_start),
        patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke),
        patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete),
        patch("lanegate.orchestrate.run_review_agent", return_value=True),
        patch("lanegate.lifecycle.cmd_merge", side_effect=fake_merge),
        patch("lanegate.orchestrate.spawn_watch_daemon"),
    ):
        cmd_orchestrate(cfg, tmp_path, all_milestones=True, auto_analyze=False)

    # Check that the log file was created and contains batch diagnostics
    logs_dir = tmp_path / ".lanegate" / "logs"
    log_files = list(logs_dir.glob("orchestrate-*.log"))
    assert len(log_files) > 0, "No log file created"

    log_path = log_files[0]
    log_content = log_path.read_text()

    # Verify that batch diagnostic lines appear in the log file
    assert "[orchestrate] batch:" in log_content, "Batch diagnostic line not found in log"
    # Should show "batch: 1 running of cap 3" when only TICK-001 runs (parallel_safe=false)
    # and TICK-002/003 are held due to lock or parallelism
    assert "running of cap 3" in log_content, "Batch capacity line not in log"


# ---------------------------------------------------------------------------
# failed status is a terminal status
# ---------------------------------------------------------------------------


class TestFailedStatusIsTerminal:
    def test_failed_is_in_terminal_statuses(self):
        from lanegate.ticket import TERMINAL_STATUSES

        assert "failed" in TERMINAL_STATUSES

    def test_failed_is_in_standard_statuses(self):
        from lanegate.ticket import _STANDARD_STATUSES

        assert "failed" in _STANDARD_STATUSES

    def test_failed_ticket_not_eligible_as_open(self, tmp_path):
        """A ticket with status=failed must not appear in next_batch."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "failed", touches=["a.py"])
        result = next_batch(cfg, tmp_path)
        assert result == []


# ---------------------------------------------------------------------------
