"""Tests for companion.py — companion repo branch create/merge.

Regression coverage for TICK-125: companion_branch_merge must not report a
successful merge when the checkout/merge step itself failed.

Regression coverage for F40 (TICK-229): companion operations must never
mutate the user's live companion checkout. Both branch create and merge use
isolated git worktrees; merge additionally never checks out `main` in
companion_root (not even transiently) since main may already be checked out
live there — see companion.py's companion_branch_merge docstring.
"""

import subprocess

import pytest

from lanegate.companion import CompanionMergeResult, companion_branch_create, companion_branch_merge


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=check
    )


def _init_repo(path):
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    (path / "shared.txt").write_text("A\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "init")
    return path


def _rev(repo, ref):
    return _git(repo, "rev-parse", ref).stdout.strip()


@pytest.fixture
def companion_repo(tmp_path):
    return _init_repo(tmp_path / "companion")


def test_companion_branch_merge_advances_main(companion_repo, capsys):
    companion_branch_create(companion_repo, str(companion_repo), "tick-1", "TICK-1")
    # Access the worktree to make the changes
    wt_path = companion_repo / ".worktrees" / "tick-1"
    (wt_path / "shared.txt").write_text("B\n")
    _git(wt_path, "commit", "-am", "feature change")

    main_before = _rev(companion_repo, "main")
    result = companion_branch_merge(companion_repo, str(companion_repo), "tick-1", "TICK-1", "Test")

    assert result == CompanionMergeResult.MERGED
    assert _rev(companion_repo, "main") != main_before
    assert _git(
        companion_repo, "merge-base", "--is-ancestor", "tick-1", "main", check=False
    ).returncode == 0
    assert "merged tick-1 → main" in capsys.readouterr().out


def test_companion_branch_merge_does_not_touch_live_checkout_on_different_branch(
    companion_repo, capsys
):
    """F40: merge must succeed and never touch companion_root's live working
    directory, even when the user is mid-edit on an unrelated branch with
    uncommitted changes. Before the isolated-worktree fix, this exact scenario
    made `git checkout main` fail (TICK-125's regression), or — worse, if the
    uncommitted change didn't conflict — silently discarded the user's
    in-progress edit by force-switching their live tree to main."""
    companion_branch_create(companion_repo, str(companion_repo), "tick-1", "TICK-1")
    wt_path = companion_repo / ".worktrees" / "tick-1"
    (wt_path / "shared.txt").write_text("B\n")
    _git(wt_path, "commit", "-am", "feature change")

    # Put companion_repo's live checkout on an unrelated branch with a
    # divergent committed change, plus an uncommitted edit on top.
    _git(companion_repo, "checkout", "-b", "other")
    (companion_repo / "shared.txt").write_text("D\n")
    _git(companion_repo, "commit", "-am", "divergent change on other")
    (companion_repo / "shared.txt").write_text("uncommitted-in-progress-edit\n")

    main_before = _rev(companion_repo, "main")
    result = companion_branch_merge(companion_repo, str(companion_repo), "tick-1", "TICK-1", "Test")

    assert result == CompanionMergeResult.MERGED
    assert _rev(companion_repo, "main") != main_before
    assert _git(
        companion_repo, "merge-base", "--is-ancestor", "tick-1", "main", check=False
    ).returncode == 0

    # The user's live checkout is completely untouched: still on "other",
    # still carrying the uncommitted edit.
    assert _git(companion_repo, "branch", "--show-current").stdout.strip() == "other"
    assert (companion_repo / "shared.txt").read_text() == "uncommitted-in-progress-edit\n"
    status = _git(companion_repo, "status", "--porcelain").stdout
    assert "shared.txt" in status  # still shows as locally modified, untouched by us

    captured = capsys.readouterr()
    assert "merged tick-1 → main" in captured.out


def test_companion_branch_merge_warns_when_branch_missing(companion_repo, capsys):
    result = companion_branch_merge(
        companion_repo, str(companion_repo), "no-such-branch", "TICK-1", "Test"
    )
    assert result == CompanionMergeResult.SKIPPED_NO_BRANCH
    assert "not found" in capsys.readouterr().err


def test_companion_branch_merge_returns_failed_merge_on_conflict(companion_repo, capsys):
    """A real merge conflict (checkout succeeds, `git merge` itself fails) must
    also be reported as a failure, distinct from a failed checkout."""
    companion_branch_create(companion_repo, str(companion_repo), "tick-1", "TICK-1")
    wt_path = companion_repo / ".worktrees" / "tick-1"
    (wt_path / "shared.txt").write_text("B\n")
    _git(wt_path, "commit", "-am", "feature change on branch")

    _git(companion_repo, "checkout", "main")
    (companion_repo / "shared.txt").write_text("C\n")
    _git(companion_repo, "commit", "-am", "conflicting change on main")

    result = companion_branch_merge(companion_repo, str(companion_repo), "tick-1", "TICK-1", "Test")

    assert result == CompanionMergeResult.FAILED_MERGE
    captured = capsys.readouterr()
    assert "merged tick-1" not in captured.out
    assert "[WARN]" in captured.err
    assert "merge failed" in captured.err

    # Clean up the conflicted merge state so pytest's tmp_path teardown doesn't
    # trip over a dirty index.
    _git(companion_repo, "merge", "--abort", check=False)
