"""Unit tests for lanegate.executor_events progress protocol."""

import time
from lanegate.executor_events import (
    ExecutorEvent,
    bound_path,
    check_stall,
    fallback_heartbeat_event,
    has_structured_progress,
    normalize_claude_event,
    normalize_codex_event,
    normalize_executor_event,
    phase_for_step,
    redact_text,
    redact_transcript_text,
)
from lanegate.orchestrate.status import format_executor_event_status


def test_redaction_secrets_and_size_limits():
    raw_secret = "api_key = 'sk-1234567890123456789012' secret=bearer_token_xyz"
    redacted = redact_text(raw_secret)
    assert "sk-" not in redacted
    assert "[REDACTED]" in redacted

    long_str = "x" * 1000
    bounded = redact_text(long_str, max_len=50)
    assert len(bounded) <= 50
    assert bounded.endswith("...")


def test_transcript_redaction_preserves_ordinary_line_length():
    ordinary_line = "x" * 1000
    assert redact_transcript_text(ordinary_line) == ordinary_line

    redacted = redact_transcript_text("api_key=sk-1234567890123456789012")
    assert "sk-" not in redacted
    assert "[REDACTED]" in redacted


def test_path_bounding():
    assert bound_path(None) is None
    assert bound_path("src/api.py") == "src/api.py"
    assert bound_path("/abs/path/to/src/api.py") == "abs/path/to/src/api.py"

    long_path = "a/" * 200 + "file.py"
    bounded = bound_path(long_path)
    assert len(bounded) <= 256
    assert bounded.startswith("...")
    assert bound_path("../../.env") is None


def test_serialized_event_allows_only_safe_bounded_metadata():
    event = ExecutorEvent.from_dict(
        {
            "phase": "review",
            "activity": "raw executor reasoning that must not be displayed",
            "executor": "codex token=sk-1234567890123456789012",
            "model": "x" * 200,
            "path": "../secret.env",
            "test_summary": {"status": "pass", "output": "password=hunter2", "passed": 3},
            "provider_usage": {"input_tokens": 10, "reasoning": "raw private text"},
            "raw_output": "do not retain this",
        }
    ).to_dict()
    assert event["phase"] == "reviewing"
    assert event["activity"] == "heartbeat"
    assert "sk-" not in event["executor"]
    assert len(event["model"]) <= 96
    assert event["path"] is None
    assert event["test_summary"] == {"status": "pass", "passed": 3}
    assert event["provider_usage"] == {"input_tokens": 10}
    assert "raw_output" not in event


def test_lifecycle_steps_are_normalized_to_public_phases():
    assert phase_for_step("implement") == "implementing"
    assert phase_for_step("review") == "reviewing"
    assert phase_for_step("fix") == "implementing"


def test_claude_normalization():
    # Read tool
    claude_read = {
        "type": "content_block_start",
        "content_block": {"type": "tool_use", "name": "Read", "input": {"file_path": "src/main.py"}},
    }
    ev = normalize_claude_event(claude_read, executor="claude", model="sonnet-3.5")
    assert ev is not None
    assert ev.phase == "implementing"
    assert ev.activity == "reading_file"
    assert ev.tool_category == "file_read"
    assert ev.path == "src/main.py"

    # Edit tool
    claude_edit = {
        "type": "content_block_start",
        "content_block": {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/main.py"}},
    }
    ev = normalize_claude_event(claude_edit, executor="claude")
    assert ev is not None
    assert ev.activity == "writing_file"
    assert ev.tool_category == "file_write"

    # Bash test tool
    claude_bash = {
        "type": "content_block_start",
        "content_block": {"type": "tool_use", "name": "Bash", "input": {"command": "pytest tests/", "description": "Run focused regression tests"}},
    }
    ev = normalize_claude_event(claude_bash, executor="claude")
    assert ev is not None
    assert ev.phase == "testing"
    assert ev.activity == "testing"
    assert ev.tool_category == "pytest"
    assert ev.intent == "Run focused regression tests"
    assert ev.test_summary == {"category": "pytest", "status": "running"}

    # Result
    claude_result = {
        "type": "result",
        "total_cost_usd": 0.015,
        "usage": {"input_tokens": 500, "output_tokens": 100},
    }
    ev = normalize_claude_event(claude_result, executor="claude")
    assert ev is not None
    assert ev.activity == "completed"
    assert ev.provider_usage == {"input_tokens": 500, "output_tokens": 100, "cost_usd": 0.015}

    # Recent Claude stream-json versions wrap tool calls in assistant events.
    wrapped = {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Write", "input": {"path": "src/new.py"}}]},
    }
    ev = normalize_claude_event(wrapped, current_phase="review")
    assert ev is not None
    assert ev.phase == "reviewing"
    assert ev.activity == "writing_file"

    # Claude emits each command's return value as a user tool_result event.
    claude_tool_result = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "content": "Success: no issues found in 59 source files"}]},
    }
    ev = normalize_claude_event(claude_tool_result, executor="claude")
    assert ev is not None
    assert ev.phase == "testing"
    assert ev.activity == "testing"
    assert ev.test_summary == {"category": "test", "status": "pass"}
    assert ev.intent == "Success: no issues found in 59 source files"


def test_semantic_intent_and_test_results_across_executor_streams():
    claude = normalize_claude_event(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Inspect the failed contract before editing."}]}},
        executor="claude",
    )
    assert claude is not None
    assert claude.intent == "Inspect the failed contract before editing."

    agy = normalize_executor_event(
        '{"toolAction":"Rerun mypy after reverting contract","toolSummary":"mypy: 0 errors"}',
        executor="agy",
    )
    assert agy is not None
    assert agy.intent == "Rerun mypy after reverting contract"
    assert agy.test_summary == {"category": "mypy", "errors": 0, "status": "pass"}
    assert "mypy: 0 errors" in format_executor_event_status("TICK-622", agy)

    codex = normalize_codex_event(
        {"type": "item.completed", "item": {"type": "command_execution", "command": "pytest -q", "description": "Verify the live ticker", "exit_code": 0}},
        executor="codex",
    )
    assert codex is not None
    assert codex.intent == "Verify the live ticker"
    assert codex.test_summary == {"category": "pytest", "status": "pass"}

    aider = normalize_executor_event("pytest: 59 passed", executor="aider")
    assert aider is not None
    assert aider.intent == "pytest: 59 passed"
    assert aider.test_summary == {"category": "pytest", "passed": 59, "status": "pass"}

    ticker = format_executor_event_status("TICK-622", aider)
    assert "intent: pytest: 59 passed" in ticker
    assert "pytest: 59 passed" in ticker


def test_semantic_intent_is_redacted_and_bounded():
    ev = normalize_claude_event(
        {"type": "tool_use", "name": "Bash", "input": {"command": "pytest", "description": "token=supersecret " + "x" * 200}},
    )
    assert ev is not None
    assert "supersecret" not in ev.intent
    assert len(ev.intent) <= 160


def test_codex_normalization():
    codex_started = {
        "type": "item.started",
        "item": {"type": "tool_call", "name": "file_read", "path": "tests/test_api.py"},
    }
    ev = normalize_codex_event(codex_started, executor="codex")
    assert ev is not None
    assert ev.activity == "reading_file"
    assert ev.path == "tests/test_api.py"

    codex_completed = {
        "type": "item.completed",
        "item": {"type": "command_execution", "command": "pytest tests/", "exit_code": 0},
    }
    ev = normalize_codex_event(codex_completed, executor="codex")
    assert ev is not None
    assert ev.phase == "testing"
    assert ev.activity == "testing"
    assert ev.test_summary == {"category": "pytest", "status": "pass"}

    codex_turn = {
        "type": "turn.completed",
        "usage": {"input_tokens": 1000, "output_tokens": 200},
    }
    ev = normalize_codex_event(codex_turn, executor="codex")
    assert ev is not None
    assert ev.activity == "completed"
    assert ev.provider_usage == {"input_tokens": 1000, "output_tokens": 200}


def test_malformed_events():
    assert normalize_executor_event("") is None
    assert normalize_executor_event("not json") is None
    assert normalize_executor_event("12345") is None
    assert normalize_executor_event("{}") is None
    assert normalize_executor_event('{"type": "unknown_type_xyz"}') is not None
    assert normalize_claude_event({"type": "user", "message": {"content": [{"type": "tool_result", "content": None}]}}) is None


def test_fallback_behavior_and_stall_detection():
    fb = fallback_heartbeat_event("aider", model="gpt-4o", current_phase="implementing", activity_age=5.0)
    assert fb.executor == "aider"
    assert fb.activity == "heartbeat"
    assert fb.activity_age == 5.0

    fb_stall = fallback_heartbeat_event("claude", is_stall=True)
    assert fb_stall.activity == "stall"

    now = time.time()
    assert check_stall(now - 10.0, threshold_secs=30.0) is False
    assert check_stall(now - 40.0, threshold_secs=30.0) is True


def test_has_structured_progress_distinguishes_json_streaming_executors():
    assert has_structured_progress("claude") is True
    assert has_structured_progress("claude-process") is True
    assert has_structured_progress("codex") is True
    assert has_structured_progress("codex-cli") is True
    # Anything without a structured JSON progress stream -- aider and any
    # other generic driver (continue.dev, openhands, ...) -- must not be
    # treated as capable of the same silence-implies-stall signal claude/codex
    # get: a single local-model turn can run minutes with zero output.
    assert has_structured_progress("aider") is False
    assert has_structured_progress("aider-7b") is False
    assert has_structured_progress("aider-14b") is False
    assert has_structured_progress("agy") is False
    assert has_structured_progress("continue.dev") is False
    assert has_structured_progress("openhands") is False
