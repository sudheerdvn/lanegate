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

import yaml

from lanegate.analyze import _STOPWORDS
from lanegate.config import load_config, resolve_trunk_branch

_DEFAULT_SIMILARITY_THRESHOLD = 0.6

# Frontmatter delimiter regex, mirroring ticket.py's parse_ticket -- reconciliation
# reads blobs via `git show`, not a real file on disk, so ticket.parse_ticket
# (which requires a Path) doesn't apply directly.
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)

# Frontmatter scalars that only the lifecycle machinery on trunk should ever
# set -- a ticket branch's stale copy must never clobber these during a
# metadata-only conflict reconciliation.
_LIFECYCLE_AUTHORITATIVE_KEYS = (
    "status",
    "review_verdict",
    "review_summary",
    "verification",
    "worktree",
    "branch",
    "pr_number",
    "status_changed_at",
)

_HISTORY_SECTION_HEADERS = ("## Status History", "## Lifecycle Timeline")


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


def audit_merged_ticket_status_tracking(
    repo_root: Path,
    all_tickets: list[dict],
    trunk_branch: str | None = None,
) -> list[dict]:
    """Flag tickets marked 'merged' whose branch is not actually reachable
    from trunk -- i.e. status was set to merged without the work ever
    landing on trunk (see TICK-365, which was marked merged with no merge
    commit or reachable branch tip).

    Returns a list of discrepancy records: {'ticket_id', 'branch', 'reason'}.
    "merged" is meant to be a hard guarantee about trunk state, so this check
    is deliberately strict (unlike the conservative supersession heuristics
    above): a missing branch, or an existing branch tip that is not an
    ancestor of trunk, is flagged UNLESS trunk-side commit evidence proves
    the work landed anyway (see `_find_merge_commit`). Neither a missing
    branch ref nor a drifted/rebased branch tip is flagged by itself --
    branches are routinely deleted as post-merge cleanup, and a branch can
    keep moving (rebase, follow-up commits reusing the same local branch)
    after its own contents were already merged into trunk -- so both cases
    fall back to the commit-level check before being treated as a
    discrepancy.
    """
    trunk_branch = trunk_branch or resolve_trunk_branch(load_config(repo_root), repo_root)
    discrepancies: list[dict] = []
    for ticket in all_tickets:
        if ticket.get("status") != "merged":
            continue
        ticket_id = ticket.get("id")
        branch = ticket.get("branch")
        if not branch:
            if ticket_id and _find_merge_commit(repo_root, trunk_branch, ticket_id):
                continue
            discrepancies.append({
                "ticket_id": ticket_id,
                "branch": None,
                "reason": "no branch recorded on a merged ticket and no merge commit found on trunk",
            })
            continue

        exists = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", branch],
            cwd=repo_root,
            capture_output=True,
            text=True, encoding="utf-8",
        )
        branch_reachable = False
        if exists.returncode == 0:
            ancestor = subprocess.run(
                ["git", "merge-base", "--is-ancestor", branch, trunk_branch],
                cwd=repo_root,
                capture_output=True,
                text=True, encoding="utf-8",
            )
            branch_reachable = ancestor.returncode == 0

        if branch_reachable:
            continue

        # Neither a missing branch ref nor a branch tip that has drifted past
        # trunk is proof by itself that the work never landed: the branch is
        # routinely deleted as post-merge cleanup once its work has landed,
        # and equally, a branch that keeps existing (and moving -- rebase,
        # unrelated follow-up commits) after the commits it *had* at merge
        # time were already integrated is no longer expected to be an
        # ancestor of trunk even though that integration was real. Trunk-side
        # commit evidence is exactly as strong a guarantee either way, so
        # fall back to it before flagging.
        if ticket_id and _find_merge_commit(repo_root, trunk_branch, ticket_id):
            continue
        reason = (
            "branch does not exist and no merge commit found on trunk"
            if exists.returncode != 0
            else "branch tip is not reachable from trunk and no merge commit found on trunk"
        )
        discrepancies.append({
            "ticket_id": ticket_id,
            "branch": branch,
            "reason": reason,
        })

    return discrepancies


def _find_merge_commit(repo_root: Path, trunk_branch: str, ticket_id: str) -> str | None:
    """Return a trunk commit hash proving `ticket_id` really landed, or None
    if trunk has no such commit -- used as the commit-level fallback when a
    merged ticket's branch ref is missing or its tip is no longer an
    ancestor of trunk.

    Matches either of the two commit subjects `cmd_merge` (lifecycle/__init__.py)
    writes to trunk when it finalizes a merge:
    - "Merge {ticket_id}: ..." from the real `git merge --no-ff`, when one ran.
    - "chore: {ticket_id} status → merged" from `_commit_generated_ticket_write`,
      which lifecycle only reaches after either that real merge succeeded, or
      `branch_reachable_from_main` confirmed (against this exact repo, at
      finalize time) that the branch's commits were already an ancestor of
      trunk -- the "already integrated" path that skips `git merge`
      entirely and so never writes a "Merge {ticket_id}:" commit at all.
    """
    result = subprocess.run(
        [
            "git", "log", trunk_branch,
            "--fixed-strings",
            f"--grep=Merge {ticket_id}:",
            f"--grep=chore: {ticket_id} status → merged",
            "--format=%H", "-n", "1",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def conflicted_paths(repo_root: Path) -> list[str]:
    """Return the repo-relative paths still unmerged during an in-progress
    git conflict.

    Must be called BEFORE ``git merge --abort`` -- abort clears the conflict
    state that this reads.
    """
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=repo_root,
        capture_output=True,
        text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def is_metadata_only_conflict(paths: list[str], tickets_dir: str) -> bool:
    """True iff every conflicted path is a LaneGate-owned ticket markdown
    file under `tickets_dir` -- never a source file."""
    if not paths:
        return False
    prefix = tickets_dir.replace("\\", "/").rstrip("/") + "/"
    return all(p.replace("\\", "/").startswith(prefix) and p.endswith(".md") for p in paths)


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Split a ticket file's raw text into (frontmatter dict, body)."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    meta = yaml.safe_load(match.group(1)) or {}
    return meta, match.group(2).strip()


def _extract_history_section(body: str, header: str) -> list[str]:
    if header not in body:
        return []
    after = body.split(header, 1)[1]
    next_heading = after.find("\n##")
    section = after if next_heading == -1 else after[:next_heading]
    return [line for line in section.splitlines() if line.strip()]


def _set_history_section(body: str, header: str, lines: list[str]) -> str:
    if not lines:
        return body
    if header not in body:
        return body.rstrip() + f"\n\n{header}\n" + "\n".join(lines) + "\n"
    before, _, after = body.partition(header)
    next_heading = after.find("\n##")
    tail = "" if next_heading == -1 else after[next_heading:]
    return before + header + "\n" + "\n".join(lines) + "\n" + tail


def _merge_history_sections(ours_body: str, theirs_body: str) -> str:
    """Concatenate each history section from both sides, ours first, deduping
    identical lines."""
    merged = ours_body
    for header in _HISTORY_SECTION_HEADERS:
        ours_lines = _extract_history_section(ours_body, header)
        theirs_lines = _extract_history_section(theirs_body, header)
        combined = list(ours_lines)
        for line in theirs_lines:
            if line not in combined:
                combined.append(line)
        merged = _set_history_section(merged, header, combined)
    return merged


def _merge_lifecycle_events(ours_meta: dict, theirs_meta: dict) -> list:
    seen: set[tuple] = set()
    merged: list = []
    for event in list(ours_meta.get("lifecycle_events") or []) + list(
        theirs_meta.get("lifecycle_events") or []
    ):
        key = (event.get("at"), event.get("event")) if isinstance(event, dict) else (event, None)
        if key in seen:
            continue
        seen.add(key)
        merged.append(event)
    return merged


def resolve_metadata_conflict(repo_root: Path, path: str) -> None:
    """Auto-resolve a merge conflict limited to a single LaneGate ticket
    file's frontmatter/history: trunk's lifecycle-authoritative fields win,
    keys unique to the incoming branch are kept, `lifecycle_events` is
    unioned, and the history body sections are concatenated from both sides.

    Writes the merged ticket text and stages it with `git add`, leaving the
    caller to complete the merge commit.
    """
    ours_text = subprocess.run(
        ["git", "show", f":2:{path}"],
        cwd=repo_root, capture_output=True, text=True, encoding="utf-8",
    ).stdout
    theirs_text = subprocess.run(
        ["git", "show", f":3:{path}"],
        cwd=repo_root, capture_output=True, text=True, encoding="utf-8",
    ).stdout

    ours_meta, ours_body = _split_frontmatter(ours_text)
    theirs_meta, theirs_body = _split_frontmatter(theirs_text)

    merged_meta: dict = dict(ours_meta)
    for key, value in theirs_meta.items():
        if key not in merged_meta:
            merged_meta[key] = value
    for key in _LIFECYCLE_AUTHORITATIVE_KEYS:
        if key in ours_meta:
            merged_meta[key] = ours_meta[key]

    merged_events = _merge_lifecycle_events(ours_meta, theirs_meta)
    if merged_events:
        merged_meta["lifecycle_events"] = merged_events

    merged_body = _merge_history_sections(ours_body, theirs_body)

    front = yaml.dump(merged_meta, default_flow_style=None, sort_keys=False, allow_unicode=True)
    (repo_root / path).write_text(f"---\n{front}---\n{merged_body}\n", encoding="utf-8")

    subprocess.run(["git", "add", path], cwd=repo_root, capture_output=True)
