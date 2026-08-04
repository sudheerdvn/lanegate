"""Executor pool selection/invocation: driver resolution, prompt dispatch,
worktree commit helpers.

TICK-255/TICK-276: extracted from orchestrate/__init__.py as pure code
movement -- see docs/internal/module-split-proposal.md.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from lanegate.config import ConfigError, resolve_model
from lanegate.executor import (
    _CLAUDE_SUBPROCESS_TYPES,
    build_executor_cmd,
    get_executor_config,
    parse_structured_result,
    resolve_executor_env,
    resolved_dispatch_metadata,
)
from lanegate.executor_events import (
    check_stall,
    fallback_heartbeat_event,
    has_structured_progress,
    normalize_executor_event,
    phase_for_step,
    redact_transcript_text,
)

from .audit import (
    _capture_executor_audit_bundle,
    _iso_from_epoch,
    _utc_now_iso,
    _write_json_atomic,
)
from .run_report import _append_run_event, _stream_subprocess
from .status import (
    _remove_executor_markers,
    _write_active_status,
    _write_executor_pid_marker,
    format_executor_event_status,
)

_DEFAULT_HEARTBEAT_SECONDS = 30.0

# Sentinel exit code returned by invoke_executor() when executor-env
# resolution (resolve_executor_env, TICK-088 named-instance api_key_env)
# raises ConfigError before any subprocess is even launched. Chosen to match
# BSD sysexits.h's EX_CONFIG so it reads as "configuration error" in logs,
# and to avoid colliding with 429 (rate limit) or any real subprocess exit
# code. Routing this through the ordinary nonzero-exit-code path lets both
# callers of invoke_executor (the main implement dispatch in _drain_loop and
# run_fix_agent's fix-pass dispatch) fail this one ticket via their existing
# "executor exited nonzero" handling instead of raising past them.
_CONFIG_ERROR_EXIT_CODE = 78


def _unpack_stream_result(result) -> tuple[int, str, str, str | None]:
    """Accept legacy three-value test doubles while using the four-value API."""
    if len(result) == 3:
        rc, stdout, stderr = result
        return rc, stdout, stderr, None
    return result


def resolve_driver(step: str, ticket: dict, cfg: dict) -> str:
    """Resolve the configured driver name for a pipeline step."""
    # A ticket's executor pins the code-writing route for the entire
    # implementation lifecycle.  In particular, review fixes must go back to
    # the implementer that owns the ticket, rather than falling through to a
    # project-wide ``steps.fix`` or global executor selection.
    if step in {"implement", "fix"} and ticket.get("executor"):
        return ticket["executor"]
    if step == "review" and ticket.get("reviewer"):
        return ticket["reviewer"]

    step_driver = ((cfg.get("steps") or {}).get(step) or {}).get("driver")
    if step_driver:
        return step_driver

    if step == "review" and cfg.get("reviewer"):
        return cfg["reviewer"]

    legacy_step_driver = (cfg.get("executor_steps") or {}).get(step)
    if legacy_step_driver:
        return legacy_step_driver

    if step == "review":
        return cfg.get("executor", "claude")
    return cfg.get("executor", "claude")


def expand_driver(driver_name: str, cfg: dict) -> dict:
    """Return a named driver config, or a legacy driver-type shim."""
    drivers = cfg.get("drivers") or {}
    if driver_name in drivers:
        return dict(drivers[driver_name])
    return {"type": driver_name}


def _expand_env_refs(value: str) -> str:
    """Expand ${VAR} references from the parent process environment."""
    return re.sub(r"\$\{([^}]+)\}", lambda match: os.environ.get(match.group(1), ""), value)


def _build_env(driver_cfg: dict, base_env: dict[str, str] | None = None) -> dict[str, str] | None:
    """Merge a driver's env overlay onto the resolved subprocess environment."""
    driver_env = driver_cfg.get("env")
    if driver_env is None:
        return base_env
    if not isinstance(driver_env, dict):
        raise ConfigError(f"driver env must be a mapping, got {type(driver_env).__name__}")
    if not driver_env:
        return base_env

    env = dict(base_env) if base_env is not None else dict(os.environ)
    for key, value in driver_env.items():
        if isinstance(value, str):
            env[str(key)] = _expand_env_refs(value)
        else:
            env[str(key)] = str(value)
    return env


def _cfg_with_driver_command_overrides(cfg: dict, executor: str, driver_cfg: dict) -> dict:
    """Expose driver bin/flags overrides through the existing executor command builder."""
    overrides = {
        key: driver_cfg[key]
        for key in ("bin", "flags", "base_url")
        if key in driver_cfg
    }
    if not overrides:
        return cfg

    effective_cfg = dict(cfg)
    executors = dict(cfg.get("executors") or {})
    existing = executors.get(executor)
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged.update(overrides)
    executors[executor] = merged
    effective_cfg["executors"] = executors
    return effective_cfg


def _resolve_driver_route(cfg: dict, ticket: dict | None = None) -> dict[str, str]:
    """Resolve implement/review drivers and the resulting execution mode."""
    ticket = ticket or {}
    implement = resolve_driver("implement", ticket, cfg)
    review = resolve_driver("review", ticket, cfg)
    return {
        "implement": implement,
        "review": review,
        "mode": "combined" if implement == review else "split",
    }


def _resolve_drift_driver_name(ticket: dict, cfg: dict) -> str:
    """Resolve drift checks through review unless a drift route is explicit.

    Drift checks audit a fix independently, so they use the same review route
    (including a ticket reviewer override) by default.  Either current
    ``steps.drift_check.driver`` or legacy ``executor_steps.drift_check`` can
    explicitly select a different drift executor.
    """
    drift_check_driver = ((cfg.get("steps") or {}).get("drift_check") or {}).get("driver")
    if drift_check_driver:
        return drift_check_driver
    drift_check_driver = (cfg.get("executor_steps") or {}).get("drift_check")
    if drift_check_driver:
        return drift_check_driver
    review_driver = resolve_driver("review", ticket, cfg)
    # ``human``/``none``/``auto-none`` are final-review gates, not executable
    # drift-check agents.  Preserve the established implementation-route
    # fallback for those no-agent review configurations.
    if review_driver not in {"human", "none", "auto-none"}:
        return review_driver
    return resolve_driver("implement", ticket, cfg)


def _write_prompt_file(worktree_path: Path, ticket_id: str, step: str, prompt: str) -> Path:
    """Persist the exact executor prompt for audit/replay without bloating argv."""
    prompts_dir = worktree_path / ".lanegate" / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = prompts_dir / f"{ticket_id}-{step}.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    return prompt_path


def write_prompt_file_best_effort(
    worktree_path: Path, ticket_id: str, step: str, prompt: str
) -> Path | None:
    """Persist an audit prompt without allowing an audit I/O error to abort work."""
    try:
        return _write_prompt_file(worktree_path, ticket_id, step, prompt)
    except Exception as exc:  # audit capture must never change the step outcome
        print(
            f"WARNING: could not write {step} prompt for {ticket_id}: {exc}",
            file=sys.stderr,
        )
        return None


def make_event_line_handler(
    repo_root: Path,
    session_ts: str | None,
    ticket_id: str,
    *,
    executor: str,
    model: str | None,
    step: str,
    terminal_stream=None,
    on_event=None,
):
    """Build an ``on_line`` callback that turns raw stdout into executor events.

    Every dispatch that streams stream-json needs the same three things done
    per line — normalize it, append it to the durable run event log, and show
    a formatted line instead of the raw JSON envelope.  Keeping one
    implementation is what lets the review and drift-check steps show up in
    ``GET /api/runs/<id>/events`` (and therefore the TUI) with no consumer
    change.  ``on_event`` is the per-caller extra: ``invoke_executor`` uses it
    to mirror progress into its live status file, which review-class steps do
    not maintain.
    """
    phase = phase_for_step(step)
    last_activity_ts = time.time()

    def handle_line(line: str, is_stdout: bool) -> None:
        nonlocal last_activity_ts
        if not is_stdout:
            return
        ev = normalize_executor_event(
            line, executor=executor, model=model, current_phase=phase
        )
        if not ev:
            return
        now = time.time()
        ev.activity_age = round(now - last_activity_ts, 1)
        last_activity_ts = now
        _append_run_event(
            repo_root,
            session_ts,
            "executor_progress",
            ticket_id=ticket_id,
            progress=ev.to_dict(),
        )
        if on_event is not None:
            on_event(ev)
        if terminal_stream is not None:
            terminal_stream.write(format_executor_event_status(ticket_id, ev) + "\n")
            terminal_stream.flush()

    def activity_probe() -> float:
        return last_activity_ts

    handle_line.activity_probe = activity_probe  # type: ignore[attr-defined]
    return handle_line


def _redact_for_audit(text: str) -> str:
    """Strip secret-shaped spans without truncating the transcript."""
    return redact_transcript_text(text or "") or ""


def capture_review_step_run(
    repo_root: Path,
    worktree_path: Path,
    ticket: dict,
    cfg: dict,
    *,
    step: str,
    executor: str,
    driver_name: str,
    model: str | None,
    session_id: str,
    prompt_path: Path | None,
    start_time: float,
    exit_code: int,
    captured_stdout: str = "",
    captured_stderr: str = "",
) -> Path | None:
    """Write an executor run directory for a review-class step.

    ``run_review_agent`` and ``run_drift_check`` do not go through
    ``invoke_executor``, so they had no run directory at all — a review verdict
    was three ticket fields and nothing else.  This produces the same bundle
    layout as an implement run so ``lanegate run-report``, ``lanegate logs`` and the
    TUI pick review runs up without special-casing.

    Returns ``None`` if capture fails.  Recording evidence about a review must
    never change that review's outcome: a full disk or an unreadable worktree
    would otherwise turn an approval into changes_requested.
    """
    finished_at = time.time()
    status = {
        "schema_version": 1,
        "ticket_id": ticket["id"],
        "executor": executor,
        "resolved_driver": driver_name,
        "resolved_executor": driver_name,
        "resolved_model": model,
        "executor_pid": None,
        "executor_session": session_id,
        "step": step,
        "mode": _resolve_driver_route(cfg, ticket)["mode"],
        "worktree": str(worktree_path),
        "log_path": None,
        "prompt_path": str(prompt_path) if prompt_path is not None else None,
        "started_at": start_time,
        "started_at_iso": _iso_from_epoch(start_time),
        "finished_at": finished_at,
        "finished_at_iso": _utc_now_iso(),
        "elapsed_seconds": int(finished_at - start_time),
        "exit_code": exit_code,
        "state": "finished",
        "last_event": "executor_finished",
        "reconciliation_state": "finished",
    }
    try:
        return _capture_executor_audit_bundle(
            repo_root,
            worktree_path,
            status,
            captured_stdout=_redact_for_audit(captured_stdout),
            captured_stderr=_redact_for_audit(captured_stderr),
        )
    except Exception as exc:  # pragma: no cover - defensive
        print(
            f"WARNING: could not capture {step} run directory for {ticket['id']}: {exc}",
            file=sys.stderr,
        )
        return None


def _invoke_ollama(prompt: str, driver_cfg: dict, worktree_path: Path) -> int:
    """Invoke Ollama REST API and write response to worktree."""
    base_url = driver_cfg.get("base_url", "http://localhost:11434")
    model = driver_cfg.get("model", "llama3.2")
    payload = {"model": model, "prompt": prompt, "stream": False}

    response_text = None
    try:
        import requests

        resp = requests.post(f"{base_url}/api/generate", json=payload, timeout=300)
        resp.raise_for_status()
        response_text = resp.json().get("response", "")
    except ImportError:
        # Fall back to curl subprocess when requests is not available
        import json as json_module

        payload_str = json_module.dumps(payload)
        result = subprocess.run(
            [
                "curl",
                "-s",
                "-X",
                "POST",
                f"{base_url}/api/generate",
                "-H",
                "Content-Type: application/json",
                "-d",
                payload_str,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            return 1
        try:
            response_text = json_module.loads(result.stdout).get("response", "")
        except (ValueError, KeyError):
            return 1
    except Exception:
        return 1

    # Write response to worktree for context
    out_file = worktree_path / ".ollama_response.md"
    out_file.write_text(response_text, encoding="utf-8")
    return 0


def _ticket_for_model_resolution(ticket: dict, executor_type: str) -> dict:
    """Drop Claude-only ticket model overrides when dispatching to non-Claude executors."""
    model = ticket.get("model")
    if (
        isinstance(model, str)
        and model.lower().startswith("claude-")
        and executor_type not in _CLAUDE_SUBPROCESS_TYPES
    ):
        without_model = dict(ticket)
        without_model.pop("model", None)
        return without_model
    return ticket


def resolve_dispatch(
    ticket: dict,
    cfg: dict,
    *,
    step: str = "implement",
    executor_override: str | None = None,
) -> dict:
    """Resolve the exact driver, executor instance, and model for a dispatch.

    ``executor_override`` is used by executor pools. It changes only the
    instance receiving the work; the displayed driver remains the configured
    route that selected the work, making a pool assignment observable without
    changing dispatch behaviour.
    """
    configured_driver = resolve_driver(step, ticket, cfg)
    dispatch_target = executor_override if executor_override is not None else configured_driver
    driver_cfg = expand_driver(dispatch_target, cfg)
    executor = driver_cfg.get("type", dispatch_target)
    executor_cfg = get_executor_config(executor, cfg)
    executor_type = executor_cfg.get("type", executor)
    effective_cfg = dict(cfg, executor=executor) if executor != cfg.get("executor") else cfg
    model_ticket = _ticket_for_model_resolution(ticket, executor_type)
    model = driver_cfg.get("model") or resolve_model(effective_cfg, step, ticket=model_ticket)

    # A named driver maps to its underlying executor type, while a named
    # executor (including a pool assignment) remains visible as that instance.
    drivers = cfg.get("drivers") or {}
    visible_executor = (
        dispatch_target
        if executor_override is not None or configured_driver not in drivers
        else executor_type
    )
    result = {
        "driver_cfg": driver_cfg,
        "dispatch_target": dispatch_target,
        "executor": executor,
        "model": model,
    }
    result.update(
        resolved_dispatch_metadata(
            driver=configured_driver,
            executor=visible_executor,
            model=model,
        )
    )
    return result


def invoke_executor(
    ticket: dict,
    cfg: dict,
    worktree_path: Path,
    *,
    log_stream=None,
    terminal_stream=None,
    prompt_override: str | None = None,
    step: str = "implement",
    repo_root: Path | None = None,
    executor_override: str | None = None,
) -> tuple[int, str]:
    """
    Run the configured executor on a ticket in its worktree.

    Args:
        ticket: The ticket dict.
        cfg: loaded config dict.
        worktree_path: Path to the ticket's git worktree.
        log_stream: When provided, executor stdout/stderr are routed only to
            this stream (compact/non-verbose mode).  When None, output goes
            through sys.stdout/sys.stderr as usual (verbose mode).
        terminal_stream: When provided, heartbeat lines are also written here
            (the real terminal stream) so long-running tickets show progress
            in compact mode.  Ignored when None or in verbose mode.
        prompt_override: When provided, use this prompt instead of the one
            built from the ticket (used for combined-mode prompts that include
            review instructions appended after the implement prompt, and for
            the fix-agent prompt built by ``run_fix_agent``).
        step: Pipeline step this invocation is for — resolves the driver
            and model via ``steps.<step>.driver``/``models.<step>`` and
            labels the persisted prompt file. Defaults to "implement" so
            existing call sites are unaffected.
        repo_root: Repository root for orchestration status metadata. Defaults
            to worktree_path for direct/unit-test invocations.
        executor_override: When provided, use this named executor instance
            instead of resolving one via resolve_driver(). Used by pool
            dispatch (TICK-089) to route a specific ticket to a specific pool
            instance without writing an arbitrary instance name onto the
            ticket's own frontmatter (which would trip the pre-existing
            validate_ticket gap described in TICK-247).

    Returns (exit_code, captured_stdout, captured_stderr) tuple.
    """
    from lanegate.executor import build_implement_prompt
    from lanegate.orchestrate import _pid_alive

    status_root = repo_root if repo_root is not None else worktree_path
    status_root = Path(status_root)
    # cmd_orchestrate writes the last-run pointer before it dispatches any
    # workers.  Resolving it here preserves invoke_executor's long-standing
    # callback signature while associating progress with the durable run.
    from lanegate.orchestrate.run_report import _resolve_run_session_ts

    session_ts = _resolve_run_session_ts(status_root, None)

    prompt = (
        prompt_override
        if prompt_override is not None
        else build_implement_prompt(ticket, project_root=worktree_path, cfg=cfg)
    )
    prompt_path = _write_prompt_file(worktree_path, ticket["id"], step, prompt)

    touches = list(ticket.get("touches") or [])
    # Resolve executor (bare type or named instance, TICK-088) once so the
    # same instance config drives both cmd construction and env injection.
    try:
        dispatch = resolve_dispatch(ticket, cfg, step=step, executor_override=executor_override)
        driver_cfg = dispatch["driver_cfg"]
        executor = dispatch["executor"]
        executor_cfg = get_executor_config(executor, cfg)
        executor_type = executor_cfg.get("type", executor)
        # Resolve the model for this ticket's step after knowing the effective
        # executor type. Per-ticket model overrides from old analysis runs are
        # executor-specific in practice: a Claude model name should not be
        # passed to Codex just because a heterogeneous pool selected Codex.
        model = dispatch["model"]
        # Special handling for ollama: REST API dispatch, no subprocess path.
        if executor_type == "ollama":
            rc = _invoke_ollama(prompt, driver_cfg, worktree_path)
            if log_stream is not None:
                log_stream.write(f"[orchestrate] {ticket['id']} ollama finished (exit {rc})\n")
                log_stream.flush()
            return rc, "", ""
        executor_env = resolve_executor_env(executor_cfg)
        executor_env = _build_env(driver_cfg, base_env=executor_env)
        command_cfg = _cfg_with_driver_command_overrides(cfg, executor, driver_cfg)
        # TICK-188/TICK-310: thread a prior step's CLI session into this one
        # via --resume so the pipeline continues one conversation rather than
        # every step starting cold (the fixed per-invocation bootstrap cost
        # only needs to be cache-written once per session, not once per
        # process -- see docs/internal/session-usage-investigation.md). implement
        # resumes analyze's session; fix resumes its own prior fix pass if an
        # autofix cycle already ran once, else implement's. Gated by
        # resume_session_gate() so a stale/oversized session falls back to a
        # fresh dispatch instead of re-paying for the whole accumulated
        # history at cache-write price. A session is provider/account/model
        # specific: do not pass an Agy/Gemini or Claude conversation to a
        # Codex process merely because a pool chose a different executor for
        # the next step. Legacy tickets without origin metadata start fresh.
        if step == "implement":
            resume_candidate = ticket.get("analyze_session_id")
            resume_origin = "analyze"
        elif step == "fix":
            resume_origin = "fix" if ticket.get("fix_session_id") else "implement"
            resume_candidate = ticket.get(f"{resume_origin}_session_id")
        else:
            resume_candidate = None
            resume_origin = None
        resume_session_id = None
        if resume_candidate:
            origin_executor = ticket.get(f"{resume_origin}_session_executor")
            origin_model = ticket.get(f"{resume_origin}_session_model")
            if origin_executor != executor or origin_model != model:
                reason = "session origin does not match selected executor/model"
            else:
                from lanegate.context_log import (
                    _get_default_db_path,
                    _get_project_id,
                    resume_session_gate,
                )

                allowed, reason = resume_session_gate(
                    cfg, _get_default_db_path(), _get_project_id(status_root), resume_candidate
                )
                if allowed:
                    resume_session_id = resume_candidate
            if resume_session_id is None and log_stream is not None:
                log_stream.write(
                    f"[orchestrate] {ticket['id']}: not resuming session for {step} — {reason}\n"
                )
                log_stream.flush()
        prompt_stdin = None
        if executor_type in (_CLAUDE_SUBPROCESS_TYPES | {"codex", "ollama"}):
            cmd = build_executor_cmd(
                executor, prompt, command_cfg, model=model, touches=touches,
                analyze_session_id=resume_session_id,
                worktree_path=worktree_path,
                use_stdin=True,
            )
            prompt_stdin = prompt
        else:
            cmd = build_executor_cmd(
                executor, prompt, command_cfg, model=model, touches=touches,
                analyze_session_id=resume_session_id,
                worktree_path=worktree_path,
            )
    except ConfigError as exc:
        # Fail this one ticket via the ordinary nonzero-exit-code path rather
        # than letting a bad named-executor config (e.g. api_key_env pointing
        # at an unset var, a type with no known key-injection target, or a
        # malformed driver env overlay), or a missing executor binary,
        # propagate past invoke_executor and crash the whole orchestrate run
        # or run_fix_agent's caller.
        msg = (
            f"[orchestrate] {ticket['id']}: executor configuration failed for "
            f"'{executor}': {exc}\n"
        )
        if log_stream is not None:
            log_stream.write(msg)
            log_stream.flush()
        else:
            sys.stderr.write(msg)
        return _CONFIG_ERROR_EXIT_CODE, "", ""
    if log_stream is not None:
        log_stream.write(f"[orchestrate] prompt file: {prompt_path}\n")
        log_stream.flush()

    heartbeat_stop = threading.Event()
    start_time = time.time()
    tid = ticket["id"]
    session_id = f"{tid}-{int(start_time)}-{os.getpid()}-{step}"
    log_path = str(getattr(log_stream, "name", "")) if log_stream is not None else None
    base_status = {
        "schema_version": 1,
        "ticket_id": tid,
        "executor": executor,
        **{
            key: dispatch[key]
            for key in ("resolved_driver", "resolved_executor", "resolved_model")
        },
        "executor_pid": None,
        "executor_session": session_id,
        "step": step,
        # Every step's status.json reports the resolved execution mode, so a
        # combined-mode self-review is identifiable from the bundle rather
        # than inferable from a missing review_model.
        "mode": _resolve_driver_route(cfg, ticket)["mode"],
        "worktree": str(worktree_path),
        "log_path": log_path,
        "prompt_path": str(prompt_path),
        "started_at": start_time,
        "started_at_iso": _utc_now_iso(),
        "last_heartbeat_at": None,
        "heartbeat_count": 0,
        "last_event": "executor_launching",
        "state": "running",
        "reconciliation_state": "pending",
    }
    status_lock = threading.Lock()
    current_status = _write_active_status(status_root, base_status, session_id=session_id)
    # Keep liveness separate from parsed executor activity: a heartbeat proves
    # the child process still exists, but does not claim it made progress.
    last_verified_heartbeat_ts: float | None = None

    def update_status(**changes) -> dict:
        nonlocal current_status
        with status_lock:
            payload = dict(current_status)
            payload.update(changes)
            current_status = _write_active_status(status_root, payload, session_id=session_id)
            return current_status

    def on_process_start(pid: int) -> None:
        nonlocal last_verified_heartbeat_ts
        _write_executor_pid_marker(status_root, tid, pid, start_time)
        last_verified_heartbeat_ts = time.time()
        update_status(
            executor_pid=pid,
            last_event="executor_started",
            reconciliation_state="live",
        )

    # Report the concrete executor instance, not merely a named driver alias:
    # this is the identifier an operator can use to understand pool activity.
    resolved_driver = str(dispatch.get("resolved_executor") or executor)
    resolved_model = dispatch.get("resolved_model")
    event_phase = phase_for_step(step)

    def _mirror_event_into_status(ev) -> None:
        update_status(
            last_executor_event=ev.to_dict(),
            last_event="executor_progress",
            phase=ev.phase,
            activity=ev.activity,
        )

    handle_line = make_event_line_handler(
        repo_root,
        session_ts,
        tid,
        executor=resolved_driver,
        model=resolved_model,
        step=step,
        terminal_stream=terminal_stream,
        on_event=_mirror_event_into_status,
    )
    last_activity = handle_line.activity_probe

    heartbeat_seconds = float(cfg.get("executor_heartbeat_seconds", _DEFAULT_HEARTBEAT_SECONDS))
    if heartbeat_seconds <= 0:
        heartbeat_seconds = _DEFAULT_HEARTBEAT_SECONDS

    executor_streams_progress = has_structured_progress(resolved_driver)

    def _heartbeat():
        nonlocal last_verified_heartbeat_ts
        while not heartbeat_stop.wait(heartbeat_seconds):
            elapsed = int(time.time() - start_time)
            # An executor with no structured progress stream (aider, generic
            # drivers) never advances last_activity while genuinely busy -- a
            # single local-model generation can run minutes with no output at
            # all. Silence alone is only a meaningful stall signal for
            # executors that do stream progress; for the rest, fall back to
            # a plain heartbeat so an operator doesn't read routine work as
            # a hang.
            is_stall = executor_streams_progress and check_stall(last_activity(), threshold_secs=30.0)
            fb_ev = fallback_heartbeat_event(
                executor=resolved_driver,
                model=resolved_model,
                current_phase=event_phase,
                activity_age=round(time.time() - last_activity(), 1),
                is_stall=is_stall,
            )
            heartbeat_at = time.time()
            pid_is_live = (
                isinstance(current_status.get("executor_pid"), int)
                and _pid_alive(current_status["executor_pid"])
            )
            if pid_is_live:
                last_verified_heartbeat_ts = heartbeat_at
            status = update_status(
                last_heartbeat_at=heartbeat_at,
                last_heartbeat_at_iso=_utc_now_iso(),
                heartbeat_count=int(current_status.get("heartbeat_count") or 0) + 1,
                elapsed_seconds=elapsed,
                last_event="executor_heartbeat",
                last_executor_event=fb_ev.to_dict(),
                reconciliation_state="live" if pid_is_live else "stale",
            )
            _append_run_event(
                repo_root,
                session_ts,
                "executor_progress",
                ticket_id=tid,
                progress=fb_ev.to_dict(),
            )
            # Heartbeats use the same public event contract as real progress,
            # rather than reverting compact mode to a process-only message.
            hb_line = format_executor_event_status(tid, fb_ev) + "\n"
            if log_stream is not None:
                log_stream.write(hb_line)
                log_stream.flush()
            if terminal_stream is not None:
                terminal_stream.write(hb_line)
                terminal_stream.flush()

    def last_verified_heartbeat() -> float | None:
        return last_verified_heartbeat_ts

    hb = threading.Thread(target=_heartbeat, daemon=True)
    hb.start()
    captured_stdout = ""
    captured_stderr = ""
    exec_timeout = driver_cfg.get("timeout_s") or cfg.get("executor_timeout_seconds", 1800)
    if exec_timeout is not None:
        exec_timeout = float(exec_timeout)
    # Output quietness alone is not a liveness signal. A fresh heartbeat keeps
    # a live executor past the short idle threshold; only a much longer lack
    # of parsed progress triggers the semantic stall cutoff.
    streaming_capable = executor_type in (_CLAUDE_SUBPROCESS_TYPES | {"codex"})
    stream_kwargs = (
        {
            "idle_timeout": float(cfg.get("executor_idle_timeout_seconds", 75)),
            "stall_timeout": float(cfg.get("executor_stall_timeout_seconds", 900)),
            "absolute_ceiling": float(cfg.get("executor_absolute_ceiling_seconds", 1500)),
            "liveness_probe": last_verified_heartbeat,
            "progress_probe": last_activity,
        }
        if streaming_capable
        else {"timeout": exec_timeout}
    )
    try:
        if log_stream is not None:
            rc, captured_stdout, captured_stderr, _kill_reason = _unpack_stream_result(_stream_subprocess(
                cmd,
                str(worktree_path),
                out_stream=log_stream,
                err_stream=log_stream,
                stdin_text=prompt_stdin,
                on_start=on_process_start,
                env=executor_env,
                on_line=handle_line,
                **stream_kwargs,
            ))
        else:
            rc, captured_stdout, captured_stderr, _kill_reason = _unpack_stream_result(_stream_subprocess(
                cmd,
                str(worktree_path),
                stdin_text=prompt_stdin,
                on_start=on_process_start,
                env=executor_env,
                on_line=handle_line,
                **stream_kwargs,
            ))
    finally:
        heartbeat_stop.set()
        hb.join(timeout=1)
    elapsed = int(time.time() - start_time)
    final_status = update_status(
        state="finished",
        active=False,
        exit_code=rc,
        elapsed_seconds=elapsed,
        finished_at=time.time(),
        finished_at_iso=_utc_now_iso(),
        last_event="executor_finished",
        reconciliation_state="finished",
    )
    _remove_executor_markers(status_root, tid)
    audit_bundle = _capture_executor_audit_bundle(
        status_root,
        worktree_path,
        final_status,
        log_stream=log_stream,
        captured_stdout=captured_stdout,
        captured_stderr=captured_stderr,
    )
    final_status = update_status(audit_bundle_path=str(audit_bundle))
    _write_json_atomic(audit_bundle / "status.json", final_status)

    parsed = parse_structured_result(executor_type, captured_stdout)
    if parsed is not None:
        from lanegate.context_log import record_step_cost

        record_step_cost(status_root, tid, step, executor, model, parsed)

    # TICK-310: persist this call's session id so a later step (fix
    # resuming implement; a second fix pass resuming the first) knows
    # what to --resume. Only implement/fix originate a resumable chain
    # today -- analyze has its own session-capture path (analyze.py),
    # and review/drift_check are handled in review.py/autofix.py.
    #
    # The executor/model identity below is recorded regardless of whether
    # a structured parser (and therefore a resumable session id) exists --
    # only claude/codex/agy have one (see parse_structured_result). Gating
    # this on `parsed is not None` left aider/ollama/openhands/etc.
    # implementers with no recorded identity at all, so review.py's
    # self-review detection (_implementer_identity) couldn't see who
    # implemented and a same-instance review got silently mislabeled
    # "independent" instead of "self".
    if step in ("implement", "fix") and "_path" in ticket:
        from lanegate.ticket import parse_ticket, write_ticket

        # Reload from disk rather than reusing the in-memory `ticket` passed
        # into this call: a combined-mode agent runs as this same subprocess
        # and may have already called `lanegate complete`/`lanegate review`
        # itself, updating status/verdict fields on disk that this function's
        # stale pre-dispatch snapshot doesn't have. Writing that snapshot
        # back would silently discard the agent's own self-reported update.
        fresh_ticket = parse_ticket(ticket["_path"]) or ticket
        new_session_id = parsed.get("session_id") if parsed is not None else None
        if new_session_id:
            fresh_ticket[f"{step}_session_id"] = new_session_id
        # resolved_driver, not the bare `executor` type: a pool of
        # same-type instances (e.g. aider-14b/aider-7b both type "aider")
        # needs the specific instance name recorded, or review.py's
        # self-review detection can't tell two different pool instances
        # apart from one instance reviewing its own work.
        fresh_ticket[f"{step}_session_executor"] = resolved_driver
        if model:
            fresh_ticket[f"{step}_session_model"] = model
        else:
            fresh_ticket.pop(f"{step}_session_model", None)
        write_ticket(fresh_ticket)

    if log_stream is not None:
        log_stream.write(f"[orchestrate] {tid} executor finished (exit {rc}, {elapsed}s elapsed)\n")
        log_stream.flush()
    return rc, captured_stdout, captured_stderr


def _committed_files(worktree_path: Path) -> set[str]:
    """Return the set of files changed on this branch relative to main.

    Uses ``git diff --name-only <trunk>...HEAD`` (three-dot form) to list all files
    that differ between the common ancestor of the trunk and HEAD and the current HEAD.
    This captures every file the branch touched, regardless of how many commits.

    Fail-open: if the git command fails, returns an empty set so that a broken
    worktree does not falsely trigger the out-of-scope guard.
    """
    from lanegate.config import load_config, resolve_trunk_branch

    trunk_branch = resolve_trunk_branch(load_config(worktree_path), worktree_path)
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{trunk_branch}...HEAD"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
        )
    except OSError:
        return set()
    if result.returncode != 0:
        return set()
    return {
        line.strip()
        for line in result.stdout.splitlines()
        # LaneGate's own worktree-local artifacts (prompt files, fix-pass status)
        # are never scope drift, even if a stray commit swept them in before
        # commit_worktree_changes started excluding .lanegate/ from `git add`.
        if line.strip() and not line.strip().startswith(".lanegate/")
    }


def check_worktree_has_commits(worktree_path: Path) -> bool:
    """Return True if the worktree branch has at least one commit ahead of the trunk.

    Runs ``git log <trunk>..HEAD --oneline`` inside the worktree.  Any output
    (one or more lines) means at least one file was committed; empty output
    means the branch is identical to the trunk (no implementation work committed).

    Raises FileNotFoundError if the worktree directory does not exist. Callers
    must handle this explicitly (e.g., mark ticket needs_review rather than
    treating a missing worktree as "no commits").

    Fail-closed: if the git command itself fails (but worktree exists), returns
    False so that a broken repository is treated as no-commits rather than
    silently approved.
    """
    if not worktree_path.exists():
        raise FileNotFoundError(f"worktree directory does not exist: {worktree_path}")
    from lanegate.config import load_config, resolve_trunk_branch

    trunk_branch = resolve_trunk_branch(load_config(worktree_path), worktree_path)
    result = subprocess.run(
        ["git", "log", f"{trunk_branch}..HEAD", "--oneline"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    return bool(result.stdout.strip())


def commit_worktree_changes(
    worktree_path: Path, ticket_id: str, message: str | None = None
) -> bool:
    """Commit executor-produced worktree edits, if any.

    Some executors return successfully after editing files but do not create a
    git commit.  LaneGate owns the ticket branch, so the orchestrator can make that
    final implementation commit before the review/completion gates run.

    Args:
        message: Commit message to use. Defaults to "feat: implement
            <ticket_id>" (the implement-step message); callers committing a
            fix pass should pass a distinct message so the commit log doesn't
            mislabel a fix as the original implementation.
    """
    if not worktree_path.exists():
        return False

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
    )
    if status.returncode != 0 or not status.stdout.strip():
        return False

    add = subprocess.run(
        # Exclude .lanegate/ so LaneGate's own audit/prompt artifacts (written inside
        # the worktree by _write_prompt_file and fix-pass status files) never
        # land on the ticket branch, regardless of whether the project's
        # committed .gitignore covers .lanegate/ yet.
        ["git", "add", "-A", "--", ".", ":(exclude).lanegate/**"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
    )
    if add.returncode != 0:
        return False

    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
    )
    if staged.returncode == 0:
        return False
    elif staged.returncode != 1:
        return False

    commit = subprocess.run(
        ["git", "commit", "-m", message or f"feat: implement {ticket_id}"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
    )
    return commit.returncode == 0
