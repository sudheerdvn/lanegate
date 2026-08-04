# /implement — Start a Ticket

Claims a ticket and creates its git worktree.

**Usage:** `/implement TICK-NNN`

```bash
lanegate start $ARGUMENTS
```

After the worktree is created (`worktrees/<ticket-id>/`), edit files **there** — never edit the main checkout's copies. Reset CWD between tickets.

Run tests from within the worktree (use relative paths to your test runner). When tests are green:

```bash
lanegate complete $ARGUMENTS
```

Then merge:

```bash
lanegate merge $ARGUMENTS
```

---

## Writing agent notes

After completing a ticket (tests green, `lanegate complete` called), write a short notes block for each file in `ticket.touches` so future agents inherit constraints discovered during this implementation.

### Note file location

For each file path in `ticket.touches`, the note file is:

```
.lanegate/notes/<flat_path>.md
```

where `<flat_path>` is the file path with every `/` replaced by `_`.

Examples:
- `src/lanegate/core.py` → `.lanegate/notes/src_lanegate_core.py.md`
- `.claude/commands/implement.md` → `.lanegate/notes/.claude_commands_implement.md.md`

### How to write notes

For small note files, **append** a tagged block to the note file (create the file if it doesn't exist):

```markdown
## [TICK-NNN]
- <factual constraint 1>
- <factual constraint 2>
```

Rules:
- Write **factual constraints only**: "X fails if Y", "always do Z before W", "field X must be non-null or parser crashes"
- **Max 5 bullets per ticket per file** — be terse
- If nothing non-obvious was learned about a file, **write nothing** (omit the block entirely for that file)
- Do not repeat what the ticket spec already says; write only what surprised you or would trip up the next agent

### Consolidating large note files

Consolidation is agent-driven at write time, as part of the normal `/implement` completion flow. It is not a scheduled job, background cleanup, or separate `lanegate` subcommand.

Before appending, check the existing note file. If adding the new note would push it over either threshold — more than 5 `## [TICK-NNN]` blocks or more than about 40 lines — consolidate first instead of appending another per-ticket block:

1. Read every existing `## [TICK-NNN]` block and the new facts you were about to append.
2. Merge duplicate or overlapping bullets that describe the same current constraint.
3. Re-verify each surviving bullet against the current code for that file; drop bullets the code no longer supports.
4. Rewrite the note file as a flat list of current constraints rather than a per-ticket log.

Each surviving bullet must keep a terse trailing ticket tag because `.lanegate/notes/` is gitignored and the tag is the only durable pointer back to the originating ticket:

```markdown
- <current factual constraint> (TICK-042)
- <merged current factual constraint> (TICK-042, TICK-071)
```

If the note file stays at or below both thresholds, behavior is unchanged: append the new `## [TICK-NNN]` block normally.

### Escalation rule

| Type of learning | Where it goes |
|---|---|
| File-specific or ephemeral (implementation detail, edge case in one file) | `.lanegate/notes/<flat_path>.md` only |
| Project-wide convention discovered during implementation | Append a proposal to `.lanegate/pending-globals.md` (also gitignored) with the ticket ID and rationale |

**Never write to `CLAUDE.md` autonomously.** Only a human may promote a proposal from `pending-globals.md` into `CLAUDE.md`.

### Example

After implementing TICK-042 that touched `src/lanegate/concurrency.py` and `.lanegate.yml`:

`.lanegate/notes/src_lanegate_concurrency.py.md`:
```markdown
## [TICK-042]
- flock path must be on the same filesystem as the repo root or acquire silently fails
- always release lock in a trap, not in normal control flow
```

`.lanegate/notes/.lanegate.yml.md` — omitted (nothing non-obvious learned about the YAML schema)
