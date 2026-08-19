# /implement — Start a Ticket

Claims a ticket and creates its git worktree.

**Usage:** `/implement TICK-NNN`

```bash
lanegate start $ARGUMENTS
```

After the worktree is created (`.lanegate/worktrees/<ticket-id>/`), edit files **there** — never edit the main checkout's copies. Reset CWD between tickets.

## Shared notes

`.lanegate/notes/` inside every ticket worktree is a symlink to the control
checkout's durable notes store. Use `.lanegate/notes/global.md` for factual
project-wide guidance and `.lanegate/notes/v2/<encoded_path>.md` for facts
specific to a file. **In this order**, replace `_` with `_u`, then `/` with
`_s` (for example, `src/app.py` maps to `v2/src_sapp.py.md` and
`src/foo_bar.py` maps to `v2/src_sfoo_ubar.py.md`; create `v2/` if needed).
Before updating a v2 note, verify the legacy flat name is unambiguous: inspect
its provenance and confirm no other tracked repository path maps to it. Only
then fold that legacy flat note into v2 and remove it. If it is ambiguous,
preserve it unchanged, do not create a competing correction, and report the
migration conflict rather than reassigning or deleting another path's facts.
These notes survive worktree removal and
are available to later tickets. Do not replace the link or create a private
notes directory.

Capture only non-obvious durable facts: constraints, edge cases, invariants,
and lessons that prevent repeat mistakes. Append dated, provenance-labelled
blocks instead of overwriting useful facts. Consolidate each note to at most
five factual blocks and roughly forty lines. Do not write summaries that are
discoverable directly from code, diffs, or tests.

Run tests from within the worktree (use relative paths to your test runner). When tests are green:

```bash
lanegate complete $ARGUMENTS
```

Review before merging. With a configured reviewer, `lanegate review` runs that
agent and records its verdict; it is not an instant state flip. With
`reviewer: human`, `none`, or `auto-none`, inspect the diff yourself and
record the decision explicitly. Merge only an approved ticket; never use an
explicit approval to bypass a configured reviewer.

```bash
lanegate review $ARGUMENTS
# Human/none/auto-none only, after inspecting the diff:
lanegate review $ARGUMENTS --verdict approved
lanegate merge $ARGUMENTS
```

For `changes_requested`, do not merge: address the findings and re-review.

---

## Project-wide conventions

If you discover a project-wide convention during implementation (not specific to one file), append a proposal to `.lanegate/pending-globals.md` (gitignored) with the ticket ID and rationale:

```markdown
## [TICK-NNN]
- <proposed convention and why>
```

**Never write to `CLAUDE.md` autonomously.** Only a human may promote a proposal from `pending-globals.md` into `CLAUDE.md`.
