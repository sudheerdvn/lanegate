"""
tests/test_ghsync.py — coverage for lanegate/ghsync.py (previously untested).

Covers:
- _gh_available: gh present/absent/erroring
- _find_issue: exact-match dedup (TICK-1 must not match TICK-10)
- cmd_gh_sync: create/skip-terminal/update/close/reopen, and dry-run for each
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from lanegate.ghsync import _find_issue, _gh_available, cmd_gh_sync


def _default_cfg(tickets_dir: Path) -> dict:
    return {
        "ticket_prefix": "TICK",
        "tickets_dir": tickets_dir.name,
    }


def _write_ticket(tickets_dir: Path, ticket_id: str, status: str, title: str = "Do a thing") -> None:
    num = ticket_id.split("-")[1]
    (tickets_dir / f"TICK-{num}.md").write_text(
        f"---\nid: {ticket_id}\ntitle: {title}\nstatus: {status}\nbatch: 1\npriority: 2\n---\n\nbody\n",
        encoding="utf-8",
    )


class TestGhAvailable:
    def test_available_when_gh_returns_zero(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=0)) as m:
            assert _gh_available() is True
            m.assert_called_once_with(["gh", "--version"], capture_output=True)

    def test_unavailable_when_gh_returns_nonzero(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=1)):
            assert _gh_available() is False

    def test_unavailable_when_gh_not_on_path(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert _gh_available() is False


class TestFindIssue:
    def test_exact_prefix_match(self, tmp_path):
        issues = [{"number": 1, "title": "[TICK-1] Do a thing", "state": "OPEN"}]
        r = MagicMock(returncode=0, stdout=json.dumps(issues))
        with patch("subprocess.run", return_value=r):
            found = _find_issue("TICK-1", tmp_path)
        assert found == issues[0]

    def test_does_not_substring_match_tick_1_against_tick_10(self, tmp_path):
        """TICK-1 must not match an issue titled [TICK-10] ... — exact prefix only."""
        issues = [{"number": 2, "title": "[TICK-10] Something else", "state": "OPEN"}]
        r = MagicMock(returncode=0, stdout=json.dumps(issues))
        with patch("subprocess.run", return_value=r):
            found = _find_issue("TICK-1", tmp_path)
        assert found is None

    def test_returns_none_on_nonzero_exit(self, tmp_path):
        r = MagicMock(returncode=1, stdout="")
        with patch("subprocess.run", return_value=r):
            assert _find_issue("TICK-1", tmp_path) is None

    def test_returns_none_on_bad_json(self, tmp_path):
        r = MagicMock(returncode=0, stdout="not json")
        with patch("subprocess.run", return_value=r):
            assert _find_issue("TICK-1", tmp_path) is None


class TestCmdGhSync:
    def test_skips_entirely_when_gh_unavailable(self, tmp_path, capsys):
        tickets_dir = tmp_path / "tickets"
        tickets_dir.mkdir()
        cfg = _default_cfg(tickets_dir)
        with patch("lanegate.ghsync._gh_available", return_value=False):
            cmd_gh_sync(cfg, tmp_path)
        err = capsys.readouterr().err
        assert "not available" in err

    def test_creates_issue_for_new_non_terminal_ticket(self, tmp_path, capsys):
        tickets_dir = tmp_path / "tickets"
        tickets_dir.mkdir()
        _write_ticket(tickets_dir, "TICK-1", "in_progress")
        cfg = _default_cfg(tickets_dir)

        create_result = MagicMock(returncode=0, stdout="https://github.com/x/y/issues/1", stderr="")
        with patch("lanegate.ghsync._gh_available", return_value=True), \
                patch("lanegate.ghsync._find_issue", return_value=None), \
                patch("subprocess.run", return_value=create_result) as m:
            cmd_gh_sync(cfg, tmp_path)

        args = m.call_args.args[0]
        assert args[:3] == ["gh", "issue", "create"]
        assert "[TICK-1] Do a thing" in args
        assert "created" in capsys.readouterr().out

    def test_skips_creating_issue_for_terminal_ticket_with_no_existing_issue(self, tmp_path):
        tickets_dir = tmp_path / "tickets"
        tickets_dir.mkdir()
        _write_ticket(tickets_dir, "TICK-1", "merged")
        cfg = _default_cfg(tickets_dir)

        with patch("lanegate.ghsync._gh_available", return_value=True), \
                patch("lanegate.ghsync._find_issue", return_value=None), \
                patch("subprocess.run") as m:
            cmd_gh_sync(cfg, tmp_path)

        m.assert_not_called()

    def test_dry_run_does_not_call_gh_issue_create(self, tmp_path, capsys):
        tickets_dir = tmp_path / "tickets"
        tickets_dir.mkdir()
        _write_ticket(tickets_dir, "TICK-1", "in_progress")
        cfg = _default_cfg(tickets_dir)

        with patch("lanegate.ghsync._gh_available", return_value=True), \
                patch("lanegate.ghsync._find_issue", return_value=None), \
                patch("subprocess.run") as m:
            cmd_gh_sync(cfg, tmp_path, dry_run=True)

        m.assert_not_called()
        assert "would create" in capsys.readouterr().out

    def test_updates_existing_open_issue_for_non_terminal_ticket(self, tmp_path, capsys):
        tickets_dir = tmp_path / "tickets"
        tickets_dir.mkdir()
        _write_ticket(tickets_dir, "TICK-1", "in_progress")
        cfg = _default_cfg(tickets_dir)
        existing = {"number": 7, "title": "[TICK-1] Do a thing", "state": "OPEN"}

        with patch("lanegate.ghsync._gh_available", return_value=True), \
                patch("lanegate.ghsync._find_issue", return_value=existing), \
                patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as m:
            cmd_gh_sync(cfg, tmp_path)

        # only the edit call, no close/reopen for a still-open non-terminal ticket
        assert m.call_count == 1
        assert m.call_args.args[0][:3] == ["gh", "issue", "edit"]
        assert "updated #7" in capsys.readouterr().out

    def test_closes_open_issue_when_ticket_reaches_terminal_status(self, tmp_path, capsys):
        tickets_dir = tmp_path / "tickets"
        tickets_dir.mkdir()
        _write_ticket(tickets_dir, "TICK-1", "merged")
        cfg = _default_cfg(tickets_dir)
        existing = {"number": 7, "title": "[TICK-1] Do a thing", "state": "OPEN"}

        with patch("lanegate.ghsync._gh_available", return_value=True), \
                patch("lanegate.ghsync._find_issue", return_value=existing), \
                patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as m:
            cmd_gh_sync(cfg, tmp_path)

        calls = [c.args[0] for c in m.call_args_list]
        assert calls[0][:3] == ["gh", "issue", "edit"]
        assert calls[1][:3] == ["gh", "issue", "close"]
        assert "closed #7" in capsys.readouterr().out

    def test_reopens_closed_issue_when_ticket_becomes_non_terminal_again(self, tmp_path, capsys):
        tickets_dir = tmp_path / "tickets"
        tickets_dir.mkdir()
        _write_ticket(tickets_dir, "TICK-1", "in_progress")
        cfg = _default_cfg(tickets_dir)
        existing = {"number": 7, "title": "[TICK-1] Do a thing", "state": "CLOSED"}

        with patch("lanegate.ghsync._gh_available", return_value=True), \
                patch("lanegate.ghsync._find_issue", return_value=existing), \
                patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as m:
            cmd_gh_sync(cfg, tmp_path)

        calls = [c.args[0] for c in m.call_args_list]
        assert calls[0][:3] == ["gh", "issue", "edit"]
        assert calls[1][:3] == ["gh", "issue", "reopen"]
        assert "reopened #7" in capsys.readouterr().out

    def test_dry_run_reports_would_close(self, tmp_path, capsys):
        tickets_dir = tmp_path / "tickets"
        tickets_dir.mkdir()
        _write_ticket(tickets_dir, "TICK-1", "merged")
        cfg = _default_cfg(tickets_dir)
        existing = {"number": 7, "title": "[TICK-1] Do a thing", "state": "OPEN"}

        with patch("lanegate.ghsync._gh_available", return_value=True), \
                patch("lanegate.ghsync._find_issue", return_value=existing), \
                patch("subprocess.run") as m:
            cmd_gh_sync(cfg, tmp_path, dry_run=True)

        m.assert_not_called()
        assert "would close #7" in capsys.readouterr().out

    def test_create_failure_prints_to_stderr(self, tmp_path, capsys):
        tickets_dir = tmp_path / "tickets"
        tickets_dir.mkdir()
        _write_ticket(tickets_dir, "TICK-1", "in_progress")
        cfg = _default_cfg(tickets_dir)

        fail_result = MagicMock(returncode=1, stdout="", stderr="permission denied")
        with patch("lanegate.ghsync._gh_available", return_value=True), \
                patch("lanegate.ghsync._find_issue", return_value=None), \
                patch("subprocess.run", return_value=fail_result):
            cmd_gh_sync(cfg, tmp_path)

        err = capsys.readouterr().err
        assert "FAILED" in err
        assert "permission denied" in err
