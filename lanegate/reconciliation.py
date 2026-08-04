"""
reconciliation.py — detect tickets superseded by main or by an equivalent
merged ticket (TICK-284).

Two independent, deliberately conservative checks:

1. branch_reachable_from_main: a ticket's own branch tip is already an
   ancestor of main -- every commit it has already exists on main through
   some other path (e.g. someone committed the same fix directly to main,
   or another ticket delivered equivalent commits and this branch was
   rebased/cherry-picked in already). This is a pure git fact, unambiguous
   and safe to act on automatically.

2. find_equivalent_merged_ticket: a heuristic, NOT a proof -- exact-touches
   overlap plus title-term similarity against an already-merged ticket.
   Deliberately surfaced as a *candidate* for human confirmation (via
   `lanegate supersede`) rather than auto-closed, since two tickets touching
   the same files with similar titles are not guaranteed to be duplicates.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from lanegate.analyze import _STOPWORDS
from lanegate.config import load_config, resolve_trunk_branch

_DEFAULT_SIMILARITY_THRESHOLD = 0.6


def branch_reachable_from_main(
    repo_root: Path, branch: str, trunk_branch: str | None = None
) -> str | None:
    """Return the trunk's current commit hash if `branch`'s tip is already an
    ancestor of the trunk -- i.e. this ticket's own eventual merge would be a
    no-op because the work already landed through some other path.

    Returns None (never raises) when the branch doesn't exist, isn't
    reachable, or git fails for any reason.
    """
    trunk_branch = trunk_branch or resolve_trunk_branch(load_config(repo_root), repo_root)
    exists = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", branch],
        cwd=repo_root,
        capture_output=True,
        text=True, encoding="utf-8",
    )
    if exists.returncode != 0:
        return None

    # A branch tip that is already on main is only meaningful supersession
    # evidence when the ticket branch actually advanced from its creation
    # point. Plain ``merge-base branch main`` cannot establish that after a
    # merge: it returns the branch tip both for an empty branch and for a
    # branch whose commits were merged. The branch reflog retains the local
    # creation point, so use it to distinguish those cases. If it is absent,
    # fail closed rather than misclassifying a never-started ticket as obsolete.
    reflog = subprocess.run(
        ["git", "reflog", "show", "--format=%H%x00%gs", branch],
        cwd=repo_root,
        capture_output=True,
        text=True, encoding="utf-8",
    )
    if reflog.returncode != 0:
        return None
    entries = [entry.partition("\x00") for entry in reflog.stdout.splitlines() if entry]
    if not entries:
        return None
    base = entries[-1][0]
    if not base:
        return None

    # A rebase of an empty branch advances its tip to main even though it
    # contains no ticket work. A normal commit, cherry-pick, or rebase pick
    # records actual work in the reflog; a bare ``rebase (finish)`` does not.
    # Require that evidence as well as a tip past the creation point.
    if not any(
        action.startswith(("commit", "cherry-pick", "rebase (pick)", "rebase (continue)"))
        for _, _, action in entries
    ):
        return None

    ahead = subprocess.run(
        ["git", "rev-list", "--count", f"{base}..{branch}"],
        cwd=repo_root,
        capture_output=True,
        text=True, encoding="utf-8",
    )
    try:
        commit_count = int(ahead.stdout.strip())
    except ValueError:
        return None
    if ahead.returncode != 0 or commit_count < 1:
        return None

    main_tip = subprocess.run(
        ["git", "rev-parse", trunk_branch],
        cwd=repo_root,
        capture_output=True,
        text=True, encoding="utf-8",
    )
    if main_tip.returncode != 0:
        return None
    main_tip_hash = main_tip.stdout.strip()
    if not main_tip_hash:
        return None

    # A branch whose tip is *literally* main's current tip (e.g. after a
    # `git reset --hard main` used to discard junk commits) has zero commits
    # of its own ahead of main right now, regardless of what its reflog says
    # happened in the past. That is "never advanced" / "cleaned up", not
    # supersession -- only a branch tip that differs from main's tip while
    # still being an ancestor of it (its real commits landed on main through
    # another path, and main has since moved on) counts as evidence.
    branch_tip = subprocess.run(
        ["git", "rev-parse", branch],
        cwd=repo_root,
        capture_output=True,
        text=True, encoding="utf-8",
    )
    if branch_tip.returncode != 0:
        return None
    if branch_tip.stdout.strip() == main_tip_hash:
        return None

    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", branch, trunk_branch],
        cwd=repo_root,
        capture_output=True,
        text=True, encoding="utf-8",
    )
    if ancestor.returncode != 0:
        return None

    return main_tip_hash or None


def _title_terms(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]{3,}", text.lower()) if t not in _STOPWORDS}


def _title_similarity(a: str, b: str) -> float:
    """Jaccard similarity of title terms; 0.0 if either title has no terms."""
    ta, tb = _title_terms(a), _title_terms(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def find_equivalent_merged_ticket(
    ticket: dict,
    all_tickets: list[dict],
    *,
    threshold: float = _DEFAULT_SIMILARITY_THRESHOLD,
) -> str | None:
    """Return the id of an already-merged ticket that plausibly covers the
    same intent as `ticket`, or None.

    Conservative by construction: requires an *exact* touches-set match
    (not overlap) plus title-term similarity above `threshold`. Wildcard
    touches (['*']) and empty touches never match anything -- both are too
    broad/ambiguous to signal "same scope".
    """
    my_touches = set(ticket.get("touches") or [])
    if not my_touches or "*" in my_touches:
        return None

    for other in all_tickets:
        if other.get("id") == ticket.get("id"):
            continue
        if other.get("status") != "merged":
            continue
        other_touches = set(other.get("touches") or [])
        if other_touches != my_touches:
            continue
        if _title_similarity(ticket.get("title", ""), other.get("title", "")) >= threshold:
            return other["id"]
    return None


def reconcile_ticket(
    ticket: dict,
    all_tickets: list[dict],
    repo_root: Path,
    *,
    threshold: float = _DEFAULT_SIMILARITY_THRESHOLD,
    trunk_branch: str | None = None,
) -> dict | None:
    """Check whether `ticket` is superseded. Returns metadata to record on
    the ticket ({'replacement_commit': <hash>} or
    {'equivalent_ticket_id': <id>}), preferring the unambiguous git-fact
    check over the heuristic one. None if neither check finds evidence.
    """
    branch = ticket.get("branch")
    if branch:
        commit = branch_reachable_from_main(repo_root, branch, trunk_branch)
        if commit:
            return {"replacement_commit": commit}

    equivalent = find_equivalent_merged_ticket(ticket, all_tickets, threshold=threshold)
    if equivalent:
        return {"equivalent_ticket_id": equivalent}

    return None
