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


@pytest.fixture(autouse=True)
def _resolve_executor_bins_to_their_names(monkeypatch):
    monkeypatch.setattr("lanegate.executor.shutil.which", lambda bin_name: bin_name)






# --- _parse_response ---


def test_parse_response_plain_json():
    result = _parse_response(_GOOD_RESPONSE)
    assert result["touches"] == ["lanegate/foo.py", "tests/test_foo.py"]
    assert "cmd_foo" in result["close_criteria"]


def test_parse_response_strips_markdown_fence():
    wrapped = f"```json\n{_GOOD_RESPONSE}\n```"
    result = _parse_response(wrapped)
    assert result["touches"] == ["lanegate/foo.py", "tests/test_foo.py"]


def test_parse_response_strips_thinking_tags():
    thought_response = (
        "<think>\n"
        "Let's think about this ticket.\n"
        "We should touch {example} and make sure {key: val} works.\n"
        "</think>\n"
        f"{_GOOD_RESPONSE}"
    )
    result = _parse_response(thought_response)
    assert result["touches"] == ["lanegate/foo.py", "tests/test_foo.py"]


def test_parse_response_no_json_raises():
    with pytest.raises(ValueError):
        _parse_response("Sorry, I cannot help with that.")


def test_parse_response_bad_json_raises():
    with pytest.raises(json.JSONDecodeError):
        _parse_response("{bad json ,,}")


def test_parse_response_ignores_trailing_braces():
    trailing = f"{_GOOD_RESPONSE}\n\nNote: also see `{{example}}` for reference."
    result = _parse_response(trailing)
    assert result["touches"] == ["lanegate/foo.py", "tests/test_foo.py"]


def test_parse_response_ignores_braces_inside_strings():
    nested = '{"touches": [], "close_criteria": "uses {curly} braces in prose"}'
    result = _parse_response(nested)
    assert result["close_criteria"] == "uses {curly} braces in prose"


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
    """When model returns change_notes, cmd_analyze stores readable body section and scalar summary."""
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
    raw = (repo / "tickets" / "TICK-001.md").read_text(encoding="utf-8")
    assert "change_notes_summary: 2" in raw
    assert "change_notes:" not in raw.split("---\n")[1]
    assert "## Change Notes" in raw
    assert "**lanegate/foo.py**: Add foo() function at line 10." in raw

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


def _init_git_repo(path: Path) -> None:
    _subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    _subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)














































def test_heading_relevance_ignores_single_term_overlap_in_local_context(repo):
    """A filename-derived term alone must not scope in an unrelated section."""
    (repo / "docs").mkdir()
    (repo / "docs" / "voice-agent-roadmap.md").write_text(
        """
## Phone client live capture

- `/api/process` must support push-to-talk requests.

## Voice agent architecture

- The voice router must preserve caller context.
"""
    )
    ticket = {
        "id": "TICK-693a",
        "title": "Split daylog capture helper",
        "_body": (
            "Split daylog/capture.py; see docs/voice-agent-roadmap.md for the "
            "voice agent architecture context."
        ),
        "close_criteria": "The voice router preserves caller context.",
    }

    audit = audit_acceptance_contract(ticket, repo)

    joined = "\n".join(audit.findings)
    assert "/api/process" not in joined
    assert audit.ok is True


def test_heading_relevance_catches_two_term_overlap_in_local_context(repo):
    """Two meaningful heading terms in the path context retain enforcement."""
    (repo / "docs").mkdir()
    (repo / "docs" / "voice-agent-roadmap.md").write_text(
        """
## Phone client live capture

- `/api/process` must support push-to-talk requests.

## Voice agent architecture

- The voice router must preserve caller context.
"""
    )
    ticket = {
        "id": "TICK-693b",
        "title": "Split daylog capture helper",
        "_body": (
            "Split daylog/capture.py; see docs/voice-agent-roadmap.md for the "
            "voice agent architecture context."
        ),
        "close_criteria": "Split the capture helper.",
    }

    audit = audit_acceptance_contract(ticket, repo)

    assert audit.ok is False
    joined = "\n".join(audit.findings)
    assert "voice router must preserve caller context" in joined
    assert "/api/process" not in joined




def test_audit_ignores_review_findings(repo):
    """Generated review history must not become a new contract source."""
    ticket = {
        "id": "TICK-674",
        "title": "Extract voice routing helper",
        "_body": (
            "## Review Findings\n\n"
            "- `/api/process` must be included in the close criteria.\n\n"
            "## Dismissal Rationale\n\n"
            "- `/api/tts` must be included in the close criteria.\n\n"
            "## Acceptance Contract Audit\n\n"
            "- `/api/audit` must be included in the close criteria.\n\n"
            "## Background\n\nExtract the routing helper.\n"
        ),
        "close_criteria": "Extract the routing helper.",
    }

    audit = audit_acceptance_contract(ticket, repo)

    assert audit.ok is True
    assert audit.findings == []
    assert audit.checked_items == []
    assert audit.omitted_items == []














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

    prompt = _build_prompt(ticket, repo, cfg={"reference_docs": ["docs/ARCHITECTURE.md"]})

    assert "Orchestration Loop" in prompt
    assert "orchestrate.py" in prompt
    assert "Prose about promote.py" not in prompt








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
        drivers={"analyzer": {"type": "claude-process", "model": "claude-driver-analyze-model"}},
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
    assert cmd[cmd.index("--model") + 1] == "claude-driver-analyze-model"


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


def test_resolve_analyze_driver_passes_pool_name(repo):
    """resolve_analyze_driver must forward pool_name through to resolve_pool_executor.

    loop.py's _select_pool_instance already does this for implement dispatch
    (pool_name=name); analyze's driver resolution silently dropped an
    effective --pool override before this fix.
    """
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
    captured_kwargs = {}

    def fake_resolve_pool_executor(step, ticket, cfg, repo_root, **kwargs):
        captured_kwargs.update(kwargs)
        return "claude-1"

    with (
        patch(
            "lanegate.orchestrate.resolve_pool_executor",
            side_effect=fake_resolve_pool_executor,
        ),
        patch("lanegate.analyze._call_model", return_value=(_GOOD_RESPONSE, None)),
    ):
        cmd_analyze("TICK-001", cfg, repo)

    assert "pool_name" in captured_kwargs
    assert captured_kwargs["pool_name"] is None


def test_resolve_analyze_driver_error_explains_pool_sibling(repo):
    """When every pool sibling is cooling down, the raised error must name that
    condition and point at `lanegate executor status` instead of repeating the old
    opaque 'no healthy pool sibling' phrase.
    """
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

    with patch("lanegate.orchestrate.resolve_pool_executor", return_value=None):
        with pytest.raises(SystemExit) as exc_info:
            cmd_analyze("TICK-001", cfg, repo)

    detail = exc_info.value.__cause__
    assert detail is not None
    assert "cooling down" in str(detail)
    assert "lanegate executor status" in str(detail)


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


def test_analyze_fallback_retries_rate_limited_pool_instance(repo):
    """The normal-analysis fallback uses the same healthy-sibling failover."""
    _make_draft(repo / "tickets")
    source = repo / "lanegate"
    source.mkdir()
    (source / "foo.py").write_text("def actual():\n    return True\n")
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
        if len(calls) == 1:
            return json.dumps(
                {
                    "already_resolved": True,
                    "already_resolved_reason": "lanegate/foo.py:1 contains `def missing()`.",
                }
            ), None
        if kwargs["executor"] == "claude-1":
            raise RuntimeError("claude-1 failed (exit 1): rate limit exceeded")
        return _GOOD_RESPONSE, None

    with patch("lanegate.analyze._call_model", side_effect=fake_call_model):
        cmd_analyze("TICK-001", cfg, repo)

    assert calls == ["claude-1", "claude-1", "claude-2"]
    assert parse_ticket(repo / "tickets" / "TICK-001.md")["status"] == "open"


def test_analyze_cools_down_executor_after_consecutive_non_rate_limit_failures(repo):
    """A dead analyzer is removed from the pool after two matching failures."""
    from lanegate.executor import is_cooling_down

    for ticket_id in ("TICK-001", "TICK-002", "TICK-003"):
        _make_draft(repo / "tickets", ticket_id=ticket_id)
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
    failure_session_ids = iter(
        (
            "02ce0512-3419-455e-a6eb-e1b2f062178a",
            "7605b503-d8b2-4706-a6d8-2dcfe47dd6f8",
        )
    )

    def fake_call_model(prompt, **kwargs):
        calls.append(kwargs["executor"])
        if kwargs["executor"] == "claude-1":
            raise RuntimeError(
                "claude-1 failed (exit 1): synthetic executor error "
                f"session_id={next(failure_session_ids)}"
            )
        return _GOOD_RESPONSE, None

    with (
        patch("lanegate.analyze._active_analyze_run_id", return_value="test-run"),
        patch("lanegate.analyze._call_model", side_effect=fake_call_model),
    ):
        with pytest.raises(SystemExit):
            cmd_analyze("TICK-001", cfg, repo)
        with pytest.raises(SystemExit):
            cmd_analyze("TICK-002", cfg, repo)
        cmd_analyze("TICK-003", cfg, repo)

    assert calls == ["claude-1", "claude-1", "claude-2"]
    assert is_cooling_down(repo, "claude-1") is True
    assert is_cooling_down(repo, "claude-2") is False
    assert parse_ticket(repo / "tickets" / "TICK-003.md")["status"] == "open"


def test_analyze_fails_over_when_quota_notice_is_past_the_summary_clip(repo):
    """A stream-json quota error still fails over to a healthy sibling.

    The Claude CLI emits its whole transcript as a few enormous single lines,
    so "Claude AI usage limit reached" sits far past the 240-char clip that
    _summarize_executor_output applies to the exception message. Classifying
    off str(exc) missed it entirely and skipped pool failover; classification
    must run against the raw subprocess output.
    """
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
    # Shape captured live from a depleted claude-a on 2026-08-04: one 834-byte
    # line with the quota notice at offset ~680, well past the 240-char clip.
    quota_line = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "session_id": "2a57b0e1-ff8d-4891-abc1-96f7fe6e4918",
            "api_error_status": 429,
            "usage": {"input_tokens": 0, "output_tokens": 0, "padding": "x" * 400},
            "result": "You've hit your weekly limit · resets Aug 7, 6am (America/Los_Angeles)",
        }
    )
    assert quota_line.index("hit your weekly limit") > 240, "fixture must exceed the clip boundary"
    # Both pool members resolve to the same `claude` bin, so count model calls
    # rather than matching on argv: the first is claude-1, the retry claude-2.
    model_calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        if cmd[0] != "claude":
            return _subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        model_calls.append(list(cmd))
        if len(model_calls) == 1:
            return _subprocess.CompletedProcess(cmd, 1, stdout=quota_line, stderr="")
        return _subprocess.CompletedProcess(cmd, 0, stdout=_GOOD_RESPONSE, stderr="")

    with patch("lanegate.analyze.subprocess.run", side_effect=fake_run):
        cmd_analyze("TICK-001", cfg, repo)

    assert len(model_calls) == 2, "quota error did not trigger a sibling retry"

    assert parse_ticket(repo / "tickets" / "TICK-001.md")["status"] == "open"


def test_analyze_named_driver_bin_flags_and_env_reach_subprocess(repo, monkeypatch):
    """steps.analyze.driver applies command and env overrides to the analyze subprocess."""
    _make_draft(repo / "tickets")
    cfg = dict(
        _CFG,
        drivers={
            "analyzer": {
                "type": "claude-process",
                "model": "claude-driver-analyze-model",
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
    assert cmd[cmd.index("--model") + 1] == "claude-driver-analyze-model"
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
        assert "-s" in cmd, f"cmd_analyze omitted git commit signoff: {cmd}"


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


def test_cmd_analyze_captures_analyzed_at_sha(repo):
    """cmd_analyze records the repo HEAD SHA on the ticket, so implement can
    later determine whether any touched file drifted since analyze ran."""
    if shutil.which("git") is None:
        pytest.skip("git is required for analyze commit integration test")

    _make_draft(repo / "tickets")
    _subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    _subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    _subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    _subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    head_sha = _subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()

    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: _GOOD_RESPONSE)

    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t.get("analyzed_at_sha") == head_sha


# ---------------------------------------------------------------------------
# TICK-052: AST symbol index, import graph, enrichment chain
# ---------------------------------------------------------------------------




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










# --- _build_candidate_skeletons ---












# --- _build_ast_index ---










# --- _ast_symbol_hits ---










# --- _import_graph_expand ---








# --- enrich_context fallback chain ---










# --- prompt template updated ---












# --- analysis noise filtering ---








# ---------------------------------------------------------------------------
# TICK-081: real tree-sitter symbol indexing for non-Python files
# ---------------------------------------------------------------------------


























# Real grammar tests — skipped gracefully when grammar packages not installed.
























# ---------------------------------------------------------------------------
# TICK-085: static touch inference from close_criteria / intent text
# ---------------------------------------------------------------------------






























# ---------------------------------------------------------------------------
# TICK-291: companion docs + stale-path detection
# ---------------------------------------------------------------------------




























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














def test_analyze_warns_on_missing_grammar(repo, capsys, monkeypatch):
    from lanegate.analyze import _TS_LANG_CACHE, _build_file_skeleton

    _TS_LANG_CACHE.clear()
    try:
        go_file = repo / "main.go"
        go_file.write_text("package main\nfunc main() {}\n")

        import importlib
        orig_import = importlib.import_module

        def mock_import(name, *args, **kwargs):
            if name == "tree_sitter_go":
                raise ImportError("No module named 'tree_sitter_go'")
            return orig_import(name, *args, **kwargs)

        monkeypatch.setattr("importlib.import_module", mock_import)

        skeleton = _build_file_skeleton(go_file, repo)
        err = capsys.readouterr().err

        assert "WARNING" in err
        assert "missing tree-sitter grammar" in err or "tree_sitter_go" in err
        assert "main.go" in skeleton
    finally:
        _TS_LANG_CACHE.clear()


def test_analyze_warns_when_tree_sitter_not_installed(repo, capsys, monkeypatch):
    import lanegate.analyze_symbols as analyze_mod

    analyze_mod._TS_LANG_CACHE.clear()
    monkeypatch.setattr(analyze_mod, "_HAS_TREE_SITTER", False)
    try:
        go_file = repo / "main.go"
        go_file.write_text("package main\nfunc main() {}\n")

        skeleton = analyze_mod._build_file_skeleton(go_file, repo)
        err = capsys.readouterr().err

        assert "WARNING: tree-sitter is not installed" in err
        assert "main.go" in skeleton
    finally:
        analyze_mod._TS_LANG_CACHE.clear()


_HIGH_RISK_MATRIX = {
    "invariants": ["Configuration loads from the trusted root."],
    "adversarial_cases": ["A malformed configuration is rejected."],
    "compatibility_cases": ["Existing valid configuration keeps loading."],
    "regression_tests": ["test_config_loads_trusted_root"],
}


def test_analyze_persists_complete_high_risk_acceptance_matrix(repo):
    _make_draft(repo / "tickets", title="Harden configuration routing")
    response = json.dumps({
        "touches": ["lanegate/config.py", "tests/test_config.py"],
        "close_criteria": "Trusted configuration routing is covered by test_config_loads_trusted_root.",
        "depends_on": [],
        "acceptance_matrix": _HIGH_RISK_MATRIX,
    })
    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda _prompt: response)
    assert parse_ticket(repo / "tickets" / "TICK-001.md")["acceptance_matrix"] == _HIGH_RISK_MATRIX


@pytest.mark.parametrize("missing_field", ["adversarial_cases", "compatibility_cases", "regression_tests"])
def test_analyze_rejects_missing_high_risk_matrix_category(repo, missing_field):
    _make_draft(repo / "tickets", title="Harden configuration routing")
    incomplete = dict(_HIGH_RISK_MATRIX, **{missing_field: []})
    response = json.dumps({
        "touches": ["lanegate/config.py"], "close_criteria": "Configuration is safe.",
        "depends_on": [], "acceptance_matrix": incomplete,
    })
    with pytest.raises(SystemExit):
        cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda _prompt: response)
    assert parse_ticket(repo / "tickets" / "TICK-001.md")["status"] == "draft"


def test_analyze_succeeds_when_inferred_touch_would_otherwise_deadlock(repo):
    """[BLOCKING] regression test: the overlap gate was evaluated against
    merged_touches, which includes paths infer_touches_from_criteria/
    companion_docs_from_criteria add *after* the model responds, from the
    model's own close_criteria -- paths the model was never shown or asked
    about. That made the ticket permanently unanalyzable: the model correctly
    omits overlap_review (it never proposed that path), the gate finds the
    collision anyway, and every retry regenerates the identical close_criteria
    and fails identically. The gate must only see touches the model could
    have known about (existing + model-proposed); an inferred touch alone
    must not be able to deadlock analysis."""
    _make_draft(repo / "tickets", title="Harden configuration routing")
    other = _make_draft(
        repo / "tickets", ticket_id="TICK-002", title="Harden configuration routing",
        touches=["src/overlap_target.ext"],
    )
    other.write_text(other.read_text().replace("status: draft", "status: open"))
    response = json.dumps({
        "touches": ["src/unrelated.ext"],
        "close_criteria": "new file src/overlap_target.ext is added for X.",
        "depends_on": [], "acceptance_matrix": _HIGH_RISK_MATRIX,
    })

    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda _prompt: response)

    ticket = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert ticket["status"] == "open"
    assert "src/overlap_target.ext" in ticket["touches"]


def test_analyze_existing_touch_overlap_still_enforced_and_surfaced_in_prompt(repo):
    """existing_touches (carried forward on a re-analyzed ticket) ARE known
    before the prompt is built, unlike inferred/companion-doc touches, so the
    gate keeps enforcing them -- and the model is told about the collision
    directly, since it otherwise has no way to know a path it didn't itself
    propose is even part of its own touches."""
    tickets_dir = repo / "tickets"
    ticket_path = _make_draft(
        tickets_dir, title="Harden configuration routing", touches=["src/overlap_target.ext"],
    )
    ticket_path.write_text(ticket_path.read_text().replace("status: draft", "status: open"))
    other = _make_draft(
        tickets_dir, ticket_id="TICK-002", title="Harden configuration routing",
        touches=["src/overlap_target.ext"],
    )
    other.write_text(other.read_text().replace("status: draft", "status: open"))

    prompt = _build_prompt(parse_ticket(tickets_dir / "TICK-001.md"), repo, _CFG)
    assert "Your ticket's existing touches already overlap active tickets" in prompt
    assert "TICK-002" in prompt

    response = json.dumps({
        "touches": ["src/unrelated.ext"], "close_criteria": "Configuration is safe.",
        "depends_on": [], "acceptance_matrix": _HIGH_RISK_MATRIX,
    })
    with pytest.raises(SystemExit):
        cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda _prompt: response)


def test_overlap_error_names_missing_ticket_and_paths(repo, capsys):
    """[non-blocking, compounding] neither error branch named what was
    missing even though the ticket ID and paths were already in hand."""
    _make_draft(repo / "tickets", title="Harden configuration routing")
    other = _make_draft(repo / "tickets", ticket_id="TICK-002", title="Harden configuration routing", touches=["src/configuration.ext"])
    other.write_text(other.read_text().replace("status: draft", "status: open"))
    response = json.dumps({
        "touches": ["src/configuration.ext"], "close_criteria": "Configuration is safe.",
        "depends_on": [], "acceptance_matrix": _HIGH_RISK_MATRIX,
    })

    with pytest.raises(SystemExit):
        cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda _prompt: response)

    err = capsys.readouterr().err
    assert "TICK-002" in err
    assert "src/configuration.ext" in err


def test_overlap_error_names_specifically_missing_ticket_when_partially_declared(repo, capsys):
    _make_draft(repo / "tickets", title="Harden configuration routing")
    for tid, path in [("TICK-002", "src/config_a.ext"), ("TICK-003", "src/config_b.ext")]:
        other = _make_draft(repo / "tickets", ticket_id=tid, title="Harden configuration routing", touches=[path])
        other.write_text(other.read_text().replace("status: draft", "status: open"))
    response = json.dumps({
        "touches": ["src/config_a.ext", "src/config_b.ext"], "close_criteria": "Configuration is safe.",
        "depends_on": [], "acceptance_matrix": _HIGH_RISK_MATRIX,
        "overlap_review": {"mode": "stacked_review", "ticket_ids": ["TICK-002"]},
    })

    with pytest.raises(SystemExit):
        cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda _prompt: response)

    err = capsys.readouterr().err
    assert "TICK-003" in err
    assert "src/config_b.ext" in err




def test_analyze_rejects_control_plane_overlap_without_plan(repo):
    _make_draft(repo / "tickets", title="Harden configuration routing")
    other = _make_draft(repo / "tickets", ticket_id="TICK-002", title="Harden configuration routing", touches=["src/configuration.ext"])
    other.write_text(other.read_text().replace("status: draft", "status: open"))
    response = json.dumps({
        "touches": ["src/configuration.ext"], "close_criteria": "Configuration is safe.",
        "depends_on": [], "acceptance_matrix": _HIGH_RISK_MATRIX,
    })
    with pytest.raises(SystemExit):
        cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda _prompt: response)


@pytest.mark.parametrize(
    ("mode", "depends_on"),
    [("dependencies", ["TICK-002"]), ("stacked_review", [])],
)
def test_analyze_accepts_and_persists_control_plane_overlap_plan(repo, mode, depends_on):
    _make_draft(repo / "tickets", title="Harden configuration routing")
    other = _make_draft(repo / "tickets", ticket_id="TICK-002", title="Harden configuration routing", touches=["src/configuration.ext"])
    other.write_text(other.read_text().replace("status: draft", "status: open"))
    response = json.dumps({
        "touches": ["src/configuration.ext"], "close_criteria": "Configuration is safe.",
        "depends_on": depends_on, "acceptance_matrix": _HIGH_RISK_MATRIX,
        "overlap_review": {"mode": mode, "ticket_ids": ["TICK-002"]},
    })

    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda _prompt: response)

    ticket = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert ticket["overlap_review"] == {
        "mode": mode,
        "ticket_ids": ["TICK-002"],
        "paths": {"TICK-002": ["src/configuration.ext"]},
    }
    assert validate_ticket({k: v for k, v in ticket.items() if not k.startswith("_")}, _CFG) == []


def test_analyze_rejects_dependency_overlap_plan_without_depends_on(repo):
    _make_draft(repo / "tickets", title="Harden configuration routing")
    other = _make_draft(repo / "tickets", ticket_id="TICK-002", title="Harden configuration routing", touches=["src/configuration.ext"])
    other.write_text(other.read_text().replace("status: draft", "status: open"))
    response = json.dumps({
        "touches": ["src/configuration.ext"], "close_criteria": "Configuration is safe.",
        "depends_on": [], "acceptance_matrix": _HIGH_RISK_MATRIX,
        "overlap_review": {"mode": "dependencies", "ticket_ids": ["TICK-002"]},
    })

    with pytest.raises(SystemExit):
        cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda _prompt: response)
    assert parse_ticket(repo / "tickets" / "TICK-001.md")["status"] == "draft"


def test_analyze_rejects_non_mapping_overlap_plan_cleanly(repo, capsys):
    _make_draft(repo / "tickets", title="Harden configuration routing")
    other = _make_draft(repo / "tickets", ticket_id="TICK-002", title="Harden configuration routing", touches=["src/configuration.ext"])
    other.write_text(other.read_text().replace("status: draft", "status: open"))
    response = json.dumps({
        "touches": ["src/configuration.ext"], "close_criteria": "Configuration is safe.",
        "depends_on": [], "acceptance_matrix": _HIGH_RISK_MATRIX,
        "overlap_review": ["TICK-002"],
    })

    with pytest.raises(SystemExit):
        cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda _prompt: response)
    assert "overlap_review must be a mapping" in capsys.readouterr().err


def test_analyze_accepts_optional_partial_acceptance_matrix(repo):
    _make_draft(repo / "tickets", title="Fix README typo")
    optional_matrix = {
        "invariants": ["Existing links remain valid."],
        "adversarial_cases": [],
        "compatibility_cases": [],
        "regression_tests": [],
    }
    response = json.dumps({
        "touches": ["README.md"], "close_criteria": "README text is corrected.",
        "depends_on": [], "acceptance_matrix": optional_matrix,
    })

    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda _prompt: response)
    ticket = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert ticket["status"] == "open"
    assert ticket["acceptance_matrix"] == optional_matrix


def test_analyze_accepts_optional_matrix_with_omitted_categories(repo):
    _make_draft(repo / "tickets", title="Fix README typo")
    optional_matrix = {"invariants": ["Existing links remain valid."]}
    response = json.dumps({
        "touches": ["README.md"], "close_criteria": "README lifecycle text is corrected.",
        "depends_on": [], "acceptance_matrix": optional_matrix,
    })

    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda _prompt: response)

    assert parse_ticket(repo / "tickets" / "TICK-001.md")["acceptance_matrix"] == optional_matrix


def test_analyze_discards_malformed_optional_contract_fields_without_losing_analysis(repo, capsys):
    _make_draft(repo / "tickets", title="Fix README typo")
    response = json.dumps({
        "touches": ["README.md"], "close_criteria": "README text is corrected.",
        "depends_on": [], "acceptance_matrix": ["not a mapping"],
        "overlap_review": {"mode": "none", "ticket_ids": []},
    })

    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda _prompt: response)

    ticket = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert ticket["status"] == "open"
    assert "acceptance_matrix" not in ticket
    assert "overlap_review" not in ticket
    err = capsys.readouterr().err
    assert "discarding malformed optional acceptance_matrix" in err
    assert "discarding overlap_review" in err


def test_analyze_discards_self_referential_optional_overlap_plan(repo):
    _make_draft(repo / "tickets", title="Fix README typo")
    response = json.dumps({
        "touches": ["README.md"], "close_criteria": "README text is corrected.",
        "depends_on": [],
        "overlap_review": {"mode": "dependencies", "ticket_ids": ["TICK-001"]},
    })

    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda _prompt: response)

    assert "overlap_review" not in parse_ticket(repo / "tickets" / "TICK-001.md")








def test_analyze_does_not_require_matrix_from_model_generated_criteria(repo):
    _make_draft(repo / "tickets", title="Fix application behavior")
    response = json.dumps({
        "touches": ["README.md"], "close_criteria": "Application lifecycle behavior is documented.",
        "depends_on": [],
    })

    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda _prompt: response)

    assert parse_ticket(repo / "tickets" / "TICK-001.md")["status"] == "open"




# --- close_criteria drift detection ---


def test_close_criteria_drifted_helper():
    """Unit-tests for _close_criteria_drifted covering the key cases."""
    # Identical strings → no drift
    assert not _close_criteria_drifted("foo passes.", "foo passes.")

    # Whitespace/case only differ → not drift (normalised equal)
    assert not _close_criteria_drifted("foo  Passes.", "foo passes.")
    assert not _close_criteria_drifted("  foo passes. ", "foo passes.")

    # Genuinely reworded → drift detected
    assert _close_criteria_drifted(
        "test_analyze_restores_close_criteria_on_drift passes.",
        "the drift restoration test passes and the wording is correct.",
    )

    # Empty original → gracefully not drifted (no prior criteria to restore)
    assert not _close_criteria_drifted("", "some proposed criteria")

    # Empty original AND empty proposed → no prior criteria, nothing to restore
    assert not _close_criteria_drifted("", "")

    # Empty proposed with non-empty original → model omitted a pre-existing
    # criteria; this IS drift so the guard can restore the original wording.
    assert _close_criteria_drifted("original criteria", "")

    # --- list-typed original (YAML list close_criteria) ---
    # List original identical to proposed string → no drift
    assert not _close_criteria_drifted(["item 1"], "item 1")

    # List original different from proposed string → drift detected; must not
    # raise AttributeError ('list' object has no attribute 'strip').
    assert _close_criteria_drifted(["item 1", "item 2"], "completely different wording")

    # Empty list original → no prior criteria, nothing to restore
    assert not _close_criteria_drifted([], "some proposed criteria")

    # Non-empty list original, empty proposed → model omitted; this IS drift
    assert _close_criteria_drifted(["item 1"], "")


def test_analyze_restores_close_criteria_on_drift(repo, capsys, monkeypatch):
    """When the model rewrites close_criteria, _cmd_analyze_core restores the original.

    This is the exact regression test named in the TICK-655 acceptance matrix.
    """
    original_criteria = (
        "tests/test_analyze.py::test_analyze_restores_close_criteria_on_drift passes, "
        "proving that when the model's response rewords the close_criteria in "
        "_cmd_analyze_core, the original wording is auto-restored before saving the ticket."
    )
    # Write a draft with an explicit original close_criteria so the drift guard
    # has something to compare against.
    ticket_path = repo / "tickets" / "TICK-001.md"
    fm = (
        f"id: TICK-001\ntitle: Add foo command\nstatus: draft\npriority: 3\n"
        f"touches: []\nclose_criteria: |\n  {original_criteria}\n"
    )
    ticket_path.write_text(f"---\n{fm}---\n## Background\nWe need a foo command.\n")

    # Model returns a reworded close_criteria (complete content change, not whitespace).
    reworded_criteria = (
        "The implementation is correct and all tests pass with the proper wording restored."
    )
    reworded_response = json.dumps({
        "touches": ["lanegate/foo.py", "tests/test_foo.py"],
        "close_criteria": reworded_criteria,
        "depends_on": [],
    })

    inference_inputs = []

    def inferred_from(text, _repo_root):
        inference_inputs.append(text)
        return ["tests/test_analyze.py"]

    def companions_from(text, _repo_root, _cfg):
        inference_inputs.append(text)
        return ["README.md"]

    monkeypatch.setattr("lanegate.analyze.infer_touches_from_criteria", inferred_from)
    monkeypatch.setattr("lanegate.analyze.companion_docs_from_criteria", companions_from)

    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda _prompt: reworded_response)

    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    # Original must be restored, not the model's reworded version
    assert t["close_criteria"].strip() == original_criteria.strip()
    assert reworded_criteria not in t["close_criteria"]
    assert "tests/test_analyze.py" in t["touches"]
    assert "README.md" in t["touches"]
    assert len(inference_inputs) == 2
    assert all(original_criteria in text for text in inference_inputs)
    assert all(reworded_criteria not in text for text in inference_inputs)

    # A warning must have been emitted (to stderr)
    captured = capsys.readouterr()
    assert "rewrote close_criteria" in captured.err



def test_analyze_restores_close_criteria_when_model_returns_empty(repo, capsys):
    """When the model returns an empty close_criteria for a ticket that has one,
    the drift guard must restore the original instead of hard-failing.

    This covers the sibling path to test_analyze_restores_close_criteria_on_drift:
    model omission (empty string) is also drift and must be handled gracefully.
    """
    original_criteria = (
        "tests/test_analyze.py::test_analyze_restores_close_criteria_on_drift passes, "
        "proving that when the model's response rewords the close_criteria in "
        "_cmd_analyze_core, the original wording is auto-restored before saving the ticket."
    )
    ticket_path = repo / "tickets" / "TICK-001.md"
    fm = (
        f"id: TICK-001\ntitle: Add foo command\nstatus: draft\npriority: 3\n"
        f"touches: []\nclose_criteria: |\n  {original_criteria}\n"
    )
    ticket_path.write_text(f"---\n{fm}---\n## Background\nWe need a foo command.\n")

    # Model returns an empty close_criteria — simulates a model that dropped it entirely.
    empty_response = json.dumps({
        "touches": ["lanegate/foo.py", "tests/test_foo.py"],
        "close_criteria": "",
        "depends_on": [],
    })

    # Must not raise SystemExit — the drift guard restores the original.
    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda _prompt: empty_response)

    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    # Original must be restored, not an empty string
    assert t["close_criteria"].strip() == original_criteria.strip()

    # A warning about the restore must have been emitted
    captured = capsys.readouterr()
    assert "rewrote close_criteria" in captured.err


def test_analyze_does_not_crash_with_list_close_criteria(repo, capsys):
    """cmd_analyze must not crash with AttributeError when the ticket stores
    close_criteria as a YAML list (e.g. ``- item 1``).

    Regression for: 'list' object has no attribute 'strip' in _close_criteria_drifted.
    """
    ticket_path = repo / "tickets" / "TICK-001.md"
    # Write a ticket with a YAML-list close_criteria
    ticket_path.write_text(
        "---\n"
        "id: TICK-001\n"
        "title: Add foo command\n"
        "status: draft\n"
        "priority: 3\n"
        "touches: []\n"
        "close_criteria:\n"
        "  - item 1 passes\n"
        "  - item 2 passes\n"
        "---\n"
        "## Background\n"
        "We need a foo command.\n"
    )

    # Model returns a reworded single-string criteria — should trigger drift guard.
    reworded_response = json.dumps({
        "touches": ["lanegate/foo.py", "tests/test_foo.py"],
        "close_criteria": "completely different wording that is not the original",
        "depends_on": [],
    })

    # Must not raise AttributeError; drift guard must restore the original list.
    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda _prompt: reworded_response)

    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    # Original list must be preserved (drift guard restored it)
    original = t["close_criteria"]
    assert isinstance(original, list), f"Expected list, got {type(original)}: {original!r}"
    assert "item 1 passes" in original[0]

    # Warning about restore must be present
    captured = capsys.readouterr()
    assert "rewrote close_criteria" in captured.err
