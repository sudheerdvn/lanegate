"""Regression coverage for automatic recovery of scope-only review pauses."""

from __future__ import annotations

from tests.orchestrate.conftest import _default_cfg, _write_ticket, patch
from lanegate.orchestrate.loop import recover_scope_only_needs_review_tickets
from lanegate.ticket import parse_ticket


def _scope_paused_ticket(tmp_path, *, unexpected: str = "src/support.py"):
    tickets_dir = tmp_path / "tickets"
    worktree = tmp_path / "worktrees" / "tick-001"
    worktree.mkdir()
    path = _write_ticket(
        tickets_dir, "TICK-001", "needs_review", touches=["src/declared.py"]
    )
    path.write_text(
        path.read_text()
        .replace("close_criteria: All tests pass.\n", f"worktree: {worktree}\nclose_criteria: All tests pass.\n")
        .replace(
            "Body.\n",
            "Background.\n\n## Needs Review Reason\n\n"
            f"committed files outside touches list: {unexpected}\n",
        )
    )
    return path


def test_scope_only_pause_is_claimed_recorded_and_sent_to_review(tmp_path):
    cfg = _default_cfg(tmp_path)
    cfg["auto_claim_touches"] = True
    path = _scope_paused_ticket(tmp_path)

    def fake_reopen(tid, cfg_, repo_root):
        path.write_text(path.read_text().replace("status: needs_review", "status: code_complete"))

    def fake_review(tid, cfg_, repo_root):
        text = path.read_text().replace("status: code_complete", "status: in_review")
        path.write_text(text.replace("---\nBackground.", "review_verdict: approved\n---\nBackground."))

    with (
        patch("lanegate.orchestrate._committed_files", return_value={"src/declared.py", "src/support.py"}),
        patch("lanegate.orchestrate._run_static_analysis", return_value=[]),
        patch("lanegate.orchestrate._run_acceptance_contract_audit", return_value=[]),
        patch("lanegate.lifecycle.cmd_reopen", side_effect=fake_reopen) as reopen,
        patch("lanegate.lifecycle.cmd_review", side_effect=fake_review) as review,
    ):
        recovered = recover_scope_only_needs_review_tickets(cfg, tmp_path)

    assert recovered == ["TICK-001"]
    reopen.assert_called_once()
    review.assert_called_once()
    ticket = parse_ticket(path)
    assert ticket["status"] == "in_review"
    assert "src/support.py" in ticket["touches"]
    assert "## Scope Updates" in ticket["_body"]
    assert "`src/support.py`" in ticket["_body"]


def test_scope_recovery_does_not_bypass_hard_blocked_paths(tmp_path):
    cfg = _default_cfg(tmp_path)
    cfg["auto_claim_touches"] = True
    path = _scope_paused_ticket(tmp_path, unexpected=".github/workflows/checks.yml")

    with (
        patch(
            "lanegate.orchestrate._committed_files",
            return_value={"src/declared.py", ".github/workflows/checks.yml"},
        ),
        patch("lanegate.lifecycle.cmd_reopen") as reopen,
    ):
        recovered = recover_scope_only_needs_review_tickets(cfg, tmp_path)

    assert recovered == []
    reopen.assert_not_called()
    ticket = parse_ticket(path)
    assert ".github/workflows/checks.yml" not in ticket["touches"]
