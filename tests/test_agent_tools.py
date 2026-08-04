"""
tests/test_agent_tools.py — coverage for lanegate/agent_tools.py (previously untested).

Covers:
- install_claude_commands: copies bundled skill .md files, builds slash-command names
- mcp_config_snippet / install_mcp_configs: writes Codex + generic MCP config JSON
- describe_agent_tools: bounded MCP surface
- install_agent_tools: end-to-end aggregation of the above
"""

from __future__ import annotations

import json
from pathlib import Path

from lanegate.agent_tools import (
    BOUNDED_MCP_TOOLS,
    describe_agent_tools,
    install_agent_tools,
    install_claude_commands,
    install_mcp_configs,
    mcp_config_snippet,
)


class TestInstallClaudeCommands:
    def test_copies_all_bundled_skill_files(self, tmp_path):
        result = install_claude_commands(tmp_path)

        dest_dir = tmp_path / ".claude" / "commands" / "lanegate"
        assert dest_dir.is_dir()
        copied_names = sorted(p.name for p in dest_dir.glob("*.md"))
        result_names = sorted(Path(f).name for f in result["files"])
        assert copied_names == result_names
        assert len(copied_names) > 0
        for name in copied_names:
            assert (dest_dir / name).read_text(encoding="utf-8") != ""

    def test_result_shape_and_relative_paths(self, tmp_path):
        result = install_claude_commands(tmp_path)

        assert result["agent"] == "claude"
        assert result["kind"] == "slash_commands"
        assert result["directory"] == str(
            (tmp_path / ".claude" / "commands" / "lanegate").relative_to(tmp_path)
        )
        for f in result["files"]:
            assert not f.startswith("/")
            assert (tmp_path / f).is_file()

    def test_commands_derived_from_filenames(self, tmp_path):
        result = install_claude_commands(tmp_path)

        stems = {Path(f).stem for f in result["files"]}
        expected = {f"/lanegate:{stem}" for stem in stems}
        assert set(result["commands"]) == expected

    def test_idempotent_on_repeat_install(self, tmp_path):
        first = install_claude_commands(tmp_path)
        second = install_claude_commands(tmp_path)
        assert first == second


class TestMcpConfigSnippet:
    def test_snippet_shape(self):
        snippet = mcp_config_snippet()
        assert snippet == {"mcpServers": {"lanegate": {"command": "lanegate", "args": ["mcp"]}}}


class TestInstallMcpConfigs:
    def test_writes_codex_and_generic_configs(self, tmp_path):
        artifacts = install_mcp_configs(tmp_path)

        assert {a["agent"] for a in artifacts} == {"codex", "generic-mcp"}
        codex = next(a for a in artifacts if a["agent"] == "codex")
        generic = next(a for a in artifacts if a["agent"] == "generic-mcp")

        codex_path = tmp_path / codex["path"]
        generic_path = tmp_path / generic["path"]
        assert codex_path == tmp_path / ".codex" / "mcp" / "lanegate.json"
        assert generic_path == tmp_path / ".lanegate" / "agent-tools" / "mcp-lanegate.json"

        assert json.loads(codex_path.read_text(encoding="utf-8")) == mcp_config_snippet()
        assert json.loads(generic_path.read_text(encoding="utf-8")) == mcp_config_snippet()

    def test_paths_in_result_are_relative(self, tmp_path):
        artifacts = install_mcp_configs(tmp_path)
        for a in artifacts:
            assert not a["path"].startswith("/")

    def test_creates_parent_directories(self, tmp_path):
        assert not (tmp_path / ".codex").exists()
        install_mcp_configs(tmp_path)
        assert (tmp_path / ".codex" / "mcp" / "lanegate.json").is_file()


class TestDescribeAgentTools:
    def test_reports_bounded_tool_list(self):
        info = describe_agent_tools()
        assert info["bounded_tools"] == BOUNDED_MCP_TOOLS
        # must be a copy, not the live module list
        assert info["bounded_tools"] is not BOUNDED_MCP_TOOLS

    def test_reports_mcp_server_command(self):
        info = describe_agent_tools()
        assert info["mcp_server"] == {"command": "lanegate", "args": ["mcp"]}

    def test_guarantees_present(self):
        info = describe_agent_tools()
        assert any("byte-capped" in g for g in info["guarantees"])


class TestInstallAgentTools:
    def test_aggregates_claude_and_mcp_artifacts(self, tmp_path):
        result = install_agent_tools(tmp_path)

        assert result["ok"] is True
        assert result["project_root"] == str(tmp_path)
        assert len(result["artifacts"]) == 3  # claude commands + codex mcp + generic mcp
        assert result["artifacts"][0]["agent"] == "claude"
        assert {a["agent"] for a in result["artifacts"][1:]} == {"codex", "generic-mcp"}
        assert result["tools"] == describe_agent_tools()

    def test_writes_files_to_disk(self, tmp_path):
        install_agent_tools(tmp_path)
        assert (tmp_path / ".claude" / "commands" / "lanegate").is_dir()
        assert (tmp_path / ".codex" / "mcp" / "lanegate.json").is_file()
        assert (tmp_path / ".lanegate" / "agent-tools" / "mcp-lanegate.json").is_file()
