"""
lanegate/orchestrate/audit.py — tee logging and executor audit-bundle capture.

Extracted from orchestrate.py (TICK-255/TICK-271/TICK-272): the stdout/log
tee, active-status file naming, and the executor audit-bundle capture/
manifest/gate-capture machinery that records what an executor did on a
ticket (prompt, transcript, task outputs, git snapshot, static-analysis
gate results) into .lanegate/executor-runs/<tid>/<session>/.
"""

from __future__ import annotations

import datetime
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from lanegate.timeutil import utc_now_iso as _utc_now_iso

_ACTIVE_STATUS_FILE = "active-orchestrate.json"
_MAX_AUDIT_FILE_BYTES = 2 * 1024 * 1024
_MAX_AUDIT_TEXT_BYTES = 512 * 1024

# ---------------------------------------------------------------------------
# Tee logging
# ---------------------------------------------------------------------------


class _LogTee:
    """Duplicate writes to both a stream and an open log file."""

    def __init__(self, stream, log_file):
        self._stream = stream
        self._log = log_file

    def write(self, data):
        try:
            self._stream.write(data)
        except (BrokenPipeError, OSError):
            pass
        self._log.write(data)
        self._log.flush()

    def flush(self):
        try:
            self._stream.flush()
        except (BrokenPipeError, OSError):
            pass
        self._log.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _status(tid: str, stage: str, orig_out, log_f=None) -> None:
    """Write a compact progress line to the terminal and optionally the log file."""
    ts = time.strftime("%H:%M:%S")
    line = f"  {ts}  {tid:10s}  [{stage}]\n"
    orig_out.write(line)
    orig_out.flush()
    if log_f is not None:
        log_f.write(line)
        log_f.flush()


def _active_status_path(repo_root: Path, session_id: str | None = None) -> Path:
    state = repo_root / ".lanegate"
    state.mkdir(parents=True, exist_ok=True)
    if session_id is None:
        return state / _ACTIVE_STATUS_FILE
    # Per-session status file to support concurrent executors without clobbering
    status_dir = state / "active-orchestrate"
    status_dir.mkdir(parents=True, exist_ok=True)
    return status_dir / f"{session_id}.json"


def _iso_from_epoch(ts: float) -> str:
    """Render a recorded epoch start time in the same shape as _utc_now_iso."""
    return datetime.datetime.fromtimestamp(ts, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _write_bounded_text(path: Path, text: str, *, limit: int = _MAX_AUDIT_TEXT_BYTES) -> dict:
    raw = text.encode("utf-8", errors="replace")
    truncated = len(raw) > limit
    if truncated:
        raw = raw[:limit] + b"\n[truncated by LaneGate audit capture]\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return {"path": str(path), "bytes": len(raw), "truncated": truncated}


def _copy_bounded_file(src: Path, dest: Path, *, limit: int = _MAX_AUDIT_FILE_BYTES) -> dict:
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    truncated = False
    with open(src, "rb") as in_f, open(dest, "wb") as out_f:
        while total < limit:
            chunk = in_f.read(min(1024 * 1024, limit - total))
            if not chunk:
                break
            out_f.write(chunk)
            total += len(chunk)
        if in_f.read(1):
            truncated = True
            out_f.write(b"\n[truncated by LaneGate audit capture]\n")
    return {
        "source": str(src),
        "path": str(dest),
        "bytes": dest.stat().st_size,
        "truncated": truncated,
    }


def _copy_formatted_jsonl(
    src: Path, dest: Path, *, limit: int = _MAX_AUDIT_FILE_BYTES
) -> dict:
    """Copy a JSONL artifact with each event rendered for human inspection.

    Events remain independent JSON values in their original order; pretty
    printing happens per source line rather than by collecting the transcript
    into an array.  Stop at an event boundary when the bounded capture fills
    up so captured JSON is never cut through the middle of an object.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    truncated = False
    with open(src, encoding="utf-8") as in_f, open(dest, "w", encoding="utf-8") as out_f:
        for source_line in in_f:
            if not source_line.strip():
                continue
            formatted = json.dumps(json.loads(source_line), indent=2) + "\n"
            encoded = formatted.encode("utf-8")
            if total + len(encoded) > limit:
                truncated = True
                break
            out_f.write(formatted)
            total += len(encoded)
        if not truncated and in_f.read(1):
            truncated = True
        if truncated:
            out_f.write("[truncated by LaneGate audit capture]\n")
    return {
        "source": str(src),
        "path": str(dest),
        "bytes": dest.stat().st_size,
        "truncated": truncated,
    }


def _safe_rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _audit_bundle_path(repo_root: Path, tid: str, session_id: str) -> Path:
    safe_session = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id)
    return repo_root / ".lanegate" / "executor-runs" / tid / safe_session


def _find_latest_audit_bundle(repo_root: Path, tid: str) -> Path | None:
    """Find the most recently created audit bundle for a ticket.

    Scans .lanegate/executor-runs/<tid>/ for session directories and returns the
    path to the most recent one by modification time. Used as a fallback when
    the shared status file cannot reliably track concurrent executors.
    """
    bundles_dir = repo_root / ".lanegate" / "executor-runs" / tid
    if not bundles_dir.exists():
        return None

    try:
        session_dirs = [d for d in bundles_dir.iterdir() if d.is_dir()]
        if not session_dirs:
            return None
        # Sort by modification time, return the most recent
        latest = max(session_dirs, key=lambda d: d.stat().st_mtime)
        return latest
    except (OSError, ValueError):
        return None


def has_step_bundle(repo_root: Path, tid: str, step: str) -> bool:
    """Return whether any audit bundle for *tid* records the requested step."""
    bundles_dir = repo_root / ".lanegate" / "executor-runs" / tid
    if not bundles_dir.exists():
        return False

    try:
        session_dirs = [d for d in bundles_dir.iterdir() if d.is_dir()]
    except OSError:
        return False

    for session_dir in session_dirs:
        try:
            status = json.loads((session_dir / "status.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        if status.get("step") == step:
            return True
    return False


def _new_manifest(status: dict, bundle_path: Path) -> dict:
    return {
        "schema_version": 1,
        "created_at": _utc_now_iso(),
        "ticket_id": status.get("ticket_id"),
        "executor": status.get("executor"),
        "executor_session": status.get("executor_session"),
        "step": status.get("step"),
        "worktree": status.get("worktree"),
        "bundle_path": str(bundle_path),
        "captured": {},
        "missing": [],
    }


def _manifest_capture(manifest: dict, name: str, detail: dict) -> None:
    manifest.setdefault("captured", {})[name] = detail


def _manifest_missing(manifest: dict, name: str, reason: str) -> None:
    manifest.setdefault("missing", []).append({"artifact": name, "reason": reason})


def _load_audit_manifest(bundle_path: Path) -> dict:
    manifest_path = bundle_path / "manifest.json"
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {
            "schema_version": 1,
            "created_at": _utc_now_iso(),
            "bundle_path": str(bundle_path),
            "captured": {},
            "missing": [],
        }


def _save_audit_manifest(bundle_path: Path, manifest: dict) -> None:
    manifest["updated_at"] = _utc_now_iso()
    _write_json_atomic(bundle_path / "manifest.json", manifest)


def _run_git_snapshot(worktree_path: Path, args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=str(worktree_path),
            capture_output=True,
            text=True, encoding="utf-8",
            timeout=30,
        )
    except Exception as exc:
        return f"[lanegate audit] git {' '.join(args)} failed: {exc}\n"
    text = result.stdout
    if result.stderr:
        text += ("\n" if text else "") + result.stderr
    if result.returncode != 0:
        text += f"\n[lanegate audit] git exited {result.returncode}\n"
    return text


def _claude_encoded_cwd(worktree_path: Path) -> str:
    # Mirrors how Claude Code names its own transcript directory from cwd.
    # str(path) uses backslashes on Windows, which a bare "/" replace leaves
    # untouched -- those would then be re-parsed as extra path segments when
    # this result gets joined onto another Path. Also strip the drive-letter
    # colon (e.g. "C:"), which is an invalid character in a Windows path
    # *component* outside the drive-root position.
    return str(worktree_path.resolve()).replace("\\", "-").replace("/", "-").replace(":", "")


def _mtime_in_window(path: Path, started_at: float | None, finished_at: float | None) -> bool:
    if started_at is None:
        return True
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    upper = (finished_at if isinstance(finished_at, (int, float)) else time.time()) + 300
    return (started_at - 300) <= mtime <= upper


def _find_claude_transcript(worktree_path: Path, status: dict) -> tuple[Path | None, str]:
    session_id = str(status.get("executor_session") or "")
    if not session_id:
        return None, "no executor_session recorded in status; cannot identify transcript"
    project_dir = Path.home() / ".claude" / "projects" / _claude_encoded_cwd(worktree_path)
    if not project_dir.exists():
        return None, f"Claude project transcript directory not found: {project_dir}"
    candidates = list(project_dir.glob("*.jsonl"))
    if not candidates:
        return None, f"no Claude jsonl transcripts found in {project_dir}"
    for candidate in candidates:
        if candidate.stem == session_id or candidate.name == f"{session_id}.jsonl":
            return candidate, ""
    return None, f"no transcript found matching session_id {session_id!r}"


def _copy_claude_task_outputs(
    worktree_path: Path, status: dict, transcript_path: Path | None, bundle_path: Path
) -> tuple[list[dict], str | None]:
    encoded = _claude_encoded_cwd(worktree_path)
    session_candidates = [str(status.get("executor_session") or "")]
    if transcript_path is not None:
        session_candidates.insert(0, transcript_path.stem)
    copied: list[dict] = []
    searched: list[str] = []
    for base in sorted(Path("/tmp").glob("claude-*")):
        for session in [s for s in session_candidates if s]:
            tasks_dir = base / encoded / session / "tasks"
            searched.append(str(tasks_dir))
            if not tasks_dir.exists():
                continue
            for src in sorted(tasks_dir.glob("*.output"))[:50]:
                copied.append(_copy_bounded_file(src, bundle_path / "tasks" / src.name))
            if copied:
                return copied, None
    return copied, "no Claude background task outputs found" + (
        f" under {', '.join(searched[:3])}" if searched else ""
    )


def _codex_session_dirs(started_at: float | None, finished_at: float | None) -> list[Path]:
    base = Path.home() / ".codex" / "sessions"
    if not base.exists():
        return []
    stamps: set[tuple[int, int, int]] = set()
    for ts in (started_at, finished_at, time.time()):
        if isinstance(ts, (int, float)):
            dt = datetime.datetime.fromtimestamp(ts)
            stamps.add((dt.year, dt.month, dt.day))
    return [base / f"{y:04d}" / f"{m:02d}" / f"{d:02d}" for y, m, d in sorted(stamps)]


def _find_codex_transcript(status: dict) -> tuple[Path | None, str]:
    session_id = str(status.get("executor_session") or "")
    dirs = _codex_session_dirs(status.get("started_at"), status.get("finished_at"))
    if not dirs:
        return None, f"Codex sessions directory not found: {Path.home() / '.codex' / 'sessions'}"
    candidates: list[Path] = []
    for day_dir in dirs:
        if day_dir.exists():
            candidates.extend(day_dir.glob("rollout-*.jsonl"))
    if not candidates:
        return None, "no Codex rollout transcripts found for this run date"
    for candidate in candidates:
        if session_id and session_id in candidate.name:
            return candidate, ""
    recent = [p for p in candidates if _mtime_in_window(p, status.get("started_at"), status.get("finished_at"))]
    if not recent:
        return None, "no Codex transcript matched this run window"
    recent.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return recent[0], ""


# Steps whose value as an audit record is the transcript itself, so the bundle
# keeps stdout/stderr regardless of exit code.  Implement/fix are excluded on
# purpose: their evidence is the diff, and capturing every successful
# implementation transcript would bloat every bundle.
_ALWAYS_CAPTURE_OUTPUT_STEPS = frozenset({"review", "drift_check"})


def _capture_executor_audit_bundle(
    repo_root: Path,
    worktree_path: Path,
    status: dict,
    *,
    log_stream=None,
    captured_stdout: str = "",
    captured_stderr: str = "",
) -> Path:
    tid = str(status.get("ticket_id") or "unknown")
    session_id = str(status.get("executor_session") or f"session-{int(time.time())}")
    bundle_path = _audit_bundle_path(repo_root, tid, session_id)
    bundle_path.mkdir(parents=True, exist_ok=True)
    manifest = _new_manifest(status, bundle_path)

    prompt_value = status.get("prompt_path")
    prompt_path = Path(str(prompt_value)) if prompt_value else None
    if prompt_path is not None and prompt_path.exists():
        detail = _copy_bounded_file(prompt_path, bundle_path / "prompt.md")
        _manifest_capture(manifest, "prompt.md", detail)
    else:
        _manifest_missing(
            manifest,
            "prompt.md",
            f"prompt path missing: {prompt_path}" if prompt_path else "prompt was not persisted",
        )

    # Raw captured stdout/stderr, persisted on any non-zero exit (not just
    # ones already classified as a rate limit) so a future misclassification
    # is diagnosable from the bundle alone instead of requiring a live rerun.
    #
    # Review-class steps persist it on success too: the whole point of a
    # review bundle is showing what the reviewer actually said, and the
    # verdict-bearing transcript arrives on the common exit-0 path.  Callers
    # are responsible for redacting before handing text here (see
    # review.py/autofix.py), which is why this stays a plain write.
    exit_code = status.get("exit_code")
    if status.get("step") in _ALWAYS_CAPTURE_OUTPUT_STEPS or exit_code not in (0, None):
        if captured_stdout.strip() or captured_stderr.strip():
            captured_text = (
                f"--- stdout ---\n{captured_stdout}\n--- stderr ---\n{captured_stderr}\n"
            )
            detail = _write_bounded_text(bundle_path / "captured-output.txt", captured_text)
            _manifest_capture(manifest, "captured-output.txt", detail)
        else:
            _manifest_missing(
                manifest, "captured-output.txt", "no stdout/stderr captured for this exit"
            )

    _write_json_atomic(bundle_path / "status.json", status)
    _manifest_capture(manifest, "status.json", {"path": str(bundle_path / "status.json")})

    log_path = status.get("log_path")
    if log_path:
        ref = f"{log_path}\n"
        _write_bounded_text(bundle_path / "orchestrate-log-ref.txt", ref)
        _manifest_capture(
            manifest,
            "orchestrate-log-ref.txt",
            {"path": str(bundle_path / "orchestrate-log-ref.txt"), "target": str(log_path)},
        )
    else:
        _manifest_missing(manifest, "orchestrate-log-ref.txt", "no orchestrate log path in status")

    git_status = _run_git_snapshot(worktree_path, ["status", "--short", "--branch"])
    _write_bounded_text(bundle_path / "git-status.txt", git_status)
    _manifest_capture(manifest, "git-status.txt", {"path": str(bundle_path / "git-status.txt")})

    diff_stat = _run_git_snapshot(worktree_path, ["diff", "--stat"])
    _write_bounded_text(bundle_path / "diff-stat.txt", diff_stat)
    _manifest_capture(manifest, "diff-stat.txt", {"path": str(bundle_path / "diff-stat.txt")})

    executor = str(status.get("executor") or "")
    transcript: Path | None = None
    transcript_reason = ""
    if executor in ("claude", "claude-process", "claude-subagent"):
        transcript, transcript_reason = _find_claude_transcript(worktree_path, status)
    elif executor == "codex":
        transcript, transcript_reason = _find_codex_transcript(status)
    else:
        transcript_reason = f"executor {executor!r} has no LaneGate transcript discovery adapter"

    if transcript is not None:
        detail = _copy_formatted_jsonl(transcript, bundle_path / "executor-session.jsonl")
        _manifest_capture(manifest, "executor-session.jsonl", detail)
    else:
        _manifest_missing(manifest, "executor-session.jsonl", transcript_reason)

    if executor in ("claude", "claude-process", "claude-subagent"):
        copied, reason = _copy_claude_task_outputs(worktree_path, status, transcript, bundle_path)
        if copied:
            _manifest_capture(manifest, "tasks", {"files": copied})
        elif reason:
            _manifest_missing(manifest, "tasks", reason)
    else:
        _manifest_missing(manifest, "tasks", f"executor {executor!r} does not use Claude task outputs")

    _manifest_missing(manifest, "gates", "static-analysis gate has not run yet")
    _save_audit_manifest(bundle_path, manifest)
    if log_stream is not None:
        log_stream.write(f"[orchestrate] executor audit bundle: {bundle_path}\n")
        log_stream.flush()
    return bundle_path


def _start_gate_capture(audit_bundle_path: Path | None, cfg: dict) -> tuple[Path | None, list[dict]]:
    if audit_bundle_path is None:
        return None, []
    gates_dir = audit_bundle_path / "gates"
    gates_dir.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(gates_dir / "config.json", cfg.get("static_analysis") or {})
    return gates_dir, []


def _record_gate(
    records: list[dict],
    tool: str,
    status: str,
    *,
    reason: str | None = None,
    command: list[str] | None = None,
    returncode: int | None = None,
    stdout: str | None = None,
    stderr: str | None = None,
) -> None:
    item: dict = {"tool": tool, "status": status}
    if reason:
        item["reason"] = reason
    if command is not None:
        item["command"] = command
    if returncode is not None:
        item["returncode"] = returncode
    if stdout is not None:
        item["stdout"] = stdout
    if stderr is not None:
        item["stderr"] = stderr
    records.append(item)


def _artifact_safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "tool"


def _run_gate_command(
    gates_dir: Path | None,
    records: list[dict],
    tool: str,
    cmd: list[str],
    **kwargs,
):
    result = subprocess.run(cmd, **kwargs)
    stdout_path = None
    stderr_path = None
    if gates_dir is not None:
        safe = _artifact_safe_name(tool)
        stdout_path = str(gates_dir / f"{safe}-stdout.txt")
        stderr_path = str(gates_dir / f"{safe}-stderr.txt")
        _write_bounded_text(Path(stdout_path), getattr(result, "stdout", "") or "")
        _write_bounded_text(Path(stderr_path), getattr(result, "stderr", "") or "")
    _record_gate(
        records,
        tool,
        "ran",
        command=list(cmd),
        returncode=getattr(result, "returncode", None),
        stdout=stdout_path,
        stderr=stderr_path,
    )
    return result


def _finish_gate_capture(
    audit_bundle_path: Path | None,
    gates_dir: Path | None,
    records: list[dict],
    *,
    findings: list[str],
    decision: dict | None = None,
) -> None:
    if audit_bundle_path is None or gates_dir is None:
        return
    summary = {
        "schema_version": 1,
        "created_at": _utc_now_iso(),
        "tools": records,
        "findings": findings,
    }
    if decision is not None:
        summary["decision"] = decision
    _write_json_atomic(gates_dir / "summary.json", summary)

    manifest = _load_audit_manifest(audit_bundle_path)
    missing = [
        item for item in manifest.get("missing", []) if item.get("artifact") != "gates"
    ]
    manifest["missing"] = missing
    _manifest_capture(
        manifest,
        "gates",
        {
            "path": str(gates_dir),
            "summary": str(gates_dir / "summary.json"),
            "tool_count": len(records),
            "finding_count": len(findings),
        },
    )
    _save_audit_manifest(audit_bundle_path, manifest)


def _write_review_verdict(audit_bundle_path: Path | None, verdict: dict) -> None:
    """Persist a review-class step's parsed outcome next to its transcript.

    Written for every outcome — approve, changes_requested, subprocess error,
    ceiling kill — so a bundle always answers "what did this reviewer decide?"
    without re-parsing captured-output.txt.
    """
    if audit_bundle_path is None:
        return
    try:
        payload = {"schema_version": 1, "recorded_at": _utc_now_iso(), **verdict}
        verdict_path = audit_bundle_path / "verdict.json"
        _write_json_atomic(verdict_path, payload)
        manifest = _load_audit_manifest(audit_bundle_path)
        _manifest_capture(manifest, "verdict.json", {"path": str(verdict_path)})
        _save_audit_manifest(audit_bundle_path, manifest)
    except Exception as exc:  # audit persistence must not change a review verdict
        print(
            f"WARNING: could not write review verdict to {audit_bundle_path}: {exc}",
            file=sys.stderr,
        )


def _record_static_analysis_decision(
    audit_bundle_path: Path | None,
    *,
    findings: list[str],
    threshold: int,
    blocked: bool,
) -> None:
    if audit_bundle_path is None:
        return
    gates_dir = audit_bundle_path / "gates"
    gates_dir.mkdir(parents=True, exist_ok=True)
    decision = {
        "finding_count": len(findings),
        "threshold": threshold,
        "blocked": blocked,
        "decided_at": _utc_now_iso(),
    }
    _write_json_atomic(gates_dir / "decision.json", decision)
    summary_path = gates_dir / "summary.json"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        summary = {"schema_version": 1, "tools": [], "findings": findings}
    summary["decision"] = decision
    _write_json_atomic(summary_path, summary)
    manifest = _load_audit_manifest(audit_bundle_path)
    _manifest_capture(
        manifest,
        "gates",
        {
            "path": str(gates_dir),
            "summary": str(summary_path),
            "decision": str(gates_dir / "decision.json"),
            "finding_count": len(findings),
        },
    )
    manifest["missing"] = [
        item for item in manifest.get("missing", []) if item.get("artifact") != "gates"
    ]
    _save_audit_manifest(audit_bundle_path, manifest)
