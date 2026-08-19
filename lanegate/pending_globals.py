"""Pending global proposals inspector and notification helper."""

from __future__ import annotations

import re
from pathlib import Path
from lanegate import APP_NAME


def get_pending_globals_path(repo_root: Path) -> Path:
    """Return path to .lanegate/pending-globals.md."""
    return repo_root / f".{APP_NAME}" / "pending-globals.md"


def check_pending_globals(repo_root: Path) -> dict:
    """Inspect .lanegate/pending-globals.md for pending proposals.

    Returns dict with keys:
    - has_pending: bool
    - count: int (number of proposal blocks or bullet points)
    - path: Path
    - text: str
    """
    path = get_pending_globals_path(repo_root)
    if not path.is_file():
        return {"has_pending": False, "count": 0, "path": path, "text": ""}

    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return {"has_pending": False, "count": 0, "path": path, "text": ""}

    # Count ## [TICK-...] headers or non-header bullet items
    headers = re.findall(r"^##\s+\[.*?\]", text, flags=re.MULTILINE)
    bullets = re.findall(r"^\s*[-*]\s+", text, flags=re.MULTILINE)
    count = max(len(headers), len(bullets), 1 if text else 0)

    return {"has_pending": True, "count": count, "path": path, "text": text}


def format_pending_globals_notice(info: dict, *, rich_markup: bool = False) -> str:
    """Format user-facing notice string when pending globals exist."""
    if not info.get("has_pending"):
        return ""

    count = info.get("count", 1)
    plural = "s" if count != 1 else ""
    path_str = f".{APP_NAME}/pending-globals.md"

    if rich_markup:
        return f"[bold yellow]💡 NOTICE:[/bold yellow] [bold]{count}[/bold] pending global proposal{plural} in [bold cyan]{path_str}[/bold cyan] await review for CLAUDE.md."
    return f"💡 NOTICE: {count} pending global proposal{plural} in {path_str} await review for CLAUDE.md."
