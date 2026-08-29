"""
Tests for lanegate/orchestrate/review.py — review agent driver dispatch, combined mode handling.

Split out of the former monolithic tests/test_orchestrate.py (TICK-316).
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime

from lanegate.config import ConfigError
from lanegate.orchestrate.review import _minimal_cfg, _refresh_ticket_content_from_worktree
from lanegate.reviewer import ReviewError
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

    def _persisted_ticket(self, tmp_path: Path) -> dict:
        """A ticket with a real backing file, so _escalate_harness_error's
        needs_review persistence actually runs instead of short-circuiting on
        the missing _path that the in-memory tickets above deliberately have."""
        tickets_dir = tmp_path / "tickets"
        tickets_dir.mkdir(exist_ok=True)
        path = _write_ticket(tickets_dir, "TICK-997", "code_complete")
        return parse_ticket(path)

    def test_ollama_reviewer_escalates_ticket_to_needs_review(self, tmp_path):
        """Raw ollama has no code-application/review step of its own — it
        must be rejected before any subprocess review dispatch, and the
        ticket must actually land in needs_review the same way other
        fail-closed reviewer misconfigurations leave it."""
        ticket = self._persisted_ticket(tmp_path)
        cfg = {
            "trunk_branch": "main",
            "reviewer": "ollama",
            "commit_status_changes": False,
        }

        with (
            self._patch_diff(),
            patch("lanegate.config.load_config", return_value=cfg),
            patch("lanegate.orchestrate.subprocess.run") as mock_run,
            patch("lanegate.lifecycle.cmd_review") as mock_cmd_review,
        ):
            result = run_review_agent(ticket, tmp_path)

        assert result is False
        assert mock_run.call_count == 1  # only get_commit_messages' git log call
        mock_cmd_review.assert_not_called()
        assert parse_ticket(ticket["_path"])["status"] == "needs_review"

    def test_named_ollama_instance_reviewer_escalates_to_needs_review(self, tmp_path):
        """A named executor instance (TICK-088) backed by ollama must hit the
        same guard. expand_driver() only expands `drivers:` entries, so
        `reviewer: local-ollama` arrives at the guard as the instance name;
        resolving it through get_executor_config() is what makes the guard
        fire instead of dispatching `ollama run ...` and accepting whatever
        verdict-shaped text comes back."""
        ticket = self._persisted_ticket(tmp_path)
        cfg = {
            "trunk_branch": "main",
            "reviewer": "local-ollama",
            "executors": {"local-ollama": {"type": "ollama"}},
            "commit_status_changes": False,
        }

        with (
            self._patch_diff(),
            patch("lanegate.config.load_config", return_value=cfg),
            patch("lanegate.orchestrate.subprocess.run") as mock_run,
            patch("lanegate.lifecycle.cmd_review") as mock_cmd_review,
        ):
            result = run_review_agent(ticket, tmp_path)

        assert result is False
        assert mock_run.call_count == 1  # only get_commit_messages' git log call
        mock_cmd_review.assert_not_called()
        assert parse_ticket(ticket["_path"])["status"] == "needs_review"

    def test_escalate_harness_error_prevents_worktree_frontmatter_overlay(self, tmp_path):
        """Review escalation reloads control checkout frontmatter and prevents
        worktree-authored ticket frontmatter from being overlaid and persisted,
        while clearing prior substantive review verdicts."""
        tickets_dir = tmp_path / "tickets"
        tickets_dir.mkdir(parents=True, exist_ok=True)
        control_ticket_path = tickets_dir / "TICK-603.md"
        control_ticket_path.write_text(
            "---\n"
            "id: TICK-603\n"
            "title: Control Checkout Title\n"
            "status: code_complete\n"
            "review_verdict: changes_requested\n"
            "review_summary: Prior substantive rejection summary\n"
            "touches:\n"
            "  - lanegate/orchestrate/review.py\n"
            "---\n"
            "Control body.\n"
        )
        ticket = parse_ticket(control_ticket_path)

        wt_path = tmp_path / "worktree"
        wt_tickets_dir = wt_path / "tickets"
        wt_tickets_dir.mkdir(parents=True, exist_ok=True)
        wt_ticket_path = wt_tickets_dir / "TICK-603.md"
        wt_ticket_path.write_text(
            "---\n"
            "id: TICK-603\n"
            "title: Modified Worktree Title\n"
            "status: code_complete\n"
            "touches:\n"
            "  - lanegate/orchestrate/review.py\n"
            "  - lanegate/malicious.py\n"
            "---\n"
            "Modified worktree body.\n"
        )

        cfg = {
            "trunk_branch": "main",
            "reviewer": "ollama",
            "commit_status_changes": False,
        }

        with (
            self._patch_diff(),
            patch("lanegate.config.load_config", return_value=cfg),
            patch("lanegate.orchestrate.subprocess.run") as mock_run,
            patch("lanegate.lifecycle.cmd_review") as mock_cmd_review,
        ):
            result = run_review_agent(ticket, tmp_path, worktree_path=wt_path)

        assert result is False
        mock_cmd_review.assert_not_called()

        persisted = parse_ticket(control_ticket_path)
        assert persisted["status"] == "needs_review"
        assert persisted["title"] == "Control Checkout Title"
        assert persisted["touches"] == ["lanegate/orchestrate/review.py"]
        assert "review_verdict" not in persisted
        assert "review_summary" not in persisted



class TestRefreshTicketContentFromWorktree:
    """TICK-551: review must not be dispatched against a stale close_criteria.

    Regression for TICK-545: an implementer/auto-fix commit narrowed
    close_criteria on the ticket's own branch, but review is dispatched with
    the ticket dict loaded from the control checkout, which never saw that
    edit until merge. The reviewer had to spend most of its turns
    reconciling a contradiction between a stale CLOSE CRITERIA field and the
    actual diff/commits before it could trust the diff.
    """

    def _control_ticket(self, repo_root: Path) -> dict:
        tickets_dir = repo_root / "tickets"
        tickets_dir.mkdir(parents=True, exist_ok=True)
        path = tickets_dir / "TICK-551.md"
        path.write_text(
            "---\nid: TICK-551\nstatus: code_complete\n"
            "close_criteria: original stale contract\n"
            "touches: [a.py]\n---\nstale body\n"
        )
        return {
            "id": "TICK-551",
            "status": "code_complete",
            "close_criteria": "original stale contract",
            "touches": ["a.py"],
            "_body": "stale body",
            "_path": path,
        }

    def _write_worktree_copy(self, repo_root: Path, worktree_path: Path, content: str) -> None:
        wt_tickets_dir = worktree_path / "tickets"
        wt_tickets_dir.mkdir(parents=True, exist_ok=True)
        (wt_tickets_dir / "TICK-551.md").write_text(content)

    def test_overlays_close_criteria_touches_and_body_from_worktree(self, tmp_path):
        repo_root = tmp_path / "repo"
        worktree_path = tmp_path / "worktree"
        ticket = self._control_ticket(repo_root)
        self._write_worktree_copy(
            repo_root,
            worktree_path,
            "---\nid: TICK-551\nstatus: code_complete\n"
            "close_criteria: current narrowed contract\n"
            "touches: [a.py, b.py]\n---\ncurrent body\n",
        )

        _refresh_ticket_content_from_worktree(ticket, repo_root, worktree_path)

        assert ticket["close_criteria"] == "current narrowed contract"
        assert ticket["touches"] == ["a.py", "b.py"]
        assert ticket["_body"] == "current body"
        # Lifecycle-authoritative fields are untouched by this overlay.
        assert ticket["status"] == "code_complete"

    def test_noop_when_worktree_missing(self, tmp_path):
        repo_root = tmp_path / "repo"
        worktree_path = tmp_path / "does-not-exist"
        ticket = self._control_ticket(repo_root)

        _refresh_ticket_content_from_worktree(ticket, repo_root, worktree_path)

        assert ticket["close_criteria"] == "original stale contract"

    def test_noop_when_worktree_ticket_copy_missing(self, tmp_path):
        repo_root = tmp_path / "repo"
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        ticket = self._control_ticket(repo_root)

        _refresh_ticket_content_from_worktree(ticket, repo_root, worktree_path)

        assert ticket["close_criteria"] == "original stale contract"

    def test_noop_when_worktree_ticket_copy_unparseable(self, tmp_path):
        repo_root = tmp_path / "repo"
        worktree_path = tmp_path / "worktree"
        ticket = self._control_ticket(repo_root)
        self._write_worktree_copy(repo_root, worktree_path, "not a valid ticket file at all")

        _refresh_ticket_content_from_worktree(ticket, repo_root, worktree_path)

        assert ticket["close_criteria"] == "original stale contract"

    def test_run_review_agent_refreshes_ticket_before_building_prompt(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        ticket = {
            "id": "TICK-551",
            "title": "t",
            "close_criteria": "stale",
            "_body": "",
            "worktree": str(tmp_path / "worktree"),
        }
        calls = []
        monkeypatch.setattr(
            "lanegate.orchestrate.review._refresh_ticket_content_from_worktree",
            lambda t, root, wt: calls.append((t["id"], root, wt)),
        )
        with (
            patch(
                "lanegate.reviewer.get_worktree_diff",
                side_effect=ReviewError("no worktree — stop before any real dispatch"),
            ),
        ):
            run_review_agent(ticket, repo_root)

        assert calls == [("TICK-551", repo_root, Path(tmp_path / "worktree"))]


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
                    "model": "claude-review-driver-model",
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
        assert review_cmd[review_cmd.index("--model") + 1] == "claude-review-driver-model"
        assert review_kwargs["env"]["REVIEW_TOKEN"] == "review-token"
        assert mock_cmd_review.call_args.kwargs["verdict"] == "approved"

    def test_review_dispatch_delegates_main_checkout_leak_check(self, tmp_path):
        ticket = self._make_ticket()
        cfg = {"executor": "claude-process"}

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch(
                "lanegate.orchestrate.review._stream_subprocess",
                return_value=(0, json.dumps({"verdict": "approved", "notes": "ok"}), "", None),
            ),
            patch("lanegate.orchestrate.review._main_checkout_leak_diff", return_value="") as leak_diff,
            patch("lanegate.lifecycle.cmd_review"),
        ):
            assert run_review_agent(ticket, tmp_path, cfg=cfg) is True

        before, after, passed_cfg, passed_root = leak_diff.call_args.args
        assert before == after == ""
        assert passed_cfg is cfg
        assert passed_root == tmp_path

    def test_review_dispatch_passes_read_only(self, tmp_path):
        ticket = self._make_ticket()
        cfg = {"executor": "claude-process"}
        calls = []
        build_cmd_calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if cmd[:2] == ["git", "log"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps({"verdict": "approved", "notes": "ok"}),
                stderr="",
            )

        def spy_build_executor_cmd(*args, **kwargs):
            build_cmd_calls.append(kwargs)
            return _build_executor_cmd(*args, **kwargs)

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.orchestrate.subprocess.run", side_effect=fake_run),
            patch("lanegate.lifecycle.cmd_review"),
            patch(
                "lanegate.orchestrate.review.build_executor_cmd",
                side_effect=spy_build_executor_cmd,
            ),
        ):
            assert run_review_agent(ticket, tmp_path, cfg=cfg) is True

        # The disallowedTools flag alone doesn't prove read_only=True was passed --
        # an explicit disallowed_tools list with read_only=False produces the same
        # flag. Assert the actual kwarg directly.
        assert any(call.get("read_only") is True for call in build_cmd_calls)

        review_cmd = next(cmd for cmd in calls if cmd[0] != "git")
        assert "--disallowedTools" in review_cmd
        assert review_cmd[review_cmd.index("--disallowedTools") + 1] == "Bash,Write,Edit"

    def test_review_succeeds_when_sibling_status_changes_during_dispatch(self, tmp_path):
        """TICK-688: sibling lifecycle bookkeeping must not discard a verdict."""
        ticket = self._make_ticket()
        cfg = {"executor": "claude-process"}
        statuses = iter(["", " M .lanegate/tickets/TICK-999.md\n"])
        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if cmd == ["git", "status", "--porcelain", "-uno"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=next(statuses), stderr="")
            return real_run(cmd, *args, **kwargs)

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch(
                "lanegate.orchestrate.review._stream_subprocess",
                return_value=(0, json.dumps({"verdict": "approved", "notes": "ok"}), "", None),
            ),
            patch("lanegate.orchestrate.review.subprocess.run", side_effect=fake_run),
            patch("lanegate.lifecycle.cmd_review") as mock_cmd_review,
        ):
            assert run_review_agent(ticket, tmp_path, cfg=cfg) is True

        assert mock_cmd_review.call_args.kwargs["verdict"] == "approved"

    def test_review_dispatch_uses_validated_worktree_model_but_root_policy(self, tmp_path):
        from lanegate.config import load_config, load_worktree_config

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".lanegate.yml").write_text(
            "executor: agy\n"
            "review_fallback: different_model\n"
            "models:\n"
            "  review: gemini-3.1-pro-high\n"
        )
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        wt_lanegate = worktree_path / "lanegate"
        wt_lanegate.mkdir()
        source_lanegate = Path(__file__).parents[2] / "lanegate"
        shutil.copy2(source_lanegate / "__init__.py", wt_lanegate / "__init__.py")
        shutil.copy2(source_lanegate / "config.py", wt_lanegate / "config.py")
        shutil.copy2(source_lanegate / "ticket.py", wt_lanegate / "ticket.py")
        with (wt_lanegate / "config.py").open("a") as config_file:
            config_file.write(
                "\nfrom pathlib import Path\n"
                "Path('worktree-validator-executed').write_text('unsafe')\n"
                "_KNOWN_AGY_MODELS.add('custom-worktree-model-99')\n"
            )

        (worktree_path / ".lanegate.yml").write_text(
            "executor: agy\n"
            "review_fallback: same_model\n"
            "acceptance_contract_mode: advisory\n"
            "project_guidance:\n"
            "  files: [UNTRUSTED.md]\n"
            "models:\n"
            "  review: custom-worktree-model-99\n"
        )

        with pytest.raises(ConfigError) as exc_info:
            load_config(worktree_path)
        assert "custom-worktree-model-99" in str(exc_info.value)
        assert load_worktree_config(worktree_path)["models"]["review"] == "custom-worktree-model-99"
        assert not (worktree_path / "worktree-validator-executed").exists()

        ticket = self._make_ticket()
        ticket["implement_session_executor"] = "codex"
        review_commands = []
        root_cfg = load_config(repo_root)

        def fake_stream(cmd, **kwargs):
            review_commands.append(list(cmd))
            return 0, json.dumps({"verdict": "approved", "notes": "ok"}), "", None

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.reviewer.build_review_prompt", return_value="trusted prompt") as build_prompt,
            patch(
                "lanegate.orchestrate.review.resolve_independent_review_driver",
                return_value=("agy", "independent"),
            ) as resolve_reviewer,
            patch("lanegate.orchestrate.review._stream_subprocess", side_effect=fake_stream),
            patch("lanegate.lifecycle.cmd_review") as mock_cmd_review,
        ):
            result = run_review_agent(
                ticket,
                repo_root,
                worktree_path=worktree_path,
                cfg=root_cfg,
            )

        assert result is True
        # The statically validated worktree model preserves self-hosting
        # compatibility, but policy and prompt inputs stay at the control root.
        selection_cfg = resolve_reviewer.call_args.args[1]
        assert selection_cfg["models"]["review"] == "gemini-3.1-pro-high"
        assert selection_cfg["review_fallback"] == "different_model"
        assert selection_cfg.get("acceptance_contract_mode") != "advisory"
        assert review_commands[0][review_commands[0].index("--model") + 1] == "custom-worktree-model-99"
        assert build_prompt.call_args.kwargs["cfg"] == root_cfg
        assert mock_cmd_review.call_args.kwargs["verdict"] == "approved"

    def test_worktree_reviewer_cannot_transplant_model_onto_root_driver(self):
        from lanegate.orchestrate.review import _review_dispatch_config

        root_cfg = {
            "executor": "agy",
            "models": {"review": "gemini-3.1-pro-high"},
        }
        worktree_cfg = {
            "executor": "agy",
            "reviewer": "codex",
            "models": {"review": "gpt-5.6-sol"},
        }

        dispatch_cfg = _review_dispatch_config(root_cfg, worktree_cfg, "agy", "agy")

        assert dispatch_cfg["models"]["review"] == "gemini-3.1-pro-high"
        assert dispatch_cfg.get("reviewer") is None

    def test_worktree_naming_alias_type_cannot_bind_a_named_driver_alias(self):
        """Finding [2]: a `steps.review.driver` alias (e.g. `trusted-codex`)
        expands to a bare type (`codex`) for compatibility checks, but the
        worktree must name the *alias itself*, not merely that type, to
        receive the model overlay -- or any worktree naming a same-typed
        `reviewer: codex` could ride through an unrelated aliased route."""
        from lanegate.orchestrate.review import _review_dispatch_config

        control_cfg = {
            "executor": "claude-impl",
            "drivers": {"trusted-codex": {"type": "codex"}},
            "models": {"review": "control-model"},
        }

        # Naming only the resolved type, not the alias, must not bind.
        worktree_cfg_bare_type = {
            "reviewer": "codex",
            "models": {"review": "worktree-bare-type-model"},
        }
        dispatch_cfg = _review_dispatch_config(
            control_cfg, worktree_cfg_bare_type, "trusted-codex", "codex"
        )
        assert dispatch_cfg["models"]["review"] == "control-model"

        # Naming the exact alias through the real modern step route, with a
        # matching declared type, does bind.
        worktree_cfg_named_alias = {
            "steps": {"review": {"driver": "trusted-codex"}},
            "drivers": {"trusted-codex": {"type": "codex"}},
            "models": {"review": "worktree-alias-model"},
        }
        dispatch_cfg = _review_dispatch_config(
            control_cfg, worktree_cfg_named_alias, "trusted-codex", "codex"
        )
        assert dispatch_cfg["models"]["review"] == "worktree-alias-model"

    def test_worktree_model_overlay_binds_to_selected_mixed_pool_executor(self, tmp_path):
        """An Agy-only worktree model cannot be sent to Codex when a trusted
        mixed pool selects the Codex sibling for independent review."""
        ticket = self._make_ticket()
        ticket["implement_session_executor"] = "agy-impl"
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        (worktree_path / ".lanegate.yml").write_text("# validated separately\n")
        control_cfg = {
            "executor": "agy",
            "models": {"review": "gpt-5.6-sol"},
            "executors": {
                "agy-impl": {"type": "agy"},
                "codex-review": {"type": "codex"},
            },
            "pools": {"default": {"executors": ["agy-impl", "codex-review"]}},
            "default_pool": "default",
            "review_fallback": "different_model",
        }
        worktree_cfg = {
            "executor": "agy",
            "models": {"review": "gemini-3.1-pro-high"},
            "executors": {
                "agy-impl": {"type": "agy"},
                "codex-review": {"type": "codex"},
            },
        }
        dispatched = []

        def fake_stream(cmd, **kwargs):
            dispatched.append(list(cmd))
            return 0, json.dumps({"verdict": "approved", "notes": "ok"}), "", None

        with (
            patch("lanegate.config.load_worktree_config", return_value=worktree_cfg),
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.orchestrate.review._stream_subprocess", side_effect=fake_stream),
            patch("lanegate.lifecycle.cmd_review") as mock_cmd_review,
        ):
            assert run_review_agent(
                ticket, repo_root, worktree_path=worktree_path, cfg=control_cfg
            ) is True

        assert dispatched[0][0] == "codex"
        assert dispatched[0][dispatched[0].index("--model") + 1] == "gpt-5.6-sol"
        assert mock_cmd_review.call_args.kwargs["verdict"] == "approved"

    def test_worktree_codex_model_cannot_reach_trusted_claude_review_route(self, tmp_path):
        """A worktree-declared Codex model must never dispatch on a trusted
        Claude review route, even when it relabels the same instance name."""
        ticket = self._make_ticket()
        ticket["implement_session_executor"] = "claude-impl"
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        (worktree_path / ".lanegate.yml").write_text("# validated separately\n")
        control_cfg = {
            "executor": "claude-impl",
            "models": {"review": "claude-opus-5"},
            "executors": {
                "claude-impl": {"type": "claude-process"},
                "codex-review": {"type": "claude-process"},
            },
            "pools": {"default": {"executors": ["claude-impl", "codex-review"]}},
            "default_pool": "default",
            "review_fallback": "different_model",
        }
        worktree_cfg = {
            "executor": "claude-impl",
            "models": {"review": "gpt-5.6-sol"},
            "executors": {
                "claude-impl": {"type": "claude-process"},
                "codex-review": {"type": "codex", "models": {"review": "gpt-5.6-sol"}},
            },
        }
        dispatched = []

        def fake_stream(cmd, **kwargs):
            dispatched.append(list(cmd))
            return 0, json.dumps({"verdict": "approved", "notes": "ok"}), "", None

        with (
            patch("lanegate.config.load_worktree_config", return_value=worktree_cfg),
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.orchestrate.review._stream_subprocess", side_effect=fake_stream),
            patch("lanegate.lifecycle.cmd_review") as mock_cmd_review,
        ):
            assert run_review_agent(
                ticket, repo_root, worktree_path=worktree_path, cfg=control_cfg
            ) is True

        assert dispatched[0][0] == "claude"
        assert dispatched[0][dispatched[0].index("--model") + 1] == "claude-opus-5"
        assert mock_cmd_review.call_args.kwargs["verdict"] == "approved"

    def test_worktree_claude_model_cannot_reach_trusted_codex_review_route(self, tmp_path):
        """The reverse of the above: a worktree-declared Claude model must
        never dispatch on a trusted Codex review route."""
        ticket = self._make_ticket()
        ticket["implement_session_executor"] = "codex-impl"
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        (worktree_path / ".lanegate.yml").write_text("# validated separately\n")
        control_cfg = {
            "executor": "codex-impl",
            "models": {"review": "gpt-5.6-sol"},
            "executors": {
                "codex-impl": {"type": "codex"},
                "claude-review": {"type": "codex"},
            },
            "pools": {"default": {"executors": ["codex-impl", "claude-review"]}},
            "default_pool": "default",
            "review_fallback": "different_model",
        }
        worktree_cfg = {
            "executor": "codex-impl",
            "models": {"review": "claude-opus-5"},
            "executors": {
                "codex-impl": {"type": "codex"},
                "claude-review": {"type": "claude-process", "models": {"review": "claude-opus-5"}},
            },
        }
        dispatched = []

        def fake_stream(cmd, **kwargs):
            dispatched.append(list(cmd))
            return 0, json.dumps({"verdict": "approved", "notes": "ok"}), "", None

        with (
            patch("lanegate.config.load_worktree_config", return_value=worktree_cfg),
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.orchestrate.review._stream_subprocess", side_effect=fake_stream),
            patch("lanegate.lifecycle.cmd_review") as mock_cmd_review,
        ):
            assert run_review_agent(
                ticket, repo_root, worktree_path=worktree_path, cfg=control_cfg
            ) is True

        assert dispatched[0][0] == "codex"
        assert dispatched[0][dispatched[0].index("--model") + 1] == "gpt-5.6-sol"
        assert mock_cmd_review.call_args.kwargs["verdict"] == "approved"

    def test_review_dispatch_without_worktree_config_uses_root_config_and_custom_path(self, tmp_path):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".lanegate.yml").write_text(
            "worktrees_dir: custom-worktrees\n"
            "executor: agy\n"
            "models:\n"
            "  review: gemini-3.1-pro-high\n"
        )
        worktree_path = repo_root / "custom-worktrees" / "tick-998"
        worktree_path.mkdir(parents=True)
        review_commands = []

        def fake_stream(cmd, **kwargs):
            review_commands.append(list(cmd))
            return 0, json.dumps({"verdict": "approved", "notes": "ok"}), "", None

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.orchestrate.review._stream_subprocess", side_effect=fake_stream),
            patch("lanegate.lifecycle.cmd_review") as mock_cmd_review,
        ):
            result = run_review_agent(self._make_ticket(), repo_root)

        assert result is True
        assert review_commands[0][review_commands[0].index("--model") + 1] == "gemini-3.1-pro-high"
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

    def test_sibling_retry_does_not_carry_worktree_model_overlay_across_executor_types(
        self, tmp_path
    ):
        """Finding [1]: a rate-limited Agy route failing over to a Codex pool
        sibling must recompute the worktree model overlay for the new
        driver, not reuse the Agy-scoped overlay built for the first pick."""
        ticket = self._make_ticket()
        ticket["implement_session_executor"] = "claude-impl"
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        (worktree_path / ".lanegate.yml").write_text("# validated separately\n")
        control_cfg = {
            "executor": "claude-impl",
            "models": {"review": "codex-control-default-model"},
            "executors": {
                "claude-impl": {"type": "claude-process"},
                "agy-review": {"type": "agy"},
                "codex-review": {"type": "codex"},
            },
            "pools": {
                "default": {"executors": ["claude-impl", "agy-review", "codex-review"]}
            },
            "default_pool": "default",
            "review_fallback": "different_model",
        }
        # Bound only to agy-review by name and type -- must not leak onto
        # codex-review once the pool fails over past its rate limit.
        worktree_cfg = {
            "reviewer": "agy-review",
            "executors": {"agy-review": {"type": "agy"}},
            "models": {"review": "claude-worktree-agy-model"},
        }
        dispatched = []

        def fake_stream(cmd, **kwargs):
            dispatched.append(list(cmd))
            if cmd[0] == "agy":
                return 1, "", "rate limit exceeded", None
            return 0, json.dumps({"verdict": "approved", "notes": "ok"}), "", None

        with (
            patch("lanegate.config.load_worktree_config", return_value=worktree_cfg),
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.orchestrate.review._stream_subprocess", side_effect=fake_stream),
            patch("lanegate.lifecycle.cmd_review") as mock_cmd_review,
        ):
            assert run_review_agent(
                ticket, repo_root, worktree_path=worktree_path, cfg=control_cfg
            ) is True

        assert [cmd[0] for cmd in dispatched] == ["agy", "codex"]
        # First attempt legitimately used the bound worktree overlay...
        first_cmd = dispatched[0]
        assert first_cmd[first_cmd.index("--model") + 1] == "claude-worktree-agy-model"
        # ...but the failed-over Codex sibling must fall back to the trusted
        # control-checkout default, never the stale Agy-scoped model.
        second_cmd = dispatched[1]
        assert second_cmd[second_cmd.index("--model") + 1] == "codex-control-default-model"
        assert mock_cmd_review.call_args.kwargs["verdict"] == "approved"

    def test_sibling_retry_rebuilds_prompt_when_crossing_tool_capable_boundary(
        self, tmp_path
    ):
        """A rate-limited tool-capable reviewer (codex) failing over to a
        non-tool-capable sibling (aider) must rebuild the review prompt for
        the new resolved type -- otherwise the stale tool-capable "run git
        diff yourself" prompt gets sent to aider, which has no tool-dispatch
        loop and reproduces the TICK-644 dead-end <tool_call> bug on the
        retry path. codex specifically (not a Claude type) stays genuinely
        tool-capable under read_only=True -- its --sandbox read-only flag
        still permits reads, unlike Claude's disallowed_tools which blocks
        Bash entirely."""
        ticket = self._make_ticket()
        cfg = {
            "ticket_prefix": "TICK",
            "tickets_dir": "tickets",
            "executor": "codex-review-1",
            "executors": {
                "codex-review-1": {"type": "codex"},
                "aider-review": {"type": "aider"},
            },
            "pools": {
                "default": {"executors": ["codex-review-1", "aider-review"]}
            },
            "default_pool": "default",
        }
        dispatched: list[tuple[str, str]] = []

        def fake_build_executor_cmd(executor, prompt, cfg_, **kwargs):
            dispatched.append((executor, prompt))
            return [executor]

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["git", "log"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd == ["codex-review-1"]:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="rate limit exceeded")
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"verdict": "approved", "notes": "ok"}), stderr=""
            )

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.orchestrate.review.build_executor_cmd", side_effect=fake_build_executor_cmd),
            patch("lanegate.orchestrate.subprocess.run", side_effect=fake_run),
            patch("lanegate.lifecycle.cmd_review") as mock_cmd_review,
        ):
            result = run_review_agent(ticket, tmp_path, cfg=cfg)

        assert result is True
        assert [executor for executor, _ in dispatched] == ["codex-review-1", "aider-review"]
        first_prompt = dispatched[0][1]
        second_prompt = dispatched[1][1]
        assert "Run `git diff main...HEAD`" in first_prompt
        assert "GIT DIFF" not in first_prompt
        assert "GIT DIFF" in second_prompt
        assert "diff --git a/foo.py" in second_prompt
        assert "Run `git diff main...HEAD`" not in second_prompt
        assert mock_cmd_review.call_args.kwargs["verdict"] == "approved"

    def test_initial_review_skips_known_cooling_reviewer(self, tmp_path):
        """A recorded Claude A cooldown routes the very first review to B."""
        from lanegate.executor import write_cooldown

        ticket = self._make_ticket()
        ticket["executor"] = "claude-a"
        cfg = {
            "ticket_prefix": "TICK", "tickets_dir": "tickets", "executor": "claude-a",
            "executors": {
                "claude-a": {"type": "claude-process"},
                "claude-b": {"type": "claude-process"},
            },
            "pools": {"default": {"executors": ["claude-a", "claude-b"]}},
            "default_pool": "default",
        }
        write_cooldown(tmp_path, "claude-a", "weekly quota", retry_after=3600)
        dispatched = []

        def build(executor, prompt, cfg_, **kwargs):
            dispatched.append(executor)
            return [executor]

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["git", "log"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"verdict": "approved", "notes": "ok"}), stderr=""
            )

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.orchestrate.review.build_executor_cmd", side_effect=build),
            patch("lanegate.orchestrate.subprocess.run", side_effect=fake_run),
            patch("lanegate.lifecycle.cmd_review"),
        ):
            assert run_review_agent(ticket, tmp_path, cfg=cfg) is True
        assert dispatched == ["claude-b"]

    def test_rate_limited_review_hibernates_without_verdict_or_findings(self, tmp_path):
        """No healthy sibling means review-pending, never a false rejection."""
        from lanegate.ticket import parse_ticket

        tickets = tmp_path / "tickets"
        tickets.mkdir()
        path = tickets / "TICK-429.md"
        path.write_text(
            "---\nid: TICK-429\ntitle: Rate limited\nstatus: code_complete\ntouches: [foo.py]\n---\nBody.\n"
        )
        ticket = parse_ticket(path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        cfg = {
            "ticket_prefix": "TICK", "tickets_dir": "tickets", "worktrees_dir": "worktrees",
            "executor": "claude-a", "commit_status_changes": False,
            "executors": {"claude-a": {"type": "claude-process"}},
            "pools": {"default": {"executors": ["claude-a"]}}, "default_pool": "default",
        }

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["git", "log"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                cmd,
                1,
                stdout="",
                stderr="HTTP 429: weekly limit resets Aug 7, 6am (America/Los_Angeles)",
            )

        fake_now = datetime(2026, 8, 7, 0, 0, 0, tzinfo=UTC)
        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.orchestrate.subprocess.run", side_effect=fake_run),
            patch("lanegate.executor._utc_now", return_value=fake_now),
        ):
            assert run_review_agent(ticket, tmp_path, worktree_path=worktree, cfg=cfg) is False

        refreshed = parse_ticket(path)
        assert refreshed["status"] == "hibernated"
        assert refreshed["review_pending"] is True
        assert not refreshed.get("review_verdict")
        assert not refreshed.get("review_findings")
        cooldown = json.loads((tmp_path / ".lanegate" / "executors" / "claude-a.cooldown").read_text())
        assert cooldown["until"].startswith("2026-08-07T13:00:00")

    def test_rate_limited_review_persists_only_clean_summary_and_audits_raw_output(self, tmp_path):
        """Verbose CLI envelopes remain audit evidence, not ticket transcript text."""
        from lanegate.ticket import parse_ticket

        tickets = tmp_path / "tickets"
        tickets.mkdir()
        path = tickets / "TICK-430.md"
        path.write_text(
            "---\nid: TICK-430\ntitle: Verbose rate limit\nstatus: code_complete\ntouches: [foo.py]\n---\nBody.\n"
        )
        ticket = parse_ticket(path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        cfg = {
            "ticket_prefix": "TICK", "tickets_dir": "tickets", "worktrees_dir": "worktrees",
            "executor": "claude-a", "commit_status_changes": False,
            "executors": {"claude-a": {"type": "claude-process"}},
            "pools": {"default": {"executors": ["claude-a"]}}, "default_pool": "default",
        }
        envelope = json.dumps(
            {
                "type": "error",
                "session_id": "session-should-only-appear-in-audit",
                "api_error_status": 429,
                "usage": {"input_tokens": 98765, "output_tokens": 4321, "cost_usd": 12.34},
                "content": [{"type": "text", "text": "x" * 4096}],
                "error": {"type": "rate_limit_error", "message": "Weekly usage limit reached."},
            }
        )

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["git", "log"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout=envelope, stderr="")

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.orchestrate.subprocess.run", side_effect=fake_run),
        ):
            assert run_review_agent(ticket, tmp_path, worktree_path=worktree, cfg=cfg) is False

        persisted = path.read_text()
        assert "## Review Pending" in persisted
        assert "rate_limit_error: Weekly usage limit reached." in persisted
        assert envelope not in persisted
        assert "session-should-only-appear-in-audit" not in persisted
        assert '"usage"' not in persisted
        assert '"content"' not in persisted
        captured = next((tmp_path / ".lanegate" / "executor-runs" / "TICK-430").glob("*/captured-output.txt"))
        assert envelope in captured.read_text()

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

    def test_routed_review_model_pin_wins_over_rereview_escalation(self, tmp_path):
        """TICK-554: an explicit route pin remains authoritative on re-review."""
        ticket = self._make_ticket()
        ticket.update(
            executor="codex-impl",
            review_model_pin="gpt-5.6-sol",
            review_verdict="changes_requested",
        )
        cfg = {
            "ticket_prefix": "TICK",
            "tickets_dir": "tickets",
            "executor": "codex-impl",
            "executors": {
                "codex-impl": {"type": "codex"},
                "codex-review": {"type": "codex"},
            },
            "pools": {"default": {"executors": ["codex-impl", "codex-review"]}},
            "default_pool": "default",
            "models": {"review_escalation": "gpt-5.6-terra"},
        }
        dispatched_models: list[str | None] = []

        def fake_build(executor, prompt, cfg_, **kwargs):
            dispatched_models.append(kwargs.get("model"))
            return [executor]

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.orchestrate.review.build_executor_cmd", side_effect=fake_build),
            patch("lanegate.orchestrate.subprocess.run", side_effect=self._fake_run),
            patch("lanegate.lifecycle.cmd_review"),
        ):
            assert run_review_agent(ticket, tmp_path, cfg=cfg) is True

        assert dispatched_models == ["gpt-5.6-sol"]

    def test_routed_review_model_pin_mismatched_to_ollama_aider_provider_fails_closed(
        self, tmp_path
    ):
        """A stale/leaked claude-* review_model_pin routed onto an
        Ollama-provider aider reviewer must fail closed (needs_review) rather
        than silently dispatching a model that executor can't use -- the
        review-time counterpart of the pool-dispatch implement leak this
        ticket fixes. Provider lives on the `drivers:` entry here, not on an
        `executors:` instance, exercising the same fallback pool.py's
        resolve_dispatch() needed."""
        ticket = self._make_ticket()
        ticket.update(
            executor="codex-impl",
            reviewer="aider-review",
            review_model_pin="claude-sonnet-5",
        )
        cfg = {
            "ticket_prefix": "TICK",
            "tickets_dir": "tickets",
            "executor": "codex-impl",
            "executors": {"codex-impl": {"type": "codex"}},
            "drivers": {"aider-review": {"type": "aider", "provider": "ollama"}},
        }
        dispatched_models: list[str | None] = []

        def fake_build(executor, prompt, cfg_, **kwargs):
            dispatched_models.append(kwargs.get("model"))
            return [executor]

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.orchestrate.review.build_executor_cmd", side_effect=fake_build),
            patch("lanegate.orchestrate.subprocess.run", side_effect=self._fake_run),
            patch("lanegate.lifecycle.cmd_review"),
        ):
            assert run_review_agent(ticket, tmp_path, cfg=cfg) is False

        assert dispatched_models == []

    def test_recorded_implementer_cannot_be_hidden_by_later_executor_pin(self, tmp_path):
        """Review exclusion uses who implemented, never a later route edit."""
        ticket = self._make_ticket()
        ticket.update(
            executor="codex-review",
            implement_session_executor="claude-impl",
        )
        cfg = {
            "ticket_prefix": "TICK",
            "tickets_dir": "tickets",
            "executor": "claude-impl",
            "executors": {
                "claude-impl": {"type": "claude-process"},
                "codex-review": {"type": "codex"},
            },
            "pools": {"default": {"executors": ["claude-impl", "codex-review"]}},
            "default_pool": "default",
            "review_fallback": "needs_review",
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
            assert run_review_agent(ticket, tmp_path, cfg=cfg) is True

        assert dispatched == ["codex-review"]
        assert mock_cmd_review.call_args.kwargs["review_independence"] == "independent"

    def test_incompatible_routed_pin_never_launches_on_pool_selected_reviewer(self, tmp_path):
        """A pool retry/selection cannot send a Claude pin to Codex."""
        ticket = self._make_ticket()
        ticket.update(
            executor="claude-impl",
            review_model_pin="claude-sonnet-5",
        )
        cfg = {
            "ticket_prefix": "TICK",
            "tickets_dir": "tickets",
            "executor": "claude-impl",
            "executors": {
                "claude-impl": {"type": "claude-process"},
                "codex-review": {"type": "codex"},
            },
            "pools": {"default": {"executors": ["claude-impl", "codex-review"]}},
            "default_pool": "default",
            "review_fallback": "needs_review",
        }

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.orchestrate.review._stream_subprocess") as stream,
            patch("lanegate.lifecycle.cmd_review"),
        ):
            assert run_review_agent(ticket, tmp_path, cfg=cfg) is False

        stream.assert_not_called()

    def test_review_attribution_does_not_suppress_rereview_escalation(self, tmp_path):
        """TICK-554: a previous review's model is not an operator pin."""
        ticket = self._make_ticket()
        ticket.update(
            executor="codex-impl",
            review_model="gpt-5.6-terra",
            review_verdict="changes_requested",
        )
        cfg = {
            "ticket_prefix": "TICK",
            "tickets_dir": "tickets",
            "executor": "codex-impl",
            "executors": {
                "codex-impl": {"type": "codex"},
                "codex-review": {"type": "codex"},
            },
            "pools": {"default": {"executors": ["codex-impl", "codex-review"]}},
            "default_pool": "default",
            "models": {"review": "gpt-5.6-terra", "review_escalation": "gpt-5.6-sol"},
        }
        dispatched_models: list[str | None] = []

        def fake_build(executor, prompt, cfg_, **kwargs):
            dispatched_models.append(kwargs.get("model"))
            return [executor]

        with (
            patch("lanegate.reviewer.get_worktree_diff", return_value="diff --git a/foo.py"),
            patch("lanegate.orchestrate.review.build_executor_cmd", side_effect=fake_build),
            patch("lanegate.orchestrate.subprocess.run", side_effect=self._fake_run),
            patch("lanegate.lifecycle.cmd_review"),
        ):
            assert run_review_agent(ticket, tmp_path, cfg=cfg) is True

        assert dispatched_models == ["gpt-5.6-sol"]

    def test_pool_name_override_reaches_review_dispatch(self, tmp_path):
        """An explicit pool_name (as orchestrate --pool passes) must govern
        review dispatch, not just implement -- ticket carries no pool of its
        own, default_pool has claude-a/claude-b, but pool_name="codex-only"
        must still win and select codex, never falling back to default_pool."""
        ticket = self._make_ticket()
        ticket["executor"] = "claude-a"
        cfg = {
            "ticket_prefix": "TICK",
            "tickets_dir": "tickets",
            "executor": "claude-a",
            "executors": {
                "claude-a": {"type": "claude-process"},
                "claude-b": {"type": "claude-process"},
                "codex": {"type": "codex"},
            },
            "pools": {
                "default": {"executors": ["claude-a", "claude-b"]},
                "codex-only": {"executors": ["codex"]},
            },
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
            result = run_review_agent(ticket, tmp_path, cfg=cfg, pool_name="codex-only")

        assert result is True
        assert dispatched == ["codex"]
        assert mock_cmd_review.call_args.kwargs["verdict"] == "approved"

    def test_degrades_to_different_model_when_no_alternative_instance_exists(self, tmp_path):
        """Rung 2 (different-model): a single-account config has no sibling
        instance to hand review to, but a distinct review model is
        configured -- that is a genuinely different reviewer, so it must be
        used and labeled rather than falling straight to self-review."""
        ticket = self._make_ticket()
        ticket["executor"] = "claude-a"
        ticket["review_model_pin"] = "claude-opus-5"
        cfg = {
            "ticket_prefix": "TICK",
            "tickets_dir": "tickets",
            "executor": "claude-a",
            "executors": {"claude-a": {"type": "claude-process"}},
            "models": {"implement": "model-implement", "review": "model-review"},
            "review_fallback": "different_model",
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
        assert mock_cmd_review.call_args.kwargs["review_model"] == "claude-opus-5"

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
            "review_fallback": "same_model",
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

    def test_needs_review_fallback_does_not_select_self(self, tmp_path):
        from lanegate.orchestrate.review import resolve_independent_review_driver

        ticket = self._make_ticket()
        ticket["executor"] = "claude-a"
        cfg = {
            "ticket_prefix": "TICK", "tickets_dir": "tickets", "executor": "claude-a",
            "executors": {"claude-a": {"type": "claude-process"}},
            "review_fallback": "needs_review",
        }
        assert resolve_independent_review_driver(
            ticket, cfg, tmp_path, implementer="claude-a"
        ) == (None, "needs_review")

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

    @pytest.mark.parametrize("pinned", [False, True])
    def test_manual_implementation_yields_undetermined_independence(self, tmp_path, capsys, pinned):
        ticket = self._make_ticket()
        ticket["implement_mode"] = "manual"
        if pinned:
            ticket["reviewer"] = "claude-b"
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
        assert mock_cmd_review.call_args.kwargs["review_independence"] == "undetermined"
        assert "implemented manually with no recorded implementer identity" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# TICK-517: reviewer-cooldown retry/hibernation vs. permanent needs_review
# ---------------------------------------------------------------------------


class TestReviewerCooldownRetry:
    def _persisted_ticket(self, tmp_path: Path) -> dict:
        tickets_dir = tmp_path / "tickets"
        tickets_dir.mkdir(exist_ok=True)
        path = _write_ticket(tickets_dir, "TICK-517", "code_complete")
        return parse_ticket(path)

    def _pool_cfg(self) -> dict:
        return {
            "ticket_prefix": "TICK",
            "tickets_dir": "tickets",
            "executor": "claude-a",
            "commit_status_changes": False,
            "executors": {
                "claude-a": {"type": "claude-process"},
                "claude-b": {"type": "claude-process"},
            },
            "pools": {"default": {"executors": ["claude-a", "claude-b"]}},
            "default_pool": "default",
        }

    def test_reviewer_cooldown_hibernates_with_retry_metadata(self, tmp_path):
        """All pool reviewers cooling down must hibernate with a recorded
        reason, next-retry-time, and attempt count -- distinct from a real
        subprocess rate limit (no dispatch is ever attempted here)."""
        from lanegate.executor import write_cooldown

        ticket = self._persisted_ticket(tmp_path)
        ticket["executor"] = "claude-a"
        cfg = self._pool_cfg()
        write_cooldown(tmp_path, "claude-a", "weekly quota", retry_after=1800)
        write_cooldown(tmp_path, "claude-b", "weekly quota", retry_after=3600)
        earliest_until = json.loads(
            (tmp_path / ".lanegate" / "executors" / "claude-a.cooldown").read_text()
        )["until"]

        result = run_review_agent(ticket, tmp_path, cfg=cfg)

        assert result is False
        refreshed = parse_ticket(ticket["_path"])
        assert refreshed["status"] == "hibernated"
        assert refreshed["review_pending"] is True
        assert "temporarily unavailable" in refreshed["review_pending_reason"]
        # Deliberately distinct from the real rate-limit marker so
        # resume-watch's rate-limit recovery never picks this up.
        assert "rate limit or quota interruption" not in refreshed["review_pending_reason"]
        assert refreshed["review_retry_attempt"] == 1
        assert refreshed["review_retry_after"] == earliest_until

    def test_earliest_reviewer_retry_compares_parsed_datetimes_not_strings(self, tmp_path):
        """`until` timestamps can carry a non-UTC offset -- comparing the raw
        strings picks the wrong candidate whenever the lexicographically
        smaller offset digit corresponds to a later moment in time (TICK-517)."""
        from lanegate.executor import write_cooldown
        from lanegate.orchestrate.review import _earliest_reviewer_retry

        # 05:00-07:00 is 12:00 UTC; 06:00+00:00 is 06:00 UTC -- the second
        # is chronologically earlier despite starting with a larger digit.
        # Both must be far enough in the future that read_cooldown doesn't
        # treat them as already-expired and discard the file.
        write_cooldown(tmp_path, "claude-a", "weekly quota", retry_after="2027-08-12T05:00:00-07:00")
        write_cooldown(tmp_path, "claude-b", "weekly quota", retry_after="2027-08-12T06:00:00+00:00")
        cfg = self._pool_cfg()
        ticket = self._persisted_ticket(tmp_path)

        earliest = _earliest_reviewer_retry(tmp_path, cfg, ticket, "default")

        assert earliest == "2027-08-12T06:00:00+00:00"

    def test_earliest_reviewer_retry_includes_implementer_outside_pool(self, tmp_path):
        """A per-ticket `executor:` pin need not be a member of the routed
        pool, but it is still the fallback candidate
        resolve_independent_review_driver checks -- its cooldown must be
        consulted too, not just the pool's own executors (TICK-517)."""
        from lanegate.executor import write_cooldown
        from lanegate.orchestrate.review import _earliest_reviewer_retry

        cfg = self._pool_cfg()
        ticket = self._persisted_ticket(tmp_path)
        ticket["executor"] = "claude-c"  # not a member of the "default" pool
        write_cooldown(tmp_path, "claude-c", "weekly quota", retry_after=1800)

        earliest = _earliest_reviewer_retry(tmp_path, cfg, ticket, "default")

        expected_until = json.loads(
            (tmp_path / ".lanegate" / "executors" / "claude-c.cooldown").read_text()
        )["until"]
        assert earliest == expected_until

    def test_earliest_reviewer_retry_defaults_when_no_until_resolves(self, tmp_path):
        """If every candidate's cooldown state lacks a resolvable `until`,
        returning None reads as "nothing to wait for" and makes the ticket
        immediately re-eligible -- burning the whole retry budget in one
        scan instead of actually waiting. Must fall back to a concrete
        future retry time (TICK-517)."""
        import datetime

        from lanegate.executor import _cooldown_path
        from lanegate.orchestrate.review import _earliest_reviewer_retry

        cfg = self._pool_cfg()
        ticket = self._persisted_ticket(tmp_path)
        # A cooldown file with no "until" (e.g. a non-retryable error record).
        path = _cooldown_path(tmp_path, "claude-a")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"until": None, "reason": "non-retryable"}))

        before = datetime.datetime.now(datetime.timezone.utc)
        earliest = _earliest_reviewer_retry(tmp_path, cfg, ticket, "default")

        assert earliest is not None
        earliest_dt = datetime.datetime.fromisoformat(earliest)
        assert earliest_dt > before

    def test_pinned_reviewer_cooldown_hibernates_with_retry_metadata(self, tmp_path):
        """A pinned per-ticket reviewer (ticket['reviewer']) that is cooling
        down must get the same retry-metadata treatment as the pool-resolved
        case, not silently fall through to the old rate-limited hibernation."""
        from lanegate.executor import write_cooldown

        ticket = self._persisted_ticket(tmp_path)
        ticket["reviewer"] = "claude-b"
        cfg = self._pool_cfg()
        write_cooldown(tmp_path, "claude-b", "weekly quota", retry_after=900)
        expected_until = json.loads(
            (tmp_path / ".lanegate" / "executors" / "claude-b.cooldown").read_text()
        )["until"]

        result = run_review_agent(ticket, tmp_path, cfg=cfg)

        assert result is False
        refreshed = parse_ticket(ticket["_path"])
        assert refreshed["status"] == "hibernated"
        assert refreshed["review_pending"] is True
        assert "temporarily unavailable" in refreshed["review_pending_reason"]
        assert refreshed["review_retry_attempt"] == 1
        assert refreshed["review_retry_after"] == expected_until

    def test_reviewer_cooldown_repeated_failure_escalates_to_needs_review(self, tmp_path):
        """Once review_retry_attempt has already exhausted the retry budget,
        another cooldown must fail closed to needs_review via
        _escalate_no_reviewer rather than hibernate forever."""
        from lanegate.executor import write_cooldown
        from lanegate.orchestrate.review import _MAX_REVIEWER_UNAVAILABLE_RETRIES
        from lanegate.ticket import classify_needs_review_cause, write_ticket

        ticket = self._persisted_ticket(tmp_path)
        ticket["executor"] = "claude-a"
        ticket["review_retry_attempt"] = _MAX_REVIEWER_UNAVAILABLE_RETRIES
        write_ticket(ticket)
        ticket = parse_ticket(ticket["_path"])
        cfg = self._pool_cfg()
        write_cooldown(tmp_path, "claude-a", "weekly quota", retry_after=1800)
        write_cooldown(tmp_path, "claude-b", "weekly quota", retry_after=3600)

        result = run_review_agent(ticket, tmp_path, cfg=cfg)

        assert result is False
        refreshed = parse_ticket(ticket["_path"])
        assert refreshed["status"] == "needs_review"
        assert not refreshed.get("review_pending")
        assert classify_needs_review_cause(refreshed) == "no_independent_reviewer"
        assert "retry budget exhausted" in (refreshed.get("_body") or "")

    def test_no_independent_reviewer_config_stays_needs_review(self, tmp_path):
        """A permanent no-alternative-reviewer configuration (review_fallback:
        needs_review) must escalate straight to needs_review with no retry
        metadata recorded -- there is nothing to wait out."""
        ticket = self._persisted_ticket(tmp_path)
        ticket["executor"] = "claude-a"
        cfg = {
            "ticket_prefix": "TICK",
            "tickets_dir": "tickets",
            "executor": "claude-a",
            "commit_status_changes": False,
            "executors": {"claude-a": {"type": "claude-process"}},
            "review_fallback": "needs_review",
        }

        result = run_review_agent(ticket, tmp_path, cfg=cfg)

        assert result is False
        refreshed = parse_ticket(ticket["_path"])
        assert refreshed["status"] == "needs_review"
        assert not refreshed.get("review_pending")
        assert not refreshed.get("review_retry_after")
        from lanegate.ticket import classify_needs_review_cause

        assert classify_needs_review_cause(refreshed) == "no_independent_reviewer"


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
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=(False, None)),
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
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=(False, None)),
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
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=(False, None)),
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
        # A bounded auto-fix failure is an explicit human gate: do not leave
        # a code_complete lock that blocks overlapping open tickets or gets
        # retried forever.
        assert t["status"] == "needs_review"
        assert t["review_verdict"] == "changes_requested"

    def test_combined_mode_executor_no_verdict_pauses_with_error(self, tmp_path, capsys):
        """F7 fix: when the combined-mode executor runs `lanegate complete` but NOT
        `lanegate review --verdict`, the ticket ends up in code_complete with no
        verdict. The orchestrator must detect this unhandled state, pause, and
        report an error — not silently mark as done and wedge the board.

        TICK-508: leaving the ticket at code_complete here (rather than forcing
        needs_review) was itself a fail-open bug — a later orphan sweep or
        risk-scan failure could route a code_complete ticket back onto the
        auto-merge path without ever repeating review. The ticket must land on
        needs_review, not remain in code_complete."""
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
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=(False, None)),
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

        # Ticket must land on needs_review (not advanced to merged, and not
        # left stranded at code_complete for an orphan sweep to pick up).
        t = parse_ticket(tickets_dir / "TICK-001.md")
        assert t["status"] == "needs_review"

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
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=(False, None)),
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
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=(False, None)),
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
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=(False, None)),
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
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=(False, None)),
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
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=(False, None)),
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
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=(False, None)),
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
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=(False, None)),
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
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=(False, None)),
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
            "models": {"review": "codex-review-model"},
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
        assert status["resolved_model"] == "codex-review-model"
        assert "elapsed_seconds" in status

        recorded = json.loads((bundle / "verdict.json").read_text())
        assert recorded["verdict"] == verdict
        assert recorded["model"] == "codex-review-model"

    def test_review_records_nonzero_step_costs(self, tmp_path, monkeypatch):
        """TICK-408: a real review dispatch must log a step_costs row with
        nonzero tokens instead of silently skipping cost recording -- the
        observed bug was 543 logged review dispatches with only 13 carrying
        nonzero tokens."""
        verdict_text = json.dumps({"verdict": "approved", "notes": "ok"})
        codex_jsonl = "\n".join(
            [
                json.dumps(
                    {"type": "item.completed", "item": {"type": "agent_message", "text": verdict_text}}
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 8000,
                            "cached_input_tokens": 500,
                            "output_tokens": 120,
                            "reasoning_output_tokens": 0,
                        },
                    }
                ),
            ]
        )

        recorded = []
        monkeypatch.setattr(
            "lanegate.context_log.record_step_cost",
            lambda *args, **kwargs: recorded.append(args[-1]),
        )
        approved, _ = self._run(tmp_path, monkeypatch, codex_jsonl)

        assert approved is True
        assert len(recorded) == 1
        parsed = recorded[0]
        assert parsed["input_tokens"] == 8000 - 500
        assert parsed["output_tokens"] == 120
        assert parsed["cost_usd"] is not None
        assert parsed["cost_usd"] > 0

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
        ticket_path.write_text(
            ticket_path.read_text().replace(
                "status: code_complete\n",
                "status: code_complete\nreview_verdict: changes_requested\nreview_summary: Prior rejection summary\n",
            )
        )
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
        assert "review_verdict" not in refreshed
        assert "review_summary" not in refreshed
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
        cfg["models"] = {"review": "claude-review-model"}
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

    def test_unescaped_interior_quote_in_summary_is_repaired(self):
        review = self._parse(
            '{"verdict":"changes_requested","summary":"the function returns '
            '"foo" unexpectedly","findings":"none"}'
        )

        assert review.verdict == "changes_requested"
        assert "foo" in review.notes

    def test_unescaped_interior_quote_in_findings_is_repaired(self):
        review = self._parse(
            '{"verdict":"changes_requested","summary":"quoted source",'
            '"findings":"the expression returns "foo" unexpectedly"}'
        )

        assert review.verdict == "changes_requested"
        assert "foo" in review.findings

    def test_unescaped_interior_quote_before_comma_and_colon_is_repaired(self):
        review_comma = self._parse(
            '{"verdict":"changes_requested","summary":"the flag "--force", is ignored","findings":""}'
        )
        assert review_comma.verdict == "changes_requested"
        assert '--force"' in review_comma.notes

        review_colon = self._parse(
            '{"verdict":"changes_requested","summary":"see "foo": broken","findings":""}'
        )
        assert review_colon.verdict == "changes_requested"
        assert '"foo": broken' in review_colon.notes

    def test_unescaped_interior_quote_with_ticket_background_payload_is_repaired(self):
        output = (
            '{"verdict": "changes_requested", '
            '"summary": "whisper load bug", '
            '"findings": "calls WhisperModel(compute_type="int8", cpu_threads=4)) '
            'and prints "Loading Whisper..." here"}'
        )
        review = self._parse(output)

        assert review.verdict == "changes_requested"
        assert review.notes == "whisper load bug"
        assert 'compute_type="int8", cpu_threads=4)' in review.findings
        assert '"Loading Whisper..." here' in review.findings

    def test_prose_with_quoted_phrase_followed_by_valid_verdict_json(self):
        output = (
            'Reviewing now. The bug is in the "parse" helper.\n\n'
            '{"verdict": "approved", "summary": "ok", "findings": ""}'
        )
        review = self._parse(output)
        assert review.verdict == "approved"
        assert review.notes == "ok"

        output_array = (
            'The diff adds x = "a" which is fine.\n'
            '{"verdict": "approved", "summary": "ok", "findings": ["a", "b"]}'
        )
        review_array = self._parse(output_array)
        assert review_array.verdict == "approved"
        assert review_array.notes == "ok"


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


class TestInvokeCmdReviewSystemExitHandling:
    """_invoke_cmd_review absorbs the SystemExit cmd_review raises on a normal
    changes_requested verdict, but must not absorb the same SystemExit when
    it instead means cmd_review's own code_complete status guard rejected
    the call outright -- that case never wrote a verdict, and silently
    passing made a lost review indistinguishable from a real rejection
    (found live: TICK-392/393/395/396/398/400 all lost their reviewer
    findings this way in the same run)."""

    def test_changes_requested_system_exit_is_absorbed(self, tmp_path):
        from lanegate.orchestrate.review import _invoke_cmd_review

        def fake_cmd_review(*args, **kwargs):
            raise SystemExit(1)

        _invoke_cmd_review(fake_cmd_review, "TICK-001", {}, tmp_path, verdict="changes_requested")

    def test_unexpected_system_exit_for_non_changes_requested_verdict_raises(self, tmp_path):
        """Simulates cmd_review's code_complete guard firing: it always exits
        via sys.exit(1) same as a real changes_requested verdict, but the
        caller here asked for a different verdict (e.g. approved), so this
        exit cannot be the expected one -- it must surface, not vanish."""
        from lanegate.orchestrate.review import _invoke_cmd_review

        def fake_cmd_review(*args, **kwargs):
            raise SystemExit(1)

        with pytest.raises(RuntimeError, match="approved"):
            _invoke_cmd_review(fake_cmd_review, "TICK-001", {}, tmp_path, verdict="approved")

    def test_unexpected_system_exit_with_no_verdict_kwarg_raises(self, tmp_path):
        from lanegate.orchestrate.review import _invoke_cmd_review

        def fake_cmd_review(*args, **kwargs):
            raise SystemExit(1)

        with pytest.raises(RuntimeError):
            _invoke_cmd_review(fake_cmd_review, "TICK-001", {}, tmp_path, review_driver="human")

    def test_suppresses_direct_action_tracking_for_wrapped_review_command(self, tmp_path):
        """TICK-510: cmd_review is decorated with lifecycle._track_direct_action,
        which fabricates a standalone action-*.events.jsonl entry unless
        direct-action tracking is suppressed. Every loop/review-agent verdict
        write reaches cmd_review through this helper, so the suppression must
        live here rather than at each of that helper's many call sites."""
        from lanegate.lifecycle import _track_direct_action
        from lanegate.orchestrate.review import _invoke_cmd_review

        def fake_cmd_review(*args, **kwargs):
            raise SystemExit(1)

        tracked_fake_review = _track_direct_action("review")(fake_cmd_review)

        _invoke_cmd_review(
            tracked_fake_review, "TICK-001", {}, tmp_path, verdict="changes_requested"
        )

        logs_dir = tmp_path / ".lanegate" / "logs"
        assert not logs_dir.exists() or list(logs_dir.glob("action-*.events.jsonl")) == []

    def test_type_error_fallback_still_absorbs_changes_requested_exit(self, tmp_path):
        """Old-signature test mocks (no review_driver/model/independence
        kwargs) fall back to a clean call; that path's own SystemExit
        absorption must keep the same verdict-aware behavior."""
        from lanegate.orchestrate.review import _invoke_cmd_review

        calls = []

        def fake_cmd_review(tid, cfg, repo_root, *, verdict=None, **kwargs):
            calls.append(kwargs)
            if kwargs:
                raise TypeError("old signature does not accept review_driver")
            raise SystemExit(1)

        _invoke_cmd_review(
            fake_cmd_review, "TICK-001", {}, tmp_path,
            verdict="changes_requested", review_driver="agy",
        )
        # First call carries the unsupported kwarg and raises TypeError;
        # the retry strips it and succeeds (raising the expected SystemExit).
        assert calls == [{"review_driver": "agy"}, {}]

    def test_type_error_retry_stays_suppressed_and_records_no_action(self, tmp_path):
        """TICK-510: the TypeError compatibility retry is a second nested call
        into the same decorated cmd_review -- it must stay inside the same
        suppression as the primary call, not just the first attempt."""
        from lanegate.lifecycle import _track_direct_action
        from lanegate.orchestrate.review import _invoke_cmd_review
        from lanegate.orchestrate.run_report import direct_action_tracking_suppressed

        calls = []

        def fake_cmd_review(tid, cfg, repo_root, *, verdict=None, **kwargs):
            assert direct_action_tracking_suppressed()
            calls.append(kwargs)
            if kwargs:
                raise TypeError("old signature does not accept review_driver")
            raise SystemExit(1)

        tracked_fake_review = _track_direct_action("review")(fake_cmd_review)

        _invoke_cmd_review(
            tracked_fake_review, "TICK-001", {}, tmp_path,
            verdict="changes_requested", review_driver="agy",
        )
        assert calls == [{"review_driver": "agy"}, {}]

        logs_dir = tmp_path / ".lanegate" / "logs"
        assert not logs_dir.exists() or list(logs_dir.glob("action-*.events.jsonl")) == []


def test_control_plane_file_requires_independent_review(tmp_path):
    """Control-plane files (lanegate/review.py, lanegate/analyze.py, lanegate/safeguards.py) require independent model review."""
    from lanegate.orchestrate.review import resolve_independent_review_driver

    ticket_cp = {
        "id": "TICK-610",
        "touches": ["lanegate/orchestrate/review.py"],
        "executor": "codex",
    }
    cfg_same_fallback = {
        "executor": "codex",
        "review_fallback": "same_model",
        "control_plane_files": ["lanegate/orchestrate/review.py"],
    }

    # Same model fallback is rejected for control-plane files
    driver, ind = resolve_independent_review_driver(
        ticket_cp, cfg_same_fallback, tmp_path, implementer="codex"
    )
    assert driver is None
    assert ind == "needs_review"

    # Non-control-plane file permits same_model fallback if configured
    ticket_reg = {
        "id": "TICK-610",
        "touches": ["src/app.py"],
        "executor": "codex",
    }
    driver_reg, ind_reg = resolve_independent_review_driver(
        ticket_reg, cfg_same_fallback, tmp_path, implementer="codex"
    )
    assert driver_reg == "codex"
    assert ind_reg == "self"


def test_control_plane_file_escalates_when_review_independence_undetermined(tmp_path):
    """Control-plane files escalate to needs_review when review independence is undetermined (e.g. manual implementer)."""
    from unittest.mock import MagicMock, patch
    from lanegate.orchestrate.review import run_review_agent

    ticket_manual = {
        "id": "TICK-610",
        "touches": ["lanegate/orchestrate/review.py"],
        "implement_mode": "manual",
    }
    cfg = {
        "control_plane_files": ["lanegate/orchestrate/review.py"],
    }
    with patch("lanegate.orchestrate.review._escalate_no_reviewer") as mock_esc, \
         patch("lanegate.orchestrate.resolve_pool_executor", return_value="codex"):
        mock_esc.return_value = {"status": "needs_review"}
        res = run_review_agent(ticket_manual, tmp_path, cfg=cfg)
        assert mock_esc.called
        assert "cannot be determined" in mock_esc.call_args.kwargs.get("reason", "")


class TestMinimalCfgConfigErrorPropagation:
    """TICK-650 review finding: _minimal_cfg's fallback must not mask a genuine
    config validation failure (e.g. a malformed executors.aider.model_settings
    block) behind a bogus executors-less config."""

    def test_config_error_propagates_not_swallowed(self, tmp_path):
        tickets_dir = tmp_path / "tickets"
        tickets_dir.mkdir()
        ticket_path = tickets_dir / "TICK-900.md"
        ticket_path.write_text("---\nid: TICK-900\n---\nBody.\n")
        ticket = {"id": "TICK-900", "_path": ticket_path}

        (tmp_path / ".lanegate.yml").write_text(
            "executors:\n"
            "  aider:\n"
            "    model_settings:\n"
            "      some-model:\n"
            "        context_window_tokens: 0\n"
        )

        with pytest.raises(ConfigError):
            _minimal_cfg(ticket, tmp_path)

    def test_other_exceptions_still_fall_back(self, tmp_path):
        """A non-ConfigError failure (e.g. an unreadable/corrupt file) must
        still use the inferred-from-ticket-path fallback, unchanged."""
        tickets_dir = tmp_path / "tickets"
        tickets_dir.mkdir()
        ticket_path = tickets_dir / "TICK-901.md"
        ticket_path.write_text("---\nid: TICK-901\n---\nBody.\n")
        ticket = {"id": "TICK-901", "_path": ticket_path}

        with patch("lanegate.config.load_config", side_effect=OSError("boom")):
            cfg = _minimal_cfg(ticket, tmp_path)

        assert cfg["ticket_prefix"] == "TICK"
        assert cfg["tickets_dir"] == str(tickets_dir)


from lanegate.orchestrate.review import run_review_agent
from lanegate.ticket import REVIEW_FINDINGS_HEADER

def test_circuit_breaker_recurring_findings(tmp_path, monkeypatch):
    wt = tmp_path / "worktree"
    wt.mkdir()
    
    # Body with 2 previous attempts
    body = f"""
{REVIEW_FINDINGS_HEADER}
- The cache key is wrong.

{REVIEW_FINDINGS_HEADER} (attempt 2)
- The cache key is wrong.
"""
    
    ticket = {
        "id": "TICK-1",
        "status": "in_review",
        "_body": body,
        "review_findings": ["The cache key is wrong."],
        "_path": str(wt / "TICK-1.md")
    }
    
    # create dummy file so `_escalate_harness_error` works
    (wt / "TICK-1.md").write_text("dummy")
    
    cfg = {}
    
    # Mock the LLM output to return changes_requested with the same finding
    def fake_subprocess_run(*args, **kwargs):
        class FakeResult:
            returncode = 0
            stdout = '{"verdict": "changes_requested", "findings": "The cache key is wrong."}'
            stderr = ""
        return FakeResult()
    
    monkeypatch.setattr("subprocess.run", fake_subprocess_run)
    monkeypatch.setattr("lanegate.orchestrate.review._write_review_verdict", lambda *a, **k: None)
    
    escalated = {}
    def fake_mark_needs_review(t, cfg, repo, reason=None):
        escalated["reason"] = reason
        t["status"] = "needs_review"
    monkeypatch.setattr("lanegate.lifecycle._mark_needs_review", fake_mark_needs_review)
    
    run_review_agent(ticket, tmp_path, worktree_path=wt, cfg=cfg, pool_name="pool")
    
    assert "circuit breaker" in escalated.get("reason", "")
    assert ticket["status"] == "needs_review"
