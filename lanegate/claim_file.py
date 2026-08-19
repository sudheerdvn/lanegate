"""
claim_file.py — add a file path to a ticket's touches list.

Checks that the file is not already locked by another ticket in a lock
status (TOCTOU-safe), then appends it (deduplicating) and commits the
updated ticket file.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lanegate.concurrency import claim_lock, touches_overlap
from lanegate.lifecycle import _control_repo_root
from lanegate.ticket import (
    canonical_id,
    load_all_tickets,
    parse_ticket,
    write_ticket,
)


def claim_files(
    file_paths: list[str], ticket_id: str, cfg: dict, repo_root: Path
) -> tuple[bool, str | None]:
    """Atomically add *file_paths* to a ticket's touches list.

    Returns ``(True, None)`` on success.  On a missing ticket or conflicting
    lock it leaves the ticket untouched and returns ``(False, detail)``.  The
    non-exiting form lets the orchestrator safely attempt automatic recovery
    without partially claiming a multi-file scope expansion.
    """
    requested = sorted({path for path in file_paths if path})
    if not requested:
        return True, None
    repo_root = _control_repo_root(repo_root)
    prefix = cfg["ticket_prefix"]
    tickets_dir = repo_root / cfg["tickets_dir"]
    lock_statuses: list[str] = cfg.get(
        "lock_statuses", ["in_progress", "code_complete", "in_review"]
    )

    # Resolve the canonical ticket id we're extending.
    target_id = canonical_id(ticket_id)

    # Serialize the conflict check and touches update: under this flock, both the
    # re-read of all tickets and the target ticket, plus the status write, are
    # atomic against any other `lanegate claim-file` or `lanegate start` call.
    with claim_lock(repo_root):
        # TOCTOU re-read: refresh all tickets and the target ticket inside the lock.
        fresh_tickets, _ = load_all_tickets(tickets_dir, prefix, cfg)

        # Find the target ticket again with fresh data.
        fresh_target = None
        for t in fresh_tickets:
            try:
                if canonical_id(t.get("id", "")) == target_id:
                    fresh_target = t
                    break
            except ValueError:
                continue

        if fresh_target is None:
            return False, f"ticket {target_id} not found in {tickets_dir}"

        # Check every requested path before modifying the ticket, so a conflict
        # cannot leave a multi-file claim only partly applied.
        for t in fresh_tickets:
            try:
                is_self = canonical_id(t.get("id", "")) == target_id
            except ValueError:
                is_self = False
            if is_self:
                continue  # skip our own ticket
            if t.get("status") in lock_statuses:
                locked_touches = t.get("touches") or []
                if touches_overlap(locked_touches, requested):
                    conflicts = sorted(set(locked_touches) & set(requested)) or ["*"]
                    blocking_id = t.get("id", "<unknown>")
                    return (
                        False,
                        f"{', '.join(conflicts)} is already locked by {blocking_id} "
                        f"(status: {t.get('status')})",
                    )

        # TOCTOU safety: re-read the target ticket from disk inside the lock.
        ticket_path: Path = fresh_target["_path"]
        fresh = parse_ticket(ticket_path)
        if fresh is None:
            return False, f"ticket file disappeared: {ticket_path}"

        # Add every requested path, deduplicating.
        current_touches: list[str] = fresh.get("touches") or []
        current_touches = current_touches + [
            path for path in requested if path not in current_touches
        ]
        fresh["touches"] = current_touches

        write_ticket(fresh)

        if cfg.get("commit_status_changes", True):
            subprocess.run(
                [
                    "git",
                    "commit",
                    "-s",
                    "--only",
                    str(ticket_path),
                    "-m",
                    f"chore: {target_id} claim files",
                ],
                cwd=repo_root,
                check=False,
                capture_output=True,
            )

    return True, None


def cmd_claim_file(file_path: str, ticket_id: str, cfg: dict, repo_root: Path) -> None:
    """Add one file path to a ticket's touches list.

    This CLI-compatible wrapper retains the historical exit-on-conflict
    behavior while sharing the atomic multi-file implementation used by the
    automatic scope-recovery path.
    """
    ok, detail = claim_files([file_path], ticket_id, cfg, repo_root)
    if not ok:
        print(f"ERROR: {detail}", file=sys.stderr)
        sys.exit(1)
    print(f"{canonical_id(ticket_id)}: added {file_path} to touches")
