"""
test_update_docs.py — unit tests for ad-hoc `lanegate update-docs` command.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

from lanegate.update_docs import (
    cmd_update_docs,
    enumerate_tickets_since_watermark,
    get_doc_watermark,
)


def _init_git_repo(repo_dir: Path) -> None:
    """Initialize a git repository in repo_dir for testing."""
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )


def test_watermark_calculation(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    readme = tmp_path / "README.md"
    arch = docs_dir / "ARCHITECTURE.md"

    readme.write_text("# Test Project\n")
    arch.write_text("# Architecture\n")

    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial doc commit"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    c1 = (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        .stdout.strip()
    )

    watermark = get_doc_watermark(tmp_path, ["README.md", "docs/ARCHITECTURE.md"])
    assert watermark == c1

    # Non-doc commit should not change doc watermark
    src_file = tmp_path / "main.py"
    src_file.write_text("print('hello')\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Add main.py"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    watermark2 = get_doc_watermark(tmp_path, ["README.md", "docs/ARCHITECTURE.md"])
    assert watermark2 == c1

    # Doc update should update doc watermark
    readme.write_text("# Test Project Updated\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Update README"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    c3 = (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        .stdout.strip()
    )

    watermark3 = get_doc_watermark(tmp_path, ["README.md", "docs/ARCHITECTURE.md"])
    assert watermark3 == c3
    assert watermark3 != c1


def test_enumerate_tickets_since_watermark(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)

    tickets_dir = tmp_path / ".lanegate" / "tickets"
    tickets_dir.mkdir(parents=True)

    # Ticket 1 before watermark
    t1 = tickets_dir / "TICK-101.md"
    t1.write_text(
        "---\n"
        "id: TICK-101\n"
        "title: First ticket\n"
        "status: done\n"
        "touches: []\n"
        "---\n"
        "Body 1\n"
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Add TICK-101"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    # Add doc file -> watermark
    readme = tmp_path / "README.md"
    readme.write_text("# Initial Doc\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial docs"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    watermark = get_doc_watermark(tmp_path, ["README.md"])
    assert watermark is not None

    # Ticket 2 after watermark (status: done)
    t2 = tickets_dir / "TICK-102.md"
    t2.write_text(
        "---\n"
        "id: TICK-102\n"
        "title: Second ticket\n"
        "status: done\n"
        "touches: []\n"
        "---\n"
        "Body 2\n"
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Merge TICK-102: Second ticket"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    # Ticket 3 after watermark (status: open)
    t3 = tickets_dir / "TICK-103.md"
    t3.write_text(
        "---\n"
        "id: TICK-103\n"
        "title: Third ticket\n"
        "status: open\n"
        "touches: []\n"
        "---\n"
        "Body 3\n"
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Add TICK-103"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    cfg = {"tickets_dir": ".lanegate/tickets", "ticket_prefix": "TICK"}

    qualifying = enumerate_tickets_since_watermark(
        tmp_path, cfg, watermark, status_filter=["done"]
    )
    ids = [t["id"] for t in qualifying]
    assert "TICK-102" in ids
    assert "TICK-101" not in ids
    assert "TICK-103" not in ids


def test_no_op_when_no_new_tickets(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)

    readme = tmp_path / "README.md"
    readme.write_text("# Doc\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial docs"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    head_before = (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        .stdout.strip()
    )

    cfg = {"tickets_dir": ".lanegate/tickets", "ticket_prefix": "TICK"}
    res = cmd_update_docs(cfg, tmp_path)

    head_after = (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        .stdout.strip()
    )

    assert res["status"] == "no_op"
    assert res["tickets"] == []
    assert head_before == head_after


def test_update_docs_commits_with_signoff(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    readme = tmp_path / "README.md"
    arch = docs_dir / "ARCHITECTURE.md"

    readme.write_text("# Initial README\n")
    arch.write_text("# Initial ARCHITECTURE\n")

    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial docs"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    initial_watermark = get_doc_watermark(tmp_path)
    assert initial_watermark is not None

    tickets_dir = tmp_path / ".lanegate" / "tickets"
    tickets_dir.mkdir(parents=True)

    t1 = tickets_dir / "TICK-201.md"
    t1.write_text(
        "---\n"
        "id: TICK-201\n"
        "title: Added rate limiter\n"
        "status: done\n"
        "touches: [src/api.py]\n"
        "close_criteria: Rate limiter added\n"
        "---\n"
        "Implemented rate limiting middleware.\n"
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Merge TICK-201: Added rate limiter"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    def fake_executor(repo_root: Path, prompt: str, tickets: list[dict], doc_paths: list[str]) -> None:
        (repo_root / "README.md").write_text("# Initial README\n\n## Rate Limiting\nAdded rate limiter.\n")

    cfg = {
        "tickets_dir": ".lanegate/tickets",
        "ticket_prefix": "TICK",
        "doc_update": {
            "doc_paths": ["README.md", "docs/ARCHITECTURE.md"],
            "status_filter": ["done"],
        },
    }

    calls = []
    real_run = subprocess.run

    def recording_run(cmd, **kwargs):
        calls.append(list(cmd))
        return real_run(cmd, **kwargs)

    with patch("lanegate.update_docs.subprocess.run", side_effect=recording_run):
        res = cmd_update_docs(cfg, tmp_path, executor_fn=fake_executor)

    assert res["ok"] is True
    assert res["status"] == "committed"
    assert "TICK-201" in res["tickets"]
    assert "README.md" in res["modified_docs"]

    assert "# Initial README\n\n## Rate Limiting\nAdded rate limiter.\n" in readme.read_text()

    new_watermark = get_doc_watermark(tmp_path)
    assert new_watermark is not None
    assert new_watermark != initial_watermark
    assert res["watermark"] == new_watermark

    commit_calls = [cmd for cmd in calls if cmd[:2] == ["git", "commit"]]
    assert len(commit_calls) == 1
    assert "-s" in commit_calls[0]
