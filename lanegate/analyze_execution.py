"""Analysis execution state, visibility, and model invocation."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from lanegate.analyze import (
    _ACTIVE_ANALYSIS_FILE,
    _CLAUDE_EXECUTORS,
    _CLAUDE_MODEL_PREFIXES,
    _MAX_LOGGED_EXECUTOR_OUTPUT,
    _SESSION_EXECUTORS,
)


class ExecutorCallError(RuntimeError):
    """Executor subprocess exited non-zero during analyze.

    Carries the *unclipped* stdout/stderr alongside the display message.
    The message itself goes through _summarize_executor_output, which clips
    every line to 240 chars — and stream-json executors (the Claude CLI)
    emit their whole transcript as a handful of enormous single lines, so a
    quota notice like "Claude AI usage limit reached" lands far past that
    boundary. Classifying rate limits off str(exc) therefore silently missed
    every stream-json quota error and skipped pool failover; callers must
    classify against these raw fields instead.
    """

    def __init__(self, message: str, *, raw_stdout: str = "", raw_stderr: str = ""):
        super().__init__(message)
        self.raw_stdout = raw_stdout
        self.raw_stderr = raw_stderr


def _summarize_executor_output(text: str, *, max_lines: int = 12, max_line_len: int = 240) -> str:
    """Return a compact error summary without dumping prompts or transcripts."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return ""

    interesting = [
        line
        for line in lines
        if re.search(
            r"error|failed|invalid|quota|rate limit|too many requests|traceback", line, re.I
        )
    ]
    selected = interesting[:max_lines] if interesting else lines[-max_lines:]

    clipped: list[str] = []
    for line in selected:
        if len(line) > max_line_len:
            line = line[: max_line_len - 3] + "..."
        clipped.append(line)
    return "\n".join(clipped)


def _bounded_executor_output(text: str, *, max_bytes: int = _MAX_LOGGED_EXECUTOR_OUTPUT) -> str:
    """Keep executor evidence actionable without allowing unbounded log growth."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="replace") + "\n... [truncated]"


def _active_analysis_path(repo_root: Path) -> Path:
    return repo_root / ".lanegate" / _ACTIVE_ANALYSIS_FILE


def _write_active_analysis(
    repo_root: Path,
    *,
    ticket_id: str,
    phase: str,
    executor: str,
    model: str | None,
    started_at: float,
    log_file: Path,
) -> None:
    """Atomically update the active standalone-analysis record for API/TUI use."""
    path = _active_analysis_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        log_path = log_file.relative_to(repo_root).as_posix()
    except ValueError:
        log_path = str(log_file)
    payload = {
        "ticket_id": ticket_id,
        "phase": phase,
        "executor": executor,
        "model": model or "default",
        "started_at": started_at,
        "log_path": log_path,
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def get_active_analysis_status(repo_root: Path) -> dict | None:
    """Return the public active-analysis payload, or ``None`` when inactive."""
    path = _active_analysis_path(repo_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        started_at = float(data["started_at"])
    except (FileNotFoundError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    return {
        "ticket_id": str(data.get("ticket_id", "")),
        "phase": str(data.get("phase", "unknown")),
        "executor": str(data.get("executor", "unknown")),
        "model": str(data.get("model", "default")),
        "elapsed_seconds": max(0, int(time.time() - started_at)),
        "log_path": str(data.get("log_path", "")),
    }


def _clear_active_analysis(repo_root: Path) -> None:
    try:
        _active_analysis_path(repo_root).unlink()
    except FileNotFoundError:
        pass


def _estimate_prompt_tokens(prompt: str) -> int:
    """A deliberately conservative prompt-token estimate for operator feedback."""
    return max(1, (len(prompt) + 2) // 3)


class _WaitingReporter:
    """Emit periodic elapsed-time updates while the synchronous executor runs."""

    def __init__(self, emit, started_at: float, interval: float) -> None:
        self._emit = emit
        self._started_at = started_at
        self._interval = max(0.01, interval)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._emit(f"Waiting for model... (elapsed {int(time.time() - self._started_at)}s)")


class _AnalysisVisibility:
    """Keep terminal, log, and active-status lifecycle views in lockstep."""

    def __init__(self, repo_root: Path, ticket_id: str) -> None:
        from lanegate.logs import analyze_log_path, write_analysis_event

        self.repo_root = repo_root
        self.ticket_id = ticket_id
        self.started_at = time.time()
        self.log_file = analyze_log_path(repo_root)
        self._write_event = write_analysis_event
        self.executor = "resolving"
        self.model: str | None = None
        self._update("starting")
        self._write_event(self.log_file, "starting", "Standalone analysis started")

    def _update(self, phase: str) -> None:
        _write_active_analysis(
            self.repo_root,
            ticket_id=self.ticket_id,
            phase=phase,
            executor=self.executor,
            model=self.model,
            started_at=self.started_at,
            log_file=self.log_file,
        )

    def set_driver(self, executor: str, model: str | None) -> None:
        self.executor = executor
        self.model = model

    def emit(self, phase: str, message: str, *, error: bool = False) -> None:
        print(f"[analyze] {message}", file=sys.stderr if error else sys.stdout, flush=True)
        self._write_event(self.log_file, phase, message)
        self._update(phase)

    def executor_output(self, text: str) -> None:
        self._write_event(self.log_file, "executor_output", _bounded_executor_output(text))

    def cleanup(self) -> None:
        _clear_active_analysis(self.repo_root)


def _call_model(
    prompt: str,
    model: str | None = None,
    executor: str = "claude",
    cfg: dict | None = None,
    driver_cfg: dict | None = None,
    repo_root: Path | None = None,
    tid: str | None = None,
) -> tuple[str, str | None]:
    """Call the configured executor with prompt; return (raw text response, session_id or None).

    build_executor_cmd already decides whether to request structured output
    (``--output-format json`` for Claude types, ``--json`` for Codex) based on
    the executor's *resolved* type, unconditionally, regardless of caller —
    so unwrap here via the same ``parse_structured_result`` registry used by
    review/autofix/pool dispatch (see executor.py), keyed on that same
    resolved type. Adding a new structured executor (e.g. Gemini CLI) is one
    flag change in build_executor_cmd plus one parser + one registry entry in
    executor.py; nothing here needs to change. Executors with no registered
    parser (aider, ollama, openhands, plain) get None back and raw stdout is
    used as-is.

    ``repo_root``/``tid`` are optional purely so this can still be called
    without them (e.g. a future non-ticket caller); when both are supplied
    and the executor's output parsed to a structured envelope, the dispatch
    cost is recorded the same way review/implement/fix already do -- analyze
    previously parsed this same envelope only to read session_id/result_text
    and threw the usage/cost fields away, leaving it the one step invisible
    to ``context-stats``.
    """
    from lanegate.executor import (
        _CLAUDE_SUBPROCESS_TYPES,
        build_executor_cmd,
        executor_types_with,
        get_executor_config,
        parse_structured_result,
        resolve_executor_env,
        run_executor_subprocess,
    )
    from lanegate.orchestrate import _build_env, _cfg_with_driver_command_overrides

    base_cfg = cfg or {}
    effective_driver_cfg = driver_cfg or {}
    command_cfg = _cfg_with_driver_command_overrides(base_cfg, executor, effective_driver_cfg)
    resolved_executor_cfg = get_executor_config(executor, base_cfg)
    executor_env = resolve_executor_env(resolved_executor_cfg)
    executor_env = _build_env(effective_driver_cfg, base_env=executor_env)
    resolved_executor_type = resolved_executor_cfg.get("type", executor)

    use_stdin = resolved_executor_type in executor_types_with("stdin_capable")
    # Analyze must stay read-only: the prompt carries candidate-file skeletons
    # (see _build_prompt) so touches/change_notes precision doesn't depend on
    # the model reading real files itself, and denying edit capability here
    # closes the gap where the executor's own default full-access flags
    # (--dangerously-skip-permissions, --yes-always, --dangerously-bypass-
    # approvals-and-sandbox) would otherwise leave analyze free to write --
    # at a draft ticket, before any worktree exists, directly against the
    # main checkout. disallowed_tools is Claude's own mechanism
    # (--disallowedTools); read_only=True covers every other executor type
    # via build_executor_cmd's per-type read-only flag (aider --dry-run,
    # codex --sandbox read-only, agy --mode plan).
    disallowed_tools = ["Bash", "Write", "Edit"] if resolved_executor_type in _CLAUDE_SUBPROCESS_TYPES else None
    cmd = build_executor_cmd(
        executor, prompt, command_cfg, model=model, use_stdin=use_stdin,
        disallowed_tools=disallowed_tools,
        read_only=True,
        step="analyze",
    )

    start_time = time.time()
    result = run_executor_subprocess(
        resolved_executor_type, cmd,
        capture_output=True,
        text=True, encoding="utf-8",
        env=executor_env,
        # Executors whose prompt is already on argv must not inherit
        # LaneGate's stdin.  In particular, kiro-cli may probe stdin even in
        # --no-interactive mode; an open pipe owned by LaneGate's caller then
        # never reaches EOF and leaves `analyze` waiting indefinitely.
        stdin=None if use_stdin else subprocess.DEVNULL,
        input=prompt if use_stdin else None,
    )
    if result.returncode != 0:
        cmd_label = " ".join(cmd[:2]) if len(cmd) > 1 and cmd[1] == "exec" else cmd[0]
        details = _summarize_executor_output(result.stderr or result.stdout)
        suffix = f": {details}" if details else ""
        raise ExecutorCallError(
            f"{cmd_label} failed (exit {result.returncode}){suffix}",
            raw_stdout=result.stdout or "",
            raw_stderr=result.stderr or "",
        )

    raw = result.stdout.strip()
    session_id: str | None = None
    parsed = parse_structured_result(resolved_executor_type, raw)
    if parsed is not None:
        session_id = parsed.get("session_id") or None
        raw = parsed.get("result_text", raw)
        if repo_root is not None and tid is not None:
            from lanegate.context_log import record_step_cost

            record_step_cost(
                repo_root, tid, "analyze", executor, model, parsed,
                dispatch_start_time=start_time,
            )

    return raw, session_id
