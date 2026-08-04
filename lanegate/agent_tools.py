"""Agent-native tool installation helpers.

This module keeps CLI output as presentation only. Tests and future callers can
use the returned dictionaries to discover exactly which artifacts were written.
"""

from __future__ import annotations

import importlib.resources as pkg_resources
import json
import shutil
from pathlib import Path
from typing import Any


BOUNDED_MCP_TOOLS = [
    "board",
    "next_ticket",
    "pipeline_status",
    "repo_status",
    "recent_logs",
    "continuation_context",
    "start",
    "orchestrate",
    "complete",
    "review",
    "merge",
    "validate",
    "done",
]


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def install_claude_commands(project_root: Path) -> dict[str, Any]:
    """Copy bundled Claude slash commands into ``.claude/commands/lanegate``."""
    dest_dir = project_root / ".claude" / "commands" / "lanegate"
    dest_dir.mkdir(parents=True, exist_ok=True)

    skills_ref = pkg_resources.files("lanegate").joinpath("skills")
    copied: list[str] = []
    for resource in sorted(skills_ref.iterdir(), key=lambda r: r.name):
        if not resource.name.endswith(".md"):
            continue
        dst = dest_dir / resource.name
        with pkg_resources.as_file(resource) as src:
            shutil.copy(src, dst)
        copied.append(_rel(dst, project_root))

    return {
        "agent": "claude",
        "kind": "slash_commands",
        "directory": _rel(dest_dir, project_root),
        "files": copied,
        "commands": [f"/lanegate:{Path(name).stem}" for name in copied],
    }


def mcp_config_snippet() -> dict[str, Any]:
    """Return a generic MCP client config snippet for the LaneGate stdio server."""
    return {"mcpServers": {"lanegate": {"command": "lanegate", "args": ["mcp"]}}}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def install_mcp_configs(project_root: Path) -> list[dict[str, Any]]:
    """Write Codex and generic MCP config snippets for agent-native LaneGate tools."""
    snippet = mcp_config_snippet()
    artifacts = [
        {
            "agent": "codex",
            "kind": "mcp_config",
            "path": project_root / ".codex" / "mcp" / "lanegate.json",
            "config": snippet,
        },
        {
            "agent": "generic-mcp",
            "kind": "mcp_config",
            "path": project_root / ".lanegate" / "agent-tools" / "mcp-lanegate.json",
            "config": snippet,
        },
    ]
    for artifact in artifacts:
        _write_json(artifact["path"], artifact["config"])
        artifact["path"] = _rel(artifact["path"], project_root)
    return artifacts


def describe_agent_tools() -> dict[str, Any]:
    """Return the bounded MCP surface that agent installers expose."""
    return {
        "mcp_server": {"command": "lanegate", "args": ["mcp"]},
        "bounded_tools": list(BOUNDED_MCP_TOOLS),
        "guarantees": [
            "lifecycle action output is byte-capped",
            "log excerpts are line- and byte-capped",
            "continuation uses tickets, worktrees, logs, and locks as durable state",
            "no unbounded prompt, log, or diff reads are exposed",
        ],
    }


def install_agent_tools(project_root: Path) -> dict[str, Any]:
    """Install all currently supported agent-native LaneGate artifacts."""
    claude = install_claude_commands(project_root)
    mcp_artifacts = install_mcp_configs(project_root)
    return {
        "ok": True,
        "project_root": str(project_root),
        "artifacts": [claude, *mcp_artifacts],
        "tools": describe_agent_tools(),
    }
