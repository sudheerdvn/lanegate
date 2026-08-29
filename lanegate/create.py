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
from lanegate.git import has_tracking_remote as _has_tracking_remote
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
    boundary rather than mid-word. Truncated titles end in an ellipsis, so
    the title never silently presents an incomplete thought as the full
    intent. Falls back to a hard cutoff only when no word boundary exists
    within max_len (e.g. one unbroken token).
    """
    first_line = intent.split("\n", 1)[0].strip()
    match = _SENTENCE_END_RE.search(first_line)
    candidate = first_line[: match.start() + 1] if match else first_line

    if len(candidate) <= max_len:
        return candidate

    # Reserve one character for a visible truncation marker.  A max_len of
    # one is still useful for callers that need a strict fixed-width label.
    if max_len <= 1:
        return "…"[:max_len]
    truncated = candidate[: max_len - 1].rstrip()
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space].rstrip()
    return truncated + "…"


def _discover_milestones(
    tickets_dir: Path, prefix: str, cfg: dict | None = None, *, raise_on_error: bool = False
) -> list[str]:
    """Return sorted unique milestone values used across existing tickets.

    An empty result means no ticket in the tickets dir uses the milestone
    field at all -- callers elsewhere (e.g. orchestrate's milestone-scope
    probe) rely on that as well as this function's own milestone-prompt
    suggestions, so keep both reading from this one scan.

    raise_on_error controls how a load failure (missing/unreadable
    tickets_dir, etc.) is handled. Default False degrades to an empty list,
    fine for this function's original caller (an interactive suggestion
    list -- silently showing no suggestions is harmless). A caller using
    the result for a correctness-critical decision -- like orchestrate's
    milestone-scope probe deciding whether a bare `lanegate run` is safe to
    treat as `--all` -- must pass True instead: silently treating a real
    load failure as "no milestones in use" would let that decision proceed
    on wrong information instead of surfacing the underlying problem.
    """
    try:
        tickets, _ = load_all_tickets(tickets_dir, prefix, cfg)
    except Exception:
        if raise_on_error:
            raise
        return []
    milestones: dict[str, int] = {}
    for t in tickets:
        ms = t.get("milestone")
        if ms:
            milestones[ms] = milestones.get(ms, 0) + 1
    # Sort by frequency descending, then alphabetically
    return sorted(milestones, key=lambda m: (-milestones[m], m))


def _prompt_milestone(
    tickets_dir: Path, prefix: str, default_milestone: str | None, cfg: dict | None = None
) -> str | None:
    """Interactively prompt for a milestone value.

    Returns the chosen milestone string, or None if the user accepts blank.
    """
    existing = _discover_milestones(tickets_dir, prefix, cfg)
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


def cmd_create(
    intent: str,
    cfg: dict,
    repo_root: Path,
    milestone: str | None = None,
    title: str | None = None,
    autonomy: str | None = None,
    touches: list[str] | None = None,
    depends_on: list[str] | None = None,
) -> str:
    """Write a draft ticket for the given intent. Returns the new ticket id.

    Args:
        intent: natural-language description of the ticket
        cfg: loaded config dict
        repo_root: repository root path
        milestone: explicit milestone string; when None, falls back to
                   cfg['default_milestone'], then interactive prompt.
        title: explicit board title. When None, derive a concise title from
               the intent for backwards compatibility.
        autonomy: per-ticket autonomy override. When None, use the project
                   default (or ``supervised`` when it is not configured).
        touches: files this draft ticket is allowed to modify.
        depends_on: ticket IDs which must complete before this ticket starts.
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
                tickets_dir, prefix, cfg.get("default_milestone"), cfg
            )
        else:
            print(
                "Tip: untagged tickets are excluded from --milestone runs.",
                file=sys.stderr,
            )

    has_remote = _has_tracking_remote(repo_root)
    commit_status = cfg.get("commit_status_changes", True)
    max_retries = 10

    with claim_lock(repo_root):
        for attempt in range(max_retries):
            if has_remote:
                subprocess.run(
                    ["git", "fetch", "--quiet"],
                    cwd=repo_root,
                    check=False,
                    capture_output=True,
                )
                merge_base_res = subprocess.run(
                    ["git", "merge-base", "--is-ancestor", "@{u}", "HEAD"],
                    cwd=repo_root,
                    check=False,
                )
                if merge_base_res.returncode != 0:
                    rebase_res = subprocess.run(
                        ["git", "rebase", "--autostash", "--quiet", "@{u}"],
                        cwd=repo_root,
                        check=False,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                    )
                    if rebase_res.returncode != 0:
                        subprocess.run(
                            ["git", "rebase", "--abort"],
                            cwd=repo_root,
                            check=False,
                            capture_output=True,
                        )
                        raise RuntimeError(
                            f"Failed to rebase onto upstream tracking branch '@{{u}}': {rebase_res.stderr or rebase_res.stdout}"
                        )

            ticket_id = _next_id(tickets_dir, prefix)
            path = tickets_dir / f"{ticket_id}.md"

            ticket: dict = {
                "id": ticket_id,
                # An explicit title is intentionally never shortened: the board
                # folds its title column, while the full title remains available
                # in both board JSON and the ticket frontmatter.
                "title": title.strip() if title is not None else _derive_title(intent),
                "status": "draft",
                "priority": 5,
                "touches": list(touches or []),
                "parallel_safe": True,
                # Keep the per-ticket field explicit, but seed it from the
                # project policy so a configured `autonomy: full` also applies to
                # tickets created later.  Existing configs retain supervised mode.
                "autonomy": autonomy if autonomy is not None else cfg.get("autonomy", "supervised"),
                "_path": path,
                "_body": (
                    f"## Background\n{intent}\n\n"
                    "## Acceptance Criteria\n"
                    "- [ ] \n\n"
                    "## Non-Goals\n\n"
                    "## Technical Notes & Invariants\n"
                ),
            }
            if depends_on:
                ticket["depends_on"] = list(depends_on)
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

            if commit_status:
                subprocess.run(
                    ["git", "add", "-f", str(path)],
                    cwd=repo_root,
                    check=False,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "commit", "-s", "--only", str(path), "-m", f"chore: create draft ticket {ticket_id}"],
                    cwd=repo_root,
                    check=False,
                    capture_output=True,
                )

                if has_remote:
                    push_res = subprocess.run(
                        ["git", "push"],
                        cwd=repo_root,
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    if push_res.returncode != 0:
                        # Remote push rejected due to concurrent creation on remote.
                        # Roll back local commit without destroying uncommitted working tree files.
                        subprocess.run(
                            ["git", "reset", "--mixed", "HEAD~1"],
                            cwd=repo_root,
                            check=False,
                            capture_output=True,
                        )
                        path.unlink(missing_ok=True)
                        if attempt == max_retries - 1:
                            raise RuntimeError(
                                f"Failed to push draft ticket {ticket_id} after {max_retries} attempts: {push_res.stderr}"
                            )
                        continue

            break

    print(ticket_id)
    return ticket_id
