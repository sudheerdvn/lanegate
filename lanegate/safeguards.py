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
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

# Keep this private alias: tests patch only safeguards' process creation,
# without mutating the process-wide ``subprocess`` module used by lifecycle.
from subprocess import Popen as _Popen

import portalocker

_SAFEGUARD_LOCK = threading.Lock()

def is_control_plane_file(path: str | Path, cfg: dict | None = None) -> bool:
    """Return True if path refers to a control-plane or trust-boundary file.

    Control-plane files are project-specific and come only from
    ``control_plane_files`` in ``.lanegate.yml`` (``cfg``) -- never hardcoded
    here, since this module ships to every project that installs LaneGate,
    not just this repo's own self-hosted checkout.
    """
    control_plane_files = (cfg or {}).get("control_plane_files") or []
    if not control_plane_files:
        return False
    p = Path(path)
    if p.is_absolute():
        for root in ((cfg or {}).get("repo_root"), (cfg or {}).get("worktree"), Path.cwd()):
            if root:
                try:
                    p = p.relative_to(Path(root))
                    break
                except ValueError:
                    pass

    p_str = str(p).replace("\\", "/")
    if p_str.startswith("./"):
        p_str = p_str[2:]

    is_abs = p.is_absolute()
    for cp in control_plane_files:
        cp_norm = str(cp).replace("\\", "/")
        if cp_norm.startswith("./"):
            cp_norm = cp_norm[2:]
        if p_str == cp_norm:
            return True
        if is_abs and p_str.endswith("/" + cp_norm) and not any(part in ("third_party", "vendor") for part in p.parts):
            return True
    return False


def collect_control_plane_touches(
    ticket: dict, worktree: Path | None, cfg: dict | None
) -> tuple[list[str], str | None]:
    """Gather every control-plane file touched by this ticket, plus its current branch.

    Checks the ticket's declared ``touches``, uncommitted/staged working-tree
    changes, and every commit ahead of the last known-shared state -- not
    just the most recent commit, which would miss earlier control-plane
    edits in a multi-commit direct-to-trunk session. When already on trunk,
    "last known-shared state" is the remote-tracking ref (``origin/<trunk>``)
    if one resolves, since a bare ``trunk...HEAD`` self-diff is always empty.
    """
    if not (cfg or {}).get("control_plane_files"):
        return [], None

    touches = [str(p) for p in (ticket.get("touches") or [])]
    found = [t for t in touches if is_control_plane_file(t, cfg)]

    has_git = worktree is not None and worktree.exists() and (worktree / ".git").exists()
    current_branch: str | None = None
    if not has_git:
        return found, current_branch

    trunk = (cfg or {}).get("trunk_branch", "main")
    try:
        res_branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=worktree, capture_output=True, text=True, check=False,
        )
        if res_branch.returncode == 0:
            current_branch = res_branch.stdout.strip()

        # Uncommitted / staged changes.
        res_status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree, capture_output=True, text=True, check=False,
        )
        if res_status.returncode == 0:
            for line in res_status.stdout.splitlines():
                if len(line) < 3:
                    continue
                raw_paths = line[3:].strip()
                if " -> " in raw_paths:
                    paths = [p.strip().strip('"') for p in raw_paths.split(" -> ")]
                else:
                    paths = [raw_paths.strip('"')]
                for p in paths:
                    if p and is_control_plane_file(p, cfg):
                        found.append(p)

        on_trunk = current_branch in (trunk, "main", "master")
        if on_trunk:
            # trunk...HEAD is always empty when HEAD is trunk itself; compare
            # against the remote-tracking ref instead so every local commit
            # since the last known-shared state is inspected, not just the
            # last one.
            res_remote = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", f"origin/{trunk}"],
                cwd=worktree, capture_output=True, text=True, check=False,
            )
            base_ref = f"origin/{trunk}" if res_remote.returncode == 0 else None
        else:
            base_ref = trunk

        if base_ref:
            res_diff = subprocess.run(
                ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
                cwd=worktree, capture_output=True, text=True, check=False,
            )
            if res_diff.returncode == 0:
                for line in res_diff.stdout.splitlines():
                    f = line.strip()
                    if f and is_control_plane_file(f, cfg):
                        found.append(f)
        elif on_trunk:
            # No remote-tracking ref to compare against: fall back to
            # scanning every commit in the repo's local history rather than
            # silently checking nothing, or only the last commit.
            res_log = subprocess.run(
                ["git", "log", "--name-only", "--pretty=format:"],
                cwd=worktree, capture_output=True, text=True, check=False,
            )
            if res_log.returncode == 0:
                for line in res_log.stdout.splitlines():
                    f = line.strip()
                    if f and is_control_plane_file(f, cfg):
                        found.append(f)
    except Exception:
        pass

    return found, current_branch


def _check_control_plane_branch_isolation(ticket: dict, worktree: Path, cfg: dict) -> list[str]:
    """Verify control-plane files are modified within a ticket worktree and not directly on main."""
    if ticket.get("status") in ("merged", "post_merge"):
        return []

    modified_control_files, current_branch = collect_control_plane_touches(ticket, worktree, cfg)
    if not modified_control_files:
        return []

    trunk_branch = (cfg or {}).get("trunk_branch", "main")
    if current_branch == trunk_branch or current_branch in ("main", "master") or ticket.get("is_main") or not ticket.get("id"):
        cp_list = ", ".join(sorted(set(modified_control_files)))
        return [
            f"Control-plane files ({cp_list}) must be modified within a ticket worktree, never directly on {trunk_branch}."
        ]

    return []




def _safeguard_lock_root(worktree: Path) -> Path:
    """Return the shared checkout for a standard linked worktree.

    ``git worktree`` stores a ``.git`` *file* in linked worktrees.  Its
    ``gitdir:`` pointer normally has the form
    ``<control>/.git/worktrees/<name>``.  Reading that pointer avoids a Git
    subprocess here: guard tests deliberately mock subprocess execution, and
    acquiring a lock must not add another command to the guard being tested.
    """
    git_pointer = worktree / ".git"
    if not git_pointer.is_file():
        return worktree
    try:
        text = git_pointer.read_text(encoding="utf-8").strip()
    except OSError:
        return worktree
    if not text.startswith("gitdir: "):
        return worktree

    git_dir = Path(text.removeprefix("gitdir: "))
    if not git_dir.is_absolute():
        git_dir = (worktree / git_dir).resolve()
    if git_dir.parent.name == "worktrees" and git_dir.parent.parent.name == ".git":
        return git_dir.parent.parent.parent
    return worktree


@contextmanager
def _safeguard_file_lock(worktree: Path):
    """Serialize guards across every worktree of the same control checkout.

    A ticket worktree has its own ``.lanegate`` directory, so using a lock
    relative to *worktree* would not coordinate parallel executors.  Resolve
    the shared control checkout first and place the lock there instead.
    """
    lock_dir = _safeguard_lock_root(worktree) / ".lanegate"

    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    lock_file = lock_dir / "safeguard.lock"
    lock: portalocker.Lock
    try:
        # Do not turn ordinary queueing behind another long-running guard into
        # a failed test result.  First probe without waiting so operators see
        # real contention, then wait for the holder to finish.
        lock = portalocker.Lock(
            str(lock_file), "a", timeout=0, fail_when_locked=True
        )
        lock.acquire()
    except portalocker.exceptions.LockException:
        print("  WAIT [safeguard-lock] another worktree is running safeguards", flush=True)
        # portalocker interprets ``None`` as its five-second default, not an
        # infinite wait.  Guard contention is ordinary queueing, so it must
        # remain pending until the current guard completes rather than fail or
        # crash a ticket after that default interval.
        lock = portalocker.Lock(str(lock_file), "a", timeout=math.inf)
        lock.acquire()
    try:
        yield
    finally:
        lock.release()


@contextmanager
def _safeguard_execution_lock(worktree: Path):
    """Serialize guards in this process and across sibling worktrees.

    The thread lock is necessary because file-lock behavior for two handles in
    the same process differs by platform.  The portalocker lock then extends
    that serialization to other LaneGate processes.  Neither layer converts
    normal contention into a failed safeguard; both queue visibly instead.
    """
    acquired = _SAFEGUARD_LOCK.acquire(blocking=False)
    if not acquired:
        print("  WAIT [safeguard-lock] another local guard is running", flush=True)
        _SAFEGUARD_LOCK.acquire()
    try:
        with _safeguard_file_lock(worktree):
            yield
    finally:
        _SAFEGUARD_LOCK.release()


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

# A normal ticket markdown file (title + body + criteria + readable body sections
# such as Change Notes, Acceptance Contract Audit, and Lifecycle Timeline) does
# not naturally approach this size; TICK-259 observed a 25KB+ prompt get
# echoed back into persisted state, so this catches that class of artifact
# without depending on the executor whose prompt it was.
_MAX_TICKET_MARKDOWN_BYTES = 40000

_OPERATIONAL_SECTION_RE = re.compile(
    r"(?:^|\n)##\s*(Needs Review Reason|Hibernation Reason|Auto-Fix Attempt \d+"
    r"|Review Findings|Dismissal Rationale|Acceptance Contract Audit|Status History|Lifecycle Timeline"
    r"|Post-Merge Verification Diagnostic)"
    r".*?(?=\n##\s|\Z)",
    re.IGNORECASE | re.DOTALL,
)

_PROMPT_ARTIFACT_PATTERNS = (
    r"\{[^\n{}]*\"(?:role|type)\"\s*:\s*\"(?:user|assistant|system|USER_INPUT|PLANNER_RESPONSE|SYSTEM|USER_EXPLICIT|MODEL)\"",
    r"\"(tool_calls|tool_use|tool_result|planner_response|user_input)\"\s*:",
    r"<\/?(system|assistant|untrusted-data|user-request|task|instructions)>",
    r"===\s*(PROMPT|TRANSCRIPT)\s*===",
    r"(BEGIN|END)\s*(PROMPT|TRANSCRIPT)",
    r"(System|User|Assistant)\s+Prompt\s*:",
    r"You are an agent\.\s+Follow ONLY the instructions",
    r"You are an AI\b",
    r"You are pair programming with a USER",
    r"\[INST\]|\[\/INST\]|<<SYS>>|<\|im_start\|>|<\|im_end\|>",
    r"ignore\s+(previous|above|prior)\s+instructions?",
    r"disregard\s+(the\s+)?(above|previous|prior)",
    r"new\s+(system\s+)?(prompt|instructions?)\s*:",
)

_PROMPT_ARTIFACT_PATTERNS_RE = re.compile(
    "|".join(_PROMPT_ARTIFACT_PATTERNS),
    re.IGNORECASE,
)


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
    old embedded ``file_skeletons``, ``change_notes``, or ``acceptance_contract_audit``
    blob predating sidecar/readable-body section migration) has nothing to do with
    whether *this* ticket's body just got polluted with a pasted transcript --
    it shouldn't block every future unrelated merge until someone notices and
    manually trims it. When *own_ticket_id* is None (the default, used by
    direct/legacy callers), every ``.md`` file is checked.
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
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    content = ""

                is_artifact = False

                if _PROMPT_ARTIFACT_PATTERNS_RE.search(content):
                    is_artifact = True

                if not is_artifact:
                    stripped = _OPERATIONAL_SECTION_RE.sub("", content)
                    if len(stripped.encode("utf-8")) > _MAX_TICKET_MARKDOWN_BYTES:
                        is_artifact = True

                if not is_artifact:
                    sections = re.split(r"\n(?=##\s)", stripped)
                    for sec in sections:
                        if len(sec.encode("utf-8")) > _MAX_TICKET_MARKDOWN_BYTES:
                            is_artifact = True
                            break

                if is_artifact:
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

    res = _resolve_command(guard, worktree)
    if isinstance(res, tuple) and res[0] is None:
        cmd_name = res[1].split("unresolved: ", 1)[-1] if "unresolved: " in res[1] else res[1]
        return False, f"unresolved command: {cmd_name} not found on PATH"
    if res is None:
        return False, "command resolution failed"
    cmd = res

    # Retry loop: run up to (1 + retry_count) times
    attempts_remaining = 1 + retry_count
    while attempts_remaining > 0:
        attempts_remaining -= 1
        try:
            proc = _Popen(
                cmd,
                cwd=worktree,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                start_new_session=sys.platform != "win32",
            )
            try:
                stdout_data, stderr_data = proc.communicate(timeout=timeout_s)
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                # Forcefully terminate whole process group with SIGTERM then SIGKILL
                if sys.platform != "win32" and hasattr(os, "killpg"):
                    try:
                        pgid = os.getpgid(proc.pid)
                        os.killpg(pgid, signal.SIGTERM)
                        time.sleep(0.1)
                        os.killpg(pgid, signal.SIGKILL)
                    except OSError:
                        pass
                else:
                    proc.kill()
                proc.wait()
                print(f"  TIMEOUT: guard exceeded {timeout_s}s", file=sys.stderr)
                if timed_out is not None:
                    timed_out.append(guard)
                return False, f"timed out after {timeout_s}s"

            if returncode == 0:
                return True, None
            # Guard failed with non-zero exit; print error output so operator sees cause
            if stdout_data:
                print(stdout_data.strip(), file=sys.stderr)
            if stderr_data:
                print(stderr_data.strip(), file=sys.stderr)
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


def _check_path_resolution(cmd: list[str], worktree: Path | None = None) -> list[str] | tuple[None, str]:
    if cmd and not os.path.isabs(cmd[0]):
        if os.sep in cmd[0] or (os.altsep and os.altsep in cmd[0]) or "/" in cmd[0]:
            target = (worktree / cmd[0]) if worktree else Path(cmd[0])
            if not target.exists():
                return None, f"unresolved: {cmd[0]}"
        elif shutil.which(cmd[0]) is None:
            return None, f"unresolved: {cmd[0]}"
    return cmd


def _resolve_command(guard: str, worktree: Path) -> list[str] | tuple[None, str] | None:
    """Translate a guard string into an argv list ready for subprocess.run.

    Returns None when the guard cannot be resolved (e.g. .sh on Windows).
    Returns (None, "unresolved: <cmd>") when argv[0] is not found on PATH.
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
        return _check_path_resolution(["npm", "run", "test"], worktree)
    if lower.startswith("npm run "):
        target = guard[len("npm run ") :].strip()
        return _check_path_resolution(["npm", "run", target], worktree)
    if lower.startswith("npm test "):
        # treat "npm test <script>" as "npm run test <script>"
        rest = guard[len("npm test ") :].strip()
        return _check_path_resolution(["npm", "run", "test", rest], worktree)

    # --- cargo test ---
    if lower == "cargo test" or lower.startswith("cargo test "):
        return _check_path_resolution(shlex.split(guard), worktree)

    # --- go test ---
    if lower.startswith("go test"):
        return _check_path_resolution(shlex.split(guard), worktree)

    # --- make <target> ---
    if lower.startswith("make"):
        return _check_path_resolution(shlex.split(guard), worktree)

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
    return _check_path_resolution(parts, worktree)


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

    if stage != "post_merge" and label != "post_merge_verify" and (not label or "post_merge" not in label) and ticket.get("status") not in ("merged", "post_merge"):
        cp_violations = _check_control_plane_branch_isolation(ticket, worktree, cfg)
        if cp_violations:
            for violation in cp_violations:
                print(f"  ERROR: {violation}", file=sys.stderr)
            return False, "; ".join(cp_violations)


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

    with _safeguard_execution_lock(worktree):
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
