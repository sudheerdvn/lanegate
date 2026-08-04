# /orchestrate — Clear the Board

Runs the ticket-processing loop. Pulls touch-disjoint batches from `lanegate next`,
takes each ticket as far as policy allows **in parallel up to a resource cap**,
and repeats until no eligible tickets remain.

**Usage:** `/orchestrate [--max N] [--dry-run] [--human-review final|per_ticket|none]`

This skill coordinates the run. It never edits code itself — it dispatches per-ticket
subagents and runs the CLI verbs between them.

---

## 0. Preflight — single-orchestrator guard

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
  while live_workers < max_parallel:
    batch_json = `lanegate --json next`
    candidates = [batch_json.next] + batch_json.peers  # touch-disjoint after excluding live touches
    if candidates is empty: break
    dispatch the first candidate into the free slot
  wait for the next worker to finish
  as that slot frees, re-run `lanegate next` and refill it immediately
  if no workers are live and no candidates remain: break
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

### Prior agent notes injection

Before dispatching the subagent for a ticket, collect any notes left by previous agents
for files this ticket will touch.

**Step 1 — collect notes**

For each path in `ticket.touches`, derive the flat filename:
- Replace every `/` in the path with `_`
- Read `.lanegate/notes/<flat_path>.md` if it exists; silently skip if missing

**Step 2 — build the injection**

Concatenate all non-empty note file contents. If the result is non-empty, prepend it to
the agent prompt as a dedicated section:

```
## Prior agent notes

<concatenated note file contents>
```

If no note files exist (or all are empty), **omit the section entirely** — do not add a
heading with no content.

**Step 3 — dispatch**

Pass the (possibly augmented) prompt to the subagent. The injection is executor-agnostic:
it works the same for `claude-subagent`, `claude-process`, `aider`, and any other
executor in the dispatch table below.

---

### Per-ticket executor dispatch

The executor for each ticket is resolved per-ticket:

```python
executor = resolve_driver("implement", ticket, cfg)
```

The resolved `executor` value may be a built-in executor type or a named driver key from
the `drivers:` block in `.lanegate.yml`. `resolve_driver()` selects the driver name, then
the dispatch layer expands named drivers to their underlying `type`, `model`, `bin`,
`flags`, and other supported settings.

| resolved executor / driver type | Dispatch method |
|---|---|
| `claude-subagent` (or `claude`) | Task tool (in-process subagent) |
| `claude-process` | `claude -p <prompt>` subprocess in the ticket's worktree |
| `aider`, `codex`, `<cmd>` | configured agent as a process in the ticket's worktree |

```
lanegate start  TICK-NNN     # claim + worktree (TOCTOU-safe; see below)
  → dispatch per executor (see table above) with prior notes prepended
lanegate complete TICK-NNN   # drift check vs touches + status → code_complete
  → review gate: see §3 below
lanegate merge  TICK-NNN     # ff merge → main, worktree removed
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
reviewer = resolve_driver("review", ticket, cfg)
```

Resolution order (first non-empty value wins):
1. Ticket-level `reviewer` field in frontmatter (per-ticket override)
2. `steps.review.driver` in `.lanegate.yml`
3. Project-level `reviewer` in `.lanegate.yml` (legacy shorthand; may be `human`)
4. Legacy `executor_steps.review` in `.lanegate.yml`
5. Project-level `executor` in `.lanegate.yml` (fallback to implementer's executor)

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

- Leave the ticket at `code_complete` with the findings stored.
- Continue the rest of the batch — do not halt the whole run.
- Report blocked tickets in the end-of-run summary (§6).
- Do **not** auto-retry the implementation by default — a human should inspect first.
- **Exception**: when the resolved `autonomy` is `full` (ticket-level `autonomy:`
  overrides a project-level default in `.lanegate.yml`; default is `supervised`,
  the behavior above), auto-retry via fix → drift-check → re-review, up to
  `max_auto_fix_attempts` cycles. A drift-check failure (the fix diverges from
  the ticket's own intent) or an exhausted attempt budget still escalates to a
  human regardless — full autonomy never bypasses that gate, only the initial
  human wait before it.

---

## Multi-agent configuration

Use `drivers:` to define named agent instances and `steps:` to route pipeline steps to
those instances. A named driver wraps an underlying executor `type` with settings such
as `model`, `bin`, `flags`, or `base_url`. Per-ticket `executor:` and `reviewer:`
frontmatter can also name a driver and take precedence for implementation and review,
respectively.

```yaml
# .lanegate.yml
executor: claude             # legacy shorthand fallback
reviewer: human              # legacy shorthand fallback for review
max_parallel: 2              # legacy shorthand concurrency cap

drivers:
  claude-main:
    type: claude-process
    model: claude-sonnet-4-6

  ollama-qwen:
    type: ollama
    model: qwen2.5-coder:32b
    base_url: http://localhost:11434

  aider-local:
    type: aider
    model: ollama/qwen2.5-coder:32b
    flags: [--no-auto-commits]

  codex-cloud:
    type: codex
    model: o4-mini

steps:
  analyze:
    driver: claude-main
  implement:
    driver: ollama-qwen
  review:
    driver: claude-main
```

Omitting `drivers:` and `steps:` keeps the existing behavior: LaneGate resolves the legacy
top-level `executor:` / `reviewer:` settings and dispatches the built-in executor types
directly. The legacy `executors:` block remains available for per-executor flags,
models, and concurrency caps in that mode.

---

## 4. Rate-limit / load handling (set the cap optimistically)
<!-- was §3 before the review gate was added as §3 -->

So you can set `max_parallel` to the cap rather than guessing low:

- On HTTP **429 / rate-limit** from a subagent: respect `Retry-After`, exponential
  backoff with jitter, then retry that ticket. Do **not** fail the batch.
- If 429s persist across a round, **shrink the live worker count by 1** (adaptive
  throttle) and log it; recover upward after a clean round.
- On a **local** executor, OOM/timeout is the signal instead — lower the cap, never retry
  blindly into the same wall.

---

## 5. Review policy (human gates only — agent review ALWAYS runs)

Set per-batch via `--human-review`, default `final`. Agent review at full quality runs on
**every** ticket regardless.

| Mode | Agent review | Human gate |
|---|---|---|
| `per_ticket` | full quality | stop each ticket at `in_review` for a human |
| `final` (default) | full quality | one human pass over the whole batch at the end |
| `none` | full quality | none — pre-vetted/trusted runs only |

`none` does not mean unreviewed; it means no *human* gate.

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
   import sys

   log_path = repo_root / ".lanegate" / "watch.log"
   pid = spawn_detached([sys.executable, "-m", "lanegate", "watch"], log_path)
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

## 7. Context hygiene — keep the main session slim

The orchestrator runs in the main Claude session. Every tool result that lands here
consumes context that is never reclaimed. Subagents exist precisely to keep heavy
content out of this session. Violating these rules causes the main context to fill
faster than the board clears, eventually forcing a summarisation mid-run.

### Rules

**`lanegate start` — suppress the ticket echo**

`lanegate start` prints the full ticket spec to stdout. Capture only the first 3 lines
(worktree path / branch / "Started"):

```bash
lanegate start TICK-NNN 2>&1 | head -3
```

**Reviewer subagents — never relay diffs through main context**

Do NOT fetch `git diff main...tick-NNN` in the main session and paste it into the
reviewer prompt. Instead, tell the reviewer where to look:

```
Read the diff yourself:
  git -C /home/.../worktrees/tick-NNN diff main...tick-NNN
Do not ask me to provide it — run the command directly.
```

This keeps the diff bytes in the subagent's context, not here.

**Reviewer subagents — read worktree files, not main-repo files**

Reviewer agents that need to read source files MUST be told the worktree path
explicitly. Agents that default to the main repo see pre-merge state and produce
false `changes_requested` verdicts. Always include in every reviewer prompt:

```
Work from the worktree at: /home/.../worktrees/tick-NNN
Do NOT read files from the main repo path.
```

**Notes files — bounded by design**

Notes files are written only on ticket hibernation (interrupted executor sessions).
Each hibernation overwrites the file (not appends), and diffs are truncated at 30 KB
at write time. Notes files are therefore always small and safe to inject as-is.

**`lanegate next` / `lanegate board` — use sparingly**

Call `lanegate --json next` once per loop iteration. Do not call `lanegate board` mid-loop
unless diagnosing a problem — the JSON output from `next` has everything needed.

### Symptoms of drift

If the main context is above ~40 % mid-run, check:
1. Were recent `lanegate start` outputs unsuppressed?
2. Did a reviewer prompt contain an inline diff?
3. Was a large notes file read into context?

Correct forward; do not restart the run.

---

## 6. Stop conditions

- `lanegate next` returns no candidates → **no eligible tickets remain**, exit cleanly.
- Any ticket pauses (`hibernated`, `needs_review`, `failed`, `changes_requested`) →
  **continue draining touch/dependency-independent tickets**; paused tickets are filtered
  from future `lanegate next` queries for the rest of this run so they are not re-attempted.
  A ticket that pauses does not halt unrelated work.
- Any ticket with `--human-review per_ticket` approved → stops at `in_review` for a human.
- Interrupt / error → release the orchestrator lock (§0 trap) and report what merged,
  what is in flight, and what remains.

End every run with a summary: merged, blocked/in-review, skipped (lost races), remaining.
If any tickets paused, the summary lists each paused ticket with its current status and
a per-status remediation command (`lanegate reopen <id> && lanegate orchestrate` or similar).

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
