"""
concurrency.py — parallel-safety helpers for ticket claiming.

Guarantees (same-checkout, multiple processes):
- Claim flock: an exclusive `flock` serializes the read→write→commit window of a claim,
  so two racing `lanegate start` processes cannot interleave (closes the residual TOCTOU
  gap the best-effort re-read alone leaves — git's index.lock only serializes the commit).
- TOCTOU re-read: re-reads the ticket file immediately before writing the status lock,
  so two racing processes cannot both "win" the claim.
- Lock-status set: configurable set of statuses that hold the touches lock (not just in_progress).
- Fetch/divergence check: detects cross-clone divergence before claiming, refusing a start
  if the local ticket branch is behind its remote.

Single-orchestrator advisory lock (DECISIONS F14):
- A PID file at `.lanegate/orchestrator.lock` ensures only one orchestrator drives a repo
  at a time, so the global live-agent count can't silently double (each orchestrator
  enforces its own max_parallel). The check-and-set is made atomic by a short flock, and a
  dead holder (PID no longer alive) is reclaimed automatically.

Per-ticket safeguard lock (TICK-246):
- A non-blocking flock at `.lanegate/safeguard-locks/<tid>.lock` ensures only one
  `lanegate complete`/`lanegate merge` invocation runs a ticket's safeguard commands (e.g.
  pytest) at a time. A second concurrent invocation refuses immediately instead of
  spawning its own redundant (possibly hanging) subprocess. Held only for the duration
  of the safeguard run and released automatically on process exit, including a crash
  or kill -9 — so a holder that dies mid-run can never wedge a future invocation.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from pathlib import Path

import portalocker
import portalocker.exceptions

from lanegate import APP_NAME
from lanegate.pidutil import pid_alive
from lanegate.ticket import parse_ticket

# ---------------------------------------------------------------------------
# Lock file location
# ---------------------------------------------------------------------------


def state_dir(repo_root: Path) -> Path:
    """Return (creating if needed) the repo-scoped `.lanegate/` state directory."""
    d = repo_root / f".{APP_NAME}"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Per-ticket claim flock
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def claim_lock(repo_root: Path):
    """
    Hold an exclusive advisory lock for the duration of a claim's read→write→commit window.

    Two `lanegate start` processes on the same checkout serialize here, so the
    re-read→write→commit sequence is atomic with respect to each other. The lock is an
    OS-level exclusive lock on `.lanegate/claim.lock`, released automatically when the
    context exits (including on crash), so it never goes stale.
    """
    lock_path = state_dir(repo_root) / "claim.lock"
    with portalocker.Lock(str(lock_path), "a", timeout=None):
        yield


class SafeguardLockHeld(RuntimeError):
    """Raised when another lanegate complete/merge invocation is already running
    this ticket's safeguards."""


@contextlib.contextmanager
def safeguard_lock(repo_root: Path, tid: str):
    """
    Non-blocking exclusive lock scoped to one ticket's safeguard run.

    Two concurrent `lanegate complete`/`lanegate merge` invocations for the same ticket
    (e.g. a resumed executor overlapping a still-running prior one) must not both
    spawn their own safeguard subprocess — see TICK-246. Raises SafeguardLockHeld
    immediately if another invocation already holds it; never blocks/waits. The
    underlying flock is released automatically when the holding process exits for
    any reason (including a crash), so a dead holder can never wedge future runs.
    """
    lock_dir = state_dir(repo_root) / "safeguard-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{tid}.lock"
    acquired = False
    try:
        with portalocker.Lock(str(lock_path), "a", timeout=0, fail_when_locked=True):
            acquired = True
            yield
    except portalocker.exceptions.LockException as exc:
        raise SafeguardLockHeld(
            f"{tid}: another lanegate complete/merge invocation is already running "
            f"this ticket's safeguards"
        ) from exc
    finally:
        # Best-effort cleanup, and only once we actually held the lock (never
        # unlink out from under a still-running holder that beat us to it via
        # fail_when_locked above). Released before this runs, so a lost race
        # against a fresh acquirer just means the file gets recreated on the
        # next complete/merge -- harmless.
        if acquired:
            lock_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Single-orchestrator advisory lock (PID file + stale reclaim)
# ---------------------------------------------------------------------------


def _orchestrator_lock_path(repo_root: Path) -> Path:
    return state_dir(repo_root) / "orchestrator.lock"


def _pid_alive(pid: int) -> bool:
    """Return True if a process with this PID exists (and we may signal it).

    Delegates to the shared cross-platform probe; on Windows a plain
    ``os.kill(pid, 0)`` would *terminate* the process being checked.
    """
    return pid_alive(pid)


def _read_lock_pid(lock_path: Path) -> int | None:
    """Return the PID recorded in the lock file, or None if absent/garbage."""
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(raw.split()[0]) if raw else None
    except (ValueError, IndexError):
        return None


@contextlib.contextmanager
def _flock_guard(repo_root: Path):
    """Brief exclusive lock making the orchestrator-lock check-and-set atomic."""
    guard_path = state_dir(repo_root) / "orchestrator.lock.guard"
    with portalocker.Lock(str(guard_path), "a", timeout=None):
        yield


class OrchestratorLockError(RuntimeError):
    """Raised when the orchestrator advisory lock is already held by a live process."""


def acquire_orchestrator_lock(repo_root: Path, pid: int | None = None, force: bool = False) -> int:
    """
    Atomically claim the single-orchestrator advisory lock for `pid` (default: current PID).

    Returns the PID written on success. Raises OrchestratorLockError if a *live* orchestrator
    already holds it (unless force=True). A stale lock (holder PID is dead) is reclaimed
    automatically. The PID file persists after this call returns — it represents the
    orchestrator session, not this process — so the caller (or `release_orchestrator_lock`)
    is responsible for removing it.
    """
    pid = os.getpid() if pid is None else pid
    lock_path = _orchestrator_lock_path(repo_root)
    with _flock_guard(repo_root):
        existing = _read_lock_pid(lock_path)
        if existing is not None and existing != pid and _pid_alive(existing) and not force:
            raise OrchestratorLockError(
                f"an orchestrator (PID {existing}) is already running for this repo. "
                f"Use --force to override a wedged lock, or attach read-only with status."
            )
        lock_path.write_text(f"{pid}\n", encoding="utf-8")
    return pid


def release_orchestrator_lock(repo_root: Path, pid: int | None = None, force: bool = False) -> bool:
    """
    Release the orchestrator advisory lock if it is held by `pid` (default: current PID).

    Returns True if a lock was removed. With force=True, removes regardless of holder.
    Returns False if no matching lock was present (idempotent — safe to call on exit).
    """
    pid = os.getpid() if pid is None else pid
    lock_path = _orchestrator_lock_path(repo_root)
    with _flock_guard(repo_root):
        existing = _read_lock_pid(lock_path)
        if existing is None:
            return False
        if existing != pid and not force:
            return False
        with contextlib.suppress(OSError):
            lock_path.unlink()
        return True


def orchestrator_lock_status(repo_root: Path) -> dict:
    """
    Report the orchestrator lock state without acquiring it (read-only attach).

    Returns {"held": bool, "pid": int|None, "alive": bool}. "held" is True only when a live
    process owns the lock; a stale lock reports held=False so callers know it is reclaimable.
    """
    lock_path = _orchestrator_lock_path(repo_root)
    pid = _read_lock_pid(lock_path)
    if pid is None:
        return {"held": False, "pid": None, "alive": False}
    alive = _pid_alive(pid)
    return {"held": alive, "pid": pid, "alive": alive}


# Lock statuses that assert "an executor already did real work here" and
# so are worth re-verifying against the branch before trusting the lock.
# in_progress is deliberately excluded: a just-claimed ticket legitimately
# has zero commits yet, and that's not a sign anything is wrong.
_VERIFIABLE_LOCK_STATUSES = frozenset({"code_complete", "in_review"})


def locked_touches(
    tickets: list[dict],
    lock_statuses: list[str],
    repo_root: Path | None = None,
) -> set[str]:
    """Return all file paths locked by tickets in any lock status.

    When repo_root is given, a ticket in a verifiable lock status
    (code_complete, in_review) only contributes its touches if its branch
    actually has commits ahead of main. This stops a ticket whose status
    was set without any real implementation (e.g. a hand-edited ticket
    file) from silently blocking the rest of the board forever -- see
    hollow_lock_holders() for surfacing those tickets to a human.
    """
    lock_set = set(lock_statuses)
    locked: set[str] = set()
    for t in tickets:
        status = t.get("status")
        if status not in lock_set:
            continue
        if (
            repo_root is not None
            and status in _VERIFIABLE_LOCK_STATUSES
            and not _trusted_lock_holder(t, repo_root)
        ):
            continue
        locked.update(t.get("touches") or [])
    return locked


def touches_overlap(left: set[str] | list[str], right: set[str] | list[str]) -> bool:
    """Return whether two declared touch sets must not run concurrently.

    ``touches: ["*"]`` is the documented broad-scope declaration.  It must
    lock every *declared* path, while an empty list continues to mean that no
    file scope was declared and therefore creates no file lock.
    """
    left_set = set(left)
    right_set = set(right)
    if not left_set or not right_set:
        return False
    return bool(left_set & right_set) or "*" in left_set or "*" in right_set


def hollow_lock_holders(
    tickets: list[dict],
    lock_statuses: list[str],
    repo_root: Path,
) -> list[dict]:
    """Tickets in a verifiable lock status whose branch has no real commits.

    locked_touches() silently excludes these from the locked-files set when
    repo_root is passed; callers should still surface them loudly, since
    they reached this status via something other than a genuine completion
    and are sitting there unnoticed instead of blocking the board forever.
    """
    lock_set = set(lock_statuses)
    return [
        t
        for t in tickets
        if t.get("status") in lock_set
        and t.get("status") in _VERIFIABLE_LOCK_STATUSES
        and not _trusted_lock_holder(t, repo_root)
    ]


def _trusted_lock_holder(ticket: dict, repo_root: Path) -> bool:
    """Whether a code_complete/in_review ticket's lock should be honored.

    Only returns False when there's a worktree/branch recorded to check
    against and it has no real commits -- a ticket with neither field set
    (e.g. a synthetic/test ticket, or any status not actually reached via
    the normal executor/worktree flow) is left trusted rather than guessed
    at, since a real code_complete/in_review ticket always has both.
    """
    if not ticket.get("worktree") or not ticket.get("branch"):
        return True
    from lanegate.reviewer import worktree_has_commits

    return worktree_has_commits(ticket, repo_root)


def reread_and_assert_open(ticket_path: Path) -> dict:
    """
    Re-read the ticket file from disk immediately before claiming.
    Raises RuntimeError if the ticket is no longer open (grabbed by another session).
    Returns the fresh ticket dict on success.
    """
    fresh = parse_ticket(ticket_path)
    if fresh is None:
        raise RuntimeError(f"ticket file disappeared: {ticket_path}")
    if fresh.get("status") != "open":
        raise RuntimeError(f"ticket was grabbed by another session (now '{fresh.get('status')}')")
    return fresh


def check_local_not_behind_remote(repo_root: Path, branch: str) -> None:
    """
    Fetch and check whether the local ticket branch is behind its remote tracking branch.
    Raises RuntimeError if diverged (cross-clone race detected).
    Silently passes if the remote branch does not exist yet (first claim is safe).
    """
    # Fetch quietly; ignore errors (no remote, no network — not fatal)
    subprocess.run(
        ["git", "fetch", "--quiet", "origin", branch],
        cwd=repo_root,
        capture_output=True,
    )

    # Check if remote tracking ref exists
    check = subprocess.run(
        ["git", "rev-parse", "--verify", f"origin/{branch}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if check.returncode != 0:
        return  # remote branch doesn't exist — first claim, safe

    # Count commits behind
    behind = subprocess.run(
        ["git", "rev-list", "--count", f"HEAD..origin/{branch}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if behind.returncode == 0 and behind.stdout.strip() != "0":
        raise RuntimeError(
            f"local branch is behind origin/{branch} — another clone may have claimed this ticket. "
            f"Run 'git fetch && git pull' and retry."
        )
