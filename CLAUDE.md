Local-first, git-native workflow for coding agents: tickets, worktrees, review gates, staged deploys. Read `README.md` for the CLI/workflow overview and `docs/ARCHITECTURE.md` for module structure before assuming either from context.

## Response Style — be terse

- No preamble before tool calls ("I'll now read…", "Let me check…") — just call the tool.
- No trailing summary after completing a task — the diff speaks for itself.
- Between tool calls: one sentence max, only if direction changed or something unexpected was found.
- Code changes: show the edit, skip prose explanation of what it does.
- Status updates only when something was found, failed, or the plan changed.
