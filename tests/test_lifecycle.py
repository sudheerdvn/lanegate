"""Tests for lifecycle.py — status transitions, lock-until-merge, merge worktree cleanup."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lanegate.lifecycle import (
    _commit_status,
    _push_branch_and_open_pr,
    check_touches_compliance,
    cmd_complete,
    cmd_done,
    cmd_hibernate,
    cmd_merge,
    cmd_needs_review,
    cmd_open,
    cmd_reopen,
    cmd_review,
    cmd_stop,
    cmd_supersede,
    cmd_validate,
    resolve_reviewer,
    spawn_detached,
)


def _default_cfg(tickets_dir, worktrees_dir):
    return {
        "ticket_prefix": "TICK",
        "tickets_dir": str(tickets_dir.name),
        "worktrees_dir": str(worktrees_dir.name),
        "lock_statuses": ["in_progress", "code_complete", "in_review"],
        "commit_status_changes": False,
        "environments": [],
    }


def _write_ticket(
    tickets_dir: Path,
    ticket_id: str,
    status: str,
    worktree=None,
    branch=None,
    review_verdict=None,
    companion_repos=None,
    touches=None,
):
    content = f"---\nid: {ticket_id}\ntitle: Test {ticket_id}\nstatus: {status}\n"
    if worktree:
        content += f"worktree: {worktree}\n"
    if branch:
        content += f"branch: {branch}\n"
    if review_verdict:
        content += f"review_verdict: {review_verdict}\n"
    if touches:
        content += "touches:\n"
        for t in touches:
            content += f"  - {t}\n"
    if companion_repos:
        content += "companion_repos:\n"
        for c in companion_repos:
            content += f"  - {c}\n"
    content += "---\nBody.\n"
    path = tickets_dir / f"{ticket_id}.md"
    path.write_text(content)
    return path


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)


def _commit_all(path: Path, message: str = "init") -> None:
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=path, check=True, capture_output=True)


def _tracked_path_is_clean(repo: Path, path: Path) -> bool:
    r = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", str(path)],
        cwd=repo,
        capture_output=True,
    )
    return r.returncode == 0


def test_complete_advances_from_in_progress(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()

    wt = worktrees_dir / "tick-001"
    wt.mkdir()
    touches_yaml = "  - some_file.py"
    content = (
        f"---\n"
        f"id: TICK-001\n"
        f"title: Test TICK-001\n"
        f"status: in_progress\n"
        f"worktree: {wt}\n"
        f"touches:\n"
        f"{touches_yaml}\n"
        f"---\nBody.\n"
    )
    (tickets_dir / "TICK-001.md").write_text(content)

    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    mock_run = _make_git_diff_mock(committed_files=["some_file.py"])
    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        cmd_complete("TICK-001", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-001.md")
    assert ticket["status"] == "code_complete"


def test_complete_refuses_zero_commits(tmp_path, capsys):
    """cmd_complete refuses to advance a ticket whose worktree has no real
    commits ahead of main — the TICK-030/089/159 wedge scenario, where an
    executor stalls but the ticket still gets marked code_complete."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    wt_path = worktrees_dir / "tick-002"
    wt_path.mkdir()
    _write_ticket(tickets_dir, "TICK-002", "in_progress", worktree=str(wt_path), branch="tick-002")

    mock_run = _make_git_diff_mock(committed_files=[], uncommitted_files=[])

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        with pytest.raises(SystemExit) as exc_info:
            cmd_complete("TICK-002", cfg, tmp_path)

    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "no commits ahead of main" in err

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-002.md")
    assert ticket["status"] == "in_progress"


def test_complete_refuses_missing_worktree(tmp_path, capsys):
    """F12: cmd_complete refuses to advance when the worktree directory is missing."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    wt_path = worktrees_dir / "tick-003"
    wt_path.mkdir()
    _write_ticket(tickets_dir, "TICK-003", "in_progress", worktree=str(wt_path))

    wt_path.rmdir()

    with pytest.raises(SystemExit) as exc_info:
        cmd_complete("TICK-003", cfg, tmp_path)

    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "worktree" in err and "does not exist" in err

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-003.md")
    assert ticket["status"] == "in_progress"


def test_complete_refuses_unset_worktree(tmp_path, capsys):
    """cmd_complete refuses to advance when the worktree is not set."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    _write_ticket(tickets_dir, "TICK-004", "in_progress")

    with pytest.raises(SystemExit) as exc_info:
        cmd_complete("TICK-004", cfg, tmp_path)

    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "no worktree set" in err

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-004.md")
    assert ticket["status"] == "in_progress"


def test_complete_rejects_wrong_status(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-001", "open")
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    with pytest.raises(SystemExit):
        cmd_complete("TICK-001", cfg, tmp_path)


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


def test_merge_cleans_up_worktree(tmp_path):
    """After merge, the worktree must actually be removed (bug fix test)."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-001"
    wt.mkdir()

    _write_ticket(
        tickets_dir,
        "TICK-001",
        "in_review",
        worktree=str(wt),
        branch="tick-001",
        review_verdict="approved",
    )

    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    removed = []

    def mock_run(args, **kwargs):
        if "merge" in args and "--no-ff" in args:
            return MagicMock(returncode=0, stdout="", stderr="")
        if "worktree" in args and "remove" in args:
            removed.append(args)
            # Simulate removal
            if wt.exists():
                wt.rmdir()
            return MagicMock(returncode=0)
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        with patch("lanegate.worktree.subprocess.run") as mock_wt_run:
            mock_wt_run.side_effect = mock_run
            cmd_merge("TICK-001", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-001.md")
    assert ticket["status"] == "merged"
    assert ticket.get("worktree") is None


def test_merge_does_not_advance_status_when_companion_checkout_fails(tmp_path):
    """TICK-135: a companion repo merge failure must not let the ticket land on
    'merged' — the main repo merge succeeded but the companion side silently
    didn't, which was previously invisible. (F40/TICK-229 changed *how*
    companion merges can fail — they now happen in an isolated scratch
    worktree rather than via `git checkout main` in the user's live companion
    tree — so the injection point here is the scratch worktree creation, not
    the checkout it replaced. The principle under test — a companion failure
    must block the ticket from reaching 'merged' — is unchanged.)"""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    companion_path = (tmp_path / "companion-repo").resolve()

    _write_ticket(
        tickets_dir,
        "TICK-001",
        "in_review",
        branch="tick-001",
        review_verdict="approved",
        companion_repos=[str(companion_path)],
    )

    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    def mock_run(args, **kwargs):
        if kwargs.get("cwd") == companion_path:
            if args[:3] == ["git", "rev-parse", "--verify"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if args[:3] == ["git", "worktree", "add"] and "--detach" in args:
                return MagicMock(returncode=1, stdout="", stderr="error: unmerged paths")
        return MagicMock(returncode=0, stdout="", stderr="")

    from lanegate.lifecycle import MergeFailedError

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        with pytest.raises(MergeFailedError):
            cmd_merge("TICK-001", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-001.md")
    assert ticket["status"] != "merged"


def test_merge_advances_status_when_companion_merges_cleanly(tmp_path):
    """Normal path: companion repo has no conflicts — behavior unchanged."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    companion_path = (tmp_path / "companion-repo").resolve()

    _write_ticket(
        tickets_dir,
        "TICK-001",
        "in_review",
        branch="tick-001",
        review_verdict="approved",
        companion_repos=[str(companion_path)],
    )

    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    def mock_run(args, **kwargs):
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        cmd_merge("TICK-001", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-001.md")
    assert ticket["status"] == "merged"


def test_done_from_merged(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-001", "merged")
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)
    cmd_done("TICK-001", cfg, tmp_path)
    from lanegate.ticket import parse_ticket

    assert parse_ticket(tickets_dir / "TICK-001.md")["status"] == "done"


def test_done_from_validated(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-001", "validated")
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)
    cmd_done("TICK-001", cfg, tmp_path)
    from lanegate.ticket import parse_ticket

    assert parse_ticket(tickets_dir / "TICK-001.md")["status"] == "done"


def test_validated_is_optional_passthrough(tmp_path):
    """validated stage can be skipped — merged → done is valid."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-001", "merged")
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)
    # Skip validated, go directly to done
    cmd_done("TICK-001", cfg, tmp_path)
    from lanegate.ticket import parse_ticket

    assert parse_ticket(tickets_dir / "TICK-001.md")["status"] == "done"


def test_validate_runs_post_merge_guard_and_advances(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-116", "merged")
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)
    cfg["safeguards"] = {"post_merge": ["pytest"]}
    calls = []

    def mock_guard(args, **kwargs):
        calls.append((list(args), kwargs.get("cwd")))
        return MagicMock(returncode=0)

    with patch("lanegate.safeguards.subprocess.run", side_effect=mock_guard):
        cmd_validate("TICK-116", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-116.md")
    assert ticket["status"] == "validated"
    guard_calls = [call for call in calls if call[0][:3] == [sys.executable, "-m", "pytest"]]
    assert guard_calls == [([sys.executable, "-m", "pytest", "-q", "--tb=short"], tmp_path)]


def test_validate_blocks_on_failing_post_merge_guard(tmp_path, capsys):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-117", "merged")
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)
    cfg["safeguards"] = {"post_merge": ["pytest"]}

    def mock_guard(args, **kwargs):
        return MagicMock(returncode=1)

    with patch("lanegate.safeguards.subprocess.run", side_effect=mock_guard):
        with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_guard):
            cmd_validate("TICK-117", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    # Post-merge safeguard failure routes to needs_review
    assert parse_ticket(tickets_dir / "TICK-117.md")["status"] == "needs_review"
    err = capsys.readouterr().err
    assert "post_merge" in err
    assert "safeguard" in err.lower()


def test_validate_without_post_merge_guard_remains_optional(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-118", "merged")
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    cmd_done("TICK-118", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    assert parse_ticket(tickets_dir / "TICK-118.md")["status"] == "done"


def test_done_requires_validated_when_post_merge_configured(tmp_path, capsys):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-119", "merged")
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)
    cfg["safeguards"] = {"post_merge": ["pytest"]}

    with pytest.raises(SystemExit) as exc_info:
        cmd_done("TICK-119", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    assert exc_info.value.code != 0
    assert parse_ticket(tickets_dir / "TICK-119.md")["status"] == "merged"
    err = capsys.readouterr().err
    assert "lanegate validate TICK-119" in err


def test_merge_prints_validate_command_when_post_merge_configured(tmp_path, capsys):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(
        tickets_dir, "TICK-120", "in_review", branch="tick-120", review_verdict="approved"
    )
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)
    cfg["safeguards"] = {"post_merge": ["pytest"]}

    def mock_run(args, **kwargs):
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        cmd_merge("TICK-120", cfg, tmp_path)

    out = capsys.readouterr().out
    assert "lanegate validate TICK-120" in out


def test_merge_conflict_aborts_and_leaves_clean_state(tmp_path):
    """On merge conflict: git merge --abort is called, no MERGE_HEAD remains, ticket stays in_review."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()

    _write_ticket(
        tickets_dir, "TICK-002", "in_review", branch="tick-002", review_verdict="approved"
    )

    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    # Simulate a real git repo: create MERGE_HEAD to indicate a mid-merge state
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    merge_head = git_dir / "MERGE_HEAD"
    merge_head.write_text("deadbeef\n")

    abort_called = []

    def mock_run(args, **kwargs):
        if "merge" in args and "--no-ff" in args:
            # Conflict: non-zero exit, MERGE_HEAD already exists (set above)
            return MagicMock(
                returncode=1, stdout="", stderr="CONFLICT (content): Merge conflict in foo.py"
            )
        if "merge" in args and "--abort" in args:
            abort_called.append(True)
            # Simulate abort cleaning up MERGE_HEAD
            if merge_head.exists():
                merge_head.unlink()
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    from lanegate.lifecycle import MergeFailedError

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        with pytest.raises(MergeFailedError):
            cmd_merge("TICK-002", cfg, tmp_path)

    # git merge --abort must have been called
    assert abort_called, "git merge --abort was not called after conflict"

    # Repo must not be mid-merge (MERGE_HEAD gone)
    assert not merge_head.exists(), "MERGE_HEAD still present after abort — repo is mid-merge"

    # Ticket status must NOT have advanced — still in_review
    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-002.md")
    assert ticket["status"] == "in_review", (
        f"Ticket status advanced to '{ticket['status']}' on merge failure — must stay 'in_review'"
    )


def test_merge_conflict_prints_stdout_detail(tmp_path, capsys):
    """TICK-123 symptom A: git's real conflict detail ("CONFLICT (content):
    ...") is written to stdout, not stderr. cmd_merge must surface it instead
    of printing an empty error."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()

    _write_ticket(
        tickets_dir, "TICK-003", "in_review", branch="tick-003", review_verdict="approved"
    )
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    def mock_run(args, **kwargs):
        if "merge" in args and "--no-ff" in args:
            return MagicMock(
                returncode=1,
                stdout="CONFLICT (content): Merge conflict in TICK-003.md\n",
                stderr="",
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    from lanegate.lifecycle import MergeFailedError

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        with pytest.raises(MergeFailedError):
            cmd_merge("TICK-003", cfg, tmp_path)

    err = capsys.readouterr().err
    assert "CONFLICT" in err, f"real conflict detail missing from error output: {err!r}"


def test_merge_does_not_clobber_ticket_body_from_incoming_branch(tmp_path):
    """TICK-123 symptom B: `git merge` can change the ticket's own file (new
    sections, DoD checkbox state from the branch) before cmd_merge writes it
    again to set status=merged. The stale pre-merge in-memory copy must not
    overwrite what the merge just brought in."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()

    path = _write_ticket(
        tickets_dir, "TICK-004", "in_review", branch="tick-004", review_verdict="approved"
    )

    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    merged_body = (
        "---\nid: TICK-004\ntitle: Test TICK-004\nstatus: in_review\n"
        "branch: tick-004\nreview_verdict: approved\n"
        "---\n## Root Cause\n\nFound it.\n\n- [x] done item\n"
    )

    def mock_run(args, **kwargs):
        if "merge" in args and "--no-ff" in args:
            # Simulate git merge bringing in the branch's ticket-file content.
            path.write_text(merged_body)
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        cmd_merge("TICK-004", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    final = parse_ticket(path)
    assert final["status"] == "merged", "status must still be advanced to merged"
    assert "Root Cause" in final["_body"], "merge-introduced body content was clobbered"
    assert "[x] done item" in final["_body"], "merge-introduced DoD state was clobbered"


# ── cmd_review verdict tests ──────────────────────────────────────────────────


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


def test_review_findings_stored_when_updating_in_review_verdict(tmp_path):
    """When updating verdict on already-in_review ticket, findings are stored in frontmatter."""
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


# ---------------------------------------------------------------------------
# TICK-039: merge precondition — require in_review + approved verdict
# ---------------------------------------------------------------------------


def _merge_cfg(tmp_path, status, review_verdict=None, branch=None):
    """Helper: set up a minimal cfg and ticket with the given status/verdict."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir(exist_ok=True)
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir(exist_ok=True)
    _write_ticket(tickets_dir, "TICK-001", status, branch=branch, review_verdict=review_verdict)
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)
    return cfg


def test_merge_from_open_is_blocked(tmp_path):
    """cmd_merge must exit non-zero when ticket is 'open'."""
    cfg = _merge_cfg(tmp_path, "open")
    with pytest.raises(SystemExit) as exc_info:
        cmd_merge("TICK-001", cfg, tmp_path)
    assert exc_info.value.code != 0


def test_merge_from_in_progress_is_blocked(tmp_path):
    """cmd_merge must exit non-zero when ticket is 'in_progress'."""
    cfg = _merge_cfg(tmp_path, "in_progress")
    with pytest.raises(SystemExit) as exc_info:
        cmd_merge("TICK-001", cfg, tmp_path)
    assert exc_info.value.code != 0


def test_merge_from_code_complete_is_blocked(tmp_path, capsys):
    """cmd_merge must exit non-zero when ticket is 'code_complete' and print a helpful message."""
    cfg = _merge_cfg(tmp_path, "code_complete")
    with pytest.raises(SystemExit) as exc_info:
        cmd_merge("TICK-001", cfg, tmp_path)
    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "code_complete" in err
    assert "review" in err.lower()


def test_merge_from_in_review_without_verdict_is_blocked(tmp_path, capsys):
    """cmd_merge must exit non-zero when ticket is 'in_review' but has no review_verdict."""
    cfg = _merge_cfg(tmp_path, "in_review")  # no review_verdict
    with pytest.raises(SystemExit) as exc_info:
        cmd_merge("TICK-001", cfg, tmp_path)
    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "review_verdict" in err or "approved" in err


def test_merge_from_in_review_with_changes_requested_is_blocked(tmp_path, capsys):
    """cmd_merge must exit non-zero when review_verdict is 'changes_requested'."""
    cfg = _merge_cfg(tmp_path, "in_review", review_verdict="changes_requested")
    with pytest.raises(SystemExit) as exc_info:
        cmd_merge("TICK-001", cfg, tmp_path)
    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "approved" in err


def test_merge_from_in_review_with_approved_verdict_succeeds(tmp_path):
    """cmd_merge must succeed when ticket is 'in_review' with review_verdict='approved'."""
    cfg = _merge_cfg(tmp_path, "in_review", review_verdict="approved", branch="tick-001")

    def mock_run(args, **kwargs):
        if "merge" in args and "--no-ff" in args:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        cmd_merge("TICK-001", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    tickets_dir = tmp_path / "tickets"
    ticket = parse_ticket(tickets_dir / "TICK-001.md")
    assert ticket["status"] == "merged"


def test_merge_commits_pending_ticket_diff_before_merging(tmp_path):
    """TICK-122: an uncommitted local diff to the ticket's own file (left by
    an earlier lifecycle transition or manual edit) must be committed before
    `git merge` runs, so git's own safety check doesn't refuse the merge
    outright."""
    cfg = _merge_cfg(tmp_path, "in_review", review_verdict="approved", branch="tick-001")
    tickets_dir = tmp_path / "tickets"

    calls = []

    def mock_run(args, **kwargs):
        calls.append(list(args))
        if "diff" in args and "--quiet" in args:
            return MagicMock(returncode=1, stdout="", stderr="")  # dirty
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        cmd_merge("TICK-001", cfg, tmp_path)

    diff_idx = next(i for i, c in enumerate(calls) if "diff" in c and "--quiet" in c)
    commit_idx = next(i for i, c in enumerate(calls) if "commit" in c and "--only" in c)
    merge_idx = next(i for i, c in enumerate(calls) if "merge" in c and "--no-ff" in c)
    assert diff_idx < commit_idx < merge_idx, "pending diff must be committed before the merge"
    assert str(tickets_dir / "TICK-001.md") in calls[commit_idx]


def test_merge_skips_commit_when_ticket_file_clean(tmp_path):
    """No extra commit should happen when the ticket's own file has no
    uncommitted diff on main."""
    cfg = _merge_cfg(tmp_path, "in_review", review_verdict="approved", branch="tick-001")

    calls = []

    def mock_run(args, **kwargs):
        calls.append(list(args))
        if "diff" in args and "--quiet" in args:
            return MagicMock(returncode=0, stdout="", stderr="")  # clean
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        cmd_merge("TICK-001", cfg, tmp_path)

    assert not any("commit" in c and "--only" in c for c in calls)


# ---------------------------------------------------------------------------
# merge auto-log
# ---------------------------------------------------------------------------


def test_merge_auto_logs_to_sqlite(tmp_path):
    """cmd_merge writes an analytics entry to the SQLite DB on success.

    The executor is resolved from ticket → cfg → default ('claude').
    """
    from lanegate.context_log import _load_entries_from_db

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(
        tickets_dir, "TICK-001", "in_review", branch="tick-001", review_verdict="approved"
    )

    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    db = tmp_path / "test_analytics.db"

    def mock_run(args, **kwargs):
        if "merge" in args and "--no-ff" in args:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run),
        patch("lanegate.context_log._get_default_db_path", return_value=db),
    ):
        cmd_merge("TICK-001", cfg, tmp_path)

    entries = _load_entries_from_db(db)
    assert len(entries) == 1
    assert entries[0]["ticket_id"] == "TICK-001"
    # executor resolves from ticket (none) → cfg (none) → default "claude"
    assert entries[0]["executor"] == "claude"
    assert entries[0]["subagent_tokens"] is None


def test_merge_logs_non_zero_touched_files_and_wall_time(tmp_path):
    """cmd_merge writes non-empty touched_files and non-zero wall_time_ms to the DB."""
    from lanegate.context_log import _load_entries_from_db

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(
        tickets_dir, "TICK-001", "in_review", branch="tick-001", review_verdict="approved"
    )

    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    db = tmp_path / "test_analytics.db"

    import time

    # A unix timestamp from one hour ago so wall_time_ms is non-zero
    fake_first_commit_ts = int(time.time()) - 3600

    def mock_run(args, **kwargs):
        if "merge" in args and "--no-ff" in args:
            return MagicMock(returncode=0, stdout="", stderr="")
        if "diff" in args and "--name-only" in args:
            return MagicMock(
                returncode=0, stdout="lanegate/lifecycle.py\ntests/test_lifecycle.py\n", stderr=""
            )
        if "log" in args and "--format=%ct" in args:
            return MagicMock(returncode=0, stdout=f"{fake_first_commit_ts}\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run),
        patch("lanegate.context_log._get_default_db_path", return_value=db),
    ):
        cmd_merge("TICK-001", cfg, tmp_path)

    entries = _load_entries_from_db(db)
    assert len(entries) == 1
    assert entries[0]["touched_files"], "touched_files should be non-empty"
    assert len(entries[0]["touched_files"]) == 2
    assert entries[0]["wall_time_ms"] > 0, "wall_time_ms should be non-zero"


# ---------------------------------------------------------------------------
# _push_branch_and_open_pr unit tests
# ---------------------------------------------------------------------------


def _make_ticket(tid="TICK-001", title="Test ticket", branch="tick-001"):
    """Minimal ticket dict for helper tests."""
    return {"id": tid, "title": title, "branch": branch}


def test_push_branch_skips_when_gh_not_installed(tmp_path):
    """Returns None immediately when gh is not on PATH."""
    ticket = _make_ticket()
    with patch("lanegate.lifecycle.shutil.which", return_value=None):
        result = _push_branch_and_open_pr(tmp_path, "tick-001", ticket)
    assert result is None


def test_push_branch_skips_when_no_remote(tmp_path):
    """Returns None when git remote get-url origin fails."""
    ticket = _make_ticket()

    def mock_run(args, **kwargs):
        if "remote" in args and "get-url" in args:
            return MagicMock(returncode=1, stdout="", stderr="No remote")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.lifecycle.shutil.which", return_value="/usr/bin/gh"),
        patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run),
    ):
        result = _push_branch_and_open_pr(tmp_path, "tick-001", ticket)
    assert result is None


def test_push_branch_skips_when_push_fails(tmp_path):
    """Returns None when git push fails, prints warning."""
    ticket = _make_ticket()

    def mock_run(args, **kwargs):
        if "remote" in args and "get-url" in args:
            return MagicMock(returncode=0, stdout="git@github.com:org/repo.git", stderr="")
        if "push" in args:
            return MagicMock(returncode=1, stdout="", stderr="push rejected")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.lifecycle.shutil.which", return_value="/usr/bin/gh"),
        patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run),
    ):
        result = _push_branch_and_open_pr(tmp_path, "tick-001", ticket)
    assert result is None


def test_push_branch_creates_pr_and_returns_number_url(tmp_path):
    """Creates PR when no existing PR; returns (pr_number, pr_url)."""
    ticket = _make_ticket(tid="TICK-042", title="My feature", branch="tick-042")

    calls = []

    def mock_run(args, **kwargs):
        calls.append(list(args))
        if "remote" in args and "get-url" in args:
            return MagicMock(returncode=0, stdout="git@github.com:org/repo.git", stderr="")
        if "push" in args:
            return MagicMock(returncode=0, stdout="", stderr="")
        if "pr" in args and "view" in args:
            # No existing PR
            return MagicMock(returncode=1, stdout="", stderr="no pull requests found")
        if "pr" in args and "create" in args:
            return MagicMock(
                returncode=0, stdout="https://github.com/org/repo/pull/42\n", stderr=""
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.lifecycle.shutil.which", return_value="/usr/bin/gh"),
        patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run),
    ):
        result = _push_branch_and_open_pr(tmp_path, "tick-042", ticket)

    assert result == (42, "https://github.com/org/repo/pull/42")

    # Verify gh pr create was called with the right flags
    create_call = next((c for c in calls if "create" in c), None)
    assert create_call is not None
    assert "--base" in create_call and "main" in create_call
    assert "--head" in create_call and "tick-042" in create_call
    assert "--title" in create_call and "My feature" in create_call


def test_push_branch_reuses_existing_pr(tmp_path):
    """When gh pr view succeeds, returns existing PR without creating a new one."""
    ticket = _make_ticket(tid="TICK-007", title="Existing PR ticket", branch="tick-007")

    existing = {"number": 7, "url": "https://github.com/org/repo/pull/7"}

    calls = []

    def mock_run(args, **kwargs):
        calls.append(list(args))
        if "remote" in args and "get-url" in args:
            return MagicMock(returncode=0, stdout="git@github.com:org/repo.git", stderr="")
        if "push" in args:
            return MagicMock(returncode=0, stdout="", stderr="")
        if "pr" in args and "view" in args:
            return MagicMock(returncode=0, stdout=json.dumps(existing), stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.lifecycle.shutil.which", return_value="/usr/bin/gh"),
        patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run),
    ):
        result = _push_branch_and_open_pr(tmp_path, "tick-007", ticket)

    assert result == (7, "https://github.com/org/repo/pull/7")
    # gh pr create must NOT have been called
    assert not any("create" in c for c in calls)


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


# ---------------------------------------------------------------------------
# Generated metadata commit tests and optional GitHub PR behavior
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("initial_status", "transition"),
    [
        ("in_progress", "complete"),
        ("in_progress", "needs_review"),
        ("in_progress", "hibernate"),
        ("failed", "reopen"),
        ("in_review", "merge"),
    ],
)
def test_generated_status_writes_commit_even_when_commit_status_false(
    tmp_path, initial_status, transition
):
    """With commit_status_changes=False, status changes leave tracked ticket files dirty (F33 fix)."""
    if shutil.which("git") is None:
        pytest.skip("git is required for status commit integration test")

    _init_git_repo(tmp_path)
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    ticket_kwargs = {}
    if transition == "merge":
        ticket_kwargs["review_verdict"] = "approved"
    if transition == "complete":
        wt = worktrees_dir / "tick-001"
        wt.mkdir()
        subprocess.run(["git", "init"], cwd=wt, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=wt,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=wt,
            check=True,
            capture_output=True,
        )
        (wt / "some_file.py").write_text("# test\n")
        subprocess.run(["git", "add", "."], cwd=wt, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial commit"],
            cwd=wt,
            check=True,
            capture_output=True,
        )
        ticket_kwargs["worktree"] = str(wt)
        ticket_kwargs["branch"] = "main"
    ticket_path = _write_ticket(tickets_dir, "TICK-001", initial_status, **ticket_kwargs)
    _commit_all(tmp_path)

    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = "tickets"
    cfg["worktrees_dir"] = "worktrees"
    cfg["commit_status_changes"] = False

    if transition == "complete":
        from lanegate.lifecycle import _has_committed_changes
        with patch("lanegate.lifecycle._has_committed_changes", return_value=True):
            cmd_complete("TICK-001", cfg, tmp_path, allow_drift=True)
    elif transition == "needs_review":
        cmd_needs_review("TICK-001", cfg, tmp_path, reason="needs human check")
    elif transition == "hibernate":
        cmd_hibernate("TICK-001", cfg, tmp_path, reason="pause")
    elif transition == "reopen":
        cmd_reopen("TICK-001", cfg, tmp_path)
    elif transition == "merge":
        cmd_merge("TICK-001", cfg, tmp_path)

    assert not _tracked_path_is_clean(tmp_path, ticket_path), "with commit_status_changes=False, ticket should remain dirty"


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
        "lanegate.lifecycle._push_branch_and_open_pr",
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
        "lanegate.lifecycle._push_branch_and_open_pr",
        side_effect=lambda *a, **kw: push_calls.append(a) or None,
    ):
        cmd_review("TICK-001", cfg, tmp_path, verdict="approved")

    assert len(push_calls) == 1, "_push_branch_and_open_pr not called once with github_pr=True"


# ---------------------------------------------------------------------------
# spawn_detached tests
# ---------------------------------------------------------------------------


def test_spawn_detached_unix(tmp_path):
    """On non-Windows: Popen is called with start_new_session=True."""

    log_path = tmp_path / "logs" / "watch.log"

    mock_proc = MagicMock()
    mock_proc.pid = 12345

    with (
        patch("lanegate.lifecycle.subprocess.Popen", return_value=mock_proc) as mock_popen,
        patch("lanegate.lifecycle.sys.platform", "linux"),
    ):
        pid = spawn_detached(["lanegate", "watch"], log_path)

    assert pid == 12345
    assert log_path.parent.exists(), "log_path.parent must be created"
    call_kwargs = mock_popen.call_args[1]
    assert call_kwargs.get("start_new_session") is True
    assert call_kwargs.get("close_fds") is True
    assert "creationflags" not in call_kwargs


def test_spawn_detached_windows(tmp_path):
    """On Windows: Popen is called with DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP."""
    log_path = tmp_path / "logs" / "watch.log"

    mock_proc = MagicMock()
    mock_proc.pid = 99999

    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    expected_flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

    import subprocess as _real_subprocess

    mock_popen = MagicMock(return_value=mock_proc)

    with (
        patch("lanegate.lifecycle.sys.platform", "win32"),
        patch(
            "builtins.__import__",
            side_effect=lambda name, *args, **kwargs: (
                type("mod", (), {"Popen": mock_popen, "DEVNULL": _real_subprocess.DEVNULL})()
                if name == "subprocess"
                else __import__(name, *args, **kwargs)
            ),
        ),
    ):
        # Import subprocess normally but intercept Popen on the win32 branch
        # We patch the inner import via a simpler approach:
        pass

    # Simpler approach: patch sys.platform and the subprocess import inside the win32 branch
    captured = {}

    def fake_popen(args, **kwargs):
        captured.update(kwargs)
        captured["args"] = args
        return mock_proc

    # The win32 branch does `import subprocess as _sp` then `_sp.Popen(...)`.
    # We patch subprocess.Popen globally for the duration.
    with (
        patch("subprocess.Popen", side_effect=fake_popen),
        patch("lanegate.lifecycle.sys.platform", "win32"),
    ):
        pid = spawn_detached(["lanegate", "watch"], log_path)

    assert pid == 99999
    assert log_path.parent.exists()
    assert captured.get("close_fds") is True
    assert captured.get("creationflags") == expected_flags
    assert "start_new_session" not in captured


def test_spawn_detached_creates_log_dir(tmp_path):
    """spawn_detached creates the log directory even if it doesn't exist."""
    log_path = tmp_path / "deeply" / "nested" / "dir" / "watch.log"
    assert not log_path.parent.exists()

    mock_proc = MagicMock()
    mock_proc.pid = 1

    with patch("lanegate.lifecycle.subprocess.Popen", return_value=mock_proc):
        spawn_detached(["lanegate", "watch"], log_path)

    assert log_path.parent.exists()


def test_spawn_detached_appends_to_existing_log(tmp_path):
    """spawn_detached opens the log in append mode, not truncating existing content."""
    log_path = tmp_path / "watch.log"
    log_path.write_text("existing content\n")

    mock_proc = MagicMock()
    mock_proc.pid = 2

    with patch("lanegate.lifecycle.subprocess.Popen", return_value=mock_proc):
        spawn_detached(["lanegate", "watch"], log_path)

    # File should still have the existing content (not been truncated)
    content = log_path.read_text()
    assert "existing content" in content


# ---------------------------------------------------------------------------
# resolve_reviewer tests
# ---------------------------------------------------------------------------


def test_resolve_reviewer_ticket_level_override():
    """ticket.reviewer takes precedence over everything."""
    ticket = {"reviewer": "aider"}
    cfg = {"reviewer": "openhands", "executor": "claude"}
    assert resolve_reviewer(ticket, cfg) == "aider"


def test_resolve_reviewer_cfg_level_when_no_ticket_override():
    """cfg.reviewer wins when the ticket has no reviewer field."""
    ticket = {}
    cfg = {"reviewer": "openhands", "executor": "claude"}
    assert resolve_reviewer(ticket, cfg) == "openhands"


def test_resolve_reviewer_falls_back_to_executor():
    """Falls back to cfg.executor when neither ticket.reviewer nor cfg.reviewer is set."""
    ticket = {}
    cfg = {"executor": "claude-process"}
    assert resolve_reviewer(ticket, cfg) == "claude-process"


def test_resolve_reviewer_default_when_nothing_set():
    """Returns 'claude' when no reviewer or executor is configured at all."""
    assert resolve_reviewer({}, {}) == "claude"


# ---------------------------------------------------------------------------
# TICK-043: no --no-verify in git commits
# ---------------------------------------------------------------------------


def test_commit_status_does_not_use_no_verify(tmp_path):
    """_commit_status must not pass --no-verify so pre-commit hooks run normally."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        result = MagicMock()
        result.returncode = 0
        return result

    ticket_path = tmp_path / "tickets" / "TICK-001.md"
    ticket_path.parent.mkdir(parents=True, exist_ok=True)
    ticket_path.write_text("---\nid: TICK-001\nstatus: open\n---\n")

    with patch("lanegate.lifecycle.subprocess.run", side_effect=fake_run):
        _commit_status(tmp_path, ticket_path, "TICK-001", "in_progress")

    for cmd in calls:
        assert "--no-verify" not in cmd, f"_commit_status used --no-verify in git call: {cmd}"


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


def test_resolve_reviewer_ticket_none_skips_to_cfg():
    """ticket.reviewer=None is treated as absent; cfg.reviewer is used."""
    ticket = {"reviewer": None}
    cfg = {"reviewer": "codex", "executor": "aider"}
    assert resolve_reviewer(ticket, cfg) == "codex"


def test_resolve_reviewer_empty_string_skips_to_cfg():
    """ticket.reviewer='' is treated as absent (falsy); cfg.reviewer is used."""
    ticket = {"reviewer": ""}
    cfg = {"reviewer": "human", "executor": "aider"}
    # Empty string is falsy, so cfg.reviewer wins
    assert resolve_reviewer(ticket, cfg) == "human"


# ---------------------------------------------------------------------------
# TICK-047: commit worktree claim AFTER setup, not before
# ---------------------------------------------------------------------------

from lanegate.lifecycle import cmd_start  # noqa: E402


def _start_cfg(tmp_path, *, commit_status_changes=False):
    """Minimal config for cmd_start tests."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir(exist_ok=True)
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir(exist_ok=True)
    return {
        "ticket_prefix": "TICK",
        "tickets_dir": str(tickets_dir),
        "worktrees_dir": str(worktrees_dir),
        "lock_statuses": ["in_progress", "code_complete", "in_review"],
        "commit_status_changes": commit_status_changes,
        "environments": [],
    }


def _write_open_ticket(tickets_dir: Path, ticket_id: str, touches=("lanegate/lifecycle.py",)):
    content = f"---\nid: {ticket_id}\ntitle: Test {ticket_id}\nstatus: open\ntouches:\n"
    for t in touches:
        content += f"  - {t}\n"
    content += "---\nBody text.\n"
    path = tickets_dir / f"{ticket_id}.md"
    path.write_text(content)
    return path


def _patch_start_externals(*, worktree_raises=False, commit_ok=True):
    """
    Return a context-manager stack that patches all external calls in cmd_start:
    - check_local_not_behind_remote: no-op
    - claim_lock: passthrough (uses actual tmp file via claim_lock fixture logic,
      but we don't need a real lock file because tests are single-threaded)
    - create_worktree: succeeds or raises RuntimeError
    - _commit_status: succeeds or fails
    """
    from contextlib import ExitStack

    stack = ExitStack()

    # Bypass fetch/divergence check
    stack.enter_context(patch("lanegate.lifecycle.check_local_not_behind_remote", return_value=None))

    # Bypass the flock (no .lanegate dir needed in tmp_path)
    import contextlib

    @contextlib.contextmanager
    def _noop_lock(_repo_root):
        yield

    stack.enter_context(patch("lanegate.lifecycle.claim_lock", side_effect=_noop_lock))

    # create_worktree: succeed or raise
    if worktree_raises:
        stack.enter_context(
            patch(
                "lanegate.lifecycle.create_worktree",
                side_effect=RuntimeError("git worktree add failed"),
            )
        )
    else:
        stack.enter_context(patch("lanegate.lifecycle.create_worktree", return_value=MagicMock()))

    # _commit_status: controlled
    stack.enter_context(patch("lanegate.lifecycle._is_git_worktree", return_value=True))
    stack.enter_context(
        patch(
            "lanegate.lifecycle._commit_status",
            return_value=commit_ok,
        )
    )

    # companion_branch_create: not relevant to these tests
    stack.enter_context(patch("lanegate.lifecycle.companion_branch_create", return_value=None))

    return stack


def test_start_worktree_failure_leaves_ticket_open(tmp_path):
    """If create_worktree raises, the ticket must NOT be committed as in_progress."""
    cfg = _start_cfg(tmp_path, commit_status_changes=True)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_open_ticket(tickets_dir, "TICK-100")

    commit_calls = []

    with _patch_start_externals(worktree_raises=True):
        # _commit_status is also patched in _patch_start_externals, but we want to
        # ensure it is truly never called, so override it here.
        with patch(
            "lanegate.lifecycle._commit_status",
            side_effect=lambda *a, **kw: commit_calls.append(a) or True,
        ):
            with pytest.raises(SystemExit) as exc_info:
                cmd_start("TICK-100", cfg, tmp_path)

    assert exc_info.value.code != 0, "cmd_start must exit non-zero on worktree failure"

    # The commit must NOT have been called — the status claim is never committed.
    assert not commit_calls, (
        f"_commit_status was called {len(commit_calls)} time(s) despite worktree failure"
    )

    # The ticket file must still be 'open'.
    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-100.md")
    assert ticket["status"] == "open", (
        f"ticket status advanced to '{ticket['status']}' despite worktree failure"
    )
    assert ticket.get("worktree") is None
    assert ticket.get("branch") is None


def test_start_success_marks_ticket_in_progress(tmp_path):
    """Successful worktree setup must leave the ticket as in_progress."""
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_open_ticket(tickets_dir, "TICK-101")

    with _patch_start_externals(worktree_raises=False):
        cmd_start("TICK-101", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-101.md")
    assert ticket["status"] == "in_progress"
    assert ticket.get("branch") == "tick-101"


def test_start_wildcard_lock_blocks_concrete_ticket(tmp_path, capsys):
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    blocker = _write_open_ticket(tickets_dir, "TICK-100", touches=('"*"',))
    blocker.write_text(blocker.read_text().replace("status: open", "status: in_progress"))
    _write_open_ticket(tickets_dir, "TICK-101", touches=("src/concrete.py",))

    with _patch_start_externals(worktree_raises=False), pytest.raises(SystemExit):
        cmd_start("TICK-101", cfg, tmp_path)

    assert "TICK-100" in capsys.readouterr().err
    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-101.md")
    assert ticket["status"] == "open"


def test_start_reattaches_hibernated_worktree(tmp_path):
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    wt = tmp_path / "worktrees" / "tick-104"
    wt.mkdir(parents=True)
    (tickets_dir / "TICK-104.md").write_text(
        "---\n"
        "id: TICK-104\n"
        "title: Test TICK-104\n"
        "status: hibernated\n"
        "touches:\n"
        "  - lanegate/lifecycle.py\n"
        f"worktree: {wt}\n"
        "branch: tick-104\n"
        "---\nBody text.\n"
    )

    with (
        patch("lanegate.lifecycle.check_local_not_behind_remote", return_value=None),
        patch("lanegate.lifecycle.claim_lock", side_effect=_noop_lock_ctx),
        patch("lanegate.lifecycle.create_worktree") as mock_create,
        patch("lanegate.lifecycle._commit_status", return_value=True),
        patch("lanegate.lifecycle.companion_branch_create", return_value=None),
    ):
        cmd_start("TICK-104", cfg, tmp_path)

    mock_create.assert_not_called()
    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-104.md")
    assert ticket["status"] == "in_progress"
    assert ticket["worktree"] == str(wt)


def test_start_success_commits_status_after_worktree(tmp_path):
    """When commit_status_changes=True, _commit_status is called only after create_worktree returns."""
    cfg = _start_cfg(tmp_path, commit_status_changes=True)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_open_ticket(tickets_dir, "TICK-102")

    call_order = []

    def recording_create_worktree(*args, **kwargs):
        call_order.append("create_worktree")
        return MagicMock()

    def recording_commit_status(*args, **kwargs):
        call_order.append("_commit_status")
        return True

    with (
        patch("lanegate.lifecycle.check_local_not_behind_remote", return_value=None),
        patch("lanegate.lifecycle.claim_lock", side_effect=_noop_lock_ctx),
        patch("lanegate.lifecycle.create_worktree", side_effect=recording_create_worktree),
        patch("lanegate.lifecycle._is_git_worktree", return_value=True),
        patch("lanegate.lifecycle._commit_status", side_effect=recording_commit_status),
        patch("lanegate.lifecycle.companion_branch_create", return_value=None),
    ):
        cmd_start("TICK-102", cfg, tmp_path)

    assert "create_worktree" in call_order
    assert "_commit_status" in call_order
    wt_idx = call_order.index("create_worktree")
    cs_idx = call_order.index("_commit_status")
    assert wt_idx < cs_idx, (
        f"create_worktree (pos {wt_idx}) must happen before _commit_status (pos {cs_idx})"
    )


def _noop_lock_ctx(repo_root):
    """Standalone no-op lock context manager (for use outside _patch_start_externals)."""
    import contextlib

    @contextlib.contextmanager
    def _inner(_r):
        yield

    return _inner(repo_root)


def test_start_idempotent_retry_after_failure_succeeds(tmp_path):
    """After a worktree failure leaves the ticket open, a second attempt must succeed."""
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_open_ticket(tickets_dir, "TICK-103")

    # First attempt: worktree creation fails
    with _patch_start_externals(worktree_raises=True):
        with pytest.raises(SystemExit):
            cmd_start("TICK-103", cfg, tmp_path)

    # Ticket must still be open after the failed first attempt
    from lanegate.ticket import parse_ticket

    ticket_after_fail = parse_ticket(tickets_dir / "TICK-103.md")
    assert ticket_after_fail["status"] == "open", (
        f"ticket not reset to 'open' after first failed attempt: '{ticket_after_fail['status']}'"
    )

    # Second attempt: worktree creation succeeds
    with _patch_start_externals(worktree_raises=False):
        cmd_start("TICK-103", cfg, tmp_path)

    ticket_after_success = parse_ticket(tickets_dir / "TICK-103.md")
    assert ticket_after_success["status"] == "in_progress", (
        f"retry after failure should mark ticket in_progress, got '{ticket_after_success['status']}'"
    )


def test_hibernate_writes_notes_and_preserves_worktree(tmp_path):
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    wt = tmp_path / "worktrees" / "tick-120"
    wt.mkdir(parents=True)
    (tickets_dir / "TICK-120.md").write_text(
        "---\n"
        "id: TICK-120\n"
        "title: Test TICK-120\n"
        "status: in_progress\n"
        "touches:\n"
        "  - lanegate/lifecycle.py\n"
        f"worktree: {wt}\n"
        "branch: tick-120\n"
        "close_criteria: Notes are written.\n"
        "---\nBody text.\n"
    )

    def git_mock(args, **kwargs):
        if args[:2] == ["git", "log"]:
            return MagicMock(returncode=0, stdout="abc123 partial commit\n", stderr="")
        if args[:2] == ["git", "diff"]:
            return MagicMock(
                returncode=0,
                stdout="diff --git a/lanegate/lifecycle.py b/lanegate/lifecycle.py\n",
                stderr="",
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("lanegate.lifecycle.subprocess.run", side_effect=git_mock):
        cmd_hibernate("TICK-120", cfg, tmp_path, reason="usage limit")

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-120.md")
    assert ticket["status"] == "hibernated"
    assert ticket["worktree"] == str(wt)
    assert not (tmp_path / ".lanegate" / "notes" / "lanegate_lifecycle.py.md").exists()
    note = (tmp_path / ".lanegate" / "recovery" / "TICK-120.md").read_text()
    assert "Hibernated partial work" in note
    assert "Worktree:" in note
    assert "usage limit" in note


def test_stop_sigterms_live_executor_and_hibernates(tmp_path):
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_ticket(tickets_dir, "TICK-330", "in_progress")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    marker_base = tmp_path / ".lanegate" / "TICK-330"
    marker_base.parent.mkdir()
    marker_base.with_suffix(".pid").write_text(f"{proc.pid}\n")
    marker_base.with_suffix(".session").write_text("session\n")

    try:
        result = cmd_stop("TICK-330", cfg, tmp_path, grace_seconds=0.1)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-330.md")
    assert ticket["status"] == "hibernated"
    assert result["stopped"] is True
    assert not marker_base.with_suffix(".pid").exists()
    assert not marker_base.with_suffix(".session").exists()


def test_stop_reports_clean_result_when_pid_already_gone(tmp_path):
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_ticket(tickets_dir, "TICK-331", "in_progress")
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    marker_base = tmp_path / ".lanegate" / "TICK-331"
    marker_base.parent.mkdir()
    marker_base.with_suffix(".pid").write_text(f"{proc.pid}\n")

    result = cmd_stop("TICK-331", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    assert result["stopped"] is False
    assert result["reason"] == "already_gone"
    assert parse_ticket(tickets_dir / "TICK-331.md")["status"] == "in_progress"
    assert not marker_base.with_suffix(".pid").exists()


def test_stop_no_pid_marker_returns_clean_result(tmp_path):
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_ticket(tickets_dir, "TICK-332", "in_progress")

    result = cmd_stop("TICK-332", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    assert result == {
        "ticket_id": "TICK-332",
        "stopped": False,
        "pid": None,
        "reason": "no_pid_marker",
    }
    assert parse_ticket(tickets_dir / "TICK-332.md")["status"] == "in_progress"


def test_stop_ignores_lock_and_watch_pid_files(tmp_path):
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_ticket(tickets_dir, "TICK-333", "in_progress")
    state_dir = tmp_path / ".lanegate"
    state_dir.mkdir()
    state_dir.joinpath("lock.pid").write_text(f"{os.getpid()}\n")
    state_dir.joinpath("watch.pid").write_text(f"{os.getpid()}\n")

    result = cmd_stop("TICK-333", cfg, tmp_path)

    assert result["reason"] == "no_pid_marker"
    assert os.getpid() == int(state_dir.joinpath("lock.pid").read_text())
    assert os.getpid() == int(state_dir.joinpath("watch.pid").read_text())


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


# ---------------------------------------------------------------------------
# TICK-046: check_touches_compliance — block completion on undeclared file drift
# ---------------------------------------------------------------------------


def _make_git_diff_mock(committed_files=(), uncommitted_files=()):
    """Return a mock for subprocess.run that simulates git diff --name-only output."""

    def mock_run(args, **kwargs):
        if "diff" in args and "main...HEAD" in args:
            output = "\n".join(committed_files) + ("\n" if committed_files else "")
            return MagicMock(returncode=0, stdout=output, stderr="")
        if "diff" in args and "HEAD" in args:
            output = "\n".join(uncommitted_files) + ("\n" if uncommitted_files else "")
            return MagicMock(returncode=0, stdout=output, stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    return mock_run


def test_touches_compliance_blocks_undeclared_files(tmp_path, capsys):
    """check_touches_compliance raises SystemExit when diff contains undeclared files."""
    ticket = {"id": "TICK-200", "touches": ["lanegate/lifecycle.py"]}
    mock_run = _make_git_diff_mock(
        committed_files=["lanegate/lifecycle.py", "lanegate/executor.py"],
    )
    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        with pytest.raises(SystemExit) as exc_info:
            check_touches_compliance("TICK-200", ticket, tmp_path)
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "lanegate/executor.py" in err
    assert "ERROR" in err


def test_touches_compliance_passes_when_only_declared_files(tmp_path):
    """check_touches_compliance does NOT raise when all changed files are declared."""
    ticket = {"id": "TICK-201", "touches": ["lanegate/lifecycle.py", "tests/test_lifecycle.py"]}
    mock_run = _make_git_diff_mock(
        committed_files=["lanegate/lifecycle.py", "tests/test_lifecycle.py"],
    )
    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        # Must not raise
        check_touches_compliance("TICK-201", ticket, tmp_path)


def test_touches_compliance_paired_test_file_not_declared_passes(tmp_path):
    """TICK-245: a committed test file paired with an already-declared module is
    not scope drift, even when the test file itself isn't in touches."""
    ticket = {"id": "TICK-201b", "touches": ["lanegate/lifecycle.py"]}
    mock_run = _make_git_diff_mock(
        committed_files=["lanegate/lifecycle.py", "tests/test_lifecycle.py"],
    )
    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        # Must not raise
        check_touches_compliance("TICK-201b", ticket, tmp_path)


def test_touches_compliance_wildcard_skips_check(tmp_path):
    """touches: ['*'] bypasses the drift check entirely — no error even with unexpected files."""
    ticket = {"id": "TICK-202", "touches": ["*"]}
    mock_run = _make_git_diff_mock(
        committed_files=["lanegate/lifecycle.py", "lanegate/executor.py", "some/random/file.py"],
    )
    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        # Must not raise
        check_touches_compliance("TICK-202", ticket, tmp_path)


def test_touches_compliance_allow_drift_warns_not_blocks(tmp_path, capsys):
    """--allow-drift emits a WARNING to stderr but does NOT raise SystemExit."""
    ticket = {"id": "TICK-203", "touches": ["lanegate/lifecycle.py"]}
    mock_run = _make_git_diff_mock(
        committed_files=["lanegate/lifecycle.py", "lanegate/executor.py"],
    )
    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        # Must not raise
        check_touches_compliance("TICK-203", ticket, tmp_path, allow_drift=True)
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "lanegate/executor.py" in err


def test_touches_compliance_no_changes_passes(tmp_path):
    """When the diff is empty, check_touches_compliance passes regardless of touches."""
    ticket = {"id": "TICK-204", "touches": ["lanegate/lifecycle.py"]}
    mock_run = _make_git_diff_mock(committed_files=[], uncommitted_files=[])
    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        check_touches_compliance("TICK-204", ticket, tmp_path)


def _complete_cfg(tmp_path, touches=("lanegate/lifecycle.py",)):
    """Set up a minimal cfg and in_progress ticket with a real worktree directory."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir(exist_ok=True)
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir(exist_ok=True)
    wt = worktrees_dir / "tick-210"
    wt.mkdir()

    # Quote values that are special in YAML (e.g. '*' is an alias indicator)
    def _yaml_value(v):
        if v in ("*",):
            return f'"{v}"'
        return v

    touches_yaml = "\n".join(f"  - {_yaml_value(t)}" for t in touches)
    content = (
        f"---\n"
        f"id: TICK-210\n"
        f"title: Test TICK-210\n"
        f"status: in_progress\n"
        f"worktree: {wt}\n"
        f"touches:\n"
        f"{touches_yaml}\n"
        f"---\nBody.\n"
    )
    (tickets_dir / "TICK-210.md").write_text(content)

    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)
    return cfg, tickets_dir, wt


def test_cmd_complete_blocks_on_undeclared_file(tmp_path, capsys):
    """cmd_complete exits non-zero when the diff contains undeclared files."""
    cfg, tickets_dir, wt = _complete_cfg(tmp_path, touches=["lanegate/lifecycle.py"])
    mock_run = _make_git_diff_mock(committed_files=["lanegate/lifecycle.py", "lanegate/executor.py"])

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        with pytest.raises(SystemExit) as exc_info:
            cmd_complete("TICK-210", cfg, tmp_path)
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "lanegate/executor.py" in err

    # Status must NOT have advanced
    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-210.md")
    assert ticket["status"] == "in_progress"


def test_cmd_complete_succeeds_with_declared_files_only(tmp_path):
    """cmd_complete succeeds when all changed files are declared in touches."""
    cfg, tickets_dir, wt = _complete_cfg(
        tmp_path, touches=["lanegate/lifecycle.py", "lanegate/executor.py"]
    )
    mock_run = _make_git_diff_mock(committed_files=["lanegate/lifecycle.py", "lanegate/executor.py"])

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        cmd_complete("TICK-210", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-210.md")
    assert ticket["status"] == "code_complete"


def test_cmd_complete_succeeds_with_wildcard_touches(tmp_path):
    """cmd_complete succeeds when touches: ['*'] even if unexpected files are changed."""
    cfg, tickets_dir, wt = _complete_cfg(tmp_path, touches=["*"])
    mock_run = _make_git_diff_mock(committed_files=["lanegate/lifecycle.py", "some/unrelated/file.py"])

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        cmd_complete("TICK-210", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-210.md")
    assert ticket["status"] == "code_complete"


def test_cmd_complete_allow_drift_warns_and_advances(tmp_path, capsys):
    """cmd_complete --allow-drift warns about undeclared files but still advances status."""
    cfg, tickets_dir, wt = _complete_cfg(tmp_path, touches=["lanegate/lifecycle.py"])
    mock_run = _make_git_diff_mock(committed_files=["lanegate/lifecycle.py", "lanegate/executor.py"])

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        cmd_complete("TICK-210", cfg, tmp_path, allow_drift=True)

    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "lanegate/executor.py" in err

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-210.md")
    assert ticket["status"] == "code_complete"


# TICK-086: --auto-update-touches


def test_cmd_complete_auto_update_touches_adds_undeclared_files(tmp_path, capsys):
    """--auto-update-touches auto-adds undeclared committed files to touches and advances."""
    cfg, tickets_dir, wt = _complete_cfg(tmp_path, touches=["lanegate/lifecycle.py"])
    mock_run = _make_git_diff_mock(committed_files=["lanegate/lifecycle.py", "lanegate/executor.py"])

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        cmd_complete("TICK-210", cfg, tmp_path, auto_update_touches=True)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-210.md")
    assert ticket["status"] == "code_complete"
    assert "lanegate/executor.py" in ticket["touches"]
    assert "lanegate/lifecycle.py" in ticket["touches"]

    err = capsys.readouterr().err
    assert "auto-updating touches" in err


def test_cmd_complete_auto_update_touches_noop_when_all_declared(tmp_path):
    """--auto-update-touches is a no-op when all committed files are already in touches."""
    cfg, tickets_dir, wt = _complete_cfg(tmp_path, touches=["lanegate/lifecycle.py"])
    mock_run = _make_git_diff_mock(committed_files=["lanegate/lifecycle.py"])

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        cmd_complete("TICK-210", cfg, tmp_path, auto_update_touches=True)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-210.md")
    assert ticket["status"] == "code_complete"
    assert ticket["touches"] == ["lanegate/lifecycle.py"]


def test_cmd_complete_blocks_without_auto_update_touches(tmp_path):
    """Without --auto-update-touches, undeclared files still block completion."""
    cfg, tickets_dir, wt = _complete_cfg(tmp_path, touches=["lanegate/lifecycle.py"])
    mock_run = _make_git_diff_mock(committed_files=["lanegate/lifecycle.py", "lanegate/executor.py"])

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run), pytest.raises(SystemExit):
        cmd_complete("TICK-210", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-210.md")
    assert ticket["status"] == "in_progress"


def test_reopen_transitions_failed_to_open(tmp_path):
    """cmd_reopen transitions failed → open."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    _write_ticket(tickets_dir, "TICK-001", "failed")
    cmd_reopen("TICK-001", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-001.md")
    assert ticket["status"] == "open"


def test_reopen_strips_failure_reason(tmp_path):
    """cmd_reopen removes the ## Failure Reason section from the body."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    path = tickets_dir / "TICK-002.md"
    path.write_text(
        "---\nid: TICK-002\ntitle: T\nstatus: failed\n---\n"
        "Background.\n\n## Failure Reason\n\nexecutor exited with code 1\n"
    )
    cmd_reopen("TICK-002", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(path)
    assert ticket["status"] == "open"
    assert "Failure Reason" not in ticket.get("_body", "")


def test_reopen_rejects_non_failed_ticket(tmp_path):
    """cmd_reopen exits with error if ticket is not in failed state."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    _write_ticket(tickets_dir, "TICK-003", "open")
    with pytest.raises(SystemExit):
        cmd_reopen("TICK-003", cfg, tmp_path)


def test_reopen_needs_review_with_no_commits_resets_to_open(tmp_path):
    """A needs_review ticket whose worktree never got any real commits (e.g.
    blocked by a pre-flight gate before an executor ran) resets to open with
    the stale empty worktree cleaned up, same as the failed case."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    wt_path = worktrees_dir / "tick-004"
    wt_path.mkdir()
    path = tickets_dir / "TICK-004.md"
    path.write_text(
        f"---\nid: TICK-004\ntitle: T\nstatus: needs_review\nworktree: {wt_path}\n"
        "branch: tick-004\n---\n"
        "Background.\n\n## Needs Review Reason\n\nacceptance-contract audit failed\n"
    )

    with patch("lanegate.reviewer.worktree_has_commits", return_value=False):
        cmd_reopen("TICK-004", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(path)
    assert ticket["status"] == "open"
    assert not ticket.get("worktree")
    assert "Needs Review Reason" not in ticket.get("_body", "")
    assert "## Status History" in ticket.get("_body", "")
    assert "needs_review → open" in ticket.get("_body", "")


def test_cmd_reopen_zero_commit_branch_resets_hibernated_ticket_to_open(tmp_path):
    """A hibernated branch with no ticket commits is safe to discard and retry."""
    _init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("init\n")
    _commit_all(tmp_path)
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt_path = worktrees_dir / "tick-005"
    subprocess.run(
        ["git", "worktree", "add", "-b", "tick-005", str(wt_path)], cwd=tmp_path, check=True
    )
    _write_ticket(
        tickets_dir,
        "TICK-005",
        "hibernated",
        worktree=str(wt_path),
        branch="tick-005",
    )
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    cmd_reopen("TICK-005", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-005.md")
    assert ticket["status"] == "open"
    assert ticket.get("worktree") is None
    assert not wt_path.exists()
    assert "hibernated → open" in ticket.get("_body", "")


def test_reopen_needs_review_with_commits_resets_to_code_complete(tmp_path):
    """A needs_review ticket whose worktree has real commits (implementation
    ran, then a post-implementation gate like a stale touches-scope check or
    a static-analysis finding downgraded it) resets to code_complete with the
    worktree/branch/commits preserved, ready for `lanegate review`. No main-branch
    drift here, so the rebase-onto-main check is a no-op."""
    if shutil.which("git") is None:
        pytest.skip("git is required for rebase-onto-main regression test")

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    wt_path = worktrees_dir / "tick-005"
    wt_path.mkdir()
    _init_git_repo(wt_path)
    (wt_path / "shared.py").write_text("line1\n")
    _commit_all(wt_path, "base")
    subprocess.run(["git", "checkout", "-b", "tick-005"], cwd=wt_path, check=True, capture_output=True)
    (wt_path / "foo.py").write_text("ticket work\n")
    _commit_all(wt_path, "ticket work")

    path = tickets_dir / "TICK-005.md"
    path.write_text(
        f"---\nid: TICK-005\ntitle: T\nstatus: needs_review\nworktree: {wt_path}\n"
        "branch: tick-005\nreview_verdict: changes_requested\nreview_summary: blocked by orchestrate gate\n"
        "---\n"
        "Background.\n\n## Needs Review Reason\n\ncommitted files outside touches list: foo.py\n"
    )

    cmd_reopen("TICK-005", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(path)
    assert ticket["status"] == "code_complete"
    assert ticket.get("worktree") == str(wt_path)
    assert ticket.get("branch") == "tick-005"
    assert not ticket.get("review_verdict")
    assert not ticket.get("review_summary")
    assert "Needs Review Reason" not in ticket.get("_body", "")
    assert "## Status History" in ticket.get("_body", "")
    assert "needs_review → code_complete" in ticket.get("_body", "")


def test_reopen_needs_review_with_conflicting_main_stays_needs_review_conflict_flagged(tmp_path):
    """A needs_review ticket whose branch predates a conflicting main change
    must not be silently restored for review — reopen should rebase-check
    first, hit a real conflict, abort it, and leave the ticket needs_review
    with the conflict recorded, per the same trust check orchestrate.py runs
    for hibernated auto-resume."""
    if shutil.which("git") is None:
        pytest.skip("git is required for rebase-onto-main regression test")

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    wt_path = worktrees_dir / "tick-900"
    wt_path.mkdir()
    _init_git_repo(wt_path)
    (wt_path / "shared.py").write_text("line1\n")
    _commit_all(wt_path, "base")

    subprocess.run(["git", "checkout", "-b", "tick-900"], cwd=wt_path, check=True, capture_output=True)
    (wt_path / "shared.py").write_text("line1\nticket change\n")
    _commit_all(wt_path, "ticket work")

    subprocess.run(["git", "checkout", "main"], cwd=wt_path, check=True, capture_output=True)
    (wt_path / "shared.py").write_text("line1\nmain change\n")
    _commit_all(wt_path, "main drifted")

    subprocess.run(["git", "checkout", "tick-900"], cwd=wt_path, check=True, capture_output=True)

    path = tickets_dir / "TICK-900.md"
    path.write_text(
        f"---\nid: TICK-900\ntitle: T\nstatus: needs_review\nworktree: {wt_path}\n"
        "branch: tick-900\n---\n"
        "Background.\n\n## Needs Review Reason\n\nsome prior gate\n"
    )

    with patch("lanegate.orchestrate.autofix.run_rebase_fix_agent", return_value=False):
        cmd_reopen("TICK-900", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(path)
    assert ticket["status"] == "needs_review"
    assert "conflict" in ticket.get("_body", "").lower()

    status = subprocess.run(
        ["git", "status"], cwd=wt_path, capture_output=True, text=True
    )
    assert "rebase in progress" not in status.stdout, "conflicting rebase must be aborted, not left mid-rebase"


def test_reopen_needs_review_with_stale_but_clean_rebase_updates_worktree_before_review(tmp_path):
    """A needs_review ticket whose branch is stale but rebases cleanly should
    actually be rebased onto main (worktree contains the intervening main
    commits) before being handed back for review — not restored as-is."""
    if shutil.which("git") is None:
        pytest.skip("git is required for rebase-onto-main regression test")

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    wt_path = worktrees_dir / "tick-901"
    wt_path.mkdir()
    _init_git_repo(wt_path)
    (wt_path / "shared.py").write_text("line1\n")
    _commit_all(wt_path, "base")

    subprocess.run(["git", "checkout", "-b", "tick-901"], cwd=wt_path, check=True, capture_output=True)
    (wt_path / "ticket_only.py").write_text("ticket work\n")
    _commit_all(wt_path, "ticket work")

    subprocess.run(["git", "checkout", "main"], cwd=wt_path, check=True, capture_output=True)
    (wt_path / "main_only.py").write_text("intervening main commit\n")
    _commit_all(wt_path, "main drifted")

    subprocess.run(["git", "checkout", "tick-901"], cwd=wt_path, check=True, capture_output=True)

    path = tickets_dir / "TICK-901.md"
    path.write_text(
        f"---\nid: TICK-901\ntitle: T\nstatus: needs_review\nworktree: {wt_path}\n"
        "branch: tick-901\n---\n"
        "Background.\n\n## Needs Review Reason\n\nsome prior gate\n"
    )

    cmd_reopen("TICK-901", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(path)
    assert ticket["status"] == "code_complete"
    assert ticket.get("worktree") == str(wt_path)

    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=wt_path, capture_output=True, text=True
    ).stdout
    assert "main drifted" in log, "worktree must actually be rebased onto the new main tip"
    assert (wt_path / "main_only.py").exists(), "intervening main commit's file must be present after rebase"


def test_reopen_from_code_complete(tmp_path):
    """A code_complete ticket whose worktree has zero real commits (the
    cmd_complete guard's own bug scenario, wedged before the guard existed)
    recovers to open with the stale empty worktree cleaned up."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    wt_path = worktrees_dir / "tick-006"
    wt_path.mkdir()
    path = tickets_dir / "TICK-006.md"
    path.write_text(
        f"---\nid: TICK-006\ntitle: T\nstatus: code_complete\nworktree: {wt_path}\n"
        "branch: tick-006\n---\n"
        "Background.\n"
    )

    with patch("lanegate.reviewer.worktree_has_commits", return_value=False):
        cmd_reopen("TICK-006", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(path)
    assert ticket["status"] == "open"
    assert not ticket.get("worktree")
    assert "## Status History" in ticket.get("_body", "")
    assert "code_complete → open" in ticket.get("_body", "")


def test_reopen_from_code_complete_with_commits_refuses(tmp_path):
    """A code_complete ticket with real commits is healthy, not wedged —
    reopen must refuse rather than discard legitimate work."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    wt_path = worktrees_dir / "tick-007"
    wt_path.mkdir()
    path = tickets_dir / "TICK-007.md"
    path.write_text(
        f"---\nid: TICK-007\ntitle: T\nstatus: code_complete\nworktree: {wt_path}\n"
        "branch: tick-007\n---\n"
        "Background.\n"
    )

    with patch("lanegate.reviewer.worktree_has_commits", return_value=True):
        with pytest.raises(SystemExit) as exc_info:
            cmd_reopen("TICK-007", cfg, tmp_path)

    assert exc_info.value.code != 0

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(path)
    assert ticket["status"] == "code_complete"
    assert ticket.get("worktree") == str(wt_path)


# ---------------------------------------------------------------------------
# cmd_open: draft → open without re-running analysis
# ---------------------------------------------------------------------------


def _write_draft_ticket(tickets_dir: Path, ticket_id: str, touches: list | None = None) -> None:
    touches_yaml = ""
    if touches:
        touches_yaml = "touches:\n" + "".join(f"  - {t}\n" for t in touches)
    (tickets_dir / f"{ticket_id}.md").write_text(
        f"---\nid: {ticket_id}\ntitle: Test {ticket_id}\nstatus: draft\n{touches_yaml}---\nBody.\n"
    )


def test_cmd_open_transitions_draft_to_open(tmp_path):
    from lanegate.lifecycle import cmd_open
    from lanegate.ticket import parse_ticket

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    _write_draft_ticket(tickets_dir, "TICK-010", touches=["src/foo.py"])
    cmd_open("TICK-010", cfg, tmp_path)

    ticket = parse_ticket(tickets_dir / "TICK-010.md")
    assert ticket["status"] == "open"


def test_cmd_open_rejects_empty_touches(tmp_path):
    from lanegate.lifecycle import cmd_open

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    _write_draft_ticket(tickets_dir, "TICK-011", touches=[])
    with pytest.raises(SystemExit):
        cmd_open("TICK-011", cfg, tmp_path)


def test_cmd_open_rejects_non_draft(tmp_path):
    from lanegate.lifecycle import cmd_open

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    _write_ticket(tickets_dir, "TICK-012", "open")
    with pytest.raises(SystemExit):
        cmd_open("TICK-012", cfg, tmp_path)


def test_cmd_open_writes_status_changed_at(tmp_path):
    from lanegate.lifecycle import cmd_open
    from lanegate.ticket import parse_ticket

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    _write_draft_ticket(tickets_dir, "TICK-013", touches=["src/bar.py"])
    cmd_open("TICK-013", cfg, tmp_path)

    ticket = parse_ticket(tickets_dir / "TICK-013.md")
    assert _is_iso_utc(ticket.get("status_changed_at"))


# ---------------------------------------------------------------------------
# TICK-082: status_changed_at timestamp is written on every transition
# ---------------------------------------------------------------------------

import re as _re  # noqa: E402


def _is_iso_utc(s: str) -> bool:
    """True if the string looks like a UTC ISO-8601 timestamp (YYYY-MM-DDTHH:MM:SSZ)."""
    return bool(_re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", str(s or "")))


def test_start_writes_status_changed_at(tmp_path):
    """cmd_start must write status_changed_at when transitioning open → in_progress."""
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_open_ticket(tickets_dir, "TICK-200")

    with _patch_start_externals(worktree_raises=False):
        cmd_start("TICK-200", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-200.md")
    assert ticket["status"] == "in_progress"
    assert _is_iso_utc(ticket.get("status_changed_at")), (
        f"status_changed_at not set or wrong format: {ticket.get('status_changed_at')!r}"
    )


def test_hibernate_writes_status_changed_at(tmp_path):
    """cmd_hibernate must write status_changed_at when transitioning in_progress → hibernated."""
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    wt = tmp_path / "worktrees" / "tick-201"
    wt.mkdir(parents=True)
    (tickets_dir / "TICK-201.md").write_text(
        "---\n"
        "id: TICK-201\n"
        "title: Test TICK-201\n"
        "status: in_progress\n"
        "touches:\n"
        "  - lanegate/lifecycle.py\n"
        f"worktree: {wt}\n"
        "branch: tick-201\n"
        "close_criteria: Hibernate test.\n"
        "---\nBody text.\n"
    )

    def git_mock(args, **kwargs):
        if args[:2] == ["git", "log"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        if args[:2] == ["git", "diff"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("lanegate.lifecycle.subprocess.run", side_effect=git_mock):
        cmd_hibernate("TICK-201", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-201.md")
    assert ticket["status"] == "hibernated"
    assert _is_iso_utc(ticket.get("status_changed_at")), (
        f"status_changed_at not set on hibernate: {ticket.get('status_changed_at')!r}"
    )


def test_merge_writes_status_changed_at(tmp_path):
    """cmd_merge must write status_changed_at when transitioning in_review → merged."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(
        tickets_dir, "TICK-202", "in_review", branch="tick-202", review_verdict="approved"
    )

    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    def mock_run(args, **kwargs):
        if "merge" in args and "--no-ff" in args:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        cmd_merge("TICK-202", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-202.md")
    assert ticket["status"] == "merged"
    assert _is_iso_utc(ticket.get("status_changed_at")), (
        f"status_changed_at not set on merge: {ticket.get('status_changed_at')!r}"
    )


def test_merge_auto_promotes_trigger_auto(tmp_path, capsys):
    """cmd_merge triggers auto-promotion for environments where trigger==auto and from matches merge target."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(
        tickets_dir, "TICK-001", "in_review", branch="tick-001", review_verdict="approved"
    )
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)
    cfg["environments"] = [
        {
            "name": "paper",
            "branch": "paper",
            "from": "main",
            "trigger": "auto",
            "sync": "ff-only",
            "guard_script": None,
            "pre_promote": [],
            "post_promote": [],
        }
    ]

    promotion_called = []

    def mock_run(args, **kwargs):
        if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return MagicMock(returncode=0, stdout="main\n", stderr="")
        if "merge" in args and "--no-ff" in args:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    def mock_auto_promote(cfg, repo_root, from_branch):
        promotion_called.append(from_branch)

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        with patch("lanegate.lifecycle._auto_promote_environments", side_effect=mock_auto_promote) as mock_ap:
            cmd_merge("TICK-001", cfg, tmp_path)
            mock_ap.assert_called_once()
            _, _, called_from = mock_ap.call_args.args
            assert called_from == "main"


def test_merge_auto_promote_failure_warns(tmp_path, capsys):
    """Auto-promote failure must warn but not fail the merge."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(
        tickets_dir, "TICK-001", "in_review", branch="tick-001", review_verdict="approved"
    )
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)
    cfg["environments"] = [
        {
            "name": "paper",
            "branch": "paper",
            "from": "main",
            "trigger": "auto",
            "sync": "ff-only",
            "guard_script": None,
            "pre_promote": [],
            "post_promote": [],
        }
    ]

    def mock_run(args, **kwargs):
        if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return MagicMock(returncode=0, stdout="main\n", stderr="")
        if "merge" in args and "--no-ff" in args:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    def failing_auto_promote(cfg, repo_root, from_branch):
        import sys

        print("WARNING: auto-promote of 'paper' failed — merge succeeded; resolve the environment promotion manually.", file=sys.stderr)

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        with patch("lanegate.lifecycle._auto_promote_environments", side_effect=failing_auto_promote):
            cmd_merge("TICK-001", cfg, tmp_path)  # must not raise

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-001.md")
    assert ticket["status"] == "merged"
    captured = capsys.readouterr()
    assert "WARNING" in captured.err and "paper" in captured.err


def test_merge_no_auto_promote_non_matching(tmp_path):
    """Environments whose from does not match the merge target are skipped."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(
        tickets_dir, "TICK-001", "in_review", branch="tick-001", review_verdict="approved"
    )
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)
    cfg["environments"] = [
        {
            "name": "staging",
            "branch": "staging",
            "from": "develop",  # does not match "main"
            "trigger": "auto",
            "sync": "ff-only",
            "guard_script": None,
            "pre_promote": [],
            "post_promote": [],
        }
    ]

    promotion_called = []

    def mock_run(args, **kwargs):
        if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return MagicMock(returncode=0, stdout="main\n", stderr="")
        if "merge" in args and "--no-ff" in args:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    def spy_auto_promote(cfg, repo_root, from_branch):
        # Call the real function to verify filtering logic
        from lanegate.promote import _auto_promote_environments as real_aape
        from unittest.mock import patch as _patch

        with _patch("lanegate.promote._run_promotion") as mock_rp:
            real_aape(cfg, repo_root, from_branch)
            promotion_called.extend(mock_rp.call_args_list)

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        with patch("lanegate.lifecycle._auto_promote_environments", side_effect=spy_auto_promote):
            cmd_merge("TICK-001", cfg, tmp_path)

    assert not promotion_called, "Expected no promotions for non-matching from branch"


def test_reopen_writes_status_changed_at(tmp_path):
    """cmd_reopen must write status_changed_at when transitioning failed → open."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    _write_ticket(tickets_dir, "TICK-203", "failed")
    cmd_reopen("TICK-203", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-203.md")
    assert ticket["status"] == "open"
    assert _is_iso_utc(ticket.get("status_changed_at")), (
        f"status_changed_at not set on reopen: {ticket.get('status_changed_at')!r}"
    )


def test_missing_status_changed_at_shows_dash_on_board(tmp_path):
    """Tickets without status_changed_at must display '—' on the board (no crash)."""
    from lanegate.board import _time_in_status

    ticket_no_ts = {"id": "TICK-300", "status": "open", "title": "No timestamp"}
    assert _time_in_status(ticket_no_ts) == "—", "Expected '—' for ticket without status_changed_at"


def test_valid_status_changed_at_shows_age_on_board():
    """A ticket with a recent status_changed_at must return a non-dash age string."""
    import datetime as _dt

    from lanegate.board import _time_in_status

    # Set timestamp to 2 hours ago
    two_hours_ago = _dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=2)
    ticket = {
        "id": "TICK-301",
        "status": "in_progress",
        "title": "With timestamp",
        "status_changed_at": two_hours_ago.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    age = _time_in_status(ticket)
    assert age != "—", f"Expected a real age string, got {age!r}"
    assert "h" in age or "m" in age or "d" in age, f"Unexpected age format: {age!r}"


# ---------------------------------------------------------------------------
# _cleanup_ticket_notes
# ---------------------------------------------------------------------------


def test_cleanup_stale_notes(tmp_path):
    from lanegate.lifecycle import _cleanup_ticket_notes

    notes_dir = tmp_path / ".lanegate" / "notes"
    notes_dir.mkdir(parents=True)

    stale1 = notes_dir / "lanegate_lifecycle.py.md"
    stale2 = notes_dir / "tests_test_lifecycle.py.md"
    stale1.write_text("Stale hibernation dump.")
    stale2.write_text("More stale content.")

    curated = notes_dir / "lanegate_analyze.py.md"
    curated.write_text("Curated note from TICK-085.")

    companion_curated = notes_dir / "lanegate_companion.py.md"
    companion_curated.write_text("Curated note from TICK-135.")

    ticket = {
        "id": "TICK-116",
        "touches": ["lanegate/lifecycle.py", "tests/test_lifecycle.py"],
    }

    _cleanup_ticket_notes(ticket, tmp_path)

    assert not stale1.exists(), "stale note for lanegate/lifecycle.py should have been deleted"
    assert not stale2.exists(), "stale note for tests/test_lifecycle.py should have been deleted"
    assert curated.exists(), "lanegate_analyze.py.md must be preserved"
    assert companion_curated.exists(), "lanegate_companion.py.md must be preserved"


def test_cleanup_stale_notes_missing_notes_dir_is_noop(tmp_path):
    from lanegate.lifecycle import _cleanup_ticket_notes

    ticket = {
        "id": "TICK-001",
        "touches": ["lanegate/lifecycle.py"],
    }
    _cleanup_ticket_notes(ticket, tmp_path)


def test_cleanup_stale_notes_called_on_merge(tmp_path):
    """cmd_merge must call _cleanup_ticket_notes so stale orphan notes are swept on resolution."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()

    notes_dir = tmp_path / ".lanegate" / "notes"
    notes_dir.mkdir(parents=True)

    stale = notes_dir / "lanegate_lifecycle.py.md"
    stale.write_text("Stale content.")

    content = (
        "---\n"
        "id: TICK-400\n"
        "title: Test cleanup\n"
        "status: in_review\n"
        "branch: tick-400\n"
        "review_verdict: approved\n"
        "touches:\n"
        "  - lanegate/lifecycle.py\n"
        "---\nBody.\n"
    )
    (tickets_dir / "TICK-400.md").write_text(content)

    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    def mock_run(args, **kwargs):
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        cmd_merge("TICK-400", cfg, tmp_path)

    assert not stale.exists(), "stale notes file should be deleted by cmd_merge"


# ---------------------------------------------------------------------------
# analyze session ID capture (TICK-188)
# ---------------------------------------------------------------------------


def test_analyze_captures_session_id(tmp_path):
    """cmd_analyze persists analyze_session_id to the ticket when model_fn returns a session tuple."""
    from lanegate.analyze import cmd_analyze
    from lanegate.ticket import parse_ticket

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    ticket_path = tickets_dir / "TICK-999.md"
    ticket_path.write_text(
        "---\n"
        "id: TICK-999\n"
        "title: Test session capture\n"
        "status: draft\n"
        "touches: []\n"
        "---\nTest ticket body.\n"
    )

    _cfg = {
        "ticket_prefix": "TICK",
        "tickets_dir": "tickets",
        "commit_status_changes": False,
    }
    session_id = "test-session-abc-123"
    model_response = '{"touches": ["lanegate/foo.py"], "close_criteria": "foo works", "depends_on": []}'

    def model_fn_with_session(prompt: str):
        return model_response, session_id

    cmd_analyze("TICK-999", _cfg, tmp_path, model_fn=model_fn_with_session)

    t = parse_ticket(ticket_path)
    assert t.get("analyze_session_id") == session_id, (
        f"expected analyze_session_id={session_id!r}, got {t.get('analyze_session_id')!r}"
    )


def test_analyze_no_session_when_model_fn_returns_str(tmp_path):
    """cmd_analyze does not set analyze_session_id when model_fn returns plain str."""
    from lanegate.analyze import cmd_analyze
    from lanegate.ticket import parse_ticket

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    ticket_path = tickets_dir / "TICK-998.md"
    ticket_path.write_text(
        "---\n"
        "id: TICK-998\n"
        "title: Test no session\n"
        "status: draft\n"
        "touches: []\n"
        "---\nTest ticket body.\n"
    )

    _cfg = {
        "ticket_prefix": "TICK",
        "tickets_dir": "tickets",
        "commit_status_changes": False,
    }
    model_response = '{"touches": ["lanegate/foo.py"], "close_criteria": "foo works", "depends_on": []}'

    cmd_analyze("TICK-998", _cfg, tmp_path, model_fn=lambda p: model_response)

    t = parse_ticket(ticket_path)
    assert "analyze_session_id" not in t or t.get("analyze_session_id") is None


def test_hibernate_reset_preserves_branch_if_diff_truncated(tmp_path):
    """When reset=True and diff is truncated (>30KB), the branch is preserved for recovery."""
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    wt = tmp_path / "worktrees" / "tick-150"
    wt.mkdir(parents=True)
    (tickets_dir / "TICK-150.md").write_text(
        "---\n"
        "id: TICK-150\n"
        "title: Test TICK-150\n"
        "status: in_progress\n"
        "touches:\n"
        "  - lanegate/lifecycle.py\n"
        f"worktree: {wt}\n"
        "branch: tick-150\n"
        "close_criteria: Diff is preserved.\n"
        "---\nBody text.\n"
    )

    # Create a large diff (40 KB) that will be truncated
    large_diff = "diff --git a/file.py b/file.py\n" + "x" * 35_000 + "\n"

    def git_mock(args, **kwargs):
        if args[:2] == ["git", "log"]:
            return MagicMock(returncode=0, stdout="abc123 partial commit\n", stderr="")
        if args[:2] == ["git", "diff"]:
            # Return a diff larger than _MAX_DIFF_BYTES (30_000)
            return MagicMock(returncode=0, stdout=large_diff, stderr="")
        if args[:2] == ["git", "branch"]:
            # Should NOT be called for branch deletion
            raise AssertionError("git branch -D should not be called when diff is truncated")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.lifecycle.subprocess.run", side_effect=git_mock),
        patch("lanegate.git.subprocess.run", side_effect=git_mock),
    ):
        cmd_hibernate("TICK-150", cfg, tmp_path, reset=True, reason="test")

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-150.md")
    assert ticket["status"] == "hibernated"
    # Branch should be preserved (not set to None)
    assert ticket["branch"] == "tick-150"
    # Worktree should be cleared
    assert ticket["worktree"] is None
    # Recovery note should show truncation
    note = (tmp_path / ".lanegate" / "recovery" / "TICK-150.md").read_text()
    assert "(diff truncated at 30 KB)" in note


def test_hibernate_reset_deletes_branch_if_diff_not_truncated(tmp_path):
    """When reset=True and diff is NOT truncated, the branch is deleted as before."""
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    wt = tmp_path / "worktrees" / "tick-151"
    wt.mkdir(parents=True)
    (tickets_dir / "TICK-151.md").write_text(
        "---\n"
        "id: TICK-151\n"
        "title: Test TICK-151\n"
        "status: in_progress\n"
        "touches:\n"
        "  - lanegate/lifecycle.py\n"
        f"worktree: {wt}\n"
        "branch: tick-151\n"
        "close_criteria: Diff is small.\n"
        "---\nBody text.\n"
    )

    # Create a small diff that will NOT be truncated
    small_diff = "diff --git a/file.py b/file.py\n+small change\n"
    branch_deleted = False

    def git_mock(args, **kwargs):
        nonlocal branch_deleted
        if args[:2] == ["git", "log"]:
            return MagicMock(returncode=0, stdout="abc123 partial commit\n", stderr="")
        if args[:2] == ["git", "diff"]:
            # Return a small diff
            return MagicMock(returncode=0, stdout=small_diff, stderr="")
        if args[:3] == ["git", "branch", "-D"]:
            # Branch deletion should be called
            branch_deleted = True
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.lifecycle.subprocess.run", side_effect=git_mock),
        patch("lanegate.git.subprocess.run", side_effect=git_mock),
    ):
        cmd_hibernate("TICK-151", cfg, tmp_path, reset=True, reason="test")

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-151.md")
    assert ticket["status"] == "hibernated"
    # Branch should be deleted
    assert ticket["branch"] is None
    # Worktree should be cleared
    assert ticket["worktree"] is None
    # Branch deletion should have been called
    assert branch_deleted, "git branch -D should have been called for small diff"
    # Recovery note should NOT show truncation
    note = (tmp_path / ".lanegate" / "recovery" / "TICK-151.md").read_text()
    assert "(diff truncated" not in note


def test_hibernate_reset_preserves_branch_when_diff_capture_fails(tmp_path):
    """A failed committed-diff capture cannot be treated as an empty diff during reset."""
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    wt = tmp_path / "worktrees" / "tick-152"
    wt.mkdir(parents=True)
    (tickets_dir / "TICK-152.md").write_text(
        "---\n"
        "id: TICK-152\n"
        "title: Test TICK-152\n"
        "status: in_progress\n"
        "touches:\n"
        "  - lanegate/lifecycle.py\n"
        f"worktree: {wt}\n"
        "branch: tick-152\n"
        "close_criteria: Capture failures preserve recovery.\n"
        "---\nBody text.\n"
    )

    def git_mock(args, **kwargs):
        if args[:2] == ["git", "log"]:
            return MagicMock(returncode=0, stdout="abc123 partial commit\n", stderr="")
        if args[:2] == ["git", "diff"]:
            return MagicMock(returncode=128, stdout="", stderr="")
        if args[:3] == ["git", "branch", "-D"]:
            raise AssertionError("git branch -D should not be called when diff capture fails")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.lifecycle.subprocess.run", side_effect=git_mock),
        patch("lanegate.git.subprocess.run", side_effect=git_mock),
    ):
        cmd_hibernate("TICK-152", cfg, tmp_path, reset=True, reason="test")

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-152.md")
    assert ticket["status"] == "hibernated"
    assert ticket["branch"] == "tick-152"
    assert ticket["worktree"] is None
    note = (tmp_path / ".lanegate" / "recovery" / "TICK-152.md").read_text()
    assert "git diff main...tick-152 failed (exit 128)" in note
    assert "Git capture warning" in note


# ---------------------------------------------------------------------------
# TICK-283: acceptance verification gate
# ---------------------------------------------------------------------------


def _write_ticket_with_body(tickets_dir: Path, ticket_id: str, status: str, body: str, **kwargs) -> Path:
    """Like _write_ticket, but with a caller-supplied body (for Acceptance
    Criteria checklists) and passthrough of extra frontmatter kwargs
    (e.g. review_verdict, verification)."""
    lines = [f"---", f"id: {ticket_id}", f"title: Test {ticket_id}", f"status: {status}"]
    for key, value in kwargs.items():
        if value is None:
            continue
        if key == "touches" and isinstance(value, list):
            lines.append("touches:")
            lines.extend(f"  - {t}" for t in value)
        elif key == "verification" and isinstance(value, list):
            lines.append("verification:")
            for rec in value:
                lines.append(f"  - criterion: {rec['criterion']!r}")
                lines.append(f"    status: {rec['status']}")
                lines.append(f"    evidence: {rec.get('evidence', '')!r}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append(body)
    path = tickets_dir / f"{ticket_id}.md"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_cmd_complete_records_verification_from_acceptance_checklist(tmp_path):
    """cmd_complete populates ticket['verification'] with one record per
    Acceptance Criteria checklist item, without blocking (verdict is None
    at complete time -- the gate only blocks an approved review verdict)."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-300"
    wt.mkdir()
    body = "## Acceptance Criteria\n- [ ] Add a widget function\n"
    _write_ticket_with_body(
        tickets_dir, "TICK-300", "in_progress", body, worktree=str(wt), touches=["lanegate/foo.py"]
    )
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    mock_run = _make_git_diff_mock(committed_files=["lanegate/foo.py"])
    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        with patch(
            "lanegate.analyze._worktree_diff_text",
            return_value="def add_widget_function(): pass",
        ):
            cmd_complete("TICK-300", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-300.md")
    assert ticket["status"] == "code_complete"
    assert ticket["verification"] == [
        {
            "criterion": "Add a widget function",
            "status": "verified",
            "evidence": "3/3 terms matched in diff: add, widget, function",
            "checked_at": None,
        }
    ]


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

    with patch("lanegate.analyze._worktree_diff_text", return_value="unrelated diff content"):
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

    with patch("lanegate.analyze._worktree_diff_text", return_value="unrelated diff content"):
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
        "lanegate.analyze._worktree_diff_text",
        return_value="def add_widget_function(): pass",
    ):
        cmd_review("TICK-303", cfg, tmp_path, verdict="approved")

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-303.md")
    assert ticket["status"] == "in_review"
    statuses = {r["criterion"]: r["status"] for r in ticket["verification"]}
    assert statuses["Add a widget function"] == "verified"
    assert statuses["Full suite green."] == "verified"


def test_cmd_merge_blocks_on_stale_unresolved_verification(tmp_path, capsys):
    """Defense in depth: even with review_verdict=approved, cmd_merge refuses
    when the ticket's persisted verification records still show an
    unresolved criterion (e.g. hand-edited frontmatter between review and
    merge)."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-304"
    wt.mkdir()
    _write_ticket_with_body(
        tickets_dir,
        "TICK-304",
        "in_review",
        "Body.\n",
        worktree=str(wt),
        branch="tick-304",
        review_verdict="approved",
        verification=[{"criterion": "Add a widget function", "status": "unverified", "evidence": ""}],
    )
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    with pytest.raises(SystemExit) as exc_info:
        cmd_merge("TICK-304", cfg, tmp_path)

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "Add a widget function" in err

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-304.md")
    assert ticket["status"] == "in_review"


# ---------------------------------------------------------------------------
# TICK-284: reconciliation -- cmd_supersede, cmd_reopen guard
# ---------------------------------------------------------------------------


def test_cmd_supersede_closes_ticket_when_branch_reachable_from_main(tmp_path):
    """A ticket whose branch is already merged into main gets closed with
    replacement_commit evidence recorded, not left to hold its touches
    lock forever."""
    _init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "-b", "tick-500"], cwd=tmp_path, check=True)
    (tmp_path / "already_landed.txt").write_text("already landed\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "already landed"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "merge", "--no-ff", "tick-500", "-m", "merge tick-500"],
        cwd=tmp_path,
        check=True,
    )

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-500"
    wt.mkdir()

    _write_ticket(
        tickets_dir,
        "TICK-500",
        "in_progress",
        worktree=str(wt),
        branch="tick-500",
        touches=["already_landed.txt"],
    )
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    cmd_supersede("TICK-500", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-500.md")
    assert ticket["status"] == "closed"
    assert ticket.get("replacement_commit")
    assert ticket.get("worktree") is None


def test_cmd_supersede_blocks_when_no_evidence(tmp_path, capsys):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()

    _write_ticket(tickets_dir, "TICK-501", "in_progress", touches=["lanegate/novel.py"])
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    with pytest.raises(SystemExit) as exc_info:
        cmd_supersede("TICK-501", cfg, tmp_path)

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "no reconciliation evidence" in err

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-501.md")
    assert ticket["status"] == "in_progress"


def test_manual_supersede_retires_hibernated_ticket_and_cleans_up(tmp_path):
    """A human reason retires obsolete hibernated work without Git evidence."""
    _init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("init\n")
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-510"
    _write_ticket(
        tickets_dir,
        "TICK-510",
        "hibernated",
        worktree=str(wt),
        branch="tick-510",
        touches=["lanegate/obsolete.py"],
    )
    _commit_all(tmp_path)
    subprocess.run(
        ["git", "worktree", "add", "-b", "tick-510", str(wt)], cwd=tmp_path, check=True
    )

    marker = tmp_path / ".lanegate" / "TICK-510.pid"
    marker.parent.mkdir()
    marker.write_text("12345\n")
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg.update(
        {
            "tickets_dir": str(tickets_dir),
            "worktrees_dir": str(worktrees_dir),
            "commit_status_changes": True,
        }
    )

    reason = "A newer architecture covers this goal without a literal duplicate."
    cmd_supersede("TICK-510", cfg, tmp_path, reason=reason)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-510.md")
    assert ticket["status"] == "closed"
    assert ticket.get("worktree") is None
    assert reason in ticket["_body"]
    assert not wt.exists()
    assert not marker.exists()
    log = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "TICK-510 status → superseded" in log


def test_cmd_reopen_blocks_when_ticket_superseded(tmp_path, capsys):
    """cmd_reopen must not re-dispatch a ticket whose work already exists
    on main -- it should point at `lanegate supersede` instead."""
    _init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "-b", "tick-502"], cwd=tmp_path, check=True)
    (tmp_path / "already_landed2.txt").write_text("already landed\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "already landed"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "merge", "--no-ff", "tick-502", "-m", "merge tick-502"],
        cwd=tmp_path,
        check=True,
    )

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()

    _write_ticket(
        tickets_dir,
        "TICK-502",
        "needs_review",
        branch="tick-502",
        touches=["already_landed2.txt"],
    )
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    with pytest.raises(SystemExit) as exc_info:
        cmd_reopen("TICK-502", cfg, tmp_path)

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "superseded" in err
    assert "lanegate supersede TICK-502" in err

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-502.md")
    # Blocked: must not have advanced to open
    assert ticket["status"] == "needs_review"


def test_cmd_reopen_proceeds_normally_when_not_superseded(tmp_path):
    """Sanity check: the new reconciliation guard doesn't interfere with
    the ordinary failed -> open reopen path when there's no evidence of
    supersession."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()

    _write_ticket(tickets_dir, "TICK-503", "failed", touches=["lanegate/novel.py"])
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    cmd_reopen("TICK-503", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-503.md")
    assert ticket["status"] == "open"


def test_reopen_rebase_conflict_autofix(tmp_path):
    """Verify cmd_reopen automatically invokes rebase fix agent on rebase conflict."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-322"
    wt.mkdir()

    _write_ticket(tickets_dir, "TICK-322", "needs_review", worktree=str(wt))
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    with patch("lanegate.lifecycle._worktree_has_commits", return_value=True), \
         patch("lanegate.orchestrate._worktree_is_dirty", return_value=False), \
         patch("lanegate.orchestrate._run_rebase", return_value=("conflict", "detail")), \
         patch("lanegate.orchestrate.autofix.run_rebase_fix_agent", return_value=True) as mock_fix:
        cmd_reopen("TICK-322", cfg, tmp_path)

    mock_fix.assert_called_once()
    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-322.md")
    assert ticket["status"] == "code_complete"
