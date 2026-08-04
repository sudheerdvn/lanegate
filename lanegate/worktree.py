"""
worktree.py — git worktree create/remove with protected-branch guard.

Protected branches (environment branches from config) are refused for remove/prune.
Worktree dir and branch name both use lowercase ticket ID to avoid case-mismatch on
case-insensitive filesystems (macOS, Windows).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from lanegate.config import resolve_trunk_branch


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, cwd=cwd)


def worktree_path(worktrees_dir: Path, ticket_id: str) -> Path:
    """Canonical worktree path — always lowercase to avoid case-mismatch bugs."""
    return worktrees_dir / ticket_id.lower()


def create_worktree(
    repo_root: Path,
    worktrees_dir: Path,
    ticket_id: str,
    branch: str,
    base: str | None = None,
) -> Path:
    """
    Create a worktree for ticket_id on branch. Both dir name and branch name are lowercase.
    Returns the worktree path.
    """
    base = base or resolve_trunk_branch({}, repo_root)
    path = worktree_path(worktrees_dir, ticket_id)
    if path.exists():
        return path

    # Try to create branch from base
    r = _run(["git", "worktree", "add", "-b", branch, str(path), base], repo_root)
    if r.returncode != 0:
        # Branch may already exist — try attaching without -b
        r2 = _run(["git", "worktree", "add", str(path), branch], repo_root)
        if r2.returncode != 0:
            raise RuntimeError(f"ERROR creating worktree:\n{r.stderr}\n{r2.stderr}")
    return path


def remove_worktree(
    repo_root: Path,
    wt_path: str | Path,
    protected: set[str],
) -> None:
    """
    Remove a worktree. Refuses if its branch is in the protected set.
    protected is the set of branch names from environments[*].branch.
    """
    path = Path(wt_path)
    if not path.exists():
        return

    # Determine the branch checked out in this worktree
    branch_check = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        cwd=path,
    )
    branch = branch_check.stdout.strip() if branch_check.returncode == 0 else ""

    if branch in protected:
        raise PermissionError(
            f"Refusing to remove worktree '{path}': branch '{branch}' is a protected environment branch. "
            f"Protected branches: {sorted(protected)}"
        )

    subprocess.run(
        ["git", "worktree", "remove", "--force", str(path)],
        cwd=repo_root,
        capture_output=True,
    )


def prune_worktrees(repo_root: Path, protected: set[str], worktrees_dir: Path) -> None:
    """
    Prune stale worktrees. Never prunes worktrees whose branch is in the protected set.
    """
    # Collect protected worktree paths before pruning
    protected_paths = set()
    if worktrees_dir.exists():
        for wt in worktrees_dir.iterdir():
            if not wt.is_dir():
                continue
            branch_check = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                cwd=wt,
            )
            branch = branch_check.stdout.strip() if branch_check.returncode == 0 else ""
            if branch in protected:
                protected_paths.add(wt)

    # Only prune if there are no protected worktrees (git worktree prune has no exclude option)
    if not protected_paths:
        subprocess.run(["git", "worktree", "prune"], cwd=repo_root, capture_output=True)
