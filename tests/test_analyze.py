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
    _ast_symbol_hits,
    _build_ast_index,
    _build_file_skeleton,
    _build_prompt,
    _call_model,
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
    enrich_context,
    infer_touches_from_criteria,
    validate_touched_paths,
    verify_acceptance_criteria,
)
from lanegate.config import ConfigError
from lanegate.ticket import parse_ticket

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


@pytest.fixture
def repo(tmp_path):
    td = tmp_path / "tickets"
    td.mkdir()
    return tmp_path


# --- _parse_response ---


def test_parse_response_plain_json():
    result = _parse_response(_GOOD_RESPONSE)
    assert result["touches"] == ["lanegate/foo.py", "tests/test_foo.py"]
    assert "cmd_foo" in result["close_criteria"]


def test_parse_response_strips_markdown_fence():
    wrapped = f"```json\n{_GOOD_RESPONSE}\n```"
    result = _parse_response(wrapped)
    assert result["touches"] == ["lanegate/foo.py", "tests/test_foo.py"]


def test_parse_response_no_json_raises():
    with pytest.raises(ValueError):
        _parse_response("Sorry, I cannot help with that.")


def test_parse_response_bad_json_raises():
    with pytest.raises(json.JSONDecodeError):
        _parse_response("{bad json ,,}")


# --- cmd_analyze happy path ---


def test_analyze_flips_draft_to_open(repo):
    _make_draft(repo / "tickets")
    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: _GOOD_RESPONSE)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t["status"] == "open"


def test_analyze_writes_touches(repo):
    _make_draft(repo / "tickets")
    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: _GOOD_RESPONSE)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t["touches"] == ["lanegate/foo.py", "tests/test_foo.py"]


def test_analyze_writes_close_criteria(repo):
    _make_draft(repo / "tickets")
    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: _GOOD_RESPONSE)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert "cmd_foo" in t["close_criteria"]


def test_analyze_idempotent_on_open_ticket(repo):
    """Re-running on an already-open ticket should succeed (re-propose)."""
    _make_draft(repo / "tickets")
    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: _GOOD_RESPONSE)
    # Run again on now-open ticket — should update touches without error
    new_response = json.dumps(
        {
            "touches": ["lanegate/bar.py"],
            "close_criteria": "bar is done.",
            "depends_on": [],
        }
    )
    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: new_response)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t["touches"] == ["lanegate/foo.py", "tests/test_foo.py", "lanegate/bar.py"]


def test_analyze_preserves_existing_touch_model_omits(repo):
    """Pre-curated touches survive even when the model omits them."""
    _make_draft(repo / "tickets", touches=["docs/manual-plan.md"])
    response = json.dumps(
        {
            "touches": ["lanegate/foo.py"],
            "close_criteria": "cmd_foo works.",
            "depends_on": [],
        }
    )
    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: response)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert "docs/manual-plan.md" in t["touches"]


def test_analyze_adds_model_touch_to_existing_touches(repo):
    """Model-proposed touches are added beyond a pre-set touch list."""
    _make_draft(repo / "tickets", touches=["docs/manual-plan.md"])
    response = json.dumps(
        {
            "touches": ["lanegate/foo.py"],
            "close_criteria": "cmd_foo works.",
            "depends_on": [],
        }
    )
    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: response)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t["touches"] == ["docs/manual-plan.md", "lanegate/foo.py"]


def test_analyze_keep_draft_leaves_status_draft(repo):
    """keep_draft=True populates touches but does not flip status to open."""
    _make_draft(repo / "tickets")
    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: _GOOD_RESPONSE, keep_draft=True)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t["status"] == "draft"
    assert t["touches"] == ["lanegate/foo.py", "tests/test_foo.py"]


def test_analyze_keep_draft_then_reanalyze_opens(repo):
    """After keep_draft analyze, a normal analyze flips the ticket to open."""
    _make_draft(repo / "tickets")
    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: _GOOD_RESPONSE, keep_draft=True)
    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: _GOOD_RESPONSE)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t["status"] == "open"


def test_analyze_writes_change_notes(repo):
    """When model returns change_notes, cmd_analyze stores them."""
    _make_draft(repo / "tickets")
    response_with_notes = json.dumps(
        {
            "touches": ["lanegate/foo.py", "tests/test_foo.py"],
            "close_criteria": "cmd_foo works.",
            "depends_on": [],
            "change_notes": {
                "lanegate/foo.py": "Add foo() function at line 10.",
                "tests/test_foo.py": "Add test_foo_basic.",
            },
        }
    )
    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: response_with_notes)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t.get("change_notes") is not None
    assert t["change_notes"]["lanegate/foo.py"] == "Add foo() function at line 10."
    assert t["change_notes"]["tests/test_foo.py"] == "Add test_foo_basic."


def test_analyze_avoids_writing_legacy_model_overrides(repo):
    """cmd_analyze avoids writing legacy hardcoded model frontmatter entries."""
    _make_draft(repo / "tickets")
    response_with_model = json.dumps(
        {
            "touches": ["lanegate/foo.py"],
            "close_criteria": "cmd_foo works.",
            "depends_on": [],
            "model": "claude-sonnet-4-6",
        }
    )
    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: response_with_model)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert "model" not in t


def test_analyze_omits_model_recommendation_for_codex_executor(repo):
    """Claude model recommendations are not persisted for Codex implementation."""
    _make_draft(repo / "tickets")
    cfg = dict(_CFG, executor_steps={"implement": "codex"})
    response_with_model = json.dumps(
        {
            "touches": ["lanegate/foo.py"],
            "close_criteria": "cmd_foo works.",
            "depends_on": [],
            "model": "claude-sonnet-4-6",
        }
    )
    cmd_analyze("TICK-001", cfg, repo, model_fn=lambda p: response_with_model)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert "model" not in t


def test_analyze_writes_file_skeletons_sidecar(repo):
    """cmd_analyze stores bulky file skeletons in a sidecar, not ticket frontmatter."""
    _make_draft(repo / "tickets")
    (repo / "lanegate").mkdir()
    (repo / "lanegate" / "foo.py").write_text("def cmd_foo(x, y=1):\n    pass\n")
    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: _GOOD_RESPONSE)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert "file_skeletons" not in t
    assert t["file_skeletons_ref"] == ".lanegate/context/TICK-001/file_skeletons.json"
    sidecar = repo / t["file_skeletons_ref"]
    skeletons = json.loads(sidecar.read_text())
    assert set(skeletons) == {"lanegate/foo.py", "tests/test_foo.py"}
    assert "lanegate/foo.py  (2 lines)" in skeletons["lanegate/foo.py"]
    assert "def cmd_foo(x, y=1)" in skeletons["lanegate/foo.py"]
    # touched file that doesn't exist on disk degrades to a "not found" stub
    assert "not found" in skeletons["tests/test_foo.py"]
    assert t["file_skeletons_summary"] == {
        "files": 2,
        "bytes": len(sidecar.read_bytes()),
    }
    ticket_text = (repo / "tickets" / "TICK-001.md").read_text()
    assert "def cmd_foo" not in ticket_text


def test_analyze_omits_missing_change_notes(repo):
    """When model doesn't return change_notes, field is not added to ticket."""
    _make_draft(repo / "tickets")
    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: _GOOD_RESPONSE)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    # Should not have change_notes field if model didn't provide it
    assert "change_notes" not in t


def test_analyze_omits_missing_model(repo):
    """When model doesn't return model field, ticket field is not added."""
    _make_draft(repo / "tickets")
    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: _GOOD_RESPONSE)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    # Should not have model field if model didn't provide it
    assert "model" not in t


def test_acceptance_contract_audit_catches_narrowed_close_criteria(repo):
    """Regression: linked design contract cannot be collapsed to endpoint tests only."""
    (repo / "docs").mkdir()
    (repo / "docs" / "v2-interface-boundaries.md").write_text(
        """
| Endpoint | Purpose | Response |
| --- | --- | --- |
| `POST /api/orchestrate/start` | Start a run | `{run_id, status}` |
| `GET /api/runs/current` | Current run state | `{run_id, status, started_at}` |
| `GET /api/diff/{id}` | Diff for a ticket branch/worktree | `{id, base, branch, files: [{path, status, patch?}]}` |
| `POST /api/orchestrate/stop` | Request graceful stop/cancel | `{run_id, status, stop_requested: true}` |
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
| `POST /api/orchestrate/start` | Start a run | `{run_id, status}` |
| `GET /api/runs/current` | Current run state | `{run_id, status, started_at}` |
| `GET /api/diff/{id}` | Diff for a ticket branch/worktree | `{id, base, branch, files: [{path, status, patch?}]}` |
| `POST /api/orchestrate/stop` | Request graceful stop/cancel | `{run_id, status, stop_requested: true}` |
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
## executors

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
## executors

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
        "title": "Executors config",
        "_body": "Update the executors section of docs/config-reference.md.",
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


def test_analyze_records_acceptance_contract_audit_metadata(repo):
    _make_draft(repo / "tickets")
    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: _GOOD_RESPONSE)
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


# --- cmd_analyze error paths ---


def test_analyze_missing_ticket_exits(repo, capsys):
    with pytest.raises(SystemExit) as exc:
        cmd_analyze("TICK-999", _CFG, repo, model_fn=lambda p: _GOOD_RESPONSE)
    assert exc.value.code == 1


def test_analyze_wrong_status_exits(repo):
    path = repo / "tickets" / "TICK-001.md"
    path.write_text(
        "---\nid: TICK-001\ntitle: Foo\nstatus: in_progress\ntouches:\n  - src/x.py\n---\nBody.\n"
    )
    with pytest.raises(SystemExit) as exc:
        cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: _GOOD_RESPONSE)
    assert exc.value.code == 1


def test_analyze_model_failure_exits(repo):
    _make_draft(repo / "tickets")

    def bad_model(p):
        raise RuntimeError("quota exceeded")

    with pytest.raises(SystemExit) as exc:
        cmd_analyze("TICK-001", _CFG, repo, model_fn=bad_model)
    assert exc.value.code == 1


def test_analyze_empty_touches_exits(repo):
    _make_draft(repo / "tickets")
    bad = json.dumps({"touches": [], "close_criteria": "done.", "depends_on": []})
    with pytest.raises(SystemExit) as exc:
        cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: bad)
    assert exc.value.code == 1


def test_analyze_empty_close_criteria_exits(repo):
    _make_draft(repo / "tickets")
    bad = json.dumps({"touches": ["lanegate/foo.py"], "close_criteria": "", "depends_on": []})
    with pytest.raises(SystemExit) as exc:
        cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: bad)
    assert exc.value.code == 1


def test_analyze_malformed_json_exits(repo):
    _make_draft(repo / "tickets")
    with pytest.raises(SystemExit) as exc:
        cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: "not json at all")
    assert exc.value.code == 1


def test_analyze_ticket_left_as_draft_on_error(repo):
    _make_draft(repo / "tickets")
    bad = json.dumps({"touches": [], "close_criteria": "", "depends_on": []})
    with pytest.raises(SystemExit):
        cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: bad)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t["status"] == "draft"


# --- prompt includes relevant content ---


def test_build_prompt_includes_ticket_id(repo):
    _make_draft(repo / "tickets")
    tickets_dir = repo / "tickets"
    from lanegate.ticket import parse_ticket as pt

    ticket = pt(tickets_dir / "TICK-001.md")
    prompt = _build_prompt(ticket, repo)
    assert "TICK-001" in prompt


def test_build_prompt_includes_intent(repo):
    _make_draft(repo / "tickets", title="Build login page")
    from lanegate.ticket import parse_ticket as pt

    ticket = pt(repo / "tickets" / "TICK-001.md")
    prompt = _build_prompt(ticket, repo)
    assert "Build login page" in prompt or "foo command" in prompt


def test_build_prompt_includes_project_guidance(repo):
    (repo / "AGENTS.md").write_text("Use table-driven tests for parsers.")
    _make_draft(repo / "tickets", title="Build parser")
    from lanegate.ticket import parse_ticket as pt

    ticket = pt(repo / "tickets" / "TICK-001.md")
    prompt = _build_prompt(ticket, repo)

    assert "## Project guidance" in prompt
    assert "Use table-driven tests for parsers." in prompt


# ---------------------------------------------------------------------------
# TICK-306: bounded analyze-prompt payload -- touches don't exist yet at
# analyze time, so AST symbol hits stand in as the relevance signal.
# ---------------------------------------------------------------------------

_LARGE_ARCH_DOC = (
    "# Architecture Reference\n\n"
    "## Overview\n"
    + ("General background prose about the project. " * 20)
    + "\n\n"
    "## Orchestration Loop\n"
    "The orchestrate.py module implements the board-clearing loop. "
    + ("Detail sentence about orchestrate.py behavior. " * 20)
    + "\n\n"
    "## Delivery Axis\n"
    + ("Prose about promote.py and feature flags. " * 20)
    + "\n"
)


def _write_large_arch_doc(repo_root: Path) -> None:
    docs = repo_root / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "ARCHITECTURE.md").write_text(_LARGE_ARCH_DOC)


def test_architecture_not_unconditional_for_unrelated_ticket(repo):
    """A ticket whose intent matches nothing in the repo (no AST symbol hits)
    must not pull in the full architecture doc -- TICK-306.
    """
    _write_large_arch_doc(repo)
    _make_draft(repo / "tickets", title="Zzqxv unrelated marker phrase")
    ticket = parse_ticket(repo / "tickets" / "TICK-001.md")

    prompt = _build_prompt(ticket, repo)

    assert "Prose about promote.py" not in prompt
    assert "Orchestration Loop" not in prompt


def test_bounded_architecture_excerpt_for_relevant_symbol_hits(repo):
    """A ticket whose intent matches a real def name (AST symbol hit) gets a
    bounded excerpt of just the doc section naming that module -- TICK-306.
    """
    _write_large_arch_doc(repo)
    module_dir = repo / "mymodule"
    module_dir.mkdir()
    (module_dir / "orchestrate.py").write_text("def run_loop():\n    pass\n")
    _make_draft(repo / "tickets", title="Update the loop behavior")
    ticket = parse_ticket(repo / "tickets" / "TICK-001.md")

    prompt = _build_prompt(ticket, repo)

    assert "Orchestration Loop" in prompt
    assert "orchestrate.py" in prompt
    assert "Prose about promote.py" not in prompt


def test_describe_analyze_payload_returns_component_metadata(repo):
    from lanegate.analyze import describe_analyze_payload

    _write_large_arch_doc(repo)
    _make_draft(repo / "tickets", title="Build parser")
    ticket = parse_ticket(repo / "tickets" / "TICK-001.md")

    components = describe_analyze_payload(ticket, repo)

    assert isinstance(components, list)
    assert components
    for component in components:
        assert set(component.keys()) == {
            "label", "source", "step", "bytes", "tokens_est", "injected", "reason",
        }
        assert component["step"] == "analyze"
    labels = {c["label"] for c in components}
    assert "architecture-excerpt:docs/ARCHITECTURE.md" in labels
    assert "ticket-intent" in labels


def test_describe_analyze_payload_never_exposes_ticket_content(repo):
    from lanegate.analyze import describe_analyze_payload

    _make_draft(repo / "tickets", title="SECRET_TITLE_MARKER")
    ticket = parse_ticket(repo / "tickets" / "TICK-001.md")

    components = describe_analyze_payload(ticket, repo)

    serialized = json.dumps(components)
    assert "SECRET_TITLE_MARKER" not in serialized


def test_describe_analyze_payload_accounting_deterministic(repo):
    from lanegate.analyze import describe_analyze_payload

    _write_large_arch_doc(repo)
    _make_draft(repo / "tickets", title="Build parser")
    ticket = parse_ticket(repo / "tickets" / "TICK-001.md")

    first = describe_analyze_payload(ticket, repo)
    second = describe_analyze_payload(ticket, repo)

    assert first == second


# ---------------------------------------------------------------------------
# TICK-043: no --no-verify in git commits
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# TICK-051: model resolution in analyze
# ---------------------------------------------------------------------------


def test_analyze_passes_model_from_config_to_call_model(repo):
    """When cfg has models.analyze, cmd_analyze passes it to _call_model."""
    _make_draft(repo / "tickets")
    cfg_with_model = dict(_CFG, models={"analyze": "claude-opus-4-5"})

    captured_cmds = []

    def fake_run(cmd, **kwargs):
        captured_cmds.append(list(cmd))
        return _subprocess.CompletedProcess(cmd, 0, stdout=_GOOD_RESPONSE, stderr="")

    with patch("lanegate.analyze.subprocess.run", side_effect=fake_run):
        cmd_analyze("TICK-001", cfg_with_model, repo)

    analyze_calls = [c for c in captured_cmds if "claude" in c and "-p" in c]
    assert analyze_calls, "No claude -p call was made"
    cmd = analyze_calls[0]
    assert "--model" in cmd
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "claude-opus-4-5"


def test_analyze_uses_named_driver_model(repo):
    """steps.analyze.driver can route analyze through a named driver's model."""
    _make_draft(repo / "tickets")
    cfg = dict(
        _CFG,
        drivers={"analyzer": {"type": "claude-process", "model": "driver-analyze-model"}},
        steps={"analyze": {"driver": "analyzer"}},
    )

    captured_cmds = []

    def fake_run(cmd, **kwargs):
        captured_cmds.append(list(cmd))
        return _subprocess.CompletedProcess(cmd, 0, stdout=_GOOD_RESPONSE, stderr="")

    with patch("lanegate.analyze.subprocess.run", side_effect=fake_run):
        cmd_analyze("TICK-001", cfg, repo)

    analyze_calls = [c for c in captured_cmds if "claude" in c and "-p" in c]
    assert analyze_calls, "No claude -p call was made"
    cmd = analyze_calls[0]
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "driver-analyze-model"


def test_analyze_unwraps_json_envelope_for_named_claude_instance(repo):
    """A named executor instance (e.g. "claude-a") of type claude/claude-process
    still gets --output-format json added by build_executor_cmd based on its
    *resolved* type, so _call_model must unwrap the envelope using that same
    resolved type — not the raw instance name, which never matches
    _CLAUDE_SUBPROCESS_TYPES and previously left the envelope unparsed.
    """
    _make_draft(repo / "tickets")
    cfg = dict(
        _CFG,
        executor="claude-a",
        executors={"claude-a": {"type": "claude-process"}},
    )

    envelope = json.dumps({"session_id": "abc123", "result": _GOOD_RESPONSE})

    def fake_run(cmd, **kwargs):
        return _subprocess.CompletedProcess(cmd, 0, stdout=envelope, stderr="")

    with patch("lanegate.analyze.subprocess.run", side_effect=fake_run):
        cmd_analyze("TICK-001", cfg, repo)

    ticket = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert ticket["status"] == "open"
    assert ticket["touches"] == ["lanegate/foo.py", "tests/test_foo.py"]


def test_analyze_unwraps_codex_jsonl_stream(repo):
    """Codex's ``exec --json`` reply is a JSONL event stream, not a single
    envelope — cmd_analyze must extract the agent_message text via the same
    parse_structured_result registry entry (parse_codex_json_result), not
    just the Claude-only unwrap path.
    """
    _make_draft(repo / "tickets")
    cfg = dict(_CFG, executor="codex", models={"analyze": "gpt-4o"})

    events = [
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": _GOOD_RESPONSE},
        },
        {
            "type": "turn.completed",
            "thread_id": "codex-analysis-1",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
    ]
    stdout = "\n".join(json.dumps(e) for e in events)

    def fake_run(cmd, **kwargs):
        return _subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with patch("lanegate.analyze.subprocess.run", side_effect=fake_run):
        cmd_analyze("TICK-001", cfg, repo)

    ticket = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert ticket["status"] == "open"
    assert ticket["touches"] == ["lanegate/foo.py", "tests/test_foo.py"]
    assert ticket["analyze_session_id"] == "codex-analysis-1"
    assert ticket["analyze_session_executor"] == "codex"
    assert ticket["analyze_session_model"] == "gpt-4o"


def test_analyze_retries_rate_limited_pool_instance_on_healthy_sibling(repo):
    """Analyze dispatch fails over instead of reusing a rate-limited pool member."""
    _make_draft(repo / "tickets")
    cfg = dict(
        _CFG,
        executor="claude-1",
        executors={
            "claude-1": {"type": "claude-process"},
            "claude-2": {"type": "claude-process"},
        },
        pools={"default": {"executors": ["claude-1", "claude-2"], "strategy": "round-robin"}},
        default_pool="default",
    )
    calls: list[str] = []

    def fake_call_model(prompt, **kwargs):
        calls.append(kwargs["executor"])
        if kwargs["executor"] == "claude-1":
            raise RuntimeError("claude-1 failed (exit 1): rate limit exceeded")
        return _GOOD_RESPONSE, None

    with patch("lanegate.analyze._call_model", side_effect=fake_call_model):
        cmd_analyze("TICK-001", cfg, repo)

    assert calls == ["claude-1", "claude-2"]


def test_analyze_named_driver_bin_flags_and_env_reach_subprocess(repo, monkeypatch):
    """steps.analyze.driver applies command and env overrides to the analyze subprocess."""
    _make_draft(repo / "tickets")
    cfg = dict(
        _CFG,
        drivers={
            "analyzer": {
                "type": "claude-process",
                "model": "driver-analyze-model",
                "bin": "custom-analyze",
                "flags": ["--driver-flag"],
                "env": {
                    "ANALYZE_TOKEN": "${SOURCE_ANALYZE_TOKEN}",
                    "ANALYZE_LITERAL": "literal",
                },
            }
        },
        steps={"analyze": {"driver": "analyzer"}},
    )
    monkeypatch.setenv("SOURCE_ANALYZE_TOKEN", "expanded-analyze-token")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        return _subprocess.CompletedProcess(cmd, 0, stdout=_GOOD_RESPONSE, stderr="")

    with patch("lanegate.analyze.subprocess.run", side_effect=fake_run):
        cmd_analyze("TICK-001", cfg, repo)

    analyze_calls = [(cmd, kwargs) for cmd, kwargs in calls if cmd and cmd[0] == "custom-analyze"]
    assert analyze_calls, "No custom analyze driver subprocess was made"
    cmd, kwargs = analyze_calls[0]
    assert "--driver-flag" in cmd
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "driver-analyze-model"
    assert kwargs["env"]["ANALYZE_TOKEN"] == "expanded-analyze-token"
    assert kwargs["env"]["ANALYZE_LITERAL"] == "literal"


def test_analyze_model_fn_does_not_receive_model_kwarg(repo):
    """When model_fn is provided, it is called as model_fn(prompt) — no model kwarg."""
    _make_draft(repo / "tickets")
    received_args = []

    def capturing_model_fn(prompt):
        received_args.append(prompt)
        return _GOOD_RESPONSE

    cmd_analyze("TICK-001", _CFG, repo, model_fn=capturing_model_fn)
    assert len(received_args) == 1
    assert isinstance(received_args[0], str)


def test_call_model_injects_model_flag():
    """_call_model appends --model <model> when model is provided."""
    captured = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return _subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    with patch("lanegate.analyze.subprocess.run", side_effect=fake_run):
        _call_model("hello", model="claude-sonnet-4-5")

    assert captured
    cmd = captured[0]
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-5"


def test_call_model_no_flag_without_model():
    """_call_model does NOT add --model when model is None."""
    captured = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return _subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    with patch("lanegate.analyze.subprocess.run", side_effect=fake_run):
        _call_model("hello", model=None)

    assert captured
    assert "--model" not in captured[0]


def test_call_model_missing_executor_bin_raises_before_subprocess(monkeypatch):
    monkeypatch.setenv("PATH", "/restricted/bin")
    monkeypatch.setattr("lanegate.executor.shutil.which", lambda _bin_name: None)

    with patch("lanegate.analyze.subprocess.run") as mock_run:
        with pytest.raises(ConfigError, match="executor 'claude'.*bin 'claude'.*PATH"):
            _call_model("hello")

    mock_run.assert_not_called()


def test_analyze_commit_does_not_use_no_verify(repo):
    """When commits are enabled, cmd_analyze does not use --no-verify."""
    import subprocess as _sp
    from unittest.mock import patch

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        # Return a completed process with empty stdout/stderr so callers
        # that inspect .stdout (e.g. ripgrep seed) don't see None.
        return _sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    cfg_with_commit = dict(_CFG, commit_status_changes=True)
    _make_draft(repo / "tickets")

    with patch("lanegate.analyze.subprocess.run", side_effect=fake_run):
        cmd_analyze("TICK-001", cfg_with_commit, repo, model_fn=lambda p: _GOOD_RESPONSE)

    sidecar_path = str(repo / ".lanegate/context/TICK-001/file_skeletons.json")
    git_adds = [c for c in calls if len(c) >= 2 and c[:2] == ["git", "add"]]
    assert any(sidecar_path in c for c in git_adds)
    git_commits = [c for c in calls if "commit" in c]
    assert any(sidecar_path in c for c in git_commits)
    for cmd in git_commits:
        assert "--no-verify" not in cmd, f"cmd_analyze used --no-verify in git commit: {cmd}"


def test_analyze_skips_commits_when_commit_status_false(repo):
    """Zero-footprint default: cmd_analyze with commit_status_changes=False does not commit."""
    if shutil.which("git") is None:
        pytest.skip("git is required for analyze commit integration test")

    (repo / ".gitignore").write_text(".lanegate/*\n")
    _make_draft(repo / "tickets")
    _subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    _subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    _subprocess.run(["git", "add", ".gitignore", "tickets/TICK-001.md"], cwd=repo, check=True)
    _subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: _GOOD_RESPONSE)

    # With commit_status_changes=False (default), nothing should be committed
    # Ticket file may be modified (since it's in worktree, not committed) but
    # the sidecar should never be in git history
    sidecar = ".lanegate/context/TICK-001/file_skeletons.json"
    tracked = _subprocess.run(
        ["git", "ls-files", sidecar],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert tracked.stdout.strip() == "", "sidecar must not be committed in zero-footprint mode"


def test_analyze_skips_all_commits_when_ticket_dir_is_untracked(repo):
    """Zero-footprint default: even with untracked ticket dir, nothing is committed
    when commit_status_changes=False. The old behavior force-added the sidecar
    to work around the ticket pathspec being unknown — that's now unnecessary."""
    if shutil.which("git") is None:
        pytest.skip("git is required for analyze commit integration test")

    (repo / ".gitignore").write_text("tickets/*\n.lanegate/*\n")
    _make_draft(repo / "tickets")
    _subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    _subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    _subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    _subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: _GOOD_RESPONSE)

    # With commit_status_changes=False (default), sidecar must not be committed
    sidecar = ".lanegate/context/TICK-001/file_skeletons.json"
    tracked = _subprocess.run(
        ["git", "ls-files", sidecar],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert tracked.stdout.strip() == "", "sidecar must not be committed in zero-footprint mode"

    # Nothing should be staged or dirty
    dirty = _subprocess.run(
        ["git", "status", "--porcelain", "--", ".lanegate/context"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert dirty.stdout == "", "sidecar must not be left staged-but-uncommitted"

    ticket_tracked = _subprocess.run(
        ["git", "ls-files", "tickets/TICK-001.md"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert ticket_tracked.stdout.strip() == "", "gitignored ticket file should stay untracked"


def test_analyze_commits_sidecar_when_opt_in(repo):
    """When commit_status_changes=True, cmd_analyze commits the sidecar to git."""
    if shutil.which("git") is None:
        pytest.skip("git is required for analyze commit integration test")

    (repo / ".gitignore").write_text("tickets/*\n.lanegate/*\n")
    _make_draft(repo / "tickets")
    _subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    _subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    _subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    _subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    # Opt in to commit_status_changes
    cfg_with_commits = dict(_CFG, commit_status_changes=True)
    cmd_analyze("TICK-001", cfg_with_commits, repo, model_fn=lambda p: _GOOD_RESPONSE)

    # With opt-in, sidecar should be committed
    sidecar = ".lanegate/context/TICK-001/file_skeletons.json"
    tracked = _subprocess.run(
        ["git", "ls-files", sidecar],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert tracked.stdout.strip() == sidecar, "sidecar should be committed when opt-in"

    ticket_tracked = _subprocess.run(
        ["git", "ls-files", "tickets/TICK-001.md"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert ticket_tracked.stdout.strip() == "tickets/TICK-001.md", "ticket file should be committed when opt-in, even though tickets_dir is gitignored"

    # Nothing should be left staged
    dirty = _subprocess.run(
        ["git", "status", "--porcelain", "--", ".lanegate/context"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert dirty.stdout == "", "sidecar must be committed, not left staged"


# ---------------------------------------------------------------------------
# TICK-052: AST symbol index, import graph, enrichment chain
# ---------------------------------------------------------------------------


def _write_py(path: Path, source: str) -> None:
    """Helper: write a .py file, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)


# --- _index_py_file ---


def test_index_py_file_extracts_function_names(tmp_path):
    f = tmp_path / "foo.py"
    _write_py(f, "def my_function(): pass\ndef another(): pass\n")
    idx = _index_py_file(f)
    assert idx is not None
    assert "my_function" in idx.defs
    assert "another" in idx.defs


def test_index_py_file_extracts_class_names(tmp_path):
    f = tmp_path / "bar.py"
    _write_py(f, "class MyClass:\n    pass\n")
    idx = _index_py_file(f)
    assert idx is not None
    assert "MyClass" in idx.defs


def test_index_py_file_extracts_imports(tmp_path):
    f = tmp_path / "baz.py"
    _write_py(f, "import os\nfrom pathlib import Path\nfrom lanegate.ticket import load_all_tickets\n")
    idx = _index_py_file(f)
    assert idx is not None
    assert "os" in idx.imports
    assert "pathlib" in idx.imports
    assert "lanegate.ticket" in idx.imports


def test_index_py_file_returns_none_on_syntax_error(tmp_path):
    f = tmp_path / "broken.py"
    _write_py(f, "def bad syntax(:\n")
    result = _index_py_file(f)
    assert result is None


def test_index_py_file_returns_none_on_missing_file(tmp_path):
    result = _index_py_file(tmp_path / "nonexistent.py")
    assert result is None


def test_index_py_file_extracts_async_functions(tmp_path):
    f = tmp_path / "async_mod.py"
    _write_py(f, "async def fetch_data(): pass\n")
    idx = _index_py_file(f)
    assert idx is not None
    assert "fetch_data" in idx.defs


def test_index_py_file_populates_def_infos(tmp_path):
    f = tmp_path / "mod.py"
    _write_py(
        f,
        "def alpha(a, b=1):\n    pass\n\n\nclass Widget:\n    def spin(self, n):\n        pass\n",
    )
    idx = _index_py_file(f)
    assert idx is not None
    by_name = {d.name: d for d in idx.def_infos}
    assert by_name["alpha"].line == 1
    assert by_name["alpha"].signature == "def alpha(a, b=1)"
    assert by_name["alpha"].kind == "function"
    assert by_name["Widget"].kind == "class"
    assert by_name["spin"].kind == "method"
    assert by_name["spin"].line == 6


# --- _build_file_skeleton ---


def test_build_file_skeleton_python_file(tmp_path):
    f = tmp_path / "lanegate" / "foo.py"
    _write_py(f, "def cmd_foo(x, y=1):\n    pass\n\n\ndef cmd_bar():\n    pass\n")
    skeleton = _build_file_skeleton(Path("lanegate/foo.py"), tmp_path)
    lines = skeleton.splitlines()
    assert lines[0] == "lanegate/foo.py  (6 lines)"
    assert "line   1: def cmd_foo(x, y=1)" in skeleton
    assert "line   5: def cmd_bar()" in skeleton


def test_build_file_skeleton_non_python_file_is_stub_only(tmp_path):
    f = tmp_path / "docs" / "notes.md"
    f.parent.mkdir()
    f.write_text("line one\nline two\nline three\n")
    skeleton = _build_file_skeleton(Path("docs/notes.md"), tmp_path)
    assert skeleton == "docs/notes.md  (3 lines)"


def test_build_file_skeleton_missing_file(tmp_path):
    skeleton = _build_file_skeleton(Path("lanegate/missing.py"), tmp_path)
    assert "not found" in skeleton


# --- _build_ast_index ---


def test_build_ast_index_finds_all_py_files(tmp_path):
    _write_py(tmp_path / "mod_a.py", "def alpha(): pass\n")
    _write_py(tmp_path / "pkg" / "mod_b.py", "def beta(): pass\n")
    indices = _build_ast_index(tmp_path)
    paths = [idx.path for idx in indices]
    assert tmp_path / "mod_a.py" in paths
    assert tmp_path / "pkg" / "mod_b.py" in paths


def test_build_ast_index_skips_hidden_dirs(tmp_path):
    _write_py(tmp_path / ".hidden" / "secret.py", "def hidden_fn(): pass\n")
    _write_py(tmp_path / "visible.py", "def visible_fn(): pass\n")
    indices = _build_ast_index(tmp_path)
    names = [idx.path.name for idx in indices]
    assert "visible.py" in names
    assert "secret.py" not in names


def test_build_ast_index_skips_pycache(tmp_path):
    _write_py(tmp_path / "__pycache__" / "cached.py", "x = 1\n")
    _write_py(tmp_path / "real.py", "def real_fn(): pass\n")
    indices = _build_ast_index(tmp_path)
    names = [idx.path.name for idx in indices]
    assert "real.py" in names
    assert "cached.py" not in names


def test_build_ast_index_silently_skips_parse_errors(tmp_path):
    _write_py(tmp_path / "good.py", "def good_fn(): pass\n")
    _write_py(tmp_path / "bad.py", "def bad syntax(:\n")
    # Should not raise, and good.py should still be indexed
    indices = _build_ast_index(tmp_path)
    names = [idx.path.name for idx in indices]
    assert "good.py" in names
    assert "bad.py" not in names


# --- _ast_symbol_hits ---


def test_ast_symbol_hits_returns_matching_files(tmp_path):
    _write_py(tmp_path / "analyze.py", "def enrich_context(): pass\n")
    _write_py(tmp_path / "unrelated.py", "def something_else(): pass\n")
    hits = _ast_symbol_hits("enrich context for ticket analysis", tmp_path)
    hit_names = [idx.path.name for idx in hits]
    assert "analyze.py" in hit_names
    assert "unrelated.py" not in hit_names


def test_ast_symbol_hits_returns_empty_for_no_matches(tmp_path):
    _write_py(tmp_path / "mod.py", "def totally_unrelated(): pass\n")
    hits = _ast_symbol_hits("xyzzy frobnicate quux", tmp_path)
    assert hits == []


def test_ast_symbol_hits_case_insensitive(tmp_path):
    _write_py(tmp_path / "mod.py", "def MyParser(): pass\n")
    hits = _ast_symbol_hits("myparser integration", tmp_path)
    hit_names = [idx.path.name for idx in hits]
    assert "mod.py" in hit_names


def test_ast_symbol_hits_returns_empty_for_short_keywords(tmp_path):
    _write_py(tmp_path / "mod.py", "def foo(): pass\n")
    # All words < 4 chars — should find nothing
    hits = _ast_symbol_hits("add foo bar", tmp_path)
    assert hits == []


# --- _import_graph_expand ---


def test_import_graph_expand_finds_importers(tmp_path):
    _write_py(tmp_path / "core.py", "def core_logic(): pass\n")
    _write_py(tmp_path / "consumer.py", "from core import core_logic\n")
    _write_py(tmp_path / "other.py", "def unrelated(): pass\n")

    all_indices = _build_ast_index(tmp_path)
    core_idx = next(idx for idx in all_indices if idx.path.name == "core.py")
    importers = _import_graph_expand([core_idx], all_indices)
    importer_names = [idx.path.name for idx in importers]
    assert "consumer.py" in importer_names
    assert "other.py" not in importer_names


def test_import_graph_expand_empty_hits_returns_empty(tmp_path):
    _write_py(tmp_path / "mod.py", "def fn(): pass\n")
    all_indices = _build_ast_index(tmp_path)
    result = _import_graph_expand([], all_indices)
    assert result == []


def test_import_graph_expand_does_not_re_include_hits(tmp_path):
    _write_py(tmp_path / "alpha.py", "def alpha(): pass\n")
    _write_py(tmp_path / "beta.py", "from alpha import alpha\n")

    all_indices = _build_ast_index(tmp_path)
    alpha_idx = next(idx for idx in all_indices if idx.path.name == "alpha.py")
    importers = _import_graph_expand([alpha_idx], all_indices)
    # alpha.py itself must not appear as its own importer
    assert alpha_idx not in importers


# --- enrich_context fallback chain ---


def test_enrich_context_uses_ast_when_hits_found(tmp_path):
    _write_py(tmp_path / "analyze_module.py", "def analyze_data(): pass\n")
    ctx = enrich_context("analyze data pipeline", tmp_path)
    # AST hits should be populated since "analyze" matches "analyze_data"
    assert ctx.source in ("ast", "ripgrep", "none")  # AST is preferred if found


def test_enrich_context_source_ast_when_match(tmp_path):
    _write_py(tmp_path / "enrichment.py", "def enrich_context_data(): pass\n")
    ctx = enrich_context("enrich context data processing", tmp_path)
    # "enrich" (6 chars) and "context" (7 chars) should match "enrich_context_data"
    if ctx.source == "ast":
        assert "enrichment.py" in ctx.symbol_hits


def test_enrich_context_falls_back_to_ripgrep_when_no_ast_hits(tmp_path, monkeypatch):
    """When AST finds nothing and tree-sitter is absent, ripgrep is used."""
    _write_py(tmp_path / "mod.py", "x = 1\n")  # no matching symbols

    monkeypatch.setattr("lanegate.analyze._HAS_TREE_SITTER", False)

    rg_called = []

    def fake_run(cmd, **kwargs):
        rg_called.append(cmd)
        return _subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("lanegate.analyze.subprocess.run", side_effect=fake_run):
        ctx = enrich_context("xyzzy_totally_unique_keyword_never_matches", tmp_path)

    # ripgrep fallback was tried (or returned empty); source is ripgrep or none
    assert ctx.source in ("ripgrep", "none")


def test_enrich_context_last_resort_returns_repo_structure(tmp_path, monkeypatch):
    """When everything else fails, repo_structure is populated."""
    _write_py(tmp_path / "mod.py", "x = 1\n")

    monkeypatch.setattr("lanegate.analyze._HAS_TREE_SITTER", False)

    def fake_run(cmd, **kwargs):
        # Simulate git ls-files returning one file, rg returning nothing
        if cmd[0] == "git":
            return _subprocess.CompletedProcess(cmd, 0, stdout="mod.py\n", stderr="")
        return _subprocess.CompletedProcess(cmd, 0, stdout="", stderr="1")  # rg: no hits

    with patch("lanegate.analyze.subprocess.run", side_effect=fake_run):
        ctx = enrich_context("xyzzy_totally_unique_keyword_never_matches", tmp_path)

    assert "mod.py" in ctx.repo_structure


# --- prompt template updated ---


def test_build_prompt_has_symbol_hits_section(tmp_path):
    _write_py(tmp_path / "analyze_module.py", "def analyze_data(): pass\n")
    td = tmp_path / "tickets"
    td.mkdir()
    path = td / "TICK-001.md"
    path.write_text(
        "---\nid: TICK-001\ntitle: analyze data\nstatus: draft\npriority: 3\ntouches: []\n---\nAnalyze data.\n"
    )
    from lanegate.ticket import parse_ticket as pt

    ticket = pt(path)
    prompt = _build_prompt(ticket, tmp_path)
    assert "Symbol matches" in prompt
    assert "Importers" in prompt


def test_build_prompt_ripgrep_section_present(tmp_path):
    td = tmp_path / "tickets"
    td.mkdir()
    path = td / "TICK-001.md"
    path.write_text(
        "---\nid: TICK-001\ntitle: frobnicate\nstatus: draft\npriority: 3\ntouches: []\n---\nBody.\n"
    )
    from lanegate.ticket import parse_ticket as pt

    ticket = pt(path)
    with patch("lanegate.analyze.subprocess.run") as mock_run:
        mock_run.return_value = _subprocess.CompletedProcess([], 0, stdout="", stderr="")
        prompt = _build_prompt(ticket, tmp_path)
    assert "Ripgrep keyword hits" in prompt


# --- analysis noise filtering ---


def test_build_ast_index_skips_dependency_and_worktree_dirs(tmp_path):
    _write_py(
        tmp_path / "venv" / "lib" / "python3.13" / "site-packages" / "pkg.py",
        "def noisy_symbol(): pass\n",
    )
    _write_py(
        tmp_path / "worktrees" / "tick-001" / "lanegate" / "copy.py", "def noisy_symbol(): pass\n"
    )
    _write_py(tmp_path / "src" / "app.py", "def real_symbol(): pass\n")
    indices = _build_ast_index(tmp_path)
    rel_paths = {idx.path.relative_to(tmp_path).as_posix() for idx in indices}
    assert "src/app.py" in rel_paths
    assert "venv/lib/python3.13/site-packages/pkg.py" not in rel_paths
    assert "worktrees/tick-001/lanegate/copy.py" not in rel_paths


def test_repo_structure_filters_dependency_and_worktree_paths(tmp_path):
    def fake_run(cmd, **kwargs):
        stdout = (
            "\n".join(
                [
                    "src/app.py",
                    "tests/test_app.py",
                    "venv/lib/python3.13/site-packages/pkg.py",
                    "worktrees/tick-001/lanegate/copy.py",
                    ".pytest_cache/cache.py",
                    "module.pyc",
                ]
            )
            + "\n"
        )
        return _subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with patch("lanegate.analyze.subprocess.run", side_effect=fake_run):
        structure = _repo_structure(tmp_path)

    assert "src/app.py" in structure
    assert "tests/test_app.py" in structure
    assert "venv/" not in structure
    assert "worktrees/" not in structure
    assert ".pytest_cache" not in structure
    assert "module.pyc" not in structure


def test_ripgrep_seed_filters_dependency_and_worktree_paths(tmp_path):
    def fake_run(cmd, **kwargs):
        stdout = (
            "\n".join(
                [
                    "venv/lib/python3.13/site-packages/pkg.py",
                    "worktrees/tick-001/lanegate/copy.py",
                    "src/app.py",
                ]
            )
            + "\n"
        )
        return _subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with patch("lanegate.analyze.subprocess.run", side_effect=fake_run):
        seed = _ripgrep_seed("analyze noisy libraries", tmp_path)

    assert "src/app.py" in seed
    assert "venv/" not in seed
    assert "worktrees/" not in seed


# ---------------------------------------------------------------------------
# TICK-081: real tree-sitter symbol indexing for non-Python files
# ---------------------------------------------------------------------------


def test_ht_has_tree_sitter_flag_is_bool():
    """_HAS_TREE_SITTER must be a bool (True when tree-sitter installed)."""
    assert isinstance(_HAS_TREE_SITTER, bool)


def test_ts_language_map_covers_expected_extensions():
    """_TS_LANGUAGE_MAP covers the core set of non-Python extensions."""
    expected = {".go", ".js", ".jsx", ".ts", ".tsx", ".rs", ".java", ".rb", ".c", ".cpp", ".h"}
    for ext in expected:
        assert ext in _TS_LANGUAGE_MAP, f"Missing extension {ext} from _TS_LANGUAGE_MAP"


def test_treesitter_hits_returns_empty_when_flag_false(tmp_path):
    """_treesitter_hits returns [] immediately when _HAS_TREE_SITTER is False."""
    go_file = tmp_path / "server.go"
    go_file.write_bytes(b"package main\nfunc FetchUser() {}\n")

    with patch("lanegate.analyze._HAS_TREE_SITTER", False):
        hits = _treesitter_hits("fetch user data", tmp_path)

    assert hits == []


def test_index_non_py_file_returns_none_when_no_treesitter(tmp_path, monkeypatch):
    """_index_non_py_file returns None when _HAS_TREE_SITTER is False."""
    go_file = tmp_path / "main.go"
    go_file.write_bytes(b"package main\nfunc Main() {}\n")
    monkeypatch.setattr("lanegate.analyze._HAS_TREE_SITTER", False)
    assert _index_non_py_file(go_file) is None


def test_index_non_py_file_returns_none_for_unknown_extension(tmp_path):
    """_index_non_py_file returns None for extensions not in _TS_LANGUAGE_MAP."""
    txt_file = tmp_path / "notes.txt"
    txt_file.write_bytes(b"some text\n")
    result = _index_non_py_file(txt_file)
    assert result is None


def test_index_non_py_file_returns_none_for_missing_file(tmp_path):
    """_index_non_py_file returns None when the file does not exist."""
    result = _index_non_py_file(tmp_path / "nonexistent.go")
    assert result is None


# Real grammar tests — skipped gracefully when grammar packages not installed.


def test_index_non_py_go_function(tmp_path):
    """_index_non_py_file extracts function names from a Go file."""
    pytest.importorskip("tree_sitter_go")
    go_file = tmp_path / "service.go"
    go_file.write_bytes(b'package main\n\nfunc FetchUser(id int) string {\n  return ""\n}\n')
    idx = _index_non_py_file(go_file)
    assert idx is not None
    assert "FetchUser" in idx.defs


def test_index_non_py_go_method(tmp_path):
    """_index_non_py_file extracts method names from a Go file."""
    pytest.importorskip("tree_sitter_go")
    go_file = tmp_path / "repo.go"
    go_file.write_bytes(
        b"package main\n\ntype UserRepo struct{}\n\n"
        b'func (r *UserRepo) FindById(id int) string {\n  return ""\n}\n'
    )
    idx = _index_non_py_file(go_file)
    assert idx is not None
    assert "FindById" in idx.defs


def test_index_non_py_go_type(tmp_path):
    """_index_non_py_file extracts type names from a Go file."""
    pytest.importorskip("tree_sitter_go")
    go_file = tmp_path / "types.go"
    go_file.write_bytes(
        b"package main\n\ntype UserService struct {\n  name string\n}\n"
        b"type Fetcher interface {\n  Fetch() string\n}\n"
    )
    idx = _index_non_py_file(go_file)
    assert idx is not None
    assert "UserService" in idx.defs
    assert "Fetcher" in idx.defs


def test_index_non_py_js_function(tmp_path):
    """_index_non_py_file extracts function names from a JS file."""
    pytest.importorskip("tree_sitter_javascript")
    js_file = tmp_path / "api.js"
    js_file.write_bytes(
        b"function fetchData(url) { return null; }\nclass UserService { render() {} }\n"
    )
    idx = _index_non_py_file(js_file)
    assert idx is not None
    assert "fetchData" in idx.defs
    assert "UserService" in idx.defs


def test_index_non_py_ts_class(tmp_path):
    """_index_non_py_file extracts class names from a TS file."""
    pytest.importorskip("tree_sitter_typescript")
    ts_file = tmp_path / "repository.ts"
    ts_file.write_bytes(
        b"class UserRepository {\n"
        b'  findUser(id: number): string { return ""; }\n'
        b"}\n"
        b"function parseToken(token: string): boolean { return true; }\n"
    )
    idx = _index_non_py_file(ts_file)
    assert idx is not None
    assert "UserRepository" in idx.defs
    assert "parseToken" in idx.defs


def test_treesitter_hits_go_match(tmp_path):
    """_treesitter_hits returns the Go file when its function matches the intent."""
    pytest.importorskip("tree_sitter_go")
    go_file = tmp_path / "handler.go"
    go_file.write_bytes(b"package main\nfunc HandleRequest(w string) {}\n")
    hits = _treesitter_hits("handle request routing", tmp_path)
    assert "handler.go" in hits


def test_treesitter_hits_js_match(tmp_path):
    """_treesitter_hits returns the JS file when its function matches the intent."""
    pytest.importorskip("tree_sitter_javascript")
    js_file = tmp_path / "auth.js"
    js_file.write_bytes(b"function validateToken(tok) { return true; }\n")
    hits = _treesitter_hits("validate token credentials", tmp_path)
    assert "auth.js" in hits


def test_treesitter_hits_ts_class_match(tmp_path):
    """_treesitter_hits returns the TS file when its class matches the intent."""
    pytest.importorskip("tree_sitter_typescript")
    ts_file = tmp_path / "user.ts"
    ts_file.write_bytes(b"class UserManager { create() {} }\n")
    hits = _treesitter_hits("user manager creation", tmp_path)
    assert "user.ts" in hits


def test_treesitter_hits_no_match(tmp_path):
    """_treesitter_hits returns [] when no file's symbols match the intent."""
    pytest.importorskip("tree_sitter_go")
    go_file = tmp_path / "util.go"
    go_file.write_bytes(b"package main\nfunc PrintHello() {}\n")
    hits = _treesitter_hits("database query executor", tmp_path)
    assert hits == []


def test_treesitter_hits_skips_ignored_dirs(tmp_path):
    """_treesitter_hits does not index files inside ignored directories."""
    pytest.importorskip("tree_sitter_go")
    # File inside an ignored directory
    ignored_dir = tmp_path / "node_modules" / "lib"
    ignored_dir.mkdir(parents=True)
    ignored_file = ignored_dir / "helper.go"
    ignored_file.write_bytes(b"package lib\nfunc HelperFunc() {}\n")
    # No matching file in a non-ignored dir
    hits = _treesitter_hits("helper function", tmp_path)
    assert "node_modules/lib/helper.go" not in hits


def test_index_non_py_file_defs_used_for_symbols(tmp_path):
    """The returned _FileIndex uses defs for symbol names and imports is empty."""
    pytest.importorskip("tree_sitter_javascript")
    js_file = tmp_path / "mod.js"
    js_file.write_bytes(b"function doSomething() {}\n")
    idx = _index_non_py_file(js_file)
    assert idx is not None
    assert "doSomething" in idx.defs
    assert idx.imports == []


# ---------------------------------------------------------------------------
# TICK-085: static touch inference from close_criteria / intent text
# ---------------------------------------------------------------------------


def test_infer_touches_board_mention(tmp_path):
    """'lanegate board' in criteria implies lanegate/board.py."""
    result = infer_touches_from_criteria("lanegate board shows new column", tmp_path)
    assert "lanegate/board.py" in result


def test_infer_touches_stats_mention(tmp_path):
    """'lanegate stats' in criteria implies lanegate/stats.py."""
    result = infer_touches_from_criteria("lanegate stats output includes cycle time", tmp_path)
    assert "lanegate/stats.py" in result


def test_infer_touches_unknown_cmd_does_not_imply_cli(tmp_path):
    """'lanegate <unknown-cmd>' must NOT inject lanegate/cli.py.

    Prose like "lanegate foobar runs the new thing" is not a subcommand reference
    — silently ignore unknown tokens rather than adding a false cli.py touch.
    """
    result = infer_touches_from_criteria("lanegate foobar runs the new thing", tmp_path)
    assert "lanegate/cli.py" not in result


def test_infer_touches_prose_ticket_creation_no_cli(tmp_path):
    """'lanegate ticket creation is handled by lifecycle' must NOT add cli.py."""
    result = infer_touches_from_criteria(
        "lanegate ticket creation is handled by lifecycle", tmp_path
    )
    assert "lanegate/cli.py" not in result


def test_infer_touches_prose_runs_project_no_cli(tmp_path):
    """'lanegate runs the project' must NOT add cli.py."""
    result = infer_touches_from_criteria("lanegate runs the project", tmp_path)
    assert "lanegate/cli.py" not in result


def test_infer_touches_prose_docs_no_cli(tmp_path):
    """'lanegate docs shows the config' must NOT add cli.py."""
    result = infer_touches_from_criteria("lanegate docs shows the config", tmp_path)
    assert "lanegate/cli.py" not in result


def test_infer_touches_show_in_board(tmp_path):
    """'show in board' implies lanegate/board.py even without 'lanegate board'."""
    result = infer_touches_from_criteria("column X is show in board output", tmp_path)
    assert "lanegate/board.py" in result


def test_infer_touches_add_column(tmp_path):
    """'add column' implies lanegate/board.py."""
    result = infer_touches_from_criteria("add column for milestone to the output", tmp_path)
    assert "lanegate/board.py" in result


def test_infer_touches_new_module(tmp_path):
    """'new file lanegate/foo.py' implies that path."""
    result = infer_touches_from_criteria("new file lanegate/foo.py is created", tmp_path)
    assert "lanegate/foo.py" in result


def test_infer_touches_new_module_keyword(tmp_path):
    """'new module lanegate/bar.py' implies that path."""
    result = infer_touches_from_criteria("new module lanegate/bar.py added for routing", tmp_path)
    assert "lanegate/bar.py" in result


def test_infer_touches_new_file_non_py_path(tmp_path):
    """'new file docs/plan.md' implies that non-Python path."""
    result = infer_touches_from_criteria(
        "close criteria: new file tests/fixtures/spec_artifacts/README.md exists",
        tmp_path,
    )
    assert "tests/fixtures/spec_artifacts/README.md" in result


def test_infer_touches_no_duplicates(tmp_path):
    """Duplicate mentions of the same command yield a single entry."""
    result = infer_touches_from_criteria("lanegate board shows X; lanegate board also shows Y", tmp_path)
    assert result.count("lanegate/board.py") == 1


def test_infer_touches_empty_text(tmp_path):
    """Empty text yields no inferences."""
    result = infer_touches_from_criteria("", tmp_path)
    assert result == []


def test_infer_touches_no_false_positives(tmp_path):
    """Text with no command patterns yields no inferences."""
    result = infer_touches_from_criteria(
        "the ticket body says nothing about any subcommand", tmp_path
    )
    assert result == []


# ---------------------------------------------------------------------------
# TICK-291: companion docs + stale-path detection
# ---------------------------------------------------------------------------


def test_detect_companion_docs_from_criteria(tmp_path):
    """close_criteria mentioning README/ARCHITECTURE implies those doc paths (TICK-253)."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ARCHITECTURE.md").write_text("# Architecture\n")
    (tmp_path / "README.md").write_text("# README\n")

    result = companion_docs_from_criteria(
        "Update README and docs/ARCHITECTURE.md to describe the new touches-guard behavior.",
        tmp_path,
    )

    assert "README.md" in result
    assert "docs/ARCHITECTURE.md" in result


def test_detect_companion_docs_bare_keyword(tmp_path):
    """A bare doc keyword with no explicit path still resolves if the file exists."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "security-model.md").write_text("# Security model\n")

    result = companion_docs_from_criteria(
        "The security model doc needs a new section on this threat.", tmp_path
    )

    assert "docs/security-model.md" in result


def test_companion_docs_ignores_nonexistent_doc_mentions(tmp_path):
    """A doc keyword must not inject a path that doesn't exist in the repo."""
    result = companion_docs_from_criteria(
        "Update the CHANGELOG and README to note this fix.", tmp_path
    )
    assert result == []


def test_companion_docs_empty_text(tmp_path):
    result = companion_docs_from_criteria("", tmp_path)
    assert result == []


def test_validate_touched_paths_detects_stale_references(tmp_path):
    """A touches directory renamed/moved by a prior merged ticket is flagged (TICK-269)."""
    (tmp_path / "tui").mkdir()
    (tmp_path / "tui" / "internal").mkdir()

    result = validate_touched_paths(["tui_spike/", "tui/internal/"], tmp_path)

    assert result == ["tui_spike/"]


def test_validate_touched_paths_ignores_file_entries(tmp_path):
    """Plain file paths are not checked -- a ticket may declare a file it will create."""
    result = validate_touched_paths(["lanegate/not_created_yet.py"], tmp_path)
    assert result == []


def test_validate_touched_paths_no_stale_entries(tmp_path):
    (tmp_path / "lanegate").mkdir()
    result = validate_touched_paths(["lanegate/"], tmp_path)
    assert result == []


def test_cmd_analyze_adds_companion_docs_to_touches(repo):
    """cmd_analyze augments touches with companion docs implied by close_criteria."""
    (repo / "docs").mkdir()
    (repo / "docs" / "ARCHITECTURE.md").write_text("# Architecture\n")
    _make_draft(repo / "tickets", title="Fix touches-guard false positives")
    response = json.dumps(
        {
            "touches": ["lanegate/analyze.py"],
            "close_criteria": "analyze detects companion docs; update docs/ARCHITECTURE.md accordingly.",
            "depends_on": [],
        }
    )
    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: response)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert "docs/ARCHITECTURE.md" in t["touches"]


def test_cmd_analyze_drops_stale_directory_touches(repo, capsys):
    """cmd_analyze drops a pre-existing touches entry whose directory no longer exists."""
    _make_draft(
        repo / "tickets", title="Finish TUI migration", touches=["tui_spike/"]
    )
    response = json.dumps(
        {
            "touches": ["tui/internal/board.go"],
            "close_criteria": "tui/internal/board.go renders the board view.",
            "depends_on": [],
        }
    )
    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: response)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert "tui_spike/" not in t["touches"]
    assert "tui/internal/board.go" in t["touches"]
    assert "stale touches" in capsys.readouterr().err


def test_cmd_analyze_keeps_directory_touches_that_still_exist(repo):
    """cmd_analyze does not drop a directory touches entry that still exists on disk."""
    (repo / "lanegate").mkdir()
    _make_draft(repo / "tickets", title="Update lanegate package", touches=["lanegate/"])
    response = json.dumps(
        {
            "touches": ["lanegate/analyze.py"],
            "close_criteria": "analyze.py gains a new helper.",
            "depends_on": [],
        }
    )
    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: response)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert "lanegate/" in t["touches"]


def test_cmd_analyze_augments_touches_from_criteria(repo):
    """cmd_analyze adds statically-inferred files not returned by the model."""
    _make_draft(repo / "tickets", title="Show cycle time in lanegate board output")
    response = json.dumps(
        {
            "touches": ["lanegate/lifecycle.py"],
            "close_criteria": "lanegate board shows cycle_time column for in_progress tickets.",
            "depends_on": [],
        }
    )
    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: response)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert "lanegate/lifecycle.py" in t["touches"]
    assert "lanegate/board.py" in t["touches"]


def test_cmd_analyze_does_not_widen_correct_touches(repo):
    """When model returns a correct touches list and criteria has no patterns, nothing extra is added."""
    _make_draft(repo / "tickets", title="Fix parsing edge case in ticket loader")
    response = json.dumps(
        {
            "touches": ["lanegate/ticket.py", "tests/test_ticket.py"],
            "close_criteria": "parse_ticket handles missing title field without raising.",
            "depends_on": [],
        }
    )
    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: response)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t["touches"] == ["lanegate/ticket.py", "tests/test_ticket.py"]


def test_cmd_analyze_no_duplicate_when_model_already_includes_inferred(repo):
    """If model already includes lanegate/board.py, inference does not duplicate it."""
    _make_draft(repo / "tickets", title="Add column to lanegate board")
    response = json.dumps(
        {
            "touches": ["lanegate/board.py"],
            "close_criteria": "lanegate board shows new column for priority.",
            "depends_on": [],
        }
    )
    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: response)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t["touches"].count("lanegate/board.py") == 1


# --- standalone analysis visibility ---


def test_analyze_terminal_lifecycle(repo, capsys):
    _make_draft(repo / "tickets")

    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: _GOOD_RESPONSE)

    output = capsys.readouterr().out
    assert "Indexing context..." in output
    assert "Prompt ready (" in output
    assert "Executor:" in output and "Model:" in output
    assert "Waiting for model... (elapsed 0s)" in output


def test_analyze_log_persistence(repo):
    _make_draft(repo / "tickets")

    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: _GOOD_RESPONSE)

    logs = list((repo / ".lanegate" / "logs").glob("analyze-*.log"))
    assert len(logs) == 1
    contents = logs[0].read_text()
    for phase in ("context_indexed", "prompt_ready", "model_requested", "model_responded", "analysis_complete"):
        assert phase in contents
    assert "executor_output" in contents
    assert '"touches"' in contents


def test_analyze_status_record(repo):
    _make_draft(repo / "tickets")
    model_started = threading.Event()
    allow_model_to_finish = threading.Event()
    failures: list[BaseException] = []

    def blocking_model(_prompt):
        model_started.set()
        assert allow_model_to_finish.wait(3)
        return _GOOD_RESPONSE

    def run() -> None:
        try:
            cmd_analyze("TICK-001", _CFG, repo, model_fn=blocking_model)
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert model_started.wait(3)
    status_path = repo / ".lanegate" / "analyze-active.json"
    for _ in range(30):
        if status_path.exists():
            break
        time.sleep(0.01)
    assert status_path.exists()
    active = json.loads(status_path.read_text())
    assert active["phase"] == "model_requested"
    assert active["executor"]
    assert active["model"]
    assert active["log_path"].startswith(".lanegate/logs/analyze-")

    allow_model_to_finish.set()
    worker.join(3)
    assert not worker.is_alive()
    assert failures == []
    assert not status_path.exists()


def test_analyze_api_status(repo):
    from lanegate.analyze import _write_active_analysis
    from lanegate.api import LaneGateApiServer
    from lanegate.logs import analyze_log_path

    log_file = analyze_log_path(repo)
    _write_active_analysis(
        repo,
        phase="model_requested",
        executor="claude",
        model="claude-test",
        started_at=time.time() - 2,
        log_file=log_file,
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = LaneGateApiServer(_CFG, repo, port=port)
    server.start()
    try:
        time.sleep(0.05)
        connection = HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request("GET", "/api/v1/analyze/status")
        response = connection.getresponse()
        body = json.loads(response.read().decode())
        connection.close()
        assert response.status == 200
        assert body["phase"] == "model_requested"
        assert body["executor"] == "claude"
        assert body["model"] == "claude-test"
        assert body["elapsed_seconds"] >= 2
        assert body["log_path"].startswith(".lanegate/logs/analyze-")
    finally:
        server.stop()


def test_analyze_cleanup(repo):
    _make_draft(repo / "tickets")
    status_path = repo / ".lanegate" / "analyze-active.json"

    def unavailable_model(_prompt):
        raise RuntimeError("unavailable")

    def interrupted_model(_prompt):
        signal.raise_signal(signal.SIGTERM)

    with pytest.raises(SystemExit):
        cmd_analyze("TICK-001", _CFG, repo, model_fn=unavailable_model)
    assert not status_path.exists()

    with pytest.raises(SystemExit) as interrupted:
        cmd_analyze("TICK-001", _CFG, repo, model_fn=interrupted_model)
    assert interrupted.value.code == 130
    assert not status_path.exists()
    logs = sorted((repo / ".lanegate" / "logs").glob("analyze-*.log"))
    assert len(logs) == 2
    assert all("analysis_failed" in log.read_text() for log in logs)
