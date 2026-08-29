# Command Reference

`lanegate --help` starts with the daily workflow: `board`, `next`, `create`,
`start`, `complete`, `review`, `merge`, and `run`. It then groups the
less-frequent lifecycle/recovery, monitoring, setup/integration, and
agent/orchestrator commands. Run `lanegate --help-all` (or `lgt --help-all` / `lane --help-all`)
for the complete flat command list, including compatibility commands.

See the [README Quick start](../README.md#quick-start) for the common day-to-day flow
(`board`, `next`, `create`, `start`, `complete`, `review`, `merge`, `run`). This page covers
everything else: planning/reporting, the full lifecycle, deployment, feature flags, MCP,
the local API/UI, the TUI, monitoring daemons, and setup utilities.

## Board & planning

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

## Ticket lifecycle

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

# Recovery
lanegate reopen TICK-007          # send a failed/needs_review ticket back to open
lanegate reset TICK-007           # discard a stuck non-terminal ticket's worktree + branch and
                                  #   clear its verdict, so it re-implements from scratch. Refuses
                                  #   terminal tickets (merged/done/failed/closed); only removes a
                                  #   worktree that matches the ticket's own canonical path + branch.
```

`create` derives a short board title from the intent when `--title` is omitted.
Pass `--title` to preserve a distinct, full title on the board; long titles wrap
rather than being shortened there. `--autonomy full`, `--autonomy supervised`,
`--autonomy manual`, `--autonomy green`, `--autonomy yellow`, or `--autonomy red`
sets that ticket's policy and overrides the project-level `autonomy` default.
Green and yellow follow the automatic path like full; red always requires human
escalation. The default remains `supervised` unless the project config sets
another value.

See [docs/lifecycle.md](lifecycle.md) for the full state machine — every status,
what blocks each transition, where a ticket lands when a guard or review fails,
and how many review rounds run.

## Deployment

```bash
lanegate promote staging          # guard → pre-promote → sync → post-promote
lanegate promote production
lanegate pipeline-status          # see what's pending where
```

## Feature flags

```bash
lanegate flag list                        # show all flags (global)
lanegate flag list --env staging          # show flags for 'staging' environment
lanegate flag enable  new_checkout_flow --env staging
lanegate flag disable new_checkout_flow --env production
```

## MCP server

```bash
lanegate mcp    # start stdio MCP server; attach any MCP-compatible client
```

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

The server starts on stdio, no shell commands required. Exposed tools include
`board`, `next_ticket`, `pipeline_status`, `flag_list`, `flag_set`, `repo_status`,
`recent_logs`, `continuation_context`, `start`, `run`, `complete`, `review`, `merge`,
`promote`, `hibernate`, `needs_review`, `fail`, `reopen`, `validate`, `done`, and `stats`.
The older `orchestrate` MCP tool remains as a compatibility alias. Agent-facing action
and log tools are bounded: lifecycle output is byte-capped, log excerpts are line- and
byte-capped, and continuation state is reconstructed from durable repo data instead of
chat context.

Run `lanegate install-agent-tools` to write Claude slash commands and reusable Codex or
generic MCP config snippets into the repo. See [docs/agent-tools.md](agent-tools.md)
for the supported agent surfaces and continuation guarantees.

## Local API / UI preview

`lanegate api` is built: a loopback-only (`127.0.0.1`) JSON/SSE server over board, ticket, diff, and LaneGate run state. `lanegate ui` (a bundled UI served by that API) is still planned:

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
- `PUT /api/pools/{name}/executors` (reorders a pool's executor list)
- `GET /api/runs`
- `GET /api/runs/current`
- `GET /api/runs/current/logs`
- `GET /api/runs/current/logs/stream`
- `POST /api/runs/start`
- `POST /api/runs/stop`

`/api/orchestrate/start` and `/api/orchestrate/stop` remain backward-compatible aliases.

Per-ticket lifecycle mutation endpoints (`start`/`complete`/`review`/`merge`) are
still design-only. See
[docs/v2-interface-boundaries.md](v2-interface-boundaries.md).

`lanegate ui` will use this API for common board, ticket, diff, review, and
run operations once built. Advanced or executor-specific workflows
stay available through the CLI instead of being forced into the browser.

## Terminal UI (TUI)

A Go TUI is also available via `lanegate tui`, rendering board, ticket detail,
blocked queue, diff, run, and settings screens from a fixture or
the local API. It is mostly read-only, with one exception: the settings
screen can reorder a pool's executor list and persist it via
`PUT /api/pools/{name}/executors`. See
[docs/v2-interface-boundaries.md](v2-interface-boundaries.md#tick-118-go-tui-runtime-spike-result)
for its current scope and limits.

`lanegate tui` is a separate Go binary, not part of the `lanegate` Python package. A
plain `pip install` does not give you `lanegate-tui`. To use it:

- **Build from source** (this repo ships the Go module at `tui/`): `go build -o
  lanegate-tui ./tui/cmd/lanegate-tui`, then either put `lanegate-tui` on your `PATH` or set
  `LANEGATE_TUI_BIN=/path/to/lanegate-tui`.
- **Run without building**, if you have the Go toolchain and a checkout of this repo:
  `lanegate tui` falls back to `go run ./cmd/lanegate-tui` automatically when no
  `lanegate-tui` binary is found on `PATH` or via `LANEGATE_TUI_BIN`.

If neither a binary nor the Go source is available, `lanegate tui` exits with an error
telling you to set `LANEGATE_TUI_BIN`. See
[Troubleshooting](troubleshooting.md#lanegate-tui-fails-go-tui-binary-or-source-not-found).

## Monitoring & auto-resume

Three detached background daemons cover runs you're not watching live, each with its own PID/log file under `.lanegate/` and a `--status`/`--stop` pair:

```bash
lanegate watch                  # poll PR review decisions, auto-merge on approval
lanegate resume-watch           # wait out a rate limit on backoff, auto-retry `lanegate run`
lanegate resume-watch --history # what happened: hibernated -> retrying -> resumed/gave_up, with timestamps
lanegate notify-watch --test        # send a test phone push (ntfy.sh), verify setup
lanegate notify-watch --background  # push a phone alert when a run looks stuck (dead process, stale
                                     # heartbeat, or halted with tickets waiting); detaches, survives this
                                     # terminal closing
lanegate notify-watch --once --json # run stuck-ticket detection once, print a structured record
                                     # (empty list + exit 0 when nothing is stuck), no daemon — for
                                     # cron/CI health checks
```

See [Rate limits and auto-resume](config-reference.md#rate-limits-and-auto-resume) and [Phone alerts for stuck runs](config-reference.md#phone-alerts-for-stuck-runs-notify-watch) in the config reference for configuration and setup, including running `notify-watch` under systemd so it survives reboots.

## Utilities

```bash
lanegate init             # scaffold .lanegate/ + .lanegate.yml
lanegate audit-refactor   # scan for oversized modules and emit a dependency-ordered
                          #   draft-ticket DAG to split them (--threshold LINES, --path DIR,
                          #   --milestone M)
lanegate install-agent-tools # install Claude commands plus Codex/generic MCP snippets
lanegate install-commands # compatibility alias: copy Claude slash commands only
lanegate gh-sync          # mirror tickets to GitHub Issues (manual visibility sync; read-only view, not bidirectional)
lanegate gh-sync --dry-run  # preview what would be created or updated
```
