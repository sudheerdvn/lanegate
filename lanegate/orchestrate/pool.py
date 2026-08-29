"""Executor pool selection/invocation: driver resolution, prompt dispatch,
worktree commit helpers.

Extracted from orchestrate module as pure code
movement -- see docs/internal/module-split-proposal.md.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from lanegate import APP_NAME
from lanegate.budget import DispatchMeter, metering_supported_for
from lanegate.config import (
    CONFIG_FILENAME,
    ConfigError,
    resolve_model,
    resolve_trunk_branch,
    validate_model_for_executor,
)
from lanegate.executor import (
    _CLAUDE_SUBPROCESS_TYPES,
    _SESSION_RESUME_TYPES,
    _check_aider_parser_rejection,
    build_executor_cmd,
    executor_types_with,
    get_executor_config,
    parse_structured_result,
    reject_ollama_for_code_step,
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
from lanegate.lifecycle.touches import check_touches_compliance
from lanegate.reviewer import get_worktree_diff
from lanegate.ticket import branch_name

from .audit import (
    _capture_executor_audit_bundle,
    _iso_from_epoch,
    _load_audit_manifest,
    _manifest_capture,
    _run_git_snapshot,
    _save_audit_manifest,
    _utc_now_iso,
    _write_bounded_text,
    _write_json_atomic,
)
from .run_report import INTERNAL_RUN_ENV, _append_run_event, _stream_subprocess
from .status import (
    _remove_executor_markers,
    _write_active_status,
    _write_executor_pid_marker,
    format_executor_event_status,
)

_DEFAULT_HEARTBEAT_SECONDS = 30.0

# Sentinel exit code returned by invoke_executor() when executor-env
# resolution (resolve_executor_env, named-instance api_key_env)
# raises ConfigError before any subprocess is even launched. Chosen to match
# BSD sysexits.h's EX_CONFIG so it reads as "configuration error" in logs,
# and to avoid colliding with 429 (rate limit) or any real subprocess exit
# code. Routing this through the ordinary nonzero-exit-code path lets both
# callers of invoke_executor (the main implement dispatch in _drain_loop and
# run_fix_agent's fix-pass dispatch) fail this one ticket via their existing
# "executor exited nonzero" handling instead of raising past them.
_CONFIG_ERROR_EXIT_CODE = 78

# Substrings that mark a nonzero exit as "the --resume session id was
# rejected / stale" rather than a genuine task failure. When one of these
# appears with an active resume_session_id, invoke_executor retries once
# without --resume (losing the prior conversation context but letting the
# ticket proceed). Kept deliberately anchored on resume/session wording so a
# task that merely failed on its own merits is not silently retried at
# double cost. codex's exact string is listed first; the rest are the
# generic shapes cursor-agent / agy / claude emit for an unknown session.
_RESUME_REJECTION_MARKERS = (
    "thread/resume failed: no rollout found for thread id",
    "no rollout found",
    "session not found",
    "no such session",
    "unknown session id",
    "invalid session id",
    "session has expired",
    "session expired",
    "could not resume session",
    "failed to resume session",
    "resume failed",
)


class WorktreeGuardViolation(RuntimeError):
    """Raised when a tool call targets a path outside its assigned worktree."""


def _assert_path_in_worktree(tool_name: str, path: str | Path, worktree_root: Path) -> None:
    """Assert that a candidate path resolves inside the given worktree root.

    Raises RuntimeError with a [worktree-guard] prefix if the resolved path
    is not a descendant of (or equal to) the resolved worktree root.
    Exempts intentionally-symlinked paths planted by lanegate (.lanegate/notes
    and graphify-out) which point to shared control stores.
    """
    candidate = Path(path)
    worktree_root_path = Path(worktree_root)
    if not candidate.is_absolute():
        candidate = worktree_root_path / candidate

    normalized_candidate = Path(os.path.normpath(candidate))
    normalized_root = Path(os.path.normpath(worktree_root_path))

    try:
        rel_path = normalized_candidate.relative_to(normalized_root)
        if rel_path.parts[:2] == (".lanegate", "notes") or rel_path.parts[:1] == ("graphify-out",):
            return
    except ValueError:
        pass

    resolved_path = candidate.resolve()
    resolved_root = worktree_root_path.resolve()

    try:
        resolved_path.relative_to(resolved_root)
        return
    except ValueError:
        pass

    for exempt_rel in (Path(".lanegate") / "notes", Path("graphify-out")):
        symlink_path = worktree_root_path / exempt_rel
        if symlink_path.is_symlink() or symlink_path.exists():
            try:
                resolved_target = symlink_path.resolve()
                resolved_path.relative_to(resolved_target)
                return
            except (ValueError, OSError):
                pass

    raise WorktreeGuardViolation(
        f"[worktree-guard] {tool_name} tool call targeting path outside worktree: {path}"
    )


def _is_main_checkout_bookkeeping_path(path: str, cfg: dict, repo_root: Path) -> bool:
    """True if `path` is lanegate's own control-plane bookkeeping (ticket
    status files, top-level config, generated analysis/log state) or project
    documentation -- rather than user source code an executor's own dispatch
    could plausibly have touched.  These routinely change in the main checkout
    during concurrent orchestration -- a sibling ticket's status-transition
    commit, an analyze pass rewriting a `file_skeletons.json`, a human editing
    `.lanegate.yml` or a supervision-session doc -- and are not evidence of an
    executor escaping its assigned worktree.

    The isolation-leak check exists to catch an executor writing *shared source
    code* into the main checkout instead of its worktree, so the whitelist is
    deliberately broad: lanegate's own state tree, top-level config, the docs/
    tree and root-level Markdown are treated as bookkeeping (see TICK-680,
    TICK-708, TICK-722).
    """
    normalized = path.strip().strip('"')
    if normalized == CONFIG_FILENAME:
        return True

    app_state_dir = f".{APP_NAME}"
    if normalized == app_state_dir or normalized.startswith(app_state_dir + "/"):
        # Everything lanegate itself generates in the main checkout:
        # tickets/, context/ skeletons, logs/, prompts/, executor-runs/, notes/.
        return True

    tickets_dir = str(cfg.get("tickets_dir", f".{APP_NAME}/tickets"))
    if os.path.isabs(tickets_dir):
        try:
            tickets_dir = os.path.relpath(tickets_dir, repo_root)
        except ValueError:
            pass
    # git porcelain paths always use "/", regardless of platform -- normalize
    # to that separator so a Windows os.path.normpath("\\") result still
    # compares correctly against them (see TICK-680 review).
    tickets_dir = os.path.normpath(tickets_dir).replace(os.sep, "/")
    if normalized == tickets_dir or normalized.startswith(tickets_dir + "/"):
        return True

    # Documentation the supervisor edits during a run: the docs/ tree and
    # top-level Markdown (README.md, CHANGELOG.md, ...). A *nested* .md
    # (lanegate/skills/*.md, a package README) is a real project file an
    # executor edits in its worktree, so a concurrent main-checkout change to
    # one still counts as a possible leak — only docs/ and root .md are exempt.
    if normalized.startswith("docs/"):
        return True
    if normalized.endswith(".md") and "/" not in normalized:
        return True

    return False


def _main_checkout_leak_diff(before: str, after: str, cfg: dict, repo_root: Path) -> str:
    """Return changed `git status --porcelain` lines that represent a real
    main-checkout isolation leak.

    A blind full-tree status diff false-positives on routine concurrent
    activity (a sibling ticket's status-transition commit, a human editing
    .lanegate.yml) that has nothing to do with the dispatched executor's
    own worktree isolation -- see TICK-680.
    """

    def _relevant_lines(text: str) -> set[str]:
        relevant: set[str] = set()
        for line in text.splitlines():
            if not line:
                continue
            status_code = line[:2]
            raw_path = line[3:] if len(line) > 3 else ""
            is_rename_or_copy = "R" in status_code or "C" in status_code
            targets = (
                raw_path.split(" -> ")
                if is_rename_or_copy and " -> " in raw_path
                else [raw_path]
            )
            if all(
                _is_main_checkout_bookkeeping_path(t, cfg, repo_root)
                for t in targets
                if t
            ):
                continue
            relevant.add(line)
        return relevant

    changed = _relevant_lines(after) ^ _relevant_lines(before)
    return "\n".join(sorted(changed))


def _check_line_for_worktree_boundary(line: str, worktree_root: Path) -> None:
    """Extract tool calls from JSON event line and assert worktree path boundaries."""
    if not line or not line.strip():
        return
    try:
        data = json.loads(line.strip())
    except (json.JSONDecodeError, TypeError, ValueError):
        return
    if not isinstance(data, dict):
        return

    raw_blocks = data.get("content_block")
    if raw_blocks is None:
        msg = data.get("message")
        raw_blocks = msg.get("content") if isinstance(msg, dict) else None

    if raw_blocks is None:
        blocks = [data]
    elif isinstance(raw_blocks, list):
        blocks = raw_blocks
    elif isinstance(raw_blocks, dict):
        blocks = [raw_blocks]
    else:
        blocks = [data]

    for block in blocks:
        if not isinstance(block, dict):
            continue
        tool_name = block.get("name") or data.get("name") or ""
        if not isinstance(tool_name, str):
            tool_name = str(tool_name)

        inputs = block.get("input") or block.get("inputs") or data.get("input") or {}
        if isinstance(inputs, dict):
            target_path = (
                inputs.get("file_path")
                or inputs.get("path")
                or inputs.get("TargetFile")
                or inputs.get("target_file")
                or inputs.get("filename")
                or inputs.get("TargetPath")
                or inputs.get("target_path")
                or inputs.get("file")
                or inputs.get("AbsolutePath")
            )
            if target_path and tool_name.lower() in (
                "write", "edit", "create", "filewrite", "replace_file_content",
                "write_to_file", "write_file", "edit_file"
            ):
                _assert_path_in_worktree(tool_name, str(target_path), worktree_root)


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


def _resolve_drift_driver_name(
    ticket: dict, cfg: dict, repo_root: Path | None = None, *, pool_name: str | None = None
) -> str:
    """Resolve drift checks through review unless a drift route is explicit.

    Drift checks audit a fix independently, so they use the same review route
    (including a ticket reviewer override) by default.  Either current
    ``steps.drift_check.driver`` or legacy ``executor_steps.drift_check`` can
    explicitly select a different drift executor. When ``pool_name`` and
    ``repo_root`` are given, the review route is resolved through the pool
    (via ``resolve_pool_executor``, step="review") so an ``orchestrate --pool``
    override reaches drift-check the same way it reaches review itself —
    still deferring to a per-ticket ``reviewer:`` pin, which that resolver
    honors before consulting the pool.
    """
    drift_check_driver = ((cfg.get("steps") or {}).get("drift_check") or {}).get("driver")
    if drift_check_driver:
        return drift_check_driver
    drift_check_driver = (cfg.get("executor_steps") or {}).get("drift_check")
    if drift_check_driver:
        return drift_check_driver
    if pool_name and repo_root is not None:
        from lanegate.orchestrate.loop import resolve_pool_executor

        pooled_review_driver = resolve_pool_executor(
            "review", ticket, cfg, repo_root, pool_name=pool_name, healthy_only=True
        )
        if pooled_review_driver:
            return pooled_review_driver
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
    meter=None,
    worktree_path: Path | None = None,
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

    ``meter``, when given a :class:`~lanegate.budget.DispatchMeter`,
    receives both the raw line and its normalized event. It needs both: the raw
    line carries turn envelopes, usage figures and the session id that
    normalization deliberately discards, while only the normalized event
    classifies what the turn was spent doing. It never affects the dispatch —
    it only records what the run cost so an expensive ticket can be attributed
    afterwards.
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
        if meter is not None:
            # Metering must never be able to disturb the run it is measuring,
            # and must see every line — including the ones that normalize to
            # nothing but still carry usage or the session id.
            try:
                meter.observe(line, ev)
                if ev:
                    ev.turns = meter.turns
                    ev.cumulative_tokens = meter.tokens
                    ev.current_context_tokens = meter.last_turn_tokens
            except Exception:
                pass
        if ev:
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

        if worktree_path is not None:
            _check_line_for_worktree_boundary(line, worktree_path)

    def activity_probe() -> float:
        return last_activity_ts

    handle_line.activity_probe = activity_probe  # type: ignore[attr-defined]
    handle_line.meter = meter  # type: ignore[attr-defined]
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


def _last_lifecycle_event_epoch(ticket: dict, event_name: str) -> float | None:
    """Return the newest valid epoch timestamp for a ticket lifecycle event."""
    for record in reversed(ticket.get("lifecycle_events") or []):
        if record.get("event") != event_name:
            continue
        try:
            return datetime.strptime(record["at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=UTC
            ).timestamp()
        except (KeyError, TypeError, ValueError):
            continue
    return None


def capture_manual_implement_step_run(
    repo_root: Path,
    worktree_path: Path,
    ticket: dict,
    cfg: dict,
    *,
    safeguards_passed: bool,
    safeguard_reason: str | None,
) -> Path | None:
    """Capture evidence for a hand-implemented ticket completing outside dispatch."""
    tid = ticket["id"]
    finished_at = time.time()
    started_at = _last_lifecycle_event_epoch(ticket, "implementation_started") or finished_at
    trunk = resolve_trunk_branch(cfg, repo_root)
    branch = ticket.get("branch") or branch_name(tid)
    before_sha = _run_git_snapshot(worktree_path, ["merge-base", trunk, "HEAD"]).strip()
    after_sha = _run_git_snapshot(worktree_path, ["rev-parse", "HEAD"]).strip()
    status = {
        "schema_version": 1,
        "ticket_id": tid,
        "executor": "manual",
        "resolved_driver": "manual",
        "resolved_executor": "manual",
        "resolved_model": None,
        "executor_pid": None,
        "executor_session": f"{tid}-{int(finished_at)}-manual",
        "step": "implement",
        "mode": "manual",
        "worktree": str(worktree_path),
        "log_path": None,
        "prompt_path": None,
        "started_at": started_at,
        "started_at_iso": _iso_from_epoch(started_at),
        "finished_at": finished_at,
        "finished_at_iso": _utc_now_iso(),
        "elapsed_seconds": int(finished_at - started_at),
        "exit_code": 0,
        "state": "finished",
        "last_event": "executor_finished",
        "reconciliation_state": "finished",
        "before_sha": before_sha,
        "after_sha": after_sha,
        "safeguards_passed": safeguards_passed,
        "safeguard_reason": safeguard_reason,
    }
    try:
        bundle_path = _capture_executor_audit_bundle(repo_root, worktree_path, status)
    except Exception as exc:  # pragma: no cover - defensive
        print(
            f"WARNING: could not capture manual implement run directory for {tid}: {exc}",
            file=sys.stderr,
        )
        return None

    try:
        diff_text = get_worktree_diff(worktree_path, branch, base=trunk)
    except Exception:
        diff_text = ""
    if diff_text.strip():
        detail = _write_bounded_text(bundle_path / "diff.patch", diff_text)
        manifest = _load_audit_manifest(bundle_path)
        _manifest_capture(manifest, "diff.patch", detail)
        _save_audit_manifest(bundle_path, manifest)
    return bundle_path


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
            text=True, encoding="utf-8",
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
    if model is not None:
        # Catch a resolved model from the wrong vendor family loudly, at the
        # point of dispatch, instead of silently handing it to an executor
        # that can't use it (e.g. a top-level `models:` block authored for
        # cfg's own executor leaking a claude-*/gemini-* name into a pool
        # member with no per-executor override of its own).
        validate_model_for_executor(
            model, executor_type, context_label=f"executor '{executor}'",
            # A `drivers:` entry can carry `provider` directly on itself
            # (e.g. `drivers: {fast-ollama: {type: aider, provider: ollama}}`)
            # rather than on an `executors:` instance -- executor_cfg alone
            # misses that route, since it's looked up by resolved type/name,
            # not by the driver's own key.
            provider=executor_cfg.get("provider") or driver_cfg.get("provider"),
        )

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


def _get_step_budget_cap(cfg: dict, step: str, cap_key: str) -> int | None:
    val = cfg.get(cap_key)
    if isinstance(val, dict):
        v = val.get(step)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return int(v)
        return None
    if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
        return int(val)
    return None


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
    no_resume: bool = False,
) -> tuple[int, str, str]:
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
            dispatch to route a specific ticket to a specific pool
            instance without writing an arbitrary instance name onto the
            ticket's own frontmatter (which would trip the pre-existing
            validate_ticket gap described in TICK-247).
        no_resume: When True (or when cfg['no_resume'] is set), bypass session
            resumption via --resume (TICK-572 escape hatch).

    Returns (exit_code, captured_stdout, captured_stderr) tuple.
    """
    from lanegate.executor import build_implement_prompt
    from lanegate.pidutil import pid_alive as _pid_alive

    status_root = repo_root if repo_root is not None else worktree_path
    status_root = Path(status_root)
    main_checkout_status_before = None
    if repo_root is not None:
        main_checkout_status_before = subprocess.run(
            ["git", "status", "--porcelain", "-uno"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        ).stdout
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
    # Resolve executor (bare type or named instance) once so the
    # same instance config drives both cmd construction and env injection.
    # Bound ahead of the try so the except block's message below has a name
    # to report even when resolve_dispatch() itself raises ConfigError
    # (e.g. a model/provider mismatch caught before dispatch["executor"] is
    # ever assigned).
    executor = executor_override or "unresolved"
    try:
        dispatch = resolve_dispatch(ticket, cfg, step=step, executor_override=executor_override)
        driver_cfg = dispatch["driver_cfg"]
        executor = dispatch["executor"]
        executor_cfg = get_executor_config(executor, cfg)
        executor_type = executor_cfg.get("type", executor)
        claude_config_dir = None
        if executor_type in _CLAUDE_SUBPROCESS_TYPES:
            configured_bin = executor_cfg.get("bin") or "claude"
            claude_config_dir = Path.home() / f".{Path(str(configured_bin)).name}"
        reject_ollama_for_code_step(step, executor_type)
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
        # Nested lifecycle commands issued by an executor belong to this
        # orchestrate session, not to standalone manual-action history rows.
        # Always materialize an env copy: resolve_executor_env deliberately
        # returns None when no credentials need injecting.
        executor_env = dict(executor_env) if executor_env is not None else dict(os.environ)
        executor_env[INTERNAL_RUN_ENV] = "1"
        command_cfg = _cfg_with_driver_command_overrides(cfg, executor, driver_cfg)
        # Thread a prior step's CLI session into this one
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
        # TICK-572: Session-origin / cwd mismatch hypothesis: analyze session
        # originates in the main checkout root before any worktree exists.
        # Resuming it via --resume could preserve the original checkout as cwd.
        # _assert_path_in_worktree guards against out-of-bounds writes, and
        # no_resume allows bypassing --resume session reuse entirely.
        skip_resume = no_resume or bool(cfg.get("no_resume"))
        if not skip_resume and step == "implement":
            resume_candidate = ticket.get("analyze_session_id")
            resume_origin = "analyze"
        elif not skip_resume and step == "fix":
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
        step_max_turns = _get_step_budget_cap(cfg, step, "max_turns")
        step_max_tokens = _get_step_budget_cap(cfg, step, "max_cumulative_tokens")
        prompt_stdin = None
        if executor_type in executor_types_with("stdin_capable"):
            cmd = build_executor_cmd(
                executor, prompt, command_cfg, model=model, touches=touches,
                analyze_session_id=resume_session_id,
                worktree_path=worktree_path,
                claude_config_dir=claude_config_dir,
                use_stdin=True,
                max_turns=step_max_turns,
            )
            prompt_stdin = prompt
        else:
            cmd = build_executor_cmd(
                executor, prompt, command_cfg, model=model, touches=touches,
                analyze_session_id=resume_session_id,
                worktree_path=worktree_path,
                claude_config_dir=claude_config_dir,
                max_turns=step_max_turns,
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
        return _CONFIG_ERROR_EXIT_CODE, "", msg
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

    budget_exceeded_flag = False
    budget_exceeded_detail = ""

    def _check_budget() -> str | None:
        nonlocal budget_exceeded_flag, budget_exceeded_detail
        if meter is not None:
            if step_max_turns is not None and meter.turns >= step_max_turns:
                budget_exceeded_flag = True
                budget_exceeded_detail = f"max_turns cap reached ({meter.turns}/{step_max_turns} turns, {meter.tokens} tokens)"
                return budget_exceeded_detail
            if step_max_tokens is not None and meter.tokens >= step_max_tokens:
                budget_exceeded_flag = True
                budget_exceeded_detail = f"max_cumulative_tokens cap reached ({meter.tokens}/{step_max_tokens} tokens, {meter.turns} turns)"
                return budget_exceeded_detail
        return None

    def _mirror_event_into_status(ev) -> None:
        status_updates = {
            "last_executor_event": ev.to_dict(),
            "last_event": "executor_progress",
            "phase": ev.phase,
            "activity": ev.activity,
        }
        if getattr(ev, "turns", None) is not None:
            status_updates["turns"] = ev.turns
        if getattr(ev, "cumulative_tokens", None) is not None:
            status_updates["cumulative_tokens"] = ev.cumulative_tokens
        update_status(**status_updates)
        _check_budget()

    # Only streaming executors emit anything countable; for the rest the meter
    # stays at zero rather than reporting a misleading turn count of one.
    meter = (
        DispatchMeter(step=step) if metering_supported_for(executor_type) else None
    )
    handle_line = make_event_line_handler(
        status_root,
        session_ts,
        tid,
        executor=resolved_driver,
        model=resolved_model,
        step=step,
        terminal_stream=terminal_stream,
        on_event=_mirror_event_into_status,
        meter=meter,
        worktree_path=worktree_path,
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
            if meter is not None:
                fb_ev.turns = meter.turns
                fb_ev.cumulative_tokens = meter.tokens
                fb_ev.current_context_tokens = meter.last_turn_tokens
            heartbeat_at = time.time()
            pid_is_live = (
                isinstance(current_status.get("executor_pid"), int)
                and _pid_alive(current_status["executor_pid"])
            )
            if pid_is_live:
                last_verified_heartbeat_ts = heartbeat_at
            hb_status = {
                "last_heartbeat_at": heartbeat_at,
                "last_heartbeat_at_iso": _utc_now_iso(),
                "heartbeat_count": int(current_status.get("heartbeat_count") or 0) + 1,
                "elapsed_seconds": elapsed,
                "last_event": "executor_heartbeat",
                "last_executor_event": fb_ev.to_dict(),
                "reconciliation_state": "live" if pid_is_live else "stale",
            }
            if meter is not None:
                hb_status["turns"] = meter.turns
                hb_status["cumulative_tokens"] = meter.tokens
            status = update_status(**hb_status)
            _append_run_event(
                status_root,
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
    streaming_capable = executor_type in executor_types_with("streaming_capable")
    stream_kwargs = (
        {
            "idle_timeout": float(cfg.get("executor_idle_timeout_seconds", 75)),
            "stall_timeout": float(cfg.get("executor_stall_timeout_seconds", 900)),
            "absolute_ceiling": float(cfg.get("executor_absolute_ceiling_seconds", 1500)),
            "liveness_probe": last_verified_heartbeat,
            "progress_probe": last_activity,
            "budget_probe": _check_budget,
        }
        if streaming_capable
        else {"timeout": exec_timeout, "budget_probe": _check_budget}
    )
    kill_reason = None

    def stream_executor(command: list[str]):
        """Run one executor command through the ordinary streaming path."""
        if log_stream is not None:
            return _unpack_stream_result(_stream_subprocess(
                command,
                str(worktree_path),
                out_stream=log_stream,
                err_stream=log_stream,
                stdin_text=prompt_stdin,
                on_start=on_process_start,
                env=executor_env,
                on_line=handle_line,
                **stream_kwargs,
            ))
        return _unpack_stream_result(_stream_subprocess(
            command,
            str(worktree_path),
            stdin_text=prompt_stdin,
            on_start=on_process_start,
            env=executor_env,
            on_line=handle_line,
            **stream_kwargs,
        ))

    try:
        rc, captured_stdout, captured_stderr, kill_reason = stream_executor(cmd)
        resume_error = (captured_stdout + "\n" + captured_stderr).lower()
        if (
            rc != 0
            and resume_session_id is not None
            and step in ("implement", "fix")
            and executor_type in _SESSION_RESUME_TYPES
            and any(marker in resume_error for marker in _RESUME_REJECTION_MARKERS)
        ):
            fresh_cmd = build_executor_cmd(
                executor, prompt, command_cfg, model=model, touches=touches,
                worktree_path=worktree_path,
                claude_config_dir=claude_config_dir,
                use_stdin=prompt_stdin is not None,
                max_turns=step_max_turns,
            )
            if log_stream is not None:
                log_stream.write(
                    f"[orchestrate] {ticket['id']}: {executor} resume session expired/rejected; retrying fresh\n"
                )
                log_stream.flush()
            rc, captured_stdout, captured_stderr, kill_reason = stream_executor(fresh_cmd)
    finally:
        heartbeat_stop.set()
        hb.join(timeout=1)

    if repo_root is not None and kill_reason != "worktree_violation":
        main_checkout_status_after = subprocess.run(
            ["git", "status", "--porcelain", "-uno"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        ).stdout
        leaked_diff = _main_checkout_leak_diff(
            main_checkout_status_before or "", main_checkout_status_after, cfg, repo_root
        )
        if leaked_diff:
            rc = 1
            kill_reason = "main_checkout_violation"
            msg = (
                f"[orchestrate] {tid}: worktree isolation leak detected: tracked files in "
                f"the main checkout changed during dispatch:\n{leaked_diff}\n"
            )
            captured_stderr += msg
            if log_stream is not None:
                log_stream.write(msg)
                log_stream.flush()
            if terminal_stream is not None:
                terminal_stream.write(msg)
                terminal_stream.flush()
            sys.stderr.write(msg)

    if (
        executor_type == "aider"
        and rc == 0
        and "--dry-run" not in cmd
        and "--edit-format" in cmd
        and "ask" not in cmd
        and "Commit " not in captured_stdout
        and "Committing " not in captured_stdout
    ):
        _check_aider_parser_rejection(captured_stdout + "\n" + captured_stderr)

    _check_budget()
    if kill_reason in ('idle', 'stall', 'ceiling', 'timeout', 'suspend_gap'):
        elapsed_diag = int(time.time() - start_time)
        hb_count = current_status.get("heartbeat_count", 0)
        diag_msg = f"[orchestrate] {tid}: dispatch terminated due to '{kill_reason}' after {elapsed_diag}s ({hb_count} heartbeats received)\n"
        captured_stderr += diag_msg
        if log_stream is not None:
            log_stream.write(diag_msg)
            log_stream.flush()
        sys.stderr.write(diag_msg)

    if budget_exceeded_flag or kill_reason == "budget_exceeded":
        if rc == 0:
            rc = 1
        detail = budget_exceeded_detail or (
            f"max_turns/tokens cap reached ({meter.turns if meter else 0} turns, {meter.tokens if meter else 0} tokens)"
        )
        msg = f"[orchestrate] {tid}: dispatch aborted early — budget cap exceeded: {detail}\n"
        if log_stream is not None:
            log_stream.write(msg)
            log_stream.flush()
        if terminal_stream is not None:
            terminal_stream.write(msg)
            terminal_stream.flush()
        sys.stderr.write(msg)
    if kill_reason == "worktree_violation":
        if rc == 0:
            rc = 1
        msg = (
            f"[orchestrate] {tid}: dispatch aborted — this is a LaneGate worktree-isolation "
            f"bug (the executor wrote outside its assigned worktree), not a merge conflict or "
            f"user error\n"
        )
        if log_stream is not None:
            log_stream.write(msg)
            log_stream.flush()
        if terminal_stream is not None:
            terminal_stream.write(msg)
            terminal_stream.flush()
        sys.stderr.write(msg)
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

        record_step_cost(
            status_root, tid, step, executor, model, parsed, dispatch_start_time=start_time
        )

    # Report what this dispatch cost and, when it was expensive,
    # attribute it. This never changes the outcome of the step -- an expensive
    # run still succeeds or fails on its own merits -- but a 130-turn implement
    # that nobody sees is how a project quietly burns a daily quota.
    if meter is not None and meter.turns:
        _append_run_event(
            status_root,
            session_ts,
            "executor_metrics",
            ticket_id=tid,
            metrics={"step": step, "executor": resolved_driver, **meter.summary()},
        )
        diagnosis = meter.diagnose(cfg)
        cost_line = f"[orchestrate] {tid} {step} cost: {meter.format_usage()}\n"
        if diagnosis is not None:
            cost_line += (
                f"[orchestrate] {tid} {step} is expensive "
                f"({diagnosis['verdict']}): {diagnosis['summary']}\n"
                f"[orchestrate]   {diagnosis['detail']}\n"
            )
        for stream in (log_stream, terminal_stream):
            if stream is not None:
                stream.write(cost_line)
                stream.flush()

    # Persist this call's session id so a later step (fix
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
        if not new_session_id and meter is not None:
            # A dispatch killed by a wall-clock watchdog never emits the final
            # result envelope `parsed` comes from, so previously its
            # session id was lost and any continuation had to start a cold
            # conversation -- re-reading the repo and re-deriving everything the
            # dead session already knew. The id seen on the stream is the same
            # conversation, so prefer losing nothing over losing all of it.
            new_session_id = meter.session_id
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
            text=True, encoding="utf-8",
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
        text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        return False
    return bool(result.stdout.strip())


def commit_worktree_changes(
    worktree_path: Path,
    ticket_id: str,
    message: str | None = None,
    *,
    ticket: dict | None = None,
    paths: list[str] | None = None,
) -> tuple[bool, str | None]:
    """Commit executor-produced worktree edits, if any.

    Some executors return successfully after editing files but do not create a
    git commit.  LaneGate owns the ticket branch, so the orchestrator can make that
    final implementation commit before the review/completion gates run.

    Args:
        message: Commit message to use. Defaults to "feat: implement
            <ticket_id>" (the implement-step message); callers committing a
        fix pass should pass a distinct message so the commit log doesn't
            mislabel a fix as the original implementation.
        paths: When provided, stage only these worktree-relative paths. This
            is required for recovery commits, which must not sweep unrelated
            worktree edits into a conflict-resolution commit.
    """
    if not worktree_path.exists():
        return False, None
    if paths == []:
        return False, None

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True, encoding="utf-8",
    )
    if status.returncode != 0 or not status.stdout.strip():
        return False, None

    pathspec = [f":(literal){p}" for p in paths] if paths is not None else [".", ":(exclude).lanegate/**"]
    add = subprocess.run(
        # Exclude .lanegate/ for regular executor commits so LaneGate's own
        # audit artifacts never land on the ticket branch. Scoped callers may
        # deliberately include a conflicted metadata file.
        ["git", "add", "-A", "--", *pathspec],
        cwd=str(worktree_path),
        capture_output=True,
        text=True, encoding="utf-8",
    )
    if add.returncode != 0:
        return False, None

    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True, encoding="utf-8",
    )
    if staged.returncode == 0:
        return False, None
    elif staged.returncode != 1:
        return False, None

    if ticket is not None:
        try:
            check_touches_compliance(ticket_id, ticket, worktree_path)
        except SystemExit:
            subprocess.run(["git", "restore", "--staged", "."], cwd=str(worktree_path))
            return False, None

    commit_cmd = ["git", "commit", "-s", "-m", message or f"feat: implement {ticket_id}"]
    if paths is not None:
        commit_cmd.extend(["--", *pathspec])

    commit = subprocess.run(
        commit_cmd,
        cwd=str(worktree_path),
        capture_output=True,
        text=True, encoding="utf-8",
    )
    if commit.returncode != 0:
        return False, commit.stderr.strip() or commit.stdout.strip()
    return True, None
