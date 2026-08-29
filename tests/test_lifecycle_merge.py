"""Tests for lifecycle merge, validation, and completion commands."""

from __future__ import annotations

import json
import shutil
import sys
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lanegate.lifecycle import cmd_done, cmd_merge, cmd_validate
from lanegate.ticket import parse_ticket, write_ticket
from lanegate.worktree import worktree_path
from tests._helpers.lifecycle import (
    commit_all as _commit_all,
    default_cfg as _default_cfg,
    init_git_repo as _init_git_repo,
    is_iso_utc as _is_iso_utc,
    tracked_path_is_clean as _tracked_path_is_clean,
    write_ticket as _write_ticket,
    write_ticket_with_body as _write_ticket_with_body,
)

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
    import lanegate.safeguards as safeguards

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
        process = MagicMock(returncode=0, stdout="", stderr="")
        process.communicate.return_value = ("", "")
        return process

    guard_process_target = (
        "lanegate.safeguards._Popen"
        if hasattr(safeguards, "_Popen")
        else "lanegate.safeguards.subprocess.run"
    )
    with patch(guard_process_target, side_effect=mock_guard):
        cmd_validate("TICK-116", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-116.md")
    assert ticket["status"] == "validated"
    guard_calls = [call for call in calls if call[0][:3] == [sys.executable, "-m", "pytest"]]
    assert guard_calls == [([sys.executable, "-m", "pytest", "-q", "--tb=short"], tmp_path)]


def test_validate_blocks_on_failing_post_merge_guard(tmp_path, capsys):
    import lanegate.safeguards as safeguards

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
        process = MagicMock(returncode=1, stdout="", stderr="")
        process.communicate.return_value = ("", "")
        return process

    guard_process_target = (
        "lanegate.safeguards._Popen"
        if hasattr(safeguards, "_Popen")
        else "lanegate.safeguards.subprocess.run"
    )
    with patch(guard_process_target, side_effect=mock_guard):
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


def test_merge_metadata_conflict_auto_reconciles_and_finalizes(tmp_path, capsys):
    """A conflict limited to the ticket's own metadata is an expected
    lifecycle race, so merge resolves it without requiring a second command."""
    cfg = _merge_cfg(tmp_path, "in_review", review_verdict="approved", branch="tick-001")
    tickets_dir = tmp_path / "tickets"
    conflict_path = f"{tickets_dir}/TICK-001.md"

    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    merge_head = git_dir / "MERGE_HEAD"
    merge_head.write_text("deadbeef\n")

    commit_calls = []

    def mock_run(args, **kwargs):
        if "merge" in args and "--no-ff" in args:
            return MagicMock(
                returncode=1,
                stdout="CONFLICT (content): Merge conflict in tickets/TICK-001.md",
                stderr="Automatic merge failed",
            )
        if "diff" in args and "--diff-filter=U" in args:
            return MagicMock(returncode=0, stdout=f"{conflict_path}\n", stderr="")
        if "rev-parse" in args and "--verify" in args:
            return MagicMock(returncode=1, stdout="", stderr="")
        if "commit" in args and "--no-edit" in args:
            commit_calls.append(list(args))
            if merge_head.exists():
                merge_head.unlink()
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        cmd_merge("TICK-001", cfg, tmp_path)

    ticket = parse_ticket(tickets_dir / "TICK-001.md")
    assert ticket["status"] == "merged"
    assert commit_calls, "git commit --no-edit must complete the reconciled merge"
    out = capsys.readouterr().out
    assert "merge integrated; ticket status finalized" in out
    assert "CONFLICT (content): Merge conflict in tickets/TICK-001.md" not in out


def test_merge_reconcile_flag_remains_compatible_for_metadata_conflicts(tmp_path, capsys):
    """The former --reconcile flag is accepted, but is no longer required."""
    cfg = _merge_cfg(tmp_path, "in_review", review_verdict="approved", branch="tick-001")
    tickets_dir = tmp_path / "tickets"
    conflict_path = f"{tickets_dir}/TICK-001.md"

    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    merge_head = git_dir / "MERGE_HEAD"
    merge_head.write_text("deadbeef\n")

    commit_calls = []
    abort_called = []

    def mock_run(args, **kwargs):
        if "merge" in args and "--no-ff" in args:
            return MagicMock(
                returncode=1, stdout="", stderr="CONFLICT (content): Merge conflict in TICK-001.md"
            )
        if "merge" in args and "--abort" in args:
            abort_called.append(True)
            if merge_head.exists():
                merge_head.unlink()
            return MagicMock(returncode=0, stdout="", stderr="")
        if "diff" in args and "--diff-filter=U" in args:
            return MagicMock(returncode=0, stdout=f"{conflict_path}\n", stderr="")
        if "rev-parse" in args and "--verify" in args:
            return MagicMock(returncode=1, stdout="", stderr="")
        if "commit" in args and "--no-edit" in args:
            commit_calls.append(list(args))
            if merge_head.exists():
                merge_head.unlink()
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run),
        patch("lanegate.reconciliation.resolve_metadata_conflict") as mock_resolve,
    ):
        cmd_merge("TICK-001", cfg, tmp_path, reconcile=True)

    mock_resolve.assert_called_once_with(tmp_path, conflict_path)
    assert commit_calls, "git commit --no-edit must complete the reconciled merge"
    assert "-s" in commit_calls[0], "the reconciled merge commit must carry DCO sign-off"
    assert not abort_called, "merge --abort must not run when reconciliation succeeds"

    ticket = parse_ticket(tickets_dir / "TICK-001.md")
    assert ticket["status"] == "merged"

    out = capsys.readouterr().out
    assert "merge integrated; ticket status finalized" in out


def test_merge_source_conflict_reconcile_flag_still_blocks(tmp_path, capsys):
    """A genuine source-code conflict is never auto-resolved, even with
    --reconcile passed."""
    cfg = _merge_cfg(tmp_path, "in_review", review_verdict="approved", branch="tick-001")

    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    merge_head = git_dir / "MERGE_HEAD"
    merge_head.write_text("deadbeef\n")

    abort_called = []

    def mock_run(args, **kwargs):
        if "merge" in args and "--no-ff" in args:
            return MagicMock(
                returncode=1,
                stdout="CONFLICT (content): Merge conflict in lanegate/foo.py",
                stderr="Automatic merge failed",
            )
        if "merge" in args and "--abort" in args:
            abort_called.append(True)
            if merge_head.exists():
                merge_head.unlink()
            return MagicMock(returncode=0, stdout="", stderr="")
        if "diff" in args and "--diff-filter=U" in args:
            return MagicMock(returncode=0, stdout="lanegate/foo.py\n", stderr="")
        if "rev-parse" in args and "--verify" in args:
            return MagicMock(returncode=1, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    from lanegate.lifecycle import MergeFailedError

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        with pytest.raises(MergeFailedError) as exc_info:
            cmd_merge("TICK-001", cfg, tmp_path, reconcile=True)

    assert abort_called, "git merge --abort must still be called for a real source conflict"
    assert "CONFLICT (content): Merge conflict in lanegate/foo.py" in str(exc_info.value)

    ticket = parse_ticket(tmp_path / "tickets" / "TICK-001.md")
    assert ticket["status"] == "in_review"


def test_merge_already_integrated_skips_second_merge(tmp_path, capsys):
    """When the ticket branch is already an ancestor of trunk (interrupted
    merge recovery), `git merge --no-ff` is never invoked a second time, and
    the ticket is finalized straight through."""
    cfg = _merge_cfg(tmp_path, "in_review", review_verdict="approved", branch="tick-001")

    merge_calls = []

    def mock_run(args, **kwargs):
        if "merge" in args and "--no-ff" in args:
            merge_calls.append(list(args))
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run),
        patch("lanegate.reconciliation.branch_reachable_from_main", return_value="deadbeef" * 5),
    ):
        cmd_merge("TICK-001", cfg, tmp_path)

    assert not merge_calls, "git merge --no-ff must not run when the branch is already integrated"

    ticket = parse_ticket(tmp_path / "tickets" / "TICK-001.md")
    assert ticket["status"] == "merged"

    out = capsys.readouterr().out
    assert "branch already integrated" in out or "ticket status finalized" in out


def test_merge_already_integrated_failed_verify_preserves_merged(tmp_path, capsys):
    """When a ticket branch is already integrated into main and post_merge_verify fails,
    no git reset --hard is run, ticket status remains 'merged', and post_merge_diagnostic is recorded."""
    from lanegate.lifecycle import MergeFailedError

    cfg = _merge_cfg(tmp_path, "in_review", review_verdict="approved", branch="tick-001")

    reset_calls = []

    def mock_run(args, **kwargs):
        if "reset" in args and "--hard" in args:
            reset_calls.append(list(args))
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="abc123\n", stderr="")

    def mock_run_safeguards(stage, ticket, cfg, wt, **kwargs):
        if kwargs.get("label") == "post_merge_verify":
            return False, "test suite failed"
        return True, None

    with (
        patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run),
        patch("lanegate.reconciliation.branch_reachable_from_main", return_value="deadbeef" * 5),
        patch("lanegate.lifecycle.merge.run_safeguards", side_effect=mock_run_safeguards),
    ):
        with pytest.raises(MergeFailedError):
            cmd_merge("TICK-001", cfg, tmp_path)

    assert not reset_calls, "git reset --hard must not run when branch was already integrated"

    ticket = parse_ticket(tmp_path / "tickets" / "TICK-001.md")
    assert ticket["status"] == "merged"
    assert "post_merge_diagnostic" in ticket
    assert "test suite failed" in ticket["post_merge_diagnostic"]


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
    assert "-s" in calls[commit_idx]


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


def test_merge_auto_logs_real_executor_from_step_costs(tmp_path):
    """cmd_merge's analytics row must reflect step_costs' real per-dispatch
    executor, not the static ticket/cfg default -- reverting the
    get_ticket_executor() call at the merge site leaves this the only test
    that fails (TICK-549 review round 3 finding: this write-time half of
    the fix had no coverage)."""
    from lanegate.context_log import _get_project_id, _load_entries_from_db, log_step_cost

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
    # cfg's own default executor is "claude" (see _default_cfg) -- step_costs
    # must win over it.
    assert cfg.get("executor", "claude") != "claude-b"

    db = tmp_path / "test_analytics.db"
    project = _get_project_id(tmp_path)
    log_step_cost(db, project, "TICK-001", "implement", executor="claude-b", cost_usd=0.05)

    def mock_run(args, **kwargs):
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run),
        patch("lanegate.context_log._get_default_db_path", return_value=db),
    ):
        cmd_merge("TICK-001", cfg, tmp_path)

    entries = _load_entries_from_db(db, project=project)
    assert len(entries) == 1
    assert entries[0]["executor"] == "claude-b"


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
        with patch("lanegate.lifecycle.merge._auto_promote_environments", side_effect=mock_auto_promote) as mock_ap:
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
        with patch("lanegate.lifecycle.merge._auto_promote_environments", side_effect=failing_auto_promote):
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
        with patch("lanegate.lifecycle.merge._auto_promote_environments", side_effect=spy_auto_promote):
            cmd_merge("TICK-001", cfg, tmp_path)

    assert not promotion_called, "Expected no promotions for non-matching from branch"


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


def test_merge_deletes_local_branch_when_cleanup_enabled(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-704a"
    wt.mkdir()

    _write_ticket(
        tickets_dir,
        "TICK-704",
        "in_review",
        worktree=str(wt),
        branch="tick-704a",
        review_verdict="approved",
    )

    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)
    cfg["cleanup_branch_on_merge"] = True

    deleted_branches = []

    def mock_run(args, **kwargs):
        if "branch" in args and "-D" in args:
            deleted_branches.append(args)
            return MagicMock(returncode=0, stdout="", stderr="")
        if "worktree" in args and "remove" in args:
            if wt.exists():
                wt.rmdir()
            return MagicMock(returncode=0)
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("lanegate.lifecycle.merge.subprocess.run", side_effect=mock_run):
        with patch("lanegate.worktree.subprocess.run", side_effect=mock_run):
            cmd_merge("TICK-704", cfg, tmp_path)

    assert any(cmd == ["git", "branch", "-D", "--", "tick-704a"] for cmd in deleted_branches)


def test_merge_skips_branch_deletion_when_cleanup_disabled(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-704b"
    wt.mkdir()

    _write_ticket(
        tickets_dir,
        "TICK-704",
        "in_review",
        worktree=str(wt),
        branch="tick-704b",
        review_verdict="approved",
    )

    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)
    cfg["cleanup_branch_on_merge"] = False

    deleted_branches = []

    def mock_run(args, **kwargs):
        if "branch" in args and "-D" in args:
            deleted_branches.append(args)
            return MagicMock(returncode=0, stdout="", stderr="")
        if "worktree" in args and "remove" in args:
            if wt.exists():
                wt.rmdir()
            return MagicMock(returncode=0)
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("lanegate.lifecycle.merge.subprocess.run", side_effect=mock_run):
        with patch("lanegate.worktree.subprocess.run", side_effect=mock_run):
            cmd_merge("TICK-704", cfg, tmp_path)

    assert len(deleted_branches) == 0


def test_merge_deletes_remote_branch_when_pushed(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-704c"
    wt.mkdir()

    _write_ticket(
        tickets_dir,
        "TICK-704",
        "in_review",
        worktree=str(wt),
        branch="tick-704c",
        review_verdict="approved",
    )

    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)
    cfg["cleanup_branch_on_merge"] = True
    cfg["github_pr"] = True

    pushed_deletes = []

    def mock_run(args, **kwargs):
        if "push" in args and "--delete" in args:
            pushed_deletes.append(args)
            return MagicMock(returncode=0, stdout="", stderr="")
        if "worktree" in args and "remove" in args:
            if wt.exists():
                wt.rmdir()
            return MagicMock(returncode=0)
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("lanegate.lifecycle.merge.subprocess.run", side_effect=mock_run):
        with patch("lanegate.worktree.subprocess.run", side_effect=mock_run):
            cmd_merge("TICK-704", cfg, tmp_path)

    assert any(
        cmd == ["git", "push", "origin", "--delete", "--", "tick-704c"] for cmd in pushed_deletes
    )


def test_merge_handles_remote_branch_already_deleted(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-704d"
    wt.mkdir()

    _write_ticket(
        tickets_dir,
        "TICK-704",
        "in_review",
        worktree=str(wt),
        branch="tick-704d",
        review_verdict="approved",
    )

    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)
    cfg["cleanup_branch_on_merge"] = True
    cfg["github_pr"] = True

    def mock_run(args, **kwargs):
        if "push" in args and "--delete" in args:
            return MagicMock(returncode=1, stdout="", stderr="error: remote ref does not exist")
        if "worktree" in args and "remove" in args:
            if wt.exists():
                wt.rmdir()
            return MagicMock(returncode=0)
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("lanegate.lifecycle.merge.subprocess.run", side_effect=mock_run):
        with patch("lanegate.worktree.subprocess.run", side_effect=mock_run):
            cmd_merge("TICK-704", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-704.md")
    assert ticket["status"] == "merged"


def test_merge_warns_on_genuine_local_branch_delete_failure(tmp_path, capsys):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-704e"
    wt.mkdir()

    _write_ticket(
        tickets_dir,
        "TICK-704",
        "in_review",
        worktree=str(wt),
        branch="tick-704e",
        review_verdict="approved",
    )

    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)
    cfg["cleanup_branch_on_merge"] = True

    def mock_run(args, **kwargs):
        if "branch" in args and "-D" in args:
            return MagicMock(
                returncode=1,
                stdout="",
                stderr="error: Cannot delete branch 'tick-704e' checked out at '/other/worktree'",
            )
        if "worktree" in args and "remove" in args:
            if wt.exists():
                wt.rmdir()
            return MagicMock(returncode=0)
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("lanegate.lifecycle.merge.subprocess.run", side_effect=mock_run):
        with patch("lanegate.worktree.subprocess.run", side_effect=mock_run):
            cmd_merge("TICK-704", cfg, tmp_path)

    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "tick-704e" in captured.err

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-704.md")
    assert ticket["status"] == "merged"
