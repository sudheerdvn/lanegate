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

    def test_executor_nonzero_exit_returns_false(self, tmp_path):
        from lanegate.orchestrate import run_fix_agent

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/x.py"),
            patch("lanegate.reviewer.build_fix_prompt", return_value="fix prompt"),
            patch("lanegate.orchestrate.autofix.invoke_executor", return_value=(1, "", "")),
            patch("lanegate.orchestrate.autofix.commit_worktree_changes") as mock_commit,
        ):
            result = run_fix_agent(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        assert result is False
        mock_commit.assert_not_called()

    def test_no_new_commit_returns_false(self, tmp_path):
        """Executor exits 0 but makes no commit — check_worktree_has_commits would
        be trivially True here (main-relative), so this must use a HEAD-sha
        comparison against pre_fix_sha instead."""
        from lanegate.orchestrate import run_fix_agent

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/x.py"),
            patch("lanegate.reviewer.build_fix_prompt", return_value="fix prompt"),
            patch("lanegate.orchestrate.autofix.invoke_executor", return_value=(0, "", "")),
            patch("lanegate.orchestrate.autofix.commit_worktree_changes", return_value=False),
            patch("lanegate.orchestrate.autofix._git_head_sha", return_value="sha-before"),
        ):
            result = run_fix_agent(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        assert result is False

    def test_missing_diff_returns_false(self, tmp_path):
        from lanegate.orchestrate import run_fix_agent
        from lanegate.reviewer import ReviewError

        ticket = self._make_ticket()
        cfg = _default_cfg(tmp_path)

        with patch(
            "lanegate.reviewer.get_worktree_diff", side_effect=ReviewError("no worktree")
        ):
            result = run_fix_agent(ticket, cfg, tmp_path, tmp_path, "a finding", "sha-before")

        assert result is False

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
        cfg = {"executor": "claude", "executor_steps": {"implement": "aider", "review": "aider"}}
        assert _is_combined_mode(cfg, self._ticket()) is True

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
        cfg = {"executor": "aider", "executor_steps": {}}
        ticket = self._ticket(executor="aider")
        # implement→aider (ticket), review→aider (global) → combined
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
        ticket = self._ticket(tmp_path)
        cfg = self._cfg(tmp_path, max_attempts=2)

        with (
            patch("lanegate.orchestrate.autofix._git_head_sha", return_value="abc123"),
            patch("lanegate.orchestrate.autofix.run_fix_agent", return_value=False),
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

    with patch("lanegate.orchestrate.autofix.invoke_executor", return_value=(0, "", "")) as mock_exec, \
         patch("lanegate.orchestrate.loop._conflicted_files", return_value=["a.py"]), \
         patch("lanegate.orchestrate.loop._continue_rebase", return_value=(True, "")) as mock_cont, \
         patch("lanegate.orchestrate.autofix.commit_worktree_changes"):
        ok = run_rebase_fix_agent(ticket, cfg, tmp_path, worktree, "conflict in a.py")

    assert ok is True
    mock_exec.assert_called_once()
    mock_cont.assert_called_once_with(worktree, ["a.py"])


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
        from lanegate.orchestrate.autofix import run_fix_agent

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
            result = run_fix_agent(ticket, cfg, tmp_path, tmp_path, "a finding", "before")

        assert result is False
        mock_invoke.assert_not_called()


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


# ---------------------------------------------------------------------------
