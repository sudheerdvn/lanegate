# Changelog

All notable changes to LaneGate are logged here. Dates are the day a change merged to `main`.

## Unreleased

## v1.1.2 (2026-08-26): review independence, isolation hardening, and dispatch reliability

- **Review independence**: a human-approved security-sensitive ("red-lane") diff no longer re-triggers the same escalation on the next pass when the underlying commit is unchanged — approval now binds to the exact commit, so a genuinely new commit still re-escalates normally. Pool-health checks no longer treat a stale rate-limit hibernation marker as permanent, so one long-hibernated ticket can't make lanegate think an entire reviewer pool is down. `.lanegate.yml.example` now documents the same-executor-different-model pattern for genuine review independence without a second provider.
- **Main-checkout/worktree isolation**: the orchestrator lock is now scoped to the main checkout (fails loudly from a linked worktree, never kills a live lock holder, shows the holder's working directory), the isolation-leak detector no longer false-positives on routine sibling-ticket/config changes while still catching real leaks (including stray partial edits written directly to the main checkout during dispatch), review dispatch now shares the same hardened isolation check as the rest of the orchestrator instead of a second unpatched copy, and rebase-conflict recovery no longer sweeps in unrelated untracked worktree files.
- **Review dispatch correctness**: review steps now actually get the read-only sandbox they were always supposed to have, diff-access instructions no longer hardcode `main` as the trunk branch, and review is hardened for cross-file refactor tickets (touches-scoping, stateless re-review, a circuit breaker on recurring identical findings).
- **Executor/doctor validation**: `lanegate doctor` now checks every configured executor instance for required unattended-mode flags, hard-fails a codex executor missing its sandbox-bypass flag (and flags a review verdict when the executor's own output shows a sandbox error, so auto-fix can't act on a review that couldn't actually run), and no longer false-positives on bare executor/reviewer fields or its combined-mode warning when distinct drivers are already configured. `lanegate route --executor` now rejects an incompatible model pin at route time instead of dispatching it.
- **Aider reliability**: `edit_format` selection is now size-aware and respects an explicit override instead of silently changing it, the invalid `neutralize_touches`+`whole` combination is rejected at config-load time, and dispatch failures (config errors, context-budget issues, stalls) now surface real diagnostic detail instead of a bare exit code.
- **Analyze/close-criteria integrity**: drift is now caught at analyze time instead of only at review, the "already resolved" pre-check can no longer hallucinate a false verdict against code that doesn't match the current worktree, and the acceptance-contract audit no longer misreads its own findings text as new contract items.
- **Ticket lifecycle hygiene**: fix/auto-fix commits no longer sweep in debug scratch files via a blind `git add -A`, a failed ticket's worktree/branch is preserved when it has real commits ahead of trunk, `lanegate create` skips an unnecessary rebase when already an ancestor of upstream, a genuinely successful agy review is no longer discarded over an accompanying harness-level error, and `lanegate board`'s "Next Steps" reason label no longer goes stale.
- A Codex implement dispatch hitting an expired resume thread now retries once fresh instead of failing the ticket outright, and a real auto-commit failure now surfaces the actual git error instead of a generic "no commits" message.
- `lanegate run` now sets a repo-identifying process title, so concurrent runs across different repos are distinguishable in `ps`/`pgrep` output.

## v1.1.1 (2026-08-23): small fixes and reliability follow-ups

- `interactive_init()` refactored into named sub-steps for readability; prompt order, text, defaults, and validation behavior are unchanged.
- Review verdict JSON extraction is now resilient to unescaped interior quotes in the model's response, instead of failing to parse a well-formed verdict.
- Aider executor now supports per-model `context_window_tokens`/`edit_format` overrides.
- `.lanegate/notes/` writes are exempted from touches-drift blocking, so agents can update cross-ticket notes without tripping the drift check.
- Fix-dispatch's independence check now allows same-executor-different-model, matching the review path's existing behavior.
- `tui.py`'s `api_proc` teardown now kills the whole process group instead of just the PID, so child processes don't leak on exit.
- An aider executor routed to a local Ollama model with no model resolved for the current step (analyze/implement/fix/review/drift_check) now fails immediately with a config error instead of silently falling back to aider's own default — an interactive OpenRouter browser-auth flow that hangs for several minutes in a non-interactive dispatch and then fails, indistinguishable to the caller from a genuine step failure.
- The review, fix, and drift-check prompts' diff payload is now truncated at file boundaries when it exceeds the step's size budget, instead of a plain byte-offset clip that could sever a diff mid-file. A file that gets cut is now omitted whole (never partially shown) and named in an explicit note in the prompt, so a reviewer can't mistake "this file was cut from what I was shown" for "this file has no changes" — previously a large multi-file diff could silently drop the exact file a fix landed in from a re-review with no signal anything was missing.
- `lanegate init`'s model-selection wizard now also prompts for `models.fix` and `models.drift_check`, matching the existing analyze/implement/review prompts — these two steps were previously left unconfigured by a fresh `init`, which is what let the local-Ollama drift-check hang above go unnoticed until it actually happened.
- A local-Ollama aider setup detected during `init` now defaults `max_auto_fix_attempts` to 2 instead of the cloud-oriented default of 1, since a local model's auto-fix retries have no per-call cost the way a cloud API's do.
- Added a fully-local, VRAM-tiered driver example to `.lanegate.yml.example`: separate `aider-local-analyze`/`-implement`/`-review` drivers sized for a single consumer GPU, since analyze/review stay small and byte-budgeted while implement sees full file contents and is the step most likely to spill out of VRAM.
- `context-stats`' payload-composition audit now resolves the review step's `reviewer_type`/diff the same way `run_review_agent()` actually dispatches it (including the inlined-diff prompt shape for a non-tool-capable aider/ollama reviewer), instead of always auditing against the tool-capable default — previously this could under-report review payload size for a local-model reviewer setup.
- `analyze` and `orchestrate/pool`'s stdin-capable/streaming-capable executor-type checks now go through the shared `executor_types_with()` registry instead of separately hardcoded type sets at each call site, so the two can't silently drift out of sync with each other.

## v1.1.0 (2026-08-19): model-validation hardening, security fixes, review/merge reliability

- Model validation now catches a cross-vendor model string leaking to the wrong executor
  on every dispatch path, not just implement: the review path's `resolve_model` fallback
  and analyze's dispatch are now validated (previously only an explicit `review_model_pin`
  was checked on review, and analyze validated against the named `executors:` instance name
  instead of its resolved type, so the check silently no-op'd for any named instance). A
  named `executors:` instance can now also carry a single blanket `model:` field (as
  `docs/config-reference.md`'s own worked example already showed) as a fallback when no
  step-keyed `models:` entry is set. This is validated at config-load time like any other
  model field, so a bad value here is now a startup error where it was previously silently
  ignored.
- Removed the `treesitter` optional extra; tree-sitter grammars are now built-in.
- Removed implicit `docs/ARCHITECTURE.md` prompt injection. Reference documentation is now opt-in via `reference_docs` in `.lanegate.yml` with no hardcoded filename assumptions. Deprecated `architecture_doc` config key while continuing to honor it for backward compatibility.
- Analyze now stays read-only in practice, not just by convention: Claude-CLI executors get
  `--disallowedTools Bash,Write,Edit` during analyze, closing a gap where the executor's own
  `--dangerously-skip-permissions` flag otherwise left analyze with full unrestricted tool
  access it never needed (touches/close_criteria/change_notes were always written by
  `analyze.py` parsing the model's JSON response, never by the model editing files itself).
- The analyze prompt now embeds real, line-numbered signatures (via the existing stdlib-`ast`
  skeleton builder) for the files its symbol/importer search already matched, bounded to 25
  files / 15KB. The model gets accurate signatures up front instead of fetching them one file
  at a time through Read tool calls, which was the main source of analyze's multi-minute
  latency on tickets touching many files.
- `lanegate orchestrate`'s auto-analyze pass no longer drains the entire draft backlog before
  dispatching anything. It now stops as soon as one analyzed draft is actually dispatchable, so
  ready-to-implement work (already open, unblocked) never sits idle behind unrelated drafts
  still waiting their turn; the remaining drafts get analyzed interleaved with dispatch instead
  of front-loaded ahead of it. Priority order is unchanged: already-open/hibernated tickets
  always dispatch before any draft gets analyzed at all.
- Added `profile: strict` in `.lanegate.yml`. It bundles the safer end of the
  existing review/acceptance-contract knobs into one name: it defaults
  `acceptance_contract_mode` to `blocker` instead of `advisory`, and rejects
  `review_fallback: same_model` at config-load time (the one fallback that
  silently self-reviews when no independent reviewer or model is available).
  `profile: default` (or omitting the key) is unchanged. See
  [config reference](docs/config-reference.md#profile).
- Added `CONTRIBUTING.md` documenting the project's DCO (`Signed-off-by`)
  sign-off policy for commits, per the Developer Certificate of Origin.
- Fixed several permission-boundary gaps found in a pre-release security audit:
  `.lanegate.yml` is now hard-blocked from a worktree-planted override that could
  otherwise lower an agent's own permission thresholds; the reviewer's trusted
  instruction layer no longer builds from files an agent could edit inside its own
  worktree; review diffs now use three-dot (`...`) comparison instead of two-dot so a
  stale local branch can't pollute what the reviewer sees; the remote-divergence check
  compares the actual ticket branch instead of `HEAD`; ticket branches are now cleaned
  up on fail/reopen instead of accumulating; and the loopback API's mutating endpoints
  now require an auth token, closing a CSRF-style gap that survived an earlier
  CORS-only fix.
- Narrowed the `lanegate/` self-modification guard back down to just the
  safety-critical files (guards, reviewer, review dispatch, lifecycle status
  transitions) instead of leaving the whole directory unprotected. This restores real
  protection without re-blocking most of this repo's own dogfooded tickets.
- Closed two auto-merge code paths that could merge a ticket to main without ever
  running the red-lane risk scan, the blocked-file check, or static analysis. The
  red-lane scan itself no longer fails open (it used to silently treat a broken `git
  diff` as "no risk"); it now fails closed like every other gate in the chain.
- Review no longer strands a ticket in permanent `needs_review` when every eligible
  independent reviewer is temporarily unhealthy or cooling down: that case now gets a
  bounded hibernate/retry with a recorded reason and next-retry time, and `lanegate
  run` auto-recovers once the window passes.
- Lifecycle verification no longer downgrades or strands an already-merged ticket when
  a configured verification command can't resolve on the shell PATH; the pipeline now
  validates commands resolve before changing ticket state, and a later failed
  verification is recorded as a separate diagnostic instead of overwriting the merged
  state.
- The review agent no longer re-runs the full test suite during review (pre_complete /
  pre_merge / post_merge_verify already run it deterministically); it's now required
  to reproduce a correctness or verification-gap finding before reporting it rather
  than asserting a bug from reading the diff alone; and the "tests already ran" claim
  it's given is now tied to the actual commit under re-review instead of a stale claim
  carried across an auto-fix cycle.
- The read-only guard during `analyze` (no Bash/Write/Edit) now applies to every
  executor type, not just Claude. Aider, codex, and agy previously kept full
  edit/commit access during what's supposed to be a read-only planning step.
- The orchestrator's run history and TUI no longer fabricate a stream of fake
  "direct action" entries for every ticket dispatched during a normal `lanegate run`.
  Only genuinely direct human/CLI actions are tracked now, and the spurious
  per-action log file written for each ticket at each phase is gone.
- Fixed an edge case where a ticket that had ever been rate-limit-hibernated and later
  resumed could carry a stale hibernation marker forever, causing a later genuine
  human-escalation case to be misclassified as an old rate limit.
- Analysis contracts for high-risk control-plane tickets (config, security, lifecycle,
  orchestration, prompt-trust) now require a structured acceptance matrix: invariants,
  adversarial/failure cases, compatibility cases, and exact regression tests, mapped
  to tests before implementation starts, to cut down on review churn from
  underspecified tickets.
- Fixed several crash and false-positive bugs in ticket-ID handling and dispatch: a
  malformed ticket ID no longer crashes every command scanning the board (hardened
  across the CLI, the MCP boundary, and internal call sites); `--tickets` no longer
  false-positives "unknown ticket" on real tickets, caused by two dispatch code paths
  loading the ticket list differently; a crash on a stale, non-fast-forward branch no
  longer gets misreported as a rate-limit pause; and stale-branch recovery can now
  complete a multi-conflict rebase without dropping back to manual git steps.
- Fixed an orchestrator-lock race that could leave an orphaned run with no run_end
  event or ticket list if the lock was already held when a run started; run history
  now also distinguishes a manually-triggered run from one launched automatically by
  the resume-watch daemon after a rate-limit recovery.
- `resume-watch`'s retry backoff is now lock-aware, so it no longer burns a full
  backoff cycle retrying while the original orchestrator process still holds the lock.
- Fixed the global batch dispatch cap silently throttling an entire run to one
  concurrent ticket whenever any single low-capacity pool member (e.g. a GPU-bound
  local model) was present, instead of reflecting the pool's real total capacity.
- Removed the vestigial top-level `executor:`/`reviewer:` config keys and collapsed a
  sprawl of overlapping named pools down to one canonical pool. Least-loaded routing
  already picks the right instance, so the extra pool names were just confusing which
  one was actually active.
- `needs_review` recovery advice no longer points at recovery commands that silently
  no-op for a ticket that's already had an auto-fix attempt.
- The TUI Run screen no longer gets stuck failing to update live after a while,
  requiring a full quit-and-reopen to see progress again.
- Fixed a severity-classifier false-positive rate: the Raw Audit Log's error flagging
  matched the substring "error" anywhere in a line, including in ordinary prose, so
  unrelated ticket text was getting flagged red.
- Added an MCP `create` tool so a master agent driving lanegate purely over MCP can
  create tickets without falling back to the CLI or writing ticket files directly.
- `lanegate create` now explains what a failed auto-analyze pass actually means and
  gives a concrete next step, instead of leaving the ticket stuck in draft with no
  guidance.
- `lanegate doctor`'s remedy text for a missing tree-sitter grammar no longer suggests
  a plain `pip install lanegate`, which was a no-op once lanegate was already
  installed and left the warning reappearing on every run.
- Added `--reviewer`, `--executor`, and `--model` flags to `lanegate route` so routing
  metadata can be updated safely instead of hand-edited.
- `context-stats --compare` now shows a `claude` row (it was silently missing despite
  claude driving the majority of tracked spend) and adds a cost/token-usage column for
  DB-backed executor rows.
- Tiered CLI help is now fixed to 80 columns regardless of terminal width, instead of
  expanding unpredictably on wide terminals.
- Raised this repo's unattended review-fix budget to two auto-fix cycles.
- The aider-ollama executor can now pick from an ordered list of context-size tiers
  (`context_tiers`) and automatically selects the smallest model whose context window
  fits the ticket's estimated token cost.
- Assorted hardening and cleanup: automated commits (including the daily
  traffic-snapshot job) are now DCO-signed like the rest of the tree; the shared
  ticket-notes store tolerates missing symlink privilege on Windows and no longer
  fails closed on a pre-existing real notes directory; safeguard test subprocesses are
  now reaped via process groups with a concurrency lock so parallel test runs stop
  thrashing host resources; pre-merge worktree verification is re-enabled before
  merge; the agy executor resumes a previous session with `--conversation` instead of
  an unsupported `--resume` flag; and a full mypy typecheck pass now runs clean across
  the tree.
- Combined (self-review) mode — where one executor implements and reviews a ticket in the
  same session — is now gated behind an allowlist of executor types that actually have the
  shell/tool-execution capability to self-drive it (`claude`, `claude-subagent`,
  `claude-process`, `codex`, `agy`; asserted to stay a subset of the canonical
  `_VALID_EXECUTOR_TYPES` registry). A pure code-editing tool like `aider` has no such
  capability — pinning `reviewer:` to it explicitly used to force combined mode anyway,
  producing a ticket that committed real, correct code and then failed identically on
  every retry. An executor outside the allowlist now falls through to split-mode/
  independence-ladder dispatch instead, which always completes. The "reviewer resolves
  identically to the implement executor" warning (`lanegate doctor` and the startup config
  check) shares the same allowlist, so it no longer fires for executors that can't run
  combined mode anyway.
- `lane init --interactive`'s reviewer prompt now distinguishes a blank answer from a
  typed one: leaving it blank keeps `reviewer` unset in `.lanegate.yml`, so the
  independence ladder resolves it at dispatch time (including its `review_fallback:
  needs_review` safety escalation for single-account setups) instead of a blank Enter
  permanently disabling that safety net. Typing a value — even one matching the executor —
  is still a deliberate pin; an unrecognized/typo'd answer is now treated like blank rather
  than silently pinning self-review by mistake. The prompt's bracketed default now reads
  `[auto]` instead of the executor's name, since a blank answer here doesn't behave like
  every other prompt's accepted default.
- `lanegate run` no longer hard-errors on a fresh project's first ticket just because no
  `--milestone`/`--all`/`default_milestone` was given. If no ticket in the tickets dir uses
  the `milestone` field at all, there's nothing to scope by, so it now runs everything
  (same as `--all`). The error still fires once any ticket actually sets `milestone`, since
  the scope becomes ambiguous at that point.
- After a review attempt comes back unapproved, `lanegate run` now logs the ticket's real
  outcome instead of a generic one: a ticket escalated to `needs_review` (e.g. no
  independent reviewer available), one hibernated for a temporary rate limit/reviewer
  cooldown, and one correctly parked at `in_review` awaiting a pool-resolved human
  reviewer each get their own accurate outcome, rather than risking an "auto-fix/re-review
  did not reach approval" message overwriting a more specific reason already recorded on
  the ticket. `run_auto_fix_cycle` also enforces its own `code_complete` precondition
  internally now, so a future caller can't reintroduce that overwrite.
- `.lanegate.yml` is no longer gitignored by `lanegate init` — `git worktree add` only
  checks out committed content, so an ignored (never-committed) config left the very first
  ticket's worktree without one at all. Re-running `init` (or upgrading) also migrates a
  project whose `.gitignore` already had a stale `.lanegate.yml` entry from before this
  change, instead of leaving it gitignored forever.
- Analyze now corrects a flat-guessed touches path (e.g. `calc.py`) against the real
  nested file (`src/calc.py`) when the guess doesn't exist on disk but uniquely matches a
  tracked file's basename elsewhere in the repo, deduping the touches list when two
  declared paths correct to the same real file.
- Fixed the interactive `lanegate start`/`=== Context Prompt ===` terminal block always
  printing `Invariants: none`, even when the ticket has real invariants. It was reading a
  top-level `invariants` field that never exists; the analyzer nests it under
  `acceptance_matrix.invariants`, which is what the actual executor prompt already read
  correctly. Cosmetic only — the executor's prompt was never affected — but misleading to a
  human glancing at the terminal.
- Wizard prompts (`_prompt` and the five yes/no confirms) now degrade to their default
  instead of raising a raw `EOFError` traceback when piped/non-interactive stdin runs out
  mid-wizard — previously inconsistent, since one unrelated prompt in `cli.py` already
  caught `EOFError` while everything in the main wizard didn't.
- Docs: fixed `docs/config-reference.md`'s `--human-review` reference table and
  `README.md`'s security recommendation, both of which described the wrong default
  behavior (a default project silently skips review and auto-merges, and `autonomy` is
  unrelated to the merge gate — neither is true; see the fixed docs for the actual two
  independent axes). Fixed `docs/demo-walkthrough.md`'s local-Ollama example, which showed
  a bare `executor: ollama` config that errors at dispatch for implement/review (only
  `executor: aider` with an `ollama`/`ollama_chat`-prefixed model is a supported
  code-writing path); added the missing `needs_review` (no independent reviewer) escape
  hatch via `lanegate human-review --rationale`, which the walkthrough's review-step
  coverage skipped entirely; added a one-line prerequisite note (README and the walkthrough)
  that safeguard commands like `pytest` run in the project's own environment and must be
  installed there first; documented `lane init --interactive`'s agy setup adding
  `--disable-slash-commands` alongside the already-documented
  `--dangerously-skip-permissions`; and replaced `AGENTS.md`'s stale Codex flag combo
  (`--sandbox workspace-write --approve-for-me`) with the one already fixed everywhere else.
- `lane init --interactive` now prints a one-time warning when piped/non-interactive stdin
  runs out mid-wizard, instead of silently defaulting every remaining prompt with no
  signal. Degrading to defaults on exhausted stdin (see above) fixed a raw traceback, but
  it also removed the only sign that a piped answer string had the wrong line count — a
  miscounted/misaligned answer set could silently write an unintended config (e.g. with
  `executor`/`reviewer` swapped) with nothing flagging it before `.lanegate.yml` was
  written.
- Cost tracking (`context-stats`, `step_costs`) no longer trusts a self-reported
  `duration_ms`/`duration_seconds` larger than the dispatch's own measured wall-clock
  elapsed time — it's now clamped to that measured value. `agy`'s `duration_seconds`
  reflects the whole resumed `--conversation` session (prior turns included), not just the
  current invocation, so it could report nearly double the actual subprocess call's
  duration and inflate that step's tracked cost.

## v1.0.3 (2026-08-04): fixture redaction, docs

- A test fixture (`tests/fixtures/captured_output/tick-349-nested-brace-review.txt`) was a
  real captured Claude Code session transcript: real session IDs, timestamps, cost data, and
  internal file paths. The test only needs one JSONL line, so replaced the 162-line real
  transcript with a small synthetic one carrying the same structural shape.
- README now documents the `treesitter` optional extra (`pip install "lanegate[treesitter]"`)
  for non-Python projects — without it, analyze's non-Python symbol matching silently falls
  back to plain ripgrep text search, which is less precise and pushes more exploration (and
  token cost) onto the analyzing agent. Python-only projects don't need it. This extra already
  shipped in earlier versions; it just wasn't documented.

## v1.0.2 (2026-08-04): docs cleanup

- Removed a migration doc that referenced this project's working name before its first
  public release. It didn't apply to anyone: there was no prior public release to migrate
  from.

## v1.0.1 (2026-08-03): Windows encoding fix

- Explicit `encoding="utf-8"` on the remaining ~85 `subprocess.run`/`Popen` call sites that
  still defaulted to `locale.getpreferredencoding()`, which mangles non-ASCII output on a
  default Windows setup. `git.py` was already fixed as part of the earlier Windows CI pass,
  and this closes out the rest of the tree.
- Added test coverage for two previously-untested modules: `lanegate/ghsync.py` (GitHub
  Issues mirror) and `lanegate/agent_tools.py` (Claude/Codex/MCP installer).
- Fixed a pre-rename leftover (TICK-388): 96 ticket files still had `file_skeletons_ref`
  pointing at `.vyuha/context/...` instead of `.lanegate/context/...`, which failed schema
  validation and silently quarantined those tickets off the board.

## v1.0.0 (2026-08-03): first public release

### Defaults changed from earlier development

Two behaviors that used to require opt-in are now the default for new `lanegate init`
projects:

- **Independent review by default.** Review no longer silently self-reviews. It runs a
  genuinely separate reviewer (a different tool instance, a different model, or a
  different account, in that preference order) before falling back to self-review only
  when nothing else is available.
- **Ticket evidence is git-tracked by default.** Ticket files land under version control
  out of the box instead of being gitignored, so ticket history survives the same way
  the rest of the repo's history does.

### Known gaps not yet run (read before relying on this release)

This release has **not** had the following checks run against it. They don't block
day-to-day use, but you should know they're outstanding rather than assume full coverage:

- **No `mypy` pass has been run against the tree** (tracked as TICK-365, deferred to v1.6).
- **No systematic sweep for drifted duplicate logic has been run.** A handful of instances
  were found incidentally during development and fixed as they were found, but a
  deliberate grep-driven sweep across module boundaries has not happened (tracked as
  TICK-366, deferred to v1.6, and should run *before* 365).

A few smaller items are also explicitly deferred to v1.6 and tracked as open tickets
(TICK-378, TICK-379, TICK-383). None are launch blockers. All are pre-existing scope
decisions, not omissions discovered late.

- **`lanegate flag` has only been unit-tested, not run end-to-end against a real deploy.**
  `tests/test_flags.py` covers the read-modify-write logic against temp paths, but no real
  project has a `.lanegate.yml` `environments:`/`flag_file` setup with an actual deploy hook
  reading the result. Tracked as TICK-387, deferred to v1.6.
