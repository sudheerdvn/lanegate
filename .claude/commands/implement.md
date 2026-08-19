# /implement — Start a Ticket

Claims a ticket and creates its git worktree.

**Usage:** `/implement TICK-NNN`

```bash
lanegate start $ARGUMENTS
```

After the worktree is created (`worktrees/<ticket-id>/`), edit files **there** — never edit the main checkout's copies. Reset CWD between tickets.

Run tests from within the worktree (use relative paths to your test runner). When tests are green, commit your changes in the worktree — `lanegate complete` refuses to advance a ticket with no commits ahead of main:

```bash
git -C worktrees/<ticket-id> add -A
git -C worktrees/<ticket-id> commit -m "<summary of the change>"
lanegate complete $ARGUMENTS
```

Then submit for review — this only advances an independently-reviewed ticket straight to `in_review`; a ticket where the same executor implemented and would review its own work is correctly hard-blocked into `needs_review` instead, requiring an explicit human call instead of a self-approval:

```bash
lanegate review $ARGUMENTS --verdict approved
# if that errors with "is 'needs_review'. Use human-review...", the review
# couldn't be verified independent (e.g. same executor implemented and would
# review) -- record your own approval instead, then --verdict approved works:
lanegate human-review $ARGUMENTS --rationale "..."
lanegate review $ARGUMENTS --verdict approved
```

Then merge:

```bash
lanegate merge $ARGUMENTS
```

---

## Project-wide conventions

If you discover a project-wide convention during implementation (not specific to one file), append a proposal to `.lanegate/pending-globals.md` (gitignored) with the ticket ID and rationale:

```markdown
## [TICK-NNN]
- <proposed convention and why>
```

**Never write to `CLAUDE.md` autonomously.** Only a human may promote a proposal from `pending-globals.md` into `CLAUDE.md`.
