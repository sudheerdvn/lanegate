"""Tests for pending globals checking, notifications, and CLI integration."""

from pathlib import Path
from lanegate.pending_globals import check_pending_globals, format_pending_globals_notice, get_pending_globals_path


def test_check_pending_globals_empty_when_file_missing(tmp_path: Path):
    info = check_pending_globals(tmp_path)
    assert not info["has_pending"]
    assert info["count"] == 0


def test_check_pending_globals_detects_proposals(tmp_path: Path):
    notes_dir = tmp_path / ".lanegate"
    notes_dir.mkdir(parents=True)
    pg_file = notes_dir / "pending-globals.md"
    pg_file.write_text(
        "## [TICK-001] Rule 1\n- Proposal: A\n\n## [TICK-002] Rule 2\n- Proposal: B\n",
        encoding="utf-8",
    )

    info = check_pending_globals(tmp_path)
    assert info["has_pending"]
    assert info["count"] == 2
    assert "Rule 1" in info["text"]

    notice = format_pending_globals_notice(info)
    assert "2 pending global proposals" in notice
    assert ".lanegate/pending-globals.md" in notice
