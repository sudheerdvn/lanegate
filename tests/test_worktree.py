"""Tests for worktree.py — protected-branch guard, case canonicalization."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lanegate.worktree import _run, create_worktree, prune_worktrees, remove_worktree, worktree_path


def test_worktree_path_lowercase(tmp_path):
    """Dir name is always lowercase — prevents case-mismatch on macOS/Windows."""
    wt = worktree_path(tmp_path / "worktrees", "TICK-007")
    assert wt.name == "tick-007"


def test_worktree_path_already_lowercase(tmp_path):
    wt = worktree_path(tmp_path / "worktrees", "tick-007")
    assert wt.name == "tick-007"


def _init_repo(root):
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    (root / "README.md").write_text("hello")
    (root / ".gitignore").write_text(".lanegate/notes\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)


def test_create_worktree_symlinks_shared_notes(tmp_path):
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)

    wt_path = create_worktree(repo_root, repo_root / ".lanegate" / "worktrees", "TICK-010", "tick-010", base="main")

    notes_link = wt_path / ".lanegate" / "notes"
    assert notes_link.is_symlink()
    assert notes_link.resolve() == (repo_root / ".lanegate" / "notes").resolve()


def test_create_worktree_sets_format_signoff(tmp_path):
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    worktrees_dir = repo_root / ".lanegate" / "worktrees"

    with patch("lanegate.worktree._run", wraps=_run) as mock_run:
        wt_path = create_worktree(repo_root, worktrees_dir, "TICK-014", "tick-014", base="main")

    mock_run.assert_any_call(["git", "config", "format.signoff", "true"], wt_path)
    assert subprocess.run(
        ["git", "config", "--get", "format.signoff"],
        cwd=wt_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == "true"


def test_reused_worktree_links_shared_notes(tmp_path):
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    worktrees_dir = repo_root / ".lanegate" / "worktrees"
    wt_path = create_worktree(repo_root, worktrees_dir, "TICK-011", "tick-011", base="main")
    (wt_path / ".lanegate" / "notes").unlink()

    reused_path = create_worktree(repo_root, worktrees_dir, "TICK-011", "tick-011", base="main")

    assert reused_path == wt_path
    assert (reused_path / ".lanegate" / "notes").resolve() == (repo_root / ".lanegate" / "notes").resolve()


def test_reused_degraded_worktree_warns_about_private_notes(tmp_path):
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    worktrees_dir = repo_root / ".lanegate" / "worktrees"
    wt_path = create_worktree(repo_root, worktrees_dir, "TICK-015", "tick-015", base="main")
    notes = wt_path / ".lanegate" / "notes"
    notes.unlink()
    notes.mkdir()

    with pytest.warns(RuntimeWarning, match="using a worktree-private notes directory"):
        reused_path = create_worktree(repo_root, worktrees_dir, "TICK-015", "tick-015", base="main")

    assert reused_path == wt_path
    assert notes.is_dir()
    assert not notes.is_symlink()


def test_existing_notes_file_is_rejected(tmp_path):
    from lanegate.worktree import _ensure_notes_symlink

    control = tmp_path / "control"
    worktree = tmp_path / "worktree"
    control.mkdir()
    (worktree / ".lanegate").mkdir(parents=True)
    (worktree / ".lanegate" / "notes").write_text("not a directory")

    with pytest.raises(RuntimeError, match="not the required shared link"):
        _ensure_notes_symlink(control, worktree)


def test_notes_survive_worktree_removal(tmp_path):
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    worktrees_dir = repo_root / ".lanegate" / "worktrees"
    worktree_a = create_worktree(repo_root, worktrees_dir, "TICK-012", "tick-012", base="main")
    (worktree_a / ".lanegate" / "notes" / "global.md").write_text("keep this fact")

    remove_worktree(repo_root, worktree_a, protected=set())
    worktree_b = create_worktree(repo_root, worktrees_dir, "TICK-013", "tick-013", base="main")

    assert (repo_root / ".lanegate" / "notes" / "global.md").read_text() == "keep this fact"
    assert (worktree_b / ".lanegate" / "notes" / "global.md").read_text() == "keep this fact"


def test_symlink_failure_degrades_to_directory(tmp_path, monkeypatch):
    from lanegate.worktree import _ensure_notes_symlink

    control = tmp_path / "control"
    worktree = tmp_path / "worktree"
    control.mkdir()
    _init_repo(worktree)

    def fail_symlink(self, target, target_is_directory=False):
        raise OSError("symlinks unavailable")

    monkeypatch.setattr("pathlib.Path.symlink_to", fail_symlink)
    with pytest.warns(RuntimeWarning, match="using a worktree-private notes directory"):
        _ensure_notes_symlink(control, worktree)

    notes = worktree / ".lanegate" / "notes"
    assert notes.is_dir()
    assert not notes.is_symlink()


def test_ensure_notes_symlink_handles_existing_directory(tmp_path):
    from lanegate.worktree import _ensure_notes_symlink

    control = tmp_path / "control"
    worktree = tmp_path / "worktree"
    control.mkdir()
    (worktree / ".lanegate" / "notes").mkdir(parents=True)
    (worktree / ".lanegate" / "notes" / "keep.md").write_text("existing note")

    with pytest.warns(RuntimeWarning, match="using a worktree-private notes directory"):
        _ensure_notes_symlink(control, worktree)

    notes = worktree / ".lanegate" / "notes"
    assert notes.is_dir()
    assert not notes.is_symlink()
    assert (notes / "keep.md").read_text() == "existing note"


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


def test_create_worktree_symlinks_graphify_out(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_root, check=True)
    (repo_root / "README.md").write_text("hello")
    (repo_root / ".gitignore").write_text("graphify-out\n.lanegate/notes\n")
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_root, check=True)

    graphify_dir = repo_root / "graphify-out"
    graphify_dir.mkdir()

    worktrees_dir = repo_root / ".lanegate" / "worktrees"
    wt_path = create_worktree(repo_root, worktrees_dir, "TICK-001", "tick-001", base="main")

    symlink = wt_path / "graphify-out"
    assert symlink.is_symlink()
    assert symlink.resolve() == graphify_dir.resolve()


def test_create_worktree_skips_graphify_symlink_when_not_ignored(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_root, check=True)
    (repo_root / "README.md").write_text("hello")
    (repo_root / ".gitignore").write_text(".lanegate/notes\n")
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_root, check=True)

    graphify_dir = repo_root / "graphify-out"
    graphify_dir.mkdir()

    worktrees_dir = repo_root / ".lanegate" / "worktrees"
    wt_path = create_worktree(repo_root, worktrees_dir, "TICK-002", "tick-002", base="main")

    symlink = wt_path / "graphify-out"
    assert not symlink.exists()
    assert not symlink.is_symlink()


def test_create_worktree_handles_graphify_symlink_oserror(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_root, check=True)
    (repo_root / "README.md").write_text("hello")
    (repo_root / ".gitignore").write_text("graphify-out\n.lanegate/notes\n")
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_root, check=True)

    graphify_dir = repo_root / "graphify-out"
    graphify_dir.mkdir()

    original_symlink_to = Path.symlink_to

    def mock_symlink_to(self, target, target_is_directory=False):
        if self.name in {"graphify-out", "notes"}:
            raise OSError("[WinError 1314] A required privilege is not held by the client")
        return original_symlink_to(self, target, target_is_directory=target_is_directory)

    monkeypatch.setattr("pathlib.Path.symlink_to", mock_symlink_to)

    worktrees_dir = repo_root / ".lanegate" / "worktrees"
    with pytest.warns(RuntimeWarning, match="using a worktree-private notes directory"):
        wt_path = create_worktree(repo_root, worktrees_dir, "TICK-003", "tick-003", base="main")
    assert wt_path.exists()
    assert not (wt_path / "graphify-out").exists()
    notes = wt_path / ".lanegate" / "notes"
    assert notes.is_dir()
    assert not notes.is_symlink()


def test_create_worktree_preserves_unattached_branch_with_commits(tmp_path):
    """Recovery branches are never silently deleted or reused for a fresh dispatch."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_root, check=True)
    (repo_root / "README.md").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_root, check=True)

    # Pre-create an unattached recovery branch with work that must survive.
    subprocess.run(["git", "branch", "tick-099"], cwd=repo_root, check=True)
    subprocess.run(["git", "checkout", "tick-099"], cwd=repo_root, check=True)
    (repo_root / "recovery.py").write_text("preserve me\n")
    subprocess.run(["git", "add", "recovery.py"], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "recovery work"], cwd=repo_root, check=True)
    recovery_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(["git", "checkout", "main"], cwd=repo_root, check=True)

    worktrees_dir = repo_root / ".lanegate" / "worktrees"
    with pytest.raises(RuntimeError, match="Existing unattached branch 'tick-099' was preserved"):
        create_worktree(repo_root, worktrees_dir, "TICK-099", "tick-099", base="main")
    assert not worktree_path(worktrees_dir, "TICK-099").exists()
    assert subprocess.run(
        ["git", "rev-parse", "tick-099"], cwd=repo_root, check=True, capture_output=True, text=True
    ).stdout.strip() == recovery_head

    recovered = create_worktree(
        repo_root, worktrees_dir, "TICK-099", "tick-099", base="main", reuse_existing_branch=True
    )
    assert (recovered / "recovery.py").read_text() == "preserve me\n"


def test_create_worktree_skips_notes_symlink_when_not_ignored(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_root, check=True)
    (repo_root / "README.md").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_root, check=True)

    worktrees_dir = repo_root / ".lanegate" / "worktrees"
    with pytest.warns(RuntimeWarning, match="is not ignored by git"):
        wt_path = create_worktree(repo_root, worktrees_dir, "TICK-017", "tick-017", base="main")
    notes = wt_path / ".lanegate" / "notes"
    assert notes.is_dir()
    assert not notes.is_symlink()


def test_create_worktree_preserves_unattached_branch_behind_unregistered_directory(tmp_path):
    """An ordinary directory occupying the canonical path is not proof the branch is stale.

    Reproduces finding [1]: a recovery branch (based on main, so it passes
    ancestry) plus an unrelated, un-registered directory sitting at the
    canonical worktree path must not cause the branch to be treated as
    "released by replacing the stale worktree" and deleted. Only a
    confirmed git worktree with this branch actually checked out may be
    treated that way.
    """
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)

    # Recovery branch based on main (passes ancestry validation).
    subprocess.run(["git", "branch", "tick-999"], cwd=repo_root, check=True)
    subprocess.run(["git", "checkout", "tick-999"], cwd=repo_root, check=True)
    (repo_root / "recovery.py").write_text("preserve me\n")
    subprocess.run(["git", "add", "recovery.py"], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "recovery work"], cwd=repo_root, check=True)
    recovery_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(["git", "checkout", "main"], cwd=repo_root, check=True)

    # Ordinary, unregistered directory occupying the canonical path -- not a
    # git worktree at all, just a leftover/foreign directory.
    worktrees_dir = repo_root / ".lanegate" / "worktrees"
    stray = worktree_path(worktrees_dir, "TICK-999")
    stray.mkdir(parents=True)
    (stray / "unrelated.txt").write_text("not a worktree\n")

    with pytest.raises(RuntimeError, match="Existing unattached branch 'tick-999' was preserved"):
        create_worktree(repo_root, worktrees_dir, "TICK-999", "tick-999", base="main")

    assert not stray.exists()
    assert subprocess.run(
        ["git", "rev-parse", "refs/heads/tick-999"],
        cwd=repo_root, check=True, capture_output=True, text=True,
    ).stdout.strip() == recovery_head

    recovered = create_worktree(
        repo_root, worktrees_dir, "TICK-999", "tick-999", base="main", reuse_existing_branch=True
    )
    assert (recovered / "recovery.py").read_text() == "preserve me\n"


def test_recovery_refuses_stale_base_branch_after_invalid_worktree_cleanup(tmp_path):
    """A branch rejected by ancestry validation cannot be reattached on recovery.

    The worktree here is correctly attached to ``tick-101`` -- only its
    ancestry is bad -- so the refusal must happen *before* any removal
    (finding [1]): the live worktree and its contents stay exactly as they
    were, not force-removed and then rejected.
    """
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    worktrees_dir = repo_root / ".lanegate" / "worktrees"
    stale = create_worktree(repo_root, worktrees_dir, "TICK-101", "tick-101", base="main")

    subprocess.run(["git", "checkout", "--orphan", "unrelated"], cwd=stale, check=True)
    subprocess.run(["git", "rm", "-rf", "."], cwd=stale, check=True)
    (stale / "recovery.py").write_text("preserve stale recovery\n")
    subprocess.run(["git", "add", "recovery.py"], cwd=stale, check=True)
    subprocess.run(["git", "commit", "-m", "unrelated recovery"], cwd=stale, check=True)
    subprocess.run(["git", "branch", "-f", "tick-101", "HEAD"], cwd=stale, check=True)
    subprocess.run(["git", "checkout", "tick-101"], cwd=stale, check=True)
    stale_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=stale, check=True, capture_output=True, text=True
    ).stdout.strip()

    with pytest.raises(RuntimeError, match="shares no history with 'main'"):
        create_worktree(
            repo_root,
            worktrees_dir,
            "TICK-101",
            "tick-101",
            base="main",
            reuse_existing_branch=True,
        )

    assert worktree_path(worktrees_dir, "TICK-101").exists()
    assert (stale / "recovery.py").read_text() == "preserve stale recovery\n"
    assert subprocess.run(
        ["git", "rev-parse", "refs/heads/tick-101"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == stale_head


def test_recovery_refuses_stale_base_branch_with_no_worktree_present(tmp_path):
    """Ancestry validation must reject reuse even when no stale worktree exists.

    This exercises the second, independent ancestry check (no canonical
    worktree directory at all — the common post-hibernate shape, where the
    worktree was already cleaned up and only the recovery branch remains).
    The first-block check in test_recovery_refuses_stale_base_branch_after_
    invalid_worktree_cleanup only covers the case where a stale worktree
    directory is still present.
    """
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)

    # Create an unattached recovery branch whose history does not descend
    # from main (simulates a branch reset to unrelated/rejected history).
    subprocess.run(["git", "checkout", "--orphan", "tick-103"], cwd=repo_root, check=True)
    subprocess.run(["git", "rm", "-rf", "."], cwd=repo_root, check=True)
    (repo_root / "recovery.py").write_text("preserve stale recovery\n")
    subprocess.run(["git", "add", "recovery.py"], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "unrelated recovery"], cwd=repo_root, check=True)
    stale_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(["git", "checkout", "main"], cwd=repo_root, check=True)

    worktrees_dir = repo_root / ".lanegate" / "worktrees"
    assert not worktree_path(worktrees_dir, "TICK-103").exists()

    with pytest.raises(RuntimeError, match="shares no history with 'main'"):
        create_worktree(
            repo_root,
            worktrees_dir,
            "TICK-103",
            "tick-103",
            base="main",
            reuse_existing_branch=True,
        )

    assert not worktree_path(worktrees_dir, "TICK-103").exists()
    assert subprocess.run(
        ["git", "rev-parse", "refs/heads/tick-103"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == stale_head


def test_create_worktree_reattaches_after_trunk_advances(tmp_path):
    """Trunk gaining commits after the ticket branch was cut must not break resume.

    Regression test for the ancestry gate that required ``base`` (main's
    *current* tip) to be an ancestor of the ticket branch -- true only until
    the very next commit lands on main. Every ticket branch failed this as
    soon as any other ticket merged, force-removing the live, correctly
    attached worktree before raising "not based on 'main'". Verified via a
    real repro in review: a worktree for one ticket, one commit added to
    main, then a reattach exactly as ``cmd_start`` performs for a
    hibernated/needs_review ticket.
    """
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    worktrees_dir = repo_root / ".lanegate" / "worktrees"
    wt_path = create_worktree(repo_root, worktrees_dir, "TICK-500", "tick-500", base="main")
    (wt_path / "ticket-work.py").write_text("real ticket work\n")
    subprocess.run(["git", "add", "ticket-work.py"], cwd=wt_path, check=True)
    subprocess.run(["git", "commit", "-m", "ticket work"], cwd=wt_path, check=True)

    # Trunk advances independently of the ticket branch (e.g. another ticket
    # merged in the meantime).
    (repo_root / "other.py").write_text("unrelated main progress\n")
    subprocess.run(["git", "add", "other.py"], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "other ticket merged"], cwd=repo_root, check=True)

    reattached = create_worktree(
        repo_root, worktrees_dir, "TICK-500", "tick-500", base="main", reuse_existing_branch=True
    )

    assert reattached == wt_path
    assert (reattached / "ticket-work.py").read_text() == "real ticket work\n"


def test_create_worktree_reuse_existing_branch_sets_format_signoff(tmp_path):
    """The reuse_existing_branch reattach path (hibernate --reset recovery)
    must configure DCO sign-off same as a fresh worktree create -- it's a
    separate `git worktree add` call, not something inherited automatically.
    Regression test: this path returned early before reaching the signoff
    config, so every commit made after a hibernate-recovery resume lacked a
    Signed-off-by trailer while the ticket's own pre-hibernation commits (and
    every other ticket) had one."""
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    worktrees_dir = repo_root / ".lanegate" / "worktrees"
    wt_path = create_worktree(repo_root, worktrees_dir, "TICK-901", "tick-901", base="main")
    subprocess.run(["git", "worktree", "remove", "--force", str(wt_path)], cwd=repo_root, check=True)

    reattached = create_worktree(
        repo_root, worktrees_dir, "TICK-901", "tick-901", base="main", reuse_existing_branch=True
    )

    assert subprocess.run(
        ["git", "config", "--get", "format.signoff"],
        cwd=reattached,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == "true"


def test_create_worktree_ignores_same_named_tag_when_creating_ticket_branch(tmp_path):
    """A tag must not block creation or be reused as a detached ticket worktree."""
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    subprocess.run(["git", "tag", "tick-102"], cwd=repo_root, check=True)

    created = create_worktree(
        repo_root, repo_root / ".lanegate" / "worktrees", "TICK-102", "tick-102", base="main"
    )

    assert subprocess.run(
        ["git", "symbolic-ref", "--quiet", "HEAD"], cwd=created, check=True, capture_output=True, text=True
    ).stdout.strip() == "refs/heads/tick-102"


def test_create_worktree_preserves_detached_worktree_despite_same_named_tag(tmp_path):
    """A tag cannot turn a detached worktree into disposable ticket state.

    Before ``_local_branch_exists`` was scoped to ``refs/heads``, ``git
    rev-parse --verify <name>`` also matched a tag. A same-named tag remains
    irrelevant when refusing a detached canonical worktree.
    """
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    worktrees_dir = repo_root / ".lanegate" / "worktrees"

    # Stale/invalid canonical worktree: checked out on an unrelated branch.
    stale = create_worktree(repo_root, worktrees_dir, "TICK-104", "tick-104", base="main")
    subprocess.run(["git", "checkout", "--detach"], cwd=stale, check=True)

    # Same-named tag pointing at history that does not descend from main.
    subprocess.run(["git", "checkout", "--orphan", "tick-104-unrelated"], cwd=repo_root, check=True)
    subprocess.run(["git", "rm", "-rf", "."], cwd=repo_root, check=True)
    (repo_root / "unrelated.py").write_text("not based on main\n")
    subprocess.run(["git", "add", "unrelated.py"], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "unrelated"], cwd=repo_root, check=True)
    subprocess.run(["git", "tag", "tick-104"], cwd=repo_root, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo_root, check=True)

    with pytest.raises(RuntimeError, match="detached HEAD"):
        create_worktree(repo_root, worktrees_dir, "TICK-104", "tick-104", base="main")

    assert stale.exists()
    # The tag is untouched, still pointing at the unrelated history.
    assert subprocess.run(
        ["git", "rev-list", "-1", "refs/tags/tick-104"],
        cwd=repo_root, check=True, capture_output=True, text=True,
    ).stdout.strip() != subprocess.run(
        ["git", "rev-list", "-1", "refs/heads/tick-104"],
        cwd=repo_root, check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_create_worktree_preserves_clean_detached_checkout_at_ticket_tip(tmp_path):
    """Matching HEAD is not proof a detached checkout belongs to fresh dispatch."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_root, check=True)
    (repo_root / "README.md").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_root, check=True)

    worktrees_dir = repo_root / ".lanegate" / "worktrees"
    wt_path = create_worktree(repo_root, worktrees_dir, "TICK-100", "tick-100", base="main")
    assert wt_path.exists()

    # Switch HEAD in worktree to simulate an invalid/corrupt worktree state (e.g. checked out on wrong branch)
    subprocess.run(["git", "checkout", "--detach"], cwd=wt_path, check=True)

    with pytest.raises(RuntimeError, match="detached HEAD"):
        create_worktree(repo_root, worktrees_dir, "TICK-100", "tick-100", base="main")

    assert subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=wt_path,
        check=True, capture_output=True, text=True,
    ).stdout.strip() == "HEAD"


def test_create_worktree_preserves_registered_canonical_path_on_wrong_branch(tmp_path):
    """A canonical path does not authorize deleting another branch's worktree."""
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    worktrees_dir = repo_root / ".lanegate" / "worktrees"

    subprocess.run(["git", "branch", "tick-999"], cwd=repo_root, check=True)
    subprocess.run(["git", "checkout", "tick-999"], cwd=repo_root, check=True)
    (repo_root / "recovery.py").write_text("preserve me\n")
    subprocess.run(["git", "add", "recovery.py"], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "recovery work"], cwd=repo_root, check=True)
    recovery_head = subprocess.run(
        ["git", "rev-parse", "refs/heads/tick-999"],
        cwd=repo_root, check=True, capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(["git", "checkout", "main"], cwd=repo_root, check=True)

    canonical = worktree_path(worktrees_dir, "TICK-999")
    subprocess.run(
        ["git", "worktree", "add", "-b", "wrong-branch", str(canonical), "main"],
        cwd=repo_root,
        check=True,
    )

    # The foreign checkout is detached at the recovery branch tip. Matching
    # HEAD must not let a fresh dispatch take it over.
    subprocess.run(["git", "checkout", "--detach", "refs/heads/tick-999"], cwd=canonical, check=True)

    with pytest.raises(RuntimeError, match="registered on detached HEAD"):
        create_worktree(repo_root, worktrees_dir, "TICK-999", "tick-999", base="main")

    assert canonical.exists()
    assert (canonical / "recovery.py").read_text() == "preserve me\n"
    assert subprocess.run(
        ["git", "rev-parse", "refs/heads/tick-999"],
        cwd=repo_root, check=True, capture_output=True, text=True,
    ).stdout.strip() == recovery_head


def test_create_worktree_refuses_to_replace_protected_stale_worktree(tmp_path):
    """A configured environment branch is never removed via a ticket path collision."""
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    worktrees_dir = repo_root / ".lanegate" / "worktrees"
    protected_path = worktree_path(worktrees_dir, "TICK-777")
    protected_path.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", "production", str(protected_path), "main"],
        cwd=repo_root,
        check=True,
    )

    with pytest.raises(PermissionError, match="protected environment branch"):
        create_worktree(
            repo_root,
            worktrees_dir,
            "TICK-777",
            "tick-777",
            base="main",
            protected={"production"},
        )

    assert protected_path.exists()
    assert subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=protected_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == "production"
