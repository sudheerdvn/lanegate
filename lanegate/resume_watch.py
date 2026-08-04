"""
resume_watch.py — session-independent background daemon that waits out a rate
limit and resumes `lanegate orchestrate` automatically.

Spawned by orchestrate.py's spawn_resume_watch_daemon() when a run halts on a
rate limit and on_rate_limit=resume. Mirrors watch.py's detached-daemon
pattern (PID file, log file, --status/--stop), but polls with a capped
exponential backoff instead of a fixed interval, since we have no reliable
way to know exactly when an API/subscription rate limit will clear.

Usage:
    lanegate resume-watch            # run the poll loop (background it yourself)
    lanegate resume-watch --status   # print whether a watcher is running and its pid
    lanegate resume-watch --stop     # kill a running watcher
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lanegate import APP_NAME
from lanegate.config import _DEFAULT_RESUME_CEILING_S
from lanegate.notify import send_ntfy

# These four decide whether a hibernation is *waitable* (a rate limit that will
# clear) or *permanently broken* (a bad model name, a 400). They used to be
# re-declared here with a "must match orchestrate" comment as the only thing
# holding the contract together, and the two copies had already drifted in both
# directions — orchestrate's regexes matched `model gpt-5-x does not exist`
# where this module's substring test did not, so a dead configuration was
# classified here as a rate limit and retried forever. One definition now.
# loop.py imports this module only inside a function, so this is not circular.
from lanegate.orchestrate.loop import (
    _RATE_LIMIT_MARKER as _RATE_LIMIT_MARKER,
    _active_rate_limit_hibernation as _active_rate_limit_hibernation,
    _has_non_rate_limit_hard_error as _has_non_rate_limit_hard_error,
)
from lanegate.pidutil import pid_alive
from lanegate.ticket import load_all_tickets


# ---------------------------------------------------------------------------
# Reset-time parsing (TICK-257)
# ---------------------------------------------------------------------------
#
# Confirmed against a real captured-output.txt (TICK-157 executor-run
# artifact, persisted by TICK-256's audit-bundle change): the `claude` CLI
# emits "You've hit your session limit · resets 11:40am (America/Los_Angeles)"
# — a bare clock time with an IANA zone name in parens, not UTC and not the
# machine's local zone. These patterns cover that confirmed shape plus the
# other needle family in orchestrate._is_rate_limit ("try again at ..."), and
# an absolute ISO-8601 timestamp in case a future/other executor emits one.
# Anything that doesn't match falls through to None, which callers treat as
# "use the exponential-backoff fallback" — an unrecognized format must never
# raise.
_RESET_CONTEXT_RE = re.compile(
    r"(?:try again at|resets(?:\s+at)?)\s*[:\-]?\s*(.{0,60})", re.IGNORECASE
)
_RESET_ISO_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
)
_RESET_CLOCK_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b", re.IGNORECASE)
# IANA zone name in parens, as seen in the real capture above. Unknown/absent
# -> the clock time is treated as already being in `now`'s zone (UTC).
_RESET_TZ_RE = re.compile(r"\(([A-Za-z_]+(?:/[A-Za-z_]+){1,2})\)")

# Default buffer added on top of a parsed reset time, so the retry doesn't
# race the clock if the parsed instant is slightly off. Overridable via
# rate_limit_resume.reset_buffer_s in .lanegate.yml.
_DEFAULT_RESET_BUFFER_S = 90.0


def _parse_reset_time(
    text: str, now: datetime | None = None, *, allow_rollover: bool = True
) -> datetime | None:
    """Best-effort parse of a rate-limit reset time out of raw executor text.

    Returns an aware UTC datetime, or None if no recognizable reset-time hint
    is present — the caller falls back to exponential backoff in that case.

    *allow_rollover* controls what a bare clock time already in the past means.
    For a freshly emitted hint it means tomorrow ("resets 11:40am" printed at
    3pm), which is the default. For a hint re-read out of a ticket body on a
    later loop iteration it means the opposite — the window has already
    cleared, retry now — so `_run_loop` passes False after the first wait.
    """
    if not text:
        return None
    if now is None:
        now = datetime.now(UTC)

    context_match = _RESET_CONTEXT_RE.search(text)
    if not context_match:
        return None
    snippet = context_match.group(1)

    iso_match = _RESET_ISO_RE.search(snippet)
    if iso_match:
        raw = iso_match.group(1).replace(" ", "T")
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed

    clock_match = _RESET_CLOCK_RE.search(snippet)
    if clock_match:
        hour = int(clock_match.group(1))
        minute = int(clock_match.group(2) or 0)
        if not (1 <= hour <= 12 and 0 <= minute <= 59):
            return None
        if hour == 12:
            hour = 0
        if clock_match.group(3).lower() == "p":
            hour += 12

        tz = UTC
        tz_match = _RESET_TZ_RE.search(snippet)
        if tz_match:
            try:
                tz = ZoneInfo(tz_match.group(1))
            except ZoneInfoNotFoundError:
                tz = UTC

        now_in_tz = now.astimezone(tz)
        candidate = now_in_tz.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now_in_tz and allow_rollover:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)

    return None


def _reset_wait_seconds(
    hibernated: list[dict],
    *,
    buffer_s: float = _DEFAULT_RESET_BUFFER_S,
    now: datetime | None = None,
    allow_rollover: bool = True,
) -> float | None:
    """Seconds-from-now to wait based on parsed reset-time hints, or None.

    Takes the latest (not earliest) reset time across hibernated tickets, so
    a retry isn't scheduled before every rate-limited ticket's window has
    cleared. Returns None (triggering the backoff fallback) if none of the
    tickets carry a parseable hint.

    With *allow_rollover* False a deadline already in the past yields a
    retry-now wait (just the buffer) rather than a ~24-hour sleep — see
    `_parse_reset_time`.
    """
    if now is None:
        now = datetime.now(UTC)
    deadlines = [
        d
        for d in (
            _parse_reset_time(t.get("_body") or "", now=now, allow_rollover=allow_rollover)
            for t in hibernated
        )
        if d is not None
    ]
    if not deadlines:
        return None
    return max(0.0, (max(deadlines) - now).total_seconds()) + buffer_s


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _resume_watch_pid_file(repo_root: Path) -> Path:
    """Return the path to the resume-watch PID file."""
    state_dir = repo_root / f".{APP_NAME}"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "resume-watch.pid"


def _resume_watch_log_file(repo_root: Path) -> Path:
    """Return the path to the resume-watch log file."""
    state_dir = repo_root / f".{APP_NAME}"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "resume-watch.log"


def _resume_watch_history_file(repo_root: Path) -> Path:
    """Return the path to the resume-watch structured history log (JSONL)."""
    state_dir = repo_root / f".{APP_NAME}"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "resume-watch-history.jsonl"


def _write_log(log_path: Path, line: str) -> None:
    """Append one already-terminated line to the resume-watch log."""
    with open(log_path, "a") as f:
        f.write(line)


def _append_history(repo_root: Path, event: str, **fields) -> None:
    """Append one structured event to the resume-watch history log."""
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        **fields,
    }
    with open(_resume_watch_history_file(repo_root), "a") as f:
        f.write(json.dumps(entry) + "\n")


def read_history(repo_root: Path, limit: int = 20) -> list[dict]:
    """Return the last `limit` resume-watch history entries, oldest first."""
    path = _resume_watch_history_file(repo_root)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines[-limit:]:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def get_daemon_status(repo_root: Path) -> dict | None:
    """Return current resume-watch daemon state for the API, or None if absent.

    Reads the PID file and history JSONL to derive phase without re-implementing
    the backoff/ceiling math. Returns a dict with:
      phase          — "waiting" | "retrying" | "gave_up"
      elapsed_time   — seconds since hibernation started (float)
      next_retry_eta — ISO timestamp or None (not computable without cfg access)
    """
    pid_path = _resume_watch_pid_file(repo_root)
    pid = _read_pid(pid_path)
    daemon_alive = pid is not None

    entries = read_history(repo_root)
    last_event = entries[-1] if entries else None

    if not daemon_alive:
        if last_event and last_event.get("event") == "gave_up":
            return {
                "phase": "gave_up",
                "elapsed_time": float(last_event.get("elapsed_s", 0)),
                "next_retry_eta": None,
            }
        return None

    # Daemon alive — phase is "retrying" while orchestrate subprocess runs,
    # "waiting" at all other times (initial wait or between retries).
    if last_event and last_event.get("event") == "retrying":
        return {
            "phase": "retrying",
            "elapsed_time": float(last_event.get("elapsed_s", 0)),
            "next_retry_eta": None,
        }

    # Waiting (initial hold or post-retry sleep).  Compute elapsed from the
    # most recent "hibernated" event timestamp so the caller gets a live
    # wall-clock value for the *current* wait — the history file accumulates
    # entries across every past hibernation, so picking the first match
    # instead of the last would report elapsed time since the oldest one
    # ever recorded (potentially days old) rather than the current wait.
    elapsed_s = 0.0
    hibernated_event = next((e for e in reversed(entries) if e.get("event") == "hibernated"), None)
    if hibernated_event:
        try:
            import datetime as _dt
            ts_str = hibernated_event["ts"]
            start = _dt.datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=_dt.timezone.utc
            )
            elapsed_s = (_dt.datetime.now(_dt.timezone.utc) - start).total_seconds()
        except (KeyError, ValueError):
            pass

    return {
        "phase": "waiting",
        "elapsed_time": round(elapsed_s),
        "next_retry_eta": None,
    }


def read_history_since(repo_root: Path, since_iso: str) -> list[dict]:
    """Resume-watch history entries at or after `since_iso` (UTC, same
    "%Y-%m-%dT%H:%M:%SZ" format as _append_history's own timestamps).

    resume-watch-history.jsonl is a flat, run-agnostic log — a rate-limit
    hibernation and its retries have no record of which orchestrate run
    they belong to. String comparison works here because both this file's
    timestamps and the run-report's are the same zero-padded UTC format,
    which sorts lexicographically the same as chronologically.
    """
    if not since_iso:
        return []
    entries = read_history(repo_root, limit=10_000)
    return [e for e in entries if str(e.get("ts", "")) >= since_iso]


def _read_pid(pid_path: Path) -> int | None:
    """
    Return the PID from the pid file, or None if missing, unreadable, or stale
    (i.e. the process is no longer running).
    """
    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        pid = int(raw)
    except (ValueError, TypeError):
        return None
    if pid_alive(pid):
        return pid
    return None


# ---------------------------------------------------------------------------
# Ticket state
# ---------------------------------------------------------------------------


def _hibernated_for_rate_limit(cfg: dict, repo_root: Path) -> list[dict]:
    """Hibernated tickets whose hibernation notes indicate a rate limit.

    Other hibernation/halt reasons (needs_review, failed, changes_requested)
    need a human, not a timer — resume-watch only cares about this subset.
    """
    tickets_dir = Path(cfg.get("tickets_dir", "tickets"))
    if not tickets_dir.is_absolute():
        tickets_dir = repo_root / tickets_dir
    tickets, _ = load_all_tickets(tickets_dir, cfg.get("ticket_prefix", "TICK"), cfg)
    return [
        t
        for t in tickets
        if t.get("status") == "hibernated"
        and _active_rate_limit_hibernation(t.get("_body") or "")
    ]


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------


def _orchestrate_args_file(repo_root: Path) -> Path:
    """Return the path to the stored orchestrate arguments file."""
    state_dir = repo_root / f".{APP_NAME}"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "resume-watch-args.json"


# The complete set of flags store_orchestrate_args() can emit. This file is
# gitignored control-plane state that the touches guard cannot see (it inspects
# *committed* files), so a worktree agent can write it and steer a later
# unattended orchestrate run. Whatever is in it becomes argv, so it is treated
# as untrusted input rather than as something we wrote.
_ORCHESTRATE_BOOL_FLAGS = frozenset(
    {"--dry-run", "--all", "--no-auto-analyze", "--no-recover", "--verbose"}
)
_ORCHESTRATE_VALUE_FLAGS = frozenset(
    {"--max", "--human-review", "--milestone", "--tickets", "--pool"}
)


def _read_orchestrate_args(repo_root: Path) -> tuple[list[str], list[str]]:
    """Read and validate the stored orchestrate argv.

    Returns ``(args, dropped)``. Anything that is not a ``list[str]`` of
    recognized flags is discarded rather than passed through — an unvalidated
    payload previously either steered the resumed run or killed the daemon
    outright (a str payload raises TypeError on ``list + str``, a list of ints
    raises inside ``' '.join``, and neither is caught by the OSError handler
    around the subprocess call).

    ``--human-review none`` is dropped even though it is a flag we can emit:
    the resumed run then falls back to the config's ``default_human_review``.
    A resume happens by construction when nobody is watching, so this path may
    never weaken the review gate below what the config asks for — only match it.
    """
    args_file = _orchestrate_args_file(repo_root)
    try:
        data = json.loads(args_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], []

    raw = data.get("args") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not all(isinstance(a, str) for a in raw):
        return [], ["payload is not a list of strings"]

    args: list[str] = []
    dropped: list[str] = []
    i = 0
    while i < len(raw):
        token = raw[i]
        if token in _ORCHESTRATE_BOOL_FLAGS:
            args.append(token)
            i += 1
        elif token in _ORCHESTRATE_VALUE_FLAGS:
            value = raw[i + 1] if i + 1 < len(raw) else None
            if value is None or value.startswith("-"):
                dropped.append(f"{token} (missing value)")
                i += 1
            elif token == "--human-review" and value == "none":
                dropped.append("--human-review none (would weaken the gate on an unattended run)")
                i += 2
            else:
                args.extend([token, value])
                i += 2
        else:
            dropped.append(token)
            i += 1
    return args, dropped


def store_orchestrate_args(repo_root: Path, args: list[str]) -> None:
    """Store the orchestrate command arguments for resume-watch to use on retry."""
    args_file = _orchestrate_args_file(repo_root)
    data = {"args": args}
    args_file.write_text(json.dumps(data), encoding="utf-8")


def _run_loop(cfg: dict, repo_root: Path) -> None:
    """
    Internal polling loop. Called by cmd_resume_watch when running as a daemon.
    Logs to .lanegate/resume-watch.log.
    """
    log_path = _resume_watch_log_file(repo_root)

    def log(msg: str) -> None:
        line = f"{msg}\n"
        print(line, end="", flush=True)
        _write_log(log_path, line)

    log(f"[resume-watch] started (PID {os.getpid()})")

    topic = (cfg.get("notify") or {}).get("ntfy_topic")

    def push(message: str) -> None:
        if topic and not send_ntfy(topic, message, title="lanegate resume-watch"):
            log("[resume-watch] ntfy push failed")

    resume_cfg = cfg.get("rate_limit_resume") or {}
    backoff = float(resume_cfg.get("initial_backoff_s", 300))
    max_backoff = float(resume_cfg.get("max_backoff_s", 7200))
    reset_buffer = float(resume_cfg.get("reset_buffer_s", _DEFAULT_RESET_BUFFER_S))
    # Re-stated rather than relying on the DEFAULTS block: load_config merges
    # .lanegate.yml shallowly, so a user setting only `initial_backoff_s:` would
    # otherwise drop `ceiling_s` and silently get poll-forever back.
    ceiling = resume_cfg.get("ceiling_s", _DEFAULT_RESUME_CEILING_S)
    elapsed = 0.0

    hibernated = _hibernated_for_rate_limit(cfg, repo_root)
    if not hibernated:
        log("[resume-watch] no rate-limited hibernated tickets — nothing to do, exiting")
        return

    ids = [t["id"] for t in hibernated]
    log(f"[resume-watch] rate limit hit on {', '.join(ids)} — auto-resume starting")
    push(f"rate limit hit on {', '.join(ids)} — auto-resume watching, will retry with backoff")
    _append_history(repo_root, "hibernated", ticket_ids=ids)

    current = hibernated
    first_pass = True
    while True:
        if ceiling is not None and elapsed >= ceiling:
            log(
                f"[resume-watch] gave up after {elapsed:.0f}s (ceiling_s={ceiling:.0f}) — "
                "manual resume needed: lanegate orchestrate"
            )
            push(
                f"gave up auto-resuming {', '.join(ids)} after {elapsed:.0f}s — "
                "manual resume needed: lanegate orchestrate"
            )
            _append_history(repo_root, "gave_up", ticket_ids=ids, elapsed_s=elapsed)
            break

        # After the first pass `current` is re-read from ticket bodies that may
        # still carry the *original* reset hint. A time already in the past
        # there means the window cleared, not "tomorrow" — rolling it forward
        # turned one unproductive retry into a ~24h sleep.
        parsed_wait = _reset_wait_seconds(
            current, buffer_s=reset_buffer, allow_rollover=first_pass
        )
        if parsed_wait is not None:
            # max_backoff applies here too: docs/config-reference.md documents it
            # as the cap on any single wait, and the parsed path used to ignore it.
            wait = min(parsed_wait, max_backoff)
            if ceiling is not None:
                wait = min(wait, ceiling - elapsed)
            log(
                f"[resume-watch] parsed reset time from hibernation reason — waiting "
                f"{wait:.0f}s before retrying (elapsed {elapsed:.0f}s)"
            )
        else:
            wait = backoff if ceiling is None else min(backoff, ceiling - elapsed)
            log(f"[resume-watch] waiting {wait:.0f}s before retrying (elapsed {elapsed:.0f}s)")
        time.sleep(wait)
        elapsed += wait

        orchestrate_args, dropped_args = _read_orchestrate_args(repo_root)
        if dropped_args:
            log(f"[resume-watch] ignored unrecognized stored args: {', '.join(dropped_args)}")
        cmd = [APP_NAME, "orchestrate"] + orchestrate_args
        log(f"[resume-watch] retrying: {' '.join(cmd)}")
        _append_history(repo_root, "retrying", ticket_ids=ids, elapsed_s=elapsed, backoff_s=backoff)
        try:
            result = subprocess.run(
                cmd,
                cwd=repo_root,
                capture_output=True,
                text=True,
            )
            tail = "\n".join((result.stdout + result.stderr).splitlines()[-20:])
            log(f"[resume-watch] orchestrate exited {result.returncode}\n{tail}")
        except OSError as exc:
            log(f"[resume-watch] failed to invoke lanegate orchestrate: {exc}")

        still_limited = _hibernated_for_rate_limit(cfg, repo_root)
        if not still_limited:
            log("[resume-watch] no rate-limited tickets remain — resumed successfully, exiting")
            push(f"resumed successfully after {elapsed:.0f}s: {', '.join(ids)}")
            _append_history(repo_root, "resumed", ticket_ids=ids, elapsed_s=elapsed)
            break

        log(f"[resume-watch] still rate-limited ({len(still_limited)} ticket(s)) — backing off")
        backoff = min(backoff * 2, max_backoff)
        current = still_limited
        first_pass = False

    log("[resume-watch] exiting")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def cmd_resume_watch(
    cfg: dict,
    repo_root: Path,
    *,
    status: bool = False,
    stop: bool = False,
    history: bool = False,
) -> None:
    """
    Main entry point for `lanegate resume-watch`.

      lanegate resume-watch            — run the poll loop
      lanegate resume-watch --status   — report running state
      lanegate resume-watch --stop     — kill the running watcher
      lanegate resume-watch --history  — print recent resume attempts and outcomes
    """
    pid_path = _resume_watch_pid_file(repo_root)

    # ── --history ─────────────────────────────────────────────────────────────
    if history:
        entries = read_history(repo_root)
        if not entries:
            print("[resume-watch] no history recorded yet")
            return
        for entry in entries:
            ts = entry.get("ts", "?")
            event = entry.get("event", "?")
            ids = ", ".join(entry.get("ticket_ids") or [])
            extra = ""
            if "elapsed_s" in entry:
                extra += f" elapsed={entry['elapsed_s']:.0f}s"
            if "backoff_s" in entry:
                extra += f" next_backoff={entry['backoff_s']:.0f}s"
            print(f"{ts}  {event:10s}  {ids}{extra}")
        return

    # ── --status ──────────────────────────────────────────────────────────────
    if status:
        pid = _read_pid(pid_path)
        if pid is None:
            print("[resume-watch] not running")
        else:
            print(f"[resume-watch] running (PID {pid})")
        return

    # ── --stop ────────────────────────────────────────────────────────────────
    if stop:
        pid = _read_pid(pid_path)
        if pid is None:
            print("[resume-watch] not running — nothing to stop")
            # Clean up a stale file if present (process is dead)
            if pid_path.exists():
                pid_path.unlink(missing_ok=True)
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError) as exc:
            print(f"[resume-watch] could not kill PID {pid}: {exc}", file=sys.stderr)
        else:
            print(f"[resume-watch] sent SIGTERM to PID {pid}")
            pid_path.unlink(missing_ok=True)
        return

    # ── run the poll loop ─────────────────────────────────────────────────────

    # Detect and clean up a stale PID file before starting.
    if pid_path.exists():
        existing_pid = _read_pid(pid_path)
        if existing_pid is not None:
            print(
                f"[resume-watch] already running (PID {existing_pid}). Use --stop to kill it first.",
                file=sys.stderr,
            )
            sys.exit(1)
        else:
            # Stale file — process is dead, clean it up.
            pid_path.unlink(missing_ok=True)

    # Write our own PID file.
    pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")

    # Remove PID file on clean exit. SIGTERM handler is Unix-only;
    # on Windows --stop uses TerminateProcess so the handler never fires.
    def _cleanup(*_):
        pid_path.unlink(missing_ok=True)
        sys.exit(0)

    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, _cleanup)

    try:
        _run_loop(cfg, repo_root)
    finally:
        pid_path.unlink(missing_ok=True)
