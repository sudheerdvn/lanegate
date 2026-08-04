"""Tests for worktree.py — protected-branch guard, case canonicalization."""

from unittest.mock import MagicMock, patch

import pytest

from lanegate.worktree import prune_worktrees, remove_worktree, worktree_path


def test_worktree_path_lowercase(tmp_path):
    """Dir name is always lowercase — prevents case-mismatch on macOS/Windows."""
    wt = worktree_path(tmp_path / "worktrees", "TICK-007")
    assert wt.name == "tick-007"


def test_worktree_path_already_lowercase(tmp_path):
    wt = worktree_path(tmp_path / "worktrees", "tick-007")
    assert wt.name == "tick-007"


def test_remove_worktree_protected_branch_raises(tmp_path):
    """Removing a worktree on a protected environment branch must be refused."""
    wt_dir = tmp_path / "worktrees" / "deploy"
    wt_dir.mkdir(parents=True)

    def mock_run(args, **kwargs):
        if args == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
            return MagicMock(returncode=0, stdout="deploy\n")
        return MagicMock(returncode=0, stdout="")

    with patch("lanegate.worktree.subprocess.run", side_effect=mock_run):
        with pytest.raises(PermissionError, match="protected environment branch"):
            remove_worktree(tmp_path, wt_dir, protected={"deploy", "staging"})


def test_remove_worktree_non_protected_succeeds(tmp_path):
    """Non-protected worktrees can be removed."""
    wt_dir = tmp_path / "worktrees" / "tick-007"
    wt_dir.mkdir(parents=True)

    calls = []

    def mock_run(args, **kwargs):
        calls.append(args)
        return MagicMock(returncode=0, stdout="tick-007\n")

    with patch("lanegate.worktree.subprocess.run", side_effect=mock_run):
        remove_worktree(tmp_path, wt_dir, protected={"deploy", "staging"})

    # Should have called worktree remove
    remove_calls = [c for c in calls if "worktree" in c and "remove" in c]
    assert len(remove_calls) == 1


def test_remove_nonexistent_worktree_is_noop(tmp_path):
    """Removing a worktree that doesn't exist is a no-op."""
    with patch("lanegate.worktree.subprocess.run") as mock_run:
        remove_worktree(tmp_path, tmp_path / "nonexistent", protected=set())
        mock_run.assert_not_called()


def test_prune_worktrees_no_protected_branches_runs_prune(tmp_path):
    """When there are no protected worktrees, prune should run."""
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir(parents=True)

    # Create a non-protected worktree
    wt_dir = worktrees_dir / "tick-007"
    wt_dir.mkdir()

    calls = []

    def mock_run(args, **kwargs):
        calls.append(args)
        return MagicMock(returncode=0, stdout="tick-007\n")

    with patch("lanegate.worktree.subprocess.run", side_effect=mock_run):
        prune_worktrees(tmp_path, protected={"deploy", "staging"}, worktrees_dir=worktrees_dir)

    # Should have called prune
    prune_calls = [c for c in calls if c == ["git", "worktree", "prune"]]
    assert len(prune_calls) == 1


def test_prune_worktrees_with_protected_branches_skips_prune(tmp_path):
    """When there are protected worktrees, prune should NOT run."""
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir(parents=True)

    # Create a protected worktree
    wt_dir = worktrees_dir / "deploy"
    wt_dir.mkdir()

    calls = []

    def mock_run(args, **kwargs):
        calls.append(args)
        # Return protected branch name for the worktree
        if args == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
            return MagicMock(returncode=0, stdout="deploy\n")
        return MagicMock(returncode=0, stdout="")

    with patch("lanegate.worktree.subprocess.run", side_effect=mock_run):
        prune_worktrees(tmp_path, protected={"deploy", "staging"}, worktrees_dir=worktrees_dir)

    # Should NOT have called prune
    prune_calls = [c for c in calls if c == ["git", "worktree", "prune"]]
    assert len(prune_calls) == 0
