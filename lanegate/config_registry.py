"""Global project registry stored in ~/.lanegate/projects.json."""

from __future__ import annotations

import json
from pathlib import Path

from lanegate import APP_NAME


_REGISTRY_DIR = Path.home() / f".{APP_NAME}"
_REGISTRY_FILE = _REGISTRY_DIR / "projects.json"


def _registry_load() -> list[dict]:
    """Return the list of registered projects, or [] if registry doesn't exist."""
    if not _REGISTRY_FILE.exists():
        return []
    try:
        data = json.loads(_REGISTRY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _registry_save(projects: list[dict]) -> None:
    _REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    _REGISTRY_FILE.write_text(json.dumps(projects, indent=2) + "\n", encoding="utf-8")


def registry_add(repo_root: Path) -> None:
    """Register repo_root in ~/.lanegate/projects.json (idempotent)."""
    resolved = str(repo_root.resolve())
    projects = _registry_load()
    for entry in projects:
        if entry.get("path") == resolved:
            return  # already registered
    projects.append({"path": resolved, "name": repo_root.resolve().name})
    _registry_save(projects)


def registry_remove(repo_root: Path) -> None:
    """Deregister repo_root from ~/.lanegate/projects.json (no-op if not present)."""
    resolved = str(repo_root.resolve())
    projects = _registry_load()
    updated = [e for e in projects if e.get("path") != resolved]
    _registry_save(updated)


def registry_load() -> list[dict]:
    """Return the list of registered projects as a list of {path, name} dicts."""
    return _registry_load()


def registry_path() -> Path:
    """Return the path to the global project registry file."""
    return _REGISTRY_FILE


