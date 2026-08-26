"""Tests for concurrency.py — TOCTOU re-read, lock-status set, divergence check,
claim flock, single-orchestrator advisory lock."""

import os
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

_unix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="fcntl / flock not available on Windows",
)

from lanegate.concurrency import (  # noqa: E402
    OrchestratorLockError,
    SafeguardLockHeld,
    acquire_orchestrator_lock,
    check_local_not_behind_remote,
    claim_lock,
    locked_touches,
    metadata_commit_lock,
    orchestrator_lock_status,
    release_orchestrator_lock,
    reread_and_assert_open,
    safeguard_lock,
    state_dir,
    touches_overlap,
)


def _ticket(tid: str, status: str, touches=None) -> dict:
    return {"id": tid, "status": status, "touches": touches or []}


# ── locked_touches ────────────────────────────────────────────────────────────


def test_locked_touches_in_progress():
    tickets = [
        _ticket("TICK-001", "in_progress", ["src/db.py", "src/auth.py"]),
        _ticket("TICK-002", "open", ["src/api.py"]),
    ]
    locked = locked_touches(tickets, ["in_progress", "code_complete", "in_review"])
    assert "src/db.py" in locked
    assert "src/api.py" not in locked


def test_locked_touches_held_until_code_complete():
    """Lock must be held at code_complete (not just in_progress)."""
    tickets = [
        _ticket("TICK-001", "code_complete", ["src/db.py"]),
        _ticket("TICK-002", "open", ["src/api.py"]),
    ]
    locked = locked_touches(tickets, ["in_progress", "code_complete", "in_review"])
    assert "src/db.py" in locked


def test_locked_touches_held_in_review():
    """Lock must be held at in_review."""
    tickets = [
        _ticket("TICK-001", "in_review", ["src/db.py"]),
    ]
    locked = locked_touches(tickets, ["in_progress", "code_complete", "in_review"])
    assert "src/db.py" in locked


def test_locked_touches_released_at_merged():
    """Lock released once merged."""
    tickets = [
        _ticket("TICK-001", "merged", ["src/db.py"]),
    ]
    locked = locked_touches(tickets, ["in_progress", "code_complete", "in_review"])
    assert "src/db.py" not in locked


def test_locked_touches_no_tickets():
    assert locked_touches([], ["in_progress"]) == set()


def test_locked_touches_ticket_with_no_touches():
    tickets = [_ticket("TICK-001", "in_progress", [])]
    assert locked_touches(tickets, ["in_progress"]) == set()


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (["src/shared.py"], ["src/shared.py"], True),
        (["src/left.py"], ["src/right.py"], False),
        (["*"], ["src/right.py"], True),
        (["src/left.py"], ["*"], True),
        (["*"], ["*"], True),
        (["*"], [], False),
    ],
)
def test_touches_overlap_wildcards(left, right, expected):
    assert touches_overlap(left, right) is expected


# ── locked_touches / hollow_lock_holders with repo_root verification ──────────
#
# A ticket can reach code_complete/in_review via a direct ticket-file edit
# that never went through an executor (e.g. hand-bypassing a stuck
# needs_review gate) -- see TICK-048/TICK-156 in this project's own history.
# When repo_root is passed, such a ticket's touches must not lock out the
# rest of the board.


def _hollow_ticket(tid: str, status: str, touches, tmp_path) -> dict:
    wt_path = tmp_path / "worktrees" / tid.lower()
    wt_path.mkdir(parents=True, exist_ok=True)
    return {
        "id": tid,
        "status": status,
        "touches": touches or [],
        "worktree": str(wt_path),
        "branch": tid.lower(),
    }


def test_locked_touches_ignores_code_complete_with_no_real_commits(tmp_path):
    tickets = [_hollow_ticket("TICK-001", "code_complete", ["src/db.py"], tmp_path)]
    with patch("lanegate.reviewer.worktree_has_commits", return_value=False):
        locked = locked_touches(tickets, ["code_complete"], tmp_path)
    assert "src/db.py" not in locked


def test_locked_touches_still_honors_code_complete_with_real_commits(tmp_path):
    tickets = [_hollow_ticket("TICK-001", "code_complete", ["src/db.py"], tmp_path)]
    with patch("lanegate.reviewer.worktree_has_commits", return_value=True):
        locked = locked_touches(tickets, ["code_complete"], tmp_path)
    assert "src/db.py" in locked


def test_locked_touches_repo_root_none_skips_verification(tmp_path):
    """Backward compat: without repo_root, status alone still decides locking."""
    tickets = [_hollow_ticket("TICK-001", "code_complete", ["src/db.py"], tmp_path)]
    locked = locked_touches(tickets, ["code_complete"])
    assert "src/db.py" in locked


def test_locked_touches_no_worktree_recorded_stays_trusted(tmp_path):
    """A code_complete ticket with no worktree/branch at all (e.g. a
    synthetic/test ticket) is trusted rather than guessed at."""
    tickets = [_ticket("TICK-001", "code_complete", ["src/db.py"])]
    locked = locked_touches(tickets, ["code_complete"], tmp_path)
    assert "src/db.py" in locked


def test_locked_touches_in_progress_never_verified(tmp_path):
    """in_progress is excluded from verification -- a freshly claimed ticket
    legitimately has zero commits yet."""
    tickets = [_hollow_ticket("TICK-001", "in_progress", ["src/db.py"], tmp_path)]
    with patch("lanegate.reviewer.worktree_has_commits", return_value=False):
        locked = locked_touches(tickets, ["in_progress", "code_complete"], tmp_path)
    assert "src/db.py" in locked


def test_locked_touches_ignores_hollow_wildcard_holder(tmp_path):
    tickets = [_hollow_ticket("TICK-001", "code_complete", ["*"], tmp_path)]
    with patch("lanegate.reviewer.worktree_has_commits", return_value=False):
        locked = locked_touches(tickets, ["code_complete"], tmp_path)
    assert locked == set()


def test_hollow_lock_holders_flags_code_complete_with_no_commits(tmp_path):
    from lanegate.concurrency import hollow_lock_holders

    tickets = [
        _hollow_ticket("TICK-001", "code_complete", ["src/db.py"], tmp_path),
        _hollow_ticket("TICK-002", "in_review", ["src/api.py"], tmp_path),
    ]
    with patch("lanegate.reviewer.worktree_has_commits", return_value=False):
        hollow = hollow_lock_holders(tickets, ["code_complete", "in_review"], tmp_path)
    assert {h["id"] for h in hollow} == {"TICK-001", "TICK-002"}


def test_hollow_lock_holders_excludes_in_progress_and_real_completions(tmp_path):
    from lanegate.concurrency import hollow_lock_holders

    tickets = [
        _hollow_ticket("TICK-001", "in_progress", ["src/db.py"], tmp_path),
        _hollow_ticket("TICK-002", "code_complete", ["src/api.py"], tmp_path),
    ]

    with patch("lanegate.reviewer.worktree_has_commits", return_value=True):
        hollow = hollow_lock_holders(tickets, ["in_progress", "code_complete"], tmp_path)
    assert hollow == []


# ── reread_and_assert_open ────────────────────────────────────────────────────


def test_reread_open_succeeds(tmp_path):
    ticket_path = tmp_path / "TICK-001.md"
    ticket_path.write_text("---\nid: TICK-001\nstatus: open\n---\nBody.\n")
    result = reread_and_assert_open(ticket_path)
    assert result["status"] == "open"


def test_reread_grabbed_raises(tmp_path):
    ticket_path = tmp_path / "TICK-001.md"
    ticket_path.write_text("---\nid: TICK-001\nstatus: in_progress\n---\nBody.\n")
    with pytest.raises(RuntimeError, match="grabbed by another session"):
        reread_and_assert_open(ticket_path)


def test_reread_missing_raises(tmp_path):
    with pytest.raises(RuntimeError, match="ticket file disappeared"):
        reread_and_assert_open(tmp_path / "nonexistent.md")


# ── check_local_not_behind_remote ─────────────────────────────────────────────


def test_divergence_check_passes_when_no_remote(tmp_path):
    """Silently passes when origin branch doesn't exist yet (first claim)."""
    with patch("lanegate.concurrency.subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0),  # git fetch
            MagicMock(returncode=1),  # git rev-parse -- remote doesn't exist
        ]
        check_local_not_behind_remote(tmp_path, "tick-007")  # must not raise


def test_divergence_check_passes_when_not_behind(tmp_path):
    with patch("lanegate.concurrency.subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0),  # git fetch
            MagicMock(returncode=0),  # git rev-parse -- remote exists
            MagicMock(returncode=0, stdout="0\n"),  # rev-list count = 0
        ]
        check_local_not_behind_remote(tmp_path, "tick-007")  # must not raise

    assert mock_run.call_args_list[2].args[0] == [
        "git", "rev-list", "--count", "tick-007..origin/tick-007"
    ]


def test_divergence_check_raises_when_behind(tmp_path):
    with patch("lanegate.concurrency.subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0),  # git fetch
            MagicMock(returncode=0),  # git rev-parse -- remote exists
            MagicMock(returncode=0, stdout="3\n"),  # rev-list count = 3 (behind!)
        ]
        with pytest.raises(RuntimeError, match="behind origin"):
            check_local_not_behind_remote(tmp_path, "tick-007")


def test_divergence_check_raises_when_local_branch_is_missing(tmp_path):
    with patch("lanegate.concurrency.subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0),  # git fetch
            MagicMock(returncode=0),  # git rev-parse -- remote exists
            MagicMock(returncode=128),  # rev-list cannot resolve local branch
        ]
        with pytest.raises(RuntimeError, match="could not compare local branch"):
            check_local_not_behind_remote(tmp_path, "tick-007")


# ── claim_lock (per-ticket claim flock) ───────────────────────────────────────


@_unix_only
def test_claim_lock_creates_state_dir(tmp_path):
    from lanegate import APP_NAME

    with claim_lock(tmp_path):
        pass
    assert (tmp_path / f".{APP_NAME}").is_dir()


@_unix_only
def test_claim_lock_is_reentrant_after_release(tmp_path):
    """Sequential acquisitions in the same process must each succeed."""
    for _ in range(3):
        with claim_lock(tmp_path):
            pass  # released each time


@_unix_only
def test_metadata_commit_lock_is_reentrant_after_release(tmp_path):
    for _ in range(3):
        with metadata_commit_lock(tmp_path):
            assert (tmp_path / ".lanegate" / "metadata-commit.lock").exists()


@_unix_only
def test_claim_lock_serializes_concurrent_holders(tmp_path):
    """A second holder must block until the first releases (flock across fds)."""
    order: list[str] = []
    first_holding = threading.Event()
    release_first = threading.Event()

    def first():
        with claim_lock(tmp_path):
            order.append("first-acquired")
            first_holding.set()
            release_first.wait(timeout=5)
            order.append("first-releasing")

    def second():
        first_holding.wait(timeout=5)
        with claim_lock(tmp_path):
            order.append("second-acquired")

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    first_holding.wait(timeout=5)
    t2.start()
    # Give the second thread a moment to attempt acquisition while first still holds.
    time.sleep(0.2)
    assert "second-acquired" not in order  # blocked
    release_first.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert order == ["first-acquired", "first-releasing", "second-acquired"]


# ── per-ticket safeguard lock (TICK-246) ──────────────────────────────────────


@_unix_only
def test_safeguard_lock_creates_state_dir(tmp_path):
    from lanegate import APP_NAME

    lock_dir = tmp_path / f".{APP_NAME}" / "safeguard-locks"
    with safeguard_lock(tmp_path, "TICK-999"):
        assert (lock_dir / "TICK-999.lock").exists()
    assert lock_dir.exists()


@_unix_only
def test_safeguard_lock_unlinks_after_release(tmp_path):
    """Lock file must not accumulate forever -- cleaned up once released."""
    from lanegate import APP_NAME

    lock_path = tmp_path / f".{APP_NAME}" / "safeguard-locks" / "TICK-777.lock"
    with safeguard_lock(tmp_path, "TICK-777"):
        pass
    with safeguard_lock(tmp_path, "TICK-777"):
        pass
    assert not lock_path.exists()


@_unix_only
def test_safeguard_lock_is_reentrant_after_release(tmp_path):
    """Sequential acquisitions in the same process must each succeed."""
    for _ in range(3):
        with safeguard_lock(tmp_path, "TICK-999"):
            pass  # released each time


@_unix_only
def test_safeguard_lock_scoped_per_ticket(tmp_path):
    """Two different tickets never contend with each other."""
    with safeguard_lock(tmp_path, "TICK-001"), safeguard_lock(tmp_path, "TICK-002"):
        pass  # both held simultaneously — no error


@_unix_only
def test_safeguard_lock_refuses_concurrent_holder_same_ticket(tmp_path):
    """A second invocation for the same ticket refuses immediately instead of blocking."""
    holding = threading.Event()
    release = threading.Event()
    second_result: list[str] = []

    def first():
        with safeguard_lock(tmp_path, "TICK-050"):
            holding.set()
            release.wait(timeout=5)

    def second():
        holding.wait(timeout=5)
        try:
            with safeguard_lock(tmp_path, "TICK-050"):
                second_result.append("acquired")
        except SafeguardLockHeld:
            second_result.append("refused")

    t1 = threading.Thread(target=first)
    t1.start()
    holding.wait(timeout=5)
    t2 = threading.Thread(target=second)
    t2.start()
    t2.join(timeout=5)  # must return immediately — non-blocking
    release.set()
    t1.join(timeout=5)

    assert second_result == ["refused"]


@_unix_only
def test_safeguard_lock_free_after_holder_releases(tmp_path):
    """Once the first holder releases, a new invocation for the same ticket succeeds."""
    with safeguard_lock(tmp_path, "TICK-050"):
        pass
    with safeguard_lock(tmp_path, "TICK-050"):
        pass  # would raise SafeguardLockHeld if the lock leaked


# ── single-orchestrator advisory lock ─────────────────────────────────────────


def _dead_pid() -> int:
    """Return a PID that is no longer running."""
    import subprocess

    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


@_unix_only
def test_orchestrator_acquire_writes_pid(tmp_path):
    pid = acquire_orchestrator_lock(tmp_path, pid=12345)
    assert pid == 12345
    assert (state_dir(tmp_path) / "orchestrator.lock").read_text().strip() == "12345"


@_unix_only
def test_orchestrator_acquire_defaults_to_current_pid(tmp_path):
    pid = acquire_orchestrator_lock(tmp_path)
    assert pid == os.getpid()


def _live_other_pid() -> int:
    """A live PID different from the current process (a sleeping child)."""
    import subprocess

    proc = subprocess.Popen(["sleep", "5"])
    return proc.pid


@_unix_only
def test_orchestrator_acquire_blocks_second_live_holder(tmp_path):
    """A second acquire by a different live PID must be refused."""
    acquire_orchestrator_lock(tmp_path, pid=os.getpid())
    with pytest.raises(OrchestratorLockError, match="already running"):
        acquire_orchestrator_lock(tmp_path, pid=_live_other_pid())


@_unix_only
def test_orchestrator_reclaims_stale_lock(tmp_path):
    """A lock held by a dead PID is silently reclaimed."""
    dead = _dead_pid()
    (state_dir(tmp_path) / "orchestrator.lock").write_text(f"{dead}\n")
    pid = acquire_orchestrator_lock(tmp_path, pid=999999)
    assert pid == 999999
    assert (state_dir(tmp_path) / "orchestrator.lock").read_text().strip() == "999999"


@_unix_only
def test_orchestrator_force_overrides_live_holder(tmp_path):
    acquire_orchestrator_lock(tmp_path, pid=_live_other_pid())
    pid = acquire_orchestrator_lock(tmp_path, pid=os.getpid(), force=True)
    assert pid == os.getpid()


def test_orchestrator_force_and_before_claim_are_mutually_exclusive(tmp_path):
    """Combining them used to silently make force a no-op against a live
    holder instead of doing what either knob promises on its own."""
    with pytest.raises(ValueError, match="mutually exclusive"):
        acquire_orchestrator_lock(tmp_path, force=True, before_claim=lambda: None)


@_unix_only
def test_orchestrator_reacquire_same_pid_ok(tmp_path):
    """The same holder re-acquiring is idempotent, not a conflict."""
    acquire_orchestrator_lock(tmp_path, pid=4242)
    pid = acquire_orchestrator_lock(tmp_path, pid=4242)
    assert pid == 4242


@_unix_only
def test_orchestrator_release_by_owner(tmp_path):
    acquire_orchestrator_lock(tmp_path, pid=4242)
    assert release_orchestrator_lock(tmp_path, pid=4242) is True
    assert not (state_dir(tmp_path) / "orchestrator.lock").exists()


@_unix_only
def test_orchestrator_release_wrong_pid_noop(tmp_path):
    acquire_orchestrator_lock(tmp_path, pid=4242)
    assert release_orchestrator_lock(tmp_path, pid=9999) is False
    assert (state_dir(tmp_path) / "orchestrator.lock").exists()


@_unix_only
def test_orchestrator_release_wrong_pid_force(tmp_path):
    acquire_orchestrator_lock(tmp_path, pid=4242)
    assert release_orchestrator_lock(tmp_path, pid=9999, force=True) is True
    assert not (state_dir(tmp_path) / "orchestrator.lock").exists()


@_unix_only
def test_orchestrator_release_absent_is_false(tmp_path):
    assert release_orchestrator_lock(tmp_path, pid=4242) is False


@_unix_only
def test_orchestrator_status_none(tmp_path):
    st = orchestrator_lock_status(tmp_path)
    assert st == {"held": False, "pid": None, "alive": False}


@_unix_only
def test_orchestrator_status_held_live(tmp_path):
    acquire_orchestrator_lock(tmp_path, pid=os.getpid())
    st = orchestrator_lock_status(tmp_path)
    assert st["held"] is True
    assert st["pid"] == os.getpid()
    assert st["alive"] is True


@_unix_only
def test_orchestrator_status_stale(tmp_path):
    dead = _dead_pid()
    (state_dir(tmp_path) / "orchestrator.lock").write_text(f"{dead}\n")
    st = orchestrator_lock_status(tmp_path)
    assert st["held"] is False  # stale → not held
    assert st["pid"] == dead
    assert st["alive"] is False


@_unix_only
def test_orchestrator_garbage_lock_is_reclaimable(tmp_path):
    (state_dir(tmp_path) / "orchestrator.lock").write_text("not-a-pid\n")
    # Garbage parses as no PID → acquire succeeds.
    pid = acquire_orchestrator_lock(tmp_path, pid=777)
    assert pid == 777


# ── TOCTOU race: concurrent claims with overlapping touches ───────────────────

@_unix_only
def test_claim_lock_prevents_concurrent_start_with_overlapping_touches(tmp_path):
    """Concurrent `lanegate start` calls for different tickets with the same touches
    must not both succeed. Only the first should claim; the second should fail.
    """
    from lanegate.lifecycle import cmd_start
    from lanegate.ticket import write_ticket
    from pathlib import Path
    import subprocess

    # Setup: create a bare git repo with test infrastructure
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    # -b main: don't depend on the ambient init.defaultBranch config, which
    # varies by machine/CI image and would otherwise make "main" below wrong.
    subprocess.run(
        ["git", "init", "--bare", "-b", "main"], cwd=repo_path, check=True, capture_output=True
    )

    # Clone it to a working directory
    work_path = tmp_path / "work"
    subprocess.run(["git", "clone", repo_path, work_path], check=True, capture_output=True)
    # git clone doesn't inherit committer identity from anywhere; CI runners
    # have no global git config, so `git commit` below fails without this.
    subprocess.run(
        ["git", "config", "user.email", "lanegate-tests@example.com"], cwd=work_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "LaneGate Tests"], cwd=work_path, check=True)

    # Create config and tickets structure
    tickets_dir = work_path / ".lanegate" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    (work_path / ".lanegate" / "worktrees").mkdir(parents=True, exist_ok=True)

    cfg = {
        "ticket_prefix": "TICK",
        "tickets_dir": ".lanegate/tickets",
        "worktrees_dir": ".lanegate/worktrees",
        "lock_statuses": ["in_progress", "code_complete", "in_review"],
        "commit_status_changes": True,
    }

    # Create two tickets both touching the same file
    ticket1_path = tickets_dir / "TICK-001.md"
    ticket2_path = tickets_dir / "TICK-002.md"

    ticket1 = {
        "id": "TICK-001",
        "title": "First ticket",
        "status": "open",
        "touches": ["shared.py"],
        "_path": ticket1_path,
    }
    ticket2 = {
        "id": "TICK-002",
        "title": "Second ticket",
        "status": "open",
        "touches": ["shared.py"],
        "_path": ticket2_path,
    }

    write_ticket(ticket1)
    write_ticket(ticket2)

    # Commit initial ticket files
    subprocess.run(
        ["git", "add", ".lanegate/tickets/"],
        cwd=work_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "initial tickets"],
        cwd=work_path,
        check=True,
        capture_output=True,
    )

    results = {"first": None, "second": None}

    def start_ticket_1():
        try:
            cmd_start("TICK-001", cfg, work_path, interactive=False)
            results["first"] = "success"
        except SystemExit as e:
            results["first"] = f"exit({e.code})"

    def start_ticket_2():
        # Give ticket 1 a moment to start its claim attempt
        time.sleep(0.1)
        try:
            cmd_start("TICK-002", cfg, work_path, interactive=False)
            results["second"] = "success"
        except SystemExit as e:
            results["second"] = f"exit({e.code})"

    t1 = threading.Thread(target=start_ticket_1)
    t2 = threading.Thread(target=start_ticket_2)

    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    # One should succeed (first), one should fail (second) due to conflict
    # The actual outcomes may vary slightly depending on timing, but the key is:
    # both should not succeed. Either the first succeeds and second fails,
    # or vice versa, but NOT both.
    assert results["first"] is not None, "First thread did not complete"
    assert results["second"] is not None, "Second thread did not complete"

    success_count = sum(1 for v in results.values() if v == "success")
    assert success_count == 1, f"Expected exactly 1 success, got {success_count}: {results}"


@_unix_only
def test_claim_file_prevents_concurrent_claims_with_overlapping_touches(tmp_path):
    """Concurrent `lanegate claim-file` calls for different tickets with the same file
    must not both succeed. Only the first should claim; the second should fail.
    """
    from lanegate.claim_file import cmd_claim_file
    from lanegate.ticket import write_ticket, parse_ticket

    # Setup tickets structure
    tickets_dir = tmp_path / ".lanegate" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".lanegate").mkdir(parents=True, exist_ok=True)

    cfg = {
        "ticket_prefix": "TICK",
        "tickets_dir": ".lanegate/tickets",
        "lock_statuses": ["in_progress", "code_complete", "in_review"],
        "commit_status_changes": False,
    }

    # Create two tickets, both in_progress, with no touches yet
    ticket1_path = tickets_dir / "TICK-001.md"
    ticket2_path = tickets_dir / "TICK-002.md"

    ticket1 = {
        "id": "TICK-001",
        "title": "First ticket",
        "status": "in_progress",
        "touches": [],
        "_path": ticket1_path,
    }
    ticket2 = {
        "id": "TICK-002",
        "title": "Second ticket",
        "status": "in_progress",
        "touches": [],
        "_path": ticket2_path,
    }

    write_ticket(ticket1)
    write_ticket(ticket2)

    results = {"first": None, "second": None}

    def claim_file_1():
        try:
            cmd_claim_file("shared.py", "TICK-001", cfg, tmp_path)
            results["first"] = "success"
        except SystemExit as e:
            results["first"] = f"exit({e.code})"

    def claim_file_2():
        # Give claim 1 a moment to start its claim attempt
        time.sleep(0.05)
        try:
            cmd_claim_file("shared.py", "TICK-002", cfg, tmp_path)
            results["second"] = "success"
        except SystemExit as e:
            results["second"] = f"exit({e.code})"

    t1 = threading.Thread(target=claim_file_1)
    t2 = threading.Thread(target=claim_file_2)

    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    # One should succeed (first), one should fail (second) due to conflict
    assert results["first"] is not None, "First thread did not complete"
    assert results["second"] is not None, "Second thread did not complete"

    success_count = sum(1 for v in results.values() if v == "success")
    assert success_count == 1, f"Expected exactly 1 success, got {success_count}: {results}"

    # Verify that shared.py is in exactly one ticket's touches
    t1_final = parse_ticket(tickets_dir / "TICK-001.md")
    t2_final = parse_ticket(tickets_dir / "TICK-002.md")
    t1_has_file = "shared.py" in (t1_final.get("touches") or [])
    t2_has_file = "shared.py" in (t2_final.get("touches") or [])
    assert t1_has_file != t2_has_file, "File should be in exactly one ticket's touches"
    assert t1_has_file or t2_has_file, "File should be in at least one ticket's touches"
