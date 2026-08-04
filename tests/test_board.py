"""Tests for board.py — cmd_board renders [DRAFT] section; cmd_next excludes drafts."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from lanegate.board import (
    _new_board_table,
    _ticket_data,
    _ticket_flags,
    cmd_blocked,
    cmd_board,
    cmd_next,
    cmd_pipeline_status,
    cmd_route,
)
from lanegate.git import PendingCommits

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
    touches=None,
    priority: int = 5,
    parallel_safe: bool = True,
    review_verdict=None,
    complexity=None,
    depends_on=None,
) -> None:
    frontmatter = f"id: {ticket_id}\ntitle: Test {ticket_id}\nstatus: {status}\npriority: {priority}\nparallel_safe: {str(parallel_safe).lower()}\n"
    if review_verdict is not None:
        frontmatter += f"review_verdict: {review_verdict}\n"
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
    assert "lanegate orchestrate --status" in out


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


def _make_milestone_ticket(tickets_dir, ticket_id, status, milestone=None):
    ms_line = f"milestone: {milestone}\n" if milestone else ""
    content = (
        f"---\n"
        f"id: {ticket_id}\n"
        f"title: Test {ticket_id}\n"
        f"status: {status}\n"
        f"priority: 5\n"
        f"parallel_safe: true\n"
        f"{ms_line}"
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
    assert "check: lanegate orchestrate" in out
    assert "--status" in out
    assert "TICK-002: hibernated - next: lanegate orchestrate" in out
    assert "TICK-003: needs_review" in out
    assert "lanegate reopen TICK-003 &&" in out
    assert "TICK-004: changes_requested" in out


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
    assert "Blocked tickets" in out


def test_blocked_in_review_with_changes_requested(tickets_root, capsys):
    """cmd_blocked should include tickets with status=in_review and review_verdict=changes_requested.
    
    This happens when the acceptance-contract audit fails after the executor
    has already moved the ticket to in_review with approved verdict.
    """
    td = tickets_root / "tickets"
    _make_ticket(td, "TICK-001", "in_review", touches=["src/foo.py"], review_verdict="changes_requested")
    cmd_blocked(_BASE_CFG, tickets_root)
    out = capsys.readouterr().out
    assert "TICK-001" in out
    assert "Blocked tickets" in out


def test_blocked_mixed_statuses(tickets_root, capsys):
    """cmd_blocked should include both code_complete and in_review tickets with changes_requested."""
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
    assert "TICK-002" in out
    assert "TICK-003" not in out  # no changes_requested verdict
    assert "TICK-004" not in out  # approved, not changes_requested


def test_blocked_json_output(tickets_root, capsys):
    """cmd_blocked --json should include both code_complete and in_review tickets."""
    td = tickets_root / "tickets"
    _make_ticket(
        td, "TICK-001", "code_complete", touches=["src/foo.py"], review_verdict="changes_requested"
    )
    _make_ticket(
        td, "TICK-002", "in_review", touches=["src/bar.py"], review_verdict="changes_requested"
    )
    cmd_blocked(_BASE_CFG, tickets_root, json_output=True)
    out = capsys.readouterr().out
    data = json.loads(out)
    ids = [t["id"] for t in data]
    assert "TICK-001" in ids
    assert "TICK-002" in ids
    assert len(data) == 2


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


def test_pipeline_status_valid_empty_range_is_up_to_date(tickets_root, capsys):
    with patch("lanegate.board.pending_commits", return_value=PendingCommits([])):
        cmd_pipeline_status(_PIPELINE_CFG, tickets_root)

    assert "up to date" in capsys.readouterr().out
