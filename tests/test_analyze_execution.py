"""Tests for analyze_execution.py."""

"""Tests for analyze.py — cmd_analyze with stubbed model seam."""

import json
import shutil
import signal
import socket
import subprocess as _subprocess
import threading
import time
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

import pytest

from lanegate.analyze import (
    _HAS_TREE_SITTER,
    _TS_LANGUAGE_MAP,
    _already_resolved_reason_matches_worktree,
    _ast_symbol_hits,
    _build_ast_index,
    _build_candidate_skeletons,
    _build_file_skeleton,
    _build_prompt,
    _call_model,
    _close_criteria_drifted,
    _extract_acceptance_checklist,
    _import_graph_expand,
    _index_non_py_file,
    _index_py_file,
    _parse_response,
    _repo_structure,
    _ripgrep_seed,
    _treesitter_hits,
    audit_acceptance_contract,
    cmd_analyze,
    companion_docs_from_criteria,
    correct_touches_by_basename,
    enrich_context,
    infer_touches_from_criteria,
    validate_touched_paths,
    verify_acceptance_criteria,
)
from lanegate.config import ConfigError
from lanegate.ticket import parse_ticket, validate_ticket

_CFG = {
    "ticket_prefix": "TICK",
    "tickets_dir": "tickets",
    "commit_status_changes": False,
}

_GOOD_RESPONSE = json.dumps(
    {
        "touches": ["lanegate/foo.py", "tests/test_foo.py"],
        "close_criteria": "cmd_foo writes a file and returns 0.",
        "depends_on": [],
    }
)


@pytest.fixture(autouse=True)
def _resolve_executor_bins_to_their_names(monkeypatch):
    monkeypatch.setattr("lanegate.executor.shutil.which", lambda bin_name: bin_name)


def _make_draft(
    tickets_dir: Path,
    ticket_id: str = "TICK-001",
    title: str = "Add foo command",
    touches: list[str] | None = None,
) -> Path:
    if touches:
        touches_yaml = "touches:\n" + "".join(f"  - {touch}\n" for touch in touches)
    else:
        touches_yaml = "touches: []\n"
    fm = f"id: {ticket_id}\ntitle: {title}\nstatus: draft\npriority: 3\n{touches_yaml}"
    path = tickets_dir / f"{ticket_id}.md"
    path.write_text(f"---\n{fm}---\n## Background\nWe need a foo command.\n")
    return path


@pytest.fixture
def repo(tmp_path):
    td = tmp_path / "tickets"
    td.mkdir()
    return tmp_path


def test_call_model_injects_model_flag():
    """_call_model appends --model <model> when model is provided."""
    captured = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return _subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    with patch("lanegate.analyze_execution.subprocess.run", side_effect=fake_run):
        _call_model("hello", model="claude-sonnet-4-5")

    assert captured
    cmd = captured[0]
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-5"


def test_call_model_no_flag_without_model():
    """_call_model does NOT add --model when model is None."""
    captured = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return _subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    with patch("lanegate.analyze_execution.subprocess.run", side_effect=fake_run):
        _call_model("hello", model=None)

    assert captured
    assert "--model" not in captured[0]


def test_call_model_claude_denies_mutating_tools():
    """Analyze must stay read-only: Bash/Write/Edit are denied for Claude-CLI
    executors even though the executor's own flags (e.g.
    --dangerously-skip-permissions) would otherwise grant full tool access."""
    captured = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return _subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    with patch("lanegate.analyze_execution.subprocess.run", side_effect=fake_run):
        _call_model("hello", executor="claude")

    assert captured
    cmd = captured[0]
    assert "--disallowedTools" in cmd
    assert cmd[cmd.index("--disallowedTools") + 1] == "Bash,Write,Edit"


def test_call_model_non_claude_executor_omits_disallowed_tools():
    captured = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return _subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    with patch("lanegate.analyze_execution.subprocess.run", side_effect=fake_run):
        _call_model("hello", executor="ollama")

    assert captured
    assert "--disallowedTools" not in captured[0]


@pytest.mark.parametrize(
    "executor,flag,flag_value",
    [("aider", "--dry-run", None), ("codex", "--sandbox", "read-only"), ("agy", "--mode", "plan")],
)
def test_call_model_non_claude_executor_readonly_during_analyze(executor, flag, flag_value):
    """Analyze must stay read-only for aider/codex/agy too, not just Claude --
    TICK-573: aider previously ran with full edit/commit capability during
    analyze, before any worktree exists, against the main checkout."""
    captured = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return _subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    with patch("lanegate.analyze_execution.subprocess.run", side_effect=fake_run):
        _call_model("hello", executor=executor)

    # aider's build path makes its own preliminary `git` subprocess calls
    # (context budgeting) through the same patched subprocess.run, so the
    # executor's own cmd isn't always captured[0] -- find it by bin name.
    cmd = next(c for c in captured if c and c[0] == executor)
    assert flag in cmd
    if flag_value is not None:
        assert cmd[cmd.index(flag) + 1] == flag_value


def test_call_model_non_stdin_executor_gets_devnull_stdin():
    """A non-stdin-capable executor (e.g. kiro) must not inherit LaneGate's
    stdin: kiro-cli can probe stdin even under --no-interactive, and an
    inherited pipe that never reaches EOF hangs `analyze` forever."""
    captured_kwargs = []

    def fake_run(executor_type, cmd, **kwargs):
        captured_kwargs.append(kwargs)
        return _subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    with patch("lanegate.executor.run_executor_subprocess", side_effect=fake_run):
        _call_model("hello", executor="kiro")

    assert captured_kwargs
    assert captured_kwargs[0]["stdin"] == _subprocess.DEVNULL
    assert captured_kwargs[0]["input"] is None


def test_call_model_stdin_executor_keeps_default_stdin():
    """A stdin-capable executor (e.g. claude) still delivers the prompt via
    `input=`, unaffected by the kiro DEVNULL fix."""
    captured_kwargs = []

    def fake_run(cmd, **kwargs):
        captured_kwargs.append(kwargs)
        return _subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    with patch("lanegate.analyze_execution.subprocess.run", side_effect=fake_run):
        _call_model("hello", executor="claude")

    assert captured_kwargs
    assert captured_kwargs[0]["stdin"] is None
    assert captured_kwargs[0]["input"] == "hello"


def test_call_model_missing_executor_bin_raises_before_subprocess(monkeypatch):
    monkeypatch.setenv("PATH", "/restricted/bin")
    monkeypatch.setattr("lanegate.executor.shutil.which", lambda _bin_name: None)

    with patch("lanegate.analyze_execution.subprocess.run") as mock_run:
        with pytest.raises(ConfigError, match="executor 'claude'.*bin 'claude'.*PATH"):
            _call_model("hello")

    mock_run.assert_not_called()


def test_analyze_terminal_lifecycle(repo, capsys):
    _make_draft(repo / "tickets")

    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: _GOOD_RESPONSE)

    output = capsys.readouterr().out
    assert "Indexing context..." in output
    assert "Prompt ready (" in output
    assert "Executor:" in output and "Model:" in output
    assert "Waiting for model... (elapsed 0s)" in output


def test_analyze_log_persistence(repo):
    _make_draft(repo / "tickets")

    cmd_analyze("TICK-001", _CFG, repo, model_fn=lambda p: _GOOD_RESPONSE)

    logs = list((repo / ".lanegate" / "logs").glob("analyze-*.log"))
    assert len(logs) == 1
    contents = logs[0].read_text()
    for phase in ("context_indexed", "prompt_ready", "model_requested", "model_responded", "analysis_complete"):
        assert phase in contents
    assert "executor_output" in contents
    assert '"touches"' in contents


def test_analyze_status_record(repo):
    _make_draft(repo / "tickets")
    model_started = threading.Event()
    allow_model_to_finish = threading.Event()
    failures: list[BaseException] = []

    def blocking_model(_prompt):
        model_started.set()
        assert allow_model_to_finish.wait(3)
        return _GOOD_RESPONSE

    def run() -> None:
        try:
            cmd_analyze("TICK-001", _CFG, repo, model_fn=blocking_model)
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert model_started.wait(3)
    status_path = repo / ".lanegate" / "analyze-active.json"
    for _ in range(30):
        if status_path.exists():
            break
        time.sleep(0.01)
    assert status_path.exists()
    active = json.loads(status_path.read_text())
    assert active["ticket_id"] == "TICK-001"
    assert active["phase"] == "model_requested"
    assert active["executor"]
    assert active["model"]
    assert active["log_path"].startswith(".lanegate/logs/analyze-")

    allow_model_to_finish.set()
    worker.join(3)
    assert not worker.is_alive()
    assert failures == []
    assert not status_path.exists()


def test_analyze_api_status(repo):
    from lanegate.analyze import _write_active_analysis
    from lanegate.api import LaneGateApiServer
    from lanegate.logs import analyze_log_path

    log_file = analyze_log_path(repo)
    _write_active_analysis(
        repo,
        ticket_id="TICK-001",
        phase="model_requested",
        executor="claude",
        model="claude-test",
        started_at=time.time() - 2,
        log_file=log_file,
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = LaneGateApiServer(_CFG, repo, port=port)
    server.start()
    try:
        time.sleep(0.05)
        connection = HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request("GET", "/api/v1/analyze/status")
        response = connection.getresponse()
        body = json.loads(response.read().decode())
        connection.close()
        assert response.status == 200
        assert body["ticket_id"] == "TICK-001"
        assert body["phase"] == "model_requested"
        assert body["executor"] == "claude"
        assert body["model"] == "claude-test"
        assert body["elapsed_seconds"] >= 2
        assert body["log_path"].startswith(".lanegate/logs/analyze-")
    finally:
        server.stop()


def test_analyze_cleanup(repo):
    _make_draft(repo / "tickets")
    status_path = repo / ".lanegate" / "analyze-active.json"

    def unavailable_model(_prompt):
        raise RuntimeError("unavailable")

    def interrupted_model(_prompt):
        signal.raise_signal(signal.SIGTERM)

    with pytest.raises(SystemExit):
        cmd_analyze("TICK-001", _CFG, repo, model_fn=unavailable_model)
    assert not status_path.exists()

    with pytest.raises(SystemExit) as interrupted:
        cmd_analyze("TICK-001", _CFG, repo, model_fn=interrupted_model)
    assert interrupted.value.code == 130
    assert not status_path.exists()
    logs = sorted((repo / ".lanegate" / "logs").glob("analyze-*.log"))
    assert len(logs) == 2
    assert all("analysis_failed" in log.read_text() for log in logs)

