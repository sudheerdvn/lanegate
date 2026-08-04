"""Tests for create.py — cmd_create: id allocation, draft frontmatter, validation."""

import threading
from unittest.mock import patch

import pytest

from lanegate.create import _derive_title, _next_id, cmd_create
from lanegate.ticket import parse_ticket, validate_ticket

_CLI_CFG = {
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


_CFG = {
    "ticket_prefix": "TICK",
    "tickets_dir": "tickets",
    "commit_status_changes": False,  # no git in tests
}


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "tickets").mkdir()
    return tmp_path


# --- _next_id ---


def test_next_id_empty_dir(repo):
    assert _next_id(repo / "tickets", "TICK") == "TICK-001"


def test_next_id_after_existing(repo):
    (repo / "tickets" / "TICK-003.md").write_text(
        "---\nid: TICK-003\ntitle: x\nstatus: open\n---\n"
    )
    assert _next_id(repo / "tickets", "TICK") == "TICK-004"


def test_next_id_matches_slugged_filenames(repo):
    """Regression for TICK-124: projects that slug ticket filenames
    (TICK-148-some-slug.md) must still be parsed for the highest id,
    instead of falling back to TICK-001 and colliding with a real ticket."""
    (repo / "tickets" / "TICK-148-monitor-incident-bridge.md").write_text(
        "---\nid: TICK-148\ntitle: x\nstatus: done\n---\n"
    )
    assert _next_id(repo / "tickets", "TICK") == "TICK-149"


def test_next_id_handles_gap(repo):
    for n in (1, 3, 7):
        (repo / "tickets" / f"TICK-{n:03d}.md").write_text(
            f"---\nid: TICK-{n:03d}\ntitle: x\nstatus: open\n---\n"
        )
    assert _next_id(repo / "tickets", "TICK") == "TICK-008"


def test_next_id_zero_pads_to_3(repo):
    assert _next_id(repo / "tickets", "TICK") == "TICK-001"


def test_next_id_wider_than_3_when_needed(repo):
    (repo / "tickets" / "TICK-999.md").write_text(
        "---\nid: TICK-999\ntitle: x\nstatus: open\n---\n"
    )
    nid = _next_id(repo / "tickets", "TICK")
    assert nid == "TICK-1000"


# --- cmd_create ---


def test_create_writes_file(repo):
    ticket_id = cmd_create("Build a login page", _CFG, repo)
    assert ticket_id == "TICK-001"
    path = repo / "tickets" / "TICK-001.md"
    assert path.exists()


def test_create_draft_status(repo):
    cmd_create("Build a login page", _CFG, repo)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t["status"] == "draft"


def test_create_empty_touches(repo):
    cmd_create("Build a login page", _CFG, repo)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t.get("touches") == []


def test_create_intent_in_body(repo):
    cmd_create("Build a login page", _CFG, repo)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert "Build a login page" in t["_body"]


def test_create_title_from_intent(repo):
    cmd_create("Build a login page", _CFG, repo)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t["title"] == "Build a login page"


def test_create_title_truncated_at_80(repo):
    long_intent = "x" * 120
    cmd_create(long_intent, _CFG, repo)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert len(t["title"]) <= 80


# ---------------------------------------------------------------------------
# TICK-368: title truncation must land on a word/clause boundary, and use
# only the first sentence of a multi-sentence intent.
# ---------------------------------------------------------------------------


class TestDeriveTitle:
    def test_short_intent_unchanged(self):
        assert _derive_title("Build a login page") == "Build a login page"

    def test_no_terminator_truncates_on_word_boundary(self):
        intent = (
            "Recommend scoped Claude Code permissions instead of requiring "
            "--dangerously-skip-permissions"
        )
        title = _derive_title(intent)
        assert len(title) <= 80
        assert not intent[: len(title) + 1].endswith(title + "-")
        # No mid-word cut: title is a prefix ending at a space in the original.
        assert intent[len(title) : len(title) + 1] in ("", " ")

    def test_multi_sentence_uses_first_sentence_only(self):
        intent = (
            "Systematic sweep for drifted duplicate logic across module "
            "boundaries. Six drifted instances found so far."
        )
        title = _derive_title(intent)
        assert title == (
            "Systematic sweep for drifted duplicate logic across module boundaries."
        )
        assert "Six" not in title

    def test_multi_line_intent_uses_first_line(self):
        intent = "First line of the intent\nSecond line, body-only content."
        title = _derive_title(intent)
        assert title == "First line of the intent"

    def test_degenerate_single_token_falls_back_to_hard_cutoff(self):
        title = _derive_title("x" * 120)
        assert len(title) <= 80

    def test_custom_max_len(self):
        assert _derive_title("one two three four five", max_len=13) == "one two three"

    def test_create_uses_derive_title(self, repo):
        """cmd_create must not have its own separate truncation logic —
        regression guard against the duplicate-drift class this project
        keeps re-discovering."""
        intent = (
            "Recommend scoped Claude Code permissions instead of requiring "
            "--dangerously-skip-permissions"
        )
        cmd_create(intent, _CFG, repo)
        t = parse_ticket(repo / "tickets" / "TICK-001.md")
        assert t["title"] == _derive_title(intent)
        assert not t["title"].endswith("requi")


def test_create_passes_validate_ticket(repo):
    cmd_create("Build a login page", _CFG, repo)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    errors = validate_ticket({k: v for k, v in t.items() if not k.startswith("_")})
    assert errors == []


def test_create_no_collision(repo):
    id1 = cmd_create("First task", _CFG, repo)
    id2 = cmd_create("Second task", _CFG, repo)
    assert id1 != id2
    assert id1 == "TICK-001"
    assert id2 == "TICK-002"


def test_create_prints_id(repo, capsys):
    cmd_create("Build a login page", _CFG, repo)
    out = capsys.readouterr().out.strip()
    assert out == "TICK-001"


def test_create_autonomy_supervised(repo):
    cmd_create("Build a login page", _CFG, repo)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t.get("autonomy") == "supervised"


def test_create_autonomy_uses_project_default(repo):
    cmd_create("Build a login page", dict(_CFG, autonomy="full"), repo)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t.get("autonomy") == "full"


# --- analyze failure path (via CLI) ---

# ---------------------------------------------------------------------------
# TICK-043: no --no-verify in git commits
# ---------------------------------------------------------------------------


def test_create_commit_does_not_use_no_verify(repo):
    """cmd_create must not pass --no-verify to git commit so hooks run normally."""
    import subprocess as _sp

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        result = _sp.CompletedProcess(cmd, 0)
        return result

    cfg_with_commit = dict(_CFG, commit_status_changes=True)

    with patch("lanegate.create.subprocess.run", side_effect=fake_run):
        cmd_create("Test no-verify removal", cfg_with_commit, repo)

    git_commits = [c for c in calls if "commit" in c]
    for cmd in git_commits:
        assert "--no-verify" not in cmd, f"cmd_create used --no-verify in git commit: {cmd}"


def test_create_commits_ticket_when_tickets_dir_gitignored(repo):
    """cmd_create force-adds the ticket file so it commits even when tickets_dir is gitignored."""
    import shutil
    import subprocess as _subprocess

    if shutil.which("git") is None:
        pytest.skip("git is required for create commit integration test")

    (repo / ".gitignore").write_text("tickets/*\n")
    _subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    _subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    _subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    _subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    cmd_create("Test durable commit", dict(_CFG, commit_status_changes=True), repo)

    ticket_tracked = _subprocess.run(
        ["git", "ls-files", "tickets/TICK-001.md"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert ticket_tracked.stdout.strip() == "tickets/TICK-001.md", "draft ticket file should be committed when commit_status_changes=True"


def test_create_cli_analyze_passes_keep_draft(repo):
    """CLI create passes keep_draft=True to cmd_analyze so ticket stays draft."""
    from lanegate import cli

    calls = []

    def capturing_analyze(ticket_id, cfg, repo_root, *, keep_draft=False, **kw):
        calls.append({"ticket_id": ticket_id, "keep_draft": keep_draft})

    with (
        patch("lanegate.cli.find_repo_root", return_value=repo),
        patch("lanegate.cli.load_config", return_value=_CLI_CFG),
        patch("lanegate.analyze.cmd_analyze", side_effect=capturing_analyze),
        patch("sys.argv", ["lanegate", "create", "Build a login page"]),
    ):
        cli.main()

    assert calls, "cmd_analyze was not called"
    assert calls[0]["keep_draft"] is True


def test_create_analyze_failure_leaves_draft_and_exits_zero(repo, capsys):
    """When analyze raises, ticket stays draft, exit code is 0, helpful message to stderr."""
    from lanegate import cli

    with (
        patch("lanegate.cli.find_repo_root", return_value=repo),
        patch("lanegate.cli.load_config", return_value=_CLI_CFG),
        patch("lanegate.analyze.cmd_analyze", side_effect=RuntimeError("model unavailable")),
        patch("sys.argv", ["lanegate", "create", "Build a login page"]),
    ):
        # Must not raise SystemExit
        cli.main()

    # ticket file must exist and stay at draft status
    path = repo / "tickets" / "TICK-001.md"
    assert path.exists(), "ticket file was not created"
    t = parse_ticket(path)
    assert t["status"] == "draft", f"expected draft, got {t['status']}"

    # helpful next-step message must appear on stderr
    captured = capsys.readouterr()
    assert "TICK-001" in captured.err
    assert "lanegate analyze TICK-001" in captured.err


# ---------------------------------------------------------------------------
# Milestone support in cmd_create
# ---------------------------------------------------------------------------


def test_create_milestone_flag_writes_field(repo):
    """--milestone flag writes the milestone field into the ticket frontmatter."""
    cmd_create("Build a feature", _CFG, repo, milestone="v1")
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t.get("milestone") == "v1"


def test_create_milestone_from_config_default(repo):
    """default_milestone in cfg writes the milestone field without prompting."""
    cfg_with_ms = dict(_CFG, default_milestone="sprint-1")
    cmd_create("Build a feature", cfg_with_ms, repo)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t.get("milestone") == "sprint-1"


def test_create_milestone_flag_overrides_config_default(repo):
    """Explicit milestone flag takes precedence over default_milestone in cfg."""
    cfg_with_ms = dict(_CFG, default_milestone="v1")
    cmd_create("Build a feature", cfg_with_ms, repo, milestone="v2")
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t.get("milestone") == "v2"


def test_create_no_milestone_no_default_no_tty_omits_field(repo, capsys):
    """When no milestone and stdin is not a TTY, ticket is created without milestone field."""
    import sys

    with patch.object(sys.stdin, "isatty", return_value=False):
        cmd_create("Build a feature", _CFG, repo)

    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t.get("milestone") is None

    # Should print a reminder to stderr
    captured = capsys.readouterr()
    assert "untagged" in captured.err.lower() or "milestone" in captured.err.lower()


def test_create_interactive_prompt_accepts_value(repo):
    """When stdin is a TTY and user provides a value, milestone is written."""
    import sys

    with (
        patch.object(sys.stdin, "isatty", return_value=True),
        patch("builtins.input", return_value="v3"),
    ):
        cmd_create("Build a feature", _CFG, repo)

    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t.get("milestone") == "v3"


def test_create_interactive_prompt_empty_accepted_omits_field(repo, capsys):
    """When user presses Enter (empty) and no bracket default, milestone is omitted."""
    import sys

    with (
        patch.object(sys.stdin, "isatty", return_value=True),
        patch("builtins.input", return_value=""),
    ):
        cmd_create("Build a feature", _CFG, repo)

    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t.get("milestone") is None

    # Tip message should appear on stderr
    captured = capsys.readouterr()
    assert "untagged" in captured.err.lower() or "milestone" in captured.err.lower()


def test_create_interactive_prompt_empty_with_config_default_uses_default(repo):
    """Empty input when cfg has default_milestone uses the config default."""
    import sys

    cfg_with_ms = dict(_CFG, default_milestone="v1")
    with (
        patch.object(sys.stdin, "isatty", return_value=True),
        patch("builtins.input", return_value=""),
    ):
        cmd_create("Build a feature", cfg_with_ms, repo)

    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t.get("milestone") == "v1"


def test_create_milestone_passes_validate_ticket(repo):
    """A ticket created with a milestone field passes schema validation."""
    cmd_create("Build a feature", _CFG, repo, milestone="v1")
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    errors = validate_ticket({k: v for k, v in t.items() if not k.startswith("_")})
    assert errors == []


def test_create_concurrent_no_collision(repo):
    """Concurrent creates must allocate distinct ticket IDs and not overwrite drafts."""
    results = []
    errors = []

    def create_in_thread(intent):
        try:
            ticket_id = cmd_create(intent, _CFG, repo)
            results.append(ticket_id)
        except Exception as e:
            errors.append(e)

    threads = [
        threading.Thread(target=create_in_thread, args=(f"Task {i}",))
        for i in range(5)
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Should have no errors
    assert errors == [], f"Concurrent creates raised errors: {errors}"

    # Should have 5 distinct ticket IDs
    assert len(results) == 5, f"Expected 5 ticket IDs, got {len(results)}"
    assert len(set(results)) == 5, f"Duplicate ticket IDs detected: {results}"

    # All tickets should exist and have different intents
    ticket_intents = {}
    for ticket_id in results:
        path = repo / "tickets" / f"{ticket_id}.md"
        assert path.exists(), f"Ticket file {ticket_id}.md does not exist"
        t = parse_ticket(path)
        ticket_intents[ticket_id] = t.get("_body", "")

    # Verify each ticket has its own body (not overwritten)
    task_nums = set()
    for body in ticket_intents.values():
        for i in range(5):
            if f"Task {i}" in body:
                task_nums.add(i)

    assert len(task_nums) == 5, f"Not all tasks found in ticket bodies; found {task_nums}"
