"""
companion.py — companion repo branch create/merge with isolated worktrees.

Companion repo paths in ticket frontmatter are repo-relative (e.g. "../dashboard")
or absolute. They are resolved relative to repo_root at call time.

Companion operations use git worktrees to provide isolation: each ticket gets a
separate worktree within the companion repo, preventing parallel tickets from
interfering with each other's working directories. The worktree is created on
branch create and removed after merge.
"""

from __future__ import annotations

import subprocess
import sys
from enum import Enum
from pathlib import Path

from lanegate.config import load_config, resolve_trunk_branch

class CompanionMergeResult(Enum):
    """Outcome of a single companion_branch_merge() call.

    MERGED and SKIPPED_NO_BRANCH are non-blocking (nothing to merge, or the
    merge happened). FAILED_CHECKOUT and FAILED_MERGE are real blockers —
    callers must not treat them the same as success.
    """

    MERGED = "merged"
    SKIPPED_NO_BRANCH = "skipped_no_branch"
    FAILED_CHECKOUT = "failed_checkout"
    FAILED_MERGE = "failed_merge"


def _resolve_companion(repo_root: Path, companion_path: str) -> Path:
    p = Path(companion_path)
    if p.is_absolute():
        return p
    return (repo_root / p).resolve()


def companion_worktree_path(companion_root: Path, ticket_id: str) -> Path:
    """Canonical companion worktree path — always lowercase to match main worktree naming."""
    return companion_root / ".worktrees" / ticket_id.lower()


def companion_branch_create(
    repo_root: Path, companion_path: str, branch: str, ticket_id: str, base: str | None = None
) -> None:
    """Create an isolated worktree for the companion repo and check out the branch.

    Creates a git worktree at companion_root/.worktrees/<ticket_id> to isolate
    this ticket's work from the user's live companion working directory and from
    other parallel tickets.
    """
    companion_root = _resolve_companion(repo_root, companion_path)
    base = base or resolve_trunk_branch(load_config(companion_root), companion_root)
    wt_path = companion_worktree_path(companion_root, ticket_id)

    if wt_path.exists():
        # Worktree already exists; try to check out the branch in it
        r = subprocess.run(
            ["git", "checkout", branch],
            capture_output=True,
            text=True, encoding="utf-8",
            cwd=wt_path,
        )
        if r.returncode != 0:
            print(
                f"  [WARN] companion {companion_path}: could not checkout branch {branch}",
                file=sys.stderr,
            )
        else:
            print(f"  companion {companion_path}: checked out existing branch {branch}")
        return

    # Create the worktree with the branch
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(wt_path), base],
        capture_output=True,
        text=True, encoding="utf-8",
        cwd=companion_root,
    )
    if r.returncode != 0:
        # Branch may already exist — try attaching without -b
        r2 = subprocess.run(
            ["git", "worktree", "add", str(wt_path), branch],
            capture_output=True,
            text=True, encoding="utf-8",
            cwd=companion_root,
        )
        if r2.returncode != 0:
            print(
                f"  [WARN] companion {companion_path}: could not create/checkout branch {branch}:\n{r.stderr}",
                file=sys.stderr,
            )
        else:
            print(f"  companion {companion_path}: checked out existing branch {branch}")
    else:
        print(f"  companion {companion_path}: created branch {branch}")


def _merge_worktree_path(companion_root: Path, ticket_id: str) -> Path:
    return companion_root / ".worktrees" / f"_merge-{ticket_id.lower()}"


def companion_branch_merge(
    repo_root: Path,
    companion_path: str,
    branch: str,
    ticket_id: str,
    title: str,
    base: str | None = None,
) -> CompanionMergeResult:
    """Merge a companion branch into the resolved trunk without touching the user's live
    companion checkout.

    `main` may already be checked out live in companion_root (very plausibly —
    it's usually the default branch), so we cannot `git checkout main` there:
    git refuses to check out a branch that's checked out in another worktree,
    and even when it wouldn't refuse, force-checking out the user's live tree
    mid-merge is exactly the destructive behavior this ticket exists to remove.
    Instead: merge in a scratch worktree detached at main's current tip, then
    move the `main` ref with `git branch -f` (a plumbing ref update — it never
    touches companion_root's working directory or index, so any uncommitted
    work the user has there is left alone, even if they happen to be on main).
    """
    companion_root = _resolve_companion(repo_root, companion_path)
    base = base or resolve_trunk_branch(load_config(companion_root), companion_root)
    wt_path = companion_worktree_path(companion_root, ticket_id)
    merge_wt_path = _merge_worktree_path(companion_root, ticket_id)

    check = subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        capture_output=True,
        text=True, encoding="utf-8",
        cwd=companion_root,
    )
    if check.returncode != 0:
        print(
            f"  [WARN] companion {companion_path}: branch {branch} not found — skipping merge",
            file=sys.stderr,
        )
        return CompanionMergeResult.SKIPPED_NO_BRANCH

    # Clean up any stale scratch worktree from a prior crashed run before reusing the path.
    if merge_wt_path.exists():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(merge_wt_path)],
            cwd=companion_root,
            capture_output=True,
        )

    merge_wt_path.parent.mkdir(parents=True, exist_ok=True)
    co = subprocess.run(
        ["git", "worktree", "add", "--detach", str(merge_wt_path), base],
        capture_output=True,
        text=True, encoding="utf-8",
        cwd=companion_root,
    )
    if co.returncode != 0:
        print(
            f"  [WARN] companion {companion_path}: could not create scratch worktree for "
            f"merge — merge skipped:\n{co.stderr}",
            file=sys.stderr,
        )
        return CompanionMergeResult.FAILED_CHECKOUT

    try:
        r = subprocess.run(
            ["git", "merge", "--no-ff", branch, "-m", f"Merge {ticket_id}: {title}"],
            capture_output=True,
            text=True, encoding="utf-8",
            cwd=merge_wt_path,
        )
        if r.returncode != 0:
            print(
                f"  [WARN] companion {companion_path}: merge failed:\n{r.stderr}",
                file=sys.stderr,
            )
            return CompanionMergeResult.FAILED_MERGE

        merged_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True, encoding="utf-8",
            cwd=merge_wt_path,
        ).stdout.strip()

        # Plumbing ref update — moves the trunk ref to the merge commit without
        # checking anything out. `git branch -f` refuses this ("cannot force
        # update the branch ... used by worktree") specifically because main
        # may be checked out live in companion_root; `update-ref` has no such
        # guard, which is exactly what we need here.
        mv = subprocess.run(
            ["git", "update-ref", f"refs/heads/{base}", merged_sha],
            capture_output=True,
            text=True, encoding="utf-8",
            cwd=companion_root,
        )
        if mv.returncode != 0:
            print(
                f"  [WARN] companion {companion_path}: merge succeeded but moving {base} to "
                f"{merged_sha[:12]} failed:\n{mv.stderr}",
                file=sys.stderr,
            )
            return CompanionMergeResult.FAILED_MERGE
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(merge_wt_path)],
            cwd=companion_root,
            capture_output=True,
        )

    # Clean up the ticket's own isolated worktree.
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(wt_path)],
        cwd=companion_root,
        capture_output=True,
    )

    print(f"  companion {companion_path}: merged {branch} → {base}")
    return CompanionMergeResult.MERGED

def companion_worktree_cleanup(repo_root: Path, companion_path: str, ticket_id: str) -> None:
    """Remove the isolated companion worktree for a ticket.

    Called during ticket cleanup (e.g. on cancel/merge failure) to clean up
    worktrees that may not have been removed by companion_branch_merge.
    """
    companion_root = _resolve_companion(repo_root, companion_path)
    wt_path = companion_worktree_path(companion_root, ticket_id)

    if not wt_path.exists():
        return

    subprocess.run(
        ["git", "worktree", "remove", "--force", str(wt_path)],
        cwd=companion_root,
        capture_output=True,
    )
