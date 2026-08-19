"""Tests for ticket.py — parse, write, glob, canonical IDs, validation."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import yaml
import pytest

import lanegate.ticket as ticket_module
from lanegate.ticket import (
    append_lifecycle_event,
    _STANDARD_STATUSES,
    DEPENDENCY_SATISFIED_STATUSES,
    TERMINAL_STATUSES,
    QuarantinedTicket,
    append_status_history,
    attention_category,
    attention_summary,
    branch_name,
    canonical_id,
    collect_cross_ticket_change_notes,
    find_control_plane_touch_overlaps,
    display_order,
    get_ticket_diff,
    get_ticket_summary,
    group_by_status,
    load_all_tickets,
    is_paired_test_file,
    load_file_skeletons,
    needs_attention,
    parse_ticket,
    ticket_glob,
    validate_ticket,
    validate_acceptance_matrix,
    unresolved_dependencies,
    write_file_skeletons_sidecar,
    write_ticket,
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _make_ticket(
    tmp_path: Path, ticket_id: str, status: str = "open", touches=None, **extra
) -> Path:
    frontmatter = f"id: {ticket_id}\ntitle: Test ticket {ticket_id}\nstatus: {status}\n"
    if touches:
        frontmatter += "touches:\n" + "".join(f"  - {t}\n" for t in touches)
    for k, v in extra.items():
        frontmatter += f"{k}: {v}\n"
    path = tmp_path / f"{ticket_id}.md"
    path.write_text(f"---\n{frontmatter}---\nBody text.\n")
    return path


def test_parse_ticket_basic(tmp_path):
    path = _make_ticket(tmp_path, "TICK-001", status="open")
    t = parse_ticket(path)
    assert t["id"] == "TICK-001"
    assert t["status"] == "open"
    assert t["_body"] == "Body text."
    assert t["_path"] == path


def test_parse_ticket_missing_returns_none(tmp_path):
    assert parse_ticket(tmp_path / "nonexistent.md") is None


def test_parse_ticket_normalizes_unquoted_date(tmp_path):
    """created: 2026-07-03 (unquoted) is parsed by YAML as datetime.date — must come
    back as an ISO string so the ticket dict is JSON-serializable by construction."""
    import json

    path = _make_ticket(tmp_path, "TICK-003", status="open", created="2026-07-03")
    t = parse_ticket(path)
    assert t["created"] == "2026-07-03"
    assert isinstance(t["created"], str)
    json.dumps({k: v for k, v in t.items() if not k.startswith("_")})  # must not raise


def test_parse_ticket_no_frontmatter(tmp_path):
    path = tmp_path / "TICK-000.md"
    path.write_text("Just a body, no frontmatter.")
    assert parse_ticket(path) is None


def test_write_ticket_roundtrip(tmp_path):
    path = _make_ticket(tmp_path, "TICK-002", status="open")
    t = parse_ticket(path)
    t["status"] = "in_progress"
    write_ticket(t)
    reread = parse_ticket(path)
    assert reread["status"] == "in_progress"
    assert reread["_body"] == "Body text."


def test_write_ticket_is_atomic_against_interleaved_writers(tmp_path, monkeypatch):
    """An interleaved stale writer cannot leave ticket-frontmatter fragments."""
    path = _make_ticket(tmp_path, "TICK-002", status="open")
    older = parse_ticket(path)
    newer = parse_ticket(path)
    older["review_summary"] = "old summary " * 400
    older["_body"] = "old body"
    newer["review_summary"] = "new summary"
    newer["close_criteria"] = "new close criterion " * 200
    newer["_body"] = "new body"

    original_write_text = Path.write_text
    original_replace = ticket_module.os.replace
    interleaved = False

    def simulated_interleaved_write(self, text, *args, **kwargs):
        """Model a direct target write being preempted by the newer writer."""
        nonlocal interleaved
        if self == path and not interleaved:
            interleaved = True
            split_at = text.index("review_summary") + len("review_summary: ") + 20
            self.write_bytes(text[:split_at].encode(kwargs.get("encoding") or "utf-8"))
            write_ticket(newer)
            with self.open("r+", encoding=kwargs.get("encoding") or "utf-8") as handle:
                handle.seek(split_at)
                handle.write(text[split_at:])
            return len(text)
        return original_write_text(self, text, *args, **kwargs)

    def interleave_before_replace(source, destination):
        nonlocal interleaved
        if destination == path and not interleaved:
            interleaved = True
            write_ticket(newer)
        return original_replace(source, destination)

    monkeypatch.setattr(Path, "write_text", simulated_interleaved_write)
    monkeypatch.setattr(ticket_module.os, "replace", interleave_before_replace)

    write_ticket(older)

    raw = path.read_text()
    frontmatter = raw.split("---\n", 2)[1]
    assert yaml.safe_load(frontmatter)
    assert parse_ticket(path)["review_summary"] == older["review_summary"]
    assert raw.count("id: TICK-002") == 1


def test_write_ticket_truncates_oversized_frontmatter_scalar(tmp_path):
    path = _make_ticket(tmp_path, "TICK-002", status="open")
    ticket = parse_ticket(path)
    ticket["review_summary"] = "x" * (ticket_module._MAX_SCALAR_LEN + 17)

    write_ticket(ticket)

    reread = parse_ticket(path)
    assert reread["review_summary"].startswith("x" * ticket_module._MAX_SCALAR_LEN)
    assert reread["review_summary"].endswith("[truncated, 17 chars omitted]")
    assert len(reread["review_summary"]) > ticket_module._MAX_SCALAR_LEN


def test_ticket_glob_prefix(tmp_path):
    _make_ticket(tmp_path, "TICK-001")
    _make_ticket(tmp_path, "TICK-010")
    other = tmp_path / "README.md"
    other.write_text("not a ticket")
    paths = ticket_glob(tmp_path, "TICK")
    names = [p.name for p in paths]
    assert "TICK-001.md" in names
    assert "TICK-010.md" in names
    assert "README.md" not in names


def test_ticket_glob_excludes_other_prefix(tmp_path):
    _make_ticket(tmp_path, "FEAT-001")
    tick_paths = ticket_glob(tmp_path, "TICK")
    assert tick_paths == []
    feat_paths = ticket_glob(tmp_path, "FEAT")
    assert len(feat_paths) == 1


def test_canonical_id():
    assert canonical_id("tick-007") == "TICK-007"
    assert canonical_id("TICK-007") == "TICK-007"
    assert canonical_id("tick-7") == "TICK-007"
    assert canonical_id("acme.proj-7") == "ACME.PROJ-007"


@pytest.mark.parametrize("ticket_id", ["../TICK-001", "ACME..PROJ-001", "ACME.PROJ-001.", "TICK-001.lock"])
def test_canonical_id_rejects_path_and_git_ref_syntax(ticket_id):
    with pytest.raises(ValueError, match="invalid ticket ID"):
        canonical_id(ticket_id)


def test_branch_name():
    assert branch_name("TICK-007") == "tick-007"
    assert branch_name("tick-007") == "tick-007"


def test_get_ticket_diff_returns_structured_files(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    src = tmp_path / "src"
    src.mkdir()
    target = src / "foo.py"
    target.write_text("old\n")
    _git(tmp_path, "add", "src/foo.py")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "checkout", "-b", "tick-001")
    target.write_text("new\n")
    _git(tmp_path, "add", "src/foo.py")
    _git(tmp_path, "commit", "-m", "change foo")

    result = get_ticket_diff("TICK-001", tmp_path)

    assert result["id"] == "TICK-001"
    assert result["branch"] == "tick-001"
    assert result["files"][0]["path"] == "src/foo.py"
    assert result["files"][0]["status"] == "M"
    assert "-old" in result["files"][0]["patch"]
    assert "+new" in result["files"][0]["patch"]
    assert result["truncated"] is False
    assert result["error"] is None


def test_get_ticket_diff_missing_branch_returns_friendly_error(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "README.md").write_text("hello\n")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "base")

    result = get_ticket_diff("TICK-999", tmp_path)

    assert result["files"] == []
    assert result["truncated"] is False
    assert "no branch" in result["error"]
    assert "fatal:" not in result["error"]
    assert "ambiguous argument" not in result["error"]


def test_get_ticket_diff_truncates_large_patch(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    target = tmp_path / "notes.txt"
    target.write_text("old\n")
    _git(tmp_path, "add", "notes.txt")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "checkout", "-b", "tick-002")
    target.write_text("new\n" * 100)
    _git(tmp_path, "add", "notes.txt")
    _git(tmp_path, "commit", "-m", "change notes")

    result = get_ticket_diff("TICK-002", tmp_path, max_patch_chars=40)

    assert result["files"][0]["truncated"] is True
    assert result["truncated"] is True
    assert result["files"][0]["patch"].endswith("... [truncated]\n")


def test_get_ticket_diff_include_patches_false_skips_per_file_diff(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "notes.txt").write_text("old\n")
    _git(tmp_path, "add", "notes.txt")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "checkout", "-b", "tick-002")
    (tmp_path / "notes.txt").write_text("new\n")
    _git(tmp_path, "add", "notes.txt")
    _git(tmp_path, "commit", "-m", "change notes")

    with patch("lanegate.ticket._run_git", wraps=ticket_module._run_git) as spy:
        result = get_ticket_diff("TICK-002", tmp_path, include_patches=False)

    assert result["files"][0]["path"] == "notes.txt"
    assert result["files"][0]["status"] == "M"
    assert result["files"][0]["patch"] == ""
    assert "notes.txt" in result["stat"]
    # rev-parse + stat + name-status only -- no per-file `git diff -- <path>` call.
    diff_calls = [c for c in spy.call_args_list if c.args[1][:2] == ["diff", "main..tick-002"] and "--" in c.args[1]]
    assert diff_calls == []


def test_get_ticket_summary_aggregates_reason_cause_and_diff(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "README.md").write_text("hello\n")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "checkout", "-b", "tick-001")
    (tmp_path / "src.py").write_text("changed\n")
    _git(tmp_path, "add", "src.py")
    _git(tmp_path, "commit", "-m", "change")

    (tickets_dir / "TICK-001.md").write_text(
        "---\n"
        "id: TICK-001\n"
        "title: Fix the thing\n"
        "status: needs_review\n"
        "priority: 2\n"
        "milestone: v1.7\n"
        "review_verdict: changes_requested\n"
        "review_summary: nope\n"
        "---\n"
        "Body.\n\n"
        "## Needs Review Reason\n"
        "static analysis findings: unused import in src.py\n"
    )
    cfg = {"ticket_prefix": "TICK", "tickets_dir": "tickets"}

    result = get_ticket_summary("TICK-001", cfg, tmp_path)

    assert result["id"] == "TICK-001"
    assert result["status"] == "needs_review"
    assert result["title"] == "Fix the thing"
    assert "unused import in src.py" in result["reason"]
    assert result["needs_review_cause"] == "static_analysis"
    assert "lanegate reopen" in result["needs_review_recovery"]
    assert result["next_step"] == result["needs_review_recovery"]
    assert result["review_verdict"] == "changes_requested"
    assert result["review_summary"] == "nope"
    assert "src.py" in result["diff_stat"]
    assert result["files_changed"] == [{"path": "src.py", "status": "A"}]


def test_get_ticket_summary_code_complete_next_step_by_verdict(tmp_path):
    """A code_complete ticket with findings but no recorded verdict (e.g. from an
    ad-hoc/audit review) must not look identical to one still awaiting its first
    review -- it needs its own next-step advisory."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "README.md").write_text("hello\n")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "base")
    cfg = {"ticket_prefix": "TICK", "tickets_dir": "tickets"}

    (tickets_dir / "TICK-002.md").write_text(
        "---\nid: TICK-002\ntitle: Stalled\nstatus: code_complete\n"
        "review_findings:\n  - a real bug\n---\nBody.\n"
    )
    stalled = get_ticket_summary("TICK-002", cfg, tmp_path)
    assert "no verdict recorded" in stalled["next_step"]
    assert "lanegate review TICK-002" in stalled["next_step"]

    (tickets_dir / "TICK-003.md").write_text(
        "---\nid: TICK-003\ntitle: Rejected\nstatus: code_complete\n"
        "review_verdict: changes_requested\n---\nBody.\n"
    )
    rejected = get_ticket_summary("TICK-003", cfg, tmp_path)
    assert rejected["next_step"] == "address feedback, then: lanegate review TICK-003 --verdict approved"

    (tickets_dir / "TICK-004.md").write_text(
        "---\nid: TICK-004\ntitle: Approved\nstatus: code_complete\n"
        "review_verdict: approved\n---\nBody.\n"
    )
    approved = get_ticket_summary("TICK-004", cfg, tmp_path)
    assert approved["next_step"] == "lanegate merge TICK-004"


def test_get_ticket_summary_does_not_surface_stale_reason_on_a_healthy_ticket(tmp_path):
    """A ticket resumed via `lanegate start` (which bypasses cmd_reopen's body-
    stripping) can carry an old `## Needs Review Reason` section through to a
    later, healthy status -- it must not be reported as the current reason."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    (tickets_dir / "TICK-005.md").write_text(
        "---\nid: TICK-005\ntitle: Done\nstatus: merged\n---\n"
        "Body.\n\n## Needs Review Reason\nstale: unused import in src.py\n"
    )
    cfg = {"ticket_prefix": "TICK", "tickets_dir": "tickets"}

    result = get_ticket_summary("TICK-005", cfg, tmp_path)

    assert "reason" not in result


def test_get_ticket_summary_missing_ticket_returns_error(tmp_path):
    (tmp_path / "tickets").mkdir()
    cfg = {"ticket_prefix": "TICK", "tickets_dir": "tickets"}

    result = get_ticket_summary("TICK-999", cfg, tmp_path)

    assert result["status"] == 404
    assert "TICK-999" in result["error"]


def test_load_all_tickets(tmp_path):
    _make_ticket(tmp_path, "TICK-001", status="open")
    _make_ticket(tmp_path, "TICK-002", status="in_progress")
    tickets, quarantined = load_all_tickets(tmp_path, "TICK")
    assert len(tickets) == 2
    assert quarantined == []
    ids = {t["id"] for t in tickets}
    assert "TICK-001" in ids
    assert "TICK-002" in ids


def test_group_by_status(tmp_path):
    _make_ticket(tmp_path, "TICK-001", status="open")
    _make_ticket(tmp_path, "TICK-002", status="open")
    _make_ticket(tmp_path, "TICK-003", status="in_progress")
    tickets, _ = load_all_tickets(tmp_path, "TICK")
    grouped = group_by_status(tickets)
    assert len(grouped["open"]) == 2
    assert len(grouped["in_progress"]) == 1


def test_unknown_status_not_dropped(tmp_path):
    """Tickets with unknown statuses must not be silently dropped."""
    _make_ticket(tmp_path, "TICK-001", status="backlog")
    _make_ticket(tmp_path, "TICK-002", status="deferred")
    tickets, _ = load_all_tickets(tmp_path, "TICK")
    grouped = group_by_status(tickets)
    assert "backlog" in grouped
    assert "deferred" in grouped


def test_terminal_statuses():
    assert "merged" in TERMINAL_STATUSES
    assert "done" in TERMINAL_STATUSES
    assert "validated" in TERMINAL_STATUSES
    assert "open" not in TERMINAL_STATUSES


def test_closed_is_terminal_but_not_a_delivered_dependency():
    """Closed tickets remain archived while their dependents stay blocked."""
    assert "closed" in TERMINAL_STATUSES
    assert "closed" not in DEPENDENCY_SATISFIED_STATUSES


def test_unresolved_dependencies_uses_delivered_statuses_and_canonical_ids():
    status_map = {
        "TICK-001": "merged",
        "TICK-002": "validated",
        "TICK-003": "done",
        "TICK-004": "failed",
        "TICK-005": "closed",
    }
    assert unresolved_dependencies(["tick-1", "TICK-002", "TICK-3"], status_map) == []
    assert unresolved_dependencies(["TICK-004", "tick-5"], status_map) == ["TICK-004", "tick-5"]


def test_unresolved_dependencies_treats_malformed_id_as_unresolved_not_a_crash():
    """A malformed depends_on entry (executor/LLM-writable, unvalidated by
    validate_ticket) can never resolve to a real ticket -- it must count as
    unresolved like any other unmet dependency, not raise ValueError out of
    canonical_id() and crash board/next/batch selection for every ticket."""
    status_map = {"TICK-001": "merged"}
    assert unresolved_dependencies(["TICK-123 (blocked)", "tick-1"], status_map) == [
        "TICK-123 (blocked)"
    ]


# --- validate_ticket ---


def test_validate_ticket_valid():
    assert validate_ticket({"id": "TICK-001", "title": "Foo", "status": "open"}) == []


def test_validate_ticket_valid_draft_empty_touches():
    errors = validate_ticket({"id": "TICK-001", "title": "Foo", "status": "draft", "touches": []})
    assert errors == []


def test_validate_ticket_missing_id():
    errors = validate_ticket({"title": "Foo", "status": "open"})
    assert any("id" in e for e in errors)


def test_validate_ticket_missing_title():
    errors = validate_ticket({"id": "TICK-001", "status": "open"})
    assert any("title" in e for e in errors)


def test_validate_ticket_missing_status():
    errors = validate_ticket({"id": "TICK-001", "title": "Foo"})
    assert any("status" in e for e in errors)


def test_validate_ticket_bad_status():
    errors = validate_ticket({"id": "TICK-001", "title": "Foo", "status": "flying"})
    assert any("status" in e for e in errors)


def test_ticket_closed_status(tmp_path):
    """A ticket with status: closed is accepted as valid and loads without quarantine."""
    _make_ticket(tmp_path, "TICK-001", status="closed")
    tickets, quarantined = load_all_tickets(tmp_path, "TICK")

    # Verify no quarantine occurred
    assert len(quarantined) == 0, f"Expected no quarantined tickets, but got: {[q.error for q in quarantined]}"

    # Verify the ticket is loaded and accessible
    assert len(tickets) == 1
    ticket = tickets[0]
    assert ticket["id"] == "TICK-001"
    assert ticket["status"] == "closed"

    # Verify board grouping includes the closed status
    grouped = group_by_status(tickets)
    assert "closed" in grouped
    assert len(grouped["closed"]) == 1


def test_validate_ticket_bad_priority():
    errors = validate_ticket(
        {"id": "TICK-001", "title": "Foo", "status": "open", "priority": "high"}
    )
    assert any("priority" in e for e in errors)


def test_validate_ticket_int_priority_ok():
    assert (
        validate_ticket({"id": "TICK-001", "title": "Foo", "status": "open", "priority": 2}) == []
    )


def test_validate_ticket_touches_not_list():
    errors = validate_ticket(
        {"id": "TICK-001", "title": "Foo", "status": "open", "touches": "src/foo.py"}
    )
    assert any("touches" in e for e in errors)


def test_validate_ticket_file_skeletons_valid():
    errors = validate_ticket(
        {
            "id": "TICK-001",
            "title": "Foo",
            "status": "open",
            "file_skeletons": {"src/foo.py": "src/foo.py  (10 lines)\n  line   1: def foo()"},
        }
    )
    assert errors == []


def test_validate_ticket_file_skeletons_ref_summary_valid():
    errors = validate_ticket(
        {
            "id": "TICK-001",
            "title": "Foo",
            "status": "open",
            "file_skeletons_ref": ".lanegate/context/TICK-001/file_skeletons.json",
            "file_skeletons_summary": {"files": 1, "bytes": 42},
        }
    )
    assert errors == []


def test_validate_ticket_file_skeletons_summary_invalid():
    errors = validate_ticket(
        {
            "id": "TICK-001",
            "title": "Foo",
            "status": "open",
            "file_skeletons_ref": ".lanegate/context/TICK-001/file_skeletons.json",
            "file_skeletons_summary": {"files": "1", "bytes": -1},
        }
    )
    assert any("file_skeletons_summary.files" in e for e in errors)
    assert any("file_skeletons_summary.bytes" in e for e in errors)


def test_validate_ticket_file_skeletons_ref_must_be_repo_local_context_path():
    errors = validate_ticket(
        {
            "id": "TICK-001",
            "title": "Foo",
            "status": "open",
            "file_skeletons_ref": "../outside/file_skeletons.json",
            "file_skeletons_summary": {"files": 1, "bytes": 42},
        }
    )
    assert any("file_skeletons_ref" in e for e in errors)


def test_validate_ticket_file_skeletons_not_dict():
    errors = validate_ticket(
        {"id": "TICK-001", "title": "Foo", "status": "open", "file_skeletons": ["src/foo.py"]}
    )
    assert any("file_skeletons" in e for e in errors)


def test_validate_ticket_file_skeletons_non_string_value():
    errors = validate_ticket(
        {
            "id": "TICK-001",
            "title": "Foo",
            "status": "open",
            "file_skeletons": {"src/foo.py": 123},
        }
    )
    assert any("file_skeletons" in e for e in errors)


def test_write_file_skeletons_sidecar_roundtrip_compact_frontmatter(tmp_path):
    path = _make_ticket(tmp_path, "TICK-001", status="open")
    ticket = parse_ticket(path)

    ref = write_file_skeletons_sidecar(
        ticket,
        tmp_path,
        {"src/foo.py": "src/foo.py  (2 lines)\n  line   1: def foo()"},
    )
    write_ticket(ticket)

    reread = parse_ticket(path)
    sidecar = tmp_path / ref
    assert reread["file_skeletons_ref"] == ".lanegate/context/TICK-001/file_skeletons.json"
    assert reread["file_skeletons_summary"] == {
        "files": 1,
        "bytes": len(sidecar.read_bytes()),
    }
    assert "file_skeletons" not in reread
    assert json.loads(sidecar.read_text()) == {
        "src/foo.py": "src/foo.py  (2 lines)\n  line   1: def foo()"
    }
    assert "def foo()" not in path.read_text()


def test_load_file_skeletons_reads_sidecar(tmp_path):
    sidecar = tmp_path / ".lanegate" / "context" / "TICK-001" / "file_skeletons.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(json.dumps({"src/foo.py": "src/foo.py\n  line   1: def foo()"}))
    ticket = {
        "id": "TICK-001",
        "file_skeletons_ref": ".lanegate/context/TICK-001/file_skeletons.json",
    }

    assert load_file_skeletons(ticket, tmp_path) == {
        "src/foo.py": "src/foo.py\n  line   1: def foo()"
    }


def test_load_file_skeletons_falls_back_to_legacy_inline():
    ticket = {
        "id": "TICK-001",
        "file_skeletons": {"src/foo.py": "src/foo.py\n  line   1: def foo()"},
    }

    assert load_file_skeletons(ticket) == {
        "src/foo.py": "src/foo.py\n  line   1: def foo()"
    }


def test_load_file_skeletons_regenerates(tmp_path):
    """TICK-412: regenerate=True re-parses the current worktree file instead of
    replaying the stale analyze-time sidecar snapshot."""
    sidecar = tmp_path / ".lanegate" / "context" / "TICK-001" / "file_skeletons.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(json.dumps({"src/foo.py": "src/foo.py  (1 lines)\n  line   1: def stale()"}))

    src = tmp_path / "src" / "foo.py"
    src.parent.mkdir(parents=True)
    src.write_text("def fresh(x, y):\n    pass\n")

    ticket = {
        "id": "TICK-001",
        "touches": ["src/foo.py"],
        "file_skeletons_ref": ".lanegate/context/TICK-001/file_skeletons.json",
    }

    stale = load_file_skeletons(ticket, tmp_path)
    assert "def stale()" in stale["src/foo.py"]

    fresh = load_file_skeletons(ticket, tmp_path, regenerate=True)
    assert "def fresh(x, y)" in fresh["src/foo.py"]
    assert "def stale()" not in fresh["src/foo.py"]


def test_validate_ticket_bad_autonomy():
    errors = validate_ticket(
        {"id": "TICK-001", "title": "Foo", "status": "open", "autonomy": "robot"}
    )
    assert any("autonomy" in e for e in errors)


def test_validate_ticket_valid_autonomy_values():
    for val in ("full", "supervised", "manual"):
        assert (
            validate_ticket({"id": "TICK-001", "title": "Foo", "status": "open", "autonomy": val})
            == []
        )


def test_validate_ticket_risk_autonomy_lanes():
    """green/yellow/red risk-lane autonomy values (TICK-467) validate like
    the existing full/supervised/manual values; anything else still errors."""
    for val in ("green", "yellow", "red"):
        assert (
            validate_ticket({"id": "TICK-001", "title": "Foo", "status": "open", "autonomy": val})
            == []
        )

    errors = validate_ticket(
        {"id": "TICK-001", "title": "Foo", "status": "open", "autonomy": "orange"}
    )
    assert any("autonomy" in e for e in errors)


def test_validate_ticket_human_reviewer_ok():
    errors = validate_ticket(
        {"id": "TICK-001", "title": "Foo", "status": "open", "reviewer": "human"}
    )
    assert errors == []


def test_validate_ticket_human_executor_rejected():
    errors = validate_ticket(
        {"id": "TICK-001", "title": "Foo", "status": "open", "executor": "human"}
    )
    assert any("unknown executor" in e for e in errors)


def test_validate_ticket_ollama_executor_ok():
    errors = validate_ticket(
        {"id": "TICK-001", "title": "Foo", "status": "open", "executor": "ollama"}
    )
    assert errors == []


def test_validate_ticket_named_driver_executor_ok_with_cfg():
    errors = validate_ticket(
        {"id": "TICK-001", "title": "Foo", "status": "open", "executor": "local-codex"},
        {"drivers": {"local-codex": {"type": "codex"}}},
    )
    assert errors == []


def test_validate_ticket_named_driver_executor_rejected_without_drivers_cfg():
    errors = validate_ticket(
        {"id": "TICK-001", "title": "Foo", "status": "open", "executor": "local-codex"},
        {},
    )
    assert any("unknown executor" in e for e in errors)


def test_validate_ticket_legacy_executor_ok_with_cfg():
    errors = validate_ticket(
        {"id": "TICK-001", "title": "Foo", "status": "open", "executor": "codex"},
        {"drivers": {"local-codex": {"type": "codex"}}},
    )
    assert errors == []


def test_validate_ticket_schema_version_accepted():
    errors = validate_ticket(
        {"id": "TICK-001", "title": "Foo", "status": "open", "schema_version": 1}
    )
    assert errors == []


def test_validate_ticket_multiple_errors():
    errors = validate_ticket({"status": "flying"})
    assert len(errors) >= 3  # missing id, title, bad status


def test_load_all_tickets_with_named_driver_executor(tmp_path):
    """Verify that load_all_tickets passes cfg to validate_ticket for named driver validation."""
    _make_ticket(tmp_path, "TICK-001", status="open", executor="my-custom-driver")
    cfg = {"drivers": {"my-custom-driver": {"type": "codex"}}}
    tickets, quarantined = load_all_tickets(tmp_path, "TICK", cfg)
    assert len(tickets) == 1
    assert quarantined == []
    assert tickets[0]["id"] == "TICK-001"


def test_load_all_tickets_with_named_executor_instance(tmp_path):
    """TICK-247: the originally-reported repro -- a ticket carrying
    executor: <named-instance-under-executors:-with-a-type-field> (not
    drivers:) must not be quarantined when cfg is passed."""
    _make_ticket(tmp_path, "TICK-001", status="open", executor="claude-2")
    cfg = {"executors": {"claude-2": {"type": "claude-process"}}}
    tickets, quarantined = load_all_tickets(tmp_path, "TICK", cfg)
    assert len(tickets) == 1
    assert quarantined == []
    assert tickets[0]["id"] == "TICK-001"


def test_load_all_tickets_rejects_unknown_named_driver_without_cfg(tmp_path):
    """Verify that named drivers are rejected when cfg is not passed."""
    _make_ticket(tmp_path, "TICK-001", status="open", executor="my-custom-driver")
    tickets, quarantined = load_all_tickets(tmp_path, "TICK")
    assert len(tickets) == 0
    assert len(quarantined) == 1
    assert "unknown executor" in quarantined[0].error


def test_load_all_tickets_accepts_new_executor_types(tmp_path):
    """Verify that gemini and continue executor types are now accepted."""
    _make_ticket(tmp_path, "TICK-001", status="open", executor="gemini")
    _make_ticket(tmp_path, "TICK-002", status="open", executor="continue")
    tickets, quarantined = load_all_tickets(tmp_path, "TICK")
    assert len(tickets) == 2
    assert quarantined == []


# --- draft in _STANDARD_STATUSES and display_order ---


def test_draft_in_standard_statuses():
    assert "draft" in _STANDARD_STATUSES


def test_draft_not_terminal():
    assert "draft" not in TERMINAL_STATUSES


def test_recovery_statuses_are_standard_not_terminal():
    assert "hibernated" in _STANDARD_STATUSES
    assert "needs_review" in _STANDARD_STATUSES
    assert "hibernated" not in TERMINAL_STATUSES
    assert "needs_review" not in TERMINAL_STATUSES


def test_draft_display_order_before_open(tmp_path):
    _make_ticket(tmp_path, "TICK-001", status="open")
    _make_ticket(tmp_path, "TICK-002", status="draft")
    tickets, _ = load_all_tickets(tmp_path, "TICK")
    ordered = display_order(tickets)
    statuses = [t["status"] for t in ordered]
    assert statuses.index("draft") < statuses.index("open")


def test_clean_attention_reason_exit_codes():
    from lanegate.ticket import _clean_attention_reason
    assert _clean_attention_reason("executor exited with code 2") == "executor failed (exit code 2: CLI / configuration error)"
    assert _clean_attention_reason("executor exited with code 137") == "executor failed (exit code 137: process killed / out of memory)"
    assert _clean_attention_reason("executor exited with code 1") == "executor failed (exit code 1: general error)"


def test_classify_needs_review_cause_prioritizes_explicit_patterns():
    from lanegate.ticket import classify_needs_review_cause

    ticket = {
        "status": "needs_review",
        "_body": (
            "## Hibernation Reason\n\n"
            "rate limit or quota interruption (executor exited 429)\n\n"
            "## Needs Review Reason\n\n"
            "security_sensitive_paths — human review required"
        ),
    }

    assert classify_needs_review_cause(ticket) == "protected_path"


def test_classify_needs_review_cause_identifies_exhausted_auto_fix():
    from lanegate.ticket import classify_needs_review_cause, needs_review_recovery_advice

    ticket = {
        "id": "TICK-001",
        "status": "needs_review",
        "_body": "## Needs Review Reason\n\nbounded auto-fix/re-review exhausted (1/1)\n",
    }

    assert classify_needs_review_cause(ticket) == "auto_fix_exhausted"
    assert "lanegate reopen TICK-001, then lanegate review TICK-001" in needs_review_recovery_advice(ticket)


def test_needs_review_recovery_advice_rate_limit_with_auto_fix_attempts():
    from lanegate.ticket import classify_needs_review_cause, needs_review_recovery_advice

    ticket = {
        "id": "TICK-590",
        "status": "needs_review",
        "auto_fix_attempts": 1,
        "_body": "## Hibernation Reason\n\nrate limit or quota interruption (executor exited 429)\n",
    }

    assert classify_needs_review_cause(ticket) == "rate_limit"
    advice = needs_review_recovery_advice(ticket)
    assert "lanegate reopen TICK-590" in advice
    assert "recover-rate-limited-reviews" not in advice


def test_needs_review_recovery_advice_rate_limit_without_auto_fix_attempts():
    from lanegate.ticket import classify_needs_review_cause, needs_review_recovery_advice

    ticket = {
        "id": "TICK-590",
        "status": "needs_review",
        "_body": "## Hibernation Reason\n\nrate limit or quota interruption (executor exited 429)\n",
    }

    assert classify_needs_review_cause(ticket) == "rate_limit"
    assert "recover-rate-limited-reviews" in needs_review_recovery_advice(ticket)


def test_needs_attention():
    """Only tickets requiring a person enter the needs-human-decision queue."""
    cases = [
        ({"status": "needs_review"}, "escalated", "Manual review required"),
        ({"status": "failed"}, "failed", "Ticket failed; inspect log and worktree"),
        ({"status": "code_complete", "review_verdict": "changes_requested"}, "rejected", "Review changes requested"),
        (
            {
                "status": "hibernated",
                "_body": "## Hibernation Reason\n\nexecutor requires re-authentication",
            },
            "stuck",
            "executor requires re-authentication",
        ),
        (
            {"status": "in_review", "review_verdict": "approved", "autonomy": "manual"},
            "awaiting_merge",
            "Approved; awaiting human merge decision",
        ),
        (
            {"status": "in_review", "review_verdict": "approved", "autonomy": "red"},
            "awaiting_merge",
            "Approved; awaiting human merge decision",
        ),
        (
            {
                "status": "in_review",
                "review_verdict": "approved",
                "autonomy": "full",
                "requires_human_merge": True,
                "rebase_conflict_files": ["src/auth.py"],
            },
            "awaiting_merge",
            "Automated rebase conflict recovery; human merge approval required; inspect recovered files: src/auth.py",
        ),
    ]
    for ticket, category, summary in cases:
        assert needs_attention(ticket)
        assert attention_category(ticket) == category
        assert attention_summary(ticket) == summary

    excluded = [
        {
            "status": "hibernated",
            "_body": "## Hibernation Reason\n\nrate limit or quota interruption (executor exited 429)",
        },
        # Reviewer-cooldown hibernations auto-retry after review_retry_after
        # (TICK-517) -- they must not show up as "Stuck"/needing human
        # action, same as a genuine rate-limit hibernation above.
        {
            "status": "hibernated",
            "review_pending": True,
            "review_pending_reason": "Independent reviewer temporarily unavailable (cooldown); retry after 2026-08-13T01:00:00Z.",
        },
        {"status": "in_progress", "review_verdict": "changes_requested"},
        {"status": "in_review", "review_verdict": "changes_requested"},
        {"status": "in_review", "review_verdict": "approved", "autonomy": "full"},
        # A ticket closed via `lanegate supersede` flips status without
        # clearing review_verdict -- a stale changes_requested from before
        # closure must not resurrect a closed/merged/validated/done ticket
        # into the Next Steps queue forever.
        {"status": "closed", "review_verdict": "changes_requested"},
        {"status": "merged", "review_verdict": "changes_requested"},
        {"status": "validated", "review_verdict": "changes_requested"},
        {"status": "done", "review_verdict": "changes_requested"},
    ]
    for ticket in excluded:
        assert not needs_attention(ticket)
        assert attention_category(ticket) == ""



# --- quarantine ---


def _make_bad_ticket(tmp_path: Path, ticket_id: str, raw_frontmatter: str) -> Path:
    """Write a ticket with custom (possibly invalid) frontmatter."""
    path = tmp_path / f"{ticket_id}.md"
    path.write_text(f"---\n{raw_frontmatter}\n---\nBody.\n")
    return path


def test_valid_ticket_loads_normally(tmp_path):
    """A well-formed ticket appears in valid list and not in quarantine."""
    _make_ticket(tmp_path, "TICK-010", status="open")
    valid, quarantined = load_all_tickets(tmp_path, "TICK")
    assert len(valid) == 1
    assert valid[0]["id"] == "TICK-010"
    assert quarantined == []


def test_ticket_missing_required_field_is_quarantined(tmp_path):
    """A ticket missing 'title' is placed in quarantine, not in valid list."""
    _make_bad_ticket(tmp_path, "TICK-020", "id: TICK-020\nstatus: open\n")
    valid, quarantined = load_all_tickets(tmp_path, "TICK")
    assert valid == []
    assert len(quarantined) == 1
    assert isinstance(quarantined[0], QuarantinedTicket)
    assert "title" in quarantined[0].error
    assert quarantined[0].path.name == "TICK-020.md"


def test_ticket_wrong_type_for_required_field_is_quarantined(tmp_path):
    """A ticket with touches as a string (not a list) is quarantined."""
    _make_bad_ticket(
        tmp_path,
        "TICK-021",
        "id: TICK-021\ntitle: Bad touches\nstatus: open\ntouches: src/foo.py\n",
    )
    valid, quarantined = load_all_tickets(tmp_path, "TICK")
    assert valid == []
    assert len(quarantined) == 1
    assert "touches" in quarantined[0].error


def test_quarantined_tickets_do_not_appear_in_normal_board_output(tmp_path):
    """Quarantined tickets are excluded from the valid list returned to callers."""
    _make_ticket(tmp_path, "TICK-030", status="open")  # valid
    _make_bad_ticket(tmp_path, "TICK-031", "id: TICK-031\nstatus: open\n")  # missing title
    valid, quarantined = load_all_tickets(tmp_path, "TICK")
    assert len(valid) == 1
    assert valid[0]["id"] == "TICK-030"
    assert len(quarantined) == 1
    assert quarantined[0].path.name == "TICK-031.md"


def test_unparseable_ticket_is_quarantined(tmp_path):
    """A file without valid YAML frontmatter is quarantined rather than silently skipped."""
    bad_path = tmp_path / "TICK-040.md"
    bad_path.write_text("No frontmatter here at all.\n")
    valid, quarantined = load_all_tickets(tmp_path, "TICK")
    assert valid == []
    assert len(quarantined) == 1
    assert "could not parse" in quarantined[0].error


def test_invalid_yaml_frontmatter_is_quarantined_and_valid_tickets_still_load(tmp_path):
    """A YAML scanner error in one ticket does not prevent neighboring tickets loading."""
    _make_ticket(tmp_path, "TICK-041", status="open")
    bad_path = _make_bad_ticket(
        tmp_path,
        "TICK-042",
        "\n".join(
            [
                "id: TICK-042",
                "title: Invalid YAML",
                "status: open",
                "close_criteria: Tests cover: malformed YAML",
            ]
        ),
    )

    valid, quarantined = load_all_tickets(tmp_path, "TICK")

    assert [ticket["id"] for ticket in valid] == ["TICK-041"]
    assert len(quarantined) == 1
    assert quarantined[0].path == bad_path
    assert "could not parse frontmatter" in quarantined[0].error
    assert "mapping values are not allowed" in quarantined[0].error


# --- milestone field ---


def test_validate_ticket_milestone_valid():
    """A non-empty string milestone is accepted."""
    errors = validate_ticket(
        {"id": "TICK-001", "title": "Foo", "status": "open", "milestone": "v1"}
    )
    assert errors == []


def test_validate_ticket_milestone_free_string():
    """Any non-empty string is accepted as a milestone value."""
    for val in ("v1", "v2", "sprint-3", "auth", "batch-1"):
        errors = validate_ticket(
            {"id": "TICK-001", "title": "Foo", "status": "open", "milestone": val}
        )
        assert errors == [], f"expected no errors for milestone={val!r}, got {errors}"


def test_validate_ticket_milestone_absent_is_fine():
    """Tickets without a milestone field are valid."""
    errors = validate_ticket({"id": "TICK-001", "title": "Foo", "status": "open"})
    assert errors == []


def test_validate_ticket_milestone_empty_string_rejected():
    """An empty string milestone is invalid."""
    errors = validate_ticket({"id": "TICK-001", "title": "Foo", "status": "open", "milestone": ""})
    assert any("milestone" in e for e in errors)


def test_validate_ticket_milestone_whitespace_only_rejected():
    """A whitespace-only milestone is invalid."""
    errors = validate_ticket(
        {"id": "TICK-001", "title": "Foo", "status": "open", "milestone": "   "}
    )
    assert any("milestone" in e for e in errors)


# TICK-076: source and trusted field validation


def test_validate_ticket_trusted_false_valid():
    errors = validate_ticket({"id": "TICK-001", "title": "Foo", "status": "open", "trusted": False})
    assert errors == []


def test_validate_ticket_trusted_true_valid():
    errors = validate_ticket({"id": "TICK-001", "title": "Foo", "status": "open", "trusted": True})
    assert errors == []


def test_validate_ticket_trusted_absent_valid():
    errors = validate_ticket({"id": "TICK-001", "title": "Foo", "status": "open"})
    assert errors == []


def test_validate_ticket_trusted_non_bool_rejected():
    errors = validate_ticket({"id": "TICK-001", "title": "Foo", "status": "open", "trusted": "yes"})
    assert any("trusted" in e for e in errors)


def test_validate_ticket_source_string_valid():
    errors = validate_ticket(
        {"id": "TICK-001", "title": "Foo", "status": "open", "source": "github_issue"}
    )
    assert errors == []


def test_validate_ticket_source_absent_valid():
    errors = validate_ticket({"id": "TICK-001", "title": "Foo", "status": "open"})
    assert errors == []


def test_validate_ticket_source_non_string_rejected():
    errors = validate_ticket({"id": "TICK-001", "title": "Foo", "status": "open", "source": 123})
    assert any("source" in e for e in errors)


# ---------------------------------------------------------------------------
# validate_ticket — safeguards field (F41)
# ---------------------------------------------------------------------------


def test_validate_ticket_safeguards_valid_dict():
    """Safeguards as a dict with valid stages is accepted."""
    errors = validate_ticket({
        "id": "TICK-001",
        "title": "Foo",
        "status": "open",
        "safeguards": {
            "pre_complete": ["pytest"],
            "pre_merge": ["npm test"],
            "post_merge": ["make verify"],
        }
    })
    assert errors == []


def test_validate_ticket_safeguards_valid_string():
    """Safeguards as strings (single guard per stage) are accepted."""
    errors = validate_ticket({
        "id": "TICK-001",
        "title": "Foo",
        "status": "open",
        "safeguards": {
            "pre_complete": "pytest",
            "pre_merge": "npm test",
        }
    })
    assert errors == []


def test_validate_ticket_safeguards_valid_none():
    """Safeguards as None are accepted."""
    errors = validate_ticket({
        "id": "TICK-001",
        "title": "Foo",
        "status": "open",
        "safeguards": None
    })
    assert errors == []


def test_validate_ticket_safeguards_absent_is_fine():
    """Safeguards can be absent (not required)."""
    errors = validate_ticket({
        "id": "TICK-001",
        "title": "Foo",
        "status": "open",
    })
    assert errors == []


def test_validate_ticket_safeguards_not_dict_rejected():
    """Safeguards as a non-dict (non-None) are rejected."""
    errors = validate_ticket({
        "id": "TICK-001",
        "title": "Foo",
        "status": "open",
        "safeguards": ["pytest"]  # list, not dict
    })
    assert any("safeguards" in e and "dict" in e for e in errors)


def test_validate_ticket_safeguards_invalid_stage():
    """Unknown safeguards stage is rejected."""
    errors = validate_ticket({
        "id": "TICK-001",
        "title": "Foo",
        "status": "open",
        "safeguards": {
            "pre_complete": ["pytest"],
            "invalid_stage": ["npm test"],
        }
    })
    assert any("unknown stage" in e and "invalid_stage" in e for e in errors)


def test_validate_ticket_safeguards_non_string_guard():
    """Guard values must be strings (not ints, dicts, etc.)."""
    errors = validate_ticket({
        "id": "TICK-001",
        "title": "Foo",
        "status": "open",
        "safeguards": {
            "pre_complete": [123, "pytest"],  # 123 is not a string
        }
    })
    assert any("guards must be strings" in e for e in errors)


def test_validate_ticket_safeguards_with_none_stage():
    """A safeguards stage can be None (means no guards for that stage)."""
    errors = validate_ticket({
        "id": "TICK-001",
        "title": "Foo",
        "status": "open",
        "safeguards": {
            "pre_complete": None,  # None is allowed
            "pre_merge": ["pytest"],
        }
    })
    assert errors == []


def test_write_ticket_preserves_milestone(tmp_path):
    """write_ticket round-trips the milestone field correctly."""
    path = _make_ticket(tmp_path, "TICK-050", status="open", milestone="v1")
    t = parse_ticket(path)
    assert t["milestone"] == "v1"
    t["status"] = "in_progress"
    write_ticket(t)
    reread = parse_ticket(path)
    assert reread["milestone"] == "v1"
    assert reread["status"] == "in_progress"


def test_analyze_session_id_roundtrip(tmp_path):
    """analyze_session_id persists through write_ticket / parse_ticket round-trip."""
    session_id = "550e8400-e29b-41d4-a716-446655440000"
    path = _make_ticket(tmp_path, "TICK-051", status="open", analyze_session_id=session_id)
    t = parse_ticket(path)
    assert t["analyze_session_id"] == session_id
    t["status"] = "in_progress"
    write_ticket(t)
    reread = parse_ticket(path)
    assert reread["analyze_session_id"] == session_id
    assert reread["status"] == "in_progress"


def test_analyze_session_id_absent_is_valid(tmp_path):
    """Tickets without analyze_session_id pass validation."""
    path = _make_ticket(tmp_path, "TICK-052", status="open")
    t = parse_ticket(path)
    assert "analyze_session_id" not in t
    assert validate_ticket({k: v for k, v in t.items() if not k.startswith("_")}) == []


def test_analyze_session_id_non_string_rejected():
    """analyze_session_id must be a string; non-string values are rejected."""
    meta = {"id": "TICK-001", "title": "t", "status": "open", "analyze_session_id": 12345}
    errors = validate_ticket(meta)
    assert any("analyze_session_id" in e for e in errors)


# ---------------------------------------------------------------------------
# milestone_near_miss_warnings
# ---------------------------------------------------------------------------


def test_milestone_near_miss_warnings_detect_missing_v_prefix():
    """milestone_near_miss_warnings detects milestone='1.5' as a near-miss of v1.5."""
    from lanegate.ticket import milestone_near_miss_warnings

    tickets = [
        {"id": "TICK-001", "milestone": "1.5"},
        {"id": "TICK-002", "milestone": "v1.5"},
    ]
    warnings = milestone_near_miss_warnings(tickets, "v1.5")
    assert len(warnings) == 1
    assert warnings[0]["ticket_id"] == "TICK-001"
    assert warnings[0]["ticket_milestone"] == "1.5"
    assert warnings[0]["active_milestone"] == "v1.5"


def test_milestone_near_miss_warnings_no_false_positives():
    """milestone_near_miss_warnings does not warn for unrelated milestone values."""
    from lanegate.ticket import milestone_near_miss_warnings

    tickets = [
        {"id": "TICK-001", "milestone": "v2"},
        {"id": "TICK-002", "milestone": "experimental"},
    ]
    warnings = milestone_near_miss_warnings(tickets, "v1.5")
    assert len(warnings) == 0


def test_milestone_near_miss_warnings_case_insensitive():
    """milestone_near_miss_warnings is case-insensitive for exact matches but detects structural mismatches."""
    from lanegate.ticket import milestone_near_miss_warnings

    tickets = [
        {"id": "TICK-001", "milestone": "V1.5"},  # uppercase V — matches after normalization
        {"id": "TICK-002", "milestone": "1.5"},  # missing v — near-miss
    ]
    warnings = milestone_near_miss_warnings(tickets, "v1.5")
    # Only the one missing the v prefix should be warned about
    assert len(warnings) == 1
    assert warnings[0]["ticket_id"] == "TICK-002"


def test_milestone_near_miss_warnings_none_active_milestone():
    """milestone_near_miss_warnings returns empty when active_milestone is None."""
    from lanegate.ticket import milestone_near_miss_warnings

    tickets = [
        {"id": "TICK-001", "milestone": "1.5"},
    ]
    warnings = milestone_near_miss_warnings(tickets, None)
    assert len(warnings) == 0


def test_milestone_near_miss_warnings_exact_match_no_warning():
    """milestone_near_miss_warnings does not warn for exact matches."""
    from lanegate.ticket import milestone_near_miss_warnings

    tickets = [
        {"id": "TICK-001", "milestone": "v1.5"},
        {"id": "TICK-002", "milestone": "v2"},
    ]
    warnings = milestone_near_miss_warnings(tickets, "v1.5")
    assert len(warnings) == 0


# ---------------------------------------------------------------------------
# append_status_history
# ---------------------------------------------------------------------------


def test_append_status_history_creates_section_when_absent():
    ticket = {"_body": "Background text.\n"}
    append_status_history(ticket, "code_complete", "open", "hollow completion")
    body = ticket["_body"]
    assert "Background text." in body
    assert "## Status History" in body
    assert "code_complete → open (hollow completion)" in body


def test_append_status_history_appends_to_existing_section():
    ticket = {"_body": "Background.\n\n## Status History\n- 2026-07-20: open → in_progress (claimed)\n"}
    append_status_history(ticket, "code_complete", "open", "hollow completion")
    body = ticket["_body"]
    assert "open → in_progress (claimed)" in body
    assert "code_complete → open (hollow completion)" in body
    # New entry appended after the existing one, not replacing it.
    assert body.index("in_progress (claimed)") < body.index("code_complete → open")


def test_append_status_history_preserves_later_sections():
    ticket = {
        "_body": (
            "Background.\n\n## Status History\n- 2026-07-20: open → in_progress (claimed)\n"
            "\n## Needs Review Reason\n\nsome unrelated later section\n"
        )
    }
    append_status_history(ticket, "code_complete", "open", "hollow completion")
    body = ticket["_body"]
    assert "code_complete → open (hollow completion)" in body
    assert "## Needs Review Reason" in body
    assert "some unrelated later section" in body
    # The new history line lands before the later section, not after it.
    assert body.index("code_complete → open") < body.index("## Needs Review Reason")


def test_append_status_history_writes_iso_date():
    import datetime

    ticket = {"_body": ""}
    append_status_history(ticket, "failed", "open", "reset for fresh dispatch")
    today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    assert f"- {today}: failed → open (reset for fresh dispatch)" in ticket["_body"]


def test_append_lifecycle_event_writes_structured_and_readable_timeline():
    ticket = {"_body": "Background.\n"}

    append_lifecycle_event(
        ticket,
        event="merged",
        from_status="in_review",
        to_status="merged",
        summary="merge completed on main",
    )

    event = ticket["lifecycle_events"][-1]
    assert event["event"] == "merged"
    assert event["from_status"] == "in_review"
    assert event["to_status"] == "merged"
    assert event["summary"] == "merge completed on main"
    assert event["at"].endswith("Z")
    assert "## Lifecycle Timeline" in ticket["_body"]
    assert "in_review → merged — merged: merge completed on main" in ticket["_body"]


def test_append_lifecycle_event_preserves_later_ticket_sections():
    ticket = {"_body": "Background.\n\n## Needs Review Reason\n\nmanual check\n"}
    append_lifecycle_event(ticket, event="needs_review", summary="manual check")

    body = ticket["_body"]
    assert "## Lifecycle Timeline" in body
    assert "## Needs Review Reason" in body
    assert body.index("## Lifecycle Timeline") > body.index("## Needs Review Reason")


def test_append_lifecycle_event_roundtrip_preserves_event_name_and_summary(tmp_path):
    path = tmp_path / "TICK-100.md"
    path.write_text(
        "---\n"
        "id: TICK-100\n"
        "title: Test event preservation\n"
        "status: open\n"
        "---\n"
        "## Background\n\nProse.\n",
        encoding="utf-8",
    )
    t = parse_ticket(path)
    append_lifecycle_event(
        t,
        event="implementation_started",
        from_status="open",
        to_status="in_progress",
        summary="worktree claimed for implementation",
    )
    write_ticket(t)

    reloaded = parse_ticket(path)
    assert len(reloaded["lifecycle_events"]) == 1
    evt = reloaded["lifecycle_events"][0]
    assert evt["event"] == "implementation_started"
    assert evt["summary"] == "worktree claimed for implementation"
    assert evt["from_status"] == "open"
    assert evt["to_status"] == "in_progress"

    from lanegate.orchestrate.pool import _last_lifecycle_event_epoch
    epoch = _last_lifecycle_event_epoch(reloaded, "implementation_started")
    assert epoch is not None


def test_write_ticket_migrates_legacy_nested_frontmatter_to_readable_body_and_scalar_summaries(tmp_path):
    path = tmp_path / "TICK-999.md"
    legacy_text = (
        "---\n"
        "id: TICK-999\n"
        "title: Test ticket\n"
        "status: open\n"
        "change_notes:\n"
        "  lanegate/ticket.py: update serialization\n"
        "  lanegate/analyze.py: update analyze\n"
        "acceptance_contract_audit:\n"
        "  ok: false\n"
        "  findings:\n"
        "    - close_criteria omits item X\n"
        "  omitted_items:\n"
        "    - item X\n"
        "  checked_items:\n"
        "    - item Y\n"
        "  sources:\n"
        "    - docs/ARCHITECTURE.md\n"
        "lifecycle_events:\n"
        "  - at: 2026-08-06T05:21:10Z\n"
        "    event: start\n"
        "    from_status: open\n"
        "    to_status: in_progress\n"
        "    summary: claimed for implementation\n"
        "---\n"
        "## Background\n\n"
        "Test background prose.\n"
    )
    path.write_text(legacy_text, encoding="utf-8")

    t = parse_ticket(path)
    assert t["change_notes"]["lanegate/ticket.py"] == "update serialization"
    assert t["acceptance_contract_audit"]["ok"] is False
    assert t["lifecycle_events"][0]["from_status"] == "open"

    write_ticket(t)

    raw = path.read_text(encoding="utf-8")
    frontmatter_part = raw.split("---\n")[1]

    assert "change_notes_summary: 2" in frontmatter_part
    assert "acceptance_contract_audit_summary:" in frontmatter_part
    assert "lifecycle_events_summary: 1" in frontmatter_part

    # Ensure raw frontmatter YAML no longer has nested dicts/lists
    assert "\nchange_notes:\n" not in frontmatter_part
    assert "\nacceptance_contract_audit:\n" not in frontmatter_part
    assert "\nlifecycle_events:\n" not in frontmatter_part

    # Ensure raw body has readable sections
    assert "## Change Notes" in raw
    assert "**lanegate/ticket.py**: update serialization" in raw
    assert "## Acceptance Contract Audit" in raw
    assert "close_criteria omits item X" in raw
    assert "## Lifecycle Timeline" in raw
    assert "open → in_progress — start: claimed for implementation" in raw

    # Re-parse ticket and verify structured fields are rehydrated from body
    reloaded = parse_ticket(path)
    assert reloaded["change_notes"]["lanegate/ticket.py"] == "update serialization"
    assert reloaded["change_notes"]["lanegate/analyze.py"] == "update analyze"
    assert reloaded["acceptance_contract_audit"]["ok"] is False
    assert reloaded["acceptance_contract_audit"]["findings"] == ["close_criteria omits item X"]
    assert reloaded["lifecycle_events"][0]["at"] == "2026-08-06T05:21:10Z"
    assert reloaded["lifecycle_events"][0]["from_status"] == "open"
    assert reloaded["lifecycle_events"][0]["to_status"] == "in_progress"
    assert reloaded["lifecycle_events"][0]["event"] == "start"
    assert reloaded["lifecycle_events"][0]["summary"] == "claimed for implementation"


def test_load_change_notes_and_audit_and_lifecycle_from_body_and_legacy():
    # Legacy in-memory dicts take precedence
    t1 = {
        "change_notes": {"foo.py": "note 1"},
        "acceptance_contract_audit": {"ok": True, "findings": []},
        "lifecycle_events": [{"at": "2026-01-01T00:00:00Z", "event": "start", "summary": "test"}],
        "_body": "## Change Notes\n**bar.py**: note 2\n",
    }
    from lanegate.ticket import load_change_notes, load_acceptance_contract_audit, load_lifecycle_events
    assert load_change_notes(t1) == {"foo.py": "note 1"}
    assert load_acceptance_contract_audit(t1)["ok"] is True
    assert len(load_lifecycle_events(t1)) == 1

    # Hydration from body when frontmatter is missing or contains scalar summary
    t2 = {
        "change_notes_summary": 1,
        "acceptance_contract_audit_summary": "ok (0 findings)",
        "lifecycle_events_summary": 1,
        "_body": (
            "Background prose.\n\n"
            "## Change Notes\n"
            "**bar.py**: note 2\n\n"
            "## Acceptance Contract Audit\n"
            "**Status**: ok (0 findings)\n"
            "**Checked Items**:\n"
            "- item 1\n\n"
            "## Lifecycle Timeline\n"
            "- 2026-08-06T05:21:10Z: open → in_progress — claimed\n"
        ),
    }
    assert load_change_notes(t2) == {"bar.py": "note 2"}
    assert load_acceptance_contract_audit(t2)["ok"] is True
    assert load_acceptance_contract_audit(t2)["checked_items"] == ["item 1"]
    events = load_lifecycle_events(t2)
    assert len(events) == 1
    assert events[0]["from_status"] == "open"
    assert events[0]["to_status"] == "in_progress"


def test_collect_cross_ticket_change_notes(tmp_path):
    """A new ticket touching a file a prior *merged* ticket also touched should
    surface that prior ticket's change_notes for the overlapping file, tagged
    with the prior ticket's ID for provenance (TICK-481: replaces the dead
    worktree-vs-repo_root per-file .lanegate/notes/ mechanism with a lookup
    over the already git-tracked change_notes field)."""
    prior = {
        "_path": tmp_path / "TICK-100.md",
        "id": "TICK-100",
        "title": "Prior ticket",
        "status": "merged",
        "touches": ["foo.py"],
        "change_notes": {"foo.py": "some constraint discovered while implementing TICK-100"},
    }
    write_ticket(prior)

    new_ticket = {"id": "TICK-200", "touches": ["foo.py"]}

    result = collect_cross_ticket_change_notes(new_ticket, tmp_path, {"ticket_prefix": "TICK"})

    assert "Prior Change Notes" in result
    assert "TICK-100" in result
    assert "some constraint discovered while implementing TICK-100" in result


def test_collect_cross_ticket_change_notes_no_overlap_returns_empty(tmp_path):
    prior = {
        "_path": tmp_path / "TICK-101.md",
        "id": "TICK-101",
        "title": "Prior ticket",
        "status": "merged",
        "touches": ["bar.py"],
        "change_notes": {"bar.py": "unrelated constraint"},
    }
    write_ticket(prior)

    new_ticket = {"id": "TICK-201", "touches": ["foo.py"]}

    result = collect_cross_ticket_change_notes(new_ticket, tmp_path, {"ticket_prefix": "TICK"})

    assert result == ""


def test_collect_cross_ticket_change_notes_ignores_non_terminal_status(tmp_path):
    """An in-progress prior ticket's change_notes should not leak into a new
    ticket's prompt -- only merged/done tickets are considered settled."""
    prior = {
        "_path": tmp_path / "TICK-102.md",
        "id": "TICK-102",
        "title": "In-flight ticket",
        "status": "in_progress",
        "touches": ["foo.py"],
        "change_notes": {"foo.py": "not yet settled"},
    }
    write_ticket(prior)

    new_ticket = {"id": "TICK-202", "touches": ["foo.py"]}

    result = collect_cross_ticket_change_notes(new_ticket, tmp_path, {"ticket_prefix": "TICK"})

    assert result == ""



# --- F35: delimiter parsing resilience ---


def test_parse_ticket_with_dashes_in_title(tmp_path):
    """F35: A title containing --- should not corrupt the ticket.

    Prior to the fix, a title like "Handle --- case" would split
    at the --- in the title instead of at the delimiter, causing
    the frontmatter to be parsed incorrectly.
    """
    path = tmp_path / "TICK-999.md"
    path.write_text("---\nid: TICK-999\ntitle: Handle --- case\nstatus: open\n---\nBody text.\n")

    t = parse_ticket(path)

    # Must parse successfully
    assert t is not None, "parse_ticket returned None (ticket was corrupted)"
    assert t["id"] == "TICK-999"
    assert t["title"] == "Handle --- case"
    assert t["status"] == "open"
    assert t["_body"] == "Body text."


def test_parse_ticket_with_dashes_in_frontmatter_values(tmp_path):
    """F35: Multiple --- patterns in frontmatter should not corrupt parsing."""
    path = tmp_path / "TICK-998.md"
    path.write_text(
        "---\n"
        "id: TICK-998\n"
        "title: Fix --- handling ---\n"
        "status: open\n"
        "close_criteria: Code handles --- separators --- correctly\n"
        "---\n"
        "Body with --- in it too.\n"
    )

    t = parse_ticket(path)

    assert t is not None
    assert t["id"] == "TICK-998"
    assert t["title"] == "Fix --- handling ---"
    assert t["close_criteria"] == "Code handles --- separators --- correctly"
    assert t["_body"] == "Body with --- in it too."


def test_write_and_roundtrip_ticket_with_dashes_in_title(tmp_path):
    """F35: A ticket with --- in the title should roundtrip correctly."""
    path = _make_ticket(tmp_path, "TICK-997", status="open", title="Handle --- case")
    t = parse_ticket(path)
    assert t["title"] == "Handle --- case"

    # Modify and write back
    t["status"] = "in_progress"
    write_ticket(t)

    # Reread and verify
    reread = parse_ticket(path)
    assert reread["title"] == "Handle --- case"
    assert reread["status"] == "in_progress"
    assert reread["_body"] == "Body text."


def test_load_all_tickets_with_dashes_in_title(tmp_path):
    """F35: load_all_tickets should not quarantine tickets with --- in titles."""
    _make_ticket(tmp_path, "TICK-996", status="open", title="Handle --- case")
    _make_ticket(tmp_path, "TICK-995", status="open", title="Another --- separator --- test")

    tickets, quarantined = load_all_tickets(tmp_path, "TICK")

    assert len(quarantined) == 0, f"Expected no quarantined tickets, got: {[q.error for q in quarantined]}"
    assert len(tickets) == 2
    ids = {t["id"] for t in tickets}
    assert "TICK-996" in ids
    assert "TICK-995" in ids


class TestIsPairedTestFile:
    """Unit tests for is_paired_test_file (TICK-245). Shared by orchestrate.py's
    board-clearing-loop guard and lifecycle.py's check_touches_compliance."""

    def test_paired_test_file_for_touched_module(self):
        assert is_paired_test_file("tests/test_orchestrate.py", {"lanegate/orchestrate.py"})

    def test_unrelated_test_file_not_paired(self):
        assert not is_paired_test_file("tests/test_other.py", {"lanegate/orchestrate.py"})

    def test_non_test_file_not_paired(self):
        assert not is_paired_test_file("lanegate/unexpected.py", {"lanegate/orchestrate.py"})

    def test_test_file_outside_tests_dir_not_paired(self):
        assert not is_paired_test_file("lanegate/test_orchestrate.py", {"lanegate/orchestrate.py"})

    def test_paired_test_file_when_touched_module_nested(self):
        """Matches by module stem regardless of the touched module's own directory depth."""
        assert is_paired_test_file("tests/test_foo.py", {"myapp/sub/foo.py"})


def test_explanatory_docstrings():
    """TICK-391 stripped these down to one-liners; TICK-452 restores the
    architectural context (canonical-parser and status/lifecycle-auditing
    invariants) so it isn't lost again to a future rewrite."""
    funcs = [
        ticket_module.review_findings_sections,
        ticket_module.next_review_findings_header,
        append_status_history,
        append_lifecycle_event,
    ]
    for func in funcs:
        doc = func.__doc__ or ""
        assert len(doc.strip().splitlines()) > 1, f"{func.__name__} is missing its explanatory docstring"
        assert len(doc.strip()) > 120, f"{func.__name__} docstring is too short to be explanatory"


def test_acceptance_matrix_requires_every_contract_category():
    complete = {
        "invariants": ["The lock remains atomic."],
        "adversarial_cases": ["Malformed input is rejected."],
        "compatibility_cases": ["Existing CLI output remains accepted."],
        "regression_tests": ["test_lock_rejects_malformed_input"],
    }
    assert validate_acceptance_matrix(complete, required=True) == []
    incomplete = dict(complete, adversarial_cases=[])
    assert "adversarial_cases" in " ".join(validate_acceptance_matrix(incomplete, required=True))
    assert validate_acceptance_matrix(incomplete) == []
    assert validate_acceptance_matrix({"invariants": complete["invariants"]}) == []


def test_find_control_plane_touch_overlaps_ignores_terminal_tickets(tmp_path):
    _make_ticket(tmp_path, "TICK-001", status="open", touches=["src/control.ext"], title="Harden configuration routing")
    _make_ticket(tmp_path, "TICK-002", status="merged", touches=["src/control.ext"], title="Harden configuration routing")
    _make_ticket(tmp_path, "TICK-003", status="open", touches=["docs/guide.md"], title="Harden configuration routing")
    overlaps = find_control_plane_touch_overlaps(
        {"id": "TICK-004", "title": "Harden configuration routing", "touches": ["src/control.ext"]}, tmp_path
    )
    assert overlaps == [{"ticket_id": "TICK-001", "paths": ["src/control.ext"]}]


def test_upsert_body_section_with_nested_subheadings():
    """Verify _upsert_body_section correctly replaces sections containing ### subheadings
    without treating ### as an H2 section boundary."""
    from lanegate.ticket import _upsert_body_section

    header = "## Archived Review Findings (2026-08-17)"
    sec1 = f"{header}\n\n**Summary**: summary 1\n**Reviewed At**: 2026-08-17T10:00:00Z\n**Dismissal Rationale**: rationale 1\n\n### Findings\n- finding 1\n"
    sec2 = f"{header}\n\n**Summary**: summary 2\n**Reviewed At**: 2026-08-17T11:00:00Z\n**Dismissal Rationale**: rationale 2\n\n### Findings\n- finding 2\n"

    body0 = "Initial body prose.\n\n## Change Notes\n**foo.py**: updated\n"
    body1 = _upsert_body_section(body0, header, sec1)
    assert "summary 1" in body1
    assert "finding 1" in body1
    assert "## Change Notes" in body1

    body2 = _upsert_body_section(body1, header, sec2)
    assert "summary 2" in body2
    assert "finding 2" in body2
    assert "## Change Notes" in body2
    assert "summary 1" not in body2
    assert "finding 1" not in body2
    assert body2.count("### Findings") == 1

