"""Tests for acceptance_contract.py."""

"""Tests for analyze.py — cmd_analyze with stubbed model seam."""

import json
import shutil
import signal
import socket
import subprocess as _subprocess
import threading
import time
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

import pytest

from lanegate.analyze import (
    _HAS_TREE_SITTER,
    _TS_LANGUAGE_MAP,
    _already_resolved_reason_matches_worktree,
    _ast_symbol_hits,
    _build_ast_index,
    _build_candidate_skeletons,
    _build_file_skeleton,
    _build_prompt,
    _call_model,
    _close_criteria_drifted,
    _extract_acceptance_checklist,
    _import_graph_expand,
    _index_non_py_file,
    _index_py_file,
    _parse_response,
    _repo_structure,
    _ripgrep_seed,
    _treesitter_hits,
    audit_acceptance_contract,
    cmd_analyze,
    companion_docs_from_criteria,
    correct_touches_by_basename,
    enrich_context,
    infer_touches_from_criteria,
    validate_touched_paths,
    verify_acceptance_criteria,
)
from lanegate.config import ConfigError
from lanegate.ticket import parse_ticket, validate_ticket

_CFG = {
    "ticket_prefix": "TICK",
    "tickets_dir": "tickets",
    "commit_status_changes": False,
}

_GOOD_RESPONSE = json.dumps(
    {
        "touches": ["lanegate/foo.py", "tests/test_foo.py"],
        "close_criteria": "cmd_foo writes a file and returns 0.",
        "depends_on": [],
    }
)


@pytest.fixture(autouse=True)
def _resolve_executor_bins_to_their_names(monkeypatch):
    monkeypatch.setattr("lanegate.executor.shutil.which", lambda bin_name: bin_name)


def _make_draft(
    tickets_dir: Path,
    ticket_id: str = "TICK-001",
    title: str = "Add foo command",
    touches: list[str] | None = None,
) -> Path:
    if touches:
        touches_yaml = "touches:\n" + "".join(f"  - {touch}\n" for touch in touches)
    else:
        touches_yaml = "touches: []\n"
    fm = f"id: {ticket_id}\ntitle: {title}\nstatus: draft\npriority: 3\n{touches_yaml}"
    path = tickets_dir / f"{ticket_id}.md"
    path.write_text(f"---\n{fm}---\n## Background\nWe need a foo command.\n")
    return path


def _init_git_repo(path: Path) -> None:
    _subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    _subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)


@pytest.fixture
def repo(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    return tmp_path


def test_acceptance_contract_audit_blocks_close_criteria_narrowed_since_analyze(repo):
    """Regression (TICK-545): a later commit that quietly narrows close_criteria to
    match a reduced implementation must not read as a clean, self-consistent pass."""
    if shutil.which("git") is None:
        pytest.skip("git is required for this test")
    _init_git_repo(repo)
    ticket_path = repo / "tickets" / "TICK-545.md"
    ticket_path.parent.mkdir(exist_ok=True)
    ticket_path.write_text(
        "---\nid: TICK-545\nclose_criteria: create() wraps cmd_create + cmd_analyze\n---\nbody\n"
    )
    _subprocess.run(["git", "add", "tickets/TICK-545.md"], cwd=repo, check=True)
    _subprocess.run(["git", "commit", "-m", "analyzed"], cwd=repo, check=True, capture_output=True)
    sha = _subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    ticket = {
        "id": "TICK-545",
        "title": "Expose create over MCP",
        "_body": "wrap create + analyze",
        "close_criteria": "create() writes a draft ticket",
        "analyzed_at_sha": sha,
        "_path": ticket_path,
    }

    audit = audit_acceptance_contract(ticket, repo)

    assert audit.ok is False
    assert any("without a recorded human approval" in f for f in audit.findings)


def test_acceptance_contract_audit_allows_narrowed_close_criteria_with_human_approval(repo):
    """Same drift as above, but with an owner-recorded approval — must not block."""
    if shutil.which("git") is None:
        pytest.skip("git is required for this test")
    _init_git_repo(repo)
    ticket_path = repo / "tickets" / "TICK-545.md"
    ticket_path.parent.mkdir(exist_ok=True)
    ticket_path.write_text(
        "---\nid: TICK-545\nclose_criteria: create() wraps cmd_create + cmd_analyze\n---\nbody\n"
    )
    _subprocess.run(["git", "add", "tickets/TICK-545.md"], cwd=repo, check=True)
    _subprocess.run(["git", "commit", "-m", "analyzed"], cwd=repo, check=True, capture_output=True)
    sha = _subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    ticket = {
        "id": "TICK-545",
        "title": "Expose create over MCP",
        "_body": "wrap create + analyze",
        "close_criteria": "create() writes a draft ticket",
        "analyzed_at_sha": sha,
        "_path": ticket_path,
        "close_criteria_drift_approved_at": "2026-08-13T16:00:00Z",
        "close_criteria_drift_approved_snapshot": "create() writes a draft ticket",
    }

    audit = audit_acceptance_contract(ticket, repo)

    assert audit.ok is True
    assert audit.findings == []


def test_close_criteria_drift_approval_self_invalidates_on_further_edit(repo):
    """An approval tied to specific close_criteria text must not cover a later, further edit."""
    if shutil.which("git") is None:
        pytest.skip("git is required for this test")
    _init_git_repo(repo)
    ticket_path = repo / "tickets" / "TICK-545.md"
    ticket_path.parent.mkdir(exist_ok=True)
    ticket_path.write_text(
        "---\nid: TICK-545\nclose_criteria: create() wraps cmd_create + cmd_analyze\n---\nbody\n"
    )
    _subprocess.run(["git", "add", "tickets/TICK-545.md"], cwd=repo, check=True)
    _subprocess.run(["git", "commit", "-m", "analyzed"], cwd=repo, check=True, capture_output=True)
    sha = _subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    ticket = {
        "id": "TICK-545",
        "title": "Expose create over MCP",
        "_body": "wrap create + analyze",
        "close_criteria": "create() only wraps cmd_create",
        "analyzed_at_sha": sha,
        "_path": ticket_path,
        "close_criteria_drift_approved_at": "2026-08-13T16:00:00Z",
        "close_criteria_drift_approved_snapshot": "create() writes a draft ticket",
    }

    audit = audit_acceptance_contract(ticket, repo)

    assert audit.ok is False
    assert any("close_criteria changed" in f for f in audit.findings)


def test_acceptance_contract_audit_catches_narrowed_close_criteria(repo):
    """Regression: linked design contract cannot be collapsed to endpoint tests only."""
    (repo / "docs").mkdir()
    (repo / "docs" / "v2-interface-boundaries.md").write_text(
        """
| Endpoint | Purpose | Response |
| --- | --- | --- |
| `POST /api/runs/start` | Start a run | `{run_id, status}` |
| `GET /api/runs/current` | Current run state | `{run_id, status, started_at}` |
| `GET /api/diff/{id}` | Diff for a ticket branch/worktree | `{id, base, branch, files: [{path, status, patch?}]}` |
| `POST /api/runs/stop` | Request graceful stop/cancel | `{run_id, status, stop_requested: true}` |
"""
    )
    ticket = {
        "id": "TICK-146",
        "title": "Implement local API",
        "_body": "Implement the local API from docs/v2-interface-boundaries.md.",
        "close_criteria": "tests/test_api.py passes for /api/board, /api/tickets, and /api/diff.",
    }

    audit = audit_acceptance_contract(ticket, repo)

    assert audit.ok is False
    joined = "\n".join(audit.findings)
    assert "docs/v2-interface-boundaries.md" in joined
    assert "run_id/status response" in joined
    assert "/api/runs/current" in joined
    assert "structured diff files" in joined
    assert "graceful stop" in joined


def test_acceptance_contract_audit_passes_when_close_criteria_covers_linked_contract(repo):
    (repo / "docs").mkdir()
    (repo / "docs" / "v2-interface-boundaries.md").write_text(
        """
| Endpoint | Purpose | Response |
| --- | --- | --- |
| `POST /api/runs/start` | Start a run | `{run_id, status}` |
| `GET /api/runs/current` | Current run state | `{run_id, status, started_at}` |
| `GET /api/diff/{id}` | Diff for a ticket branch/worktree | `{id, base, branch, files: [{path, status, patch?}]}` |
| `POST /api/runs/stop` | Request graceful stop/cancel | `{run_id, status, stop_requested: true}` |
"""
    )
    ticket = {
        "id": "TICK-147",
        "title": "Implement local API",
        "_body": "See docs/v2-interface-boundaries.md for the API contract.",
        "close_criteria": (
            "The docs/v2-interface-boundaries.md contract is implemented, including "
            "run_id/status responses, /api/runs/current, structured diff files, and graceful stop."
        ),
    }

    audit = audit_acceptance_contract(ticket, repo)

    assert audit.ok is True
    assert audit.findings == []


def test_extract_acceptance_checklist_finds_items_in_section():
    body = (
        "## Background\nSome prose.\n\n"
        "## Acceptance Criteria\n"
        "- [ ] Do the first thing\n"
        "- [x] Do the second thing\n\n"
        "## Status History\n- opened\n"
    )
    assert _extract_acceptance_checklist(body) == [
        "Do the first thing",
        "Do the second thing",
    ]


def test_extract_acceptance_checklist_matches_heading_as_first_line():
    """Regression: a body that starts *with* the heading (no leading blank
    line) must still match -- not every ticket has a Background section
    before Acceptance Criteria."""
    body = "## Acceptance Criteria\n- [ ] Add a widget function\n"
    assert _extract_acceptance_checklist(body) == ["Add a widget function"]


def test_extract_acceptance_checklist_matches_lowercase_heading_variant():
    body = "## Acceptance criteria\n- [ ] Add a widget function\n"
    assert _extract_acceptance_checklist(body) == ["Add a widget function"]


def test_extract_acceptance_checklist_empty_when_no_section():
    assert _extract_acceptance_checklist("## Background\nJust prose.\n") == []


def test_verify_acceptance_criteria_verified_when_diff_matches(repo):
    ticket = {
        "id": "TICK-400",
        "_body": "## Acceptance Criteria\n- [ ] Add a widget function\n",
    }
    records = verify_acceptance_criteria(
        ticket, repo, diff_text="def add_widget_function(): pass"
    )
    assert len(records) == 1
    assert records[0].status == "verified"
    assert "add" in records[0].evidence


def test_verify_acceptance_criteria_unverified_when_diff_does_not_match(repo):
    ticket = {
        "id": "TICK-401",
        "_body": "## Acceptance Criteria\n- [ ] Add a widget function\n",
    }
    records = verify_acceptance_criteria(ticket, repo, diff_text="unrelated content")
    assert len(records) == 1
    assert records[0].status == "unverified"


def test_verify_acceptance_criteria_manual_for_human_judgment_wording(repo):
    ticket = {
        "id": "TICK-402",
        "_body": "## Acceptance Criteria\n"
        "- [ ] Confirm the actual literal format against a live capture\n",
    }
    records = verify_acceptance_criteria(ticket, repo, diff_text="anything at all")
    assert len(records) == 1
    assert records[0].status == "manual"


def test_verify_acceptance_criteria_suite_green_auto_verified(repo):
    """'Full suite green' is already enforced by the separate pre_complete/
    pre_merge pytest safeguard -- it must not need its own diff-text match,
    or nearly every ticket would land with an unresolved criterion."""
    ticket = {
        "id": "TICK-403",
        "_body": "## Acceptance Criteria\n- [ ] Full suite green.\n",
    }
    records = verify_acceptance_criteria(ticket, repo, diff_text="")
    assert len(records) == 1
    assert records[0].status == "verified"


def test_verify_acceptance_criteria_preserves_prior_manual_signoff(repo):
    """A criterion a human already signed off on (status='manual' from a
    prior --findings) must not silently revert to 'unverified' on a later
    re-verification just because automated matching still can't confirm it."""
    ticket = {
        "id": "TICK-404",
        "_body": "## Acceptance Criteria\n- [ ] Some subjective UX quality bar\n",
    }
    prior = [
        {
            "criterion": "Some subjective UX quality bar",
            "status": "manual",
            "evidence": "human judgment via review findings: looks good",
            "checked_at": "2026-07-29T00:00:00Z",
        }
    ]
    records = verify_acceptance_criteria(
        ticket, repo, prior=prior, diff_text="unrelated content"
    )
    assert len(records) == 1
    assert records[0].status == "manual"
    assert records[0].evidence == "human judgment via review findings: looks good"


def test_verify_acceptance_criteria_empty_when_no_checklist(repo):
    ticket = {"id": "TICK-405", "_body": "## Background\nNo checklist here.\n"}
    assert verify_acceptance_criteria(ticket, repo, diff_text="anything") == []


def test_acceptance_contract_audit_ignores_code_fence_contents(repo):
    """Regression: illustrative code in a Design section is not a contract item."""
    ticket = {
        "id": "TICK-030",
        "title": "Generalize run_review_agent",
        "_body": (
            "## Design\n"
            "```python\n"
            "def run_review_agent(ticket, repo_root, cfg=None):\n"
            "    if cfg is None:\n"
            "        return env\n"
            "    return (\n"
            "        cmd\n"
            "    )\n"
            "```\n"
        ),
        "close_criteria": "run_review_agent accepts cfg and uses resolve_driver.",
    }

    audit = audit_acceptance_contract(ticket, repo)

    assert audit.ok is True
    assert audit.findings == []


def test_acceptance_contract_audit_ignores_incidental_ticket_mentions(repo):
    """Regression: mentioning another ticket's ID in background prose does not
    import that ticket's own close_criteria as a required contract — even when
    the mentioned ticket is a real depends_on entry."""
    (repo / "tickets" / "TICK-033.md").write_text(
        "---\nid: TICK-033\ntitle: Validation\nstatus: merged\ntouches: [lanegate/ticket.py]\n"
        "---\n"
        'errors.append(f"executor must be one of {sorted(_VALID_EXECUTOR_TYPES)}")\n'
    )
    ticket = {
        "id": "TICK-034",
        "title": "Docs update",
        "_body": "The multi-agent dispatch system (TICK-028 through TICK-033) adds drivers.",
        "close_criteria": ".lanegate.yml.example gets a drivers: block with examples.",
        "depends_on": ["TICK-033"],
        "_path": str(repo / "tickets" / "TICK-034.md"),
    }

    audit = audit_acceptance_contract(ticket, repo)

    assert audit.ok is True
    assert audit.findings == []


def test_acceptance_contract_audit_ignores_unrelated_sections_of_a_shared_reference_doc(repo):
    """Regression: linking a large multi-section reference doc for background
    must not import every unrelated field in it as a required contract item —
    only the section(s) the ticket's own text actually names should count.
    Reproduces the real-world false positive where a worker-pool ticket that
    merely cites docs/config-reference.md got blocked over an unrelated Ollama
    field it never mentioned."""
    (repo / "docs").mkdir()
    (repo / "docs" / "config-reference.md").write_text(
        """
## executor configuration

| Field | Required | Description |
| --- | --- | --- |
| `max_parallel` | No | Max concurrent tickets for this executor pool. |

## reviewers

LaneGate invokes `ollama run <model> <prompt>`, so the selected models must
already be pulled locally.
"""
    )
    ticket = {
        "id": "TICK-089",
        "title": "Multi-instance worker pool",
        "_body": "Distribute tickets across executor accounts, per docs/config-reference.md.",
        "close_criteria": "next_batch() returns (ticket, executor_instance) pairs sized to max_parallel.",
    }

    audit = audit_acceptance_contract(ticket, repo)

    assert audit.ok is True
    assert audit.findings == []


def test_acceptance_contract_audit_still_catches_relevant_section_of_a_shared_reference_doc(repo):
    """The scoping in the test above must not become a blanket exemption for
    linked docs — a section the ticket's own text actually names should still
    be enforced."""
    (repo / "docs").mkdir()
    (repo / "docs" / "config-reference.md").write_text(
        """
## executor pool

| Field | Required | Description |
| --- | --- | --- |
| `max_parallel` | No | Max concurrent tickets for this executor pool. |

## reviewers

LaneGate invokes `ollama run <model> <prompt>`, so the selected models must
already be pulled locally.
"""
    )
    ticket = {
        "id": "TICK-089b",
        "title": "Executor pool",
        "_body": "Update the executor pool section of docs/config-reference.md.",
        "close_criteria": "Config docs updated.",
    }

    audit = audit_acceptance_contract(ticket, repo)

    assert audit.ok is False
    joined = "\n".join(audit.findings)
    assert "max_parallel" in joined
    assert "Field | Required | Description" not in joined
    assert "ollama" not in joined.lower()


def test_acceptance_contract_audit_ignores_rationale_prose_but_catches_main_body_requirement(repo):
    """Regression for TICK-030: a 'Rationale:' paragraph explaining an
    architectural choice is not itself an acceptance item, even when it
    contains a should/must word -- but a genuine requirement stated in the
    ticket's main body must still be caught."""
    ticket = {
        "id": "TICK-030",
        "title": "Generalize run_review_agent",
        "_body": (
            "## Design\n\n"
            "Rationale: review-driver dispatch is a core executor-adapter concern. "
            "The UI and any runner should consume an already-resolved review "
            "driver rather than owning routing rules for the review step.\n\n"
            "## Requirements\n\n"
            "- The endpoint must return a structured `{status}` response.\n"
        ),
        "close_criteria": "run_review_agent accepts cfg and uses resolve_driver.",
    }

    audit = audit_acceptance_contract(ticket, repo)

    assert audit.ok is False
    joined = "\n".join(audit.findings)
    assert "runner should consume" not in joined
    assert "must return a structured" in joined


def test_acceptance_contract_audit_ignores_table_header_row(repo):
    """Regression: a bare table header row (e.g. 'Field | Required |
    Description') must not be extracted as a contract item just because a
    column name matches a contract verb -- only real data rows count."""
    ticket = {
        "id": "TICK-999",
        "title": "Config table",
        "_body": (
            "| Field | Required | Description |\n"
            "| --- | --- | --- |\n"
            "| `api_key_env` | No | Env var holding the API key. |\n"
        ),
        "close_criteria": "Config docs updated.",
    }

    audit = audit_acceptance_contract(ticket, repo)

    assert audit.ok is False
    joined = "\n".join(audit.findings)
    assert "api_key_env" in joined
    assert "Field | Required | Description" not in joined


def test_acceptance_contract_audit_ignores_incidental_heading_overlap_in_a_long_ticket(repo):
    """Regression for TICK-048: a long ticket body sharing one incidental word
    with an unrelated doc heading (e.g. 'resume' appearing in background
    prose about resuming hibernated tickets, and a doc heading 'Rate limits
    and auto-resume') must not pull in that whole unrelated section -- only
    the doc path's own mention should be judged for relevance."""
    (repo / "docs").mkdir()
    (repo / "docs" / "config-reference.md").write_text(
        """
## Rate limits and auto-resume

- The resume-watch daemon must poll every 30 seconds.
- It should page on-call after three missed polls.

## groups

- `max_parallel` must be set per group.
"""
    )
    ticket = {
        "id": "TICK-048",
        "title": "MLT-01: Grouped orchestration",
        "_body": (
            "Add scoped locks and per-group executor routing.\n\n"
            "A hibernated ticket may later resume once its group has capacity "
            "again -- this is unrelated to the group config format itself.\n\n"
            "See docs/config-reference.md for the groups config shape.\n"
        ),
        "close_criteria": "Group config validation and scoped locking pass per tests/test_config.py.",
    }

    audit = audit_acceptance_contract(ticket, repo)

    joined = "\n".join(audit.findings)
    assert "resume-watch daemon" not in joined
    assert "page on-call" not in joined


def test_acceptance_contract_audit_ignores_own_prior_needs_review_reason(repo):
    """Regression: a ticket's own '## Needs Review Reason' section (appended by
    cmd_needs_review after a prior failed audit) must not be re-scanned as new
    contract material — otherwise reopening a ticket compounds the same finding
    on every subsequent audit instead of clearing it."""
    ticket = {
        "id": "TICK-156",
        "title": "Go TUI implementation",
        "_body": (
            "## What\n\nBuild the TUI.\n\n"
            "## Needs Review Reason\n\n"
            "acceptance-contract audit failed: close_criteria omits contract items "
            "from ticket title/body: some prior finding that must be restated.\n"
        ),
        "close_criteria": "Build the TUI per the What section.",
    }

    audit = audit_acceptance_contract(ticket, repo)

    assert audit.ok is True
    assert audit.findings == []


def test_acceptance_contract_audit_ignores_own_stored_audit_section(repo):
    """Regression (TICK-481): the '## Acceptance Contract Audit' section that
    lanegate appends to ticket bodies must not be re-scanned as contract
    material.  Without this guard, stored findings that mention linked docs
    (e.g. docs/ARCHITECTURE.md) cause those docs to be pulled in as contract
    sources on every subsequent audit, and stored 'omitted item' strings are
    re-extracted as fresh requirements — compounding findings on every cycle."""
    # Simulate a ticket whose body contains the audit output from a previous run.
    # The stored text names docs/ARCHITECTURE.md and lanegate/skills/implement.md,
    # which the audit's linked-doc scanner would normally load as contract sources.
    # With the fix those references are stripped before scanning, so neither doc
    # is loaded and no spurious findings are generated.
    ticket = {
        "id": "TICK-999",
        "title": "Fix dead agent-notes mechanism",
        "_body": (
            "## Background\n\nBuild the notes mechanism.\n\n"
            "## Acceptance Contract Audit\n"
            "**Status**: failed (2 findings)\n\n"
            "**Findings**:\n"
            "- close_criteria omits contract items from docs/ARCHITECTURE.md:"
            " create` | **Code** | Allocate id, write draft, git commit.\n"
            "- close_criteria omits contract items from lanegate/skills/implement.md:"
            ' Write **factual constraints only**: "X fails if Y"\n\n'
            "**Omitted Items**:\n"
            "- create` | **Code** | Allocate id, write draft, git commit. Mechanical.\n"
        ),
        "close_criteria": (
            "pytest tests/test_ticket.py tests/test_analyze.py tests/test_executor.py passes, "
            "with test_collect_cross_ticket_change_notes asserting overlapping file "
            "change_notes are collected across merged tickets and injected into "
            "analyze/implement prompts."
        ),
    }

    audit = audit_acceptance_contract(ticket, repo)

    assert audit.ok is True, (
        "Stored '## Acceptance Contract Audit' section must not feed back into the audit. "
        f"Got findings: {audit.findings}"
    )
    assert audit.findings == []


def test_analyze_records_acceptance_contract_audit_metadata(repo):
    _make_draft(repo / "tickets")
    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: _GOOD_RESPONSE)
    raw = (repo / "tickets" / "TICK-001.md").read_text(encoding="utf-8")
    assert "acceptance_contract_audit_summary: ok" in raw
    assert "acceptance_contract_audit:" not in raw.split("---\n")[1]
    assert "## Acceptance Contract Audit" in raw

    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t["acceptance_contract_audit"]["ok"] is True
    assert isinstance(t["acceptance_contract_audit"]["checked_items"], list)


def test_acceptance_contract_audit_sentence_splits_unwrapped_bullet(repo):
    """Regression for TICK-171: an unwrapped paragraph as a single bullet with
    multiple sentences should extract only sentences containing contract signals
    (verb, endpoint, or brace), not the whole line.

    Per this project's CLAUDE.md convention, each paragraph is one unwrapped
    line. A bullet containing both rationale prose and one requirement sentence
    should not extract the whole paragraph as one opaque finding."""
    ticket = {
        "id": "TICK-171",
        "title": "Fix over-extraction",
        "_body": (
            "- Rationale: the dispatcher is complex and hard to reason about. "
            "The endpoint must return a structured {status} response."
        ),
        "close_criteria": (
            "The endpoint must return a structured {status} response."
        ),
    }

    audit = audit_acceptance_contract(ticket, repo)

    assert audit.ok is True
    # The rationale sentence should not appear in checked_items or findings
    joined_items = " ".join(audit.checked_items)
    assert "dispatcher is complex" not in joined_items
    # The actual requirement sentence should be in checked_items
    assert "status" in joined_items or "response" in joined_items


def test_acceptance_contract_audit_sentence_splits_multiple_signals(repo):
    """Bullet with multiple sentences, each carrying different signals."""
    ticket = {
        "id": "TICK-172",
        "title": "Multi-signal bullet",
        "_body": (
            "- Background noise here. The system should validate input. "
            "More noise. The endpoint must support GET /api/status."
        ),
        "close_criteria": (
            "The system should validate input. The endpoint must support GET /api/status."
        ),
    }

    audit = audit_acceptance_contract(ticket, repo)

    assert audit.ok is True
    joined_items = " ".join(audit.checked_items)
    # Should have extracted the "should validate" sentence
    assert "validate" in joined_items or "input" in joined_items
    # Should have extracted the "must support GET" sentence
    assert "/api/status" in joined_items or "status" in joined_items


def test_acceptance_contract_audit_table_row_still_extracted_whole(repo):
    """Table rows should still be extracted as-is (not sentence-split),
    since they are structured by construction."""
    ticket = {
        "id": "TICK-173",
        "title": "Table test",
        "_body": (
            "| Field | Required | Description |\n"
            "| --- | --- | --- |\n"
            "| `token` | Yes | Token must be set in env. Additional context here. |"
        ),
        "close_criteria": "Configuration implemented.",
    }

    audit = audit_acceptance_contract(ticket, repo)

    assert audit.ok is False
    joined_items = " ".join(audit.checked_items)
    # Table row should be extracted as a single item (not sentence-split)
    assert "`token`" in joined_items or "token" in joined_items


def test_acceptance_contract_audit_treats_non_goals_as_not_applicable(repo):
    """Regression: a ticket's Non-Goals section should exclude otherwise-
    in-scope linked-doc items from the audit as not applicable. A linked doc
    with both in-scope and explicitly non-goaled items should only flag the
    in-scope portion as missing if not covered by close_criteria, and omit
    the non-goaled portion entirely from omitted_items/findings."""
    (repo / "docs").mkdir()
    (repo / "docs" / "tui-plan.md").write_text(
        """
## MVP Screens (TICK-156)

- Screen 1: Ticket board overview
- Screen 2: Ticket detail view

## Future Screens (TICK-157)

- Screen 3: Diff view with unified/split modes
- Screen 4: Orchestration run viewer
- Screen 5: Settings and preferences
"""
    )
    ticket = {
        "id": "TICK-156",
        "title": "Go TUI implementation",
        "_body": (
            "## Implementation\n\n"
            "Build the TUI per docs/tui-plan.md.\n\n"
            "## Non-Goals\n\n"
            "Diff view, orchestration run viewer, and settings screens "
            "are deferred to TICK-157."
        ),
        "close_criteria": (
            "Screen 1 (ticket board overview) and Screen 2 (ticket detail view) "
            "are implemented and working."
        ),
    }

    audit = audit_acceptance_contract(ticket, repo)

    # The audit should pass because:
    # - "Screen 1" and "Screen 2" are covered by close_criteria
    # - "Screen 3", "Screen 4", "Screen 5" are covered by the Non-Goals section
    assert audit.ok is True, f"Expected audit to pass, but got findings: {audit.findings}"
    assert audit.findings == []
    # The omitted_items should not include the non-goaled screens
    omitted_str = " ".join(audit.omitted_items)
    assert "diff view" not in omitted_str.lower()
    assert "orchestration" not in omitted_str.lower()
    assert "settings" not in omitted_str.lower()


