"""Tests for cli.py — create + analyze subcommands and their chaining."""

import json
import os
import subprocess
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

import pytest

from lanegate.ticket import parse_ticket

# We invoke cli.main() through argparse. We need to mock out:
#  - find_repo_root / load_config (return a controlled tmp dir + cfg)
#  - cmd_analyze's model seam so no live claude subprocess fires
#  - git subprocess calls from cmd_create (commit_status_changes: False via cfg)


_CFG = {
    "ticket_prefix": "TICK",
    "tickets_dir": "tickets",
    "worktrees_dir": "worktrees",
    "executor": "claude",
    "core_files": [],
    "core_patterns": [],
    "lock_statuses": ["in_progress", "code_complete", "in_review"],
    "flag_file": "~/.lanegate/feature_flags.json",
    "environments": [],
    "commit_status_changes": False,
}

_ANALYZE_RESPONSE = json.dumps(
    {
        "touches": ["lanegate/foo.py", "tests/test_foo.py"],
        "close_criteria": "foo command writes a file.",
        "depends_on": [],
    }
)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "tickets").mkdir()
    return tmp_path


def _run_cli(args: list[str], repo: Path):
    """Patch config/repo discovery and run cli.main() with given args."""
    from lanegate import cli

    # cli.py uses `from lanegate.config import ..., find_repo_root`
    # so patch the name in the cli module's namespace, not in lanegate.config
    with (
        patch("lanegate.cli.find_repo_root", return_value=repo),
        patch("lanegate.cli.load_config", return_value=_CFG),
        patch("sys.argv", ["lanegate"] + args),
    ):
        cli.main()


def test_version_matches_installed_distribution_metadata(capsys):
    from lanegate import cli

    with patch("sys.argv", ["lanegate", "--version"]), pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"lanegate {version('lanegate')}"


def test_invalid_ticket_id_exits_cleanly_instead_of_a_raw_traceback(repo, capsys):
    """A malformed ticket_id (stray whitespace, path syntax, ...) reaching
    canonical_id() from a CLI argument must exit(1) with a clean ERROR
    message, not an unhandled ValueError traceback. Regression test for
    finding [3]: cli.py had no top-level handler for this."""
    from lanegate import cli

    with (
        patch("lanegate.cli.find_repo_root", return_value=repo),
        patch("lanegate.cli.load_config", return_value=_CFG),
        patch("sys.argv", ["lanegate", "start", "TICK-001 "]),
        pytest.raises(SystemExit) as exc,
    ):
        cli.main()

    assert exc.value.code == 1
    assert "ERROR: invalid ticket ID" in capsys.readouterr().err


# --- top-level help tiers ---


# This is the command set before the help presentation changed.  Keep it
# explicit so adding/removing/renaming a parser command cannot be mistaken for
# a harmless help-group edit.
_TOP_LEVEL_COMMANDS_BEFORE_TIERED_HELP = frozenset(
    {
        "analytics",
        "analyze",
        "api",
        "blocked",
        "board",
        "claim-file",
        "complete",
        "close",
        "context-stats",
        "create",
        "doctor",
        "done",
        "executor",
        "fix",
        "flag",
        "gh-sync",
        "globals",
        "hibernate",
        "human-review",
        "init",
        "install-agent-tools",
        "install-commands",
        "log",
        "logs",
        "mcp",
        "merge",
        "needs-review",
        "next",
        "notify-watch",
        "open",
        "orchestrate",
        "orchestrator-lock",
        "run",
        "pipeline-status",
        "projects",
        "promote",
        "prompts",
        "ps",
        "recover-rate-limited-reviews",
        "recover-rejected",
        "reopen",
        "resolve-conflict",
        "resume-watch",
        "review",
        "route",
        "run-report",
        "session-summary",
        "start",
        "stats",
        "stop",
        "summary",
        "supersede",
        "symbols",
        "tui",
        "update-docs",
        "validate",
        "watch",
    }
)


def test_default_help_is_tiered_and_aliases_use_the_same_tiers(capsys, monkeypatch):
    from lanegate import cli

    monkeypatch.setenv("COLUMNS", "200")
    for program_name in ("lanegate", "lgt", "lane"):
        with patch("sys.argv", [program_name, "--help"]), pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "Common commands:" in output
        assert "Ticket lifecycle and recovery:" in output
        assert "Monitoring and reporting:" in output
        assert "Setup and integration:" in output
        assert "Agent and run tools:" in output
        assert "--help-all" in output
        assert "context-stats" not in output
        assert all(len(line) <= 80 for line in output.splitlines())


def test_help_all_lists_every_registered_top_level_command():
    from lanegate import cli

    parser = cli.build_parser()
    with patch.object(parser, "_print_message") as print_message, pytest.raises(SystemExit) as exc:
        parser.parse_args(["--help-all"])

    assert exc.value.code == 0
    output = print_message.call_args.args[0]
    assert "All commands:" in output
    for command in cli.registered_command_names(parser):
        assert f"  {command}" in output


def test_tiered_help_keeps_the_existing_command_set_and_catches_unassigned_commands():
    from lanegate import cli

    parser = cli.build_parser()
    assert cli.registered_command_names(parser) == _TOP_LEVEL_COMMANDS_BEFORE_TIERED_HELP
    # context-stats is intentionally hidden by argparse.SUPPRESS.  Any newly
    # registered, ungrouped command makes this equality fail until assigned.
    assert cli.unassigned_command_names(parser) == cli.HIDDEN_TOP_LEVEL_COMMANDS

    # Every pre-existing name remains parseable with its per-command help.
    for command in _TOP_LEVEL_COMMANDS_BEFORE_TIERED_HELP:
        with pytest.raises(SystemExit) as exc:
            parser.parse_args([command, "--help"])
        assert exc.value.code == 0


# --- lanegate create --no-analyze ---


def test_create_no_analyze_writes_draft(repo, capsys):
    _run_cli(["create", "Build a login page", "--no-analyze"], repo)
    path = repo / "tickets" / "TICK-001.md"
    assert path.exists()
    t = parse_ticket(path)
    assert t["status"] == "draft"


def test_create_no_analyze_prints_id(repo, capsys):
    _run_cli(["create", "Build a login page", "--no-analyze"], repo)
    out = capsys.readouterr().out
    assert "TICK-001" in out


def test_create_no_analyze_does_not_call_model(repo):
    with patch("lanegate.analyze.cmd_analyze") as mock_analyze:
        _run_cli(["create", "Build a login page", "--no-analyze"], repo)
        mock_analyze.assert_not_called()


def test_create_accepts_explicit_board_title_and_autonomy(repo):
    title = "Preserve this complete board title instead of deriving one from the intent"
    _run_cli(
        [
            "create",
            "A much longer implementation description that should stay in the body.",
            "--title",
            title,
            "--autonomy",
            "full",
            "--no-analyze",
        ],
        repo,
    )
    ticket = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert ticket["title"] == title
    assert ticket["autonomy"] == "full"


def test_create_autonomy_flag(repo):
    _run_cli(["create", "Escalate credentials handling", "--autonomy", "red", "--no-analyze"], repo)

    ticket = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert ticket["autonomy"] == "red"


# --- lanegate create (default: runs analyze) ---


def test_create_default_chains_analyze(repo, capsys):
    with patch("lanegate.analyze._call_model", return_value=(_ANALYZE_RESPONSE, None)):
        _run_cli(["create", "Build a login page"], repo)
    out = capsys.readouterr().out
    ticket_id = out.splitlines()[0].strip()
    t = parse_ticket(repo / "tickets" / f"{ticket_id}.md")
    assert t["status"] == "draft"
    assert t["touches"] == ["lanegate/foo.py", "tests/test_foo.py"]


def test_create_default_prints_both_id_and_analysis(repo, capsys):
    with patch("lanegate.analyze._call_model", return_value=(_ANALYZE_RESPONSE, None)):
        _run_cli(["create", "Build a login page"], repo)
    out = capsys.readouterr().out
    assert "TICK-" in out
    assert "touches populated" in out


# --- lanegate context-stats --compare (DB-backed, real executor attribution) ---


def test_context_stats_compare_cli_resolves_executor_from_step_costs(repo, capsys):
    """The real CLI path (lanegate context-stats --compare) must route through
    step_costs, not just _print_compare called directly with a hand-built
    step_costs list. TICK-549 review caught that a naive revert of the
    cli.py wiring (moving the step_costs load back below the --compare
    early return) would leave every unit test on _print_compare green while
    silently regressing the actual DB-backed --compare output back to the
    analytics table's wrong per-ticket executor guess."""
    from lanegate.context_log import _get_default_db_path, _get_project_id, _upsert_row, log_step_cost

    db_path = _get_default_db_path()
    project = _get_project_id(repo)
    # analytics table: the static per-ticket guess, wrong (mirrors the
    # production bug this ticket exists to fix).
    _upsert_row(
        db_path, project,
        {"ticket_id": "TICK-1", "executor": "codex", "timestamp": "2026-06-20T10:00:00Z"},
    )
    # step_costs: the real executor that actually ran this ticket's implement step.
    log_step_cost(db_path, project, "TICK-1", "implement", executor="claude-b", cost_usd=0.05)
    # A second ticket genuinely run by codex, with no step_costs row -- the
    # analytics table's guess is trusted here (correctly, since it's right).
    _upsert_row(
        db_path, project,
        {"ticket_id": "TICK-2", "executor": "codex", "timestamp": "2026-06-20T10:00:00Z"},
    )

    _run_cli(["context-stats", "--compare"], repo)
    out = capsys.readouterr().out

    assert "claude-b" in out
    assert "codex" in out
    # TICK-1 must be bucketed under claude-b, not codex.
    codex_line = next(line for line in out.splitlines() if line.startswith("codex"))
    assert codex_line.split()[1] == "1"


def test_create_prints_next_step_hint_when_auto_analyze_fails(repo, capsys):
    with patch("lanegate.analyze.cmd_analyze", side_effect=SystemExit(1)):
        _run_cli(["create", "Build a login page"], repo)
    out = capsys.readouterr()
    assert "TICK-001" in out.out
    assert "TICK-001 left in draft — run `lanegate analyze TICK-001`" in out.err
    assert "analyze skipped:" in out.err


def test_create_reraises_keyboard_interrupt_from_auto_analyze(repo, capsys):
    with patch("lanegate.analyze.cmd_analyze", side_effect=SystemExit(130)):
        with pytest.raises(SystemExit) as exc_info:
            _run_cli(["create", "Build a login page"], repo)
    assert exc_info.value.code == 130
    err = capsys.readouterr().err
    assert "left in draft" not in err


# --- lanegate analyze (standalone) ---


def test_analyze_standalone_flips_draft_to_open(repo, capsys):
    # Create a draft, then analyze it standalone (two separate CLI invocations)
    _run_cli(["create", "Build a login page", "--no-analyze"], repo)
    ticket_id = capsys.readouterr().out.strip()
    with patch("lanegate.analyze._call_model", return_value=(_ANALYZE_RESPONSE, None)):
        _run_cli(["analyze", ticket_id], repo)
    t = parse_ticket(repo / "tickets" / f"{ticket_id}.md")
    assert t["status"] == "open"


def test_analyze_standalone_missing_ticket_exits(repo):
    with patch("lanegate.analyze._call_model", return_value=(_ANALYZE_RESPONSE, None)):
        with pytest.raises(SystemExit) as exc:
            _run_cli(["analyze", "TICK-999"], repo)
    assert exc.value.code == 1


# --- lanegate review --verdict flags ---


def _write_code_complete(repo):
    """Create a code_complete ticket for review tests."""
    (repo / "tickets").mkdir(exist_ok=True)
    path = repo / "tickets" / "TICK-001.md"
    path.write_text("---\nid: TICK-001\ntitle: Test\nstatus: code_complete\n---\nBody.\n")


def test_review_verdict_approved_calls_cmd_review_with_verdict(repo):
    _write_code_complete(repo)
    with patch("lanegate.lifecycle.cmd_review") as mock_review:
        mock_review.return_value = None
        with (
            patch("lanegate.cli.find_repo_root", return_value=repo),
            patch("lanegate.cli.load_config", return_value=_CFG),
            patch(
                "sys.argv",
                ["lanegate", "review", "TICK-001", "--verdict", "approved", "--summary", "LGTM"],
            ),
        ):
            from lanegate import cli

            cli.main()
        mock_review.assert_called_once()
        _, kwargs = mock_review.call_args
        assert kwargs.get("verdict") == "approved"
        assert kwargs.get("summary") == "LGTM"


def test_review_verdict_changes_requested_passed_through(repo):
    _write_code_complete(repo)
    with patch("lanegate.lifecycle.cmd_review") as mock_review:
        mock_review.return_value = None
        with (
            patch("lanegate.cli.find_repo_root", return_value=repo),
            patch("lanegate.cli.load_config", return_value=_CFG),
            patch("sys.argv", ["lanegate", "review", "TICK-001", "--verdict", "changes_requested"]),
        ):
            from lanegate import cli

            cli.main()
        _, kwargs = mock_review.call_args
        assert kwargs.get("verdict") == "changes_requested"


def test_review_no_flags_passes_none_verdict(repo):
    _write_code_complete(repo)
    with patch("lanegate.lifecycle.cmd_review") as mock_review:
        mock_review.return_value = None
        with (
            patch("lanegate.cli.find_repo_root", return_value=repo),
            patch("lanegate.cli.load_config", return_value=_CFG),
            patch("sys.argv", ["lanegate", "review", "TICK-001"]),
        ):
            from lanegate import cli

            cli.main()
        _, kwargs = mock_review.call_args
        assert kwargs.get("verdict") is None
        assert kwargs.get("summary") is None
        assert kwargs.get("findings") is None


def test_cli_fix_dispatches(repo):
    """TICK-348: `lanegate fix TICK-X` dispatches to cmd_fix with the ticket id."""
    _write_code_complete(repo)
    with patch("lanegate.orchestrate.autofix.cmd_fix") as mock_fix:
        mock_fix.return_value = None
        with (
            patch("lanegate.cli.find_repo_root", return_value=repo),
            patch("lanegate.cli.load_config", return_value=_CFG),
            patch("sys.argv", ["lanegate", "fix", "TICK-001"]),
        ):
            from lanegate import cli

            cli.main()
        mock_fix.assert_called_once()
        args, _ = mock_fix.call_args
        assert args[0] == "TICK-001"


# --- lanegate route (TICK-091) ---


def test_route_invokes_cmd_route_with_ticket_id(repo):
    with patch("lanegate.board.cmd_route") as mock_route:
        mock_route.return_value = None
        with (
            patch("lanegate.cli.find_repo_root", return_value=repo),
            patch("lanegate.cli.load_config", return_value=_CFG),
            patch("sys.argv", ["lanegate", "route", "TICK-001"]),
        ):
            from lanegate import cli

            cli.main()
        mock_route.assert_called_once()
        args, kwargs = mock_route.call_args
        assert args[2] == "TICK-001"
        assert kwargs.get("json_output") is False


def test_route_json_flag_passed_through(repo):
    with patch("lanegate.board.cmd_route") as mock_route:
        mock_route.return_value = None
        with (
            patch("lanegate.cli.find_repo_root", return_value=repo),
            patch("lanegate.cli.load_config", return_value=_CFG),
            patch("sys.argv", ["lanegate", "--json", "route", "TICK-001"]),
        ):
            from lanegate import cli

            cli.main()
        _, kwargs = mock_route.call_args
        assert kwargs.get("json_output") is True


# --- lanegate stop ---


def test_stop_routes_to_cmd_stop(repo):
    with patch("lanegate.lifecycle.cmd_stop") as mock_stop:
        mock_stop.return_value = {"ticket_id": "TICK-001", "stopped": False}
        _run_cli(["stop", "TICK-001"], repo)
        mock_stop.assert_called_once_with(
            "TICK-001", _CFG, repo, reason="", grace_seconds=5.0
        )


def test_stop_reason_and_grace_seconds_are_passed_through(repo):
    with patch("lanegate.lifecycle.cmd_stop") as mock_stop:
        mock_stop.return_value = {"ticket_id": "TICK-001", "stopped": False}
        _run_cli(
            ["stop", "TICK-001", "--reason", "operator request", "--grace-seconds", "1.5"],
            repo,
        )
        mock_stop.assert_called_once_with(
            "TICK-001", _CFG, repo, reason="operator request", grace_seconds=1.5
        )


def test_cli_human_review_parser(repo):
    with patch("lanegate.lifecycle.cmd_human_review_approve") as mock_approve:
        _run_cli(
            ["human-review", "TICK-001", "--rationale", "Manually inspected the diff; safe."],
            repo,
        )
        mock_approve.assert_called_once_with(
            "TICK-001", _CFG, repo, rationale="Manually inspected the diff; safe."
        )


def test_cli_human_review_requires_rationale_flag(repo, capsys):
    with pytest.raises(SystemExit) as exc:
        _run_cli(["human-review", "TICK-001"], repo)
    assert exc.value.code != 0
    assert "--rationale" in capsys.readouterr().err


def test_cli_human_review_code_complete_changes_requested(repo):
    """Verify CLI dispatches human-review with rationale for a code_complete ticket with changes_requested."""
    with patch("lanegate.lifecycle.cmd_human_review_approve") as mock_approve:
        _run_cli(
            ["human-review", "TICK-001", "--rationale", "Dismissing false-positive review finding."],
            repo,
        )
        mock_approve.assert_called_once_with(
            "TICK-001", _CFG, repo, rationale="Dismissing false-positive review finding."
        )


def test_install_commands_copies_skills_to_lanegate_subdir(tmp_path, capsys):
    from lanegate import cli

    with patch("sys.argv", ["lanegate", "install-commands"]):
        import os

        orig = os.getcwd()
        os.chdir(tmp_path)
        try:
            cli.main()
        finally:
            os.chdir(orig)

    dest = tmp_path / ".claude" / "commands" / "lanegate"
    assert dest.is_dir(), ".claude/commands/lanegate/ was not created"
    installed = {p.name for p in dest.glob("*.md")}
    assert "implement.md" in installed
    assert "run.md" in installed
    assert "orchestrate.md" in installed
    assert "tickets.md" in installed
    out = capsys.readouterr().out
    assert "/lanegate:implement" in out
    assert "No commands to install" not in out


def test_install_agent_tools_writes_claude_and_codex_configs(tmp_path, capsys):
    from lanegate import cli

    with patch("sys.argv", ["lanegate", "install-agent-tools", "--json"]):
        orig = os.getcwd()
        os.chdir(tmp_path)
        try:
            cli.main()
        finally:
            os.chdir(orig)

    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    agents = {artifact["agent"] for artifact in result["artifacts"]}
    assert {"claude", "codex", "generic-mcp"} <= agents
    assert "recent_logs" in result["tools"]["bounded_tools"]
    assert "continuation_context" in result["tools"]["bounded_tools"]

    claude_dir = tmp_path / ".claude" / "commands" / "lanegate"
    assert (claude_dir / "implement.md").exists()
    assert (claude_dir / "run.md").exists()
    assert (claude_dir / "orchestrate.md").exists()

    codex_config = tmp_path / ".codex" / "mcp" / "lanegate.json"
    assert codex_config.exists()
    codex_payload = json.loads(codex_config.read_text())
    assert codex_payload["mcpServers"]["lanegate"] == {"command": "lanegate", "args": ["mcp"]}

    generic_config = tmp_path / ".lanegate" / "agent-tools" / "mcp-lanegate.json"
    assert generic_config.exists()
    generic_payload = json.loads(generic_config.read_text())
    assert generic_payload == codex_payload


# --- lanegate tui ---


def test_tui_invokes_go_spike_with_fixture(repo):
    fixture = repo / "board.json"
    fixture.write_text('{"tickets": {}, "pipeline": []}')
    fake_bin = repo / "lanegate-tui"
    fake_bin.write_text("#!/bin/sh\n")

    with (
        patch.dict(os.environ, {"LANEGATE_TUI_BIN": str(fake_bin)}),
        patch("lanegate.tui.subprocess.run") as run,
    ):
        _run_cli(["tui", "--fixture", str(fixture)], repo)

    run.assert_called_once_with(
        [str(fake_bin), "--fixture", str(fixture)],
        cwd=None,
        check=True,
    )


def test_tui_reports_go_spike_failure(repo, capsys):
    fixture = repo / "board.json"
    fixture.write_text('{"tickets": {}, "pipeline": []}')
    fake_bin = repo / "lanegate-tui"
    fake_bin.write_text("#!/bin/sh\n")

    with (
        patch.dict(os.environ, {"LANEGATE_TUI_BIN": str(fake_bin)}),
        patch(
            "lanegate.tui.subprocess.run",
            side_effect=subprocess.CalledProcessError(7, [str(fake_bin)]),
        ),
        pytest.raises(SystemExit) as exc,
    ):
        _run_cli(["tui", "--fixture", str(fixture)], repo)

    assert exc.value.code == 7
    assert "Go TUI exited with status 7" in capsys.readouterr().err


# --- lanegate prompts eject ---


def test_prompts_eject_writes_three_templates(repo, capsys):
    """Basic eject: writes all built-in templates (analyze, implement, review,
    fix, drift_check — the last two added by TICK-120's auto-fix loop)."""
    _run_cli(["prompts", "eject"], repo)
    prompts_dir = repo / "prompts"
    assert prompts_dir.is_dir()
    assert (prompts_dir / "analyze.md").exists()
    assert (prompts_dir / "implement.md").exists()
    assert (prompts_dir / "review.md").exists()
    assert (prompts_dir / "fix.md").exists()
    assert (prompts_dir / "drift_check.md").exists()
    out = capsys.readouterr().out
    assert "written:" in out
    assert "5 of 5 template(s) written" in out


def test_prompts_eject_adds_header_comment(repo):
    """Each ejected file starts with a variable-documenting header comment."""
    _run_cli(["prompts", "eject"], repo)
    for name in ("analyze.md", "implement.md", "review.md", "fix.md", "drift_check.md"):
        content = (repo / "prompts" / name).read_text()
        assert content.startswith("<!-- lanegate built-in template:"), (
            f"{name} is missing a header comment"
        )


def test_prompts_eject_skips_existing_without_force(repo, capsys):
    """Without --force, existing files are skipped."""
    prompts_dir = repo / "prompts"
    prompts_dir.mkdir()
    existing = prompts_dir / "analyze.md"
    existing.write_text("my custom analyze prompt")

    _run_cli(["prompts", "eject"], repo)

    # File content must be untouched
    assert existing.read_text() == "my custom analyze prompt"

    out = capsys.readouterr().out
    assert "skipped:" in out
    # The other four should still be written
    assert "4 of 5 template(s) written" in out


def test_prompts_eject_force_overwrites_existing(repo, capsys):
    """With --force, existing files are overwritten."""
    prompts_dir = repo / "prompts"
    prompts_dir.mkdir()
    existing = prompts_dir / "implement.md"
    existing.write_text("old content")

    _run_cli(["prompts", "eject", "--force"], repo)

    new_content = existing.read_text()
    assert new_content != "old content"
    assert "<!-- lanegate built-in template: implement.md" in new_content

    out = capsys.readouterr().out
    assert "skipped:" not in out
    assert "5 of 5 template(s) written" in out


# --- lanegate orchestrator-lock ---


def test_orchestrator_lock_acquire(repo, capsys):
    _run_cli(["orchestrator-lock", "acquire", "--pid", "12345"], repo)
    out = capsys.readouterr().out
    assert "acquired" in out
    assert "12345" in out


def test_orchestrator_lock_acquire_blocks_second(repo, capsys):
    # Acquire with our live PID, then try to acquire with a different PID.
    # The lock check only needs the existing holder to be live; spawning a real
    # child process here makes Windows cmd-based CI vulnerable to Ctrl-C prompts.
    holder_pid = os.getpid()
    contender_pid = holder_pid + 1

    _run_cli(["orchestrator-lock", "acquire", "--pid", str(holder_pid)], repo)
    with pytest.raises(SystemExit) as exc:
        _run_cli(["orchestrator-lock", "acquire", "--pid", str(contender_pid)], repo)
    assert exc.value.code == 1


def test_orchestrator_lock_release(repo, capsys):
    _run_cli(["orchestrator-lock", "acquire", "--pid", "42"], repo)
    capsys.readouterr()
    _run_cli(["orchestrator-lock", "release", "--pid", "42"], repo)
    out = capsys.readouterr().out
    assert "released" in out


def test_orchestrator_lock_release_no_lock(repo, capsys):
    _run_cli(["orchestrator-lock", "release", "--pid", "42"], repo)
    out = capsys.readouterr().out
    assert "No matching" in out


def test_orchestrator_lock_status_free(repo, capsys):
    _run_cli(["orchestrator-lock", "status"], repo)
    out = capsys.readouterr().out
    assert "No orchestrator lock" in out


def test_orchestrator_lock_status_held(repo, capsys):
    _run_cli(["orchestrator-lock", "acquire", "--pid", "99"], repo)
    capsys.readouterr()
    _run_cli(["orchestrator-lock", "status"], repo)
    out = capsys.readouterr().out
    assert "STALE" in out or "HELD" in out


# --- lanegate executor status / reset (TICK-090) ---


_POOL_CFG = dict(
    _CFG,
    executors={
        "claude-1": {"type": "claude-process"},
        "claude-2": {"type": "claude-process"},
    },
    pools={"default": {"executors": ["claude-1", "claude-2"]}},
)


def _run_cli_with_cfg(args: list[str], repo: Path, cfg: dict):
    from lanegate import cli

    with (
        patch("lanegate.cli.find_repo_root", return_value=repo),
        patch("lanegate.cli.load_config", return_value=cfg),
        patch("sys.argv", ["lanegate"] + args),
    ):
        cli.main()


def test_executor_status_routes_to_cmd_executor_status(repo):
    with patch("lanegate.executor.cmd_executor_status") as mock_status:
        _run_cli_with_cfg(["executor", "status"], repo, _POOL_CFG)
    mock_status.assert_called_once()
    args, kwargs = mock_status.call_args
    assert args[0] == _POOL_CFG
    assert args[1] == repo


def test_executor_status_prints_instances(repo, capsys):
    from lanegate.executor import write_cooldown

    write_cooldown(repo, "claude-1", "session_limit")
    _run_cli_with_cfg(["executor", "status"], repo, _POOL_CFG)
    out = capsys.readouterr().out
    assert "claude-1" in out and "cooling down" in out
    assert "claude-2" in out and "active" in out


def test_executor_reset_named_instance_routes_correctly(repo):
    with patch("lanegate.executor.cmd_executor_reset") as mock_reset:
        _run_cli_with_cfg(["executor", "reset", "claude-1"], repo, _POOL_CFG)
    mock_reset.assert_called_once()
    args, kwargs = mock_reset.call_args
    assert args[0] == _POOL_CFG
    assert args[1] == repo
    assert kwargs["name"] == "claude-1"
    assert kwargs["reset_all"] is False


def test_executor_reset_all_routes_correctly(repo):
    with patch("lanegate.executor.cmd_executor_reset") as mock_reset:
        _run_cli_with_cfg(["executor", "reset", "--all"], repo, _POOL_CFG)
    mock_reset.assert_called_once()
    args, kwargs = mock_reset.call_args
    assert kwargs["name"] is None
    assert kwargs["reset_all"] is True


def test_executor_reset_requires_name_or_all(repo):
    with pytest.raises(SystemExit):
        _run_cli_with_cfg(["executor", "reset"], repo, _POOL_CFG)


def test_executor_reset_named_instance_clears_cooldown_file(repo, capsys):
    from lanegate.executor import is_cooling_down, write_cooldown

    write_cooldown(repo, "claude-1", "session_limit")
    _run_cli_with_cfg(["executor", "reset", "claude-1"], repo, _POOL_CFG)
    assert is_cooling_down(repo, "claude-1") is False


def test_executor_reset_all_clears_every_cooldown_file(repo):
    from lanegate.executor import is_cooling_down, write_cooldown

    write_cooldown(repo, "claude-1", "session_limit")
    write_cooldown(repo, "claude-2", "rate_limit")
    _run_cli_with_cfg(["executor", "reset", "--all"], repo, _POOL_CFG)
    assert is_cooling_down(repo, "claude-1") is False
    assert is_cooling_down(repo, "claude-2") is False


# --- orchestrate --status ---


def test_run_status_reports_active_ticket(repo, capsys):
    """lanegate run --status reports active ticket, executor PID, elapsed time, log path."""
    import time

    # Create an active status
    status = {
        "schema_version": 1,
        "ticket_id": "TICK-055",
        "executor": "claude-process",
        "executor_pid": 12345,
        "executor_session": "TICK-055-123456-9999-implement",
        "step": "implement",
        "state": "running",
        "reconciliation_state": "live",
        "started_at": time.time() - 123,  # 123 seconds ago
        "started_at_iso": "2026-01-01T00:00:00Z",
        "log_path": "/tmp/orchestrate-TICK-055.log",
        "audit_bundle_path": "/repo/.lanegate/executor-runs/TICK-055/session",
        "prompt_path": "/tmp/prompt-TICK-055.md",
        "heartbeat_count": 3,
        "last_event": "executor_heartbeat",
        "updated_at": "2026-01-01T00:02:00Z",
    }

    from lanegate.orchestrate import _write_active_status

    with patch("lanegate.cli.find_repo_root", return_value=repo), patch(
        "lanegate.cli.load_config", return_value=_CFG
    ), patch("lanegate.pidutil.pid_alive", return_value=True), patch("sys.argv", ["lanegate", "run", "--status"]):
        _write_active_status(repo, status)
        from lanegate import cli

        cli.main()

    out = capsys.readouterr().out
    assert "TICK-055" in out
    assert "12345" in out or "PID" in out
    assert "2m" in out or "123" in out or "elapsed" in out.lower()
    assert "/tmp/orchestrate-TICK-055.log" in out or "log" in out.lower()
    assert "/repo/.lanegate/executor-runs/TICK-055/session" in out


def test_run_status_json_includes_audit_bundle_path(repo, capsys):
    import json

    from lanegate.orchestrate import _write_active_status

    _write_active_status(
        repo,
        {
            "schema_version": 1,
            "ticket_id": "TICK-055",
            "executor": "codex",
            "executor_session": "TICK-055-session",
            "state": "finished",
            "reconciliation_state": "finished",
            "audit_bundle_path": "/repo/.lanegate/executor-runs/TICK-055/session",
        },
    )
    with patch("lanegate.cli.find_repo_root", return_value=repo), patch(
        "lanegate.cli.load_config", return_value=_CFG
    ), patch("sys.argv", ["lanegate", "--json", "run", "--status"]):
        from lanegate import cli

        cli.main()

    data = json.loads(capsys.readouterr().out)
    assert data["audit_bundle_path"] == "/repo/.lanegate/executor-runs/TICK-055/session"


def test_run_status_no_active_run(repo, capsys):
    """lanegate run --status reports when no run is active."""
    with patch("lanegate.cli.find_repo_root", return_value=repo), patch(
        "lanegate.cli.load_config", return_value=_CFG
    ), patch("sys.argv", ["lanegate", "run", "--status"]):
        from lanegate import cli

        cli.main()

    out = capsys.readouterr().out
    assert "no" in out.lower() or "active" in out.lower() or "none" in out.lower()


# --- lanegate logs ---


def test_logs_tails_latest_lanegate_log(repo, capsys):
    logs_dir = repo / ".lanegate" / "logs"
    logs_dir.mkdir(parents=True)
    older = logs_dir / "orchestrate-20260709-100000.log"
    newer = logs_dir / "orchestrate-20260709-110000.log"
    # Marker text avoids the substring "older", which false-positives on
    # macOS: pytest's tmp_path there lives under /private/var/folders/...,
    # and "folders" itself contains "older".
    older.write_text("stale-log-marker\n", encoding="utf-8")
    newer.write_text("line one\nline two\nline three\n", encoding="utf-8")
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))

    _run_cli(["logs", "--lines", "2", "--color", "never"], repo)

    out = capsys.readouterr().out
    assert "20260709-110000.log" in out
    assert "line two" in out
    assert "line three" in out
    assert "line one" not in out
    assert "stale-log-marker" not in out


def test_logs_color_always_emits_ansi(repo, capsys):
    log_path = repo / ".lanegate" / "logs" / "orchestrate-20260709-120000.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("diff --git a/app.py b/app.py\n+added\nERROR failed\n", encoding="utf-8")

    _run_cli(["logs", "--path", str(log_path), "--lines", "3", "--color", "always"], repo)

    out = capsys.readouterr().out
    assert "\x1b[" in out
    assert "+added" in out
    assert "ERROR failed" in out


def test_logs_open_with_launches_external_viewer(repo):
    log_path = repo / ".lanegate" / "logs" / "orchestrate-20260709-130000.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("hello\n", encoding="utf-8")

    with patch("lanegate.logs.shutil.which", return_value="/usr/bin/lnav") as which, patch(
        "lanegate.logs.subprocess.run"
    ) as run:
        _run_cli(["logs", "--path", str(log_path), "--open-with", "lnav"], repo)

    which.assert_called_once_with("lnav")
    run.assert_called_once_with(["/usr/bin/lnav", str(log_path)], check=False)


def test_logs_open_with_missing_viewer_exits(repo):
    log_path = repo / ".lanegate" / "logs" / "orchestrate-20260709-140000.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("hello\n", encoding="utf-8")

    with patch("lanegate.logs.shutil.which", return_value=None), pytest.raises(SystemExit) as exc:
        _run_cli(["logs", "--path", str(log_path), "--open-with", "lnav"], repo)

    assert "not installed" in str(exc.value)


def test_logs_line_style_prevents_false_positives():
    from lanegate.logs import _line_style, semantic_line_metadata

    bullet_line_1 = "- HTTP 429 rate limit retry"
    bullet_line_2 = "  - HTTP 429 rate limit retry"
    checklist_line = "- [ ] checklist item"

    assert _line_style(bullet_line_1) != "red"
    assert _line_style(bullet_line_2) != "red"
    assert _line_style(checklist_line) != "red"
    assert semantic_line_metadata(bullet_line_1)["level"] != "error"
    assert semantic_line_metadata(bullet_line_2)["level"] != "error"
    assert semantic_line_metadata(checklist_line)["level"] != "error"

    prose_merged = "PR #123 was merged into main"
    prose_passed = "The test passed after retry"
    prose_merged_reverted = "The PR was merged, then reverted"
    prose_no_passed = "No tests passed."
    identifier_approved = "downgrade_approved_review_to_needs_review"

    assert _line_style(prose_merged) != "green"
    assert _line_style(prose_passed) != "green"
    assert _line_style(prose_merged_reverted) != "green"
    assert _line_style(prose_no_passed) != "green"
    assert _line_style(identifier_approved) != "green"
    assert semantic_line_metadata(prose_merged)["level"] != "success"
    assert semantic_line_metadata(prose_passed)["level"] != "success"
    assert semantic_line_metadata(prose_merged_reverted)["level"] != "success"
    assert semantic_line_metadata(prose_no_passed)["level"] != "success"
    assert semantic_line_metadata(identifier_approved)["level"] != "success"

    assert _line_style("-def foo():") == "red"
    assert _line_style("+def foo():") == "green"
    assert _line_style("-    return value") == "red"
    assert _line_style("+    return value") == "green"
    assert _line_style("verdict: approved") == "green"

    # Regression: bullets with two-space or tab indentation must not be
    # mistaken for diff-deletion lines (review attempt 2, finding [2]).
    bullet_two_space = "-  HTTP 429 rate limit retry"
    bullet_tab = "-\tHTTP 429 rate limit retry"
    assert _line_style(bullet_two_space) != "red"
    assert _line_style(bullet_tab) != "red"
    assert semantic_line_metadata(bullet_two_space)["level"] != "error"
    assert semantic_line_metadata(bullet_tab)["level"] != "error"

    # Regression: a colon-prefixed keyword followed by more prose must not
    # be treated as a standalone status line (review attempt 2, finding [1]).
    prose_state_merged_reverted = "The PR state: merged, then reverted"
    assert _line_style(prose_state_merged_reverted) != "green"
    assert semantic_line_metadata(prose_state_merged_reverted)["level"] != "success"


def test_semantic_line_metadata_protocol_scalars():
    from lanegate.logs import semantic_line_metadata

    for scalar in ("42", "null", '"foo"', "-1"):
        assert semantic_line_metadata(scalar)["kind"] == "executor"

    for protocol_line in ('{"type":"item.started"}', "[1, 2, 3]"):
        assert semantic_line_metadata(protocol_line)["kind"] == "protocol"


# ── UTF-8 output hardening (Windows cp1252 regression) ────────────────────────


def test_force_utf8_output_lets_glyphs_print_on_legacy_codepage():
    """Status glyphs must print without crashing even when stdout is cp1252.

    Reproduces the Windows failure (the console/redirected pipe defaults to a
    legacy code page that cannot encode '✓') on any platform: wrap a buffer in a
    cp1252 text stream, run the CLI's UTF-8 hardening, then print a glyph.
    """
    import io
    import sys

    from lanegate.cli import _force_utf8_output

    raw = io.BytesIO()
    legacy = io.TextIOWrapper(raw, encoding="cp1252")

    real_stdout, real_stderr = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = legacy
    try:
        _force_utf8_output()
        print("✓ done")  # would raise UnicodeEncodeError on cp1252 without the fix
        sys.stdout.flush()
    finally:
        sys.stdout, sys.stderr = real_stdout, real_stderr

    assert "✓ done" in raw.getvalue().decode("utf-8")


def test_run_command_and_lane_alias(repo):
    """Assert lane script entrypoint exists in pyproject.toml and both 'run' and 'orchestrate' invoke cmd_orchestrate."""
    import tomllib

    pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    assert "lane" in data["project"]["scripts"]
    assert data["project"]["scripts"]["lane"] == "lanegate.cli:main"

    for subcmd in ["run", "orchestrate"]:
        with patch("lanegate.orchestrate.cmd_orchestrate") as mock_cmd:
            _run_cli([subcmd], repo)
            assert mock_cmd.called, f"Expected cmd_orchestrate to be called for subcommand '{subcmd}'"


def test_orchestrate_executors_flag_parses_comma_list(repo):
    with patch("lanegate.orchestrate.cmd_orchestrate") as mock_cmd:
        _run_cli(["run", "--executors", "agy,claude-b"], repo)
        assert mock_cmd.called
        assert mock_cmd.call_args.kwargs["executors"] == ["agy", "claude-b"]
        assert mock_cmd.call_args.kwargs["pool"] is None


def test_help_all_labels_orchestrate_as_a_compatibility_command():
    from lanegate import cli

    parser = cli.build_parser()
    with patch.object(parser, "_print_message") as print_message, pytest.raises(SystemExit) as exc:
        parser.parse_args(["--help-all"])

    assert exc.value.code == 0
    output = print_message.call_args.args[0]
    assert "run                            Clear the ticket board" in output
    assert "orchestrate                    Compatibility command" in output


def test_cli_help_text_includes_direct_action_tracking():
    from lanegate import cli

    parser = cli.build_parser()
    commands = next(action.choices for action in parser._actions if isinstance(getattr(action, "choices", None), dict))
    assert "direct action IDs" in commands["ps"].format_help()
    fix_parser = commands["fix"]
    assert "stable action ID" in fix_parser.format_help()
