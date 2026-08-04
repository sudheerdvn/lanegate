# Agent Tools

LaneGate can be used by agents as native tools instead of as long-running shell
commands whose terminal output must be scraped.

## Install

```bash
lanegate install-agent-tools
```

This writes:

- Claude slash commands under `.claude/commands/lanegate/`
- a Codex MCP snippet at `.codex/mcp/lanegate.json`
- a generic MCP snippet at `.lanegate/agent-tools/mcp-lanegate.json`

`lanegate install-commands` remains as a Claude-compatible alias that installs only
the slash commands.

## Claude

Claude receives the bundled `/lanegate:*` slash commands. They remain useful for
interactive command dispatch and are copied from `lanegate/skills/*.md`.

## Codex And Other MCP Clients

Codex and generic MCP clients receive this server configuration:

```json
{
  "mcpServers": {
    "lanegate": {
      "command": "lanegate",
      "args": ["mcp"]
    }
  }
}
```

The MCP server exposes structured tools for board state, next-ticket selection,
pipeline state, lifecycle transitions, dry-run orchestration, short logs, repo
status, and continuation context.

## Bounded Output

Agent-facing tools must not expose unbounded prompt, log, or diff reads.

- lifecycle action output is capped by bytes
- `recent_logs` caps both line count and byte count
- `repo_status` returns ticket/worktree/lock metadata, not ticket bodies or diffs
- `continuation_context` combines bounded logs with metadata from tickets,
  worktrees, and the orchestrator lock

Agents that need full details should request explicit files through their normal
workspace access instead of relying on LaneGate to dump large context blobs.

## Continuation Model

Chat context is not the source of truth. A resumed agent should ask LaneGate for:

- current board or ticket status
- existing worktrees
- orchestrator lock state
- latest bounded log excerpt

Those values come from durable repo state: ticket frontmatter, `.lanegate/worktrees`
or the configured worktree directory, `.lanegate/logs`, and
`.lanegate/orchestrator.lock`.

## CLI-Only Versus Agent-Native

The CLI is still the complete human/operator surface. Agent-native installs add
structured control points so agents can call LaneGate without parsing human tables
or flooding their context with executor output.
