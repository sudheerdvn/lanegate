"""Tests for reconciliation.py — superseded-ticket detection (TICK-284)."""

import subprocess
from pathlib import Path

import pytest

from lanegate.reconciliation import (
    branch_reachable_from_main,
    find_equivalent_merged_ticket,
    reconcile_ticket,
)


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    (path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


def _run_git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path):
    _init_git_repo(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# branch_reachable_from_main
# ---------------------------------------------------------------------------


def test_branch_reachable_from_main_true_when_merged_into_main(git_repo):
    _run_git(git_repo, "checkout", "-b", "tick-100")
    (git_repo / "feature.txt").write_text("feature\n")
    _run_git(git_repo, "add", ".")
    _run_git(git_repo, "commit", "-m", "feature work")
    _run_git(git_repo, "checkout", "main")
    _run_git(git_repo, "merge", "--no-ff", "tick-100", "-m", "merge tick-100")

    result = branch_reachable_from_main(git_repo, "tick-100")

    main_tip = subprocess.run(
        ["git", "rev-parse", "main"], cwd=git_repo, capture_output=True, text=True
    ).stdout.strip()
    assert result == main_tip


def test_branch_reachable_from_main_none_when_branch_has_unmerged_commits(git_repo):
    _run_git(git_repo, "checkout", "-b", "tick-101")
    (git_repo / "unmerged.txt").write_text("unmerged\n")
    _run_git(git_repo, "add", ".")
    _run_git(git_repo, "commit", "-m", "unmerged work")

    assert branch_reachable_from_main(git_repo, "tick-101") is None


def test_branch_reachable_from_main_none_when_branch_has_no_unique_commits(git_repo):
    """A never-started ticket branch shares main's tip but is not superseded."""
    _run_git(git_repo, "branch", "tick-102")

    assert branch_reachable_from_main(git_repo, "tick-102") is None

    ticket = {
        "id": "TICK-102",
        "branch": "tick-102",
        "title": "Novel ticket work",
        "touches": ["lanegate/novel.py"],
    }
    assert reconcile_ticket(ticket, [ticket], git_repo) is None


def test_branch_reachable_from_main_none_when_empty_branch_is_rebased(git_repo):
    """Rebasing an untouched branch onto newer main is not ticket work."""
    _run_git(git_repo, "branch", "tick-103")
    (git_repo / "unrelated.txt").write_text("main moved\n")
    _run_git(git_repo, "add", ".")
    _run_git(git_repo, "commit", "-m", "unrelated main work")
    _run_git(git_repo, "checkout", "tick-103")
    _run_git(git_repo, "rebase", "main")

    assert branch_reachable_from_main(git_repo, "tick-103") is None


def test_branch_reachable_from_main_none_when_branch_reset_to_main_after_real_commit(git_repo):
    """A branch that had a real commit but was later reset back onto main's
    current tip (e.g. manual junk-commit cleanup, or a hibernate/fail-cycle
    rebase) must not be misclassified as superseded: its tip is identical to
    main's, so it has zero commits of its own right now (TICK-333)."""
    _run_git(git_repo, "checkout", "-b", "tick-104")
    (git_repo / "junk.txt").write_text("junk\n")
    _run_git(git_repo, "add", ".")
    _run_git(git_repo, "commit", "-m", "junk commit")

    # main advances past the branch's creation point while the branch sits
    # on its junk commit -- this is what makes `base..branch` (base = the
    # branch's original creation point in its reflog) trivially non-empty
    # after the reset below, since it now contains main's own new commits.
    _run_git(git_repo, "checkout", "main")
    (git_repo / "unrelated.txt").write_text("main moved on\n")
    _run_git(git_repo, "add", ".")
    _run_git(git_repo, "commit", "-m", "unrelated main work")

    _run_git(git_repo, "checkout", "tick-104")
    _run_git(git_repo, "reset", "--hard", "main")

    assert branch_reachable_from_main(git_repo, "tick-104") is None

    ticket = {
        "id": "TICK-104",
        "branch": "tick-104",
        "title": "Novel ticket work",
        "touches": ["lanegate/novel.py"],
    }
    assert reconcile_ticket(ticket, [ticket], git_repo) is None


def test_branch_reachable_from_main_none_when_branch_missing(git_repo):
    assert branch_reachable_from_main(git_repo, "no-such-branch") is None


def test_branch_reachable_from_main_none_when_not_a_git_repo(tmp_path):
    assert branch_reachable_from_main(tmp_path, "main") is None


# ---------------------------------------------------------------------------
# find_equivalent_merged_ticket
# ---------------------------------------------------------------------------

_MERGED_TICKET = {
    "id": "TICK-200",
    "status": "merged",
    "title": "Fix timezone-aware reset parsing in resume-watch",
    "touches": ["lanegate/resume_watch.py"],
}


def test_find_equivalent_merged_ticket_matches_same_touches_and_similar_title():
    candidate = {
        "id": "TICK-201",
        "title": "Fix timezone-aware reset parsing in resume-watch (regression)",
        "touches": ["lanegate/resume_watch.py"],
    }
    assert find_equivalent_merged_ticket(candidate, [_MERGED_TICKET, candidate]) == "TICK-200"


def test_find_equivalent_merged_ticket_none_when_touches_differ():
    candidate = {
        "id": "TICK-202",
        "title": "Fix timezone-aware reset parsing in resume-watch",
        "touches": ["lanegate/other_module.py"],
    }
    assert find_equivalent_merged_ticket(candidate, [_MERGED_TICKET, candidate]) is None


def test_find_equivalent_merged_ticket_none_when_titles_dissimilar():
    candidate = {
        "id": "TICK-203",
        "title": "Completely unrelated change to logging output",
        "touches": ["lanegate/resume_watch.py"],
    }
    assert find_equivalent_merged_ticket(candidate, [_MERGED_TICKET, candidate]) is None


def test_find_equivalent_merged_ticket_none_for_wildcard_touches():
    candidate = {
        "id": "TICK-204",
        "title": "Fix timezone-aware reset parsing in resume-watch",
        "touches": ["*"],
    }
    assert find_equivalent_merged_ticket(candidate, [_MERGED_TICKET, candidate]) is None


def test_find_equivalent_merged_ticket_none_for_empty_touches():
    candidate = {
        "id": "TICK-205",
        "title": "Fix timezone-aware reset parsing in resume-watch",
        "touches": [],
    }
    assert find_equivalent_merged_ticket(candidate, [_MERGED_TICKET, candidate]) is None


def test_find_equivalent_merged_ticket_ignores_non_merged_candidates():
    other = {**_MERGED_TICKET, "id": "TICK-206", "status": "in_progress"}
    candidate = {
        "id": "TICK-207",
        "title": "Fix timezone-aware reset parsing in resume-watch",
        "touches": ["lanegate/resume_watch.py"],
    }
    assert find_equivalent_merged_ticket(candidate, [other, candidate]) is None


# ---------------------------------------------------------------------------
# reconcile_ticket / test_superseded_ticket_marked_and_filtered
# ---------------------------------------------------------------------------


def test_superseded_ticket_marked_and_filtered(git_repo):
    """Close criteria for TICK-284: a ticket whose implementation commits
    are already reachable from main is marked with replacement_commit
    metadata, and (once cmd_supersede applies that metadata and flips
    status) is filtered from next_batch's open/hibernated candidate set."""
    _run_git(git_repo, "checkout", "-b", "tick-300")
    (git_repo / "already_landed.txt").write_text("already landed\n")
    _run_git(git_repo, "add", ".")
    _run_git(git_repo, "commit", "-m", "already landed work")
    _run_git(git_repo, "checkout", "main")
    _run_git(git_repo, "merge", "--no-ff", "tick-300", "-m", "merge tick-300")

    ticket = {
        "id": "TICK-300",
        "status": "in_progress",
        "branch": "tick-300",
        "touches": ["already_landed.txt"],
    }
    evidence = reconcile_ticket(ticket, [ticket], git_repo)

    assert evidence is not None
    assert "replacement_commit" in evidence

    ticket.update(evidence)
    ticket["status"] = "closed"

    # Filtering: next_batch (orchestrate.py) only considers status in
    # ("open", "hibernated") -- a closed/superseded ticket is excluded by
    # construction once reconciliation flips its status.
    assert ticket["status"] not in ("open", "hibernated")


def test_reconcile_ticket_prefers_branch_evidence_over_equivalent_ticket(git_repo):
    """When both checks would find evidence, the unambiguous git fact
    (branch already on main) wins over the fuzzy title/touches heuristic."""
    _run_git(git_repo, "checkout", "-b", "tick-301")
    (git_repo / "resume_watch.py").write_text("# already landed\n")
    _run_git(git_repo, "add", ".")
    _run_git(git_repo, "commit", "-m", "already landed work")
    _run_git(git_repo, "checkout", "main")
    _run_git(git_repo, "merge", "--no-ff", "tick-301", "-m", "merge tick-301")

    ticket = {
        "id": "TICK-301",
        "branch": "tick-301",
        "title": "Fix timezone-aware reset parsing in resume-watch",
        "touches": ["resume_watch.py"],
    }
    merged_equivalent = {
        "id": "TICK-200",
        "status": "merged",
        "title": "Fix timezone-aware reset parsing in resume-watch",
        "touches": ["resume_watch.py"],
    }

    evidence = reconcile_ticket(ticket, [ticket, merged_equivalent], git_repo)

    assert "replacement_commit" in evidence
    assert "equivalent_ticket_id" not in evidence


def test_reconcile_ticket_none_when_no_evidence(git_repo):
    ticket = {
        "id": "TICK-302",
        "branch": None,
        "title": "Completely novel change",
        "touches": ["lanegate/novel_module.py"],
    }
    assert reconcile_ticket(ticket, [ticket], git_repo) is None
