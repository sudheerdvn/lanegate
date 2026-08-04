from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from lanegate.analyze import cmd_analyze
from lanegate.lifecycle import (
    cmd_complete,
    cmd_done,
    cmd_merge,
    cmd_open,
    cmd_review,
    cmd_start,
    cmd_validate,
)
from lanegate.config import CONFIG_FILENAME, load_config
from lanegate.ticket import get_ticket_diff, parse_ticket


TICKET_ID = "TICK-141"


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {cwd}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def _write_draft_ticket(repo, ticket_id: str = TICKET_ID) -> Path:
    path = repo.tickets_dir / f"{ticket_id}.md"
    path.write_text(
        "\n".join(
            [
                "---",
                f"id: {ticket_id}",
                "title: Exercise real lifecycle",
                "status: draft",
                "---",
                "Update the application value.",
                "",
            ]
        )
    )
    repo.commit_all(f"add draft {ticket_id}")
    return path


def _analysis_response() -> str:
    return json.dumps(
        {
            "touches": ["src/app.py"],
            "close_criteria": "src/app.py exposes the implemented lifecycle value.",
            "depends_on": [],
        }
    )


def _ticket(path: Path) -> dict:
    parsed = parse_ticket(path)
    assert parsed is not None
    return parsed


def _assert_status(path: Path, status: str) -> dict:
    ticket = _ticket(path)
    assert ticket["status"] == status
    return ticket


def _worktree_paths(repo) -> list[str]:
    result = repo.git("worktree", "list", "--porcelain")
    return [
        line.removeprefix("worktree ")
        for line in result.stdout.splitlines()
        if line.startswith("worktree ")
    ]


def test_ticket_lifecycle_uses_real_git_worktree_and_merge(real_lanegate_repo):
    repo = real_lanegate_repo
    ticket_path = _write_draft_ticket(repo)

    cmd_analyze(
        TICKET_ID,
        repo.cfg,
        repo.root,
        model_fn=lambda _prompt: _analysis_response(),
        keep_draft=True,
    )
    analyzed = _assert_status(ticket_path, "draft")
    assert analyzed["touches"] == ["src/app.py"]
    assert analyzed["close_criteria"] == "src/app.py exposes the implemented lifecycle value."

    cmd_open(TICKET_ID, repo.cfg, repo.root)
    _assert_status(ticket_path, "open")

    cmd_start(TICKET_ID, repo.cfg, repo.root, interactive=False)
    started = _assert_status(ticket_path, "in_progress")
    branch = started["branch"]
    worktree = Path(started["worktree"])
    assert branch == "tick-141"
    assert worktree.is_dir()
    # git worktree list --porcelain always uses forward slashes, even on
    # Windows, regardless of the platform's native path separator.
    assert worktree.as_posix() in _worktree_paths(repo)
    assert _git(worktree, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == branch
    assert repo.git("rev-parse", "--verify", branch).returncode == 0

    (worktree / "src" / "app.py").write_text('VALUE = "implemented"\n')
    _git(worktree, "add", "src/app.py")
    _git(worktree, "commit", "-m", "implement ticket")
    assert repo.git("rev-list", "--count", f"main..{branch}").stdout.strip() == "1"

    cmd_complete(TICKET_ID, repo.cfg, repo.root)
    _assert_status(ticket_path, "code_complete")
    assert worktree.is_dir()

    cmd_review(
        TICKET_ID,
        repo.cfg,
        repo.root,
        verdict="approved",
        summary="integration approved",
    )
    reviewed = _assert_status(ticket_path, "in_review")
    assert reviewed["review_verdict"] == "approved"

    cmd_merge(TICKET_ID, repo.cfg, repo.root)
    merged = _assert_status(ticket_path, "merged")
    assert merged.get("worktree") is None
    assert (repo.root / "src" / "app.py").read_text() == 'VALUE = "implemented"\n'
    assert not worktree.exists()
    assert str(worktree) not in _worktree_paths(repo)
    assert repo.git("merge-base", "--is-ancestor", branch, "main").returncode == 0

    cmd_validate(TICKET_ID, repo.cfg, repo.root)
    _assert_status(ticket_path, "validated")

    cmd_done(TICKET_ID, repo.cfg, repo.root)
    _assert_status(ticket_path, "done")


def test_ticket_lifecycle_uses_configured_master_for_worktree_diff_and_merge(real_lanegate_repo):
    repo = real_lanegate_repo
    repo.git("branch", "-m", "master")
    config_path = repo.root / CONFIG_FILENAME
    config_path.write_text(config_path.read_text() + "trunk_branch: master\n")
    repo.commit_all("configure master as trunk")
    cfg = load_config(repo.root)
    assert cfg["trunk_branch"] == "master"

    ticket_path = _write_draft_ticket(repo, "TICK-143")
    cmd_analyze(
        "TICK-143",
        cfg,
        repo.root,
        model_fn=lambda _prompt: _analysis_response(),
        keep_draft=True,
    )
    cmd_open("TICK-143", cfg, repo.root)
    master_before_start = repo.git("rev-parse", "master").stdout.strip()
    cmd_start("TICK-143", cfg, repo.root, interactive=False)

    started = _assert_status(ticket_path, "in_progress")
    branch = started["branch"]
    worktree = Path(started["worktree"])
    assert repo.git("merge-base", "master", branch).stdout.strip() == master_before_start

    (worktree / "src" / "app.py").write_text('VALUE = "master trunk"\n')
    _git(worktree, "add", "src/app.py")
    _git(worktree, "commit", "-m", "implement against master")

    diff = get_ticket_diff("TICK-143", repo.root)
    assert diff["base"] == "master"
    assert "src/app.py" in [entry["path"] for entry in diff["files"]]

    cmd_complete("TICK-143", cfg, repo.root)
    cmd_review("TICK-143", cfg, repo.root, verdict="approved", summary="approved")
    cmd_merge("TICK-143", cfg, repo.root)

    assert repo.git("merge-base", "--is-ancestor", branch, "master").returncode == 0
    assert repo.git("rev-parse", "--verify", "main", check=False).returncode != 0


def test_pre_complete_guard_failure_blocks_real_worktree_transition(real_lanegate_repo):
    repo = real_lanegate_repo
    ticket_path = _write_draft_ticket(repo, "TICK-142")
    guard_path = repo.root / "scripts" / "fail_complete.py"
    guard_path.parent.mkdir()
    guard_path.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import sys",
                'Path("guard-ran.txt").write_text(Path.cwd().name)',
                "sys.exit(1)",
                "",
            ]
        )
    )
    repo.commit_all("add failing guard")

    cfg = dict(repo.cfg)
    cfg["safeguards"] = {
        "pre_complete": [f"{shlex.quote(sys.executable)} scripts/fail_complete.py"]
    }

    cmd_analyze(
        "TICK-142",
        cfg,
        repo.root,
        model_fn=lambda _prompt: _analysis_response(),
        keep_draft=True,
    )
    cmd_open("TICK-142", cfg, repo.root)
    cmd_start("TICK-142", cfg, repo.root, interactive=False)
    started = _assert_status(ticket_path, "in_progress")
    worktree = Path(started["worktree"])
    assert worktree.is_dir()

    cmd_complete("TICK-142", cfg, repo.root)

    # Pre-complete safeguard failure routes to needs_review
    assert (worktree / "guard-ran.txt").read_text() == worktree.name
    needs_review = _assert_status(ticket_path, "needs_review")
    assert needs_review["worktree"] == str(worktree)
    assert worktree.is_dir()


def _write_open_ticket(repo, ticket_id: str, title: str, touches: list[str]) -> Path:
    lines = ["---", f"id: {ticket_id}", f"title: {title}", "status: open", "touches:"]
    lines += [f"  - {t}" for t in touches]
    lines += ["---", "Body.", ""]
    path = repo.tickets_dir / f"{ticket_id}.md"
    path.write_text("\n".join(lines))
    repo.commit_all(f"add {ticket_id}")
    return path


def test_post_merge_verify_catches_combination_break_and_routes_to_needs_review(
    real_lanegate_repo, capsys
):
    """TICK-251: `pre_merge` guards only ever proved a ticket's branch passes
    on top of whatever `main` looked like when the worktree was created — not
    that the actual resulting merge commit still passes once combined with
    everything else already on `main`. Reproduce the exact TICK-089/TICK-196
    incident: ticket A adds a new kwarg to a shared function and updates the
    only call site it knows about; ticket B (branched from the same base
    commit) adds a new call site using the old calling convention. Each is
    correct in its own isolated worktree. Merged back-to-back, the second
    merge's result is broken even though both individual pre_merge runs
    passed."""
    repo = real_lanegate_repo

    guard_script = repo.root / "scripts" / "combo_check.py"
    guard_script.parent.mkdir()
    guard_script.write_text(
        "\n".join(
            [
                "import sys",
                "from pathlib import Path",
                "lib = Path('src/lib.py').read_text()",
                "caller_exists = Path('src/caller.py').exists()",
                "if caller_exists and 'extra=None' in lib:",
                "    sys.exit(1)",
                "sys.exit(0)",
                "",
            ]
        )
    )
    (repo.root / "src" / "lib.py").write_text("def run(cb):\n    return cb()\n")
    repo.commit_all("add src/lib.py and combo_check guard")

    cfg = dict(repo.cfg)
    cfg["safeguards"] = {
        "pre_merge": [f"{shlex.quote(sys.executable)} scripts/combo_check.py"]
    }

    # Both tickets branch from the same base commit, before either lands.
    a_path = _write_open_ticket(repo, "TICK-201", "pool-support kwarg", ["src/lib.py"])
    b_path = _write_open_ticket(
        repo, "TICK-202", "new caller", ["src/caller.py", "tests/test_caller.py"]
    )

    cmd_start("TICK-201", cfg, repo.root, interactive=False)
    wt_a = Path(_ticket(a_path)["worktree"])
    (wt_a / "src" / "lib.py").write_text("def run(cb, extra=None):\n    return cb(extra)\n")
    _git(wt_a, "add", "src/lib.py")
    _git(wt_a, "commit", "-m", "TICK-201: add pool-support kwarg to run()")

    cmd_start("TICK-202", cfg, repo.root, interactive=False)
    wt_b = Path(_ticket(b_path)["worktree"])
    (wt_b / "src" / "caller.py").write_text(
        "from src.lib import run\n\n\ndef caller():\n    return run(lambda: 42)\n"
    )
    (wt_b / "tests").mkdir(exist_ok=True)
    (wt_b / "tests" / "test_caller.py").write_text(
        "from src.caller import caller\n\n\ndef test_caller():\n    assert caller() == 42\n"
    )
    _git(wt_b, "add", "src/caller.py", "tests/test_caller.py")
    _git(wt_b, "commit", "-m", "TICK-202: add caller() using old run() convention")

    for tid, path in (("TICK-201", a_path), ("TICK-202", b_path)):
        cmd_complete(tid, cfg, repo.root)
        cmd_review(tid, cfg, repo.root, verdict="approved", summary=f"{tid} approved")

    # Ticket A merges cleanly: main doesn't have src/caller.py yet, so the
    # combo guard passes both in A's own worktree and against merged main.
    cmd_merge("TICK-201", cfg, repo.root)
    _assert_status(a_path, "merged")

    pre_b_merge_head = repo.git("rev-parse", "HEAD").stdout.strip()

    # Ticket B's own worktree still has the ORIGINAL run() (it never touched
    # src/lib.py), so its pre_merge guard run passes there too. But main
    # already has A's updated run() — the merge result breaks the combo guard.
    from lanegate.lifecycle import MergeFailedError

    with pytest.raises(MergeFailedError):
        cmd_merge("TICK-202", cfg, repo.root)

    b_ticket = _ticket(b_path)
    assert b_ticket["status"] == "needs_review", (
        f"post-merge verify failure must route the ticket to needs_review, got {b_ticket['status']!r}"
    )
    assert "## Needs Review Reason" in b_ticket["_body"]

    # main was reset back to its pre-merge commit (plus the needs_review status
    # write on top): B's merge commit must not stand, and its files must be gone.
    assert (
        repo.git("merge-base", "--is-ancestor", pre_b_merge_head, "HEAD").returncode == 0
    ), "pre-merge commit is no longer an ancestor of HEAD — reset --hard did not run as expected"
    assert not (repo.root / "src" / "caller.py").exists()
    assert repo.git("merge-base", "--is-ancestor", "tick-202", "main", check=False).returncode != 0

    # The worktree/branch are preserved so the ticket can be reworked and re-merged.
    assert wt_b.is_dir()
    assert (wt_b / "src" / "caller.py").exists()

    err = capsys.readouterr().err
    assert "post_merge_verify" in err or "post-merge" in err.lower()
