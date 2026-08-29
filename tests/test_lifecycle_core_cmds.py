"""Tests for lifecycle operational commands."""

from __future__ import annotations

import inspect
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lanegate.concurrency import SafeguardLockHeld
from lanegate.lifecycle import (
    _commit_status,
    cmd_complete,
    cmd_fail,
    cmd_reopen,
    cmd_resolve_conflict,
    cmd_start,
)
from lanegate.ticket import parse_ticket, write_ticket
from lanegate.worktree import worktree_path
from tests._helpers.lifecycle import (
    commit_all as _commit_all,
    default_cfg as _default_cfg,
    init_git_repo as _init_git_repo,
    is_iso_utc as _is_iso_utc,
    make_git_diff_mock as _make_git_diff_mock,
    start_cfg as _start_cfg,
    tracked_path_is_clean as _tracked_path_is_clean,
    write_ticket as _write_ticket,
    write_ticket_with_body as _write_ticket_with_body,
)

def test_complete_advances_from_in_progress(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()

    wt = worktrees_dir / "tick-001"
    wt.mkdir()
    touches_yaml = "  - some_file.py"
    content = (
        f"---\n"
        f"id: TICK-001\n"
        f"title: Test TICK-001\n"
        f"status: in_progress\n"
        f"worktree: {wt}\n"
        f"touches:\n"
        f"{touches_yaml}\n"
        f"---\nBody.\n"
    )
    (tickets_dir / "TICK-001.md").write_text(content)

    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    mock_run = _make_git_diff_mock(committed_files=["some_file.py"])
    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        cmd_complete("TICK-001", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-001.md")
    assert ticket["status"] == "code_complete"


def test_complete_records_pre_complete_verified_sha_matching_head(tmp_path):
    """TICK-530: cmd_complete must persist the commit sha pre_complete safeguards
    actually ran against, so a later fix commit can be detected as stale."""
    _init_git_repo(tmp_path)
    (tmp_path / "some_file.py").write_text("before\n")
    _commit_all(tmp_path)
    subprocess.run(["git", "checkout", "-b", "tick-010"], cwd=tmp_path, check=True)
    (tmp_path / "some_file.py").write_text("implementation\n")
    _commit_all(tmp_path, "implementation")

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(
        tickets_dir,
        "TICK-010",
        "in_progress",
        worktree=str(tmp_path),
        branch="tick-010",
        touches=["some_file.py"],
    )
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    cmd_complete("TICK-010", cfg, tmp_path)

    expected_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout.strip()
    ticket = parse_ticket(tickets_dir / "TICK-010.md")
    assert ticket["pre_complete_verified_sha"] == expected_sha


def test_complete_writes_manual_implement_bundle_when_no_dispatch_record(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "some_file.py").write_text("before\n")
    _commit_all(tmp_path)
    subprocess.run(["git", "checkout", "-b", "tick-008"], cwd=tmp_path, check=True)
    (tmp_path / "some_file.py").write_text("manual change\n")
    _commit_all(tmp_path, "manual implementation")

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(
        tickets_dir,
        "TICK-008",
        "in_progress",
        worktree=str(tmp_path),
        branch="tick-008",
        touches=["some_file.py"],
    )
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    cmd_complete("TICK-008", cfg, tmp_path)

    bundle_dirs = list((tmp_path / ".lanegate" / "executor-runs" / "TICK-008").iterdir())
    assert len(bundle_dirs) == 1
    status = json.loads((bundle_dirs[0] / "status.json").read_text())
    assert status["mode"] == "manual"
    assert status["step"] == "implement"
    assert status["before_sha"]
    assert status["after_sha"]
    assert isinstance(status["elapsed_seconds"], int)
    assert status["safeguards_passed"] is True
    assert status["safeguard_reason"] is None
    assert parse_ticket(tickets_dir / "TICK-008.md")["implement_mode"] == "manual"


def test_complete_skips_manual_bundle_when_implement_bundle_exists(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "some_file.py").write_text("before\n")
    _commit_all(tmp_path)
    subprocess.run(["git", "checkout", "-b", "tick-009"], cwd=tmp_path, check=True)
    (tmp_path / "some_file.py").write_text("dispatched change\n")
    _commit_all(tmp_path, "dispatched implementation")

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(
        tickets_dir,
        "TICK-009",
        "in_progress",
        worktree=str(tmp_path),
        branch="tick-009",
        touches=["some_file.py"],
    )
    existing_bundle = tmp_path / ".lanegate" / "executor-runs" / "TICK-009" / "dispatch"
    existing_bundle.mkdir(parents=True)
    (existing_bundle / "status.json").write_text(json.dumps({"step": "implement"}))

    cmd_complete("TICK-009", _default_cfg(tickets_dir, worktrees_dir), tmp_path)

    assert list(existing_bundle.parent.iterdir()) == [existing_bundle]
    assert "implement_mode" not in parse_ticket(tickets_dir / "TICK-009.md")


def test_complete_refuses_zero_commits(tmp_path, capsys):
    """cmd_complete refuses to advance a ticket whose worktree has no real
    commits ahead of main — the TICK-030/089/159 wedge scenario, where an
    executor stalls but the ticket still gets marked code_complete."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    wt_path = worktrees_dir / "tick-002"
    wt_path.mkdir()
    _write_ticket(tickets_dir, "TICK-002", "in_progress", worktree=str(wt_path), branch="tick-002")

    mock_run = _make_git_diff_mock(committed_files=[], uncommitted_files=[])

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        with pytest.raises(SystemExit) as exc_info:
            cmd_complete("TICK-002", cfg, tmp_path)

    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "no commits ahead of main" in err

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-002.md")
    assert ticket["status"] == "in_progress"


def test_complete_refuses_missing_worktree(tmp_path, capsys):
    """F12: cmd_complete refuses to advance when the worktree directory is missing."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    wt_path = worktrees_dir / "tick-003"
    wt_path.mkdir()
    _write_ticket(tickets_dir, "TICK-003", "in_progress", worktree=str(wt_path))

    wt_path.rmdir()

    with pytest.raises(SystemExit) as exc_info:
        cmd_complete("TICK-003", cfg, tmp_path)

    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "worktree" in err and "does not exist" in err

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-003.md")
    assert ticket["status"] == "in_progress"


def test_complete_refuses_unset_worktree(tmp_path, capsys):
    """cmd_complete refuses to advance when the worktree is not set."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    _write_ticket(tickets_dir, "TICK-004", "in_progress")

    with pytest.raises(SystemExit) as exc_info:
        cmd_complete("TICK-004", cfg, tmp_path)

    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "no worktree set" in err

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-004.md")
    assert ticket["status"] == "in_progress"


def test_complete_rejects_wrong_status(tmp_path):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-001", "open")
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    with pytest.raises(SystemExit):
        cmd_complete("TICK-001", cfg, tmp_path)


def test_complete_unresolved_safeguard_preserves_status(tmp_path, capsys):
    """When a pre_complete safeguard command cannot be resolved on PATH, cmd_complete
    exits without changing status to needs_review."""
    from lanegate.lifecycle import cmd_complete

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-301"
    wt.mkdir()

    content = (
        "---\n"
        "id: TICK-301\n"
        "title: Test unresolved safeguard\n"
        "status: in_progress\n"
        f"worktree: {wt}\n"
        "touches:\n"
        '  - "*"\n'
        "---\n\n"
        "Body content\n"
    )
    (tickets_dir / "TICK-301.md").write_text(content)
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = "tickets"
    cfg["safeguards"] = {"pre_complete": ["nonexistent-command-12345 --flag"]}

    with patch("lanegate.lifecycle.core_cmds._has_committed_changes", return_value=True):
        with pytest.raises(SystemExit) as exc_info:
            cmd_complete("TICK-301", cfg, tmp_path)

    assert exc_info.value.code == 1

    ticket = parse_ticket(tickets_dir / "TICK-301.md")
    assert ticket["status"] == "in_progress"
    err = capsys.readouterr().err
    assert "cannot resolve" in err
    assert "Leaving ticket status unchanged" in err


def _write_open_ticket(tickets_dir: Path, ticket_id: str, touches=("lanegate/lifecycle.py",)):
    content = f"---\nid: {ticket_id}\ntitle: Test {ticket_id}\nstatus: open\ntouches:\n"
    for t in touches:
        content += f"  - {t}\n"
    content += "---\nBody text.\n"
    path = tickets_dir / f"{ticket_id}.md"
    path.write_text(content)
    return path


def _patch_start_externals(*, worktree_raises=False, commit_ok=True):
    """
    Return a context-manager stack that patches all external calls in cmd_start:
    - check_local_not_behind_remote: no-op
    - claim_lock: passthrough (uses actual tmp file via claim_lock fixture logic,
      but we don't need a real lock file because tests are single-threaded)
    - create_worktree: succeeds or raises RuntimeError
    - _commit_status: succeeds or fails
    """
    from contextlib import ExitStack

    stack = ExitStack()

    # Bypass fetch/divergence check
    stack.enter_context(patch("lanegate.lifecycle.core_cmds.check_local_not_behind_remote", return_value=None))

    # Bypass the flock (no .lanegate dir needed in tmp_path)
    import contextlib

    @contextlib.contextmanager
    def _noop_lock(_repo_root):
        yield

    stack.enter_context(patch("lanegate.lifecycle.core_cmds.claim_lock", side_effect=_noop_lock))

    # create_worktree: succeed or raise
    if worktree_raises:
        stack.enter_context(
            patch(
                "lanegate.lifecycle.core_cmds.create_worktree",
                side_effect=RuntimeError("git worktree add failed"),
            )
        )
    else:
        stack.enter_context(patch("lanegate.lifecycle.core_cmds.create_worktree", return_value=MagicMock()))

    # _commit_status: controlled
    stack.enter_context(patch("lanegate.lifecycle._is_git_worktree", return_value=True))
    stack.enter_context(
        patch(
            "lanegate.lifecycle._commit_status",
            return_value=commit_ok,
        )
    )

    # companion_branch_create: not relevant to these tests
    stack.enter_context(patch("lanegate.lifecycle.companion_branch_create", return_value=None))

    return stack


def test_start_worktree_failure_leaves_ticket_open(tmp_path):
    """If create_worktree raises, the ticket must NOT be committed as in_progress."""
    cfg = _start_cfg(tmp_path, commit_status_changes=True)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_open_ticket(tickets_dir, "TICK-100")

    commit_calls = []

    with _patch_start_externals(worktree_raises=True):
        # _commit_status is also patched in _patch_start_externals, but we want to
        # ensure it is truly never called, so override it here.
        with patch(
            "lanegate.lifecycle._commit_status",
            side_effect=lambda *a, **kw: commit_calls.append(a) or True,
        ):
            with pytest.raises(SystemExit) as exc_info:
                cmd_start("TICK-100", cfg, tmp_path)

    assert exc_info.value.code != 0, "cmd_start must exit non-zero on worktree failure"

    # The commit must NOT have been called — the status claim is never committed.
    assert not commit_calls, (
        f"_commit_status was called {len(commit_calls)} time(s) despite worktree failure"
    )

    # The ticket file must still be 'open'.
    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-100.md")
    assert ticket["status"] == "open", (
        f"ticket status advanced to '{ticket['status']}' despite worktree failure"
    )
    assert ticket.get("worktree") is None
    assert ticket.get("branch") is None


def test_start_worktree_failure_preserves_error_message_in_exit_code(tmp_path):
    """The real create_worktree failure text must survive into the SystemExit,
    not just a bare exit code, so callers can classify the actual cause
    instead of assuming a rate limit."""
    cfg = _start_cfg(tmp_path, commit_status_changes=True)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_open_ticket(tickets_dir, "TICK-100")

    error_message = (
        "ERROR: Existing branch 'tick-100' was preserved because it shares no "
        "history with 'main'; inspect or explicitly recover it before retrying."
    )

    with _patch_start_externals(worktree_raises=True):
        with patch(
            "lanegate.lifecycle.core_cmds.create_worktree",
            side_effect=RuntimeError(error_message),
        ):
            with pytest.raises(SystemExit) as exc_info:
                cmd_start("TICK-100", cfg, tmp_path)

    assert "shares no history with" in str(exc_info.value.code)


def test_start_success_marks_ticket_in_progress(tmp_path):
    """Successful worktree setup must leave the ticket as in_progress."""
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_open_ticket(tickets_dir, "TICK-101")

    with _patch_start_externals(worktree_raises=False):
        cmd_start("TICK-101", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-101.md")
    assert ticket["status"] == "in_progress"
    assert ticket.get("branch") == "tick-101"


def test_start_context_prompt_prints_acceptance_matrix_invariants(tmp_path, capsys):
    """The interactive '=== Context Prompt ===' block reads invariants from
    acceptance_matrix (the field the analyzer actually populates), not a
    nonexistent top-level 'invariants' key."""
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    content = (
        "---\n"
        "id: TICK-101\n"
        "title: Test TICK-101\n"
        "status: open\n"
        "touches:\n"
        "  - lanegate/lifecycle.py\n"
        "acceptance_matrix:\n"
        "  invariants:\n"
        "    - subtract(a, b) returns a - b for all numeric inputs\n"
        "---\nBody text.\n"
    )
    (tickets_dir / "TICK-101.md").write_text(content)

    with _patch_start_externals(worktree_raises=False):
        cmd_start("TICK-101", cfg, tmp_path)

    out = capsys.readouterr().out
    assert "Invariants: subtract(a, b) returns a - b for all numeric inputs" in out


def test_start_wildcard_lock_blocks_concrete_ticket(tmp_path, capsys):
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    blocker = _write_open_ticket(tickets_dir, "TICK-100", touches=('"*"',))
    blocker.write_text(blocker.read_text().replace("status: open", "status: in_progress"))
    _write_open_ticket(tickets_dir, "TICK-101", touches=("src/concrete.py",))

    with _patch_start_externals(worktree_raises=False), pytest.raises(SystemExit):
        cmd_start("TICK-101", cfg, tmp_path)

    assert "TICK-100" in capsys.readouterr().err
    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-101.md")
    assert ticket["status"] == "open"


def test_start_validates_hibernated_worktree_before_reattaching(tmp_path):
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    wt = tmp_path / "worktrees" / "tick-104"
    wt.mkdir(parents=True)
    (tickets_dir / "TICK-104.md").write_text(
        "---\n"
        "id: TICK-104\n"
        "title: Test TICK-104\n"
        "status: hibernated\n"
        "touches:\n"
        "  - lanegate/lifecycle.py\n"
        f"worktree: {wt}\n"
        "branch: tick-104\n"
        "---\nBody text.\n"
    )

    with (
        patch("lanegate.lifecycle.core_cmds.check_local_not_behind_remote", return_value=None),
        patch("lanegate.lifecycle.core_cmds.claim_lock", side_effect=_noop_lock_ctx),
        patch("lanegate.lifecycle.core_cmds.create_worktree", return_value=wt) as mock_create,
        patch("lanegate.lifecycle._commit_status", return_value=True),
        patch("lanegate.lifecycle.companion_branch_create", return_value=None),
    ):
        cmd_start("TICK-104", cfg, tmp_path)

    mock_create.assert_called_once_with(
        tmp_path, tmp_path / "worktrees", "TICK-104", "tick-104", "main", reuse_existing_branch=True
    )
    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-104.md")
    assert ticket["status"] == "in_progress"
    assert ticket["worktree"] == str(wt)


def test_start_validates_wrong_branch_at_canonical_hibernated_worktree(tmp_path):
    """A canonical path alone cannot bypass create_worktree's branch validation."""
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    wt = tmp_path / "worktrees" / "tick-106"
    wt.mkdir(parents=True)
    (tickets_dir / "TICK-106.md").write_text(
        "---\n"
        "id: TICK-106\n"
        "title: Test TICK-106\n"
        "status: hibernated\n"
        "touches:\n"
        "  - lanegate/lifecycle.py\n"
        f"worktree: {wt}\n"
        "branch: tick-106\n"
        "---\nBody text.\n"
    )

    with (
        patch("lanegate.lifecycle.core_cmds.check_local_not_behind_remote", return_value=None),
        patch("lanegate.lifecycle.core_cmds.claim_lock", side_effect=_noop_lock_ctx),
        patch("lanegate.lifecycle.core_cmds.create_worktree", return_value=wt) as mock_create,
        patch("lanegate.lifecycle._commit_status", return_value=True),
        patch("lanegate.lifecycle.companion_branch_create", return_value=None),
    ):
        cmd_start("TICK-106", cfg, tmp_path)

    # The production implementation receives this call and validates that the
    # existing checkout is actually on tick-106 and descends from main.
    mock_create.assert_called_once_with(
        tmp_path, tmp_path / "worktrees", "TICK-106", "tick-106", "main", reuse_existing_branch=True
    )


def test_start_does_not_reattach_untrusted_worktree_metadata(tmp_path):
    """A forged resume path must not make an executor operate outside worktrees_dir."""
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    unrelated = tmp_path / "unrelated-directory"
    unrelated.mkdir()
    sentinel = unrelated / "keep.txt"
    sentinel.write_text("must survive")
    canonical_wt = tmp_path / "worktrees" / "tick-105"
    (tickets_dir / "TICK-105.md").write_text(
        "---\n"
        "id: TICK-105\n"
        "title: Test TICK-105\n"
        "status: hibernated\n"
        "touches:\n"
        "  - lanegate/lifecycle.py\n"
        f"worktree: {unrelated}\n"
        "branch: tick-105\n"
        "---\nBody text.\n"
    )

    with (
        patch("lanegate.lifecycle.core_cmds.check_local_not_behind_remote", return_value=None),
        patch("lanegate.lifecycle.core_cmds.claim_lock", side_effect=_noop_lock_ctx),
        patch("lanegate.lifecycle.core_cmds.create_worktree", return_value=canonical_wt) as mock_create,
        patch("lanegate.lifecycle._commit_status", return_value=True),
        patch("lanegate.lifecycle.companion_branch_create", return_value=None),
    ):
        cmd_start("TICK-105", cfg, tmp_path)

    mock_create.assert_called_once()
    assert sentinel.read_text() == "must survive"
    ticket = parse_ticket(tickets_dir / "TICK-105.md")
    assert ticket["worktree"] == str(canonical_wt)


def test_start_success_commits_status_after_worktree(tmp_path):
    """When commit_status_changes=True, _commit_status is called only after create_worktree returns."""
    cfg = _start_cfg(tmp_path, commit_status_changes=True)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_open_ticket(tickets_dir, "TICK-102")

    call_order = []

    def recording_create_worktree(*args, **kwargs):
        call_order.append("create_worktree")
        return MagicMock()

    def recording_commit_status(*args, **kwargs):
        call_order.append("_commit_status")
        return True

    with (
        patch("lanegate.lifecycle.core_cmds.check_local_not_behind_remote", return_value=None),
        patch("lanegate.lifecycle.core_cmds.claim_lock", side_effect=_noop_lock_ctx),
        patch("lanegate.lifecycle.core_cmds.create_worktree", side_effect=recording_create_worktree),
        patch("lanegate.lifecycle._is_git_worktree", return_value=True),
        patch("lanegate.lifecycle._commit_status", side_effect=recording_commit_status),
        patch("lanegate.lifecycle.companion_branch_create", return_value=None),
    ):
        cmd_start("TICK-102", cfg, tmp_path)

    assert "create_worktree" in call_order
    assert "_commit_status" in call_order
    wt_idx = call_order.index("create_worktree")
    cs_idx = call_order.index("_commit_status")
    assert wt_idx < cs_idx, (
        f"create_worktree (pos {wt_idx}) must happen before _commit_status (pos {cs_idx})"
    )


def _start_claim_mocks():
    """Patches shared by the cross-clone push tests: everything in cmd_start's
    claim path except the real git commit/push/reset for the status change."""
    return [
        patch("lanegate.lifecycle.core_cmds.check_local_not_behind_remote", return_value=None),
        patch("lanegate.lifecycle.core_cmds.claim_lock", side_effect=_noop_lock_ctx),
        patch("lanegate.lifecycle.core_cmds.create_worktree", return_value=MagicMock()),
        patch("lanegate.lifecycle.companion_branch_create", return_value=None),
    ]


def _start_cross_clone_cfg():
    return {
        "ticket_prefix": "TICK",
        "tickets_dir": "tickets",
        "worktrees_dir": "worktrees",
        "lock_statuses": ["in_progress", "code_complete", "in_review"],
        "commit_status_changes": True,
        "environments": [],
    }


def test_start_pushes_claim_commit_and_rolls_back_on_rejection(tmp_path):
    """A claim commit that loses the race to push must not leave the ticket
    silently in_progress locally — the push rejection should roll back both
    the commit and the ticket status so the operator sees the conflict."""
    if shutil.which("git") is None:
        pytest.skip("git is required for cross-clone integration test")

    from contextlib import ExitStack

    remote_dir = tmp_path / "remote.git"
    remote_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", "-b", "main"], cwd=remote_dir, check=True, capture_output=True)

    clone1 = tmp_path / "clone1"
    subprocess.run(["git", "clone", str(remote_dir), str(clone1)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "c1@example.com"], cwd=clone1, check=True)
    subprocess.run(["git", "config", "user.name", "Clone 1"], cwd=clone1, check=True)

    (clone1 / "tickets").mkdir()
    _write_open_ticket(clone1 / "tickets", "TICK-001", touches=("a.py",))
    _write_open_ticket(clone1 / "tickets", "TICK-002", touches=("b.py",))
    subprocess.run(["git", "add", "tickets"], cwd=clone1, check=True)
    subprocess.run(["git", "commit", "-m", "initial tickets"], cwd=clone1, check=True, capture_output=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=clone1, check=True, capture_output=True)

    clone2 = tmp_path / "clone2"
    subprocess.run(["git", "clone", str(remote_dir), str(clone2)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "c2@example.com"], cwd=clone2, check=True)
    subprocess.run(["git", "config", "user.name", "Clone 2"], cwd=clone2, check=True)

    # clone1 claims TICK-001 first and pushes successfully.
    with ExitStack() as stack:
        for p in _start_claim_mocks():
            stack.enter_context(p)
        cmd_start("TICK-001", _start_cross_clone_cfg(), clone1)

    ticket1 = parse_ticket(clone1 / "tickets" / "TICK-001.md")
    assert ticket1["status"] == "in_progress"

    remote_head_after_clone1 = subprocess.run(
        ["git", "rev-parse", "main"], cwd=remote_dir, capture_output=True, text=True
    ).stdout.strip()
    assert remote_head_after_clone1  # clone1's claim reached the remote

    # clone2 is still at the old HEAD and races to claim a different ticket.
    with ExitStack() as stack:
        for p in _start_claim_mocks():
            stack.enter_context(p)
        with pytest.raises(SystemExit):
            cmd_start("TICK-002", _start_cross_clone_cfg(), clone2)

    # Rolled back: ticket stays open locally, no orphaned commit left behind.
    ticket2 = parse_ticket(clone2 / "tickets" / "TICK-002.md")
    assert ticket2["status"] == "open"

    clone2_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=clone2, capture_output=True, text=True
    ).stdout.strip()
    clone2_base = subprocess.run(
        ["git", "rev-parse", "origin/main"], cwd=clone2, capture_output=True, text=True
    ).stdout.strip()
    assert clone2_head == clone2_base, "rejected claim commit must be reset off HEAD"

    # Remote is unaffected by the losing clone's rejected push.
    remote_head_final = subprocess.run(
        ["git", "rev-parse", "main"], cwd=remote_dir, capture_output=True, text=True
    ).stdout.strip()
    assert remote_head_final == remote_head_after_clone1


def test_start_skips_push_without_tracking_remote(tmp_path):
    """No upstream configured (e.g. a solo local repo) must not attempt a push
    or roll back a perfectly good local claim commit."""
    if shutil.which("git") is None:
        pytest.skip("git is required for this integration test")

    from contextlib import ExitStack

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "solo@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Solo User"], cwd=repo, check=True)

    (repo / "tickets").mkdir()
    _write_open_ticket(repo / "tickets", "TICK-001", touches=("a.py",))
    subprocess.run(["git", "add", "tickets"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial ticket"], cwd=repo, check=True, capture_output=True)

    with ExitStack() as stack:
        for p in _start_claim_mocks():
            stack.enter_context(p)
        cmd_start("TICK-001", _start_cross_clone_cfg(), repo)

    ticket = parse_ticket(repo / "tickets" / "TICK-001.md")
    assert ticket["status"] == "in_progress"


def _noop_lock_ctx(repo_root):
    """Standalone no-op lock context manager (for use outside _patch_start_externals)."""
    import contextlib

    @contextlib.contextmanager
    def _inner(_r):
        yield

    return _inner(repo_root)


def test_start_idempotent_retry_after_failure_succeeds(tmp_path):
    """After a worktree failure leaves the ticket open, a second attempt must succeed."""
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_open_ticket(tickets_dir, "TICK-103")

    # First attempt: worktree creation fails
    with _patch_start_externals(worktree_raises=True):
        with pytest.raises(SystemExit):
            cmd_start("TICK-103", cfg, tmp_path)

    # Ticket must still be open after the failed first attempt
    from lanegate.ticket import parse_ticket

    ticket_after_fail = parse_ticket(tickets_dir / "TICK-103.md")
    assert ticket_after_fail["status"] == "open", (
        f"ticket not reset to 'open' after first failed attempt: '{ticket_after_fail['status']}'"
    )

    # Second attempt: worktree creation succeeds
    with _patch_start_externals(worktree_raises=False):
        cmd_start("TICK-103", cfg, tmp_path)

    ticket_after_success = parse_ticket(tickets_dir / "TICK-103.md")
    assert ticket_after_success["status"] == "in_progress", (
        f"retry after failure should mark ticket in_progress, got '{ticket_after_success['status']}'"
    )


def _complete_cfg(tmp_path, touches=("lanegate/lifecycle.py",)):
    """Set up a minimal cfg and in_progress ticket with a real worktree directory."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir(exist_ok=True)
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir(exist_ok=True)
    wt = worktrees_dir / "tick-210"
    wt.mkdir()

    # Quote values that are special in YAML (e.g. '*' is an alias indicator)
    def _yaml_value(v):
        if v in ("*",):
            return f'"{v}"'
        return v

    touches_yaml = "\n".join(f"  - {_yaml_value(t)}" for t in touches)
    content = (
        f"---\n"
        f"id: TICK-210\n"
        f"title: Test TICK-210\n"
        f"status: in_progress\n"
        f"worktree: {wt}\n"
        f"touches:\n"
        f"{touches_yaml}\n"
        f"---\nBody.\n"
    )
    (tickets_dir / "TICK-210.md").write_text(content)

    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)
    return cfg, tickets_dir, wt


def test_cmd_complete_blocks_on_undeclared_file(tmp_path, capsys):
    """cmd_complete exits non-zero when the diff contains undeclared files."""
    cfg, tickets_dir, wt = _complete_cfg(tmp_path, touches=["lanegate/lifecycle.py"])
    mock_run = _make_git_diff_mock(committed_files=["lanegate/lifecycle.py", "lanegate/executor.py"])

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        with pytest.raises(SystemExit) as exc_info:
            cmd_complete("TICK-210", cfg, tmp_path)
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "lanegate/executor.py" in err

    # Status must NOT have advanced
    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-210.md")
    assert ticket["status"] == "in_progress"


def test_cmd_complete_succeeds_with_declared_files_only(tmp_path):
    """cmd_complete succeeds when all changed files are declared in touches."""
    cfg, tickets_dir, wt = _complete_cfg(
        tmp_path, touches=["lanegate/lifecycle.py", "lanegate/executor.py"]
    )
    mock_run = _make_git_diff_mock(committed_files=["lanegate/lifecycle.py", "lanegate/executor.py"])

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        cmd_complete("TICK-210", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-210.md")
    assert ticket["status"] == "code_complete"


def test_cmd_complete_succeeds_with_wildcard_touches(tmp_path):
    """cmd_complete succeeds when touches: ['*'] even if unexpected files are changed."""
    cfg, tickets_dir, wt = _complete_cfg(tmp_path, touches=["*"])
    mock_run = _make_git_diff_mock(committed_files=["lanegate/lifecycle.py", "some/unrelated/file.py"])

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        cmd_complete("TICK-210", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-210.md")
    assert ticket["status"] == "code_complete"


def test_cmd_complete_allow_drift_warns_and_advances(tmp_path, capsys):
    """cmd_complete --allow-drift warns about undeclared files but still advances status."""
    cfg, tickets_dir, wt = _complete_cfg(tmp_path, touches=["lanegate/lifecycle.py"])
    mock_run = _make_git_diff_mock(committed_files=["lanegate/lifecycle.py", "lanegate/executor.py"])

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        cmd_complete("TICK-210", cfg, tmp_path, allow_drift=True)

    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "lanegate/executor.py" in err

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-210.md")
    assert ticket["status"] == "code_complete"


def test_cmd_complete_auto_update_touches_adds_undeclared_files(tmp_path, capsys):
    """--auto-update-touches auto-adds undeclared committed files to touches and advances."""
    cfg, tickets_dir, wt = _complete_cfg(tmp_path, touches=["lanegate/lifecycle.py"])
    mock_run = _make_git_diff_mock(committed_files=["lanegate/lifecycle.py", "lanegate/executor.py"])

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        cmd_complete("TICK-210", cfg, tmp_path, auto_update_touches=True)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-210.md")
    assert ticket["status"] == "code_complete"
    assert "lanegate/executor.py" in ticket["touches"]
    assert "lanegate/lifecycle.py" in ticket["touches"]

    err = capsys.readouterr().err
    assert "auto-updating touches" in err


def test_cmd_complete_auto_update_touches_noop_when_all_declared(tmp_path):
    """--auto-update-touches is a no-op when all committed files are already in touches."""
    cfg, tickets_dir, wt = _complete_cfg(tmp_path, touches=["lanegate/lifecycle.py"])
    mock_run = _make_git_diff_mock(committed_files=["lanegate/lifecycle.py"])

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        cmd_complete("TICK-210", cfg, tmp_path, auto_update_touches=True)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-210.md")
    assert ticket["status"] == "code_complete"
    assert ticket["touches"] == ["lanegate/lifecycle.py"]


def test_cmd_complete_blocks_without_auto_update_touches(tmp_path):
    """Without --auto-update-touches, undeclared files still block completion."""
    cfg, tickets_dir, wt = _complete_cfg(tmp_path, touches=["lanegate/lifecycle.py"])
    mock_run = _make_git_diff_mock(committed_files=["lanegate/lifecycle.py", "lanegate/executor.py"])

    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run), pytest.raises(SystemExit):
        cmd_complete("TICK-210", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-210.md")
    assert ticket["status"] == "in_progress"


def test_reopen_transitions_failed_to_open(tmp_path):
    """cmd_reopen transitions failed → open."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    _write_ticket(tickets_dir, "TICK-001", "failed")
    cmd_reopen("TICK-001", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-001.md")
    assert ticket["status"] == "open"


def test_reopen_strips_failure_reason(tmp_path):
    """cmd_reopen removes the ## Failure Reason section from the body."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    path = tickets_dir / "TICK-002.md"
    path.write_text(
        "---\nid: TICK-002\ntitle: T\nstatus: failed\n---\n"
        "Background.\n\n## Failure Reason\n\nexecutor exited with code 1\n"
    )
    cmd_reopen("TICK-002", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(path)
    assert ticket["status"] == "open"
    assert "Failure Reason" not in ticket.get("_body", "")


def test_cmd_fail_deletes_git_branch(tmp_path):
    """cmd_fail deletes the ticket branch unless review_verdict is changes_requested."""
    repo_root = tmp_path
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_root, check=True)
    (repo_root / "README.md").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_root, check=True)

    tickets_dir = repo_root / ".lanegate" / "tickets"
    tickets_dir.mkdir(parents=True)
    worktrees_dir = repo_root / ".lanegate" / "worktrees"
    worktrees_dir.mkdir(parents=True)
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = ".lanegate/tickets"
    cfg["worktrees_dir"] = ".lanegate/worktrees"

    _write_ticket(tickets_dir, "TICK-042", "open", touches=["README.md"])
    cmd_start("TICK-042", cfg, repo_root)

    # Verify branch tick-042 exists
    res = subprocess.run(["git", "rev-parse", "--verify", "tick-042"], cwd=repo_root, capture_output=True)
    assert res.returncode == 0

    # Now fail the ticket
    cmd_fail("TICK-042", cfg, repo_root, reason="test failure")

    # Verify branch tick-042 was deleted
    res_after = subprocess.run(["git", "rev-parse", "--verify", "tick-042"], cwd=repo_root, capture_output=True)
    assert res_after.returncode != 0

    ticket = parse_ticket(tickets_dir / "TICK-042.md")
    assert ticket["status"] == "failed"
    assert ticket.get("worktree") is None
    assert ticket.get("branch") is None


def test_cmd_fail_delete_verification_ignores_same_named_tag(tmp_path):
    """A tag sharing the ticket branch's name must not cause a false deletion-failure error.

    `git rev-parse --verify <bare-name>` is ambiguous and prefers a same-named
    tag over a branch. Post-delete verification must check `refs/heads/<branch>`
    specifically, or a leftover tag makes cmd_fail falsely report that branch
    deletion failed even though the real branch is gone.
    """
    repo_root = tmp_path
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_root, check=True)
    (repo_root / "README.md").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_root, check=True)

    tickets_dir = repo_root / ".lanegate" / "tickets"
    tickets_dir.mkdir(parents=True)
    worktrees_dir = repo_root / ".lanegate" / "worktrees"
    worktrees_dir.mkdir(parents=True)
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = ".lanegate/tickets"
    cfg["worktrees_dir"] = ".lanegate/worktrees"

    _write_ticket(tickets_dir, "TICK-043", "open", touches=["README.md"])
    cmd_start("TICK-043", cfg, repo_root)

    # An unrelated tag with the same name as the ticket branch.
    subprocess.run(["git", "tag", "tick-043"], cwd=repo_root, check=True)

    # Failing must delete the real branch and must not raise despite the tag.
    cmd_fail("TICK-043", cfg, repo_root, reason="test failure")

    res_branch = subprocess.run(
        ["git", "rev-parse", "--verify", "refs/heads/tick-043"], cwd=repo_root, capture_output=True
    )
    assert res_branch.returncode != 0
    res_tag = subprocess.run(
        ["git", "rev-parse", "--verify", "refs/tags/tick-043"], cwd=repo_root, capture_output=True
    )
    assert res_tag.returncode == 0  # tag is untouched

    ticket = parse_ticket(tickets_dir / "TICK-043.md")
    assert ticket["status"] == "failed"


def test_reopen_and_fresh_dispatch_after_failure_starts_clean(tmp_path):
    """Reopening a failed ticket deletes stale branches so fresh dispatch creates a clean branch."""
    repo_root = tmp_path
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_root, check=True)
    (repo_root / "README.md").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_root, check=True)

    tickets_dir = repo_root / ".lanegate" / "tickets"
    tickets_dir.mkdir(parents=True)
    worktrees_dir = repo_root / ".lanegate" / "worktrees"
    worktrees_dir.mkdir(parents=True)
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = ".lanegate/tickets"
    cfg["worktrees_dir"] = ".lanegate/worktrees"

    _write_ticket(tickets_dir, "TICK-043", "open", touches=["README.md"])
    cmd_start("TICK-043", cfg, repo_root)

    # Simulate bad uncommitted work made in the worktree during failed attempt
    wt_path = worktree_path(worktrees_dir, "TICK-043")
    (wt_path / "bad_file.py").write_text("# bad work")

    # Fail ticket
    cmd_fail("TICK-043", cfg, repo_root, reason="bad attempt")

    # Reopen ticket
    cmd_reopen("TICK-043", cfg, repo_root)

    ticket = parse_ticket(tickets_dir / "TICK-043.md")
    assert ticket["status"] == "open"

    # Re-dispatch / restart ticket
    cmd_start("TICK-043", cfg, repo_root)

    new_wt_path = worktree_path(worktrees_dir, "TICK-043")
    assert not (new_wt_path / "bad_file.py").exists(), "fresh dispatch must not carry stale bad commit"


def test_cmd_reopen_deletes_stale_branch_and_worktree_when_cleared_metadata(tmp_path):
    """cmd_reopen deletes stale git branches and worktrees even if ticket metadata was cleared."""
    repo_root = tmp_path
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_root, check=True)
    (repo_root / "README.md").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_root, check=True)

    tickets_dir = repo_root / ".lanegate" / "tickets"
    tickets_dir.mkdir(parents=True)
    worktrees_dir = repo_root / ".lanegate" / "worktrees"
    worktrees_dir.mkdir(parents=True)
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = ".lanegate/tickets"
    cfg["worktrees_dir"] = ".lanegate/worktrees"

    _write_ticket(tickets_dir, "TICK-044", "open", touches=["README.md"])
    cmd_start("TICK-044", cfg, repo_root)

    wt_path = worktree_path(worktrees_dir, "TICK-044")
    (wt_path / "stale_file.py").write_text("# stale")
    subprocess.run(["git", "add", "."], cwd=wt_path, check=True)
    subprocess.run(["git", "commit", "-m", "stale commit"], cwd=wt_path, check=True)

    # Manually simulate a legacy failed ticket where metadata was cleared but worktree/branch remain
    t_file = tickets_dir / "TICK-044.md"
    _write_ticket(tickets_dir, "TICK-044", "failed", touches=["README.md"], worktree=None, branch=None)

    # Verify worktree directory and branch still exist before reopen
    assert wt_path.exists()
    br_res = subprocess.run(["git", "rev-parse", "--verify", "tick-044"], cwd=repo_root, capture_output=True)
    assert br_res.returncode == 0

    # Reopen ticket — must delete the stale branch and worktree directory
    cmd_reopen("TICK-044", cfg, repo_root)

    assert not wt_path.exists()
    br_res_after = subprocess.run(["git", "rev-parse", "--verify", "tick-044"], cwd=repo_root, capture_output=True)
    assert br_res_after.returncode != 0

    ticket = parse_ticket(t_file)
    assert ticket["status"] == "open"

    # Start ticket fresh
    cmd_start("TICK-044", cfg, repo_root)

    new_wt = worktree_path(worktrees_dir, "TICK-044")
    assert not (new_wt / "stale_file.py").exists()


def test_cmd_reopen_delete_verification_ignores_same_named_tag(tmp_path):
    """A tag sharing the ticket branch's name must not cause a false deletion-failure error.

    Mirrors test_cmd_fail_delete_verification_ignores_same_named_tag but for
    the cmd_reopen branch-deletion path, which had the same bare-name
    `rev-parse --verify` ambiguity.
    """
    repo_root = tmp_path
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_root, check=True)
    (repo_root / "README.md").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_root, check=True)

    tickets_dir = repo_root / ".lanegate" / "tickets"
    tickets_dir.mkdir(parents=True)
    worktrees_dir = repo_root / ".lanegate" / "worktrees"
    worktrees_dir.mkdir(parents=True)
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = ".lanegate/tickets"
    cfg["worktrees_dir"] = ".lanegate/worktrees"

    _write_ticket(tickets_dir, "TICK-046", "open", touches=["README.md"])
    cmd_start("TICK-046", cfg, repo_root)

    t_file = tickets_dir / "TICK-046.md"
    _write_ticket(tickets_dir, "TICK-046", "failed", touches=["README.md"], worktree=None, branch=None)

    # An unrelated tag with the same name as the ticket branch.
    subprocess.run(["git", "tag", "tick-046"], cwd=repo_root, check=True)

    cmd_reopen("TICK-046", cfg, repo_root)

    res_branch = subprocess.run(
        ["git", "rev-parse", "--verify", "refs/heads/tick-046"], cwd=repo_root, capture_output=True
    )
    assert res_branch.returncode != 0
    res_tag = subprocess.run(
        ["git", "rev-parse", "--verify", "refs/tags/tick-046"], cwd=repo_root, capture_output=True
    )
    assert res_tag.returncode == 0  # tag is untouched

    ticket = parse_ticket(t_file)
    assert ticket["status"] == "open"


def test_cmd_fail_preserves_worktree_when_changes_requested(tmp_path):
    """cmd_fail preserves worktree/branch for changes_requested, and cmd_reopen deletes them."""
    repo_root = tmp_path
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_root, check=True)
    (repo_root / "README.md").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_root, check=True)

    tickets_dir = repo_root / ".lanegate" / "tickets"
    tickets_dir.mkdir(parents=True)
    worktrees_dir = repo_root / ".lanegate" / "worktrees"
    worktrees_dir.mkdir(parents=True)
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = ".lanegate/tickets"
    cfg["worktrees_dir"] = ".lanegate/worktrees"

    _write_ticket(tickets_dir, "TICK-045", "open", touches=["README.md"])
    cmd_start("TICK-045", cfg, repo_root)

    t_file = tickets_dir / "TICK-045.md"
    ticket = parse_ticket(t_file)
    ticket["review_verdict"] = "changes_requested"
    _write_ticket(tickets_dir, "TICK-045", ticket["status"], touches=ticket["touches"], worktree=ticket["worktree"], branch=ticket["branch"], review_verdict="changes_requested")

    wt_path = worktree_path(worktrees_dir, "TICK-045")
    cmd_fail("TICK-045", cfg, repo_root, reason="inspection needed")

    # Verify preserved for inspection
    assert wt_path.exists()
    br_res = subprocess.run(["git", "rev-parse", "--verify", "tick-045"], cwd=repo_root, capture_output=True)
    assert br_res.returncode == 0

    # Reopen ticket — should clean up preserved worktree and branch for fresh dispatch
    cmd_reopen("TICK-045", cfg, repo_root)
    assert not wt_path.exists()
    br_res_after = subprocess.run(["git", "rev-parse", "--verify", "tick-045"], cwd=repo_root, capture_output=True)
    assert br_res_after.returncode != 0


def test_cmd_fail_preserves_worktree_with_commits(tmp_path):
    """cmd_fail preserves worktree/branch when worktree branch has commits ahead of trunk."""
    repo_root = tmp_path
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_root, check=True)
    (repo_root / "README.md").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_root, check=True)

    tickets_dir = repo_root / ".lanegate" / "tickets"
    tickets_dir.mkdir(parents=True)
    worktrees_dir = repo_root / ".lanegate" / "worktrees"
    worktrees_dir.mkdir(parents=True)
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = ".lanegate/tickets"
    cfg["worktrees_dir"] = ".lanegate/worktrees"

    _write_ticket(tickets_dir, "TICK-045", "open", touches=["README.md"])
    cmd_start("TICK-045", cfg, repo_root)

    wt_path = worktree_path(worktrees_dir, "TICK-045")
    assert wt_path.exists()

    # Simulate a real commit in the worktree
    (wt_path / "README.md").write_text("hello world")
    subprocess.run(["git", "add", "README.md"], cwd=wt_path, check=True)
    subprocess.run(["git", "commit", "-m", "real commit"], cwd=wt_path, check=True)

    cmd_fail("TICK-045", cfg, repo_root, reason="timeout")

    # Verify preserved for inspection
    assert wt_path.exists()
    br_res = subprocess.run(["git", "rev-parse", "--verify", "tick-045"], cwd=repo_root, capture_output=True)
    assert br_res.returncode == 0


def test_fail_and_reopen_do_not_remove_untrusted_worktree_metadata(tmp_path):
    """Lifecycle cleanup only targets the canonical managed worktree path."""
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    unrelated = tmp_path / "unrelated-directory"
    unrelated.mkdir()
    sentinel = unrelated / "keep.txt"
    sentinel.write_text("must survive")

    (tickets_dir / "TICK-153.md").write_text(
        "---\n"
        "id: TICK-153\n"
        "title: metadata path safety\n"
        "status: in_progress\n"
        "touches: []\n"
        f"worktree: {unrelated}\n"
        "branch: tick-153\n"
        "---\n"
    )
    cmd_fail("TICK-153", cfg, tmp_path, reason="test")
    assert sentinel.read_text() == "must survive"

    (tickets_dir / "TICK-154.md").write_text(
        "---\n"
        "id: TICK-154\n"
        "title: metadata path safety\n"
        "status: failed\n"
        "touches: []\n"
        f"worktree: {unrelated}\n"
        "branch: tick-154\n"
        "---\n"
    )
    cmd_reopen("TICK-154", cfg, tmp_path)
    assert sentinel.read_text() == "must survive"


@pytest.mark.parametrize(
    ("command", "status"), [(cmd_fail, "in_progress"), (cmd_reopen, "failed")]
)
def test_lifecycle_preserves_registered_canonical_worktree_on_wrong_or_detached_branch(
    tmp_path, command, status
):
    """The canonical directory is not deletion authority for foreign/recovery worktrees."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_root, check=True)
    (repo_root / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo_root, check=True)

    tickets_dir = repo_root / ".lanegate" / "tickets"
    tickets_dir.mkdir(parents=True)
    worktrees_dir = repo_root / ".lanegate" / "worktrees"
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = ".lanegate/tickets"
    cfg["worktrees_dir"] = ".lanegate/worktrees"
    cfg["commit_status_changes"] = False
    _write_ticket(tickets_dir, "TICK-999", status, touches=["README.md"])

    canonical = worktree_path(worktrees_dir, "TICK-999")
    subprocess.run(
        ["git", "worktree", "add", "-b", "wrong-branch", str(canonical), "main"],
        cwd=repo_root,
        check=True,
    )
    (canonical / "keep.txt").write_text("must survive\n")
    subprocess.run(["git", "checkout", "--detach"], cwd=canonical, check=True)

    with pytest.raises(RuntimeError, match="expected branch 'tick-999'.*detached HEAD"):
        command("TICK-999", cfg, repo_root)

    assert (canonical / "keep.txt").read_text() == "must survive\n"
    assert parse_ticket(tickets_dir / "TICK-999.md")["status"] == status


def test_fail_and_reopen_do_not_remove_untrusted_branch_metadata(tmp_path):
    """Lifecycle cleanup only deletes the canonical ticket branch."""
    repo_root = tmp_path
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_root, check=True)
    (repo_root / "README.md").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_root, check=True)
    subprocess.run(["git", "branch", "unrelated-branch"], cwd=repo_root, check=True)

    tickets_dir = repo_root / ".lanegate" / "tickets"
    tickets_dir.mkdir(parents=True)
    worktrees_dir = repo_root / ".lanegate" / "worktrees"
    worktrees_dir.mkdir(parents=True)
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = ".lanegate/tickets"
    cfg["worktrees_dir"] = ".lanegate/worktrees"

    _write_ticket(
        tickets_dir,
        "TICK-155",
        "in_progress",
        touches=["README.md"],
        branch="unrelated-branch",
    )
    cmd_fail("TICK-155", cfg, repo_root, reason="test")
    assert subprocess.run(
        ["git", "rev-parse", "--verify", "unrelated-branch"],
        cwd=repo_root,
        capture_output=True,
    ).returncode == 0

    _write_ticket(
        tickets_dir,
        "TICK-156",
        "failed",
        touches=["README.md"],
        branch="unrelated-branch",
    )
    cmd_reopen("TICK-156", cfg, repo_root)
    assert subprocess.run(
        ["git", "rev-parse", "--verify", "unrelated-branch"],
        cwd=repo_root,
        capture_output=True,
    ).returncode == 0


def test_reopen_rejects_non_failed_ticket(tmp_path):
    """cmd_reopen exits with error if ticket is not in failed state."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    _write_ticket(tickets_dir, "TICK-003", "open")
    with pytest.raises(SystemExit):
        cmd_reopen("TICK-003", cfg, tmp_path)


def test_reopen_needs_review_with_no_commits_resets_to_open(tmp_path):
    """A needs_review ticket whose worktree never got any real commits (e.g.
    blocked by a pre-flight gate before an executor ran) resets to open with
    the stale empty worktree cleaned up, same as the failed case."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    wt_path = worktrees_dir / "tick-004"
    wt_path.mkdir()
    path = tickets_dir / "TICK-004.md"
    path.write_text(
        f"---\nid: TICK-004\ntitle: T\nstatus: needs_review\nworktree: {wt_path}\n"
        "branch: tick-004\n---\n"
        "Background.\n\n## Needs Review Reason\n\nacceptance-contract audit failed\n"
    )

    with patch("lanegate.reviewer.worktree_has_commits", return_value=False):
        cmd_reopen("TICK-004", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(path)
    assert ticket["status"] == "open"
    assert not ticket.get("worktree")
    assert "Needs Review Reason" not in ticket.get("_body", "")
    assert "## Status History" in ticket.get("_body", "")
    assert "needs_review → open" in ticket.get("_body", "")


def test_cmd_reopen_zero_commit_branch_resets_hibernated_ticket_to_open(tmp_path):
    """A hibernated branch with no ticket commits is safe to discard and retry."""
    _init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("init\n")
    _commit_all(tmp_path)
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt_path = worktrees_dir / "tick-005"
    subprocess.run(
        ["git", "worktree", "add", "-b", "tick-005", str(wt_path)], cwd=tmp_path, check=True
    )
    _write_ticket(
        tickets_dir,
        "TICK-005",
        "hibernated",
        worktree=str(wt_path),
        branch="tick-005",
    )
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    cmd_reopen("TICK-005", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-005.md")
    assert ticket["status"] == "open"
    assert ticket.get("worktree") is None
    assert not wt_path.exists()
    assert "hibernated → open" in ticket.get("_body", "")


def test_reopen_hibernated_branch_only_recovery_refuses_not_deletes(tmp_path):
    """cmd_hibernate --reset preserves recovery work by clearing
    ticket["worktree"] while keeping ticket["branch"] and the branch ref
    (hibernate.py's "preserving branch ... resume with `lanegate start`" path).
    reopen's has_commits guard used to compute False for this exact shape
    (worktree_has_commits required a worktree dir), so it fell through to the
    unconditional `git branch -D` cleanup and destroyed the preserved
    recovery commits -- the one thing that preserve path exists to protect.
    Regression test for finding [1]: must refuse (has_commits guard), not
    silently delete the branch."""
    if shutil.which("git") is None:
        pytest.skip("git is required for this test")

    _init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("init\n")
    _commit_all(tmp_path)
    subprocess.run(["git", "checkout", "-qb", "tick-900"], cwd=tmp_path, check=True)
    (tmp_path / "recovery.py").write_text("preserved work\n")
    _commit_all(tmp_path, "recovery work")
    subprocess.run(["git", "checkout", "-q", "main"], cwd=tmp_path, check=True)
    recovery_head = subprocess.run(
        ["git", "rev-parse", "refs/heads/tick-900"],
        cwd=tmp_path, check=True, capture_output=True, text=True,
    ).stdout.strip()

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    _write_ticket(
        tickets_dir,
        "TICK-900",
        "hibernated",
        worktree=None,
        branch="tick-900",
    )
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    with pytest.raises(SystemExit):
        cmd_reopen("TICK-900", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-900.md")
    assert ticket["status"] == "hibernated"
    assert subprocess.run(
        ["git", "rev-parse", "refs/heads/tick-900"],
        cwd=tmp_path, check=True, capture_output=True, text=True,
    ).stdout.strip() == recovery_head


def test_reopen_needs_review_with_commits_resets_to_code_complete(tmp_path):
    """A needs_review ticket whose worktree has real commits (implementation
    ran, then a post-implementation gate like a stale touches-scope check or
    a static-analysis finding downgraded it) resets to code_complete with the
    worktree/branch/commits preserved, ready for `lanegate review`. No main-branch
    drift here, so the rebase-onto-main check is a no-op."""
    if shutil.which("git") is None:
        pytest.skip("git is required for rebase-onto-main regression test")

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    wt_path = worktrees_dir / "tick-005"
    wt_path.mkdir()
    _init_git_repo(wt_path)
    (wt_path / "shared.py").write_text("line1\n")
    _commit_all(wt_path, "base")
    subprocess.run(["git", "checkout", "-b", "tick-005"], cwd=wt_path, check=True, capture_output=True)
    (wt_path / "foo.py").write_text("ticket work\n")
    _commit_all(wt_path, "ticket work")

    path = tickets_dir / "TICK-005.md"
    path.write_text(
        f"---\nid: TICK-005\ntitle: T\nstatus: needs_review\nworktree: {wt_path}\n"
        "branch: tick-005\nreview_verdict: changes_requested\nreview_summary: blocked by orchestrate gate\n"
        "review_retry_attempt: 3\nreview_retry_after: '2026-08-01T00:00:00Z'\n"
        "---\n"
        "Background.\n\n## Needs Review Reason\n\ncommitted files outside touches list: foo.py\n"
    )

    cmd_reopen("TICK-005", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(path)
    assert ticket["status"] == "code_complete"
    assert ticket.get("worktree") == str(wt_path)
    assert ticket.get("branch") == "tick-005"
    assert not ticket.get("review_verdict")
    assert not ticket.get("review_summary")
    # A reviewer-cooldown retry budget exhausted in an earlier incident
    # (TICK-517) must not survive a reopen and immediately re-exhaust on the
    # ticket's very next unrelated cooldown.
    assert "review_retry_attempt" not in ticket
    assert "review_retry_after" not in ticket
    assert "Needs Review Reason" not in ticket.get("_body", "")
    assert "## Status History" in ticket.get("_body", "")
    assert "needs_review → code_complete" in ticket.get("_body", "")


def test_reopen_clears_review_pending_so_next_run_does_not_rehibernate(tmp_path):
    """A ticket carrying review_pending: true (hibernated once with no review
    verdict, then unblocked) must have that marker cleared on reopen — otherwise
    the next `lanegate run` pass re-hibernates it for the same reason (TICK-675
    bug 1)."""
    if shutil.which("git") is None:
        pytest.skip("git is required")

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    wt_path = worktrees_dir / "tick-006"
    wt_path.mkdir()
    _init_git_repo(wt_path)
    (wt_path / "shared.py").write_text("line1\n")
    _commit_all(wt_path, "base")
    subprocess.run(["git", "checkout", "-b", "tick-006"], cwd=wt_path, check=True, capture_output=True)
    (wt_path / "foo.py").write_text("ticket work\n")
    _commit_all(wt_path, "ticket work")

    path = tickets_dir / "TICK-006.md"
    path.write_text(
        f"---\nid: TICK-006\ntitle: T\nstatus: needs_review\nworktree: {wt_path}\n"
        "branch: tick-006\nreview_verdict: changes_requested\nreview_summary: gate block\n"
        "review_pending: true\n"
        "review_pending_reason: 'orphaned prior session: code_complete with no review verdict'\n"
        "---\n"
        "Background.\n\n## Needs Review Reason\n\ncommitted files outside touches list: foo.py\n"
    )

    cmd_reopen("TICK-006", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(path)
    assert ticket["status"] == "code_complete"
    assert "review_pending" not in ticket
    assert "review_pending_reason" not in ticket


def test_reopen_failed_with_commits_resets_to_code_complete(tmp_path):
    """A failed ticket whose worktree/branch were preserved by cmd_fail
    (review_verdict=changes_requested, so cmd_fail skips its usual delete)
    must not be silently discarded by reopen just because `current == "failed"`
    fell outside the has_commits check. It restores to code_complete with the
    worktree/branch/commits intact, same as the needs_review-with-commits case
    -- `failed` has no `lanegate start` recovery path, so refusing outright
    would strand it with no way forward. Regression test for finding [2]."""
    if shutil.which("git") is None:
        pytest.skip("git is required for this test")

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    wt_path = worktrees_dir / "tick-217"
    wt_path.mkdir()
    _init_git_repo(wt_path)
    (wt_path / "shared.py").write_text("line1\n")
    _commit_all(wt_path, "base")
    subprocess.run(["git", "checkout", "-b", "tick-217"], cwd=wt_path, check=True, capture_output=True)
    (wt_path / "foo.py").write_text("ticket work\n")
    _commit_all(wt_path, "ticket work")

    path = tickets_dir / "TICK-217.md"
    path.write_text(
        f"---\nid: TICK-217\ntitle: T\nstatus: failed\nworktree: {wt_path}\n"
        "branch: tick-217\nreview_verdict: changes_requested\nreview_summary: blocking finding\n"
        "---\n"
        "Background.\n\n## Failure Reason\n\nauto-fix attempts exhausted\n"
    )

    cmd_reopen("TICK-217", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(path)
    assert ticket["status"] == "code_complete"
    assert ticket.get("worktree") == str(wt_path)
    assert ticket.get("branch") == "tick-217"
    assert not ticket.get("review_verdict")
    assert not ticket.get("review_summary")
    assert "Failure Reason" not in ticket.get("_body", "")
    assert "failed → code_complete" in ticket.get("_body", "")
    assert (wt_path / "foo.py").read_text() == "ticket work\n"


def test_reopen_needs_review_with_conflicting_main_preserves_branch_without_dispatch(tmp_path):
    """reopen changes lifecycle state only, even when a rebase would conflict."""
    if shutil.which("git") is None:
        pytest.skip("git is required for rebase-onto-main regression test")

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    wt_path = worktrees_dir / "tick-900"
    wt_path.mkdir()
    _init_git_repo(wt_path)
    (wt_path / "shared.py").write_text("line1\n")
    _commit_all(wt_path, "base")

    subprocess.run(["git", "checkout", "-b", "tick-900"], cwd=wt_path, check=True, capture_output=True)
    (wt_path / "shared.py").write_text("line1\nticket change\n")
    _commit_all(wt_path, "ticket work")

    subprocess.run(["git", "checkout", "main"], cwd=wt_path, check=True, capture_output=True)
    (wt_path / "shared.py").write_text("line1\nmain change\n")
    _commit_all(wt_path, "main drifted")

    subprocess.run(["git", "checkout", "tick-900"], cwd=wt_path, check=True, capture_output=True)

    path = tickets_dir / "TICK-900.md"
    path.write_text(
        f"---\nid: TICK-900\ntitle: T\nstatus: needs_review\nworktree: {wt_path}\n"
        "branch: tick-900\n---\n"
        "Background.\n\n## Needs Review Reason\n\nsome prior gate\n"
    )

    cmd_reopen("TICK-900", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(path)
    assert ticket["status"] == "code_complete"
    assert "no rebase or agent dispatch" in ticket.get("_body", "")

    status = subprocess.run(
        ["git", "status"], cwd=wt_path, capture_output=True, text=True
    )
    assert "rebase in progress" not in status.stdout, "conflicting rebase must be aborted, not left mid-rebase"


def test_reopen_needs_review_with_stale_branch_does_not_rebase_worktree(tmp_path):
    """reopen must not silently mutate a real-commit worktree."""
    if shutil.which("git") is None:
        pytest.skip("git is required for rebase-onto-main regression test")

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    wt_path = worktrees_dir / "tick-901"
    wt_path.mkdir()
    _init_git_repo(wt_path)
    (wt_path / "shared.py").write_text("line1\n")
    _commit_all(wt_path, "base")

    subprocess.run(["git", "checkout", "-b", "tick-901"], cwd=wt_path, check=True, capture_output=True)
    (wt_path / "ticket_only.py").write_text("ticket work\n")
    _commit_all(wt_path, "ticket work")

    subprocess.run(["git", "checkout", "main"], cwd=wt_path, check=True, capture_output=True)
    (wt_path / "main_only.py").write_text("intervening main commit\n")
    _commit_all(wt_path, "main drifted")

    subprocess.run(["git", "checkout", "tick-901"], cwd=wt_path, check=True, capture_output=True)

    path = tickets_dir / "TICK-901.md"
    path.write_text(
        f"---\nid: TICK-901\ntitle: T\nstatus: needs_review\nworktree: {wt_path}\n"
        "branch: tick-901\n---\n"
        "Background.\n\n## Needs Review Reason\n\nsome prior gate\n"
    )

    cmd_reopen("TICK-901", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(path)
    assert ticket["status"] == "code_complete"
    assert ticket.get("worktree") == str(wt_path)

    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=wt_path, capture_output=True, text=True
    ).stdout
    assert "main drifted" not in log, "reopen must not rebase the worktree"
    assert not (wt_path / "main_only.py").exists(), "reopen must preserve the branch as-is"


def test_reopen_from_code_complete(tmp_path):
    """A code_complete ticket whose worktree has zero real commits (the
    cmd_complete guard's own bug scenario, wedged before the guard existed)
    recovers to open with the stale empty worktree cleaned up."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    wt_path = worktrees_dir / "tick-006"
    wt_path.mkdir()
    path = tickets_dir / "TICK-006.md"
    path.write_text(
        f"---\nid: TICK-006\ntitle: T\nstatus: code_complete\nworktree: {wt_path}\n"
        "branch: tick-006\n---\n"
        "Background.\n"
    )

    with patch("lanegate.reviewer.worktree_has_commits", return_value=False):
        cmd_reopen("TICK-006", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(path)
    assert ticket["status"] == "open"
    assert not ticket.get("worktree")
    assert "## Status History" in ticket.get("_body", "")
    assert "code_complete → open" in ticket.get("_body", "")


def test_reopen_from_code_complete_with_commits_refuses(tmp_path):
    """A code_complete ticket with real commits is healthy, not wedged —
    reopen must refuse rather than discard legitimate work."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    wt_path = worktrees_dir / "tick-007"
    wt_path.mkdir()
    path = tickets_dir / "TICK-007.md"
    path.write_text(
        f"---\nid: TICK-007\ntitle: T\nstatus: code_complete\nworktree: {wt_path}\n"
        "branch: tick-007\n---\n"
        "Background.\n"
    )

    with patch("lanegate.reviewer.worktree_has_commits", return_value=True):
        with pytest.raises(SystemExit) as exc_info:
            cmd_reopen("TICK-007", cfg, tmp_path)

    assert exc_info.value.code != 0

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(path)
    assert ticket["status"] == "code_complete"
    assert ticket.get("worktree") == str(wt_path)


def test_start_writes_status_changed_at(tmp_path):
    """cmd_start must write status_changed_at when transitioning open → in_progress."""
    cfg = _start_cfg(tmp_path, commit_status_changes=False)
    tickets_dir = Path(cfg["tickets_dir"])
    _write_open_ticket(tickets_dir, "TICK-200")

    with _patch_start_externals(worktree_raises=False):
        cmd_start("TICK-200", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-200.md")
    assert ticket["status"] == "in_progress"
    assert _is_iso_utc(ticket.get("status_changed_at")), (
        f"status_changed_at not set or wrong format: {ticket.get('status_changed_at')!r}"
    )


def test_reopen_writes_status_changed_at(tmp_path):
    """cmd_reopen must write status_changed_at when transitioning failed → open."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    _write_ticket(tickets_dir, "TICK-203", "failed")
    cmd_reopen("TICK-203", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-203.md")
    assert ticket["status"] == "open"
    assert _is_iso_utc(ticket.get("status_changed_at")), (
        f"status_changed_at not set on reopen: {ticket.get('status_changed_at')!r}"
    )


def test_cmd_complete_records_verification_from_acceptance_checklist(tmp_path):
    """cmd_complete populates ticket['verification'] with one record per
    Acceptance Criteria checklist item, without blocking (verdict is None
    at complete time -- the gate only blocks an approved review verdict)."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-300"
    wt.mkdir()
    body = "## Acceptance Criteria\n- [ ] Add a widget function\n"
    _write_ticket_with_body(
        tickets_dir, "TICK-300", "in_progress", body, worktree=str(wt), touches=["lanegate/foo.py"]
    )
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    mock_run = _make_git_diff_mock(committed_files=["lanegate/foo.py"])
    with patch("lanegate.lifecycle.subprocess.run", side_effect=mock_run):
        with patch(
            "lanegate.acceptance_contract._worktree_diff_text",
            return_value="def add_widget_function(): pass",
        ):
            cmd_complete("TICK-300", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-300.md")
    assert ticket["status"] == "code_complete"
    assert ticket["verification"] == [
        {
            "criterion": "Add a widget function",
            "status": "verified",
            "evidence": "3/3 terms matched in diff: add, widget, function",
            "checked_at": None,
        }
    ]


def test_cmd_reopen_blocks_when_ticket_superseded(tmp_path, capsys):
    """cmd_reopen must not re-dispatch a ticket whose work already exists
    on main -- it should point at `lanegate supersede` instead."""
    _init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "-b", "tick-502"], cwd=tmp_path, check=True)
    (tmp_path / "already_landed2.txt").write_text("already landed\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "already landed"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "merge", "--no-ff", "tick-502", "-m", "merge tick-502"],
        cwd=tmp_path,
        check=True,
    )

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()

    _write_ticket(
        tickets_dir,
        "TICK-502",
        "needs_review",
        branch="tick-502",
        touches=["already_landed2.txt"],
    )
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    with pytest.raises(SystemExit) as exc_info:
        cmd_reopen("TICK-502", cfg, tmp_path)

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "superseded" in err
    assert "lanegate supersede TICK-502" in err

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-502.md")
    # Blocked: must not have advanced to open
    assert ticket["status"] == "needs_review"


def test_cmd_reopen_proceeds_normally_when_not_superseded(tmp_path):
    """Sanity check: the new reconciliation guard doesn't interfere with
    the ordinary failed -> open reopen path when there's no evidence of
    supersession."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()

    _write_ticket(tickets_dir, "TICK-503", "failed", touches=["lanegate/novel.py"])
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["tickets_dir"] = str(tickets_dir)
    cfg["worktrees_dir"] = str(worktrees_dir)

    cmd_reopen("TICK-503", cfg, tmp_path)

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-503.md")
    assert ticket["status"] == "open"


def test_reopen_with_commits_restores_status_without_rebasing_or_dispatching(tmp_path):
    """reopen is a lifecycle operation, never an implicit work-execution flow."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-322"
    wt.mkdir()

    _write_ticket(tickets_dir, "TICK-322", "needs_review", worktree=str(wt))
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    with patch("lanegate.lifecycle.core_cmds._worktree_has_commits", return_value=True), \
         patch("lanegate.orchestrate._run_rebase") as mock_rebase, \
         patch("lanegate.orchestrate.autofix.run_rebase_fix_agent") as mock_fix:
        cmd_reopen("TICK-322", cfg, tmp_path)

    mock_rebase.assert_not_called()
    mock_fix.assert_not_called()
    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-322.md")
    assert ticket["status"] == "code_complete"


def test_resolve_conflict_routes_fix_agent_through_explicit_pool(tmp_path):
    """Conflict resolution is explicit and its agent obeys the selected pool."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-323"
    wt.mkdir()

    _write_ticket(tickets_dir, "TICK-323", "needs_review", worktree=str(wt))
    cfg = _default_cfg(tickets_dir, worktrees_dir)
    cfg["pools"] = {"codex": {"executors": ["codex"]}}

    with patch("lanegate.lifecycle.core_cmds._worktree_has_commits", return_value=True), \
         patch("lanegate.orchestrate._worktree_is_dirty", return_value=False), \
         patch("lanegate.orchestrate._run_rebase", return_value=("conflict", "detail")), \
         patch("lanegate.orchestrate.autofix.run_rebase_fix_agent", return_value=True) as mock_fix:
        cmd_resolve_conflict("TICK-323", cfg, tmp_path, pool_name="codex")

    assert mock_fix.call_args.kwargs["pool_name"] == "codex"
    from lanegate.ticket import parse_ticket

    assert parse_ticket(tickets_dir / "TICK-323.md")["status"] == "code_complete"


def test_resolve_conflict_sequential_metadata_then_code(tmp_path):
    """Verify cmd_resolve_conflict handles sequential conflicts and runs post-rebase verification once."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-534"
    wt.mkdir()

    _write_ticket(tickets_dir, "TICK-534", "needs_review", worktree=str(wt))
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    with patch("lanegate.lifecycle.core_cmds._worktree_has_commits", return_value=True), \
         patch("lanegate.orchestrate._worktree_is_dirty", return_value=False), \
         patch("lanegate.orchestrate._run_rebase", return_value=("conflict", "detail")), \
         patch("lanegate.orchestrate.autofix.run_rebase_fix_agent", return_value=True) as mock_fix, \
         patch("lanegate.lifecycle.core_cmds.run_safeguards", return_value=(True, "")) as mock_sg:
        cmd_resolve_conflict("TICK-534", cfg, tmp_path)

    assert mock_fix.called
    assert mock_sg.call_count == 1
    from lanegate.ticket import parse_ticket
    t = parse_ticket(tickets_dir / "TICK-534.md")
    assert t["status"] == "code_complete"
    assert "<<<<<<<" not in t.get("_body", "")


def test_resolve_conflict_clean_rebase(tmp_path):
    """Verify clean rebase in cmd_resolve_conflict runs verification once and transitions to code_complete."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-534"
    wt.mkdir()

    _write_ticket(tickets_dir, "TICK-534", "needs_review", worktree=str(wt))
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    with patch("lanegate.lifecycle.core_cmds._worktree_has_commits", return_value=True), \
         patch("lanegate.orchestrate._worktree_is_dirty", return_value=False), \
         patch("lanegate.orchestrate._run_rebase", return_value=("clean", "")), \
         patch("lanegate.orchestrate.autofix.run_rebase_fix_agent") as mock_fix, \
         patch("lanegate.orchestrate.review._git_head_sha", return_value="rebased-sha"), \
         patch("lanegate.lifecycle.core_cmds.run_safeguards", return_value=(True, "")) as mock_sg:
        cmd_resolve_conflict("TICK-534", cfg, tmp_path)

    mock_fix.assert_not_called()
    assert mock_sg.call_count == 1
    from lanegate.ticket import parse_ticket
    ticket = parse_ticket(tickets_dir / "TICK-534.md")
    assert ticket["status"] == "code_complete"
    assert ticket["pre_complete_verified_sha"] == "rebased-sha"


def test_resolve_conflict_exits_when_post_rebase_safeguard_is_already_running(tmp_path, capsys):
    """Do not race an existing complete/merge safeguard for the same ticket."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    wt = worktrees_dir / "tick-534"
    wt.mkdir()

    _write_ticket(tickets_dir, "TICK-534", "needs_review", worktree=str(wt))
    cfg = _default_cfg(tickets_dir, worktrees_dir)

    with patch("lanegate.lifecycle.core_cmds._worktree_has_commits", return_value=True), \
         patch("lanegate.orchestrate._worktree_is_dirty", return_value=False), \
         patch("lanegate.orchestrate._run_rebase", return_value=("clean", "")), \
         patch("lanegate.lifecycle.core_cmds.safeguard_lock", side_effect=SafeguardLockHeld("TICK-534: safeguards busy")), \
         patch("lanegate.lifecycle.core_cmds.run_safeguards") as mock_safeguards, \
         pytest.raises(SystemExit):
        cmd_resolve_conflict("TICK-534", cfg, tmp_path)

    mock_safeguards.assert_not_called()
    assert "TICK-534: safeguards busy" in capsys.readouterr().err
