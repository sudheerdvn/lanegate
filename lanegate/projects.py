"""
projects.py — global project registry commands.

Manages ~/.lanegate/projects.json: scan directories, list, and remove registered
projects.
"""

from __future__ import annotations

import sys
from pathlib import Path

from lanegate.config import CONFIG_FILENAME, registry_add, registry_load, registry_remove


def cmd_projects_scan(cfg: dict, repo_root: Path, dirs: list[str]) -> None:
    """
    Walk one level deep in each given directory; register any folder that
    contains .lanegate.yml.  Reports added vs already-registered vs skipped.
    """
    existing_paths = {e.get("path") for e in registry_load()}
    added: list[Path] = []
    already: list[Path] = []
    skipped: list[Path] = []

    for dir_str in dirs:
        base = Path(dir_str).resolve()
        if not base.is_dir():
            print(f"WARNING: {dir_str!r} is not a directory — skipping", file=sys.stderr)
            continue

        for candidate in sorted(base.iterdir()):
            if not candidate.is_dir():
                continue
            if (candidate / CONFIG_FILENAME).exists():
                resolved = str(candidate.resolve())
                if resolved in existing_paths:
                    already.append(candidate)
                else:
                    registry_add(candidate)
                    existing_paths.add(resolved)
                    added.append(candidate)
            else:
                skipped.append(candidate)

    for p in added:
        print(f"  added:    {p}")
    for p in already:
        print(f"  already:  {p}")
    for p in skipped:
        print(f"  skipped:  {p}  (no {CONFIG_FILENAME})")

    print(
        f"\nScan complete: {len(added)} added, {len(already)} already registered, {len(skipped)} skipped."
    )


def cmd_projects_list(cfg: dict, repo_root: Path) -> None:
    """Show all registered projects."""
    entries = registry_load()
    if not entries:
        print("No projects registered.")
        return
    print(f"{'NAME':<20}  PATH")
    print("-" * 60)
    for e in entries:
        name = e.get("name", "")
        path = e.get("path", "")
        print(f"{name:<20}  {path}")


def cmd_projects_remove(cfg: dict, repo_root: Path, path: str) -> None:
    """Deregister a project by path."""
    target = Path(path).resolve()
    before = {e.get("path") for e in registry_load()}
    registry_remove(target)
    after = {e.get("path") for e in registry_load()}
    if str(target) in before and str(target) not in after:
        print(f"Removed: {target}")
    else:
        print(f"Not registered: {target}")
