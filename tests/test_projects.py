"""Tests for lanegate/projects.py and board.py --global view."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lanegate.board import cmd_board
from lanegate.config import (
    CONFIG_FILENAME,
    registry_add,
    registry_load,
)
from lanegate.projects import cmd_projects_list, cmd_projects_remove, cmd_projects_scan

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_CFG = {
    "ticket_prefix": "TICK",
    "tickets_dir": "tickets",
    "lock_statuses": ["in_progress", "code_complete", "in_review"],
    "environments": [],
    "executor": "claude",
    "max_parallel": 2,
}


def _make_project(base: Path, name: str, *, make_tickets_dir: bool = True) -> Path:
    """Create a minimal lanegate project directory under base/name."""
    proj = base / name
    proj.mkdir(parents=True, exist_ok=True)
    (proj / CONFIG_FILENAME).write_text(
        "ticket_prefix: TICK\ntickets_dir: tickets\nworktrees_dir: worktrees\nexecutor: claude\nmax_parallel: 2\n"
    )
    if make_tickets_dir:
        (proj / "tickets").mkdir(exist_ok=True)
    return proj


def _make_ticket(tickets_dir: Path, ticket_id: str, status: str) -> None:
    (tickets_dir / f"{ticket_id}.md").write_text(
        f"---\nid: {ticket_id}\ntitle: Test {ticket_id}\nstatus: {status}\npriority: 5\nparallel_safe: true\n---\nBody.\n"
    )


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    """Redirect the global registry to a temp file for each test."""
    reg_path = tmp_path / "registry" / "projects.json"
    reg_path.parent.mkdir(parents=True, exist_ok=True)

    import lanegate.config_registry as _cfg_mod

    monkeypatch.setattr(_cfg_mod, "_REGISTRY_DIR", reg_path.parent)
    monkeypatch.setattr(_cfg_mod, "_REGISTRY_FILE", reg_path)
    yield reg_path


# ---------------------------------------------------------------------------
# cmd_projects_scan
# ---------------------------------------------------------------------------


def test_scan_finds_lanegate_projects(tmp_path):
    """scan registers subdirs that contain .lanegate.yml."""
    _make_project(tmp_path, "proj-a")
    _make_project(tmp_path, "proj-b")
    (tmp_path / "not-a-project").mkdir()  # no .lanegate.yml

    cmd_projects_scan(_BASE_CFG, tmp_path, [str(tmp_path)])

    entries = registry_load()
    paths = {e["path"] for e in entries}
    assert str((tmp_path / "proj-a").resolve()) in paths
    assert str((tmp_path / "proj-b").resolve()) in paths
    assert str((tmp_path / "not-a-project").resolve()) not in paths


def test_scan_does_not_walk_deeper_than_one_level(tmp_path):
    """scan must NOT descend into nested subdirs."""
    nested = tmp_path / "outer" / "inner"
    nested.mkdir(parents=True)
    (nested / CONFIG_FILENAME).write_text(
        "ticket_prefix: TICK\nexecutor: claude\nmax_parallel: 2\n"
    )

    cmd_projects_scan(_BASE_CFG, tmp_path, [str(tmp_path)])

    entries = registry_load()
    paths = {e["path"] for e in entries}
    # outer has no .lanegate.yml so it shouldn't be registered
    assert str((tmp_path / "outer").resolve()) not in paths
    # inner is two levels deep — must not be registered
    assert str(nested.resolve()) not in paths


def test_scan_is_idempotent(tmp_path, capsys):
    """Re-scanning the same directory does not create duplicate registry entries."""
    _make_project(tmp_path, "proj-a")

    cmd_projects_scan(_BASE_CFG, tmp_path, [str(tmp_path)])
    cmd_projects_scan(_BASE_CFG, tmp_path, [str(tmp_path)])

    entries = registry_load()
    paths = [e["path"] for e in entries]
    assert paths.count(str((tmp_path / "proj-a").resolve())) == 1

    out = capsys.readouterr().out
    assert "already" in out


def test_scan_invalid_dir_warns(tmp_path, capsys):
    """Non-existent directory emits a warning and doesn't crash."""
    cmd_projects_scan(_BASE_CFG, tmp_path, [str(tmp_path / "does-not-exist")])
    err = capsys.readouterr().err
    assert "WARNING" in err


# ---------------------------------------------------------------------------
# cmd_projects_list
# ---------------------------------------------------------------------------


def test_projects_list_empty(capsys):
    cmd_projects_list(_BASE_CFG, Path("."))
    out = capsys.readouterr().out
    assert "No projects registered" in out


def test_projects_list_shows_entries(tmp_path, capsys):
    _make_project(tmp_path, "alpha")
    registry_add(tmp_path / "alpha")

    cmd_projects_list(_BASE_CFG, tmp_path)
    out = capsys.readouterr().out
    assert "alpha" in out


# ---------------------------------------------------------------------------
# cmd_projects_remove
# ---------------------------------------------------------------------------


def test_projects_remove(tmp_path, capsys):
    proj = _make_project(tmp_path, "to-remove")
    registry_add(proj)
    assert any(e["path"] == str(proj.resolve()) for e in registry_load())

    cmd_projects_remove(_BASE_CFG, tmp_path, str(proj))
    assert not any(e["path"] == str(proj.resolve()) for e in registry_load())

    out = capsys.readouterr().out
    assert "Removed" in out


def test_projects_remove_not_registered(tmp_path, capsys):
    proj = _make_project(tmp_path, "ghost")
    cmd_projects_remove(_BASE_CFG, tmp_path, str(proj))
    out = capsys.readouterr().out
    assert "Not registered" in out


# ---------------------------------------------------------------------------
# cmd_board --global (text mode)
# ---------------------------------------------------------------------------


def test_board_global_aggregates_multiple_projects(tmp_path, capsys):
    proj_a = _make_project(tmp_path, "alpha")
    _make_ticket(proj_a / "tickets", "TICK-001", "open")
    registry_add(proj_a)

    proj_b = _make_project(tmp_path, "beta")
    _make_ticket(proj_b / "tickets", "TICK-002", "open")
    registry_add(proj_b)

    cmd_board(_BASE_CFG, tmp_path, global_view=True)
    out = capsys.readouterr().out

    assert "TICK-001" in out
    assert "TICK-002" in out
    assert "alpha" in out
    assert "beta" in out


def test_board_global_groups_by_project(tmp_path, capsys):
    """Each project gets its own header in text output."""
    proj_a = _make_project(tmp_path, "alpha")
    _make_ticket(proj_a / "tickets", "TICK-001", "open")
    registry_add(proj_a)

    proj_b = _make_project(tmp_path, "beta")
    _make_ticket(proj_b / "tickets", "TICK-002", "open")
    registry_add(proj_b)

    cmd_board(_BASE_CFG, tmp_path, global_view=True)
    out = capsys.readouterr().out

    assert "=== alpha" in out
    assert "=== beta" in out


def test_board_global_skips_missing_config(tmp_path, capsys):
    """Projects whose .lanegate.yml is absent are skipped with a warning."""
    proj = tmp_path / "ghost"
    proj.mkdir()
    # Register manually without creating config
    import lanegate.config_registry as _cfg_mod

    entries = registry_load()
    entries.append({"path": str(proj.resolve()), "name": "ghost"})
    _cfg_mod._registry_save(entries)

    cmd_board(_BASE_CFG, tmp_path, global_view=True)
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "ghost" in err


def test_board_global_skips_missing_tickets_dir(tmp_path, capsys):
    """Projects whose tickets dir is missing are skipped with a warning."""
    proj = _make_project(tmp_path, "no-tickets", make_tickets_dir=False)
    registry_add(proj)

    cmd_board(_BASE_CFG, tmp_path, global_view=True)
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "no-tickets" in err


# ---------------------------------------------------------------------------
# cmd_board --global (JSON mode)
# ---------------------------------------------------------------------------


def test_board_global_json_structure(tmp_path, capsys):
    proj_a = _make_project(tmp_path, "alpha")
    _make_ticket(proj_a / "tickets", "TICK-001", "open")
    registry_add(proj_a)

    proj_b = _make_project(tmp_path, "beta")
    _make_ticket(proj_b / "tickets", "TICK-002", "open")
    registry_add(proj_b)

    cmd_board(_BASE_CFG, tmp_path, json_output=True, global_view=True)
    data = json.loads(capsys.readouterr().out)

    assert isinstance(data, list)
    assert len(data) == 2

    names = {item["project"] for item in data}
    assert "alpha" in names
    assert "beta" in names

    for item in data:
        assert "project" in item
        assert "path" in item
        assert "tickets" in item
        assert isinstance(item["tickets"], list)


def test_board_global_json_ticket_ids(tmp_path, capsys):
    proj_a = _make_project(tmp_path, "alpha")
    _make_ticket(proj_a / "tickets", "TICK-001", "open")
    registry_add(proj_a)

    cmd_board(_BASE_CFG, tmp_path, json_output=True, global_view=True)
    data = json.loads(capsys.readouterr().out)

    alpha = next(item for item in data if item["project"] == "alpha")
    ticket_ids = [t["id"] for t in alpha["tickets"]]
    assert "TICK-001" in ticket_ids


def test_board_global_json_skips_missing_config(tmp_path, capsys):
    """JSON mode skips projects with missing config; result still valid JSON."""
    ghost = tmp_path / "ghost"
    ghost.mkdir()
    import lanegate.config_registry as _cfg_mod

    entries = registry_load()
    entries.append({"path": str(ghost.resolve()), "name": "ghost"})
    _cfg_mod._registry_save(entries)

    proj_a = _make_project(tmp_path, "good")
    _make_ticket(proj_a / "tickets", "TICK-001", "open")
    registry_add(proj_a)

    cmd_board(_BASE_CFG, tmp_path, json_output=True, global_view=True)
    out = capsys.readouterr().out
    data = json.loads(out)
    assert isinstance(data, list)
    names = {item["project"] for item in data}
    assert "ghost" not in names
    assert "good" in names


# ---------------------------------------------------------------------------
# Per-project board unchanged
# ---------------------------------------------------------------------------


def test_board_per_project_unchanged(tmp_path, capsys):
    """Per-project board (no --global) is unaffected by registry contents."""
    proj = _make_project(tmp_path, "myproj")
    _make_ticket(proj / "tickets", "TICK-001", "open")
    registry_add(proj)

    cfg = {
        "ticket_prefix": "TICK",
        "tickets_dir": "tickets",
        "lock_statuses": ["in_progress", "code_complete", "in_review"],
        "environments": [],
        "executor": "claude",
        "max_parallel": 2,
    }
    cmd_board(cfg, proj, global_view=False)
    out = capsys.readouterr().out
    assert "TICK-001" in out
    assert "Ticket Board" in out
    # Should NOT show project header
    assert "=== myproj" not in out
