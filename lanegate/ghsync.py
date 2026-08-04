"""
ghsync.py — mirror tickets to GitHub Issues (optional projection).

Design notes:
- Exact-match dedup: parses the JSON title field rather than substring matching,
  so `TICK-1` does not spuriously match `TICK-10`.
- Status propagation: updates existing issues on status change; closes on terminal status.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from lanegate.ticket import TERMINAL_STATUSES, load_all_tickets


def _gh_available() -> bool:
    try:
        return subprocess.run(["gh", "--version"], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


def _find_issue(ticket_id: str, repo_root: Path) -> dict | None:
    """Find an existing issue whose title starts with [ticket_id]. Returns dict or None."""
    r = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--search",
            ticket_id,
            "--json",
            "number,title,state",
            "--limit",
            "20",
        ],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    if r.returncode != 0:
        return None
    try:
        issues = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    # Exact-match: title must start with [ticket_id] (not a substring match)
    prefix = f"[{ticket_id}]"
    for issue in issues:
        if issue.get("title", "").startswith(prefix):
            return issue
    return None


def cmd_gh_sync(cfg: dict, repo_root: Path, *, dry_run: bool = False) -> None:
    if not _gh_available():
        print("gh CLI not available — skipping", file=sys.stderr)
        return

    if dry_run:
        print("  [dry-run] no issues will be created or modified\n")

    tickets_dir = repo_root / cfg["tickets_dir"]
    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)

    for t in tickets:
        ticket_id = t["id"]
        status = t.get("status", "unknown")
        title = f"[{ticket_id}] {t.get('title', '')}"
        body = (
            f"**Status:** {status}\n"
            f"**Batch:** {t.get('batch')}\n"
            f"**Priority:** {t.get('priority')}\n"
        )

        existing = _find_issue(ticket_id, repo_root)

        if existing is None:
            if status in TERMINAL_STATUSES:
                continue  # don't create issues for already-done tickets
            if dry_run:
                print(f"  {ticket_id}: would create — {title}")
                continue
            r = subprocess.run(
                ["gh", "issue", "create", "--title", title, "--body", body],
                capture_output=True,
                text=True,
                cwd=repo_root,
            )
            if r.returncode == 0:
                print(f"  {ticket_id}: created {r.stdout.strip()}")
            else:
                print(f"  {ticket_id}: FAILED — {r.stderr.strip()}", file=sys.stderr)
        else:
            number = existing["number"]
            if dry_run:
                if status in TERMINAL_STATUSES and existing.get("state") == "OPEN":
                    print(f"  {ticket_id}: would close #{number} ({status})")
                elif status not in TERMINAL_STATUSES and existing.get("state") == "CLOSED":
                    print(f"  {ticket_id}: would reopen #{number} ({status})")
                else:
                    print(f"  {ticket_id}: would update #{number}")
                continue
            # Update body to reflect current status
            subprocess.run(
                ["gh", "issue", "edit", str(number), "--title", title, "--body", body],
                capture_output=True,
                text=True,
                cwd=repo_root,
            )
            # Close if terminal
            if status in TERMINAL_STATUSES and existing.get("state") == "OPEN":
                subprocess.run(["gh", "issue", "close", str(number)], capture_output=True, cwd=repo_root)
                print(f"  {ticket_id}: closed #{number} ({status})")
            elif status not in TERMINAL_STATUSES and existing.get("state") == "CLOSED":
                subprocess.run(["gh", "issue", "reopen", str(number)], capture_output=True, cwd=repo_root)
                print(f"  {ticket_id}: reopened #{number} ({status})")
            else:
                print(f"  {ticket_id}: updated #{number}")
