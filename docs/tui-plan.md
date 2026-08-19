# Go TUI Implementation Plan

This document turns the V1.5 interface-boundary decision into a concrete plan for the first Go terminal UI. The TUI is an operator console over Python-owned JSON and SSE contracts, and it must not parse terminal tables, import Python internals, or decide repo-local lifecycle policy.

The first pass is read-only except for local navigation and refresh controls. Mutating lifecycle actions can be shown as disabled or copied CLI commands until the Python API contracts for those actions are implemented and tested. TICK-269 is the first narrow exception: a `pools.<name>.executors` reorder control on the settings screen backed by `GET`/`PUT /api/pools`. That config is inert without a stable, meaningful effect until TICK-268's pool rotation state persists across runs, so everything else on the screen stays read-only.

## Module and Package Layout

Start with a separate Go module under `tui/` so the Python package remains the control plane and the Go dependency tree stays isolated:

```text
tui/
  go.mod
  go.sum
  cmd/lanegate-tui/
    main.go
  internal/app/
    model.go
    update.go
    view.go
    keys.go
  internal/client/
    client.go
    fixtures.go
    sse.go
    types.go
  internal/screens/
    board.go
    ticket.go
    blocked.go
    diff.go
    run.go
    settings.go
  internal/ui/
    styles.go
    table.go
    viewport.go
    statusbar.go
  testdata/
    contracts/
      README.md -> ../../../tests/fixtures/tui_contracts/README.md
```

Package responsibilities:

| Package | Owns | Must not own |
|---|---|---|
| `cmd/lanegate-tui` | CLI flags, process exit, initial config, wiring the Bubble Tea program. | Python lifecycle policy, ticket parsing, git operations. |
| `internal/app` | Top-level Bubble Tea model, routing, key handling, refresh state, error display. | Endpoint schemas beyond using typed client models. |
| `internal/client` | HTTP client, fixture client, SSE reader, generated or hand-written API DTOs. | Terminal rendering or business decisions. |
| `internal/screens` | Screen models and views for board, ticket detail, blocked queue, diff, run, settings. | Direct filesystem reads except fixture loading in tests. |
| `internal/ui` | Shared Lip Gloss styles, Bubbles wrappers, formatting helpers. | API calls or lifecycle actions. |

Keep Python-owned contracts in Python or fixture files. Go types may mirror those contracts, but the source of truth is the API schema and fixture corpus, not terminal output or Go-only assumptions.

## Library Choice

Recommendation: use the Charm stack:

- Bubble Tea for the Model-Update-View event loop.
- Lip Gloss for layout and theme styling.
- Bubbles for tables, lists, viewports, spinners, text inputs, and help views.

Why it fits LaneGate:

- It is Go-native and produces a single terminal binary that can be paired with the existing Python package.
- The update loop maps cleanly to refresh ticks, HTTP responses, SSE events, and keyboard navigation.
- Bubbles provides enough read-only widgets for board tables, ticket lists, log viewports, and diff panes without building all primitives from scratch.
- It keeps the first TUI local-first and terminal-native while sharing the same Python JSON/SSE boundary planned for richer UI surfaces.

Shortlist considered:

| Library | Decision | Reason |
|---|---|---|
| Bubble Tea/Lip Gloss/Bubbles | Choose first. | Best mix of mature Go TUI primitives, event model, styling, and single-binary packaging. |
| tview/tcell | Do not choose for first pass. | Strong widgets, but a less explicit app update model for LaneGate's API/SSE state flow. |
| termui | Do not choose. | Useful for dashboards, but weaker for multi-screen operator workflows. |

## Keyboard and Navigation Model

Use one global navigation model and screen-local keys:

| Key | Global behavior |
|---|---|
| `q`, `ctrl+c` | Quit. |
| `?` | Toggle help. |
| `r` | Refresh the current screen from the active client. |
| `tab`, `shift+tab` | Move focus between screen panes. |
| `1` through `6` | Jump to the first six MVP screens. |
| `esc` | Close modal/help, then return to the previous screen. |

Screen-local keys should stay predictable:

- `up/down`, `j/k`: move selection.
- `enter`: open selected item.
- `left/right`, `h/l`: switch adjacent pane or tab where applicable.
- `pgup/pgdn`, `home/end`: scroll long body, log, and diff viewports.
- `/`: focus local search or filter once that screen has a filter model.

The first pass should not add write actions behind single-key shortcuts. When mutations are later enabled, use confirmation prompts and call Python API endpoints that return structured status and domain errors.

## Implementation-Ordered Screens

Build screens in this order so each step validates a larger part of the contract surface without depending on unfinished orchestration APIs:

1. Board: read grouped ticket status, pipeline summary, and ticket rows.
2. Ticket detail: inspect metadata, body, close criteria, review, links, and related files for the selected ticket.
3. Needs-human-decision queue: triage escalations, rejected reviews, failures,
   non-rate-limit hibernations, and approved tickets awaiting a human merge.
   The shared screen chrome shows the queue count from the Board payload.
4. Diff view: inspect changed files and patches once the Python diff contract exists.
5. Orchestration run: show current run state and consume live SSE events.
6. Settings preview: show resolved config, repository paths, and API metadata
   after a read-only config endpoint exists.

The read-only MVP can ship after screens 1 through 3 work against fixtures and the current JSON output. Screens 4 through 6 can be fixture-first until their API endpoints land. As of TICK-157, all three exist (`GET /api/diff/{id}` and `GET /api/runs/current`/`.../logs/stream` from TICK-146, `GET /api/config` added by TICK-157 itself), and all six MVP screens are implemented and navigable via the `1`-`6` keys.

## Screen-to-Contract and Fixture Map

Fixtures live under `tests/fixtures/tui_contracts/`. Each fixture payload is a
Python-owned contract example that the Go client and renderer can consume
without shelling out to `lanegate` or importing Python code.

| Screen | Python-owned contract | Fixture path | Payload names |
|---|---|---|---|
| Board | `GET /api/board` and `GET /api/tickets` when available. Before `lanegate api`, use the existing board JSON shape from `lanegate board --json`. | `tests/fixtures/tui_contracts/board/` | `board_basic.json`, `board_empty.json`, `board_hidden_and_blocked.json`, `tickets_flat.json` |
| Ticket detail | `GET /api/tickets/{id}`. Before API, define the expected response from ticket loader output plus rendered markdown body. | `tests/fixtures/tui_contracts/ticket_detail/` | `ticket_ready.json`, `ticket_in_review.json`, `ticket_changes_requested.json`, `ticket_missing_optional_fields.json` |
| Needs-human-decision queue | `GET /api/blocked` plus selected `GET /api/tickets/{id}` detail; rows carry `attention_category` and `attention_summary`, while Board rows carry `needs_attention` for the shared count. | `tests/fixtures/tui_contracts/blocked/` | `blocked_queue.json`, `changes_requested_queue.json`, `blocked_empty.json` |
| Diff view | `GET /api/diff/{id}`. Defines truncation metadata (`truncated`, `stat`) instead of sending unbounded patches. | `tests/fixtures/tui_contracts/diff/` | `diff_small.json`, `diff_many_files.json`, `diff_truncated_patch.json`, `diff_binary_file.json`, `diff_empty.json` |
| Orchestration run | `GET /api/runs/current` plus TICK-307's safe `GET /api/runs/{id}/events` activity envelope. The default Run pane renders only those compact structured events. Paginated `GET /api/runs/{id}/logs` and the current-run log SSE stream are reserved for the explicit Raw Audit Log diagnostic mode. Renders as a single-worker view until TICK-089 formalizes multi-instance worker-pool aggregation. The `resume_watch_status` field (added by TICK-169) surfaces the background resume-watch daemon state as a separate status line when present. Three states are modelled: `waiting` (daemon pausing between retries), `gave_up` (daemon hit ceiling), and absent (daemon not running). | `tests/fixtures/tui_contracts/run/` | `run_idle.json`, `run_active.json`, `run_completed.json`, `executor_events_live.json`, `executor_events_historical.json`, `raw_audit_page.json`, `events_basic.sse`, `events_reconnect.sse`, `events_worker_error.sse`, `resume_watch_absent.json`, `resume_watch_waiting.json`, `resume_watch_gave_up.json` |
| Settings preview | `GET /api/config` (also served at `/api/settings`), read-only, sanitized (credential-shaped fields redacted server-side). Added by TICK-157. | `tests/fixtures/tui_contracts/settings/` | `settings_basic.json`, `settings_missing_optional.json`, `settings_multi_executor.json` |
| Pool executor reorder | `GET /api/pools` (executors per pool in preference order, plus TICK-268's persisted rotation/dispatch state) and `PUT /api/pools/{name}/executors` (persists a reordering to `.lanegate.yml` and rejects anything that isn't a reordering of the pool's existing executor set). Rendered as a subsection of the settings screen. Added by TICK-269. | `tests/fixtures/tui_contracts/pools/` | `pools_basic.json`, `pools_empty.json` |
| API errors | Shared structured error response used by every screen. | `tests/fixtures/tui_contracts/errors/` | `error_locked_touch.json`, `error_missing_ticket.json`, `error_remote_behind.json`, `error_worktree_error.json` |

Fixture conventions:

- JSON fixtures should contain full response envelopes, not only row arrays.
- SSE fixtures should be valid `text/event-stream` examples with `id:`, `event:`, and `data:` fields.
- Paths inside payloads should be repo-relative unless the API contract explicitly requires an absolute local path.
- Hostile or surprising text belongs in string fields so the renderer proves it displays untrusted ticket content rather than executing or interpreting it.

## Pre-API Work

The TUI can begin before `lanegate api` is complete if it uses a fixture-first client boundary:

1. Define Go DTOs that mirror the fixture payloads listed above.
2. Implement a `Client` interface with `HTTPClient` and `FixtureClient` implementations.
3. Build screen models and rendering against `FixtureClient`.
4. Add snapshot or golden rendering tests for narrow terminal sizes and common empty/error states.
5. Wire `--fixture-dir tests/fixtures/tui_contracts` for local development.
6. Connect `HTTPClient` screen by screen as Python endpoints land.

Allowed pre-API inputs:

- fixture JSON and SSE files from `tests/fixtures/tui_contracts/`
- existing JSON CLI output only when it is already structured and explicitly
  used as a temporary source

Disallowed pre-API inputs:

- parsing terminal tables, ANSI output, or prose CLI messages
- importing Python package internals from Go
- duplicating ticket lifecycle, lock, merge, review, or orchestration policy in Go

## Live Agent Observability

TICK-148 made executor audit bundles durable after an executor invocation, but the TUI should treat that as the persistence layer, not as the user-facing experience. Operators should not need to know Claude temp directories, Codex session paths, or LaneGate's internal status files to understand what is happening.

What the executors expose today:

| Executor | Available signal today | Current LaneGate capture | TUI interpretation |
|---|---|---|---|
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` includes structured events such as session metadata, assistant progress messages, tool calls, tool outputs, token-count updates, and final task-complete messages. | TICK-148 copies the matching rollout into `.lanegate/executor-runs/<ticket>/<session>/executor-session.jsonl` when discoverable. Codex does not have Claude-style `tasks/*.output` files. | Parse into a timeline of messages, commands, outputs, test results, errors, and completion state. Treat any nested-agent/subtask detail as best-effort transcript content unless Codex exposes a stable worker API. |
| Claude Code | `~/.claude/projects/<encoded-cwd>/<session>.jsonl` includes session events, user/assistant messages, tool use, attachments, agent/tool listings, sidechain markers, interruptions, and hook summaries. Claude background task output can also appear under `/tmp/claude-*/<encoded-cwd>/<session>/tasks/*.output`. | TICK-148 copies the matching transcript plus bounded task outputs when discoverable, and records missing artifacts in `manifest.json`. | Parse transcript messages and task-output files into one timeline. Surface sidechains/tasks as nested activity when present, but do not assume LaneGate can control Claude's internal agents individually. |

The TUI should not render raw JSONL as the primary experience. Run (5) presents only the live orchestration run, while Run History (6) owns completed-run selection and drilldown; Settings is screen 7. Their default Activity panes consume the safe structured `/api/runs/{id}/events` contract. Raw executor output remains available only in the explicit, paginated Raw Audit Log mode. The Python API should normalize executor-specific events into a small LaneGate-owned stream. With `max_parallel > 1`, executor status is tracked per-session in `.lanegate/active-orchestrate/<session_id>.json` rather than a single shared file, so concurrent executors do not clobber each other's status:

```json
{
  "id": "evt-00123",
  "run_id": "run-20260709-143500",
  "worker_id": "TICK-149:implement",
  "ticket_id": "TICK-149",
  "executor": "codex",
  "timestamp": "2026-07-09T21:38:44Z",
  "type": "tool_output",
  "summary": "pytest focused suite passed",
  "artifact": ".lanegate/executor-runs/TICK-149/.../executor-session.jsonl",
  "data": {
    "tool": "pytest",
    "exit_code": 0,
    "truncated": false
  }
}
```

Recommended first event types:

| Event type | Meaning |
|---|---|
| `worker_started` | LaneGate launched an executor for a ticket/step. |
| `heartbeat` | The executor is still running, and includes elapsed time and PID liveness. |
| `agent_message` | Executor progress message or assistant-visible summary. |
| `tool_call` | Command/tool invocation began. |
| `tool_output` | Command/tool output or result arrived. |
| `gate_result` | LaneGate-owned gate output such as static analysis, touched-file guard, or safeguards. |
| `artifact_captured` | Prompt/transcript/task-output/git/diff artifact was written or updated. |
| `intervention_requested` | Operator asked LaneGate to stop or redirect a worker. |
| `worker_stopped` | Worker stopped, hibernated, failed, reached review, or completed. |

The first TUI can start with post-run artifacts and live log streaming, then upgrade to live normalized events as the Python API grows. The Go TUI should consume only `GET /api/runs/current`, `GET /api/runs/current/logs/stream`, and future event/artifact endpoints. It should not open `~/.codex`, `~/.claude`, `/tmp/claude-*`, or `.lanegate/active-orchestrate.json` directly.

### Parallel Worker View

The target operator screen is a multi-worker console, not a single flat log:

```text
Workers
  TICK-149 implement codex   done       5m03s  tests passed
  TICK-150 implement claude  running    1m42s  editing
  TICK-151 review    codex   waiting    0m00s  queued

Activity
  [TICK-150] agent_message  Reading reviewer.py and analyze.py
  [TICK-150] tool_call      pytest tests/test_reviewer.py
  [TICK-149] gate_result    static analysis: clean
```

This requires the Python core to expose a per-run/per-worker model instead of a single active executor marker. A future API contract should persist:

```text
.lanegate/runs/<run-id>/run.json
.lanegate/runs/<run-id>/events.jsonl
.lanegate/runs/<run-id>/workers/<worker-id>.json
```

Each worker record should include ticket id, step, executor, process id, session id, worktree, prompt path, audit bundle path, state, last event, heartbeat timestamps, log offsets, and stop/intervention state.

### Operator Intervention

Do not make the first TUI depend on interactive stdin for Claude or Codex. The portable control-plane primitive should be:

1. request stop for one worker or the whole run
2. preserve the worktree and audit bundle
3. write an operator note under `.lanegate/interventions/<ticket-id>/`
4. resume with that note injected into the next prompt

This gives the operator a reliable way to say "you are going in the wrong direction" without relying on executor-specific interactive protocols. If an executor later exposes a stable live-control API, LaneGate can add an adapter for that executor while keeping stop/note/resume as the common behavior.

## Language Choice After Observability Work

The live observability work does not change the language split:

- Python remains the control plane and API owner because it already owns tickets, locks, subprocess execution, audit bundles, and lifecycle policy.
- Go remains the first TUI choice because Bubble Tea/Lip Gloss/Bubbles fit a terminal-native operator console with tables, panes, streaming logs, and keyboard navigation.
- TypeScript remains the natural browser UI choice later.
- Rust or Go remains appropriate only for a future runner/sandbox boundary, where process-tree control or security isolation becomes the product need.

The TUI should therefore make parallel agent activity readable, but it should not move orchestration policy out of Python or choose Go/Rust for the core.

## `lanegate tui` Launch and Packaging

Target command:

```text
lanegate tui [--api-url URL] [--fixture-dir PATH] [--no-api-start] [--port PORT]
```

Launch behavior:

1. If `--fixture-dir` is passed, start the Go TUI against fixtures and do not start the Python API.
2. If `--api-url` is passed, connect to that API and do not start another server.
3. Otherwise, the Python `lanegate tui` command starts or reuses a loopback `lanegate api` server on `127.0.0.1`, then launches the paired `lanegate-tui` binary with the chosen API URL.
4. `--no-api-start` requires `--api-url` and fails with a structured CLI error if the URL is missing or unreachable.

Packaging approach:

- Keep the Go binary named `lanegate-tui` to avoid confusing it with the Python `lanegate` entry point.
- Build release binaries for the supported platforms and include them as package data or downloadable companion artifacts.
- In editable/dev installs, allow `LANEGATE_TUI_BIN=/path/to/lanegate-tui` so Python command wiring can run a locally built binary.
- The Python package owns server startup, repository discovery, config loading, port selection, and user-facing `lanegate tui` command errors.
- The Go binary owns terminal rendering, keyboard interaction, API polling, and SSE consumption.

This keeps the Python package as the product entry point while letting the TUI ship as a focused terminal binary.

## Test Strategy

Contract fixture tests:

- Validate every JSON fixture against the Python API schema once schema objects exist.
- Validate SSE fixture framing and JSON `data:` payloads.
- Keep one fixture per important empty, partial, error, and truncation state.

Go client tests:

- Load every fixture path listed in this plan through `FixtureClient`.
- Decode HTTP responses and SSE events into the same DTOs used by fixtures.
- Cover domain error responses without relying on text matching.

Rendering tests:

- Use golden snapshots for each screen at common terminal sizes such as `80x24`, `120x32`, and a narrow `60x20` case.
- Include empty states, long titles, long file paths, review findings, and hostile body text.
- Assert key screen labels and selected rows are visible without requiring exact ANSI escape sequences for every style.

Command wiring tests:

- Python tests should cover `lanegate tui --fixture-dir`, `lanegate tui --api-url`, missing binary errors, and paired API startup argument construction.
- Go tests should cover flag parsing for the standalone `lanegate-tui` binary.
- Integration smoke tests can run the binary against fixture files without starting a Python API.

Manual verification for the first implementation ticket:

- Run `lanegate tui --fixture-dir tests/fixtures/tui_contracts`.
- Open board, ticket detail, blocked queue, and help.
- Confirm refresh, navigation, resize, and quit keys work.
- Confirm no screen shells out to CLI commands during fixture mode.

## Non-Goals

The first TUI pass does not include:

- replacing the browser UI plan in TICK-108
- rewriting the Python core, local API, ticket parser, or lifecycle commands
- owning repo-local lifecycle decisions in Go
- mutating tickets, reviews, merges, orchestration runs, or config from the TUI
- remote access, multi-user sessions, cloud sync, accounts, or hosted state
- parsing human terminal output as a data contract
- implementing a full diff engine in Go
- managing executor installation, credentials, quotas, or provider billing
- building a workflow designer or custom automation editor

## V2 Follow-on Delivery Map

The first six inspection screens, structured Run activity/audit mode, and the narrow pool-order edit are complete. The next work should ship as the following vertical slices rather than reopening the UI architecture:

1. TICK-336: keyboard search/filtering and deterministic selected-ticket navigation over the existing read contracts.
2. TICK-215, then TICK-337 and TICK-338: secure the loopback API's CORS/token boundary before adding Python-owned per-ticket lifecycle mutation contracts, then add confirmation-gated TUI controls that consume only those contracts.
3. TICK-339 then TICK-340: a durable per-worker intervention contract (stop, note, later resume) followed by its TUI operator experience.
4. TICK-341: package the paired launcher/binary and prove a fixture and live loopback API workflow end to end.

The mutation and intervention tickets remain explicitly confirmation-gated: the Go client renders state and submits structured requests, while Python continues to own lifecycle, lock, review, merge, and process policy.
