"""Executor progress event protocol, normalization, redaction, and stall detection.

Provides a unified, safe, bounded event schema for executor execution progress
(implementation, review, review-fix, drift-check).
"""

from dataclasses import asdict, dataclass
import datetime
import json
import re
from typing import Any

MAX_PATH_LENGTH = 256
MAX_STRING_LENGTH = 512

_PHASES = {"analyzing", "implementing", "reviewing", "testing", "waiting"}
_ACTIVITIES = {
    "planning", "tool_use", "reading_file", "writing_file", "running_command",
    "testing", "thinking", "searching", "provider_wait", "stall", "heartbeat",
    "completed",
}
_TOOL_CATEGORIES = {"file_read", "file_write", "command", "search", "think", "pytest", "test", "other"}
_TEST_STATUSES = {"running", "pass", "fail", "unknown"}
_USAGE_KEYS = {"input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens", "cost_usd"}

_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|token|password|bearer|auth)\s*[:=]\s*['\"]?([^\s'\"]+)"),
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    re.compile(r"ghp_[a-zA-Z0-9]{20,}"),
]


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def redact_text(text: str | None, max_len: int = MAX_STRING_LENGTH) -> str | None:
    """Redact sensitive patterns and enforce maximum length bounds."""
    if text is None:
        return None
    cleaned = str(text)
    for pat in _SECRET_PATTERNS:
        cleaned = pat.sub(r"\1=[REDACTED]", cleaned) if pat.groups > 1 else pat.sub("[REDACTED]", cleaned)
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 3] + "..."
    return cleaned


def redact_transcript_text(text: str) -> str:
    """Redact a raw transcript line without applying the metadata size limit."""
    return redact_text(text, max_len=max(len(text), 1)) or ""


def bound_path(path_str: str | None) -> str | None:
    """Clean and bound a file path to relative format under MAX_PATH_LENGTH."""
    if not path_str:
        return None
    p = redact_text(str(path_str).strip())
    if not p:
        return None
    # Strip leading slash or common absolute prefixes if relative wanted
    p = re.sub(r"^[a-zA-Z]:[/\\]", "", p)
    p = p.lstrip("/\\")
    # Paths are metadata, not an escape hatch for arbitrary host locations.
    # The executor has no repository root here, so reject traversal rather
    # than attempting to resolve it against a potentially wrong directory.
    parts = [part for part in re.split(r"[/\\]+", p) if part and part != "."]
    if any(part == ".." for part in parts):
        return None
    p = "/".join(parts)
    if len(p) > MAX_PATH_LENGTH:
        p = "..." + p[-(MAX_PATH_LENGTH - 3) :]
    return p


def phase_for_step(step: str | None) -> str:
    """Map lifecycle step names to the public, executor-neutral phases.

    Idempotent: the normalizers below re-apply this to their ``current_phase``
    argument, so a caller that had already mapped ``review`` to ``reviewing``
    used to get it silently re-mapped to ``implementing`` — every review and
    analyze event was reported under the wrong phase.
    """
    value = str(step or "").lower()
    if value in _PHASES:
        return value
    if value in ("analyze", "analysis"):
        return "analyzing"
    if value in ("review", "drift-check", "drift_check"):
        return "reviewing"
    if value in ("test", "testing"):
        return "testing"
    if value in ("wait", "waiting"):
        return "waiting"
    return "implementing"


def _safe_label(value: object, default: str | None = None) -> str | None:
    if value is None:
        return default
    return redact_text(str(value), 96) or default


def _safe_timestamp(value: object) -> str:
    """Keep only a compact ISO-like timestamp; executor text never supplies it."""
    if isinstance(value, str) and len(value) <= 64:
        try:
            datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return value
        except ValueError:
            pass
    return _utc_now_iso()


def _safe_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or value != value:  # includes NaN
        return None
    return value


def _safe_test_summary(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    category = value.get("category")
    if category in _TOOL_CATEGORIES:
        result["category"] = category
    status = value.get("status")
    if status in _TEST_STATUSES:
        result["status"] = status
    for key in ("passed", "failed"):
        number = _safe_number(value.get(key))
        if number is not None:
            result[key] = int(number)
    return result or None


def _safe_provider_usage(value: object) -> dict[str, int | float] | None:
    if not isinstance(value, dict):
        return None
    return {
        key: number
        for key in _USAGE_KEYS
        if (number := _safe_number(value.get(key))) is not None
    } or None


@dataclass
class ExecutorEvent:
    """Bounded, safe executor progress event."""

    phase: str  # "analyzing", "implementing", "reviewing", "testing", "waiting", "idle", "completed", "failed"
    activity: str  # "planning", "tool_use", "reading_file", "writing_file", "running_command", "testing", "thinking", "provider_wait", "stall", "heartbeat"
    ts: str
    activity_age: float = 0.0
    executor: str = "unknown"
    model: str | None = None
    tool_category: str | None = None  # "file_read", "file_write", "command", "search", "think", "test", "other"
    path: str | None = None
    test_summary: dict[str, Any] | None = None
    provider_usage: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        # Never serialize provider payloads verbatim.  This boundary is used
        # by event logs, API responses, terminal status, and the TUI.
        return self.from_dict(asdict(self)).__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutorEvent":
        phase = data.get("phase")
        activity = data.get("activity")
        known = {
            "phase": phase if phase in _PHASES else phase_for_step(str(phase or "")),
            "activity": activity if activity in _ACTIVITIES else "heartbeat",
            "ts": _safe_timestamp(data.get("ts")),
            "activity_age": _safe_number(data.get("activity_age")) or 0.0,
            "executor": _safe_label(data.get("executor"), "unknown"),
            "model": _safe_label(data.get("model")),
            "tool_category": data.get("tool_category") if data.get("tool_category") in _TOOL_CATEGORIES else None,
            "path": bound_path(data.get("path")),
            "test_summary": _safe_test_summary(data.get("test_summary")),
            "provider_usage": _safe_provider_usage(data.get("provider_usage")),
        }
        return cls(**known)


def parse_command_category(cmd_text: str) -> tuple[str, str, dict[str, Any] | None]:
    """Parse shell command into (tool_category, activity, test_summary).

    Never retains full command line or arguments to prevent secret leakage.
    """
    cmd_lower = cmd_text.lower()
    if any(t in cmd_lower for t in ["pytest", "unittest", "npm test", "jest", "go test", "cargo test", "vitest"]):
        cat = "pytest" if "pytest" in cmd_lower else "test"
        return cat, "testing", {"category": cat, "status": "running"}
    elif any(t in cmd_lower for t in ["grep", "rg ", "find ", "locate"]):
        return "search", "searching", None
    elif any(t in cmd_lower for t in ["git diff", "git status", "git log"]):
        return "command", "running_command", None
    return "command", "running_command", None


def normalize_claude_event(
    data: dict[str, Any],
    executor: str = "claude",
    model: str | None = None,
    current_phase: str = "implementing",
) -> ExecutorEvent | None:
    """Normalize Claude print-mode stream-json event line into ExecutorEvent."""
    if not isinstance(data, dict):
        return None

    current_phase = phase_for_step(current_phase)
    event_type = data.get("type")
    ts = _utc_now_iso()

    # Claude stream-json wraps tool calls in an assistant message on some
    # versions, while others emit content_block_start directly.  Normalize
    # both shapes without retaining text/reasoning payloads.
    if event_type == "assistant":
        content = ((data.get("message") or {}).get("content") or [])
        tool = next((item for item in content if isinstance(item, dict) and item.get("type") == "tool_use"), None)
        if tool:
            data = {"type": "tool_use", **tool}
            event_type = "tool_use"

    # Extract model if available
    if event_type == "message_start":
        msg = data.get("message") or {}
        extracted_model = msg.get("model") or model
        return ExecutorEvent(
            phase=current_phase,
            activity="planning",
            ts=ts,
            executor=executor,
            model=extracted_model,
        )

    # Tool invocation or content block
    if event_type in ("content_block_start", "tool_use"):
        block = data.get("content_block") or data
        block_type = block.get("type") or event_type
        tool_name = block.get("name") or data.get("name") or ""
        inputs = block.get("input") or data.get("input") or {}

        if block_type == "thinking":
            return ExecutorEvent(
                phase=current_phase,
                activity="thinking",
                ts=ts,
                executor=executor,
                model=model,
                tool_category="think",
            )

        if tool_name in ("Read", "View", "FileRead", "read_file"):
            filePath = bound_path(inputs.get("file_path") or inputs.get("path") or inputs.get("AbsolutePath"))
            return ExecutorEvent(
                phase=current_phase,
                activity="reading_file",
                ts=ts,
                executor=executor,
                model=model,
                tool_category="file_read",
                path=filePath,
            )
        elif tool_name in ("Edit", "Write", "FileWrite", "replace_file_content", "write_to_file"):
            filePath = bound_path(inputs.get("file_path") or inputs.get("path") or inputs.get("TargetFile"))
            return ExecutorEvent(
                phase=current_phase,
                activity="writing_file",
                ts=ts,
                executor=executor,
                model=model,
                tool_category="file_write",
                path=filePath,
            )
        elif tool_name in ("Bash", "Terminal", "Execute", "run_command"):
            cmd_str = str(inputs.get("command") or inputs.get("CommandLine") or "")
            category, activity, test_sum = parse_command_category(cmd_str)
            phase = "testing" if activity == "testing" else current_phase
            return ExecutorEvent(
                phase=phase,
                activity=activity,
                ts=ts,
                executor=executor,
                model=model,
                tool_category=category,
                test_summary=test_sum,
            )
        elif tool_name in ("Grep", "Glob", "Search"):
            return ExecutorEvent(
                phase=current_phase,
                activity="searching",
                ts=ts,
                executor=executor,
                model=model,
                tool_category="search",
            )
        elif tool_name:
            return ExecutorEvent(
                phase=current_phase,
                activity="tool_use",
                ts=ts,
                executor=executor,
                model=model,
                tool_category="other",
            )

    if event_type == "thinking":
        return ExecutorEvent(
            phase=current_phase,
            activity="thinking",
            ts=ts,
            executor=executor,
            model=model,
            tool_category="think",
        )

    if event_type == "result":
        usage = data.get("usage") or {}
        provider_usage = {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cost_usd": data.get("total_cost_usd"),
        }
        return ExecutorEvent(
            phase=phase_for_step(current_phase),
            activity="completed",
            ts=ts,
            executor=executor,
            model=model,
            provider_usage=provider_usage,
        )

    if event_type in ("assistant", "text"):
        return ExecutorEvent(
            phase=current_phase,
            activity="planning",
            ts=ts,
            executor=executor,
            model=model,
        )

    return None


def normalize_codex_event(
    data: dict[str, Any],
    executor: str = "codex",
    model: str | None = None,
    current_phase: str = "implementing",
) -> ExecutorEvent | None:
    """Normalize Codex exec --json event line into ExecutorEvent."""
    if not isinstance(data, dict):
        return None

    current_phase = phase_for_step(current_phase)
    event_type = data.get("type")
    ts = _utc_now_iso()

    if event_type in ("item.started", "item.completed"):
        item = data.get("item") or {}
        item_type = item.get("type")

        if item_type == "reasoning":
            return ExecutorEvent(
                phase=current_phase,
                activity="thinking",
                ts=ts,
                executor=executor,
                model=model,
                tool_category="think",
            )

        if item_type in ("tool_call", "command_execution", "file_change"):
            name = item.get("name") or item.get("tool") or ""
            cmd = item.get("command") or ""
            path = bound_path(item.get("path") or item.get("file"))

            if "read" in name or "view" in name:
                return ExecutorEvent(
                    phase=current_phase,
                    activity="reading_file",
                    ts=ts,
                    executor=executor,
                    model=model,
                    tool_category="file_read",
                    path=path,
                )
            elif item_type == "file_change" or "write" in name or "edit" in name or "modify" in name:
                return ExecutorEvent(
                    phase=current_phase,
                    activity="writing_file",
                    ts=ts,
                    executor=executor,
                    model=model,
                    tool_category="file_write",
                    path=path,
                )
            elif cmd or item_type == "command_execution":
                category, activity, test_sum = parse_command_category(cmd)
                phase = "testing" if activity == "testing" else current_phase
                if event_type == "item.completed" and test_sum:
                    exit_code = item.get("exit_code")
                    test_sum["status"] = "pass" if exit_code == 0 else "fail"
                return ExecutorEvent(
                    phase=phase,
                    activity=activity,
                    ts=ts,
                    executor=executor,
                    model=model,
                    tool_category=category,
                    test_summary=test_sum,
                )

    if event_type == "turn.completed":
        usage = data.get("usage") or {}
        return ExecutorEvent(
            phase=current_phase,
            activity="completed",
            ts=ts,
            executor=executor,
            model=model,
            provider_usage={
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
            },
        )

    return None


def normalize_executor_event(
    raw_line: str,
    executor: str = "unknown",
    model: str | None = None,
    current_phase: str = "implementing",
) -> ExecutorEvent | None:
    """Safely attempt parsing a raw JSON stream line into an ExecutorEvent."""
    if not raw_line or not raw_line.strip():
        return None

    try:
        data = json.loads(raw_line.strip())
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(data, dict):
        return None

    current_phase = phase_for_step(current_phase)
    if "claude" in executor:
        return normalize_claude_event(data, executor=executor, model=model, current_phase=current_phase)
    elif "codex" in executor:
        return normalize_codex_event(data, executor=executor, model=model, current_phase=current_phase)
    else:
        # Generic JSON event attempt
        if "type" in data:
            if "claude" in str(data.get("type")):
                return normalize_claude_event(data, executor=executor, model=model, current_phase=current_phase)
            elif "item" in data or "turn" in data:
                return normalize_codex_event(data, executor=executor, model=model, current_phase=current_phase)
            else:
                return ExecutorEvent(
                    phase=current_phase,
                    activity="tool_use",
                    ts=_utc_now_iso(),
                    executor=executor,
                    model=model,
                )
    return None


def fallback_heartbeat_event(
    executor: str,
    model: str | None = None,
    current_phase: str = "implementing",
    activity_age: float = 0.0,
    is_stall: bool = False,
) -> ExecutorEvent:
    """Create a clean fallback event for silent streams or unsupported executors."""
    activity = "stall" if is_stall else "heartbeat"
    return ExecutorEvent(
        phase=phase_for_step(current_phase),
        activity=activity,
        ts=_utc_now_iso(),
        activity_age=activity_age,
        executor=executor,
        model=model,
    )


def check_stall(last_event_ts: float, threshold_secs: float = 30.0) -> bool:
    """Return True if time since last event exceeds stall threshold."""
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    return (now - last_event_ts) > threshold_secs


def has_structured_progress(executor: str) -> bool:
    """Whether `executor` streams parseable per-turn JSON progress (see normalize_executor_event).

    Executors without one (aider, generic drivers) never advance last_activity
    while genuinely busy -- e.g. a single local-model generation can run
    several minutes with no output at all. Treating that silence as a stall
    is only meaningful for executors this returns True for.
    """
    return "claude" in executor or "codex" in executor
