"""Touched-file and scope-drift checks for lifecycle commands."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lanegate.config import load_config, resolve_trunk_branch
from lanegate.ticket import is_paired_test_file


def _trunk_branch(worktree_path: Path, trunk_branch: str | None = None) -> str:
    return trunk_branch or resolve_trunk_branch(load_config(worktree_path), worktree_path)


def _get_touched_files(
    worktree_path: Path, branch: str, trunk_branch: str | None = None
) -> list[str]:
    """Return files changed on this branch relative to the trunk (committed changes only).

    Runs ``git diff --name-only <trunk>...{branch}`` in the worktree so the list reflects
    exactly what will enter the trunk at merge time.  Returns an empty list on any error.
    """
    r = subprocess.run(
        ["git", "diff", "--name-only", f"{_trunk_branch(worktree_path, trunk_branch)}...{branch}"],
        capture_output=True,
        text=True,
        cwd=worktree_path,
    )
    if r.returncode != 0:
        return []
    return [f for f in r.stdout.strip().splitlines() if f]


def _get_branch_wall_time_ms(worktree_path: Path, trunk_branch: str | None = None) -> int:
    """Return elapsed wall-clock ms from the first commit on this branch to now.

    Uses ``git log --format=%ct <trunk>..HEAD`` to list commit timestamps (newest first),
    takes the *oldest* one (last in the list), and computes ms to the current time.
    Returns 0 if git fails or no commits are on the branch.
    """
    r = subprocess.run(
        ["git", "log", "--format=%ct", f"{_trunk_branch(worktree_path, trunk_branch)}..HEAD"],
        capture_output=True,
        text=True,
        cwd=worktree_path,
    )
    if r.returncode != 0:
        return 0
    lines = [line for line in r.stdout.strip().splitlines() if line.strip()]
    if not lines:
        return 0
    try:
        oldest_ts = int(lines[-1].strip())
    except ValueError:
        return 0
    from lanegate import lifecycle as lifecycle_module

    now_ts = lifecycle_module.time.time()
    return max(0, int((now_ts - oldest_ts) * 1000))


def _has_committed_changes(wt_path: Path) -> bool:
    """True if the worktree branch has at least one file committed ahead of main.

    Fail-closed: if the git command itself fails, treat the worktree as having
    no commits rather than silently letting a broken worktree advance.
    """
    r = subprocess.run(
        ["git", "diff", f"{_trunk_branch(wt_path)}...HEAD", "--name-only"],
        capture_output=True,
        text=True,
        cwd=wt_path,
    )
    if r.returncode != 0:
        return False
    return bool(r.stdout.strip())


def _get_changed_files(wt_path: Path) -> set[str]:
    """Return the set of files changed on this branch (committed vs main + uncommitted)."""
    changed: set[str] = set()

    # Committed changes on this branch vs main
    r = subprocess.run(
        ["git", "diff", f"{_trunk_branch(wt_path)}...HEAD", "--name-only"],
        capture_output=True,
        text=True,
        cwd=wt_path,
    )
    if r.returncode == 0:
        changed |= {f for f in r.stdout.strip().splitlines() if f}

    # Staged + unstaged uncommitted changes
    r2 = subprocess.run(
        ["git", "diff", "HEAD", "--name-only"],
        capture_output=True,
        text=True,
        cwd=wt_path,
    )
    if r2.returncode == 0:
        changed |= {f for f in r2.stdout.strip().splitlines() if f}

    return changed


def check_touches_compliance(
    tid: str,
    ticket: dict,
    wt_path: Path,
    *,
    allow_drift: bool = False,
) -> None:
    """Block completion when the diff contains files not declared in ticket.touches.

    Raises SystemExit(1) when undeclared files are found and neither the wildcard
    escape hatch (touches: ["*"]) nor the --allow-drift flag is active.

    Args:
        tid: Canonical ticket ID for error messages.
        ticket: Parsed ticket dict.
        wt_path: Path to the worktree (used to run git diff).
        allow_drift: When True, emit a warning instead of blocking (--allow-drift flag).
    """
    declared = set(ticket.get("touches") or [])

    # Wildcard escape hatch: touches: ["*"] means "anything goes"
    if "*" in declared:
        return

    changed = _get_changed_files(wt_path)
    undeclared = changed - declared
    # TICK-245: a changed file that's the natural paired test file for an
    # already-declared module is not scope drift.
    undeclared = {f for f in undeclared if not is_paired_test_file(f, declared)}

    if not undeclared:
        return

    if allow_drift:
        print(
            f"WARNING: --allow-drift: {tid} touched {len(undeclared)} undeclared file(s):",
            file=sys.stderr,
        )
        for f in sorted(undeclared):
            print(f"  + {f}", file=sys.stderr)
        print(
            "  These files are not locked — another ticket may conflict on merge.", file=sys.stderr
        )
        print(f"  Add them to touches in tickets/{tid}.md to extend the lock.", file=sys.stderr)
        print(file=sys.stderr)
        return

    # Hard block
    print(
        f"ERROR: {tid} touched {len(undeclared)} file(s) not declared in touches:", file=sys.stderr
    )
    for f in sorted(undeclared):
        print(f"  + {f}", file=sys.stderr)
    print(file=sys.stderr)
    print("  Completion blocked. To fix:", file=sys.stderr)
    print(f"    1. Add the files above to touches in tickets/{tid}.md, OR", file=sys.stderr)
    print(
        "    2. Use 'touches: [\"*\"]' to declare that this ticket touches unpredictable files, OR",
        file=sys.stderr,
    )
    print(
        "    3. Pass --allow-drift to bypass this check with a warning (use sparingly).",
        file=sys.stderr,
    )
    sys.exit(1)


def _check_touches_drift(tid: str, ticket: dict, wt_path: Path) -> None:
    """Kept for backward compatibility — delegates to check_touches_compliance in warn-only mode."""
    # allow_drift=True preserves the old warning-only behaviour for any callers
    # that imported this name directly before TICK-046.
    check_touches_compliance(tid, ticket, wt_path, allow_drift=True)
