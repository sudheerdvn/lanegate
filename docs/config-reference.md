# LaneGate Config Reference

This document describes the `.lanegate.yml` configuration keys supported by LaneGate.

---

## Default tracking posture & opt-out

By default, LaneGate commits ticket specs and review verdicts (`review_verdict`/`review_summary` frontmatter fields) to git history as they change (`commit_status_changes: true`), while keeping raw executor transcripts, logs, cooldowns, locks, and worktrees local under `.lanegate/`. Analysis sidecar artifacts (file skeletons under `.lanegate/context`) also remain local-only.

Set `commit_status_changes: false` in `.lanegate.yml` to opt back into fully zero-footprint local state.

## Default values

| Key | Default | Notes |
|-----|---------|-------|
| `tickets_dir` | `.lanegate/tickets` | Git-tracked by `lanegate init` (carved out of `.gitignore`) |
| `worktrees_dir` | `.lanegate/worktrees` | Gitignored by `lanegate init` |
| `trunk_branch` | auto-detected | Ticket worktree/diff/merge base. An explicit value wins. Otherwise LaneGate reads `refs/remotes/origin/HEAD`, then falls back to `main`. |
| `commit_status_changes` | `true` | Set to `false` to disable automatic git commits for status changes |
| `github_pr` | `false` | Set to `true` to auto-push branches and open PRs on review |
| `ticket_prefix` | `TICK` | Prefix for ticket IDs |
| `executor` | `claude` | Which AI executor runs implementations |
| `max_parallel` | `2` | Maximum concurrent in-flight tickets |
| `executor_idle_timeout_seconds` | `75` | Kill only when executor output is quiet *and* its verified heartbeat is stale |
| `executor_stall_timeout_seconds` | `900` | Kill a live executor only after this long without parsed semantic progress |
| `executor_absolute_ceiling_seconds` | `1500` | Final hard limit for one executor invocation |
| `print_timeout_seconds` | `30` | Single-turn terminal output print timeout in seconds |
| `tree_sitter_languages` | `{}` | Custom file extension to tree-sitter grammar package mapping |
| `safeguards` | `{}` | Optional pre-complete / pre-merge verification commands |
| `max_turns` | `null` | Optional executor turn cap; see [Dispatch budget caps](#dispatch-budget-caps) |
| `max_cumulative_tokens` | `null` | Optional executor token cap; see [Dispatch budget caps](#dispatch-budget-caps) |
| `max_auto_fix_attempts` | `1` | Fix → drift-check → re-review cycles run when a review returns `changes_requested`, see [Auto-fix attempts](#auto-fix-attempts) |
| `on_rate_limit` | `resume` | `halt` or `resume`, see [Rate limits and auto-resume](#rate-limits-and-auto-resume) |
| `notify.ntfy_topic` | `null` | ntfy.sh topic for phone alerts, see [Phone alerts for stuck runs](#phone-alerts-for-stuck-runs-notify-watch) |
| `notify.poll_seconds` | `60` | How often `notify-watch` checks state |
| `notify.heartbeat_stale_seconds` | `180` | Seconds without a heartbeat before the executor is considered wedged |

## Executor watchdogs

Executor monitoring separates process liveness from work progress. A fresh heartbeat means a quiet child process is still alive, so `executor_idle_timeout_seconds` does not terminate it merely for producing no output. Parsed executor events reset the longer `executor_stall_timeout_seconds` clock. A live but non-progressing process eventually gets stopped there instead. `executor_absolute_ceiling_seconds` remains the final backstop for every invocation.

The values must satisfy:

```text
executor_idle_timeout_seconds < executor_stall_timeout_seconds < executor_absolute_ceiling_seconds
```

## .gitignore management

`lanegate init` automatically manages entries in `.gitignore` (creating the file if it does not exist):

```
.lanegate/*
!.lanegate/tickets/
!.lanegate/tickets/*
```

The state directory is ignored while the ticket spec files are carved out and tracked. `.lanegate.yml` itself is **not** gitignored — commit it. A ticket worktree is created with `git worktree add`, which only ever sees committed content, so an ignored (never-committed) config leaves the very first ticket's worktree with no config at all.

When `aider` is configured as the executor, reviewer, or any `executors:` instance, init also adds `.aider.*` to `.gitignore`. Aider's own default behavior silently modifies `.gitignore` (adding the same pattern) as an uncommitted side effect separate from its own commit; seeding the pattern up front makes that edit a no-op instead of an unexpected diff LaneGate's scope-drift check would otherwise flag. It also covers aider's scratch/cache files (chat history, input history, tags cache) if you pass `--no-gitignore` in `executors.aider.flags` to stop aider from managing `.gitignore` itself — without this entry already present, those scratch files would be swept into the commit and flagged instead. See [aider capabilities](executor-capabilities.md#aider).

## Opt out of git tracking

To disable automatic git commits for ticket status changes:

```yaml
commit_status_changes: false
```

To auto-push branches and open pull requests when a review is approved:

```yaml
github_pr: true
```

To store tickets in a fully separate git-tracked directory:

```yaml
tickets_dir: tickets
worktrees_dir: worktrees
```

And remove `tickets/` from `.gitignore` (or don't add it in the first place).

## Re-init safety

If you run `lanegate init` on a project that already has tickets in a directory other than the default (e.g., you previously used `tickets/`):

- lanegate detects the existing directory and prints a warning.
- `tickets_dir` is set to the existing location so your tickets are not lost.
- lanegate **never** deletes or moves existing ticket files.
- To switch to the new default, migrate your tickets manually and then update
  `tickets_dir` in `.lanegate.yml`.

## Full example

```yaml
ticket_prefix: TICK
tickets_dir: .lanegate/tickets       # git-tracked by default
worktrees_dir: .lanegate/worktrees   # gitignored
commit_status_changes: true
executor: claude
max_parallel: 2
github_pr: false                  # no auto-push to remote
trunk_branch: main                # optional; override origin/HEAD detection

core_files:
  - src/core/auth.py
core_patterns:
  - "src/db/**"

verification:
  groups: []   # see "Visual verification for UI tickets" above — populate for projects with a UI to visually verify

lock_statuses: [in_progress, code_complete, in_review]

safeguards:
  pre_complete:
    - pytest
  pre_merge:
    - pytest
  post_merge:
    - pytest

flag_file: ~/.lanegate/feature_flags.json

environments:
  - name: staging
    branch: staging
    from: main
    trigger: auto        # lanegate auto-promotes this env during cmd_merge when from matches merge target
    flag_file: ~/.lanegate/staging.feature_flags.json

  - name: production
    branch: production
    from: main
    trigger: manual      # LaneGate promote production runs the full sequence
    guard_script: scripts/check-deploy-window.sh
    pre_promote:
      - scripts/run-integration-tests.sh
    post_promote:
      - scripts/restart-service.sh
    flag_file: ~/.lanegate/feature_flags.json
```

## Explicit config always wins

Values set in `.lanegate.yml` always override the built-in defaults. If you have `tickets_dir: tickets` in your config file, it is used as-is regardless of what the current default is. You never need to re-run `lanegate init` after changing `.lanegate.yml`.

---

## Project guidance

LaneGate can add repo-local coding practices to the analyze, implement, and review prompts. By default it looks for common convention files such as `AGENTS.md`, `CLAUDE.md`, `.cursorrules`, `.cursor/rules/*.mdc`, `.github/copilot-instructions.md`, `CONTRIBUTING.md`, and `docs/DEVELOPMENT.md`.

Use `project_guidance` to point LaneGate at project-specific files or to disable this layer:

```yaml
project_guidance:
  include_defaults: true
  files:
    - docs/coding-practices.md
    - docs/architecture-decisions.md
  max_bytes: 20000
```

Set `project_guidance: false` for projects where convention files are noisy or should not be sent to the executor. Guidance is added as trusted project policy, but LaneGate lifecycle instructions, ticket close criteria, and safety rules still take precedence.

---

## Reference docs

Documents whose relevant sections are excerpted into analyze, implement, and review prompts so agents see architectural boundaries without every ticket restating them. Opt-in: LaneGate never guesses a project's doc filenames, and an unconfigured project gets no reference-doc injection at all.

```yaml
reference_docs:
  - docs/ARCHITECTURE.md
  - docs/adr/0007-storage.md
```

Prefer this over listing the same file under `project_guidance.files`. Reference docs are scoped to the ticket's touched paths and bounded by the per-step payload budget (12KB analyze/implement, 8KB review/fix); guidance files are bounded only by the looser `project_guidance.max_bytes`. A file listed in both is injected once, via `reference_docs`.

The step budget is shared across all listed docs, so adding a second one cannot silently double the prompt.

---

## Code discovery

LaneGate tells agents how to find code, cheapest route first, because open-ended repo-wide searching is the single most expensive way to answer "where is X defined" and is billed again on every subsequent turn.

The built-in path needs no configuration: `lanegate symbols <file>...` lists declarations straight from the AST (stdlib `ast` for Python and tree-sitter for Go, JS, TS, Rust, Java, Ruby, C, and C++).

Projects already running their own code-intelligence tool can declare it as an optional extra, ranked below the built-in and above raw search:

```yaml
code_intel:
  command: 'mytool query "<question>"'
  description: returns a scoped subgraph rather than raw file dumps
```

A bare string is accepted as shorthand for `command`. Nothing is required here, and no third-party tool is a LaneGate dependency — omitting the key simply leaves it unmentioned in the prompt.

### Project-declared tree-sitter grammars (`tree_sitter_languages`)

To enable symbol extraction and file skeletons for non-standard file extensions or custom grammars, configure `tree_sitter_languages`:

```yaml
tree_sitter_languages:
  .vue: tree_sitter_vue
  .lua: tree_sitter_lua
```

LaneGate imports the corresponding Python grammar package dynamically to parse symbols for matching files.

---

## Turn advisories

LaneGate measures what each agent dispatch costs in turns, tokens, and dollars, and reports it after the step. **This never stops or limits a dispatch.** A cap would punish large tickets rather than wasteful ones, and a killed run that is retried starts a fresh conversation that re-explores the repository — costing more than letting the original finish.

When a run exceeds the advisory threshold for its step, LaneGate also attributes the cost from the turn mix: whether the agent spent its turns re-deriving context LaneGate failed to supply, spread changes across enough files that the ticket should be split, or kept re-running failing tests.

```yaml
turn_advisory:
  implement: 100    # defaults: analyze 30, implement 100, review 50, fix 50, drift_check 20
  review: 0         # 0 disables the advisory for that step
```

Metering requires an executor that streams progress events (Claude and Codex today); others report nothing rather than a misleading count.

---

## Dispatch budget caps

`max_turns` and `max_cumulative_tokens` stop an executor dispatch once its measured turn count or cumulative token count reaches the configured positive-integer cap. Both default to `null`, which leaves the corresponding cap disabled.

Use a scalar to apply a cap to every dispatch:

```yaml
max_turns: 50
max_cumulative_tokens: 1000000
```

Or use a mapping to cap individual execution steps. The allowed step names are `implement`, `review`, `fix`, and `drift_check`; an omitted step has no cap for that key.

```yaml
max_turns:
  implement: 60
  review: 30
max_cumulative_tokens:
  implement: 1200000
  review: 300000
```

---

## Visual verification for UI tickets

LaneGate has no way to run a browser or observe the app itself. It inspects the git diff after the executor exits, nothing else. `verification` exists so the *prompt* (which is LaneGate's responsibility) can tell the agent when a visual check is expected and where to find the running app. The agent still needs its own browser/screenshot tool (a Playwright MCP server, an in-session browser subagent, etc.) wired up on the executor side, and that setup happens outside LaneGate, in your MCP client config.

`verification.groups` is a list, not one flat block, because a single repo can have more than one distinct UI area with its own dev server and URL. For example, separate `apps/web` and `apps/admin` frontends in a monorepo, or, in an AEM project, a webpack-served `ui.frontend` clientlib on `localhost:3000` alongside the actual AEM author instance on `localhost:4502` that renders `ui.apps`/`ui.content`:

```yaml
verification:
  groups:
    - patterns: ["apps/web/**", "**/*.tsx"]
      dev_server: "npm run dev"
      url: "http://localhost:3000"
    - patterns: ["apps/admin/**"]
      dev_server: "npm run dev:admin"
      url: "http://localhost:4000"
```

Each group needs a non-empty `patterns` list. `dev_server`/`url` are optional free text passed straight into the prompt. A ticket can match more than one group at once (e.g. a change spanning `apps/web` and `apps/admin`), and every matched group's dev_server/url gets listed so the agent knows which environment applies to which files.

When a ticket's `touches` match any group's `patterns`:

- `implement` gets an added instruction to run the relevant app(s) (using each matched group's `dev_server`/`url`) and visually confirm the change with whatever browser tooling it has access to, then leave a `Verification:` line in the commit message describing what it observed, or say plainly that it had no such tooling.
- `review` gets an added instruction to look for that `Verification:` note in the commit log (LaneGate passes `git log base..branch` to the reviewer alongside the diff, specifically for this) and to return `changes_requested` if a UI-facing ticket has no verification evidence at all.

Leave `groups` empty (the default) to disable this: no verification instructions are added. This only shapes the prompt. It is not a hard gate the way `safeguards` is, since LaneGate cannot mechanically check that a visual observation is genuine, only that a `Verification:` note exists.

Directory-scoped patterns (`apps/web/**`) hold up better than pure file extension patterns (`**/*.tsx`) in a polyglot monorepo, since extensions don't capture `.vue`/`.svelte`/Angular templates or CSS-only visual changes, and directories map to actual team/app ownership boundaries.

---

## Per-step executor configuration

### `executor`

The global default executor used for all pipeline steps unless overridden.

```yaml
executor: claude   # default
```

Valid values: `claude`, `claude-subagent`, `claude-process`, `aider`, `openhands`, `codex`, `ollama`, `gemini` (deprecated 2026-06-18, use `agy`), `agy`, `continue`.

For a detailed comparison of executor capabilities, including headless support, prompt transport mechanism, local model support, auto-commit behavior, and sandbox status, see [docs/executor-capabilities.md](executor-capabilities.md).

For local/offline execution, use `executor: aider` with a local Ollama model as aider's
backend:

```yaml
executor: aider
models:
  analyze: ollama/llama3.1                    # any executor works for analyze -- text-only, no file edits
  implement: ollama_chat/qwen2.5-coder:14b    # must include ollama/ or ollama_chat/ prefix
  review: ollama_chat/qwen2.5-coder:14b
  review_escalation: ollama_chat/qwen2.5-coder:32b # optional escalated model for retried reviews
```

This is the validated local/offline path, because Aider has a real read/edit/commit
loop. See [executor-capabilities.md](executor-capabilities.md#aider) for
`context_window_tokens` and other local-model-specific settings.

**`executor: ollama` (`type: ollama`) does not itself apply edits or commit.** It posts
to Ollama's REST API (`{base_url}/api/generate`, default `http://localhost:11434`) and
returns a single text completion. Ollama is a model server, not a coding agent, so
`implement`/`review`/`fix`/`drift_check` steps dispatched to it now raise a
configuration error at dispatch time instead of silently producing zero commits and
failing. It is only useful for the text-only `analyze` step. See
[executor-capabilities.md](executor-capabilities.md#ollama) for the full caveat.

#### Remote/rented-GPU Ollama (SSH tunnel)

Ollama's API is unauthenticated, so don't expose it on a public port. Tunnel it instead,
then point aider at the tunnel via the `OLLAMA_API_BASE` environment variable (aider
reads this directly, LaneGate has no separate config field for it):

```bash
ssh -N -L 11435:localhost:11434 <user>@<remote-host>
export OLLAMA_API_BASE=http://localhost:11435
lanegate run
```

`base_url` only takes effect on a **named `type: ollama` instance under `drivers:`**
(see [Named driver instances](#named-driver-instances-drivers) below), that's the raw
REST driver above, still only viable for text-only steps like `analyze`:

```yaml
drivers:
  remote-ollama:
    type: ollama
    model: qwen3-coder:30b-a3b-q4_K_M
    base_url: http://localhost:11435   # local end of the tunnel above

executor_steps:
  analyze: remote-ollama   # or set per-ticket `executor: remote-ollama`
```

### `reviewer`

Optional reviewer for the review step. Use another supported executor for model review, or `human` when a person should record each review verdict.

```yaml
executor: claude
reviewer: human
```

For a batch-level human gate, run `lanegate run --human-review final` without changing the reviewer. For per-ticket human verdicts, set `reviewer: human` and run `lanegate run --human-review per_ticket`. LaneGate completes the implementation, moves the ticket to `in_review`, and halts before merge. A person must then run `lanegate review <ticket> --verdict approved` or request changes.

#### `--human-review` reference

`--human-review` (default: `none`) does **not** control whether a review runs — that's a separate axis, governed by `reviewer`/pool/`executor_steps.review` configuration and [`review_fallback`](#review_fallback) below. A default project never silently skips review and auto-merges: split mode always dispatches a reviewer, and if no independent reviewer is available and `review_fallback` is at its default (`needs_review`), the ticket escalates to `needs_review` and stops for a human instead of falling back to an unreviewed merge. (The only way to get a truly unreviewed auto-approval is to explicitly configure `reviewer: none`/`auto-none` — an opt-out, not something `--human-review` triggers.) Combined mode has the same executor perform both steps in one prompt instead of dispatching a separate reviewer, but it still applies the merge gate below like split mode does. Setting `reviewer: human` overrides everything here and always pauses for a human-recorded verdict, in either mode.

What `--human-review` actually controls is whether an *additional* human approval is required to merge once a review verdict already exists (from an agent, from `reviewer: human`, or from a human resolving a `needs_review` escalation):

| `--human-review` | Extra human merge gate |
|---|---|
| `none` (default) | Only skipped when the ticket's `autonomy` is *also* `full`, `green`, or `yellow` (the auto-fix lanes). The default `autonomy: supervised` still pauses every approved ticket for a human merge decision — merge is fully unattended only when both conditions hold. |
| `per_ticket` | Always pauses the ticket for a human merge decision after approval, regardless of autonomy. |
| `final` | Approved tickets aren't merged individually; they wait for one end-of-batch human approval pass instead. |

None of these three values change whether an independent reviewer is dispatched — only `reviewer:`, pool/`executor_steps.review` configuration, and `review_fallback` do.

#### `default_human_review`

`--human-review`'s Python default is `"none"`, and that has no project-wide override unless you set `default_human_review` in `.lanegate.yml`:

```yaml
default_human_review: per_ticket   # or "final"; falls back to "none" if unset
```

`cmd_orchestrate` uses this value only when `--human-review` is not passed explicitly on the CLI. An explicit CLI flag (including `--human-review none`) always wins over this config default. This lets a higher-stakes project set a safe default once, instead of relying on every `lanegate run` invocation remembering the flag.

**This is related to, but separate from, a ticket's `autonomy` field.** Valid autonomy values are `full`, `supervised`, `manual`, `green`, `yellow`, and `red`. `full`, `green`, and `yellow` stay on the automatic fix/merge path (`is_auto_fix_lane`); `supervised` (the default when unset) and `manual` always pause for a human merge decision; `red` always escalates to a human. `human_review`/`default_human_review` and `autonomy` both gate the *merge* step only — either one requiring a pause is enough to pause it — and neither one controls whether the review step itself runs.

### `executor_steps`

Override the executor for individual pipeline steps. Supported V1.5 routing keys are
`implement` and `review`. The review step also accepts `human`.

```yaml
executor: claude          # global default (used when a step is not listed below)
executor_steps:
  implement: codex        # use Codex for implementation
  review: claude          # use claude for review
```

When `executor_steps` is absent (the default), all steps inherit the global `executor`.
A ticket-level `executor:` frontmatter value overrides the `implement` step only.
Review routing is controlled by ticket-level `reviewer:`, top-level `reviewer:`,
then `executor_steps.review`.

To pin the model for a ticket's routed review, run `lanegate route TICK-NNN --model MODEL`.
This stores `review_model_pin:` in ticket frontmatter and applies only to review dispatch.
`review_model:`, by contrast, records the model that actually performed a completed review;
it is review attribution, not a routing setting.

Multi-executor routing only chooses which CLI LaneGate invokes for each step. It is
not OS or container sandboxing, and it does not isolate tools from the checkout.

### Named driver instances (`drivers:`)

`drivers:` defines named *invocation* configs, the model, binary, flags, declared provider, and (for `ollama`) `base_url` a driver name resolves to. Reference a driver name from `steps:`/`executor_steps`, from a ticket's own `executor:`/`reviewer:` frontmatter, or as the top-level `executor:` default:

```yaml
drivers:
  claude-main:
    type: claude-process
    model: claude-sonnet-4-6
  remote-ollama:
    type: ollama
    model: qwen3-coder:30b-a3b-q4_K_M
    base_url: http://localhost:11435
  local-aider:
    type: aider
    provider: ollama

executor: claude-main
```

| Field | Required | Description |
| --- | --- | --- |
| `type` | yes | Underlying executor driver, see `executor` above for the valid list. |
| `model` | no | Model name/tag passed to the driver. |
| `base_url` | no | HTTP endpoint for the `ollama` driver's REST API. Defaults to `http://localhost:11434`. Only read from `drivers:` entries, see the note below. |
| `bin` | no | Override the binary/command name. |
| `flags` | no | Extra CLI flags prepended before the prompt. |
| `provider` | no | Explicit backing route, such as `ollama`. This enables provider-specific safeguards and warnings. Set it to the actual route type, not a model-name guess. |

**`drivers:` vs. `executors:`.** These are two separate blocks that happen to support the same kind of "named instance" pattern, and they are not merged automatically:

- `drivers:` (this section) controls *how* a named instance is invoked, model, base_url, bin, flags.
- `executors:` (next section) controls *concurrency and pool membership*, `max_parallel`, `api_key_env`, pool assignment.

If you give an `ollama` instance a name only under `executors:` (e.g. for pool membership) without a matching same-named entry under `drivers:`, its `base_url`/`model` will **not** reach dispatch, `base_url` is silently ignored and the instance falls back to the default `http://localhost:11434` with the default model. To use a non-default `base_url`, e.g. a remote box reached through an SSH tunnel, define the same name under both blocks:

```yaml
drivers:
  remote-ollama:
    type: ollama
    model: qwen3-coder:30b-a3b-q4_K_M
    base_url: http://localhost:11435

executors:
  remote-ollama:
    type: ollama
    max_parallel: 1   # caps against the box's VRAM, not a rate limit
```

### Named executor instances (`executors:`)

If you have more than one account of the same executor type, for example two
Claude Pro subscriptions, each with its own session/weekly usage limit, define
a **named instance** for each one under `executors:` and route tickets to a
specific instance instead of picking one account and hitting its limit before
the other helps.

```yaml
executors:
  claude-1:
    type: claude-process
    api_key_env: ANTHROPIC_API_KEY_1   # env var holding this account's key
    max_parallel: 2
  claude-2:
    type: claude-process
    api_key_env: ANTHROPIC_API_KEY_2
    max_parallel: 2
  local-ollama:
    type: ollama          # text-only (analyze); implement/review/fix now raise a config error
    max_parallel: 4
  local-aider:
    type: aider
    provider: ollama
    context_window_tokens: 32768
```

Each entry under `executors:` is a named instance keyed by whatever name you
choose (`claude-1`, `claude-2`, `local-ollama`, ...). Fields:

| Field | Required | Description |
| --- | --- | --- |
| `type` | yes | The underlying executor driver, one of `claude`, `claude-subagent`, `claude-process`, `aider`, `openhands`, `codex`, `ollama`, `gemini` (deprecated), `agy`, `continue`. |
| `model` | no | Blanket model override for every step dispatched to this instance (implement/review/fix/analyze). Use `models:` (below) instead when different steps need different models. |
| `api_key_env` | no | Name of the environment variable (already set in your shell) that holds this instance's API key. LaneGate injects its *value* into the subprocess environment under the variable the driver itself expects (e.g. `ANTHROPIC_API_KEY` for `claude`/`claude-process`), the variable name is never written to logs. When absent, the driver's default env var is used unchanged. |
| `max_parallel` | no | Per-instance concurrency cap, same precedence rules as the legacy per-type `executors:` block below. |
| `provider` | no | Explicit backing route for provider-specific safeguards. For an Aider route backed by Ollama, use `provider: ollama`. |
| `context_window_tokens` | no | Usable input-context budget for an Aider route. Required to enable its context preflight. See [Context window tokens](executor-capabilities.md#context-window-tokens). |
| `print_timeout_seconds` | no | Timeout in seconds for executor print/run execution commands (overrides root `print_timeout_seconds`). |
| `neutralize_touches` | no | Boolean (default `false`). When `true`, suppresses eager positional arguments for touched files during Aider initialization. |

**Which types support `api_key_env`.** LaneGate only knows the target environment variable to inject into for `claude`, `claude-subagent`, `claude-process` (`ANTHROPIC_API_KEY`) and `codex` (`OPENAI_API_KEY`). `gemini`, `agy`, and `continue` do not currently have a target env var mapping, setting `api_key_env` on one of these instances raises a config error at dispatch time rather than silently doing nothing, since a silent no-op would dispatch under whatever key is already in your shell with no indication anything went wrong. If you need per-account key isolation for `gemini`/`agy`/`continue`, use that driver's own account-switching mechanism outside LaneGate for now.

LaneGate also raises a config error (rather than silently falling back to the parent shell's environment) if `api_key_env` is set but the named environment variable is not actually set, the whole point of `api_key_env` is per-account key isolation, so a value that can't be resolved should stop dispatch, not dispatch under the wrong account.

Route a ticket to a specific instance the same way you'd route it to a bare
executor type, via ticket-level `executor:` frontmatter, `executor_steps`, or
the global `executor:` default:

```yaml
# .lanegate.yml
executor: claude-1   # global default now points at a specific instance
```

```yaml
# TICK-123.md frontmatter
executor: claude-2
```

**Backward compatibility.** A bare executor type (e.g. `executor:
claude-process`, with no matching key under `executors:`) keeps working
exactly as before, LaneGate resolves it to the first configured named instance
of that type, or dispatches the type directly (no per-instance overrides) when
no named instance exists at all:

```yaml
# No executors: block at all — resolves and dispatches exactly as pre-TICK-088.
executor: claude-process
```

```yaml
# executors: has named claude-process instances, but a ticket/global default
# still uses the bare type — resolves to the first one defined (claude-1).
executor: claude-process
executors:
  claude-1:
    type: claude-process
    api_key_env: ANTHROPIC_API_KEY_1
  claude-2:
    type: claude-process
    api_key_env: ANTHROPIC_API_KEY_2
```

`lanegate board` shows the resolved instance name (e.g. `claude-1`) rather than
the bare type for in-progress tickets, falling back to the bare type when no
named instance is configured.

> **Note on the older per-type `executors:` block.** Before this ticket,
> `executors:` entries were keyed directly by executor *type* (e.g.
> `executors: {aider: {max_parallel: 3}}`) to override `max_parallel`,
> `models`, `bin`, `flags`, `provider`, or `context_window_tokens` for that type globally. That form still works
> unchanged: an entry with no `type` field is treated as this legacy per-type
> override, not a named instance. Add a `type` field to turn an entry into a
> named instance.

For a legacy Aider route, declare the backing provider and budget together:

```yaml
executors:
  aider:
    provider: ollama
    context_window_tokens: 32768
```

### Executor pools (`pools:`)

Once you have more than one named instance (above), `pools:` distributes
tickets across them automatically instead of you routing each ticket by
hand:

```yaml
pools:
  default:
    executors: [claude-1, claude-2]
    strategy: least-loaded   # or round-robin
default_pool: default        # used by `lanegate run` when --pool isn't passed
```

```bash
lanegate run --pool default   # explicit
lanegate run                  # uses default_pool if set
```

#### Ad-hoc executor selection (`--executors`)

As a lightweight alternative to pre-declaring named pool entries for every combination, you can pass an ad-hoc list of executors directly on the CLI:

```bash
lanegate run --executors agy,claude-b
```

This selects `least-loaded` across exactly those named `executors:` entries for one run without declaring a `pools:` block entry for the combination. Note that `--pool` and `--executors` are mutually exclusive (passing both raises a configuration error).

- `least-loaded` (default) picks the instance with the fewest tickets
  currently running under it.
- `round-robin` distributes sequentially regardless of load.
- An instance that currently has a ticket hibernated on a rate limit is
  skipped in favor of a healthy one in the same pool, so one account running
  out mid-run doesn't stall tickets that another account could still pick up.
- A ticket's own explicit `executor:`/`reviewer:` override always wins, pools
  only fill in a choice for tickets that don't already have one.

Pool selection is a dispatch-time-only decision: it is **not** written onto
the ticket's own frontmatter (unlike a manual `executor:` override), so it
doesn't survive being reloaded from disk. Practically this means: the
selected instance is authoritative for that one run's dispatch and appears in
that run's log output, but `lanegate board`'s per-ticket executor column reflects
the ticket's static config-resolved default while a pool-dispatched ticket is
in progress, not the pool's live pick, a known follow-up, not yet closed.

#### Review independence (TICK-345)

Review is dispatched through the same pool seam as implement and analyze
(`resolve_pool_executor`), and it always excludes the instance that
implemented the ticket, least-loaded can never hand a self-review back to
the account that produced the diff. The implementer is identified from a
pinned `executor:` on the ticket, falling back to `implement_session_executor`
(the pool instance the implement step actually ran on, recorded for session
resume).

A too-small pool is degraded through, never blocked on:

1. **`independent`**, a different pool instance reviewed. The common case
   whenever the pool has more than one healthy candidate.
2. **`different-model`**, no other instance exists, but the review step
   resolves to a model different from the one the implementer used (via
   `models.review` / a named driver's own `model`). Still a genuinely
   different reviewer, and the only independence available to a
   single-account setup.
3. **`self`**, same instance, same model. No alternative existed anywhere.
   Review still runs rather than stalling the ticket, and a warning naming
   the pool is printed to explain why.
4. **`undetermined`**, the ticket was implemented manually (`implement_mode:
   manual`, TICK-373) and no implementer identity was recorded (no `executor:`
   pin or `implement_session_executor` from a dispatch run). Independence
   cannot be verified either way, so it is recorded as undetermined rather than
   assumed independent, even when a `reviewer:` pin is set.

The independence ladder is consulted by default (TICK-381) even when no explicit
review route is configured. When both steps fall through to the shared global executor
default, LaneGate attempts an independent pool instance (rung 1) or a different model
(rung 2) first, falling back to combined self-review (rung 3) only when no alternative
exists.

Every reviewed ticket records which rung applied in a `review_independence:
independent|different-model|self|undetermined` field, so a self-review is always
distinguishable from an independent one instead of the two being
byte-identical in the ticket frontmatter. Combined-mode self-reviews also record
`review_independence: self`. An explicit per-ticket `reviewer:`
override still wins outright regardless of this ladder, that is a human
decision and is never second-guessed, but if it happens to name the same
executor that implemented the ticket, a warning is printed and
`review_independence` is recorded as `self`.
`undetermined` is the one rung that is not a degradation of pool selection but
a statement that the implementer is simply unknown.

#### `review_fallback`

Controls what happens when the ladder above can't find an independent
instance (rung 1) or a different model (rung 2) — i.e. what to do about
rung 3. Three values:

| Value | Behavior |
|---|---|
| `needs_review` (default) | Never self-review. The ticket is marked `needs_review` for a human to resolve instead. |
| `different_model` | Same as `needs_review`, except it also tries rung 2 (a different model on the same instance) before giving up. |
| `same_model` | Permissive: falls all the way through to rung 3 (`self`) and runs the self-review anyway, with a warning. |

Because the default is `needs_review`, a genuinely single-account,
single-model install does **not** quietly run in combined self-review mode —
every review escalates to a human `needs_review` state instead. Combined
mode's self-review path only actually runs when `review_fallback:
same_model` is set (or an explicit same-executor `reviewer:`/`steps.review`
pin is configured).

### `profile`

`profile: strict` (default: `default`) bundles the safer end of the knobs
above into one name, and closes the `review_fallback` door to self-review —
but not every door to self-review:

- `acceptance_contract_mode` defaults to `blocker` instead of `advisory`
  (still overridable explicitly).
- `review_fallback: same_model` — the fallback that silently self-reviews —
  is rejected at config-load time with a `ConfigError`. `needs_review` and
  `different_model` both already refuse to self-review, so both remain legal
  under `profile: strict`.
- This does **not** close the separate, explicit route: a `reviewer:` (or
  `steps.review`) pin that names the same instance as `executor:` still
  self-reviews under `profile: strict`, emitting a `warnings.warn` rather
  than being rejected. `profile: strict` only forbids the implicit
  `same_model` fallback, not a deliberate same-executor pin.

```yaml
profile: strict
```

`profile: default` (or omitting the key) preserves every default described
above unchanged.

### Complexity-based routing (`routing:`)

Once `pools:` exists, `routing:` decides *which* pool a given ticket goes to
based on how complex it is, instead of every ticket competing for the same
pool. This is meant to pair with the `analyze` step: a ticket that only
touches one small utility function doesn't need a full-price model, a local
Ollama or Aider instance can implement it faster and for free once `analyze`
has already done the hard part (understanding scope and risk).

```yaml
routing:
  - when:
      complexity_max: 2        # analyze score <= 2
      touches_max: 3            # <= 3 files touched
    executor_pool: local        # route to the local (Ollama/Aider) pool
  - when:
      complexity_min: 3
    executor_pool: default      # route to the Claude pool
default_pool: default           # fallback when no rule matches
```

Rules are evaluated **top to bottom, the first whose `when` filters all
match wins.** Available `when` fields (same filter vocabulary as ticket
groups elsewhere in LaneGate):

| Field | Matches when |
|---|---|
| `complexity_min` / `complexity_max` | ticket's analyze-assigned `complexity` score is within range (inclusive) |
| `touches_min` / `touches_max` | number of files in the ticket's `touches` list is within range (inclusive) |
| `priority_min` / `priority_max` | ticket's `priority` is within range (inclusive) |
| `label` | `label` appears in the ticket's `labels` list |

A ticket that's missing a field a rule filters on (most commonly:
`complexity` on a ticket that hasn't been through `lanegate analyze` yet) never
matches that rule, it simply falls through to the next rule, and eventually
to top-level `default_pool` if nothing matches. This is not an error. Running
`lanegate analyze` before orchestrating is only *recommended*, not required,
when routing rules are configured. Every `executor_pool` named in `routing:`
must be defined under `pools:`, or config loading fails with a `ConfigError`.

`lanegate next` / `lanegate run`'s batch selection (`next_batch()`)
resolves and attaches the routed pool to each selected ticket so pool
dispatch can pick an executor instance from it. `lanegate board` shows the
resolved pool as a `pool:<name>` flag (or `pool:unrouted` when no rule
matched and no `default_pool` is set) whenever `routing:` or `default_pool`
is configured, it stays silent for projects that don't use routing at all.

To dry-run what a specific ticket would resolve to and why, without
dispatching anything:

```bash
lanegate route TICK-042
```

```
TICK-042 — Add rate limit headers
  complexity=1  touches=2  priority=2
  routed_pool: local
  reason: routing[0] matched (complexity<=2, touches<=3)
```

**Worked example, three tiers (local / Sonnet / Opus):**

```yaml
executors:
  local-1: { type: aider, provider: ollama, model: ollama_chat/qwen2.5-coder:14b }
  sonnet-1: { type: claude-process, model: claude-sonnet-5 }
  opus-1: { type: claude-process, model: claude-opus-5 }

pools:
  local:
    executors: [local-1]
  sonnet:
    executors: [sonnet-1]
  opus:
    executors: [opus-1]

routing:
  - when: {complexity_max: 2, touches_max: 3}
    executor_pool: local     # trivial, narrowly-scoped tickets
  - when: {complexity_max: 5}
    executor_pool: sonnet    # everyday feature/bugfix work
  - when: {complexity_min: 6}
    executor_pool: opus      # architecturally risky or wide-reaching tickets

default_pool: sonnet          # unanalyzed tickets land on the safe middle tier
```

---

## Combined vs split mode

LaneGate uses one of two execution modes for each ticket, chosen automatically based on whether the `implement` and `review` steps resolve to the same executor.

### Combined mode (default for single-account/single-model setups)

When both steps resolve to the same executor and the review independence ladder degrades to `self` (or when an explicit same-executor route is pinned), LaneGate runs a **single subprocess** that receives a merged implement+review prompt. The executor is responsible for calling `lanegate complete` and `lanegate review --verdict <v>` internally after finishing its work.

The orchestrator then skips the separate `cmd_complete` and review-agent subprocess calls, since the combined executor has already handled them.

**When no explicit review route is configured**, LaneGate checks the review independence ladder (TICK-381) before defaulting to combined mode. An explicit same-executor pin (`reviewer:`, `steps.review.driver`, or `executor_steps.review`) bypasses the ladder and runs combined mode directly — but only for an executor type capable of it: combined mode's prompt instructs the executor to shell out and run the `lanegate complete`/`lanegate review --verdict` commands below itself, which requires real shell/tool-execution capability (`claude`/`claude-subagent`/`claude-process`, `codex`, `agy`). A pure code-editing tool like `aider` has no such capability — an explicit same-executor pin for one of those instead falls through to split mode below, still honoring the pin, dispatched as a separate cold subprocess rather than one combined call.

Combined mode is, by construction, one continuous session across both steps — there's no separate review dispatch to be independent of the implementer's reasoning trail, since it's the same conversation throughout. Split mode's same-executor review dispatch (the ladder's `self` rung, or a same-executor pin on a combined-incapable executor per above) is different: it's a genuinely separate subprocess, and **that dispatch runs as a fresh, independent session by default, not a continuation of the implement session**, even when it's literally the same model. See `session_chaining.chain_review` below (default `false`) for the one opt-in exception, and its cost/independence tradeoff.

**Same-executor combined mode example**

```yaml
executor: claude
executor_steps:
  implement: claude
  review: claude
```

Since both steps resolve to `claude`, LaneGate uses one combined executor process.

**Combined prompt structure:**

```
[implement prompt]

After your implementation is complete and committed:

1. Review your own diff (`git diff main..HEAD`).
2. If the implementation meets the close criteria, run:
       lanegate complete && lanegate review --verdict approved --summary "<one line>"
3. If changes are needed, run:
       lanegate complete && lanegate review --verdict changes_requested --summary "<reason>"

Do not exit until you have run one of the above commands.
```

### Split mode

When `implement` and `review` resolve to **different** executors, LaneGate runs two separate subprocesses:

1. The **implement executor** receives the standard implement prompt and runs normally.
2. After the implement executor exits successfully, the **review executor** is spawned as a separate review agent (current `run_review_agent` behavior, now using `resolve_executor(cfg, "review")` instead of a hardcoded executor name).

**Example: Codex implements, Claude reviews**

```yaml
executor: claude
executor_steps:
  implement: codex
  review: claude
```

With this config, `implement` resolves to `codex` and `review` resolves to `claude`.
Since they differ, split mode is used.

To pause for a person instead of spawning a review executor, route review to
`human` and run orchestration with per-ticket human review:

```yaml
executor: claude
executor_steps:
  implement: codex
reviewer: human
```

`reviewer: human` overrides `executor_steps.review` for the review step. The
implement executor still comes from ticket-level `executor:`, then
`executor_steps.implement`, then the global `executor`.

---

## Design rationale

Combined mode was introduced to reduce round-trips and subprocess overhead when both steps can share the same LLM session. A single long-running agent can implement, self-review, and record its verdict without the orchestrator having to spin up a second process. This is especially valuable when the executor is an interactive agent (e.g. `claude`) where session state and context window continuity matter.

Split mode is preserved for heterogeneous setups, for example, running a fast code-generation tool for implementation and a higher-quality model or separate review pipeline for verification.

The `_is_combined_mode(cfg, ticket)` predicate in `lanegate/orchestrate/autofix.py` drives this decision. The run loop checks it once per ticket, immediately before invoking the executor.

---

## Auto-fix attempts

```yaml
max_auto_fix_attempts: 1   # default
```

When `lanegate review` returns `changes_requested`, LaneGate runs up to this many **fix → drift-check → re-review** cycles per continuation pass. The default of `1` means each pass has one re-review.

This cycle always runs regardless of the ticket's `autonomy`. `autonomy` only decides what happens to a successful result: `full`, `green`, and `yellow` merge unattended on re-review approval, while `supervised` (the default) and `manual` land the ticket at `in_review` awaiting an explicit human verdict. `red` always escalates to a human.

Three conditions stop the current cycle early. A **drift-check failure** (the fix diverged from the ticket's intent) is fail-closed and immediate: it does not consume a remaining attempt, is never automatically resumed, and requires a human. A `changes_requested` verdict carrying **no findings** is treated as a harness error rather than a rejection, so no fix is attempted. An **exhausted budget** leaves the ticket eligible for a later bounded continuation pass.

On escalation the ticket stays at `code_complete` / `review_verdict: changes_requested`, with one `## Auto-Fix Attempt N` section per attempt in the body. A later `lanegate run` treats that pair as a resumable auto-fix continuation, so it does not permanently lock overlapping open tickets. Raising this value trades executor spend for fewer scheduler passes. See [Ticket Lifecycle](lifecycle.md#the-auto-fix-sub-loop) for how this fits the wider state machine.

### `human_escalation`

Configure risk signals that force a red-lane human escalation, regardless of a
ticket's resolved autonomy:

```yaml
human_escalation:
  credentials: true       # default: true
  security_actions: true  # default: true
  retry_limit: 3          # default: 3; positive integer
```

`credentials` and `security_actions` are booleans. When enabled, detected
external credentials/secrets or security-sensitive/irreversible operations
require human review. `retry_limit` caps automatic fix attempts; LaneGate uses
the lower of this value and `max_auto_fix_attempts`. Omit the block to use all
defaults, or set only the keys your project needs to override.

---

## Safeguards

Safeguards are deterministic commands that run before LaneGate advances lifecycle
state. Use them for tests, lint, type checks, or local scripts that must pass
before code is marked complete, merged, or closed after merge.

```yaml
safeguards:
  pre_complete:
    - pytest
  pre_merge:
    - pytest
    - scripts/pre-deploy-check.sh
  pre_merge_worktree: true  # optional: run pre_merge before git merge (default)
  post_merge:
    - pytest
  timeout_s: 600         # optional: kill hanging guards after N seconds
  retry_on_failure: 0    # optional: retry failed guards up to N times
```

`pre_complete` runs before `lanegate complete` advances a ticket to
`code_complete`. `pre_merge` runs before `lanegate merge` merges the ticket branch
back to `main`. `post_merge` runs from the merged control checkout during
`lanegate validate`, after the branch has landed on the primary branch.

### `pre_merge` re-verification against the actual merge result

`pre_merge` guards always run against the real merge commit in the primary
checkout after `git merge` succeeds. This is what catches two
independently-developed tickets that are each individually correct but break a
shared assumption once combined.

By default, the same guard list also runs in the ticket's isolated worktree
before `git merge`. Set `safeguards.pre_merge_worktree: false` when an unchanged
ticket branch has already passed an equivalent `pre_complete` check and the
post-merge verification is the only additional check you need. This setting
skips only the worktree run; it never disables the verification against merged
`main`.

If the second run fails, `lanegate merge` reports it as a distinct failure (not
a plain `pre_merge` failure, the merge itself succeeded, but broke `main`),
runs `git reset --hard` back to the commit `main` was on before the merge, and
routes the ticket to `needs_review` with a `## Needs Review Reason` section
explaining what happened, instead of leaving a broken commit on `main`.

If a project has no `pre_merge` guards configured at all, this step is a
no-op, there is nothing to re-run. Anyone merging a batch of
independently-developed tickets in one sitting (draining a
`needs_review`/`code_complete` backlog by hand, for example) should still run
the full test suite on `main` themselves once the batch is done, as a final
check that this per-merge re-verification does not replace: it only re-runs
the `pre_merge` list after *each* merge, not the fuller suite a project may
reserve for CI.

Supported guard commands include:

- `pytest` or `pytest <args>`, resolved to `python -m pytest ...`
- `npm test` or `npm run <script>`
- `cargo test`
- `go test`
- `make` or `make <target>`
- executable scripts such as `scripts/run-tests.sh`

A non-zero exit blocks the transition. Per-ticket `safeguards` can override the
project-level commands when one ticket needs a narrower or broader check. When
effective `post_merge` guards are configured, `lanegate done` requires the ticket
to pass `lanegate validate` first. Without `post_merge`, `merged → done` remains
available for compatibility.

### Timeout and flaky-test retry support

By default, guards have no timeout and are run only once. Add two optional
config keys to handle hanging tests and flaky suite transients:

**`safeguards.timeout_s`** (integer, default: no timeout)

Kill a guard subprocess if it does not complete within N seconds. When a
timeout occurs, the guard fails with a distinct "timed out" message in stderr,
and the ticket is marked `needs_review` (no retry, timeout is fatal). This
protects against deadlocked integration tests or tests that hang waiting for a
resource that will never become available.

**`safeguards.retry_on_failure`** (integer, default: `0`)

Automatically re-run a failed guard (nonzero exit) up to N times before
propagating the failure to the ticket status. The final attempt's result is
what matters, if any re-run succeeds, the guard is marked as passing and the
ticket continues. If all attempts fail, the ticket is marked `needs_review`.
Timeouts are never retried (timeout is a hard failure, not a transient
condition). This is intended for projects with known-flaky test suites where
intermittent CI infrastructure hiccups (e.g. a resource briefly unavailable)
should not block a ticket that would pass on a second run.

Example: a project with a flaky integration test that passes ~90% of the time:

```yaml
safeguards:
  pre_merge:
    - pytest
  timeout_s: 600          # kill if tests hang for 10 minutes
  retry_on_failure: 2     # allow 2 retries (up to 3 attempts total)
```

When both `timeout_s` and `retry_on_failure` are absent (the default), guard
behavior is unchanged: no timeout, run once, fail on any nonzero exit.

### Multi-Ecosystem Safeguard Recipes (The Two-Pillar Rule)

Every robust autonomous agent project should configure safeguards with **two pillars**:
1. **Static Analysis & Type Contracts** (e.g. `tsc`, `mypy`, `golangci-lint`, `clippy`, `mvn compile`) to prevent interface drift.
2. **Behavioral Test Suites** (e.g. `pytest`, `jest`, `cargo test`, `mvn test`) to prevent logic regressions.

#### 1. TypeScript / React / React Native / Angular
```yaml
safeguards:
  pre_complete:
    - npm run typecheck   # or: npx tsc --noEmit
    - npm run lint
    - npm test -- --watchAll=false
  pre_merge:
    - npm run build       # verifies production bundling & assets
```

#### 2. Python
```yaml
safeguards:
  pre_complete:
    - mypy <package_name>
    - pytest -q
  pre_merge:
    - mypy <package_name>
    - pytest -q
```

#### 3. Java / Kotlin / AEM (Maven / Gradle)
```yaml
safeguards:
  pre_complete:
    - mvn compile test-compile
    - mvn test -Dtest=*UnitTest
  pre_merge:
    - mvn clean verify    # full packaging + checkstyle/spotbugs
```

#### 4. Monorepos (Turborepo / Nx / Lerna)
```yaml
safeguards:
  pre_complete:
    - turbo run check-types lint test --filter=...[origin/main]
  pre_merge:
    - turbo run build --filter=...[origin/main]
```

#### 5. Go
```yaml
safeguards:
  pre_complete:
    - golangci-lint run
    - go test -race ./...
  pre_merge:
    - go build ./...
```

#### 6. Rust
```yaml
safeguards:
  pre_complete:
    - cargo clippy -- -D warnings
    - cargo test
  pre_merge:
    - cargo check --all-targets
```

---

## Rate limits and auto-resume

When the executor exits with a 429 or its stderr matches rate-limit/quota
phrasing (`rate limit`, `quota exceeded`, `too many requests`, `usage limit`,
`purchase more credits`, `try again at`), the orchestrator hibernates the
current ticket (preserving worktree state and committed work) and halts the
run rather than failing the ticket outright. What happens next depends on
`on_rate_limit`:

```yaml
on_rate_limit: resume   # default

rate_limit_resume:
  initial_backoff_s: 300
  max_backoff_s: 7200
  ceiling_s: 86400
  lock_poll_interval_s: 30
```

- **`resume`** (default), hibernates, then spawns a detached `lanegate
  resume-watch` daemon (`.lanegate/resume-watch.pid`, `.lanegate/resume-watch.log`)
  that waits and re-invokes `lanegate run` itself each time. It exits as
  soon as no rate-limited tickets remain hibernated.
- **`halt`**, prints re-run instructions and stops. You re-run `lanegate
  orchestrate` yourself once you've checked your quota/billing. The hibernated
  ticket is automatically priority-boosted so it's picked up first.

How long it waits: if the executor's output carries a recognizable reset time
(`resets 11:40am (America/Los_Angeles)`, `try again at …`, or an ISO
timestamp), that time plus `reset_buffer_s` is used directly. Otherwise it
falls back to capped exponential backoff, starting at `initial_backoff_s`,
doubling each retry up to `max_backoff_s`. **`max_backoff_s` caps any single
wait on both paths.** A parsed reset time that has already passed by the time
the daemon re-reads it means the window has cleared, so it retries promptly
rather than assuming the same clock time tomorrow.

`ceiling_s` is the total-waiting budget: the daemon gives up after that many
seconds and falls back to manual-resume instructions. It defaults to `86400`
(24 hours). Setting it to `null` polls forever, occasionally what you want
for a weekly usage-window limit that resets only after days, but understand
that it removes the only automatic stop on a retry loop.

Before each retry, resume-watch also checks whether the repo's orchestrator
advisory lock (see `concurrency.py`'s `acquire_orchestrator_lock`) is
currently held by a live process. If it is — for example the original
`lanegate run` invocation is still finishing already-in-flight tickets —
resume-watch polls again after `lock_poll_interval_s` (default `30`) without
advancing the backoff timer or consuming any of the `ceiling_s` give-up
budget, since a lock conflict says nothing about whether the rate limit has
actually cleared. The give-up clock only starts running once the lock is
free and a retry can meaningfully test the rate limit.

`lanegate resume-watch --status` reports whether a watcher is currently running,
and `lanegate resume-watch --stop` kills it.

If `notify.ntfy_topic` is configured (see below), `resume-watch` pushes a
phone notification at each event that matters: the rate limit hitting (auto-resume
starting), giving up at the ceiling (previously silent, logged only), and
resuming successfully. `lanegate resume-watch --history` prints the recorded
sequence for past runs (`hibernated` → `retrying` → `resumed`/`gave_up`,
each with a timestamp), backed by `.lanegate/resume-watch-history.jsonl`.

A blind retry-on-a-timer has a real failure mode worth knowing: if the
underlying issue isn't actually a transient rate limit, a billing problem,
an exhausted monthly quota, or the executor genuinely broken, `resume-watch`
will keep re-invoking `lanegate run` on the same backoff schedule rather
than recognizing the difference. Two mitigations: `ceiling_s` defaults to 24h so it gives
up and notifies instead of retrying forever, and remember that a resumed run
still has to pass the same `pre_complete`/`pre_merge` safeguards and land in
`needs_review` before merge, auto-resume re-queues the ticket into the
normal pipeline, it does not bypass review or quality gates.

---

## Session chaining (`--resume` across pipeline steps)

Each pipeline step normally dispatches as a cold, independent CLI process.
For Claude subprocess executors, a cold call pays a large fixed
per-invocation bootstrap cost (system prompt/tools get cache-*written*, not
just read), measured on this project at ~13K cache-creation tokens / $0.084
for a trivial round trip, vs. ~$0.0089 (9.5x cheaper) when the same session
is resumed instead of restarted. `session_chaining` extends `--resume`
(previously only used for analyze→implement, TICK-188) across the rest of
the pipeline:

```yaml
session_chaining:
  enabled: true
  chain_review: false
  max_session_age_s: 2700
  max_session_tokens: 150000
```

- **`enabled`** (default `true`), master switch for all `--resume` chaining,
  including the pre-existing analyze→implement link.
- **`chain_review`** (default `false`), review normally starts a fresh,
  independent session even when chaining is otherwise enabled. An
  independent check is the whole point of the split-review pipeline. A
  reviewer that inherits the implementer's exact reasoning trail undermines
  that. Set `true` only if the cost saving matters more than that
  independence for your project.
- **`max_session_age_s`** (default `2700`, 45 min), a resume is skipped
  (falls back to a fresh dispatch) once more than this many seconds have
  passed since that session's last recorded step. This exists because the
  cost saving depends on the server's prompt cache still being warm (the CLI
  requests a 1-hour TTL). Past that, a "resumed" call has to resend the
  *entire* accumulated session as a fresh cache-write instead of just the
  fixed bootstrap a cold call pays, which can cost **more** than starting
  fresh. The 45-minute default leaves a safety margin under the 1-hour TTL
  for the check itself to run and the dispatch to complete.
- **`max_session_tokens`** (default `150000`), a resume is also skipped once
  the session has accumulated more than this many tokens (input + cache
  creation + cache read, summed from real per-step data, see
  `lanegate analytics --full`). Cache reads are cheap per-token but not free, and
  they scale with the whole accumulated session, not just the current turn's
  content, there's no CLI-level way to compact/summarize a session in
  headless mode, so once a chain gets this large the only lever is to stop
  extending it rather than shrink it.

Which step resumes which session: `implement` resumes `analyze`'s session,
`fix` resumes `implement`'s (or a prior `fix` pass's, once a second autofix
cycle starts). `drift_check` resumes `fix`'s (it's reviewing what fix just
did, in the same continuity). `review` only resumes `implement`'s session
when `chain_review: true` is set. All of this only applies to Claude
subprocess executor types. Codex has its own `codex exec resume` mechanism,
not yet wired into this same age/size gating.

This is a live cost lever, not a one-shot decision: watch real numbers via
`lanegate analytics --full` (the "Real Step Cost" panel) across a few ticket
batches before adjusting the defaults above.

---

## Phone alerts for stuck runs (notify-watch)

`lanegate notify-watch` is a detached daemon (mirrors `watch.py`/`resume_watch.py`'s
PID-file pattern) that polls local state every `notify.poll_seconds` and pushes
an ntfy.sh notification when an orchestrate run looks stuck:

```yaml
notify:
  ntfy_topic: null      # set this to enable pushes; null = log only, no pushes sent
  poll_seconds: 60
  heartbeat_stale_seconds: 180
```

It checks three things, using the same local files `lanegate run` already
writes, so it works the same regardless of which executor (Claude, Codex,
aider, Ollama) is running a given ticket, and regardless of whether
`orchestrate` was started from a terminal, cron, or an MCP `orchestrate()`
call:

- **Process died**, `.lanegate/active-orchestrate.json` says a ticket is
  running, but the orchestrator lock's PID is dead.
- **Executor wedged**, orchestrate is alive, but no heartbeat update in over
  `heartbeat_stale_seconds`.
- **Loop halted with work waiting**, no orchestrate process running at all,
  and tickets sit in `needs_review` / `blocked` / `failed`, or `hibernated`
  for a reason other than a rate limit currently being retried (a rate-limit
  hibernation is only exempt while a `resume-watch` process is actually alive
  for the repo, a crashed or gave-up `resume-watch` still gets flagged).

Each new problem produces exactly one push (deduped against the last-reported
state), plus a "back to normal" push on recovery. It does **not** cover a
single ticket waiting on GitHub PR review (that's `watch.py`), only that
`orchestrate` itself is making progress.

Setup:

```bash
lanegate notify-watch --test        # send one test push, verify your phone gets it
lanegate notify-watch --background  # spawn detached; survives this terminal closing (works on Windows too)
lanegate notify-watch --status      # is a watcher currently running, and its PID
lanegate notify-watch --stop        # kill it
```

`--background` covers "keep running after I close this terminal" on any
platform. It does not survive a reboot/logout on its own — for that, see
systemd below (Linux/macOS) or Task Scheduler (Windows).

**Privacy note:** the public `ntfy.sh` instance is unauthenticated pub/sub by
topic name, anyone who knows/guesses your `ntfy_topic` can read or post to
it. Use a long random topic (not a guessable word), and treat it like a
shared secret. Self-hosting ntfy is the stronger option if that matters to
you.

Because the topic name is credential-like, keep it out of `.lanegate.yml` if
that file is git-tracked: set the `LANEGATE_NTFY_TOPIC` environment variable
instead (e.g. in your shell profile), it overrides `notify.ntfy_topic`
whenever set, so the file itself can just say `ntfy_topic: null`.

**Surviving reboots/logouts:** run it under a systemd `--user` service so it
starts on login and restarts on failure:

```ini
# ~/.config/systemd/user/lanegate-notify-watch.service
[Unit]
Description=lanegate notify-watch (phone alert when orchestrate looks stuck)

[Service]
WorkingDirectory=/path/to/your/repo
ExecStart=/path/to/lanegate notify-watch
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now lanegate-notify-watch.service
loginctl enable-linger $USER   # keep it running after you log out
```

On Windows there is no systemd equivalent; use Task Scheduler with a trigger
"At log on" running `lanegate notify-watch --background` (the `--background`
flag itself is what makes the process detach and outlive the launching
terminal/session — Task Scheduler is only needed for restart-on-boot).

---

## Resolution order: which driver/instance actually runs a step

Two layers decide what runs a given pipeline step, and they're easy to mix up
because both can be present in the same config at once.

**Layer 1, `resolve_driver(step, ticket, cfg)`: which named driver/executor
is configured for this step**, checked in this order (first hit wins):

1. `ticket.executor` field (implement step only, per-ticket override)
2. `ticket.reviewer` field (review step only, per-ticket override)
3. `steps.<step>.driver` (the current recommended way to pin a step to a
   named `drivers:` entry, e.g. `codex-implement`)
4. Top-level `reviewer` (review step only. May be `human`)
5. `executor_steps[step]` (older per-step override, pre-dating `steps:`)
6. Global `executor` (top-level default, falls back to `"claude"`)

**Layer 2, `resolve_pool_executor(step, ticket, cfg, repo_root)`: does an
active pool override layer 1's answer?** This is the seam every real
dispatch (implement, analyze, review) actually goes through:

1. If the ticket carries an explicit `executor:`/`reviewer:` override, that
   wins outright, pools never override a ticket's own explicit choice.
2. Otherwise resolve a pool via `resolve_ticket_pool` (a matching `routing:`
   rule, else `default_pool`, else none).
3. **If a pool resolved: it replaces whatever layer 1 computed entirely**,
   including a `steps.<step>.driver` pin. The pool picks an instance from its
   `executors:` list (skipping one currently cooling down on a rate limit,
   preferring one under its own `max_parallel` cap) via `least-loaded` or
   `round-robin`.
4. **If no pool resolved** (no `routing:` match and no `default_pool` set):
   layer 1's answer, e.g. a `steps.<step>.driver` pin, is used as-is, with
   no pool involvement at all.

The practical consequence: `steps:` (or `executor_steps:`) and `pools:` /
`default_pool` are not additive, whichever one layer 2 selects wins
completely for that dispatch. If you want steps pinned to specific named
drivers (e.g. keeping implement and review on distinct models) with **no**
pool failover, leave `default_pool` unset and don't add a `routing:` block.
If you want automatic failover across multiple accounts of the same
executor type, set `default_pool` (or `routing:`), doing so overrides any
`steps.<step>.driver` pin for that step, so don't set both expecting them to
combine. `lanegate route TICK-042` dry-runs this whole resolution for one
ticket without dispatching anything.

## Analytics database environment overrides

LaneGate stores analytics and context-log records in a SQLite database at
`~/.local/share/lanegate/analytics.db` by default. Set either
`LANEGATE_CONTEXT_LOG_DB` or `LANEGATE_ANALYTICS_DB` to an absolute or relative
database path to override that location. These overrides are particularly useful
for test isolation, so tests do not read from or write to the production
analytics database.

`LANEGATE_CONTEXT_LOG_DB` takes precedence when both variables are set.
