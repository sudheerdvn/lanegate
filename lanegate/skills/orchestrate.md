# /orchestrate — Compatibility Alias for /run

> Prefer `/lanegate:run` or `lanegate run`. This legacy slash command remains
> available for existing agent setups and follows the same run procedure.

Runs the ticket-processing loop. Pulls touch-disjoint batches from `lanegate next`,
takes each ticket as far as policy allows **in parallel up to a resource cap**,
and repeats until no eligible tickets remain.

**Usage:** `/orchestrate [--max N] [--dry-run] [--human-review none|per_ticket|final] [--milestone <m> | --all]` (default: `none`)

This skill coordinates the run. It never edits code itself — it dispatches per-ticket
subagents and runs the CLI verbs between them.

---

## 0a. Preflight — milestone resolution

Before doing anything else, read `.lanegate.yml` and resolve the active milestone:

1. `--milestone <m>` flag on this invocation → use that milestone.
2. `default_milestone` key in `.lanegate.yml` → use that milestone silently.
3. `--all` flag → process tickets across all milestones (no filter).
4. None of the above, and no ticket in the tickets dir has a `milestone` field set →
   nothing to scope by, so process tickets across all milestones (same as `--all`).
5. None of the above, but at least one ticket has a `milestone` field set → **exit with
   a clear error** (the scope is ambiguous once milestones are in use):
   ```
   ERROR: no milestone specified and no default_milestone in .lanegate.yml.
   Run with --milestone <m> or --all to process tickets across all milestones.
   ```

Log the active milestone at startup:
```
[orchestrate] milestone filter: v1
```

Forward the resolved milestone to **every** `lanegate next` call:
```bash
lanegate --json next --milestone v1
```

When `--all` is active, omit the `--milestone` flag from `lanegate next` calls entirely.

---

## 0b. Preflight — single-orchestrator guard

Only one orchestrator may run per repo. A second one would double the live agent count
and blow past the rate-limit / GPU cap (each orchestrator enforces `max_parallel`
independently, with no cross-process coordination).

The check-and-set, stale-lock reclaim, and atomicity (`flock`) live in `concurrency.py`;
the skill calls the verb with its own shell PID (`$$`):

```bash
lanegate orchestrator-lock acquire --pid $$ || exit 1   # refuses if a live orchestrator holds it
trap 'lanegate orchestrator-lock release --pid $$' EXIT  # release on exit/error/interrupt
```

> A stale lock (holder process dead) is reclaimed automatically. To inspect without
> claiming, run `lanegate orchestrator-lock status`; to override a wedged lock, add `--force`.

---

## 1. Resolve the concurrency cap

The cap is the **resource gate**; `lanegate next` is the **correctness gate**. The
effective live worker count is never greater than
`min(eligible_disjoint_tickets, max_parallel)`.

Resolve `max_parallel` in this order (first hit wins):

1. `--max N` flag on this invocation.
2. `executors.<executor>.max_parallel` in `.lanegate.yml` for the active `executor`.
3. Top-level `max_parallel` in `.lanegate.yml`.
4. Built-in default: **2**.

```yaml
# .lanegate.yml
executor: claude
max_parallel: 2            # global default
executors:
  claude: { max_parallel: 3 }   # cloud → bounded by API rate limits
  local:  { max_parallel: 1 }   # single-GPU → effectively sequential
```

Reminder on semantics: the executor value is an **override**, not additive.
`local: 1` runs one ticket at a time on purpose; raise it only when your local server
batches (e.g. vLLM continuous batching).

---

## 2. The loop

```
repeat:
  # Auto-analyze draft tickets before consulting the queue
  for each draft ticket matching the milestone filter:
    lanegate analyze <id>    # flips draft → open; errors are logged and skipped
  # If milestone resolved (not --all), pass --milestone flag
  while live_workers < max_parallel:
    batch_json = `lanegate --json next [--milestone <m>]`
    candidates = [batch_json.next] + batch_json.peers  # touch-disjoint after excluding live touches
    if candidates is empty: break
    dispatch the first candidate into the free slot
  wait for the next worker to finish
  as that slot frees, re-run `lanegate next` and refill it immediately
  if no workers are live and no candidates remain: break
```

### 2a. Draft auto-analyze step

At the top of **every** loop iteration — before calling `lanegate next` — the orchestrator
runs `lanegate analyze <id>` for each draft ticket that matches the active milestone filter.
This promotes drafts to `open` automatically so they can enter the queue without a
separate manual step.

Rules:

- Only drafts with `status: draft` are eligible; all other statuses are ignored.
- When a milestone filter is active, only drafts whose `milestone` field matches are
  analyzed; unmatched milestones are silently skipped.
- If `lanegate analyze` raises an exception for a ticket, a warning is printed and the loop
  continues — the failed draft is left in `draft` status and the orchestrate run is not
  aborted.
- Pass `--no-auto-analyze` to disable this step entirely:
  ```bash
  lanegate run --no-auto-analyze
  ```
- In `--dry-run` mode the orchestrator prints which drafts *would* be analyzed without
  actually calling `lanegate analyze`:
  ```
  [dry-run] would analyze draft TICK-042
  ```

Use a **worker-pool / semaphore**, not blind fan-out: keep at most `max_parallel`
subagents live at once and pull the next candidate as each finishes. Do **not** spawn the
whole board and wait. `lanegate next` only ever returns a disjoint set, so re-querying
each round naturally respects the `touches` lock as tickets enter/leave flight.

The Python orchestrator owns the live-slot accounting. Claude Code's Task tool returns
fan-out results as a batch, so the scheduler must not depend on Task-tool streaming to
notice individual completions. It refills slots from Python when an executor invocation
finishes, passing the touches of still-running tickets as transient exclusions to the
next queue query until their status locks are visible on disk. When `max_parallel=1`,
this reduces to the previous batch-synchronous/sequential behavior.

### Shared notes injection

Do not collect or prepend `.lanegate/notes` in this coordinator. The analyze and
implementation prompt builders inject the canonical global and touch-relevant notes once,
through `get_bounded_shared_notes`; this keeps the content within its payload budget.
`_collect_prior_notes` is reserved for hibernation recovery context.

---

### Per-ticket executor dispatch

The executor for each ticket is resolved per-ticket:

```python
executor = ticket.get('executor') or cfg['executor']
```

| `executor` value | Dispatch method |
|---|---|
| `claude-subagent` (or `claude`) | Task tool (in-process subagent) |
| `claude-process` | `claude -p <prompt>` subprocess in the ticket's worktree |
| `aider`, `codex`, `<cmd>` | configured agent as a process in the ticket's worktree |

```
lanegate start  TICK-NNN     # claim + worktree (TOCTOU-safe; see below)
  → dispatch per executor (see table above) with prior notes prepended
lanegate complete TICK-NNN   # drift check vs touches + status → code_complete
  → review gate: see §3 below
lanegate merge  TICK-NNN     # --no-ff merge → main, worktree removed
  → analytics log: see §2a below (MANDATORY — do not skip)
```

### §2a. Analytics logging (mandatory after every merge)

Immediately after `lanegate merge TICK-NNN` succeeds, call `lanegate log` with the token
counts from the task-notification `<usage>` blocks. Do not skip this step — missing
entries cannot be backfilled accurately later.

```bash
lanegate log TICK-NNN \
  --tokens <total_subagent_tokens>    # sum of implementer + all reviewer usage blocks
  --summary-tokens <summary_tok>      # rough: len(agent_result_text) // 4
  --executor claude-subagent \
  --model claude-sonnet-4-6 \
  --tests-passed                      # include if tests passed in the subagent
```

**Where to get the numbers:**
- `total_subagent_tokens`: sum the `subagent_tokens` field from every task-notification
  `<usage>` block for this ticket (implementer + reviewer(s))
- `summary_tokens`: rough estimate — `len(result_text) // 4` where `result_text` is the
  agent's returned summary string

Log immediately; if the merge was done manually (without `lanegate merge`), still call
`lanegate log` right after the git commit.

If `lanegate start` fails with *"grabbed by another session"* or *"conflicts with …"*,
**skip that ticket this round** and move on — it is not an error, just a lost race or a
lock. The next `lanegate next` will reflect reality.

For `claude-subagent`/`claude`: Give the subagent only the ticket spec and its `touches` —
not the board state.

For `claude-process`: Run `claude -p "<prompt>"` as a subprocess in the ticket's worktree
(`worktrees/tick-NNN`). The prompt is the same context as what would be given to a Task
subagent. Capture stdout/stderr; non-zero exit means implementation failed.

---

## 3. Review gate (always runs — agent review at full quality)

After `lanegate complete TICK-NNN`, dispatch a **review subagent** using the resolved
reviewer executor — which may differ from the implementer's executor. Resolve it as:

```python
reviewer = ticket.get('reviewer') or cfg.get('reviewer') or cfg['executor']
```

Resolution order (first non-empty value wins):
1. Ticket-level `reviewer` field in frontmatter (per-ticket override)
2. Project-level `reviewer` in `.lanegate.yml` (project-wide reviewer default)
3. Project-level `executor` in `.lanegate.yml` (fallback to implementer's executor)

Dispatch the review subagent using the resolved `reviewer` value, **not** the
implementer's executor. This allows, for example, a `claude-process` project to use
`claude-subagent` for reviews, or a specific ticket to route its review to `human`.

### What to give the reviewer

```
Ticket spec:
  id: TICK-NNN
  title: <title>
  close_criteria: <close_criteria>
  touches: [<files>]

Diff (branch vs main):
<output of: git diff main...tick-nnn>
```

### What the reviewer returns

A JSON object on stdout:
```json
{
  "verdict": "approved" | "changes_requested",
  "summary": "<one-line verdict summary>",
  "findings": ["<finding 1>", "<finding 2>"]
}
```

### How the orchestrator calls the CLI

```bash
# Parse the JSON verdict and call:
lanegate review TICK-NNN \
  --verdict <verdict> \
  --summary "<summary>" \
  --findings "<newline-joined findings>"
```

`lanegate review --verdict approved` → flips ticket to `in_review`, stores
`review_verdict` and `review_summary` in frontmatter, appends findings under
`## Review Findings` in the ticket body.

`lanegate review --verdict changes_requested` → stores fields, exits non-zero,
**leaves ticket at `code_complete`**. The orchestrator treats this ticket as
blocked for that batch round.

### Policy on changes_requested

- Continue the rest of the batch — do not halt the whole run.
- Report blocked/awaiting-review tickets in the end-of-run summary (§6).
- Fix → drift-check → re-review, up to `max_auto_fix_attempts` cycles,
  **always runs** when a review comes back `changes_requested` and findings
  exist — this no longer depends on the resolved `autonomy` (ticket-level
  `autonomy:` overrides a project-level default in `.lanegate.yml`; default is
  `supervised`). Do-not-auto-retry was never about withholding the fix — it
  was about not auto-*approving* a retry without a human in the loop. A
  human-gated retry is now the default path, not a missing one.
- `autonomy` controls what happens to the result, not whether the cycle runs:
  - `autonomy: full` — re-review approval proceeds straight to merge,
    unattended, exactly as before.
  - `autonomy: supervised` (default) and `autonomy: manual` — the cycle runs
    to completion and the ticket lands at `in_review` awaiting an explicit
    human verdict instead of auto-merging.
- A drift-check failure (the fix diverges from the ticket's own intent) or an
  exhausted attempt budget still escalates to a human in every mode — this
  gate is never bypassed, and a drifted fix does not retry past that one
  failure.
- `lanegate fix TICK-NNN` runs this same cycle out-of-band for a ticket already
  sitting at `code_complete`/`changes_requested` — e.g. after a human ran
  `lanegate review` directly, outside of `orchestrate`.

---

## 4. Rate-limit / load handling

On HTTP **429 / rate-limit** from a subagent:

- Halt dispatcher immediately and hibernate the run (stop accepting new tickets).
- If `on_rate_limit: resume` is set in `.lanegate.yml` (the default), spawn a background `resume_watch` daemon that polls for recovery and resumes automatically.
- If `on_rate_limit` is `halt`, stop the orchestrate run and leave the board in `in_progress` state for manual intervention.

This is controlled by the `on_rate_limit` config setting: `resume` (default, spawns a background daemon to retry automatically) or `halt` (stops the run).

---

## 5. Review policy

Set per-batch via `--human-review`, default `none`. Whether an independent agent review
actually runs depends on both this flag and the driver mode (combined vs split — see
`docs/config-reference.md` for the full table):

| Mode | Independent agent review (split mode) | Auto-merge | Human gate |
|---|---|---|---|
| `none` (default) | none — auto-approved with no review of any kind | yes, immediately | none |
| `per_ticket` | yes — full review agent (`run_review_agent`) | no — `auto_merge_approved_local_tickets` refuses unless `human_review == none` | stop each ticket at `in_review` for a human |
| `final` | none — flips to `in_review`, no agent review runs | no | one human pass over the whole batch at the end |

In **combined mode** (same executor for implement and review), the executor self-reviews as
part of its own prompt regardless of `--human-review` — there is no independent second
opinion. `reviewer: human` in `.lanegate.yml` overrides all of the above and always pauses for
a human verdict.

Only `per_ticket` invokes a real, independent review agent in split mode. `none` and `final`
do not run agent review — `none` auto-approves silently, `final` just defers to the
end-of-batch human pass.

---

## 5a. Watch daemon spawn on exit (TICK-019 / TICK-023)

After the run loop exits — whether the board is empty or only blocked/in-review
tickets remain — check for tickets still at `in_review` status.

**If any `in_review` tickets exist:**

1. Check whether a watcher is already running:
   ```bash
   lanegate watch --status
   ```
2. If not running, spawn a detached background watcher using the `spawn_detached`
   helper from `lanegate.lifecycle`. This is platform-agnostic Python — no `nohup`,
   no `&`, no shell syntax:
   ```python
   from lanegate.lifecycle import spawn_detached
   from pathlib import Path

   log_path = repo_root / ".lanegate" / "watch.log"
   pid = spawn_detached(["lanegate", "watch"], log_path)
   print(f"[orchestrate] watch daemon started (pid {pid}) — will merge approved PRs in background")
   ```
   The orchestrator's `spawn_watch_daemon(repo_root)` function wraps this exactly.
3. If already running, skip spawn and note it in the summary.

**End-of-run summary line (when daemon spawned or already running):**
```
[waiting] N ticket(s) in review — watch daemon running (pid NNN)
```

**If no `in_review` tickets:** do not spawn the watcher.

The watcher polls every 60 seconds, calls `lanegate merge` on APPROVED PRs, and exits
when no `in_review` tickets with a `pr_number` remain.

---

## 6. Stop conditions

- `lanegate next` returns no candidates → **no eligible tickets remain**, exit cleanly.
- Any ticket fails review (`changes_requested`) → leave at `code_complete` (§3), continue
  rest, report blocked tickets at the end.
- Any ticket with `--human-review per_ticket` approved → stops at `in_review` for a human.
- Interrupt / error → release the orchestrator lock (§0 trap) and report what merged,
  what is in flight, and what remains.

End every run with a summary: merged, blocked/in-review, skipped (lost races), remaining.

---

## 7. Context management

After each ticket is fully merged and before starting the next batch, check context
load. If context is heavy (above ~50% of the window), run `/compact` with a brief
summary of what has been completed and what remains on the board, then resume the
run loop.

The compact summary must preserve:
- Which tickets were merged this session
- Which tickets are still open or blocked
- The current milestone filter and concurrency cap

Do not compact mid-ticket or mid-batch — only at clean batch boundaries where no
in-flight state would be lost.

---

## Notes / open design points

- **`lanegate next` returns one disjoint batch, not a `--limit`.** The skill applies the
  `max_parallel` slice itself. A future `lanegate next --limit N` would let the CLI enforce
  the cap (testable without an LLM in the loop).
- **The single-orchestrator lock is advisory** (PID file, §0) but now hardened in
  `concurrency.py` (TICK-009 / DECISIONS F14): atomic check-and-set under an `flock`, with
  stale-lock reclaim, exposed via `lanegate orchestrator-lock`. The per-ticket claim also
  takes an `flock` around its read→write→commit window (`claim_lock`), closing the residual
  TOCTOU gap — git's `index.lock` only serialized the status commit, not the whole claim.
