"""
Tests for lanegate/orchestrate/loop_dispatch.py.

Split out of the former monolithic tests/test_orchestrate.py (TICK-316).
"""

from __future__ import annotations

import datetime

from lanegate.git import GitText
from tests.orchestrate.conftest import *  # noqa: F401,F403


def test_structured_429_overrides_earlier_setup_error_text():
    """A real provider 429 must start the resume path even when the executor
    transcript also contains an earlier setup/configuration error."""
    from lanegate.orchestrate import loop
    from lanegate.ticket import _is_resumable_rate_limit

    assert loop._is_resumable_rate_limit is _is_resumable_rate_limit

    assert loop._is_rate_limit(
        1,
        captured_stderr=(
            "ERROR: invalid_request_error: unknown model\n"
            '{"error":"rate_limit","api_error_status":429}'
        ),
    )


class TestPoolInstanceHealthy:
    def test_pool_instance_healthy_live_cooldown_and_recent_marker_block_instance(self, tmp_path):
        from lanegate.executor import write_cooldown
        from lanegate.orchestrate.loop import _pool_instance_healthy, resolve_pool_executor
        from lanegate.ticket import _RATE_LIMIT_MARKER

        cfg = _default_cfg(tmp_path)
        cfg["pools"] = {"default": {"executors": ["codex", "claude"], "strategy": "round-robin"}}
        tickets_dir = Path(cfg["tickets_dir"])
        path = _write_ticket(tickets_dir, "TICK-001", "hibernated", touches=["a.py"])
        recent = datetime.datetime.now(datetime.UTC).isoformat()
        path.write_text(
            path.read_text()
            .replace("status: hibernated", f"status: hibernated\nstatus_changed_at: {recent}")
            .replace("Body.", f"## Hibernation Reason\n{_RATE_LIMIT_MARKER}\npool instance: codex")
        )

        assert _pool_instance_healthy(tmp_path, cfg, "codex") is False
        assert resolve_pool_executor("implement", {}, cfg, tmp_path, pool_name="default") == "claude"

        write_cooldown(tmp_path, "claude", "rate limit or quota interruption")
        assert _pool_instance_healthy(tmp_path, cfg, "claude") is False

    @pytest.mark.parametrize("status_changed_at", [None, "not-a-timestamp", "2000-01-01T00:00:00Z"])
    def test_pool_instance_healthy_stale_or_invalid_marker_fails_open(self, tmp_path, status_changed_at):
        from lanegate.orchestrate.loop import _pool_instance_healthy
        from lanegate.ticket import _RATE_LIMIT_MARKER

        cfg = _default_cfg(tmp_path)
        tickets_dir = Path(cfg["tickets_dir"])
        path = _write_ticket(tickets_dir, "TICK-001", "hibernated", touches=["a.py"])
        timestamp = "" if status_changed_at is None else f"status_changed_at: {status_changed_at}\n"
        path.write_text(
            path.read_text()
            .replace("status: hibernated", f"status: hibernated\n{timestamp}")
            .replace("Body.", f"## Hibernation Reason\n{_RATE_LIMIT_MARKER}\npool instance: codex")
        )

        assert _pool_instance_healthy(tmp_path, cfg, "codex") is True

    def test_pool_instance_healthy_malformed_ticket_directory_fails_open(self, tmp_path):
        from lanegate.orchestrate.loop import _pool_instance_healthy

        cfg = _default_cfg(tmp_path)
        with patch("lanegate.orchestrate.loop_dispatch.load_all_tickets", side_effect=ValueError):
            assert _pool_instance_healthy(tmp_path, cfg, "codex") is True


