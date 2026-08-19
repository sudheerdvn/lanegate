"""Human-friendly rendering for LaneGate log files."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

_ANSI_STYLES = {
    "bold red": "\033[1;31m",
    "red": "\033[31m",
    "yellow": "\033[33m",
    "green": "\033[32m",
    "magenta": "\033[35m",
    "bold blue": "\033[1;34m",
    "cyan": "\033[36m",
    "dim": "\033[2m",
}
_ANSI_RESET = "\033[0m"
_SUPPORTED_VIEWERS = {"lnav", "multitail", "colortail"}


def analyze_log_path(repo_root: Path, timestamp: datetime | None = None) -> Path:
    """Return a new timestamped log path for one standalone analysis."""
    stamp = (timestamp or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S%fZ")
    logs_dir = repo_root / ".lanegate" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir / f"analyze-{stamp}.log"


def write_analysis_event(log_file: Path, phase: str, details: str) -> None:
    """Append one durable, human-readable standalone-analysis event."""
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    safe_details = str(details).replace("\x00", "")
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(f"{timestamp} [analyze] {phase}: {safe_details}\n")


def _latest_log_path(repo_root: Path) -> Path | None:
    logs_dir = repo_root / ".lanegate" / "logs"
    if not logs_dir.exists():
        return None
    logs = [p for p in logs_dir.glob("*.log") if p.is_file()]
    if not logs:
        return None
    return max(logs, key=lambda p: p.stat().st_mtime)


def _tail_lines(path: Path, lines: int) -> list[str]:
    if lines <= 0:
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise RuntimeError(f"cannot read log file {path}: {exc}") from exc


def _line_style(line: str) -> str:
    stripped = line.lstrip()
    upper = line.upper()
    lower = line.lower()

    if (
        re.match(
            r"^(?:ERROR:|\[orchestrate\]\s+ERROR\b|Traceback \(most recent call last\)|"
            r"FAILED\b|\[orchestrate\].*\bfailed\b)",
            stripped,
            re.IGNORECASE,
        )
        or re.search(r"\(exit\s+(?:code\s+)?-?[1-9]\d*(?:[,)\s:])", stripped, re.IGNORECASE)
    ):
        return "bold red"
    if "WARNING" in upper or "CHANGES_REQUESTED" in upper:
        return "yellow"
    if line.startswith("+") and not line.startswith("+++"):
        return "green"
    if line.startswith("-") and not line.startswith("---"):
        if re.match(r"^-(?:\t| {1,3})\S", line):
            pass
        else:
            return "red"
    if stripped.startswith("@@"):
        return "magenta"
    if stripped.startswith(("diff --git", "index ", "+++", "---")):
        return "bold blue"
    if line.startswith("[orchestrate]") or line.startswith("  ") and "[" in line and "]" in line:
        return "cyan"
    if (
        re.search(r":\s*(?:passed|approved|merged)\.?\s*$", lower)
        or re.search(r"^\s*\[?(?:passed|approved|merged)\]?:?\s*$", lower)
    ):
        return "green"
    return ""


def semantic_line_metadata(line: str) -> dict[str, str]:
    """Return the shared presentation metadata for one raw audit-log line.

    The CLI, API, and TUI consume the same classifier so an audit line has
    the same meaning everywhere. ``line`` itself is never altered: callers
    can safely retain it for copying, export, and durable incident recovery.
    """
    style = _line_style(line)
    if style in {"bold red", "red"}:
        level = "error"
    elif style == "yellow":
        level = "warning"
    elif style == "green":
        level = "success"
    else:
        level = "info"

    if line.lstrip().startswith("[orchestrate]"):
        kind = "orchestrator"
    elif line.lstrip().startswith(("{", "[")):
        try:
            json.loads(line)
            kind = "protocol"
        except (TypeError, json.JSONDecodeError):
            kind = "executor"
    else:
        kind = "executor"

    return {"style": style, "level": level, "kind": kind}


def _render_line(line: str, *, use_color: bool) -> None:
    style = _line_style(line)
    if use_color and style:
        code = _ANSI_STYLES.get(style)
        if code:
            print(f"{code}{line}{_ANSI_RESET}")
            return
    print(line)


def _use_color(color: str) -> bool:
    if color == "always":
        return True
    if color == "never":
        return False
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def _print_header(path: Path, *, use_color: bool) -> None:
    line = f"Log: {path}"
    if use_color:
        print(f"{_ANSI_STYLES['dim']}{line}{_ANSI_RESET}")
    else:
        print(line)


def _viewer_command(viewer: str, log_path: Path) -> list[str]:
    if viewer not in _SUPPORTED_VIEWERS:
        supported = ", ".join(sorted(_SUPPORTED_VIEWERS))
        raise SystemExit(f"Unsupported log viewer {viewer!r}. Supported viewers: {supported}.")
    exe = shutil.which(viewer)
    if exe is None:
        raise SystemExit(
            f"Log viewer {viewer!r} is not installed or not on PATH. "
            "Use the built-in renderer or install the viewer."
        )
    return [exe, str(log_path)]


def _open_with_viewer(viewer: str, log_path: Path) -> None:
    subprocess.run(_viewer_command(viewer, log_path), check=False)


def cmd_logs(
    repo_root: Path,
    *,
    path: Path | None = None,
    lines: int = 80,
    follow: bool = False,
    color: str = "auto",
    open_with: str | None = None,
) -> None:
    """Print a colorized tail of a LaneGate log file."""
    log_path = path or _latest_log_path(repo_root)
    if log_path is None:
        print("No LaneGate logs found under .lanegate/logs.")
        return
    if not log_path.exists():
        raise SystemExit(f"Log file not found: {log_path}")

    if open_with:
        _open_with_viewer(open_with, log_path)
        return

    use_color = _use_color(color)
    _print_header(log_path, use_color=use_color)

    all_lines = _tail_lines(log_path, lines)
    for line in all_lines[-lines:]:
        _render_line(line, use_color=use_color)

    if not follow:
        return

    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(0, 2)
        try:
            while True:
                line = handle.readline()
                if line:
                    _render_line(line.rstrip("\n"), use_color=use_color)
                    continue
                time.sleep(0.5)
        except KeyboardInterrupt:
            return
