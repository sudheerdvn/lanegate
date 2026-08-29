"""Tests for lifecycle recovery commands and review-pending transitions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lanegate.lifecycle import (
    cmd_recover_rate_limited_reviews,
    cmd_recover_rejected,
    resume_review_pending,
)
from lanegate.ticket import parse_ticket, write_ticket

def _default_cfg(tickets_dir, worktrees_dir):
    return {
        "ticket_prefix": "TICK",
        "tickets_dir": str(tickets_dir.name),
        "worktrees_dir": str(worktrees_dir.name),
        "lock_statuses": ["in_progress", "code_complete", "in_review"],
        "commit_status_changes": False,
        "environments": [],
    }


def _write_ticket(
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



def _start_cfg(tmp_path, *, commit_status_changes=False):
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


def test_resume_review_pending_restores_code_complete(tmp_path):
    """Regression test: a review-pending ticket resumed by cmd_start lands at
    in_progress like any other resume. Without resume_review_pending
    bridging it back to code_complete, the reviewer that runs next writes a
    verdict.json the ticket can never receive -- cmd_review's own
    code_complete guard rejects the write every time (see TICK-392/393/395/
    396/398/400, all lost to this in the same live run)."""
    from lanegate.lifecycle import resume_review_pending

    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    (tickets_dir / "TICK-120.md").write_text(
        "---\n"
        "id: TICK-120\n"
        "title: Test TICK-120\n"
        "status: in_progress\n"
        "review_pending: true\n"
        "review_pending_reason: rate limited\n"
        "touches:\n  - a.py\n"
        "close_criteria: x.\n"
        "---\nBody.\n"
    )
    ticket = parse_ticket(tickets_dir / "TICK-120.md")

    resume_review_pending(ticket, cfg, tmp_path)

    assert ticket["status"] == "code_complete"
    on_disk = parse_ticket(tickets_dir / "TICK-120.md")
    assert on_disk["status"] == "code_complete"
    events = on_disk.get("lifecycle_events") or []
    assert events[-1]["from_status"] == "in_progress"
    assert events[-1]["to_status"] == "code_complete"


def test_resume_review_pending_is_idempotent_at_code_complete(tmp_path):
    from lanegate.lifecycle import resume_review_pending

    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    (tickets_dir / "TICK-120.md").write_text(
        "---\nid: TICK-120\ntitle: Test TICK-120\nstatus: code_complete\n"
        "touches:\n  - a.py\nclose_criteria: x.\n---\nBody.\n"
    )
    ticket = parse_ticket(tickets_dir / "TICK-120.md")

    resume_review_pending(ticket, cfg, tmp_path)

    assert ticket["status"] == "code_complete"


def test_resume_review_pending_rejects_unexpected_status(tmp_path):
    from lanegate.lifecycle import resume_review_pending

    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    (tickets_dir / "TICK-120.md").write_text(
        "---\nid: TICK-120\ntitle: Test TICK-120\nstatus: needs_review\n"
        "touches:\n  - a.py\nclose_criteria: x.\n---\nBody.\n"
    )
    ticket = parse_ticket(tickets_dir / "TICK-120.md")

    with pytest.raises(ValueError, match="needs_review"):
        resume_review_pending(ticket, cfg, tmp_path)


def test_recover_rejected_moves_only_exhausted_rejections_to_needs_review(tmp_path):
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_ticket(
        tickets_dir, "TICK-123", "code_complete", review_verdict="changes_requested"
    )
    exhausted = parse_ticket(tickets_dir / "TICK-123.md")
    exhausted["auto_fix_attempts"] = 1
    write_ticket(exhausted)
    _write_ticket(
        tickets_dir, "TICK-124", "code_complete", review_verdict="changes_requested"
    )

    assert cmd_recover_rejected(None, cfg, tmp_path, all_tickets=True) == 1

    assert parse_ticket(tickets_dir / "TICK-123.md")["status"] == "needs_review"
    assert parse_ticket(tickets_dir / "TICK-124.md")["status"] == "code_complete"


def test_recover_rejected_refuses_fresh_rejection(tmp_path):
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_ticket(
        tickets_dir, "TICK-125", "code_complete", review_verdict="changes_requested"
    )

    with pytest.raises(SystemExit):
        cmd_recover_rejected("TICK-125", cfg, tmp_path)


def test_recover_rate_limited_review_requires_empty_429_bundle(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-429", "needs_review", touches=["foo.py"])
    _write_ticket(tickets_dir, "TICK-430", "needs_review", touches=["foo.py"])
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    def bundle(tid: str, verdict: str, findings: str, output: str):
        path = tmp_path / ".lanegate" / "executor-runs" / tid / "review-1"
        path.mkdir(parents=True)
        (path / "status.json").write_text(json.dumps({"step": "review"}))
        (path / "verdict.json").write_text(json.dumps({"verdict": verdict, "findings": findings}))
        (path / "captured-output.txt").write_text(output)

    bundle("TICK-429", "error", "", "HTTP 429 rate limit")
    bundle("TICK-430", "changes_requested", "- real bug", "HTTP 429 rate limit")

    assert cmd_recover_rate_limited_reviews(None, cfg, tmp_path) == 1
    assert parse_ticket(tickets_dir / "TICK-429.md")["status"] == "hibernated"
    assert parse_ticket(tickets_dir / "TICK-429.md")["review_pending"] is True
    assert parse_ticket(tickets_dir / "TICK-430.md")["status"] == "needs_review"


def test_recover_rate_limited_reviews_uses_canonical_classifier(tmp_path):
    """A hard error accompanied by rate-limit-shaped text must not be recovered."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-440", "needs_review", touches=["foo.py"])
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    bundle = tmp_path / ".lanegate" / "executor-runs" / "TICK-440" / "review-1"
    bundle.mkdir(parents=True)
    (bundle / "status.json").write_text(json.dumps({"step": "review"}))
    (bundle / "verdict.json").write_text(json.dumps({"verdict": "error", "findings": ""}))
    (bundle / "captured-output.txt").write_text(
        "429 Too Many Requests; invalid_request_error: unknown model"
    )

    assert cmd_recover_rate_limited_reviews("TICK-440", cfg, tmp_path) == 0
    assert parse_ticket(tickets_dir / "TICK-440.md")["status"] == "needs_review"


def test_recover_rate_limited_reviews_refuses_current_protected_path_escalation(tmp_path):
    """A stale 429 bundle cannot requeue a later protected-path escalation."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    path = _write_ticket(tickets_dir, "TICK-441", "needs_review", touches=["foo.py"])
    path.write_text(
        path.read_text()
        + "\n## Hibernation Reason\n\nrate limit or quota interruption (executor exited 429)\n"
        + "\n## Needs Review Reason\n\nsecurity_sensitive_paths — human review required\n"
    )
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    bundle = tmp_path / ".lanegate" / "executor-runs" / "TICK-441" / "review-1"
    bundle.mkdir(parents=True)
    (bundle / "status.json").write_text(json.dumps({"step": "review"}))
    (bundle / "verdict.json").write_text(json.dumps({"verdict": "error", "findings": ""}))
    (bundle / "captured-output.txt").write_text("HTTP 429 rate limit")

    assert cmd_recover_rate_limited_reviews("TICK-441", cfg, tmp_path) == 0
    ticket = parse_ticket(path)
    assert ticket["status"] == "needs_review"
    assert not ticket.get("review_pending")
