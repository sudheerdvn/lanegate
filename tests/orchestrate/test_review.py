"""
Tests for lanegate/orchestrate/review.py — review agent driver dispatch, combined mode handling.

Split out of the former monolithic tests/test_orchestrate.py (TICK-316).
"""

from __future__ import annotations

from tests.orchestrate.conftest import *  # noqa: F401,F403


@pytest.fixture(autouse=True)
def _compat_stream_subprocess(monkeypatch):
    def fake_stream(cmd, **kwargs):
        result = subprocess.run(cmd, cwd=kwargs.get("cwd"), capture_output=True, text=True, env=kwargs.get("env"))
        return result.returncode, result.stdout, getattr(result, "stderr", ""), None
    monkeypatch.setattr("lanegate.orchestrate.review._stream_subprocess", fake_stream)


# human_review validation
# ---------------------------------------------------------------------------


class TestHumanReviewValidation:
    def test_invalid_human_review_exits(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        with pytest.raises(SystemExit) as exc_info:
            cmd_orchestrate(cfg, tmp_path, human_review="invalid-value", all_milestones=True)
        assert exc_info.value.code == 1

    def test_invalid_default_human_review_in_config_exits(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        cfg["default_human_review"] = "invalid-value"
        with pytest.raises(SystemExit) as exc_info:
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)
        assert exc_info.value.code == 1


class TestDefaultHumanReviewConfigFallback:
    """TICK-253: default_human_review in .lanegate.yml gates auto-merge when
    --human-review isn't passed on the CLI; an explicit CLI flag still wins."""

    def _approved_ticket(self, tmp_path):
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

    @staticmethod
    def _fake_merge(tickets_dir):
        """auto_merge_approved_local_tickets loops (via `continue`) until it
        finds nothing left to merge -- a bare MagicMock for cmd_merge never
        changes the ticket's on-disk status, so it "merges" the same
        already-in_review ticket forever. Mirror what a real cmd_merge does
        (flip status to merged) so the loop terminates after one pass."""

        def fake_merge(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_review", "status: merged", 1))

        return fake_merge

    def test_config_default_per_ticket_blocks_auto_merge_when_cli_flag_absent(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        cfg["default_human_review"] = "per_ticket"
        self._approved_ticket(tmp_path)

        with (
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
            patch("lanegate.lifecycle.cmd_merge") as mock_merge,
            patch("lanegate.orchestrate.spawn_watch_daemon"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_merge.assert_not_called()

    def test_explicit_cli_none_overrides_config_default_per_ticket(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        cfg["default_human_review"] = "per_ticket"
        cfg["autonomy"] = "full"
        self._approved_ticket(tmp_path)

        with (
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
            patch(
                "lanegate.lifecycle.cmd_merge", side_effect=self._fake_merge(tmp_path / "tickets")
            ) as mock_merge,
            patch("lanegate.orchestrate.spawn_watch_daemon"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True, human_review="none")

        mock_merge.assert_called_once_with("TICK-001", cfg, tmp_path)

    def test_no_config_default_requires_human_merge(self, tmp_path):
        """Default supervised autonomy preserves approved tickets for a human merge."""
        cfg = _default_cfg(tmp_path)
        self._approved_ticket(tmp_path)

        with (
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
            patch("lanegate.lifecycle.cmd_merge") as mock_merge,
            patch("lanegate.orchestrate.spawn_watch_daemon"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_merge.assert_not_called()

    def test_supervised_approved_ticket_never_auto_merges(self, tmp_path):
        """The default supervised policy preserves tickets for a human merge."""
        cfg = _default_cfg(tmp_path)
        self._approved_ticket(tmp_path)
        ticket_path = tmp_path / "tickets" / "TICK-001.md"

        with (
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
            patch("lanegate.lifecycle.cmd_merge") as mock_merge,
            patch("lanegate.orchestrate.spawn_watch_daemon"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True, human_review="none")

        mock_merge.assert_not_called()
        ticket = parse_ticket(ticket_path)
        assert ticket["status"] == "in_review"
        assert ticket["review_verdict"] == "approved"


# ---------------------------------------------------------------------------
# run_review_agent — ConfigError from resolve_executor_env must be caught
# (TICK-088 second review round regression)
# ---------------------------------------------------------------------------


class TestRunReviewAgentConfigErrorFailClosed:
    """Regression: resolve_executor_env(get_executor_config(...)) can raise
    ConfigError (a named executor instance's api_key_env points at an unset
    env var, or its type has no known api-key-injection target such as
    'gemini'/'continue'). The first TICK-088 fix round made that raise
    correct in isolation, but the call was made BEFORE run_review_agent's own
    try: block starts — so the ConfigError escaped the function entirely
    instead of being caught by the `except Exception as exc:` handler whose
    docstring documents "any ... parse error ... all return False". This
    class proves that ConfigError is now caught at the same fail-closed
    boundary as any other review-agent error."""

    def _make_ticket(self) -> dict:
        return {
            "id": "TICK-997",
            "title": "Test ticket",
            "close_criteria": "Tests pass.",
            "_body": "",
        }

    def _patch_diff(self, diff_text: str = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n+x = 1\n"):
        return patch("lanegate.reviewer.get_worktree_diff", return_value=diff_text)

    def test_unsupported_executor_type_returns_false(self, tmp_path):
        """api_key_env configured on a named instance whose type ('gemini')
        has no entry in executor._DEFAULT_API_KEY_ENV_VAR."""
        ticket = self._make_ticket()
        cfg = {
            "trunk_branch": "main",
            "executor_steps": {"review": "gemini-1"},
            "executors": {
                "gemini-1": {"type": "gemini", "api_key_env": "SOME_GEMINI_KEY"},
            },
        }

        with (
            self._patch_diff(),
            patch("lanegate.config.load_config", return_value=cfg),
            patch("lanegate.orchestrate.subprocess.run") as mock_run,
            patch("lanegate.lifecycle.cmd_review") as mock_cmd_review,
            patch("lanegate.lifecycle._mark_needs_review") as mock_mark_needs_review,
        ):
            result = run_review_agent(ticket, tmp_path)

        assert result is False
        # get_commit_messages makes one real `git log` subprocess.run call
        # before the review dispatch is ever attempted (both go through the
        # same patched stdlib subprocess.run). The ConfigError must be raised
        # (and caught) before a second call — the actual review dispatch —
        # is made.
        assert mock_run.call_count == 1
        mock_cmd_review.assert_not_called()
        mock_mark_needs_review.assert_not_called()  # in-memory ticket has no status to persist

    def test_unset_api_key_env_var_returns_false(self, tmp_path, monkeypatch):
        """api_key_env names an env var that is not actually set."""
        monkeypatch.delenv("ANTHROPIC_API_KEY_REVIEW_1", raising=False)
        ticket = self._make_ticket()
        cfg = {
            "trunk_branch": "main",
            "executor_steps": {"review": "claude-review-1"},
            "executors": {
                "claude-review-1": {
                    "type": "claude-process",
                    "api_key_env": "ANTHROPIC_API_KEY_REVIEW_1",
                },
            },
        }

        with (
            self._patch_diff(),
            patch("lanegate.config.load_config", return_value=cfg),
            patch("lanegate.orchestrate.subprocess.run") as mock_run,
            patch("lanegate.lifecycle.cmd_review"),
            patch("lanegate.lifecycle._mark_needs_review") as mock_mark_needs_review,
        ):
            result = run_review_agent(ticket, tmp_path)

        assert result is False
        assert mock_run.call_count == 1  # only get_commit_messages' git log call
        mock_mark_needs_review.assert_not_called()

    def test_continue_type_with_api_key_env_returns_false(self, tmp_path):
        """Same unsupported-type gap as 'gemini', for the 'continue' executor
        type explicitly called out in the ticket's regression description."""
        ticket = self._make_ticket()
        cfg = {
            "trunk_branch": "main",
            "executor_steps": {"review": "continue-1"},
            "executors": {
                "continue-1": {"type": "continue", "api_key_env": "SOME_CONTINUE_KEY"},
            },
        }

        with (
            self._patch_diff(),
            patch("lanegate.config.load_config", return_value=cfg),
            patch("lanegate.orchestrate.subprocess.run") as mock_run,
            patch("lanegate.lifecycle.cmd_review"),
            patch("lanegate.lifecycle._mark_needs_review") as mock_mark_needs_review,
        ):
            result = run_review_agent(ticket, tmp_path)

        assert result is False
        assert mock_run.call_count == 1  # only get_commit_messages' git log call
        mock_mark_needs_review.assert_not_called()

    def test_malformed_driver_env_returns_false(self, tmp_path):
        """A malformed review driver env overlay must be caught fail-closed."""
        ticket = self._make_ticket()
        cfg = {
            "trunk_branch": "main",
            "drivers": {
                "bad-review-env": {
                    "type": "claude-process",
                    "env": ["BAD"],
                }
            },
            "steps": {"review": {"driver": "bad-review-env"}},
        }

        with (
            self._patch_diff(),
            patch("lanegate.config.load_config", return_value=cfg),
            patch("lanegate.orchestrate.subprocess.run") as mock_run,
            patch("lanegate.lifecycle.cmd_review") as mock_cmd_review,
            patch("lanegate.lifecycle._mark_needs_review") as mock_mark_needs_review,
        ):
            result = run_review_agent(ticket, tmp_path)

        assert result is False
        assert mock_run.call_count == 1  # only get_commit_messages' git log call
        mock_cmd_review.assert_not_called()
        mock_mark_needs_review.assert_not_called()


class TestRunReviewAgentDriverDispatch:
    def _make_ticket(self) -> dict:
        return {
            "id": "TICK-998",
            "title": "Review driver",
            "close_criteria": "Tests pass.",
            "_body": "",
        }

    def test_steps_review_driver_reaches_review_subprocess(self, tmp_path, monkeypatch):
        ticket = self._make_ticket()
        cfg = {
            "executor": "claude-process",
            "drivers": {
                "review-fast": {
                    "type": "claude-process",
                    "model": "review-driver-model",
                    "bin": "custom-review",
                    "flags": ["--driver-flag"],
                    "env": {"REVIEW_TOKEN": "${SOURCE_REVIEW_TOKEN}"},
                }
            },
            "steps": {"review": {"driver": "review-fast"}},
        }
        monkeypatch.setenv("SOURCE_REVIEW_TOKEN", "review-token")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((list(cmd), kwargs))
            if cmd[:2] == ["git", "log"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps({"verdict": "approved", "notes": "ok"}),
                stderr="",
            )

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.config.load_config", return_value=cfg),
            patch("lanegate.orchestrate.subprocess.run", side_effect=fake_run),
            patch("lanegate.lifecycle.cmd_review") as mock_cmd_review,
        ):
            result = run_review_agent(ticket, tmp_path)

        assert result is True
        # git log (commit messages) plus the run directory's git snapshots
        # bracket the one executor dispatch this test is about.
        non_git = [(cmd, kwargs) for cmd, kwargs in calls if cmd[0] != "git"]
        assert len(non_git) == 1
        review_cmd, review_kwargs = non_git[0]
        assert review_cmd[0] == "custom-review"
        assert "--driver-flag" in review_cmd
        assert "--model" in review_cmd
        assert review_cmd[review_cmd.index("--model") + 1] == "review-driver-model"
        assert review_kwargs["env"]["REVIEW_TOKEN"] == "review-token"
        assert mock_cmd_review.call_args.kwargs["verdict"] == "approved"

    def test_review_retries_rate_limited_pool_instance_on_healthy_sibling(self, tmp_path):
        """Split-mode review fails over to a healthy pool sibling on quota errors."""
        ticket = self._make_ticket()
        cfg = {
            "ticket_prefix": "TICK",
            "tickets_dir": "tickets",
            "executor": "claude-review-1",
            "executors": {
                "claude-review-1": {"type": "claude-process"},
                "claude-review-2": {"type": "claude-process"},
            },
            "pools": {
                "default": {
                    "executors": ["claude-review-1", "claude-review-2"],
                    "strategy": "round-robin",
                }
            },
            "default_pool": "default",
        }
        dispatched: list[str] = []

        def fake_build_executor_cmd(executor, prompt, cfg_, **kwargs):
            dispatched.append(executor)
            return [executor]

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["git", "log"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd == ["claude-review-1"]:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="rate limit exceeded")
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps({"verdict": "approved", "notes": "ok"}),
                stderr="",
            )

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.orchestrate.review.build_executor_cmd", side_effect=fake_build_executor_cmd),
            patch("lanegate.orchestrate.subprocess.run", side_effect=fake_run),
            patch("lanegate.lifecycle.cmd_review") as mock_cmd_review,
        ):
            result = run_review_agent(ticket, tmp_path, cfg=cfg)

        assert result is True
        assert dispatched == ["claude-review-1", "claude-review-2"]
        assert mock_cmd_review.call_args.kwargs["verdict"] == "approved"

    def test_review_unwraps_json_envelope_for_named_claude_instance(self, tmp_path):
        """A named executor instance (e.g. "claude-a") of type claude-process
        must still have its --output-format json envelope unwrapped before the
        verdict is extracted -- resolving via expand_driver() alone leaves the
        type as the raw instance name, which parse_structured_result's
        registry never matches, so the review verdict regex would otherwise
        run against the raw envelope and find no verdict at all.
        """
        ticket = self._make_ticket()
        cfg = {
            "ticket_prefix": "TICK",
            "tickets_dir": "tickets",
            "executor": "claude-a",
            "executors": {"claude-a": {"type": "claude-process"}},
        }
        envelope = json.dumps(
            {
                "session_id": "abc123",
                "result": json.dumps({"verdict": "approved", "notes": "ok"}),
            }
        )

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["git", "log"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout=envelope, stderr="")

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.orchestrate.subprocess.run", side_effect=fake_run),
            patch("lanegate.lifecycle.cmd_review") as mock_cmd_review,
        ):
            result = run_review_agent(ticket, tmp_path, cfg=cfg)

        assert result is True
        assert mock_cmd_review.call_args.kwargs["verdict"] == "approved"

    def test_chain_review_default_stays_independent(self, tmp_path):
        """TICK-310: review must NOT resume implement's session by default,
        even when a resumable implement_session_id is present on the ticket —
        independence is the point of the split-review pipeline."""
        ticket = self._make_ticket()
        ticket["implement_session_id"] = "sess-implement-1"
        cfg = {"executor": "claude-process"}
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if cmd[:2] == ["git", "log"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"verdict": "approved", "notes": "ok"}), stderr=""
            )

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.orchestrate.subprocess.run", side_effect=fake_run),
            patch("lanegate.lifecycle.cmd_review"),
        ):
            run_review_agent(ticket, tmp_path, cfg=cfg)

        review_cmd = next(c for c in calls if c[:1] != ["git"])
        assert "--resume" not in review_cmd

    def test_chain_review_resumes_implement_session_when_opted_in(self, tmp_path):
        """session_chaining.chain_review: true routes review through the same
        --resume mechanism as fix/drift_check."""
        ticket = self._make_ticket()
        ticket["implement_session_id"] = "sess-implement-1"
        cfg = {
            "executor": "claude-process",
            "session_chaining": {"chain_review": True},
        }
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if cmd[:2] == ["git", "log"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"verdict": "approved", "notes": "ok"}), stderr=""
            )

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.orchestrate.subprocess.run", side_effect=fake_run),
            patch("lanegate.lifecycle.cmd_review"),
            patch("lanegate.context_log._get_default_db_path", return_value=tmp_path / "analytics.db"),
        ):
            run_review_agent(ticket, tmp_path, cfg=cfg)

        review_cmd = next(c for c in calls if c[:1] != ["git"])
        assert "--resume" in review_cmd
        assert review_cmd[review_cmd.index("--resume") + 1] == "sess-implement-1"

    def test_chain_review_opted_in_but_gate_blocks_stale_session(self, tmp_path):
        from lanegate.context_log import log_step_cost

        ticket = self._make_ticket()
        ticket["implement_session_id"] = "sess-stale"
        cfg = {
            "executor": "claude-process",
            "session_chaining": {"chain_review": True},
        }
        db_path = tmp_path / "analytics.db"
        log_step_cost(
            db_path, "proj", ticket["id"], "implement",
            session_id="sess-stale", timestamp="2020-01-01T00:00:00Z",
        )
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if cmd[:2] == ["git", "log"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"verdict": "approved", "notes": "ok"}), stderr=""
            )

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.orchestrate.subprocess.run", side_effect=fake_run),
            patch("lanegate.lifecycle.cmd_review"),
            patch("lanegate.context_log._get_default_db_path", return_value=db_path),
            patch("lanegate.context_log._get_project_id", return_value="proj"),
        ):
            run_review_agent(ticket, tmp_path, cfg=cfg)

        review_cmd = next(c for c in calls if c[:1] != ["git"])
        assert "--resume" not in review_cmd


# ---------------------------------------------------------------------------
# TICK-345: review independence ladder — never self-review a pool-dispatched
# implementer when an alternative exists; degrade (never block) when it
# doesn't.
# ---------------------------------------------------------------------------


class TestReviewIndependenceLadder:
    def _make_ticket(self) -> dict:
        return {
            "id": "TICK-345",
            "title": "Review independence",
            "close_criteria": "Tests pass.",
            "_body": "",
        }

    @staticmethod
    def _fake_build_executor_cmd(executor, prompt, cfg_, **kwargs):
        return [executor]

    @staticmethod
    def _fake_run(cmd, **kwargs):
        if cmd[:2] == ["git", "log"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"verdict": "approved", "notes": "ok"}), stderr=""
        )

    @pytest.mark.parametrize("pool_order", [["claude-a", "claude-b"], ["claude-b", "claude-a"]])
    def test_pool_dispatch_never_selects_the_implementer(self, tmp_path, pool_order):
        """Rung 1 (independent): with a two-instance pool and ticket["executor"]
        pinned to one of them, review must land on the other — for both
        orderings of the pool list, so this cannot pass by an accidental
        list-order tie-break."""
        ticket = self._make_ticket()
        ticket["executor"] = "claude-a"
        cfg = {
            "ticket_prefix": "TICK",
            "tickets_dir": "tickets",
            "executor": "claude-a",
            "executors": {
                "claude-a": {"type": "claude-process"},
                "claude-b": {"type": "claude-process"},
            },
            "pools": {"default": {"executors": pool_order}},
            "default_pool": "default",
        }
        dispatched: list[str] = []

        def fake_build(executor, prompt, cfg_, **kwargs):
            dispatched.append(executor)
            return [executor]

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.orchestrate.review.build_executor_cmd", side_effect=fake_build),
            patch("lanegate.orchestrate.subprocess.run", side_effect=self._fake_run),
            patch("lanegate.lifecycle.cmd_review") as mock_cmd_review,
        ):
            result = run_review_agent(ticket, tmp_path, cfg=cfg)

        assert result is True
        assert dispatched == ["claude-b"]
        assert mock_cmd_review.call_args.kwargs["verdict"] == "approved"
        assert mock_cmd_review.call_args.kwargs["review_independence"] == "independent"

    def test_degrades_to_different_model_when_no_alternative_instance_exists(self, tmp_path):
        """Rung 2 (different-model): a single-account config has no sibling
        instance to hand review to, but a distinct review model is
        configured -- that is a genuinely different reviewer, so it must be
        used and labeled rather than falling straight to self-review."""
        ticket = self._make_ticket()
        ticket["executor"] = "claude-a"
        cfg = {
            "ticket_prefix": "TICK",
            "tickets_dir": "tickets",
            "executor": "claude-a",
            "executors": {"claude-a": {"type": "claude-process"}},
            "models": {"implement": "model-implement", "review": "model-review"},
        }

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch(
                "lanegate.orchestrate.review.build_executor_cmd",
                side_effect=self._fake_build_executor_cmd,
            ),
            patch("lanegate.orchestrate.subprocess.run", side_effect=self._fake_run),
            patch("lanegate.lifecycle.cmd_review") as mock_cmd_review,
        ):
            result = run_review_agent(ticket, tmp_path, cfg=cfg)

        assert result is True
        assert mock_cmd_review.call_args.kwargs["verdict"] == "approved"
        assert mock_cmd_review.call_args.kwargs["review_independence"] == "different-model"
        assert mock_cmd_review.call_args.kwargs["review_model"] == "model-review"

    def test_degrades_to_self_review_and_warns_when_truly_no_alternative(self, tmp_path, capsys):
        """Rung 3 (self): single account, no pool, no distinct review model --
        there is genuinely no alternative, so review must proceed on the
        implementer rather than stalling, and must warn about it."""
        ticket = self._make_ticket()
        ticket["executor"] = "claude-a"
        cfg = {
            "ticket_prefix": "TICK",
            "tickets_dir": "tickets",
            "executor": "claude-a",
            "executors": {"claude-a": {"type": "claude-process"}},
        }

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch(
                "lanegate.orchestrate.review.build_executor_cmd",
                side_effect=self._fake_build_executor_cmd,
            ),
            patch("lanegate.orchestrate.subprocess.run", side_effect=self._fake_run),
            patch("lanegate.lifecycle.cmd_review") as mock_cmd_review,
        ):
            result = run_review_agent(ticket, tmp_path, cfg=cfg)

        assert result is True
        assert mock_cmd_review.call_args.kwargs["verdict"] == "approved"
        assert mock_cmd_review.call_args.kwargs["review_independence"] == "self"
        captured = capsys.readouterr()
        assert "no independent reviewer available" in captured.err
        assert "claude-a" in captured.err

    def test_single_member_pool_degrades_same_as_no_pools_configured(self, tmp_path, capsys):
        """A `pools` key with exactly one executor -- the shape most real users
        with a single account will actually have -- must degrade the same way
        as no `pools` key at all (rung 3: self-review with a warning), not
        stall or silently skip the warning because a pool happens to be
        configured."""
        ticket = self._make_ticket()
        ticket["executor"] = "claude-a"
        cfg = {
            "ticket_prefix": "TICK",
            "tickets_dir": "tickets",
            "executor": "claude-a",
            "executors": {"claude-a": {"type": "claude-process"}},
            "pools": {"default": {"executors": ["claude-a"]}},
            "default_pool": "default",
        }

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch(
                "lanegate.orchestrate.review.build_executor_cmd",
                side_effect=self._fake_build_executor_cmd,
            ),
            patch("lanegate.orchestrate.subprocess.run", side_effect=self._fake_run),
            patch("lanegate.lifecycle.cmd_review") as mock_cmd_review,
        ):
            result = run_review_agent(ticket, tmp_path, cfg=cfg)

        assert result is True
        assert mock_cmd_review.call_args.kwargs["verdict"] == "approved"
        assert mock_cmd_review.call_args.kwargs["review_independence"] == "self"
        captured = capsys.readouterr()
        assert "no independent reviewer available" in captured.err
        assert "claude-a" in captured.err

    def test_pinned_reviewer_matching_implementer_warns_but_is_not_overridden(self, tmp_path, capsys):
        """A per-ticket reviewer: pin always wins outright, even when it
        equals the implementer -- but that coincidence must be surfaced with
        a warning rather than silently producing an unlabeled self-review."""
        ticket = self._make_ticket()
        ticket["executor"] = "claude-a"
        ticket["reviewer"] = "claude-a"
        cfg = {
            "ticket_prefix": "TICK",
            "tickets_dir": "tickets",
            "executor": "claude-a",
            "executors": {
                "claude-a": {"type": "claude-process"},
                "claude-b": {"type": "claude-process"},
            },
            "pools": {"default": {"executors": ["claude-a", "claude-b"]}},
            "default_pool": "default",
        }

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch(
                "lanegate.orchestrate.review.build_executor_cmd",
                side_effect=self._fake_build_executor_cmd,
            ),
            patch("lanegate.orchestrate.subprocess.run", side_effect=self._fake_run),
            patch("lanegate.lifecycle.cmd_review") as mock_cmd_review,
        ):
            result = run_review_agent(ticket, tmp_path, cfg=cfg)

        assert result is True
        assert mock_cmd_review.call_args.kwargs["verdict"] == "approved"
        assert mock_cmd_review.call_args.kwargs["review_independence"] == "self"
        captured = capsys.readouterr()
        assert "reviewer pinned to 'claude-a'" in captured.err
        assert "same executor that implemented" in captured.err


# ---------------------------------------------------------------------------
# Combined mode board-clearing loop integration
# ---------------------------------------------------------------------------


class TestCombinedModeBoardClearingLoop:
    """Integration tests: combined mode skips separate cmd_complete + review subprocess."""

    def _make_open_ticket(self, tmp_path: Path) -> Path:
        tickets_dir = tmp_path / "tickets"
        return _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"])

    def test_combined_mode_skips_cmd_complete_and_review(self, tmp_path):
        """In combined mode, cmd_complete and run_review_agent are NOT called."""
        cfg = _default_cfg(tmp_path)
        cfg["reviewer"] = cfg["executor"]
        tickets_dir = tmp_path / "tickets"
        # Default config → combined mode (same executor for implement+review)
        self._make_open_ticket(tmp_path)

        def fake_invoke_combined(ticket, cfg_, wt, *, log_stream=None, terminal_stream=None, prompt_override=None, repo_root=None, executor_override=None):
            # Simulate the combined executor calling `lanegate complete` internally
            # by flipping the ticket status to code_complete.
            p = tickets_dir / f"{ticket['id']}.md"
            text = p.read_text().replace("status: open", "status: code_complete")
            p.write_text(text)
            return (0, "", "")

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke_combined),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.lifecycle.cmd_complete") as mock_complete,
            patch("lanegate.orchestrate.run_review_agent") as mock_review_agent,
            patch("lanegate.lifecycle.cmd_review") as mock_review,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_complete.assert_not_called()
        mock_review_agent.assert_not_called()
        mock_review.assert_not_called()

    def test_combined_mode_approved_in_review_is_not_downgraded(self, tmp_path):
        """TICK-266 regression: when the combined-mode executor correctly runs
        `lanegate complete && lanegate review --verdict approved`, leaving the ticket
        at status=in_review/review_verdict=approved, orchestrate must accept
        this as success (and proceed to auto-merge) instead of falling through
        the "unhandled combined-mode state" branch and force-downgrading it to
        needs_review/changes_requested."""
        cfg = _default_cfg(tmp_path)
        cfg["reviewer"] = cfg["executor"]
        cfg["autonomy"] = "full"
        tickets_dir = tmp_path / "tickets"
        self._make_open_ticket(tmp_path)

        def fake_start(tid, cfg_, repo_root, *, interactive=False):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: open", "status: in_progress"))

        def fake_invoke_combined(ticket, cfg_, wt, *, log_stream=None, terminal_stream=None, prompt_override=None, repo_root=None, executor_override=None):
            # Simulate the combined executor calling `lanegate complete && lanegate
            # review --verdict approved` internally after cmd_start put the
            # ticket into in_progress. The post-execution gate must re-read
            # this durable update rather than inspecting its stale in-memory
            # in_progress ticket.
            p = tickets_dir / f"{ticket['id']}.md"
            text = p.read_text().replace(
                "status: in_progress", "status: in_review\nreview_verdict: approved"
            )
            p.write_text(text)
            return (0, "", "")

        def fake_merge(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            text = p.read_text().replace("status: in_review", "status: merged")
            p.write_text(text)

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=fake_start),
            patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke_combined),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.lifecycle.cmd_complete") as mock_complete,
            patch("lanegate.orchestrate.run_review_agent") as mock_review_agent,
            patch("lanegate.lifecycle.cmd_review") as mock_review,
            patch("lanegate.lifecycle.cmd_merge", side_effect=fake_merge) as mock_merge,
            patch("lanegate.orchestrate._run_acceptance_contract_audit", return_value=[]),
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        # The executor's own complete+review calls are what advanced the
        # ticket — orchestrate must not re-invoke complete/review itself.
        mock_complete.assert_not_called()
        mock_review_agent.assert_not_called()
        mock_review.assert_not_called()
        # Success falls through to the normal in_review handling: auto-merge
        # (human_review defaults to "none" in _default_cfg).
        mock_merge.assert_called_once_with("TICK-001", cfg, tmp_path)

        from lanegate.ticket import parse_ticket

        t = parse_ticket(tickets_dir / "TICK-001.md")
        assert t["status"] == "merged"
        assert t["review_verdict"] == "approved"

    def test_combined_mode_changes_requested_pauses_instead_of_done(self, tmp_path):
        """TICK-120 Slice 0 regression: when the combined-mode executor's own
        `lanegate review --verdict changes_requested` call leaves the ticket at
        code_complete, the orchestrator must pause — not silently fall through
        to `_status(tid, "done", ...)` and move on as if nothing happened."""
        cfg = _default_cfg(tmp_path)
        cfg["reviewer"] = cfg["executor"]
        tickets_dir = tmp_path / "tickets"
        self._make_open_ticket(tmp_path)

        def fake_invoke_combined(ticket, cfg_, wt, *, log_stream=None, terminal_stream=None, prompt_override=None, repo_root=None, executor_override=None):
            # Simulate the combined executor calling `lanegate complete && lanegate
            # review --verdict changes_requested` internally.
            p = tickets_dir / f"{ticket['id']}.md"
            text = p.read_text().replace(
                "status: open", "status: code_complete\nreview_verdict: changes_requested"
            )
            p.write_text(text)
            return (0, "", "")

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke_combined),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.lifecycle.cmd_complete") as mock_complete,
            patch("lanegate.orchestrate.run_review_agent") as mock_review_agent,
            patch("lanegate.lifecycle.cmd_review") as mock_review,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_complete.assert_not_called()
        mock_review_agent.assert_not_called()
        mock_review.assert_not_called()

        from lanegate.ticket import parse_ticket

        t = parse_ticket(tickets_dir / "TICK-001.md")
        # Must still be code_complete/changes_requested — not silently
        # advanced past review, and not hibernated/failed either.
        assert t["status"] == "code_complete"
        assert t["review_verdict"] == "changes_requested"

    def test_combined_mode_executor_no_verdict_pauses_with_error(self, tmp_path, capsys):
        """F7 fix: when the combined-mode executor runs `lanegate complete` but NOT
        `lanegate review --verdict`, the ticket ends up in code_complete with no
        verdict. The orchestrator must detect this unhandled state, pause, and
        report an error — not silently mark as done and wedge the board."""
        cfg = _default_cfg(tmp_path)
        cfg["reviewer"] = cfg["executor"]
        tickets_dir = tmp_path / "tickets"
        self._make_open_ticket(tmp_path)

        def fake_invoke_incomplete(
            ticket,
            cfg_,
            wt,
            *,
            log_stream=None,
            terminal_stream=None,
            prompt_override=None,
            repo_root=None,
            executor_override=None,
        ):
            # Simulate the combined executor crashing mid-session, after complete but before review
            p = tickets_dir / f"{ticket['id']}.md"
            text = p.read_text().replace("status: open", "status: code_complete")
            p.write_text(text)
            return 0, "", ""

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke_incomplete),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.lifecycle.cmd_complete") as mock_complete,
            patch("lanegate.lifecycle.cmd_review") as mock_review,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        # In combined mode, cmd_complete and cmd_review should NOT be called
        # (the executor is responsible for them)
        mock_complete.assert_not_called()
        mock_review.assert_not_called()

        # Check error message in stderr
        captured = capsys.readouterr()
        assert "unhandled state" in captured.err
        assert "combined-mode executor exited 0" in captured.err
        # Verify the error message describes the actual state
        assert "status=code_complete" in captured.err
        assert "verdict=None" in captured.err

        # Ticket must remain in code_complete, not advanced to merged
        t = parse_ticket(tickets_dir / "TICK-001.md")
        assert t["status"] == "code_complete"

    def test_combined_mode_outside_touches_pauses_if_already_in_review(
        self, tmp_path, capsys
    ):
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        self._make_open_ticket(tmp_path)

        def fake_invoke_combined(ticket, cfg_, wt, *, log_stream=None, terminal_stream=None, prompt_override=None, repo_root=None, executor_override=None):
            p = tickets_dir / f"{ticket['id']}.md"
            text = p.read_text().replace(
                "status: open", "status: in_review\nreview_verdict: approved"
            )
            p.write_text(text)
            return (0, "", "")

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke_combined),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._committed_files", return_value={"a.py", "other.py"}),
            patch("lanegate.lifecycle.cmd_needs_review") as mock_needs_review,
            patch("lanegate.lifecycle.cmd_complete") as mock_complete,
            patch("lanegate.lifecycle.cmd_review") as mock_review,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_needs_review.assert_not_called()
        mock_complete.assert_not_called()
        mock_review.assert_not_called()
        captured = capsys.readouterr()
        assert "approved review invalidated by gate" in captured.err
        assert "committed files outside touches list: other.py" in captured.err

    def test_split_mode_calls_cmd_complete_and_review(self, tmp_path):
        """In split mode, cmd_complete IS called and review follows normal flow."""
        cfg = _default_cfg(tmp_path)
        cfg["executor_steps"] = {"implement": "codex", "review": "claude"}
        tickets_dir = tmp_path / "tickets"
        self._make_open_ticket(tmp_path)

        def fake_complete(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            text = p.read_text().replace("status: open", "status: code_complete")
            p.write_text(text)

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", return_value=(0, "", "")),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete) as mock_complete,
            patch("lanegate.lifecycle.cmd_review") as mock_review,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_complete.assert_called_once()
        mock_review.assert_called_once()

    def test_split_mode_skips_review_when_complete_routes_to_needs_review(self, tmp_path, capsys):
        """cmd_complete can route a ticket straight to needs_review (e.g. a failed
        pre_complete safeguard) and return without raising. The loop must notice
        the ticket never reached code_complete and skip review dispatch --
        otherwise a reviewer runs (and can even approve) a ticket the ticket
        itself already gave up on, leaving an orphaned verdict no one applies.
        """
        cfg = _default_cfg(tmp_path)
        cfg["executor_steps"] = {"implement": "codex", "review": "claude"}
        tickets_dir = tmp_path / "tickets"
        self._make_open_ticket(tmp_path)

        def fake_complete(tid, cfg_, repo_root):
            # Mirrors cmd_complete's real behavior when pre_complete safeguards
            # fail: it marks the ticket needs_review and just returns.
            p = tickets_dir / f"{tid}.md"
            text = p.read_text().replace("status: open", "status: needs_review")
            p.write_text(text)

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", return_value=(0, "", "")),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete) as mock_complete,
            patch("lanegate.lifecycle.cmd_review") as mock_review,
            patch("lanegate.orchestrate.run_review_agent") as mock_review_agent,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_complete.assert_called_once()
        mock_review.assert_not_called()
        mock_review_agent.assert_not_called()
        ticket = parse_ticket(tickets_dir / "TICK-001.md")
        assert ticket["status"] == "needs_review"

    def test_human_review_none_auto_merges_after_approval(self, tmp_path):
        """Default no-human-review mode auto-approves and merges local tickets."""
        cfg = _default_cfg(tmp_path)
        cfg["autonomy"] = "full"
        tickets_dir = tmp_path / "tickets"
        self._make_open_ticket(tmp_path)

        def fake_complete(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            text = p.read_text().replace("status: open", "status: code_complete")
            p.write_text(text)

        def fake_review(tid, cfg_, repo_root, *, verdict=None, summary=None, findings=None):
            p = tickets_dir / f"{tid}.md"
            text = p.read_text().replace(
                "status: code_complete",
                "status: in_review\nreview_verdict: approved",
            )
            p.write_text(text)

        def fake_merge(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            text = p.read_text().replace("status: in_review", "status: merged")
            p.write_text(text)

        def fake_review_agent(ticket, *_args, **_kwargs):
            fake_review(ticket["id"], cfg, tmp_path, verdict="approved")
            return True

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", return_value=(0, "", "")),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete),
            patch("lanegate.lifecycle.cmd_review", side_effect=fake_review),
            patch("lanegate.orchestrate.run_review_agent", side_effect=fake_review_agent),
            patch("lanegate.lifecycle.cmd_merge", side_effect=fake_merge) as mock_merge,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_merge.assert_called_once_with("TICK-001", cfg, tmp_path)

    def test_combined_mode_combined_prompt_passed_to_executor(self, tmp_path):
        """In combined mode, invoke_executor receives the combined prompt (not the plain implement prompt)."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        self._make_open_ticket(tmp_path)

        captured_prompt = []

        def fake_invoke(ticket, cfg_, wt, *, log_stream=None, terminal_stream=None, prompt_override=None, repo_root=None, executor_override=None):
            captured_prompt.append(prompt_override)
            # Simulate executor calling `lanegate complete` to prevent infinite loop
            p = tickets_dir / f"{ticket['id']}.md"
            text = p.read_text().replace("status: open", "status: code_complete")
            p.write_text(text)
            return (0, "", "")

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._is_combined_mode", return_value=True),
            patch("lanegate.lifecycle.cmd_complete"),
            patch("lanegate.lifecycle.cmd_review"),
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        assert captured_prompt, "invoke_executor was not called"
        prompt = captured_prompt[0]
        assert prompt is not None, "prompt_override should be set in combined mode"
        assert "lanegate complete TICK-001" in prompt
        assert "lanegate review TICK-001 --verdict" in prompt

    def test_split_mode_no_prompt_override(self, tmp_path):
        """In split mode, invoke_executor receives prompt_override=None (plain implement prompt)."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        self._make_open_ticket(tmp_path)

        captured_prompt = []

        def fake_invoke(ticket, cfg_, wt, *, log_stream=None, terminal_stream=None, prompt_override=None, repo_root=None, executor_override=None):
            captured_prompt.append(prompt_override)
            return (0, "", "")

        def fake_complete(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            text = p.read_text().replace("status: open", "status: code_complete")
            p.write_text(text)

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete),
            patch("lanegate.lifecycle.cmd_review"),
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        assert captured_prompt, "invoke_executor was not called"
        assert captured_prompt[0] is None, "prompt_override should be None in split mode"

    def test_split_mode_uses_resolve_executor_for_review(self, tmp_path):
        """run_review_agent uses resolve_executor(cfg, 'review') — not hardcoded 'claude'."""
        # This is validated by checking run_review_agent is called in split mode
        # and that build_executor_cmd is invoked with the resolved executor
        cfg = _default_cfg(tmp_path)
        cfg["reviewer"] = "claude-process"
        tickets_dir = tmp_path / "tickets"
        self._make_open_ticket(tmp_path)

        def fake_complete(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            text = p.read_text().replace("status: open", "status: code_complete")
            p.write_text(text)

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", return_value=(0, "", "")),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete),
            patch("lanegate.orchestrate.run_review_agent", return_value=True) as mock_review_agent,
            patch("lanegate.lifecycle.cmd_review"),
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True, human_review="per_ticket")

        mock_review_agent.assert_called_once()

    def test_human_reviewer_pauses_without_review_agent(self, tmp_path):
        """reviewer: human completes work, records in_review, and pauses for a human verdict."""
        cfg = _default_cfg(tmp_path)
        cfg["reviewer"] = "human"
        tickets_dir = tmp_path / "tickets"
        self._make_open_ticket(tmp_path)

        def fake_complete(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            text = p.read_text().replace("status: open", "status: code_complete")
            p.write_text(text)

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", return_value=(0, "", "")),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete),
            patch("lanegate.orchestrate.run_review_agent") as mock_review_agent,
            patch("lanegate.lifecycle.cmd_review") as mock_review,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True, human_review="per_ticket")

        mock_review_agent.assert_not_called()
        mock_review.assert_called_once_with("TICK-001", cfg, tmp_path, review_driver="human")


def test_codex_reviewer_runs_without_human_gate(tmp_path):
    """Test that configured non-human reviewer runs even when human_review is 'none'."""
    from unittest import mock
    from lanegate.orchestrate.loop import cmd_orchestrate
    from lanegate.ticket import parse_ticket

    cfg = _default_cfg(tmp_path)
    tickets_dir = Path(cfg["tickets_dir"])
    t_path = _write_ticket(
        tickets_dir,
        "TICK-901",
        "open",
        touches=["a.py"],
    )
    t_text = t_path.read_text(encoding="utf-8")
    t_path.write_text(t_text.replace("---\n", "---\nmilestone: v1.5\nreviewer: codex\n", 1), encoding="utf-8")

    cfg["default_human_review"] = "none"

    def _fake_complete_writes_code_complete(tid_, cfg_, repo_root_, **kwargs):
        tickets_dir_ = Path(repo_root_) / cfg_["tickets_dir"]
        p_ = tickets_dir_ / f"{tid_}.md"
        p_.write_text(p_.read_text().replace("status: in_progress", "status: code_complete", 1))

    with patch("lanegate.lifecycle.cmd_start", side_effect=_fake_start_writes_in_progress):
        with patch("lanegate.lifecycle.cmd_complete", side_effect=_fake_complete_writes_code_complete):
            with patch("lanegate.orchestrate.invoke_executor", return_value=(0, "", "")):
                with patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True):
                    with patch("lanegate.orchestrate.run_review_agent", return_value=True) as mock_review:
                        cmd_orchestrate(cfg, repo_root=tmp_path, human_review="none", all_milestones=True)
                        mock_review.assert_called_once()
                        passed_ticket = mock_review.call_args[0][0]
                        assert passed_ticket["reviewer"] == "codex"


def test_supervised_ticket_waits_for_human_merge_after_llm_approval(tmp_path):
    """Supervised means LLM-reviewed and fixed, with a human-only merge gate."""
    cfg = _default_cfg(tmp_path)
    cfg["default_human_review"] = "none"
    tickets_dir = Path(cfg["tickets_dir"])
    ticket_path = _write_ticket(tickets_dir, "TICK-902", "open", touches=["a.py"])
    ticket_path.write_text(
        ticket_path.read_text().replace("---\n", "---\nautonomy: supervised\nreviewer: codex\n", 1)
    )

    def fake_complete(tid, cfg_, repo_root, **_kwargs):
        path = Path(repo_root) / cfg_["tickets_dir"] / f"{tid}.md"
        path.write_text(path.read_text().replace("status: in_progress", "status: code_complete", 1))

    with (
        patch("lanegate.lifecycle.cmd_start", side_effect=_fake_start_writes_in_progress),
        patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete),
        patch("lanegate.orchestrate.invoke_executor", return_value=(0, "", "")),
        patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
        patch("lanegate.orchestrate.run_review_agent", return_value=True),
        patch("lanegate.lifecycle.cmd_merge") as merge,
    ):
        cmd_orchestrate(cfg, repo_root=tmp_path, human_review="none", all_milestones=True)

    merge.assert_not_called()


# TICK-343: review runs leave an auditable run directory
# ---------------------------------------------------------------------------


class TestReviewRunDirectory:
    """A review verdict used to be three ticket fields and nothing else.

    These assert the review step now produces the same run-directory evidence
    the implement step has always produced, so a verdict can be checked rather
    than only trusted.
    """

    def _make_ticket(self) -> dict:
        return {
            "id": "TICK-343",
            "title": "Auditable reviews",
            "close_criteria": "Tests pass.",
            "_body": "",
        }

    def _cfg(self) -> dict:
        return {
            "ticket_prefix": "TICK",
            "tickets_dir": "tickets",
            "executor": "claude-process",
            "models": {"review": "review-model"},
            "steps": {
                "implement": {"driver": "claude-process"},
                "review": {"driver": "codex"},
            },
        }

    def _run(self, tmp_path, monkeypatch, stdout: str, cfg: dict | None = None):
        """Run a split-mode review whose executor emits ``stdout``."""
        worktree = tmp_path / "wt"
        worktree.mkdir()

        def fake_stream(cmd, **kwargs):
            on_line = kwargs.get("on_line")
            if on_line is not None:
                for line in stdout.splitlines():
                    on_line(line, True)
            # Mirrors _stream_subprocess's documented fallback: with no
            # out_stream, raw executor output lands on the real stdout.
            import sys as _sys

            (kwargs.get("out_stream") or _sys.stdout).write(stdout)
            return 0, stdout, "", None

        monkeypatch.setattr("lanegate.orchestrate.review._stream_subprocess", fake_stream)
        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.reviewer.get_commit_messages", return_value="commit msg"),
            patch("lanegate.lifecycle.cmd_review") as mock_cmd_review,
        ):
            approved = run_review_agent(
                self._make_ticket(), tmp_path, worktree_path=worktree, cfg=cfg or self._cfg()
            )
        return approved, mock_cmd_review

    @staticmethod
    def _bundle(tmp_path) -> Path:
        runs = sorted((tmp_path / ".lanegate" / "executor-runs" / "TICK-343").iterdir())
        assert len(runs) == 1, f"expected exactly one review run directory, got {runs}"
        return runs[0]

    @pytest.mark.parametrize(
        "verdict,expected_approved",
        [("approved", True), ("changes_requested", False)],
    )
    def test_review_writes_run_directory(
        self, tmp_path, monkeypatch, verdict, expected_approved
    ):
        payload = json.dumps({"verdict": verdict, "notes": "why", "findings": "f1"})
        approved, _ = self._run(tmp_path, monkeypatch, payload)
        assert approved is expected_approved

        bundle = self._bundle(tmp_path)
        assert bundle.name.endswith("-review")
        assert (bundle / "prompt.md").read_text()
        assert (bundle / "captured-output.txt").exists()

        status = json.loads((bundle / "status.json").read_text())
        assert status["step"] == "review"
        assert status["mode"] == "split"
        assert status["exit_code"] == 0
        assert status["resolved_model"] == "review-model"
        assert "elapsed_seconds" in status

        recorded = json.loads((bundle / "verdict.json").read_text())
        assert recorded["verdict"] == verdict
        assert recorded["model"] == "review-model"

    def test_review_records_split_mode_when_reviewer_differs_from_implementer(
        self, tmp_path, monkeypatch
    ):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        cfg = {
            "ticket_prefix": "TICK",
            "tickets_dir": "tickets",
            "executor": "claude-process",
            "steps": {"implement": {"driver": "claude-process"}, "review": {"driver": "codex"}},
        }
        payload = json.dumps({"verdict": "approved", "notes": "ok"})

        monkeypatch.setattr(
            "lanegate.orchestrate.review._stream_subprocess",
            lambda cmd, **kwargs: (0, payload, "", None),
        )
        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.reviewer.get_commit_messages", return_value="commit msg"),
            patch("lanegate.lifecycle.cmd_review"),
        ):
            run_review_agent(self._make_ticket(), tmp_path, worktree_path=worktree, cfg=cfg)

        status = json.loads((self._bundle(tmp_path) / "status.json").read_text())
        assert status["mode"] == "split"

    def test_secret_shaped_review_output_is_redacted_before_reaching_disk(
        self, tmp_path, monkeypatch
    ):
        secret = "sk-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        payload = (
            f"leaked api_key={secret}\n"
            + json.dumps({"verdict": "approved", "notes": "ok"})
        )
        self._run(tmp_path, monkeypatch, payload)

        captured = (self._bundle(tmp_path) / "captured-output.txt").read_text()
        assert secret not in captured
        assert "[REDACTED]" in captured

    def test_prompt_audit_failure_does_not_abort_review(self, tmp_path, monkeypatch):
        """A read-only prompt directory must not turn an approval into a failure."""
        monkeypatch.setattr(
            "lanegate.orchestrate.pool._write_prompt_file",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read-only")),
        )
        approved, _ = self._run(
            tmp_path, monkeypatch, json.dumps({"verdict": "approved", "notes": "ok"})
        )

        assert approved is True
        bundle = self._bundle(tmp_path)
        assert not (bundle / "prompt.md").exists()
        assert (bundle / "verdict.json").exists()

    def test_diff_extraction_failure_routes_to_needs_review(self, tmp_path):
        from lanegate.reviewer import ReviewError

        with (
            patch("lanegate.reviewer.get_worktree_diff", side_effect=ReviewError("no commits")),
            patch("lanegate.lifecycle.cmd_review") as mock_cmd_review,
            patch("lanegate.lifecycle._mark_needs_review") as mock_mark_needs_review,
        ):
            approved = run_review_agent(
                self._make_ticket(), tmp_path, worktree_path=tmp_path, cfg=self._cfg()
            )

        assert approved is False
        mock_cmd_review.assert_not_called()
        mock_mark_needs_review.assert_not_called()

    def test_subprocess_crash_is_needs_review_and_cannot_merge(self, tmp_path, monkeypatch):
        """A reviewer crash is infrastructure failure, not a substantive
        rejection eligible for auto-fix or merge."""
        cfg = _default_cfg(tmp_path)
        ticket_path = _write_ticket(tmp_path / "tickets", "TICK-343", "code_complete")
        ticket = parse_ticket(ticket_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        monkeypatch.setattr(
            "lanegate.orchestrate.review._stream_subprocess",
            lambda *args, **kwargs: (1, "connection reset", "", None),
        )

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.reviewer.get_commit_messages", return_value="commit msg"),
            patch("lanegate.lifecycle.cmd_review") as mock_cmd_review,
        ):
            assert run_review_agent(ticket, tmp_path, worktree_path=worktree, cfg=cfg) is False

        refreshed = parse_ticket(ticket_path)
        assert refreshed["status"] == "needs_review"
        assert refreshed.get("review_verdict") != "changes_requested"
        assert "Reviewer harness error" in refreshed["_body"]
        mock_cmd_review.assert_not_called()

        from lanegate.lifecycle import cmd_merge

        with pytest.raises(SystemExit):
            cmd_merge("TICK-343", cfg, tmp_path)

    def test_successful_but_unparseable_review_is_recorded_as_harness_error(
        self, tmp_path, monkeypatch
    ):
        approved, mock_cmd_review = self._run(
            tmp_path, monkeypatch, "I reviewed the change, but omitted the verdict JSON."
        )

        assert approved is False
        mock_cmd_review.assert_not_called()
        recorded = json.loads((self._bundle(tmp_path) / "verdict.json").read_text())
        assert recorded["verdict"] == "error"
        assert "no JSON verdict" in recorded["notes"]

    def test_stream_json_becomes_events_and_never_reaches_stdout(
        self, tmp_path, monkeypatch, capsys
    ):
        envelope = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "foo.py"}}
                    ]
                },
            }
        )
        payload = envelope + "\n" + json.dumps({"verdict": "approved", "notes": "ok"})
        # Events are durable only within an active orchestrate run, which is
        # what the TUI reads.
        from lanegate.orchestrate.run_report import _write_last_run_pointer

        session_ts = "20260801-120000"
        (tmp_path / ".lanegate" / "logs").mkdir(parents=True, exist_ok=True)
        _write_last_run_pointer(tmp_path, session_ts, tmp_path / ".lanegate" / "logs" / "run.log")

        monkeypatch.setattr(
            "lanegate.orchestrate.run_report.orchestrator_lock_status",
            lambda _repo_root: {"held": True},
        )
        cfg = self._cfg()
        cfg["executor"] = "codex"
        cfg["steps"] = {
            "implement": {"driver": "codex"},
            "review": {"driver": "claude-process"},
        }
        self._run(tmp_path, monkeypatch, payload, cfg)

        out = capsys.readouterr().out
        assert envelope not in out
        assert '"type": "assistant"' not in out

        # The same executor_progress shape the implement step emits, so review
        # runs appear in GET /api/runs/<id>/events with no consumer change.
        events = [
            e
            for e in _load_run_events(tmp_path, session_ts)
            if e.get("event") == "executor_progress"
        ]
        assert events, "review produced no executor events"
        assert events[-1]["ticket_id"] == "TICK-343"
        assert events[-1]["progress"]["phase"] == "reviewing"

    def test_standalone_review_does_not_append_to_completed_run(self, tmp_path, monkeypatch):
        """A stale last-run pointer is not an active orchestration session."""
        from lanegate.orchestrate.run_report import _load_run_events, _write_last_run_pointer

        session_ts = "20260801-120000"
        logs = tmp_path / ".lanegate" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        _write_last_run_pointer(tmp_path, session_ts, logs / "run.log")

        payload = json.dumps({"verdict": "approved", "notes": "ok"})
        self._run(tmp_path, monkeypatch, payload)

        assert _load_run_events(tmp_path, session_ts) == []

    def test_rate_limited_retry_keeps_each_attempt_bundle_and_verdict(self, tmp_path, monkeypatch):
        """Fast sibling retries neither overwrite nor leave the first attempt incomplete."""
        worktree = tmp_path / "wt"
        worktree.mkdir()
        cfg = {
            "ticket_prefix": "TICK",
            "tickets_dir": "tickets",
            "executor": "claude-review-1",
            "executors": {
                "claude-review-1": {"type": "claude-process"},
                "claude-review-2": {"type": "claude-process"},
            },
            "pools": {"default": {"executors": ["claude-review-1", "claude-review-2"]}},
            "default_pool": "default",
        }
        responses = iter([
            (1, "", "rate limit exceeded", None),
            (0, json.dumps({"verdict": "approved", "notes": "ok"}), "", None),
        ])
        monkeypatch.setattr(
            "lanegate.orchestrate.review._stream_subprocess", lambda *args, **kwargs: next(responses)
        )
        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.reviewer.get_commit_messages", return_value="commit msg"),
            patch("lanegate.lifecycle.cmd_review"),
        ):
            assert run_review_agent(self._make_ticket(), tmp_path, worktree_path=worktree, cfg=cfg)

        runs = sorted((tmp_path / ".lanegate" / "executor-runs" / "TICK-343").iterdir())
        assert len(runs) == 2
        verdicts = [json.loads((run / "verdict.json").read_text())["verdict"] for run in runs]
        assert sorted(verdicts) == ["approved", "error"]


# ---------------------------------------------------------------------------
# TICK-352: review verdict extraction must handle braces inside JSON strings
# ---------------------------------------------------------------------------


class TestReviewVerdictExtraction:
    def _parse(self, output: str):
        from lanegate.orchestrate.review import _extract_review_verdict_json
        from lanegate.reviewer import parse_review_result

        raw = _extract_review_verdict_json(output)
        assert raw is not None
        return parse_review_result(raw)

    def test_real_tick_349_bundle_preserves_nested_brace_findings(self):
        """The successful review that found TICK-349's real TUI race was
        previously discarded because it quoted Go's ``{ return nil }``."""
        from lanegate.executor import parse_structured_result

        # Real captured executor output, frozen as a committed fixture (was
        # previously read live from this repo's own gitignored .lanegate/
        # state dir, which doesn't exist in a fresh CI checkout).
        fixtures_dir = Path(__file__).resolve().parents[1] / "fixtures/captured_output"
        captured = (fixtures_dir / "tick-349-nested-brace-review.txt").read_text()
        parsed = parse_structured_result("claude-process", captured)
        assert parsed is not None

        review = self._parse(parsed["result_text"])

        assert review.verdict == "changes_requested"
        assert review.notes
        assert "{ return nil }" in review.findings

    def test_approved_verdict_with_braces_is_not_downgraded(self):
        review = self._parse(
            'Reviewer notes follow. {"verdict":"approved",'
            '"summary":"all good",'
            '"findings":"checked the branch: { return nil }"}'
        )

        assert review.verdict == "approved"
        assert review.findings == "checked the branch: { return nil }"

    def test_escaped_quote_and_brace_inside_finding_are_parsed(self):
        payload = json.dumps(
            {
                "verdict": "changes_requested",
                "summary": "quoted source",
                "findings": 'the expression says \\"{ keep going }\\"',
            }
        )

        review = self._parse(f"Review complete:\n{payload}")

        assert review.verdict == "changes_requested"
        assert review.findings == 'the expression says \\"{ keep going }\\"'

    def test_fenced_json_is_preferred_over_unfenced_json(self):
        output = (
            '{"verdict":"changes_requested","summary":"example only"}\n'
            "```json\n"
            '{"verdict":"approved","summary":"actual verdict","findings":"{ safe }"}\n'
            "```"
        )

        review = self._parse(output)

        assert review.verdict == "approved"
        assert review.notes == "actual verdict"

    def test_last_verdict_object_wins(self):
        output = (
            '{"not_a_verdict":{"nested":true}}\n'
            '{"verdict":"changes_requested","summary":"superseded"}\n'
            '{"verdict":"approved","summary":"final"}'
        )

        review = self._parse(output)

        assert review.verdict == "approved"
        assert review.notes == "final"

    def test_unparseable_output_has_no_extractable_verdict(self):
        from lanegate.orchestrate.review import _extract_review_verdict_json
        from lanegate.reviewer import parse_review_result

        assert _extract_review_verdict_json("review complete: {not valid json") is None
        assert parse_review_result("review complete: {not valid json").verdict == "changes_requested"


# ---------------------------------------------------------------------------
# TICK-367 — a review whose executor envelope reports failure must not approve
# ---------------------------------------------------------------------------


class TestErroredRunCannotApprove:
    """An executor can exit 0 while its own envelope reports the run failed --
    observed on agy-claude, whose envelope carried `"status": "ERROR"` and an
    error string while the model's verdict prose was still present in the
    output. run_review_agent keyed success purely on the exit code, so it
    parsed the verdict out of that output and recorded `approved`: fail-open,
    in a pipeline that is fail-closed on every other path.

    The parsers already computed the signal (`is_error`); no consumer read it.
    """

    def _make_ticket(self) -> dict:
        return {
            "id": "TICK-997",
            "title": "Test ticket",
            "close_criteria": "Tests pass.",
            "_body": "",
        }

    def _cfg(self) -> dict:
        return {
            "ticket_prefix": "TICK",
            "tickets_dir": "tickets",
            "executor": "claude-a",
            "executors": {"claude-a": {"type": "claude-process"}},
        }

    def _run(self, tmp_path, envelope: str):
        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["git", "log"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout=envelope, stderr="")

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.orchestrate.subprocess.run", side_effect=fake_run),
            patch("lanegate.lifecycle.cmd_review") as mock_cmd_review,
            patch("lanegate.lifecycle._mark_needs_review"),
        ):
            result = run_review_agent(self._make_ticket(), tmp_path, cfg=self._cfg())
        return result, mock_cmd_review

    def test_errored_envelope_with_approval_text_does_not_approve(self, tmp_path):
        """The failure case. Exit code 0, envelope reports failure, and the
        output still contains a well-formed approved verdict."""
        envelope = json.dumps(
            {
                "session_id": "abc123",
                "is_error": True,
                "result": json.dumps({"verdict": "approved", "notes": "looks good"}),
            }
        )

        result, mock_cmd_review = self._run(tmp_path, envelope)

        assert result is False
        approved_calls = [
            c
            for c in mock_cmd_review.call_args_list
            if c.kwargs.get("verdict") == "approved"
        ]
        assert approved_calls == []

    def test_missing_is_error_still_approves(self, tmp_path):
        """is_error is tri-state. None means the parser cannot determine status,
        not that the run failed -- treating it as failure would fail-close every
        review from an executor that does not report status at all."""
        envelope = json.dumps(
            {
                "session_id": "abc123",
                "result": json.dumps({"verdict": "approved", "notes": "ok"}),
            }
        )

        result, mock_cmd_review = self._run(tmp_path, envelope)

        assert result is True
        assert mock_cmd_review.call_args.kwargs["verdict"] == "approved"

    def test_explicit_success_envelope_still_approves(self, tmp_path):
        """The control: is_error present and False must behave exactly as before."""
        envelope = json.dumps(
            {
                "session_id": "abc123",
                "is_error": False,
                "result": json.dumps({"verdict": "approved", "notes": "ok"}),
            }
        )

        result, mock_cmd_review = self._run(tmp_path, envelope)

        assert result is True
        assert mock_cmd_review.call_args.kwargs["verdict"] == "approved"
