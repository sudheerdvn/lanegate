"""Tests for lifecycle review, needs-review, and human-approval commands."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lanegate.lifecycle import (
    _clear_human_review_approval,
    _mark_needs_review,
    cmd_human_review_approve,
    cmd_needs_review,
    cmd_reopen,
    cmd_review,
    record_auto_fix_attempt,
)
from lanegate.ticket import parse_ticket, write_ticket
from tests._helpers.lifecycle import (
    commit_all as _commit_all,
    default_cfg as _default_cfg,
    init_git_repo as _init_git_repo,
    start_cfg as _start_cfg,
    write_ticket as _write_ticket,
    write_ticket_with_body as _write_ticket_with_body,
)

def _review_cfg(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir(exist_ok=True)
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir(exist_ok=True)
    _write_ticket(tickets_dir, "TICK-001", "code_complete")
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)
    return cfg, tickets_dir


def test_review_no_verdict_backward_compat(tmp_path):
    cfg, tickets_dir = _review_cfg(tmp_path)
    cfg["reviewer"] = "none"
    cmd_review("TICK-001", cfg, tmp_path)
    from lanegate.ticket import parse_ticket

    t = parse_ticket(tickets_dir / "TICK-001.md")
    assert t["status"] == "in_review"
    assert t.get("review_verdict") is None


def test_review_no_verdict_dispatches_llm_reviewer_when_configured(tmp_path):
    cfg, tickets_dir = _review_cfg(tmp_path)
    with patch("lanegate.orchestrate.review.run_review_agent") as mock_run:
        cmd_review("TICK-001", cfg, tmp_path)

    mock_run.assert_called_once()
    args, kwargs = mock_run.call_args
    assert args[0]["id"] == "TICK-001"
    assert args[1] == tmp_path
    assert kwargs["cfg"] == cfg

    from lanegate.ticket import parse_ticket

    t = parse_ticket(tickets_dir / "TICK-001.md")
    assert t["status"] == "code_complete"


def test_review_no_verdict_human_reviewer_flips_with_warning(tmp_path, capsys):
    cfg, tickets_dir = _review_cfg(tmp_path)
    cfg["reviewer"] = "human"
    cmd_review("TICK-001", cfg, tmp_path)

    captured = capsys.readouterr()
    assert "human" in captured.err

    from lanegate.ticket import parse_ticket

    t = parse_ticket(tickets_dir / "TICK-001.md")
    assert t["status"] == "in_review"


def test_review_approved_flips_status_and_stores_verdict(tmp_path):
    cfg, tickets_dir = _review_cfg(tmp_path)
    cmd_review("TICK-001", cfg, tmp_path, verdict="approved", summary="LGTM")
    from lanegate.ticket import parse_ticket

    t = parse_ticket(tickets_dir / "TICK-001.md")
    assert t["status"] == "in_review"
    assert t["review_verdict"] == "approved"
    assert t["review_summary"] == "LGTM"


def test_review_changes_requested_exits_nonzero_keeps_code_complete(tmp_path):
    cfg, tickets_dir = _review_cfg(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        cmd_review(
            "TICK-001",
            cfg,
            tmp_path,
            verdict="changes_requested",
            summary="Needs tests",
            findings="- Missing unit test for edge case X",
        )
    assert exc_info.value.code == 1
    from lanegate.ticket import parse_ticket

    t = parse_ticket(tickets_dir / "TICK-001.md")
    assert t["status"] == "code_complete"
    assert t["review_verdict"] == "changes_requested"
    assert t["review_summary"] == "Needs tests"


def test_review_with_verdict_clears_stale_retry_attempt_counter(tmp_path):
    """A real verdict resolves the incident, so a leftover per-ticket-lifetime
    review_retry_attempt from an earlier, unrelated cooldown must not carry
    forward and falsely exhaust the budget on a later, unrelated one (TICK-517)."""
    cfg, tickets_dir = _review_cfg(tmp_path)
    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-001.md")
    ticket["review_retry_attempt"] = 3
    ticket["review_retry_after"] = "2026-08-01T00:00:00Z"
    write_ticket(ticket)

    cmd_review("TICK-001", cfg, tmp_path, verdict="approved", summary="LGTM")

    t = parse_ticket(tickets_dir / "TICK-001.md")
    assert "review_retry_attempt" not in t
    assert "review_retry_after" not in t


def _last_action_end_event(tmp_path):
    log_paths = sorted((tmp_path / ".lanegate" / "logs").glob("action-*.events.jsonl"))
    assert log_paths, "expected an action-*.events.jsonl file to be written"
    events = [json.loads(line) for line in log_paths[-1].read_text(encoding="utf-8").splitlines() if line]
    action_ends = [e for e in events if e.get("event") == "action_end"]
    assert action_ends, "expected at least one action_end event"
    return action_ends[-1]


def test_review_approved_action_end_records_verdict(tmp_path):
    cfg, tickets_dir = _review_cfg(tmp_path)
    cmd_review("TICK-001", cfg, tmp_path, verdict="approved", summary="LGTM")

    event = _last_action_end_event(tmp_path)
    assert event["verdict"] == "approved"
    assert event["review_summary"] == "LGTM"
    assert event["status"] == "success"


def test_review_changes_requested_action_end_records_verdict(tmp_path):
    cfg, tickets_dir = _review_cfg(tmp_path)
    with pytest.raises(SystemExit):
        cmd_review(
            "TICK-001",
            cfg,
            tmp_path,
            verdict="changes_requested",
            summary="Needs tests",
            findings="- Missing unit test for edge case X",
        )

    event = _last_action_end_event(tmp_path)
    assert event["verdict"] == "changes_requested"
    assert event["review_summary"] == "Needs tests"


def test_review_action_end_omits_stale_verdict_from_prior_call(tmp_path):
    cfg, tickets_dir = _review_cfg(tmp_path)
    cmd_review("TICK-001", cfg, tmp_path, verdict="approved", summary="LGTM")

    ticket = parse_ticket(tickets_dir / "TICK-001.md")
    ticket["status"] = "merged"
    write_ticket(ticket)

    with pytest.raises(SystemExit) as exc_info:
        cmd_review("TICK-001", cfg, tmp_path, verdict="changes_requested", summary="Needs tests")
    assert exc_info.value.code == 1

    event = _last_action_end_event(tmp_path)
    assert event["status"] == "failed"
    assert "verdict" not in event
    assert "review_summary" not in event


def test_review_action_end_stays_failure_when_crash_follows_approved_write(tmp_path, monkeypatch):
    cfg, tickets_dir = _review_cfg(tmp_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("push failed")

    monkeypatch.setattr(
        "lanegate.lifecycle.review_cmds._commit_generated_ticket_write", _boom
    )
    with pytest.raises(RuntimeError):
        cmd_review("TICK-001", cfg, tmp_path, verdict="approved", summary="LGTM")

    from lanegate.orchestrate.run_summary import _build_direct_action_summary

    log_paths = sorted((tmp_path / ".lanegate" / "logs").glob("action-*.events.jsonl"))
    action_id = log_paths[-1].stem.removesuffix(".events")
    summary = _build_direct_action_summary(tmp_path, action_id)
    from lanegate.orchestrate.run_report import TicketOutcomeStatus

    assert summary.batch_tickets[0].outcome == TicketOutcomeStatus.FAILURE


def test_review_tracking_preserves_audit_event_when_ticket_load_fails(tmp_path, monkeypatch):
    """Ticket-read failures must not replace a review failure or lose action_end."""
    cfg, _ = _review_cfg(tmp_path)

    def _unreadable_tickets(*args, **kwargs):
        raise OSError("ticket directory unreadable")

    monkeypatch.setattr("lanegate.lifecycle.review_cmds.load_all_tickets", _unreadable_tickets)

    with pytest.raises(OSError, match="ticket directory unreadable"):
        cmd_review("TICK-001", cfg, tmp_path, verdict="changes_requested")

    event = _last_action_end_event(tmp_path)
    assert event["status"] == "failed"
    assert "verdict" not in event


def test_review_changes_requested_appends_findings_to_body(tmp_path):
    cfg, tickets_dir = _review_cfg(tmp_path)
    with pytest.raises(SystemExit):
        cmd_review(
            "TICK-001",
            cfg,
            tmp_path,
            verdict="changes_requested",
            findings="- Missing edge case\n- Add type hints",
        )
    from lanegate.ticket import parse_ticket

    t = parse_ticket(tickets_dir / "TICK-001.md")
    assert "## Review Findings" in t["_body"]
    assert "Missing edge case" in t["_body"]


def test_review_approved_with_findings_appends_to_body(tmp_path):
    cfg, tickets_dir = _review_cfg(tmp_path)
    cmd_review(
        "TICK-001", cfg, tmp_path, verdict="approved", findings="- Minor: rename `x` to `count`"
    )
    from lanegate.ticket import parse_ticket

    t = parse_ticket(tickets_dir / "TICK-001.md")
    assert t["status"] == "in_review"
    assert "## Review Findings" in t["_body"]


def test_re_review_appends_rather_than_replacing_earlier_findings(tmp_path):
    """TICK-343: a changes_requested → fix → re-review cycle used to destroy
    the findings that motivated the fix, which is exactly when both sets
    matter."""
    from lanegate.ticket import parse_ticket

    cfg, tickets_dir = _review_cfg(tmp_path)
    ticket_path = tickets_dir / "TICK-001.md"

    with pytest.raises(SystemExit):
        cmd_review(
            "TICK-001",
            cfg,
            tmp_path,
            verdict="changes_requested",
            summary="first pass",
            findings="- [P0] unbounded read",
        )

    # The fix agent leaves the ticket at code_complete for the re-review.
    cmd_review(
        "TICK-001",
        cfg,
        tmp_path,
        verdict="approved",
        summary="second pass",
        findings="- [P2] rename a variable",
    )

    t = parse_ticket(ticket_path)
    assert "unbounded read" in t["_body"], "first reviewer's findings were destroyed"
    assert "rename a variable" in t["_body"]
    assert "## Review Findings (attempt 1)" in t["_body"]
    assert "## Review Findings (attempt 2)" in t["_body"]

    # Frontmatter stays single-valued and reflects the latest verdict — the
    # board needs current state, not a history.
    assert t["review_verdict"] == "approved"
    assert t["review_summary"] == "second pass"


def test_re_review_numbers_from_a_legacy_unnumbered_findings_section(tmp_path):
    """Tickets reviewed before TICK-343 have a bare ``## Review Findings``;
    it counts as attempt 1 and must survive the next review."""
    from lanegate.ticket import parse_ticket

    cfg, tickets_dir = _review_cfg(tmp_path)
    ticket_path = tickets_dir / "TICK-001.md"
    ticket_path.write_text(
        ticket_path.read_text().rstrip()
        + "\n\n## Review Findings\n\n- [P1] legacy finding\n"
    )

    cmd_review("TICK-001", cfg, tmp_path, verdict="approved", findings="- [P2] new finding")

    t = parse_ticket(ticket_path)
    assert "legacy finding" in t["_body"]
    assert "## Review Findings (attempt 2)" in t["_body"]


def test_latest_review_findings_feeds_the_fix_agent_the_newest_set(tmp_path):
    """The fix agent must address the review that just ran, not the first one."""
    from lanegate.orchestrate.autofix import _extract_review_findings
    from lanegate.ticket import parse_ticket

    cfg, tickets_dir = _review_cfg(tmp_path)
    with pytest.raises(SystemExit):
        cmd_review(
            "TICK-001", cfg, tmp_path, verdict="changes_requested", findings="- old finding"
        )
    with pytest.raises(SystemExit):
        cmd_review(
            "TICK-001", cfg, tmp_path, verdict="changes_requested", findings="- new finding"
        )

    t = parse_ticket(tickets_dir / "TICK-001.md")
    assert _extract_review_findings(t) == "- new finding"


def test_review_findings_stored_in_frontmatter(tmp_path):
    """Findings are stored as a list in review_findings frontmatter for re-review checklist."""
    cfg, tickets_dir = _review_cfg(tmp_path)
    with pytest.raises(SystemExit):
        cmd_review(
            "TICK-001",
            cfg,
            tmp_path,
            verdict="changes_requested",
            findings="- Missing unit test for edge case X\n- Add type hints\n- Needs docstring",
        )
    from lanegate.ticket import parse_ticket

    t = parse_ticket(tickets_dir / "TICK-001.md")
    assert t.get("review_findings") == [
        "- Missing unit test for edge case X",
        "- Add type hints",
        "- Needs docstring",
    ]


def test_review_rejection_from_in_review_returns_to_code_complete(tmp_path):
    """A manual rejection reopens the normal fix/re-review lifecycle."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir(exist_ok=True)
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir(exist_ok=True)
    # Create a ticket already in_review state
    _write_ticket(tickets_dir, "TICK-001", "in_review")
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    with pytest.raises(SystemExit):
        cmd_review(
            "TICK-001",
            cfg,
            tmp_path,
            verdict="changes_requested",
            findings="- Item 1\n- Item 2",
        )
    from lanegate.ticket import parse_ticket

    t = parse_ticket(tickets_dir / "TICK-001.md")
    assert t["status"] == "code_complete"
    assert t.get("review_findings") == ["- Item 1", "- Item 2"]
    assert t["review_verdict"] == "changes_requested"


def test_review_rejects_wrong_status(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-001", "in_progress")
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)
    with pytest.raises(SystemExit):
        cmd_review("TICK-001", cfg, tmp_path, verdict="approved")


def test_cmd_review_needs_review_error_suggests_human_review(tmp_path, capsys):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-001", "needs_review")
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)
    with pytest.raises(SystemExit) as exc_info:
        cmd_review("TICK-001", cfg, tmp_path, verdict="approved")
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "human-review" in err
    assert "--rationale" in err



# ---------------------------------------------------------------------------
# TICK-039: merge precondition — require in_review + approved verdict
# ---------------------------------------------------------------------------



# Tests moved from test_lifecycle.py with lifecycle.review_cmds.

def test_review_advances_from_code_complete(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-001", "code_complete")
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)
    cfg["reviewer"] = "none"
    cmd_review("TICK-001", cfg, tmp_path)
    from lanegate.ticket import parse_ticket

    assert parse_ticket(tickets_dir / "TICK-001.md")["status"] == "in_review"


def test_review_approved_creates_pr_and_stores_in_frontmatter(tmp_path):
    """cmd_review with verdict=approved pushes branch and stores pr_number/pr_url in ticket."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    # Include branch in the ticket so the helper is called
    _write_ticket(tickets_dir, "TICK-001", "code_complete", branch="tick-001")
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)
    cfg["github_pr"] = True  # explicit opt-in; default is False

    def mock_run(args, **kwargs):
        if "remote" in args and "get-url" in args:
            return MagicMock(returncode=0, stdout="git@github.com:org/repo.git", stderr="")
        if "push" in args:
            return MagicMock(returncode=0, stdout="", stderr="")
        if "pr" in args and "view" in args:
            return MagicMock(returncode=1, stdout="", stderr="no pull requests found")
        if "pr" in args and "create" in args:
            return MagicMock(
                returncode=0, stdout="https://github.com/org/repo/pull/99\n", stderr=""
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.lifecycle.shutil.which", return_value="/usr/bin/gh"),
        patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run),
    ):
        cmd_review("TICK-001", cfg, tmp_path, verdict="approved")

    from lanegate.ticket import parse_ticket

    t = parse_ticket(tickets_dir / "TICK-001.md")
    assert t["status"] == "in_review"
    assert t["pr_number"] == 99
    assert t["pr_url"] == "https://github.com/org/repo/pull/99"


def test_review_approved_skips_pr_when_no_branch(tmp_path):
    """cmd_review approved without a branch field skips PR creation silently."""
    cfg, tickets_dir = _review_cfg(tmp_path)

    # No mock needed — gh should never be called because branch is absent
    gh_calls = []

    def mock_run(args, **kwargs):
        gh_calls.append(list(args))
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.lifecycle.shutil.which", return_value="/usr/bin/gh"),
        patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run),
    ):
        cmd_review("TICK-001", cfg, tmp_path, verdict="approved")

    from lanegate.ticket import parse_ticket

    t = parse_ticket(tickets_dir / "TICK-001.md")
    assert t["status"] == "in_review"
    assert t.get("pr_url") is None
    # gh pr create must not have been called
    assert not any("create" in c for c in gh_calls)


def test_review_no_verdict_skips_pr(tmp_path):
    """cmd_review without verdict (backward compat) does not attempt PR creation."""
    cfg, tickets_dir = _review_cfg(tmp_path)
    cfg["reviewer"] = "none"

    gh_calls = []

    def mock_run(args, **kwargs):
        gh_calls.append(list(args))
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.lifecycle.shutil.which", return_value="/usr/bin/gh"),
        patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run),
    ):
        cmd_review("TICK-001", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    t = parse_ticket(tickets_dir / "TICK-001.md")
    assert t["status"] == "in_review"
    assert t.get("pr_url") is None
    assert not any("create" in c for c in gh_calls)


def test_commit_status_true_calls_commit(tmp_path):
    """commit_status_changes=True triggers _commit_status on status transitions."""
    if shutil.which("git") is None:
        pytest.skip("git is required for status commit integration test")

    _init_git_repo(tmp_path)
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-001", "code_complete")
    _commit_all(tmp_path)
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = "tickets"
    cfg["worktrees_dir"] = "worktrees"
    cfg["commit_status_changes"] = True

    commit_calls = []
    with patch(
        "lanegate.lifecycle._commit_status",
        side_effect=lambda *a, **kw: commit_calls.append(a) or True,
    ):
        cmd_review("TICK-001", cfg, tmp_path, verdict="approved")

    assert len(commit_calls) >= 1, "_commit_status not called with commit_status_changes=True"


def test_github_pr_false_suppresses_push(tmp_path):
    """github_pr=False prevents _push_branch_and_open_pr from being called."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-001", "code_complete", branch="tick-001")
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)
    cfg["github_pr"] = False

    push_calls = []
    with patch(
        "lanegate.lifecycle.review_cmds._push_branch_and_open_pr",
        side_effect=lambda *a, **kw: push_calls.append(a) or None,
    ):
        cmd_review("TICK-001", cfg, tmp_path, verdict="approved")

    assert not push_calls, (
        f"_push_branch_and_open_pr called {len(push_calls)} times with github_pr=False"
    )

    from lanegate.ticket import parse_ticket

    t = parse_ticket(tickets_dir / "TICK-001.md")
    assert t["status"] == "in_review"
    assert t.get("pr_url") is None


def test_github_pr_true_calls_push(tmp_path):
    """github_pr=True calls _push_branch_and_open_pr on approved review."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-001", "code_complete", branch="tick-001")
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)
    cfg["github_pr"] = True

    push_calls = []
    with patch(
        "lanegate.lifecycle.review_cmds._push_branch_and_open_pr",
        side_effect=lambda *a, **kw: push_calls.append(a) or None,
    ):
        cmd_review("TICK-001", cfg, tmp_path, verdict="approved")

    assert len(push_calls) == 1, "_push_branch_and_open_pr not called once with github_pr=True"


def test_review_from_linked_worktree_commits_status_on_control_branch(tmp_path):
    """A lifecycle command run from an environment worktree must not commit status there."""
    if shutil.which("git") is None:
        pytest.skip("git is required for linked-worktree integration test")

    main = tmp_path / "main"
    stage = tmp_path / "stage"
    subprocess.run(["git", "init", "-b", "main", str(main)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=main, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=main, check=True)

    tickets_dir = main / "tickets"
    worktrees_dir = main / "worktrees"
    tickets_dir.mkdir()
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-001", "code_complete")
    (main / ".lanegate.yml").write_text(
        "ticket_prefix: TICK\n"
        "tickets_dir: tickets\n"
        "worktrees_dir: worktrees\n"
        "commit_status_changes: true\n"
    )
    subprocess.run(["git", "add", "."], cwd=main, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=main, check=True, capture_output=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", "stage", str(stage), "main"],
        cwd=main,
        check=True,
        capture_output=True,
    )

    cfg = _default_cfg(stage / "tickets", stage / "worktrees")
    cfg["tickets_dir"] = "tickets"
    cfg["worktrees_dir"] = "worktrees"
    cfg["commit_status_changes"] = True

    cmd_review("TICK-001", cfg, stage, verdict="approved")

    from lanegate.ticket import parse_ticket

    assert parse_ticket(main / "tickets" / "TICK-001.md")["status"] == "in_review"
    assert parse_ticket(stage / "tickets" / "TICK-001.md")["status"] == "code_complete"

    main_head = subprocess.run(
        ["git", "rev-parse", "main"],
        cwd=main,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    stage_head = subprocess.run(
        ["git", "rev-parse", "stage"],
        cwd=main,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert main_head != stage_head, "status commit should advance the control branch"
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", "stage", "main"],
        cwd=main,
    ).returncode == 0
    assert subprocess.run(
        ["git", "status", "--short"],
        cwd=stage,
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""


def test_record_auto_fix_attempt_appends_unique_section_per_attempt(tmp_path):
    """TICK-120: each auto-fix attempt gets its own '## Auto-Fix Attempt N'
    section, not a shared header that would clobber earlier attempts."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-001", "code_complete")
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    from lanegate.lifecycle import record_auto_fix_attempt

    record_auto_fix_attempt(
        "TICK-001", cfg, tmp_path, attempt=1, max_attempts=3, note="first attempt note"
    )
    record_auto_fix_attempt(
        "TICK-001", cfg, tmp_path, attempt=2, max_attempts=3, note="second attempt note"
    )

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-001.md")
    assert "## Auto-Fix Attempt 1" in ticket["_body"]
    assert "first attempt note" in ticket["_body"]
    assert "## Auto-Fix Attempt 2" in ticket["_body"]
    assert "second attempt note" in ticket["_body"]
    assert ticket["auto_fix_attempts"] == 2


def test_record_auto_fix_attempt_escalate_sets_summary_leaves_status_and_verdict(tmp_path):
    """Escalation must leave status=code_complete, review_verdict=changes_requested
    unchanged — cmd_blocked() and cmd_merge's guard both key off that exact pair."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-001", "code_complete", review_verdict="changes_requested")
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    from lanegate.lifecycle import record_auto_fix_attempt

    record_auto_fix_attempt(
        "TICK-001",
        cfg,
        tmp_path,
        attempt=1,
        max_attempts=1,
        note="auto-fix attempts exhausted (1/1) — escalated for human review",
        escalate=True,
    )

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-001.md")
    assert ticket["status"] == "code_complete"
    assert ticket["review_verdict"] == "changes_requested"
    assert ticket["review_summary"] == "auto-fix attempts exhausted (1/1) — escalated for human review"


def test_record_auto_fix_attempt_persists_structured_drift_result(tmp_path):
    """TICK-348: drift_ok/drift_reason are persisted as a structured
    drift_check_result field, not only prose in the attempt note."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-001", "code_complete", review_verdict="changes_requested")
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    from lanegate.lifecycle import record_auto_fix_attempt

    record_auto_fix_attempt(
        "TICK-001",
        cfg,
        tmp_path,
        attempt=1,
        max_attempts=1,
        note="auto-fix escalated: drift-check failed (attempt 1/1): touched unrelated file",
        escalate=True,
        drift_ok=False,
        drift_reason="touched unrelated file",
    )

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-001.md")
    assert ticket["drift_check_result"] == {"ok": False, "reason": "touched unrelated file"}


def test_needs_review_preserves_worktree_and_records_reason(tmp_path):
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    wt = tmp_path / "worktrees" / "tick-121"
    wt.mkdir(parents=True)
    (tickets_dir / "TICK-121.md").write_text(
        "---\n"
        "id: TICK-121\n"
        "title: Test TICK-121\n"
        "status: in_progress\n"
        "touches:\n"
        "  - lanegate/lifecycle.py\n"
        f"worktree: {wt}\n"
        "branch: tick-121\n"
        "---\nBody text.\n"
    )

    cmd_needs_review("TICK-121", cfg, tmp_path, reason="merge conflict")

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-121.md")
    assert ticket["status"] == "needs_review"
    assert ticket["worktree"] == str(wt)
    assert "merge conflict" in ticket["_body"]


def test_needs_review_escalates_rejected_code_complete_ticket(tmp_path):
    """Manual escalation releases a rejected code_complete touch lock audibly."""
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    wt = tmp_path / "worktrees" / "tick-122"
    wt.mkdir(parents=True)
    (tickets_dir / "TICK-122.md").write_text(
        f"---\nid: TICK-122\ntitle: Test TICK-122\nstatus: code_complete\n"
        f"worktree: {wt}\nbranch: tick-122\nreview_verdict: changes_requested\n"
        "---\nBody text.\n"
    )

    cmd_needs_review("TICK-122", cfg, tmp_path, reason="auto-fix budget exhausted")

    ticket = parse_ticket(tickets_dir / "TICK-122.md")
    assert ticket["status"] == "needs_review"
    assert ticket["review_verdict"] == "changes_requested"
    assert ticket["worktree"] == str(wt)
    assert "auto-fix budget exhausted" in ticket["_body"]
    assert "code_complete → needs_review" in ticket["_body"]


def test_human_review_approval(tmp_path):
    """A needs_review ticket blocked on a hard-blocked (protected) path cannot be
    auto-resumed by `lanegate reopen` -- it requires an audited human approval via
    `lanegate human-review`, which requires a rationale, records it in ticket
    history, preserves the worktree/commits exactly as-is, advances only to
    code_complete (never touching review/merge), and unblocks `lanegate reopen`
    once approved."""
    if shutil.which("git") is None:
        pytest.skip("git is required for this test")

    from lanegate.lifecycle import cmd_human_review_approve

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    wt_path = worktrees_dir / "tick-006"
    wt_path.mkdir()
    _init_git_repo(wt_path)
    (wt_path / "shared.py").write_text("line1\n")
    _commit_all(wt_path, "base")
    subprocess.run(["git", "checkout", "-b", "tick-006"], cwd=wt_path, check=True, capture_output=True)
    (wt_path / "requirements.txt").write_text("requests==2.0\n")
    _commit_all(wt_path, "ticket work")

    path = tickets_dir / "TICK-006.md"
    path.write_text(
        f"---\nid: TICK-006\ntitle: T\nstatus: needs_review\nworktree: {wt_path}\n"
        "branch: tick-006\nreview_verdict: changes_requested\nreview_summary: blocked by orchestrate gate\n"
        "review_retry_attempt: 3\nreview_retry_after: '2026-08-12T00:00:00Z'\n"
        "review_pending: true\n"
        "review_pending_reason: 'orphaned prior session: code_complete with no review verdict'\n"
        "---\n"
        "Background.\n\n## Needs Review Reason\n\n"
        "committed files match hard-blocked categories: requirements.txt "
        "[dependency manifest: requirements.txt]\n"
    )

    # Rationale is required -- rejected before ever touching the ticket.
    with pytest.raises(SystemExit):
        cmd_human_review_approve("TICK-006", cfg, tmp_path, rationale="")
    with pytest.raises(SystemExit):
        cmd_human_review_approve("TICK-006", cfg, tmp_path, rationale="   ")

    # Blocked from an automatic re-dispatch: reopen refuses this protected-path
    # ticket without an explicit human approval on record.
    with pytest.raises(SystemExit):
        cmd_reopen("TICK-006", cfg, tmp_path)
    ticket = parse_ticket(path)
    assert ticket["status"] == "needs_review"

    cmd_human_review_approve(
        "TICK-006", cfg, tmp_path, rationale="Manually verified the pin bump; safe to proceed."
    )

    ticket = parse_ticket(path)
    assert ticket["status"] == "code_complete"
    # Worktree/branch/commits preserved exactly -- no rebase, no re-dispatch.
    assert ticket.get("worktree") == str(wt_path)
    assert ticket.get("branch") == "tick-006"
    commit_count = subprocess.run(
        ["git", "rev-list", "--count", "main..tick-006"],
        cwd=wt_path, capture_output=True, text=True, check=True,
    )
    assert int(commit_count.stdout.strip()) == 1
    # Approval rationale recorded in structured frontmatter and ticket history.
    assert (
        ticket.get("protected_path_approved_rationale") == "Manually verified the pin bump; safe to proceed."
        or ticket.get("human_review_rationale") == "Manually verified the pin bump; safe to proceed."
    )
    assert ticket.get("protected_path_approved_at") or ticket.get("human_review_approved_at")
    assert ticket.get("protected_path_approved_actor") == "human" or ticket.get("human_review_actor") == "human"
    assert "## Status History" in ticket.get("_body", "")
    assert "human review approved" in ticket.get("_body", "")
    events = ticket.get("lifecycle_events") or []
    assert any(e.get("event") == "human_review_approved" for e in events)
    # Merge/review remain separate decisions -- approval never sets a verdict.
    assert not ticket.get("review_verdict")
    assert not ticket.get("review_summary")
    # Regression (TICK-517 round 2): the exhausted per-incident review retry
    # budget must not survive approval -- otherwise the very next `lanegate
    # run` this command recommends can hibernate straight back to
    # needs_review "(retry budget exhausted)" after zero fresh retries.
    assert "review_retry_attempt" not in ticket
    assert "review_retry_after" not in ticket
    # Regression (TICK-675 bug 1): the review-pending hibernation marker must
    # not survive approval -- otherwise the next `lanegate run` re-hibernates
    # the ticket for the same "orphaned prior session" reason it was just
    # approved out of.
    assert "review_pending" not in ticket
    assert "review_pending_reason" not in ticket

    # Now that it's an ordinary code_complete ticket, `lanegate reopen` no
    # longer applies (it has real commits and isn't needs_review), so the
    # normal review/merge path is what's expected next -- not another reopen.
    with pytest.raises(SystemExit):
        cmd_reopen("TICK-006", cfg, tmp_path)

    # Regression: a later, unrelated protected-path violation must not be
    # silently covered by the earlier approval. Re-entering needs_review
    # has to clear the stale protected_path_approved_at so cmd_reopen/cmd_start
    # block re-orchestration again until a fresh `human-review` approval.
    _mark_needs_review(
        ticket,
        cfg,
        tmp_path,
        reason=(
            "committed files match hard-blocked categories: requirements.txt "
            "[dependency manifest: requirements.txt]"
        ),
    )
    ticket = parse_ticket(path)
    assert ticket["status"] == "needs_review"
    assert not ticket.get("protected_path_approved_at")
    assert not ticket.get("protected_path_approved_rationale")
    assert not ticket.get("human_review_approved_at")
    assert not ticket.get("human_review_rationale")
    with pytest.raises(SystemExit):
        cmd_reopen("TICK-006", cfg, tmp_path)


def test_human_review_approve_sets_close_criteria_drift_fields_and_survives_unrelated_bounce(tmp_path):
    """human-review-approve must record close_criteria_drift_approved_* so the
    acceptance-contract audit can be cleared, and those fields must survive a
    later needs_review bounce for an unrelated reason (e.g. a mypy failure) --
    unlike protected_path_approved_at, which is diff-specific and must clear."""
    if shutil.which("git") is None:
        pytest.skip("git is required for this test")

    from lanegate.lifecycle import cmd_human_review_approve

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    wt_path = worktrees_dir / "tick-624"
    wt_path.mkdir()
    _init_git_repo(wt_path)
    (wt_path / "shared.py").write_text("line1\n")
    _commit_all(wt_path, "base")
    subprocess.run(["git", "checkout", "-b", "tick-624"], cwd=wt_path, check=True, capture_output=True)
    (wt_path / "feature.py").write_text("line1\n")
    _commit_all(wt_path, "ticket work")

    path = tickets_dir / "TICK-624.md"
    path.write_text(
        f"---\nid: TICK-624\ntitle: T\nstatus: needs_review\nworktree: {wt_path}\n"
        "branch: tick-624\nclose_criteria: feature() does X\n"
        "---\n"
        "Background.\n\n## Needs Review Reason\n\nclose_criteria changed since it was analyzed\n"
    )

    cmd_human_review_approve(
        "TICK-624", cfg, tmp_path, rationale="Scope narrowed intentionally; approving drift."
    )

    ticket = parse_ticket(path)
    assert ticket["status"] == "code_complete"
    assert ticket.get("close_criteria_drift_approved_at")
    assert ticket.get("close_criteria_drift_approved_rationale") == "Scope narrowed intentionally; approving drift."
    assert ticket.get("close_criteria_drift_approved_actor") == "human"
    assert ticket.get("close_criteria_drift_approved_snapshot") == "feature() does X"

    # An unrelated needs_review bounce (e.g. a mypy failure) must not wipe the
    # close-criteria-drift approval -- only the diff-specific protected-path
    # approval is invalidated by re-entering needs_review.
    _mark_needs_review(ticket, cfg, tmp_path, reason="review-pending: mypy lanegate: nonzero exit")
    ticket = parse_ticket(path)
    assert ticket["status"] == "needs_review"
    assert not ticket.get("protected_path_approved_at")
    assert ticket.get("close_criteria_drift_approved_at")
    assert ticket.get("close_criteria_drift_approved_rationale") == "Scope narrowed intentionally; approving drift."
    assert ticket.get("close_criteria_drift_approved_actor") == "human"
    assert ticket.get("close_criteria_drift_approved_snapshot") == "feature() does X"


def test_human_review_approve_records_red_lane_approved_sha(tmp_path):
    """cmd_human_review_approve must record red_lane_approved_at_sha at the
    worktree's current HEAD so loop.py's red-lane diff re-scans can recognize
    an unchanged diff and skip re-escalating it."""
    if shutil.which("git") is None:
        pytest.skip("git is required for this test")

    from lanegate.lifecycle import cmd_human_review_approve

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    wt_path = worktrees_dir / "tick-699"
    wt_path.mkdir()
    _init_git_repo(wt_path)
    (wt_path / "shared.py").write_text("line1\n")
    _commit_all(wt_path, "base")
    subprocess.run(["git", "checkout", "-b", "tick-699"], cwd=wt_path, check=True, capture_output=True)
    (wt_path / "feature.py").write_text("line1\n")
    _commit_all(wt_path, "ticket work")

    path = tickets_dir / "TICK-699.md"
    path.write_text(
        f"---\nid: TICK-699\ntitle: T\nstatus: needs_review\nworktree: {wt_path}\n"
        "branch: tick-699\n"
        "---\n"
        "Background.\n\n## Needs Review Reason\n\nred-lane escalation\n"
    )

    cmd_human_review_approve(
        "TICK-699", cfg, tmp_path, rationale="Reviewed and approved the credential-like string."
    )

    expected_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=wt_path, capture_output=True, text=True
    ).stdout.strip()

    ticket = parse_ticket(path)
    assert ticket["status"] == "code_complete"
    assert ticket.get("red_lane_approved_at_sha") == expected_sha




def test_human_review_approve_code_complete_changes_requested(tmp_path):
    """cmd_human_review_approve on a code_complete ticket with review_verdict=changes_requested
    archives findings, clears frontmatter review fields, and retains code_complete status."""
    if shutil.which("git") is None:
        pytest.skip("git is required for this test")

    from lanegate.lifecycle import cmd_human_review_approve

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    wt_path = worktrees_dir / "tick-100"
    wt_path.mkdir()
    _init_git_repo(wt_path)
    (wt_path / "shared.py").write_text("line1\n")
    _commit_all(wt_path, "base")
    subprocess.run(["git", "checkout", "-b", "tick-100"], cwd=wt_path, check=True, capture_output=True)
    (wt_path / "app.py").write_text("print('hello')\n")
    _commit_all(wt_path, "ticket work")

    path = tickets_dir / "TICK-100.md"
    path.write_text(
        f"---\nid: TICK-100\ntitle: T\nstatus: code_complete\nworktree: {wt_path}\n"
        "branch: tick-100\nreview_verdict: changes_requested\nreview_summary: minor style finding\n"
        "review_findings:\n- style issue in app.py\nreviewed_at: '2026-08-17T10:00:00Z'\n"
        "---\n"
        "Background prose.\n"
    )

    cmd_human_review_approve(
        "TICK-100", cfg, tmp_path, rationale="False positive; style finding is intentional."
    )

    ticket = parse_ticket(path)
    assert ticket["status"] == "code_complete"
    assert ticket.get("review_findings_dismissal_rationale") == "False positive; style finding is intentional."
    assert ticket.get("review_findings_dismissed_at")
    assert ticket.get("review_findings_dismissal_actor") == "human"
    assert "human_review_approved_at" not in ticket

    # Frontmatter review fields cleared
    assert "review_verdict" not in ticket
    assert "review_summary" not in ticket
    assert "review_findings" not in ticket
    assert "reviewed_at" not in ticket

    # Body history has archived findings
    body = ticket.get("_body", "")
    assert "## Archived Review Findings" in body
    assert "minor style finding" in body
    assert "style issue in app.py" in body
    assert "False positive; style finding is intentional." in body

    # Lifecycle event recorded
    events = ticket.get("lifecycle_events") or []
    assert any(
        e.get("event") == "human_review_approved"
        and e.get("from_status") == "code_complete"
        and e.get("to_status") == "code_complete"
        for e in events
    )


def test_human_review_approve_code_complete_multiple_dismissals_same_day(tmp_path):
    """Calling cmd_human_review_approve twice on the same day on code_complete tickets
    updates the archived findings section cleanly without creating orphaned ### Findings blocks."""
    if shutil.which("git") is None:
        pytest.skip("git is required for this test")

    from lanegate.lifecycle import cmd_human_review_approve

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    wt_path = worktrees_dir / "tick-100"
    wt_path.mkdir()
    _init_git_repo(wt_path)
    (wt_path / "shared.py").write_text("line1\n")
    _commit_all(wt_path, "base")
    subprocess.run(["git", "checkout", "-b", "tick-100"], cwd=wt_path, check=True, capture_output=True)
    (wt_path / "app.py").write_text("print('hello')\n")
    _commit_all(wt_path, "ticket work")

    path = tickets_dir / "TICK-100.md"
    path.write_text(
        f"---\nid: TICK-100\ntitle: T\nstatus: code_complete\nworktree: {wt_path}\n"
        "branch: tick-100\nreview_verdict: changes_requested\nreview_summary: first finding\n"
        "review_findings:\n- first issue\nreviewed_at: '2026-08-17T10:00:00Z'\n"
        "---\n"
        "Background prose.\n"
    )

    cmd_human_review_approve(
        "TICK-100", cfg, tmp_path, rationale="First dismissal."
    )

    ticket = parse_ticket(path)
    body1 = ticket.get("_body", "")
    assert "first finding" in body1
    assert "first issue" in body1

    # Simulate a second changes_requested review on the same ticket
    ticket["review_verdict"] = "changes_requested"
    ticket["review_summary"] = "second finding"
    ticket["review_findings"] = ["second issue"]
    ticket["reviewed_at"] = "2026-08-17T11:00:00Z"
    write_ticket(ticket)

    cmd_human_review_approve(
        "TICK-100", cfg, tmp_path, rationale="Second dismissal."
    )

    ticket2 = parse_ticket(path)
    body2 = ticket2.get("_body", "")
    assert "second finding" in body2
    assert "second issue" in body2
    assert "Second dismissal." in body2
    assert body2.count("### Findings") == 1


def test_human_review_approve_code_complete_refuses_without_changes_requested(tmp_path):
    """cmd_human_review_approve on a code_complete ticket without review_verdict=changes_requested
    raises an error."""
    if shutil.which("git") is None:
        pytest.skip("git is required for this test")

    from lanegate.lifecycle import cmd_human_review_approve

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    wt_path = worktrees_dir / "tick-101"
    wt_path.mkdir()
    _init_git_repo(wt_path)
    (wt_path / "shared.py").write_text("line1\n")
    _commit_all(wt_path, "base")
    subprocess.run(["git", "checkout", "-b", "tick-101"], cwd=wt_path, check=True, capture_output=True)
    (wt_path / "app.py").write_text("print('hello')\n")
    _commit_all(wt_path, "ticket work")

    path = tickets_dir / "TICK-101.md"
    path.write_text(
        f"---\nid: TICK-101\ntitle: T\nstatus: code_complete\nworktree: {wt_path}\n"
        "branch: tick-101\n"
        "---\n"
        "Background prose.\n"
    )

    with pytest.raises(SystemExit):
        cmd_human_review_approve("TICK-101", cfg, tmp_path, rationale="Dismissing non-existent verdict")


def test_cmd_review_blocks_approved_verdict_with_unresolved_criteria(tmp_path, capsys):
    """lanegate review --verdict approved refuses when a required criterion has
    no automated evidence and no --findings was given to cover it."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-301"
    wt.mkdir()
    body = "## Acceptance Criteria\n- [ ] Add a widget function\n"
    _write_ticket_with_body(tickets_dir, "TICK-301", "code_complete", body, worktree=str(wt))
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    with patch("lanegate.acceptance_contract._worktree_diff_text", return_value="unrelated diff content"):
        with pytest.raises(SystemExit) as exc_info:
            cmd_review("TICK-301", cfg, tmp_path, verdict="approved")

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "Add a widget function" in err

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-301.md")
    # Blocked: must not have advanced to in_review
    assert ticket["status"] == "code_complete"
    # But the (failed) verification attempt is persisted for inspection
    assert ticket["verification"][0]["status"] == "unverified"


def test_cmd_review_approved_with_findings_flips_unresolved_to_manual(tmp_path):
    """--findings on an approved verdict covers criteria the automated
    verifier couldn't confirm, preserving human judgment for non-automatable
    criteria instead of blocking forever."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-302"
    wt.mkdir()
    body = "## Acceptance Criteria\n- [ ] Add a widget function\n"
    _write_ticket_with_body(tickets_dir, "TICK-302", "code_complete", body, worktree=str(wt))
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    with patch("lanegate.acceptance_contract._worktree_diff_text", return_value="unrelated diff content"):
        cmd_review(
            "TICK-302",
            cfg,
            tmp_path,
            verdict="approved",
            findings="Manually verified the widget function works.",
        )

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-302.md")
    assert ticket["status"] == "in_review"
    record = ticket["verification"][0]
    assert record["status"] == "manual"
    assert "human judgment via review findings" in record["evidence"]


def test_cmd_review_approves_cleanly_when_all_criteria_verified(tmp_path):
    """No blocking, no findings needed, when the diff already covers every
    criterion -- the common case must stay frictionless."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-303"
    wt.mkdir()
    body = "## Acceptance Criteria\n- [ ] Add a widget function\n- [ ] Full suite green.\n"
    _write_ticket_with_body(tickets_dir, "TICK-303", "code_complete", body, worktree=str(wt))
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    with patch(
        "lanegate.acceptance_contract._worktree_diff_text",
        return_value="def add_widget_function(): pass",
    ):
        cmd_review("TICK-303", cfg, tmp_path, verdict="approved")

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-303.md")
    assert ticket["status"] == "in_review"
    statuses = {r["criterion"]: r["status"] for r in ticket["verification"]}
    assert statuses["Add a widget function"] == "verified"
    assert statuses["Full suite green."] == "verified"


def test_cmd_review_blocks_mid_rebase_worktree(tmp_path):
    """Verify cmd_review refuses to run when worktree is mid-rebase."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-534"
    wt.mkdir()

    _write_ticket(tickets_dir, "TICK-534", "code_complete", worktree=str(wt))
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    with patch("lanegate.orchestrate.loop.is_mid_rebase", return_value=True), \
         pytest.raises(SystemExit):
        cmd_review("TICK-534", cfg, tmp_path)


def test_direct_lifecycle_action_prints_and_logs_tracking_ref(tmp_path, capsys):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-484", "code_complete")
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    cmd_review("TICK-484", cfg, tmp_path, verdict="approved")

    output = capsys.readouterr().out
    assert "Action action-" in output
    assert "review success" in output
    action_log = next((tmp_path / ".lanegate" / "logs").glob("action-*.events.jsonl"))
    assert '"status": "success"' in action_log.read_text()


def test_review_nudges_global_md_consolidation(tmp_path, capsys):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-683", "code_complete")
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    notes_dir = tmp_path / ".lanegate" / "notes"
    notes_dir.mkdir(parents=True)
    (notes_dir / "src_foo.py.md").write_text("file-specific fact")

    # global.md missing -> nudge emitted during review
    cmd_review("TICK-683", cfg, tmp_path, verdict="approved")
    captured = capsys.readouterr()
    err = captured.err
    out = captured.out
    assert "Consider consolidating project-wide facts into .lanegate/notes/global.md." in (err + out)

    # global.md present -> nudge NOT emitted
    (notes_dir / "global.md").write_text("project-wide fact")
    _write_ticket(tickets_dir, "TICK-684", "code_complete")
    cmd_review("TICK-684", cfg, tmp_path, verdict="approved")
    captured2 = capsys.readouterr()
    assert "Consider consolidating project-wide facts into .lanegate/notes/global.md." not in (captured2.err + captured2.out)

