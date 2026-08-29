"""Tests for analyze_prompt.py."""

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

from tests._helpers.analyze import _make_draft, _write_large_arch_doc, _write_py

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






def test_analyze_already_resolved_flags_needs_review(repo):
    _make_draft(repo / "tickets")
    response = json.dumps(
        {
            "already_resolved": True,
            "already_resolved_reason": "The feature is already present in the current implementation.",
        }
    )
    with pytest.raises(SystemExit) as exc:
        cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: response)
    assert exc.value.code == 0
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t["status"] == "needs_review"
    assert t["touches"] == []
    assert "## Needs Review Reason" in t["_body"]
    assert "already present" in t["_body"]


def test_already_resolved_hallucination_fallback(repo, capsys):
    """A false cited resolved claim is retried as ordinary analysis, not persisted."""
    _make_draft(repo / "tickets")
    source = repo / "lanegate"
    source.mkdir()
    (source / "foo.py").write_text("def actual_implementation():\n    return True\n")
    responses = iter(
        [
            json.dumps(
                {
                    "already_resolved": True,
                    "already_resolved_reason": (
                        "lanegate/foo.py:1 contains `def missing_implementation()`."
                    ),
                }
            ),
            _GOOD_RESPONSE,
        ]
    )

    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda _prompt: next(responses))

    ticket = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert ticket["status"] == "open"
    assert ticket["touches"] == ["lanegate/foo.py", "tests/test_foo.py"]
    assert "Needs Review Reason" not in ticket["_body"]
    assert "rejected already_resolved verdict" in capsys.readouterr().err


def test_already_resolved_fallback_rejects_second_hallucination(repo, capsys):
    """The fallback response cannot bypass worktree citation validation."""
    _make_draft(repo / "tickets")
    (repo / "Makefile").write_text("test:\n\tpytest -q\n")
    responses = iter(
        [
            json.dumps(
                {
                    "already_resolved": True,
                    "already_resolved_reason": "Makefile:1 contains `release: ship`.",
                }
            ),
            json.dumps(
                {
                    "already_resolved": True,
                    "already_resolved_reason": "Makefile:10 contains `deployment target`.",
                }
            ),
        ]
    )

    with pytest.raises(SystemExit) as exc:
        cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda _prompt: next(responses))

    assert exc.value.code == 1
    assert parse_ticket(repo / "tickets" / "TICK-001.md")["status"] == "draft"
    assert "fallback returned an invalid already_resolved verdict" in capsys.readouterr().err


def test_already_resolved_validation_ignores_backticked_labels(repo):
    """Backticked filenames and concepts are labels, not literal code claims."""
    source = repo / "lanegate"
    source.mkdir()
    (source / "foo.py").write_text("def implemented():\n    return True\n")

    assert _already_resolved_reason_matches_worktree(
        "`lanegate/foo.py` implements `ticket routing` at lanegate/foo.py:1-2.", repo
    ) == (True, None)


def test_already_resolved_validation_ignores_conversational_colon_numbers(repo):
    assert _already_resolved_reason_matches_worktree(
        "The implementation handles Error: 404 and retries Note: 10.", repo
    ) == (True, None)


def test_already_resolved_validation_ignores_backticked_file_citation_as_snippet(repo):
    source = repo / "lanegate"
    source.mkdir()
    (source / "foo.py").write_text("def implemented():\n    return True\n")

    assert _already_resolved_reason_matches_worktree(
        "The implementation is present at `lanegate/foo.py:1`.", repo
    ) == (True, None)


def test_already_resolved_validation_checks_root_file_citations(repo):
    (repo / "Makefile").write_text("test:\n\tpytest -q\n")

    verified, mismatch = _already_resolved_reason_matches_worktree(
        "Makefile:10 contains `release: ship`.", repo
    )

    assert not verified
    assert "is not in the worktree" in mismatch


def test_analyze_already_resolved_without_reason_errors(repo):
    _make_draft(repo / "tickets")
    response = json.dumps({"already_resolved": True})
    with pytest.raises(SystemExit) as exc:
        cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: response)
    assert exc.value.code == 1
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t["status"] == "draft"


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


def test_analyze_prompt_includes_global_and_file_notes(repo):
    (repo / "helper.py").write_text("def update_helper(): pass\n")
    _make_draft(repo / "tickets", title="Update helper")
    notes = repo / ".lanegate" / "notes"
    notes.mkdir(parents=True)
    (notes / "global.md").write_text("global analysis fact")
    (notes / "helper.py.md").write_text("helper-specific fact")

    ticket = parse_ticket(repo / "tickets" / "TICK-001.md")
    prompt = _build_prompt(ticket, repo)

    assert "global analysis fact" in prompt
    assert "helper-specific fact" in prompt


def test_build_prompt_includes_project_guidance(repo):
    (repo / "AGENTS.md").write_text("Use table-driven tests for parsers.")
    _make_draft(repo / "tickets", title="Build parser")
    from lanegate.ticket import parse_ticket as pt

    ticket = pt(repo / "tickets" / "TICK-001.md")
    prompt = _build_prompt(ticket, repo)

    assert "## Project guidance" in prompt
    assert "Use table-driven tests for parsers." in prompt


def test_describe_analyze_payload_returns_component_metadata(repo):
    from lanegate.analyze import describe_analyze_payload

    _write_large_arch_doc(repo)
    _make_draft(repo / "tickets", title="Build parser")
    ticket = parse_ticket(repo / "tickets" / "TICK-001.md")

    components = describe_analyze_payload(ticket, repo, cfg={"reference_docs": ["docs/ARCHITECTURE.md"]})

    assert isinstance(components, list)
    assert components
    for component in components:
        assert set(component.keys()) == {
            "label", "source", "step", "bytes", "tokens_est", "injected", "reason",
        }
        assert component["step"] == "analyze"
    labels = {c["label"] for c in components}
    assert "reference-excerpt:docs/ARCHITECTURE.md" in labels
    assert "ticket-intent" in labels


def test_describe_analyze_payload_never_exposes_ticket_content(repo):
    from lanegate.analyze import describe_analyze_payload

    _make_draft(repo / "tickets", title="SECRET_TITLE_MARKER")
    ticket = parse_ticket(repo / "tickets" / "TICK-001.md")

    components = describe_analyze_payload(ticket, repo, cfg={"reference_docs": ["docs/ARCHITECTURE.md"]})

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


def test_analyze_prompt_surfaces_overlapping_cross_ticket_change_notes(tmp_path):
    """A prior *merged* ticket's change_notes for a file that analyze's AST
    symbol-hit scan also flags as a candidate touch should be folded into the
    analyze prompt (TICK-481: git-tracked change_notes replaces the dead
    worktree-vs-repo_root per-file .lanegate/notes/ mechanism, and analyze
    time uses symbol hits as the touches-don't-exist-yet stand-in)."""
    _write_py(tmp_path / "analyze_module.py", "def analyze_data(): pass\n")
    td = tmp_path / "tickets"
    td.mkdir()

    prior_path = td / "TICK-100.md"
    prior_path.write_text(
        "---\nid: TICK-100\ntitle: Prior work\nstatus: merged\n"
        "touches:\n  - analyze_module.py\n---\n"
        "## Change Notes\n**analyze_module.py**: must not be called with unsanitized input\n"
    )

    new_path = td / "TICK-001.md"
    new_path.write_text(
        "---\nid: TICK-001\ntitle: analyze data\nstatus: draft\npriority: 3\ntouches: []\n---\nAnalyze data.\n"
    )
    from lanegate.ticket import parse_ticket as pt

    ticket = pt(new_path)
    prompt = _build_prompt(ticket, tmp_path, cfg={"ticket_prefix": "TICK", "tickets_dir": "tickets"})

    assert "Prior Change Notes" in prompt
    assert "TICK-100" in prompt
    assert "must not be called with unsanitized input" in prompt


def test_build_prompt_has_candidate_skeletons_section(tmp_path):
    _write_py(tmp_path / "analyze_module.py", "def analyze_data(x, y):\n    pass\n")
    td = tmp_path / "tickets"
    td.mkdir()
    path = td / "TICK-001.md"
    path.write_text(
        "---\nid: TICK-001\ntitle: analyze data\nstatus: draft\npriority: 3\ntouches: []\n---\nAnalyze data.\n"
    )
    from lanegate.ticket import parse_ticket as pt

    ticket = pt(path)
    prompt = _build_prompt(ticket, tmp_path)
    assert "## Candidate file skeletons" in prompt
    assert "def analyze_data(x, y)" in prompt


def test_build_prompt_omits_candidate_skeletons_section_when_no_matches(tmp_path):
    td = tmp_path / "tickets"
    td.mkdir()
    path = td / "TICK-001.md"
    path.write_text(
        "---\nid: TICK-001\ntitle: zzzznomatch\nstatus: draft\npriority: 3\ntouches: []\n---\nBody.\n"
    )
    from lanegate.ticket import parse_ticket as pt

    ticket = pt(path)
    with patch("lanegate.analyze.subprocess.run") as mock_run:
        mock_run.return_value = _subprocess.CompletedProcess([], 0, stdout="", stderr="")
        prompt = _build_prompt(ticket, tmp_path)
    assert "## Candidate file skeletons" not in prompt


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


def test_analyze_build_prompt_scopes_relevant_paths_to_symbol_hits(tmp_path):
    from unittest.mock import MagicMock

    ticket = {"id": "TICK-001", "title": "Test ticket", "_body": "Test body"}
    mock_ctx = MagicMock()
    mock_ctx.symbol_hits = ["lanegate/api.py"]
    mock_ctx.importers = ["lanegate/cli.py"]
    mock_ctx.ripgrep_hits = ""
    mock_ctx.repo_structure = ""

    with patch("lanegate.analyze_prompt.enrich_context", return_value=mock_ctx), \
         patch("lanegate.prompts.load_project_guidance") as mock_guidance, \
         patch("lanegate.prompts.get_bounded_reference_excerpts", return_value=("", [])) as mock_ref, \
         patch("lanegate.analyze_prompt._build_candidate_skeletons", return_value="") as mock_skel:
        mock_guidance.return_value = ""
        _build_prompt(ticket, tmp_path)

        mock_guidance.assert_called_once()
        _, kwargs = mock_guidance.call_args
        assert kwargs["relevant_paths"] == ["lanegate/api.py"]

        mock_ref.assert_called_once()
        args, kwargs = mock_ref.call_args
        assert args[1] == ["lanegate/api.py"]

        mock_skel.assert_called_once_with(["lanegate/api.py"], tmp_path)


def test_low_risk_ticket_prompt_omits_control_plane_overlap_section(repo):
    """[non-blocking] find_control_plane_touch_overlaps never fires for a
    non-high-reasoning ticket regardless of its touches, so showing this
    section to one is pure noise with nothing it could ever need to satisfy."""
    _make_draft(repo / "tickets", title="Fix README typo")
    other = _make_draft(repo / "tickets", ticket_id="TICK-002", title="Harden configuration routing", touches=["src/configuration.ext"])
    other.write_text(other.read_text().replace("status: draft", "status: open"))

    prompt = _build_prompt(parse_ticket(repo / "tickets" / "TICK-001.md"), repo, _CFG)

    assert "Active control-plane tickets" not in prompt
    assert "Required control-plane overlap response contract" not in prompt


def test_high_risk_prompt_override_receives_required_contract(repo):
    _make_draft(repo / "tickets", title="Fix security regression")
    prompt_dir = repo / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "analyze.md").write_text("custom analysis prompt\n")

    prompt = _build_prompt(parse_ticket(repo / "tickets" / "TICK-001.md"), repo, _CFG)

    assert "custom analysis prompt" in prompt
    assert "Required high-risk response contract" in prompt
    assert "adversarial_cases" in prompt


def test_control_plane_overlap_prompt_override_receives_required_contract(repo):
    """Same portability gap as the acceptance_matrix fallback, for the sibling
    overlap_review gate: a project analyze.md override predates/omits the
    contract's shape, but the overlap gate is unconditional whenever this
    ticket's touches turn out to overlap an active control-plane ticket.
    Without a fallback, the model never learns overlap_review's shape and
    every retry fails identically until the other ticket reaches a terminal
    status (see test_analyze_rejects_control_plane_overlap_without_plan)."""
    _make_draft(repo / "tickets", title="Harden lifecycle cleanup")
    other = _make_draft(repo / "tickets", ticket_id="TICK-002", title="Harden lifecycle cleanup", touches=["src/lifecycle.ext"])
    other.write_text(other.read_text().replace("status: draft", "status: open"))
    prompt_dir = repo / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "analyze.md").write_text("custom analysis prompt\n")

    prompt = _build_prompt(parse_ticket(repo / "tickets" / "TICK-001.md"), repo, _CFG)

    assert "custom analysis prompt" in prompt
    assert "Active control-plane tickets" in prompt
    assert "Required control-plane overlap response contract" in prompt
    assert "overlap_review" in prompt
    assert "depends_on" in prompt


def test_active_control_plane_context_is_bounded_and_says_so_when_truncated(repo, monkeypatch):
    """This section had no byte budget while every neighbouring context
    section did (collect_cross_ticket_change_notes, get_bounded_shared_notes,
    get_bounded_reference_excerpts), so a board with many high-reasoning
    tickets at several touches each could inject tens of KB into every
    analyze prompt. Truncation is whole-line (a byte cutoff mid-line could
    misrepresent a ticket's true touches to the overlap gate) and says so
    explicitly when it drops tickets, since a silently-partial list would
    look exhaustive to the model doing overlap detection."""
    import lanegate.analyze_prompt as analyze_module
    from lanegate.analyze import _active_control_plane_ticket_context

    monkeypatch.setattr(analyze_module, "_ACTIVE_CONTROL_PLANE_BUDGET_BYTES", 120)

    tickets_dir = repo / "tickets"
    _make_draft(tickets_dir, title="Harden lifecycle cleanup")
    for i in range(2, 6):
        other = _make_draft(
            tickets_dir, ticket_id=f"TICK-00{i}", title="Harden lifecycle cleanup",
            touches=[f"src/module_{i}.py", f"src/other_{i}.py"],
        )
        other.write_text(other.read_text().replace("status: draft", "status: open"))

    result = _active_control_plane_ticket_context(
        parse_ticket(tickets_dir / "TICK-001.md"), tickets_dir, _CFG
    )

    assert "TICK-002" in result
    assert "TICK-005" not in result
    assert "more active control-plane ticket(s) omitted" in result
    assert "do not assume this list is exhaustive" in result


def test_analyze_prompt_discloses_active_control_plane_tickets_and_proposed_touch_matrix_rule(repo):
    _make_draft(repo / "tickets", title="Harden lifecycle cleanup")
    other = _make_draft(repo / "tickets", ticket_id="TICK-002", title="Harden lifecycle cleanup", touches=["src/lifecycle.ext"])
    other.write_text(other.read_text().replace("status: draft", "status: open"))

    prompt = _build_prompt(parse_ticket(repo / "tickets" / "TICK-001.md"), repo, _CFG)

    assert "Active control-plane tickets" in prompt
    assert "TICK-002 (open): src/lifecycle.ext" in prompt
    assert "every acceptance_matrix list must be non-empty" in prompt


def test_analyze_restores_close_criteria_on_already_resolved(repo, capsys):
    """When the model returns already_resolved=true with a reworded close_criteria,
    the drift guard must still detect drift and emit a warning before exiting.
    """
    original_criteria = "Original criteria wording that must be preserved."
    ticket_path = repo / "tickets" / "TICK-001.md"
    ticket_path.write_text(
        f"---\nid: TICK-001\ntitle: Add foo\nstatus: draft\npriority: 3\ntouches: []\n"
        f"close_criteria: |\n  {original_criteria}\n---\n## Background\n"
    )

    reworded_response = json.dumps({
        "already_resolved": True,
        "already_resolved_reason": "Feature is already present in master.",
        "close_criteria": "Reworded criteria by model.",
        "touches": ["lanegate/foo.py"],
    })

    # Running analyze should exit 0 via already_resolved path and emit the drift warning
    try:
        cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda _prompt: reworded_response)
    except SystemExit as e:
        assert e.code == 0

    captured = capsys.readouterr()
    assert "rewrote close_criteria" in captured.err


