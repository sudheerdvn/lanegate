from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from lanegate.config import CONFIG_FILENAME, load_config


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


@dataclass(frozen=True)
class RealLaneGateRepo:
    root: Path
    cfg: dict

    @property
    def tickets_dir(self) -> Path:
        return self.root / self.cfg["tickets_dir"]

    @property
    def worktrees_dir(self) -> Path:
        return self.root / self.cfg["worktrees_dir"]

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return _git(self.root, *args, check=check)

    def commit_all(self, message: str) -> None:
        self.git("add", ".")
        self.git("commit", "-m", message)


@pytest.fixture
def real_lanegate_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RealLaneGateRepo:
    """Create a real git repo with tracked LaneGate state for integration tests."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "lanegate-tests@example.com")
    _git(repo, "config", "user.name", "LaneGate Tests")

    (repo / ".lanegate" / "tickets").mkdir(parents=True)
    (repo / ".lanegate" / "worktrees").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text('VALUE = "initial"\n')
    (repo / CONFIG_FILENAME).write_text(
        "\n".join(
            [
                "ticket_prefix: TICK",
                "tickets_dir: .lanegate/tickets",
                "worktrees_dir: .lanegate/worktrees",
                "executor: claude",
                "max_parallel: 2",
                "lock_statuses:",
                "  - in_progress",
                "  - code_complete",
                "  - in_review",
                "commit_status_changes: true",
                "github_pr: false",
                "safeguards: {}",
                "",
            ]
        )
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial repo")

    return RealLaneGateRepo(root=repo, cfg=load_config(repo))
