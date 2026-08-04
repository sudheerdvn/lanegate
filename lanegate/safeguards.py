"""
safeguards.py — ticket-level pre_complete, pre_merge, and post_merge quality gates.

Guards are configured in .lanegate.yml under the ``safeguards`` key, or
per-ticket in the ``safeguards`` frontmatter field.  Per-ticket config
overrides the project-level config entirely for that ticket.

Supported guard values
----------------------
``pytest`` / ``pytest <args>``
    Runs ``python -m pytest [args]`` in the worktree.
``npm test`` / ``npm run <x>``
    Runs ``npm run <x>`` in the worktree.
``cargo test``
    Runs ``cargo test`` in the worktree.
``go test ./...``
    Runs ``go test ./...`` in the worktree.
``make <target>``
    Runs ``make <target>`` in the worktree.
``path/to/script.sh``
    Resolves the path relative to the worktree root and executes it.
    On Windows this produces a clear error rather than a silent failure.
anything else
    Parsed with ``shlex.split`` and run as a subprocess (shell=False for
    security — avoids shell injection from config values).
"""

from __future__ import annotations

import fnmatch
import shlex
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Prompt/transcript persistence guard (TICK-306)
# ---------------------------------------------------------------------------
#
# LaneGate must never let a full executor prompt or a persisted executor
# transcript/history file become a commit candidate under tickets_dir --
# only ticket markdown + the deterministic file-skeleton sidecar belong
# there. Filenames matching these patterns are always rejected regardless
# of content; a ticket markdown file is additionally rejected if its size
# alone strongly suggests a raw prompt/transcript got pasted into the body.

_PROMPT_ARTIFACT_FILENAME_PATTERNS = (
    "*.session",
    "*.jsonl",
    "*prompt*",
    "*transcript*",
    "*.chat",
    "*.history",
)

# A normal ticket markdown file (title + body + criteria + change_notes) does
# not naturally approach this size; TICK-259 observed a 25KB+ prompt get
# echoed back into persisted state, so this catches that class of artifact
# without depending on the executor whose prompt it was.
_MAX_TICKET_MARKDOWN_BYTES = 40000


def _find_prompt_artifact_violations(
    tickets_dir: Path, own_ticket_id: str | None = None
) -> list[str]:
    """Return violation strings for files under *tickets_dir* that look like
    a persisted full prompt or executor transcript rather than normal ticket
    markdown + the file-skeleton sidecar JSON.

    Returns an empty list (not an error) when *tickets_dir* doesn't exist --
    most LaneGate worktrees don't carry a copy of it at all (TICK-306).

    The filename-pattern check scans every file in *tickets_dir* regardless of
    *own_ticket_id* -- a stray artifact file (``*.session``, ``*.jsonl``, ...)
    appearing anywhere is suspicious no matter which ticket is in flight. The
    size-ceiling check is scoped to *own_ticket_id*'s own markdown file when
    given: this runs as a pre_merge/post_merge gate for one specific ticket,
    and a long-since-merged ticket's oversized legacy front matter (e.g. an
    old embedded ``file_skeletons`` blob predating ``file_skeletons_ref``) has
    nothing to do with whether *this* ticket's body just got polluted with a
    pasted transcript -- it shouldn't block every future unrelated merge until
    someone notices and manually trims it. When *own_ticket_id* is None (the
    default, used by direct/legacy callers), every ``.md`` file is checked.
    """
    violations: list[str] = []
    if not tickets_dir.is_dir():
        return violations

    for path in sorted(tickets_dir.rglob("*")):
        if not path.is_file():
            continue
        name = path.name.lower()
        if any(fnmatch.fnmatch(name, pat) for pat in _PROMPT_ARTIFACT_FILENAME_PATTERNS):
            violations.append(
                f"{path}: filename matches a prompt/transcript artifact pattern "
                "and must not be persisted under tickets_dir"
            )
            continue
        if path.suffix == ".md":
            if own_ticket_id is not None and path.stem != own_ticket_id:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > _MAX_TICKET_MARKDOWN_BYTES:
                violations.append(
                    f"{path}: {size} bytes exceeds the {_MAX_TICKET_MARKDOWN_BYTES}-byte ticket "
                    "markdown ceiling -- looks like a full prompt/transcript was pasted into the "
                    "ticket body rather than normal ticket content"
                )

    return violations

# ---------------------------------------------------------------------------
# Built-in runner dispatch
# ---------------------------------------------------------------------------


def _run_one_guard(
    guard: str,
    worktree: Path,
    timeout_s: int | None = None,
    retry_count: int = 0,
    *,
    timed_out: list[str] | None = None,
) -> tuple[bool, str | None]:
    """Run a single guard string with optional timeout and retry.

    Args:
        guard:       Guard command string.
        worktree:    Working directory for subprocess.
        timeout_s:   Timeout in seconds; None means no timeout.
        retry_count: Number of retries on failure (0 = no retry).
        timed_out:   Optional list; the guard string is appended to it if this
                     guard specifically timed out (as opposed to a plain
                     non-zero exit), so callers can report a distinct reason.

    Returns:
        (True, None) on success.
        (False, reason) on failure; reason describes the failure mode.
    """
    guard = guard.strip()
    if not guard:
        return True, None  # empty entry is a no-op

    cmd = _resolve_command(guard, worktree)
    if cmd is None:
        return False, "command resolution failed"

    # Retry loop: run up to (1 + retry_count) times
    attempts_remaining = 1 + retry_count
    last_error = None

    while attempts_remaining > 0:
        attempts_remaining -= 1
        try:
            result = subprocess.run(
                cmd,
                cwd=worktree,
                capture_output=True,
                text=True, encoding="utf-8",
                timeout=timeout_s,
                start_new_session=True,
            )
            if result.returncode == 0:
                return True, None
            # Guard failed with non-zero exit; print error output so operator sees cause
            if result.stdout:
                print(result.stdout.strip(), file=sys.stderr)
            if result.stderr:
                print(result.stderr.strip(), file=sys.stderr)
            last_error = "nonzero"
            if attempts_remaining == 0:
                if retry_count > 0:
                    return False, f"failed after {retry_count + 1} attempts"
                else:
                    return False, "nonzero exit"
        except subprocess.TimeoutExpired:
            print(f"  TIMEOUT: guard exceeded {timeout_s}s", file=sys.stderr)
            if timed_out is not None:
                timed_out.append(guard)
            return False, f"timed out after {timeout_s}s"
        except FileNotFoundError as exc:
            print(f"  ERROR: command not found: {exc}", file=sys.stderr)
            return False, "command not found"

    return False, "unknown failure"


def _resolve_command(guard: str, worktree: Path) -> list[str] | None:
    """Translate a guard string into an argv list ready for subprocess.run.

    Returns None when the guard cannot be resolved (e.g. .sh on Windows).
    """
    lower = guard.lower()

    # --- pytest ---
    if lower == "pytest" or lower.startswith("pytest "):
        args = guard[len("pytest") :].strip()
        cmd = [sys.executable, "-m", "pytest", "-q"]
        if "--tb" not in args:
            cmd.append("--tb=short")
        if args:
            cmd += shlex.split(args)
        return cmd

    # --- npm test / npm run <x> ---
    if lower == "npm test":
        return ["npm", "run", "test"]
    if lower.startswith("npm run "):
        target = guard[len("npm run ") :].strip()
        return ["npm", "run", target]
    if lower.startswith("npm test "):
        # treat "npm test <script>" as "npm run test <script>"
        rest = guard[len("npm test ") :].strip()
        return ["npm", "run", "test", rest]

    # --- cargo test ---
    if lower == "cargo test" or lower.startswith("cargo test "):
        return shlex.split(guard)

    # --- go test ---
    if lower.startswith("go test"):
        return shlex.split(guard)

    # --- make <target> ---
    if lower.startswith("make"):
        return shlex.split(guard)

    # --- shell script (.sh) ---
    if guard.endswith(".sh"):
        if sys.platform == "win32":
            print(
                f"  ERROR: shell scripts (.sh) are not supported on Windows. Guard: {guard!r}",
                file=sys.stderr,
            )
            return None
        # Resolve relative to worktree root
        script_path = worktree / guard
        if not script_path.exists():
            print(
                f"  ERROR: safeguard script not found: {script_path}",
                file=sys.stderr,
            )
            return None
        return [str(script_path)]

    # --- fallback: shlex.split (shell=False for security) ---
    try:
        parts = shlex.split(guard)
    except ValueError as exc:
        print(f"  ERROR: could not parse guard {guard!r}: {exc}", file=sys.stderr)
        return None
    if not parts:
        return None
    return parts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def effective_safeguards(stage: str, ticket: dict, cfg: dict) -> list[str]:
    """Return the guard commands that apply to *stage*.

    Per-ticket safeguards are restricted for security: they may only add guards
    to the project-level config, never remove them, and they cannot use shell
    scripts (.sh files) or other high-privilege guard types.

    Only project-level safeguards are used unless the ticket is marked as
    trusted. Built-in guards (pytest, npm, cargo, go, make) are safe in
    per-ticket config, but shell scripts must be project-level only.
    """
    project_guards = (cfg.get("safeguards") or {}).get(stage, [])
    if project_guards is None:
        project_guards = []
    if isinstance(project_guards, str):
        project_guards = [project_guards]
    else:
        project_guards = list(project_guards)

    ticket_safeguards = ticket.get("safeguards") or {}
    if not isinstance(ticket_safeguards, dict) or stage not in ticket_safeguards:
        return project_guards

    ticket_guards = ticket_safeguards.get(stage) or []
    if isinstance(ticket_guards, str):
        ticket_guards = [ticket_guards]
    else:
        ticket_guards = list(ticket_guards)

    if ticket.get("trusted") is True:
        return project_guards + ticket_guards

    filtered_ticket_guards = []
    for guard in ticket_guards:
        if _is_safe_guard_for_ticket(guard):
            filtered_ticket_guards.append(guard)

    return project_guards + filtered_ticket_guards


def _is_safe_guard_for_ticket(guard: str) -> bool:
    """Check if a guard is safe to use in a per-ticket safeguards config.

    Per-ticket guards must be one of the built-in types (pytest, npm, cargo, go,
    make). Shell scripts (.sh) are not allowed per-ticket as they could be
    modified by the agent during implementation.
    """
    guard = guard.strip().lower()

    if guard.endswith(".sh"):
        return False

    if guard.startswith(("pytest", "npm", "cargo", "go", "make")):
        return True

    return False


def _check_script_guard_conflicts(ticket: dict) -> list[str]:
    """Check that no guard script appears in the ticket's touches list.

    Returns error strings if conflicts are found; empty list if safe.
    This prevents agents from modifying guard scripts before they execute.
    """
    errors: list[str] = []
    touches = ticket.get("touches") or []
    safeguards = ticket.get("safeguards") or {}

    if not isinstance(safeguards, dict):
        return errors

    for stage, guards in safeguards.items():
        guard_list = [guards] if isinstance(guards, str) else (guards or [])
        for guard in guard_list:
            if not isinstance(guard, str):
                continue
            if guard.endswith(".sh"):
                guard_path = guard
                if guard_path in touches:
                    errors.append(
                        f"safeguards[{stage!r}] script {guard_path!r} cannot be in touches "
                        f"(agent could modify the guard before it runs)"
                    )

    return errors


def run_safeguards(
    stage: str,
    ticket: dict,
    cfg: dict,
    worktree: Path,
    *,
    timed_out_guards: list[str] | None = None,
    label: str | None = None,
) -> tuple[bool, str | None]:
    """Run all guards for *stage*.

    Resolution order: per-ticket safeguards override project-level ones.
    If neither defines anything for the stage, returns True immediately
    (no regression when safeguards are not configured).

    Args:
        stage:            ``"pre_complete"``, ``"pre_merge"``, or ``"post_merge"``.
                          Selects which configured guard list to run.
        ticket:           Parsed ticket dict (may contain a ``safeguards`` key).
        cfg:              Project config dict (may contain a ``safeguards`` key).
        worktree:         Absolute path to the checkout where guards should run.
        timed_out_guards: Optional list; any guard that specifically timed out
                          (rather than just failing) is appended to it, so
                          callers can report a distinct "safeguard timed out"
                          reason instead of a generic failure.
        label:            Optional override for the ``[stage]`` text in PASS/FAIL
                          log lines, for callers that re-run *stage*'s guard list
                          in a different context (e.g. ``cmd_merge`` re-running
                          the ``pre_merge`` list against the merged main tree
                          instead of the ticket's own worktree).

    Returns:
        (True, None)      — all guards passed (or no guards configured).
        (False, reason)   — at least one guard failed; reason describes the failure.
    """
    conflicts = _check_script_guard_conflicts(ticket)
    if conflicts:
        for conflict in conflicts:
            print(f"  ERROR: {conflict}", file=sys.stderr)
        return False, "; ".join(conflicts)

    if stage in ("pre_complete", "pre_merge"):
        tickets_dir = worktree / cfg.get("tickets_dir", ".lanegate/tickets")
        artifact_violations = _find_prompt_artifact_violations(
            tickets_dir, own_ticket_id=ticket.get("id")
        )
        if artifact_violations:
            for violation in artifact_violations:
                print(f"  ERROR: {violation}", file=sys.stderr)
            return False, "; ".join(artifact_violations)

    guards = effective_safeguards(stage, ticket, cfg)

    if not guards:
        return True, None

    # Extract safeguards config (timeout and retry settings)
    safeguards_cfg = cfg.get("safeguards") or {}
    timeout_s = safeguards_cfg.get("timeout_s")  # None if not set
    retry_on_failure = safeguards_cfg.get("retry_on_failure", 0)  # default 0
    print_label = label or stage

    for guard in guards:
        # A safeguard can take minutes.  Flush so the progress frame remains
        # visible when the CLI's stdout is redirected rather than a terminal.
        print(f"  RUN  [{print_label}] {guard}", flush=True)
        start = time.monotonic()
        passed, reason = _run_one_guard(
            guard,
            worktree,
            timeout_s=timeout_s,
            retry_count=retry_on_failure,
            timed_out=timed_out_guards,
        )
        elapsed = time.monotonic() - start
        if not passed:
            print(f"  FAIL [{print_label}] {guard} ({elapsed:.1f}s)", file=sys.stderr)
            return False, f"[{stage}] {guard}: {reason}"
        else:
            print(f"  PASS [{print_label}] {guard} ({elapsed:.1f}s)")
    return True, None
