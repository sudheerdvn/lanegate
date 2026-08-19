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


def test_run_mcp_defaults_to_dry_run_and_orchestrate_remains_an_alias(tmp_path, monkeypatch):
    from lanegate import mcp

    seen = {}

    def fake_orchestrate(cfg, repo_root, **kwargs):
        seen.update(kwargs)
        print("planned TICK-001")

    monkeypatch.setattr(mcp, "_cfg_and_root", lambda: (_cfg(), tmp_path))
    monkeypatch.setattr("lanegate.orchestrate.cmd_orchestrate", fake_orchestrate)

    result = mcp.run()

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert seen["dry_run"] is True
    assert seen["verbose"] is False
    assert result["output"] == "planned TICK-001"

    assert mcp.orchestrate()["dry_run"] is True


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


def test_capture_action_catches_value_error():
    from lanegate import mcp

    def action_that_raises_value_error():
        raise ValueError("invalid ticket ID: 'TICK-900 '")

    result = mcp._capture_action(action_that_raises_value_error)

    assert result["ok"] is False
    assert "invalid ticket ID" in result["error"]
    assert "exit_code" not in result
    assert result["output"] == ""
    assert result["stderr"] == ""


def test_review_does_not_crash_when_action_fails_on_malformed_id(tmp_path, monkeypatch):
    from lanegate import mcp

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-001", "code_complete")

    cfg = _cfg()
    monkeypatch.setattr(mcp, "_cfg_and_root", lambda: (cfg, tmp_path))

    result = mcp.review("TICK-900 ")

    assert result["ok"] is False
    assert "invalid ticket ID" in result["error"]
    assert "verdict" not in result


def test_repo_status_malformed_ticket_id_filter_returns_no_match(tmp_path, monkeypatch):
    from lanegate import mcp

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-001", "open")

    cfg = _cfg()
    monkeypatch.setattr(mcp, "_cfg_and_root", lambda: (cfg, tmp_path))

    status = mcp.repo_status(ticket_id="TICK-900 ")

    assert status["ok"] is True
    assert status["tickets"]["ticket_count"] == 0
    assert status["tickets"]["tickets"] == []


def test_human_review_approve_mcp_tool(tmp_path, monkeypatch):
    import shutil
    import subprocess
    import pytest
    from lanegate import mcp
    from lanegate.ticket import parse_ticket

    if shutil.which("git") is None:
        pytest.skip("git is required for this test")

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()

    wt_path = worktrees_dir / "tick-006"
    wt_path.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=wt_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=wt_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=wt_path, check=True, capture_output=True)
    (wt_path / "shared.py").write_text("line1\n")
    subprocess.run(["git", "add", "."], cwd=wt_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=wt_path, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "tick-006"], cwd=wt_path, check=True, capture_output=True)
    (wt_path / "requirements.txt").write_text("requests==2.0\n")
    subprocess.run(["git", "add", "."], cwd=wt_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "ticket work"], cwd=wt_path, check=True, capture_output=True)

    ticket_file = tickets_dir / "TICK-006.md"
    ticket_file.write_text(
        f"---\nid: TICK-006\ntitle: Title\nstatus: needs_review\nworktree: {wt_path}\n"
        "branch: tick-006\nreview_verdict: changes_requested\nreview_summary: blocked by gate\n"
        "---\n"
        "Body content.\n"
    )

    cfg = _cfg()
    monkeypatch.setattr(mcp, "_cfg_and_root", lambda: (cfg, tmp_path))

    # Reject when rationale is empty or whitespace
    res_empty = mcp.human_review_approve("TICK-006", rationale="")
    assert res_empty["ok"] is False
    assert "--rationale is required" in res_empty["stderr"]

    ticket = parse_ticket(ticket_file)
    assert ticket["status"] == "needs_review"

    # Approve with rationale
    res_ok = mcp.human_review_approve("TICK-006", rationale="Verified manually by operator")
    assert res_ok["ok"] is True
    assert res_ok["status"] == "code_complete"

    ticket = parse_ticket(ticket_file)
    assert ticket["status"] == "code_complete"
    assert ticket.get("protected_path_approved_rationale") == "Verified manually by operator"
    assert ticket.get("protected_path_approved_actor") == "agent"
    expected_msg = "human-review approval recorded via agent tool call, rationale: Verified manually by operator"
    assert expected_msg in ticket.get("_body", "")
    events = ticket.get("lifecycle_events") or []
    assert any(e.get("event") == "human_review_approved" and e.get("summary") == expected_msg for e in events)


def test_mcp_start_does_not_record_killable_executor_pid(tmp_path, monkeypatch):
    from lanegate import lifecycle, mcp
    from lanegate.orchestrate.loop import _reap_orphaned_executor_processes
    from lanegate.orchestrate.status import (
        _reconcile_stale_executor_markers,
        _remove_executor_markers,
    )

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-001", "open")

    cfg = _cfg()
    monkeypatch.setattr(mcp, "_cfg_and_root", lambda: (cfg, tmp_path))
    monkeypatch.setattr(lifecycle, "check_local_not_behind_remote", lambda *_: None)
    monkeypatch.setattr(
        lifecycle,
        "create_worktree",
        lambda _root, worktrees_dir, _tid, _branch, _trunk, **_kwargs: worktrees_dir / "tick-001",
    )
    monkeypatch.setattr(lifecycle, "_commit_generated_ticket_write", lambda *_args, **_kwargs: True)

    kill_calls = []
    monkeypatch.setattr("lanegate.orchestrate.loop._kill_pid", lambda pid: kill_calls.append(pid) or True)

    result = mcp.start("TICK-001")

    marker_base = tmp_path / ".lanegate" / "TICK-001"
    assert result["ok"] is True
    assert result["action_id"].startswith("action-")
    assert result["status"] == "success"
    assert Path(result["log_path"]).is_file()
    assert marker_base.with_suffix(".mcp").exists()
    assert not marker_base.with_suffix(".pid").exists()

    reaped = _reap_orphaned_executor_processes(cfg, tmp_path)
    assert reaped == []
    assert len(kill_calls) == 0

    assert _reconcile_stale_executor_markers(cfg, tmp_path) is None
    assert marker_base.with_suffix(".mcp").exists()

    _remove_executor_markers(tmp_path, "TICK-001")
    assert not marker_base.with_suffix(".mcp").exists()


def test_create_mcp_tool_writes_draft_ticket_without_analyzing(tmp_path, monkeypatch):
    import lanegate.analyze as analyze_mod
    from lanegate import mcp
    from lanegate.ticket import parse_ticket

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()

    cfg = _cfg()
    monkeypatch.setattr(mcp, "_cfg_and_root", lambda: (cfg, tmp_path))

    analyze_calls = []
    monkeypatch.setattr(
        analyze_mod, "cmd_analyze", lambda *a, **k: analyze_calls.append((a, k))
    )

    result = mcp.create("Add a health check endpoint")

    assert result["ok"] is True
    assert result["ticket_id"] == "TICK-001"
    assert result["status"] == "draft"
    assert result["touches"] == []
    assert analyze_calls == []

    ticket = parse_ticket(tickets_dir / "TICK-001.md")
    assert ticket["status"] == "draft"


def test_create_mcp_tool_does_not_leak_intent_into_tracked_ticket_id(tmp_path, monkeypatch):
    """Regression (TICK-545): the intent prose must not end up in the action
    event's ticket_id field, where `lanegate ps`/TUI run history expect a
    short ticket id."""
    import json

    from lanegate import mcp

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()

    cfg = _cfg()
    monkeypatch.setattr(mcp, "_cfg_and_root", lambda: (cfg, tmp_path))

    intent = "Add a health check endpoint so uptime monitors can poll it"
    result = mcp.create(intent)

    events = [
        json.loads(line) for line in Path(result["log_path"]).read_text().splitlines() if line
    ]
    assert events
    for event in events:
        assert event.get("ticket_id") != intent


def test_analyze_mcp_tool_advances_draft_to_open(tmp_path, monkeypatch):
    import lanegate.analyze as analyze_mod
    from lanegate import mcp
    from lanegate.ticket import parse_ticket, write_ticket

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-001", "draft")

    cfg = _cfg()
    monkeypatch.setattr(mcp, "_cfg_and_root", lambda: (cfg, tmp_path))

    def fake_analyze(ticket_id, cfg, repo_root, keep_draft=False):
        path = tickets_dir / f"{ticket_id}.md"
        ticket = parse_ticket(path)
        ticket["status"] = "draft" if keep_draft else "open"
        ticket["touches"] = ["lanegate/health.py"]
        ticket["close_criteria"] = ["health endpoint returns 200"]
        ticket["_path"] = path
        write_ticket(ticket)

    monkeypatch.setattr(analyze_mod, "cmd_analyze", fake_analyze)

    result = mcp.analyze("TICK-001")

    assert result["ok"] is True
    assert result["ticket_id"] == "TICK-001"
    assert result["status"] == "open"
    assert result["touches"] == ["lanegate/health.py"]
    assert result["close_criteria"] == ["health endpoint returns 200"]

    ticket = parse_ticket(tickets_dir / "TICK-001.md")
    assert ticket["status"] == "open"


def test_analyze_mcp_tool_keep_draft_stays_draft(tmp_path, monkeypatch):
    import lanegate.analyze as analyze_mod
    from lanegate import mcp
    from lanegate.ticket import parse_ticket, write_ticket

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-001", "draft")

    cfg = _cfg()
    monkeypatch.setattr(mcp, "_cfg_and_root", lambda: (cfg, tmp_path))

    def fake_analyze(ticket_id, cfg, repo_root, keep_draft=False):
        path = tickets_dir / f"{ticket_id}.md"
        ticket = parse_ticket(path)
        ticket["status"] = "draft" if keep_draft else "open"
        ticket["touches"] = ["lanegate/health.py"]
        ticket["_path"] = path
        write_ticket(ticket)

    monkeypatch.setattr(analyze_mod, "cmd_analyze", fake_analyze)

    result = mcp.analyze("TICK-001", keep_draft=True)

    assert result["ok"] is True
    assert result["status"] == "draft"
    assert result["touches"] == ["lanegate/health.py"]


def test_analyze_mcp_tool_reports_failure_without_crashing(tmp_path, monkeypatch):
    import lanegate.analyze as analyze_mod
    from lanegate import mcp

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    _write_ticket(tickets_dir, "TICK-001", "draft")

    cfg = _cfg()
    monkeypatch.setattr(mcp, "_cfg_and_root", lambda: (cfg, tmp_path))

    def failing_analyze(ticket_id, cfg, repo_root, keep_draft=False):
        raise SystemExit(1)

    monkeypatch.setattr(analyze_mod, "cmd_analyze", failing_analyze)

    result = mcp.analyze("TICK-001")

    assert result["ok"] is False
    assert result["exit_code"] == 1

    from lanegate.ticket import parse_ticket

    ticket = parse_ticket(tickets_dir / "TICK-001.md")
    assert ticket["status"] == "draft"


def test_analyze_mcp_tool_handles_malformed_ticket_id_without_exception(tmp_path, monkeypatch):
    from lanegate import mcp

    cfg = _cfg()
    monkeypatch.setattr(mcp, "_cfg_and_root", lambda: (cfg, tmp_path))

    result = mcp.analyze("TICK-900 ")

    assert result["ok"] is False
    assert "invalid ticket ID" in result["error"]
