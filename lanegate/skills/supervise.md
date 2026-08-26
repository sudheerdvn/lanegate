# /supervise — Background-Supervised Run

External supervisor for `lanegate run`: launches it detached, checks in on a cheap
schedule, and takes corrective action the CLI's own loop can't (it only marks things
`blocked`/`needs_review`/`hibernated` — it never resolves those states on its own).

**Usage:** `/supervise` (no flags — reads the project's own `.lanegate.yml` for
executor/reviewer/milestone config, same as `/run`)

This skill is the low-token alternative to `/run`: `/run` makes Claude the orchestrator
line-by-line, dispatching subagents itself, which burns conversation tokens on every
ticket's full dispatch. `/supervise` instead launches `lanegate run` as an independent
background process — the actual work happens outside the conversation — and only wakes
up periodically to read small, purpose-built status output and intervene when needed.
Project-agnostic: read the current project's own `.lanegate.yml` rather than assuming
any config shape below.

---

## 0. Read this project's own executor/reviewer setup first

Every project's `.lanegate.yml` can differ. Before doing anything, read it and note:
- Top-level `executor:`/`reviewer:` (bare types) and any `executors:` named instances
  (their `type`, `model`, `bin`, `flags`).
- Whether `pools:`/`default_pool:`/`routing:` are configured. **Caveat that applies to
  every project**: pool routing (`resolve_ticket_pool`) is resolved per-*ticket*, not
  per-*step* — the same pool applies to both implement and review. lanegate's own
  `resolve_independent_review_driver` already excludes the implementer instance from
  review selection within a pool (no self-review risk from pooling alone), but if the
  project wants "implement always local, review always cloud" as a hard separation,
  that still needs bare `executor:`/`reviewer:` fields (rotated externally — see §3)
  rather than relying on `default_pool`/`routing:` alone. Don't turn on
  `default_pool`/`routing:` for a project that doesn't already have it without reading
  that project's `docs/config-reference.md` "Executor pools" section first — the risk
  is silent scope creep, not a crash.
- If a named executor instance was hand-added to `.lanegate.yml` (rather than generated
  by `lanegate init`'s interactive wizard), it likely lacks flags/model the wizard would
  have set automatically — see §0a.

Run `lanegate route <TICKET-ID>` any time to confirm the resolved
`implement_executor`/review executor before trusting your mental model — config drifts.

## 0a. Setting up a new hand-added executor instance

`lanegate init`'s wizard is the only place that auto-populates the unattended/headless
flags an executor needs (e.g. `agy` needs `--dangerously-skip-permissions
--disable-slash-commands`; `codex` needs `--dangerously-bypass-approvals-and-sandbox
--ignore-user-config --ignore-rules --ephemeral`; `aider` needs `--yes-always
--no-gitignore`). `lanegate doctor` does **not** currently validate that a hand-edited
`executors:` entry has these — a missing flag/model fails silently or bizarrely at
dispatch time instead of at config time (permission-denied errors, wrong-model
rejections, or an executor's own sandbox failing outright), not with a clear "you're
missing X" message.

Before hand-adding a new executor instance to `.lanegate.yml`:
1. Check whether **another lanegate project on this machine already has a working
   config for that executor type** — a proven `.lanegate.yml` beats reconstructing
   flags from docs/trial-and-error.
2. Failing that, check `.lanegate.yml.example` (shipped alongside lanegate) for the
   `drivers:`/`steps:` pattern, which documents per-executor-type flags and is the
   currently-recommended config shape (cleaner than the legacy `executors:` +
   `executor:`/`reviewer:` bare-field approach used elsewhere in this skill for
   backward compatibility with projects already set up that way).
3. Give the instance an explicit `model:` if its type doesn't accept the project's
   default `models:` string (e.g. a cloud executor can't use a local model tag, and
   vice versa) — don't assume it'll fall back sanely; it may inherit an incompatible
   global default and fail with a confusing "unmapped model" error.
4. After adding it, run `lanegate doctor` and then actually dispatch one ticket through
   it before trusting it in a long unattended run — a config that loads cleanly can
   still fail on first real use. Missing headless flags typically surface as: a review
   executor failing permission checks on every dispatch, an "unmapped model" error, a
   model rejected as unsupported for the authenticated account, or an executor's own
   internal sandbox failing entirely with a low-level error — the last of which can
   silently degrade a review to read-only code inspection with zero command execution,
   with no error surfaced anywhere except deep in the captured output.

## 1. Launch

```bash
nohup lanegate run > /tmp/lanegate_run.log 2>&1 &
disown
```

No `-v` — default mode streams compact progress lines only, not full executor
transcripts, which is what keeps the log file (and any tail of it) small. `lanegate run`
picks up `default_milestone`/`--milestone` from `.lanegate.yml` same as always.

Confirm it's actually alive before moving on:
```bash
sleep 3 && pgrep -af "lanegate run"
```

## 1a. Before relaunching `lanegate run` — always double-check liveness

**Never relaunch based on a `lanegate run --status` reading alone.** That command can
report "No active LaneGate run" while the orchestrator process is still genuinely alive
and mid-step — status output isn't a substitute for checking the process table. Before
every relaunch:

```bash
pgrep -af "lanegate run"                    # exact single pattern, not an alternation
lanegate orchestrator-lock status           # confirms whether a lock is actually held
ps -p <pid> -o pid,stat,etime,cmd           # if pgrep is ambiguous, confirm directly
```

**Pgrep alternation footgun**: a combined pattern like `pgrep -af "lanegate run\|agy"`
can silently match nothing even when a process is alive — the escaping doesn't behave
the way you'd expect across shells. If a liveness check comes back empty and you have
any doubt, re-run with a single plain pattern and/or `ps -p <pid>` before trusting the
empty result.

Relaunching while an orchestrator is genuinely still running risks the new invocation
reaping an in-flight executor process as "orphaned" and killing it mid-work — a live fix
attempt SIGTERM'd under a false "not running" belief loses whatever that attempt hadn't
already committed. Don't assume you'll get lucky.

## 2. Supervise (cheap status only — never tail the full log)

Wake on a schedule (`/loop` dynamic pacing — start at ~4-5 min if there's been a recent
run of errors/config fixes needing close attention, widen to ~20-30 min once two
consecutive ticks pass with no new failure; this is background work, not something that
needs permanent minute-by-minute polling) and each time, run only:

```bash
lanegate run --status      # active ticket, executor PID, elapsed, log path
lanegate blocked           # anything stuck, with findings
```

Only pull anything bigger than these when one of them flags a problem — then a targeted
look at *that one ticket's* audit bundle (`.lanegate/executor-runs/<TICKET>/...`,
`status.json`/`captured-output.txt`) or a grep of the orchestrate log by ticket ID, not
the whole log and not `/tmp/lanegate_run.log` in full.

When `lanegate run --status` shows no active run (confirmed via `pgrep`/`ps`, not the
status output alone — see §1a) and `lanegate blocked` only contains tickets already
known to need the user, or is empty: the board is at rest. Run `lanegate run-report
--json` once for a final tally, report merged/blocked/remaining to the user, and stop
the loop (don't keep polling an empty or fully-blocked board).

**Both `board`'s "Next Steps" reason label and `blocked`'s top-line "Reason:" can go
stale** — independently, in either direction (sometimes showing an old failure reason
after a fresh unrelated one landed, sometimes the reverse). `blocked`'s detailed
Findings list underneath its own Reason line is usually accurate even when that Reason
line isn't. Whenever anything conflict- or rate-limit-related looks off, verify the
actual state directly (`git log`/`git status`/conflict-marker grep in the ticket's
worktree, a compile check, the real test suite) rather than trusting either command's
summary text alone.

## 2a. Checkpoint progress periodically — don't rely on conversation history alone

A supervision loop can run for many wakeups and consume a large fraction of the context
window before the board settles. Everything is reconstructible from the conversation
transcript right up until it isn't — a context compaction or session boundary landing
mid-loop loses the detailed *why* behind decisions (why a hand-fix was scoped the way it
was, why a ticket got escalated instead of retried, what a flaky auto-fix already tried
and failed). Don't wait until the very end to write any of this down.

Every few tickets resolved (merged, escalated, or settled) — not every wakeup, that's
too chatty — append a short checkpoint to a durable location: what merged since the
last checkpoint, what's newly escalated and why, current executor/reviewer/model config
state if it changed, and any open question the user will need to weigh in on. Two
reasonable targets, pick based on what the project already has:
- A running session-log doc in the project's own `docs/` (or equivalent) directory,
  committed alongside the code changes it describes — gives the user something
  reviewable.
- A memory entry, updated in place rather than re-created each time, if the project
  doesn't have a docs convention or the checkpoint is more about supervision process
  than project substance.

At the natural end of a supervision session (board reaches a stable resting state, or
the user asks to stop/switch tasks), do a final pass: make sure the checkpoint doc/memory
reflects the *final* state, not just the last mid-session update, and that any standing
config changes (a model swap, a workaround flag left enabled) are called out explicitly
so a future session doesn't have to rediscover them from `.lanegate.yml` alone.

## 3. Rotate the cloud reviewer between batches

If the project uses the bare-`reviewer:` rotation pattern (rather than `steps.review`
pinned to a fixed driver): between ticket dispatches (i.e. when `lanegate run --status`
shows no ticket `[implementing]`/`[reviewing]` — the run is between tickets, not
mid-dispatch), cycle `reviewer:` in `.lanegate.yml` through the project's available cloud
executor types. This is a plain text edit of the one `reviewer:` line — nothing else in
the file. Never edit config while a ticket is actively mid-dispatch; wait for the gap
between tickets.

Track which one was last set (read the current value out of `.lanegate.yml` rather than
assuming) so consecutive rotations don't repeat the same instance twice in a row.

**If the project has a Claude-based executor configured too**, it commonly should stay
supervisor-only by default — i.e. left out of the routine review rotation, reserved as a
last-resort fallback only when the other cloud reviewers are cooling down/rate-limited at
the same time (check via `lanegate executor status` first). Confirm this preference with
the user rather than assuming it, but if they've stated it once for a project, keep
honoring it for that project. Say so explicitly when a fallback actually happens.

## 4. Never self-approve — hand-fixing a ticket goes through a real reviewer too

If you (Claude) fix a ticket's code directly rather than letting an executor do it —
because auto-fix exhausted, or you're unblocking something a supervised run can't resolve
on its own — that fix still needs an **independent** review before merge, exactly like an
executor-authored change. Do not shortcut this by recording your own approval.

**Correct procedure:**
1. Implement the fix in the ticket's worktree (`.lanegate/worktrees/tick-NNN`), on its
   branch.
2. Verify it yourself first — run the project's tests/compile checks, actually execute
   the specific scenario the bug report described, not just "the code looks right now."
3. Commit in the worktree.
4. Get it to `code_complete`:
   - A **fresh** ticket you implemented from scratch: `lanegate complete TICK-NNN`.
   - A ticket **resuming from `needs_review`/`failed`/`hibernated`** after your fix:
     `lanegate human-review TICK-NNN --rationale "..."` — write the rationale to
     explicitly state you are clearing a *metadata/touches/environment* block or
     providing your own fix, **not** approving the implementation's correctness.
5. `lanegate run` (after the liveness check in §1a) to let the **orchestrator dispatch a
   real reviewer**. Do not call `lanegate review TICK-NNN --verdict approved` yourself —
   that verdict must come from the dispatched reviewer, not from you, or the entire
   point of an independent review is defeated.
6. Only run `lanegate merge TICK-NNN` after a genuine `review_verdict: approved` has been
   recorded by that dispatched reviewer.

**Don't trust a local implement/fix executor's output at face value just because tests
pass, especially before building further work on top of it or writing your own review
rationale that assumes it's real.** A model can pass its own unit tests while having
fabricated the actual behavior — e.g. hardcoding a plausible-looking response instead of
calling the real integration, where the test happens to mock exactly the part that's
fake. Where the ticket integrates something externally checkable (a network call, a
subprocess, a file the code claims to have created), do at least one manual live check
of the actual behavior before relying on it — the same discipline as step 2 above but
applied to output you're *inheriting*, not just output you wrote yourself.

**If a reviewer's verdict looks wrong** — e.g. it contradicts an architecture already
validated by an earlier independent review, or its own output shows signs it couldn't
actually execute anything (a sandbox/permission error embedded in its response, a
generic-sounding harness failure) — don't take it at face value. Pull the raw
`captured-output.txt` from that review's audit bundle before accepting or arguing with
the verdict; a `changes_requested` produced by a crippled reviewer that couldn't run
tests is not equivalent to one from a reviewer that could (see §0a's flag list — a
missing sandbox-bypass flag can silently reduce review quality to code-reading only,
with no error surfaced anywhere except deep in the captured output).

**Don't hand-dispatch a standalone `lanegate review`/`lanegate merge` and then hand-roll a
wait-loop around it** — this happened for real and caused two compounding problems at
once. First, a `pgrep -f "lanegate review TICK-NNN"` wait-loop launched via a backgrounded
shell call **matches its own wrapper process**, because the shell invocation running the
loop itself contains that exact string in its command line — the loop never sees the
target exit and spins forever even after the real review finished and printed its
verdict. Second, stacking a fresh one of these across turns instead of reusing/killing the
previous one left several duplicate wait-loops running at once, invisible until `pgrep -af`
was run and they were all listed together. If a standalone one-off dispatch really is
warranted, capture the PID directly at launch (`... & pid=$!`) and poll `kill -0 "$pid"` or
wait on that PID — never pattern-match by command text — and never launch a second wait for
the same target without confirming and clearing the previous one first. But the better fix
is usually to not hand-dispatch at all: relaunch `lanegate run` per the procedure above and
use the normal cheap `--status`/`blocked` polling from §2, so the same supervision loop
that watches the rest of the board also watches this ticket, instead of a bespoke wait
mechanism.

## 5. Before merging anything — check the full diff, not just what you touched

Before recording an approval or running `lanegate merge`, run:
```bash
git diff <trunk-branch>...tick-NNN --stat
```
and skim the whole thing, not only the file(s) you personally edited. An earlier fix
attempt on the same ticket (by an executor, or an earlier hand-fix) can leave stray
artifacts committed alongside real changes — e.g. an auto-fix pass running with an
auto-commit flag can pick up a build artifact or a database file the module happened to
create at its default path, and it merges straight to trunk if only the specific file
being fixed gets reviewed. Add anything genuinely non-source to the project's
`.gitignore` once found, rather than just deleting the one instance.

## 6. Corrective action on a flagged ticket

| Symptom | Action |
|---|---|
| `changes_requested`, fix/re-review cycle exhausted | `lanegate fix TICK-NNN` once more if budget remains, or leave for human if `max_auto_fix_attempts` already spent |
| `needs_review` from a metadata gap (e.g. "committed files outside touches list") for content that's actually in-scope | Verify the extra file(s) are legitimately part of the ticket's own stated task, update `touches:`, then `lanegate human-review TICK-NNN --rationale "..."` making clear you're correcting metadata, not approving content — then let the real pipeline re-review (§4) |
| `needs_review` from a genuine correctness problem | Read the specific findings; decide if a human is actually needed, or if you should hand-fix it yourself (§4's full procedure — never skip straight to recording your own approval) |
| rate-limited / cooling down cloud instance | `lanegate executor status` to confirm, `lanegate executor reset <name>` once the cooldown window has actually passed — don't force-reset early |
| merge conflict | `lanegate resolve-conflict TICK-NNN` once — if it fails again on the *same* conflict (conflict markers still present in the worktree after it reports failure — the common failure shape is "two independent tickets each add one case to the same function," where the fix agent keeps re-inserting the marker text into its own edit instead of removing it), don't keep retrying it: hand-resolve directly instead — `git rebase <trunk-branch>` in the ticket's worktree, resolve each conflicted hunk by hand (for the common case, keep both sides' additions side by side), verify no `<<<<<<<`/`=======`/`>>>>>>>` markers remain, run a compile check + the project's real test suite, then continue the rebase and proceed as a normal hand-fix (§4: commit, `human-review`, never self-approve) |
| `status: failed` | `lanegate run`/`next_batch` never auto-retries a `failed` ticket — it is permanently excluded from the eligible queue until reopened. Don't blind-reopen: read the ticket's findings/audit bundle first — `failed` usually means something actually broke (a real bug, a genuinely wrong scope), not a transient blip. Root-cause it (or confirm it's already fixed/no-longer-applicable), *then* `lanegate reopen TICK-NNN` so it re-enters the pool. If the root cause is itself a lanegate bug, file it per §7 before reopening — otherwise the reopen just reproduces the same failure. |
| rejected review recovery needed | `lanegate recover-rejected TICK-NNN` |
| `lanegate run` process died but open tickets remain | re-launch per §1, after the liveness check in §1a |

After acting, let the background run continue — don't re-dispatch the ticket yourself
outside of the commands above.

## 7. Filing genuine lanegate bugs found while supervising

Some failures aren't the current project's config/tickets — they're lanegate itself, or
an executor driver, misbehaving. Don't just treat these as "reopen and retry": when a
failure's root cause traces back to lanegate rather than the current project's own code,
report it so it lands in lanegate's own backlog instead of getting silently worked
around and forgotten.

**Signal to look for**: a failure reason too generic to explain what actually happened
(e.g. "executor exited 0 but made no commits", "reviewer harness error", a bare non-zero
exit with no further detail). That's a cue to pull the raw executor transcript — grep
the orchestrate log by ticket ID for the `[executing]`/`[review]` → `[finished/failed]`
window, or read that step's audit-bundle `captured-output.txt` directly — before
assuming a model was just incapable or a ticket was badly scoped. Real classes of bug
this technique catches: an edit silently dropped because a documented config override
was itself silently overridden by an internal heuristic; a fix that edited files
correctly but never committed because its prompt template never told it to; a review
that produced a complete, correct verdict but got discarded entirely because a harness
error on an unrelated instruction masked the good result; a review silently reduced to
code-only inspection with zero command execution because a missing flag left its
internal sandbox broken.

**Where to file it:**
- If lanegate's own source is checked out locally as a lanegate-managed project you have
  access to (self-hosted/dogfooding), file it as a real ticket on that board:
  ```bash
  cd <path-to-lanegate-source>   # skip if the project you're already supervising IS lanegate's own repo
  grep -n "^default_milestone" .lanegate.yml
  lanegate create "<precise root-cause description: what was expected, what actually \
  happened, the concrete trace including file:line if you found it, and what a fix should \
  cover>" --milestone <milestone> --no-analyze
  ```
  `--no-analyze` avoids a potentially long analyze pass for something you've already
  root-caused precisely. Ask the user which milestone to file under, or default to that
  project's current `default_milestone` (its **currently in-flight** work) rather than
  assuming "the next release," unless the user says otherwise.

  If you're supervising lanegate's own board directly, a bug filed under the current
  `default_milestone` lands on the *same* board the running `lanegate run` loop is
  already draining — it can get picked up mid-loop once analyzed, not just queued for
  later. That's normally fine/expected, just don't be surprised when a freshly-filed
  ticket shows up mid-run.

  If you find the exact source line(s) responsible after already filing a draft ticket,
  edit the ticket file directly to append a "Root cause update" section with the precise
  pointer — a ticket that says "here's the exact function and line" is far more
  actionable than one that only describes the symptom.
- Otherwise, open a GitHub issue against lanegate's repository with the same level of
  detail (root cause, file:line if known, repro steps). Don't open a public issue for a
  security vulnerability — see lanegate's `SECURITY.md` for private reporting.

## 8. Local-model / hardware context

If tickets are failing because a **local** model genuinely doesn't understand the task
(not a format/config mismatch — rule that out first per the corrective-action table and
§7's technique), check the current project's own docs (`CLAUDE.md` or equivalent) for
its hardware/model context before assuming nothing can be done. Flag it to the user
rather than silently downgrading scope or giving up — they may want to pull a stronger
local model, and hardware headroom is often not the actual constraint.

## 9. Stop conditions

- Board at rest (see §2) → report and stop.
- A ticket needs a genuine human decision beyond the table in §6 (ambiguous scope,
  destructive change, credential/secret involved, or a real correctness disagreement you
  can't resolve by hand-fixing per §4) → surface it to the user directly and pause
  rotation/supervision on that ticket, but keep supervising the rest of the batch.
- User asks to stop → `pgrep -af "lanegate run"` (plus `ps -p <pid>` if any doubt, per
  §1a) then kill only that specific PID (never a blind `pkill`), stop the `/loop`.
