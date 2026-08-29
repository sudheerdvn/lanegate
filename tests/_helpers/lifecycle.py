"""Shared fixtures and helpers for lifecycle command tests."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
from unittest.mock import MagicMock


def default_cfg(tickets_dir, worktrees_dir):
    return {
        "ticket_prefix": "TICK",
        "tickets_dir": str(tickets_dir.name),
        "worktrees_dir": str(worktrees_dir.name),
        "lock_statuses": ["in_progress", "code_complete", "in_review"],
        "commit_status_changes": False,
        "environments": [],
    }


def write_ticket(
    tickets_dir: Path,
    ticket_id: str,
    status: str,
    worktree=None,
    branch=None,
    review_verdict=None,
    companion_repos=None,
    touches=None,
):
    content = f"---\nid: {ticket_id}\ntitle: Test {ticket_id}\nstatus: {status}\n"
    if worktree:
        content += f"worktree: {worktree}\n"
    if branch:
        content += f"branch: {branch}\n"
    if review_verdict:
        content += f"review_verdict: {review_verdict}\n"
    if touches:
        content += "touches:\n"
        for t in touches:
            content += f"  - {t}\n"
    if companion_repos:
        content += "companion_repos:\n"
        for c in companion_repos:
            content += f"  - {c}\n"
    content += "---\nBody.\n"
    path = tickets_dir / f"{ticket_id}.md"
    path.write_text(content)
    return path


def init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)


def commit_all(path: Path, message: str = "init") -> None:
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=path, check=True, capture_output=True)


def tracked_path_is_clean(repo: Path, path: Path) -> bool:
    r = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", str(path)],
        cwd=repo,
        capture_output=True,
    )
    return r.returncode == 0


def start_cfg(tmp_path, *, commit_status_changes=False):
    """Minimal config for cmd_start tests."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir(exist_ok=True)
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir(exist_ok=True)
    return {
        "ticket_prefix": "TICK",
        "tickets_dir": str(tickets_dir),
        "worktrees_dir": str(worktrees_dir),
        "lock_statuses": ["in_progress", "code_complete", "in_review"],
        "commit_status_changes": commit_status_changes,
        "environments": [],
    }


def write_ticket_with_body(tickets_dir: Path, ticket_id: str, status: str, body: str, **kwargs) -> Path:
    """Like _write_ticket, but with a caller-supplied body (for Acceptance
    Criteria checklists) and passthrough of extra frontmatter kwargs
    (e.g. review_verdict, verification)."""
    lines = [f"---", f"id: {ticket_id}", f"title: Test {ticket_id}", f"status: {status}"]
    for key, value in kwargs.items():
        if value is None:
            continue
        if key == "touches" and isinstance(value, list):
            lines.append("touches:")
            lines.extend(f"  - {t}" for t in value)
        elif key == "verification" and isinstance(value, list):
            lines.append("verification:")
            for rec in value:
                lines.append(f"  - criterion: {rec['criterion']!r}")
                lines.append(f"    status: {rec['status']}")
                lines.append(f"    evidence: {rec.get('evidence', '')!r}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append(body)
    path = tickets_dir / f"{ticket_id}.md"
    path.write_text("\n".join(lines) + "\n")
    return path


def is_iso_utc(s: str) -> bool:
    """True if the string looks like a UTC ISO-8601 timestamp (YYYY-MM-DDTHH:MM:SSZ)."""
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", str(s or "")))


def make_git_diff_mock(committed_files=(), uncommitted_files=()):
    """Return a mock for subprocess.run that simulates git diff --name-only output."""

    def mock_run(args, **kwargs):
        if "diff" in args and "main...HEAD" in args:
            output = "\n".join(committed_files) + ("\n" if committed_files else "")
            return MagicMock(returncode=0, stdout=output, stderr="")
        if "diff" in args and "HEAD" in args:
            output = "\n".join(uncommitted_files) + ("\n" if uncommitted_files else "")
            return MagicMock(returncode=0, stdout=output, stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    return mock_run
