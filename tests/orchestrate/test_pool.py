"""
Tests for lanegate/orchestrate/pool.py — executor pools, dispatch resolution, invoke_executor.

Split out of the former monolithic tests/test_orchestrate.py (TICK-316).
"""

from __future__ import annotations

import datetime
import os
import sys

from tests.orchestrate.conftest import *  # noqa: F401,F403

from lanegate.orchestrate.audit import has_step_bundle
from lanegate.orchestrate.pool import (
    _CONFIG_ERROR_EXIT_CODE,
    _is_main_checkout_bookkeeping_path,
    capture_manual_implement_step_run,
)


class TestBuildExecutorCmd:
    def test_claude(self):
        cmd = _build_executor_cmd("claude", "do the thing", {})
        assert cmd == ["claude", "-p", "do the thing", "--output-format", "stream-json", "--verbose"]

    def test_claude_process(self):
        cmd = _build_executor_cmd("claude-process", "do the thing", {})
        assert cmd == ["claude", "-p", "do the thing", "--output-format", "stream-json", "--verbose"]

    def test_aider(self):
        cmd = _build_executor_cmd("aider", "fix the bug", {})
        assert cmd == ["aider", "--message", "fix the bug"]

    def test_openhands(self):
        cmd = _build_executor_cmd("openhands", "refactor this", {})
        assert cmd == ["openhands", "--headless", "--json", "-t", "refactor this", "--always-approve"]

    def test_passthrough(self):
        cmd = _build_executor_cmd("myexec", "do work", {})
        assert cmd == ["myexec", "do work"]

    def test_executor_cfg_bin_override(self):
        cfg = {"executors": {"claude": {"bin": "/custom/claude", "flags": ["--no-color"]}}}
        cmd = _build_executor_cmd("claude", "do the thing", cfg)
        assert cmd == ["/custom/claude", "--no-color", "-p", "do the thing", "--output-format", "stream-json", "--verbose"]

    def test_executor_cfg_flags_passed_through(self):
        # --dangerously-skip-permissions is configured per-user in .lanegate.yml,
        # not hardcoded; this test shows it flows through when set in flags.
        cfg = {"executors": {"claude": {"flags": ["--dangerously-skip-permissions"]}}}
        cmd = _build_executor_cmd("claude", "do the thing", cfg)
        assert cmd == [
            "claude", "--dangerously-skip-permissions", "-p", "do the thing",
            "--output-format", "stream-json", "--verbose",
        ]

    def test_executor_cfg_flags_only(self):
        cfg = {"executors": {"aider": {"flags": ["--yes"]}}}
        cmd = _build_executor_cmd("aider", "fix the bug", cfg)
        assert cmd == ["aider", "--yes", "--message", "fix the bug"]

    # --- model injection ---

    def test_claude_with_model(self):
        cmd = _build_executor_cmd("claude", "do the thing", {}, model="claude-sonnet-4-5")
        assert cmd == [
            "claude", "-p", "do the thing", "--model", "claude-sonnet-4-5",
            "--output-format", "stream-json", "--verbose",
        ]

    def test_claude_process_with_model(self):
        cmd = _build_executor_cmd("claude-process", "do work", {}, model="claude-opus-4-5")
        assert cmd == [
            "claude", "-p", "do work", "--model", "claude-opus-4-5",
            "--output-format", "stream-json", "--verbose",
        ]

    def test_claude_without_model_no_flag(self):
        cmd = _build_executor_cmd("claude", "do the thing", {}, model=None)
        assert "--model" not in cmd
        assert cmd == ["claude", "-p", "do the thing", "--output-format", "stream-json", "--verbose"]

    def test_aider_with_model(self):
        cmd = _build_executor_cmd("aider", "fix the bug", {}, model="gpt-4o")
        assert cmd == ["aider", "--model", "gpt-4o", "--message", "fix the bug"]

    def test_openhands_with_model(self):
        # OpenHands V1 headless invocation ignores the model argument.
        cmd = _build_executor_cmd("openhands", "refactor this", {}, model="gpt-4-turbo")
        assert cmd == ["openhands", "--headless", "--json", "-t", "refactor this", "--always-approve"]

    def test_codex_with_model(self):
        # `codex exec` has no --timeout flag in current CLI releases, so
        # none is passed (see lanegate.executor._effective_print_timeout_seconds).
        cmd = _build_executor_cmd("codex", "add feature", {}, model="o4-mini")
        assert cmd == ["codex", "exec", "--json", "--model", "o4-mini", "add feature"]

    def test_codex_without_model(self):
        cmd = _build_executor_cmd("codex", "add feature", {}, model=None)
        assert cmd == ["codex", "exec", "--json", "add feature"]

    # --- ollama positional model argument ---

    def test_ollama_model_is_positional(self):
        """ollama run flags follow its positional model argument."""
        cmd = _build_executor_cmd("ollama", "do stuff", {}, model="llama3.1")
        assert cmd == ["ollama", "run", "llama3.1", "--nowordwrap", "do stuff"]

    def test_ollama_no_model_uses_default(self):
        """ollama without a model uses llama3 as a sensible default positional arg."""
        cmd = _build_executor_cmd("ollama", "do stuff", {}, model=None)
        assert cmd[0] == "ollama"
        assert cmd[1] == "run"
        assert "--model" not in cmd
        # The default model is third, followed by the safe run-subcommand flag.
        assert cmd[2:4] == ["llama3", "--nowordwrap"]
        assert len(cmd) == 5

    def test_ollama_model_not_as_flag(self):
        """Ollama must NOT use --model flag — it uses a positional argument."""
        cmd = _build_executor_cmd("ollama", "do stuff", {}, model="mistral")
        assert "--model" not in cmd
        assert "mistral" in cmd



    def test_rebase_conflict_commit_scopes_to_conflict_files(self, tmp_path):
        """Regression test for TICK-686: commit_worktree_changes must isolate the
        commit to the requested paths, even if other files were already staged or untracked."""
        repo = tmp_path / "repo"
        repo.mkdir()

        def git(*args):
            return __import__("subprocess").run(
                ["git", *args], cwd=repo, check=True, capture_output=True, text=True
            )

        git("init", "-b", "main")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "Test User")

        (repo / "base.txt").write_text("base")
        git("add", "base.txt")
        git("commit", "-m", "base")

        conflict_file = repo / "conflict.txt"
        conflict_file.write_text("resolved")

        unrelated_staged = repo / "unrelated.txt"
        unrelated_staged.write_text("unrelated")
        git("add", "unrelated.txt")

        unrelated_untracked = repo / "untracked.txt"
        unrelated_untracked.write_text("untracked")

        from lanegate.orchestrate.pool import commit_worktree_changes
        result = commit_worktree_changes(repo, "TICK-686", paths=["conflict.txt"])

        assert result[0] is True

        committed_files = git("show", "--format=", "--name-only", "HEAD").stdout.splitlines()
        assert "conflict.txt" in committed_files
        assert "unrelated.txt" not in committed_files
        assert "untracked.txt" not in committed_files

        # unrelated.txt should still be staged
        status = git("status", "--porcelain").stdout
        assert "A  unrelated.txt" in status or "A  unrelated.txt" in status.replace("AM", "A ")
        assert "?? untracked.txt" in status


# ---------------------------------------------------------------------------
# invoke_executor
# ---------------------------------------------------------------------------


def test_resolve_driver_precedence_all_levels():
    cfg = {
        "executor": "top-impl",
        "reviewer": "top-review",
        "steps": {
            "implement": {"driver": "step-impl"},
            "review": {"driver": "step-review"},
            "analyze": {"driver": "step-analyze"},
        },
    }

    assert resolve_driver("implement", {"executor": "ticket-impl"}, cfg) == "ticket-impl"
    assert resolve_driver("review", {"reviewer": "ticket-review"}, cfg) == "ticket-review"
    assert resolve_driver("implement", {}, cfg) == "step-impl"
    assert resolve_driver("review", {}, cfg) == "step-review"
    assert resolve_driver("analyze", {}, cfg) == "step-analyze"
    assert resolve_driver("implement", {}, {"executor": "top-impl"}) == "top-impl"
    assert resolve_driver("review", {}, {"reviewer": "top-review", "executor": "top-impl"}) == (
        "top-review"
    )
    assert resolve_driver("review", {}, {"executor": "top-impl"}) == "top-impl"
    assert resolve_driver("implement", {}, {}) == "claude"
    assert resolve_driver("review", {}, {}) == "claude"


def test_resolve_driver_preserves_legacy_executor_steps_for_fix():
    cfg = {
        "executor": "claude-process",
        "executor_steps": {"fix": "codex"},
    }

    assert resolve_driver("fix", {}, cfg) == "codex"


def test_invoke_executor_rejects_ollama_for_implement(tmp_path):
    """Raw ollama has no code-application step; implement must fail closed
    with the ordinary config-error sentinel instead of dispatching to
    _invoke_ollama and silently producing zero commits."""
    ticket = {
        "id": "TICK-009",
        "title": "Ollama implement",
        "touches": ["a.py"],
        "close_criteria": "Done.",
        "_body": "Body.",
    }
    cfg = {"executor": "ollama"}

    with patch("lanegate.orchestrate.pool._invoke_ollama") as mock_ollama:
        exit_code, *_ = invoke_executor(ticket, cfg, tmp_path, step="implement")

    assert exit_code == _CONFIG_ERROR_EXIT_CODE
    mock_ollama.assert_not_called()


def test_invoke_executor_config_error_stderr(tmp_path):
    ticket = {
        "id": "TICK-ERR",
        "title": "Err title",
        "touches": ["a.py"],
        "close_criteria": "Done.",
        "_body": "Body.",
    }
    cfg = {"executor": "aider"}
    
    with patch("lanegate.orchestrate.pool.build_executor_cmd", side_effect=ConfigError("test message 123")):
        exit_code, captured_stdout, captured_stderr = invoke_executor(ticket, cfg, tmp_path, step="implement")
        
    assert exit_code == _CONFIG_ERROR_EXIT_CODE
    assert "test message 123" in captured_stderr
    assert "executor configuration failed for 'aider'" in captured_stderr


def test_invoke_executor_timeout_diagnostic(tmp_path):
    ticket = {
        "id": "TICK-TIMEOUT",
        "title": "Timeout title",
        "touches": ["a.py"],
        "close_criteria": "Done.",
        "_body": "Body.",
    }
    cfg = {"executor": "aider"}
    
    with patch("lanegate.orchestrate.pool.build_executor_cmd", return_value=["echo", "hello"]), \
         patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "out", "err", "stall")):
        exit_code, captured_stdout, captured_stderr = invoke_executor(ticket, cfg, tmp_path, step="implement")
    
    assert "dispatch terminated due to 'stall' after" in captured_stderr
    assert "heartbeats received" in captured_stderr


def test_ticket_executor_pins_implement_and_every_fix_dispatch_away_from_global_codex(tmp_path):
    """A ticket's implementer owns its complete implementation/fix lifecycle."""
    ticket = {
        "id": "TICK-PINNED",
        "title": "Pinned provider",
        "touches": ["a.py"],
        "close_criteria": "Done.",
        "_body": "Body.",
        "executor": "aider",
    }
    cfg = _default_cfg(tmp_path)
    cfg["executor"] = "codex"
    cfg["steps"] = {
        "implement": {"driver": "codex"},
        "fix": {"driver": "codex"},
    }

    with patch(
        "lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")
    ) as mock_stream:
        invoke_executor(ticket, cfg, tmp_path, step="implement")
        invoke_executor(ticket, cfg, tmp_path, step="fix", prompt_override="first fix")
        invoke_executor(ticket, cfg, tmp_path, step="fix", prompt_override="second fix")

    assert [call.args[0][0] for call in mock_stream.call_args_list] == [
        "aider",
        "aider",
        "aider",
    ]


def test_ticket_reviewer_override_bypasses_aider_implementation_and_codex_pool(tmp_path):
    """Final review remains a ticket reviewer decision, not an implementer decision."""
    from lanegate.orchestrate.loop import resolve_pool_executor

    ticket = {"id": "TICK-REVIEWER", "executor": "aider", "reviewer": "claude-process"}
    cfg = _default_cfg(tmp_path)
    cfg["executor"] = "codex"
    cfg["reviewer"] = "codex"
    cfg["pools"] = {"default": {"executors": ["codex-a"], "strategy": "round-robin"}}
    cfg["executors"] = {"codex-a": {"type": "codex"}}

    assert resolve_pool_executor("implement", ticket, cfg, tmp_path) == "aider"
    assert resolve_pool_executor("review", ticket, cfg, tmp_path) == "claude-process"


def test_expand_driver_named_and_legacy():
    cfg = {
        "drivers": {
            "fast": {
                "type": "claude-process",
                "model": "claude-fast",
                "env": {"LANEGATE_MODE": "fast"},
            }
        }
    }

    assert expand_driver("fast", cfg) == {
        "type": "claude-process",
        "model": "claude-fast",
        "env": {"LANEGATE_MODE": "fast"},
    }
    assert expand_driver("aider", cfg) == {"type": "aider"}


def test_build_env_merges_driver_env_and_expands_refs(monkeypatch):
    monkeypatch.setenv("LANEGATE_TOKEN_SOURCE", "secret-token")
    monkeypatch.setenv("UNCHANGED_PARENT", "parent-value")

    env = _build_env(
        {
            "env": {
                "TOKEN": "${LANEGATE_TOKEN_SOURCE}",
                "URL": "https://${MISSING_HOST}/api",
                "COUNT": 3,
            }
        }
    )

    assert env["UNCHANGED_PARENT"] == "parent-value"
    assert env["TOKEN"] == "secret-token"
    assert env["URL"] == "https:///api"
    assert env["COUNT"] == "3"


def test_build_env_rejects_non_mapping_env():
    with pytest.raises(ConfigError, match="driver env must be a mapping"):
        _build_env({"env": ["BAD"]})


def test_invoke_executor_marks_child_as_internal_lane_run(tmp_path):
    """Executor-owned lifecycle calls must not create direct-action history."""
    from lanegate.orchestrate.run_report import INTERNAL_RUN_ENV

    ticket = {
        "id": "TICK-INTERNAL-ENV",
        "title": "Internal run marker",
        "touches": ["a.py"],
        "close_criteria": "Done.",
        "_body": "Body.",
    }
    cfg = _default_cfg(tmp_path)

    with patch(
        "lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")
    ) as stream:
        invoke_executor(ticket, cfg, tmp_path)

    assert stream.call_args.kwargs["env"][INTERNAL_RUN_ENV] == "1"


def test_lifecycle_resolve_reviewer_checks_steps_review_driver():
    from lanegate.lifecycle import resolve_reviewer

    cfg = {
        "steps": {"review": {"driver": "step-reviewer"}},
        "reviewer": "cfg-reviewer",
        "executor": "cfg-executor",
    }

    assert resolve_reviewer({"reviewer": "ticket-reviewer"}, cfg) == "ticket-reviewer"
    assert resolve_reviewer({}, cfg) == "step-reviewer"
    assert resolve_reviewer({}, {"reviewer": "cfg-reviewer", "executor": "cfg-executor"}) == (
        "cfg-reviewer"
    )


class TestInvokeExecutor:
    def test_main_checkout_violation_caught_post_dispatch(self, tmp_path, capsys):
        ticket = {
            "id": "TICK-670",
            "title": "Detect main checkout writes",
            "touches": ["leaked_file.py"],
            "close_criteria": "Detect isolation leaks.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)

        real_run = subprocess.run
        statuses = iter(["", " M leaked_file.py\n"])

        def fake_run(cmd, *args, **kwargs):
            if cmd == ["git", "status", "--porcelain", "-uno"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=next(statuses))
            return real_run(cmd, *args, **kwargs)

        with (
            patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")),
            patch("lanegate.orchestrate.pool.subprocess.run", side_effect=fake_run),
        ):
            exit_code, *_ = invoke_executor(ticket, cfg, tmp_path, repo_root=tmp_path)

        assert exit_code == 1
        stderr = capsys.readouterr().err
        assert "worktree isolation leak" in stderr
        assert "leaked_file.py" in stderr

    def test_main_checkout_violation_catches_reverted_tracked_file(self, tmp_path, capsys):
        ticket = {
            "id": "TICK-670",
            "title": "Detect main checkout writes",
            "touches": ["leaked_file.py"],
            "close_criteria": "Detect isolation leaks.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)

        real_run = subprocess.run
        statuses = iter([" M leaked_file.py\n", ""])

        def fake_run(cmd, *args, **kwargs):
            if cmd == ["git", "status", "--porcelain", "-uno"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=next(statuses))
            return real_run(cmd, *args, **kwargs)

        with (
            patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")),
            patch("lanegate.orchestrate.pool.subprocess.run", side_effect=fake_run),
        ):
            exit_code, *_ = invoke_executor(ticket, cfg, tmp_path, repo_root=tmp_path)

        assert exit_code == 1
        stderr = capsys.readouterr().err
        assert "worktree isolation leak" in stderr
        assert "leaked_file.py" in stderr

    def test_main_checkout_violation_ignores_sibling_ticket_and_config_changes(
        self, tmp_path, capsys
    ):
        """Regression for TICK-680: a sibling ticket's status-transition
        commit or a human editing .lanegate.yml concurrently must not be
        mistaken for this ticket's executor leaking writes into the main
        checkout."""
        ticket = {
            "id": "TICK-670",
            "title": "Detect main checkout writes",
            "touches": ["leaked_file.py"],
            "close_criteria": "Detect isolation leaks.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)
        cfg["tickets_dir"] = "./.lanegate/tickets"
        tickets_relpath = ".lanegate/tickets"

        real_run = subprocess.run
        statuses = iter(
            [
                "",
                f" M {tickets_relpath}/TICK-999.md\n M .lanegate.yml\n",
            ]
        )

        def fake_run(cmd, *args, **kwargs):
            if cmd == ["git", "status", "--porcelain", "-uno"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=next(statuses))
            return real_run(cmd, *args, **kwargs)

        with (
            patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")),
            patch("lanegate.orchestrate.pool.subprocess.run", side_effect=fake_run),
        ):
            exit_code, *_ = invoke_executor(ticket, cfg, tmp_path, repo_root=tmp_path)

        assert exit_code == 0
        assert "worktree isolation leak" not in capsys.readouterr().err

    def test_main_checkout_violation_catches_source_amid_bookkeeping_changes(
        self, tmp_path, capsys
    ):
        ticket = {
            "id": "TICK-670",
            "title": "Detect main checkout writes",
            "touches": ["leaked_file.py"],
            "close_criteria": "Detect isolation leaks.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)
        tickets_relpath = os.path.relpath(cfg["tickets_dir"], tmp_path)

        real_run = subprocess.run
        statuses = iter(
            [
                "",
                f" M {tickets_relpath}/TICK-999.md\n"
                " M .lanegate.yml\n"
                " M lanegate/orchestrate/pool.py\n",
            ]
        )

        def fake_run(cmd, *args, **kwargs):
            if cmd == ["git", "status", "--porcelain", "-uno"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=next(statuses))
            return real_run(cmd, *args, **kwargs)

        with (
            patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")),
            patch("lanegate.orchestrate.pool.subprocess.run", side_effect=fake_run),
        ):
            exit_code, *_ = invoke_executor(ticket, cfg, tmp_path, repo_root=tmp_path)

        assert exit_code == 1
        stderr = capsys.readouterr().err
        assert "worktree isolation leak" in stderr
        assert "lanegate/orchestrate/pool.py" in stderr
        assert "TICK-999.md" not in stderr
        assert ".lanegate.yml" not in stderr

    def test_main_checkout_violation_not_hidden_by_arrow_in_literal_filename(
        self, tmp_path, capsys
    ):
        """Regression for TICK-680 review: a leaked file whose literal name
        contains ' -> ' (not a rename -- status code is plain 'A ') must not
        be split into two bookkeeping-looking halves and waved through."""
        ticket = {
            "id": "TICK-670",
            "title": "Detect main checkout writes",
            "touches": ["leaked_file.py"],
            "close_criteria": "Detect isolation leaks.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)

        real_run = subprocess.run
        statuses = iter(["", "A  .lanegate.yml -> .lanegate.yml\n"])

        def fake_run(cmd, *args, **kwargs):
            if cmd == ["git", "status", "--porcelain", "-uno"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=next(statuses))
            return real_run(cmd, *args, **kwargs)

        with (
            patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")),
            patch("lanegate.orchestrate.pool.subprocess.run", side_effect=fake_run),
        ):
            exit_code, *_ = invoke_executor(ticket, cfg, tmp_path, repo_root=tmp_path)

        assert exit_code == 1
        stderr = capsys.readouterr().err
        assert "worktree isolation leak" in stderr
        assert ".lanegate.yml -> .lanegate.yml" in stderr

    def test_main_checkout_violation_catches_lanegate_yaml_not_the_real_config(
        self, tmp_path, capsys
    ):
        """Regression for TICK-680 review: only '.lanegate.yml' (CONFIG_FILENAME)
        is real lanegate config. A tracked '.lanegate.yaml' is not the config
        file lanegate actually reads, so an executor writing one is a genuine
        leak and must still be reported, not silently exempted."""
        ticket = {
            "id": "TICK-670",
            "title": "Detect main checkout writes",
            "touches": ["leaked_file.py"],
            "close_criteria": "Detect isolation leaks.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)

        real_run = subprocess.run
        statuses = iter(["", "A  .lanegate.yaml\n"])

        def fake_run(cmd, *args, **kwargs):
            if cmd == ["git", "status", "--porcelain", "-uno"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=next(statuses))
            return real_run(cmd, *args, **kwargs)

        with (
            patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")),
            patch("lanegate.orchestrate.pool.subprocess.run", side_effect=fake_run),
        ):
            exit_code, *_ = invoke_executor(ticket, cfg, tmp_path, repo_root=tmp_path)

        assert exit_code == 1
        stderr = capsys.readouterr().err
        assert "worktree isolation leak" in stderr
        assert ".lanegate.yaml" in stderr

    def test_main_checkout_bookkeeping_tickets_dir_uses_posix_separator(self, tmp_path):
        """Regression for TICK-680 review: on Windows, os.path.normpath()
        renders tickets_dir with backslashes, which never matches git
        porcelain output (always '/'). Simulate Windows normpath behavior
        on this POSIX test runner to exercise the replace(os.sep, "/") fix."""
        cfg = _default_cfg(tmp_path)
        cfg["tickets_dir"] = ".lanegate/tickets"

        with (
            patch("lanegate.orchestrate.pool.os.sep", "\\"),
            patch(
                "lanegate.orchestrate.pool.os.path.normpath",
                side_effect=lambda p: p.replace("/", "\\"),
            ),
        ):
            assert _is_main_checkout_bookkeeping_path(
                ".lanegate/tickets/TICK-999.md", cfg, tmp_path
            )

    def test_main_checkout_bookkeeping_covers_generated_state_and_docs(self, tmp_path):
        """TICK-708: an analyze pass rewriting a `.lanegate/context/*` skeleton,
        or a supervisor editing a `docs/internal/*.md` session log, during a
        concurrent review must not be mistaken for an executor leak. Real
        source under a code root still is."""
        cfg = _default_cfg(tmp_path)
        bookkeeping = [
            ".lanegate/context/TICK-500/file_skeletons.json",
            ".lanegate/logs/orchestrate-2026-08-28.log",
            ".lanegate/prompts/TICK-500-review.md",
            "docs/internal/supervision-session-2026-08-27.md",
            "README.md",
        ]
        for p in bookkeeping:
            assert _is_main_checkout_bookkeeping_path(p, cfg, tmp_path), p
        for p in ("lanegate/executor.py", "tests/test_executor.py", "pyproject.toml"):
            assert not _is_main_checkout_bookkeeping_path(p, cfg, tmp_path), p

    def test_nested_markdown_is_not_whitelisted(self, tmp_path):
        """TICK-722: only docs/ and root-level .md are bookkeeping. A nested .md
        (a package README, lanegate/skills/*.md) is a real project file an
        executor edits in its worktree — a concurrent main-checkout change to
        one is still a possible leak."""
        cfg = _default_cfg(tmp_path)
        assert _is_main_checkout_bookkeeping_path("CHANGELOG.md", cfg, tmp_path)
        assert _is_main_checkout_bookkeeping_path("docs/executor-capabilities.md", cfg, tmp_path)
        for p in ("lanegate/skills/supervise.md", "lanegate/templates/prompts/review.md",
                  "some/pkg/README.md"):
            assert not _is_main_checkout_bookkeeping_path(p, cfg, tmp_path), p

    def test_only_incremental_executors_receive_output_idle_timeout(self, tmp_path):
        ticket = {
            "id": "TICK-TIMEOUT", "title": "Timeout policy", "touches": [],
            "close_criteria": "ok", "_body": "",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "aider"
        cfg["executor_timeout_seconds"] = 321

        with patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")) as mock_stream:
            exit_code, *_ = invoke_executor(ticket, cfg, tmp_path)

        assert exit_code == 0
        kwargs = mock_stream.call_args.kwargs
        assert kwargs["timeout"] == 321.0
        assert "idle_timeout" not in kwargs
        assert "absolute_ceiling" not in kwargs

    def test_incremental_executors_receive_idle_timeout_and_ceiling(self, tmp_path):
        ticket = {
            "id": "TICK-TIMEOUT", "title": "Timeout policy", "touches": [],
            "close_criteria": "ok", "_body": "",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude-process"
        cfg["executor_idle_timeout_seconds"] = 12
        cfg["executor_stall_timeout_seconds"] = 120
        cfg["executor_absolute_ceiling_seconds"] = 345

        with patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")) as mock_stream:
            exit_code, *_ = invoke_executor(ticket, cfg, tmp_path)

        assert exit_code == 0
        kwargs = mock_stream.call_args.kwargs
        assert kwargs["idle_timeout"] == 12.0
        assert kwargs["stall_timeout"] == 120.0
        assert kwargs["absolute_ceiling"] == 345.0
        assert callable(kwargs["liveness_probe"])
        assert callable(kwargs["progress_probe"])
        assert "timeout" not in kwargs

    def test_oversized_prompt_does_not_raise_arg_max(self, tmp_path):
        ticket = {
            "id": "TICK-ARGMAX", "title": "Long prompt", "touches": [],
            "close_criteria": "ok", "_body": "",
            "change_notes": {"x.py": "x" * 20_000},
        }
        cfg = _default_cfg(tmp_path)

        def reject_long_argv(cmd, *args, **kwargs):
            if any(len(str(part)) > 200 for part in cmd):
                raise OSError(7, "Argument list too long")
            return 0, "", "", None

        with patch("lanegate.orchestrate.pool._stream_subprocess", side_effect=reject_long_argv):
            rc, *_ = invoke_executor(ticket, cfg, tmp_path)
        assert rc == 0

    def test_aider_parser_mismatch_warns_on_production_dispatch_path(self, tmp_path, capsys):
        """TICK-657 attempt-4 finding: the parser-rejection warning lived only in
        dispatch_executor, which production orchestration never calls — invoke_executor
        (via _stream_subprocess) reached the generic 'made no commits' failure with no
        diagnostic. Assert the warning now fires on this real dispatch path too."""
        ticket = {
            "id": "TICK-PARSER", "title": "Parser mismatch", "touches": [],
            "close_criteria": "ok", "_body": "",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "aider"
        cfg["executors"] = {"aider": {"edit_format": "diff"}}
        diff_stdout = "@@ -1 +1 @@\n-old\n+new\n"

        with patch(
            "lanegate.orchestrate.pool._stream_subprocess",
            return_value=(0, diff_stdout, ""),
        ) as mock_stream:
            rc, *_ = invoke_executor(ticket, cfg, tmp_path)

        cmd = mock_stream.call_args.args[0]
        assert "--edit-format" in cmd
        assert rc == 0
        assert "Aider parser mismatch detected" in capsys.readouterr().err

    def test_calls_correct_subprocess_for_claude_process(self, tmp_path):
        ticket = {
            "id": "TICK-001",
            "title": "Test",
            "touches": ["a.py"],
            "close_criteria": "Tests pass.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude-process"

        with patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")) as mock_stream:
            exit_code, *_ = invoke_executor(ticket, cfg, tmp_path)

        assert exit_code == 0
        call_args = mock_stream.call_args
        cmd = call_args[0][0]
        assert cmd[0] == "claude"
        assert cmd[1] == "-p"
        # TICK-178: the bare ticket ID no longer appears in the trusted prompt
        # (kept out so the trusted layer is byte-identical/cacheable across
        # tickets); the title already identifies the ticket to the model.
        assert "TICK-001" not in call_args.kwargs["stdin_text"]
        assert "Test" in call_args.kwargs["stdin_text"]
        prompt_file = tmp_path / ".lanegate" / "prompts" / "TICK-001-implement.md"
        assert prompt_file.exists()
        assert "Test" in prompt_file.read_text()

    def test_invoke_executor_forwards_claude_config_dir_from_bin(self, tmp_path):
        ticket = {
            "id": "TICK-071",
            "title": "Config directory",
            "touches": ["a.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
        }
        captured_kwargs = []

        def fake_build_executor_cmd(*args, **kwargs):
            captured_kwargs.append(kwargs)
            return ["claude", "-p"]

        for executor, executors, expected in [
            ("claude-a", {"claude-a": {"type": "claude-process", "bin": "claude-a"}}, ".claude-a"),
            ("claude-process", {}, ".claude"),
        ]:
            cfg = _default_cfg(tmp_path)
            cfg["executor"] = executor
            cfg["executors"] = executors
            with (
                patch("lanegate.orchestrate.pool.build_executor_cmd", side_effect=fake_build_executor_cmd),
                patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")),
            ):
                exit_code, *_ = invoke_executor(ticket, cfg, tmp_path)

            assert exit_code == 0
            assert captured_kwargs.pop()["claude_config_dir"] == Path.home() / expected

    def test_calls_correct_subprocess_for_aider(self, tmp_path):
        ticket = {
            "id": "TICK-002",
            "title": "Aider task",
            "touches": ["b.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "aider"

        with patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")) as mock_stream:
            exit_code, *_ = invoke_executor(ticket, cfg, tmp_path)

        assert exit_code == 0
        call_args = mock_stream.call_args
        cmd = call_args[0][0]
        assert cmd[0] == "aider"
        assert "--message" in cmd

    def test_codex_prompt_is_sent_via_stdin(self, tmp_path):
        ticket = {
            "id": "TICK-006",
            "title": "Codex task",
            "touches": ["f.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "codex"

        with patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")) as mock_stream:
            invoke_executor(ticket, cfg, tmp_path)

        cmd = mock_stream.call_args.args[0]
        kwargs = mock_stream.call_args.kwargs
        assert cmd[:2] == ["codex", "exec"]
        assert cmd[-1] == "-"
        assert kwargs["stdin_text"]
        # TICK-178: the bare ticket ID no longer appears in the trusted prompt
        # sent via stdin either — title already identifies the ticket.
        assert "TICK-006" not in kwargs["stdin_text"]
        assert "Codex task" in kwargs["stdin_text"]
        assert all("TICK-006" not in arg for arg in cmd)
        prompt_file = tmp_path / ".lanegate" / "prompts" / "TICK-006-implement.md"
        assert prompt_file.exists()
        assert "Codex task" in prompt_file.read_text()

    def test_codex_ignores_claude_ticket_model_override(self, tmp_path):
        ticket = {
            "id": "TICK-006",
            "title": "Codex task with stale Claude model",
            "touches": ["f.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
            "model": "claude-sonnet-4-6",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "codex"
        cfg["executors"] = {"codex": {"models": {"implement": "gpt-5-codex"}}}

        with patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")) as mock_stream:
            invoke_executor(ticket, cfg, tmp_path)

        cmd = mock_stream.call_args.args[0]
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "gpt-5-codex"
        assert "claude-sonnet-4-6" not in cmd

    def test_ticket_model_field_injected_into_cmd(self, tmp_path):
        """Per-ticket model: field is passed as --model to the executor."""
        ticket = {
            "id": "TICK-003",
            "title": "Model override test",
            "touches": ["c.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
            "model": "claude-opus-4-5",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude-process"

        with patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")) as mock_stream:
            invoke_executor(ticket, cfg, tmp_path)

        cmd = mock_stream.call_args[0][0]
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "claude-opus-4-5"

    def test_cfg_models_implement_injected_into_cmd(self, tmp_path):
        """Top-level models.implement is injected when no per-ticket model is set."""
        ticket = {
            "id": "TICK-004",
            "title": "Config model test",
            "touches": ["d.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude-process"
        cfg["models"] = {"implement": "claude-sonnet-4-5"}

        with patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")) as mock_stream:
            invoke_executor(ticket, cfg, tmp_path)

        cmd = mock_stream.call_args[0][0]
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "claude-sonnet-4-5"

    def test_ticket_model_overrides_cfg_models_implement(self, tmp_path):
        """ticket.model beats cfg models.implement."""
        ticket = {
            "id": "TICK-005",
            "title": "Override precedence test",
            "touches": ["e.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
            "model": "claude-haiku-4-5-per-ticket",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude-process"
        cfg["models"] = {"implement": "claude-sonnet-4-5"}

        with patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")) as mock_stream:
            invoke_executor(ticket, cfg, tmp_path)

        cmd = mock_stream.call_args[0][0]
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "claude-haiku-4-5-per-ticket"

    def test_file_skeletons_in_saved_prompt(self, tmp_path):
        """File skeletons from TICK-064 appear in saved prompt file."""
        ticket = {
            "id": "TICK-007",
            "title": "Skeleton test",
            "touches": ["s.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
            "file_skeletons": {"s.py": "# s.py\ndef func(): ...\n  # line 5"},
        }
        cfg = _default_cfg(tmp_path)

        with patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")):
            invoke_executor(ticket, cfg, tmp_path)

        prompt_file = tmp_path / ".lanegate" / "prompts" / "TICK-007-implement.md"
        assert prompt_file.exists()
        prompt_text = prompt_file.read_text()
        assert "## File skeletons" in prompt_text
        assert "def func():" in prompt_text

    def test_change_notes_in_saved_prompt(self, tmp_path):
        """Change notes from analyze appear in saved prompt file."""
        ticket = {
            "id": "TICK-008",
            "title": "Change notes test",
            "touches": ["c.py", "tests/test_c.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
            "change_notes": {
                "c.py": "Add xyz() function at line 20.",
                "tests/test_c.py": "Add test_xyz.",
            },
        }
        cfg = _default_cfg(tmp_path)

        with patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")):
            invoke_executor(ticket, cfg, tmp_path)

        prompt_file = tmp_path / ".lanegate" / "prompts" / "TICK-008-implement.md"
        assert prompt_file.exists()
        prompt_text = prompt_file.read_text()
        assert "## Planned changes" in prompt_text
        assert "Add xyz() function" in prompt_text
        assert "Add test_xyz" in prompt_text

    def test_project_guidance_in_saved_prompt(self, tmp_path):
        """Repo-local coding guidance appears in the executor prompt artifact."""
        (tmp_path / "AGENTS.md").write_text("Use pytest fixtures for setup.")
        ticket = {
            "id": "TICK-009",
            "title": "Guidance test",
            "touches": ["g.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)

        with patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")):
            invoke_executor(ticket, cfg, tmp_path)

        prompt_file = tmp_path / ".lanegate" / "prompts" / "TICK-009-implement.md"
        prompt_text = prompt_file.read_text()
        assert "## Project guidance" in prompt_text
        assert "Use pytest fixtures for setup." in prompt_text

    def test_invoke_executor_threads_analyze_session_id_into_resume(self, tmp_path):
        """TICK-188: a ticket carrying analyze_session_id gets --resume <id> on
        the implement dispatch, so analyze and implement continue one CLI
        session instead of starting cold. This is the actual production
        dispatch path (invoke_executor -> build_executor_cmd), not the
        unused dispatch_executor() retry helper. TICK-310's resume_session_gate
        fails open (allows resume) when there's no step_costs history yet for
        this session_id, which is the case here (empty tmp db)."""
        ticket = {
            "id": "TICK-012",
            "title": "Resume threading test",
            "touches": ["r.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
            "analyze_session_id": "sess-abc123",
            "analyze_session_executor": "claude-process",
            "analyze_session_model": "claude-haiku-4-5-20251001",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude-process"

        captured_cmd = {}

        def fake_subprocess(cmd, *args, **kwargs):
            captured_cmd["cmd"] = cmd
            return 0, "", ""

        with (
            patch("lanegate.orchestrate.pool._stream_subprocess", side_effect=fake_subprocess),
            patch("lanegate.context_log._get_default_db_path", return_value=tmp_path / "analytics.db"),
        ):
            invoke_executor(ticket, cfg, tmp_path, step="implement")

        cmd = captured_cmd["cmd"]
        assert "--resume" in cmd
        assert cmd[cmd.index("--resume") + 1] == "sess-abc123"

    def test_codex_expired_resume_retries_once_fresh(self, tmp_path):
        """An expired Codex rollout retries once without its stale resume ID."""
        ticket = {
            "id": "TICK-012A",
            "title": "Expired Codex resume",
            "touches": ["r.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
            "analyze_session_id": "expired-session",
            "analyze_session_executor": "codex",
            "analyze_session_model": "gpt-5.6-terra",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "codex"
        cfg["executors"] = {"codex": {"type": "codex", "models": {"implement": "gpt-5.6-terra"}}}
        calls = []

        def expired_then_success(cmd, *args, **kwargs):
            calls.append(cmd)
            if len(calls) == 1:
                return 1, "", "Error: thread/resume failed: no rollout found for thread id expired-session"
            return 0, "", ""

        with patch("lanegate.orchestrate.pool._stream_subprocess", side_effect=expired_then_success):
            assert invoke_executor(ticket, cfg, tmp_path) == (0, "", "")

        assert len(calls) == 2
        assert calls[0][:3] == ["codex", "exec", "resume"]
        assert calls[1][:2] == ["codex", "exec"]
        assert "resume" not in calls[1]
        assert "expired-session" not in calls[1]

        calls.clear()
        with patch(
            "lanegate.orchestrate.pool._stream_subprocess",
            side_effect=lambda cmd, *args, **kwargs: (calls.append(cmd) or (1, "", "generic failure")),
        ):
            assert invoke_executor(ticket, cfg, tmp_path) == (1, "", "generic failure")

        assert len(calls) == 1

    def test_non_codex_expired_resume_also_retries_once_fresh(self, tmp_path):
        """The resume-rejection fresh retry is not codex-only: any executor in
        _SESSION_RESUME_TYPES gets one fresh attempt when its --resume id is
        rejected (TICK-718)."""
        ticket = {
            "id": "TICK-012C",
            "title": "Expired cursor resume",
            "touches": ["r.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
            "analyze_session_id": "expired-sess",
            "analyze_session_executor": "cursor",
            "analyze_session_model": "cursor-fast",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "cursor"
        cfg["executors"] = {"cursor": {"type": "cursor", "models": {"implement": "cursor-fast"}}}
        calls = []

        def expired_then_success(cmd, *args, **kwargs):
            calls.append(cmd)
            if len(calls) == 1:
                return 1, "", "error: session not found: expired-sess"
            return 0, "", ""

        with patch("lanegate.orchestrate.pool._stream_subprocess", side_effect=expired_then_success):
            assert invoke_executor(ticket, cfg, tmp_path) == (0, "", "")

        assert len(calls) == 2
        assert "--resume" in calls[0]
        assert "--resume" not in calls[1]

        # A generic non-resume failure still fails once, no retry.
        calls.clear()
        with patch(
            "lanegate.orchestrate.pool._stream_subprocess",
            side_effect=lambda cmd, *a, **k: (calls.append(cmd) or (1, "", "compilation error in r.py")),
        ):
            assert invoke_executor(ticket, cfg, tmp_path) == (1, "", "compilation error in r.py")
        assert len(calls) == 1

    def test_invoke_executor_does_not_resume_session_from_other_executor(self, tmp_path):
        """A pool switch must start fresh rather than pass an Agy/Claude ID to Codex."""
        ticket = {
            "id": "TICK-012B",
            "title": "Provider-aware resume",
            "touches": ["r.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
            "analyze_session_id": "agy-session",
            "analyze_session_executor": "agy",
            "analyze_session_model": "gemini-3.6-flash-high",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "codex"
        cfg["executors"] = {"codex": {"type": "codex", "models": {"implement": "gpt-5.6-terra"}}}
        captured_cmd = {}

        def fake_subprocess(cmd, *args, **kwargs):
            captured_cmd["cmd"] = cmd
            return 0, "", ""

        with patch("lanegate.orchestrate.pool._stream_subprocess", side_effect=fake_subprocess):
            invoke_executor(ticket, cfg, tmp_path, step="implement")

        assert captured_cmd["cmd"][:3] == ["codex", "exec", "--json"]
        assert "agy-session" not in captured_cmd["cmd"]

    def test_invoke_executor_does_not_resume_session_from_model_switch(self, tmp_path):
        """Same executor type but a different configured model must also
        start fresh -- a model switch changes the conversation's context the
        session was built on just as much as a provider switch does."""
        ticket = {
            "id": "TICK-012D",
            "title": "Model-aware resume",
            "touches": ["r.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
            "analyze_session_id": "sess-old-model",
            "analyze_session_executor": "claude-process",
            "analyze_session_model": "claude-haiku-4-5-20251001",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude-process"
        cfg["models"] = {"implement": "claude-opus-5"}
        captured_cmd = {}

        def fake_subprocess(cmd, *args, **kwargs):
            captured_cmd["cmd"] = cmd
            return 0, "", ""

        with patch("lanegate.orchestrate.pool._stream_subprocess", side_effect=fake_subprocess):
            invoke_executor(ticket, cfg, tmp_path, step="implement")

        assert "--resume" not in captured_cmd["cmd"]
        assert "sess-old-model" not in captured_cmd["cmd"]

    def test_invoke_executor_does_not_resume_for_non_implement_steps(self, tmp_path):
        """analyze_session_id must not leak into review/fix-agent dispatches —
        it's specifically for continuing analyze into implement."""
        ticket = {
            "id": "TICK-013",
            "title": "No resume outside implement",
            "touches": ["r.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
            "analyze_session_id": "sess-abc123",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude-process"

        captured_cmd = {}

        def fake_subprocess(cmd, *args, **kwargs):
            captured_cmd["cmd"] = cmd
            return 0, "", ""

        with patch("lanegate.orchestrate.pool._stream_subprocess", side_effect=fake_subprocess):
            invoke_executor(ticket, cfg, tmp_path, step="review")

        assert "--resume" not in captured_cmd["cmd"]

    def test_invoke_executor_resumes_fix_from_implement_session(self, tmp_path):
        """TICK-310: a first-time fix pass resumes implement's own session
        (fix is a continuation of implement's work, not independent)."""
        ticket = {
            "id": "TICK-014",
            "title": "Fix resumes implement",
            "touches": ["r.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
            "implement_session_id": "sess-implement-1",
            "implement_session_executor": "claude-process",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude-process"

        captured_cmd = {}

        def fake_subprocess(cmd, *args, **kwargs):
            captured_cmd["cmd"] = cmd
            return 0, "", ""

        with (
            patch("lanegate.orchestrate.pool._stream_subprocess", side_effect=fake_subprocess),
            patch("lanegate.context_log._get_default_db_path", return_value=tmp_path / "analytics.db"),
        ):
            invoke_executor(ticket, cfg, tmp_path, step="fix")

        cmd = captured_cmd["cmd"]
        assert "--resume" in cmd
        assert cmd[cmd.index("--resume") + 1] == "sess-implement-1"

    def test_invoke_executor_resumes_fix_from_prior_fix_session(self, tmp_path):
        """A second autofix cycle's fix pass resumes the *first* fix pass's
        session, not implement's -- fix_session_id takes priority once set."""
        ticket = {
            "id": "TICK-015",
            "title": "Second fix pass resumes first fix",
            "touches": ["r.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
            "implement_session_id": "sess-implement-1",
            "implement_session_executor": "claude-process",
            "fix_session_id": "sess-fix-1",
            "fix_session_executor": "claude-process",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude-process"

        captured_cmd = {}

        def fake_subprocess(cmd, *args, **kwargs):
            captured_cmd["cmd"] = cmd
            return 0, "", ""

        with (
            patch("lanegate.orchestrate.pool._stream_subprocess", side_effect=fake_subprocess),
            patch("lanegate.context_log._get_default_db_path", return_value=tmp_path / "analytics.db"),
        ):
            invoke_executor(ticket, cfg, tmp_path, step="fix")

        cmd = captured_cmd["cmd"]
        assert "--resume" in cmd
        assert cmd[cmd.index("--resume") + 1] == "sess-fix-1"

    def test_invoke_executor_skips_resume_when_gate_blocks_stale_session(self, tmp_path):
        """TICK-310: a session past max_session_age_s must dispatch fresh
        instead of --resume, even though a candidate session id exists --
        resuming a cold cache can cost more than starting over. The origin
        executor must match the selected one (fix has no configured model,
        so origin_model=None matches dispatch model=None here) so the case
        actually reaches resume_session_gate rather than being short-circuited
        by the provider/model match check first."""
        from lanegate.context_log import log_step_cost

        db_path = tmp_path / "analytics.db"
        log_step_cost(
            db_path,
            "proj",
            "TICK-016",
            "implement",
            session_id="sess-stale",
            timestamp="2020-01-01T00:00:00Z",  # far past any sane age ceiling
        )

        ticket = {
            "id": "TICK-016",
            "title": "Stale session must not resume",
            "touches": ["r.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
            "implement_session_id": "sess-stale",
            "implement_session_executor": "claude-process",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude-process"

        captured_cmd = {}

        def fake_subprocess(cmd, *args, **kwargs):
            captured_cmd["cmd"] = cmd
            return 0, "", ""

        with (
            patch("lanegate.orchestrate.pool._stream_subprocess", side_effect=fake_subprocess),
            patch("lanegate.context_log._get_default_db_path", return_value=db_path),
            patch("lanegate.context_log._get_project_id", return_value="proj"),
        ):
            invoke_executor(ticket, cfg, tmp_path, step="fix")

        assert "--resume" not in captured_cmd["cmd"]

    def test_invoke_executor_clamps_agy_duration_to_measured_elapsed(self, tmp_path):
        """agy's self-reported duration_seconds reflects the whole resumed
        --conversation session (prior turns included), not just this
        invocation's turn -- confirmed live in a fresh-install smoke test
        (~42s self-reported vs. ~22s LaneGate itself measured around the
        same subprocess call). The written step_costs row must reflect the
        actually-measured wall-clock elapsed time, not the inflated
        self-reported one."""
        import json

        ticket = {
            "id": "TICK-017",
            "title": "Agy duration clamp",
            "touches": ["r.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "agy"

        agy_stdout = json.dumps(
            {
                "conversation_id": "conv-1",
                "status": "SUCCESS",
                "response": "done",
                "duration_seconds": 300.0,
                "num_turns": 2,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        )

        def fake_subprocess(cmd, *args, **kwargs):
            return 0, agy_stdout, ""

        with (
            patch("lanegate.orchestrate.pool._stream_subprocess", side_effect=fake_subprocess),
            patch("lanegate.context_log._get_default_db_path", return_value=tmp_path / "analytics.db"),
            patch("lanegate.context_log._get_project_id", return_value="proj"),
        ):
            invoke_executor(ticket, cfg, tmp_path, step="implement")

        from lanegate.context_log import _load_step_costs_from_db

        rows = _load_step_costs_from_db(tmp_path / "analytics.db", project="proj")
        assert len(rows) == 1
        assert rows[0]["duration_ms"] < 5000

    def test_invoke_executor_does_not_resume_legacy_session_without_origin_metadata(self, tmp_path):
        """A session id written before origin metadata existed (no
        analyze_session_executor/analyze_session_model on the ticket) must
        start fresh rather than resume, even though the currently selected
        executor happens to match what the legacy session was probably
        created with -- there is no recorded model to compare against, so
        the match check treats it as a mismatch rather than guessing."""
        ticket = {
            "id": "TICK-012C",
            "title": "Legacy session metadata",
            "touches": ["r.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
            "analyze_session_id": "sess-legacy",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude-process"

        captured_cmd = {}

        def fake_subprocess(cmd, *args, **kwargs):
            captured_cmd["cmd"] = cmd
            return 0, "", ""

        with (
            patch("lanegate.orchestrate.pool._stream_subprocess", side_effect=fake_subprocess),
            patch("lanegate.context_log._get_default_db_path", return_value=tmp_path / "analytics.db"),
        ):
            invoke_executor(ticket, cfg, tmp_path, step="implement")

        assert "--resume" not in captured_cmd["cmd"]
        assert "sess-legacy" not in captured_cmd["cmd"]

    def test_invoke_executor_skips_resume_when_session_chaining_disabled(self, tmp_path):
        ticket = {
            "id": "TICK-017",
            "title": "Chaining disabled",
            "touches": ["r.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
            "analyze_session_id": "sess-abc123",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude-process"
        cfg["session_chaining"] = {"enabled": False}

        captured_cmd = {}

        def fake_subprocess(cmd, *args, **kwargs):
            captured_cmd["cmd"] = cmd
            return 0, "", ""

        with (
            patch("lanegate.orchestrate.pool._stream_subprocess", side_effect=fake_subprocess),
            patch("lanegate.context_log._get_default_db_path", return_value=tmp_path / "analytics.db"),
        ):
            invoke_executor(ticket, cfg, tmp_path, step="implement")

        assert "--resume" not in captured_cmd["cmd"]

    def test_invoke_executor_persists_session_id_for_implement_and_fix(self, tmp_path):
        """After a successful Claude dispatch, invoke_executor writes
        <step>_session_id onto the ticket file so a later step can resume it."""
        from lanegate.ticket import parse_ticket

        ticket_path = tmp_path / "TICK-018.md"
        ticket = {
            "id": "TICK-018",
            "title": "Persist session id",
            "touches": ["r.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
            "_path": ticket_path,
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude-process"

        claude_json = json.dumps({"type": "result", "result": "done", "session_id": "sess-new-1"})

        def fake_subprocess(cmd, *args, **kwargs):
            return 0, claude_json, ""

        with (
            patch("lanegate.orchestrate.pool._stream_subprocess", side_effect=fake_subprocess),
            patch("lanegate.context_log._get_default_db_path", return_value=tmp_path / "analytics.db"),
        ):
            invoke_executor(ticket, cfg, tmp_path, step="implement")

        on_disk = parse_ticket(ticket_path)
        assert on_disk["implement_session_id"] == "sess-new-1"
        assert on_disk["implement_session_executor"] == "claude-process"
        assert on_disk["implement_session_model"] == "claude-haiku-4-5-20251001"

    def test_invoke_executor_persists_implementer_identity_without_structured_parser(self, tmp_path):
        """Aider (and ollama/openhands/continue/gemini) have no registered
        structured-output parser, so parse_structured_result() always
        returns None for them -- unlike claude/codex/agy. review.py's
        self-review detection (_implementer_identity) reads
        implement_session_executor regardless of executor type, so it must
        still be recorded even with no session id to persist alongside it,
        or a same-instance review silently gets mislabeled independent."""
        from lanegate.ticket import parse_ticket

        ticket_path = tmp_path / "TICK-019.md"
        ticket = {
            "id": "TICK-019",
            "title": "Persist implementer identity for a plain-text executor",
            "touches": ["r.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
            "_path": ticket_path,
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "aider"

        def fake_subprocess(cmd, *args, **kwargs):
            return 0, "plain text reply, no JSON envelope", ""

        with (
            patch("lanegate.orchestrate.pool._stream_subprocess", side_effect=fake_subprocess),
            patch("lanegate.context_log._get_default_db_path", return_value=tmp_path / "analytics.db"),
        ):
            invoke_executor(ticket, cfg, tmp_path, step="implement")

        on_disk = parse_ticket(ticket_path)
        assert on_disk["implement_session_executor"] == "aider"
        assert "implement_session_id" not in on_disk

    def test_invoke_executor_reloads_ticket_from_disk_before_writing_identity(self, tmp_path):
        """A combined-mode agent runs as this same subprocess and may call
        `lanegate complete`/`lanegate review` itself mid-dispatch, changing
        status/verdict on disk. The identity write must not clobber that
        with the stale pre-dispatch in-memory ticket snapshot."""
        from lanegate.ticket import parse_ticket, write_ticket

        ticket_path = tmp_path / "TICK-020.md"
        ticket = {
            "id": "TICK-020",
            "title": "Combined-mode self-report survives identity write",
            "touches": ["r.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
            "_path": ticket_path,
            "status": "open",
        }
        write_ticket(dict(ticket))
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude-process"

        def fake_subprocess(cmd, *args, **kwargs):
            # Simulate the combined-mode agent itself calling `lanegate
            # complete && lanegate review --verdict` mid-run, before this
            # dispatch wrapper gets a chance to persist identity fields.
            on_disk = parse_ticket(ticket_path)
            on_disk["status"] = "in_review"
            on_disk["review_verdict"] = "approved"
            write_ticket(on_disk)
            return 0, "", ""

        with (
            patch("lanegate.orchestrate.pool._stream_subprocess", side_effect=fake_subprocess),
            patch("lanegate.context_log._get_default_db_path", return_value=tmp_path / "analytics.db"),
        ):
            invoke_executor(ticket, cfg, tmp_path, step="implement")

        on_disk = parse_ticket(ticket_path)
        assert on_disk["status"] == "in_review"
        assert on_disk["review_verdict"] == "approved"
        assert on_disk["implement_session_executor"] == "claude-process"

    def test_invoke_executor_persists_pool_instance_not_bare_type(self, tmp_path):
        """A pool of same-type instances (e.g. aider-14b/aider-7b, both type
        "aider") must have the specific instance name persisted, not the
        bare executor type shared by every instance in the pool -- otherwise
        review.py's self-review detection can't tell "aider-7b reviewed
        aider-14b's work" (independent) apart from "aider-14b reviewed its
        own work" (self), since both would read back as just "aider"."""
        from lanegate.ticket import parse_ticket

        ticket_path = tmp_path / "TICK-021.md"
        ticket = {
            "id": "TICK-021",
            "title": "Persist pool instance identity, not bare type",
            "touches": ["r.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
            "_path": ticket_path,
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "aider"
        cfg["drivers"] = {
            "aider-14b": {"type": "aider", "model": "ollama_chat/qwen2.5-coder:14b"},
            "aider-7b": {"type": "aider", "model": "ollama_chat/qwen2.5-coder:7b"},
        }
        cfg["executors"] = {
            "aider-14b": {"type": "aider", "model": "ollama_chat/qwen2.5-coder:14b"},
            "aider-7b": {"type": "aider", "model": "ollama_chat/qwen2.5-coder:7b"},
        }

        def fake_subprocess(cmd, *args, **kwargs):
            return 0, "plain text reply, no JSON envelope", ""

        with (
            patch("lanegate.orchestrate.pool._stream_subprocess", side_effect=fake_subprocess),
            patch("lanegate.context_log._get_default_db_path", return_value=tmp_path / "analytics.db"),
        ):
            invoke_executor(ticket, cfg, tmp_path, step="implement", executor_override="aider-7b")

        on_disk = parse_ticket(ticket_path)
        assert on_disk["implement_session_executor"] == "aider-7b"

    def test_invoke_executor_writes_active_status_and_heartbeats(self, tmp_path):
        """invoke_executor writes active status JSON with executor PID, heartbeats, and log path."""
        import json
        import time

        ticket = {
            "id": "TICK-010",
            "title": "Status tracking test",
            "touches": ["t.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude-process"
        cfg["executor_heartbeat_seconds"] = 0.1  # Fast heartbeat for testing

        def fake_subprocess(*args, **kwargs):
            """Simulate a subprocess that sleeps briefly then exits."""
            # Capture the on_start callback
            on_start = kwargs.get("on_start")
            if on_start is not None:
                on_start(99999)  # Fake PID
            time.sleep(0.3)  # Long enough for heartbeats
            return (0, "", "")

        with patch("lanegate.orchestrate.pool._stream_subprocess", side_effect=fake_subprocess):
            exit_code, *_ = invoke_executor(ticket, cfg, tmp_path, repo_root=tmp_path)

        assert exit_code == 0

        # Check that active status file was written to per-session file
        # (with concurrent executors, each session has its own status file)
        status_dir = tmp_path / ".lanegate" / "active-orchestrate"
        assert status_dir.exists(), f"Status directory not found at {status_dir}"

        status_files = list(status_dir.glob("*.json"))
        assert len(status_files) > 0, f"No status files found in {status_dir}"

        status = json.loads(status_files[0].read_text())
        assert status["ticket_id"] == "TICK-010"
        assert status["executor_pid"] == 99999
        assert "executor_session" in status
        assert status["executor_session"].startswith("TICK-010-")
        assert status["heartbeat_count"] >= 1
        assert status["state"] == "finished"
        assert status["exit_code"] == 0
        assert "log_path" in status

        # Check that executor PID markers were cleaned up after executor finished
        pid_marker = tmp_path / ".lanegate" / "TICK-010.pid"
        session_marker = tmp_path / ".lanegate" / "TICK-010.session"
        assert not pid_marker.exists(), "PID marker should be cleaned up after executor finishes"
        assert not session_marker.exists(), "Session marker should be cleaned up after executor finishes"

    def test_heartbeats_for_non_structured_executor_never_report_stall(self, tmp_path):
        """aider (and any driver without a structured JSON progress stream) sits
        silent -- no parseable events -- for the length of an entire local-model
        generation, which can run minutes. Without a structured stream to judge
        real inactivity against, silence alone must not be reported as 'stall':
        it always heartbeats instead, no matter how long the silence runs.
        """
        import json
        import time

        ticket = {
            "id": "TICK-011",
            "title": "Stall-label test",
            "touches": ["t.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "aider-7b"
        cfg["executor_heartbeat_seconds"] = 0.05

        def fake_subprocess(*args, **kwargs):
            on_start = kwargs.get("on_start")
            if on_start is not None:
                on_start(99999)
            # No stdout/stderr lines at all -- like aider mid-generation with
            # no structured progress to parse -- for several heartbeat ticks.
            time.sleep(0.3)
            return (0, "", "")

        with patch("lanegate.orchestrate.pool._stream_subprocess", side_effect=fake_subprocess):
            exit_code, *_ = invoke_executor(ticket, cfg, tmp_path, repo_root=tmp_path)

        assert exit_code == 0
        status_dir = tmp_path / ".lanegate" / "active-orchestrate"
        status_files = list(status_dir.glob("*.json"))
        status = json.loads(status_files[0].read_text())
        assert status["heartbeat_count"] >= 1
        assert status["last_executor_event"]["activity"] != "stall"
        assert status["last_executor_event"]["activity"] == "heartbeat"

    def test_orchestrate_streams_heartbeats_to_terminal(self, tmp_path):
        """Heartbeat lines are written to terminal_stream (compact mode) AND log_stream."""
        import io
        import time

        ticket = {
            "id": "TICK-011",
            "title": "Heartbeat streaming test",
            "touches": ["h.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude-process"
        cfg["executor_heartbeat_seconds"] = 0.05  # very fast for test speed

        log_buf = io.StringIO()
        terminal_buf = io.StringIO()

        def fake_subprocess(*args, **kwargs):
            on_start = kwargs.get("on_start")
            if on_start is not None:
                on_start(12345)
            time.sleep(0.2)  # long enough for at least one heartbeat
            return (0, "", "")

        with patch("lanegate.orchestrate.pool._stream_subprocess", side_effect=fake_subprocess):
            exit_code, *_ = invoke_executor(
                ticket,
                cfg,
                tmp_path,
                log_stream=log_buf,
                terminal_stream=terminal_buf,
                repo_root=tmp_path,
            )

        assert exit_code == 0

        log_text = log_buf.getvalue()
        terminal_text = terminal_buf.getvalue()

        # Compact mode always uses the safe phase/activity line, even when
        # the executor has not emitted a structured event yet.
        assert "TICK-011" in log_text and "[implementing]" in log_text
        assert "heartbeat" not in log_text  # activity is intentionally compacted
        assert "TICK-011" in terminal_text and "[implementing]" in terminal_text

        # Content should be identical (same line written to both)
        log_hb_lines = [l for l in log_text.splitlines() if "[implementing]" in l]
        term_hb_lines = [l for l in terminal_text.splitlines() if "[implementing]" in l]
        assert log_hb_lines, "no heartbeat lines in log"
        assert term_hb_lines, "no heartbeat lines in terminal"
        assert log_hb_lines[0] == term_hb_lines[0], "first heartbeat line differs between streams"

    def test_structured_stream_event_reaches_compact_cli_and_run_log(self, tmp_path):
        """A Codex JSONL event is normalized before either public surface sees it."""
        import io

        from lanegate.orchestrate.run_report import _run_events_path, _write_last_run_pointer, read_executor_events

        ticket = {
            "id": "TICK-011A", "title": "Structured progress", "touches": ["p.py"],
            "close_criteria": "Done.", "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "codex"
        session_ts = "2026-07-31T10-45-00"
        (tmp_path / ".lanegate" / "logs").mkdir(parents=True)
        _write_last_run_pointer(tmp_path, session_ts, tmp_path / ".lanegate" / "logs" / "orchestrate-test.log")
        terminal = io.StringIO()

        def fake_subprocess(*args, **kwargs):
            kwargs["on_line"](
                '{"type":"item.started","item":{"type":"tool_call","name":"file_read","path":"src/progress.py"}}\n',
                True,
            )
            return (0, "", "")

        with patch("lanegate.orchestrate.pool._stream_subprocess", side_effect=fake_subprocess):
            exit_code, *_ = invoke_executor(ticket, cfg, tmp_path, terminal_stream=terminal, repo_root=tmp_path)

        assert exit_code == 0
        assert "[implementing]" in terminal.getvalue()
        assert "src/progress.py" in terminal.getvalue()
        events = read_executor_events(tmp_path, session_ts)
        assert len(events) == 1
        assert events[0]["progress"]["tool_category"] == "file_read"
        assert _run_events_path(tmp_path, session_ts).is_file()

    def test_heartbeat_not_written_to_terminal_when_terminal_stream_is_none(self, tmp_path):
        """When terminal_stream is None (verbose mode), heartbeats go only to log_stream."""
        import io
        import time

        ticket = {
            "id": "TICK-012",
            "title": "No terminal stream test",
            "touches": ["n.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude-process"
        cfg["executor_heartbeat_seconds"] = 0.05

        log_buf = io.StringIO()

        def fake_subprocess(*args, **kwargs):
            on_start = kwargs.get("on_start")
            if on_start is not None:
                on_start(22222)
            time.sleep(0.2)
            return (0, "", "")

        with patch("lanegate.orchestrate.pool._stream_subprocess", side_effect=fake_subprocess):
            exit_code, *_ = invoke_executor(
                ticket,
                cfg,
                tmp_path,
                log_stream=log_buf,
                terminal_stream=None,
                repo_root=tmp_path,
            )

        assert exit_code == 0
        assert "TICK-012" in log_buf.getvalue()
        assert "[implementing]" in log_buf.getvalue()

    def test_invoke_executor_status_records_resolved_mode(self, tmp_path):
        """TICK-343: every run directory reports combined vs split, so a
        combined-mode self-review is identifiable after the fact."""
        ticket = {
            "id": "TICK-343",
            "title": "Mode capture",
            "touches": ["a.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
        }

        def _mode_for(cfg):
            with patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")):
                invoke_executor(ticket, cfg, tmp_path, repo_root=tmp_path)
            bundles = sorted(
                (tmp_path / ".lanegate" / "executor-runs" / "TICK-343").iterdir(),
                key=lambda p: p.stat().st_mtime,
            )
            return json.loads((bundles[-1] / "status.json").read_text())["mode"]

        combined_cfg = _default_cfg(tmp_path)
        combined_cfg["executor"] = "claude-process"
        # _default_cfg pins reviewer=auto-none, which is a split route.
        combined_cfg.pop("reviewer")
        assert _mode_for(combined_cfg) == "combined"

        split_cfg = _default_cfg(tmp_path)
        split_cfg["executor"] = "claude-process"
        split_cfg.pop("reviewer")
        split_cfg["executor_steps"] = {"implement": "claude-process", "review": "codex"}
        assert _mode_for(split_cfg) == "split"

    def test_claude_audit_bundle_captures_transcript_tasks_prompt_status_and_git(self, tmp_path):
        import os
        import time

        ticket = {
            "id": "TICK-148",
            "title": "Audit capture",
            "touches": ["a.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude-process"
        fake_home = tmp_path / "home"
        encoded = _claude_encoded_cwd(tmp_path)
        transcript_dir = fake_home / ".claude" / "projects" / encoded
        transcript_dir.mkdir(parents=True)

        # Use deterministic values for session_id: TICK-148-1700000000-12345-implement
        deterministic_time = 1700000000
        deterministic_pid = 12345
        session_id = f"TICK-148-{deterministic_time}-{deterministic_pid}-implement"
        (transcript_dir / f"{session_id}.jsonl").write_text('{"type":"message"}\n')

        task_dir = Path("/tmp") / "claude-1000" / encoded / session_id / "tasks"
        task_dir.mkdir(parents=True, exist_ok=True)
        task_file = task_dir / "task-1.output"
        task_file.write_text("background command output\n")

        try:
            with (
                patch("lanegate.orchestrate.Path.home", return_value=fake_home),
                patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")),
                patch("lanegate.orchestrate.time.time", return_value=deterministic_time),
                patch("os.getpid", return_value=deterministic_pid),
            ):
                exit_code, *_ = invoke_executor(ticket, cfg, tmp_path, repo_root=tmp_path)
                assert exit_code == 0
        finally:
            task_file.unlink(missing_ok=True)

        # With concurrent executors, status is now in per-session files.
        # Read from the per-session status file or find the audit bundle directly.
        status_dir = tmp_path / ".lanegate" / "active-orchestrate"
        status_files = list(status_dir.glob("*.json"))
        assert len(status_files) > 0, f"No status files found in {status_dir}"
        status = json.loads(status_files[0].read_text())

        # The audit_bundle_path is stored in the status
        assert "audit_bundle_path" in status, f"Status has no audit_bundle_path: {status}"
        bundle = Path(status["audit_bundle_path"])
        manifest = json.loads((bundle / "manifest.json").read_text())

        assert bundle.parent.parent.name == "executor-runs"
        assert bundle.parent.parent.parent.name == ".lanegate"
        assert (bundle / "prompt.md").exists()
        assert (bundle / "status.json").exists()
        assert (bundle / "executor-session.jsonl").read_text() == '{\n  "type": "message"\n}\n'
        assert (bundle / "tasks" / "task-1.output").read_text() == "background command output\n"
        assert (bundle / "git-status.txt").exists()
        assert (bundle / "diff-stat.txt").exists()
        assert "prompt.md" in manifest["captured"]
        assert "status.json" in manifest["captured"]
        assert "tasks" in manifest["captured"]
        assert any(item["artifact"] == "gates" for item in manifest["missing"])

    def test_find_claude_transcript_ignores_unrelated_concurrent_session(self, tmp_path):
        """F48 regression: a concurrent interactive Claude session's .jsonl in the
        same project dir must never be picked up as this run's transcript, even
        though its mtime falls inside the old start/finish time window."""
        import time

        fake_home = tmp_path / "home"
        encoded = _claude_encoded_cwd(tmp_path)
        transcript_dir = fake_home / ".claude" / "projects" / encoded
        transcript_dir.mkdir(parents=True)

        our_session_id = "TICK-148-1700000000-12345-implement"
        (transcript_dir / f"{our_session_id}.jsonl").write_text('{"type":"ours"}\n')
        # An unrelated concurrent session, written at the same moment.
        (transcript_dir / "unrelated-session-abc.jsonl").write_text('{"type":"unrelated"}\n')

        status = {
            "executor_session": our_session_id,
            "started_at": time.time(),
            "finished_at": time.time(),
        }
        with patch("lanegate.orchestrate.Path.home", return_value=fake_home):
            path, reason = _find_claude_transcript(tmp_path, status)

        assert path is not None
        assert path.name == f"{our_session_id}.jsonl"
        assert reason == ""

    def test_find_claude_transcript_no_fallback_when_session_id_unmatched(self, tmp_path):
        """F48 regression: if no transcript matches the recorded session_id, the
        function must report failure rather than falling back to 'most recently
        modified .jsonl in the window', which could be an unrelated session."""
        import time

        fake_home = tmp_path / "home"
        encoded = _claude_encoded_cwd(tmp_path)
        transcript_dir = fake_home / ".claude" / "projects" / encoded
        transcript_dir.mkdir(parents=True)

        # Only an unrelated session's transcript exists, freshly written.
        (transcript_dir / "unrelated-session-abc.jsonl").write_text('{"type":"unrelated"}\n')

        status = {
            "executor_session": "TICK-148-1700000000-12345-implement",
            "started_at": time.time(),
            "finished_at": time.time(),
        }
        with patch("lanegate.orchestrate.Path.home", return_value=fake_home):
            path, reason = _find_claude_transcript(tmp_path, status)

        assert path is None
        assert "TICK-148-1700000000-12345-implement" in reason

    def test_find_claude_transcript_no_session_id_recorded(self, tmp_path):
        """F48 regression: with no executor_session recorded at all, must not fall
        back to guessing by recency — that was the original misattribution bug."""
        fake_home = tmp_path / "home"
        encoded = _claude_encoded_cwd(tmp_path)
        transcript_dir = fake_home / ".claude" / "projects" / encoded
        transcript_dir.mkdir(parents=True)
        (transcript_dir / "some-other-session.jsonl").write_text('{"type":"other"}\n')

        status: dict = {}
        with patch("lanegate.orchestrate.Path.home", return_value=fake_home):
            path, reason = _find_claude_transcript(tmp_path, status)

        assert path is None
        assert "no executor_session recorded" in reason

    def test_codex_audit_bundle_captures_matching_rollout_transcript(self, tmp_path):
        import datetime
        import time

        ticket = {
            "id": "TICK-149",
            "title": "Codex audit capture",
            "touches": ["a.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "codex"
        fake_home = tmp_path / "home"
        today = datetime.datetime.fromtimestamp(time.time())
        codex_dir = (
            fake_home
            / ".codex"
            / "sessions"
            / f"{today.year:04d}"
            / f"{today.month:02d}"
            / f"{today.day:02d}"
        )
        codex_dir.mkdir(parents=True)
        (codex_dir / "rollout-test.jsonl").write_text('{"event":"codex"}\n')

        with (
            patch("lanegate.orchestrate.Path.home", return_value=fake_home),
            patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")),
        ):
            exit_code, *_ = invoke_executor(ticket, cfg, tmp_path, repo_root=tmp_path)
            assert exit_code == 0

        # With concurrent executors, status is now in per-session files.
        # Read from the per-session status file or find the audit bundle directly.
        status_dir = tmp_path / ".lanegate" / "active-orchestrate"
        status_files = list(status_dir.glob("*.json"))
        assert len(status_files) > 0, f"No status files found in {status_dir}"
        status = json.loads(status_files[0].read_text())

        # The audit_bundle_path is stored in the status
        assert "audit_bundle_path" in status, f"Status has no audit_bundle_path: {status}"
        bundle = Path(status["audit_bundle_path"])
        manifest = json.loads((bundle / "manifest.json").read_text())
        assert (bundle / "executor-session.jsonl").read_text() == '{\n  "event": "codex"\n}\n'
        assert "executor-session.jsonl" in manifest["captured"]

    def test_audit_bundle_missing_private_logs_is_manifested_without_failure(self, tmp_path):
        ticket = {
            "id": "TICK-150",
            "title": "Missing private logs",
            "touches": ["a.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude-process"
        fake_home = tmp_path / "empty-home"
        fake_home.mkdir()

        with (
            patch("lanegate.orchestrate.Path.home", return_value=fake_home),
            patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")),
        ):
            exit_code, *_ = invoke_executor(ticket, cfg, tmp_path, repo_root=tmp_path)
            assert exit_code == 0

        # With concurrent executors, status is now in per-session files.
        # Read from the per-session status file or find the audit bundle directly.
        status_dir = tmp_path / ".lanegate" / "active-orchestrate"
        status_files = list(status_dir.glob("*.json"))
        assert len(status_files) > 0, f"No status files found in {status_dir}"
        status = json.loads(status_files[0].read_text())

        # The audit_bundle_path is stored in the status
        assert "audit_bundle_path" in status, f"Status has no audit_bundle_path: {status}"
        bundle = Path(status["audit_bundle_path"])
        manifest = json.loads((bundle / "manifest.json").read_text())
        missing = {item["artifact"]: item["reason"] for item in manifest["missing"]}
        assert "executor-session.jsonl" in missing
        assert "tasks" in missing
        assert (bundle / "prompt.md").exists()

    def test_audit_bundle_captures_raw_output_on_nonzero_exit(self, tmp_path):
        """TICK-256: the raw stdout+stderr tail must land in the audit bundle
        for ANY non-zero exit, not just ones already classified as a rate
        limit — so a future misclassification is diagnosable from the bundle
        alone instead of requiring a live rerun."""
        ticket = {
            "id": "TICK-151",
            "title": "Nonzero exit capture",
            "touches": ["a.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude-process"

        with patch(
            "lanegate.orchestrate.pool._stream_subprocess",
            return_value=(1, "stdout: usage limit hit", "stderr: some detail"),
        ):
            exit_code, *_ = invoke_executor(ticket, cfg, tmp_path, repo_root=tmp_path)
            assert exit_code == 1

        status_dir = tmp_path / ".lanegate" / "active-orchestrate"
        status_files = list(status_dir.glob("*.json"))
        status = json.loads(status_files[0].read_text())
        bundle = Path(status["audit_bundle_path"])
        manifest = json.loads((bundle / "manifest.json").read_text())

        captured_path = bundle / "captured-output.txt"
        assert captured_path.exists()
        text = captured_path.read_text()
        assert "stdout: usage limit hit" in text
        assert "stderr: some detail" in text
        assert "captured-output.txt" in manifest["captured"]

    def test_audit_bundle_skips_raw_output_capture_on_zero_exit(self, tmp_path):
        """No captured-output.txt (and no misleading manifest entry) when the
        executor exits 0 — this artifact exists to diagnose failures."""
        ticket = {
            "id": "TICK-152",
            "title": "Zero exit skip capture",
            "touches": ["a.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude-process"

        with patch(
            "lanegate.orchestrate.pool._stream_subprocess",
            return_value=(0, "normal stdout", ""),
        ):
            exit_code, *_ = invoke_executor(ticket, cfg, tmp_path, repo_root=tmp_path)
            assert exit_code == 0

        status_dir = tmp_path / ".lanegate" / "active-orchestrate"
        status_files = list(status_dir.glob("*.json"))
        status = json.loads(status_files[0].read_text())
        bundle = Path(status["audit_bundle_path"])
        manifest = json.loads((bundle / "manifest.json").read_text())

        assert not (bundle / "captured-output.txt").exists()
        assert "captured-output.txt" not in manifest["captured"]

    def test_config_error_returns_sentinel_exit_code_not_raise(self, tmp_path):
        """Regression (TICK-088 second review round): resolve_executor_env
        raising ConfigError (e.g. a named instance's api_key_env pointing at
        an unset var, or a type with no known key-injection target) must not
        propagate out of invoke_executor. invoke_executor has no try/except
        of its own at any of its call sites — the main implement dispatch in
        _drain_loop, and run_fix_agent's fix-pass dispatch — so an uncaught
        exception here would crash the whole orchestrate run over one
        ticket's bad config. It must instead come back as an ordinary
        nonzero exit code that those callers already know how to handle."""
        ticket = {
            "id": "TICK-008",
            "title": "Bad named-executor config",
            "touches": ["a.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "gemini-1"
        cfg["executors"] = {
            "gemini-1": {"type": "gemini", "api_key_env": "SOME_UNSET_GEMINI_KEY"},
        }

        with patch("lanegate.orchestrate.pool._stream_subprocess") as mock_stream:
            exit_code, *_ = invoke_executor(ticket, cfg, tmp_path)

        assert exit_code != 0
        assert exit_code != 429  # must not be mistaken for a rate limit
        mock_stream.assert_not_called()

    def test_named_driver_env_overlay_reaches_subprocess(self, tmp_path, monkeypatch):
        ticket = {
            "id": "TICK-011",
            "title": "Driver env",
            "touches": ["a.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)
        cfg["drivers"] = {
            "env-driver": {
                "type": "claude-process",
                "env": {
                    "DRIVER_TOKEN": "${SOURCE_TOKEN}",
                    "DRIVER_LITERAL": "literal",
                },
            }
        }
        cfg["steps"] = {"implement": {"driver": "env-driver"}}
        monkeypatch.setenv("SOURCE_TOKEN", "expanded-token")
        monkeypatch.setenv("PARENT_VALUE", "kept")

        with patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")) as mock_stream:
            exit_code, *_ = invoke_executor(ticket, cfg, tmp_path)
            assert exit_code == 0

        env = mock_stream.call_args.kwargs["env"]
        assert env["PARENT_VALUE"] == "kept"
        assert env["DRIVER_TOKEN"] == "expanded-token"
        assert env["DRIVER_LITERAL"] == "literal"

    def test_malformed_driver_env_returns_sentinel_exit_code_not_raise(self, tmp_path):
        ticket = {
            "id": "TICK-013",
            "title": "Malformed driver env",
            "touches": ["a.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)
        cfg["drivers"] = {
            "bad-env": {
                "type": "claude-process",
                "env": ["BAD"],
            }
        }
        cfg["steps"] = {"implement": {"driver": "bad-env"}}

        with patch("lanegate.orchestrate.pool._stream_subprocess") as mock_stream:
            exit_code, *_ = invoke_executor(ticket, cfg, tmp_path)

        assert exit_code != 0
        assert exit_code != 429
        mock_stream.assert_not_called()

    def test_missing_executor_bin_returns_sentinel_exit_code_not_raise(self, tmp_path, monkeypatch):
        ticket = {
            "id": "TICK-015",
            "title": "Missing executor binary",
            "touches": ["a.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude-process"
        monkeypatch.setattr("lanegate.executor.shutil.which", lambda _bin_name: None)

        with patch("lanegate.orchestrate.pool._stream_subprocess") as mock_stream:
            exit_code, *_ = invoke_executor(ticket, cfg, tmp_path)

        assert exit_code != 0
        assert exit_code != 429
        mock_stream.assert_not_called()

    def test_named_driver_model_and_bin_reach_executor_command(self, tmp_path):
        ticket = {
            "id": "TICK-012",
            "title": "Driver model",
            "touches": ["a.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)
        cfg["drivers"] = {
            "fast": {
                "type": "claude-process",
                "model": "claude-driver-model",
                "bin": "custom-claude",
                "flags": ["--debug-driver"],
            }
        }
        cfg["steps"] = {"implement": {"driver": "fast"}}

        with patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")) as mock_stream:
            exit_code, *_ = invoke_executor(ticket, cfg, tmp_path)
            assert exit_code == 0

        cmd = mock_stream.call_args.args[0]
        assert cmd[0] == "custom-claude"
        assert "--debug-driver" in cmd
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "claude-driver-model"

    def test_legacy_executor_steps_fix_routes_fix_invocation(self, tmp_path):
        ticket = {
            "id": "TICK-014",
            "title": "Fix route",
            "touches": ["a.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor_steps"] = {"fix": "codex"}

        with patch("lanegate.orchestrate.pool._stream_subprocess", return_value=(0, "", "")) as mock_stream:
            exit_code, *_ = invoke_executor(
                ticket,
                cfg,
                tmp_path,
                prompt_override="fix prompt",
                step="fix",
            )
            assert exit_code == 0

        cmd = mock_stream.call_args.args[0]
        assert cmd[0] == "codex"
        assert mock_stream.call_args.kwargs["stdin_text"] == "fix prompt"

    def test_ollama_executor_posts_to_generate_api_writes_response(self, tmp_path):
        """Test that ollama executor posts to /api/generate and writes response.

        Uses step="analyze" — the only step raw ollama is still permitted to
        run (reject_ollama_for_code_step rejects implement/review/fix/drift_check).
        """
        # requests is an optional extra (`lanegate[ollama]`); the executor falls back
        # to curl without it, which the sibling test below covers.
        pytest.importorskip("requests")
        ticket = {
            "id": "TICK-015",
            "title": "Ollama task",
            "touches": ["g.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "ollama"

        mock_response = MagicMock()
        mock_response.json.return_value = {"response": "Generated text from ollama"}

        with patch("lanegate.orchestrate.pool._stream_subprocess") as mock_stream:
            with patch("requests.post", return_value=mock_response):
                exit_code, *_ = invoke_executor(ticket, cfg, tmp_path, step="analyze")

        assert exit_code == 0
        response_file = tmp_path / ".ollama_response.md"
        assert response_file.exists()
        assert response_file.read_text() == "Generated text from ollama"
        # Verify _stream_subprocess was not called for ollama
        mock_stream.assert_not_called()

    def test_ollama_executor_falls_back_to_curl_when_requests_missing(self, tmp_path):
        """Test that ollama falls back to curl when requests is unavailable."""
        from lanegate.orchestrate import _invoke_ollama

        # Test the _invoke_ollama function directly to test curl fallback
        # by mocking requests to raise ImportError
        with patch("subprocess.run") as mock_curl:
            # Configure the mock to return a JSON response
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = '{"response": "Curl fallback response"}'
            mock_curl.return_value = mock_result

            # Mock the requests module to raise ImportError
            import sys
            requests_backup = sys.modules.get("requests")
            try:
                sys.modules["requests"] = None
                exit_code = _invoke_ollama("test prompt", {}, tmp_path)
            finally:
                if requests_backup is not None:
                    sys.modules["requests"] = requests_backup
                else:
                    sys.modules.pop("requests", None)

        assert exit_code == 0
        response_file = tmp_path / ".ollama_response.md"
        assert response_file.exists()
        assert response_file.read_text() == "Curl fallback response"
        # Verify curl was called with the right arguments
        assert mock_curl.called
        curl_cmd = mock_curl.call_args[0][0]
        assert curl_cmd[0] == "curl"
        assert "-X" in curl_cmd
        assert "POST" in curl_cmd
        # Check that the URL is in the curl command
        assert any("localhost:11434/api/generate" in arg for arg in curl_cmd)


# executor pools (TICK-089)
# ---------------------------------------------------------------------------


def _pool_dispatch_scenario(tmp_path, *, pool_strategy: str, max_parallel: int = 2, executors: list[str] | None = None):
    """Shared harness for pool-dispatch tests: 2 disjoint-touch open tickets,
    a 2-instance pool, everything past invoke_executor faked through to
    merged so the run drains cleanly. Returns {ticket_id: executor_override
    actually passed to invoke_executor} — the pool selection is purely a
    dispatch-time concern (see the note on pool_assignment in _drain_loop),
    never written to the ticket's own frontmatter, so tests read it off the
    invoke_executor call rather than off the ticket file.
    """
    cfg = _default_cfg(tmp_path)
    cfg["max_parallel"] = max_parallel
    cfg["executors"] = {
        "claude-1": {"type": "claude-process"},
        "claude-2": {"type": "claude-process"},
    }
    if executors is None:
        cfg["pools"] = {"default": {"executors": ["claude-1", "claude-2"], "strategy": pool_strategy}}
    tickets_dir = tmp_path / "tickets"
    _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"], priority=1)
    _write_ticket(tickets_dir, "TICK-002", "open", touches=["b.py"], priority=2)

    used: dict[str, str | None] = {}

    def fake_invoke(ticket, cfg_, wt, **kwargs):
        used[ticket["id"]] = kwargs.get("executor_override")
        return 0, "", ""

    def fake_complete(tid, cfg_, repo_root):
        p = tickets_dir / f"{tid}.md"
        p.write_text(p.read_text().replace("status: in_progress", "status: code_complete", 1))

    def fake_review(tid, cfg_, repo_root, *, verdict=None, summary=None, findings=None):
        p = tickets_dir / f"{tid}.md"
        p.write_text(p.read_text().replace("status: code_complete", "status: in_review", 1))

    def fake_merge(tid, cfg_, repo_root):
        p = tickets_dir / f"{tid}.md"
        p.write_text(p.read_text().replace("status: in_review", "status: merged", 1))

    with (
        patch("lanegate.lifecycle.cmd_start", side_effect=_fake_start_writes_in_progress),
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
            tmp_path,
            max_parallel=max_parallel,
            human_review="none",
            all_milestones=True,
            auto_analyze=False,
            pool=None if executors else "default",
            executors=executors,
        )

    return used


class TestExecutorPools:
    def test_round_robin_distributes_across_instances(self, tmp_path):
        used = _pool_dispatch_scenario(tmp_path, pool_strategy="round-robin")
        assert used == {"TICK-001": "claude-1", "TICK-002": "claude-2"}

    def test_least_loaded_splits_two_tickets_across_two_instances(self, tmp_path):
        used = _pool_dispatch_scenario(tmp_path, pool_strategy="least-loaded")
        assert set(used.values()) == {"claude-1", "claude-2"}

    def test_least_loaded_splits_across_adhoc_executors_flag(self, tmp_path):
        used = _pool_dispatch_scenario(tmp_path, pool_strategy="least-loaded", executors=["claude-1", "claude-2"])
        assert set(used.values()) == {"claude-1", "claude-2"}

    def test_executors_and_pool_mutually_exclusive(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        cfg["executors"] = {"claude-1": {"type": "claude-process"}}
        cfg["pools"] = {"default": {"executors": ["claude-1"], "strategy": "least-loaded"}}
        with pytest.raises(SystemExit) as exc_info:
            cmd_orchestrate(
                cfg,
                tmp_path,
                pool="default",
                executors=["claude-1"],
            )
        assert exc_info.value.code == 1

    def test_executors_unknown_name_rejected(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        cfg["executors"] = {"claude-1": {"type": "claude-process"}}
        with pytest.raises(SystemExit) as exc_info:
            cmd_orchestrate(
                cfg,
                tmp_path,
                executors=["doesnotexist"],
            )
        assert exc_info.value.code == 1


class TestResolvedDispatchDisplay:
    def test_ticket_model_pin_applies_to_fix_dispatch(self, tmp_path):
        """An operator-selected implementation model also governs auto-fix."""
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude"
        cfg["models"] = {"fix": "claude-sonnet-5"}

        dispatch = resolve_dispatch(
            {"id": "TICK-999", "touches": [], "model": "claude-opus-5"},
            cfg,
            step="fix",
        )

        assert dispatch["model"] == "claude-opus-5"

    def test_pool_dispatch_rejects_vendor_model_leaked_into_ollama_aider(self, tmp_path):
        """A top-level `models:` block authored for cfg's own default executor
        must not silently reach a pool-dispatched aider instance pinned to
        provider: ollama, which cannot use a claude-* model at all. This must
        fail loudly via ConfigError at dispatch time, not silently pass the
        wrong model through -- mirrors the live pools.default.executors
        aider-ollama-14b misconfiguration this ticket fixes."""
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude"
        cfg["models"] = {"implement": "claude-sonnet-5"}
        cfg["executors"] = {
            "aider-ollama-14b": {"type": "aider", "provider": "ollama"},
        }
        ticket = {"id": "TICK-999", "touches": []}

        with pytest.raises(ConfigError, match="unmapped model"):
            resolve_dispatch(ticket, cfg, executor_override="aider-ollama-14b")

    def test_pool_dispatch_rejects_vendor_model_leaked_into_ollama_aider_via_drivers_block(
        self, tmp_path
    ):
        """Same leak as the `executors:` case above, but the ollama-pinned
        aider route is declared as a `drivers:` entry instead -- provider
        lives on driver_cfg here, not on an `executors:` instance, and must
        still be found."""
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude"
        cfg["models"] = {"implement": "claude-sonnet-5"}
        cfg["drivers"] = {
            "fast-ollama": {"type": "aider", "provider": "ollama"},
        }
        cfg["steps"] = {"implement": {"driver": "fast-ollama"}}
        ticket = {"id": "TICK-998", "touches": []}

        with pytest.raises(ConfigError, match="unmapped model"):
            resolve_dispatch(ticket, cfg)

    def test_named_driver_lifecycle_and_api_fields(self, tmp_path):
        import io

        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude-process"
        cfg["steps"] = {"implement": {"driver": "codex-implement"}}
        cfg["drivers"] = {"codex-implement": {"type": "codex", "model": "gpt-5.6-terra"}}
        ticket = {"id": "TICK-302", "touches": ["lanegate/executor.py"]}

        dispatch = resolve_dispatch(ticket, cfg)
        out = io.StringIO()
        write_executing_status(ticket["id"], dispatch, out)

        assert "[executing]  route=codex-implement executor=codex model=gpt-5.6-terra" in out.getvalue()
        assert {
            key: dispatch[key]
            for key in ("resolved_driver", "resolved_executor", "resolved_model")
        } == {
            "resolved_driver": "codex-implement",
            "resolved_executor": "codex",
            "resolved_model": "gpt-5.6-terra",
        }

    def test_pool_instance_lifecycle_and_api_fields(self, tmp_path):
        import io

        cfg = _default_cfg(tmp_path)
        cfg["steps"] = {"implement": {"driver": "agy-implement"}}
        cfg["drivers"] = {"agy-implement": {"type": "agy"}}
        cfg["executors"] = {
            "codex-a": {"type": "codex", "models": {"implement": "gpt-5.6-terra"}}
        }
        ticket = {"id": "TICK-303", "touches": ["lanegate/executor.py"]}

        dispatch = resolve_dispatch(ticket, cfg, executor_override="codex-a")
        out = io.StringIO()
        write_executing_status(ticket["id"], dispatch, out)

        assert (
            "[executing]  route=agy-implement executor=codex-a model=gpt-5.6-terra"
            in out.getvalue()
        )
        assert dispatch["resolved_executor"] == "codex-a"
        assert dispatch["resolved_driver"] == "agy-implement"

        active_status = {
            "active": True,
            "ticket_id": ticket["id"],
            "executor_pid": 1234,
            "state": "running",
            "reconciliation_state": "live",
            "resolved_driver": dispatch["resolved_driver"],
            "resolved_executor": dispatch["resolved_executor"],
            "resolved_model": dispatch["resolved_model"],
            "orchestrator_lock": {"pid": 4321},
        }
        with patch("lanegate.orchestrate.get_orchestration_status", return_value=active_status):
            payload = _run_payload(tmp_path, {}, None)

        assert payload["workers"][0]["resolved_executor"] == "codex-a"
        assert payload["orchestration"]["resolved_model"] == "gpt-5.6-terra"

    def test_executing_status_is_suitable_for_verbose_and_compact_output(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        cfg["steps"] = {"implement": {"driver": "codex-implement"}}
        cfg["drivers"] = {"codex-implement": {"type": "codex", "model": "gpt-5.6-terra"}}

        dispatch = resolve_dispatch({"id": "TICK-302", "touches": []}, cfg)

        # _drain_loop writes this line before its verbose/compact branch, so the
        # same resolved route is visible in either output mode.
        assert format_resolved_dispatch(dispatch) == (
            "route=codex-implement executor=codex model=gpt-5.6-terra"
        )
        assert format_resolved_dispatch({}) == "route=unknown executor=unknown model=unknown"

    def test_pool_selection_respects_instance_max_parallel(self, tmp_path):
        """TICK-286: with the orchestrator-wide --max (4) raised above
        claude-1's own cap (1), least-loaded selection must not pile a
        second concurrent ticket onto claude-1 while claude-2 (cap 3) still
        has room — regression test for _select_pool_instance ignoring each
        candidate's own executors[name].max_parallel."""
        cfg = _default_cfg(tmp_path)
        cfg["max_parallel"] = 4
        cfg["executors"] = {
            "claude-1": {"type": "claude-process", "max_parallel": 1},
            "claude-2": {"type": "claude-process", "max_parallel": 3},
        }
        cfg["pools"] = {
            "default": {"executors": ["claude-1", "claude-2"], "strategy": "least-loaded"}
        }
        tickets_dir = tmp_path / "tickets"
        for i, touch in enumerate(["a.py", "b.py", "c.py", "d.py"], start=1):
            _write_ticket(tickets_dir, f"TICK-00{i}", "open", touches=[touch], priority=i)

        used: dict[str, str | None] = {}

        def fake_invoke(ticket, cfg_, wt, **kwargs):
            used[ticket["id"]] = kwargs.get("executor_override")
            return 0, "", ""

        def fake_complete(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_progress", "status: code_complete", 1))

        def fake_review(tid, cfg_, repo_root, *, verdict=None, summary=None, findings=None):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: code_complete", "status: in_review", 1))

        def fake_merge(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_review", "status: merged", 1))

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=_fake_start_writes_in_progress),
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
                tmp_path,
                max_parallel=4,
                human_review="none",
                all_milestones=True,
                auto_analyze=False,
                pool="default",
            )

        assert len(used) == 4
        counts: dict[str, int] = {}
        for instance in used.values():
            counts[instance] = counts.get(instance, 0) + 1
        assert counts.get("claude-1", 0) <= 1
        assert counts.get("claude-2", 0) <= 3

    def test_pool_selection_refuses_overloaded_instance(self, tmp_path):
        """When every instance in the pool is already at its own
        max_parallel cap, selection still dispatches (last-resort fallback,
        same as the pre-existing rate-limit fallback) rather than hanging —
        but only once no instance has spare capacity."""
        cfg = _default_cfg(tmp_path)
        cfg["max_parallel"] = 3
        cfg["executors"] = {
            "claude-1": {"type": "claude-process", "max_parallel": 1},
            "claude-2": {"type": "claude-process", "max_parallel": 1},
        }
        cfg["pools"] = {
            "default": {"executors": ["claude-1", "claude-2"], "strategy": "least-loaded"}
        }
        tickets_dir = tmp_path / "tickets"
        for i, touch in enumerate(["a.py", "b.py", "c.py"], start=1):
            _write_ticket(tickets_dir, f"TICK-00{i}", "open", touches=[touch], priority=i)

        used: dict[str, str | None] = {}

        def fake_invoke(ticket, cfg_, wt, **kwargs):
            used[ticket["id"]] = kwargs.get("executor_override")
            return 0, "", ""

        def fake_complete(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_progress", "status: code_complete", 1))

        def fake_review(tid, cfg_, repo_root, *, verdict=None, summary=None, findings=None):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: code_complete", "status: in_review", 1))

        def fake_merge(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_review", "status: merged", 1))

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=_fake_start_writes_in_progress),
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
                tmp_path,
                max_parallel=3,
                human_review="none",
                all_milestones=True,
                auto_analyze=False,
                pool="default",
            )

        # Both instances have a cap of 1; the 3rd ticket has nowhere to go
        # without overloading someone, so it dispatches anyway (fallback)
        # rather than never running.
        assert len(used) == 3
        assert set(used.values()) == {"claude-1", "claude-2"}

    def test_pool_skips_instance_currently_rate_limited(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        cfg["executors"] = {
            "claude-1": {"type": "claude-process"},
            "claude-2": {"type": "claude-process"},
        }
        cfg["pools"] = {
            "default": {"executors": ["claude-1", "claude-2"], "strategy": "round-robin"}
        }
        tickets_dir = tmp_path / "tickets"
        # A prior ticket left hibernated because claude-1 hit a rate limit.
        # The instance name is recorded in the hibernation body (not
        # ticket.executor — see _pool_instance_healthy's docstring for why).
        stuck = tickets_dir / "TICK-900.md"
        stuck.write_text(
            "---\n"
            "id: TICK-900\n"
            "title: Test TICK-900\n"
            "status: hibernated\n"
            f"status_changed_at: {datetime.datetime.now(datetime.UTC).isoformat()}\n"
            "milestone: old\n"
            "priority: 1\n"
            "parallel_safe: true\n"
            "close_criteria: All tests pass.\n"
            "---\n"
            "## Hibernation Reason\n\n"
            "rate limit or quota interruption (executor exited 1)\n\n"
            "pool instance: claude-1\n"
        )
        fresh = _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"], priority=1)
        fresh.write_text(fresh.read_text().replace("priority: 1\n", "priority: 1\nmilestone: active\n"))

        used: dict[str, str | None] = {}

        def fake_invoke(ticket, cfg_, wt, **kwargs):
            used[ticket["id"]] = kwargs.get("executor_override")
            return 0, "", ""

        def fake_complete(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_progress", "status: code_complete", 1))

        def fake_review(tid, cfg_, repo_root, *, verdict=None, summary=None, findings=None):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: code_complete", "status: in_review", 1))

        def fake_merge(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_review", "status: merged", 1))

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=_fake_start_writes_in_progress),
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
                tmp_path,
                max_parallel=1,
                human_review="none",
                milestone="active",
                auto_analyze=False,
                pool="default",
            )

        assert used["TICK-001"] == "claude-2"

    def test_pool_does_not_skip_non_retryable_error_hibernation(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        cfg["executors"] = {
            "codex": {"type": "codex"},
            "claude-1": {"type": "claude-process"},
        }
        cfg["pools"] = {
            "default": {"executors": ["codex", "claude-1"], "strategy": "round-robin"}
        }
        tickets_dir = tmp_path / "tickets"
        stuck = tickets_dir / "TICK-900.md"
        stuck.write_text(
            "---\n"
            "id: TICK-900\n"
            "title: Test TICK-900\n"
            "status: hibernated\n"
            "milestone: old\n"
            "priority: 1\n"
            "parallel_safe: true\n"
            "close_criteria: All tests pass.\n"
            "---\n"
            "## Hibernation Reason\n\n"
            "rate limit or quota interruption (executor exited 1)\n\n"
            "Raw executor output:\n"
            "You've hit your session limit · resets 4:40pm (America/Los_Angeles)\n"
            "ERROR: {\"status\":400,\"error\":{\"type\":\"invalid_request_error\","
            "\"message\":\"requires a newer version of Codex\"}}\n\n"
            "pool instance: codex\n"
        )
        fresh = _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"], priority=1)
        fresh.write_text(fresh.read_text().replace("priority: 1\n", "priority: 1\nmilestone: active\n"))

        used: dict[str, str | None] = {}

        def fake_invoke(ticket, cfg_, wt, **kwargs):
            used[ticket["id"]] = kwargs.get("executor_override")
            return 0, "", ""

        def fake_complete(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_progress", "status: code_complete", 1))

        def fake_review(tid, cfg_, repo_root, *, verdict=None, summary=None, findings=None):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: code_complete", "status: in_review", 1))

        def fake_merge(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_review", "status: merged", 1))

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=_fake_start_writes_in_progress),
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
                tmp_path,
                max_parallel=1,
                human_review="none",
                milestone="active",
                auto_analyze=False,
                pool="default",
            )

        assert used["TICK-001"] == "codex"

    @pytest.mark.parametrize("max_parallel", [1, 2])
    def test_explicit_ticket_executor_override_is_not_replaced_by_pool(self, tmp_path, max_parallel):
        """ticket.executor, when a user sets it directly, must win over pool
        selection. Uses a plain built-in driver type ('codex') rather than a
        named pool instance as the override value — a named-instance value
        here would itself trip the pre-existing validate_ticket gap tracked
        as TICK-247 (unrelated to pools; see that ticket for detail), which
        would quarantine the ticket before dispatch ever ran.
        """
        cfg = _default_cfg(tmp_path)
        cfg["executors"] = {
            "claude-1": {"type": "claude-process"},
            "claude-2": {"type": "claude-process"},
        }
        cfg["pools"] = {
            "default": {"executors": ["claude-1", "claude-2"], "strategy": "round-robin"}
        }
        tickets_dir = tmp_path / "tickets"
        p = _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"], priority=1)
        p.write_text(p.read_text().replace("priority: 1\n", "priority: 1\nexecutor: codex\n"))

        used: dict[str, str | None] = {}

        def fake_invoke(ticket, cfg_, wt, **kwargs):
            used[ticket["id"]] = kwargs.get("executor_override")
            return 0, "", ""

        def fake_complete(tid, cfg_, repo_root):
            ticket_path = tickets_dir / f"{tid}.md"
            ticket_path.write_text(
                ticket_path.read_text().replace("status: in_progress", "status: code_complete", 1)
            )

        def fake_review(tid, cfg_, repo_root, *, verdict=None, summary=None, findings=None):
            ticket_path = tickets_dir / f"{tid}.md"
            ticket_path.write_text(
                ticket_path.read_text().replace("status: code_complete", "status: in_review", 1)
            )

        def fake_merge(tid, cfg_, repo_root):
            ticket_path = tickets_dir / f"{tid}.md"
            ticket_path.write_text(
                ticket_path.read_text().replace("status: in_review", "status: merged", 1)
            )

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=_fake_start_writes_in_progress),
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
                tmp_path,
                max_parallel=max_parallel,
                human_review="none",
                all_milestones=True,
                auto_analyze=False,
                pool="default",
            )

        # No pool instance was assigned (assign_pool_instance no-ops when the
        # ticket already carries an explicit executor) — invoke_executor's own
        # resolve_driver call then honors ticket.executor == "codex" unchanged.
        assert used["TICK-001"] is None

    def test_no_pool_configured_leaves_dispatch_unaffected(self, tmp_path):
        """Single-executor config (no pools:) continues to work unchanged."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"])

        used: dict[str, str | None] = {}

        def fake_invoke(ticket, cfg_, wt, **kwargs):
            used[ticket["id"]] = kwargs.get("executor_override")
            return 0, "", ""

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=_fake_start_writes_in_progress),
            patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=(False, None)),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._committed_files", return_value=set()),
            patch("lanegate.orchestrate._run_static_analysis", return_value=[]),
            patch("lanegate.orchestrate._run_acceptance_contract_audit", return_value=[]),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.lifecycle.cmd_complete"),
            patch("lanegate.lifecycle.cmd_review"),
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

        assert used["TICK-001"] is None

    def test_429_marks_instance_cooldown_and_reroutes_next_ticket(self, tmp_path):
        """TICK-090 close criterion: a 429/session-exhausted stderr on
        claude-1 writes .lanegate/executors/claude-1.cooldown, and does not
        halt the run — claude-2 dispatches the next ticket without any
        operator action."""
        from lanegate.executor import is_cooling_down

        cfg = _default_cfg(tmp_path)
        cfg["max_parallel"] = 1
        cfg["executors"] = {
            "claude-1": {"type": "claude-process"},
            "claude-2": {"type": "claude-process"},
        }
        cfg["pools"] = {
            "default": {"executors": ["claude-1", "claude-2"], "strategy": "round-robin"}
        }
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"], priority=1)
        _write_ticket(tickets_dir, "TICK-002", "open", touches=["b.py"], priority=2)

        used: dict[str, str | None] = {}

        def fake_invoke(ticket, cfg_, wt, **kwargs):
            used[ticket["id"]] = kwargs.get("executor_override")
            code = 1 if ticket["id"] == "TICK-001" else 0
            return code, "", ""

        def fake_is_rate_limit(exit_code, wt=None, **kwargs):
            return exit_code == 1

        def fake_hibernate(tid, cfg_, repo_root, **kwargs):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_progress", "status: hibernated", 1))

        def fake_complete(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_progress", "status: code_complete", 1))

        def fake_review(tid, cfg_, repo_root, *, verdict=None, summary=None, findings=None):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: code_complete", "status: in_review", 1))

        def fake_merge(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_review", "status: merged", 1))

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=_fake_start_writes_in_progress),
            patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke),
            patch("lanegate.orchestrate._is_rate_limit", side_effect=fake_is_rate_limit),
            patch("lanegate.lifecycle.cmd_hibernate", side_effect=fake_hibernate),
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
                tmp_path,
                max_parallel=1,
                human_review="none",
                all_milestones=True,
                auto_analyze=False,
                pool="default",
            )

        assert used["TICK-001"] == "claude-1"
        assert used["TICK-002"] == "claude-2"
        assert is_cooling_down(tmp_path, "claude-1") is True
        assert is_cooling_down(tmp_path, "claude-2") is False
        cooldown_path = tmp_path / ".lanegate" / "executors" / "claude-1.cooldown"
        assert cooldown_path.exists()

    def test_all_instances_cooling_down_waits_without_crashing(self, tmp_path):
        """When every pool instance is cooling down, the drain loop waits in
        bounded steps (calling time.sleep, not spinning a tight loop) instead
        of crashing or halting the process outright."""
        cfg = _default_cfg(tmp_path)
        cfg["max_parallel"] = 1
        cfg["executors"] = {
            "claude-1": {"type": "claude-process"},
            "claude-2": {"type": "claude-process"},
        }
        cfg["pools"] = {
            "default": {"executors": ["claude-1", "claude-2"], "strategy": "round-robin"}
        }
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"], priority=1)

        def fake_invoke(ticket, cfg_, wt, **kwargs):
            return 1, "", ""

        def fake_is_rate_limit(exit_code, wt=None, **kwargs):
            return exit_code == 1

        def fake_hibernate(tid, cfg_, repo_root, **kwargs):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_progress", "status: hibernated", 1))

        def fake_sleep(seconds):
            assert seconds == 30
            assert "status: hibernated" in (tickets_dir / "TICK-001.md").read_text()

        from lanegate.executor import write_cooldown

        # claude-2 is already cooling down before the run starts, so once
        # TICK-001 (assigned claude-1 by round-robin) also cools down, both
        # pool instances are exhausted at once.
        write_cooldown(tmp_path, "claude-2", "session_limit")

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=_fake_start_writes_in_progress),
            patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke),
            patch("lanegate.orchestrate._is_rate_limit", side_effect=fake_is_rate_limit),
            patch("lanegate.lifecycle.cmd_hibernate", side_effect=fake_hibernate),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.orchestrate.time.sleep", side_effect=fake_sleep) as mock_sleep,
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

        # Waited in bounded steps rather than raising or spinning forever.
        assert mock_sleep.called

    def test_on_rate_limit_resume_exhausted_pool_does_not_wait(self, tmp_path):
        """When resume-watch owns the retry, foreground orchestrate exits promptly."""
        cfg = _default_cfg(tmp_path)
        cfg["max_parallel"] = 1
        cfg["on_rate_limit"] = "resume"
        cfg["executors"] = {
            "claude-1": {"type": "claude-process"},
            "claude-2": {"type": "claude-process"},
        }
        cfg["pools"] = {
            "default": {"executors": ["claude-1", "claude-2"], "strategy": "round-robin"}
        }
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"], priority=1)

        def fake_invoke(ticket, cfg_, wt, **kwargs):
            return 1, "", "You've hit your session limit · resets 5:10pm"

        def fake_hibernate(tid, cfg_, repo_root, **kwargs):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_progress", "status: hibernated", 1))

        from lanegate.executor import write_cooldown

        write_cooldown(tmp_path, "claude-2", "session_limit")

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=_fake_start_writes_in_progress),
            patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke),
            patch("lanegate.orchestrate._is_rate_limit", return_value=True),
            patch(
                "lanegate.orchestrate._rate_limit_reason",
                return_value="rate limit or quota interruption (executor exited 1)",
            ),
            patch("lanegate.lifecycle.cmd_hibernate", side_effect=fake_hibernate),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.orchestrate.spawn_resume_watch_daemon") as mock_spawn,
            patch("lanegate.orchestrate.time.sleep") as mock_sleep,
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

        assert "status: hibernated" in (tickets_dir / "TICK-001.md").read_text()
        mock_spawn.assert_called_once()
        mock_sleep.assert_not_called()

    # ------------------------------------------------------------------
    # Pool state persistence across runs (TICK-268)
    # ------------------------------------------------------------------

    def _run_pool_scenario(self, tmp_path, ticket_ids, pool_strategy, *, existing_state=None):
        """Run one orchestrate pass dispatching `ticket_ids` from a 2-instance
        pool. Returns list of executor names actually used (in dispatch order)."""
        cfg = _default_cfg(tmp_path)
        cfg["max_parallel"] = 1
        cfg["executors"] = {
            "claude-1": {"type": "claude-process"},
            "claude-2": {"type": "claude-process"},
        }
        cfg["pools"] = {
            "default": {"executors": ["claude-1", "claude-2"], "strategy": pool_strategy}
        }
        tickets_dir = tmp_path / "tickets"
        for tid in ticket_ids:
            num = tid.split("-")[1]
            _write_ticket(tickets_dir, tid, "open", touches=[f"file{num}.py"], priority=1)

        if existing_state is not None:
            from lanegate.orchestrate import _pool_state_path, _write_json_atomic
            _write_json_atomic(_pool_state_path(tmp_path), existing_state)

        dispatched: list[str | None] = []

        def fake_invoke(ticket, cfg_, wt, **kwargs):
            dispatched.append(kwargs.get("executor_override"))
            return 0, "", ""

        def fake_complete(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_progress", "status: code_complete", 1))

        def fake_review(tid, cfg_, repo_root, *, verdict=None, summary=None, findings=None):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: code_complete", "status: in_review", 1))

        def fake_merge(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_review", "status: merged", 1))

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=_fake_start_writes_in_progress),
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
                tmp_path,
                max_parallel=1,
                human_review="none",
                all_milestones=True,
                auto_analyze=False,
                pool="default",
            )
        return dispatched

    def test_pool_state_file_created_and_updated(self, tmp_path):
        """State file is created after a pool run and contains expected keys."""
        from lanegate.orchestrate import _pool_state_path
        self._run_pool_scenario(tmp_path, ["TICK-001", "TICK-002"], "round-robin")
        state_path = _pool_state_path(tmp_path)
        assert state_path.exists()
        state = json.loads(state_path.read_text())
        assert "default" in state
        assert "rr_index" in state["default"]
        assert "dispatch_counts" in state["default"]
        assert state["default"]["rr_index"] == 2
        assert state["default"]["dispatch_counts"] == {"claude-1": 1, "claude-2": 1}

    def test_pool_state_round_robin_persists(self, tmp_path):
        """Round-robin rr_index continues from where the last run left off."""
        # Run 1: 3 tickets → dispatched to claude-1, claude-2, claude-1; rr_index ends at 3
        run1 = self._run_pool_scenario(
            tmp_path, ["TICK-001", "TICK-002", "TICK-003"], "round-robin"
        )
        assert run1 == ["claude-1", "claude-2", "claude-1"]

        # Run 2: rr_index=3; 3 % 2 = 1 → should start with claude-2
        run2 = self._run_pool_scenario(
            tmp_path, ["TICK-010", "TICK-011"], "round-robin"
        )
        assert run2[0] == "claude-2", (
            f"Run 2 should start with claude-2 (rr_index=3 → 3%2=1), got {run2[0]!r}"
        )

    def test_pool_state_least_loaded_persists(self, tmp_path):
        """Least-loaded respects historical dispatch counts across separate invocations."""
        # Run 1: 3 sequential tickets → claude-1 ends with 2 dispatches, claude-2 with 1
        run1 = self._run_pool_scenario(
            tmp_path, ["TICK-001", "TICK-002", "TICK-003"], "least-loaded"
        )
        assert run1 == ["claude-1", "claude-2", "claude-1"]

        # Run 2: dispatch_counts = {claude-1: 2, claude-2: 1} → claude-2 is less-loaded
        run2 = self._run_pool_scenario(
            tmp_path, ["TICK-010"], "least-loaded"
        )
        assert run2[0] == "claude-2", (
            f"Run 2 should start with claude-2 (fewer historical dispatches), got {run2[0]!r}"
        )

    def test_pool_state_persists_across_runs(self, tmp_path):
        """Main close-criteria test: both strategies continue rotation across runs,
        per-ticket executor override still takes precedence, and the state file
        is created and read by subsequent runs."""
        from lanegate.orchestrate import _pool_state_path

        # --- round-robin across runs ---
        # Run 1 uses 3 tickets so rr_index ends at 3 (odd number breaks the wrap)
        rr_run1 = self._run_pool_scenario(
            tmp_path, ["TICK-001", "TICK-002", "TICK-003"], "round-robin"
        )
        assert rr_run1 == ["claude-1", "claude-2", "claude-1"]
        state_path = _pool_state_path(tmp_path)
        assert state_path.exists(), "state file must exist after first run"
        state = json.loads(state_path.read_text())
        assert state["default"]["rr_index"] == 3

        # Run 2: rr continues from index 3 → 3%2=1 → starts with claude-2
        rr_run2 = self._run_pool_scenario(
            tmp_path, ["TICK-010", "TICK-011"], "round-robin"
        )
        assert rr_run2[0] == "claude-2", (
            "round-robin run 2 should start with claude-2 (rr_index=3)"
        )

        # --- least-loaded across runs ---
        # Seed a fresh tmp_path for the least-loaded sub-scenario
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ll_root = Path(td)
            # Set up .lanegate/tickets dir expected by _default_cfg
            (ll_root / "tickets").mkdir(parents=True, exist_ok=True)
            (ll_root / ".lanegate").mkdir(parents=True, exist_ok=True)
            # Replicate the state file pre-seeded with unequal counts
            _write_json_atomic = __import__(
                "lanegate.orchestrate", fromlist=["_write_json_atomic"]
            )._write_json_atomic
            from lanegate.orchestrate import _pool_state_path as psp
            _write_json_atomic(
                psp(ll_root),
                {"default": {"rr_index": 0, "dispatch_counts": {"claude-1": 10, "claude-2": 5}}},
            )
            ll_run = self._run_pool_scenario(ll_root, ["TICK-001"], "least-loaded")
        assert ll_run[0] == "claude-2", (
            "least-loaded should prefer claude-2 (5 prior dispatches vs 10)"
        )

        # --- per-ticket executor override takes precedence ---
        cfg = _default_cfg(tmp_path)
        cfg["executors"] = {
            "claude-1": {"type": "claude-process"},
            "claude-2": {"type": "claude-process"},
        }
        cfg["pools"] = {
            "default": {"executors": ["claude-1", "claude-2"], "strategy": "round-robin"}
        }
        tickets_dir = tmp_path / "tickets"
        override_ticket = _write_ticket(
            tickets_dir, "TICK-020", "open", touches=["override.py"], priority=1
        )
        override_ticket.write_text(
            override_ticket.read_text().replace("priority: 1\n", "priority: 1\nexecutor: codex\n")
        )
        override_dispatches: list[str | None] = []

        def fake_invoke_override(ticket, cfg_, wt, **kwargs):
            override_dispatches.append(kwargs.get("executor_override"))
            return 0, "", ""

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=_fake_start_writes_in_progress),
            patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke_override),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=(False, None)),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._committed_files", return_value=set()),
            patch("lanegate.orchestrate._run_static_analysis", return_value=[]),
            patch("lanegate.orchestrate._run_acceptance_contract_audit", return_value=[]),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.lifecycle.cmd_complete"),
            patch("lanegate.lifecycle.cmd_review"),
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(
                cfg, tmp_path, max_parallel=1, human_review="none",
                all_milestones=True, auto_analyze=False, pool="default",
            )
        assert override_dispatches == [None], (
            "per-ticket executor override must bypass pool; executor_override must be None"
        )

    def test_pool_state_single_run_unaffected(self, tmp_path):
        """Within a single run, dispatch behavior is unchanged from pre-TICK-268."""
        # No persisted state → within-run behavior identical to original code
        run1 = self._run_pool_scenario(
            tmp_path, ["TICK-001", "TICK-002"], "round-robin"
        )
        assert run1 == ["claude-1", "claude-2"]

        run1_ll = self._run_pool_scenario(
            tmp_path, ["TICK-010", "TICK-011"], "least-loaded"
        )
        # After run1 above, rr state has been saved. For least-loaded in this
        # sub-scenario (different tickets, fresh dispatch_counts from persisted
        # state after the round-robin run above), the second ticket should still
        # go to the less-loaded instance.
        assert set(run1_ll) == {"claude-1", "claude-2"}, (
            "within a run, both instances should be used for 2 tickets"
        )


# ---------------------------------------------------------------------------
# check_worktree_has_commits
# ---------------------------------------------------------------------------


class TestCheckWorktreeHasCommits:
    def test_returns_true_when_commits_exist(self, tmp_path):
        """check_worktree_has_commits returns True when git log shows output."""
        with patch("lanegate.orchestrate.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="abc1234 feat: implement thing\n",
            )
            result = check_worktree_has_commits(tmp_path)
        assert result is True

    def test_returns_false_when_no_commits(self, tmp_path):
        """check_worktree_has_commits returns False when branch is identical to main."""
        with patch("lanegate.orchestrate.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            result = check_worktree_has_commits(tmp_path)
        assert result is False

    def test_returns_false_on_git_error(self, tmp_path):
        """check_worktree_has_commits returns False (fail-closed) on git failure."""
        with patch("lanegate.orchestrate.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="")
            result = check_worktree_has_commits(tmp_path)
        assert result is False


class TestCommitWorktreeChanges:
    def test_returns_false_when_worktree_clean(self, tmp_path):
        with patch("lanegate.orchestrate.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            result = commit_worktree_changes(tmp_path, "TICK-001")

        assert result[0] is False
        assert mock_run.call_args_list[0].args[0] == ["git", "status", "--porcelain"]

    def test_empty_path_scope_does_not_stage_worktree(self, tmp_path):
        with patch("lanegate.orchestrate.subprocess.run") as mock_run:
            result = commit_worktree_changes(tmp_path, "TICK-001", paths=[])

        assert result == (False, None)
        mock_run.assert_not_called()

    def test_commits_dirty_worktree(self, tmp_path):
        calls = [
            MagicMock(returncode=0, stdout=" M a.py\n"),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=1, stdout=""),
            MagicMock(returncode=0, stdout="[tick-001 abc123] feat: implement TICK-001\n"),
        ]

        with patch("lanegate.orchestrate.subprocess.run", side_effect=calls) as mock_run:
            result = commit_worktree_changes(tmp_path, "TICK-001")

        assert result[0] is True
        assert mock_run.call_args_list[1].args[0] == [
            "git",
            "add",
            "-A",
            "--",
            ".",
            ":(exclude).lanegate/**",
        ]
        assert mock_run.call_args_list[3].args[0] == [
            "git",
            "commit",
            "-s",
            "-m",
            "feat: implement TICK-001",
        ]

    def test_excludes_lanegate_dir_from_staged_changes(self, tmp_path):
        """TICK-218: .lanegate/ (prompt files, fix-pass status) must never be
        swept onto the ticket branch by the executor-edit commit, regardless
        of whether the project's committed .gitignore covers .lanegate/ yet."""
        calls = [
            MagicMock(returncode=0, stdout=" M a.py\n"),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=1, stdout=""),
            MagicMock(returncode=0, stdout="[tick-001 abc123] feat: implement TICK-001\n"),
        ]

        with patch("lanegate.orchestrate.subprocess.run", side_effect=calls) as mock_run:
            commit_worktree_changes(tmp_path, "TICK-001")

        add_cmd = mock_run.call_args_list[1].args[0]
        assert ":(exclude).lanegate/**" in add_cmd

    def test_returns_false_and_unstages_when_drift_detected(self, tmp_path):
        calls = [
            MagicMock(returncode=0, stdout="?? scratch.py\n"),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=1, stdout=""),
            MagicMock(returncode=0),
        ]
        ticket = {"id": "TICK-001", "touches": ["lanegate/orchestrate/pool.py"]}

        with (
            patch("lanegate.orchestrate.pool.subprocess.run", side_effect=calls) as mock_run,
            patch(
                "lanegate.orchestrate.pool.check_touches_compliance",
                side_effect=SystemExit(1),
            ) as mock_compliance,
        ):
            result = commit_worktree_changes(tmp_path, "TICK-001", ticket=ticket)

        assert result == (False, None)
        mock_compliance.assert_called_once_with("TICK-001", ticket, tmp_path)
        assert mock_run.call_args_list[3].args[0] == ["git", "restore", "--staged", "."]

    def test_custom_message_overrides_default(self, tmp_path):
        """TICK-120: an explicit message param is used verbatim instead of the
        default 'feat: implement <tid>' — needed so fix-pass commits aren't
        mislabeled as the original implementation."""
        calls = [
            MagicMock(returncode=0, stdout=" M a.py\n"),
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=1, stdout=""),
            MagicMock(returncode=0, stdout="[tick-001 abc123] fix: address findings\n"),
        ]

        with patch("lanegate.orchestrate.subprocess.run", side_effect=calls) as mock_run:
            result = commit_worktree_changes(
                tmp_path, "TICK-001", message="fix: address review findings for TICK-001"
            )

        assert result[0] is True
        assert mock_run.call_args_list[3].args[0] == [
            "git",
            "commit",
            "-s",
            "-m",
            "fix: address review findings for TICK-001",
        ]

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="':' is a reserved character in Windows filenames; this fixture's literal filename cannot be created there",
    )
    def test_paths_are_treated_as_literal_pathspecs(self, tmp_path):
        """Regression test for TICK-686: commit_worktree_changes must escape
        paths as literal pathspecs so that files like ':(glob)**' are not
        interpreted as wildcards sweeping in unrelated untracked files."""
        repo = tmp_path / "repo"
        repo.mkdir()

        def git(*args):
            return subprocess.run(
                ["git", *args], cwd=repo, check=True, capture_output=True, text=True
            )

        git("init", "-b", "main")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "Test User")

        (repo / "base.txt").write_text("base")
        git("add", "base.txt")
        git("commit", "-m", "base")

        weird_file = repo / ":(glob)**"
        weird_file.write_text("modified")

        unrelated_file = repo / "unrelated.txt"
        unrelated_file.write_text("unrelated")

        result = commit_worktree_changes(repo, "TICK-686", paths=[":(glob)**"])

        assert result[0] is True

        committed_files = git("show", "--format=", "--name-only", "HEAD").stdout.splitlines()
        assert ":(glob)**" in committed_files
        assert "unrelated.txt" not in committed_files

        status = git("status", "--porcelain").stdout
        assert "?? unrelated.txt" in status


# ---------------------------------------------------------------------------
# Fail-closed on executor error
# ---------------------------------------------------------------------------


class TestFailClosedOnExecutorError:
    """Verify that a nonzero executor exit stops orchestration and marks ticket failed."""

    def _make_open_ticket(self, tmp_path: Path) -> Path:
        tickets_dir = tmp_path / "tickets"
        return _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"])

    def test_nonzero_exit_calls_cmd_fail_not_cmd_complete(self, tmp_path):
        """exit code 1 → cmd_fail called, cmd_complete NOT called."""
        cfg = _default_cfg(tmp_path)
        self._make_open_ticket(tmp_path)

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", return_value=(1, "", "")) as mock_exec,
            patch("lanegate.lifecycle.cmd_complete") as mock_complete,
            patch("lanegate.lifecycle.cmd_fail") as mock_fail,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_exec.assert_called_once()
        mock_fail.assert_called_once()
        mock_complete.assert_not_called()

    def test_nonzero_exit_does_not_call_review(self, tmp_path):
        """Orchestration must not proceed to review after an executor failure."""
        cfg = _default_cfg(tmp_path)
        self._make_open_ticket(tmp_path)

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", return_value=(2, "", "")),
            patch("lanegate.lifecycle.cmd_complete") as mock_complete,
            patch("lanegate.lifecycle.cmd_fail"),
            patch("lanegate.lifecycle.cmd_review") as mock_review,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_complete.assert_not_called()
        mock_review.assert_not_called()

    def test_nonzero_exit_passes_exit_code_as_reason(self, tmp_path):
        """cmd_fail receives a reason string mentioning the exit code."""
        cfg = _default_cfg(tmp_path)
        self._make_open_ticket(tmp_path)

        captured_reason = []

        def fake_fail(tid, cfg, repo_root, *, reason=""):
            captured_reason.append(reason)

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", return_value=(42, "", "")),
            patch("lanegate.lifecycle.cmd_fail", side_effect=fake_fail),
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        assert captured_reason, "cmd_fail was not called"
        assert "42" in captured_reason[0]

    def test_signal_exit_hibernates_and_stops_dispatch(self, tmp_path):
        """Ctrl+C-style executor exits preserve active work and stop new dispatch."""
        cfg = _default_cfg(tmp_path)
        cfg["max_parallel"] = 1
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"], priority=1)
        _write_ticket(tickets_dir, "TICK-002", "open", touches=["b.py"], priority=2)
        invoked: list[str] = []
        hibernate_reasons: list[str] = []

        def fake_invoke(ticket, cfg_, wt, **kwargs):
            invoked.append(ticket["id"])
            return -2, "turn interrupted", ""

        def fake_hibernate(tid, cfg_, repo_root, **kwargs):
            hibernate_reasons.append(kwargs.get("reason", ""))
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_progress", "status: hibernated", 1))

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=_fake_start_writes_in_progress),
            patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke),
            patch("lanegate.lifecycle.cmd_hibernate", side_effect=fake_hibernate) as mock_hibernate,
            patch("lanegate.lifecycle.cmd_fail") as mock_fail,
            patch("lanegate.orchestrate._write_executor_cooldown") as mock_cooldown,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(
                cfg,
                tmp_path,
                max_parallel=1,
                all_milestones=True,
                auto_analyze=False,
            )

        assert invoked == ["TICK-001"]
        mock_hibernate.assert_called_once()
        mock_fail.assert_not_called()
        mock_cooldown.assert_not_called()
        assert "SIGINT" in hibernate_reasons[0]
        assert "status: hibernated" in (tickets_dir / "TICK-001.md").read_text()
        assert "status: open" in (tickets_dir / "TICK-002.md").read_text()

    def test_executor_setup_error_hibernates_and_stops_dispatch(self, tmp_path):
        """Systemic executor setup errors must not fail a whole board."""
        cfg = _default_cfg(tmp_path)
        cfg["max_parallel"] = 1
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"], priority=1)
        _write_ticket(tickets_dir, "TICK-002", "open", touches=["b.py"], priority=2)
        invoked: list[str] = []
        hibernate_reasons: list[str] = []
        codex_error = (
            'ERROR: {"type":"error","status":400,'
            '"error":{"type":"invalid_request_error",'
            '"message":"The gpt-5.6-terra model requires a newer version of Codex."}}'
        )

        def fake_invoke(ticket, cfg_, wt, **kwargs):
            invoked.append(ticket["id"])
            return 1, codex_error, ""

        def fake_hibernate(tid, cfg_, repo_root, **kwargs):
            hibernate_reasons.append(kwargs.get("reason", ""))
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_progress", "status: hibernated", 1))

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=_fake_start_writes_in_progress),
            patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke),
            patch("lanegate.lifecycle.cmd_hibernate", side_effect=fake_hibernate) as mock_hibernate,
            patch("lanegate.lifecycle.cmd_fail") as mock_fail,
            patch("lanegate.orchestrate._write_executor_cooldown") as mock_cooldown,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(
                cfg,
                tmp_path,
                max_parallel=1,
                all_milestones=True,
                auto_analyze=False,
            )

        assert invoked == ["TICK-001"]
        mock_hibernate.assert_called_once()
        mock_fail.assert_not_called()
        mock_cooldown.assert_not_called()
        assert "executor setup error" in hibernate_reasons[0]
        assert "newer version of Codex" in hibernate_reasons[0]
        assert "status: hibernated" in (tickets_dir / "TICK-001.md").read_text()
        assert "status: open" in (tickets_dir / "TICK-002.md").read_text()

    def test_auth_error_hibernates_and_writes_cooldown_when_no_sibling(self, tmp_path):
        """A single-instance (no pool) executor whose OAuth session expired must
        hibernate with a re-authentication reason, cool down that instance, and
        halt the run rather than let the next queued ticket repeat the same
        60s auth timeout."""
        from lanegate.executor import is_cooling_down

        cfg = _default_cfg(tmp_path)
        cfg["max_parallel"] = 1
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"], priority=1)
        _write_ticket(tickets_dir, "TICK-002", "open", touches=["b.py"], priority=2)
        invoked: list[str] = []
        hibernate_reasons: list[str] = []
        auth_error = (
            "Authentication required. Please visit the URL to log in:\n"
            "  https://accounts.google.com/o/oauth2/auth?...\n"
            "Error: authentication timed out.\n"
        )

        def fake_invoke(ticket, cfg_, wt, **kwargs):
            invoked.append(ticket["id"])
            return 1, auth_error, ""

        def fake_hibernate(tid, cfg_, repo_root, **kwargs):
            hibernate_reasons.append(kwargs.get("reason", ""))
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_progress", "status: hibernated", 1))

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=_fake_start_writes_in_progress),
            patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke),
            patch("lanegate.lifecycle.cmd_hibernate", side_effect=fake_hibernate) as mock_hibernate,
            patch("lanegate.lifecycle.cmd_fail") as mock_fail,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(
                cfg,
                tmp_path,
                max_parallel=1,
                all_milestones=True,
                auto_analyze=False,
            )

        assert invoked == ["TICK-001"]
        mock_hibernate.assert_called_once()
        mock_fail.assert_not_called()
        assert "executor requires re-authentication" in hibernate_reasons[0]
        assert "status: hibernated" in (tickets_dir / "TICK-001.md").read_text()
        assert "status: open" in (tickets_dir / "TICK-002.md").read_text()
        assert is_cooling_down(tmp_path, "claude-process") is True

    def test_auth_error_hibernates_cooldowns_instance_and_continues_with_healthy_sibling(self, tmp_path):
        """TICK-319 close criterion: an agy OAuth device-code prompt (or its
        JSON status:ERROR envelope) on one pool instance is classified as
        'executor requires re-authentication', cools that instance down via
        write_cooldown, and the run continues — routing the next queued
        ticket to the healthy sibling instead of halting the whole batch."""
        from lanegate.executor import is_cooling_down

        cfg = _default_cfg(tmp_path)
        cfg["max_parallel"] = 1
        cfg["executors"] = {
            "claude-1": {"type": "claude-process"},
            "claude-2": {"type": "claude-process"},
        }
        cfg["pools"] = {
            "default": {"executors": ["claude-1", "claude-2"], "strategy": "round-robin"}
        }
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"], priority=1)
        _write_ticket(tickets_dir, "TICK-002", "open", touches=["b.py"], priority=2)

        used: dict[str, str | None] = {}
        agy_json_error = '{"status":"ERROR","error":"authentication failed or timed out"}'

        def fake_invoke(ticket, cfg_, wt, **kwargs):
            used[ticket["id"]] = kwargs.get("executor_override")
            if ticket["id"] == "TICK-001":
                return 1, agy_json_error, ""
            return 0, "", ""

        def fake_hibernate(tid, cfg_, repo_root, **kwargs):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_progress", "status: hibernated", 1))

        def fake_complete(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_progress", "status: code_complete", 1))

        def fake_review(tid, cfg_, repo_root, *, verdict=None, summary=None, findings=None):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: code_complete", "status: in_review", 1))

        def fake_merge(tid, cfg_, repo_root):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_review", "status: merged", 1))

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=_fake_start_writes_in_progress),
            patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke),
            patch("lanegate.lifecycle.cmd_hibernate", side_effect=fake_hibernate),
            patch("lanegate.lifecycle.cmd_fail") as mock_fail,
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
                tmp_path,
                max_parallel=1,
                human_review="none",
                all_milestones=True,
                auto_analyze=False,
                pool="default",
            )

        assert used["TICK-001"] == "claude-1"
        assert used["TICK-002"] == "claude-2"
        mock_fail.assert_not_called()
        assert is_cooling_down(tmp_path, "claude-1") is True
        assert is_cooling_down(tmp_path, "claude-2") is False
        cooldown_path = tmp_path / ".lanegate" / "executors" / "claude-1.cooldown"
        assert cooldown_path.exists()
        assert "re-authentication" in cooldown_path.read_text()
        assert "status: hibernated" in (tickets_dir / "TICK-001.md").read_text()
        # TICK-002 was dispatched to the healthy sibling instead of the run
        # halting outright — it left "open" regardless of how far the rest
        # of its pipeline (review/merge) got in this stubbed-out test.
        assert "status: open" not in (tickets_dir / "TICK-002.md").read_text()

    def test_executor_setup_error_stops_worker_pool_refill(self, tmp_path):
        """Parallel dispatch drains in-flight tickets but must not refill after setup errors."""
        cfg = _default_cfg(tmp_path)
        cfg["max_parallel"] = 2
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"], priority=1)
        _write_ticket(tickets_dir, "TICK-002", "open", touches=["b.py"], priority=2)
        _write_ticket(tickets_dir, "TICK-003", "open", touches=["c.py"], priority=3)
        invoked: list[str] = []
        codex_error = (
            'ERROR: {"type":"error","status":400,'
            '"error":{"type":"invalid_request_error",'
            '"message":"The gpt-5.6-terra model requires a newer version of Codex."}}'
        )

        def fake_invoke(ticket, cfg_, wt, **kwargs):
            invoked.append(ticket["id"])
            return 1, codex_error, ""

        def fake_hibernate(tid, cfg_, repo_root, **kwargs):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_progress", "status: hibernated", 1))

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=_fake_start_writes_in_progress),
            patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke),
            patch("lanegate.lifecycle.cmd_hibernate", side_effect=fake_hibernate),
            patch("lanegate.lifecycle.cmd_fail") as mock_fail,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(
                cfg,
                tmp_path,
                max_parallel=2,
                all_milestones=True,
                auto_analyze=False,
            )

        assert set(invoked) == {"TICK-001", "TICK-002"}
        mock_fail.assert_not_called()
        assert "status: hibernated" in (tickets_dir / "TICK-001.md").read_text()
        assert "status: hibernated" in (tickets_dir / "TICK-002.md").read_text()
        assert "status: open" in (tickets_dir / "TICK-003.md").read_text()

    def test_rate_limit_exit_hibernates_not_fail_or_complete(self, tmp_path):
        cfg = _default_cfg(tmp_path)
        self._make_open_ticket(tmp_path)

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", return_value=(1, "", "")),
            patch("lanegate.orchestrate._is_rate_limit", return_value=True),
            patch("lanegate.lifecycle.cmd_hibernate") as mock_hibernate,
            patch("lanegate.lifecycle.cmd_complete") as mock_complete,
            patch("lanegate.lifecycle.cmd_fail") as mock_fail,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_hibernate.assert_called_once()
        mock_complete.assert_not_called()
        mock_fail.assert_not_called()

    def test_is_rate_limit_detects_usage_limit_stderr(self, tmp_path):
        captured_stderr = "You've hit your usage limit. Try again at 4:29 AM."
        assert _is_rate_limit(1, tmp_path, captured_stderr=captured_stderr) is True

    def test_is_rate_limit_detects_usage_limit_stdout(self, tmp_path):
        """TICK-256: many executors (e.g. `claude -p`) write their user-facing
        error/JSON response to stdout, not stderr — detection must check both.
        """
        captured_stdout = "You've hit your usage limit. Try again at 4:29 AM."
        assert _is_rate_limit(1, tmp_path, captured_stdout=captured_stdout) is True

    def test_is_rate_limit_detects_monthly_spend_limit_captured_output(self, tmp_path):
        # Real captured executor output, frozen as a committed fixture (was
        # previously read live from this repo's own gitignored .lanegate/
        # state dir, which doesn't exist in a fresh CI checkout).
        fixtures_dir = Path(__file__).resolve().parents[1] / "fixtures/captured_output"
        captured = (fixtures_dir / "tick-346-monthly-spend-limit.txt").read_text()

        assert _is_rate_limit(1, tmp_path, captured_stdout=captured) is True

    def test_is_rate_limit_detects_weekly_limit_captured_output(self, tmp_path):
        # See fixture note above.
        fixtures_dir = Path(__file__).resolve().parents[1] / "fixtures/captured_output"
        captured = (fixtures_dir / "tick-348-weekly-limit.txt").read_text()

        assert _is_rate_limit(1, tmp_path, captured_stdout=captured) is True

    def test_is_rate_limit_detects_structured_api_error_status_429(self, tmp_path):
        captured_stdout = '{"is_error": true, "api_error_status": 429, "result": "Request failed without quota prose"}'
        assert _is_rate_limit(1, tmp_path, captured_stdout=captured_stdout) is True

    def test_is_rate_limit_detects_explicit_rate_limit_error(self, tmp_path):
        assert _is_rate_limit(1, tmp_path, captured_stderr="ERROR: rate limit hit") is True

    def test_is_auth_error_detects_authentication_required_prompt(self, tmp_path):
        stdout = (
            "Authentication required. Please visit the URL to log in:\n"
            "  https://accounts.google.com/o/oauth2/auth?...\n"
            "Waiting for authentication (timeout 60s)...\n"
        )
        assert _is_auth_error(1, tmp_path, captured_stdout=stdout) is True

    def test_is_auth_error_detects_agy_json_error_envelope(self, tmp_path):
        stdout = '{"status":"ERROR","error":"authentication failed or timed out"}'
        assert _is_auth_error(1, tmp_path, captured_stdout=stdout) is True

    def test_is_auth_error_ignores_zero_exit(self, tmp_path):
        stdout = "Authentication required. Please visit the URL to log in:\n..."
        assert _is_auth_error(0, tmp_path, captured_stdout=stdout) is False

    def test_is_auth_error_ignores_interrupted_exit(self, tmp_path):
        stdout = "Authentication required. Please visit the URL to log in:\n..."
        assert _is_auth_error(-2, tmp_path, captured_stdout=stdout) is False

    def test_gather_rate_limit_texts_ignores_worktree_files(self, tmp_path):
        """TICK-252 (F14 follow-up): executor.stderr/stderr.log/.lanegate/executor.*
        live inside the ticket's own worktree, which the executor agent has
        full write access to -- nothing in the codebase ever wrote them, so
        they were pure agent-writable attack surface an agent could plant
        rate-limit text into to force its own ticket (or, via
        rate_limit_halt, every in-flight ticket) into hibernation on demand.
        Detection must rely solely on the trusted captured_stdout/
        captured_stderr subprocess pipes."""
        (tmp_path / "executor.stderr").write_text("rate limit exceeded")
        (tmp_path / "stderr.log").write_text("rate limit exceeded")
        lanegate_dir = tmp_path / ".lanegate"
        lanegate_dir.mkdir()
        (lanegate_dir / "executor.stderr").write_text("rate limit exceeded")
        (lanegate_dir / "executor.log").write_text("rate limit exceeded")

        texts = _gather_rate_limit_texts(tmp_path, captured_stdout="", captured_stderr="")
        assert "".join(texts) == ""

    def test_is_rate_limit_ignores_agent_planted_worktree_file_content(self, tmp_path):
        """A worktree file alone (agent-controllable), with clean captured
        stdout/stderr and a normal exit code, must not trigger rate-limit
        detection."""
        (tmp_path / "executor.stderr").write_text(
            "You've hit your usage limit. Try again at 4:29 AM."
        )
        assert _is_rate_limit(1, tmp_path, captured_stdout="", captured_stderr="") is False

    def test_is_rate_limit_ignores_signal_exit_even_with_limit_text(self, tmp_path):
        captured_stdout = "You've hit your session limit · resets 5:10pm"
        assert _is_rate_limit(-2, tmp_path, captured_stdout=captured_stdout) is False

    def test_is_rate_limit_ignores_interrupted_codex_diff_mentioning_rate_limit(
        self, tmp_path
    ):
        captured_stdout = """\
diff --git a/lanegate/orchestrate.py b/lanegate/orchestrate.py
@@ -3590,6 +3590,7 @@
+        "rate limit",
+        "session limit",
 turn interrupted
 tokens used
 110,731
"""
        assert _is_rate_limit(1, tmp_path, captured_stdout=captured_stdout) is False

    def test_is_rate_limit_ignores_codex_invalid_request_with_prior_hibernation_text(
        self, tmp_path
    ):
        captured_stdout = """\
## Hibernation Reason

rate limit or quota interruption (executor exited 1)

Raw executor output:
You've hit your session limit · resets 4:40pm (America/Los_Angeles)

ERROR: {"type":"error","status":400,"error":{"type":"invalid_request_error","message":"The 'gpt-5.6-terra' model requires a newer version of Codex. Please upgrade to the latest app or CLI and try again."}}
"""
        assert _is_rate_limit(1, tmp_path, captured_stdout=captured_stdout) is False

    def test_is_rate_limit_widened_pattern_still_rejects_invalid_request_error(self, tmp_path):
        captured_stdout = (
            'ERROR: {"type":"error","status":400,'
            '"error":{"type":"invalid_request_error",'
            '"message":"You\'ve hit your weekly limit · resets 10am"}}'
        )
        assert _is_rate_limit(1, tmp_path, captured_stdout=captured_stdout) is False

    def test_stream_subprocess_stdout_flows_through_invoke_executor_to_is_rate_limit(
        self, tmp_path
    ):
        """TICK-256 regression test.

        Reproduces the exact failure mode from the 2026-07-27 validation run:
        _stream_subprocess captured the real rate-limit text on stdout (not
        stderr, as a claude-a/claude-b `claude -p` invocation does), and
        _is_rate_limit only ever looked at captured_stderr — so 11 rate-limited
        tickets were silently marked "failed" instead of "hibernated". Confirms
        captured_stdout is threaded all the way from _stream_subprocess through
        invoke_executor to _is_rate_limit.
        """
        ticket = {
            "id": "TICK-256",
            "title": "Regression test",
            "touches": ["a.py"],
            "close_criteria": "Done.",
            "_body": "Body.",
        }
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "claude-process"

        rate_limit_text = "You've hit your usage limit. Try again at 4:29 AM."
        with patch(
            "lanegate.orchestrate.pool._stream_subprocess",
            return_value=(1, rate_limit_text, ""),
        ):
            exit_code, captured_stdout, captured_stderr = invoke_executor(
                ticket, cfg, tmp_path, repo_root=tmp_path
            )

        assert exit_code == 1
        assert captured_stdout == rate_limit_text
        assert captured_stderr == ""
        # Before TICK-256, this was checked with captured_stderr only and
        # would have returned False, sending the ticket down the cmd_fail
        # path instead of cmd_hibernate.
        assert (
            _is_rate_limit(
                exit_code, tmp_path, captured_stdout=captured_stdout, captured_stderr=captured_stderr
            )
            is True
        )

    def test_rate_limit_reason_includes_raw_stderr(self, tmp_path):
        captured_stderr = "You've hit your usage limit. Try again at 4:29 AM."
        reason = _rate_limit_reason(1, tmp_path, captured_stderr=captured_stderr)
        assert "rate limit or quota interruption (executor exited 1)" in reason
        assert "Try again at 4:29 AM" in reason

    def test_rate_limit_reason_truncates_long_output(self, tmp_path):
        captured_stderr = "x" * 5000 + "rate limit"
        reason = _rate_limit_reason(1, tmp_path, captured_stderr=captured_stderr)
        assert "(truncated)" in reason
        assert len(reason) < 5000

    def test_rate_limit_reason_falls_back_to_header_when_no_raw_text(self, tmp_path):
        reason = _rate_limit_reason(429, tmp_path, captured_stderr="")
        assert reason == "rate limit or quota interruption (executor exited 429)"

    def test_rate_limit_resume_spawns_watch_daemon(self, tmp_path):
        """on_rate_limit: resume spawns the resume-watch daemon on top of hibernating."""
        cfg = _default_cfg(tmp_path)
        cfg["on_rate_limit"] = "resume"
        self._make_open_ticket(tmp_path)

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", return_value=(1, "", "")),
            patch("lanegate.orchestrate._is_rate_limit", return_value=True),
            patch("lanegate.lifecycle.cmd_hibernate"),
            patch("lanegate.orchestrate.spawn_resume_watch_daemon") as mock_spawn,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_spawn.assert_called_once()

    def test_rate_limit_halt_does_not_spawn_watch_daemon(self, tmp_path):
        """Default on_rate_limit: halt does NOT spawn the resume-watch daemon."""
        cfg = _default_cfg(tmp_path)
        self._make_open_ticket(tmp_path)

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", return_value=(1, "", "")),
            patch("lanegate.orchestrate._is_rate_limit", return_value=True),
            patch("lanegate.lifecycle.cmd_hibernate"),
            patch("lanegate.orchestrate.spawn_resume_watch_daemon") as mock_spawn,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_spawn.assert_not_called()

    def test_rate_limit_blocks_next_ticket(self, tmp_path):
        """A rate limit stops orchestrate from dispatching further tickets.

        Serial run over four independent open tickets. The executor succeeds
        for TICK-001 and TICK-002, then hits a rate limit on TICK-003.
        Orchestrate must hibernate TICK-003, stop calling next_batch() for new
        work, and never dispatch TICK-004 — exiting after the in-flight ticket
        hibernates rather than churning the rest of the board into failures.
        """
        cfg = _default_cfg(tmp_path)
        cfg["max_parallel"] = 1
        tickets_dir = tmp_path / "tickets"
        _write_ticket(tickets_dir, "TICK-001", "open", touches=["a.py"], priority=1)
        _write_ticket(tickets_dir, "TICK-002", "open", touches=["b.py"], priority=2)
        _write_ticket(tickets_dir, "TICK-003", "open", touches=["c.py"], priority=3)
        _write_ticket(tickets_dir, "TICK-004", "open", touches=["d.py"], priority=4)

        invoked: list[str] = []

        def fake_invoke(ticket, cfg_, wt, **kwargs):
            invoked.append(ticket["id"])
            exit_code = 1 if ticket["id"] == "TICK-003" else 0
            return (exit_code, "", "")

        def fake_is_rate_limit(exit_code, wt=None, captured_stdout="", captured_stderr=""):
            return exit_code == 1

        def fake_start(tid, cfg_, repo_root, **kwargs):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: open", "status: in_progress", 1))

        def fake_hibernate(tid, cfg_, repo_root, **kwargs):
            p = tickets_dir / f"{tid}.md"
            p.write_text(p.read_text().replace("status: in_progress", "status: hibernated", 1))

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

        real_next_batch = next_batch
        next_batch_calls: list[list[str]] = []

        def counting_next_batch(
            cfg_, repo_root, milestone=None, *, exclude_touches=None, ticket_ids=None
        ):
            result = real_next_batch(
                cfg_,
                repo_root,
                milestone=milestone,
                exclude_touches=exclude_touches,
                ticket_ids=ticket_ids,
            )
            next_batch_calls.append([t["id"] for t in result])
            return result

        with (
            patch("lanegate.lifecycle.cmd_start", side_effect=fake_start),
            patch("lanegate.orchestrate.invoke_executor", side_effect=fake_invoke),
            patch("lanegate.orchestrate._is_rate_limit", side_effect=fake_is_rate_limit),
            patch(
                "lanegate.orchestrate._rate_limit_reason",
                return_value="rate limit or quota interruption (executor exited 1)",
            ),
            patch("lanegate.orchestrate.next_batch", side_effect=counting_next_batch),
            patch("lanegate.lifecycle.cmd_hibernate", side_effect=fake_hibernate),
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
                tmp_path,
                max_parallel=1,
                human_review="none",
                all_milestones=True,
                auto_analyze=False,
            )

        # Tickets 1 and 2 ran; the rate limit hit on 3; ticket 4 was never dispatched.
        assert invoked == ["TICK-001", "TICK-002", "TICK-003"]
        assert "TICK-004" not in invoked
        # No further next_batch() call after the limit — three iterations only,
        # never a fourth that would pick up the still-open TICK-004.
        assert len(next_batch_calls) == 3
        assert ["TICK-004"] not in next_batch_calls
        # TICK-004 left untouched (still open); the rate-limited TICK-003
        # hibernated (a resume signal), not failed.
        assert "status: open" in (tickets_dir / "TICK-004.md").read_text()
        assert "status: hibernated" in (tickets_dir / "TICK-003.md").read_text()

    def test_empty_worktree_commits_calls_cmd_fail(self, tmp_path):
        """Exit 0 but no commits on branch → cmd_fail, not cmd_complete."""
        cfg = _default_cfg(tmp_path)
        self._make_open_ticket(tmp_path)

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", return_value=(0, "", "")),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=(False, None)),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=False),
            patch("lanegate.lifecycle.cmd_complete") as mock_complete,
            patch("lanegate.lifecycle.cmd_fail") as mock_fail,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_fail.assert_called_once()
        mock_complete.assert_not_called()

    def test_clean_exit_commits_dirty_worktree_before_commit_check(self, tmp_path):
        """Exit 0 but the ticket's status never advanced (default cfg here is
        combined mode, where cmd_complete is never called directly — the
        executor is supposed to call it internally) must still commit
        whatever's on the worktree, and must degrade to needs_review rather
        than letting cmd_hibernate's in_progress-only guard crash the whole
        run (cmd_hibernate sys.exit(1)s on any other status, e.g. this
        ticket's 'open' — see TICK-189)."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        self._make_open_ticket(tmp_path)

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", return_value=(0, "", "")),
            patch(
                "lanegate.orchestrate.commit_worktree_changes", return_value=(True, None)
            ) as mock_commit_changes,
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_commit_changes.assert_called_once()
        status_line = [
            l for l in (tickets_dir / "TICK-001.md").read_text().splitlines() if "status:" in l
        ][0]
        assert "needs_review" in status_line

    def test_clean_exit_with_commits_proceeds_to_complete(self, tmp_path):
        """Exit 0 with commits on branch → cmd_complete IS called."""
        cfg = _default_cfg(tmp_path)
        tickets_dir = tmp_path / "tickets"
        self._make_open_ticket(tmp_path)

        # After cmd_complete runs we need the ticket to appear as code_complete
        # so next_batch returns [] and the loop terminates naturally.
        def fake_complete(tid, cfg_, repo_root):
            p = tickets_dir / "TICK-001.md"
            text = p.read_text().replace("status: open", "status: code_complete")
            p.write_text(text)

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.orchestrate.invoke_executor", return_value=(0, "", "")),
            patch("lanegate.orchestrate.commit_worktree_changes", return_value=(False, None)),
            patch("lanegate.orchestrate.check_worktree_has_commits", return_value=True),
            patch("lanegate.orchestrate._is_combined_mode", return_value=False),
            patch("lanegate.lifecycle.cmd_complete", side_effect=fake_complete) as mock_complete,
            patch("lanegate.lifecycle.cmd_fail") as mock_fail,
            patch("lanegate.lifecycle.cmd_review") as mock_review,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)

        mock_complete.assert_called_once()
        mock_fail.assert_not_called()
        mock_review.assert_called_once()

    def test_bad_named_executor_config_fails_ticket_not_whole_run(self, tmp_path):
        """Regression (TICK-088 second review round): a ticket dispatched to
        a named executor instance with an unresolvable api_key_env (unset
        var, or a type with no known key-injection target) must fail only
        that one ticket via cmd_fail — the same as any other nonzero-exit
        executor error — not raise an uncaught ConfigError out of
        cmd_orchestrate (which only wraps _drain_loop in try/finally, not
        try/except) and crash the whole multi-ticket board-clearing run.

        Unlike the other tests in this class, invoke_executor is NOT mocked
        here — this exercises the real resolve_executor_env call inside it.
        """
        cfg = _default_cfg(tmp_path)
        cfg["executor"] = "gemini-1"
        cfg["executors"] = {
            "gemini-1": {"type": "gemini", "api_key_env": "SOME_UNSET_GEMINI_KEY"},
        }
        self._make_open_ticket(tmp_path)

        with (
            patch("lanegate.lifecycle.cmd_start"),
            patch("lanegate.lifecycle.cmd_complete") as mock_complete,
            patch("lanegate.lifecycle.cmd_fail") as mock_fail,
            patch("lanegate.orchestrate.acquire_orchestrator_lock", return_value=9999),
            patch("lanegate.orchestrate.release_orchestrator_lock"),
        ):
            cmd_orchestrate(cfg, tmp_path, all_milestones=True)  # must not raise

        mock_fail.assert_called_once()
        mock_complete.assert_not_called()


# ---------------------------------------------------------------------------
# _committed_files
# ---------------------------------------------------------------------------


class TestCommittedFiles:
    """Unit tests for _committed_files()."""

    def test_returns_set_of_changed_files(self, tmp_path):
        """_committed_files parses git diff --name-only output into a set."""
        with patch("lanegate.orchestrate.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="lanegate/orchestrate.py\ntests/test_orchestrate.py\n",
            )
            result = _committed_files(tmp_path)
        assert result == {"lanegate/orchestrate.py", "tests/test_orchestrate.py"}

    def test_returns_empty_set_on_git_error(self, tmp_path):
        """_committed_files returns empty set (fail-open) when git fails."""
        with patch("lanegate.orchestrate.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="")
            result = _committed_files(tmp_path)
        assert result == set()

    def test_returns_empty_set_when_no_changes(self, tmp_path):
        """_committed_files returns empty set when branch has no changes vs main."""
        with patch("lanegate.orchestrate.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            result = _committed_files(tmp_path)
        assert result == set()

    def test_uses_three_dot_diff(self, tmp_path):
        """_committed_files uses 'main...HEAD' (three-dot) form."""
        with patch("lanegate.orchestrate.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            _committed_files(tmp_path)
        cmd = mock_run.call_args.args[0]
        assert "main...HEAD" in cmd
        assert "--name-only" in cmd

    def test_excludes_lanegate_dir_entries(self, tmp_path):
        """TICK-218: .lanegate/ paths (prompt files, fix-pass status) never count
        as scope drift, even if something else committed them onto the
        branch — the touches guard and blocked-file check both key off this."""
        with patch("lanegate.orchestrate.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="lanegate/config.py\n.lanegate/prompts/TICK-001-implement.md\n"
                ".lanegate/status/TICK-001.json\n",
            )
            result = _committed_files(tmp_path)
        assert result == {"lanegate/config.py"}


class TestCaptureManualImplementStepRun:
    def test_captures_manual_implement_evidence_and_diff(self, tmp_path):
        repo_root = tmp_path
        subprocess.run(["git", "init", "-b", "main"], cwd=repo_root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_root, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_root, check=True)
        (repo_root / "example.txt").write_text("before\n")
        subprocess.run(["git", "add", "example.txt"], cwd=repo_root, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo_root, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-b", "tick-373"], cwd=repo_root, check=True, capture_output=True)
        (repo_root / "example.txt").write_text("manual change\n")
        subprocess.run(["git", "add", "example.txt"], cwd=repo_root, check=True)
        subprocess.run(["git", "commit", "-m", "manual implementation"], cwd=repo_root, check=True, capture_output=True)

        ticket = {
            "id": "TICK-373",
            "branch": "tick-373",
            "lifecycle_events": [
                {"event": "implementation_started", "at": "2026-08-05T18:05:26Z"}
            ],
        }
        bundle_path = capture_manual_implement_step_run(
            repo_root,
            repo_root,
            ticket,
            {"trunk_branch": "main"},
            safeguards_passed=True,
            safeguard_reason="all safeguards passed",
        )

        assert bundle_path is not None
        assert bundle_path.parent == repo_root / ".lanegate" / "executor-runs" / "TICK-373"
        status = json.loads((bundle_path / "status.json").read_text())
        assert status["mode"] == "manual"
        assert status["step"] == "implement"
        assert status["before_sha"]
        assert status["after_sha"]
        assert status["safeguards_passed"] is True
        assert status["safeguard_reason"] == "all safeguards passed"
        assert "manual change" in (bundle_path / "diff.patch").read_text()
        manifest = json.loads((bundle_path / "manifest.json").read_text())
        assert any(item["artifact"] == "executor-session.jsonl" for item in manifest["missing"])
        assert has_step_bundle(repo_root, "TICK-373", "implement") is True
        assert has_step_bundle(repo_root, "TICK-373", "review") is False


# ---------------------------------------------------------------------------


def test_max_turns_exceeded(tmp_path):
    from io import StringIO
    from unittest.mock import patch
    from lanegate.config import _default_config
    from lanegate.orchestrate.pool import invoke_executor

    ticket = {
        "id": "TICK-411",
        "title": "Budget Cap Test",
        "touches": [],
    }

    # Test max_turns cap
    cfg = _default_config()
    cfg["executor"] = "claude"
    cfg["max_turns"] = 2

    lines = [
        json.dumps({"type": "assistant", "message": {"content": [], "usage": {"input_tokens": 100, "output_tokens": 50}}}),
        json.dumps({"type": "assistant", "message": {"content": [], "usage": {"input_tokens": 200, "output_tokens": 100}}}),
        json.dumps({"type": "assistant", "message": {"content": [], "usage": {"input_tokens": 300, "output_tokens": 150}}}),
    ]

    def fake_subprocess(cmd, cwd, **kwargs):
        on_line = kwargs.get("on_line")
        if on_line:
            for line in lines:
                on_line(line, True)
        return 0, "\n".join(lines), ""

    log_stream = StringIO()

    with patch("lanegate.orchestrate.pool._stream_subprocess", side_effect=fake_subprocess):
        rc, out, err = invoke_executor(ticket, cfg, tmp_path, log_stream=log_stream)

    assert rc != 0
    logged_output = log_stream.getvalue()
    assert "aborted early" in logged_output
    assert "turns" in logged_output
    assert "tokens" in logged_output

    # Test max_cumulative_tokens ceiling
    cfg_tokens = _default_config()
    cfg_tokens["executor"] = "claude"
    cfg_tokens["max_cumulative_tokens"] = 300

    log_stream_tokens = StringIO()

    with patch("lanegate.orchestrate.pool._stream_subprocess", side_effect=fake_subprocess):
        rc_tok, out_tok, err_tok = invoke_executor(ticket, cfg_tokens, tmp_path, log_stream=log_stream_tokens)

    assert rc_tok != 0
    logged_tokens_output = log_stream_tokens.getvalue()
    assert "aborted early" in logged_tokens_output
    assert "tokens" in logged_tokens_output


def test_select_pool_instance_returns_none_when_all_instances_cooling_down(tmp_path):
    """When all instances in a pool are cooling down due to rate limits,
    _select_pool_instance must return None rather than raising an AssertionError."""
    from unittest.mock import patch
    from lanegate.orchestrate.loop import _pool_instance_healthy

    cfg = {
        "pools": {"default": {"executors": ["inst-1", "inst-2"], "strategy": "least-loaded"}},
        "executors": {
            "inst-1": {"type": "claude-process"},
            "inst-2": {"type": "claude-process"},
        },
    }

    # Mock _pool_instance_healthy to simulate all pool instances cooling down
    with patch("lanegate.orchestrate.loop_dispatch._pool_instance_healthy", return_value=False), \
         patch("lanegate.orchestrate.loop._COOLDOWN_POLL_SECONDS", 0):
        # We also need a scope context inside _cmd_orchestrate_body or similar,
        # but _select_pool_instance is an inner function defined inside _drain_loop.
        # We test resolve_pool_executor returning None directly.
        from lanegate.orchestrate.loop import resolve_pool_executor
        result = resolve_pool_executor("implement", {}, cfg, tmp_path, pool_name="default")
        assert result is None


class TestWorktreeBoundaryGuard:
    def test_write_outside_worktree_rejected(self, tmp_path):
        from lanegate.orchestrate.pool import make_event_line_handler

        worktree = tmp_path / "worktrees" / "tick-572"
        worktree.mkdir(parents=True)
        outside_file = tmp_path / "outside" / "cli.py"

        handler = make_event_line_handler(
            tmp_path,
            "ts",
            "tick-572",
            executor="claude",
            model="test",
            step="implement",
            worktree_path=worktree,
        )
        line = json.dumps({"content_block": {"name": "Write", "input": {"TargetFile": str(outside_file)}}})
        with pytest.raises(RuntimeError, match=r"\[worktree-guard\]"):
            handler(line, is_stdout=True)

    def test_write_inside_worktree_allowed(self, tmp_path):
        from lanegate.orchestrate.pool import make_event_line_handler

        worktree = tmp_path / "worktrees" / "tick-572"
        worktree.mkdir(parents=True)
        inside_file = worktree / "lanegate" / "cli.py"
        inside_file.parent.mkdir(parents=True)
        inside_file.write_text("test")

        handler = make_event_line_handler(
            tmp_path,
            "ts",
            "tick-572",
            executor="claude",
            model="test",
            step="implement",
            worktree_path=worktree,
        )
        line = json.dumps({"content_block": {"name": "Write", "input": {"TargetFile": str(inside_file)}}})
        handler(line, is_stdout=True)

    def test_edit_outside_worktree_rejected(self, tmp_path):
        from lanegate.orchestrate.pool import make_event_line_handler

        worktree = tmp_path / "worktrees" / "tick-572"
        worktree.mkdir(parents=True)
        outside_file = tmp_path / "outside" / "cli.py"

        handler = make_event_line_handler(
            tmp_path,
            "ts",
            "tick-572",
            executor="claude",
            model="test",
            step="implement",
            worktree_path=worktree,
        )
        line = json.dumps({"content_block": {"name": "Edit", "input": {"path": str(outside_file)}}})
        with pytest.raises(RuntimeError, match=r"\[worktree-guard\]"):
            handler(line, is_stdout=True)

    def test_edit_inside_worktree_allowed(self, tmp_path):
        from lanegate.orchestrate.pool import make_event_line_handler

        worktree = tmp_path / "worktrees" / "tick-572"
        worktree.mkdir(parents=True)
        inside_file = worktree / "lanegate" / "cli.py"
        inside_file.parent.mkdir(parents=True)
        inside_file.write_text("test")

        handler = make_event_line_handler(
            tmp_path,
            "ts",
            "tick-572",
            executor="claude",
            model="test",
            step="implement",
            worktree_path=worktree,
        )
        line = json.dumps({"content_block": {"name": "Edit", "input": {"path": str(inside_file)}}})
        handler(line, is_stdout=True)

    def test_list_shaped_content_block_outside_worktree_rejected(self, tmp_path):
        from lanegate.orchestrate.pool import make_event_line_handler

        worktree = tmp_path / "worktrees" / "tick-572"
        worktree.mkdir(parents=True)
        outside_file = tmp_path / "outside" / "cli.py"

        handler = make_event_line_handler(
            tmp_path,
            "ts",
            "tick-572",
            executor="claude",
            model="test",
            step="implement",
            worktree_path=worktree,
        )
        line = json.dumps({"content_block": [{"name": "Write", "input": {"TargetFile": str(outside_file)}}]})
        with pytest.raises(RuntimeError, match=r"\[worktree-guard\]"):
            handler(line, is_stdout=True)

    def test_direct_assert_path_in_worktree(self, tmp_path):
        from lanegate.orchestrate.pool import _assert_path_in_worktree

        worktree = tmp_path / "worktrees" / "tick-572"
        worktree.mkdir(parents=True)
        outside_file = tmp_path / "outside" / "cli.py"
        outside_file.parent.mkdir(parents=True)
        outside_file.write_text("test")

        with pytest.raises(RuntimeError, match=r"\[worktree-guard\]"):
            _assert_path_in_worktree("Write", str(outside_file), worktree)

    def test_direct_assert_path_in_worktree_raises_worktree_guard_violation_subclass(self, tmp_path):
        from lanegate.orchestrate.pool import WorktreeGuardViolation, _assert_path_in_worktree

        worktree = tmp_path / "worktrees" / "tick-585"
        worktree.mkdir(parents=True)
        outside_file = tmp_path / "outside" / "cli.py"
        outside_file.parent.mkdir(parents=True)
        outside_file.write_text("test")

        with pytest.raises(WorktreeGuardViolation, match=r"\[worktree-guard\]"):
            _assert_path_in_worktree("Write", str(outside_file), worktree)

    def test_traversal_path_outside_worktree_rejected(self, tmp_path):
        from lanegate.orchestrate.pool import _assert_path_in_worktree

        worktree = tmp_path / "worktrees" / "tick-572"
        worktree.mkdir(parents=True)

        traversal_path = "../../outside/cli.py"
        with pytest.raises(RuntimeError, match=r"\[worktree-guard\]"):
            _assert_path_in_worktree("Write", traversal_path, worktree)

    def test_symlink_outside_worktree_rejected(self, tmp_path):
        from lanegate.orchestrate.pool import _assert_path_in_worktree

        worktree = tmp_path / "worktrees" / "tick-572"
        worktree.mkdir(parents=True)
        outside_file = tmp_path / "outside.txt"
        outside_file.write_text("outside")

        link_inside = worktree / "link_inside.txt"
        os.symlink(outside_file, link_inside)

        with pytest.raises(RuntimeError, match=r"\[worktree-guard\]"):
            _assert_path_in_worktree("Write", str(link_inside), worktree)

    def test_invoke_executor_wires_worktree_path_and_logs_guard_error(self, tmp_path):
        from lanegate.orchestrate.pool import invoke_executor
        import io
        import sys

        worktree = tmp_path / "worktrees" / "tick-572"
        worktree.mkdir(parents=True)
        outside_file = tmp_path / "outside" / "cli.py"

        ticket = {"id": "TICK-572", "executor": "mock"}
        cfg = {"executors": {"mock": {"type": "claude", "command": "echo"}}}

        log_stream = io.StringIO()
        tool_call_json = json.dumps({"content_block": [{"name": "Write", "input": {"TargetFile": str(outside_file)}}]})
        # A portable stand-in for "the executor prints a tool-call line, then
        # keeps running briefly" — `bash -c` isn't reliable here since on the
        # Windows CI runner it can resolve to the WSL launcher stub instead of
        # Git Bash's bash.exe when no WSL distro is installed.
        mock_cmd_script = (
            f"import sys, time\n"
            f"sys.stdout.write({tool_call_json!r} + chr(10))\n"
            f"sys.stdout.flush()\n"
            f"time.sleep(0.2)\n"
        )
        with patch(
            "lanegate.orchestrate.pool.build_executor_cmd",
            return_value=[sys.executable, "-c", mock_cmd_script],
        ):
            with patch("lanegate.orchestrate.pool._write_prompt_file"):
                rc, stdout, stderr = invoke_executor(
                    ticket,
                    cfg,
                    worktree,
                    log_stream=log_stream,
                )
        assert rc != 0
        assert "[worktree-guard]" in log_stream.getvalue()
        assert "LaneGate worktree-isolation bug" in log_stream.getvalue()

    def test_notes_symlink_write_allowed_and_normalized(self, tmp_path):
        from lanegate.orchestrate.pool import make_event_line_handler
        from lanegate.worktree import _ensure_notes_symlink

        repo_root = tmp_path / "repo"
        repo_root.mkdir(parents=True)
        worktree = tmp_path / "worktrees" / "tick-572"
        worktree.mkdir(parents=True)

        with patch("lanegate.worktree._run") as mock_run:
            mock_run.return_value.returncode = 0
            _ensure_notes_symlink(repo_root, worktree)

        events = []
        handler = make_event_line_handler(
            tmp_path,
            "ts",
            "tick-572",
            executor="claude",
            model="test",
            step="implement",
            on_event=events.append,
            worktree_path=worktree,
        )

        notes_file = worktree / ".lanegate" / "notes" / "global.md"
        line = json.dumps({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Write", "input": {"file_path": str(notes_file)}}
                ]
            }
        })
        handler(line, is_stdout=True)
        assert len(events) == 1
        assert events[0].activity == "writing_file"
        assert events[0].tool_category == "file_write"

    def test_graphify_symlink_write_allowed(self, tmp_path):
        from lanegate.orchestrate.pool import _assert_path_in_worktree
        from lanegate.worktree import _ensure_graphify_symlink

        repo_root = tmp_path / "repo"
        repo_root.mkdir(parents=True)
        graphify_dir = repo_root / "graphify-out"
        graphify_dir.mkdir(parents=True)

        worktree = tmp_path / "worktrees" / "tick-572"
        worktree.mkdir(parents=True)

        with patch("lanegate.worktree._run") as mock_run:
            mock_run.return_value.returncode = 0
            _ensure_graphify_symlink(repo_root, worktree)

        graphify_file = worktree / "graphify-out" / "graph.json"
        _assert_path_in_worktree("Write", str(graphify_file), worktree)

    def test_non_dict_message_does_not_raise_attribute_error(self, tmp_path):
        from lanegate.orchestrate.pool import make_event_line_handler

        worktree = tmp_path / "worktrees" / "tick-572"
        worktree.mkdir(parents=True)
        handler = make_event_line_handler(
            tmp_path,
            "ts",
            "tick-572",
            executor="codex",
            model="test",
            step="implement",
            worktree_path=worktree,
        )
        line = json.dumps({"type": "item.completed", "message": "done", "usage": {"input_tokens": 1234}})
        handler(line, is_stdout=True)

    def test_boundary_violation_preserves_metering(self, tmp_path):
        from lanegate.orchestrate.pool import make_event_line_handler
        from lanegate.budget import DispatchMeter

        worktree = tmp_path / "worktrees" / "tick-572"
        worktree.mkdir(parents=True)
        outside_file = tmp_path / "outside" / "cli.py"

        meter = DispatchMeter()
        handler = make_event_line_handler(
            tmp_path,
            "ts",
            "tick-572",
            executor="claude",
            model="test",
            step="implement",
            meter=meter,
            worktree_path=worktree,
        )
        line = json.dumps({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Write", "input": {"file_path": str(outside_file)}}
                ],
                "usage": {"input_tokens": 5000, "output_tokens": 100},
            },
        })
        with pytest.raises(RuntimeError, match=r"\[worktree-guard\]"):
            handler(line, is_stdout=True)

        assert meter.turns == 1
        assert meter.tokens == 5100
