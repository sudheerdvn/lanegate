"""Hibernation and recovery bookkeeping for the lifecycle package."""

from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from lanegate import APP_NAME
from lanegate.companion import companion_worktree_cleanup
from lanegate.config import load_config, resolve_trunk_branch
from lanegate.git import GitText
from lanegate.git import git_text as _git_text
from lanegate.pidutil import pid_alive, terminate_pid
from lanegate.ticket import (
    append_lifecycle_event,
    branch_name,
    canonical_id,
    load_all_tickets,
    write_ticket,
)
from lanegate.worktree import remove_worktree


def _stamp_status_changed(ticket: dict) -> None:
    """Write an ISO-8601 UTC timestamp into ticket['status_changed_at']."""
    ticket["status_changed_at"] = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _marker_base(repo_root: Path, tid: str) -> Path:
    # Canonical implementation lives in orchestrate/status.py as
    # _executor_marker_base; imported lazily to avoid a circular import
    # (orchestrate imports lifecycle at module level elsewhere).
    from lanegate.orchestrate.status import _executor_marker_base

    return _executor_marker_base(repo_root, tid)


def _remove_executor_markers(repo_root: Path, tid: str) -> None:
    # Canonical implementation lives in orchestrate/status.py; imported
    # lazily to avoid a circular import (orchestrate imports lifecycle at
    # module level elsewhere).
    from lanegate.orchestrate.status import _remove_executor_markers as _impl

    _impl(repo_root, tid)


def _write_executor_marker(repo_root: Path, tid: str, executor: str) -> None:
    base = _marker_base(repo_root, tid)
    _remove_executor_markers(repo_root, tid)
    if executor == "claude-subagent":
        base.with_suffix(".session").write_text(f"{time.time():.6f}\n")
    elif executor == "mcp":
        base.with_suffix(".mcp").write_text(f"{time.time():.6f}\n")
    else:
        base.with_suffix(".pid").write_text(f"{os.getpid()}\n")


def _recovery_path(repo_root: Path, tid: str) -> Path:
    return repo_root / f".{APP_NAME}" / "recovery" / f"{tid}.md"


def _remove_recovery_file(repo_root: Path, tid: str) -> None:
    try:
        _recovery_path(repo_root, tid).unlink()
    except FileNotFoundError:
        pass


def _cleanup_ticket_notes(ticket: dict, repo_root: Path) -> None:
    notes_dir = repo_root / f".{APP_NAME}" / "notes"
    if not notes_dir.is_dir():
        return
    tid_note = notes_dir / f"{ticket['id']}.md"
    try:
        tid_note.unlink()
    except (FileNotFoundError, OSError):
        pass



def _render_git_text(capture: GitText) -> str:
    """Render a Git-text result for durable hibernation recovery notes."""
    if not capture.ok:
        return f"(unavailable: {capture.error})"
    return capture.text or "(none)"


def _control_repo_root(repo_root: Path) -> Path:
    """Return the primary checkout for lifecycle state.

    LaneGate lifecycle state is control-plane data: tickets, locks, executor
    markers, and status commits should live on the primary checkout/branch. If a
    command is launched from a linked worktree such as ``worktrees/stage``, Git's
    common dir still points back to the primary checkout's ``.git`` directory.
    """
    root = Path(repo_root).resolve()
    if not (root / ".git").exists():
        return root
    result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=root,
        capture_output=True,
        text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        return root

    git_common_text = result.stdout.strip() if isinstance(result.stdout, str) else ""
    if not git_common_text:
        return root

    git_common = Path(git_common_text)
    if not git_common.is_absolute():
        git_common = (root / git_common).resolve()

    if git_common.name == ".git":
        return git_common.parent
    return root


def _append_ticket_section(ticket: dict, header: str, text: str) -> None:
    body = ticket.get("_body", "")
    if header in body:
        pre, _, rest = body.partition(header)
        after = rest.lstrip("\n")
        next_heading = after.find("\n##")
        replacement = pre.rstrip() + f"\n\n{header}\n\n{text.strip()}\n"
        ticket["_body"] = replacement if next_heading == -1 else replacement + after[next_heading:]
    else:
        ticket["_body"] = body.rstrip() + f"\n\n{header}\n\n{text.strip()}\n"


def _hibernation_note(
    ticket: dict,
    repo_root: Path,
    reason: str = "",
    *,
    include_diff: bool = False,
    trunk_branch: str | None = None,
    escalation: bool = False,
) -> tuple[str, bool, bool]:
    """Generate hibernation note.

    Returns ``(note_text, was_truncated, capture_failed)``.

    ``capture_failed`` is distinct from truncation so reset callers do not
    mistake an unavailable Git capture for an empty successful diff.

    ``escalation=True`` (TICK-467: red-lane risk trigger or exhausted
    auto-fix retry budget) renders an explicit resume command in addition
    to the usual git-log/diff pointers, since a human — not the next
    orchestrate run — is expected to act on this ticket.
    """
    tid = ticket["id"]
    wt = Path(ticket["worktree"]) if ticket.get("worktree") else None
    cwd = wt if wt and wt.exists() else repo_root
    branch = ticket.get("branch") or branch_name(tid)
    trunk_branch = trunk_branch or resolve_trunk_branch(load_config(repo_root), repo_root)
    close_criteria = ticket.get("close_criteria", "")
    if isinstance(close_criteria, list):
        close_criteria = "\n".join(f"- {item}" for item in close_criteria)

    reason_text = reason.strip() or "interrupted executor session"
    note = f"""## Hibernated partial work - {tid}

This ticket was interrupted ({reason_text}). Partial work may exist.
Do not start from scratch; continue from where it stopped.
"""

    was_truncated = False
    capture_failed = False
    if include_diff:
        commit_log_capture = _git_text(
            ["git", "log", f"{trunk_branch}..{branch}", "--oneline"], cwd
        )
        diff_capture = _git_text(["git", "diff", f"{trunk_branch}...{branch}"], cwd)
        capture_failed = not commit_log_capture.ok or not diff_capture.ok
        commit_log = _render_git_text(commit_log_capture)
        diff = _render_git_text(diff_capture)
        if diff_capture.ok and len(diff_capture.text) > _MAX_DIFF_BYTES:
            was_truncated = True
            diff = (
                diff_capture.text[:_MAX_DIFF_BYTES]
                + f"\n... (diff truncated at {_MAX_DIFF_BYTES // 1000} KB)"
            )
        uncommitted = "(none)"
        if wt and wt.exists():
            uncommitted_capture = _git_text(["git", "diff", "HEAD"], wt)
            capture_failed = capture_failed or not uncommitted_capture.ok
            uncommitted = _render_git_text(uncommitted_capture)
            if uncommitted_capture.ok and len(uncommitted_capture.text) > _MAX_DIFF_BYTES:
                was_truncated = True
                uncommitted = (
                    uncommitted_capture.text[:_MAX_DIFF_BYTES]
                    + f"\n... (truncated at {_MAX_DIFF_BYTES // 1000} KB)"
                )

        note += f"""

### What was committed before interruption
{commit_log}

### Full diff vs {trunk_branch}
{diff}
"""
        if uncommitted != "(none)":
            note += f"""
### Uncommitted worktree diff
{uncommitted}
"""
        note += """
### What likely remains
Review the diff above against the close criteria below.
"""
    else:
        commit_count_capture = _git_text(
            ["git", "rev-list", "--count", f"{trunk_branch}..{branch}"], cwd
        )
        capture_failed = not commit_count_capture.ok
        commit_count = _render_git_text(commit_count_capture)
        worktree_text = str(wt) if wt else "(worktree path not recorded)"
        note += f"""

### Resume location
Worktree: {worktree_text}
Branch: {branch}
Commits already made: {commit_count}

The worktree and branch were preserved. Inspect the live state there with
`git log {trunk_branch}..{branch} --oneline`, `git diff {trunk_branch}...{branch}`, and
`git diff HEAD` rather than relying on a stale embedded diff.

### What likely remains
Review the preserved worktree against the close criteria below.
"""

    if capture_failed:
        note += """
### Git capture warning
One or more Git captures failed. The failure diagnostics above are preserved here;
the branch and its metadata must remain available for recovery.
"""

    if escalation:
        escalation_reason = reason.strip() or "risk-based autonomy lane requires human review"
        note += f"""
### Human escalation ({escalation_reason})
This ticket was escalated for human review rather than continuing the
automatic fix/re-review loop. The branch `{branch}` is preserved — it was
not deleted or reset.

To resume after review:
- Inspect the change: `git log {trunk_branch}..{branch} --oneline` and `git diff {trunk_branch}...{branch}`
- Make any needed edits directly on `{branch}`, or address the escalation reason
- Resume orchestration: `lanegate start {tid}` (or `lanegate run` to pick it up with the rest of the board)
"""

    note += f"""
Implement only what is missing.

Close criteria: {close_criteria}
"""
    return note, was_truncated, capture_failed


_MAX_DIFF_BYTES = 30_000  # truncate diffs larger than this in hibernation notes


def _write_hibernation_notes(
    ticket: dict,
    repo_root: Path,
    reason: str = "",
    *,
    include_diff: bool = False,
    trunk_branch: str | None = None,
    escalation: bool = False,
) -> tuple[bool, bool]:
    """Write hibernation notes to recovery file.

    Returns ``(was_truncated, capture_failed)``.
    """
    recovery_path = _recovery_path(repo_root, ticket["id"])
    recovery_path.parent.mkdir(parents=True, exist_ok=True)
    note, was_truncated, capture_failed = _hibernation_note(
        ticket,
        repo_root,
        reason=reason,
        include_diff=include_diff,
        trunk_branch=trunk_branch,
        escalation=escalation,
    )
    recovery_path.write_text(note.rstrip() + "\n")
    return was_truncated, capture_failed


def _push_branch_and_open_pr(
    repo_root: Path,
    branch: str,
    ticket: dict,
    base: str | None = None,
) -> tuple[int, str] | None:
    """Push branch to origin and open a GitHub PR.

    Returns (pr_number, pr_url) on success, or None if skipped/failed.
    Fails silently — the local workflow must still work when gh is absent or
    there is no remote.
    """
    if not shutil.which("gh"):
        return None

    # Check that a remote exists.
    r = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=repo_root,
        capture_output=True,
        text=True, encoding="utf-8",
    )
    if r.returncode != 0:
        return None

    # Push branch to origin (--force-with-lease on re-push is safe enough).
    push = subprocess.run(
        ["git", "push", "--force-with-lease", "-u", "origin", branch],
        cwd=repo_root,
        capture_output=True,
        text=True, encoding="utf-8",
    )
    if push.returncode != 0:
        print(f"WARNING: could not push {branch} to origin: {push.stderr.strip()}", file=sys.stderr)
        return None

    tid = ticket["id"]
    title = ticket.get("title", tid)

    # Check if a PR already exists for this branch.
    view = subprocess.run(
        ["gh", "pr", "view", branch, "--json", "url,number"],
        cwd=repo_root,
        capture_output=True,
        text=True, encoding="utf-8",
    )
    if view.returncode == 0 and view.stdout.strip():
        try:
            data = json.loads(view.stdout)
            return int(data["number"]), data["url"]
        except (KeyError, ValueError, json.JSONDecodeError):
            pass  # Fall through to create a new PR

    base = base or resolve_trunk_branch(load_config(repo_root), repo_root)

    # Create the PR.
    body = f"Ticket: {tid}\n\n{title}"
    create = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--base",
            base,
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
        ],
        cwd=repo_root,
        capture_output=True,
        text=True, encoding="utf-8",
    )
    if create.returncode != 0:
        print(f"WARNING: gh pr create failed: {create.stderr.strip()}", file=sys.stderr)
        return None

    pr_url = create.stdout.strip()
    if not pr_url:
        print("WARNING: gh pr create returned no URL", file=sys.stderr)
        return None

    # Parse the PR number from the URL (e.g. https://github.com/org/repo/pull/42).
    try:
        pr_number = int(pr_url.rstrip("/").rsplit("/", 1)[-1])
    except (ValueError, IndexError):
        print(f"WARNING: could not parse PR number from URL: {pr_url}", file=sys.stderr)
        return None

    return pr_number, pr_url


def cmd_hibernate(
    ticket_id: str,
    cfg: dict,
    repo_root: Path,
    *,
    reset: bool = False,
    reason: str = "",
    escalation: bool = False,
) -> None:
    """Transition in_progress|code_complete -> hibernated and write resumable
    context notes.

    ``code_complete`` is accepted alongside ``in_progress`` because the
    auto-fix cycle (run_auto_fix_cycle in orchestrate/autofix.py) calls this
    on a rate-limited fix pass, where the ticket is always code_complete with
    review_verdict=changes_requested at that point in the loop, never
    in_progress — rejecting that status here would raise SystemExit out of
    cmd_orchestrate and abort the rest of the batch.

    Preserves the worktree if the ticket has review_verdict=changes_requested,
    even if reset=True, to allow human inspection and fixes.

    ``escalation=True`` (TICK-467: a red-lane risk trigger or an exhausted
    auto-fix retry budget) always preserves the branch — even under
    ``reset=True`` — and renders an explicit resume command in the
    hibernation note, since a human is expected to act on it directly.
    """
    from . import _commit_generated_ticket_write

    repo_root = _control_repo_root(repo_root)
    tid = canonical_id(ticket_id)
    tickets_dir = repo_root / cfg["tickets_dir"]
    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    ticket = next((t for t in tickets if t["id"] == tid), None)
    if not ticket:
        print(f"ERROR: {tid} not found", file=sys.stderr)
        sys.exit(1)

    current = ticket.get("status")
    if current not in ("in_progress", "code_complete"):
        print(
            f"ERROR: {tid} is '{current}', expected in_progress or code_complete",
            file=sys.stderr,
        )
        sys.exit(1)

    wt = ticket.get("worktree")
    branch = ticket.get("branch") or branch_name(tid)

    # Skip reset if ticket has review_verdict=changes_requested to preserve for inspection
    skip_reset = reset and ticket.get("review_verdict") == "changes_requested"
    if skip_reset:
        print(
            f"[lifecycle] skipping worktree cleanup for {tid} (review_verdict=changes_requested) — preserved for inspection",
            file=sys.stderr,
        )

    was_diff_truncated, capture_failed = _write_hibernation_notes(
        ticket,
        repo_root,
        reason=reason,
        include_diff=reset and not skip_reset,
        trunk_branch=resolve_trunk_branch(cfg, repo_root),
        escalation=escalation,
    )
    if reason:
        _append_ticket_section(ticket, "## Hibernation Reason", reason)

    if reset and not skip_reset:
        if wt:
            wt_path = Path(wt)
            if wt_path.exists():
                from lanegate.config import protected_branches

                protected = protected_branches(cfg)
                try:
                    remove_worktree(repo_root, wt_path, protected)
                except PermissionError as e:
                    print(f"WARNING: {e}", file=sys.stderr)
        # Failed or truncated captures leave recovery evidence only in the
        # branch; an escalation (red-lane trigger / exhausted retry budget)
        # always preserves the branch for human review, same as those.
        preserve_branch = was_diff_truncated or capture_failed or escalation
        if branch and not preserve_branch:
            subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=repo_root,
                capture_output=True,
                text=True, encoding="utf-8",
            )
        for companion in ticket.get("companion_repos") or []:
            companion_worktree_cleanup(repo_root, companion, tid)
        if branch and was_diff_truncated:
            print(
                f"[lifecycle] preserving branch {branch} for {tid} (diff was truncated) — full work is only in the branch",
                file=sys.stderr,
            )
        elif branch and capture_failed:
            print(
                f"[lifecycle] preserving branch {branch} for {tid} (Git capture failed) — recovery diagnostics are in the hibernation note",
                file=sys.stderr,
            )
        elif branch and escalation:
            print(
                f"[lifecycle] preserving branch {branch} for {tid} (escalated for human review) — "
                f"resume with `lanegate start {tid}`",
                file=sys.stderr,
            )
        if not preserve_branch:
            ticket["branch"] = None
        ticket["worktree"] = None

    ticket["status"] = "hibernated"
    _stamp_status_changed(ticket)
    append_lifecycle_event(
        ticket,
        event="hibernated",
        from_status=current,
        to_status="hibernated",
        summary=reason or "work paused for later recovery",
    )
    write_ticket(ticket)
    _remove_executor_markers(repo_root, tid)
    _commit_generated_ticket_write(repo_root, ticket["_path"], tid, "hibernated", cfg)

    print(f"{tid}: {current} -> hibernated")
    if reset and not skip_reset:
        print("  worktree and branch reset")


def cmd_stop(
    ticket_id: str,
    cfg: dict,
    repo_root: Path,
    *,
    reason: str = "",
    grace_seconds: float = 5.0,
) -> dict:
    """Stop one ticket's executor and preserve its work for later recovery.

    The durable PID marker is ticket-scoped by design.  Do not broaden this to
    a marker glob: shared lifecycle markers such as ``lock.pid`` and
    ``watch.pid`` must never be eligible for an operator stop request.
    """
    repo_root = _control_repo_root(repo_root)
    tid = canonical_id(ticket_id)
    tickets_dir = repo_root / cfg["tickets_dir"]
    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    ticket = next((t for t in tickets if t["id"] == tid), None)
    if not ticket:
        print(f"ERROR: {tid} not found", file=sys.stderr)
        sys.exit(1)

    pid_path = _marker_base(repo_root, tid).with_suffix(".pid")
    if not pid_path.exists():
        print(f"[lifecycle] {tid}: no live executor (no PID marker found)", file=sys.stderr)
        return {"ticket_id": tid, "stopped": False, "pid": None, "reason": "no_pid_marker"}

    try:
        pid = int(pid_path.read_text().strip())
    except ValueError:
        print(f"[lifecycle] {tid}: no live executor (unreadable PID marker)", file=sys.stderr)
        return {
            "ticket_id": tid,
            "stopped": False,
            "pid": None,
            "reason": "unreadable_pid_marker",
        }

    if not pid_alive(pid):
        print(f"[lifecycle] {tid}: no live executor (PID {pid} already gone)", file=sys.stderr)
        _remove_executor_markers(repo_root, tid)
        return {"ticket_id": tid, "stopped": False, "pid": pid, "reason": "already_gone"}

    stop_reason = reason or f"stopped by operator: terminated executor PID {pid}"
    if not terminate_pid(pid):
        if pid_alive(pid):
            print(f"ERROR: permission denied terminating executor PID {pid}", file=sys.stderr)
            sys.exit(1)
        print(f"[lifecycle] {tid}: no live executor (PID {pid} already gone)", file=sys.stderr)
        _remove_executor_markers(repo_root, tid)
        return {"ticket_id": tid, "stopped": False, "pid": pid, "reason": "already_gone"}

    deadline = time.time() + grace_seconds
    while time.time() < deadline and pid_alive(pid):
        time.sleep(0.1)

    if ticket.get("status") == "in_progress":
        cmd_hibernate(tid, cfg, repo_root, reason=stop_reason)
    _remove_executor_markers(repo_root, tid)

    exited = not pid_alive(pid)
    state = "exited" if exited else "still running after grace period"
    print(f"[lifecycle] {tid}: terminate signal sent to executor PID {pid}; {state}", file=sys.stderr)
    return {
        "ticket_id": tid,
        "stopped": True,
        "pid": pid,
        "exited": exited,
        "reason": stop_reason,
    }
