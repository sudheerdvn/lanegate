"""
create.py — allocate a new ticket id and write a draft ticket file.

Mechanical only — no LLM call. Analysis is a separate step (analyze.py),
chained by default at the CLI layer (TICK-004).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from lanegate.concurrency import claim_lock
from lanegate.ticket import load_all_tickets, ticket_glob, validate_ticket, write_ticket


def _next_id(tickets_dir: Path, prefix: str) -> str:
    """Return the next ticket id, zero-padded to at least 3 digits."""
    existing = ticket_glob(tickets_dir, prefix)
    pat = re.compile(rf"^{re.escape(prefix)}-(\d+)(?:-[^/]*)?\.md$", re.IGNORECASE)
    nums = []
    for p in existing:
        m = pat.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    n = (max(nums) + 1) if nums else 1
    width = max(3, len(str(n)))
    return f"{prefix}-{n:0{width}d}"


_SENTENCE_END_RE = re.compile(r"[.!?](?:\s|$)")


def _derive_title(intent: str, max_len: int = 80) -> str:
    """Derive a ticket title from a free-form intent.

    Uses only the intent's first sentence (not the leading N characters of a
    multi-sentence description), then truncates to max_len on a word
    boundary rather than mid-word. Falls back to a hard cutoff only when no
    word boundary exists within max_len (e.g. one unbroken token).
    """
    first_line = intent.split("\n", 1)[0].strip()
    match = _SENTENCE_END_RE.search(first_line)
    candidate = first_line[: match.start() + 1] if match else first_line

    if len(candidate) <= max_len:
        return candidate

    truncated = candidate[:max_len]
    if candidate[max_len] == " ":
        # Cutoff already lands exactly at a word boundary.
        return truncated
    last_space = truncated.rfind(" ")
    return truncated[:last_space] if last_space > 0 else truncated


def _discover_milestones(tickets_dir: Path, prefix: str) -> list[str]:
    """Return sorted unique milestone values used across existing tickets."""
    try:
        tickets, _ = load_all_tickets(tickets_dir, prefix)
    except Exception:
        return []
    milestones: dict[str, int] = {}
    for t in tickets:
        ms = t.get("milestone")
        if ms:
            milestones[ms] = milestones.get(ms, 0) + 1
    # Sort by frequency descending, then alphabetically
    return sorted(milestones, key=lambda m: (-milestones[m], m))


def _prompt_milestone(tickets_dir: Path, prefix: str, default_milestone: str | None) -> str | None:
    """Interactively prompt for a milestone value.

    Returns the chosen milestone string, or None if the user accepts blank.
    """
    existing = _discover_milestones(tickets_dir, prefix)
    suggestions = ", ".join(existing) if existing else "(none yet)"

    # Determine the suggested default shown in brackets
    bracket_default = default_milestone or (existing[0] if existing else "")

    prompt_str = f"milestone (existing: {suggestions})"
    if bracket_default:
        prompt_str += f" [{bracket_default}]"
    prompt_str += ": "

    try:
        raw = input(prompt_str).strip()
    except (EOFError, KeyboardInterrupt):
        return None

    if not raw:
        if bracket_default:
            return bracket_default
        print(
            "Tip: untagged tickets are excluded from --milestone runs.",
            file=sys.stderr,
        )
        return None

    return raw


def cmd_create(intent: str, cfg: dict, repo_root: Path, milestone: str | None = None) -> str:
    """Write a draft ticket for the given intent. Returns the new ticket id.

    Args:
        intent: natural-language description of the ticket
        cfg: loaded config dict
        repo_root: repository root path
        milestone: explicit milestone string; when None, falls back to
                   cfg['default_milestone'], then interactive prompt.
    """
    prefix = cfg["ticket_prefix"]
    tickets_dir = repo_root / cfg["tickets_dir"]
    tickets_dir.mkdir(parents=True, exist_ok=True)

    # Resolve milestone
    resolved_milestone: str | None = milestone
    if resolved_milestone is None:
        resolved_milestone = cfg.get("default_milestone") or None
    if resolved_milestone is None:
        # Interactive prompt only when stdin is a TTY
        if sys.stdin.isatty():
            resolved_milestone = _prompt_milestone(
                tickets_dir, prefix, cfg.get("default_milestone")
            )
        else:
            print(
                "Tip: untagged tickets are excluded from --milestone runs.",
                file=sys.stderr,
            )

    with claim_lock(repo_root):
        ticket_id = _next_id(tickets_dir, prefix)
        path = tickets_dir / f"{ticket_id}.md"

        ticket: dict = {
            "id": ticket_id,
            "title": _derive_title(intent),
            "status": "draft",
            "priority": 5,
            "touches": [],
            "parallel_safe": True,
            # Keep the per-ticket field explicit, but seed it from the
            # project policy so a configured `autonomy: full` also applies to
            # tickets created later.  Existing configs retain supervised mode.
            "autonomy": cfg.get("autonomy", "supervised"),
            "_path": path,
            "_body": f"## Background\n{intent}",
        }
        if resolved_milestone:
            ticket["milestone"] = resolved_milestone

        errors = validate_ticket(
            {k: v for k, v in ticket.items() if not k.startswith("_")}, cfg
        )
        if errors:
            for e in errors:
                print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)

        write_ticket(ticket)

        if cfg.get("commit_status_changes", True):
            subprocess.run(
                ["git", "add", "-f", str(path)],
                cwd=repo_root,
                check=False,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "--only", str(path), "-m", f"chore: create draft ticket {ticket_id}"],
                cwd=repo_root,
                check=False,
                capture_output=True,
            )

    print(ticket_id)
    return ticket_id
