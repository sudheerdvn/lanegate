from __future__ import annotations

import os
from pathlib import Path


def _write_ticket(
    tickets_dir: Path,
    ticket_id: str,
    status: str,
    *,
    worktree: str | None = None,
) -> None:
    worktree_line = f"worktree: {worktree}\n" if worktree else ""
    (tickets_dir / f"{ticket_id}.md").write_text(
        "---\n"
        f"id: {ticket_id}\n"
        f"title: {ticket_id} title\n"
        f"status: {status}\n"
        "touches:\n"
        "  - lanegate/example.py\n"
        f"{worktree_line}"
        "---\n"
        "Body is intentionally not exposed by status tools.\n",
        encoding="utf-8",
    )


def _cfg() -> dict:
    return {
        "ticket_prefix": "TICK",
        "tickets_dir": "tickets",
        "worktrees_dir": "worktrees",
        "lock_statuses": ["in_progress", "code_complete", "in_review"],
        "environments": [],
    }


def test_agent_tools_are_bounded(tmp_path, monkeypatch):
    from lanegate import mcp
    from lanegate.concurrency import acquire_orchestrator_lock, release_orchestrator_lock

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    (worktrees_dir / "tick-001").mkdir(parents=True)
    logs_dir = tmp_path / ".lanegate" / "logs"
    logs_dir.mkdir(parents=True)
    log_path = logs_dir / "orchestrate-20260708.log"
    log_path.write_text("\n".join(f"line {i:03d} " + ("x" * 40) for i in range(120)))
    _write_ticket(tickets_dir, "TICK-001", "in_progress", worktree="worktrees/tick-001")
    _write_ticket(tickets_dir, "TICK-002", "open")

    cfg = _cfg()
    monkeypatch.setattr(mcp, "_cfg_and_root", lambda: (cfg, tmp_path))
    acquire_orchestrator_lock(tmp_path, pid=os.getpid(), force=True)

    def noisy_action():
        print("o" * (mcp._MAX_ACTION_OUTPUT_BYTES + 100))

    action = mcp._capture_action(noisy_action)
    assert action["ok"] is True
    assert action["output_truncated"] is True
    assert len(action["output"].encode("utf-8")) <= mcp._MAX_ACTION_OUTPUT_BYTES
    assert action["limits"]["max_output_bytes"] == mcp._MAX_ACTION_OUTPUT_BYTES

    logs = mcp.recent_logs(limit=1, max_lines=5, max_bytes=200)
    assert logs["ok"] is True
    assert logs["logs"][0]["truncated"] is True
    assert logs["logs"][0]["line_count"] <= 5
    assert logs["logs"][0]["byte_count"] <= 200
    assert "line 119" in logs["logs"][0]["text"]

    status = mcp.repo_status()
    assert status["tickets"]["counts"]["in_progress"] == 1
    assert status["tickets"]["counts"]["open"] == 1
    assert status["tickets"]["tickets"][0]["touches_count"] == 1
    assert "_body" not in status["tickets"]["tickets"][0]
    assert status["worktrees"]["count"] == 1
    assert status["orchestrator_lock"]["pid"] == os.getpid()
    assert status["latest_log"] == str(log_path)

    continuation = mcp.continuation_context("TICK-001")
    assert continuation["ok"] is True
    assert continuation["tickets"]["ticket_count"] == 1
    assert continuation["tickets"]["tickets"][0]["id"] == "TICK-001"
    assert continuation["recent_logs"][0]["byte_count"] <= 4096
    assert ".lanegate/logs" in continuation["source_of_truth"]

    release_orchestrator_lock(tmp_path, pid=os.getpid(), force=True)


def test_orchestrate_mcp_defaults_to_dry_run(tmp_path, monkeypatch):
    from lanegate import mcp

    seen = {}

    def fake_orchestrate(cfg, repo_root, **kwargs):
        seen.update(kwargs)
        print("planned TICK-001")

    monkeypatch.setattr(mcp, "_cfg_and_root", lambda: (_cfg(), tmp_path))
    monkeypatch.setattr("lanegate.orchestrate.cmd_orchestrate", fake_orchestrate)

    result = mcp.orchestrate()

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert seen["dry_run"] is True
    assert seen["verbose"] is False
    assert result["output"] == "planned TICK-001"


def test_capture_action_handles_merge_failed_error():
    from lanegate import mcp
    from lanegate.lifecycle import MergeFailedError

    def action_that_raises_merge_error():
        raise MergeFailedError("ERROR merging branch → main:\nCONFLICT (content): file.py")

    result = mcp._capture_action(action_that_raises_merge_error)

    assert result["ok"] is False
    assert "ERROR merging branch → main" in result["error"]
    assert "exit_code" not in result
    assert result["output"] == ""
    assert result["stderr"] == ""
