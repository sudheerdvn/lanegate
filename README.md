# LaneGate

[![PyPI](https://img.shields.io/pypi/v/lanegate?cacheSeconds=3600)](https://pypi.org/project/lanegate/)
[![CI](https://github.com/sudheerdvn/lanegate/actions/workflows/ci.yml/badge.svg)](https://github.com/sudheerdvn/lanegate/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)
[![Local Ollama](https://img.shields.io/badge/local-Ollama-4b5563)](https://ollama.com/)
[![MCP](https://img.shields.io/badge/MCP-ready-7c3aed)](https://modelcontextprotocol.io/)

**Parallel lanes. Protected gates.**
Every agent gets a lane. Nothing ships without a gate.

LaneGate helps coding agents (Claude, Codex, Ollama, aider, etc.) work on the same repo with less manual juggling. Tickets live as local Markdown files, agents claim them, worktrees keep changes separate, and file-level locks reduce overlapping edits. No SaaS, no subscriptions, no external project state.

LaneGate is not a replacement for coding agents, IDEs, or spec-driven planning tools. Those tools do the planning and implementation work; LaneGate records scoped work as repo-local state and coordinates the execution loop around it.

> **Security note.** V1 provides git-level isolation and diff inspection; it does not sandbox agents at the OS level. Agents run as host processes. See [Security Status](#security-status) and [Known Limitations](#known-limitations) before running it on repositories you care about.

---

## Demo

![LaneGate demo: create tickets, inspect the board, and run orchestrate](docs/assets/demo.gif)

---

## When LaneGate Helps

LaneGate is useful when you already use coding agents and want a small local workflow around them:

- keep agent work as local Markdown tickets
- run each task in its own git worktree
- reduce edit collisions with explicit file-level `touches`
- block out-of-scope or sensitive-file changes from auto-merge
- run configured safeguards before completion or merge
- pause for human review before anything lands on `main`

It is intentionally smaller than a company issue tracker and less ambitious than an IDE. Use those for planning, discussion, and implementation. Use LaneGate when you want repo-local execution state around agent runs.

By default, LaneGate commits ticket specs and review verdicts to git as they change — tickets are a git-native artifact, not local-only state. Executor transcripts, logs, cooldowns, locks, and worktrees stay local under `.lanegate/`. Set `commit_status_changes: false` to opt back into fully zero-footprint local state.

MCP is built in — run `lanegate mcp` to expose LaneGate commands as native tools for any MCP-compatible agent (Claude, Cursor, etc.), no shell commands required.

LaneGate's loopback Python API (`lanegate api`) is built and running today, serving
board, ticket, diff, and orchestration-run state as JSON/SSE. The first local
UI is still planned as a small add-on launched with `lanegate ui`: a bundled
TypeScript frontend over that API for board scanning, ticket detail,
blocked/review triage, diffs, orchestration logs, and read-only settings
preview. The CLI remains the complete fallback for advanced or custom
workflows; the UI is not a SaaS service and does not move project state out of
your checkout.

---

## Concepts in 30 seconds

```
.lanegate/tickets/TICK-007.md   ← source of truth (YAML frontmatter)
        │
        ▼
lanegate next         ← agent asks: what should I work on?
lanegate start TICK-007  ← agent claims it; worktree + branch created
  ... agent works in .lanegate/worktrees/tick-007/ ...
lanegate complete TICK-007
lanegate review TICK-007
lanegate merge TICK-007   ← branch → main, worktree removed
lanegate promote production  ← staged deploy with guard/pre/post hooks
```

Main command groups:

| Group | Commands |
|---|---|
| **Scoping** — record work as tickets and ask an executor to infer `touches` / `close_criteria` | `create`, `analyze` |
| **Execution** — who works on what without colliding | `board`, `next`, `start`, `complete`, `review`, `merge` |
| **Delivery** — how merged code reaches environments | `promote`, `pipeline-status`, `flag` |

---

## Security Status

LaneGate is experimental software for coordinating coding agents on a local repo. Before letting it run without review, read this section.

**What LaneGate does to limit agent scope:**
- Runs each agent in a dedicated git worktree, one per ticket
- Enforces a `touches` list: files committed outside the declared scope route to `needs_review`
- Hard-blocks writes to CI/CD configs, dependency manifests (Python, JS/Node, Rust, Go, Java/Gradle, Ruby, PHP, .NET), and credential-shaped files (`.env`, `*.pem`, `secrets.*`)
- Scans ticket text for prompt injection signals before dispatch
- Optionally runs gitleaks, semgrep/bandit, pip-audit, npm-audit, composer-audit, and bundler-audit on agent-produced diffs
- Runs configured `safeguards` such as tests or scripts before `complete` and `merge`

**What LaneGate does NOT do:**
- MCP is not a sandbox. `lanegate mcp` exposes ticket lifecycle commands as native tools; an agent with access to those tools can claim tickets, create branches, and trigger merges. The MCP server authenticates nothing beyond what the MCP client config enforces.
- Host executors inherit host permissions. Agents (Claude Code, aider, Codex) run as the invoking OS user with full filesystem and network access. There is no bwrap, seccomp, or container wrapping.
- The file-based lock is single-machine, single-checkout. Two separate clones on two machines can both claim the same ticket.
- File-level locks are not semantic dependency analysis. Two tickets can touch different files and still break each other through shared APIs, imports, types, schemas, or runtime behavior. Treat `touches` as a practical coordination boundary, not a proof of correctness.

**Recommendation:** use `--human-review final` when running `lanegate orchestrate` on any repository with production code. This stops after implementation and requires `lanegate merge <id>` to be run manually after you inspect the diff. To make this the safe default without depending on every invocation remembering the flag, set `default_human_review: final` (or `per_ticket`) in `.lanegate.yml` — an explicit `--human-review` flag still overrides it. Note that this is unrelated to a ticket's `autonomy` field, which only governs auto-fix-retry behavior, not the merge gate.

For the full threat model, executor permissions, and safe usage notes, see [SECURITY.md](SECURITY.md) and [docs/security-model.md](docs/security-model.md).

---

## Known Limitations

- **Lock scope is single-machine, single-checkout.** The file-based concurrency lock at `.lanegate/orchestrator.lock` prevents two `lanegate orchestrate` runs on the same checkout from racing, but does not coordinate across separate clones or machines.
- **No OS-level sandbox in V1.** Agents run as child processes with full user permissions — no bwrap, seccomp, or container wrapping — regardless of which Claude Code permission mode is configured (see the next bullet for that separate, application-level choice). LaneGate inspects the git diff after the agent exits; it cannot observe what the agent read or sent over the network during execution.
- **Executor permissions come from the executor runtime, not from LaneGate.** `lanegate init` configures Claude Code with a scoped `--allowedTools` set by default so the agent can run headless without interactive prompts, while tools outside that list stay gated. You can instead configure `flags: ["--dangerously-skip-permissions"]`, which disables Claude Code's per-action approval prompts entirely — a valid choice for setups like a sandboxed CI runner, but means the agent acts on anything without confirmation. See [Security Status](docs/security-model.md#headless-permission-options-for-the-claude-executor) for the full set of options.

---

## Platform support

| Platform | Status |
|---|---|
| Linux | Supported — primary development platform |
| macOS | Supported — CI-verified on every push |
| Windows | Core functionality works; projects using `.sh` executor scripts require WSL |

---

## Install

```bash
pip install lanegate
lanegate init    # scaffold .lanegate/ + .lanegate.yml in your repo
```

`lgt` is installed alongside `lanegate` as a short alias for the same command (e.g. `lgt board`, `lgt next`).

LaneGate is a standalone CLI, not a library you import into a project — [pipx](https://pipx.pypa.io/)
is the recommended way to install it, since it isolates the tool's own dependencies
from whatever Python environment your project uses:

```bash
pipx install lanegate
lanegate init
```

To run from source:

```bash
git clone https://github.com/sudheerdvn/lanegate
cd lanegate
pip install -e ".[dev]"
```

---

## Quick start

For real repositories, configure safeguards before you let an executor run:

```yaml
safeguards:
  pre_complete:
    - pytest
  pre_merge:
    - pytest
  post_merge:
    - pytest
```

```bash
# 1. Create a ticket file
cat > .lanegate/tickets/TICK-001.md << 'EOF'
---
id: TICK-001
title: Add rate limiting to the upload endpoint
status: open
priority: 1
touches:
  - src/upload.py
  - src/middleware.py
parallel_safe: true
autonomy: supervised
close_criteria: POST /upload returns 429 after 100 requests/min per user
---

## Background
A single client can saturate the worker pool with rapid uploads.
EOF

# 2. See the board
lanegate board

# 3. Agent claims and works
lanegate start TICK-001
# ... implement in .lanegate/worktrees/tick-001/ ...
lanegate complete TICK-001
lanegate review TICK-001
lanegate merge TICK-001
lanegate validate TICK-001
lanegate done TICK-001

# 4. Deploy
lanegate promote production
```

---

## Commands

### Board & planning

```bash
lanegate board                    # ticket board + pipeline snapshot
lanegate next                     # recommend next ticket(s) + non-overlapping batch
lanegate pipeline-status          # commits pending at each environment stage

# Machine-readable output (any board/next/pipeline-status/flag list command):
lanegate --json board
lanegate --json next
lanegate --json pipeline-status
lanegate --json flag list
```

### Ticket lifecycle

```bash
# Draft → open
lanegate analyze TICK-007         # fill touches + close_criteria, then open the ticket
lanegate open TICK-007            # flip draft → open without re-running analysis (requires touches already set)

# Execution
lanegate start TICK-007           # claim; creates branch + worktree
lanegate complete TICK-007        # code done → code_complete
lanegate review TICK-007          # submit for review → in_review
lanegate merge TICK-007           # merge to main, delete worktree → merged
lanegate validate TICK-007        # run configured post-merge checks → validated
lanegate done TICK-007            # close ticket → done
```

### Deployment

```bash
lanegate promote staging          # guard → pre-promote → sync → post-promote
lanegate promote production
lanegate pipeline-status          # see what's pending where
```

### Feature flags

```bash
lanegate flag list                        # show all flags (global)
lanegate flag list --env staging          # show flags for 'staging' environment
lanegate flag enable  new_checkout_flow --env staging
lanegate flag disable new_checkout_flow --env production
```

### MCP server

```bash
lanegate mcp    # start stdio MCP server; attach any MCP-compatible client
```

Exposed tools include `board`, `next_ticket`, `pipeline_status`, `flag_list`,
`flag_set`, `repo_status`, `recent_logs`, `continuation_context`, `start`,
`orchestrate`, `complete`, `review`, `merge`, `promote`, `hibernate`,
`needs_review`, `fail`, `reopen`, `validate`, `done`, and `stats`. Agent-facing
action and log tools are bounded: lifecycle output is byte-capped, log excerpts
are line- and byte-capped, and continuation state is reconstructed from durable
repo data instead of chat context.

### Local API / UI preview

`lanegate api` is built: a loopback-only (`127.0.0.1`) JSON/SSE server over board,
ticket, diff, and orchestration-run state. `lanegate ui` (a bundled UI served by
that API) is still planned:

```bash
lanegate api    # built — start the loopback JSON/SSE API for local clients
lanegate ui     # planned — start the API, serve the bundled UI, open a browser
```

Current `lanegate api` surface:

- `GET /api/board`
- `GET /api/tickets`
- `GET /api/tickets/{id}`
- `GET /api/blocked`
- `GET /api/diff/{id}`
- `GET /api/status`
- `GET /api/config`
- `GET /api/pools`
- `PUT /api/pools/{name}/executors` — reorders a pool's executor list
- `GET /api/runs`
- `GET /api/runs/current`
- `GET /api/runs/current/logs`
- `GET /api/runs/current/logs/stream`
- `POST /api/orchestrate/start`
- `POST /api/orchestrate/stop`

Per-ticket lifecycle mutation endpoints (`start`/`complete`/`review`/`merge`) are
still design-only — see
[docs/v2-interface-boundaries.md](docs/v2-interface-boundaries.md).

`lanegate ui` will use this API for common board, ticket, diff, review, and
orchestration operations once built. Advanced or executor-specific workflows
stay available through the CLI instead of being forced into the browser.

### Terminal UI (TUI)

A Go TUI is also available via `lanegate tui`, rendering board, ticket detail,
blocked queue, diff, orchestration-run, and settings screens from a fixture or
the local API. It is mostly read-only, with one exception: the settings
screen can reorder a pool's executor list and persist it via
`PUT /api/pools/{name}/executors`. See
[docs/v2-interface-boundaries.md](docs/v2-interface-boundaries.md#tick-118-go-tui-runtime-spike-result)
for its current scope and limits.

`lanegate tui` is a separate Go binary, not part of the `lanegate` Python package — a
plain `pip install` does not give you `lanegate-tui`. To use it:

- **Build from source** (this repo ships the Go module at `tui/`): `go build -o
  lanegate-tui ./tui/cmd/lanegate-tui`, then either put `lanegate-tui` on your `PATH` or set
  `LANEGATE_TUI_BIN=/path/to/lanegate-tui`.
- **Run without building**, if you have the Go toolchain and a checkout of this repo:
  `lanegate tui` falls back to `go run ./cmd/lanegate-tui` automatically when no
  `lanegate-tui` binary is found on `PATH` or via `LANEGATE_TUI_BIN`.

If neither a binary nor the Go source is available, `lanegate tui` exits with an error
telling you to set `LANEGATE_TUI_BIN` — see
[Troubleshooting](docs/troubleshooting.md#lanegate-tui-fails-go-tui-binary-or-source-not-found).

### Monitoring & auto-resume

Three detached background daemons for runs you're not watching live — each has its own PID/log file under `.lanegate/` and a `--status`/`--stop` pair:

```bash
lanegate watch                  # poll PR review decisions, auto-merge on approval
lanegate resume-watch           # wait out a rate limit on backoff, auto-retry `lanegate orchestrate`
lanegate resume-watch --history # what happened: hibernated -> retrying -> resumed/gave_up, with timestamps
lanegate notify-watch --test    # send a test phone push (ntfy.sh), verify setup
lanegate notify-watch           # push a phone alert when orchestrate looks stuck (dead process, stale
                              # heartbeat, or halted with tickets waiting)
```

See [Rate limits and auto-resume](docs/config-reference.md#rate-limits-and-auto-resume) and [Phone alerts for stuck runs](docs/config-reference.md#phone-alerts-for-stuck-runs-notify-watch) in the config reference for configuration and setup, including running `notify-watch` under systemd so it survives reboots.

### Utilities

```bash
lanegate init             # scaffold .lanegate/ + .lanegate.yml
lanegate install-agent-tools # install Claude commands plus Codex/generic MCP snippets
lanegate install-commands # compatibility alias: copy Claude slash commands only
lanegate gh-sync          # mirror tickets to GitHub Issues (manual visibility sync; read-only view, not bidirectional)
lanegate gh-sync --dry-run  # preview what would be created or updated
```

---

## Configuration

`.lanegate.yml` in your repo root (walk-up discovery — run from any subdirectory):

### Executors

LaneGate dispatches tickets to whichever executor you configure. Each executor CLI has its own flag for non-interactive operation. Pass those flags via `executors.<name>.flags` so the orchestrator can run without waiting for prompts:

```yaml
executor: claude   # default executor for orchestrate

executors:
  claude:
    flags: ["--allowedTools", "Bash,Edit,Write,Read,Glob,Grep"]  # scoped headless default; see Known Limitations
  aider:
    flags: ["--yes-always"]                    # auto-confirm all prompts; skip editor
  codex:
    flags: ["--approval-policy=never"]         # no per-action approval prompt
  ollama:
    models:
      implement: llama3.1
      review: qwen2.5-coder

```

Per-executor `bin` overrides the binary name for a supported executor; `flags` are prepended before the prompt.

For V1.5 multi-executor routing, set per-step executors:

```yaml
executor: claude
executor_steps:
  implement: codex
  review: claude
```

When `implement` and `review` resolve to the same executor, LaneGate uses combined mode. When they differ, LaneGate implements first and then runs the resolved review route. A ticket-level `executor:` overrides implementation only; `reviewer:` controls review routing, including `reviewer: human`. Multi-executor routing is not OS/container sandboxing and does not isolate tools from the checkout.

For a batch-level human gate, run `lanegate orchestrate --human-review final`. For per-ticket human verdicts, set `reviewer: human` and run `lanegate orchestrate --human-review per_ticket`; LaneGate will wait for `lanegate review <ticket> --verdict approved` or a changes-requested verdict before merge.

Use `safeguards` to run deterministic checks before a ticket is marked complete,
merged, or closed after merge:

```yaml
safeguards:
  pre_complete:
    - pytest
  pre_merge:
    - pytest
    - scripts/pre-deploy-check.sh
  post_merge:
    - pytest
```

Supported guard commands include `pytest`, `npm test`, `cargo test`, `go test`, `make ...`, and executable scripts. A failing guard blocks the transition. `post_merge` runs from the merged control checkout during `lanegate validate`; when it is configured, `lanegate done` requires validation first.

`pre_merge` guards also re-run automatically a second time, right after the `git merge` lands, against the merged `main` checkout itself — not just the ticket's isolated worktree. This catches the case where two independently-approved tickets each pass their own worktree's tests but break something once combined. If that second run fails, `lanegate merge` resets `main` back to its pre-merge commit and routes the ticket to `needs_review` instead of leaving a broken commit on `main`. See [config-reference.md](docs/config-reference.md#pre_merge-re-verification-against-the-actual-merge-result) for details. This does not replace running the full suite on `main` yourself after merging a whole backlog in one sitting — it only re-checks after each individual merge.

For fully local/offline runs, use `executor: aider` with a local Ollama model as
aider's backend — this is the validated path, since Aider has a real read/edit/commit
loop:

```yaml
executor: aider
models:
  analyze: llama3.1        # any executor works for analyze — text-only, no file edits
  implement: qwen2.5-coder # passed to aider as --model ollama/qwen2.5-coder
  review: qwen2.5-coder
```

Aider talks to Ollama's own local API; install and pull those models with Ollama first
(locally, or on whatever host aider's model route points at). See
[executor-capabilities.md](docs/executor-capabilities.md#aider) for `context_window_tokens`
and other local-model-specific settings.

`executor: ollama` (`type: ollama`) is a separate, more limited option: LaneGate posts
directly to Ollama's REST API (`{base_url}/api/generate}`) and gets back a single text
completion — Ollama itself has no file-editing or commit capability, so this cannot
complete an `implement`/`review` step (it will produce zero commits and fail). It's
only useful for text-only steps like `analyze`. See
[executor-capabilities.md](docs/executor-capabilities.md#ollama) for the full caveat.

For a rented/remote GPU box (e.g. a cloud instance running Ollama), tunnel its port
instead of exposing it — Ollama's API is unauthenticated:

```bash
ssh -N -L 11435:localhost:11434 <user>@<remote-host>
export OLLAMA_API_BASE=http://localhost:11435   # aider reads this directly; LaneGate has no separate field for it
lanegate orchestrate
```

Use `verification.groups` to tell the implement/review prompts when a UI check is expected and where to find the running app. LaneGate never runs a browser itself — the executor needs its own tooling (a Playwright MCP server, an in-session browser subagent) wired up separately. `groups` is a list because one repo can have more than one UI area, each with its own dev server and URL — separate frontend apps in a monorepo, or e.g. an AEM `ui.frontend` clientlib served by webpack alongside the actual AEM author instance:

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

See [config-reference.md](docs/config-reference.md#visual-verification-for-ui-tickets) for the full behavior.

```yaml
ticket_prefix: TICK
tickets_dir: .lanegate/tickets
worktrees_dir: .lanegate/worktrees
executor: claude

core_files:
  - src/core/auth.py
core_patterns:
  - "src/db/**"

verification:
  groups: []   # see above — populate for projects with a UI to visually verify

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

---

## V1 Coordination Model

**Scope: single git checkout, single machine.**

The `touches` list in each ticket is a pessimistic file-level lock. LaneGate holds
the lock from `in_progress` through `in_review` — releasing only at `merged`.
This reduces edit collisions when multiple agents work in parallel.

```
Agent A: TICK-003  touches: [src/auth.py]    status: in_progress  → LOCKED
Agent B: TICK-007  touches: [src/billing.py] status: in_progress  → LOCKED
Agent C: TICK-009  touches: [src/auth.py]    status: open         → BLOCKED (overlaps A)
```

**What this does NOT prevent:** semantic conflicts. If one ticket changes an exported API and another ticket changes a caller in a different file, both tickets may be touch-disjoint and still incompatible. LaneGate relies on safeguards, static checks, and review to catch integration problems before they land.

It also does not prevent two separate git clones on two machines from claiming the same ticket. `check_local_not_behind_remote` runs on every `start` to reduce this window, but it does not close it entirely — this is a V1 limitation covered in [Known Limitations](#known-limitations).

Five concurrency bugs fixed versus the original orchestrator:

| Bug | Fix |
|---|---|
| Lock released at `code_complete` | Lock held until `merged` |
| TOCTOU on `start` | Re-read ticket from disk immediately before write |
| Merge worktree leaked | Capture worktree path before nulling the field |
| Case mismatch on macOS | Worktree dirs always lowercased |
| Substring dedup in gh-sync | Exact `[TICK-N]` prefix match |

---

## Testing

```bash
python3 -m pytest tests/ -q
```

Most tests are fast unit-style checks and mock git-facing subprocess calls where needed to isolate edge cases. `tests/test_e2e_lifecycle.py` is the real lifecycle integration suite: it creates a temporary git repository with `git init`, makes real commits, creates a real linked worktree, runs the ticket from draft through done, and verifies actual branch, merge, and worktree state. That suite still runs under the default `pytest` invocation; only the model response is stubbed to avoid nondeterminism and external cost.

The release-artifact gate builds a wheel/sdist in a temporary copy, uses a clean
non-editable installation, and exercises the CLI in throwaway repositories:

```bash
python -m pip install build
python ci/smoke_release.py
```

See [the release smoke-gate guide](docs/release-smoke-gate.md) for its seven
checks and the currently expected first-promotion failure in CI.

---

## MCP integration

Add to your MCP client config:

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

The server starts on stdio. An AI agent can then call `board()`, `next_ticket()`,
`repo_status()`, `recent_logs()`, `orchestrate(dry_run=true)`,
`start("TICK-007")`, etc. as native tools — no shell commands required. Run
`lanegate install-agent-tools` to write Claude slash commands and reusable Codex or
generic MCP config snippets into the repo. See
[`docs/agent-tools.md`](docs/agent-tools.md) for the supported agent surfaces
and continuation guarantees.

---

## Roadmap

Planned work:

- Dependency-aware scope hints using AST/import information
- Stronger sandboxing for agent runs
- Executor conformance tests
- Distributed ticket coordination

See [ROADMAP.md](ROADMAP.md) for details.

---

## Docs

- `docs/demo-walkthrough.md` — end-to-end walkthrough: idea → analyze → parallel worktrees → review → merge, using a toy calculator project; includes a dry-run path for users without an executor configured
- `docs/troubleshooting.md` — FAQ and debug steps: executor hangs, stuck tickets, worktree cleanup, lock files, MCP setup, and more
- `SECURITY.md` — security policy, reporting vulnerabilities, and what LaneGate does and does not do
- `docs/security-model.md` — full threat model, trust boundaries, executor permission matrix, MCP trust model, V1 limitations, and safe usage recommendations
- `docs/ARCHITECTURE.md` — built architecture, module map, and design invariants
- `docs/config-reference.md` — supported `.lanegate.yml` keys and defaults
- `docs/executor-capabilities.md` — capability matrix comparing Claude, Codex, Aider, and Ollama across headless support, prompt transport, local model support, auto-commit behavior, and sandbox status
- `docs/v2-interface-boundaries.md` — V1.5 layer boundaries (Python core / local API / UI add-on / optional runner), the `lanegate api` endpoint contract and what's built vs. still design-only, and the Go TUI spike result
- `.lanegate.yml.example` — annotated example configuration
- `docs/migration-vyuha-to-lanegate.md` — upgrading an existing repo that still has a `.vyuha/` directory or `.vyuha.yml` from before the project was renamed
