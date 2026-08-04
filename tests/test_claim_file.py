"""Tests for claim_file.py — cmd_claim_file: lock check, dedup, commit."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lanegate.claim_file import claim_files, cmd_claim_file
from lanegate.ticket import parse_ticket

_CFG = {
    "ticket_prefix": "TICK",
    "tickets_dir": "tickets",
    "lock_statuses": ["in_progress", "code_complete", "in_review"],
    "commit_status_changes": False,  # no git in tests
}


def _write_ticket(tickets_dir: Path, ticket_id: str, status: str, touches=None) -> Path:
    touches_yaml = ""
    if touches:
        items = "\n".join(f"- {f}" for f in touches)
        touches_yaml = f"touches:\n{items}\n"
    else:
        touches_yaml = "touches: []\n"
    path = tickets_dir / f"{ticket_id}.md"
    path.write_text(
        f"---\nid: {ticket_id}\ntitle: Test ticket\nstatus: {status}\n{touches_yaml}---\n## Body\n"
    )
    return path


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "tickets").mkdir()
    return tmp_path


# ── Happy path ────────────────────────────────────────────────────────────────


def test_claim_file_adds_to_touches(repo):
    """File not locked by anyone → it gets added to the target ticket's touches."""
    _write_ticket(repo / "tickets", "TICK-001", "in_progress", touches=[])
    cmd_claim_file("src/foo.py", "TICK-001", _CFG, repo)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert "src/foo.py" in t["touches"]


def test_claim_file_prints_confirmation(repo, capsys):
    _write_ticket(repo / "tickets", "TICK-001", "in_progress", touches=[])
    cmd_claim_file("src/foo.py", "TICK-001", _CFG, repo)
    out = capsys.readouterr().out
    assert "TICK-001" in out
    assert "src/foo.py" in out


def test_claim_file_writes_file(repo):
    """Written ticket should survive a round-trip parse."""
    _write_ticket(repo / "tickets", "TICK-001", "in_progress", touches=["existing/file.py"])
    cmd_claim_file("new/file.py", "TICK-001", _CFG, repo)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert "existing/file.py" in t["touches"]
    assert "new/file.py" in t["touches"]


# ── Conflict: file locked by another ticket ───────────────────────────────────


def test_claim_file_conflict_exits_1(repo):
    """File already locked by a different in_progress ticket → sys.exit(1)."""
    _write_ticket(repo / "tickets", "TICK-001", "in_progress", touches=["src/shared.py"])
    _write_ticket(repo / "tickets", "TICK-002", "in_progress", touches=[])

    with pytest.raises(SystemExit) as exc_info:
        cmd_claim_file("src/shared.py", "TICK-002", _CFG, repo)
    assert exc_info.value.code == 1


def test_claim_file_conflict_prints_blocker(repo, capsys):
    """Error message must name the blocking ticket."""
    _write_ticket(repo / "tickets", "TICK-001", "in_progress", touches=["src/shared.py"])
    _write_ticket(repo / "tickets", "TICK-002", "in_progress", touches=[])

    with pytest.raises(SystemExit):
        cmd_claim_file("src/shared.py", "TICK-002", _CFG, repo)
    err = capsys.readouterr().err
    assert "TICK-001" in err
    assert "src/shared.py" in err


def test_claim_file_conflict_code_complete(repo):
    """Lock held at code_complete as well."""
    _write_ticket(repo / "tickets", "TICK-001", "code_complete", touches=["src/shared.py"])
    _write_ticket(repo / "tickets", "TICK-002", "in_progress", touches=[])

    with pytest.raises(SystemExit) as exc_info:
        cmd_claim_file("src/shared.py", "TICK-002", _CFG, repo)
    assert exc_info.value.code == 1


def test_claim_file_concrete_request_conflicts_with_wildcard_lock(repo):
    _write_ticket(repo / "tickets", "TICK-001", "in_progress", touches=['"*"'])
    _write_ticket(repo / "tickets", "TICK-002", "in_progress", touches=[])
    ok, detail = claim_files(["src/concrete.py"], "TICK-002", _CFG, repo)
    assert not ok
    assert detail and "*" in detail and "TICK-001" in detail


def test_claim_file_wildcard_request_conflicts_with_concrete_lock(repo):
    _write_ticket(repo / "tickets", "TICK-001", "in_progress", touches=["src/concrete.py"])
    _write_ticket(repo / "tickets", "TICK-002", "in_progress", touches=[])
    ok, detail = claim_files(["*"], "TICK-002", _CFG, repo)
    assert not ok
    assert detail and "*" in detail and "TICK-001" in detail


def test_claim_files_is_atomic_when_one_path_is_locked(repo):
    """A recovery claim must not add its safe path if a sibling path conflicts."""
    _write_ticket(repo / "tickets", "TICK-001", "in_progress", touches=["src/locked.py"])
    _write_ticket(repo / "tickets", "TICK-002", "needs_review", touches=[])

    ok, detail = claim_files(["src/safe.py", "src/locked.py"], "TICK-002", _CFG, repo)

    assert not ok
    assert detail and "TICK-001" in detail
    ticket = parse_ticket(repo / "tickets" / "TICK-002.md")
    assert ticket["touches"] == []


def test_claim_file_no_conflict_when_locker_is_merged(repo):
    """Merged ticket no longer holds a lock — claim must succeed."""
    _write_ticket(repo / "tickets", "TICK-001", "merged", touches=["src/shared.py"])
    _write_ticket(repo / "tickets", "TICK-002", "in_progress", touches=[])

    cmd_claim_file("src/shared.py", "TICK-002", _CFG, repo)
    t = parse_ticket(repo / "tickets" / "TICK-002.md")
    assert "src/shared.py" in t["touches"]


# ── Idempotent: file already in this ticket's touches ─────────────────────────


def test_claim_file_idempotent_no_duplicates(repo):
    """Claiming a file already in the ticket's own touches → no duplicates."""
    _write_ticket(repo / "tickets", "TICK-001", "in_progress", touches=["src/foo.py"])
    cmd_claim_file("src/foo.py", "TICK-001", _CFG, repo)
    t = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert t["touches"].count("src/foo.py") == 1


def test_claim_file_idempotent_no_conflict_with_self(repo):
    """File already in this ticket's touches must not be treated as a conflict."""
    _write_ticket(repo / "tickets", "TICK-001", "in_progress", touches=["src/foo.py"])
    # Must not exit 1 — the file is locked by *this* ticket, not another.
    cmd_claim_file("src/foo.py", "TICK-001", _CFG, repo)


# ── Missing ticket ─────────────────────────────────────────────────────────────


def test_claim_file_missing_ticket_exits_1(repo, capsys):
    """Target ticket does not exist → exit 1 with a clear error."""
    with pytest.raises(SystemExit) as exc_info:
        cmd_claim_file("src/foo.py", "TICK-999", _CFG, repo)
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "TICK-999" in err


# ── Git commit is called when commit_status_changes is True ───────────────────


def test_claim_file_commits_when_enabled(repo):
    """With commit_status_changes=True, git commit is invoked for the ticket file."""
    cfg = {**_CFG, "commit_status_changes": True}
    _write_ticket(repo / "tickets", "TICK-001", "in_progress", touches=[])

    with patch("lanegate.claim_file.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        cmd_claim_file("src/foo.py", "TICK-001", cfg, repo)

    # At least one call should be git commit --only
    commit_calls = [c for c in mock_run.call_args_list if "commit" in c.args[0]]
    assert commit_calls, "expected a git commit call"
    commit_cmd = commit_calls[0].args[0]
    assert "--only" in commit_cmd
