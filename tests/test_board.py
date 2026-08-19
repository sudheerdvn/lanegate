"""Tests for board.py — cmd_board renders [DRAFT] section; cmd_next excludes drafts."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from lanegate.board import (
    _new_board_table,
    _next_step_lines,
    _ticket_data,
    _ticket_flags,
    cmd_blocked,
    cmd_board,
    cmd_next,
    cmd_pipeline_status,
    cmd_route,
    cmd_summary,
)
from lanegate.git import GitText, PendingCommits
from lanegate.ticket import parse_ticket, write_ticket

_BASE_CFG = {
    "ticket_prefix": "TICK",
    "tickets_dir": "tickets",
    "lock_statuses": ["in_progress", "code_complete", "in_review"],
    "environments": [],
}


def _make_ticket(
    tickets_dir: Path,
    ticket_id: str,
    status: str,
    touches=("src/app.py",),
    priority: int = 5,
    parallel_safe: bool = True,
    review_verdict=None,
    autonomy=None,
    complexity=None,
    depends_on=None,
) -> None:
    frontmatter = f"id: {ticket_id}\ntitle: Test {ticket_id}\nstatus: {status}\npriority: {priority}\nparallel_safe: {str(parallel_safe).lower()}\n"
    if review_verdict is not None:
        frontmatter += f"review_verdict: {review_verdict}\n"
    if autonomy is not None:
        frontmatter += f"autonomy: {autonomy}\n"
    if complexity is not None:
        frontmatter += f"complexity: {complexity}\n"
    if touches is not None:
        frontmatter += "touches:\n" + "".join(f"  - {t}\n" for t in touches)
    if depends_on is not None:
        frontmatter += "depends_on:\n" + "".join(f"  - {dep}\n" for dep in depends_on)
    (tickets_dir / f"{ticket_id}.md").write_text(f"---\n{frontmatter}---\nBody.\n")


@pytest.fixture
def tickets_root(tmp_path):
    td = tmp_path / "tickets"
    td.mkdir()
    return tmp_path


# --- cmd_board: [DRAFT] section ---


def test_board_draft_section_text(tickets_root, capsys):
    _make_ticket(tickets_root / "tickets", "TICK-001", "draft")
    cmd_board(_BASE_CFG, tickets_root)
    out = capsys.readouterr().out
    assert "DRAFT" in out
    assert "TICK-001" in out


def test_board_draft_with_touches_says_open_not_analyze(tickets_root, capsys):
    """A draft whose touches were already populated by analyze (still draft
    because analyze doesn't itself flip draft -> open) must be told to run
    `lanegate open`, not re-told to run `lanegate analyze` or have its
    already-populated touches mischaracterized as manually set."""
    _make_ticket(tickets_root / "tickets", "TICK-001", "draft", touches=("src/app.py",))
    cmd_board(_BASE_CFG, tickets_root)
    out = capsys.readouterr().out
    assert "Touches already populated" in out
    assert "lanegate open <id>" in out
    assert "No touches yet" not in out


def test_board_draft_without_touches_says_analyze(tickets_root, capsys):
    _make_ticket(tickets_root / "tickets", "TICK-001", "draft", touches=None)
    cmd_board(_BASE_CFG, tickets_root)
    out = capsys.readouterr().out
    assert "No touches yet" in out
    assert "lanegate analyze <id>" in out
    assert "Touches already populated" not in out


def test_board_folds_an_explicit_long_title_instead_of_dropping_its_end(tickets_root, capsys):
    title = "Preserve the complete authentication migration plan and rollout safeguards on this board"
    (tickets_root / "tickets" / "TICK-001.md").write_text(
        f"---\nid: TICK-001\ntitle: {title}\nstatus: draft\npriority: 5\n---\nBody.\n"
    )
    cmd_board(_BASE_CFG, tickets_root)
    out = capsys.readouterr().out
    assert "Preserve the complete" in out
    assert "safeguards on this board" in out


def test_board_draft_not_in_other(tickets_root, capsys):
    _make_ticket(tickets_root / "tickets", "TICK-001", "draft")
    cmd_board(_BASE_CFG, tickets_root)
    out = capsys.readouterr().out
    assert "OTHER" not in out


def test_board_draft_before_open(tickets_root, capsys):
    _make_ticket(tickets_root / "tickets", "TICK-001", "draft")
    _make_ticket(tickets_root / "tickets", "TICK-002", "open", touches=["src/foo.py"])
    cmd_board(_BASE_CFG, tickets_root)
    out = capsys.readouterr().out
    assert out.index("DRAFT") < out.index("OPEN")


def test_board_draft_section_json(tickets_root, capsys):
    _make_ticket(tickets_root / "tickets", "TICK-001", "draft")
    cmd_board(_BASE_CFG, tickets_root, json_output=True)
    data = json.loads(capsys.readouterr().out)
    assert "draft" in data["tickets"]


def test_board_json_survives_unquoted_date_field(tickets_root, capsys):
    """Regression for TICK-121: an unquoted `created: 2026-07-03` frontmatter value
    is parsed by YAML as datetime.date; json_output must not raise on it."""
    (tickets_root / "tickets" / "TICK-001.md").write_text(
        "---\nid: TICK-001\ntitle: Test TICK-001\nstatus: open\npriority: 5\n"
        "parallel_safe: true\ncreated: 2026-07-03\n---\nBody.\n"
    )
    cmd_board(_BASE_CFG, tickets_root, json_output=True)  # must not raise
    data = json.loads(capsys.readouterr().out)
    assert data["tickets"]["open"][0]["created"] == "2026-07-03"


def test_board_draft_not_in_json_other(tickets_root, capsys):
    _make_ticket(tickets_root / "tickets", "TICK-001", "draft")
    cmd_board(_BASE_CFG, tickets_root, json_output=True)
    data = json.loads(capsys.readouterr().out)
    # draft should be a top-level key, not buried under some unknown bucket
    assert "draft" in data["tickets"]
    # no ticket should appear in two buckets
    all_ids = [t["id"] for group in data["tickets"].values() for t in group]
    assert all_ids.count("TICK-001") == 1


def test_board_milestone_filter_applies_to_drafts(tickets_root, capsys):
    cfg = {**_BASE_CFG, "default_milestone": "v1"}
    td = tickets_root / "tickets"
    _make_milestone_ticket(td, "TICK-001", "draft", milestone=None)
    _make_milestone_ticket(td, "TICK-002", "draft", milestone="v1")
    _make_milestone_ticket(td, "TICK-003", "draft", milestone="v2")
    _make_milestone_ticket(td, "TICK-004", "open", milestone="v1")
    cmd_board(cfg, tickets_root)
    out = capsys.readouterr().out
    assert "DRAFT" in out
    assert "TICK-001" not in out
    assert "TICK-002" in out
    assert "TICK-003" not in out
    assert "TICK-004" in out


def test_board_milestone_filter_applies_to_drafts_json(tickets_root, capsys):
    cfg = {**_BASE_CFG, "default_milestone": "v1"}
    td = tickets_root / "tickets"
    _make_milestone_ticket(td, "TICK-001", "draft", milestone=None)
    _make_milestone_ticket(td, "TICK-002", "draft", milestone="v1")
    _make_milestone_ticket(td, "TICK-003", "draft", milestone="v2")
    cmd_board(cfg, tickets_root, json_output=True)
    data = json.loads(capsys.readouterr().out)
    assert "draft" in data["tickets"]
    draft_ids = {t["id"] for t in data["tickets"]["draft"]}
    assert draft_ids == {"TICK-002"}


# --- cmd_next: excludes drafts ---


def test_next_excludes_draft_text(tickets_root, capsys):
    _make_ticket(tickets_root / "tickets", "TICK-001", "draft", touches=[])
    cmd_next(_BASE_CFG, tickets_root)
    out = capsys.readouterr().out
    assert "TICK-001" not in out
    assert "No unblocked open tickets" in out


def test_next_excludes_draft_json(tickets_root, capsys):
    _make_ticket(tickets_root / "tickets", "TICK-001", "draft", touches=[])
    cmd_next(_BASE_CFG, tickets_root, json_output=True)
    data = json.loads(capsys.readouterr().out)
    assert data["next"] is None
    assert all(t["id"] != "TICK-001" for t in data["peers"])


def test_next_excludes_draft_even_with_open_touches(tickets_root, capsys):
    """Draft with non-conflicting touches must still be excluded from next."""
    _make_ticket(tickets_root / "tickets", "TICK-001", "draft", touches=["src/unique.py"])
    cmd_next(_BASE_CFG, tickets_root)
    out = capsys.readouterr().out
    assert "TICK-001" not in out


def test_next_no_candidate_shows_next_steps_for_claimed_ticket(tickets_root, capsys):
    _make_ticket(tickets_root / "tickets", "TICK-001", "in_progress", touches=["src/foo.py"])

    cmd_next(_BASE_CFG, tickets_root)

    out = capsys.readouterr().out
    assert "No unblocked open tickets" in out
    assert "Next steps:" in out
    assert "TICK-001: implementation running or claimed" in out
    assert "lanegate run --status" in out


def test_next_picks_open_over_draft(tickets_root, capsys):
    _make_ticket(tickets_root / "tickets", "TICK-001", "draft", touches=[], priority=1)
    _make_ticket(tickets_root / "tickets", "TICK-002", "open", touches=["src/bar.py"], priority=2)
    cmd_next(_BASE_CFG, tickets_root)
    out = capsys.readouterr().out
    assert "TICK-002" in out
    assert "Next:" in out


@pytest.mark.parametrize("status", ["merged", "validated", "done"])
def test_next_unblocks_delivered_canonical_dependency(tickets_root, capsys, status):
    td = tickets_root / "tickets"
    _make_ticket(td, "TICK-001", status)
    _make_ticket(td, "TICK-002", "open", depends_on=["tick-1"])
    cmd_next(_BASE_CFG, tickets_root, json_output=True)
    assert json.loads(capsys.readouterr().out)["next"]["id"] == "TICK-002"


@pytest.mark.parametrize("status", ["failed", "closed"])
def test_next_blocks_undelivered_dependency_in_text_and_json(tickets_root, capsys, status):
    td = tickets_root / "tickets"
    _make_ticket(td, "TICK-001", status)
    _make_ticket(td, "TICK-002", "open", depends_on=["TICK-001"])
    cmd_next(_BASE_CFG, tickets_root)
    assert "TICK-002" not in capsys.readouterr().out
    cmd_next(_BASE_CFG, tickets_root, json_output=True)
    data = json.loads(capsys.readouterr().out)
    assert data["next"] is None
    assert data["peers"] == []


# ---------------------------------------------------------------------------
# Milestone filtering on cmd_board
# ---------------------------------------------------------------------------


def _make_milestone_ticket(tickets_dir, ticket_id, status, milestone=None, touches=("src/app.py",)):
    ms_line = f"milestone: {milestone}\n" if milestone else ""
    touches_line = "touches:\n" + "".join(f"  - {t}\n" for t in touches) if touches is not None else ""
    content = (
        f"---\n"
        f"id: {ticket_id}\n"
        f"title: Test {ticket_id}\n"
        f"status: {status}\n"
        f"priority: 5\n"
        f"parallel_safe: true\n"
        f"{ms_line}"
        f"{touches_line}"
        f"---\nBody.\n"
    )
    (tickets_dir / f"{ticket_id}.md").write_text(content)


def test_board_milestone_filter_shows_only_matching(tickets_root, capsys):
    """With --milestone v1, only v1 tickets are shown."""
    td = tickets_root / "tickets"
    _make_milestone_ticket(td, "TICK-001", "open", milestone="v1")
    _make_milestone_ticket(td, "TICK-002", "open", milestone="v2")
    cmd_board(_BASE_CFG, tickets_root, milestone="v1")
    out = capsys.readouterr().out
    assert "TICK-001" in out
    assert "TICK-002" not in out


def test_board_milestone_filter_hides_other_non_draft_milestones(tickets_root, capsys):
    """With --milestone v2, non-draft tickets from other milestones are hidden."""
    td = tickets_root / "tickets"
    _make_milestone_ticket(td, "TICK-001", "code_complete", milestone="v2")
    _make_milestone_ticket(td, "TICK-002", "open", milestone="v1.5")
    _make_milestone_ticket(td, "TICK-003", "in_review", milestone="v3")

    cmd_board(_BASE_CFG, tickets_root, milestone="v2", show_all=True)

    out = capsys.readouterr().out
    assert "TICK-001" in out
    assert "TICK-002" not in out
    assert "TICK-003" not in out


def test_board_no_milestone_filter_shows_all(tickets_root, capsys):
    """Without --milestone, all tickets (including untagged) are shown."""
    td = tickets_root / "tickets"
    _make_milestone_ticket(td, "TICK-001", "open", milestone="v1")
    _make_milestone_ticket(td, "TICK-002", "open", milestone=None)
    cmd_board(_BASE_CFG, tickets_root)
    out = capsys.readouterr().out
    assert "TICK-001" in out
    assert "TICK-002" in out


def test_board_all_milestones_overrides_milestone_filter(tickets_root, capsys):
    """--all-milestones shows tickets across milestones."""
    td = tickets_root / "tickets"
    _make_milestone_ticket(td, "TICK-001", "draft", milestone="v1.5")
    _make_milestone_ticket(td, "TICK-002", "draft", milestone="v2")

    cmd_board(_BASE_CFG, tickets_root, milestone="v1.5", all_milestones=True)

    out = capsys.readouterr().out
    assert "TICK-001" in out
    assert "TICK-002" in out


def test_board_milestone_tag_shown_inline(tickets_root, capsys):
    """Milestone appears in the board text for tagged tickets."""
    td = tickets_root / "tickets"
    _make_milestone_ticket(td, "TICK-001", "open", milestone="v1")
    cmd_board(_BASE_CFG, tickets_root)
    out = capsys.readouterr().out
    assert "v1" in out


def test_board_untagged_ticket_flagged(tickets_root, capsys):
    """Tickets without a milestone tag show the 'no milestone' marker."""
    td = tickets_root / "tickets"
    _make_milestone_ticket(td, "TICK-001", "open", milestone=None)
    cmd_board(_BASE_CFG, tickets_root)
    out = capsys.readouterr().out
    assert "no milestone" in out


def test_board_tagged_ticket_not_flagged(tickets_root, capsys):
    """Tagged tickets do NOT show the 'no milestone' marker."""
    td = tickets_root / "tickets"
    _make_milestone_ticket(td, "TICK-001", "open", milestone="v1")
    cmd_board(_BASE_CFG, tickets_root)
    out = capsys.readouterr().out
    assert "no milestone" not in out


# ---------------------------------------------------------------------------
# Milestone filtering on cmd_next
# ---------------------------------------------------------------------------


def test_next_milestone_filter_restricts_candidates(tickets_root, capsys):
    """With milestone='v1', only v1 tickets are eligible for next."""
    td = tickets_root / "tickets"
    _make_milestone_ticket(td, "TICK-001", "open", milestone="v1")
    _make_milestone_ticket(td, "TICK-002", "open", milestone="v2")
    cmd_next(_BASE_CFG, tickets_root, milestone="v1")
    out = capsys.readouterr().out
    assert "TICK-001" in out
    assert "TICK-002" not in out


def test_next_milestone_filter_json(tickets_root, capsys):
    """JSON output respects milestone filter."""
    td = tickets_root / "tickets"
    _make_milestone_ticket(td, "TICK-001", "open", milestone="v1")
    _make_milestone_ticket(td, "TICK-002", "open", milestone="v2")
    cmd_next(_BASE_CFG, tickets_root, json_output=True, milestone="v1")
    data = json.loads(capsys.readouterr().out)
    assert data["next"] is not None
    assert data["next"]["id"] == "TICK-001"
    assert all(p["id"] != "TICK-002" for p in data["peers"])


# ---------------------------------------------------------------------------
# Terminal-width-aware column format
# ---------------------------------------------------------------------------


def test_board_table_uses_compact_layout_for_narrow_terminals():
    table, compact = _new_board_table(80)

    assert compact is True
    assert table.expand is True
    assert [column.header for column in table.columns] == [
        "ID",
        "P",
        "MS",
        "Age",
        "Title / Flags",
    ]


def test_board_table_uses_separate_flags_column_for_wide_terminals():
    table, compact = _new_board_table(120)

    assert compact is False
    assert table.expand is True
    assert [column.header for column in table.columns] == [
        "ID",
        "P",
        "MS",
        "Age",
        "Title",
        "Flags",
    ]


def test_ticket_flags_feature_flag():
    t = {"feature_flag": "ff_x", "milestone": "v1"}
    assert "flag:ff_x" in _ticket_flags(t)


def test_ticket_flags_depends_on():
    t = {"depends_on": ["TICK-001", "TICK-002"], "milestone": "v1"}
    assert "needs:TICK-001, TICK-002" in _ticket_flags(t)


def test_ticket_flags_worktree_shows_basename():
    t = {"worktree": "wt/tick-004", "milestone": "v1"}
    assert "wt:tick-004" in _ticket_flags(t)


def test_ticket_flags_review_verdict():
    t = {"review_verdict": "approved", "milestone": "v1"}
    assert "approved" in _ticket_flags(t)


def test_board_in_review_section_shows_review_summary_and_next_action(tickets_root, capsys):
    td = tickets_root / "tickets"
    (td / "TICK-001.md").write_text(
        "---\n"
        "id: TICK-001\n"
        "title: Test TICK-001\n"
        "status: in_review\n"
        "priority: 5\n"
        "parallel_safe: true\n"
        "milestone: v1\n"
        "review_verdict: approved\n"
        "review_summary: Ready for merge\n"
        "---\n"
        "Body.\n"
    )

    cmd_board(_BASE_CFG, tickets_root)

    out = capsys.readouterr().out
    assert "Review" in out
    assert "TICK-001: approved" in out
    assert "summary: Ready for merge" in out
    assert "Ready for merge" in out
    assert "next: lanegate merge TICK-001" in out


def test_board_in_review_section_shows_pending_verdict_hint(tickets_root, capsys):
    _make_ticket(tickets_root / "tickets", "TICK-001", "in_review")

    cmd_board(_BASE_CFG, tickets_root)

    out = capsys.readouterr().out
    assert "TICK-001: pending verdict" in out
    assert "next: lanegate review TICK-001 --verdict approved" in out


def test_board_next_steps_section_shows_recovery_actions(tickets_root, capsys):
    td = tickets_root / "tickets"
    _make_ticket(td, "TICK-001", "in_progress")
    _make_ticket(td, "TICK-002", "hibernated")
    _make_ticket(td, "TICK-003", "needs_review")
    (td / "TICK-004.md").write_text(
        "---\n"
        "id: TICK-004\n"
        "title: Test TICK-004\n"
        "status: code_complete\n"
        "priority: 5\n"
        "parallel_safe: true\n"
        "review_verdict: changes_requested\n"
        "---\n"
        "Body.\n"
    )

    cmd_board(_BASE_CFG, tickets_root)

    out = capsys.readouterr().out
    assert "Next Steps" in out
    assert "TICK-001: implementation running or claimed" in out
    assert "check: lanegate run" in out
    assert "--status" in out
    assert "TICK-002: hibernated - inspect log/worktree" in out
    assert "TICK-003: needs_review" in out
    assert "lanegate reopen TICK-003 &&" in out
    assert "TICK-004: changes_requested" in out


def test_board_shows_audited_recovery_for_exhausted_rejected_ticket(tickets_root, capsys):
    td = tickets_root / "tickets"
    _make_ticket(td, "TICK-006", "code_complete", review_verdict="changes_requested")
    ticket = parse_ticket(td / "TICK-006.md")
    ticket["auto_fix_attempts"] = 1
    write_ticket(ticket)

    cmd_board(_BASE_CFG, tickets_root)

    out = capsys.readouterr().out
    assert "TICK-006: exhausted rejected review" in out
    assert "after manual fixes, request a fresh independent review: lanegate review TICK-006" in out
    assert "lanegate recover-rejected TICK-006" in out


def test_board_next_steps_shows_fresh_code_complete_ticket(tickets_root, capsys):
    """A plain code_complete ticket with no review verdict yet (not stuck,
    just awaiting review) must still get a Next Steps line -- needs_attention()
    never returns true for this status, so the guidance branch is only
    reachable if the code_complete case is exempted from that gate."""
    td = tickets_root / "tickets"
    _make_ticket(td, "TICK-005", "code_complete")

    cmd_board(_BASE_CFG, tickets_root)

    out = capsys.readouterr().out
    assert "TICK-005: code_complete - awaiting review; next: lanegate run" in out
    assert "lanegate review TICK-005 --verdict approved to self-approve" in out


def test_board_next_steps_shows_needs_review_reason(tickets_root, capsys):
    td = tickets_root / "tickets"
    (td / "TICK-001.md").write_text(
        "---\n"
        "id: TICK-001\n"
        "title: Test TICK-001\n"
        "status: needs_review\n"
        "priority: 5\n"
        "parallel_safe: true\n"
        "---\n"
        "Body.\n\n"
        "## Needs Review Reason\n\n"
        "static analysis findings (1): semgrep non-literal import\n"
    )

    cmd_board(_BASE_CFG, tickets_root)

    out = capsys.readouterr().out
    assert "TICK-001: needs_review" in out
    assert "reason: static analysis findings (1): semgrep non-literal import" in out


def test_board_next_steps_contextual_recovery_guidance(tickets_root, capsys):
    td = tickets_root / "tickets"
    (td / "TICK-001.md").write_text(
        "---\n"
        "id: TICK-001\n"
        "title: Test TICK-001\n"
        "status: hibernated\n"
        "priority: 5\n"
        "review_pending: true\n"
        'review_pending_reason: "rate limit or quota interruption"\n'
        "---\n"
        "Body.\n"
    )
    (td / "TICK-002.md").write_text(
        "---\n"
        "id: TICK-002\n"
        "title: Test TICK-002\n"
        "status: hibernated\n"
        "priority: 10\n"
        "---\n"
        "Body.\n\n"
        "## Hibernation Reason\n\n"
        "rate limit or quota interruption (executor exited 1)\n"
    )

    cmd_board(_BASE_CFG, tickets_root)

    out = capsys.readouterr().out
    assert "TICK-001: review_pending (rate-limited)" in out
    assert "reason: rate limit or quota interruption" in out
    assert "TICK-002: hibernated (rate-limited)" in out
    assert "reason: rate limit or quota interruption" in out


def test_next_step_lines_rate_limit_uses_canonical_classifier():
    """A hard error accompanied by a rate-limit marker still needs an operator."""
    ticket = {
        "id": "TICK-440",
        "status": "hibernated",
        "priority": 1,
        "_body": (
            "## Hibernation Reason\n\n"
            "rate limit or quota interruption; "
            "invalid_request_error: unknown model"
        ),
    }

    lines = _next_step_lines([ticket])

    assert any("hibernated - inspect log/worktree" in line for line in lines)
    assert not any("rate-limited" in line for line in lines)


def test_next_step_lines_states_auto_retry_for_reviewer_cooldown():
    """A reviewer-cooldown hibernation must be labeled distinctly from a
    plain rate-limit and state that it auto-retries after the recorded time,
    rather than the generic 'reviewer did not return a verdict' message."""
    ticket = {
        "id": "TICK-517",
        "status": "hibernated",
        "priority": 1,
        "review_pending": True,
        "review_pending_reason": (
            "Independent reviewer temporarily unavailable (cooldown); "
            "retry after 2026-08-12T20:00:00+00:00. No healthy independent "
            "reviewer is available."
        ),
        "review_retry_after": "2026-08-12T20:00:00+00:00",
    }

    lines = _next_step_lines([ticket])

    line = next(line for line in lines if "TICK-517" in line)
    assert "review_pending (reviewer cooldown)" in line
    assert "auto-retries after 2026-08-12T20:00:00+00:00" in line
    assert "next: lanegate run" in line
    assert "rate-limited" not in line
    assert "did not return a verdict" not in line


def test_next_step_lines_states_human_action_for_no_independent_reviewer():
    """The permanent no-independent-reviewer needs_review escalation must
    surface cause-specific human-action guidance, not the generic
    review_rejection advice."""
    ticket = {
        "id": "TICK-518",
        "status": "needs_review",
        "priority": 1,
        "_body": (
            "## Needs Review Reason\n\n"
            "No healthy independent reviewer is available; "
            "review_fallback=needs_review resolved to needs_review. "
            "(retry budget exhausted)\n"
        ),
    }

    lines = _next_step_lines([ticket])

    line = next(line for line in lines if "TICK-518" in line)
    assert "needs_review (no_independent_reviewer)" in line
    assert "lanegate human-review TICK-518 --rationale" in line
    assert "review_fallback: same_model" in line


def test_board_needs_review_reason_aware_guidance():
    """needs_review next-step guidance is cause-specific, not a blanket
    'reopen && orchestrate' -- a hard-blocked path must point at the audited
    `lanegate human-review` path instead of an automatic reopen, while a plain
    scope-drift ticket still gets the ordinary reopen advice."""
    protected_ticket = {
        "id": "TICK-441",
        "status": "needs_review",
        "priority": 1,
        "_body": (
            "## Needs Review Reason\n\n"
            "committed files match hard-blocked categories: "
            ".github/workflows/ci.yml [CI/CD: .github/ directory]\n"
        ),
    }
    scope_drift_ticket = {
        "id": "TICK-442",
        "status": "needs_review",
        "priority": 2,
        "_body": (
            "## Needs Review Reason\n\ncommitted files outside touches list: extra.py\n"
        ),
    }

    lines = _next_step_lines([protected_ticket, scope_drift_ticket])

    protected_line = next(line for line in lines if "TICK-441" in line)
    assert "needs_review (protected_path)" in protected_line
    assert "lanegate human-review TICK-441 --rationale" in protected_line
    assert "lanegate reopen TICK-441 && lanegate run" not in protected_line

    scope_line = next(line for line in lines if "TICK-442" in line)
    assert "needs_review (scope_drift)" in scope_line
    assert "lanegate reopen TICK-442 && lanegate run" in scope_line


def test_board_needs_review_guidance_prioritizes_protected_path_over_stale_rate_limit():
    ticket = {
        "id": "TICK-443",
        "status": "needs_review",
        "priority": 1,
        "_body": (
            "## Hibernation Reason\n\n"
            "rate limit or quota interruption (executor exited 429)\n\n"
            "## Needs Review Reason\n\n"
            "security_sensitive_paths — human review required\n"
        ),
    }

    lines = _next_step_lines([ticket])

    protected_line = next(line for line in lines if "TICK-443" in line)
    assert "needs_review (protected_path)" in protected_line
    assert "lanegate human-review TICK-443 --rationale" in protected_line
    assert "recover-rate-limited-reviews" not in protected_line


def test_next_step_lines_merged_diagnostic():
    """A merged ticket with a post_merge_diagnostic renders repair advice with lanegate validate, not reopen or run."""
    ticket = {
        "id": "TICK-518",
        "status": "merged",
        "priority": 1,
        "post_merge_diagnostic": "post-merge verification safeguard failed on main: test failure",
    }

    lines = _next_step_lines([ticket])

    line = next(line for line in lines if "TICK-518" in line)
    assert "post_merge_diagnostic" in line
    assert "fix the diagnostic, then lanegate validate TICK-518" in line
    assert "lanegate reopen" not in line
    assert "lanegate run" not in line


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def test_cmd_summary_aggregates_reason_review_and_diff(tickets_root, capsys):
    _git(tickets_root, "init", "-b", "main")
    _git(tickets_root, "config", "user.email", "test@example.com")
    _git(tickets_root, "config", "user.name", "Test User")
    (tickets_root / "README.md").write_text("hello\n")
    _git(tickets_root, "add", "README.md")
    _git(tickets_root, "commit", "-m", "base")
    _git(tickets_root, "checkout", "-b", "tick-001")
    (tickets_root / "src.py").write_text("changed\n")
    _git(tickets_root, "add", "src.py")
    _git(tickets_root, "commit", "-m", "change")

    (tickets_root / "tickets" / "TICK-001.md").write_text(
        "---\n"
        "id: TICK-001\n"
        "title: Fix the thing\n"
        "status: needs_review\n"
        "priority: 2\n"
        "---\n"
        "Body.\n\n"
        "## Needs Review Reason\n"
        "static analysis findings: unused import in src.py\n"
    )

    cmd_summary("TICK-001", _BASE_CFG, tickets_root, json_output=False)
    out = capsys.readouterr().out
    assert "TICK-001 — Fix the thing" in out
    assert "unused import in src.py" in out
    assert "src.py" in out
    assert "lanegate reopen" in out

    cmd_summary("TICK-001", _BASE_CFG, tickets_root, json_output=True)
    data = json.loads(capsys.readouterr().out)
    assert data["needs_review_cause"] == "static_analysis"
    assert data["files_changed"] == [{"path": "src.py", "status": "A"}]


def test_cmd_summary_missing_ticket_errors(tickets_root, capsys):
    with pytest.raises(SystemExit):
        cmd_summary("TICK-999", _BASE_CFG, tickets_root, json_output=False)
    assert "TICK-999" in capsys.readouterr().err


def test_cmd_summary_missing_ticket_json_still_exits_nonzero(tickets_root, capsys):
    with pytest.raises(SystemExit) as exc:
        cmd_summary("TICK-999", _BASE_CFG, tickets_root, json_output=True)
    assert exc.value.code == 1
    data = json.loads(capsys.readouterr().out)
    assert "TICK-999" in data["error"]


def test_cmd_blocked_renders_merged_diagnostic(tickets_root, capsys):
    """cmd_blocked includes merged tickets with post_merge_diagnostic under Post-merge verification diagnostic section."""
    td = tickets_root / "tickets"
    (td / "TICK-518.md").write_text(
        "---\n"
        "id: TICK-518\n"
        "title: Integrated ticket\n"
        "status: merged\n"
        "post_merge_diagnostic: test suite failure on main\n"
        "---\n"
        "Body.\n\n"
        "## Post-Merge Verification Diagnostic\n\n"
        "test suite failure on main\n"
    )

    cmd_blocked(_BASE_CFG, tickets_root, json_output=False)
    out = capsys.readouterr().out
    assert "Post-merge verification diagnostic:" in out
    assert "TICK-518 — Integrated ticket" in out

    cmd_blocked(_BASE_CFG, tickets_root, json_output=True)
    data = json.loads(capsys.readouterr().out)
    entry = next(e for e in data if e["id"] == "TICK-518")
    assert entry["attention_category"] == "merged_diagnostic"



def test_board_json_includes_attention_summary(tickets_root, capsys):
    td = tickets_root / "tickets"
    (td / "TICK-001.md").write_text(
        "---\n"
        "id: TICK-001\n"
        "title: Test TICK-001\n"
        "status: needs_review\n"
        "priority: 5\n"
        "parallel_safe: true\n"
        "---\n"
        "Body.\n\n"
        "## Needs Review Reason\n\n"
        "manual fix needed before re-run\n"
    )

    cmd_board(_BASE_CFG, tickets_root, json_output=True)

    data = json.loads(capsys.readouterr().out)
    assert data["tickets"]["needs_review"][0]["attention_summary"] == "manual fix needed before re-run"
    assert data["tickets"]["needs_review"][0]["needs_attention"] is True
    assert data["tickets"]["needs_review"][0]["attention_category"] == "escalated"


def test_ticket_flags_no_milestone():
    t = {"milestone": None, "title": "No ms"}
    assert "no milestone" in _ticket_flags(t)


def test_ticket_flags_with_milestone_no_flag():
    t = {"milestone": "v2", "title": "Has ms"}
    assert "no milestone" not in _ticket_flags(t)


def test_ticket_flags_split_executor_route():
    t = {
        "milestone": "v2",
        "executor_route": {"implement": "codex", "review": "claude", "mode": "split"},
    }
    assert "exec:codex->claude split" in _ticket_flags(t)


def test_ticket_flags_combined_executor_route_stays_quiet():
    t = {
        "milestone": "v2",
        "executor_route": {"implement": "claude", "review": "claude", "mode": "combined"},
    }
    assert "exec:" not in _ticket_flags(t)


def test_ticket_data_includes_resolved_executor_route():
    cfg = {**_BASE_CFG, "executor": "claude", "executor_steps": {"implement": "codex"}}
    data = _ticket_data({"id": "TICK-001", "status": "open"}, cfg)

    assert data["implement_executor"] == "codex"
    assert data["review_executor"] == "claude"
    assert data["execution_mode"] == "split"
    assert data["executor_route"] == {
        "implement": "codex",
        "review": "claude",
        "mode": "split",
    }


def test_board_renders_ticket_id(tickets_root, capsys):
    """Board text output includes the ticket ID."""
    td = tickets_root / "tickets"
    _make_milestone_ticket(td, "TICK-001", "open", milestone="v1")
    cmd_board(_BASE_CFG, tickets_root)
    out = capsys.readouterr().out
    assert "TICK-001" in out


def test_board_json_unchanged_by_column_format(tickets_root, capsys):
    """JSON output is unaffected by the column formatting changes."""
    td = tickets_root / "tickets"
    _make_milestone_ticket(td, "TICK-001", "open", milestone="v1")
    cmd_board(_BASE_CFG, tickets_root, json_output=True)
    data = json.loads(capsys.readouterr().out)
    assert "open" in data["tickets"]


def test_board_json_includes_resolved_executor_route(tickets_root, capsys):
    td = tickets_root / "tickets"
    _make_milestone_ticket(td, "TICK-001", "open", milestone="v1")
    cfg = {**_BASE_CFG, "executor": "claude", "executor_steps": {"implement": "codex"}}

    cmd_board(cfg, tickets_root, json_output=True)

    data = json.loads(capsys.readouterr().out)
    ticket = data["tickets"]["open"][0]
    assert ticket["implement_executor"] == "codex"
    assert ticket["review_executor"] == "claude"
    assert ticket["execution_mode"] == "split"
    assert data["tickets"]["open"][0]["id"] == "TICK-001"


# --- complexity-based routing (TICK-091) ---

_ROUTING_CFG = {
    **_BASE_CFG,
    "pools": {
        "local": {"executors": ["ollama-1"]},
        "default": {"executors": ["claude-1"]},
    },
    "default_pool": "default",
    "routing": [
        {"when": {"complexity_max": 2, "touches_max": 3}, "executor_pool": "local"},
        {"when": {"complexity_min": 3}, "executor_pool": "default"},
    ],
}


def test_ticket_data_shows_routed_pool_when_routing_configured():
    data = _ticket_data({"id": "TICK-001", "status": "open", "complexity": 1, "touches": []}, _ROUTING_CFG)
    assert data["routed_pool"] == "local"


def test_ticket_data_shows_unrouted_when_no_rule_matches_and_no_default_pool():
    cfg = {
        **_BASE_CFG,
        "pools": {"local": {"executors": ["ollama-1"]}},
        "routing": [{"when": {"complexity_max": 2}, "executor_pool": "local"}],
    }
    data = _ticket_data({"id": "TICK-001", "status": "open", "complexity": 9, "touches": []}, cfg)
    assert data["routed_pool"] == "unrouted"


def test_ticket_data_omits_routed_pool_when_routing_not_configured():
    data = _ticket_data({"id": "TICK-001", "status": "open"}, _BASE_CFG)
    assert "routed_pool" not in data


def test_ticket_flags_shows_pool_when_routed():
    t = {"routed_pool": "local"}
    assert "pool:local" in _ticket_flags(t)


def test_ticket_flags_no_pool_flag_when_routing_not_configured():
    t = {"milestone": "v2"}
    assert "pool:" not in _ticket_flags(t)


def test_board_text_shows_assigned_pool(tickets_root, capsys):
    _make_ticket(tickets_root / "tickets", "TICK-001", "open", touches=["a.py"], complexity=1)
    cmd_board(_ROUTING_CFG, tickets_root)
    out = capsys.readouterr().out
    assert "pool:local" in out


def test_board_text_shows_unrouted_when_ticket_unanalyzed(tickets_root, capsys):
    cfg = {**_ROUTING_CFG, "default_pool": None}
    _make_ticket(tickets_root / "tickets", "TICK-001", "open", touches=["z.py", "y.py", "x.py", "w.py"])
    cmd_board(cfg, tickets_root)
    out = capsys.readouterr().out
    assert "pool:unrouted" in out


def test_board_json_shows_assigned_pool(tickets_root, capsys):
    _make_ticket(tickets_root / "tickets", "TICK-001", "open", touches=["a.py"], complexity=1)
    cmd_board(_ROUTING_CFG, tickets_root, json_output=True)
    data = json.loads(capsys.readouterr().out)
    assert data["tickets"]["open"][0]["routed_pool"] == "local"


def test_cmd_route_explains_winning_rule_text(tickets_root, capsys):
    _make_ticket(tickets_root / "tickets", "TICK-001", "open", touches=["a.py"], complexity=1)
    cmd_route(_ROUTING_CFG, tickets_root, "TICK-001")
    out = capsys.readouterr().out
    assert "routed_pool: local" in out
    assert "routing[0]" in out


def test_cmd_route_json(tickets_root, capsys):
    _make_ticket(tickets_root / "tickets", "TICK-001", "open", touches=["a.py"], complexity=5)
    cmd_route(_ROUTING_CFG, tickets_root, "TICK-001", json_output=True)
    data = json.loads(capsys.readouterr().out)
    assert data["routed_pool"] == "default"
    assert "routing[1]" in data["reason"]


def test_cmd_route_reports_runtime_context_for_named_aider_ollama_driver(tickets_root, capsys):
    cfg = {
        **_ROUTING_CFG,
        "executor": "aider-ollama",
        "pools": {"local": {"executors": ["aider-ollama"]}},
        "default_pool": "local",
        "drivers": {
            "aider-ollama": {
                "type": "aider",
                "provider": "ollama",
                "model": "ollama_chat/qwen2.5-coder:14b",
                "env": {"OLLAMA_API_BASE": "http://127.0.0.1:11435"},
                "context_window_tokens": 8192,
            }
        },
    }
    _make_ticket(tickets_root / "tickets", "TICK-001", "open", touches=["a.py"], complexity=1)

    with patch("lanegate.board.discover_ollama_context", return_value=(16384, "runtime")) as discover:
        cmd_route(cfg, tickets_root, "TICK-001", json_output=True)

    data = json.loads(capsys.readouterr().out)
    discover.assert_called_once_with("http://127.0.0.1:11435", "ollama_chat/qwen2.5-coder:14b")
    assert data["discovered_ollama_context"] == 16384
    assert data["ollama_context_source"] == "runtime"
    assert data["configured_context_window_tokens"] == 8192
    assert data["ollama_mismatch"] is True


def test_cmd_route_does_not_flag_static_ollama_metadata_as_mismatch(tickets_root, capsys):
    cfg = {
        **_ROUTING_CFG,
        "executor": "aider-ollama",
        "pools": {"local": {"executors": ["aider-ollama"]}},
        "default_pool": "local",
        "drivers": {
            "aider-ollama": {
                "type": "aider",
                "provider": "ollama",
                "model": "ollama_chat/qwen2.5-coder:14b",
                "base_url": "http://127.0.0.1:11435",
                "context_window_tokens": 8192,
            }
        },
    }
    _make_ticket(tickets_root / "tickets", "TICK-001", "open", touches=["a.py"], complexity=1)

    with patch("lanegate.board.discover_ollama_context", return_value=(16384, "model_metadata")):
        cmd_route(cfg, tickets_root, "TICK-001", json_output=True)

    data = json.loads(capsys.readouterr().out)
    assert data["ollama_context_source"] == "model_metadata"
    assert data["ollama_mismatch"] is False


def test_cmd_route_unanalyzed_ticket_explains_default_pool_fallback(tickets_root, capsys):
    _make_ticket(tickets_root / "tickets", "TICK-001", "open", touches=["a.py"])
    cmd_route(_ROUTING_CFG, tickets_root, "TICK-001", json_output=True)
    data = json.loads(capsys.readouterr().out)
    assert data["routed_pool"] == "default"
    assert "default_pool" in data["reason"]


def test_cmd_route_unknown_ticket_errors(tickets_root, capsys):
    with pytest.raises(SystemExit):
        cmd_route(_ROUTING_CFG, tickets_root, "TICK-999", json_output=True)
    data = json.loads(capsys.readouterr().out)
    assert "not found" in data["error"]


def test_cmd_route_skips_malformed_ticket_id_when_scanning(tickets_root, capsys):
    _make_ticket(tickets_root / "tickets", "TICK-001", "open", touches=["a.py"], complexity=1)
    (tickets_root / "tickets" / "TICK-900.md").write_text(
        "---\nid: 'TICK-900 '\ntitle: Malformed\nstatus: open\npriority: 5\n"
        "touches:\n  - other.py\n---\nBody.\n"
    )
    cmd_route(_ROUTING_CFG, tickets_root, "TICK-001", json_output=True)
    data = json.loads(capsys.readouterr().out)
    assert data["routed_pool"] == "local"


def test_board_other_group_shows_ticket(capsys):
    """OTHER group renders with the ticket ID visible."""
    from lanegate.board import _print_board_text

    ticket = {"id": "TICK-007", "title": "Unknown status ticket", "priority": 3, "milestone": "v1"}
    grouped = {"weird_status": [ticket]}
    _print_board_text(_BASE_CFG, Path("."), [], grouped, show_all=True)
    out = capsys.readouterr().out
    assert "OTHER" in out
    assert "TICK-007" in out


# ---------------------------------------------------------------------------
# --in-flight pre-filtering on cmd_next
# ---------------------------------------------------------------------------


def test_next_in_flight_filters_overlapping_candidate(tickets_root, capsys):
    """--in-flight excludes candidates whose touches overlap with the named ticket's touches."""
    td = tickets_root / "tickets"
    # TICK-001 is the in-flight ticket (already started by orchestrator)
    _make_ticket(td, "TICK-001", "in_progress", touches=["src/shared.py"])
    # TICK-002 overlaps with TICK-001 on src/shared.py — should be excluded
    _make_ticket(td, "TICK-002", "open", touches=["src/shared.py"])
    cmd_next(_BASE_CFG, tickets_root, in_flight=["TICK-001"])
    out = capsys.readouterr().out
    assert "TICK-002" not in out
    assert "No unblocked open tickets" in out


def test_next_in_flight_non_conflicting_still_returned(tickets_root, capsys):
    """Non-conflicting candidates are still returned when --in-flight is set."""
    td = tickets_root / "tickets"
    # TICK-001 is the in-flight ticket touching src/alpha.py
    _make_ticket(td, "TICK-001", "in_progress", touches=["src/alpha.py"])
    # TICK-002 touches a different file — should still be recommended
    _make_ticket(td, "TICK-002", "open", touches=["src/beta.py"])
    cmd_next(_BASE_CFG, tickets_root, in_flight=["TICK-001"])
    out = capsys.readouterr().out
    assert "TICK-002" in out
    assert "Next:" in out


def test_next_in_flight_wildcard_blocks_concrete_candidate(tickets_root, capsys):
    td = tickets_root / "tickets"
    _make_ticket(td, "TICK-001", "in_progress", touches=['"*"'])
    _make_ticket(td, "TICK-002", "open", touches=["src/concrete.py"])
    cmd_next(_BASE_CFG, tickets_root, in_flight=["TICK-001"])
    out = capsys.readouterr().out
    assert "TICK-002" not in out
    assert "No unblocked open tickets" in out


def test_next_does_not_batch_wildcard_with_concrete_peer(tickets_root, capsys):
    td = tickets_root / "tickets"
    _make_ticket(td, "TICK-001", "open", touches=['"*"'], priority=1)
    _make_ticket(td, "TICK-002", "open", touches=["src/concrete.py"], priority=2)
    cmd_next(_BASE_CFG, tickets_root, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["next"]["id"] == "TICK-001"
    assert payload["peers"] == []


def test_next_absent_in_flight_behaves_as_today(tickets_root, capsys):
    """When --in-flight is absent (None), behavior is identical to today."""
    td = tickets_root / "tickets"
    _make_ticket(td, "TICK-001", "open", touches=["src/foo.py"])
    _make_ticket(td, "TICK-002", "open", touches=["src/bar.py"])
    # Without in_flight, both are valid candidates; lowest priority picked first
    cmd_next(_BASE_CFG, tickets_root)
    out = capsys.readouterr().out
    # At least one ticket is recommended with no unexpected filtering
    assert "Next:" in out
    assert "TICK-001" in out or "TICK-002" in out


# ---------------------------------------------------------------------------
# Named executor instance display on the board (TICK-088)
# ---------------------------------------------------------------------------

_NAMED_EXECUTORS_CFG = {
    **_BASE_CFG,
    "executor": "claude-process",
    "executors": {
        "claude-1": {"type": "claude-process", "api_key_env": "ANTHROPIC_API_KEY_1"},
        "claude-2": {"type": "claude-process", "api_key_env": "ANTHROPIC_API_KEY_2"},
    },
}


_POOL_DISPATCH_CFG = {
    **_BASE_CFG,
    "executor": "claude-process",
    "executors": {
        "claude-a": {"type": "claude-process", "api_key_env": "ANTHROPIC_API_KEY_A"},
        "claude-b": {"type": "claude-process", "api_key_env": "ANTHROPIC_API_KEY_B"},
    },
}


def _make_executor_ticket(tickets_dir, ticket_id, status, executor=None, touches=None):
    ex_line = f"executor: {executor}\n" if executor else ""
    frontmatter = (
        f"id: {ticket_id}\ntitle: Test {ticket_id}\nstatus: {status}\npriority: 5\n"
        f"parallel_safe: true\n{ex_line}"
    )
    if touches is not None:
        frontmatter += "touches:\n" + "".join(f"  - {t}\n" for t in touches)
    (tickets_dir / f"{ticket_id}.md").write_text(f"---\n{frontmatter}---\nBody.\n")


def _write_dispatch_event(repo_root, ticket_id, executor):
    logs_dir = repo_root / ".lanegate" / "logs"
    logs_dir.mkdir(parents=True)
    session_ts = "2026-07-29T01-02-03"
    events_path = logs_dir / f"orchestrate-{session_ts}.events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "ts": "2026-07-29T01:02:03Z",
                "event": "ticket_dispatch",
                "ticket_id": ticket_id,
                "executor": executor,
                "was_hibernated": False,
            }
        )
        + "\n"
    )
    (logs_dir / "last-run.json").write_text(
        json.dumps(
            {
                "session_ts": session_ts,
                "log_path": str(logs_dir / f"orchestrate-{session_ts}.log"),
                "events_path": str(events_path),
            }
        )
    )


def test_ticket_data_shows_named_instance_for_in_progress():
    """_ticket_data resolves a bare-type in-progress ticket to the first
    configured named instance of that type."""
    ticket = {"id": "TICK-001", "status": "in_progress", "executor": "claude-process"}
    data = _ticket_data(ticket, _NAMED_EXECUTORS_CFG)
    assert data["executor_instance"] == "claude-1"


def test_ticket_data_named_instance_direct():
    ticket = {"id": "TICK-001", "status": "in_progress", "executor": "claude-2"}
    data = _ticket_data(ticket, _NAMED_EXECUTORS_CFG)
    assert data["executor_instance"] == "claude-2"


def test_ticket_data_falls_back_to_type_when_no_named_instance():
    """Fallback: if no named instance matches, executor_instance is the bare type."""
    ticket = {"id": "TICK-001", "status": "in_progress", "executor": "aider"}
    data = _ticket_data(ticket, _NAMED_EXECUTORS_CFG)
    assert data["executor_instance"] == "aider"


def test_ticket_data_no_instance_field_when_not_in_progress():
    """Only in-progress tickets get the executor_instance field."""
    ticket = {"id": "TICK-001", "status": "open", "executor": "claude-process"}
    data = _ticket_data(ticket, _NAMED_EXECUTORS_CFG)
    assert "executor_instance" not in data


def test_executor_instance_display(tickets_root, capsys):
    """lanegate board shows the resolved named executor instance (not just the
    bare type) for in-progress tickets."""
    td = tickets_root / "tickets"
    _make_executor_ticket(td, "TICK-001", "in_progress", executor="claude-process")
    cmd_board(_NAMED_EXECUTORS_CFG, tickets_root)
    out = capsys.readouterr().out
    assert "exec:claude-1" in out


def test_executor_instance_display_json(tickets_root, capsys):
    td = tickets_root / "tickets"
    _make_executor_ticket(td, "TICK-001", "in_progress", executor="claude-process")
    cmd_board(_NAMED_EXECUTORS_CFG, tickets_root, json_output=True)
    data = json.loads(capsys.readouterr().out)
    ticket = data["tickets"]["in_progress"][0]
    assert ticket["executor_instance"] == "claude-1"


def test_board_uses_dispatch_event_instance_for_pool_routed_ticket(tickets_root, capsys):
    """Pool-routed tickets show the actual dispatched instance, not the
    first configured instance of the executor type."""
    td = tickets_root / "tickets"
    _make_executor_ticket(td, "TICK-001", "in_progress", executor="claude-process")
    _write_dispatch_event(tickets_root, "TICK-001", "claude-b")

    cmd_board(_POOL_DISPATCH_CFG, tickets_root)
    out = capsys.readouterr().out

    assert "exec:claude-b" in out
    assert "exec:claude-a" not in out


def test_board_json_uses_dispatch_event_instance_for_pool_routed_ticket(tickets_root, capsys):
    td = tickets_root / "tickets"
    _make_executor_ticket(td, "TICK-001", "in_progress", executor="claude-process")
    _write_dispatch_event(tickets_root, "TICK-001", "claude-b")

    cmd_board(_POOL_DISPATCH_CFG, tickets_root, json_output=True)
    data = json.loads(capsys.readouterr().out)

    ticket = data["tickets"]["in_progress"][0]
    assert ticket["executor_instance"] == "claude-b"


# ── get_blocked_queue service tests ───────────────────────────────────────────

def test_get_blocked_queue_empty(tickets_root):
    """When no tickets are blocked, returns empty array."""
    from lanegate.board import get_blocked_queue

    result = get_blocked_queue(_BASE_CFG, tickets_root)
    assert result == {"blocked": []}


def test_get_blocked_queue(tickets_root, capsys):
    """The queue combines all human-actionable categories, not active work."""
    from lanegate.board import get_blocked_queue

    td = tickets_root / "tickets"
    _make_ticket(td, "TICK-001", "needs_review", touches=["src/escalated.py"])
    _make_ticket(td, "TICK-002", "failed", touches=["src/failed.py"])
    _make_ticket(
        td, "TICK-003", "code_complete", touches=["src/rejected.py"], review_verdict="changes_requested"
    )
    _make_ticket(
        td, "TICK-004", "in_review", touches=["src/merge.py"], review_verdict="approved", autonomy="manual"
    )
    _make_ticket(td, "TICK-005", "in_progress", touches=["src/running.py"], review_verdict="changes_requested")
    _make_ticket(td, "TICK-006", "in_review", touches=["src/reviewing.py"], review_verdict="changes_requested")
    (td / "TICK-007.md").write_text(
        "---\n"
        "id: TICK-007\ntitle: Stuck ticket\nstatus: hibernated\npriority: 1\n"
        "---\nBody.\n\n## Hibernation Reason\n\nexecutor requires re-authentication\n"
    )
    (td / "TICK-008.md").write_text(
        "---\n"
        "id: TICK-008\ntitle: Waiting ticket\nstatus: hibernated\npriority: 1\n"
        "---\nBody.\n\n## Hibernation Reason\n\nrate limit or quota interruption (executor exited 429)\n"
    )

    queue = get_blocked_queue(_BASE_CFG, tickets_root)["blocked"]
    assert {row["id"]: row["attention_category"] for row in queue} == {
        "TICK-001": "escalated",
        "TICK-002": "failed",
        "TICK-003": "rejected",
        "TICK-004": "awaiting_merge",
        "TICK-007": "stuck",
    }
    assert all(row["attention_summary"] for row in queue)

    cmd_blocked(_BASE_CFG, tickets_root)
    rendered = capsys.readouterr().out
    for heading in ("Escalated:", "Changes requested:", "Failed:", "Stuck:", "Awaiting merge:"):
        assert heading in rendered
    assert "TICK-005" not in rendered
    assert "TICK-006" not in rendered
    assert "TICK-008" not in rendered


def test_get_blocked_queue_respects_project_autonomy_for_approved_reviews(tickets_root):
    from lanegate.board import get_blocked_queue

    td = tickets_root / "tickets"
    _make_ticket(td, "TICK-001", "in_review", touches=["src/foo.py"], review_verdict="approved")

    assert get_blocked_queue({**_BASE_CFG, "autonomy": "full"}, tickets_root) == {"blocked": []}
    res = get_blocked_queue({**_BASE_CFG, "autonomy": "red"}, tickets_root)
    assert len(res["blocked"]) == 1
    assert res["blocked"][0]["id"] == "TICK-001"
    assert res["blocked"][0]["attention_category"] == "awaiting_merge"


def test_get_blocked_queue_returns_changes_requested(tickets_root):
    """Returns only code_complete tickets with review_verdict=changes_requested."""
    from lanegate.board import get_blocked_queue

    td = tickets_root / "tickets"

    # Create blocked ticket
    blocked_frontmatter = """id: TICK-001
title: Blocked Ticket
status: code_complete
priority: 1
review_verdict: changes_requested
review_findings:
  - Finding 1
  - Finding 2
"""
    (td / "TICK-001.md").write_text(f"---\n{blocked_frontmatter}---\nBody.\n")

    # Create approved ticket (not blocked)
    approved_frontmatter = """id: TICK-002
title: Approved Ticket
status: code_complete
priority: 1
review_verdict: approved
"""
    (td / "TICK-002.md").write_text(f"---\n{approved_frontmatter}---\nBody.\n")

    # Create in-progress with changes_requested (not blocked)
    in_progress_frontmatter = """id: TICK-003
title: In Progress
status: in_progress
priority: 1
review_verdict: changes_requested
"""
    (td / "TICK-003.md").write_text(f"---\n{in_progress_frontmatter}---\nBody.\n")

    result = get_blocked_queue(_BASE_CFG, tickets_root)
    assert len(result["blocked"]) == 1
    assert result["blocked"][0]["id"] == "TICK-001"


def test_get_blocked_queue_does_not_quarantine_named_executor_instance(tickets_root):
    """TICK-247: get_blocked_queue must pass cfg through to load_all_tickets
    so a blocked ticket carrying a named-instance executor override isn't
    silently dropped from the blocked queue."""
    from lanegate.board import get_blocked_queue

    td = tickets_root / "tickets"
    blocked_frontmatter = """id: TICK-010
title: Blocked Ticket With Named Executor
status: code_complete
priority: 1
executor: claude-2
review_verdict: changes_requested
"""
    (td / "TICK-010.md").write_text(f"---\n{blocked_frontmatter}---\nBody.\n")

    cfg = {**_BASE_CFG, "executors": {"claude-2": {"type": "claude-process"}}}
    result = get_blocked_queue(cfg, tickets_root)

    assert len(result["blocked"]) == 1
    assert result["blocked"][0]["id"] == "TICK-010"


def test_get_blocked_queue_json_structure(tickets_root):
    """Blocked queue entries include id, title, branch, diff_cmd, findings."""
    from lanegate.board import get_blocked_queue

    td = tickets_root / "tickets"
    frontmatter = """id: TICK-001
title: Test ticket
status: code_complete
priority: 1
review_verdict: changes_requested
review_findings:
  - Finding A
"""
    (td / "TICK-001.md").write_text(f"---\n{frontmatter}---\nBody.\n")

    result = get_blocked_queue(_BASE_CFG, tickets_root)
    blocked = result["blocked"][0]
    assert blocked["id"] == "TICK-001"
    assert blocked["title"] == "Test ticket"
    assert "branch" in blocked
    assert "diff_cmd" in blocked
    assert blocked["findings"] == ["Finding A"]


def test_get_blocked_queue_excludes_active_in_review_changes_requested(tickets_root, capsys):
    """A reviewer in flight is not a human-decision queue item."""
    from lanegate.board import get_blocked_queue

    td = tickets_root / "tickets"
    _make_ticket(td, "TICK-001", "in_review", touches=["src/foo.py"], review_verdict="changes_requested")

    queue_result = get_blocked_queue(_BASE_CFG, tickets_root)
    assert "TICK-001" not in [t["id"] for t in queue_result["blocked"]]

    cmd_blocked(_BASE_CFG, tickets_root, json_output=True)
    cmd_result = json.loads(capsys.readouterr().out)
    assert "TICK-001" not in [t["id"] for t in cmd_result]


# ---------------------------------------------------------------------------
# cmd_blocked tests
# ---------------------------------------------------------------------------


def test_blocked_code_complete_with_changes_requested(tickets_root, capsys):
    """cmd_blocked should include tickets with status=code_complete and review_verdict=changes_requested."""
    td = tickets_root / "tickets"
    _make_ticket(
        td, "TICK-001", "code_complete", touches=["src/foo.py"], review_verdict="changes_requested"
    )
    cmd_blocked(_BASE_CFG, tickets_root)
    out = capsys.readouterr().out
    assert "TICK-001" in out
    assert "Needs-human-decision tickets" in out
    assert "Changes requested:" in out


def test_blocked_in_review_with_changes_requested_is_active_work(tickets_root, capsys):
    """cmd_blocked must exclude a reviewer agent that is still working."""
    td = tickets_root / "tickets"
    _make_ticket(td, "TICK-001", "in_review", touches=["src/foo.py"], review_verdict="changes_requested")
    cmd_blocked(_BASE_CFG, tickets_root)
    out = capsys.readouterr().out
    assert "TICK-001" not in out
    assert "No tickets need human attention" in out


def test_blocked_mixed_statuses(tickets_root, capsys):
    """cmd_blocked groups rejected work and approved human merge waits."""
    td = tickets_root / "tickets"
    _make_ticket(
        td, "TICK-001", "code_complete", touches=["src/foo.py"], review_verdict="changes_requested"
    )
    _make_ticket(
        td, "TICK-002", "in_review", touches=["src/bar.py"], review_verdict="changes_requested"
    )
    _make_ticket(td, "TICK-003", "code_complete", touches=["src/baz.py"])  # no verdict
    _make_ticket(td, "TICK-004", "in_review", touches=["src/qux.py"], review_verdict="approved")
    cmd_blocked(_BASE_CFG, tickets_root)
    out = capsys.readouterr().out
    assert "TICK-001" in out
    assert "TICK-002" not in out  # reviewer active
    assert "TICK-003" not in out  # no changes_requested verdict
    assert "TICK-004" in out
    assert "Awaiting merge:" in out


def test_blocked_json_output(tickets_root, capsys):
    """cmd_blocked --json carries category and summary for queue rows."""
    td = tickets_root / "tickets"
    _make_ticket(
        td, "TICK-001", "code_complete", touches=["src/foo.py"], review_verdict="changes_requested"
    )
    _make_ticket(
        td, "TICK-002", "in_review", touches=["src/bar.py"], review_verdict="approved"
    )
    cmd_blocked(_BASE_CFG, tickets_root, json_output=True)
    out = capsys.readouterr().out
    data = json.loads(out)
    ids = [t["id"] for t in data]
    assert "TICK-001" in ids
    assert "TICK-002" in ids
    assert len(data) == 2
    assert {row["attention_category"] for row in data} == {"rejected", "awaiting_merge"}
    assert all(row["attention_summary"] for row in data)


# ---------------------------------------------------------------------------
# Pipeline pending-query failures
# ---------------------------------------------------------------------------


_PIPELINE_CFG = {
    **_BASE_CFG,
    "environments": [{"name": "stage", "branch": "stage", "from": "main"}],
}


def test_board_pipeline_text_shows_unknown_for_invalid_refs(tickets_root, capsys):
    result = PendingCommits([], "git log stage..main failed (exit 128): unknown revision")

    with patch("lanegate.board.pending_commits", return_value=result):
        cmd_board(_PIPELINE_CFG, tickets_root)

    out = capsys.readouterr().out
    assert "unknown" in out
    assert "up to date" not in out
    assert "unknown revision" in out


def test_board_pipeline_json_exposes_pending_query_failure(tickets_root, capsys):
    result = PendingCommits([], "git log stage..main failed (exit 128): unknown revision")

    with patch("lanegate.board.pending_commits", return_value=result):
        cmd_board(_PIPELINE_CFG, tickets_root, json_output=True)

    pipeline = json.loads(capsys.readouterr().out)["pipeline"][0]
    assert pipeline["pending_state"] == "unknown"
    assert pipeline["pending_count"] is None
    assert "unknown revision" in pipeline["pending_error"]


def test_pipeline_status_text_and_json_expose_pending_query_failure(tickets_root, capsys):
    result = PendingCommits([], "git log stage..main failed (exit 128): unknown revision")

    with patch("lanegate.board.pending_commits", return_value=result):
        cmd_pipeline_status(_PIPELINE_CFG, tickets_root)
    text = capsys.readouterr().out
    assert "unknown" in text
    assert "up to date" not in text

    with patch("lanegate.board.pending_commits", return_value=result):
        cmd_pipeline_status(_PIPELINE_CFG, tickets_root, json_output=True)
    pipeline = json.loads(capsys.readouterr().out)[0]
    assert pipeline["pending_state"] == "unknown"
    assert pipeline["pending_count"] is None
    assert "unknown revision" in pipeline["pending_error"]


def test_board_pipeline_missing_branch_is_actionable_in_text_and_json(tickets_root, capsys):
    failed_pending = PendingCommits([], "git log stage..main failed (exit 128): unknown revision")

    with (
        patch("lanegate.board.pending_commits", return_value=failed_pending),
        patch("lanegate.board.verify_local_branch", return_value=GitText("")),
    ):
        cmd_board(_PIPELINE_CFG, tickets_root)
    text = capsys.readouterr().out
    normalized_text = " ".join(text.split())
    assert "branch 'stage' does not exist" in normalized_text
    assert "git branch stage main" in normalized_text
    assert ".lanegate.yml" in normalized_text
    assert "git log" not in normalized_text
    assert "unknown revision" not in normalized_text

    with (
        patch("lanegate.board.pending_commits", return_value=failed_pending),
        patch("lanegate.board.verify_local_branch", return_value=GitText("")),
    ):
        cmd_board(_PIPELINE_CFG, tickets_root, json_output=True)
    pipeline = json.loads(capsys.readouterr().out)["pipeline"][0]
    assert pipeline["pending_state"] == "unknown"
    assert "branch 'stage' does not exist" in pipeline["pending_error"]
    assert "git branch stage main" in pipeline["pending_error"]
    assert "git log" not in pipeline["pending_error"]
    assert "unknown revision" not in pipeline["pending_error"]


def test_pipeline_status_missing_branch_is_actionable_in_text_and_json(tickets_root, capsys):
    failed_pending = PendingCommits([], "git log stage..main failed (exit 128): unknown revision")

    with (
        patch("lanegate.board.pending_commits", return_value=failed_pending),
        patch("lanegate.board.verify_local_branch", return_value=GitText("")),
    ):
        cmd_pipeline_status(_PIPELINE_CFG, tickets_root)
    text = capsys.readouterr().out
    assert "branch 'stage' does not exist" in text
    assert "git branch stage main" in text
    assert ".lanegate.yml" in text
    assert "git log" not in text
    assert "unknown revision" not in text

    with (
        patch("lanegate.board.pending_commits", return_value=failed_pending),
        patch("lanegate.board.verify_local_branch", return_value=GitText("")),
    ):
        cmd_pipeline_status(_PIPELINE_CFG, tickets_root, json_output=True)
    pipeline = json.loads(capsys.readouterr().out)[0]
    assert pipeline["pending_state"] == "unknown"
    assert "branch 'stage' does not exist" in pipeline["pending_error"]
    assert "git branch stage main" in pipeline["pending_error"]
    assert "git log" not in pipeline["pending_error"]
    assert "unknown revision" not in pipeline["pending_error"]


def test_pipeline_status_valid_empty_range_is_up_to_date(tickets_root, capsys):
    with patch("lanegate.board.pending_commits", return_value=PendingCommits([])):
        cmd_pipeline_status(_PIPELINE_CFG, tickets_root)

    assert "up to date" in capsys.readouterr().out


def test_cmd_route_updates_reviewer_pin(tickets_root, capsys):
    from lanegate.board import cmd_route
    from lanegate.ticket import parse_ticket
    _make_ticket(tickets_root / "tickets", "TICK-001", "draft")

    cmd_route(_BASE_CFG, tickets_root, "TICK-001", reviewer="codex")

    ticket = parse_ticket(tickets_root / "tickets" / "TICK-001.md")
    assert ticket["reviewer"] == "codex"
    assert "review_driver" not in ticket
    assert "Updated routing for TICK-001: reviewer → codex" in capsys.readouterr().out


def test_cmd_route_updates_executor_pin(tickets_root, capsys):
    from lanegate.board import cmd_route
    from lanegate.ticket import parse_ticket
    _make_ticket(tickets_root / "tickets", "TICK-001", "draft")

    cfg = {**_BASE_CFG, "executors": {"claude-b": {"type": "claude-process"}}}
    cmd_route(cfg, tickets_root, "TICK-001", executor="claude-b")

    ticket = parse_ticket(tickets_root / "tickets" / "TICK-001.md")
    assert ticket["executor"] == "claude-b"
    assert "implement_executor" not in ticket
    assert "Updated routing for TICK-001: executor → claude-b" in capsys.readouterr().out


def test_cmd_route_updates_model_pin(tickets_root, capsys):
    from lanegate.board import cmd_route
    from lanegate.ticket import parse_ticket
    _make_ticket(tickets_root / "tickets", "TICK-001", "draft")

    cmd_route(
        {**_BASE_CFG, "reviewer": "codex"},
        tickets_root,
        "TICK-001",
        model="gpt-5.6-terra",
    )

    ticket = parse_ticket(tickets_root / "tickets" / "TICK-001.md")
    assert ticket["review_model_pin"] == "gpt-5.6-terra"
    assert "review_model" not in ticket
    out = capsys.readouterr().out
    assert "Updated routing for TICK-001: model → gpt-5.6-terra" in out
    assert "Next step:" not in out


def test_cmd_route_rejects_invalid_routing_pin_without_writing(tickets_root, capsys):
    _make_ticket(tickets_root / "tickets", "TICK-001", "draft")
    path = tickets_root / "tickets" / "TICK-001.md"
    before = path.read_text()

    with pytest.raises(SystemExit):
        cmd_route(_BASE_CFG, tickets_root, "TICK-001", reviewer="definitely-not-a-driver")

    assert path.read_text() == before
    assert "unknown reviewer" in capsys.readouterr().err


def test_cmd_route_rejects_model_incompatible_with_resolved_reviewer(tickets_root, capsys):
    _make_ticket(tickets_root / "tickets", "TICK-001", "draft")
    path = tickets_root / "tickets" / "TICK-001.md"
    before = path.read_text()

    with pytest.raises(SystemExit):
        cmd_route(
            _BASE_CFG,
            tickets_root,
            "TICK-001",
            reviewer="codex",
            model="claude-sonnet-5",
        )

    assert path.read_text() == before
    assert "unmapped model 'claude-sonnet-5' for executor 'codex'" in capsys.readouterr().err


def test_cmd_route_rejects_reviewer_incompatible_with_existing_model_pin(tickets_root, capsys):
    _make_ticket(tickets_root / "tickets", "TICK-001", "draft")
    path = tickets_root / "tickets" / "TICK-001.md"
    ticket = parse_ticket(path)
    ticket["review_model"] = "claude-sonnet-5"
    ticket["review_model_pin"] = "claude-sonnet-5"
    write_ticket(ticket)
    before = path.read_text()

    with pytest.raises(SystemExit):
        cmd_route(_BASE_CFG, tickets_root, "TICK-001", reviewer="codex")

    assert path.read_text() == before
    assert "unmapped model 'claude-sonnet-5' for executor 'codex'" in capsys.readouterr().err


def test_cmd_route_rejects_executor_change_after_implementation(tickets_root, capsys):
    _make_ticket(tickets_root / "tickets", "TICK-001", "code_complete")
    path = tickets_root / "tickets" / "TICK-001.md"
    ticket = parse_ticket(path)
    ticket["implement_session_executor"] = "claude-a"
    write_ticket(ticket)
    before = path.read_text()

    with pytest.raises(SystemExit):
        cmd_route(
            {**_BASE_CFG, "executors": {"codex-review": {"type": "codex"}}},
            tickets_root,
            "TICK-001",
            executor="codex-review",
        )

    assert path.read_text() == before
    assert "cannot change executor for TICK-001 after implementation" in capsys.readouterr().err


def test_cmd_route_allows_recorded_executor_after_implementation(tickets_root):
    """Re-pinning the executor to its recorded implementer is not a change."""
    _make_ticket(tickets_root / "tickets", "TICK-001", "code_complete")
    path = tickets_root / "tickets" / "TICK-001.md"
    ticket = parse_ticket(path)
    ticket["implement_session_executor"] = "claude-a"
    write_ticket(ticket)

    cmd_route(
        {**_BASE_CFG, "executors": {"claude-a": {"type": "claude"}}},
        tickets_root,
        "TICK-001",
        executor="claude-a",
    )

    assert parse_ticket(path)["executor"] == "claude-a"


def test_cmd_route_validates_existing_model_pin_when_executor_changes(tickets_root, capsys):
    """An executor change can alter the independent pool reviewer."""
    _make_ticket(tickets_root / "tickets", "TICK-001", "draft")
    path = tickets_root / "tickets" / "TICK-001.md"
    ticket = parse_ticket(path)
    ticket.update(executor="codex-impl", review_model_pin="claude-sonnet-5")
    write_ticket(ticket)
    before = path.read_text()
    cfg = {
        **_BASE_CFG,
        "executor": "codex-impl",
        "executors": {
            "codex-impl": {"type": "codex"},
            "claude-review": {"type": "claude-process"},
            "codex-review": {"type": "codex"},
        },
        "pools": {
            "default": {"executors": ["claude-review", "codex-review"]}
        },
        "default_pool": "default",
        "review_fallback": "needs_review",
    }

    with pytest.raises(SystemExit):
        cmd_route(cfg, tickets_root, "TICK-001", executor="claude-review")

    assert path.read_text() == before
    assert "unmapped model 'claude-sonnet-5' for executor 'codex'" in capsys.readouterr().err


def test_cmd_route_rejects_model_incompatible_with_independent_pool_reviewer(tickets_root, capsys):
    """A route pin must match the reviewer selected after self-review exclusion.

    The configured review route is Claude here, but Claude implemented this
    ticket.  The actual independent review candidate is therefore Codex.
    """
    _make_ticket(tickets_root / "tickets", "TICK-001", "draft")
    path = tickets_root / "tickets" / "TICK-001.md"
    ticket = parse_ticket(path)
    ticket["executor"] = "claude-impl"
    write_ticket(ticket)
    before = path.read_text()
    cfg = {
        **_BASE_CFG,
        "executor": "claude-impl",
        "reviewer": "claude-impl",
        "executors": {
            "claude-impl": {"type": "claude-process"},
            "codex-review": {"type": "codex"},
        },
        "pools": {"default": {"executors": ["claude-impl", "codex-review"]}},
        "default_pool": "default",
        "review_fallback": "needs_review",
    }

    with pytest.raises(SystemExit):
        cmd_route(cfg, tickets_root, "TICK-001", model="claude-sonnet-5")

    assert path.read_text() == before
    assert "unmapped model 'claude-sonnet-5' for executor 'codex'" in capsys.readouterr().err


def test_cmd_route_commits_successful_routing_update(tickets_root, capsys):
    _make_ticket(tickets_root / "tickets", "TICK-001", "draft")
    for args in (
        ["git", "init"],
        ["git", "config", "user.email", "tests@example.invalid"],
        ["git", "config", "user.name", "LaneGate Tests"],
        ["git", "add", "tickets/TICK-001.md"],
        ["git", "commit", "-m", "initial ticket"],
    ):
        subprocess.run(args, cwd=tickets_root, check=True, capture_output=True)
    before_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tickets_root, check=True, capture_output=True, text=True
    ).stdout

    cmd_route(
        {**_BASE_CFG, "commit_status_changes": True},
        tickets_root,
        "TICK-001",
        reviewer="codex",
    )

    after_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tickets_root, check=True, capture_output=True, text=True
    ).stdout
    assert after_head != before_head
    assert "tickets/TICK-001.md" in subprocess.run(
        ["git", "show", "--format=", "--name-only", "HEAD"],
        cwd=tickets_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert parse_ticket(tickets_root / "tickets" / "TICK-001.md")["reviewer"] == "codex"
    assert not subprocess.run(
        ["git", "status", "--short", "--", "tickets/TICK-001.md"],
        cwd=tickets_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_cmd_route_fails_when_required_ticket_commit_is_rejected(tickets_root, capsys):
    _make_ticket(tickets_root / "tickets", "TICK-001", "draft")
    for args in (
        ["git", "init"],
        ["git", "config", "user.email", "tests@example.invalid"],
        ["git", "config", "user.name", "LaneGate Tests"],
        ["git", "add", "tickets/TICK-001.md"],
        ["git", "commit", "-m", "initial ticket"],
    ):
        subprocess.run(args, cwd=tickets_root, check=True, capture_output=True)
    hook = tickets_root / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    ticket_path = tickets_root / "tickets" / "TICK-001.md"
    before_ticket = ticket_path.read_text()
    before_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tickets_root, check=True, capture_output=True, text=True
    ).stdout

    with pytest.raises(SystemExit):
        cmd_route(
            {**_BASE_CFG, "commit_status_changes": True},
            tickets_root,
            "TICK-001",
            reviewer="codex",
        )

    after_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tickets_root, check=True, capture_output=True, text=True
    ).stdout
    assert after_head == before_head
    assert ticket_path.read_text() == before_ticket
    assert not subprocess.run(
        ["git", "status", "--short", "--", "tickets/TICK-001.md"],
        cwd=tickets_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    output = capsys.readouterr()
    assert "failed to commit generated ticket write" in output.err
    assert "Updated routing" not in output.out


def test_cmd_route_json_update_emits_only_json_and_preserves_review_state(tickets_root, capsys):
    from lanegate.board import cmd_route
    from lanegate.ticket import parse_ticket

    _make_ticket(tickets_root / "tickets", "TICK-001", "draft")

    cmd_route(
        {**_BASE_CFG, "executors": {"claude-b": {"type": "claude-process"}}},
        tickets_root,
        "TICK-001",
        json_output=True,
        reviewer="codex",
        executor="claude-b",
        model="gpt-5.6-terra",
    )

    data = json.loads(capsys.readouterr().out)
    assert data["id"] == "TICK-001"
    ticket = parse_ticket(tickets_root / "tickets" / "TICK-001.md")
    assert ticket["reviewer"] == "codex"
    assert ticket["executor"] == "claude-b"
    assert ticket["review_model_pin"] == "gpt-5.6-terra"
    assert "review_model" not in ticket
    assert "review_driver" not in ticket
    assert "implement_executor" not in ticket
