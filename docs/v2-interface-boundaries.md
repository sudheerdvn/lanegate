# V1.5 Interface Boundaries

This document records the V1.5 architecture decision that should land before the larger V2 backlog starts. Many older V2 tickets were written against the current Python package and name files such as `lanegate/orchestrate.py` or `lanegate/executor.py`. Treat those file names as implementation hints, not as a permanent decision about every future surface.

The decision: LaneGate remains a local-first orchestration control plane. It owns workflow state, ticket scope, locks, review gates, prompt construction, executor selection, audit trails, and delivery policy. Executors such as Claude, Codex, Aider, Ollama, OpenHands, and future tools remain workers. The UI and any runner exist to make that control plane safer and more visible. They do not turn LaneGate into the code-writing worker.

## Layer Model

```text
Python core
  Tickets, config, board state, lifecycle transitions, orchestration decisions,
  lock checks, prompt construction, executor routing, review gates, analytics,
  memory retrieval, MCP, and the existing CLI.

Python local API
  A localhost JSON/SSE surface over core operations. It should call the same
  Python service functions used by the CLI where possible and keep request and
  response shapes stable enough for a UI to depend on.

UI add-on
  A visible board and operator console for ticket details, orchestration runs,
  logs, review findings, diffs, executor status, cooldowns, and memory browsing.

Optional runner/sandbox
  A future boundary for process supervision, process-tree cleanup, sandbox
  policy, network isolation, and executor conformance probes. This layer may be
  Rust when the boundary becomes security-sensitive enough to justify it.
```

The layers should communicate through structured contracts. The CLI remains a first-class automation surface, but the UI should not parse terminal tables, ANSI output, or prose messages as its data model.

Agent-native integrations follow the same boundary. Claude slash commands, Codex, and generic MCP clients can be installed with `lanegate install-agent-tools`. They receive structured MCP calls for board state, lifecycle transitions, dry-run orchestration, short logs, status, and continuation context. These tools must keep hard caps on action/log output and must reconstruct continuation from durable repo state rather than from an agent's chat transcript.

## Python Core Responsibilities

Python remains the default core implementation language because the current control-plane work fits it well:

- Markdown/YAML ticket parsing and validation
- board and dependency graph calculations
- TOCTOU-safe lifecycle transitions
- git subprocess orchestration
- prompt templating and model-specific executor adapters
- MCP integration
- analytics, memory indexing, and report generation
- fast unit-test iteration for coordination logic

The core should keep deterministic coordination in code. LLMs can analyze intent, implement scoped work, and review outputs, but they should not decide whether locks conflict, a merge is safe, or a promotion guard passed.

Do not rewrite the core merely because a future surface is not a terminal. Define stable contracts first, then move only the parts that genuinely need a different runtime.

## Language and Runtime Comparison

A runtime spike was requested comparing TypeScript+Bun, Go, Rust, and Python for likely V2 layers. It was explicitly framed as "a decision aid, not a rewrite ticket." Here is that comparison applied per layer, rather than to LaneGate as a whole:

| Layer | Python | TypeScript+Bun | Go | Rust |
|---|---|---|---|---|
| Core control plane (tickets, locks, lifecycle, orchestration) | Best fit today. The workload is I/O-bound (git, subprocess, YAML/file parsing), the same shape as Ansible, aws-cli, or Mercurial, all production Python tools at this scale. A full rewrite would also throw away the existing test suite. | No I/O-bound advantage over Python for this workload. It would mean rewriting all coordination logic for no clear gain. Not recommended. | Same rewrite cost as TypeScript, no clear benefit for this workload. Not recommended. | Same rewrite cost. Only justified if the core itself becomes a security boundary, which it is not. Not recommended. |
| Local API (JSON/SSE backend) | Straightforward: calls the same service functions the CLI uses, no cross-language boundary. | Bun's fast startup and native TypeScript support are convenient if the client is also TypeScript, but it duplicates logic that already lives in the Python core and reintroduces a cross-language boundary the API exists to avoid. | A small typed HTTP server is easy in Go, but adds a second language for no product reason while a Python API already exists. | Same objection as Go. There's no reason to add Rust here unless the API itself needs to be the security boundary. |
| UI add-on (browser) | Not directly viable. A browser needs a client-side language, so a Python-only path means server-rendered HTML (HTMX/Jinja) rather than a rich client. | Best fit. TypeScript is the browser's native typed language. Bun is a fast dev/build toolchain choice for it, not a browser runtime requirement, since the browser still runs plain JS/TS output either way. | Not applicable in the browser itself. It would only matter if choosing Go for a server that emits the UI, which is not being proposed here. | Not applicable in the browser (aside from niche WASM cases, not needed for this UI). |
| Optional runner/sandbox | Possible, but Python's subprocess/signal handling has known rough edges at the scale of precise process-tree supervision. | Not a good fit. GC pauses and less precise process/signal control work against a supervision boundary. | Reasonable: simple concurrency model, single static binary, solid process/signal handling. A credible alternative to Rust here. | Best fit if the boundary is genuinely security-sensitive: memory safety, a small trusted binary, and precise control without GC pauses during signal handling. |

Net: Python core, TypeScript UI, and a Rust-or-Go runner (Rust preferred when the boundary is security-sensitive, Go a reasonable fallback for pure process supervision) is the shape that fits each layer's actual constraints. None of this implies replacing the Python core. See the "Do not rewrite" note above.

## Local API Responsibilities

The local API is the boundary between the Python control plane and richer user interfaces. It should expose small, explicit JSON endpoints for the operations the UI needs:

```text
GET  /api/board
GET  /api/tickets
GET  /api/tickets/{id}
GET  /api/blocked
GET  /api/diff/{id}
POST /api/tickets/{id}/start
POST /api/tickets/{id}/complete
POST /api/tickets/{id}/review
POST /api/tickets/{id}/merge
POST /api/runs/start
POST /api/runs/stop
GET  /api/runs/current
GET  /api/runs/current/logs/stream
```

The API can shell out to the CLI for rare fallback operations during early development, but core flows should share Python functions with the CLI. The API must return structured status, errors, paths, ticket metadata, and log cursors instead of asking callers to scrape human-oriented command output.

### Local API Design

Recommended first backend: a small Python HTTP server that lives inside the existing package and binds to `127.0.0.1` by default. FastAPI is a reasonable choice once the endpoint set grows because it gives typed request/response models, OpenAPI output, streaming support, and easier frontend integration. The first slice can still keep the domain logic dependency-free by putting reusable service functions under the Python core and having both CLI and API call those functions.

Safe defaults:

- bind to loopback only: `127.0.0.1`
- choose an available local port unless the user passes `--port`
- require an explicit flag for non-loopback binding
- return file paths relative to the repo root unless an absolute path is needed
  to open a local worktree
- do not expose a remote network API, account system, or cloud state in V1.5
- require a per-server local token for every mutating action, including on
  loopback

#### Local API mutation authentication (F26)

At startup, `lanegate api` creates a cryptographically random token and writes
it with owner-only permissions to the gitignored
`.lanegate/api-token-<port>` file. Native local clients that need a mutation
read that file and send its value in the `X-LaneGate-Token` request header.
`POST /api/runs/start`, `POST /api/runs/stop`, their
`/api/orchestrate/*` compatibility aliases, and
`PUT /api/pools/{name}/executors` reject a missing or mismatched token with
`401` before reading or parsing the request body.

The token is intentionally neither returned by a read endpoint nor accepted in
a query string. It is also not a CORS-safelisted header, so an unrelated web
page cannot add it to a no-CORS simple request. This token is local process
authentication, not a remote API or account system.

Initial endpoint contract:

| Endpoint | Purpose | Shape |
|---|---|---|
| `GET /api/board` | Board grouped by status plus pipeline summary | `{statuses, pipeline, hidden_count, filters}` |
| `GET /api/tickets` | Flat ticket list for search/filter | `{tickets: [{id, title, status, priority, milestone, touches, depends_on, branch, worktree}]}` |
| `GET /api/tickets/{id}` | Ticket detail | `{ticket, body, close_criteria, review, files, links}` |
| `GET /api/blocked` | Needs-human-decision queue | `{blocked: [{id, attention_category, attention_summary, findings, branch, diff_cmd}]}` |
| `GET /api/diff/{id}` | Diff for a ticket branch/worktree | `{id, base, branch, files: [{path, status, patch?}]}` |
| `POST /api/tickets/{id}/start` | Claim and create/attach worktree | request `{}`, response `{id, status, branch, worktree}` |
| `POST /api/tickets/{id}/complete` | Move to `code_complete` after scope/safeguard checks | request `{allow_drift?: boolean}`, response `{id, status, warnings}` |
| `POST /api/tickets/{id}/review` | Record review or invoke review step later | request `{verdict, summary?, findings?}`, response `{id, status, review_verdict}` |
| `POST /api/tickets/{id}/merge` | Merge ticket branch when allowed | request `{delete_worktree?: boolean}`, response `{id, status, merge_commit?}` |
| `POST /api/runs/start` | Start one LaneGate run | request `{milestone?, max_parallel?, human_review?}`, response `{run_id, status}` |
| `POST /api/runs/stop` | Request graceful stop/cancel | request `{run_id}`, response `{run_id, status, stop_requested: true}` |
| `GET /api/runs/current` | Current run state | `{run_id, status, started_at, tickets, workers, last_event_id}` |
| `GET /api/runs/current/logs/stream` | Live logs/events | SSE events `{id, type, timestamp, ticket_id?, message, data?}` |

The API should prefer structured domain errors over plain text. A failed `start` response should say whether the cause is `locked_touch`, `missing_ticket`, `wrong_status`, `empty_touches`, `remote_behind`, or `worktree_error`. The UI can then show helpful actions instead of guessing from CLI prose.

### Build Result

A first slice of this design was implemented as `lanegate api`, a loopback-only (`127.0.0.1`) HTTP server in `lanegate/api.py`. It has since grown past that first slice. Shipped as of this writing:

- `GET /api/board`, `GET /api/tickets`, `GET /api/tickets/{id}`,
  `GET /api/blocked`
- `GET /api/diff/{id}`
- `GET /api/status`, `GET /api/v1/analyze/status` (also aliased at
  `/api/analyze/status`)
- `GET /api/config` (also aliased at `/api/settings`): sanitized resolved
  config, repo paths, API metadata
- `GET /api/pools` and `PUT /api/pools/{name}/executors`: read and
  persist-reorder a pool's executor list (the one mutating GET-surface
  endpoint outside the orchestrate/lifecycle actions below)
- `GET /api/runs`, `GET /api/runs/current`, `GET /api/runs/current/logs`,
  `GET /api/runs/current/logs/stream`, `GET /api/runs/{id}`,
  `GET /api/runs/{id}/logs`, `GET /api/runs/{id}/events`
  (plus `GET /api/log` as a legacy alias)
- `POST /api/runs/start`, `POST /api/runs/stop` (with `/api/orchestrate/*` compatibility aliases)

Not yet built from the original endpoint contract above: the per-ticket lifecycle mutators (`POST /api/tickets/{id}/start|complete|review|merge`). Those remain design-only until a follow-up ticket implements them. The CLI (`lanegate start`/`complete`/`review`/`merge`) is still the only way to drive per-ticket lifecycle transitions. `lanegate ui` (a bundled browser client) has not been built, though the API and the Go TUI (`lanegate tui`, see below) both exist.

Core functions to reuse, and what is genuinely new:

`lanegate/mcp.py` already answers most of "which Python functions does the API call". It is a working precedent for exactly this boundary, wrapping the same `cmd_*` functions the CLI uses behind two small adapters (`_capture_json` for read endpoints that already accept `json_output=True`, `_capture_action` for lifecycle commands that print to stdout and raise `SystemExit` on failure). The local HTTP API can follow the same pattern instead of inventing a new one:

- `GET /api/board` → `cmd_board()` (`lanegate/board.py:383`), already
  `json_output`-capable, already wrapped by `mcp.board()`
- `GET /api/tickets` → `cmd_board()`/`cmd_next()` ticket listing, or a thin
  wrapper over `load_all_tickets()` (`lanegate/ticket.py:196`) if a flatter shape
  is needed than the board's grouped-by-status view
- `GET /api/blocked` → `cmd_blocked()` (`lanegate/board.py:577`), already
  `json_output`-capable, not yet wrapped by `mcp.py` but the same shape as
  `board()`/`pipeline_status()`
- `POST /api/tickets/{id}/start|complete|review|merge` → `cmd_start`,
  `cmd_complete`, `cmd_review`, `cmd_merge` (`lanegate/lifecycle/__init__.py:249/686/1235/1390`),
  already wrapped by `mcp.start()/complete()/review()/merge()` via
  `_capture_action`

Two things are **not** already answered by an existing function and need real new work, not just a wrapper:

- **`GET /api/diff/{id}`**: there is no existing diff-generation function
  anywhere in the codebase. This endpoint needs new code that runs `git diff`
  (or `git diff --name-status` plus per-file patches) between the ticket's
  base and branch in its worktree, and decides how large a patch gets
  truncated before being sent to a browser.
- **Orchestration run lifecycle**: `cmd_orchestrate()` (`lanegate/orchestrate/loop.py:1303`)
  is today a single synchronous, blocking call. It acquires the orchestrator
  lock, runs `_drain_loop()` to completion, tees output to a log file, and
  releases the lock. There is no `run_id`, no background/async execution
  model, and no structured event stream. `context_log.py`'s SQLite logging
  records completed agent runs for analytics, not a live run's in-progress
  state. Supporting `/api/runs/start` (return immediately with a
  `run_id`), `/api/runs/current` (poll live state), and log streaming means
  building that run-tracking scaffolding from scratch: running orchestrate in
  a background thread or subprocess, giving it an addressable run id, and
  emitting structured events a poller or SSE stream can read incrementally.
  This is the single largest unknown in this design and should probably be
  its own follow-up ticket rather than an implementation detail of the API.

Run lifecycle:

1. `POST /api/runs/start` creates a run id and starts the same run logic used by the CLI.
2. The run writes structured events alongside human-readable logs.
3. `GET /api/runs/current` returns the current ticket/worker state.
4. `GET /api/runs/current/logs/stream` streams events with SSE so a browser can reconnect using `Last-Event-ID`.
5. `POST /api/runs/stop` requests graceful stop first. Hard kill belongs to the optional runner/sandbox layer once that exists.

## UI Add-on Responsibilities

The UI should start as an add-on over the local API, not as a replacement for the CLI. Its job is to make the workflows people already perform easier to inspect and operate:

- board scanning by status, priority, milestone, dependency, touches, branch, executor, and flags
- ticket detail views for body, close criteria, review findings, and actions
- orchestration run views with current workers and streaming logs
- categorized needs-human-decision queue (escalated, rejected, failed, stuck, awaiting merge), with its count surfaced in shared TUI chrome from Board rows
- diff views for in-review tickets
- executor health, quota/cooldown state, and active process visibility
- memory and prompt/debug artifacts that are hard to inspect from a terminal

A terminal-native Go TUI can share this same Python JSON/SSE boundary as an operator console. The concrete implementation plan, fixture map, launch model, and non-goals are documented in [Go TUI Implementation Plan](tui-plan.md).

### Go TUI Runtime Spike Result

The Go TUI boundary was validated with a small read-only prototype under `lanegate/tui_spike/` and Python command wiring through `lanegate tui`. The spike renders the existing `lanegate board --json` style payload from a fixture, stdin, or a loopback API response. It does not parse terminal tables, ANSI output, or prose CLI text, and it does not own ticket lifecycle, config, locks, git state, or orchestration policy.

**Since this spike:** the Go TUI (now a top-level `tui/` Go module, not `lanegate/tui_spike/`) has grown well past a single board-payload prototype. It now has board, ticket detail, blocked queue, diff, orchestration-run, and settings screens (`tui/internal/screens/`). It remains mostly read-only against the boundary described below, with one exception: the settings screen can reorder a pool's executor list and persist it via `PUT /api/pools/{name}/executors`. The launch/subprocess/packaging findings below still describe the current `lanegate tui` command shape.

Startup and launch shape:

- `lanegate tui --fixture <board.json>` launches the Go spike directly against a structured board fixture.
- `lanegate tui --fixture-dir <dir>` resolves `board.json` or `board/board_basic.json` for fixture-first development.
- `lanegate tui --api-url http://127.0.0.1:<port>` connects to an existing loopback API and lets Go fetch `/api/board`.
- Plain `lanegate tui` now starts its own `lanegate api` subprocess on a selected loopback port in the background (`lanegate/tui.py:cmd_tui`), waits for it to answer, then connects the Go renderer to it. The Python API auto-start this section originally described as pending has since landed. `--no-api-start` opts out and requires `--api-url`, `--fixture`, or `--fixture-dir` instead.

Subprocess and API lifecycle findings:

- Python remains responsible for repository discovery, API startup,
  local port selection, environment overrides, and user-facing launch errors.
- The Go side is a renderer/client only: it reads a fixture, stdin, or
  `GET /api/board`, decodes JSON into DTOs, and renders a terminal view.
- Shutdown is simple in fixture/API-client mode: Python waits for the Go
  process and propagates its exit code. In the default (no fixture/`--api-url`)
  mode, Python now also owns starting and stopping the paired `lanegate api`
  subprocess around the Go TUI's lifetime.
- API URLs are constrained to loopback hosts for the local operator model.

Port selection:

- The Python launcher validates an explicit `--port` or reserves an ephemeral
  `127.0.0.1` port when no port is supplied.
- The selected port is reported before the command fails in pre-API mode, so
  future automatic startup behavior has a tested place to attach.
- A busy explicit port is a clear CLI error owned by Python.

Packaging and cross-platform notes:

- The launcher first honors `LANEGATE_TUI_BIN`, then a `lanegate-tui` binary on
  `PATH`, then `go run ./cmd/lanegate-tui` against the Go source for
  editable/dev installs. The spike source has since moved from
  `lanegate/tui_spike/` to a top-level `tui/` Go module (`tui/cmd/lanegate-tui`),
  matching the planned layout this section originally described as future
  work, see [README.md](../README.md#local-api--ui-preview).
- The Go module ships as repo source, not inside the installed wheel, a
  plain `pip install lanegate` does not provide `lanegate-tui`. Building from a
  checkout (`go build -o lanegate-tui ./tui/cmd/lanegate-tui`) or running `lanegate tui`
  from a checkout (which falls back to `go run`) are the two supported paths.
- Release packaging should still prefer per-platform `lanegate-tui` binaries
  named separately from the Python `lanegate` entry point.
- The current subprocess contract avoids shell-specific command strings and is
  suitable for Linux, macOS, and Windows. Future API startup needs explicit
  process-group/signal handling per platform when it supervises a child API
  server.

Tests:

- Go tests load `lanegate/tui_spike/testdata/board.json`, validate structured
  ticket/status/pipeline fields, and assert the renderer includes JSON-derived
  ticket and status data.
- Python CLI tests mock the subprocess boundary and cover
  `lanegate tui --fixture` argument construction plus Go process failure
  propagation.

Layout alignment:

- The spike follows the plan's fixture-first direction and preserves
  Python as the package/control-plane owner.
- The only adjustment is path shape: this spike uses `lanegate/tui_spike/` rather
  than the full future `tui/cmd/lanegate-tui/internal/...` layout to keep the
  validation small and package-local. The contract and launch behavior remain
  compatible with the planned `lanegate-tui` binary.

Recommendation:

- Keep the Go TUI launch shape: `lanegate tui` remains the Python-owned command
  that starts or connects to local structured data, then runs a separate Go
  renderer/client.
- Before a full TUI implementation, stabilize `GET /api/board` as the board
  contract and add the API startup hook behind the existing Python
  port-selection path.
- V1.5 remains unblocked. The spike confirms the boundary is practical without
  moving lifecycle or orchestration ownership out of Python.

TypeScript is appropriate for this layer because a richer UI needs typed client
state, predictable API models, editor support, and the normal ecosystem for
browser components, routing, charts, streaming logs, and diff viewers. That is
a UI decision, not a reason to move the control plane out of Python.

### UI MVP Design, superseded 2026-07-07 by TUI-first

**Decision Update (2026-07-07):** the TypeScript-first recommendation below
was the original 2026-07-04 answer. It was reversed before any
implementation started. The current, approved decision is **TUI-first,
browser-later**, see "UI Add-on Responsibilities" above and the
[Go TUI Implementation Plan](tui-plan.md). The reasoning, launch shape, and
MVP screens table below describe the deferred browser client and should not
be read as the current build order.

Recommended eventual browser stack (once a browser client is actually
built): Python local API plus a TypeScript frontend. The backend stays in
the LaneGate Python package. The frontend can start as a small Vite/React app
or an equivalent lightweight TypeScript app bundled with the package and
served by `lanegate ui`.

Reasoning:

- the API boundary is the product boundary we want long term
- TypeScript gives stable client models for tickets, runs, diffs, and logs
- browser components for diff viewing, streaming logs, filters, and tables are
  mature in the TypeScript ecosystem
- choosing TypeScript for the UI does not imply rewriting the Python core
- a Python-only HTMX/Jinja UI would be faster for a throwaway dashboard, but it
  would delay testing the API contract that V2 needs

**Confirmed:** a Python+HTMX/Jinja MVP was considered as a way to keep the
no-extra-toolchain property (no Node dependency at all), with TypeScript as a
later upgrade once screens outgrew server-rendered partials. That path was
rejected in favor of starting with TypeScript directly, the richer screens
(live log streaming, diff viewing) are core to this UI's purpose from the
start, not a later addition, so building the HTMX version first would mean
throwing it away rather than upgrading it. TypeScript remains the settled
choice for the eventual browser client, once one is built after the TUI.

Launch shape:

```text
lanegate ui
  starts the local API on 127.0.0.1
  chooses an available local port unless --port is passed
  serves the bundled UI
  opens the browser unless --no-open is passed

lanegate api
  starts only the local API for external/local clients
```

The first `lanegate ui` implementation should be a local add-on command, not a
separate product install. It should start from the current repository root,
use the same config discovery as the CLI, and keep all state in local LaneGate
files, git branches, worktrees, and logs. No cloud service, hosted project
state, account system, or SaaS workspace is part of the MVP.

Deferred browser MVP screens (once built, the TUI's own screen map lives in
[tui-plan.md](tui-plan.md#screen-to-contract-and-fixture-map)):

| Screen | MVP purpose | Primary API calls |
|---|---|---|
| Board | Group tickets by status and scan priority, milestone, dependencies, touches, branch, worktree, executor, and flags. | `GET /api/board`, `GET /api/tickets` |
| Ticket detail | Inspect frontmatter, body, close criteria, review verdict, branch/worktree links, related logs, and lifecycle actions. | `GET /api/tickets/{id}`, lifecycle `POST /api/tickets/{id}/...` |
| Needs-human-decision queue | Triage escalated, rejected, failed, stuck, and awaiting-merge tickets by reason category, checklist findings, and next actions. | `GET /api/blocked`, `GET /api/tickets/{id}` |
| Run | See current workers, ticket state, structured events, human-readable logs, and request a graceful stop. | `POST /api/runs/start`, `GET /api/runs/current`, `GET /api/runs/current/logs/stream`, `POST /api/runs/stop` |
| Diff view | Review changed files and patches for `code_complete`, `in_review`, and `needs_review` tickets. | `GET /api/diff/{id}` |
| Settings preview | Read resolved config, executor selection, safeguards, and local paths without editing them. | `GET /api/config` or a later read-only config endpoint |

Non-goals for the eventual browser UI:

- editing arbitrary repo files
- replacing the CLI for advanced/custom workflows
- cloud sync, accounts, teams, billing, or hosted state
- remote access by default
- designing a full workflow-builder UI before the API contract is stable
- managing executor installation, credentials, or provider billing
- implementing remote team collaboration or multi-machine locks

The UI should call the local API for common operations and treat API response
types as its source of truth. Reads should use JSON endpoints. Live run output
should use SSE with reconnect support. Lifecycle actions should use explicit
`POST` endpoints that return structured status and domain errors. The browser
must not parse terminal tables, ANSI output, or prose CLI messages to drive
core UI state.

CLI fallback remains a product requirement. The UI may display exact commands
for advanced or custom workflows that are not modeled yet, such as unusual
review commands, manual git recovery, executor-specific debug runs, custom
safeguard execution, or one-off maintenance. Those fallbacks should be
copyable operator guidance, not hidden implementation dependencies. The CLI
continues to be the full automation surface for scripts, headless usage, and
operations that need flags the UI has not exposed.

Follow-up implementation tickets can be split without reopening the product
boundary:

- ~~build `lanegate api` with the loopback JSON/SSE contract~~, done
  (see [Build Result](#build-result) above, a
  subset of the endpoint contract, not the full design)
- add `lanegate ui` command wiring, port selection, bundled asset serving, and
  optional browser opening
- scaffold the TypeScript app with generated or shared API types
- implement board and ticket detail screens first
- add blocked/review queue plus diff view once `GET /api/diff/{id}` exists
- add orchestration run and live logs after background run tracking exists
- add read-only settings preview after a resolved-config API endpoint exists

## Optional Runner And Sandbox Responsibilities

**Confirmed:** this layer is out of scope for V1.5, it stays deferred until a
concrete security or process-supervision requirement justifies it. The
contract below is recorded now so that if/when it is built, it fits the
boundary already established here instead of forcing a redesign of the core,
API, or UI to accommodate it later. The runner/sandbox contract should be treated as a design
reference, not scheduled V1.5 work.

A separate runner is justified only when the process boundary itself becomes
the product requirement. Good reasons include:

- supervising and cleaning up executor process trees reliably
- applying filesystem, environment, and network policy before executor launch
- producing a small runner binary with fewer Python environment assumptions
- probing executor conformance and reporting structured outcomes
- reducing the trusted surface for security-sensitive child-process handling

Rust is a good candidate for that runner because it can produce a small typed
binary and makes many classes of memory-safety bugs less likely. Rust does not
make sandboxing safe by itself. Kernel, container, or OS policy still provides
the isolation. Rust would only wrap, apply, and verify those policies.

Do not move ticket parsing, board logic, prompt templates, memory retrieval, or
ordinary executor routing to Rust without a concrete security, packaging, or
process-control reason.

### Runner/Sandbox Contract

The initial implementation remains Python. If no runner is configured, LaneGate
keeps today's executor behavior: the Python orchestrator launches the executor
as a host subprocess under the invoking OS user, then inspects the committed
git diff after the process exits. No sandbox policy is implied by this design
until a runner is explicitly configured and its outcome is wired into the
orchestration loop.

The external runner boundary is only needed when LaneGate must supervise executor
processes or apply sandbox policy more reliably than the Python orchestrator
can. The Python core is still the control plane. The runner is a narrow,
replaceable process supervisor.

Responsibilities that stay in Python core:

- ticket parsing, validation, and lifecycle state
- touches lock and dependency decisions
- prompt rendering and executor selection
- board/API/UI state
- review and merge policy
- audit/event storage
- deciding which sandbox policy to request
- interpreting runner outcomes into lifecycle decisions
- preserving ticket state when the runner itself fails

Responsibilities that may move to an external runner:

- spawning executor processes
- applying filesystem and environment policy before launch
- applying network policy where the OS/container supports it
- tracking process groups and cleaning them up
- enforcing timeout and cancellation
- writing stdout/stderr to known log destinations
- reporting policy violations and sandbox availability
- returning structured execution outcomes

Runner request schema, versioned at the boundary:

```json
{
  "schema_version": 1,
  "ticket_id": "TICK-106",
  "run_id": "run-2026-06-25-001",
  "worktree": "/abs/path/worktrees/tick-106",
  "executor": {
    "type": "codex",
    "argv": ["codex", "exec", "..."]
  },
  "environment": {
    "inherit": true,
    "overlay": {"EXAMPLE": "value"},
    "remove": ["SECRET_NOT_FOR_EXECUTOR"]
  },
  "timeout_seconds": 3600,
  "cancellation": {
    "grace_seconds": 10,
    "kill_process_tree": true
  },
  "sandbox": {
    "mode": "off",
    "allowed_read_paths": ["/abs/path/worktrees/tick-106"],
    "allowed_write_paths": ["/abs/path/worktrees/tick-106"],
    "deny_paths": ["/abs/path/.env"],
    "network": {
      "mode": "inherit",
      "allowlist": []
    }
  },
  "logs": {
    "stdout": ".lanegate/logs/TICK-106.stdout.log",
    "stderr": ".lanegate/logs/TICK-106.stderr.log",
    "events": ".lanegate/logs/TICK-106.events.jsonl",
    "truncate_bytes": 10485760
  }
}
```

Request field rules:

| Field | Owner | Meaning |
|---|---|---|
| `schema_version` | Python core | Contract version. Unknown versions are rejected before launch. |
| `ticket_id`, `run_id` | Python core | Correlation IDs for logs, events, and lifecycle decisions. |
| `worktree` | Python core | Absolute working directory for the executor. The runner must not infer it from process cwd. |
| `executor.type`, `executor.argv` | Python core | Executor identity for reporting plus the exact argv to launch. The runner does not render prompts or choose models. |
| `environment` | Python core request, runner apply | Whether to inherit the parent environment, which variables to overlay, and which inherited variables to remove. |
| `timeout_seconds` | Python core request, runner enforce | Wall-clock limit for the executor process tree. `null` means no runner-enforced timeout. |
| `cancellation` | Python core request, runner enforce | Grace period and process-tree semantics used when the core asks the runner to stop. |
| `sandbox.mode` | Python core request, runner apply/report | `off` means no isolation. `audit` records violations without blocking where possible. `enforce` blocks or fails closed if the requested policy cannot be applied. |
| `allowed_read_paths`, `allowed_write_paths`, `deny_paths` | Python core request, runner apply/report | Filesystem policy. Paths must be absolute after core-side config resolution. Denies take precedence over allows. |
| `network.mode`, `network.allowlist` | Python core request, runner apply/report | `inherit` leaves current network behavior unchanged. `blocked` requests no network. `allowlist` requests only the named hosts/CIDRs/ports. |
| `logs` | Python core request, runner write/report | Log destinations. Paths are either absolute or relative to the repo root, resolved before launch. |

Runner outcome schema:

```json
{
  "schema_version": 1,
  "ticket_id": "TICK-106",
  "run_id": "run-2026-06-25-001",
  "status": "succeeded",
  "exit_code": 0,
  "signal": null,
  "termination_reason": "exited",
  "started_at": "2026-06-25T22:00:00Z",
  "finished_at": "2026-06-25T22:10:00Z",
  "logs": {
    "stdout": ".lanegate/logs/TICK-106.stdout.log",
    "stderr": ".lanegate/logs/TICK-106.stderr.log",
    "events": ".lanegate/logs/TICK-106.events.jsonl",
    "truncated": false
  },
  "sandbox": {
    "requested_mode": "enforce",
    "applied_mode": "audit",
    "available": false,
    "engine": null,
    "reason": "bwrap unavailable",
    "network_applied": "inherit"
  },
  "policy_violations": [],
  "runner_error": null
}
```

Outcome field rules:

| Field | Meaning |
|---|---|
| `status` | One of `succeeded`, `failed`, `timed_out`, `cancelled`, `policy_blocked`, or `runner_error`. Only `succeeded` means the executor process exited successfully. It still does not mean the ticket is complete until Python validates lifecycle state and diff policy. |
| `exit_code`, `signal`, `termination_reason` | Process result. `termination_reason` should distinguish normal exit, signal, timeout kill, cancellation kill, policy block, launch failure, and runner internal failure. |
| `started_at`, `finished_at` | Runner-observed UTC timestamps for supervision and audit. If launch fails before the child starts, `started_at` may be `null`. |
| `logs` | References to stdout, stderr, and structured runner events. The runner returns references, not large inline logs. |
| `sandbox` | What was requested, what was actually applied, whether a sandbox engine was available, and any downgrade reason. |
| `policy_violations` | Structured filesystem, environment, or network policy events. In `audit` mode these may coexist with `succeeded`. In `enforce` mode they usually produce `policy_blocked` or `failed`. |
| `runner_error` | Structured runner failure, separate from executor failure. Includes code, message, and retryability when present. |

Stdout and stderr are append-only log files owned by the run. The runner should
create parent directories, stream process output into the requested paths, and
write runner lifecycle events to JSONL. It should return log references even on
timeout, cancellation, policy block, or launch failure. The Python core may
surface tails in CLI/API/UI output, but the durable contract is by log path and
event cursor, not inline text.

Timeouts are runner-enforced wall-clock limits. When the timeout expires, the
runner should signal the process group, wait for `cancellation.grace_seconds`,
kill the process tree if requested, and report `status: "timed_out"` with the
termination details. Cancellation follows the same mechanics but is initiated
by Python core or API/UI operator intent and reports `status: "cancelled"`.
Cancellation should be cooperative first and forceful second.

Sandbox and network policy are requested by the Python core and applied by the
runner only where the host platform supports it. In `off` mode the runner must
not imply isolation. In `audit` mode the runner reports unavailable engines and
observed violations without failing solely because enforcement is unavailable.
In `enforce` mode an unavailable required sandbox engine or unsupported network
policy must produce `policy_blocked` or `runner_error`. It must not silently
fall back to host-process execution.

Runner failure is not executor failure. A malformed request, launch error,
missing sandbox engine in enforce mode, log write failure, or runner crash must
return or be recorded as `runner_error` when possible. The Python core decides
how ticket state changes, and runner failure must not mark a ticket complete,
approve a review, advance to merge, or corrupt board state. At most it may
leave the ticket `in_progress`, `hibernated`, or `needs_review` with enough log
references for recovery.

Go and Rust remain optional implementation choices for this narrow boundary,
not rewrite targets. Go is justified when the needed value is a small static
supervisor with straightforward process and signal handling. Rust is justified
when the runner becomes part of the security-sensitive trusted computing base
and a small memory-safe binary is worth the added implementation cost. Neither
language makes sandboxing safe by itself. The runner still depends on kernel,
container, or OS policy mechanisms.

TypeScript+Bun is not the preferred runtime for executor supervision. It is a
good fit for API/UI/schema-heavy surfaces and browser-adjacent tooling, but a
runner needs predictable process-tree control, signal handling, and a minimal
trusted surface more than it needs frontend-language ergonomics.

Proposed target-layer updates for existing sandbox and safety tickets:

| Feature | Proposed target layer | Why |
|---|---|---|
| Executor sandbox | Optional runner/sandbox plus Python core config | Policy must be configured in Python, but enforcement belongs at the runner boundary. |
| Tighten bwrap read profile | Optional runner/sandbox | This is enforcement detail for filesystem visibility. |
| Network sandbox | Optional runner/sandbox | Network isolation is process/OS policy, not board logic. |
| Git operation audit | Python core first, runner later if needed | Diff/git audit is currently a core post-executor control. |
| Scope pinning | Python core | This compares intended scope against resulting diff and prompts. |
| Ticket content sanitization | Python core | Ticket parsing and prompt safety stay in the control plane. |

## Retargeting Older V2 Tickets

Before implementing older V2 tickets, add a short `Target Layer` section to
each affected ticket. Use one of these values unless the ticket genuinely spans
multiple layers:

- Python core
- Python local API
- UI add-on
- optional runner/sandbox
- documentation only

Tickets that mention Python files may still target the Python core. Tickets
that describe operator experience, live logs, visual review, or diff browsing
probably belong to the UI add-on plus local API. Tickets that describe process
containment, network isolation, or executor launch policy may belong to the
runner/sandbox layer.

No older V2 implementation ticket should start until its target layer is
explicit. This keeps the backlog from accidentally forcing repeated rewrites as
LaneGate grows from a terminal-first CLI into a local project control plane with
an optional UI and, later, a hardened runner.

## Review Packet

Use this packet for the design discussion before starting the dependent tickets:

| Scope | Decision to confirm | Confirmed answer |
|---|---|---|
| Layer boundaries | What are the V1.5 layer boundaries? | Python remains the control plane. Local API exposes structured operations. A UI add-on sits over it. A runner remains optional for sandbox/process supervision only. |
| Local API | What local API should the UI use? | Loopback-only Python API with JSON endpoints and SSE logs, backed by shared core service functions rather than CLI scraping. |
| UI shape | What is the first UI product shape? | **Revised 2026-07-07:** TUI-first, browser-later, a Go terminal console over Python-owned JSON/SSE contracts ships first (see [Go TUI Implementation Plan](tui-plan.md)). The TypeScript frontend (`lanegate ui`) originally proposed here is deferred to a later browser client over the same contracts, not the first UI. |
| Runner/Sandbox | When does a Rust/Go runner enter, and is it in V1.5 scope? | Out of V1.5 scope, deferred until a concrete security/process-supervision need justifies it. If built: Rust preferred for a security-sensitive process/sandbox boundary, Go a reasonable fallback for pure process supervision. |

All four answers are settled. These decisions can become detailed
docs or implementation tickets without reopening the language/runtime debate.
