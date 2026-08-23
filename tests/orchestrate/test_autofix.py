"""
Tests for lanegate/orchestrate/autofix.py — fix agent, drift checks, auto-commits.

Split out of the former monolithic tests/test_orchestrate.py (TICK-316).
"""

from __future__ import annotations

from tests.orchestrate.conftest import *  # noqa: F401,F403


@pytest.fixture(autouse=True)
def _compat_stream_subprocess(monkeypatch):
    def fake_stream(cmd, **kwargs):
        result = subprocess.run(cmd, cwd=kwargs.get("cwd"), capture_output=True, text=True, env=kwargs.get("env"))
        return result.returncode, result.stdout, getattr(result, "stderr", ""), None
    monkeypatch.setattr("lanegate.orchestrate.autofix._stream_subprocess", fake_stream)


# run_fix_agent / run_drift_check — TICK-120 auto-fix loop
# ---------------------------------------------------------------------------


class TestRunFixAgent:
    def _make_ticket(self, **overrides) -> dict:
        base = {"id": "TICK-100", "title": "Fix me", "close_criteria": "Passes."}
        base.update(overrides)
        return base

    def test_success_returns_true_when_new_commit_made(self, tmp_path):
        from lanegate.orchestrate import run_fix_agent

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/x.py"),
            patch("lanegate.reviewer.build_fix_prompt", return_value="fix prompt") as mock_build,
            patch("lanegate.orchestrate.autofix.invoke_executor", return_value=(0, "", "")) as mock_invoke,
            patch("lanegate.orchestrate.autofix.commit_worktree_changes", return_value=True) as mock_commit,
            patch("lanegate.orchestrate.autofix._git_head_sha", return_value="sha-after"),
        ):
            result = run_fix_agent(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        assert result is True
        mock_build.assert_called_once()
        assert mock_build.call_args.kwargs["findings"] == "a finding"
        mock_invoke.assert_called_once()
        assert mock_invoke.call_args.kwargs["prompt_override"] == "fix prompt"
        assert mock_invoke.call_args.kwargs["step"] == "fix"
        mock_commit.assert_called_once()
        assert "fix" in mock_commit.call_args.kwargs["message"].lower()

    def test_run_fix_agent_failure_raises_fix_failed_error(self, tmp_path):
        from lanegate.orchestrate import run_fix_agent
        from lanegate.orchestrate.autofix import FixFailedError

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/x.py"),
            patch("lanegate.reviewer.build_fix_prompt", return_value="fix prompt"),
            patch("lanegate.orchestrate.autofix.invoke_executor", return_value=(1, "", "some error")),
            patch("lanegate.orchestrate.loop._is_rate_limit", return_value=False),
            patch("lanegate.orchestrate.loop._is_interrupted_exit", return_value=False),
            patch("lanegate.orchestrate.autofix.commit_worktree_changes") as mock_commit,
        ):
            with pytest.raises(FixFailedError):
                run_fix_agent(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        mock_commit.assert_not_called()

    def test_no_new_commit_raises_fix_failed_error(self, tmp_path):
        """Executor exits 0 but makes no commit — check_worktree_has_commits would
        be trivially True here (main-relative), so this must use a HEAD-sha
        comparison against pre_fix_sha instead."""
        from lanegate.orchestrate import run_fix_agent
        from lanegate.orchestrate.autofix import FixFailedError

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/x.py"),
            patch("lanegate.reviewer.build_fix_prompt", return_value="fix prompt"),
            patch("lanegate.orchestrate.autofix.invoke_executor", return_value=(0, "", "")),
            patch("lanegate.orchestrate.autofix.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.autofix._git_head_sha", return_value="sha-before"),
        ):
            with pytest.raises(FixFailedError):
                run_fix_agent(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

    def test_missing_diff_raises_fix_failed_error(self, tmp_path):
        from lanegate.orchestrate import run_fix_agent
        from lanegate.orchestrate.autofix import FixFailedError
        from lanegate.reviewer import ReviewError

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)

        with patch(
            "lanegate.reviewer.get_worktree_diff", side_effect=ReviewError("no worktree")
        ):
            with pytest.raises(FixFailedError):
                run_fix_agent(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

    def test_run_fix_agent_rate_limit_raises_rate_limited_fix_error(self, tmp_path):
        """A rate-limited executor exit raises RateLimitedFixError, not FixFailedError,
        so the attempt counter is not consumed."""
        from lanegate.orchestrate import run_fix_agent
        from lanegate.orchestrate.autofix import RateLimitedFixError

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/x.py"),
            patch("lanegate.reviewer.build_fix_prompt", return_value="fix prompt"),
            patch("lanegate.orchestrate.autofix.invoke_executor", return_value=(1, "", "rate limit exceeded")),
            patch("lanegate.orchestrate.loop._is_rate_limit", return_value=True),
            patch("lanegate.orchestrate.loop._is_interrupted_exit", return_value=False),
            patch("lanegate.orchestrate.autofix.commit_worktree_changes") as mock_commit,
        ):
            with pytest.raises(RateLimitedFixError):
                run_fix_agent(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        mock_commit.assert_not_called()

    def test_interrupted_exit_raises_rate_limited_fix_error(self, tmp_path):
        """A SIGINT/Ctrl-C exit (interrupted) raises RateLimitedFixError,
        matching the implement-phase interrupted-exit hibernation path."""
        from lanegate.orchestrate import run_fix_agent
        from lanegate.orchestrate.autofix import RateLimitedFixError

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/x.py"),
            patch("lanegate.reviewer.build_fix_prompt", return_value="fix prompt"),
            patch("lanegate.orchestrate.autofix.invoke_executor", return_value=(130, "", "")),
            patch("lanegate.orchestrate.loop._is_rate_limit", return_value=False),
            patch("lanegate.orchestrate.loop._is_interrupted_exit", return_value=True),
            patch("lanegate.orchestrate.autofix.commit_worktree_changes") as mock_commit,
        ):
            with pytest.raises(RateLimitedFixError):
                run_fix_agent(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        mock_commit.assert_not_called()

    def test_ticket_executor_pins_each_auto_fix_pass_to_aider(self, tmp_path):
        """Review fixes must not fall back to a global or step-level Codex route."""
        from lanegate.orchestrate import run_fix_agent

        ticket = self._make_ticket(executor="aider")
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "codex"
        cfg["steps"] = {"fix": {"driver": "codex"}}

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/x.py"),
            patch("lanegate.reviewer.build_fix_prompt", return_value="fix prompt"),
            patch("lanegate.orchestrate.autofix.commit_worktree_changes", return_value=True),
            patch("lanegate.orchestrate.autofix._git_head_sha", return_value="sha-after"),
            patch(
                "lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")
            ) as mock_stream,
        ):
            assert run_fix_agent(ticket, cfg, tmp_path, tmp_path, "first finding", "sha-before")
            assert run_fix_agent(ticket, cfg, tmp_path, tmp_path, "second finding", "sha-before")

        assert [call.args[0][0] for call in mock_stream.call_args_list] == ["aider", "aider"]


class TestRunDriftCheck:
    def _make_ticket(self, **overrides) -> dict:
        base = {"id": "TICK-100", "title": "Fix me", "close_criteria": "Passes."}
        base.update(overrides)
        return base

    def _diffs(self, original="original diff", fix="fix diff"):
        return [original, fix]

    def test_ceiling_kill_includes_partial_output_in_reason(self, tmp_path):
        from lanegate.orchestrate import run_drift_check

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)
        with (
            patch("lanegate.reviewer.get_worktree_diff", side_effect=self._diffs()),
            patch("lanegate.reviewer.build_drift_check_prompt", return_value="drift prompt"),
            patch("lanegate.orchestrate.autofix._stream_subprocess", return_value=(124, "partial drift output", "", "ceiling")),
        ):
            result = run_drift_check(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")
        assert result.ok is False
        assert "partial drift output" in result.reason

    def test_drift_ok_true(self, tmp_path):
        from lanegate.orchestrate import run_drift_check

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"drift_ok": True, "reason": "in scope"})

        with (
            patch("lanegate.reviewer.get_worktree_diff", side_effect=self._diffs()),
            patch("lanegate.reviewer.build_drift_check_prompt", return_value="drift prompt"),
            patch("lanegate.orchestrate.subprocess.run", return_value=mock_result),
        ):
            result = run_drift_check(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        assert result.ok is True
        assert result.reason == "in scope"

    def test_drift_check_writes_run_directory(self, tmp_path):
        """TICK-343: drift checks are review-class steps and were equally blind."""
        from lanegate.orchestrate import run_drift_check

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        with (
            patch("lanegate.reviewer.get_worktree_diff", side_effect=self._diffs()),
            patch("lanegate.reviewer.build_drift_check_prompt", return_value="drift prompt"),
            patch(
                "lanegate.orchestrate.autofix._stream_subprocess",
                return_value=(0, json.dumps({"drift_ok": False, "reason": "out of scope"}), "", None),
            ),
        ):
            result = run_drift_check(ticket, cfg, tmp_path, worktree, "a finding", "sha-before")

        assert result.ok is False
        runs = sorted((tmp_path / ".lanegate" / "executor-runs" / "TICK-100").iterdir())
        assert len(runs) == 1
        bundle = runs[0]
        assert bundle.name.endswith("-drift_check")
        assert (bundle / "prompt.md").read_text() == "drift prompt"
        assert (bundle / "captured-output.txt").exists()

        status = json.loads((bundle / "status.json").read_text())
        assert status["step"] == "drift_check"
        assert status["mode"] in ("combined", "split")

        recorded = json.loads((bundle / "verdict.json").read_text())
        assert recorded["drift_ok"] is False
        assert recorded["notes"] == "out of scope"

    def test_drift_check_unwraps_json_envelope_for_named_claude_instance(self, tmp_path):
        """A named executor instance (e.g. "claude-a") of type claude-process
        must still have its --output-format json envelope unwrapped before
        the verdict is parsed -- resolving via expand_driver() alone leaves
        the type as the raw instance name, which parse_structured_result's
        registry never matches, so the fail-closed path would otherwise read
        the raw envelope as "not a valid drift verdict" and always report
        drift.
        """
        from lanegate.orchestrate import run_drift_check

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude-a"
        cfg["executors"] = {"claude-a": {"type": "claude-process"}}
        envelope = json.dumps(
            {
                "session_id": "abc123",
                "result": json.dumps({"drift_ok": True, "reason": "in scope"}),
            }
        )
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = envelope

        with (
            patch("lanegate.reviewer.get_worktree_diff", side_effect=self._diffs()),
            patch("lanegate.reviewer.build_drift_check_prompt", return_value="drift prompt"),
            patch("lanegate.orchestrate.subprocess.run", return_value=mock_result),
        ):
            result = run_drift_check(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        assert result.ok is True
        assert result.reason == "in scope"

    def test_steps_implement_driver_reaches_drift_subprocess(self, tmp_path, monkeypatch):
        from lanegate.orchestrate import run_drift_check

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)
        cfg["drivers"] = {
            "implement-fast": {
                "type": "claude-process",
                "model": "drift-driver-model",
                "bin": "custom-drift",
                "flags": ["--driver-flag"],
                "env": {"DRIFT_TOKEN": "${SOURCE_DRIFT_TOKEN}"},
            }
        }
        cfg["steps"] = {"implement": {"driver": "implement-fast"}}
        monkeypatch.setenv("SOURCE_DRIFT_TOKEN", "drift-token")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"drift_ok": True, "reason": "in scope"})

        with (
            patch("lanegate.reviewer.get_worktree_diff", side_effect=self._diffs()),
            patch("lanegate.reviewer.build_drift_check_prompt", return_value="drift prompt"),
            patch("lanegate.orchestrate.subprocess.run", return_value=mock_result) as mock_run,
        ):
            result = run_drift_check(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        assert result.ok is True
        drift_cmd = _dispatch_call(mock_run).args[0]
        drift_kwargs = _dispatch_call(mock_run).kwargs
        assert drift_cmd[0] == "custom-drift"
        assert "--driver-flag" in drift_cmd
        assert "--model" in drift_cmd
        assert drift_cmd[drift_cmd.index("--model") + 1] == "drift-driver-model"
        assert drift_kwargs["env"]["DRIFT_TOKEN"] == "drift-token"

    def test_executor_steps_drift_check_wins_over_implement_driver(self, tmp_path):
        from lanegate.orchestrate import run_drift_check

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "codex"
        cfg["executor_steps"] = {
            "implement": "codex",
            "drift_check": "aider",
        }
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"drift_ok": True, "reason": "in scope"})

        with (
            patch("lanegate.reviewer.get_worktree_diff", side_effect=self._diffs()),
            patch("lanegate.reviewer.build_drift_check_prompt", return_value="drift prompt"),
            patch("lanegate.orchestrate.subprocess.run", return_value=mock_result) as mock_run,
        ):
            result = run_drift_check(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        assert result.ok is True
        drift_cmd = _dispatch_call(mock_run).args[0]
        assert drift_cmd[0] == "aider"
        assert "--message" in drift_cmd

    def test_ticket_pinned_to_aider_uses_independent_reviewer_for_drift_not_global_codex(self, tmp_path):
        """Drift audits a pinned implementer's fix through the review route."""
        from lanegate.orchestrate import run_drift_check

        ticket = self._make_ticket(executor="aider")
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "codex"
        cfg["reviewer"] = "claude-process"
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"drift_ok": True, "reason": "in scope"})

        with (
            patch("lanegate.reviewer.get_worktree_diff", side_effect=self._diffs()),
            patch("lanegate.reviewer.build_drift_check_prompt", return_value="drift prompt"),
            patch("lanegate.orchestrate.subprocess.run", return_value=mock_result) as mock_run,
        ):
            result = run_drift_check(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        assert result.ok is True
        drift_cmd = _dispatch_call(mock_run).args[0]
        assert drift_cmd[0] == "claude"
        assert "codex" not in drift_cmd
        assert "aider" not in drift_cmd

    def test_drift_ok_false(self, tmp_path):
        from lanegate.orchestrate import run_drift_check

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"drift_ok": False, "reason": "touched unrelated file"})

        with (
            patch("lanegate.reviewer.get_worktree_diff", side_effect=self._diffs()),
            patch("lanegate.reviewer.build_drift_check_prompt", return_value="drift prompt"),
            patch("lanegate.orchestrate.subprocess.run", return_value=mock_result),
        ):
            result = run_drift_check(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        assert result.ok is False
        assert result.reason == "touched unrelated file"

    def test_resumes_fix_session_when_fresh(self, tmp_path):
        """TICK-310: drift_check resumes the fix pass's own session -- it's
        reviewing what fix just did, in the same continuity."""
        from lanegate.orchestrate import run_drift_check

        ticket = self._make_ticket(
            fix_session_id="sess-fix-1",
            fix_session_executor="claude-process",
            fix_session_model=None,
        )
        cfg = _default_cfg(tmp_path)
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"drift_ok": True, "reason": "ok"})

        with (
            patch("lanegate.reviewer.get_worktree_diff", side_effect=self._diffs()),
            patch("lanegate.reviewer.build_drift_check_prompt", return_value="drift prompt"),
            patch("lanegate.orchestrate.subprocess.run", return_value=mock_result) as mock_run,
            patch("lanegate.context_log._get_default_db_path", return_value=tmp_path / "analytics.db"),
        ):
            run_drift_check(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        cmd = _dispatch_call(mock_run).args[0]
        assert "--resume" in cmd
        assert cmd[cmd.index("--resume") + 1] == "sess-fix-1"

    def test_does_not_resume_fix_session_from_a_different_executor(self, tmp_path):
        """A provider-specific fix session cannot be resumed by drift."""
        from lanegate.orchestrate import run_drift_check

        ticket = self._make_ticket(
            fix_session_id="sess-codex-fix",
            fix_session_executor="codex",
            fix_session_model=None,
        )
        cfg = _default_cfg(tmp_path)
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"drift_ok": True, "reason": "ok"})

        with (
            patch("lanegate.reviewer.get_worktree_diff", side_effect=self._diffs()),
            patch("lanegate.reviewer.build_drift_check_prompt", return_value="drift prompt"),
            patch("lanegate.orchestrate.subprocess.run", return_value=mock_result) as mock_run,
        ):
            result = run_drift_check(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        assert result.ok is True
        assert "--resume" not in _dispatch_call(mock_run).args[0]

    def test_drift_model_does_not_inherit_implementation_ticket_model(self, tmp_path):
        """The independent drift route owns its model selection."""
        from lanegate.orchestrate import run_drift_check

        ticket = self._make_ticket(model="ollama/qwen2.5-coder:14b")
        cfg = _default_cfg(tmp_path)
        cfg["reviewer"] = "codex"
        cfg["models"] = {"drift_check": "gpt-5.6-sol"}
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"drift_ok": True, "reason": "ok"})

        with (
            patch("lanegate.reviewer.get_worktree_diff", side_effect=self._diffs()),
            patch("lanegate.reviewer.build_drift_check_prompt", return_value="drift prompt"),
            patch("lanegate.orchestrate.subprocess.run", return_value=mock_result) as mock_run,
        ):
            result = run_drift_check(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        assert result.ok is True
        cmd = _dispatch_call(mock_run).args[0]
        assert cmd[cmd.index("--model") + 1] == "gpt-5.6-sol"

    def test_does_not_resume_when_no_fix_session_id(self, tmp_path):
        from lanegate.orchestrate import run_drift_check

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"drift_ok": True, "reason": "ok"})

        with (
            patch("lanegate.reviewer.get_worktree_diff", side_effect=self._diffs()),
            patch("lanegate.reviewer.build_drift_check_prompt", return_value="drift prompt"),
            patch("lanegate.orchestrate.subprocess.run", return_value=mock_result) as mock_run,
        ):
            run_drift_check(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        assert "--resume" not in _dispatch_call(mock_run).args[0]

    def test_does_not_resume_fix_session_when_gate_blocks(self, tmp_path):
        """A stale fix session must not be resumed by drift_check either."""
        from lanegate.context_log import log_step_cost
        from lanegate.orchestrate import run_drift_check

        db_path = tmp_path / "analytics.db"
        log_step_cost(
            db_path,
            "proj",
            "TICK-100",
            "fix",
            session_id="sess-fix-stale",
            timestamp="2020-01-01T00:00:00Z",
        )

        ticket = self._make_ticket(fix_session_id="sess-fix-stale")
        cfg = _default_cfg(tmp_path)
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"drift_ok": True, "reason": "ok"})

        with (
            patch("lanegate.reviewer.get_worktree_diff", side_effect=self._diffs()),
            patch("lanegate.reviewer.build_drift_check_prompt", return_value="drift prompt"),
            patch("lanegate.orchestrate.subprocess.run", return_value=mock_result) as mock_run,
            patch("lanegate.context_log._get_default_db_path", return_value=db_path),
            patch("lanegate.context_log._get_project_id", return_value="proj"),
        ):
            run_drift_check(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        assert "--resume" not in _dispatch_call(mock_run).args[0]

    def test_isolates_original_and_fix_diff_bases(self, tmp_path):
        """original_diff must be base=main; fix_diff must be base=pre_fix_sha —
        this is what isolates exactly what the fix pass changed."""
        from lanegate.orchestrate import run_drift_check

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"drift_ok": True, "reason": "ok"})

        with (
            patch(
                "lanegate.reviewer.get_worktree_diff", side_effect=self._diffs()
            ) as mock_diff,
            patch("lanegate.reviewer.build_drift_check_prompt", return_value="drift prompt"),
            patch("lanegate.orchestrate.subprocess.run", return_value=mock_result),
        ):
            run_drift_check(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before-fix")

        first_call, second_call = mock_diff.call_args_list
        assert first_call.kwargs["base"] == "main"
        assert second_call.kwargs["base"] == "sha-before-fix"

    def test_subprocess_timeout_returns_not_ok(self, tmp_path):
        from lanegate.orchestrate import run_drift_check

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)

        with (
            patch("lanegate.reviewer.get_worktree_diff", side_effect=self._diffs()),
            patch("lanegate.reviewer.build_drift_check_prompt", return_value="drift prompt"),
            patch(
                "lanegate.orchestrate.subprocess.run",
                side_effect=subprocess.TimeoutExpired("claude", 300),
            ),
        ):
            result = run_drift_check(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        assert result.ok is False

    def test_subprocess_nonzero_exit_returns_not_ok(self, tmp_path):
        from lanegate.orchestrate import run_drift_check

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with (
            patch("lanegate.reviewer.get_worktree_diff", side_effect=self._diffs()),
            patch("lanegate.reviewer.build_drift_check_prompt", return_value="drift prompt"),
            patch("lanegate.orchestrate.subprocess.run", return_value=mock_result),
        ):
            result = run_drift_check(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        assert result.ok is False

    def test_malformed_response_returns_not_ok(self, tmp_path):
        from lanegate.orchestrate import run_drift_check

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "not json at all"

        with (
            patch("lanegate.reviewer.get_worktree_diff", side_effect=self._diffs()),
            patch("lanegate.reviewer.build_drift_check_prompt", return_value="drift prompt"),
            patch("lanegate.orchestrate.subprocess.run", return_value=mock_result),
        ):
            result = run_drift_check(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        assert result.ok is False

    def test_missing_diff_returns_not_ok(self, tmp_path):
        from lanegate.orchestrate import run_drift_check
        from lanegate.reviewer import ReviewError

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)

        with patch(
            "lanegate.reviewer.get_worktree_diff", side_effect=ReviewError("no diff")
        ):
            result = run_drift_check(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        assert result.ok is False

    def test_ollama_drift_executor_returns_not_ok(self, tmp_path):
        """Raw ollama has no code-application step of its own, so a drift
        check routed to it must fail closed with a reason mentioning the
        gap instead of dispatching to the text-only /api/generate path."""
        from lanegate.orchestrate import run_drift_check

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)
        cfg["drivers"] = {"ollama-drift": {"type": "ollama"}}
        cfg["steps"] = {"drift_check": {"driver": "ollama-drift"}}

        with patch("lanegate.reviewer.get_worktree_diff", side_effect=self._diffs()):
            result = run_drift_check(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        assert result.ok is False
        assert "ollama" in result.reason

    def test_named_ollama_instance_drift_executor_returns_not_ok(self, tmp_path):
        """A named executor instance (TICK-088) backed by ollama must hit the
        same guard: expand_driver() leaves `local-ollama` unresolved, so the
        guard only fires when the type is resolved via get_executor_config()."""
        from lanegate.orchestrate import run_drift_check

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)
        cfg["executors"] = {"local-ollama": {"type": "ollama"}}
        cfg["steps"] = {"drift_check": {"driver": "local-ollama"}}

        with (
            patch("lanegate.reviewer.get_worktree_diff", side_effect=self._diffs()),
            patch("lanegate.orchestrate.subprocess.run") as mock_run,
        ):
            result = run_drift_check(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        assert result.ok is False
        assert "ollama" in result.reason
        mock_run.assert_not_called()

    def test_fix_touching_unrelated_file_is_flagged_by_agent_response(self, tmp_path):
        """Explicit close-criteria case: a fix that drifted out of scope must
        surface as ok=False via the agent's own drift_ok=false verdict — this
        test exercises the plumbing that carries that verdict back unchanged."""
        from lanegate.orchestrate import run_drift_check

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(
            {"drift_ok": False, "reason": "fix diff touches unrelated_module.py"}
        )

        with (
            patch(
                "lanegate.reviewer.get_worktree_diff",
                side_effect=["original diff", "fix diff touching unrelated_module.py"],
            ),
            patch("lanegate.reviewer.build_drift_check_prompt", return_value="drift prompt"),
            patch("lanegate.orchestrate.subprocess.run", return_value=mock_result),
        ):
            result = run_drift_check(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        assert result.ok is False
        assert "unrelated_module.py" in result.reason

    def test_returns_not_ok_on_bad_named_executor_config(self, tmp_path):
        """Regression (TICK-088 second review round): resolve_executor_env
        can raise ConfigError for a named executor instance whose type has
        no known api-key injection target (e.g. type='gemini') or whose
        api_key_env names an unset variable. Before this fix, that call
        happened before run_drift_check's try: block started, so the
        ConfigError escaped the function entirely instead of being caught
        by the same fail-closed handler as a subprocess timeout or a
        malformed response."""
        from lanegate.orchestrate import run_drift_check

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)
        cfg["executor_steps"] = {"implement": "gemini-1"}
        cfg["executors"] = {
            "gemini-1": {"type": "gemini", "api_key_env": "SOME_UNSET_GEMINI_KEY"},
        }

        with (
            patch("lanegate.reviewer.get_worktree_diff", side_effect=self._diffs()),
            patch("lanegate.reviewer.build_drift_check_prompt", return_value="drift prompt"),
            patch("lanegate.orchestrate.subprocess.run") as mock_run,
        ):
            result = run_drift_check(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        assert result.ok is False
        # The ConfigError must be raised (and caught) before any subprocess
        # is ever launched.
        mock_run.assert_not_called()

    def test_returns_not_ok_on_unset_api_key_env(self, tmp_path, monkeypatch):
        """Same fail-closed guarantee for the 'unset env var' ConfigError case."""
        from lanegate.orchestrate import run_drift_check

        monkeypatch.delenv("SOME_UNSET_DRIFT_KEY", raising=False)
        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)
        cfg["executor_steps"] = {"implement": "claude-drift-1"}
        cfg["executors"] = {
            "claude-drift-1": {"type": "claude-process", "api_key_env": "SOME_UNSET_DRIFT_KEY"},
        }

        with (
            patch("lanegate.reviewer.get_worktree_diff", side_effect=self._diffs()),
            patch("lanegate.reviewer.build_drift_check_prompt", return_value="drift prompt"),
            patch("lanegate.orchestrate.subprocess.run") as mock_run,
        ):
            result = run_drift_check(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        assert result.ok is False
        mock_run.assert_not_called()

    def test_returns_not_ok_on_malformed_driver_env(self, tmp_path):
        """A malformed driver env overlay must fail this drift check, not the batch."""
        from lanegate.orchestrate import run_drift_check

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)
        cfg["drivers"] = {
            "bad-drift-env": {
                "type": "claude-process",
                "env": ["BAD"],
            }
        }
        cfg["steps"] = {"implement": {"driver": "bad-drift-env"}}

        with (
            patch("lanegate.reviewer.get_worktree_diff", side_effect=self._diffs()),
            patch("lanegate.reviewer.build_drift_check_prompt", return_value="drift prompt"),
            patch("lanegate.orchestrate.subprocess.run") as mock_run,
        ):
            result = run_drift_check(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        assert result.ok is False
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# _is_combined_mode
# ---------------------------------------------------------------------------


class TestBackfillCombinedReviewMetadata:
    """TICK-343: a combined-mode self-review records its own verdict through
    the CLI, which has no way to name the reviewer — so those tickets carried
    a verdict with no attribution at all."""

    def _ticket(self, **overrides) -> dict:
        base = {"id": "TICK-001", "title": "Test", "_body": "Body."}
        base.update(overrides)
        return base

    def test_backfills_driver_and_model_when_missing(self, tmp_path):
        from lanegate.orchestrate.autofix import backfill_combined_review_metadata

        ticket = self._ticket(review_verdict="approved")
        dispatch = {"resolved_executor": "claude-a", "resolved_model": "claude-opus-4-8"}
        with patch("lanegate.orchestrate.autofix.write_ticket") as mock_write:
            backfill_combined_review_metadata(ticket, dispatch, tmp_path)

        assert ticket["review_driver"] == "claude-a"
        assert ticket["review_model"] == "claude-opus-4-8"
        assert ticket["review_independence"] == "self"
        mock_write.assert_called_once_with(ticket)


class TestIsCombinedMode:
    """Unit tests for _is_combined_mode()."""

    def _ticket(self, executor=None):
        t = {
            "id": "TICK-001",
            "title": "Test",
            "touches": ["a.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
        }
        if executor:
            t["executor"] = executor
        return t

    def test_true_when_no_executor_steps(self):
        """Default config (no executor_steps, no repo_root) → combined mode."""
        cfg = {"executor": "claude", "executor_steps": {}}
        assert _is_combined_mode(cfg, self._ticket()) is True

    def test_true_when_both_steps_same_executor(self):
        """Explicit same executor for both steps → combined mode."""
        cfg = {"executor": "claude", "executor_steps": {"implement": "claude", "review": "claude"}}
        assert _is_combined_mode(cfg, self._ticket()) is True

    def test_false_when_same_executor_is_not_combined_mode_capable(self):
        """Same executor for both steps, but a pure code-editing tool with no
        shell/command-execution capability (aider) can't act on combined
        mode's appended "shell out and run lanegate complete && lanegate
        review" instructions -- it commits the real change and exits, then
        the ticket permanently fails with no way to recover on retry.
        Confirmed live in a fresh-install smoke test. Falls through to
        split-mode dispatch instead, regardless of the explicit pin."""
        cfg = {"executor": "aider", "executor_steps": {"implement": "aider", "review": "aider"}}
        assert _is_combined_mode(cfg, self._ticket()) is False

    def test_false_when_implement_differs_from_review(self):
        """Different executors for implement and review → split mode."""
        cfg = {"executor": "claude", "executor_steps": {"implement": "aider"}}
        # implement→aider, review→claude (global)
        assert _is_combined_mode(cfg, self._ticket()) is False

    def test_false_when_ticket_executor_overrides_implement(self):
        """ticket.executor overrides implement; if it differs from review → split mode."""
        cfg = {"executor": "claude", "executor_steps": {}}
        ticket = self._ticket(executor="aider")
        # implement→aider (ticket), review→claude (global) → split
        assert _is_combined_mode(cfg, ticket) is False

    def test_true_when_ticket_executor_matches_review(self):
        """ticket.executor matches review executor (no repo_root) → combined mode."""
        cfg = {"executor": "claude", "executor_steps": {}}
        ticket = self._ticket(executor="claude")
        # implement→claude (ticket), review→claude (global) → combined
        assert _is_combined_mode(cfg, ticket) is True

    def test_true_with_empty_config(self):
        """Empty config → both resolve to 'claude' → combined mode."""
        assert _is_combined_mode({}, self._ticket()) is True

    def test_pool_offers_independent_instance_by_default(self, tmp_path):
        """When a 2-instance pool is available and no explicit review route is set, attempt independence -> split mode."""
        cfg = {
            "executor": "claude-a",
            "executors": {
                "claude-a": {"type": "claude"},
                "claude-b": {"type": "claude"},
            },
            "pools": {"default": {"executors": ["claude-a", "claude-b"]}},
            "default_pool": "default",
        }
        ticket = self._ticket(executor="claude-a")
        assert _is_combined_mode(cfg, ticket, tmp_path) is False

    def test_single_account_still_degrades_to_combined_self_review(self, tmp_path):
        """A single-account config with no pool falls back to self-review -> combined mode."""
        cfg = {"executor": "claude", "executor_steps": {}}
        assert _is_combined_mode(cfg, self._ticket(), tmp_path) is True

    def test_explicit_same_executor_route_skips_the_ladder(self, tmp_path):
        """An explicit same-executor pin bypasses the ladder -> combined mode."""
        cfg = {
            "executor": "claude-a",
            "executors": {
                "claude-a": {"type": "claude"},
                "claude-b": {"type": "claude"},
            },
            "pools": {"default": {"executors": ["claude-a", "claude-b"]}},
            "default_pool": "default",
            "executor_steps": {"implement": "claude-a", "review": "claude-a"},
        }
        assert _is_combined_mode(cfg, self._ticket(), tmp_path) is True

    def test_no_repo_root_preserves_legacy_same_name_comparison(self, tmp_path):
        """When repo_root is None, falls back to raw same-name comparison -> combined mode."""
        cfg = {
            "executor": "claude-a",
            "executors": {
                "claude-a": {"type": "claude"},
                "claude-b": {"type": "claude"},
            },
            "pools": {"default": {"executors": ["claude-a", "claude-b"]}},
            "default_pool": "default",
        }
        assert _is_combined_mode(cfg, self._ticket(executor="claude-a")) is True

    def test_explicit_reviewer_pin_matching_incapable_executor_is_split_mode(self, tmp_path):
        """The exact real-world shape: `lane init --interactive` accepting
        the default reviewer prompt (which mirrors executor) writes
        `reviewer: aider` explicitly into .lanegate.yml. That explicit pin
        would normally bypass the independence ladder straight to combined
        mode (test_explicit_same_executor_route_skips_the_ladder), but aider
        can't self-drive combined mode -- must still fall through to split."""
        cfg = {"executor": "aider", "reviewer": "aider"}
        assert _is_combined_mode(cfg, self._ticket(), tmp_path) is False


# ---------------------------------------------------------------------------
# _build_combined_prompt

class TestBuildCombinedPrompt:
    """Unit tests for _build_combined_prompt()."""

    def test_appendix_appended(self):
        """Combined prompt contains the implement prompt plus the review appendix."""
        ticket = {
            "id": "TICK-001",
            "title": "T",
            "close_criteria": "Done.",
            "_body": "Body.",
            "touches": ["a.py"],
        }
        result = _build_combined_prompt(ticket, "implement content here", "main")
        assert result.startswith("implement content here")
        assert "lanegate complete TICK-001" in result
        assert "lanegate review TICK-001 --verdict approved" in result
        assert "lanegate review TICK-001 --verdict changes_requested" in result
        assert "Do not exit" in result

    def test_git_diff_instruction_present(self):
        """Combined prompt instructs the agent to review its own diff."""
        ticket = {
            "id": "TICK-001",
            "title": "T",
            "close_criteria": "Done.",
            "_body": "Body.",
            "touches": ["a.py"],
        }
        result = _build_combined_prompt(ticket, "base prompt", "main")
        assert "git diff main..HEAD" in result


# ---------------------------------------------------------------------------
class TestRunAutoFixCycle:
    """Unit tests for run_auto_fix_cycle's control flow (TICK-120 Slice 2)."""

    def _ticket(
        self, tmp_path: Path, findings: str = "Reviewer requested: fix the off-by-one in foo.py."
    ) -> dict:
        tickets_dir = tmp_path / "tickets"
        tickets_dir.mkdir(exist_ok=True)
        path = _write_ticket(tickets_dir, "TICK-050", "code_complete", findings=findings)
        from lanegate.ticket import parse_ticket

        return parse_ticket(path)

    def _cfg(self, tmp_path: Path, max_attempts: int = 1) -> dict:
        cfg = _default_cfg(tmp_path)
        cfg["max_auto_fix_attempts"] = max_attempts
        return cfg

    def test_fix_drift_ok_approved_within_one_attempt(self, tmp_path):
        from lanegate.reviewer import DriftCheckResult

        ticket = self._ticket(tmp_path)
        cfg = self._cfg(tmp_path, max_attempts=3)

        with (
            patch("lanegate.orchestrate.autofix._git_head_sha", return_value="abc123"),
            patch("lanegate.orchestrate.autofix.run_fix_agent", return_value=True) as mock_fix,
            patch(
                "lanegate.orchestrate.autofix.run_drift_check", return_value=DriftCheckResult(ok=True)
            ) as mock_drift,
            patch("lanegate.orchestrate.autofix.run_review_agent", return_value=True) as mock_review,
            patch("lanegate.lifecycle.record_auto_fix_attempt") as mock_record,
        ):
            result = run_auto_fix_cycle(ticket, cfg, tmp_path, tmp_path / "worktrees" / "tick-050")

        assert result is True
        mock_fix.assert_called_once()
        mock_drift.assert_called_once()
        mock_review.assert_called_once()
        mock_record.assert_called_once()
        assert mock_record.call_args.kwargs.get("escalate", False) is False

    def test_drift_check_failure_escalates_immediately_even_with_budget_remaining(self, tmp_path):
        """A fix that touches an unrelated file (drift detected) must escalate
        right away — full autonomy never bypasses the drift-check gate, even
        with attempts remaining."""
        from lanegate.reviewer import DriftCheckResult

        ticket = self._ticket(tmp_path)
        cfg = self._cfg(tmp_path, max_attempts=3)

        with (
            patch("lanegate.orchestrate.autofix._git_head_sha", return_value="abc123"),
            patch("lanegate.orchestrate.autofix.run_fix_agent", return_value=True),
            patch(
                "lanegate.orchestrate.autofix.run_drift_check",
                return_value=DriftCheckResult(ok=False, reason="touched unrelated file"),
            ),
            patch("lanegate.orchestrate.autofix.run_review_agent") as mock_review,
            patch("lanegate.lifecycle.record_auto_fix_attempt") as mock_record,
        ):
            result = run_auto_fix_cycle(ticket, cfg, tmp_path, tmp_path / "worktrees" / "tick-050")

        assert result is False
        mock_review.assert_not_called()
        mock_record.assert_called_once()
        assert mock_record.call_args.kwargs["escalate"] is True
        assert "drift-check failed" in mock_record.call_args.kwargs["note"]

    def test_fix_pass_failure_escalates_without_drift_check(self, tmp_path):
        from lanegate.orchestrate.autofix import FixFailedError

        ticket = self._ticket(tmp_path)
        cfg = self._cfg(tmp_path, max_attempts=2)

        with (
            patch("lanegate.orchestrate.autofix._git_head_sha", return_value="abc123"),
            patch("lanegate.orchestrate.autofix.run_fix_agent", side_effect=FixFailedError("fix failed")),
            patch("lanegate.orchestrate.autofix.run_drift_check") as mock_drift,
            patch("lanegate.orchestrate.autofix.run_review_agent") as mock_review,
            patch("lanegate.lifecycle.record_auto_fix_attempt") as mock_record,
        ):
            result = run_auto_fix_cycle(ticket, cfg, tmp_path, tmp_path / "worktrees" / "tick-050")

        assert result is False
        mock_drift.assert_not_called()
        mock_review.assert_not_called()
        mock_record.assert_called_once()
        assert mock_record.call_args.kwargs["escalate"] is True
        assert "fix pass failed" in mock_record.call_args.kwargs["note"]

    def test_n_consecutive_changes_requested_escalates_at_exactly_n(self, tmp_path):
        from lanegate.reviewer import DriftCheckResult

        ticket = self._ticket(tmp_path)
        cfg = self._cfg(tmp_path, max_attempts=3)

        with (
            patch("lanegate.orchestrate.autofix._git_head_sha", return_value="abc123"),
            patch("lanegate.orchestrate.autofix.run_fix_agent", return_value=True),
            patch(
                "lanegate.orchestrate.autofix.run_drift_check", return_value=DriftCheckResult(ok=True)
            ),
            patch("lanegate.orchestrate.autofix.run_review_agent", return_value=False) as mock_review,
            patch("lanegate.lifecycle.record_auto_fix_attempt") as mock_record,
        ):
            result = run_auto_fix_cycle(ticket, cfg, tmp_path, tmp_path / "worktrees" / "tick-050")

        assert result is False
        assert mock_review.call_count == 3, "must retry up to the cap, not stop early"
        assert mock_record.call_count == 3
        last_call = mock_record.call_args_list[-1]
        assert last_call.kwargs["escalate"] is True
        assert "attempts exhausted" in last_call.kwargs["note"]
        assert last_call.kwargs["attempt"] == 3

    def test_human_escalation_retry_limit_caps_auto_fix_attempts(self, tmp_path):
        """The safety retry_limit is enforced even if the mechanical limit is higher."""
        from lanegate.reviewer import DriftCheckResult

        ticket = self._ticket(tmp_path)
        cfg = self._cfg(tmp_path, max_attempts=4)
        cfg["human_escalation"] = {"retry_limit": 2}

        with (
            patch("lanegate.orchestrate.autofix._git_head_sha", return_value="abc123"),
            patch("lanegate.orchestrate.autofix.run_fix_agent", return_value=True),
            patch(
                "lanegate.orchestrate.autofix.run_drift_check",
                return_value=DriftCheckResult(ok=True),
            ),
            patch("lanegate.orchestrate.autofix.run_review_agent", return_value=False) as mock_review,
            patch("lanegate.lifecycle.record_auto_fix_attempt") as mock_record,
        ):
            result = run_auto_fix_cycle(ticket, cfg, tmp_path, tmp_path / "worktrees" / "tick-050")

        assert result is False
        assert mock_review.call_count == 2
        assert mock_record.call_args.kwargs["attempt"] == 2
        assert mock_record.call_args.kwargs["max_attempts"] == 2
        assert mock_record.call_args.kwargs["escalate"] is True

    def test_declines_empty_findings_without_consuming_an_attempt(self, tmp_path):
        ticket = self._ticket(tmp_path, findings="")
        cfg = self._cfg(tmp_path)

        with (
            patch("lanegate.orchestrate.autofix.run_fix_agent") as mock_fix,
            patch("lanegate.orchestrate.autofix.run_drift_check") as mock_drift,
            patch("lanegate.orchestrate.autofix.run_review_agent") as mock_review,
            patch("lanegate.lifecycle.record_auto_fix_attempt") as mock_record,
        ):
            result = run_auto_fix_cycle(ticket, cfg, tmp_path, tmp_path / "worktrees" / "tick-050")

        assert result is False
        mock_fix.assert_not_called()
        mock_drift.assert_not_called()
        mock_review.assert_not_called()
        mock_record.assert_called_once()
        assert mock_record.call_args.kwargs["attempt"] == 0
        assert mock_record.call_args.kwargs["escalate"] is True
        assert "attempts exhausted" not in mock_record.call_args.kwargs["note"]


    def test_rate_limit_during_fix_hibernates_without_consuming_attempt(self, tmp_path):
        """When the fix agent is rate-limited, run_auto_fix_cycle hibernates the
        ticket (returns None) without calling record_auto_fix_attempt, so the
        attempt counter is NOT incremented."""
        from lanegate.orchestrate.autofix import RateLimitedFixError, run_auto_fix_cycle

        ticket = self._ticket(tmp_path)
        cfg = self._cfg(tmp_path, max_attempts=2)

        with (
            patch("lanegate.orchestrate.autofix._git_head_sha", return_value="abc123"),
            patch(
                "lanegate.orchestrate.autofix.run_fix_agent",
                side_effect=RateLimitedFixError("rate limit hit"),
            ),
            patch("lanegate.orchestrate.autofix.run_drift_check") as mock_drift,
            patch("lanegate.orchestrate.autofix.run_review_agent") as mock_review,
            patch("lanegate.lifecycle.record_auto_fix_attempt") as mock_record,
            patch("lanegate.lifecycle.cmd_hibernate") as mock_hibernate,
        ):
            result = run_auto_fix_cycle(ticket, cfg, tmp_path, tmp_path / "worktrees" / "tick-050")

        assert result is None, "None signals 'hibernated for rate limit', not False (escalated)"
        mock_hibernate.assert_called_once()
        # Attempt counter must NOT be incremented for a rate-limit interruption
        mock_record.assert_not_called()
        mock_drift.assert_not_called()
        mock_review.assert_not_called()

    def test_rate_limit_during_fix_actually_hibernates_without_mocking_cmd_hibernate(self, tmp_path):
        """Regression test: run_auto_fix_cycle is always invoked with a
        code_complete ticket (never in_progress — all loop.py call sites gate
        on code_complete/changes_requested before calling this function), so
        the real cmd_hibernate must accept a code_complete status instead of
        sys.exit(1)'ing with 'expected in_progress'. Deliberately does NOT
        mock lanegate.lifecycle.cmd_hibernate, unlike the test above, so a
        regression here would surface as a SystemExit instead of being
        silently masked."""
        from lanegate.orchestrate.autofix import RateLimitedFixError, run_auto_fix_cycle

        ticket = self._ticket(tmp_path)
        assert ticket["status"] == "code_complete"
        cfg = self._cfg(tmp_path, max_attempts=2)

        with (
            patch("lanegate.orchestrate.autofix._git_head_sha", return_value="abc123"),
            patch(
                "lanegate.orchestrate.autofix.run_fix_agent",
                side_effect=RateLimitedFixError("rate limit hit"),
            ),
            patch("lanegate.orchestrate.autofix.run_drift_check") as mock_drift,
            patch("lanegate.orchestrate.autofix.run_review_agent") as mock_review,
            patch("lanegate.lifecycle.record_auto_fix_attempt") as mock_record,
        ):
            result = run_auto_fix_cycle(ticket, cfg, tmp_path, tmp_path / "worktrees" / "tick-050")

        assert result is None
        mock_record.assert_not_called()
        mock_drift.assert_not_called()
        mock_review.assert_not_called()

        from lanegate.ticket import parse_ticket

        on_disk = parse_ticket(ticket["_path"])
        assert on_disk["status"] == "hibernated"

    def test_fix_failed_error_escalates_and_increments_attempt(self, tmp_path):
        """When the fix agent genuinely fails (FixFailedError), the cycle escalates,
        records the attempt, and returns False — attempt counter is consumed."""
        from lanegate.orchestrate.autofix import FixFailedError, run_auto_fix_cycle

        ticket = self._ticket(tmp_path)
        cfg = self._cfg(tmp_path, max_attempts=2)

        with (
            patch("lanegate.orchestrate.autofix._git_head_sha", return_value="abc123"),
            patch(
                "lanegate.orchestrate.autofix.run_fix_agent",
                side_effect=FixFailedError("agent failed"),
            ),
            patch("lanegate.orchestrate.autofix.run_drift_check") as mock_drift,
            patch("lanegate.orchestrate.autofix.run_review_agent") as mock_review,
            patch("lanegate.lifecycle.record_auto_fix_attempt") as mock_record,
            patch("lanegate.lifecycle.cmd_hibernate") as mock_hibernate,
        ):
            result = run_auto_fix_cycle(ticket, cfg, tmp_path, tmp_path / "worktrees" / "tick-050")

        assert result is False
        mock_record.assert_called_once()
        assert mock_record.call_args.kwargs["escalate"] is True
        assert "fix pass failed" in mock_record.call_args.kwargs["note"]
        mock_hibernate.assert_not_called()
        mock_drift.assert_not_called()
        mock_review.assert_not_called()

    def test_rate_limit_return_is_distinguishable_from_escalation(self, tmp_path):
        """None and False must be distinguishable so callers can tell 'hibernated'
        from 'escalated' — both are falsy in Python but have different semantics."""
        from lanegate.orchestrate.autofix import RateLimitedFixError, run_auto_fix_cycle

        ticket = self._ticket(tmp_path)
        cfg = self._cfg(tmp_path, max_attempts=2)

        with (
            patch("lanegate.orchestrate.autofix._git_head_sha", return_value="abc123"),
            patch(
                "lanegate.orchestrate.autofix.run_fix_agent",
                side_effect=RateLimitedFixError("rate limit"),
            ),
            patch("lanegate.lifecycle.record_auto_fix_attempt"),
            patch("lanegate.lifecycle.cmd_hibernate"),
        ):
            result = run_auto_fix_cycle(ticket, cfg, tmp_path, tmp_path / "worktrees" / "tick-050")

        assert result is None
        assert result is not False, "None and False must be distinct — callers use 'is None' checks"


class TestSplitModeAutoFix:
    """Integration tests: autonomy-gated auto-fix wiring in the split-mode
    review branch of _drain_loop (TICK-120 Slice 2)."""

    def _make_open_ticket(self, tmp_path: Path) -> Path:
        tickets_dir = tmp_path / "tickets"
        return _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"])

    def _fake_complete(self, tickets_dir: Path):
        def fake_complete(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            text = p.read_text().replace("status: open", "status: code_complete")
            p.write_text(text)

        return fake_complete

    def test_autonomy_full_runs_auto_fix_cycle_on_changes_requested(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        cfg["autonomy"] = "full"
        cfg["reviewer"] = "claude-process"
        tickets_dir = tmp_path / "tickets"
        self._make_open_ticket(tmp_path)

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", return_value=(0, "", "")),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.lifecycle.cmd_complete", side_effect=self._fake_complete(tickets_dir)),
            patch("lanegate.orchestrate.run_review_agent", return_value=False) as mock_review_agent,
            patch("lanegate.orchestrate.run_auto_fix_cycle", return_value=True) as mock_auto_fix,
            patch("lanegate.lifecycle.cmd_review"),
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True, human_review="per_ticket")

        mock_review_agent.assert_called_once()
        mock_auto_fix.assert_called_once()

    def test_autonomy_supervised_runs_auto_fix_for_ordinary_findings(self, tmp_path):
        """Supervised tickets fix ordinary findings before the human merge gate."""
        cfg = _default_cfg(tmp_path)
        cfg["reviewer"] = "claude-process"
        tickets_dir = tmp_path / "tickets"
        self._make_open_ticket(tmp_path)

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", return_value=(0, "", "")),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.lifecycle.cmd_complete", side_effect=self._fake_complete(tickets_dir)),
            patch("lanegate.orchestrate.run_review_agent", return_value=False) as mock_review_agent,
            patch("lanegate.orchestrate.run_auto_fix_cycle") as mock_auto_fix,
            patch("lanegate.lifecycle.cmd_review"),
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True, human_review="per_ticket")

        mock_review_agent.assert_called_once()
        mock_auto_fix.assert_called_once()

    def test_no_independent_reviewer_escalation_skips_auto_fix_and_keeps_its_reason(
        self, tmp_path
    ):
        """run_review_agent can return False by escalating straight to
        needs_review (e.g. no independent reviewer available) instead of
        recording a genuine changes_requested verdict -- the ticket is left
        at status=needs_review, not code_complete. run_auto_fix_cycle must
        not be called in that case: it assumes a code_complete precondition,
        finds no review findings, and its generic failure message would
        overwrite the ticket's real, more specific escalation reason."""
        cfg = _default_cfg(tmp_path)
        cfg["reviewer"] = "claude-process"
        tickets_dir = tmp_path / "tickets"
        self._make_open_ticket(tmp_path)

        def fake_escalate_to_needs_review(tid, cfg_, repo_root):
            # By the time run_review_agent runs, cmd_complete has already
            # flipped the ticket to code_complete -- mirror that same
            # transition _escalate_no_reviewer makes in production.
            p = tickets_dir / f"{tid}.md"
            text = p.read_text().replace("status: code_complete", "status: needs_review")
            text += "\n## Needs Review Reason\nNo healthy independent reviewer is available.\n"
            p.write_text(text)
            return False

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", return_value=(0, "", "")),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.lifecycle.cmd_complete", side_effect=self._fake_complete(tickets_dir)),
            patch(
                "lanegate.orchestrate.run_review_agent",
                side_effect=lambda ticket, *a, **kw: fake_escalate_to_needs_review(
                    ticket["id"], cfg, tmp_path
                ),
            ) as mock_review_agent,
            patch("lanegate.orchestrate.run_auto_fix_cycle") as mock_auto_fix,
            patch("lanegate.lifecycle.cmd_review"),
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True, human_review="per_ticket")

        mock_review_agent.assert_called_once()
        mock_auto_fix.assert_not_called()
        final_text = (tickets_dir / "TICK-001.md").read_text()
        assert "status: needs_review" in final_text
        assert "No healthy independent reviewer is available." in final_text
        assert "auto-fix/re-review did not reach approval" not in final_text

    def test_sensitive_review_findings_still_run_auto_fix(self, tmp_path):
        """TICK-348: autonomy/sensitivity no longer gates whether the fix runs
        — a P0 finding is fixed like any other; the human gate moved to the
        merge decision on the result, not the fix attempt itself."""
        cfg = _default_cfg(tmp_path)
        cfg["reviewer"] = "claude-process"
        tickets_dir = tmp_path / "tickets"
        self._make_open_ticket(tmp_path)

        def fake_complete(tid, cfg_, repo_root):
            path = tickets_dir / f"{tid}.md"
            path.write_text(path.read_text().replace("status: in_progress", "status: code_complete", 1))

        def fake_sensitive_review(ticket, *_args, **_kwargs):
            path = tickets_dir / f"{ticket['id']}.md"
            text = path.read_text()
            text = text.replace(
                "status: code_complete",
                "status: code_complete\nreview_verdict: changes_requested",
                1,
            )
            path.write_text(
                text.replace("Body.", "## Review Findings\n\n[P0] potential data loss"),
            )
            return False

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=_fake_start_writes_in_progress),
            patch("lanegate.orchestrate.invoke_executor", return_value=(0, "", "")),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete),
            patch("lanegate.orchestrate.run_review_agent", side_effect=fake_sensitive_review),
            patch("lanegate.orchestrate.run_auto_fix_cycle") as mock_auto_fix,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True, human_review="none")

        mock_auto_fix.assert_called_once()


class TestCombinedModeAutoFix:
    """Integration tests: autonomy-gated auto-fix wiring in the combined-mode
    changes_requested branch of _drain_loop (TICK-120 Slice 2)."""

    def _make_open_ticket(self, tmp_path: Path) -> Path:
        tickets_dir = tmp_path / "tickets"
        return _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"])

    def _fake_invoke_changes_requested(self, tickets_dir: Path):
        def fake_invoke_combined(ticket, cfg_, wt, *, log_stream=None, terminal_stream=None, prompt_override=None, repo_root=None, executor_override=None):
            p = tickets_dir / f"{ticket['id']}.md"
            text = p.read_text().replace(
                "status: open", "status: code_complete\nreview_verdict: changes_requested"
            )
            p.write_text(text)
            return (0, "", "")

        return fake_invoke_combined

    def test_autonomy_full_runs_auto_fix_cycle(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        cfg["autonomy"] = "full"
        tickets_dir = tmp_path / "tickets"
        self._make_open_ticket(tmp_path)

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch(
                "lanegate.orchestrate.invoke_executor",
                side_effect=self._fake_invoke_changes_requested(tickets_dir),
            ),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._is_combined_mode", return_value=True),
            patch("lanegate.lifecycle.cmd_complete") as mock_complete,
            patch("lanegate.orchestrate.run_auto_fix_cycle", return_value=True) as mock_auto_fix,
            patch("lanegate.lifecycle.cmd_review"),
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_complete.assert_not_called()
        mock_auto_fix.assert_called_once()

    def test_autonomy_absent_uses_supervised_auto_fix(self, tmp_path):
        """The default supervised policy fixes ordinary findings before pause."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        self._make_open_ticket(tmp_path)

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch(
                "lanegate.orchestrate.invoke_executor",
                side_effect=self._fake_invoke_changes_requested(tickets_dir),
            ),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._is_combined_mode", return_value=True),
            patch("lanegate.lifecycle.cmd_complete") as mock_complete,
            patch("lanegate.orchestrate.run_auto_fix_cycle") as mock_auto_fix,
            patch("lanegate.lifecycle.cmd_review"),
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_complete.assert_not_called()
        mock_auto_fix.assert_called_once()

        from lanegate.ticket import parse_ticket

        t = parse_ticket(tickets_dir / "TICK-001.md")
        assert t["status"] == "code_complete"
        assert t["review_verdict"] == "changes_requested"


def test_rebase_conflict_autofix(tmp_path):
    """Verify run_rebase_fix_agent resolves conflicts and continues rebase."""
    from lanegate.orchestrate.autofix import run_rebase_fix_agent

    ticket = {"id": "TICK-322"}
    cfg = {}
    worktree = tmp_path / "wt"
    worktree.mkdir()

    with patch("lanegate.orchestrate.loop.resolve_pool_executor", return_value="codex") as mock_resolve, \
         patch("lanegate.orchestrate.autofix.invoke_executor", return_value=(0, "", "")) as mock_exec, \
         patch("lanegate.orchestrate.loop._conflicted_files", return_value=["a.py"]), \
         patch("lanegate.orchestrate.loop._continue_rebase", return_value=(True, "")) as mock_cont, \
         patch("lanegate.orchestrate.run_report.record_direct_action_event") as mock_record, \
         patch("lanegate.orchestrate.autofix.commit_worktree_changes"):
        ok = run_rebase_fix_agent(
            ticket, cfg, tmp_path, worktree, "conflict in a.py", pool_name="codex"
        )

    assert ok is True
    mock_resolve.assert_called_once_with("fix", ticket, cfg, tmp_path, pool_name="codex")
    mock_exec.assert_called_once()
    assert mock_exec.call_args.kwargs["executor_override"] == "codex"
    mock_cont.assert_called_once_with(worktree, ["a.py"])
    assert mock_record.call_args.args[2] == "action_end"
    assert mock_record.call_args.kwargs["status"] == "success"
    assert ticket["requires_human_merge"] is True
    assert ticket["rebase_conflict_files"] == ["a.py"]


def test_rebase_conflict_autofix_sequential_metadata_then_code(tmp_path):
    """Verify sequential metadata-only then code conflict is handled cleanly."""
    from lanegate.orchestrate.autofix import run_rebase_fix_agent

    ticket = {"id": "TICK-534"}
    cfg = {"tickets_dir": ".lanegate/tickets"}
    worktree = tmp_path / "wt"
    worktree.mkdir()

    meta_file = ".lanegate/tickets/TICK-001.md"
    code_file = "app.py"

    conflicts_sequence = [[meta_file], [code_file]]
    conflicts_idx = 0

    def mock_conflicted(wt):
        nonlocal conflicts_idx
        if conflicts_idx < len(conflicts_sequence):
            return conflicts_sequence[conflicts_idx]
        return []

    continue_calls = []

    def mock_continue(wt, files):
        nonlocal conflicts_idx
        continue_calls.append(files)
        if conflicts_idx == 0:
            conflicts_idx += 1
            return False, "conflict on next commit"
        return True, ""

    with patch("lanegate.orchestrate.loop.resolve_pool_executor", return_value="codex"), \
         patch("lanegate.orchestrate.autofix.invoke_executor", return_value=(0, "", "")) as mock_exec, \
         patch("lanegate.orchestrate.loop._conflicted_files", side_effect=mock_conflicted), \
         patch("lanegate.orchestrate.loop._continue_rebase", side_effect=mock_continue), \
         patch("lanegate.reconciliation.resolve_metadata_conflict") as mock_res_meta, \
         patch("lanegate.orchestrate.run_report.record_direct_action_event") as mock_record, \
         patch("lanegate.orchestrate.autofix.commit_worktree_changes"):
        ok = run_rebase_fix_agent(ticket, cfg, tmp_path, worktree, "detail")

    assert ok is True
    # Metadata conflict resolved without LLM call for step 1
    mock_res_meta.assert_called_once_with(worktree, meta_file)
    # LLM invoke_executor called only once (for code_file)
    mock_exec.assert_called_once()
    assert mock_record.call_count >= 2
    assert ticket["requires_human_merge"] is True
    assert ticket["rebase_conflict_files"] == [code_file]


def test_rebase_metadata_conflict_does_not_require_human_merge(tmp_path):
    """Deterministic ticket metadata reconciliation remains safe for full autonomy."""
    from lanegate.orchestrate.autofix import run_rebase_fix_agent

    ticket = {"id": "TICK-534"}
    cfg = {"tickets_dir": ".lanegate/tickets"}
    worktree = tmp_path / "wt"
    worktree.mkdir()
    meta_file = ".lanegate/tickets/TICK-001.md"

    with patch("lanegate.orchestrate.loop._conflicted_files", return_value=[meta_file]), \
         patch("lanegate.orchestrate.loop._continue_rebase", return_value=(True, "")), \
         patch("lanegate.reconciliation.resolve_metadata_conflict") as mock_resolve, \
         patch("lanegate.orchestrate.autofix.invoke_executor") as mock_executor, \
         patch("lanegate.orchestrate.autofix.commit_worktree_changes"):
        ok = run_rebase_fix_agent(ticket, cfg, tmp_path, worktree, "metadata conflict")

    assert ok is True
    mock_resolve.assert_called_once_with(worktree, meta_file)
    mock_executor.assert_not_called()
    assert "requires_human_merge" not in ticket


def test_rebase_conflict_autofix_stuck_aborts(tmp_path):
    """Verify unchanged marker-free conflict state causes recovery to fail closed."""
    from lanegate.orchestrate.autofix import run_rebase_fix_agent

    ticket = {"id": "TICK-534"}
    cfg = {}
    worktree = tmp_path / "wt"
    worktree.mkdir()
    # A no-op agent can leave unresolved content that has no marker triplet.
    # The second pass must be rejected by snapshot comparison, not marker
    # detection, or no-progress recovery would be untested.
    (worktree / "code.py").write_text("unresolved agent output\n")

    with patch("lanegate.orchestrate.loop.resolve_pool_executor", return_value="codex"), \
         patch("lanegate.orchestrate.autofix.invoke_executor", return_value=(0, "", "")), \
         patch("lanegate.orchestrate.loop._conflicted_files", return_value=["code.py"]), \
         patch("lanegate.orchestrate.loop._continue_rebase", return_value=(False, "next conflict")), \
         patch("lanegate.orchestrate.loop._abort_rebase") as mock_abort, \
         patch("lanegate.orchestrate.run_report.record_direct_action_event") as mock_record:
        ok = run_rebase_fix_agent(ticket, cfg, tmp_path, worktree, "stuck detail")

    assert ok is False
    mock_abort.assert_called_once_with(worktree)
    assert any(call.args[2] == "rebase_stuck" for call in mock_record.call_args_list)
    action_ends = [call for call in mock_record.call_args_list if call.args[2] == "action_end"]
    assert len(action_ends) == 1
    assert action_ends[0].kwargs["status"] == "failed"


@pytest.mark.parametrize(
    "agent_output",
    [None, "<<<<<<< HEAD\npartially resolved\n", " <<<<<<< HEAD\n main\n =======\n topic\n >>>>>>> branch\n"],
)
def test_rebase_conflict_autofix_cannot_stage_remaining_marker_hunk(tmp_path, agent_output):
    """A real Git rebase must fail closed for complete or partial hunks."""
    from lanegate.orchestrate.autofix import run_rebase_fix_agent
    from lanegate.orchestrate.loop import is_mid_rebase

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")
    (repo / "conflict.txt").write_text("base\n")
    git("add", "conflict.txt")
    git("commit", "-m", "base")
    git("checkout", "-b", "topic")
    (repo / "conflict.txt").write_text("topic\n")
    git("commit", "-am", "topic")
    git("checkout", "main")
    (repo / "conflict.txt").write_text("main\n")
    git("commit", "-am", "main")
    git("checkout", "topic")
    assert subprocess.run(
        ["git", "rebase", "main"], cwd=repo, capture_output=True, text=True
    ).returncode != 0
    assert is_mid_rebase(repo)

    def run_agent(*_args, **_kwargs):
        if agent_output is not None:
            (repo / "conflict.txt").write_text(agent_output)
        return 0, "", ""

    with (
        patch("lanegate.orchestrate.loop.resolve_pool_executor", return_value="codex"),
        patch("lanegate.orchestrate.autofix.invoke_executor", side_effect=run_agent),
        patch("lanegate.orchestrate.autofix.commit_worktree_changes") as mock_commit,
        patch("lanegate.orchestrate.run_report.record_direct_action_event"),
    ):
        ok = run_rebase_fix_agent({"id": "TICK-534"}, {}, repo, repo, "conflict")

    assert ok is False
    assert not is_mid_rebase(repo)
    assert (repo / "conflict.txt").read_text() == "topic\n"
    mock_commit.assert_not_called()


def test_rebase_conflict_autofix_rejects_unmatched_rebase_marker_when_legitimate_marker_removed(tmp_path):
    """If original file contained one legitimate marker line but agent deleted it and left an unmatched rebase marker, residual marker check must fail closed."""
    from lanegate.orchestrate.autofix import run_rebase_fix_agent
    from lanegate.orchestrate.loop import is_mid_rebase

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")

    (repo / "source.py").write_text(
        "def foo():\n"
        "    # <<<<<<< HEAD\n"
        "    return 1\n"
        "\n"
        "def bar():\n"
        "    return 0\n"
    )
    git("add", "source.py")
    git("commit", "-m", "base")

    git("checkout", "-b", "topic")
    (repo / "source.py").write_text(
        "def foo():\n"
        "    # <<<<<<< HEAD\n"
        "    return 1\n"
        "\n"
        "def bar():\n"
        "    return 'topic'\n"
    )
    git("commit", "-am", "topic change")

    git("checkout", "main")
    (repo / "source.py").write_text(
        "def foo():\n"
        "    # <<<<<<< HEAD\n"
        "    return 1\n"
        "\n"
        "def bar():\n"
        "    return 'main'\n"
    )
    git("commit", "-am", "main change")

    git("checkout", "topic")
    assert subprocess.run(
        ["git", "rebase", "main"], cwd=repo, capture_output=True, text=True
    ).returncode != 0
    assert is_mid_rebase(repo)

    def incomplete_agent_resolution(*_args, **_kwargs):
        (repo / "source.py").write_text(
            "def foo():\n"
            "    return 1\n"
            "\n"
            "def bar():\n"
            "<<<<<<< HEAD\n"
            "    return 'main'\n"
            "    return 'topic'\n"
        )
        return 0, "", ""

    with (
        patch("lanegate.orchestrate.loop.resolve_pool_executor", return_value="codex"),
        patch("lanegate.orchestrate.autofix.invoke_executor", side_effect=incomplete_agent_resolution),
        patch("lanegate.orchestrate.autofix.commit_worktree_changes") as mock_commit,
        patch("lanegate.orchestrate.run_report.record_direct_action_event"),
    ):
        ok = run_rebase_fix_agent({"id": "TICK-534"}, {}, repo, repo, "conflict")

    assert ok is False
    assert not is_mid_rebase(repo)
    mock_commit.assert_not_called()


def test_rebase_conflict_autofix_rejects_stray_marker_with_dual_unstable_context(tmp_path):
    """When stage-2 and stage-3 both have a legitimate marker but differ on
    both the preceding and following line (prev_stable=False, next_stable=False),
    a stray marker at a completely different position must still be caught.

    Regression test for the finding at autofix.py:478: dual-unstable context
    caused positional matching to be skipped entirely, letting a residual
    conflict marker through to commit_worktree_changes.
    """
    from lanegate.orchestrate.autofix import run_rebase_fix_agent
    from lanegate.orchestrate.loop import is_mid_rebase

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    # Base: file has a legitimate '<<<<<<< HEAD' line with stable surrounding
    # context (prev="# context-base-before", next="# context-base-after").
    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")

    (repo / "source.py").write_text(
        "# context-base-before\n"
        "<<<<<<< HEAD\n"
        "# context-base-after\n"
        "\n"
        "def bar():\n"
        "    return 0\n"
    )
    git("add", "source.py")
    git("commit", "-m", "base")

    # Topic: change the lines AROUND the marker so stage-3 context differs.
    git("checkout", "-b", "topic")
    (repo / "source.py").write_text(
        "# context-topic-before\n"
        "<<<<<<< HEAD\n"
        "# context-topic-after\n"
        "\n"
        "def bar():\n"
        "    return 'topic'\n"
    )
    git("commit", "-am", "topic change")

    # Main: change the lines AROUND the marker differently so stage-2 context
    # differs from stage-3 → both prev_stable and next_stable become False.
    git("checkout", "main")
    (repo / "source.py").write_text(
        "# context-main-before\n"
        "<<<<<<< HEAD\n"
        "# context-main-after\n"
        "\n"
        "def bar():\n"
        "    return 'main'\n"
    )
    git("commit", "-am", "main change")

    git("checkout", "topic")
    assert subprocess.run(
        ["git", "rebase", "main"], cwd=repo, capture_output=True, text=True
    ).returncode != 0
    assert is_mid_rebase(repo)

    def agent_leaves_stray_marker(*_args, **_kwargs):
        # Agent resolves the real conflict but leaves a stray <<<<<<< HEAD
        # at a completely different position (end of file, different context).
        (repo / "source.py").write_text(
            "# context-main-before\n"
            "<<<<<<< HEAD\n"
            "# context-main-after\n"
            "\n"
            "def bar():\n"
            "    return 'merged'\n"
            "<<<<<<< HEAD\n"
            "    extra stray line\n"
        )
        return 0, "", ""

    with (
        patch("lanegate.orchestrate.loop.resolve_pool_executor", return_value="codex"),
        patch("lanegate.orchestrate.autofix.invoke_executor", side_effect=agent_leaves_stray_marker),
        patch("lanegate.orchestrate.autofix.commit_worktree_changes") as mock_commit,
        patch("lanegate.orchestrate.run_report.record_direct_action_event"),
    ):
        ok = run_rebase_fix_agent({"id": "TICK-534"}, {}, repo, repo, "conflict")

    assert ok is False, "stray marker with dual-unstable context must be rejected"
    assert not is_mid_rebase(repo)
    mock_commit.assert_not_called()

def test_rebase_metadata_recovery_preserves_historical_marker_text(tmp_path):
    """Historical marker text in a ticket body is not an active conflict hunk."""
    from lanegate.orchestrate.autofix import run_rebase_fix_agent
    from lanegate.orchestrate.loop import is_mid_rebase

    repo = tmp_path / "repo"
    ticket_path = repo / ".lanegate" / "tickets" / "TICK-001.md"
    ticket_path.parent.mkdir(parents=True)

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    def write_ticket(status):
        ticket_path.write_text(
            "---\n"
            "id: TICK-001\n"
            "title: Historical marker fixture\n"
            f"status: {status}\n"
            "---\n"
            "## Historical log\n"
            "<<<<<<< HEAD\n"
            "prior text\n"
            "=======\n"
            "other prior text\n"
            ">>>>>>> old-branch\n"
        )

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")
    write_ticket("open")
    git("add", ".lanegate/tickets/TICK-001.md")
    git("commit", "-m", "base")
    git("checkout", "-b", "topic")
    write_ticket("code_complete")
    git("commit", "-am", "topic status")
    git("checkout", "main")
    write_ticket("needs_review")
    git("commit", "-am", "main status")
    git("checkout", "topic")
    assert subprocess.run(
        ["git", "rebase", "main"], cwd=repo, capture_output=True, text=True
    ).returncode != 0
    assert is_mid_rebase(repo)

    with (
        patch("lanegate.orchestrate.autofix.invoke_executor") as mock_executor,
        patch("lanegate.orchestrate.autofix.commit_worktree_changes"),
        patch("lanegate.orchestrate.run_report.record_direct_action_event"),
    ):
        ok = run_rebase_fix_agent(
            {"id": "TICK-534"},
            {"tickets_dir": ".lanegate/tickets"},
            repo,
            repo,
            "metadata conflict",
        )

    assert ok is True
    assert not is_mid_rebase(repo)
    mock_executor.assert_not_called()
    assert "<<<<<<< HEAD" in ticket_path.read_text()


def test_rebase_mixed_conflict_preserves_historical_ticket_marker_text(tmp_path):
    """A mixed batch scans only source files for residual agent markers."""
    from lanegate.orchestrate.autofix import run_rebase_fix_agent
    from lanegate.orchestrate.loop import is_mid_rebase

    repo = tmp_path / "repo"
    ticket_path = repo / ".lanegate" / "tickets" / "TICK-001.md"
    ticket_path.parent.mkdir(parents=True)
    source_path = repo / "conflict.txt"

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    def write_ticket(status):
        ticket_path.write_text(
            "---\n"
            "id: TICK-001\n"
            "title: Historical marker fixture\n"
            f"status: {status}\n"
            "---\n"
            "## Historical log\n"
            "<<<<<<< HEAD\nprior text\n=======\nother prior text\n>>>>>>> old-branch\n"
        )

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")
    write_ticket("open")
    source_path.write_text("base\n")
    git("add", ".lanegate/tickets/TICK-001.md", "conflict.txt")
    git("commit", "-m", "base")
    git("checkout", "-b", "topic")
    write_ticket("code_complete")
    source_path.write_text("topic\n")
    git("commit", "-am", "topic changes")
    git("checkout", "main")
    write_ticket("needs_review")
    source_path.write_text("main\n")
    git("commit", "-am", "main changes")
    git("checkout", "topic")
    assert subprocess.run(
        ["git", "rebase", "main"], cwd=repo, capture_output=True, text=True
    ).returncode != 0
    assert is_mid_rebase(repo)

    def resolve_mixed_batch(*_args, **_kwargs):
        source_path.write_text("resolved source\n")
        return 0, "", ""

    ticket = {"id": "TICK-534"}
    with (
        patch("lanegate.orchestrate.loop.resolve_pool_executor", return_value="codex"),
        patch("lanegate.orchestrate.autofix.invoke_executor", side_effect=resolve_mixed_batch) as mock_exec,
        patch("lanegate.orchestrate.autofix.commit_worktree_changes"),
        patch("lanegate.orchestrate.run_report.record_direct_action_event"),
    ):
        ok = run_rebase_fix_agent(
            ticket, {"tickets_dir": ".lanegate/tickets"}, repo, repo, "mixed conflict"
        )

    assert ok is True
    assert not is_mid_rebase(repo)
    assert "<<<<<<< HEAD" in ticket_path.read_text()
    assert source_path.read_text() == "resolved source\n"
    assert ticket["rebase_conflict_files"] == ["conflict.txt"]
    assert ".lanegate/tickets/TICK-001.md" not in mock_exec.call_args.kwargs["prompt_override"]
    assert "conflict.txt" in mock_exec.call_args.kwargs["prompt_override"]


def test_rebase_source_recovery_allows_new_marker_prefix_content(tmp_path):
    """A correctly resolved source file may introduce a marker-prefix line."""
    from lanegate.orchestrate.autofix import run_rebase_fix_agent
    from lanegate.orchestrate.loop import is_mid_rebase

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")
    (repo / "conflict.txt").write_text("base\n")
    git("add", "conflict.txt")
    git("commit", "-m", "base")
    git("checkout", "-b", "topic")
    (repo / "conflict.txt").write_text("topic\n")
    git("commit", "-am", "topic")
    git("checkout", "main")
    (repo / "conflict.txt").write_text("main\n")
    git("commit", "-am", "main")
    git("checkout", "topic")
    assert subprocess.run(
        ["git", "rebase", "main"], cwd=repo, capture_output=True, text=True
    ).returncode != 0
    assert is_mid_rebase(repo)

    def resolve_with_delimiter(*_args, **_kwargs):
        (repo / "conflict.txt").write_text("Setup and installation\n======================\n")
        return 0, "", ""

    with (
        patch("lanegate.orchestrate.loop.resolve_pool_executor", return_value="codex"),
        patch("lanegate.orchestrate.autofix.invoke_executor", side_effect=resolve_with_delimiter),
        patch("lanegate.orchestrate.autofix.commit_worktree_changes"),
        patch("lanegate.orchestrate.run_report.record_direct_action_event"),
    ):
        ok = run_rebase_fix_agent({"id": "TICK-534"}, {}, repo, repo, "source conflict")

    assert ok is True
    assert not is_mid_rebase(repo)
    assert (repo / "conflict.txt").read_text() == "Setup and installation\n======================\n"


def test_rebase_source_recovery_allows_preexisting_marker_lines(tmp_path):
    """A source file containing pre-existing <<<<<<< EXAMPLE lines passes recovery when conflict is resolved."""
    from lanegate.orchestrate.autofix import run_rebase_fix_agent
    from lanegate.orchestrate.loop import is_mid_rebase

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")
    guide = repo / "guide.md"
    guide.write_text("Introduction\n<<<<<<< EXAMPLE\nSample guide content\n>>>>>>> EXAMPLE\nbase\n")
    git("add", "guide.md")
    git("commit", "-m", "base")
    git("checkout", "-b", "topic")
    guide.write_text("Introduction\n<<<<<<< EXAMPLE\nSample guide content\n>>>>>>> EXAMPLE\ntopic edit\n")
    git("commit", "-am", "topic")
    git("checkout", "main")
    guide.write_text("Introduction\n<<<<<<< EXAMPLE\nSample guide content\n>>>>>>> EXAMPLE\nmain edit\n")
    git("commit", "-am", "main")
    git("checkout", "topic")
    assert subprocess.run(
        ["git", "rebase", "main"], cwd=repo, capture_output=True, text=True
    ).returncode != 0
    assert is_mid_rebase(repo)

    def resolve_guide_conflict(*_args, **_kwargs):
        guide.write_text("Introduction\n<<<<<<< EXAMPLE\nSample guide content\n>>>>>>> EXAMPLE\nresolved edit\n")
        return 0, "", ""

    with (
        patch("lanegate.orchestrate.loop.resolve_pool_executor", return_value="codex"),
        patch("lanegate.orchestrate.autofix.invoke_executor", side_effect=resolve_guide_conflict),
        patch("lanegate.orchestrate.autofix.commit_worktree_changes"),
        patch("lanegate.orchestrate.run_report.record_direct_action_event"),
    ):
        ok = run_rebase_fix_agent({"id": "TICK-534"}, {}, repo, repo, "source conflict")

    assert ok is True
    assert not is_mid_rebase(repo)
    assert "<<<<<<< EXAMPLE" in guide.read_text()
    assert "resolved edit" in guide.read_text()


def test_rebase_source_recovery_rejects_duplicated_preexisting_marker(tmp_path):
    """A source file containing 1 pre-existing marker line must fail if resolution leaves a second residual marker line."""
    from lanegate.orchestrate.autofix import run_rebase_fix_agent
    from lanegate.orchestrate.loop import is_mid_rebase

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")
    guide = repo / "guide.md"
    guide.write_text("Introduction\n<<<<<<< HEAD\nSample guide content\n>>>>>>> EXAMPLE\nbase\n")
    git("add", "guide.md")
    git("commit", "-m", "base")
    git("checkout", "-b", "topic")
    guide.write_text("Introduction\n<<<<<<< HEAD\nSample guide content\n>>>>>>> EXAMPLE\ntopic edit\n")
    git("commit", "-am", "topic")
    git("checkout", "main")
    guide.write_text("Introduction\n<<<<<<< HEAD\nSample guide content\n>>>>>>> EXAMPLE\nmain edit\n")
    git("commit", "-am", "main")
    git("checkout", "topic")
    assert subprocess.run(
        ["git", "rebase", "main"], cwd=repo, capture_output=True, text=True
    ).returncode != 0
    assert is_mid_rebase(repo)

    def incomplete_resolution(*_args, **_kwargs):
        # Leave pre-existing <<<<<<< HEAD PLUS a second residual <<<<<<< HEAD from git's conflict marker
        guide.write_text("Introduction\n<<<<<<< HEAD\n<<<<<<< HEAD\nSample guide content\n>>>>>>> EXAMPLE\nresolved edit\n")
        return 0, "", ""

    with (
        patch("lanegate.orchestrate.loop.resolve_pool_executor", return_value="codex"),
        patch("lanegate.orchestrate.autofix.invoke_executor", side_effect=incomplete_resolution),
        patch("lanegate.orchestrate.autofix.commit_worktree_changes"),
        patch("lanegate.orchestrate.run_report.record_direct_action_event") as mock_record,
    ):
        ok = run_rebase_fix_agent({"id": "TICK-534"}, {}, repo, repo, "source conflict")

    assert ok is False
    assert not is_mid_rebase(repo)
    assert any(
        call.args[2] == "rebase_markers_remaining"
        for call in mock_record.call_args_list
    )


def test_rebase_source_recovery_rejects_replaced_preexisting_marker_with_residual_conflict_marker(tmp_path):
    """A source file containing 1 pre-existing marker line must fail if resolution removes the original marker line but leaves a residual conflict marker line."""
    from lanegate.orchestrate.autofix import run_rebase_fix_agent
    from lanegate.orchestrate.loop import is_mid_rebase

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")
    guide = repo / "guide.md"
    guide.write_text("Header\nIntroduction\n<<<<<<< HEAD\nSample guide content\n>>>>>>> EXAMPLE\nbase\n")
    git("add", "guide.md")
    git("commit", "-m", "base")
    git("checkout", "-b", "topic")
    guide.write_text("Header\nIntroduction\n<<<<<<< HEAD\nSample guide content\n>>>>>>> EXAMPLE\ntopic edit\n")
    git("commit", "-am", "topic")
    git("checkout", "main")
    guide.write_text("Header\nIntroduction\n<<<<<<< HEAD\nSample guide content\n>>>>>>> EXAMPLE\nmain edit\n")
    git("commit", "-am", "main")
    git("checkout", "topic")
    assert subprocess.run(
        ["git", "rebase", "main"], cwd=repo, capture_output=True, text=True
    ).returncode != 0
    assert is_mid_rebase(repo)

    def incomplete_resolution(*_args, **_kwargs):
        # Remove legitimate <<<<<<< HEAD block, but leave git's conflict marker <<<<<<< HEAD (count remains 1)
        guide.write_text("Header\n<<<<<<< HEAD\nresolved edit\n")
        return 0, "", ""

    with (
        patch("lanegate.orchestrate.loop.resolve_pool_executor", return_value="codex"),
        patch("lanegate.orchestrate.autofix.invoke_executor", side_effect=incomplete_resolution),
        patch("lanegate.orchestrate.autofix.commit_worktree_changes"),
        patch("lanegate.orchestrate.run_report.record_direct_action_event") as mock_record,
    ):
        ok = run_rebase_fix_agent({"id": "TICK-534"}, {}, repo, repo, "source conflict")

    assert ok is False
    assert not is_mid_rebase(repo)
    assert any(
        call.args[2] == "rebase_markers_remaining"
        for call in mock_record.call_args_list
    )


def test_rebase_source_recovery_rejects_marker_replacement_when_preceding_line_matches(tmp_path):
    """A pre-existing marker line replaced by an incomplete conflict resolution must fail closed even if count matches and preceding non-marker context matches."""
    from lanegate.orchestrate.autofix import run_rebase_fix_agent
    from lanegate.orchestrate.loop import is_mid_rebase

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")
    src = repo / "helper.py"
    src.write_text("def helper():\n    # <<<<<<< HEAD\n    return 'original'\n")
    git("add", "helper.py")
    git("commit", "-m", "base")
    git("checkout", "-b", "topic")
    src.write_text("def helper():\n    # <<<<<<< HEAD\n    return 'original'\n    # topic edit\n")
    git("commit", "-am", "topic")
    git("checkout", "main")
    src.write_text("def helper():\n    # <<<<<<< HEAD\n    return 'original'\n    # main edit\n")
    git("commit", "-am", "main")
    git("checkout", "topic")
    assert subprocess.run(
        ["git", "rebase", "main"], cwd=repo, capture_output=True, text=True
    ).returncode != 0
    assert is_mid_rebase(repo)

    def incomplete_resolution(*_args, **_kwargs):
        # Remove legitimate # <<<<<<< HEAD line, but leave git's conflict marker <<<<<<< HEAD (count remains 1)
        src.write_text("def helper():\n    return 'original'\n<<<<<<< HEAD\n    # main edit\n    # topic edit\n")
        return 0, "", ""

    with (
        patch("lanegate.orchestrate.loop.resolve_pool_executor", return_value="codex"),
        patch("lanegate.orchestrate.autofix.invoke_executor", side_effect=incomplete_resolution),
        patch("lanegate.orchestrate.autofix.commit_worktree_changes"),
        patch("lanegate.orchestrate.run_report.record_direct_action_event") as mock_record,
    ):
        ok = run_rebase_fix_agent({"id": "TICK-534"}, {}, repo, repo, "source conflict")

    assert ok is False
    assert not is_mid_rebase(repo)
    assert any(
        call.args[2] == "rebase_markers_remaining"
        for call in mock_record.call_args_list
    )


def test_rebase_source_recovery_rejects_marker_replacement_when_following_line_matches(tmp_path):
    """A pre-existing middle marker line replaced by incomplete conflict resolution must fail closed when following line matches but preceding line differs."""
    from lanegate.orchestrate.autofix import run_rebase_fix_agent
    from lanegate.orchestrate.loop import is_mid_rebase

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")
    guide = repo / "guide.md"
    guide.write_text("Header\n<<<<<<< HEAD\nSample content\nFooter\n")
    git("add", "guide.md")
    git("commit", "-m", "base")
    git("checkout", "-b", "topic")
    guide.write_text("Header\n<<<<<<< HEAD\nSample content\nFooter\ntopic edit\n")
    git("commit", "-am", "topic")
    git("checkout", "main")
    guide.write_text("Header\n<<<<<<< HEAD\nSample content\nFooter\nmain edit\n")
    git("commit", "-am", "main")
    git("checkout", "topic")
    assert subprocess.run(
        ["git", "rebase", "main"], cwd=repo, capture_output=True, text=True
    ).returncode != 0
    assert is_mid_rebase(repo)

    def incomplete_resolution(*_args, **_kwargs):
        # Deletes legit marker line at line 2 and leaves stray marker at different position (prev line is Footer, next line is Sample content)
        guide.write_text("Header\nSample content\nFooter\n<<<<<<< HEAD\nSample content\nmain edit\ntopic edit\n")
        return 0, "", ""

    with (
        patch("lanegate.orchestrate.loop.resolve_pool_executor", return_value="codex"),
        patch("lanegate.orchestrate.autofix.invoke_executor", side_effect=incomplete_resolution),
        patch("lanegate.orchestrate.autofix.commit_worktree_changes"),
        patch("lanegate.orchestrate.run_report.record_direct_action_event") as mock_record,
    ):
        ok = run_rebase_fix_agent({"id": "TICK-534"}, {}, repo, repo, "source conflict")

    assert ok is False
    assert not is_mid_rebase(repo)
    assert any(
        call.args[2] == "rebase_markers_remaining"
        for call in mock_record.call_args_list
    )



def test_rebase_metadata_reconciliation_exception_aborts_and_fails_closed(tmp_path):
    """An exception raised during metadata conflict resolution aborts the rebase and fails closed."""
    from lanegate.orchestrate.autofix import run_rebase_fix_agent
    from lanegate.orchestrate.loop import is_mid_rebase

    repo = tmp_path / "repo"
    ticket_path = repo / ".lanegate" / "tickets" / "TICK-001.md"
    ticket_path.parent.mkdir(parents=True)

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    def write_ticket(status):
        ticket_path.write_text(
            "---\n"
            "id: TICK-001\n"
            "title: Metadata conflict ticket\n"
            f"status: {status}\n"
            "---\n"
            "Body.\n"
        )

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")
    write_ticket("open")
    git("add", ".lanegate/tickets/TICK-001.md")
    git("commit", "-m", "base")
    git("checkout", "-b", "topic")
    write_ticket("code_complete")
    git("commit", "-am", "topic status")
    git("checkout", "main")
    write_ticket("needs_review")
    git("commit", "-am", "main status")
    git("checkout", "topic")
    assert subprocess.run(
        ["git", "rebase", "main"], cwd=repo, capture_output=True, text=True
    ).returncode != 0
    assert is_mid_rebase(repo)

    with (
        patch(
            "lanegate.reconciliation.resolve_metadata_conflict",
            side_effect=ValueError("Malformed ticket YAML during reconciliation"),
        ),
        patch("lanegate.orchestrate.run_report.record_direct_action_event") as mock_record,
    ):
        ok = run_rebase_fix_agent(
            {"id": "TICK-534"},
            {"tickets_dir": ".lanegate/tickets"},
            repo,
            repo,
            "metadata conflict",
        )

    assert ok is False
    assert not is_mid_rebase(repo)
    assert any(
        call.kwargs.get("status") == "failed" and call.args[2] == "action_end"
        for call in mock_record.call_args_list
    )


def test_run_review_agent_blocks_mid_rebase(tmp_path):
    """Verify run_review_agent refuses to dispatch while worktree is mid-rebase."""
    from lanegate.orchestrate.review import run_review_agent

    ticket = {"id": "TICK-534", "worktree": str(tmp_path / "wt")}
    wt = tmp_path / "wt"
    wt.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=wt, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=wt, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=wt, check=True)
    (wt / "tracked.py").write_text("value = 1\n")
    subprocess.run(["git", "add", "tracked.py"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=wt, check=True, capture_output=True)
    cfg = {"tickets_dir": ".lanegate/tickets"}

    with patch("lanegate.orchestrate.loop.is_mid_rebase", return_value=True) as mock_mid_rebase, \
         patch("lanegate.reviewer.get_worktree_diff") as mock_diff, \
         patch("lanegate.orchestrate.review._escalate_harness_error", return_value=False) as mock_esc:
        res = run_review_agent(ticket, tmp_path, cfg=cfg)

    assert res is False
    mock_mid_rebase.assert_called_once_with(wt)
    mock_esc.assert_called_once()
    mock_diff.assert_not_called()



class TestRunFixAgentExcludesReviewer:
    """TICK-348: the fix executor must never be the reviewer whose findings
    are being fixed — a reviewer fixing its own findings has no independent
    check (same principle as TICK-345, one step later in the cycle)."""

    def test_run_fix_agent_excludes_review_driver(self, tmp_path):
        from lanegate.orchestrate import run_fix_agent

        ticket = {
            "id": "TICK-100",
            "title": "Fix me",
            "close_criteria": "Passes.",
            "review_driver": "claude-b",
        }
        cfg = _default_cfg(tmp_path)
        cfg["default_pool"] = "pool1"
        cfg["pools"] = {"pool1": {"executors": ["claude-a", "claude-b"]}}

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/x.py"),
            patch("lanegate.reviewer.build_fix_prompt", return_value="fix prompt"),
            patch(
                "lanegate.orchestrate.autofix.invoke_executor", return_value=(0, "", "")
            ) as mock_invoke,
            patch("lanegate.orchestrate.autofix.commit_worktree_changes", return_value=True),
            patch("lanegate.orchestrate.autofix._git_head_sha", return_value="sha-after"),
        ):
            result = run_fix_agent(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        assert result is True
        assert mock_invoke.call_args.kwargs["executor_override"] == "claude-a"
        assert mock_invoke.call_args.kwargs["executor_override"] != "claude-b"

    def test_run_fix_agent_declines_when_reviewer_is_only_executor(self, tmp_path):
        """A reviewer must never be sent back in as its own fix agent when
        the pool has no eligible alternative."""
        from lanegate.orchestrate.autofix import FixFailedError, run_fix_agent

        ticket = {
            "id": "TICK-050",
            "title": "Fix isolation",
            "close_criteria": "Tests pass.",
            "review_driver": "codex",
        }
        cfg = {"executor": "codex"}

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.reviewer.build_fix_prompt", return_value="fix prompt"),
            patch("lanegate.orchestrate.loop.resolve_pool_executor", return_value="codex"),
            patch("lanegate.orchestrate.autofix.invoke_executor") as mock_invoke,
        ):
            with pytest.raises(FixFailedError):
                run_fix_agent(ticket, cfg, tmp_path, tmp_path, "a finding", "before")

        mock_invoke.assert_not_called()

    def test_run_fix_agent_allows_same_executor_different_model(self, tmp_path):
        """TICK-652: a single-executor project (no ``pools:`` block) whose
        fix step resolves to a different model than the one recorded for
        review is independent by model, not just by executor identity --
        it must dispatch instead of refusing (mirrors
        resolve_independent_review_driver's "different-model" rung)."""
        from lanegate.orchestrate.autofix import run_fix_agent

        ticket = {
            "id": "TICK-004",
            "title": "Refactor",
            "close_criteria": "Passes.",
            "review_driver": "aider",
            "review_model": "qwen-27b",
            "model": "qwen-30b-coder",
        }
        cfg = {"executor": "aider"}

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/x.py"),
            patch("lanegate.reviewer.build_fix_prompt", return_value="fix prompt"),
            patch("lanegate.orchestrate.loop.resolve_pool_executor", return_value="aider"),
            patch(
                "lanegate.orchestrate.autofix.invoke_executor", return_value=(0, "", "")
            ) as mock_invoke,
            patch("lanegate.orchestrate.autofix.commit_worktree_changes", return_value=True),
            patch("lanegate.orchestrate.autofix._git_head_sha", return_value="sha-after"),
        ):
            result = run_fix_agent(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        assert result is True
        assert mock_invoke.call_args.kwargs["executor_override"] == "aider"


class TestResolveIndependentFixDriver:
    """TICK-652: resolve_independent_fix_driver is the fix-side counterpart
    of review.resolve_independent_review_driver."""

    def test_different_pool_instance_is_independent(self, tmp_path):
        from lanegate.orchestrate.autofix import resolve_independent_fix_driver

        ticket = {"id": "TICK-100", "review_driver": "claude-b"}
        cfg = _default_cfg(tmp_path)
        cfg["default_pool"] = "pool1"
        cfg["pools"] = {"pool1": {"executors": ["claude-a", "claude-b"]}}

        driver, independence = resolve_independent_fix_driver(ticket, cfg, tmp_path)

        assert (driver, independence) == ("claude-a", "independent")

    def test_same_executor_different_model_is_different_model(self, tmp_path):
        from lanegate.orchestrate.autofix import resolve_independent_fix_driver

        ticket = {"id": "TICK-004", "review_driver": "aider", "review_model": "qwen-27b", "model": "qwen-30b-coder"}
        cfg = {"executor": "aider"}

        with patch("lanegate.orchestrate.loop.resolve_pool_executor", return_value="aider"):
            driver, independence = resolve_independent_fix_driver(ticket, cfg, tmp_path)

        assert (driver, independence) == ("aider", "different-model")

    def test_same_executor_same_model_needs_review(self, tmp_path):
        from lanegate.orchestrate.autofix import resolve_independent_fix_driver

        ticket = {"id": "TICK-050", "review_driver": "codex"}
        cfg = {"executor": "codex"}

        with patch("lanegate.orchestrate.loop.resolve_pool_executor", return_value="codex"):
            driver, independence = resolve_independent_fix_driver(ticket, cfg, tmp_path)

        assert (driver, independence) == (None, "needs_review")

    def test_driver_alias_model_override_is_not_different_model(self, tmp_path):
        """A `drivers:` alias with its own `model:` forces that model for
        every step (resolve_dispatch, pool.py) -- so it must be compared
        against the *driver's* forced model, not the base cfg's own
        per-step default, or a same-model self-fix slips through as
        "different-model"."""
        from lanegate.orchestrate.autofix import resolve_independent_fix_driver

        ticket = {"id": "TICK-004", "review_driver": "reviewer-x", "review_model": "gpt-4o"}
        cfg = {
            "executor": "codex",
            "drivers": {"reviewer-x": {"type": "codex", "model": "gpt-4o"}},
            "models": {"fix": "gpt-4-turbo"},
        }

        with patch("lanegate.orchestrate.loop.resolve_pool_executor", return_value="reviewer-x"):
            driver, independence = resolve_independent_fix_driver(ticket, cfg, tmp_path)

        assert (driver, independence) == (None, "needs_review")


class TestRunAutoFixCycleDriftStructuredResult:
    """TICK-348: the drift verdict is a structured, machine-readable
    frontmatter field (`drift_check_result`), not only prose."""

    def _ticket(self, tmp_path: Path) -> dict:
        tickets_dir = tmp_path / "tickets"
        tickets_dir.mkdir(exist_ok=True)
        path = _write_ticket(
            tickets_dir,
            "TICK-050",
            "code_complete",
            findings="Reviewer requested: fix the off-by-one in foo.py.",
        )
        from lanegate.ticket import parse_ticket

        return parse_ticket(path)

    def test_drift_failure_records_structured_result(self, tmp_path):
        from lanegate.reviewer import DriftCheckResult

        ticket = self._ticket(tmp_path)
        cfg = _default_cfg(tmp_path)
        cfg["max_auto_fix_attempts"] = 1

        with (
            patch("lanegate.orchestrate.autofix._git_head_sha", return_value="abc123"),
            patch("lanegate.orchestrate.autofix.run_fix_agent", return_value=True),
            patch(
                "lanegate.orchestrate.autofix.run_drift_check",
                return_value=DriftCheckResult(ok=False, reason="touched unrelated file"),
            ),
            patch("lanegate.orchestrate.autofix.run_review_agent") as mock_review,
        ):
            result = run_auto_fix_cycle(ticket, cfg, tmp_path, tmp_path / "worktrees" / "tick-050")

        assert result is False
        mock_review.assert_not_called()

        from lanegate.ticket import parse_ticket

        updated = parse_ticket(tmp_path / "tickets" / "TICK-050.md")
        assert updated["drift_check_result"] == {"ok": False, "reason": "touched unrelated file"}
        assert updated["status"] == "code_complete"

    def test_drift_success_records_structured_result_on_approval(self, tmp_path):
        from lanegate.reviewer import DriftCheckResult

        ticket = self._ticket(tmp_path)
        cfg = _default_cfg(tmp_path)
        cfg["max_auto_fix_attempts"] = 1

        with (
            patch("lanegate.orchestrate.autofix._git_head_sha", return_value="abc123"),
            patch("lanegate.orchestrate.autofix.run_fix_agent", return_value=True),
            patch(
                "lanegate.orchestrate.autofix.run_drift_check",
                return_value=DriftCheckResult(ok=True, reason="in scope"),
            ),
            patch("lanegate.orchestrate.autofix.run_review_agent", return_value=True),
        ):
            result = run_auto_fix_cycle(ticket, cfg, tmp_path, tmp_path / "worktrees" / "tick-050")

        assert result is True

        from lanegate.ticket import parse_ticket

        updated = parse_ticket(tmp_path / "tickets" / "TICK-050.md")
        assert updated["drift_check_result"] == {"ok": True, "reason": "in scope"}


class TestCmdFix:
    """TICK-348: `lanegate fix TICK-X` — the out-of-band entry point for the
    same fix -> drift-check -> re-review cycle the loop runs inline."""

    def _write_changes_requested_ticket(self, tickets_dir: Path, worktree: Path) -> Path:
        tickets_dir.mkdir(exist_ok=True)
        content = (
            "---\n"
            "id: TICK-050\n"
            "title: Test TICK-050\n"
            "status: code_complete\n"
            "review_verdict: changes_requested\n"
            f"worktree: {worktree}\n"
            "close_criteria: All tests pass.\n"
            "---\nBody.\n"
        )
        path = tickets_dir / "TICK-050.md"
        path.write_text(content)
        return path

    def test_cmd_fix_refuses_wrong_state(self, tmp_path, capsys):
        from lanegate.orchestrate.autofix import cmd_fix

        tickets_dir = tmp_path / "tickets"
        tickets_dir.mkdir(exist_ok=True)
        _write_ticket(tickets_dir, "TICK-060", "open")
        cfg = _default_cfg(tmp_path)

        with pytest.raises(SystemExit) as exc_info:
            cmd_fix("TICK-060", cfg, tmp_path)

        assert exc_info.value.code == 1
        assert "open" in capsys.readouterr().err

    def test_cmd_fix_missing_ticket_exits(self, tmp_path):
        from lanegate.orchestrate.autofix import cmd_fix

        cfg = _default_cfg(tmp_path)

        with pytest.raises(SystemExit) as exc_info:
            cmd_fix("TICK-999", cfg, tmp_path)

        assert exc_info.value.code == 1

    def test_cmd_fix_missing_worktree_exits(self, tmp_path):
        from lanegate.orchestrate.autofix import cmd_fix

        tickets_dir = tmp_path / "tickets"
        self._write_changes_requested_ticket(tickets_dir, tmp_path / "worktrees" / "does-not-exist")
        cfg = _default_cfg(tmp_path)

        with pytest.raises(SystemExit) as exc_info:
            cmd_fix("TICK-050", cfg, tmp_path)

        assert exc_info.value.code == 1

    def test_fix_dispatches_after_durable_changes_requested(self, tmp_path):
        """A review action with --verdict changes_requested records verdict and findings
        durably to ticket frontmatter/body despite exiting nonzero (exit code 1).
        A subsequent lanegate fix sees the persisted verdict/findings and dispatches
        run_auto_fix_cycle without refusing for missing review_verdict."""
        from lanegate.lifecycle import cmd_review
        from lanegate.orchestrate.autofix import cmd_fix
        from lanegate.ticket import parse_ticket

        worktree = tmp_path / "worktrees" / "tick-050"
        worktree.mkdir(parents=True)
        tickets_dir = tmp_path / "tickets"
        tickets_dir.mkdir(exist_ok=True)
        content = (
            "---\n"
            "id: TICK-050\n"
            "title: Test TICK-050\n"
            "status: code_complete\n"
            f"worktree: {worktree}\n"
            "close_criteria: All tests pass.\n"
            "---\nBody.\n"
        )
        ticket_file = tickets_dir / "TICK-050.md"
        ticket_file.write_text(content)

        cfg = _default_cfg(tmp_path)
        cfg["tickets_dir"] = "tickets"

        with pytest.raises(SystemExit) as exc_info:
            cmd_review(
                "TICK-050",
                cfg,
                tmp_path,
                verdict="changes_requested",
                findings="Fix off-by-one error in helper.py",
            )
        assert exc_info.value.code == 1

        reloaded = parse_ticket(ticket_file)
        assert reloaded["status"] == "code_complete"
        assert reloaded["review_verdict"] == "changes_requested"
        assert any("Fix off-by-one error" in f for f in reloaded.get("review_findings", []))

        with patch("lanegate.orchestrate.autofix.run_auto_fix_cycle", return_value=True) as mock_auto_fix:
            cmd_fix("TICK-050", cfg, tmp_path)

        assert mock_auto_fix.call_count == 1

    def test_cmd_fix_dispatches_run_fix_agent_with_excluded_reviewer(self, tmp_path):
        """`lanegate fix` must not be a way to skip the reviewer-exclusion rule."""
        from lanegate.orchestrate.autofix import cmd_fix

        worktree = tmp_path / "worktrees" / "tick-050"
        worktree.mkdir(parents=True)
        tickets_dir = tmp_path / "tickets"
        tickets_dir.mkdir(exist_ok=True)
        content = (
            "---\n"
            "id: TICK-050\n"
            "title: Test TICK-050\n"
            "status: code_complete\n"
            "review_verdict: changes_requested\n"
            f"worktree: {worktree}\n"
            "review_driver: claude-b\n"
            "close_criteria: All tests pass.\n"
            "---\nBody.\n"
            "## Review Findings\nReviewer requested: fix the off-by-one in foo.py.\n"
        )
        (tickets_dir / "TICK-050.md").write_text(content)
        cfg = _default_cfg(tmp_path)
        cfg["default_pool"] = "pool1"
        cfg["pools"] = {"pool1": {"executors": ["claude-a", "claude-b"]}}
        cfg["max_auto_fix_attempts"] = 1

        from lanegate.reviewer import DriftCheckResult

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/x.py"),
            patch("lanegate.reviewer.build_fix_prompt", return_value="fix prompt"),
            patch(
                "lanegate.orchestrate.autofix.invoke_executor", return_value=(0, "", "")
            ) as mock_invoke,
            patch("lanegate.orchestrate.autofix.commit_worktree_changes", return_value=True),
            patch("lanegate.orchestrate.autofix._git_head_sha", side_effect=["sha-before", "sha-after"]),
            patch(
                "lanegate.orchestrate.autofix.run_drift_check",
                return_value=DriftCheckResult(ok=True, reason="in scope"),
            ),
            patch("lanegate.orchestrate.autofix.run_review_agent", return_value=True),
        ):
            cmd_fix("TICK-050", cfg, tmp_path)

        assert mock_invoke.call_args.kwargs["executor_override"] == "claude-a"

        from lanegate.ticket import parse_ticket

        updated = parse_ticket(tickets_dir / "TICK-050.md")
        assert updated["drift_check_result"] == {"ok": True, "reason": "in scope"}

    def test_cmd_fix_matches_in_loop_cycle(self, tmp_path):
        """`lanegate fix` must produce the same ticket fields/body sections as the
        in-loop cycle for the same fixture, given the same mocked agents."""
        from lanegate.orchestrate.autofix import cmd_fix, run_auto_fix_cycle
        from lanegate.reviewer import DriftCheckResult
        from lanegate.ticket import parse_ticket

        def _make_fixture(tickets_dir: Path, worktree: Path) -> None:
            tickets_dir.mkdir(parents=True, exist_ok=True)
            worktree.mkdir(parents=True)
            content = (
                "---\n"
                "id: TICK-050\n"
                "title: Test TICK-050\n"
                "status: code_complete\n"
                "review_verdict: changes_requested\n"
                f"worktree: {worktree}\n"
                "close_criteria: All tests pass.\n"
                "---\nBody.\n"
            )
            (tickets_dir / "TICK-050.md").write_text(content)

        cfg = _default_cfg(tmp_path)
        cfg["max_auto_fix_attempts"] = 1
        agents = (
            patch("lanegate.orchestrate.autofix.run_fix_agent", return_value=True),
            patch(
                "lanegate.orchestrate.autofix.run_drift_check",
                return_value=DriftCheckResult(ok=True, reason="in scope"),
            ),
            patch("lanegate.orchestrate.autofix.run_review_agent", return_value=True),
            patch("lanegate.orchestrate.autofix._git_head_sha", return_value="abc123"),
        )

        loop_root = tmp_path / "loop"
        loop_tickets_dir = loop_root / "tickets"
        loop_worktree = loop_root / "worktrees" / "tick-050"
        _make_fixture(loop_tickets_dir, loop_worktree)
        loop_cfg = dict(cfg, tickets_dir=str(loop_tickets_dir))
        loop_ticket = parse_ticket(loop_tickets_dir / "TICK-050.md")
        with agents[0], agents[1], agents[2], agents[3]:
            run_auto_fix_cycle(loop_ticket, loop_cfg, loop_root, loop_worktree)

        fix_root = tmp_path / "fix"
        fix_tickets_dir = fix_root / "tickets"
        fix_worktree = fix_root / "worktrees" / "tick-050"
        _make_fixture(fix_tickets_dir, fix_worktree)
        fix_cfg = dict(cfg, tickets_dir=str(fix_tickets_dir))
        with agents[0], agents[1], agents[2], agents[3]:
            cmd_fix("TICK-050", fix_cfg, fix_root)

        loop_result = parse_ticket(loop_tickets_dir / "TICK-050.md")
        fix_result = parse_ticket(fix_tickets_dir / "TICK-050.md")

        for key in ("status", "review_verdict", "drift_check_result", "auto_fix_attempts"):
            assert loop_result.get(key) == fix_result.get(key), key
        assert loop_result["_body"] == fix_result["_body"]


def test_cmd_fix_records_direct_action_tracking(tmp_path, capsys):
    from lanegate.orchestrate.autofix import cmd_fix

    cfg = _default_cfg(tmp_path)
    with pytest.raises(SystemExit):
        cmd_fix("TICK-404", cfg, tmp_path)

    assert "Action action-" in capsys.readouterr().out
    action_log = next((tmp_path / ".lanegate" / "logs").glob("action-*.events.jsonl"))
    assert '"action_type": "fix"' in action_log.read_text()
    assert '"status": "failed"' in action_log.read_text()


def test_cmd_fix_handles_rate_limited_none_return(tmp_path, capsys):
    from lanegate.orchestrate.autofix import cmd_fix

    cfg = _default_cfg(tmp_path)
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    worktree = tmp_path / "worktrees" / "tick-055"
    worktree.mkdir(parents=True)
    (tickets_dir / "TICK-055.md").write_text(
        "---\n"
        "id: TICK-055\n"
        "title: Test TICK-055\n"
        "status: code_complete\n"
        "review_verdict: changes_requested\n"
        f"worktree: {worktree}\n"
        "---\nBody.\n"
    )
    cfg["tickets_dir"] = str(tickets_dir)

    with patch("lanegate.orchestrate.autofix.run_auto_fix_cycle", return_value=None):
        cmd_fix("TICK-055", cfg, tmp_path)

    err = capsys.readouterr().err
    assert "auto-fix cycle rate-limited / hibernated" in err
    action_log = next((tmp_path / ".lanegate" / "logs").glob("action-*.events.jsonl"))
    assert '"status": "rate_limited"' in action_log.read_text()


def test_abort_if_markers_remain_detects_unresolved_equals_separator(tmp_path):
    from lanegate.orchestrate.autofix import run_rebase_fix_agent
    from lanegate.orchestrate.loop import is_mid_rebase

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)

    (repo / "f.txt").write_text("base\n")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True)

    subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, check=True, capture_output=True)
    (repo / "f.txt").write_text("feature\n")
    subprocess.run(["git", "commit", "-am", "feature commit"], cwd=repo, check=True)

    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
    (repo / "f.txt").write_text("main\n")
    subprocess.run(["git", "commit", "-am", "main commit"], cwd=repo, check=True)

    subprocess.run(["git", "checkout", "feature"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "rebase", "main"], cwd=repo, capture_output=True)
    assert is_mid_rebase(repo)

    def resolve_with_residual_equals(*_args, **_kwargs):
        (repo / "f.txt").write_text("main\n=======\nresidual\n")
        return 0, "", ""

    with (
        patch("lanegate.orchestrate.loop.resolve_pool_executor", return_value="codex"),
        patch("lanegate.orchestrate.autofix.invoke_executor", side_effect=resolve_with_residual_equals),
        patch("lanegate.orchestrate.autofix.commit_worktree_changes"),
        patch("lanegate.orchestrate.run_report.record_direct_action_event"),
    ):
        ok = run_rebase_fix_agent({"id": "TICK-534"}, {}, repo, repo, "source conflict")

    assert ok is False


# ---------------------------------------------------------------------------
